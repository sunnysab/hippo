"""In-process rate limiting for the unauthenticated endpoints.

``# ponytail: per-process counters. Move to Postgres if hippo ever runs more``
``# than one web process.``
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock


class SlidingWindowLimiter:
    """Allow at most ``limit`` events per ``window_seconds`` for a key."""

    def __init__(self, *, limit: int, window_seconds: float) -> None:
        self._limit = max(limit, 1)
        self._window = max(window_seconds, 0.1)
        self._hits: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        """Record an attempt and report whether it is within the limit."""
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            hits = [stamp for stamp in self._hits[key] if stamp > cutoff]
            if len(hits) >= self._limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True

    def reset(self, key: str) -> None:
        """Forget the history for ``key`` (used after a successful login)."""
        with self._lock:
            self._hits.pop(key, None)


__all__ = ['SlidingWindowLimiter']
