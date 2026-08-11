"""
Rate Limit Guard — раздел 7.1 ТЗ.

Отличие от псевдокода в ТЗ: счётчик хранится в таблице quota_usage (см.
db/models.py), а не только в памяти процесса — иначе перезапуск софта в
середине дня обнулял бы память и защита от исчерпания лимита переставала
бы работать. Логика (дневной лимит, порог предупреждения, ротация ключей,
изоляция источников) — та же самая, что описана в ТЗ.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Optional

from db.models import get_connection

logger = logging.getLogger("steal_bones.rate_limit")


class QuotaExhausted(Exception):
    """Дневная квота источника исчерпана — адаптер должен быть пропущен, не всё приложение."""


class RateLimited(Exception):
    """
    ДОБАВЛЕНО (08.08.2026, запрос пользователя, п.4): площадка/RPC ответили
    именно HTTP 429 ("слишком часто") — это ПРИНЦИПИАЛЬНО не то же самое,
    что MAX_OFFSET в magic_eden.py (структурный потолок, не про частоту, см.
    его docstring) или обрыв соединения (SSLError и т.п., см. evm.py). 429 —
    единственный из трёх случаев, где имеет смысл ждать и продолжать с того
    же места (см. pipeline.py::_wait_out_rate_limit) — для двух других ждать
    физически бессмысленно.

    retry_after — секунды до сброса, ЕСЛИ площадка прислала заголовок
    Retry-After (у OpenSea это гарантировано их же документацией; у Magic
    Eden — не подтверждено). None означает "площадка не сказала, сколько
    ждать" — тогда pipeline.py использует собственную оценку с запасом
    (DEFAULT_RATE_LIMIT_WAIT_SEC), а не выдумывает точное число.
    """

    def __init__(self, message: str, retry_after: float | None = None, source: str = ""):
        super().__init__(message)
        self.retry_after = retry_after
        self.source = source


def parse_retry_after(value: str | None) -> float | None:
    """Заголовок Retry-After по стандарту (RFC 9110) — либо целое число
    секунд, либо HTTP-дата. Секунды разбираем; дату — нет (редкий случай на
    практике для JSON API, не хотим тянуть email.utils.parsedate ради этого
    одного места) — тогда просто возвращаем None, и вызывающий код падает
    обратно на оценку с запасом, а не падает с исключением."""
    if not value:
        return None
    try:
        seconds = float(value.strip())
        return seconds if seconds >= 0 else None
    except ValueError:
        return None  # похоже на HTTP-дату, а не на число секунд — не разбираем, см. docstring


@dataclass
class QuotaTracker:
    source: str            # напр. "etherscan", "magic_eden"
    key_label: str          # напр. сам ключ, либо "default" если ключ не нужен
    daily_limit: int
    db_path: Path
    warn_threshold: float = 0.9

    def _today(self) -> str:
        return date.today().isoformat()

    def used_today(self) -> int:
        conn = get_connection(self.db_path)
        try:
            row = conn.execute(
                "SELECT used FROM quota_usage WHERE source = ? AND key_label = ? AND day = ?",
                (self.source, self.key_label, self._today()),
            ).fetchone()
            return row["used"] if row else 0
        finally:
            conn.close()

    def can_request(self) -> bool:
        return self.used_today() < self.daily_limit

    def is_near_limit(self) -> bool:
        if self.daily_limit <= 0:
            return False
        return self.used_today() / self.daily_limit >= self.warn_threshold

    def usage_ratio(self) -> float:
        if self.daily_limit <= 0:
            return 0.0
        return self.used_today() / self.daily_limit

    def record_request(self, n: int = 1) -> None:
        conn = get_connection(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO quota_usage (source, key_label, day, used)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source, key_label, day) DO UPDATE SET used = used + excluded.used
                """,
                (self.source, self.key_label, self._today(), n),
            )
            conn.commit()
        finally:
            conn.close()


