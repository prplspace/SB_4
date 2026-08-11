"""
Birdeye — источник для мемкоинов (раздел 3.1 ТЗ). Свободная регистрация на
bds.birdeye.so, free-тир ~30 000 compute units/месяц (см. приложение
«Ключи API»). Покрывает Solana, Sui и основные EVM-сети.

Эндпоинт /defi/txs/token отдаёт отдельные сделки с адресом трейдера —
именно то, что не может дать DexScreener (см. dexscreener.py).

ЧЕСТНО: точные имена полей ("owner" для адреса трейдера, "side" для
buy/sell) взяты по документированной общей структуре Birdeye DeFi API, не
перепроверены вживую без сетевого доступа — код читает их защитно через
.get() с запасными именами и не падает при небольших расхождениях.
"""

from __future__ import annotations

from datetime import datetime, timezone

import requests

from adapters.marketplaces.base import ActivityRecord, AdapterError, MarketplaceAdapter
from config import settings

BASE_URL = "https://public-api.birdeye.so"


class BirdeyeAdapter(MarketplaceAdapter):
    name = "birdeye"
    requires_key = True
    default_daily_limit = 30_000  # это compute units/месяц, не запросы/день — грубая эвристика,
                                   # реальный расход зависит от типа эндпоинта (см. тарифы Birdeye)
    SUPPORTED_ASSET_TYPES = {"memecoin"}

    def fetch_activity(self, asset_type: str, target: str, limit: int = 100, target_wallets: int = 20, chain: str = "solana") -> list[ActivityRecord]:
        # target_wallets: параметр интерфейса — этот адаптер пагинацию не делает, игнорирует.
        if asset_type != "memecoin":
            raise AdapterError("Birdeye подключен здесь только для мемкоинов, для NFT используйте маркетплейс-адаптеры")
        if not settings.birdeye_key:
            raise AdapterError("Birdeye: не задан BIRDEYE_API_KEY (см. приложение «Ключи API»)")

        url = f"{BASE_URL}/defi/txs/token"
        headers = {
            "User-Agent": settings.user_agent,
            "X-API-KEY": settings.birdeye_key,
            "x-chain": chain,
        }
        try:
            resp = requests.get(url, params={"address": target, "limit": min(limit, 50)}, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise AdapterError(f"Birdeye: сбой запроса сделок для {target}: {exc}") from exc

        items = ((data.get("data") or {}).get("items")) or []
        return self._parse_items(items, target, chain)

    @staticmethod
    def _parse_items(items: list[dict], target: str, chain: str) -> list[ActivityRecord]:
        records: list[ActivityRecord] = []
        for item in items:
            addr = item.get("owner") or item.get("trader") or item.get("walletAddress")
            if not addr:
                continue
            side = (item.get("side") or "").lower()
            role = "buyer" if side == "buy" else ("seller" if side == "sell" else "buyer")
            block_time = item.get("blockUnixTime")
            ts = datetime.fromtimestamp(block_time, tz=timezone.utc) if isinstance(block_time, (int, float)) else None
            volume_usd = item.get("volumeUSD") or item.get("volumeUsd")
            records.append(ActivityRecord(
                wallet_address=addr, role=role, network=chain,
                asset_id=target, price=float(volume_usd) if volume_usd is not None else None,
                timestamp=ts,
            ))
        return records
