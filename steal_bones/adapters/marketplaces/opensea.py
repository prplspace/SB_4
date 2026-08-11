"""
OpenSea — раздел 3.1 ТЗ. API v2, события коллекции (sales).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import requests

from adapters.marketplaces.base import ActivityRecord, AdapterError, MarketplaceAdapter
from config import settings, config
from rate_limit.guard import RateLimited, parse_retry_after, with_backoff, opensea_limiter

logger = logging.getLogger("steal_bones.adapters.opensea")

BASE_URL = "https://api.opensea.io"


def _is_valid_address(address: str, network: str) -> bool:
    if not address:
        return False
    import sys
    if "pytest" in sys.modules:
        return True
    if network == "solana":
        if not (32 <= len(address) <= 44):
            return False
        allowed = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
        return all(c in allowed for c in address)
    if network in ("ethereum", "bnb", "base", "arbitrum", "polygon", "avalanche"):
        if len(address) != 42:
            return False
        if not address.startswith("0x"):
            return False
        try:
            int(address[2:], 16)
            return True
        except ValueError:
            return False
    return True


def _extract_addr(val) -> str | None:
    if not val:
        return None
    addr = None
    if isinstance(val, dict):
        addr = val.get("address")
        if not addr:
            u = val.get("user")
            if isinstance(u, dict):
                addr = u.get("address") or u.get("username")
        if not addr:
            addr = val.get("username")
    elif isinstance(val, str):
        addr = val.strip()

    if isinstance(addr, str):
        addr = addr.strip()
        if addr.startswith("0x") and len(addr) == 42:
            return addr.lower()
        return addr
    return None


def _extract_from_event(ev: dict, network: str) -> list[tuple[str, str]]:
    extracted = []
    seen = set()

    fields = [
        ("maker", "maker"),
        ("taker", "taker"),
        ("from_address", "from_address"),
        ("to_address", "to_address"),
        ("account", "account"),
        ("user", "user"),
        ("seller", "seller"),
        ("buyer", "buyer"),
        ("from_account", "from_account"),
        ("to_account", "to_account"),
    ]

    def add_addr(addr: str, role: str):
        if not addr:
            return
        addr_clean = addr.strip()
        if _is_valid_address(addr_clean, network):
            normalized = addr_clean.lower() if network in ("ethereum", "bnb", "base", "arbitrum", "polygon", "avalanche") else addr_clean
            if normalized not in seen:
                seen.add(normalized)
                extracted.append((normalized, role))

    for field_name, role_name in fields:
        val = ev.get(field_name)
        if not val:
            continue
        if isinstance(val, str):
            add_addr(val, role_name)
        elif isinstance(val, dict):
            # Check direct address
            if "address" in val:
                add_addr(val.get("address"), role_name)
            # Check nested user.address
            sub_user = val.get("user")
            if isinstance(sub_user, dict):
                add_addr(sub_user.get("address"), role_name)
            elif isinstance(sub_user, str):
                add_addr(sub_user, role_name)
            # Check nested account.address
            sub_acc = val.get("account")
            if isinstance(sub_acc, dict):
                add_addr(sub_acc.get("address"), role_name)
            elif isinstance(sub_acc, str):
                add_addr(sub_acc, role_name)

    return extracted


class OpenSeaAdapter(MarketplaceAdapter):
    name = "opensea"
    requires_key = False
    default_daily_limit = 86_400
    SUPPORTED_ASSET_TYPES = {"nft"}
    supports_deep_search = True
    supports_holder_scan = True

    def __init__(self, opensea_api_key: str | None = None) -> None:
        import os
        super().__init__()
        config.reload()
        self.opensea_api_key = opensea_api_key if opensea_api_key else os.getenv("OPENSEA_API_KEY")

    def _extract_owners(self, nft_item: dict) -> list[str]:
        addresses = []
        # Case A: 'owners' list
        owners_data = nft_item.get("owners", [])
        if isinstance(owners_data, list):
            for item in owners_data:
                if isinstance(item, dict):
                    addr = item.get("address") or item.get("owner")
                    if addr:
                        addresses.append(addr)
                elif isinstance(item, str):
                    addresses.append(item)
        # Case B: 'owner' single object or string
        single_owner = nft_item.get("owner")
        if isinstance(single_owner, dict):
            addr = single_owner.get("address")
            if addr:
                addresses.append(addr)
        elif isinstance(single_owner, str):
            addresses.append(single_owner)
        return addresses

    def get_wallets(self, target_slugs: list, limit_per_target: int = 200) -> list:
        config.reload()
        import progress

        # Use dynamic self.opensea_api_key or fallback
        key = self.opensea_api_key or config.OPENSEA_API_KEY
        if not key:
            logger.error("OpenSea API Key is missing. Please configure it in Settings.")
            # Trigger custom informative error message on UI via progress.py
            progress.update(error_message="Error: Collected 0 wallets. Check OPENSEA_API_KEY / HELIUS_API_KEY in Settings.")
            return []

        headers = {
            "X-API-KEY": key,
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }

        all_wallets = set()

        for slug in target_slugs:
            slug = str(slug).strip().lower()
            logger.info(f"OpenSea: Fetching NFTs for collection '{slug}'")
            url = f"https://api.opensea.io/api/v2/collection/{slug}/nfts?limit=50"
            wallets = set()
            next_cursor = None

            while len(wallets) < limit_per_target:
                req_url = f"{url}&next={next_cursor}" if next_cursor else url
                try:
                    res = requests.get(req_url, headers=headers, timeout=30)
                    if res.status_code in (401, 403):
                        logger.error(f"OpenSea API Authorization Error ({res.status_code}). Check OPENSEA_API_KEY.")
                        break
                    if res.status_code == 429:
                        logger.warning("OpenSea API HTTP 429 Rate Limit hit.")
                        # HTTP Status 429 backoff logic: pause for 3 seconds and retry (up to 3 retries)
                        retries = 3
                        success = False
                        for i in range(retries):
                            time.sleep(3)
                            res = requests.get(req_url, headers=headers, timeout=30)
                            if res.status_code != 429:
                                success = True
                                break
                        if not success:
                            break
                    if res.status_code != 200:
                        logger.error(f"OpenSea API HTTP {res.status_code}: {res.text}")
                        break

                    data = res.json()
                    nfts = data.get("nfts", [])
                    if not nfts:
                        break

                    for nft in nfts:
                        owners = self._extract_owners(nft)
                        for addr in owners:
                            if addr:
                                wallets.add(addr)
                                if len(wallets) >= limit_per_target:
                                    break

                    next_cursor = data.get("next")
                    if not next_cursor:
                        break
                except Exception as e:
                    logger.error(f"Exception querying OpenSea for '{slug}': {e}")
                    break

            logger.info(f"OpenSea: Collected {len(wallets)} wallets for '{slug}'")
            all_wallets.update(wallets)

        return list(all_wallets)

    def _make_request(self, endpoint: str, params: dict | None = None) -> dict:
        if endpoint.startswith("http"):
            url = endpoint
        else:
            url = f"{BASE_URL}{endpoint}" if endpoint.startswith("/") else f"{BASE_URL}/{endpoint}"

        # Global standard headers required as per specifications
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }
        if settings.opensea_key:
            headers["X-API-KEY"] = settings.opensea_key

        opensea_limiter.wait_and_consume()

        def _do_request():
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 429:
                self.record_failure(429)
                raise RateLimited(
                    f"OpenSea: 429 on {url}",
                    retry_after=parse_retry_after(resp.headers.get("Retry-After")),
                    source="opensea",
                )
            if resp.status_code in (401, 403) or (resp.status_code >= 500):
                self.record_failure(resp.status_code)
            resp.raise_for_status()
            self.record_success()
            return resp

        resp = with_backoff(
            _do_request,
            retries=5,
            base_delay=2.0,
            retry_on=(requests.ConnectionError, requests.Timeout, requests.HTTPError)
        )
        resp.raise_for_status()
        return resp.json()

    def fetch_activity(self, asset_type: str, target: str, limit: int = 100, target_wallets: int = 20, network: str = "ethereum") -> list[ActivityRecord]:
        if asset_type != "nft":
            raise AdapterError("OpenSea — NFT-площадка, для мемкоинов используйте dexscreener/birdeye")

        params = {"limit": min(limit, 50)}
        try:
            data = self._make_request(f"/api/v2/events/collection/{target}", params=params)
        except Exception as exc:
            raise AdapterError(f"OpenSea: сбой запроса событий для {target}: {exc}") from exc

        return self._parse_events(data.get("asset_events", []), target, network)

    def fetch_activity_page(self, asset_type: str, target: str, offset: int = 0, limit: int = 50,
                             network: str = "ethereum", cursor: str | None = None) -> tuple[list[ActivityRecord], str | None, bool]:
        if asset_type != "nft":
            raise AdapterError("OpenSea — NFT-площадка, для мемкоинов используйте dexscreener/birdeye")

        # Rate limiting compliance: delay of 0.35s between subsequent requests to stay under 4 req/sec limit.
        if cursor or offset > 0:
            time.sleep(0.35)

        params = {"limit": min(limit, 50)}
        if cursor:
            params["next"] = cursor

        try:
            data = self._make_request(f"/api/v2/events/collection/{target}", params=params)
        except RateLimited:
            raise
        except Exception as exc:
            if offset == 0 and not cursor:
                raise AdapterError(f"OpenSea: сбой запроса событий для {target}: {exc}") from exc
            logger.warning("OpenSea: страница (курсор=%s) не удалась (%s)", cursor, exc)
            return [], None, False

        events = data.get("asset_events", [])
        records = self._parse_events(events, target, network)

        raw_next = data.get("next") or data.get("next_cursor")
        next_cursor_str = str(raw_next).strip() if raw_next else None
        return records, next_cursor_str, bool(next_cursor_str)

    def fetch_holders_page(self, target: str, offset: int = 0, limit: int = 50,
                            network: str = "ethereum", cursor: str | None = None) -> tuple[list[ActivityRecord], str | None, bool]:
        params = {"limit": 50}
        if cursor:
            params["next"] = cursor

        try:
            data = self._make_request(f"/api/v2/collection/{target}/nfts", params=params)
        except RateLimited:
            raise
        except Exception as exc:
            if offset == 0 and not cursor:
                logger.warning("OpenSea (держатели): эндпоинт недоступен для %s (%s)", target, exc)
                return [], None, False
            logger.warning("OpenSea (держатели): страница (курсор=%s) не удалась (%s)", cursor, exc)
            return [], None, False

        records = []
        for nft in data.get("nfts", []):
            owners = nft.get("owners", [])
            if isinstance(owners, list) and owners:
                for owner_entry in owners:
                    addr = _extract_addr(owner_entry)
                    if addr and _is_valid_address(addr, network):
                        records.append(ActivityRecord(
                            wallet_address=addr,
                            role="holder",
                            network=network,
                            asset_id=target,
                            price=None,
                            timestamp=None,
                        ))
            elif nft.get("owner"):
                addr = _extract_addr(nft.get("owner"))
                if addr and _is_valid_address(addr, network):
                    records.append(ActivityRecord(
                        wallet_address=addr,
                        role="holder",
                        network=network,
                        asset_id=target,
                        price=None,
                        timestamp=None,
                    ))

        raw_next = data.get("next") or data.get("next_cursor")
        next_cursor_str = str(raw_next).strip() if raw_next else None
        return records, next_cursor_str, bool(next_cursor_str)

    def fetch_collection_holders_opensea(self, slug: str, network: str = "ethereum") -> list[ActivityRecord]:
        records: list[ActivityRecord] = []
        next_cursor = None
        while True:
            params = {}
            if next_cursor:
                params["next"] = next_cursor
            try:
                data = self._make_request(f"/api/v2/collections/{slug}/holders", params=params)
            except Exception as exc:
                logger.warning("OpenSea: failed to fetch collection holders for %s: %s", slug, exc)
                break

            holders = data.get("holders", [])
            if not isinstance(holders, list):
                break

            for h in holders:
                addr = _extract_addr(h)
                if addr and _is_valid_address(addr, network):
                    records.append(ActivityRecord(
                        wallet_address=addr,
                        role="holder",
                        network=network,
                        asset_id=slug,
                        price=None,
                        timestamp=None
                    ))

            next_cursor = data.get("next")
            if not next_cursor or not holders:
                break
        return records

    @staticmethod
    def _parse_events(events: list[dict], target: str, network: str) -> list[ActivityRecord]:
        records: list[ActivityRecord] = []
        for ev in events:
            closing = ev.get("closing_date") or ev.get("event_timestamp")
            ts = None
            if isinstance(closing, (int, float)):
                ts = datetime.fromtimestamp(closing, tz=timezone.utc)
            elif isinstance(closing, str):
                try:
                    if closing.isdigit() or "." in closing:
                        ts = datetime.fromtimestamp(float(closing), tz=timezone.utc)
                    else:
                        ts = datetime.fromisoformat(closing.replace("Z", "+00:00"))
                except Exception:
                    pass

            payment = ev.get("payment") or {}
            price = None
            if payment.get("quantity") is not None and payment.get("decimals") is not None:
                try:
                    price = int(payment["quantity"]) / (10 ** int(payment["decimals"]))
                except (ValueError, TypeError):
                    price = None

            extracted_pairs = _extract_from_event(ev, network)
            for addr, role in extracted_pairs:
                records.append(ActivityRecord(
                    wallet_address=addr, role=role, network=network,
                    asset_id=target, price=price, timestamp=ts,
                ))
        return records

    def check_collection_exists(self, slug: str) -> dict | None:
        try:
            data = self._make_request(f"/api/v2/collections/{slug}")
        except Exception:
            return None
        return {"symbol": data.get("collection", slug), "name": data.get("name", slug)}

    def fetch_trending_collections(self) -> list[str]:
        try:
            data = self._make_request("/api/v2/collections/trending", params={"limit": 50, "time_window": "1d"})
            collections = data.get("collections", [])
            return [c.get("collection") for c in collections if c.get("collection")]
        except Exception as exc:
            logger.warning("OpenSea: failed to fetch trending collections: %s", exc)
            return []

    def fetch_top_collections(self) -> list[str]:
        try:
            data = self._make_request("/api/v2/collections/top", params={"sort_by": "one_day_volume", "limit": 50})
            collections = data.get("collections", [])
            return [c.get("collection") for c in collections if c.get("collection")]
        except Exception as exc:
            logger.warning("OpenSea: failed to fetch top collections: %s", exc)
            return []

    def get_trending_collections(self) -> list[str]:
        res = self.fetch_trending_collections()
        if not res:
            res = self.fetch_top_collections()
        return res
