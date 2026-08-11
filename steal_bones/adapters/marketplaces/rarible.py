"""
Rarible — раздел 3.1 ТЗ. Мультичейн Protocol API (api.rarible.org),
X-API-KEY, свободная самостоятельная регистрация на rarible.org (см.
приложение "Ключи API").

Rarible возвращает адреса в мультичейн-формате "BLOCKCHAIN:0xadres"
(например "ETHEREUM:0xabc...") — код отрезает префикс до сети, которую
передали в target/network. Точная форма ответа (/v0.1/activities/byCollection)
не перепроверена вживую без сетевого доступа — сверить перед боевым запуском.
"""

from __future__ import annotations

import requests

from adapters.marketplaces.base import ActivityRecord, AdapterError, MarketplaceAdapter
from config import settings

BASE_URL = "https://api.rarible.org/v0.1"


class RaribleAdapter(MarketplaceAdapter):
    name = "rarible"
    requires_key = True
    SUPPORTED_ASSET_TYPES = {"nft"}

    def fetch_activity(self, asset_type: str, target: str, limit: int = 100, target_wallets: int = 20) -> list[ActivityRecord]:
        # target_wallets: параметр интерфейса (динамическая глубина поиска, см. base.py) —
        # этот адаптер пагинацию не делает, поэтому просто игнорирует значение.
        """target — строка вида "ETHEREUM:0xcontract" (blockchain:contract),
        как это принято в мультичейн-API Rarible."""
        if asset_type != "nft":
            raise AdapterError("Rarible: для мемкоинов используйте dexscreener/birdeye")
        if not settings.rarible_key:
            raise AdapterError("Rarible: не задан RARIBLE_API_KEY (см. приложение «Ключи API»)")

        network = target.split(":")[0].lower() if ":" in target else "ethereum"
        url = f"{BASE_URL}/activities/byCollection"
        try:
            resp = requests.get(
                url, params={"collection": target, "type": "SELL", "size": min(limit, 50)},
                headers={"User-Agent": settings.user_agent, "X-API-KEY": settings.rarible_key},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise AdapterError(f"Rarible: сбой запроса активности для {target}: {exc}") from exc

        return self._parse_activities(data.get("activities", []), target, network)

    @staticmethod
    def _parse_activities(activities: list[dict], target: str, network: str) -> list[ActivityRecord]:
        def _strip_chain(value):
            if not value:
                return None
            return value.split(":")[-1] if ":" in value else value

        records: list[ActivityRecord] = []
        for act in activities:
            buyer = _strip_chain(act.get("buyer"))
            seller = _strip_chain(act.get("seller"))
            price = act.get("price")
            for addr, role in ((buyer, "buyer"), (seller, "seller")):
                if addr:
                    records.append(ActivityRecord(
                        wallet_address=addr, role=role, network=network,
                        asset_id=target, price=float(price) if price is not None else None,
                        timestamp=None,
                    ))
        return records
