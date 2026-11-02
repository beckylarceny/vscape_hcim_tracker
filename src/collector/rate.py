"""Global request pacing shared by collector worker threads."""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """Global spacing between HTTP requests, shared across worker threads."""

    def __init__(self, per_second: float) -> None:
        self._gap = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = 0.0

    def wait(self) -> None:
        if not self._gap:
            return
        with self._lock:
            now = time.monotonic()
            delay = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + self._gap
        if delay:
            time.sleep(delay)