class KeyRotator:
    """Пул из нескольких бесплатных ключей на один источник — round-robin
    с автопереключением при исчерпании лимита текущего ключа (раздел 7.1)."""

    def __init__(self, source: str, keys: list[str], daily_limit: int, db_path: Path):
        self.source = source
        self.keys = keys or [""]  # "" — источник вообще без ключа (напр. публичный RPC)
        self.daily_limit = daily_limit
        self.db_path = db_path
        self._idx = 0
        self._trackers = {
            k: QuotaTracker(source=source, key_label=(k or "no-key"), daily_limit=daily_limit, db_path=db_path)
            for k in self.keys
        }

    def get_available_key(self) -> Optional[str]:
        for _ in range(len(self.keys)):
            key = self.keys[self._idx]
            self._idx = (self._idx + 1) % len(self.keys)
            if self._trackers[key].can_request():
                return key
        return None  # все ключи в пуле исчерпаны на сегодня

    def record_request(self, key: str, n: int = 1) -> None:
        self._trackers[key].record_request(n)

    def mark_rate_limited(self, key: str) -> None:
        """ДОБАВЛЕНО (08.08.2026): реальный HTTP 429 от площадки на конкретном
        ключе — помечаем его как исчерпанный НА СЕГОДНЯ (даже если наш
        локальный счётчик так не считает), чтобы get_available_key() сам
        переключился на следующий ключ в пуле при следующем вызове, вместо
        того чтобы снова наткнуться на тот же 429. Это единственное место,
        где локальный QuotaTracker намеренно обновляется по факту ответа
        сервера, а не по числу запросов, которые сделали МЫ."""
        tracker = self._trackers.get(key)
        if tracker is not None:
            remaining = max(0, tracker.daily_limit - tracker.used_today())
            if remaining:
                tracker.record_request(remaining)

    def has_other_key(self, exhausted_key: str) -> bool:
        """Есть ли в пуле ХОТЯ БЫ ОДИН ключ, отличный от только что
        исчерпанного, с доступной квотой — pipeline.py использует это, чтобы
        решить "тихо переключиться" vs "показать пользователю таймер
        ожидания" (см. п.4 запроса пользователя)."""
        return any(k != exhausted_key and self._trackers[k].can_request() for k in self.keys)

    def status(self) -> list[dict]:
        """Для уведомления в интерфейсе (Settings/Dashboard, раздел 7.1)."""
        return [
            {
                "source": self.source,
                "key_label": (k[:6] + "…") if k else "не требуется",
                "used": t.used_today(),
                "limit": t.daily_limit,
                "ratio": round(t.usage_ratio(), 3),
                "near_limit": t.is_near_limit(),
            }
            for k, t in self._trackers.items()
        ]


def with_backoff(fn: Callable, *, retries: int = 5, base_delay: float = 2.0, retry_on: tuple = (Exception,)):
    """Простая замена tenacity: экспоненциальный backoff на 429/5xx и сетевые ошибки.
    Вызывается как with_backoff(lambda: requests.get(...)).
    На HTTP 429 или RateLimited, ждет с экспоненциальным backoff (2s, 4s, 8s, up to 32s) и случайным джиттером.
    """
    import random
    last_exc: Optional[BaseException] = None
    for attempt in range(retries):
        try:
            return fn()
        except RateLimited as exc:
            last_exc = exc
            # Backoff delays: 2s, 4s, 8s, up to 32s with random jitter
            delay = min(32.0, base_delay * (2 ** attempt))
            jitter = random.uniform(0.0, 1.0)
            final_delay = delay + jitter
            logger.warning("Получен лимит запросов (HTTP 429) на попытке %s/%s. Пауза %.2fс с джиттером.", attempt + 1, retries, final_delay)
            time.sleep(final_delay)
        except retry_on as exc:  # noqa: PERF203 — намеренно широкий перехват на границе с внешним API
            # Если это HTTPError с кодом 429, обрабатываем аналогично
            is_429 = False
            import requests
            if isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code == 429:
                is_429 = True

            last_exc = exc
            delay = min(32.0, base_delay * (2 ** attempt))
            jitter = random.uniform(0.0, 1.0)
            final_delay = delay + jitter
            if is_429:
                logger.warning("Получен HTTP 429 на попытке %s/%s. Пауза %.2fс с джиттером.", attempt + 1, retries, final_delay)
            else:
                logger.warning("Попытка %s/%s не удалась (%s), пауза %.2fс с джиттером.", attempt + 1, retries, exc, final_delay)
            time.sleep(final_delay)
    raise last_exc  # type: ignore[misc]


class TokenBucketLimiter:
    def __init__(self, rate: float, capacity: float):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_update = time.time()
        self.lock = threading.Lock()

    def wait_and_consume(self, amount: float = 1.0) -> None:
        while True:
            with self.lock:
                now = time.time()
                elapsed = now - self.last_update
                self.last_update = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate)
                if self.tokens >= amount:
                    self.tokens -= amount
                    return
                needed = amount - self.tokens
                sleep_time = needed / self.rate
            time.sleep(sleep_time)


__all__ = [
    "RateLimited",
    "QuotaTracker",
    "KeyRotator",
    "parse_retry_after",
    "with_backoff",
    "magic_eden_limiter",
    "solana_rpc_limiter",
]

# Configure Token Bucket / Sliding Window parameters strictly for Free Tier API constraints
opensea_limiter = TokenBucketLimiter(rate=3.0, capacity=3.0)
magic_eden_limiter = TokenBucketLimiter(rate=5.0, capacity=5.0)
helius_limiter = TokenBucketLimiter(rate=10.0, capacity=10.0)
solana_rpc_limiter = TokenBucketLimiter(rate=10.0, capacity=10.0)
