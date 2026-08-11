"""
Bitcoin — раздел 3.3 ТЗ. mempool.space REST API, бесплатно, без ключа и без
строгого документированного лимита. Баланс = funded_txo_sum - spent_txo_sum
(подтверждённые транзакции); мемпул (неподтверждённые) учитывается отдельно
и здесь не приплюсовывается, чтобы порог баланса не "мигал" туда-обратно
на неподтверждённых транзакциях.

1 BTC = 100_000_000 satoshi.
"""

from __future__ import annotations

import requests

from adapters.balances.base import BalanceAdapter, BalanceCheckError
from config import settings

MEMPOOL_SPACE_API = "https://mempool.space/api"
SATS_PER_BTC = 100_000_000


class BitcoinBalanceAdapter(BalanceAdapter):
    network = "bitcoin"
    requires_key = False

    def get_balance(self, address: str) -> float:
        url = f"{MEMPOOL_SPACE_API}/address/{address}"
        try:
            resp = requests.get(url, headers={"User-Agent": settings.user_agent}, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise BalanceCheckError(f"mempool.space: сбой запроса баланса для {address}: {exc}") from exc

        chain_stats = data.get("chain_stats")
        if chain_stats is None:
            raise BalanceCheckError(f"mempool.space: неожиданный формат ответа для {address}: {data}")

        funded = chain_stats.get("funded_txo_sum", 0)
        spent = chain_stats.get("spent_txo_sum", 0)
        return (funded - spent) / SATS_PER_BTC
