"""
EVM-сети (Ethereum, BNB Chain, Base, Arbitrum, Polygon, Avalanche) — раздел
3.3 ТЗ. Баланс — через `eth_getBalance` на публичный RPC, ключ не нужен.
Etherscan V2 (единый ключ на все 6 сетей через chainid) остаётся опцией для
ДОПОЛНИТЕЛЬНЫХ данных (история транзакций и т.п.), но не для самого баланса —
см. раздел 8 ("Ключи API").

Публичные RPC ниже — официальные/широко используемые бесплатные эндпоинты
каждой сети. У публичных нод бывают перебои — при желании можно подставить
свой (Ankr/Infura/Alchemy free tier) через параметр rpc_url.

ИСПРАВЛЕНО (реальный баг из лога пользователя 08.08.2026): get_balance делал
РОВНО один requests.post без единой попытки повтора. В логе — ~60 подряд
идущих SSLError ("EOF occurred in violation of protocol", т.е. соединение
оборвалось на этапе TLS-рукопожатия) при обращении к eth.llamarpc.com, и
КАЖДЫЙ адрес из-за этого молча пропускался. Теперь для каждой сети — до 3
узлов, и внутри каждого — до 2 попыток с экспоненциальной паузой через
with_backoff.

СЕДЬМОЙ РАУНД (08.08.2026):
1. HTTP 429 распознаётся отдельно от обрыва соединения (RateLimited, не
   BalanceCheckError — см. rate_limit/guard.py, запрос пользователя п.4).
   Публичные RPC обычно не именные (нет "ключа" для ротации) — 429 здесь
   почти всегда означает "пробуем следующий узел из fallback-списка", а
   если 429 пришёл на ПОСЛЕДНЕМ узле — RateLimited улетает в pipeline.py
   для паузы с таймером, а не тонет в общем "все узлы недоступны".
2. get_known_token_balances() — "хотя бы часть активов" сверх нативного ETH
   (запрос пользователя п.2: бесплатно, без ключа, без привязки карты).
   Прямой eth_call к balanceOf(address) на КОНКРЕТНЫХ, заранее известных
   адресах контрактов стейблкоинов — без стороннего индексатора (Etherscan/
   Alchemy/Moralis и т.п. не нужны, всё через тот же публичный RPC, что и
   для нативного баланса). Ограничение метода: работает только для токенов
   из списка ниже (нельзя "перечислить все токены" одним RPC-вызовом на
   EVM, в отличие от Solana — см. solana.py); список — сознательно короткий
   и составлен из адресов, сверенных по Etherscan (см. комментарий у списка).
"""

from __future__ import annotations

import logging

import requests

from adapters.balances.base import BalanceAdapter, BalanceCheckError
from config import settings
from rate_limit.guard import RateLimited, parse_retry_after, with_backoff

logger = logging.getLogger("steal_bones.adapters.evm")

WEI_PER_NATIVE = 10**18
BALANCE_OF_SELECTOR = "0x70a08231"  # keccak256("balanceOf(address)")[:4] — стандарт ERC-20, стабилен с 2015г.

# network_key -> (chain_id для Etherscan V2, список публичных RPC — первый
# пробуем первым, остальные это резерв на случай отказа/перегрузки первого,
# имя нативной монеты). ЧЕСТНО: резервные адреса — известные публичные
# эндпоинты каждой сети по памяти, а не проверенный вживую список "что
# реально быстрее всего ответит прямо сейчас" — если резервный узел тоже
# окажется недоступен, ошибка будет содержать точный URL, по которому легко
# сверить/заменить его.
EVM_CHAINS: dict[str, dict] = {
    "ethereum": {
        "chain_id": 1, "native": "ETH",
        "rpc": ["https://eth.llamarpc.com", "https://ethereum.publicnode.com", "https://cloudflare-eth.com"],
    },
    "bnb": {
        "chain_id": 56, "native": "BNB",
        "rpc": ["https://bsc-dataseed.binance.org", "https://bsc.publicnode.com", "https://bsc-dataseed1.defibit.io"],
    },
    "base": {
        "chain_id": 8453, "native": "ETH",
        "rpc": ["https://mainnet.base.org", "https://base.publicnode.com", "https://base.llamarpc.com"],
    },
    "arbitrum": {
        "chain_id": 42161, "native": "ETH",
        "rpc": ["https://arb1.arbitrum.io/rpc", "https://arbitrum.llamarpc.com", "https://arbitrum-one.publicnode.com"],
    },
    "polygon": {
        "chain_id": 137, "native": "POL",
        "rpc": ["https://polygon-rpc.com", "https://polygon.llamarpc.com", "https://polygon-bor.publicnode.com"],
    },
    "avalanche": {
        "chain_id": 43114, "native": "AVAX",
        "rpc": ["https://api.avax.network/ext/bc/C/rpc", "https://avalanche-c-chain.publicnode.com"],
    },
}

# ПРОВЕРЕНО через etherscan.io (08.08.2026) — только Ethereum mainnet, только
# самые ходовые стейблкоины. Формат: contract_address -> (symbol, decimals).
# Расширять на другие сети/токены — по мере необходимости, тем же способом
# (сверить адрес и decimals по официальному Etherscan/аналогичному
# эксплореру сети перед добавлением, не угадывать).
KNOWN_ERC20_TOKENS: dict[str, dict[str, tuple[str, int]]] = {
    "ethereum": {
        "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48": ("USDC", 6),
        "0xdAC17F958D2ee523a2206206994597C13D831ec7": ("USDT", 6),
    },
}


