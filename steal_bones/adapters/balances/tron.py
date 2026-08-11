"""
Tron — раздел 3.3 ТЗ. TronGrid REST API (api.trongrid.io). Бесплатная
регистрация даёт TRON-PRO-API-KEY — без ключа запросы тоже проходят, но с
более жёстким лимитом (см. приложение "Ключи API"). Без ключа — работает,
просто добавляем заголовок только если ключ задан.

1 TRX = 1_000_000 sun. Неактивированный аккаунт (никогда не получал TRX)
возвращает пустой объект без поля balance — трактуем это как баланс 0.
"""

from __future__ import annotations

import requests

from adapters.balances.base import BalanceAdapter, BalanceCheckError
from config import settings

TRONGRID_API = "https://api.trongrid.io"
SUN_PER_TRX = 1_000_000


class TronBalanceAdapter(BalanceAdapter):
    network = "tron"
    requires_key = False  # работает и без ключа, ключ только повышает лимит

    def get_balance(self, address: str) -> float:
        url = f"{TRONGRID_API}/wallet/getaccount"
        headers = {"User-Agent": settings.user_agent, "Content-Type": "application/json"}
        if settings.trongrid_key:
            headers["TRON-PRO-API-KEY"] = settings.trongrid_key

        try:
            resp = requests.post(url, json={"address": address, "visible": True}, headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise BalanceCheckError(f"TronGrid: сбой запроса баланса для {address}: {exc}") from exc

        # Неактивированный адрес -> {} (пустой объект), это НЕ ошибка, а баланс 0
        return data.get("balance", 0) / SUN_PER_TRX
