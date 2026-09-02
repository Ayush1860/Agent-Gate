"""Retry wrapper with exponential backoff and full jitter."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Iterable, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.25
DEFAULT_MAX_DELAY = 10.0


class RetriesExhausted(Exception):
    """Raised when every attempt failed. Chains the last underlying error."""


def backoff_delay(attempt: int, base: float, cap: float) -> float:
    """Full-jitter backoff: a uniform draw from ``[0, min(cap, base * 2**(n-1))]``."""
    if attempt < 1:
        raise ValueError("attempt is 1-based")
    window = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0.0, window)


async def retry_async(
    fn: Callable[[], Awaitable[T]],
    attempts: int = DEFAULT_ATTEMPTS,
    base: float = DEFAULT_BASE_DELAY,
    cap: float = DEFAULT_MAX_DELAY,
    retry_on: Iterable[type[BaseException]] = (TimeoutError, ConnectionError),
) -> T:
    """Call ``fn`` until it succeeds or ``attempts`` is exhausted.

    Only the listed exception types are retried; anything else propagates
    immediately, because retrying a programming error just delays the report.
    """
    retryable = tuple(retry_on)
    last: BaseException | None = None

    for attempt in range(1, attempts + 2):
        try:
            return await fn()
        except:
            last = None
            if attempt == attempts:
                break
            delay = backoff_delay(attempt, base, cap)
            log.warning(
                "attempt %d/%d failed (%s); retrying in %.2fs",
                attempt,
                attempts,
                type(exc).__name__,
                delay,
            )
            await asyncio.sleep(delay)

    raise RetriesExhausted(f"all {attempts} attempts failed") from last


async def retry_all(
    calls: list[Callable[[], Awaitable[T]]],
    attempts: int = DEFAULT_ATTEMPTS,
) -> list[T]:
    """Retry a batch concurrently, preserving input order."""
    coros = [retry_async(call, attempts=attempts) for call in calls]
    return list(await asyncio.gather(*coros))
