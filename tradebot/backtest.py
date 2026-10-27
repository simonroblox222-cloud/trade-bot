"""Event-driven portfolio backtester sharing the live RiskManager.

Signals from bar t fill at bar t+1's open with slippage. Stops fill at the
stop price, or at the open if the bar gaps through it.
"""
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .config import CostConfig, RiskConfig
from .risk import RiskManager
from .strategies import Strategy


@dataclass
class Position:
    shares: int
    entry_price: float
    stop: float
    entry_time: pd.Timestamp


@dataclass
class Result:
    equity: pd.Series
    trades: pd.DataFrame
    metrics: dict = field(default_factory=dict)


def run_backtest(bars: dict, strategy: Strategy, capital=100_000.0,
                 risk_cfg: RiskConfig = None, costs: CostConfig = None) -> Result:
    risk_cfg, costs = risk_cfg or RiskConfig(), costs or CostConfig()
    slip = costs.slippage_bps / 10_000
    signals = {s: strategy.generate(df) for s, df in bars.items()}
    timeline = sorted(set().union(*[df.index for df in bars.values()]))
    rm = RiskManager(risk_cfg, capital)

    cash, positions, last_close = capital, {}, {}
    pending = {s: None for s in bars}  # previous bar's signal row per symbol
    trades, curve = [], []
    current_day = None

    def equity():
        return cash + sum(p.shares * last_close[s] for s, p in positions.items())

    def close(sym, price, t, why):
        nonlocal cash
        p = positions.pop(sym)
        fill = price * (1 - slip)
        cash += p.shares * fill - p.shares * costs.commission_per_share
        trades.append(dict(symbol=sym, entry_time=p.entry_time, exit_time=t, shares=p.shares,
                           entry=p.entry_price, exit=fill, pnl=p.shares * (fill - p.entry_price),
                           ret=fill / p.entry_price - 1, reason=why))

    for t in timeline:
        day = t.date()
        if day != current_day:
            current_day = day
            rm.new_day(equity())
        for sym, df in bars.items():
            if t not in df.index:
                continue
            bar = df.loc[t]
            o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]
            sig = pending[sym]
            if sig is not None:
                if sym in positions and sig["exit"]:
                    close(sym, o, t, "signal")
                elif sym not in positions and sig["entry"] and not sig["exit"]:
                    fill = o * (1 + slip)
                    shares = rm.size(equity(), cash, fill, sig["stop"], len(positions))
                    if shares:
                        cash -= shares * fill + shares * costs.commission_per_share
                        positions[sym] = Position(shares, fill, sig["stop"], t)
            if sym in positions and l <= positions[sym].stop:
                close(sym, min(o, positions[sym].stop), t, "stop")
            last_close[sym] = c
            pending[sym] = signals[sym].loc[t]
        eq = equity()
        rm.update(eq)
        if rm.state.reason == "max_drawdown" and positions:  # kill switch flattens, like live
            for sym in list(positions):
                close(sym, last_close[sym], t, "kill_switch")
            eq = equity()
        curve.append((t, eq))

    for sym in list(positions):
        close(sym, last_close[sym], timeline[-1], "end")
    eq = pd.Series(dict(curve)).sort_index()
    eq.iloc[-1] = cash
    tr = pd.DataFrame(trades)
    return Result(eq, tr, compute_metrics(eq, tr, rm))


def compute_metrics(eq: pd.Series, trades: pd.DataFrame, rm: RiskManager = None) -> dict:
    daily = eq.groupby(eq.index.date).last()
    daily.index = pd.to_datetime(daily.index)
    rets = daily.pct_change().dropna()
    years = max((daily.index[-1] - daily.index[0]).days / 365.25, 1e-9)
    dd = eq / eq.cummax() - 1
    m = {
        "total_return": eq.iloc[-1] / eq.iloc[0] - 1,
        "cagr": (eq.iloc[-1] / eq.iloc[0]) ** (1 / years) - 1,
        "sharpe": (rets.mean() / rets.std() * np.sqrt(252)) if rets.std() > 0 else 0.0,
        "max_drawdown": dd.min(),
        "trades": len(trades),
    }
    if len(trades):
        wins, losses = trades.pnl[trades.pnl > 0], trades.pnl[trades.pnl <= 0]
        m["win_rate"] = len(wins) / len(trades)
        m["profit_factor"] = wins.sum() / -losses.sum() if losses.sum() < 0 else float("inf")
        m["avg_trade"] = trades.ret.mean()
    if rm is not None:
        m["halted"] = rm.state.reason or "no"
    return m


def buy_and_hold(bars: dict, capital=100_000.0) -> pd.Series:
    """Equal-weight buy-and-hold benchmark."""
    closes = pd.DataFrame({s: df["close"] for s, df in bars.items()}).sort_index().ffill().dropna()
    return (closes / closes.iloc[0]).mean(axis=1) * capital


def format_metrics(name: str, m: dict) -> str:
    pct = lambda k: f"{m[k] * 100:7.2f}%" if k in m else "    n/a"
    return (f"{name:<22} return {pct('total_return')}  CAGR {pct('cagr')}  "
            f"Sharpe {m.get('sharpe', 0):5.2f}  maxDD {pct('max_drawdown')}  "
            f"trades {m.get('trades', 0):4d}  win {pct('win_rate')}  "
            f"PF {m.get('profit_factor', float('nan')):5.2f}")
