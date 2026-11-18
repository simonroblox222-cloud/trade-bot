"""New pump.fun tokens from PumpPortal's free data websocket (no key, no account).

pump.fun's own frontend API sits behind a Cloudflare bot check, which we don't try to
get around. PumpPortal publishes the same launches over a public websocket:

    wss://pumpportal.fun/api/data   {"method": "subscribeNewToken"} / {"method": "subscribeMigration"}

Each "create" message carries the mint, symbol and the creator wallet (`traderPublicKey`).
We only keep those fields: counts and addresses, never any text is interpreted.

Stdlib only: a minimal RFC 6455 client (one connection, as PumpPortal asks), running in
a background thread with reconnect backoff. Read-only: it never sends anything except
the two subscribe messages and pong replies.
"""
from __future__ import annotations

import base64
import json
import os
import socket
import ssl
import struct
import threading
import time
import urllib.parse
from collections import deque


def encode_frame(payload: bytes, opcode: int = 0x1) -> bytes:
    """Client frames must be masked (RFC 6455 5.3)."""
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 65536:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    mask = os.urandom(4)
    return head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload))


def read_frame(recv) -> tuple[bool, int, bytes]:
    """Returns (fin, opcode, payload). `recv(n)` must return exactly n bytes."""
    b0, b1 = recv(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", recv(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", recv(8))[0]
    mask = recv(4) if b1 & 0x80 else None
    data = recv(n) if n else b""
    if mask:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return bool(b0 & 0x80), b0 & 0x0F, data


def parse_event(msg: dict) -> dict | None:
    """Keep only what the bot uses from a PumpPortal message."""
    mint = msg.get("mint")
    if not isinstance(mint, str) or not mint:
        return None
    kind = msg.get("txType")
    if kind not in ("create", "migrate"):
        return None
    return {"mint": mint, "kind": kind,
            "creator": msg.get("traderPublicKey") if kind == "create" else None,
            "symbol": msg.get("symbol") if isinstance(msg.get("symbol"), str) else None}


class PumpPortalFeed:
    def __init__(self, url: str = "wss://pumpportal.fun/api/data", keep: int = 500, timeout_s: float = 30.0):
        self.url = url
        self.events: deque[dict] = deque(maxlen=keep)       # graduations (migrate events)
        self.creators: dict[str, str] = {}
        self.timeout_s = timeout_s
        self.connected = False
        self.last_error = ""
        self.received = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ thread
    def start(self) -> "PumpPortalFeed":
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="pumpportal", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        backoff = 5.0
        while not self._stop.is_set():
            try:
                self._session()
                backoff = 5.0
            except Exception as e:  # network error, refused, closed: retry later
                self.last_error = f"{type(e).__name__}: {str(e)[:100]}"
                forbidden = "HTTP 403" in str(e) or "HTTP 401" in str(e)
                backoff = 900.0 if forbidden else min(backoff * 2, 300.0)
            self.connected = False
            self._stop.wait(backoff)

    def _session(self) -> None:
        u = urllib.parse.urlsplit(self.url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((u.hostname, port), timeout=self.timeout_s)
        sock = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname) if u.scheme == "wss" else raw
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            path = (u.path or "/") + (f"?{u.query}" if u.query else "")
            sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\n"
                          f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                          f"User-Agent: paperbot/0.1\r\n\r\n").encode())
            buf = b""
            while b"\r\n\r\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("closed during handshake")
                buf += chunk
            head, rest = buf.split(b"\r\n\r\n", 1)
            status = head.split(b"\r\n", 1)[0].decode(errors="replace")
            if " 101 " not in status + " ":
                code = status.split(" ")[1] if " " in status else "?"
                raise ConnectionError(f"HTTP {code} on websocket handshake")
            pending = bytearray(rest)

            def recv(n: int) -> bytes:
                while len(pending) < n:
                    chunk = sock.recv(65536)
                    if not chunk:
                        raise ConnectionError("connection closed")
                    pending.extend(chunk)
                out = bytes(pending[:n])
                del pending[:n]
                return out

            for method in ("subscribeNewToken", "subscribeMigration"):
                sock.sendall(encode_frame(json.dumps({"method": method}).encode()))
            self.connected, self.last_error = True, ""
            sock.settimeout(120)   # new tokens arrive every few seconds; silence this long = dead link
            message = b""
            while not self._stop.is_set():
                fin, op, data = read_frame(recv)
                if op == 0x9:
                    sock.sendall(encode_frame(data, 0xA))
                elif op == 0x8:
                    raise ConnectionError("server closed the connection")
                elif op in (0x1, 0x0):
                    message += data
                    if fin:
                        self._handle(message)
                        message = b""
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _handle(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode(errors="replace"))
        except ValueError:
            return
        ev = parse_event(msg) if isinstance(msg, dict) else None
        if ev is None:
            return
        ev["ts"] = time.time()
        with self._lock:
            self.received += 1
            if ev["kind"] == "migrate":
                self.events.append(ev)
            if ev["creator"]:
                self.creators[ev["mint"]] = ev["creator"]
                if len(self.creators) > 5000:
                    self.creators = dict(list(self.creators.items())[-2500:])

    # ------------------------------------------------------------------ reads
    def mints(self) -> list[str]:
        """Tokens that just graduated from the bonding curve. Raw launches (several per second,
        nearly all dead within minutes) only feed the creator map for the dev-history check."""
        with self._lock:
            return [e["mint"] for e in reversed(self.events) if e["kind"] == "migrate"]

    def creator(self, mint: str) -> str | None:
        with self._lock:
            return self.creators.get(mint)

    def status(self) -> str:
        if self.connected:
            return f"connected, {self.received} events, {len(self.events)} graduations"
        return f"not connected ({self.last_error or 'connecting'})"
