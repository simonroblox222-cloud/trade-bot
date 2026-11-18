"""Read-only dashboard: serves dashboard.html and /api/state from the SQLite DB."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .agents import AGENTS
from .config import LIVE_TRADING_ENABLED
from .store import Store

HTML = Path(__file__).with_name("dashboard.html")


def build_state(store: Store) -> dict:
    open_pos = store.open_positions()
    cash = store.get("cash", 0.0) or 0.0
    pos_value = sum(p["last_value_sol"] or 0 for p in open_pos)
    st = store.stats()
    start = store.get("starting_balance", cash) or cash
    day_start = store.get("day_start_equity")
    equity = cash + pos_value
    closed = int(st["n"])
    return {
        "now": time.time(),
        "mode": store.get("mode", "paper"),
        "live_enabled": LIVE_TRADING_ENABLED,
        "last_tick": store.get("last_tick"),
        "halted": bool(store.get("halted", False)),
        "account": {
            "starting": start, "cash": cash, "positions_value": pos_value, "equity": equity,
            "total_pnl": equity - start, "total_pnl_pct": (equity / start - 1) * 100 if start else 0,
            "unrealized": sum((p["last_value_sol"] or 0) - p["cost_sol"] for p in open_pos),
            "realized": st["realized"], "fees": st["fees"],
            "today_pnl": equity - day_start if day_start else 0.0,
            "wins": int(st["wins"]), "losses": int(st["losses"]), "closed": closed,
            "win_rate": st["wins"] / closed * 100 if closed else 0.0,
            "best": st["best"], "worst": st["worst"],
            "exposure": sum(p["cost_sol"] for p in open_pos),
        },
        "agents": [{"name": a, **(store.get(f"agent:{a}") or {"ts": None, "summary": "idle", "state": "idle"})}
                   for a in AGENTS],
        "open_positions": open_pos,
        "closed_positions": store.closed_positions(100),
        "trades": store.trades(150),
        "events": store.events(150),
        "rugchecks": store.rugchecks(40),
        "equity_curve": [{"ts": e["ts"], "equity": e["equity"]} for e in store.equity_curve(3000)],
    }


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8787, background: bool = False):
    store = Store(db_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, HTML.read_bytes(), "text/html; charset=utf-8")
            elif self.path.startswith("/api/state"):
                self._send(200, json.dumps(build_state(store), default=str).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"Dashboard: http://{host}:{port}", flush=True)
    if background:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return httpd
    httpd.serve_forever()
