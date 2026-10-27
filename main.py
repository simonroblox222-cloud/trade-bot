#!/usr/bin/env python3
"""Command line entry point.

  python main.py backtest --strategy rsi2 --start 2015-01-01 --split 2021-01-01
  python main.py account
  python main.py run-once --strategy rsi2            # daily strategy, run before the open
  python main.py live --strategy orb --timeframe 5m  # intraday loop
"""
import argparse
import logging
from pathlib import Path

import pandas as pd

from tradebot.backtest import buy_and_hold, compute_metrics, format_metrics, run_backtest
from tradebot.config import Settings
from tradebot.data import fetch_bars, load_csv_dir
from tradebot.strategies import STRATEGIES, get_strategy


def cmd_backtest(a, settings):
    bars = load_csv_dir(a.csv_dir, settings.symbols) if a.csv_dir else \
        fetch_bars(settings, settings.symbols, a.start, a.end, a.timeframe)
    strategy = get_strategy(a.strategy)
    periods = [("full", None, None)]
    if a.split:
        periods = [("in-sample", None, a.split), ("out-of-sample", a.split, None)]
    Path("reports").mkdir(exist_ok=True)
    for label, lo, hi in periods:
        part = {s: df.loc[lo:hi] if lo or hi else df for s, df in bars.items()}
        part = {s: df for s, df in part.items() if len(df)}
        res = run_backtest(part, strategy, a.capital, settings.risk, settings.costs)
        bh = buy_and_hold(part, a.capital)
        print(f"\n[{label}] {min(df.index[0] for df in part.values()).date()} .. "
              f"{max(df.index[-1] for df in part.values()).date()}  symbols: {', '.join(part)}")
        print(format_metrics(f"{strategy.name}", res.metrics), f"halted: {res.metrics['halted']}")
        print(format_metrics("buy & hold (equal wt)", compute_metrics(bh, pd.DataFrame())))
        res.equity.to_csv(f"reports/equity_{strategy.name}_{label}.csv")
        if len(res.trades):
            res.trades.to_csv(f"reports/trades_{strategy.name}_{label}.csv", index=False)


def cmd_account(a, settings):
    from tradebot.execution import Broker

    b = Broker(settings)
    print(b.account())
    for s, p in b.positions().items():
        print(s, p)
    print(b.clock())


def cmd_run_once(a, settings):
    from tradebot.orchestrator import run_once

    run_once(settings, get_strategy(a.strategy), a.timeframe, a.dry_run)


def cmd_live(a, settings):
    from tradebot.orchestrator import live_loop

    live_loop(settings, get_strategy(a.strategy), a.timeframe, a.dry_run)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("command", choices=["backtest", "account", "run-once", "live"])
    p.add_argument("--strategy", default="rsi2", choices=sorted(STRATEGIES))
    p.add_argument("--symbols", help="comma separated, default: config.py list")
    p.add_argument("--timeframe", default="1d")
    p.add_argument("--start", default="2016-01-01")
    p.add_argument("--end")
    p.add_argument("--split", help="date: report in-sample before / out-of-sample after")
    p.add_argument("--capital", type=float, default=100_000)
    p.add_argument("--csv-dir", help="backtest from local CSV files instead of Alpaca")
    p.add_argument("--dry-run", action="store_true", help="decide but do not send orders")
    a = p.parse_args()
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler("logs/bot.log")])
    settings = Settings()
    if a.symbols:
        settings.symbols = [s.strip().upper() for s in a.symbols.split(",")]
    {"backtest": cmd_backtest, "account": cmd_account, "run-once": cmd_run_once, "live": cmd_live}[a.command](a, settings)


if __name__ == "__main__":
    main()
