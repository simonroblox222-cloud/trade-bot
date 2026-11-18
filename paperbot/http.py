"""Tiny stdlib JSON HTTP helper (no third-party deps) with optional rate limiting."""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

from .ratelimit import RateLimited, RateLimiter


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, blocked_s: float = 0.0):
        super().__init__(message)
        self.status = status
        self.blocked_s = blocked_s   # > 0: the API was paused for this long (403 backoff)


def _host(url: str) -> str:
    return urllib.parse.urlsplit(url).netloc or url[:40]


def _brief(body: str) -> str:
    """One short line from an error body: the HTML <title> if it's a page, else the start."""
    m = re.search(r"<title>(.*?)</title>", body, re.I | re.S)
    text = m.group(1) if m else body
    text = " ".join(text.split())
    return text[:100] + ("…" if len(text) > 100 else "")


def is_bot_challenge(status: int, body: str, headers) -> bool:
    if status not in (403, 503):
        return False
    if headers is not None and (headers.get("cf-mitigated") or "").lower() == "challenge":
        return True
    return "Just a moment" in body or "challenge-platform" in body or "cf-chl" in body


def _retry_after(e: urllib.error.HTTPError, default: float = 30.0) -> float:
    try:
        return max(1.0, float(e.headers.get("Retry-After")))
    except (TypeError, ValueError):
        return default


def _request(req: urllib.request.Request, url: str, timeout: float, limiter: RateLimiter | None, block: bool):
    if limiter is not None:
        try:
            limiter.acquire(block=block)
        except RateLimited as e:
            if not block:
                raise          # caller reuses cached data
            # paused for a long time (429/403 backoff): fail fast as an outage, don't stall the bot
            raise HttpError(f"{_host(url)} paused for {e.paused_s:.0f}s ({limiter.block_reason or 'rate limit'})",
                            None) from e
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if limiter is not None:
            limiter.succeeded()
        return data
    except urllib.error.HTTPError as e:
        body = e.read(4096).decode(errors="replace") if e.fp else ""
        host = _host(url)
        if e.code == 403 or is_bot_challenge(e.code, body, e.headers):
            # Cloudflare / bot protection: we never try to get around it, we just stay away
            why = "Cloudflare bot check" if is_bot_challenge(e.code, body, e.headers) else "forbidden"
            secs = limiter.forbid(f"HTTP {e.code} {why}") if limiter is not None else 0.0
            raise HttpError(f"HTTP {e.code} from {host}: {why}", e.code, blocked_s=secs) from e
        if limiter is not None:
            if e.code == 429:
                limiter.penalize(_retry_after(e))
            else:
                limiter.errors += 1
        raise HttpError(f"HTTP {e.code} from {host}: {_brief(body)}", e.code) from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as e:
        if limiter is not None:
            limiter.errors += 1
        raise HttpError(f"{type(e).__name__} from {_host(url)}: {str(e)[:100]}") from e


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