class EvmBalanceAdapter(BalanceAdapter):
    requires_key = False

    def __init__(self, network: str, rpc_url: str | None = None):
        if network not in EVM_CHAINS:
            raise ValueError(f"Неизвестная EVM-сеть: {network}. Ожидается одна из {list(EVM_CHAINS)}")
        self.network = network
        self.chain_id = EVM_CHAINS[network]["chain_id"]
        # Явно заданный rpc_url — уважаем как единственный вариант (без
        # автопереключения — если пользователь указал свой узел, это осознанный
        # выбор). Иначе — весь список публичных узлов по очереди.
        self.rpc_urls: list[str] = [rpc_url] if rpc_url else list(EVM_CHAINS[network]["rpc"])

    def _rpc_call(self, rpc_url: str, payload: dict) -> dict:
        def _do_request():
            resp = requests.post(
                rpc_url, json=payload,
                headers={"User-Agent": settings.user_agent, "Content-Type": "application/json"},
                timeout=10,
            )
            if resp.status_code == 429:
                raise RateLimited(
                    f"{self.network} RPC: 429 на {rpc_url}",
                    retry_after=parse_retry_after(resp.headers.get("Retry-After")),
                    source=self.network,
                )
            return resp

        resp = with_backoff(_do_request, retries=2, base_delay=0.6, retry_on=(requests.ConnectionError, requests.Timeout))
        resp.raise_for_status()
        return resp.json()

    def _get_balance_from(self, rpc_url: str, address: str) -> float:
        data = self._rpc_call(rpc_url, {
            "jsonrpc": "2.0", "id": 1,
            "method": "eth_getBalance",
            "params": [address, "latest"],
        })

        if "error" in data:
            raise BalanceCheckError(f"{self.network} RPC ({rpc_url}) вернул ошибку для {address}: {data['error']}")

        result_hex = data.get("result")
        if result_hex is None:
            raise BalanceCheckError(f"{self.network} RPC ({rpc_url}): неожиданный формат ответа для {address}: {data}")

        return int(result_hex, 16) / WEI_PER_NATIVE

    def get_balance(self, address: str) -> float:
        last_exc: BaseException | None = None
        for rpc_url in self.rpc_urls:
            try:
                return self._get_balance_from(rpc_url, address)
            except RateLimited as exc:
                last_exc = exc
                if rpc_url is self.rpc_urls[-1]:
                    raise  # последний узел в списке тоже 429 — дальше пробовать нечего, пусть pipeline.py решает паузу
                continue  # есть ещё резервные узлы — пробуем следующий без ожидания
            except requests.RequestException as exc:
                last_exc = exc
                continue  # этот узел исчерпал свои попытки — пробуем следующий из резерва
            except BalanceCheckError as exc:
                # Узел ответил, но с ошибкой в теле (не сетевой сбой) — тоже
                # пробуем следующий на случай, если это специфика конкретного узла
                # (например, устаревший индекс), а не самого адреса.
                last_exc = exc
                continue

        tried = ", ".join(self.rpc_urls)
        raise BalanceCheckError(
            f"{self.network} RPC: сбой запроса баланса для {address} — все {len(self.rpc_urls)} узла(ов) "
            f"недоступны ({tried}). Последняя ошибка: {last_exc}"
        )

    def get_known_token_balances(self, address: str) -> dict:
        """См. docstring модуля, п.2 седьмого раунда. Не бросает исключений —
        дополнительные данные, сбой здесь не должен ронять основную проверку
        баланса (симметрично solana.py::get_token_holdings)."""
        tokens = KNOWN_ERC20_TOKENS.get(self.network)
        if not tokens:
            return {}

        holdings: dict[str, float] = {}
        padded = address[2:].lower().zfill(64) if address.startswith("0x") else address.lower().zfill(64)
        call_data = BALANCE_OF_SELECTOR + padded

        for rpc_url in self.rpc_urls:
            try:
                for contract, (symbol, decimals) in tokens.items():
                    data = self._rpc_call(rpc_url, {
                        "jsonrpc": "2.0", "id": 1,
                        "method": "eth_call",
                        "params": [{"to": contract, "data": call_data}, "latest"],
                    })
                    result_hex = data.get("result")
                    if not result_hex or result_hex == "0x":
                        continue
                    raw = int(result_hex, 16)
                    amount = raw / (10 ** decimals)
                    if amount:
                        holdings[symbol] = amount
                return holdings  # первый рабочий узел отработал все токены — резервные не нужны
            except (requests.RequestException, RateLimited) as exc:
                logger.warning("%s: известные токены для %s не получены с узла %s (%s), пробую следующий", self.network, address, rpc_url, exc)
                continue

        logger.warning("%s: известные токены для %s не получены ни с одного узла (не критично, основной баланс это не затрагивает)", self.network, address)
        return {}


def make_evm_adapters() -> dict[str, EvmBalanceAdapter]:
    """Фабрика: по одному адаптеру на каждую из 6 EVM-сетей из раздела 3.3 ТЗ."""
    return {network: EvmBalanceAdapter(network) for network in EVM_CHAINS}
