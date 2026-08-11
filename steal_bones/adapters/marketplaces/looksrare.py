"""
LooksRare — раздел 3.1 ТЗ.

Ключ выдаётся через тикет в Developer Discord LooksRare (не самостоятельная
регистрация — см. приложение "Ключи API"), нужен только для запросов к
mainnet (Sepolia testnet работает без ключа), лимит 120 запросов/мин.

ЧЕСТНО: путь эндпоинта для ленты продаж (/api/v2/events ниже) и точные
названия полей ответа взяты по общей структуре REST API LooksRare, но не
были перепроверены вживую (нет сетевого доступа в среде разработки) —
перед боевым запуском сверьте с looksrare.dev/reference и поправьте
_parse_events при расхождении. Защитный .get() ниже не даст адаптеру упасть
при небольших расхождениях в структуре, но может вернуть 0 записей, если
путь эндпоинта в итоге окажется другим.
"""

from __future__ import annotations

import requests

from adapters.marketplaces.base import ActivityRecord, AdapterError, MarketplaceAdapter
from config import settings

BASE_URL = "https://api.looksrare.org/api/v2"


class LooksRareAdapter(MarketplaceAdapter):
    name = "looksrare"
    requires_key = True
    default_daily_limit = 172_800  # 120/мин * 60 * 24
    SUPPORTED_ASSET_TYPES = {"nft"}

    def fetch_activity(self, asset_type: str, target: str, limit: int = 100, target_wallets: int = 20) -> list[ActivityRecord]:
        # target_wallets: параметр интерфейса (динамическая глубина поиска, см. base.py) —
        # этот адаптер пагинацию не делает, поэтому просто игнорирует значение.
        if asset_type != "nft":
            raise AdapterError("LooksRare — NFT-площадка, для мемкоинов используйте dexscreener/birdeye")
        if not settings.looksrare_key:
            raise AdapterError("LooksRare: не задан LOOKSRARE_API_KEY (см. приложение «Ключи API»)")

        url = f"{BASE_URL}/events"
        try:
            resp = requests.get(
                url, params={"collection": target, "type": "SALE", "pagination": min(limit, 100)},
                headers={"User-Agent": settings.user_agent, "X-Looks-Rare-Api-Key": settings.looksrare_key},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise AdapterError(f"LooksRare: сбой запроса событий для {target}: {exc}") from exc

        items = data.get("data", data if isinstance(data, list) else [])
        return self._parse_events(items, target)

    @staticmethod
    def _parse_events(items: list[dict], target: str) -> list[ActivityRecord]:
        def _addr(value):
            if isinstance(value, dict):
                return value.get("address")
            return value

        records: list[ActivityRecord] = []
        for item in items:
            buyer = _addr(item.get("to") or item.get("buyer"))
            seller = _addr(item.get("from") or item.get("seller"))
            price = item.get("price")
            for addr, role in ((buyer, "buyer"), (seller, "seller")):
                if addr:
                    records.append(ActivityRecord(
                        wallet_address=addr, role=role, network="ethereum",
                        asset_id=target, price=float(price) if price is not None else None,
                        timestamp=None,
                    ))
        return records
