"""
Реестр адаптеров — раздел 4 ТЗ. Добавление новой площадки/сети = один новый
класс + одна строка регистрации здесь, остальной код не меняется.
"""

from __future__ import annotations

from adapters.balances.base import BalanceAdapter
from adapters.balances.bitcoin import BitcoinBalanceAdapter
from adapters.balances.evm import make_evm_adapters
from adapters.balances.solana import SolanaBalanceAdapter
from adapters.balances.sui import SuiBalanceAdapter
from adapters.balances.tron import TronBalanceAdapter
from adapters.marketplaces.base import MarketplaceAdapter
from adapters.marketplaces.birdeye import BirdeyeAdapter
from adapters.marketplaces.blur import BlurAdapter
from adapters.marketplaces.dexscreener import DexScreenerAdapter
from adapters.marketplaces.looksrare import LooksRareAdapter
from adapters.marketplaces.magic_eden import MagicEdenAdapter
from adapters.marketplaces.opensea import OpenSeaAdapter
from adapters.marketplaces.rarible import RaribleAdapter
from adapters.marketplaces.tensor import TensorAdapter

MARKETPLACE_ADAPTERS: dict[str, MarketplaceAdapter] = {
    "magic_eden": MagicEdenAdapter(),
    "opensea": OpenSeaAdapter(),
    "tensor": TensorAdapter(),
    "blur": BlurAdapter(),
    "rarible": RaribleAdapter(),
    "looksrare": LooksRareAdapter(),
    "dexscreener": DexScreenerAdapter(),
    "birdeye": BirdeyeAdapter(),
}

BALANCE_ADAPTERS: dict[str, BalanceAdapter] = {
    "solana": SolanaBalanceAdapter(),
    "bitcoin": BitcoinBalanceAdapter(),
    "tron": TronBalanceAdapter(),
    "sui": SuiBalanceAdapter(),
    **make_evm_adapters(),  # ethereum, bnb, base, arbitrum, polygon, avalanche
}

# Топ-10 сетей из раздела 3.3 ТЗ (Solana обязательна)
NETWORKS: list[str] = [
    "solana", "ethereum", "tron", "bnb", "base",
    "arbitrum", "polygon", "bitcoin", "avalanche", "sui",
]

# Какие площадки доступны для какого типа актива (раздел 3.2 ТЗ)
PLATFORMS_BY_ASSET_TYPE: dict[str, list[str]] = {
    "nft": ["magic_eden", "opensea", "tensor", "blur", "rarible", "looksrare"],
    "memecoin": ["birdeye"],  # dexscreener намеренно исключён — см. dexscreener.py (не отдаёт кошельки)
}

assert set(NETWORKS) == set(BALANCE_ADAPTERS.keys()), "Список сетей и реестр балансовых адаптеров разошлись"
