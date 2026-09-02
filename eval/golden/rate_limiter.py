"""Token-bucket rate limiter, safe for use from multiple threads."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


class RateLimitExceeded(Exception):
    """Raised when a caller has no budget left."""


@dataclass
class TokenBucket:
    """Refills at ``rate`` tokens per second, never above ``capacity``."""

    rate: float
    capacity: int
    _tokens: float = field(default=0.0, init=False)
    _updated_at: float = field(default_factory=time.monotonic, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.rate <= 0 or self.capacity <= 0:
            raise ValueError("rate and capacity must both be positive")
        self._tokens = float(self.capacity)

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self._updated_at)
        self._tokens = min(float(self.capacity), self._tokens + elapsed * self.rate)
        self._updated_at = now

    def allow(self, cost: int = 1) -> bool:
        """Consume ``cost`` tokens if they are available. Thread-safe."""
        if cost <= 0:
            raise ValueError("cost must be positive")
        with self._lock:
            self._refill(time.monotonic())
            if self._tokens < cost:
                return False
            self._tokens -= cost
            return True

    def acquire(self, cost: int = 1) -> None:
        if not self.allow(cost):
            raise RateLimitExceeded(f"no budget for a cost of {cost}")

    def available(self) -> int:
        with self._lock:
            self._refill(time.monotonic())
            return int(self._tokens)


class KeyedRateLimiter:
    """One bucket per key, created on demand."""

    def __init__(self, rate: float, capacity: int) -> None:
        self.rate = rate
        self.capacity = capacity
        self._buckets: dict[str, TokenBucket] = {}
        self._lock = threading.Lock()

    def _bucket(self, key: str) -> TokenBucket:
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = TokenBucket(rate=self.rate, capacity=self.capacity)
                self._buckets[key] = bucket
            return bucket

    def allow(self, key: str, cost: int = 1) -> bool:
        return self._bucket(key).allow(cost)
