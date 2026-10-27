"""Orchestrator: runs one decision cycle data -> signal -> risk -> execution."""
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .config import Settings
from .data import TIMEFRAMES, recent_bars
from .execution import Broker
from .risk import RiskManager
from .strategies import Strategy

log = logging.getLogger("orchestrator")
STATE = Path("logs/state.json")
JOURNAL = Path("logs/journal.csv")


def _bar_length(tf: str) -> pd.Timedelta:
    amount, unit = TIMEFRAMES[tf]
    unit = {"Day": "days", "Hour": "hours", "Minute": "minutes"}[unit]
    return pd.Timedelta(**{unit: amount})


def completed_bars(df: pd.DataFrame, tf: str, now=None) -> pd.DataFrame:
    """Drop the bar that is still forming so signals never use partial data."""
    now = now or pd.Timestamp.now(tz="UTC")
    if tf == "1d":
        today = now.tz_convert("America/New_York").date()
        return df[df.index.tz_convert("America/New_York").date < today]
    return df[df.index + _bar_length(tf) <= now]


def _journal(**row):
    JOURNAL.parent.mkdir(exist_ok=True)
    row = {"time": datetime.now(timezone.utc).isoformat(), **row}
    new = not JOURNAL.exists()
    pd.DataFrame([row]).to_csv(JOURNAL, mode="a", header=new, index=False)


def run_once(settings: Settings, strategy: Strategy, timeframe="1d", dry_run=False, broker=None):
    broker = broker or Broker(settings, dry_run=dry_run)
    acct = broker.account()
    if acct["blocked"]:
        log.error("Account is blocked for trading.")
        return
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    rm = RiskManager(settings.risk, acct["equity"])
    rm.state.peak_equity = max(state.get("peak_equity", 0.0), broker.peak_equity(), acct["equity"])
    rm.state.day_start_equity = acct["last_equity"]
    rm.update(acct["equity"])
    STATE.parent.mkdir(exist_ok=True)
    STATE.write_text(json.dumps({"peak_equity": rm.state.peak_equity}))
    log.info("equity %.2f cash %.2f peak %.2f", acct["equity"], acct["cash"], rm.state.peak_equity)

    if rm.state.reason == "max_drawdown":
        log.error("Kill switch: drawdown limit hit. Flattening and stopping.")
        broker.flatten()
        _journal(action="kill_switch", equity=acct["equity"])
        return

    positions = broker.positions()
    lookback = 400 if timeframe == "1d" else 7
    bars = recent_bars(settings, settings.symbols, timeframe=timeframe, lookback_days=lookback)
    for sym in settings.symbols:
        if sym not in bars:
            continue
        df = completed_bars(bars[sym], timeframe)
        if len(df) < 50:
            log.warning("%s: not enough bars (%d)", sym, len(df))
            continue
        sig = strategy.generate(df).iloc[-1]
        price = df["close"].iloc[-1]
        if sym in positions:
            if sig["exit"]:
                broker.sell_all(sym)
                _journal(action="sell", symbol=sym, price=price, pnl=positions[sym]["pnl"])
                positions.pop(sym)
        elif sig["entry"] and not sig["exit"] and not broker.has_open_order(sym):
            qty = rm.size(acct["equity"], acct["cash"], price, sig["stop"], len(positions))
            if qty:
                broker.buy(sym, qty, sig["stop"])
                _journal(action="buy", symbol=sym, qty=qty, price=price, stop=sig["stop"])
                positions[sym] = {"qty": qty}
                acct["cash"] -= qty * price
            else:
                log.info("%s: entry signal rejected by risk manager (%s)", sym, rm.state.reason or "sizing")


def live_loop(settings: Settings, strategy: Strategy, timeframe="5m", dry_run=False):
    """Intraday loop: one cycle per bar while the market is open, flat before the close."""
    broker = Broker(settings, dry_run=dry_run)
    step = _bar_length(timeframe).total_seconds()
    while True:
        clock = broker.clock()
        if not clock.is_open:
            wait = (clock.next_open - clock.timestamp).total_seconds()
            log.info("Market closed, sleeping %.0f min until next open.", wait / 60)
            time.sleep(min(max(wait, 60), 3600))
            continue
        to_close = (clock.next_close - clock.timestamp).total_seconds()
        if strategy.flat_eod and to_close < 10 * 60:
            if broker.positions():
                broker.flatten()
                _journal(action="flatten_eod")
            time.sleep(to_close + 60)
            continue
        try:
            run_once(settings, strategy, timeframe, dry_run, broker)
        except Exception:
            log.exception("cycle failed")
        time.sleep(step - time.time() % step + 5)  # wake just after the next bar closes
