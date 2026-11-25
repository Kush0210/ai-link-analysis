"""Simple thread-safe in-memory TTL cache for reducing repeat LLM API calls."""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Generic, Optional, TypeVar

T = TypeVar("T")


class TTLCache(Generic[T]):
    """Bounded LRU cache with per-entry time-to-live (TTL) expiration."""

    def __init__(self, max_size: int = 256, ttl_seconds: int = 3600) -> None:
        self._max_size = max(1, max_size)
        self._ttl_seconds = max(0, ttl_seconds)
        self._store: OrderedDict[str, tuple[float, T]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[T]:
        """Return cached value if present and unexpired, otherwise None."""
        if self._ttl_seconds <= 0:
            return None

        now = time.monotonic()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, value = entry
            if now >= expires_at:
                del self._store[key]
                return None
            self._store.move_to_end(key)
            return value

    def set(self, key: str, value: T) -> None:
        """Insert or update an entry with TTL expiration."""
        if self._ttl_seconds <= 0:
            return

        expires_at = time.monotonic() + self._ttl_seconds
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (expires_at, value)
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

    def clear(self) -> None:
        """Remove all cached entries."""
        with self._lock:
            self._store.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)
