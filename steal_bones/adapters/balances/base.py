"""Базовый интерфейс адаптеров баланса — раздел 4 ТЗ."""

from __future__ import annotations

from abc import ABC, abstractmethod


class BalanceAdapter(ABC):
    network: str
    requires_key: bool = False

    @abstractmethod
    def get_balance(self, address: str) -> float:
        """Баланс в нативной единице сети (SOL, ETH, TRX и т.д.).
        Бросает BalanceCheckError при сбое — вызывающий код это ловит."""
        raise NotImplementedError


class BalanceCheckError(Exception):
    pass
