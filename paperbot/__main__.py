"""CLI.

  python -m paperbot run [--config config.toml] [--mock] [--ticks N] [--no-dashboard]
  python -m paperbot dashboard [--config config.toml]
  python -m paperbot reset [--config config.toml]
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from .agents import Pipeline
from .config import load_config
from .dashboard import serve
from dataclasses import asdict

from .execution import make_execution
from .ratelimit import Limits
from .rugcheck import RugChecker
from .store import Store


def build(cfg, mock: bool, seed: int):
    limits = Limits() if mock else Limits(asdict(cfg.rate_limits))
    if mock:
        from .mock import MockWorld
        world = MockWorld(seed=seed)
        discovery = quotes = onchain = rug_api = world
    else:
        from .sources import JupiterQuotes, MarketDiscovery, RugCheckApi, SolanaRpc
        discovery, quotes = MarketDiscovery(cfg.api, limits), JupiterQuotes(cfg.api, limits)
        onchain, rug_api = SolanaRpc(cfg.api, limits), RugCheckApi(cfg.api, limits)
    # mock market only moves between cycles, so waiting out the latency would change nothing
    exe = make_execution(cfg.mode, quotes, cfg.risk, sleep=(lambda _s: None) if mock else time.sleep)
    checker = RugChecker(cfg.rug, onchain, rug_api if cfg.rug.use_rugcheck_api else None, quotes,
                         cfg.risk.slippage_bps)
    on_tick = world.step if mock else (lambda: None)
    return discovery, checker, exe, on_tick, limits


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="paperbot", description="Solana memecoin PAPER trading bot")
    ap.add_argument("command", choices=["run", "dashboard", "reset"])
    ap.add_argument("--config", default="config.toml" if os.path.exists("config.toml") else None)
    ap.add_argument("--mock", action="store_true", help="offline synthetic market (no network)")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--ticks", type=int, default=0, help="stop after N ticks (0 = forever)")
    ap.add_argument("--tick-seconds", type=float, default=None)
    ap.add_argument("--db", default=None)
    ap.add_argument("--no-dashboard", action="store_true")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args(argv)

    cfg = load_config(a.config)
    if a.db:
        cfg.db_path = a.db
    if a.tick_seconds is not None:
        cfg.tick_seconds = a.tick_seconds

    if a.command == "reset":
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(cfg.db_path + suffix):
                os.remove(cfg.db_path + suffix)
        print(f"Paper account reset ({cfg.db_path} removed).")
        return 0
    if a.command == "dashboard":
        serve(cfg.db_path, cfg.dashboard_host, cfg.dashboard_port)
        return 0

    store = Store(cfg.db_path)
    discovery, checker, exe, on_tick, limits = build(cfg, a.mock, a.seed)
    pipe = Pipeline(cfg, discovery, checker, exe, store, echo=not a.quiet, limits=limits)
    if not a.no_dashboard:
        try:
            serve(cfg.db_path, cfg.dashboard_host, cfg.dashboard_port, background=True)
        except OSError as e:
            print(f"Dashboard not started ({e}); bot keeps running. Use another dashboard_port.", flush=True)
    print(f"PAPER mode · {'MOCK market' if a.mock else 'live market data'} · db {cfg.db_path}", flush=True)
    n = 0
    try:
        while True:
            started = time.time()
            try:
                on_tick()
                pipe.tick()
            except Exception as e:  # keep the loop alive, but record the error
                pipe.log.log("Logger", "error", f"tick failed: {type(e).__name__}: {e}")
            n += 1
            if a.ticks and n >= a.ticks:
                break
            time.sleep(max(0.0, cfg.tick_seconds - (time.time() - started)))
    except KeyboardInterrupt:
        pass
    acc = pipe.equity.account()
    st = store.stats()
    print(f"Done after {n} ticks: equity {acc.equity:.4f} SOL, closed {int(st['n'])} "
          f"(W {int(st['wins'])} / L {int(st['losses'])}), open {acc.open_count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
