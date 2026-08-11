"""
Базовый интерфейс адаптеров площадок — раздел 4 ТЗ.

Реализовано на requests (не httpx — см. README, "Отличия от исходного ТЗ").
Синхронный HTTP осознанно: инструмент однопользовательский и локальный,
асинхронность здесь не даёт выигрыша, который окупил бы сложность.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass
class ActivityRecord:
    wallet_address: str
    role: str                   # "buyer" | "seller" | "bidder" | "lister" | "holder" (см. adapters седьмого раунда)
    network: str
    asset_id: str                # имя коллекции / тикер или адрес токена
    price: Optional[float]
    timestamp: Optional[datetime]
    discord: Optional[str] = None
    twitter: Optional[str] = None


class MarketplaceAdapter(ABC):
    name: str
    requires_key: bool = False
    default_daily_limit: int = 10_000  # эвристика по умолчанию, если у площадки нет явно
                                        # задокументированной суточной квоты (см. README про
                                        # необходимость сверки лимитов перед боевым запуском)
    # True только у адаптеров, реализующих fetch_activity_page (сейчас — только
    # Magic Eden). Если True, pipeline.py листает страницы сам и останавливается,
    # когда наберёт нужное число кошельков С ПРАВИЛЬНЫМ балансом (а не просто
    # найденных) — раздел про "target_wallets должен значить кошельки после
    # порога", исправлено по жалобе пользователя 04.08.2026.
    supports_deep_search: bool = False
    # ДОБАВЛЕНО (седьмой раунд, 08.08.2026) — True только у адаптеров,
    # реализующих ВТОРОЙ, независимый источник кандидатов: не лента
    # активности (недавние сделки, ограничена глубиной истории), а текущие
    # держатели коллекции (ограничены только её размером). См.
    # fetch_holders_page ниже и opensea.py — pipeline.py включает эту фазу
    # ПОСЛЕ того, как обычная лента активности исчерпана, если цель по
    # кошелькам ещё не достигнута.
    supports_holder_scan: bool = False

    def __init__(self) -> None:
        self._consecutive_failures = 0
        self._is_disabled = False

    @property
    def is_disabled(self) -> bool:
        return getattr(self, "_is_disabled", False)

    @is_disabled.setter
    def is_disabled(self, value: bool) -> None:
        self._is_disabled = value

    def record_failure(self, status_code: int) -> None:
        if status_code in (401, 403) or (500 <= status_code < 600):
            self._consecutive_failures = getattr(self, "_consecutive_failures", 0) + 1
            if self._consecutive_failures >= 5:
                self._is_disabled = True
        elif status_code >= 400:
            pass

    def record_success(self) -> None:
        self._consecutive_failures = 0

    @abstractmethod
    def fetch_activity(self, asset_type: str, target: str, limit: int = 100, target_wallets: int = 20) -> list[ActivityRecord]:
        """
        target — имя/адрес коллекции (NFT) либо тикер/mint-адрес токена (мемкоин).
        target_wallets — сколько УНИКАЛЬНЫХ кошельков минимум набрать, прежде
        чем прекратить листать историю вглубь (актуально для адаптеров с
        пагинацией, сейчас — только Magic Eden; раньше было захардкожено 20,
        теперь пробрасывается с Dashboard). Адаптеры без пагинации могут
        параметр игнорировать.
        Должен бросать AdapterError при сбое запроса (адаптер выше по стеку
        это ловит и переходит к следующему источнику — раздел 7.1 ТЗ).
        """
        raise NotImplementedError

    def fetch_activity_page(self, asset_type: str, target: str, offset: int, limit: int = 500,
                             network: str = "ethereum") -> tuple[list[ActivityRecord], bool]:
        """Опционально — только для адаптеров с supports_deep_search=True.
        Возвращает (записи_этой_страницы, есть_ли_смысл_запрашивать_дальше).
        network — ДОБАВЛЕНО 08.08.2026 вместе с постраничным поиском для
        OpenSea: мультичейн-площадкам нужно знать, в какой сети выбранного
        пользователем помечать записи (см. opensea.py); односетевые адаптеры
        (Magic Eden) параметр просто игнорируют.
        Может бросать rate_limit.guard.RateLimited при HTTP 429 — это
        ОТДЕЛЬНЫЙ случай от AdapterError, см. его docstring."""
        raise NotImplementedError(f"{self.name} не поддерживает постраничный поиск (supports_deep_search=False)")

    def fetch_holders_page(self, target: str, offset: int, limit: int = 100,
                            network: str = "ethereum") -> tuple[list[ActivityRecord], bool]:
        """Опционально — только для адаптеров с supports_holder_scan=True (см.
        выше). Та же сигнатура и семантика возврата, что у
        fetch_activity_page, но источник данных другой (владельцы, не
        сделки) — см. opensea.py::fetch_holders_page за подробностями и
        честной пометкой про непроверенную вживую форму ответа."""
        raise NotImplementedError(f"{self.name} не поддерживает сканирование держателей (supports_holder_scan=False)")

    def supports_asset_type(self, asset_type: str) -> bool:
        """По умолчанию площадка поддерживает то, что явно перечислено в SUPPORTED_ASSET_TYPES."""
        return asset_type in getattr(self, "SUPPORTED_ASSET_TYPES", {"nft"})


class AdapterError(Exception):
    """Ошибка конкретного адаптера — не должна ронять весь пайплайн (раздел 7 ТЗ)."""
