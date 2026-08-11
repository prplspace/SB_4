"""
CRUD-операции над таблицей wallets.

upsert_wallet реализует дедупликацию из раздела 3.5 ТЗ: (address, network) —
уникальный ключ, повторный запуск не создаёт дублей, а обновляет баланс и
last_seen. discord/twitter не затираются пустым значением при повторной
проверке (COALESCE на стороне Python, см. ниже).

СЕДЬМОЙ РАУНД (08.08.2026): extra_assets хранится как JSON (см. db/models.py
docstring) — сериализация/десериализация здесь, а не в pipeline.py, чтобы
формат хранения был виден в одном месте. record_skip — отдельная от
upsert_wallet функция: пропуск по кэшу НЕ трогает balance/balance_checked_at
(баланс не перепроверялся), только счётчик "сколько раз застрял в кэше" —
запрос пользователя видеть это отдельно, а не молча.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from db.models import get_connection


@dataclass
class WalletRecord:
    address: str
    network: str
    asset_type: str
    source_platform: str
    collection_or_token: Optional[str] = None
    role: Optional[str] = None
    balance: Optional[float] = None
    extra_assets: Optional[dict] = None
    discord: Optional[str] = None
    twitter: Optional[str] = None


def upsert_wallet(db_path: Path, rec: WalletRecord) -> None:
    conn = get_connection(db_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        extra_json = json.dumps(rec.extra_assets, ensure_ascii=False) if rec.extra_assets else None
        conn.execute(
            """
            INSERT INTO wallets
                (address, network, asset_type, source_platform, collection_or_token,
                 role, balance, balance_checked_at, extra_assets, discord, twitter, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address, network) DO UPDATE SET
                balance = excluded.balance,
                balance_checked_at = excluded.balance_checked_at,
                extra_assets = COALESCE(excluded.extra_assets, wallets.extra_assets),
                discord = COALESCE(NULLIF(excluded.discord, ''), wallets.discord),
                twitter = COALESCE(NULLIF(excluded.twitter, ''), wallets.twitter),
                collection_or_token = COALESCE(NULLIF(excluded.collection_or_token, ''), wallets.collection_or_token),
                role = COALESCE(NULLIF(excluded.role, ''), wallets.role),
                last_seen = excluded.last_seen
            """,
            (
                rec.address, rec.network, rec.asset_type, rec.source_platform,
                rec.collection_or_token, rec.role, rec.balance, now, extra_json,
                rec.discord, rec.twitter, now, now,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def save_wallets_batch(db_session: Path | sqlite3.Connection, wallets_data: list[WalletRecord]) -> None:
    if isinstance(db_session, (str, Path)):
        conn = get_connection(Path(db_session))
        should_close = True
    else:
        conn = db_session
        should_close = False

    try:
        now = datetime.now(timezone.utc).isoformat()
        rows = []
        for rec in wallets_data:
            extra_json = json.dumps(rec.extra_assets, ensure_ascii=False) if rec.extra_assets else None
            rows.append((
                rec.address, rec.network, rec.asset_type, rec.source_platform,
                rec.collection_or_token, rec.role, rec.balance, now, extra_json,
                rec.discord, rec.twitter, now, now,
            ))
        conn.executemany(
            """
            INSERT OR IGNORE INTO wallets
                (address, network, asset_type, source_platform, collection_or_token,
                 role, balance, balance_checked_at, extra_assets, discord, twitter, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
    finally:
        if should_close:
            conn.close()


def record_skip(db_path: Path, address: str, network: str) -> None:
    """Кошелёк встречен повторно и пропущен по кэшу баланса (needs_balance_check
    вернул False) — счётчик "раз пропущен" вместо того, чтобы это нигде не
    фиксировалось. Не трогает сам баланс — он и не перепроверялся."""
    conn = get_connection(db_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "UPDATE wallets SET times_skipped = times_skipped + 1, last_skipped_at = ? "
            "WHERE address = ? AND network = ?",
            (now, address, network),
        )
        conn.commit()
    finally:
        conn.close()


def get_last_checked(db_path: Path, address: str, network: str) -> Optional[datetime]:
    """Когда адрес последний раз проверялся в этой сети (для кэша баланса, раздел 3.5)."""
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT balance_checked_at FROM wallets WHERE address = ? AND network = ?",
            (address, network),
        ).fetchone()
        if row and row["balance_checked_at"]:
            return datetime.fromisoformat(row["balance_checked_at"])
        return None
    finally:
        conn.close()


def needs_balance_check(db_path: Path, address: str, network: str, recheck_hours: int) -> bool:
    last = get_last_checked(db_path, address, network)
    if last is None:
        return True
    return datetime.now(timezone.utc) - last > timedelta(hours=recheck_hours)


def list_wallets(
    db_path: Path,
    network: Optional[str] = None,
    min_balance: Optional[float] = None,
    collection: Optional[str] = None,
    order_by: str = "last_seen",
    order_dir: str = "DESC",
    only_skipped: bool = False,
) -> list[sqlite3.Row]:
    """only_skipped (седьмой раунд): страница "Пропущенные (в кэше)" на
    /results — те же фильтры network/collection, но независимо от balance,
    и только кошельки, у которых times_skipped > 0."""
    allowed_cols = {"address", "network", "balance", "first_seen", "last_seen", "source_platform", "times_skipped"}
    if order_by not in allowed_cols:
        order_by = "last_seen"
    order_dir = "DESC" if order_dir.upper() != "ASC" else "ASC"

    query = "SELECT * FROM wallets WHERE 1=1"
    params: list = []
    if network:
        query += " AND network = ?"
        params.append(network)
    if only_skipped:
        query += " AND times_skipped > 0"
    elif min_balance is not None:
        query += " AND balance >= ?"
        params.append(min_balance)
    if collection:
        query += " AND collection_or_token = ?"
        params.append(collection)
    query += f" ORDER BY {order_by} {order_dir}"

    conn = get_connection(db_path)
    try:
        return conn.execute(query, params).fetchall()
    finally:
        conn.close()


def count_wallets(db_path: Path) -> int:
    conn = get_connection(db_path)
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM wallets").fetchone()["c"]
    finally:
        conn.close()


def get_settings(db_path: Path) -> dict[str, str | None]:
    """
    Queries settings from sqlite DB settings table (row id=1).
    Falls back to environment variables / config if missing.
    """
    import os
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT helius_api_key, opensea_api_key FROM settings WHERE id = 1").fetchone()
        helius = None
        opensea = None
        if row:
            helius = row["helius_api_key"]
            opensea = row["opensea_api_key"]

        # Fallbacks
        if not helius:
            helius = os.getenv("HELIUS_API_KEY")
        if not opensea:
            opensea = os.getenv("OPENSEA_API_KEY")

        return {
            "helius_api_key": helius or "",
            "opensea_api_key": opensea or "",
        }
    finally:
        conn.close()


def update_settings(db_path: Path, helius_api_key: str, opensea_api_key: str) -> None:
    """
    Upserts settings in SQLite DB (row id=1).
    """
    conn = get_connection(db_path)
    try:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO settings (id, helius_api_key, opensea_api_key, updated_at)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                helius_api_key = excluded.helius_api_key,
                opensea_api_key = excluded.opensea_api_key,
                updated_at = excluded.updated_at
            """,
            (helius_api_key, opensea_api_key, now)
        )
        conn.commit()
    finally:
        conn.close()
