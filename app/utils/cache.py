"""Tiny in-memory TTL cache (per process) used to avoid paying twice for the same analysis."""
from __future__ import annotations

import time
from collections import OrderedDict
from typing import Any


class TTLCache:
    def __init__(self, max_items: int = 128) -> None:
        self._data: OrderedDict[str, tuple[float, Any]] = OrderedDict()
        self._max_items = max_items

    def get(self, key: str) -> Any | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            self._data.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: float) -> None:
        if ttl_seconds <= 0:
            return
        self._data[key] = (time.monotonic() + ttl_seconds, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max_items:
            self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()
