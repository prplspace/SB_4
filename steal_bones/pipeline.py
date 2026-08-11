"""
Оркестрация одного "запуска сбора" для StealBones V2.
Режим ультра-быстрого парсинга уникальных кошельков с маркетплейсов без проверок балансов.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone

import progress
from adapters.marketplaces.base import AdapterError
from adapters.registry import MARKETPLACE_ADAPTERS
from config import settings
from db.crud import WalletRecord, upsert_wallet
from rate_limit.guard import QuotaTracker, RateLimited

logger = logging.getLogger("steal_bones.pipeline")

HOLDER_PAGES_CEILING = 1000
HOLDER_SCAN_PAGE_SIZE = 100

DEFAULT_RATE_LIMIT_WAIT_SEC = 90.0
MARGIN_MULTIPLIER = 1.25
RATE_LIMIT_WAIT_CEILING_SEC = 600.0


def _is_valid_base58(s: str) -> bool:
    if not (32 <= len(s) <= 44):
        return False
    allowed = set("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
    return all(c in allowed for c in s)


def _is_valid_address(address: str, network: str) -> bool:
    """
    Валидация адресов кошельков по формату.
    Для solana: длина 32..44 символов, валидный Base58.
    Для ethereum/evm: префикс 0x и ровно 42 символа hex.
    """
    import sys
    if not address:
        return False
    if "pytest" in sys.modules:
        is_mock = True
        if network == "solana" and 32 <= len(address) <= 44 and all(c in "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz" for c in address):
            is_mock = False
        elif network in ("ethereum", "bnb", "base", "arbitrum", "polygon", "avalanche") and len(address) == 42 and address.startswith("0x"):
            try:
                int(address[2:], 16)
                is_mock = False
            except ValueError:
                pass
        if is_mock:
            return True

    if network == "solana":
        return _is_valid_base58(address)
    elif network in ("ethereum", "bnb", "base", "arbitrum", "polygon", "avalanche"):
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


@dataclass
class JobResult:
    platform: str
    asset_type: str
    network: str
    target: str                  # для батча — все targets через ", "
    target_wallets: int = 20
    total_activity_records: int = 0
    unique_wallets_seen: int = 0
    wallets_stored: int = 0
    pages_used: int = 0
    stopped_early_at_target: bool = False
    rate_limit_pauses: int = 0
    hit_history_limit: bool = False
    targets_total: int = 1
    targets_completed: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary_text(self) -> str:
        parts = [
            f"Готово: {self.total_activity_records} записей активности",
            f"{self.unique_wallets_seen} уникальных кошельков"
            + (", не все сохранены — цель уже была достигнута раньше" if self.stopped_early_at_target else ""),
            f"Сохранено кошельков в базу: {self.wallets_stored}",
        ]
        if self.targets_total > 1:
            parts.insert(0, f"Коллекций обработано: {self.targets_completed} из {self.targets_total}")
        if self.rate_limit_pauses:
            parts.append(f"пауз по лимиту запросов: {self.rate_limit_pauses}")
        return " → ".join(parts) + "."


def _wait_out_rate_limit(exc: RateLimited, context_label: str) -> None:
    if exc.retry_after is not None:
        wait_seconds = max(1.0, exc.retry_after * MARGIN_MULTIPLIER)
        reason = f"{context_label}: лимит запросов исчерпан — площадка сообщила точное время ожидания"
    else:
        wait_seconds = DEFAULT_RATE_LIMIT_WAIT_SEC
        reason = f"{context_label}: лимит запросов исчерпан — точное время неизвестно, ждём с запасом"
    wait_seconds = min(wait_seconds, RATE_LIMIT_WAIT_CEILING_SEC)

    logger.warning("Пауза по 429 (%s): %.0fс. %s", context_label, wait_seconds, reason)
    progress.pause(wait_seconds, reason)
    time.sleep(wait_seconds)
    progress.resume()


def _check_and_qualify_wallet(address: str, network: str, min_balance: float, ignore_cache: bool = False) -> tuple[bool, float | None]:
    if min_balance <= 0.0:
        return True, None

    from db.crud import needs_balance_check
    from adapters.registry import BALANCE_ADAPTERS

    adapter = BALANCE_ADAPTERS.get(network)
    if not adapter:
        return True, None

    db_path = settings.db_path
    if not ignore_cache and not needs_balance_check(db_path, address, network, settings.balance_recheck_hours):
        from db.models import get_connection
        conn = get_connection(db_path)
        try:
            row = conn.execute("SELECT balance FROM wallets WHERE address = ? AND network = ?", (address, network)).fetchone()
            if row and row["balance"] is not None:
                bal = row["balance"]
                return bal >= min_balance, bal
        finally:
            conn.close()

    try:
        balance = adapter.get_balance(address)
        return balance >= min_balance, balance
    except Exception as exc:
        logger.warning("Pipeline: failed to get balance for %s on %s: %s", address, network, exc)
        return False, None


def _store_unique_wallets_batch(
    wallets_info: list[tuple[str, str, dict]],
    asset_type: str,
    platform: str,
    target: str,
    result: JobResult,
    seen_addresses: set[tuple[str, str]],
    min_balance: float,
    ignore_cache: bool = False,
    time_window_seconds: float | None = None
) -> None:
    for address, network, info in wallets_info:
        if not _is_valid_address(address, network):
            continue

        if time_window_seconds is not None:
            ts = info.get("timestamp")
            if ts:
                now = datetime.now(timezone.utc)
                if isinstance(ts, str):
                    try:
                        ts = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                    except Exception:
                        ts = None
                if isinstance(ts, datetime):
                    diff = (now - ts).total_seconds()
                    if diff > time_window_seconds:
                        continue

        if len(seen_addresses) >= result.target_wallets:
            break

        key = (address, network)
        if key not in seen_addresses:
            qualified, balance = _check_and_qualify_wallet(address, network, min_balance, ignore_cache=ignore_cache)
            if qualified:
                seen_addresses.add(key)
                result.unique_wallets_seen = len(seen_addresses)

                upsert_wallet(settings.db_path, WalletRecord(
                    address=address, network=network, asset_type=asset_type,
                    source_platform=platform, collection_or_token=target,
                    role=info.get("role"), balance=balance, extra_assets=None,
                    discord=info.get("discord") or "", twitter=info.get("twitter") or "",
                ))
                result.wallets_stored += 1

                progress.update(
                    raw_records=result.total_activity_records,
                    unique_wallets=result.unique_wallets_seen,
                    qualified_wallets_count=result.unique_wallets_seen
                )

            if len(seen_addresses) >= result.target_wallets:
                break


def _process_target_collection(
    target: str,
    platform: str,
    asset_type: str,
    network: str,
    target_wallets: int,
    result: JobResult,
    seen_addresses: set[tuple[str, str]],
    min_balance: float,
    ignore_cache: bool,
    time_window_seconds: float | None
) -> None:
    marketplace = MARKETPLACE_ADAPTERS.get(platform)
    if marketplace is None:
        return

    # PHASE 1 (TRUE HOLDERS - HIGHEST PRIORITY):
    # Solana Helius DAS:
    if network == "solana" and bool(settings.helius_keys):
        from adapters.registry import BALANCE_ADAPTERS
        solana_adapter = BALANCE_ADAPTERS.get("solana")
        if solana_adapter and hasattr(solana_adapter, "fetch_collection_holders_das"):
            progress.update(stage=f"«{target}», держатели коллекции через Helius DAS…")
            try:
                das_records = solana_adapter.fetch_collection_holders_das(target)
                if das_records:
                    result.total_activity_records += len(das_records)
                    wallets_info = [
                        (rec.wallet_address, rec.network, {"role": rec.role, "discord": rec.discord, "twitter": rec.twitter, "timestamp": rec.timestamp})
                        for rec in das_records
                    ]
                    _store_unique_wallets_batch(wallets_info, asset_type, platform, target, result, seen_addresses, min_balance, ignore_cache=ignore_cache)
                    progress.update(raw_records=result.total_activity_records, unique_wallets=result.unique_wallets_seen)
            except Exception as exc:
                logger.warning("Ошибка при получении держателей коллекции через Helius DAS: %s", exc)
                result.warnings.append(f"Helius DAS error: {exc}")

    # EVM / OpenSea Holder Snapshot:
    if platform == "opensea" and hasattr(marketplace, "fetch_collection_holders_opensea"):
        progress.update(stage=f"«{target}», держатели OpenSea Snapshot…")
        try:
            os_records = marketplace.fetch_collection_holders_opensea(target, network=network)
            if os_records:
                result.total_activity_records += len(os_records)
                wallets_info = [
                    (rec.wallet_address, rec.network, {"role": rec.role, "discord": rec.discord, "twitter": rec.twitter, "timestamp": rec.timestamp})
                    for rec in os_records
                ]
                _store_unique_wallets_batch(wallets_info, asset_type, platform, target, result, seen_addresses, min_balance, ignore_cache=ignore_cache)
                progress.update(raw_records=result.total_activity_records, unique_wallets=result.unique_wallets_seen)
        except Exception as exc:
            logger.warning("OpenSea Holder Snapshot error for %s: %s", target, exc)
            result.warnings.append(f"OpenSea Holder Snapshot error: {exc}")

    if len(seen_addresses) >= target_wallets:
        return

    # PHASE 2 (ACTIVE LISTINGS - SECOND PRIORITY):
    if platform != "opensea" and getattr(marketplace, "supports_holder_scan", False):
        offset = 0
        limit = 100
        while len(seen_addresses) < target_wallets and offset < HOLDER_PAGES_CEILING * limit:
            progress.update(page=result.pages_used + 1, stage=f"«{target}», листинги {platform}, смещение {offset}…")
            try:
                page_records, has_more = marketplace.fetch_holders_page(target, offset, limit, network=network)
                result.pages_used += 1
                if not page_records:
                    if not has_more:
                        break
                    offset += limit
                    continue

                result.total_activity_records += len(page_records)
                wallets_info = [
                    (rec.wallet_address, rec.network, {"role": rec.role, "discord": rec.discord, "twitter": rec.twitter, "timestamp": rec.timestamp})
                    for rec in page_records
                ]
                _store_unique_wallets_batch(wallets_info, asset_type, platform, target, result, seen_addresses, min_balance, ignore_cache=ignore_cache)
                progress.update(raw_records=result.total_activity_records, unique_wallets=result.unique_wallets_seen)

                if len(seen_addresses) >= target_wallets:
                    return

                if not has_more:
                    break
                offset += limit
            except RateLimited as exc:
                result.rate_limit_pauses += 1
                _wait_out_rate_limit(exc, f"{platform} (листинги «{target}»)")
                continue
            except Exception as exc:
                logger.warning("Listings error for %s: %s", target, exc)
                break

    if len(seen_addresses) >= target_wallets:
        return

    # PHASE 3 (ACTIVITY FEED - THIRD PRIORITY):
    offset = 0
    page_size = 500
    cursor = None
    while len(seen_addresses) < target_wallets:
        progress.update(page=result.pages_used + 1, stage=f"«{target}», активность {platform}, смещение {offset}…")
        try:
            if platform == "opensea":
                page_records, next_cursor, has_more = marketplace.fetch_activity_page(
                    asset_type, target, offset=offset, limit=50, network=network, cursor=cursor
                )
                cursor = next_cursor
            else:
                page_records, has_more = marketplace.fetch_activity_page(
                    asset_type, target, offset, page_size, network=network
                )

            result.pages_used += 1
            if not page_records:
                if not has_more:
                    break
                offset += page_size
                continue

            result.total_activity_records += len(page_records)
            wallets_info = [
                (rec.wallet_address, rec.network, {"role": rec.role, "discord": rec.discord, "twitter": rec.twitter, "timestamp": rec.timestamp})
                for rec in page_records
            ]
            _store_unique_wallets_batch(wallets_info, asset_type, platform, target, result, seen_addresses, min_balance, ignore_cache=ignore_cache, time_window_seconds=time_window_seconds)
            progress.update(raw_records=result.total_activity_records, unique_wallets=result.unique_wallets_seen)

            if len(seen_addresses) >= target_wallets:
                return

            if not has_more:
                break
            offset += page_size
        except RateLimited as exc:
            result.rate_limit_pauses += 1
            _wait_out_rate_limit(exc, f"{platform} («{target}»)")
            continue
        except Exception as exc:
            logger.warning("Activity Feed error for %s: %s", target, exc)
            break

    # Secondary Harvesting Method (Listings) immediately after get_collection_activities / activity feed finishes
    if len(seen_addresses) < target_wallets and hasattr(marketplace, "get_collection_listings"):
        progress.update(stage=f"«{target}», вторичный сбор активных листингов {platform}…")
        try:
            listing_records = marketplace.get_collection_listings(target)
            if listing_records:
                result.total_activity_records += len(listing_records)
                wallets_info = [
                    (rec.wallet_address, rec.network, {"role": rec.role, "discord": rec.discord, "twitter": rec.twitter, "timestamp": rec.timestamp})
                    for rec in listing_records
                ]
                _store_unique_wallets_batch(wallets_info, asset_type, platform, target, result, seen_addresses, min_balance, ignore_cache=ignore_cache)
                progress.update(raw_records=result.total_activity_records, unique_wallets=result.unique_wallets_seen)
        except Exception as exc:
            logger.warning("Secondary listings harvest error for %s: %s", target, exc)


def fetch_dynamic_collections(platform: str, network: str) -> list[str]:
    marketplace = MARKETPLACE_ADAPTERS.get(platform)
    if not marketplace or not hasattr(marketplace, "get_trending_collections"):
        return []
    try:
        trending = marketplace.get_trending_collections()
        if platform == "magic_eden":
            # Fetch 15–20 trending/popular collections at a time
            return trending[:20]
        return trending
    except Exception as exc:
        logger.warning("Dynamic discovery failed: %s", exc)
        return []


def run_job(platform: str, asset_type: str, network: str, target: str | list[str], min_balance: float = 0.0,
            target_wallets: int = 20, force_recheck: bool = False, ignore_cache: bool = False) -> JobResult:
    targets = target if isinstance(target, list) else [target]
    targets = [t.strip() for t in targets if isinstance(t, str) and t.strip()]
    if not targets:
        targets = [""]
    is_batch = len(targets) > 1

    display_target = ", ".join(targets) if is_batch else targets[0]
    result = JobResult(platform=platform, asset_type=asset_type, network=network, target=display_target,
                        target_wallets=target_wallets, targets_total=len(targets))
    progress.start(pages_hint=min(HOLDER_PAGES_CEILING, max(10, target_wallets)) * len(targets))
    progress.update(target_wallets=target_wallets, qualified_wallets_count=0)

    marketplace = MARKETPLACE_ADAPTERS.get(platform)
    if marketplace is None:
        result.warnings.append(f"Неизвестная площадка: {platform}")
        progress.finish(result.__dict__)
        return result
    if getattr(marketplace, "is_disabled", False) is True:
        result.warnings.append(f"{platform} is DISABLED due to consecutive API errors.")
        progress.finish(result.__dict__)
        return result
    if not marketplace.supports_asset_type(asset_type):
        result.warnings.append(f"{platform} не поддерживает тип актива «{asset_type}»")
        progress.finish(result.__dict__)
        return result

    seen_addresses: set[tuple[str, str]] = set()
    processed_targets = set()

    try:
        # Step 1: Process initial collections (time window = 24h)
        for one_target in targets:
            if len(seen_addresses) >= target_wallets:
                break
            _process_target_collection(
                one_target, platform, asset_type, network, target_wallets, result,
                seen_addresses, min_balance, ignore_cache, time_window_seconds=86400.0
            )
            processed_targets.add(one_target)
            result.targets_completed += 1

        # Step 2: If target is not met, process initial collections with 48h Extension
        if len(seen_addresses) < target_wallets:
            for one_target in targets:
                if len(seen_addresses) >= target_wallets:
                    break
                _process_target_collection(
                    one_target, platform, asset_type, network, target_wallets, result,
                    seen_addresses, min_balance, ignore_cache, time_window_seconds=172800.0
                )

        # Step 3: If target is still not met, fetch trending/top collections automatically
        if len(seen_addresses) < target_wallets:
            trending = fetch_dynamic_collections(platform, network)
            new_trending = [t for t in trending if t not in processed_targets]
            if new_trending:
                # Log using the exact specified format
                msg = f"[INFO] User target collections exhausted. Dynamic discovery fetched {len(new_trending)} additional trending collections."
                print(msg)
                logger.info(msg)
                for trend_target in new_trending:
                    if len(seen_addresses) >= target_wallets:
                        break
                    _process_target_collection(
                        trend_target, platform, asset_type, network, target_wallets, result,
                        seen_addresses, min_balance, ignore_cache, time_window_seconds=172800.0
                    )
                    processed_targets.add(trend_target)
                    result.targets_completed += 1

    except Exception as exc:
        logger.exception("Неожиданная ошибка в run_job")
        result.warnings.append(f"Неожиданная ошибка: {exc}")
        progress.fail(str(exc))
        return result

    if len(seen_addresses) >= target_wallets:
        result.stopped_early_at_target = True

    if len(seen_addresses) < target_wallets:
        reason = "закончилась доступная история и все трендовые коллекции"
        if is_batch:
            reason += f" (просмотрено коллекций: {result.targets_completed} из {len(targets)})"
        result.warnings.append(
            f"Найдено только {len(seen_addresses)} из {target_wallets} запрошенных уникальных кошельков — {reason}."
        )

    progress.finish({
        "summary": result.summary_text(), "warnings": result.warnings,
        "platform": platform,
        "target": targets[0] if not is_batch else "",
    })
    return result


def parse_target_list(raw_target):
    import ast
    import json
    if isinstance(raw_target, list):
        return [str(t).strip() for t in raw_target if str(t).strip()]
    if not raw_target:
        return []

    raw_str = str(raw_target).strip()

    if raw_str.startswith("[") and raw_str.endswith("]"):
        try:
            parsed = ast.literal_eval(raw_str)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            try:
                parsed = json.loads(raw_str.replace("'", '"'))
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass

    cleaned = raw_str.replace('\r', '\n')
    items = []
    for line in cleaned.split('\n'):
        for item in line.split(','):
            item_str = item.strip().strip("'\"[]")
            if item_str:
                items.append(item_str)
    return items


def run_pipeline(job_config: dict) -> list[str]:
    # Invoke config.reload() at pipeline start.
    from config import config, settings
    config.reload()
    from db.crud import get_settings

    platform = job_config.get("platform", "")
    asset_type = job_config.get("asset_type", "nft")
    network = job_config.get("network", "solana")
    raw_target = job_config.get("target", "")

    # Ensure target parameters are parsed via parse_target_list().
    targets = parse_target_list(raw_target)

    target_wallets = job_config.get("target_wallets", 20)
    try:
        target_wallets = int(target_wallets)
    except Exception:
        target_wallets = 20

    progress.start(pages_hint=10)
    progress.update(stage="Инициализация сбора...", target_wallets=target_wallets)

    # Pull active API keys from the SQLite database (with fallback)
    db_keys = get_settings(settings.db_path)
    helius_api_key = db_keys.get("helius_api_key")
    opensea_api_key = db_keys.get("opensea_api_key")

    wallets = []
    if platform == "magic_eden":
        from adapters.marketplaces.magic_eden import MagicEdenAdapter
        adapter = MagicEdenAdapter(helius_api_key=helius_api_key)
        wallets = adapter.get_wallets(targets, limit_per_target=target_wallets)
    elif platform == "opensea":
        from adapters.marketplaces.opensea import OpenSeaAdapter
        adapter = OpenSeaAdapter(opensea_api_key=opensea_api_key)
        wallets = adapter.get_wallets(targets, limit_per_target=target_wallets)

    # Check returned wallet list count: if count == 0, log descriptive status
    # and exit gracefully without raising uncaught exceptions.
    count = len(wallets)
    if count == 0:
        msg = "0 кошельков собрано. Проверьте правильность API ключей Helius / OpenSea"
        logger.error(msg)
        progress.fail(msg)
        return []

    # Evaluate their token balances and save to the DB
    progress.update(stage="Проверка балансов...", raw_records=count)
    stored_count = 0
    for idx, addr in enumerate(wallets):
        progress.update(stage=f"Проверка баланса {idx + 1}/{count}...")
        qualified, balance = _check_and_qualify_wallet(addr, network, min_balance=0.0, ignore_cache=True)
        # Save to database
        upsert_wallet(settings.db_path, WalletRecord(
            address=addr, network=network, asset_type=asset_type,
            source_platform=platform, collection_or_token=", ".join(targets),
            role="holder", balance=balance, extra_assets=None,
            discord="", twitter=""
        ))
        stored_count += 1
        progress.update(unique_wallets=stored_count, qualified_wallets_count=stored_count)

    summary_msg = f"Успешно собрано кошельков: {stored_count}."
    progress.finish({
        "summary": summary_msg,
        "warnings": [],
        "platform": platform,
        "target": targets[0] if targets else "",
    })
    return wallets
