"""Per-API rate limiting so the bot runs as fast as the free plans allow, but not faster.

Token bucket per API: refills at `per_minute / 60` tokens per second, with a small
burst capacity (1/10 of a minute) so we never fire a whole minute's budget at once
into an API that counts with a sliding window. HTTP 429 pauses the bucket.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimited(Exception):
    """Raised by a non-blocking acquire when no budget is left right now."""


class RateLimiter:
    def __init__(self, name: str, per_minute: float, clock=time.monotonic, sleep=time.sleep):
        self.name = name
        self.per_minute = float(per_minute)
        self.rate = self.per_minute / 60.0
        self.capacity = max(1.0, self.per_minute / 10.0)
        self.tokens = self.capacity
        self.clock = clock
        self.sleep = sleep
        self.updated = clock()
        self.paused_until = 0.0
        self.lock = threading.Lock()
        self.recent: deque[float] = deque()
        self.total = 0
        self.http_429 = 0
        self.errors = 0
        self.waited_s = 0.0

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def available(self) -> float:
        with self.lock:
            now = self.clock()
            if now < self.paused_until:
                return 0.0
            self._refill(now)
            return self.tokens

    def acquire(self, block: bool = True) -> None:
        while True:
            with self.lock:
                now = self.clock()
                self._refill(now)
                if now >= self.paused_until and self.tokens >= 1:
                    self.tokens -= 1
                    self.total += 1
                    self.recent.append(now)
                    return
                if not block:
                    raise RateLimited(self.name)
                wait = max(self.paused_until - now, (1 - self.tokens) / self.rate if self.rate else 1.0)
            wait = min(max(wait, 0.01), 60.0)
            self.waited_s += wait
            self.sleep(wait)

    def penalize(self, seconds: float) -> None:
        """Called on HTTP 429: stop using this API for a while."""
        with self.lock:
            self.http_429 += 1
            self.paused_until = max(self.paused_until, self.clock() + seconds)
            self.tokens = 0.0

    def snapshot(self) -> dict:
        with self.lock:
            now = self.clock()
            while self.recent and now - self.recent[0] > 60:
                self.recent.popleft()
            return {"limit_rpm": self.per_minute, "last_min": len(self.recent), "total": self.total,
                    "http_429": self.http_429, "errors": self.errors, "waited_s": round(self.waited_s, 1),
                    "paused_s": round(max(0.0, self.paused_until - now), 1)}


class Limits:
    """Registry of limiters by API name. Unknown names are unlimited."""

    def __init__(self, per_minute: dict[str, float] | None = None, **kw):
        self.limiters = {n: RateLimiter(n, rpm, **kw) for n, rpm in (per_minute or {}).items() if rpm and rpm > 0}

    def get(self, name: str) -> RateLimiter | None:
        return self.limiters.get(name)

    def available(self, name: str) -> float:
        lim = self.limiters.get(name)
        return float("inf") if lim is None else lim.available()

    def snapshot(self) -> dict:
        return {n: lim.snapshot() for n, lim in self.limiters.items()}
