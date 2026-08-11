"""
Конфигурация Steal Bones.

Загружает .env вручную (без python-dotenv — в окружении, где писался этот
проект, не было сетевого доступа для pip install, поэтому все зависимости
сведены к тем, что можно гарантированно поставить/использовать offline).
Формат .env — обычный KEY=VALUE, строки с # игнорируются.
"""

from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv
from dataclasses import dataclass, field

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
DB_PATH = BASE_DIR / 'db' / 'steal_bones.db'
EXPORT_DIR = BASE_DIR / 'exports'

class Config:
    def __init__(self):
        self.reload()

    def reload(self):
        if ENV_PATH.exists():
            load_dotenv(dotenv_path=ENV_PATH, override=True)

        self.HELIUS_API_KEY = self._clean_key(os.getenv("HELIUS_API_KEY", ""))
        self.OPENSEA_API_KEY = self._clean_key(os.getenv("OPENSEA_API_KEY", ""))
        self.MAGIC_EDEN_API_KEY = self._clean_key(os.getenv("MAGIC_EDEN_API_KEY", ""))
        self.SOLANA_RPC_URL = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

    @property
    def HELIUS_RPC_URL(self) -> str:
        key = self.HELIUS_API_KEY
        if not key:
            try:
                if settings.helius_keys:
                    key = settings.helius_keys[0]
            except Exception:
                pass
        if key:
            return f"https://mainnet.helius-rpc.com/?api-key={key}"
        return self.SOLANA_RPC_URL

    @staticmethod
    def _clean_key(val: str) -> str:
        if not val:
            return ""
        return str(val).strip().strip("'\"").strip()

config = Config()

def _keys_from_env(var_name: str) -> list[str]:
    raw = os.environ.get(var_name, '')
    return [k.strip().strip("'\"") for k in raw.split(',') if k.strip().strip("'\"")]

@dataclass
class Settings:
    db_path: Path = DB_PATH
    export_dir: Path = EXPORT_DIR
    etherscan_keys: list[str] = field(default_factory=lambda: _keys_from_env('ETHERSCAN_API_KEYS'))

    _helius_keys: list[str] | None = None
    _opensea_key: str | None = None

    @property
    def helius_keys(self) -> list[str]:
        if self._helius_keys is not None:
            return self._helius_keys
        if config.HELIUS_API_KEY:
            return [k.strip() for k in config.HELIUS_API_KEY.split(',') if k.strip()]
        return []

    @helius_keys.setter
    def helius_keys(self, value):
        self._helius_keys = value

    @property
    def opensea_key(self) -> str:
        if self._opensea_key is not None:
            return self._opensea_key
        return config.OPENSEA_API_KEY

    @opensea_key.setter
    def opensea_key(self, value):
        self._opensea_key = value

    tensor_key: str = field(default_factory=lambda: os.environ.get('TENSOR_API_KEY', '').strip().strip("'\""))
    rarible_key: str = field(default_factory=lambda: os.environ.get('RARIBLE_API_KEY', '').strip().strip("'\""))
    looksrare_key: str = field(default_factory=lambda: os.environ.get('LOOKSRARE_API_KEY', '').strip().strip("'\""))
    trongrid_key: str = field(default_factory=lambda: os.environ.get('TRONGRID_API_KEY', '').strip().strip("'\""))
    xverse_key: str = field(default_factory=lambda: os.environ.get('XVERSE_API_KEY', '').strip().strip("'\""))
    blockberry_key: str = field(default_factory=lambda: os.environ.get('BLOCKBERRY_API_KEY', '').strip().strip("'\""))
    birdeye_key: str = field(default_factory=lambda: os.environ.get('BIRDEYE_API_KEY', '').strip().strip("'\""))
    user_agent: str = 'StealBones/1.0 (+wallet-analytics-tool; contact: set-your-contact-here)'
    balance_recheck_hours: int = int(os.environ.get('BALANCE_RECHECK_HOURS', '24'))

settings = Settings()
settings.export_dir.mkdir(parents=True, exist_ok=True)
settings.db_path.parent.mkdir(parents=True, exist_ok=True)

KEY_ENV_MAP = {
    'etherscan_keys': 'ETHERSCAN_API_KEYS',
    'opensea_key': 'OPENSEA_API_KEY',
    'tensor_key': 'TENSOR_API_KEY',
    'rarible_key': 'RARIBLE_API_KEY',
    'looksrare_key': 'LOOKSRARE_API_KEY',
    'helius_keys': 'HELIUS_API_KEY',
    'trongrid_key': 'TRONGRID_API_KEY',
    'xverse_key': 'XVERSE_API_KEY',
    'blockberry_key': 'BLOCKBERRY_API_KEY',
    'birdeye_key': 'BIRDEYE_API_KEY'
}

def save_env_values(values: dict[str, str]) -> None:
    existing = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, _, val = line.partition('=')
            existing[key.strip()] = val.strip()

    for k, v in values.items():
        if v:
            existing[k] = v
            os.environ[k] = v
        else:
            existing.pop(k, None)
            os.environ.pop(k, None)

    lines = []
    for k, v in existing.items():
        lines.append(f"{k}={v}")
    lines_str = '\n'.join(lines)
    ENV_PATH.write_text(lines_str + '\n', encoding='utf-8')
    config.reload()
