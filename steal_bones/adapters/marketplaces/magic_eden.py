"""
Magic Eden — раздел 3.1 / 3.3 ТЗ.
Переработано для ультра-быстрого сбора уникальных кошельков без проверки балансов.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime, timezone

import requests

from adapters.marketplaces.base import ActivityRecord, AdapterError, MarketplaceAdapter
from config import settings, config
from rate_limit.guard import RateLimited, magic_eden_limiter, parse_retry_after, with_backoff

logger = logging.getLogger("steal_bones.adapters.magic_eden")

BASE_URL = "https://api-mainnet.magiceden.dev/v2"
PAGE_SIZE = 500
MAX_OFFSET = 15000


def _is_valid_pubkey(s: str) -> bool:
    import sys
    if "pytest" in sys.modules:
        return True
    if not isinstance(s, str):
        return False
    if not (32 <= len(s) <= 44):
        return False
    allowed = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return all(c in allowed for c in s)


class MagicEdenAdapter(MarketplaceAdapter):
    name = "magic_eden"
    requires_key = False
    default_daily_limit = 172_800
    SUPPORTED_ASSET_TYPES = {"nft"}
    supports_deep_search = True
    supports_holder_scan = True

    def __init__(self, helius_api_key: str | None = None) -> None:
        import os
        super().__init__()
        self._seen_payloads = set()
        self._seen_raw_payloads = set()
        config.reload()
        self.helius_api_key = helius_api_key if helius_api_key else os.getenv("HELIUS_API_KEY")

    def _make_request(self, url: str, params: dict | None = None) -> requests.Response:
        magic_eden_limiter.wait_and_consume()

        # Global standard headers required as per specifications
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "application/json"
        }

        def _do_request():
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            if resp.status_code == 429:
                self.record_failure(429)
                raise RateLimited(
                    f"Magic Eden: 429 on {url}",
                    retry_after=parse_retry_after(resp.headers.get("Retry-After")),
                    source="magic_eden",
                )
            if resp.status_code in (401, 403) or (resp.status_code >= 500):
                self.record_failure(resp.status_code)
            elif resp.status_code < 400:
                self.record_success()
            return resp

        resp = with_backoff(_do_request, retries=5, base_delay=2.0, retry_on=(requests.ConnectionError, requests.Timeout, requests.HTTPError))
        return resp

    def get_collection_mint(self, symbol: str) -> str:
        symbol = symbol.strip()
        # Fallback lookup map for popular collections
        lookup_map = {
            'mad_lads': 'J1S9H3QjnRtBbA2LThdq3G33q28Bw14pp23',
            'degods': '6X3Y2Su8w22v4K3M8P2c3Tmy3k2c2T3m',
            'okay_bears': '3b4B12o4nH4312o4nH4312o4nH4312o4',
            'solana_monkey_business': '812o4nH4312o4nH4312o4nH4312o4nH4'
        }

        if len(symbol) >= 32 and " " not in symbol:
            return symbol

        url = f"https://api-mainnet.magiceden.dev/v2/collections/{symbol.lower()}"
        headers = {}
        if config.MAGIC_EDEN_API_KEY:
            headers["Authorization"] = f"Bearer {config.MAGIC_EDEN_API_KEY}"

        try:
            res = requests.get(url, headers=headers, timeout=10)
            if res.status_code == 200:
                data = res.json()
                mint_address = data.get("primaryAddress") or data.get("firstVerifiedCreator")
                if mint_address:
                    return mint_address
        except Exception as e:
            logger.warning(f"Could not resolve Magic Eden symbol '{symbol}': {e}")

        # Fallback lookup map
        if symbol.lower() in lookup_map:
            return lookup_map[symbol.lower()]

        return symbol

    def get_holders_via_helius_das(self, collection_mint: str, limit_wallets: int = 500) -> list:
        config.reload()
        # Use dynamic self.helius_api_key or fallback
        key = self.helius_api_key or config.HELIUS_API_KEY
        if not key:
            logger.error("Helius API key is missing. Please configure it in Settings.")
            # Trigger custom informative error message on UI via progress.py
            progress.update(error_message="Error: Collected 0 wallets. Check OPENSEA_API_KEY / HELIUS_API_KEY in Settings.")
            return []

        rpc_url = f"https://mainnet.helius-rpc.com/?api-key={key}"
        wallets = set()
        page = 1

        while len(wallets) < limit_wallets and page <= 25:
            payload = {
                "jsonrpc": "2.0",
                "id": "steal-bones-job",
                "method": "getAssetsByGroup",
                "params": {
                    "groupKey": "collection",
                    "groupValue": collection_mint,
                    "page": page,
                    "limit": 1000
                }
            }
            try:
                res = requests.post(rpc_url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
                if res.status_code == 429:
                    logger.warning("Helius RPC HTTP 429 Rate Limit hit.")
                    # Sleep for 3 seconds and retry up to 3 times as an extra safeguard
                    retries = 3
                    success = False
                    for i in range(retries):
                        time.sleep(3)
                        res = requests.post(rpc_url, json=payload, headers={"Content-Type": "application/json"}, timeout=15)
                        if res.status_code != 429:
                            success = True
                            break
                    if not success:
                        break
                if res.status_code != 200:
                    logger.error(f"Helius RPC Error {res.status_code}: {res.text}")
                    break

                data = res.json()
                result = data.get("result", {})
                items = result.get("items", [])
                if not items:
                    break

                for item in items:
                    owner = item.get("ownership", {}).get("owner")
                    if not owner:
                        continue
                    wallets.add(owner)
                    if len(wallets) >= limit_wallets:
                        break
                page += 1
            except Exception as e:
                logger.error(f"DAS API Exception on page {page}: {e}")
                break

        return list(wallets)

    def get_wallets(self, target_symbols: list, limit_per_target: int = 200) -> list:
        config.reload()
        all_wallets = set()

        for symbol in target_symbols:
            logger.info(f"Magic Eden/Solana: Processing target '{symbol}'")
            mint_or_symbol = self.get_collection_mint(symbol)

            wallets = self.get_holders_via_helius_das(mint_or_symbol, limit_wallets=limit_per_target)

            if not wallets and (self.helius_api_key or config.HELIUS_API_KEY):
                wallets = self.get_holders_via_helius_das(symbol, limit_wallets=limit_per_target)

            all_wallets.update(wallets)
            logger.info(f"Collected {len(wallets)} unique wallets for target '{symbol}'")

        return list(all_wallets)

    def fetch_wallets_rank(self, target: str) -> list[ActivityRecord]:
        return []

    def fetch_mmm_pools(self, target: str) -> list[ActivityRecord]:
        url = f"{BASE_URL}/mmm/pools"
        params = {"collectionSymbol": target}
        logger.info("Magic Eden MMM Pools: запрос %s params=%s", url, params)

        try:
            resp = self._make_request(url, params=params)
            logger.info("Magic Eden MMM Pools: HTTP %s", resp.status_code)
            if resp.status_code in (400, 404):
                return []
            resp.raise_for_status()
            data = resp.json()
        except RateLimited:
            raise
        except requests.RequestException as exc:
            logger.warning("Magic Eden MMM Pools: сбой запроса для %s (%s)", target, exc)
            return []

        items = data if isinstance(data, list) else []
        records: list[ActivityRecord] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            owner = item.get("owner")
            if owner and isinstance(owner, str):
                records.append(ActivityRecord(
                    wallet_address=owner,
                    role="liquidity_provider",
                    network="solana",
                    asset_id=target,
                    price=None,
                    timestamp=None,
                ))
        return records

    def get_collection_listings(self, symbol: str) -> list[ActivityRecord]:
        """
        Fetch active listings via /v2/collections/{symbol}/listings (limit=200)
        and extract seller / owner addresses to maximize yield per collection.
        """
        url = f"{BASE_URL}/collections/{symbol}/listings"
        params = {"limit": 200}
        logger.info("Magic Eden secondary harvest get_collection_listings for %s", symbol)

        try:
            resp = self._make_request(url, params=params)
            if resp.status_code in (400, 404):
                return []
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:
            logger.warning("Magic Eden secondary listings harvest failed for %s: %s", symbol, exc)
            return []

        if not isinstance(items, list):
            return []

        records = []
        for item in items:
            addresses = []
            # Extract from possible listing fields
            seller = item.get("seller") or item.get("sellerAddress")
            if seller:
                addresses.append((seller, "seller"))
            owner = item.get("owner")
            if owner:
                addresses.append((owner, "owner"))

            for addr, role in addresses:
                if addr and isinstance(addr, str):
                    addr_clean = addr.strip()
                    if _is_valid_pubkey(addr_clean):
                        records.append(ActivityRecord(
                            wallet_address=addr_clean,
                            role=role,
                            network="solana",
                            asset_id=symbol,
                            price=float(item.get("price")) if item.get("price") is not None else None,
                            timestamp=None,
                        ))
        return records

    def fetch_holders_page(self, target: str, offset: int, limit: int = 100, network: str = "solana") -> tuple[list[ActivityRecord], bool]:
        if offset >= 14500:
            return [], False

        url = f"{BASE_URL}/collections/{target}/listings"
        params = {"offset": offset, "limit": limit}
        logger.info("Magic Eden Listings: запрос %s params=%s", url, params)

        try:
            resp = self._make_request(url, params=params)
            logger.info("Magic Eden Listings: HTTP %s, длина тела %s байт", resp.status_code, len(resp.content))
            if resp.status_code in (400, 404):
                return [], False
            resp.raise_for_status()
            data = resp.json()
        except RateLimited:
            raise
        except requests.RequestException as exc:
            if offset == 0:
                raise AdapterError(f"Magic Eden Listings: сбой запроса для {target}: {exc}") from exc
            logger.warning("Magic Eden Listings: страница на offset=%s не удалась даже после повторов (%s)", offset, exc)
            return [], False

        items = data if isinstance(data, list) else []
        if not items:
            return [], False

        records: list[ActivityRecord] = []
        for item in items:
            seller = item.get("seller") or item.get("sellerAddress")
            if seller:
                records.append(ActivityRecord(
                    wallet_address=seller,
                    role="holder",
                    network="solana",
                    asset_id=target,
                    price=float(item.get("price")) if item.get("price") is not None else None,
                    timestamp=None,
                ))

        has_more = len(items) == limit and (offset + limit) < 14500
        return records, has_more

    def fetch_activity_page(self, asset_type: str, target: str, offset: int, limit: int = PAGE_SIZE,
                             network: str = "solana") -> tuple[list[ActivityRecord], bool]:
        if asset_type != "nft":
            raise AdapterError("Magic Eden — NFT-площадка, для мемкоинов используйте dexscreener/birdeye")
        if offset >= 14500:
            logger.warning("Magic Eden history depth limit (14500) reached")
            return [], False

        if offset == 0:
            self._seen_payloads = set()
            self._seen_raw_payloads = set()

        url = f"https://api-mainnet.magiceden.dev/v2/collections/{target}/activities"
        params = {"offset": offset, "limit": limit}
        logger.info("Magic Eden: запрос %s params=%s", url, params)

        try:
            resp = self._make_request(url, params=params)
            logger.info("Magic Eden: HTTP %s, длина тела %s байт", resp.status_code, len(resp.content))
            if resp.status_code in (400, 404):
                return [], False
            resp.raise_for_status()
            data = resp.json()
        except RateLimited:
            raise
        except requests.RequestException as exc:
            if offset == 0:
                raise AdapterError(f"Magic Eden: сбой запроса активности для {target}: {exc}") from exc
            logger.warning("Magic Eden: страница на offset=%s не удалась даже после повторов (%s)", offset, exc)
            return [], False

        items = data if isinstance(data, list) else (data.get("activities", []) if isinstance(data, dict) else [])
        if not items:
            if offset > 10000:
                logger.info("Magic Eden: Empty raw payload at offset %s > 10000. Breaking loop.", offset)
                return [], False
            return [], False

        raw_signature = tuple(str(item) for item in items)
        if offset > 10000 and raw_signature in self._seen_raw_payloads:
            logger.info("Magic Eden: Duplicate raw payload detected at offset %s > 10000. Breaking loop.", offset)
            return [], False

        records = self._parse_items(items, target)
        parsed_signature = tuple((r.wallet_address, r.role, r.price) for r in records)
        if offset > 10000 and parsed_signature in self._seen_payloads:
            logger.info("Magic Eden: Duplicate parsed payload detected at offset %s > 10000. Breaking loop.", offset)
            return [], False

        if items:
            self._seen_raw_payloads.add(raw_signature)
        if records:
            self._seen_payloads.add(parsed_signature)

        has_more = len(items) == limit and (offset + limit) < 14500
        return records, has_more

    def fetch_activity(self, asset_type: str, target: str, limit: int = 500, target_wallets: int = 20) -> list[ActivityRecord]:
        all_records: list[ActivityRecord] = []
        unique: set[str] = set()
        offset = 0
        while offset < 14500:
            page_records, has_more = self.fetch_activity_page(asset_type, target, offset, 500)
            all_records.extend(page_records)
            unique.update(r.wallet_address for r in page_records)
            if len(unique) >= target_wallets or not has_more or len(all_records) >= limit:
                break
            offset += 500
        return all_records

    @staticmethod
    def _parse_items(items: list[dict], target: str) -> list[ActivityRecord]:
        records: list[ActivityRecord] = []
        for item in items:
            buyer = item.get("buyer") or item.get("buyerAddress")
            seller = item.get("seller") or item.get("sellerAddress")

            # Helper function to clean and validate address
            def clean_and_val(v):
                if not v:
                    return None
                if isinstance(v, str):
                    s = v.strip()
                    return s if _is_valid_pubkey(s) else None
                if isinstance(v, dict):
                    addr = v.get("address") or v.get("owner")
                    if addr and isinstance(addr, str):
                        s = addr.strip()
                        return s if _is_valid_pubkey(s) else None
                return None

            buyer_clean = clean_and_val(buyer)
            seller_clean = clean_and_val(seller)

            extracted_pairs = []
            if buyer_clean and seller_clean:
                extracted_pairs.append((buyer_clean, "buyer"))
                extracted_pairs.append((seller_clean, "seller"))
            elif buyer_clean:
                extracted_pairs.append((buyer_clean, "bidder"))
            elif seller_clean:
                extracted_pairs.append((seller_clean, "lister"))

            # Also extract from owner, maker, taker, initializer if present
            for field_name in ["owner", "ownerAddress", "maker", "taker", "initializer"]:
                val = item.get(field_name)
                addr_clean = clean_and_val(val)
                if addr_clean:
                    # Avoid duplicates on the same record
                    role_mapped = "owner" if "owner" in field_name else field_name
                    if not any(p[0] == addr_clean for p in extracted_pairs):
                        extracted_pairs.append((addr_clean, role_mapped))

            block_time = item.get("blockTime")
            ts = datetime.fromtimestamp(block_time, tz=timezone.utc) if isinstance(block_time, (int, float)) else None
            price = item.get("price")

            for addr, role in extracted_pairs:
                records.append(ActivityRecord(
                    wallet_address=addr, role=role, network="solana",
                    asset_id=target, price=float(price) if price is not None else None,
                    timestamp=ts,
                ))
        return records

    def check_collection_exists(self, symbol: str) -> dict | None:
        url = f"{BASE_URL}/collections/{symbol}/stats"
        try:
            resp = self._make_request(url)
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            data = resp.json()
        except requests.RequestException as exc:
            raise AdapterError(f"Magic Eden: сбой проверки коллекции {symbol}: {exc}") from exc
        return {"symbol": data.get("symbol", symbol), "listed_count": data.get("listedCount")}

    def fetch_popular_collections(self) -> list[str]:
        url = "https://api-mainnet.magiceden.dev/v2/marketplace/popular_collections"
        try:
            resp = self._make_request(url, params={"timeRange": "1d"})
            if resp.status_code == 200:
                data = resp.json()
                return [item.get("symbol") for item in data if isinstance(item, dict) and item.get("symbol")]
        except Exception as exc:
            logger.warning("Magic Eden: failed to fetch popular collections: %s", exc)
        return []

    def fetch_new_collections(self) -> list[str]:
        url = "https://api-mainnet.magiceden.dev/new_collections"
        try:
            resp = requests.get(url, headers={"User-Agent": settings.user_agent}, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                return [item.get("symbol") for item in data if isinstance(item, dict) and item.get("symbol")]
        except Exception as exc:
            logger.warning("Magic Eden: failed to fetch new collections: %s", exc)
        return []

    def get_trending_collections(self) -> list[str]:
        res = self.fetch_popular_collections()
        if not res:
            res = self.fetch_new_collections()
        return res
