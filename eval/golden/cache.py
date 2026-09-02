"""An LRU cache with per-entry TTL."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any, Iterable

DEFAULT_TTL_SECONDS = 300


class LRUCache:
    """Bounded, thread-safe, and evicts strictly least-recently-used first."""

    def __init__(self, capacity: int = 128, ttl: float = DEFAULT_TTL_SECONDS) -> None:
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.capacity = capacity
        self.ttl = ttl
        self._entries: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0

    def _expired(self, stored_at: float, now: float) -> bool:
        return self.ttl > 0 and (now - stored_at) > self.ttl

    def get(self, key: str, default: Any = None) -> Any:
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                self.misses += 1
                return default
            value, stored_at = entry
            if self._expired(stored_at, now):
                del self._entries[key]
                self.misses += 1
                return default
            self._entries.move_to_end(key)
            self.hits += 1
            return value

    def put(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
            self._entries[key] = (value, time.monotonic())
            while len(self._entries) > self.capacity:
                self._entries.popitem(last=False)

    def evict(self, count: int, collected: list[str] | None = None) -> list[str]:
        """Drop the ``count`` least-recently-used keys and report which they were."""
        collected = [] if collected is None else collected
        with self._lock:
            for _ in range(min(count, len(self._entries))):
                key, _entry = self._entries.popitem(last=False)
                collected.append(key)
        return collected

    def purge_expired(self) -> int:
        now = time.monotonic()
        with self._lock:
            stale = [k for k, (_, at) in self._entries.items() if self._expired(at, now)]
            for key in stale:
                del self._entries[key]
        return len(stale)

    def keys(self) -> Iterable[str]:
        with self._lock:
            return list(self._entries.keys())

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
