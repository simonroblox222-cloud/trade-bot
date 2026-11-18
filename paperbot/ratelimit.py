"""Per-API rate limiting so the bot runs as fast as the free plans allow, but not faster.

Token bucket per API: refills at `per_minute / 60` tokens per second, with a small
burst capacity (1/10 of a minute) so we never fire a whole minute's budget at once
into an API that counts with a sliding window. HTTP 429 pauses the bucket.
HTTP 403 (e.g. a Cloudflare bot challenge) means "you're not welcome": the API is
paused with a long, growing backoff instead of being retried every tick.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimited(Exception):
    """Raised when no budget is left right now (non-blocking acquire), or the API is
    paused for longer than a blocking caller should wait (429 / 403 backoff)."""

    def __init__(self, name: str, paused_s: float = 0.0):
        super().__init__(f"{name} paused {paused_s:.0f}s" if paused_s else name)
        self.paused_s = paused_s


class RateLimiter:
    MAX_BLOCK_S = 30.0                       # never stall the bot longer than this on one call
    BLOCK_BACKOFF_S = (900, 1800, 3600, 7200, 21600)   # 15 min .. 6 h after repeated 403s

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
        self.http_403 = 0
        self.block_streak = 0
        self.block_reason = ""
        self.streak_429 = 0

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
                paused = self.paused_until - now
                if not block or paused > self.MAX_BLOCK_S:
                    raise RateLimited(self.name, max(0.0, paused))
                wait = max(self.paused_until - now, (1 - self.tokens) / self.rate if self.rate else 1.0)
            wait = min(max(wait, 0.01), 60.0)
            self.waited_s += wait
            self.sleep(wait)

    def penalize(self, seconds: float) -> float:
        """Called on HTTP 429: stop using this API for a while. Repeated 429s back off longer
        (Retry-After or 30 s, then 1 min, 2 min ... 10 min); a success resets it. Returns the pause in seconds."""
        with self.lock:
            self.http_429 += 1
            secs = seconds if self.streak_429 == 0 else max(seconds, min(600.0, 30.0 * 2 ** self.streak_429))
            self.streak_429 += 1
            self.paused_until = max(self.paused_until, self.clock() + secs)
            self.tokens = 0.0
            return secs

    def forbid(self, reason: str) -> float:
        """Called on HTTP 403: back off for a long, growing time. Returns the pause in seconds."""
        with self.lock:
            self.http_403 += 1
            secs = self.BLOCK_BACKOFF_S[min(self.block_streak, len(self.BLOCK_BACKOFF_S) - 1)]
            self.block_streak += 1
            self.block_reason = reason
            self.paused_until = max(self.paused_until, self.clock() + secs)
            self.tokens = 0.0
            return secs

    def succeeded(self) -> None:
        self.streak_429 = 0
        if self.block_streak:
            with self.lock:
                self.block_streak = 0
                self.block_reason = ""

    def snapshot(self) -> dict:
        with self.lock:
            now = self.clock()
            while self.recent and now - self.recent[0] > 60:
                self.recent.popleft()
            return {"limit_rpm": self.per_minute, "last_min": len(self.recent), "total": self.total,
                    "http_429": self.http_429, "http_403": self.http_403,
                    "blocked": self.block_reason if now < self.paused_until else "", "errors": self.errors, "waited_s": round(self.waited_s, 1),
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

    def forbid(self, reason: str) -> float:
        """Called on HTTP 403: back off for a long, growing time. Returns the pause in seconds."""
        with self.lock:
            self.http_403 += 1
            secs = self.BLOCK_BACKOFF_S[min(self.block_streak, len(self.BLOCK_BACKOFF_S) - 1)]
            self.block_streak += 1
            self.block_reason = reason
            self.paused_until = max(self.paused_until, self.clock() + secs)
            self.tokens = 0.0
            return secs

    def succeeded(self) -> None:
        self.streak_429 = 0
        if self.block_streak:
            with self.lock:
                self.block_streak = 0
                self.block_reason = ""

    def snapshot(self) -> dict:
        return {n: lim.snapshot() for n, lim in self.limiters.items()}
