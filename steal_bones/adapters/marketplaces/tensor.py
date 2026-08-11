"""
Tensor (Solana) — раздел 3.1 ТЗ.

Доступ выдаётся по заявке (форма в docs.tensor.trade -> "API & SDK"), не
самостоятельная регистрация — см. приложение "Ключи API". Заголовок
`x-tensor-api-key` и REST-эндпоинт для транзакций покупки подтверждены
документацией; ЧЕСТНО: точный GraphQL-запрос для ленты последних продаж по
коллекции (в отличие от "купить конкретный листинг") в открытых источниках,
которые были доступны при написании кода, не встретился в проверяемом виде —
без сетевого доступа к их GraphQL Playground его нельзя было бы подтвердить
даже если бы я его придумал. Поэтому ниже — рабочая заготовка с правильной
авторизацией, а сам запрос нужно дописать по документации, которую Tensor
пришлёт вместе с одобренным доступом.
"""

from __future__ import annotations

import requests

from adapters.marketplaces.base import ActivityRecord, AdapterError, MarketplaceAdapter
from config import settings

GRAPHQL_URL = "https://api.mainnet.tensordev.io/graphql"


class TensorAdapter(MarketplaceAdapter):
    name = "tensor"
    requires_key = True
    SUPPORTED_ASSET_TYPES = {"nft"}

    # TODO: заменить на реальный запрос из документации, которую Tensor
    # присылает после одобрения формы доступа (docs.tensor.trade/api-and-sdk).
    # Ориентир по форме (не проверено вживую): что-то вроде запроса
    # `recentTransactions(slug: $slug, limit: $limit)` с полями
    # buyerAddress/sellerAddress/priceLamports/blockTime.
    ACTIVITY_QUERY = """
    query RecentSales($slug: String!, $limit: Int!) {
      recentTransactions(slug: $slug, limit: $limit) {
        buyerAddress
        sellerAddress
        priceLamports
        blockTime
      }
    }
    """

    def fetch_activity(self, asset_type: str, target: str, limit: int = 100, target_wallets: int = 20) -> list[ActivityRecord]:
        # target_wallets: параметр интерфейса (динамическая глубина поиска, см. base.py) —
        # этот адаптер пагинацию не делает, поэтому просто игнорирует значение.
        if asset_type != "nft":
            raise AdapterError("Tensor — NFT-площадка Solana, для мемкоинов используйте dexscreener/birdeye")
        if not settings.tensor_key:
            raise AdapterError(
                "Tensor: не задан TENSOR_API_KEY — получите доступ по форме "
                "из docs.tensor.trade/trade/api-and-sdk (см. приложение «Ключи API»)"
            )

        try:
            resp = requests.post(
                GRAPHQL_URL,
                json={"query": self.ACTIVITY_QUERY, "variables": {"slug": target, "limit": min(limit, 100)}},
                headers={
                    "User-Agent": settings.user_agent,
                    "x-tensor-api-key": settings.tensor_key,
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise AdapterError(f"Tensor: сбой запроса активности для {target}: {exc}") from exc

        if "errors" in data:
            raise AdapterError(
                f"Tensor вернул ошибку GraphQL для {target} (проверьте ACTIVITY_QUERY "
                f"по документации из вашего доступа): {data['errors']}"
            )

        items = (data.get("data") or {}).get("recentTransactions", [])
        records: list[ActivityRecord] = []
        for item in items:
            price_lamports = item.get("priceLamports")
            price = price_lamports / 1_000_000_000 if price_lamports is not None else None
            for addr, role in ((item.get("buyerAddress"), "buyer"), (item.get("sellerAddress"), "seller")):
                if addr:
                    records.append(ActivityRecord(
                        wallet_address=addr, role=role, network="solana",
                        asset_id=target, price=price, timestamp=None,
                    ))
        return records
