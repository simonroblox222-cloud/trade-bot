"""Tiny stdlib JSON HTTP helper (no third-party deps)."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class HttpError(Exception):
    pass


def get_json(url: str, timeout: float = 10.0, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "paperbot/0.1", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise HttpError(f"HTTP {e.code} for {url}: {body}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise HttpError(f"{type(e).__name__} for {url}: {e}") from e


def post_json(url: str, payload: dict, timeout: float = 10.0):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "paperbot/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise HttpError(f"HTTP {e.code} for {url}: {e.read().decode(errors='replace')[:300]}") from e
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise HttpError(f"{type(e).__name__} for {url}: {e}") from e
