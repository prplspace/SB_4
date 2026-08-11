"""
Sui — раздел 3.3 ТЗ. Публичный fullnode RPC, метод `suix_getBalance`,
ключ не нужен. 1 SUI = 1_000_000_000 MIST.

Публичная нода общая для всех — при интенсивном использовании стоит
рассмотреть платный RPC-провайдер (Blockberry/BlockVision и т.п.).
"""

from __future__ import annotations

import requests

from adapters.balances.base import BalanceAdapter, BalanceCheckError
from config import settings

PUBLIC_RPC = "https://fullnode.mainnet.sui.io:443"
MIST_PER_SUI = 1_000_000_000


class SuiBalanceAdapter(BalanceAdapter):
    network = "sui"
    requires_key = False

    def get_balance(self, address: str) -> float:
        payload = {"jsonrpc": "2.0", "id": 1, "method": "suix_getBalance", "params": [address]}
        try:
            resp = requests.post(
                PUBLIC_RPC, json=payload,
                headers={"User-Agent": settings.user_agent, "Content-Type": "application/json"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise BalanceCheckError(f"Sui RPC: сбой запроса баланса для {address}: {exc}") from exc

        if "error" in data:
            raise BalanceCheckError(f"Sui RPC вернул ошибку для {address}: {data['error']}")

        total = data.get("result", {}).get("totalBalance")
        if total is None:
            raise BalanceCheckError(f"Sui RPC: неожиданный формат ответа для {address}: {data}")

        return int(total) / MIST_PER_SUI
