"""
Схема БД Steal Bones. Реализовано на стандартном sqlite3 (без SQLAlchemy —
см. README.md, раздел "Отличия от исходного ТЗ", о причине).

Таблица и уникальный ключ (address, network) в точности соответствуют
разделу 3.5 технического задания.

СЕДЬМОЙ РАУНД (08.08.2026) — по прямым запросам пользователя:
- extra_assets (TEXT, JSON) — "хотя бы часть активов" сверх нативного
  баланса: известные стейблкоины/SPL-токены и т.п. (см. adapters/balances/
  solana.py::get_token_holdings, evm.py::get_known_token_balances). Хранится
  как JSON-словарь {"USDC": 1500.0, "прочих SPL-токенов": 4}, а не
  отдельными колонками — состав known-token списка будет расти, жёсткая
  схема под это не подходит.
- times_skipped / last_skipped_at — счётчик, сколько раз кошелёк попадал в
  кэш (24ч) и не перепроверялся заново, вместо того чтобы это нигде не
  фиксировать (запрос пользователя — видеть отдельно, что "застряло" в кэше).

CREATE TABLE IF NOT EXISTS не меняет схему уже существующей БД — поэтому
миграция ниже (ALTER TABLE, если колонки ещё нет) обязательна, иначе у
пользователей с БД, созданной до этого раунда, просто будет падать запись.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS wallets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    address TEXT NOT NULL,
    network TEXT NOT NULL,
    asset_type TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    collection_or_token TEXT,
    role TEXT,
    balance REAL,
    balance_checked_at TEXT,
    extra_assets TEXT,
    times_skipped INTEGER NOT NULL DEFAULT 0,
    last_skipped_at TEXT,
    discord TEXT,
    twitter TEXT,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(address, network)
);

CREATE INDEX IF NOT EXISTS idx_wallets_network ON wallets(network);
CREATE INDEX IF NOT EXISTS idx_wallets_balance ON wallets(balance);
CREATE UNIQUE INDEX IF NOT EXISTS idx_wallets_address_chain ON wallets(address, network);

-- Дневная квота API-ключей (для Rate Limit Guard, раздел 7.1 ТЗ)
CREATE TABLE IF NOT EXISTS quota_usage (
    source TEXT NOT NULL,
    key_label TEXT NOT NULL,
    day TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (source, key_label, day)
);

-- Таблица настроек API-ключей
CREATE TABLE IF NOT EXISTS settings (
    id INTEGER PRIMARY KEY DEFAULT 1,
    helius_api_key TEXT,
    opensea_api_key TEXT,
    updated_at TEXT
);
"""

# (имя_колонки, DDL-фрагмент) — применяются к УЖЕ существующим БД, которые
# были созданы до появления этих колонок. SCHEMA выше отвечает только за
# СОЗДАНИЕ таблицы с нуля, ALTER TABLE ниже — за уже существующие файлы.
_MIGRATIONS: list[tuple[str, str]] = [
    ("extra_assets", "ALTER TABLE wallets ADD COLUMN extra_assets TEXT"),
    ("times_skipped", "ALTER TABLE wallets ADD COLUMN times_skipped INTEGER NOT NULL DEFAULT 0"),
    ("last_skipped_at", "ALTER TABLE wallets ADD COLUMN last_skipped_at TEXT"),
]


def get_connection(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _apply_migrations(conn: sqlite3.Connection) -> None:
    existing = {row[1] for row in conn.execute("PRAGMA table_info(wallets)")}
    for column, ddl in _MIGRATIONS:
        if column not in existing:
            conn.execute(ddl)


def init_db(db_path: Path) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        _apply_migrations(conn)
        conn.commit()
    finally:
        conn.close()
