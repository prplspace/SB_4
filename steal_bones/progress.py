"""
Прогресс текущего задания — раздел "анимация/индикатор поиска".

Упрощение (осознанное, см. README): инструмент однопользовательский и
локальный, поэтому состояние — один глобальный объект.

ПЕРЕРАБОТАНО под ультра-быстрый парсинг уникальных кошельков без проверки балансов.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Optional

_lock = threading.Lock()


@dataclass
class ProgressState:
    status: str = "idle"          # idle | running | paused | done | error
    stage: str = ""                # короткое описание текущего шага
    page: int = 0
    pages_hint: int = 0            # ориентировочный потолок страниц (для % прогресса)
    raw_records: int = 0
    unique_wallets: int = 0
    target_wallets: int = 20
    qualified_wallets_count: int = 0
    paused_until: Optional[float] = None  # unix-время окончания паузы, None когда не на паузе
    pause_reason: str = ""                 # описание причины паузы
    started_at: float = 0.0
    finished_at: float = 0.0
    error: Optional[str] = None
    result: Optional[dict] = None  # финальная сводка (для редиректа после завершения)


_state = ProgressState()


def start(pages_hint: int = 30) -> None:
    global _state
    with _lock:
        _state = ProgressState(status="running", stage="Запуск…", pages_hint=pages_hint, started_at=time.time())


def update(**kwargs) -> None:
    with _lock:
        for k, v in kwargs.items():
            if hasattr(_state, k):
                setattr(_state, k, v)


def pause(wait_seconds: float, reason: str) -> None:
    with _lock:
        _state.status = "paused"
        _state.paused_until = time.time() + wait_seconds
        _state.pause_reason = reason


def resume() -> None:
    with _lock:
        _state.status = "running"
        _state.paused_until = None
        _state.pause_reason = ""


def finish(result: dict) -> None:
    with _lock:
        _state.status = "done"
        _state.stage = "Готово"
        _state.finished_at = time.time()
        _state.result = result


def fail(error: str) -> None:
    with _lock:
        _state.status = "error"
        _state.stage = "Ошибка"
        _state.finished_at = time.time()
        _state.error = error


def snapshot() -> dict:
    with _lock:
        s = _state
        elapsed = (s.finished_at or time.time()) - s.started_at if s.started_at else 0

        # progress_pct calculation updated as requested
        if s.status in ("done", "error"):
            pct = 100.0
        elif s.target_wallets > 0:
            pct = min(100.0, (s.qualified_wallets_count / s.target_wallets) * 100.0)
        else:
            pct = 0.0

        wait_remaining = max(0.0, s.paused_until - time.time()) if s.paused_until else 0
        return {
            "status": s.status,
            "stage": s.stage,
            "page": s.page,
            "pages_hint": s.pages_hint,
            "raw_records": s.raw_records,
            "unique_wallets": s.unique_wallets,
            "target_wallets": s.target_wallets,
            "qualified_wallets_count": s.qualified_wallets_count,
            "pause_reason": s.pause_reason,
            "wait_remaining_sec": round(wait_remaining, 1),
            "elapsed_sec": round(elapsed, 1),
            "percent": int(pct),
            "error": s.error,
            "result": s.result,
        }
