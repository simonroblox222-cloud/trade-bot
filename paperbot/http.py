"""Tiny stdlib JSON HTTP helper (no third-party deps) with optional rate limiting."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from .ratelimit import RateLimiter


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def _retry_after(e: urllib.error.HTTPError, default: float = 30.0) -> float:
    try:
        return max(1.0, float(e.headers.get("Retry-After")))
    except (TypeError, ValueError):
        return default


def _request(req: urllib.request.Request, url: str, timeout: float, limiter: RateLimiter | None, block: bool):
    if limiter is not None:
        limiter.acquire(block=block)  # may raise RateLimited when block=False
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if limiter is not None:
            if e.code == 429:
                limiter.penalize(_retry_after(e))
            else:
                limiter.errors += 1
        body = e.read().decode(errors="replace")[:300] if e.fp else ""
        raise HttpError(f"HTTP {e.code} for {url}: {body}", e.code) from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        if limiter is not None:
            limiter.errors += 1
        raise HttpError(f"{type(e).__name__} for {url}: {e}") from e


def get_json(url: str, timeout: float = 10.0, headers: dict | None = None,
             limiter: RateLimiter | None = None, block: bool = True):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "paperbot/0.1", **(headers or {})})
    return _request(req, url, timeout, limiter, block)


def post_json(url: str, payload: dict, timeout: float = 10.0,
              limiter: RateLimiter | None = None, block: bool = True):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "paperbot/0.1"})
    return _request(req, url, timeout, limiter, block)
