"""Signal agent: each strategy turns OHLCV bars into entry/exit/stop columns.

Contract: signals on bar t are computed from data up to and including bar t's
close; the backtester and live executor act on the NEXT bar's open. That
avoids look-ahead bias.

Output columns:
  entry  bool   open a long position on the next bar
  exit   bool   close the long position on the next bar
  stop   float  protective stop price for a position opened from this bar
"""
import pandas as pd

from .indicators import atr, rsi, sma


class Strategy:
    name = "base"
    flat_eod = False  # intraday strategies close everything before the session ends

    def generate(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError


class RSI2MeanReversion(Strategy):
    """Buy short-term pullbacks inside a long-term uptrend (Connors RSI(2)).

    Long when close > SMA(trend) and RSI(2) < entry_rsi; exit when close > SMA(exit_ma).
    """

    name = "rsi2"

    def __init__(self, trend=200, entry_rsi=10, exit_ma=5, atr_mult=3.0):
        self.trend, self.entry_rsi, self.exit_ma, self.atr_mult = trend, entry_rsi, exit_ma, atr_mult

    def generate(self, df):
        c = df["close"]
        out = pd.DataFrame(index=df.index)
        out["entry"] = (c > sma(c, self.trend)) & (rsi(c, 2) < self.entry_rsi)
        out["exit"] = c > sma(c, self.exit_ma)
        out["stop"] = c - self.atr_mult * atr(df)
        return out


class DonchianTrend(Strategy):
    """Trend following: buy a new N-day high in an uptrend, exit on an M-day low."""

    name = "trend"

    def __init__(self, entry_n=55, exit_n=20, trend=200, atr_mult=3.0):
        self.entry_n, self.exit_n, self.trend, self.atr_mult = entry_n, exit_n, trend, atr_mult

    def generate(self, df):
        c = df["close"]
        out = pd.DataFrame(index=df.index)
        prior_high = df["high"].rolling(self.entry_n).max().shift()
        prior_low = df["low"].rolling(self.exit_n).min().shift()
        out["entry"] = (c > prior_high) & (c > sma(c, self.trend))
        out["exit"] = c < prior_low
        out["stop"] = c - self.atr_mult * atr(df)
        return out


class OpeningRangeBreakout(Strategy):
    """Intraday: buy a break above the first `range_minutes` high, flat at the close.

    Needs intraday bars (e.g. 5 min) with a tz-aware DatetimeIndex.
    """

    name = "orb"
    flat_eod = True

    def __init__(self, range_minutes=30, tz="America/New_York"):
        self.range_minutes, self.tz = range_minutes, tz

    def generate(self, df):
        idx = df.index.tz_convert(self.tz) if df.index.tz is not None else df.index
        minutes = pd.Series(idx.hour * 60 + idx.minute, index=df.index)
        session_start = 9 * 60 + 30
        in_range = (minutes >= session_start) & (minutes < session_start + self.range_minutes)
        day = pd.Series(idx.date, index=df.index)
        or_high = df["high"].where(in_range).groupby(day).transform("max")
        or_low = df["low"].where(in_range).groupby(day).transform("min")
        after = minutes >= session_start + self.range_minutes
        broke = after & (df["close"] > or_high)
        # only the first breakout of each day
        first = broke & ~broke.groupby(day).shift(fill_value=False).groupby(day).cummax()
        out = pd.DataFrame(index=df.index)
        out["entry"] = first & (minutes < 15 * 60)  # no new entries in the last hour
        out["exit"] = minutes >= 15 * 60 + 50       # flat 10 minutes before the close
        out["stop"] = or_low
        return out


STRATEGIES = {s.name: s for s in (RSI2MeanReversion, DonchianTrend, OpeningRangeBreakout)}


def get_strategy(name: str, **params) -> Strategy:
    try:
        return STRATEGIES[name](**params)
    except KeyError:
        raise SystemExit(f"Unknown strategy {name!r}. Choose from {sorted(STRATEGIES)}")
