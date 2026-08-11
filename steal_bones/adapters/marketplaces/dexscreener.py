"""
DexScreener — источник для мемкоинов (раздел 3.1 ТЗ). Бесплатно, без
регистрации и без ключа (60 запросов/мин на профили, 300/мин на пары).

ЧЕСТНО: публичный API DexScreener (/latest/dex/tokens, /latest/dex/pairs,
/latest/dex/search) отдаёт метаданные пары/токена и агрегированную
статистику (цена, объём, ликвидность, счётчики транзакций) — но НЕ отдаёт
список отдельных сделок с адресами кошельков-участников. Он полезен, чтобы
подтвердить, что токен существует и найти его основную пару, но не годится
как единственный источник для требования "найти кошельки, которые торгуют
этим мемкоином". Для этого используйте адаптер Birdeye (birdeye.py) —
у него есть эндпоинт с транзакциями и адресом трейдера.

Ниже — рабочая реализация того, что DexScreener реально умеет (проверка
токена + его текущая пара/объём), которую main.py использует для валидации
ввода перед тем, как идти в Birdeye за адресами кошельков.
"""

from __future__ import annotations

import requests

from adapters.marketplaces.base import ActivityRecord, AdapterError, MarketplaceAdapter
from config import settings

BASE_URL = "https://api.dexscreener.com/latest/dex"


class DexScreenerAdapter(MarketplaceAdapter):
    name = "dexscreener"
    requires_key = False
    default_daily_limit = 432_000  # 300/мин * 60 * 24 (для /pairs; /tokens ниже — 60/мин)
    SUPPORTED_ASSET_TYPES = {"memecoin"}

    def fetch_activity(self, asset_type: str, target: str, limit: int = 100, target_wallets: int = 20) -> list[ActivityRecord]:
        # target_wallets: параметр интерфейса (динамическая глубина поиска, см. base.py) —
        # этот адаптер пагинацию не делает, поэтому просто игнорирует значение.
        raise AdapterError(
            "DexScreener не отдаёт адреса кошельков по отдельным сделкам (только "
            "агрегированную статистику пары). Используйте адаптер Birdeye для "
            "получения списка кошельков, торгующих этим токеном."
        )

    def get_token_info(self, token_address: str) -> dict | None:
        """То, что DexScreener реально умеет: проверить токен и вернуть его
        основную пару (цена/объём/ликвидность) — полезно для валидации ввода
        пользователя перед запросом к Birdeye."""
        url = f"{BASE_URL}/tokens/{token_address}"
        try:
            resp = requests.get(url, headers={"User-Agent": settings.user_agent}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise AdapterError(f"DexScreener: сбой запроса для токена {token_address}: {exc}") from exc

        pairs = data.get("pairs") or []
        if not pairs:
            return None
        # Берём пару с наибольшей ликвидностью как основную
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        return {
            "symbol": best.get("baseToken", {}).get("symbol"),
            "price_usd": best.get("priceUsd"),
            "liquidity_usd": (best.get("liquidity") or {}).get("usd"),
            "volume_24h_usd": (best.get("volume") or {}).get("h24"),
            "pair_address": best.get("pairAddress"),
        }
