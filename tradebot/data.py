"""Data agent: historical and recent bars from Alpaca, cached as CSV."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from .config import Settings

CACHE = Path("data_cache")
TIMEFRAMES = {"1d": (1, "Day"), "1h": (1, "Hour"), "15m": (15, "Minute"), "5m": (5, "Minute"), "1m": (1, "Minute")}


def _client(settings: Settings):
    from alpaca.data.historical import StockHistoricalDataClient

    settings.require_keys()
    return StockHistoricalDataClient(settings.api_key, settings.secret_key)


def _timeframe(tf: str):
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit

    amount, unit = TIMEFRAMES[tf]
    return TimeFrame(amount, getattr(TimeFrameUnit, unit))


def _utc(t) -> pd.Timestamp:
    t = pd.Timestamp(t)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def fetch_bars(settings: Settings, symbols, start, end=None, timeframe="1d", use_cache=True) -> dict:
    """Return {symbol: DataFrame[open, high, low, close, volume]} indexed by UTC timestamp."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.requests import StockBarsRequest

    start = _utc(start)
    # the free IEX feed refuses the most recent 15 minutes
    end = _utc(end) if end else pd.Timestamp.now(tz="UTC") - pd.Timedelta(minutes=16)
    out, missing = {}, []
    for s in symbols:
        path = CACHE / f"{s}_{timeframe}_{start.date()}_{end.date()}.csv"
        if use_cache and path.exists():
            out[s] = pd.read_csv(path, index_col=0, parse_dates=True)
        else:
            missing.append(s)
    if missing:
        req = StockBarsRequest(symbol_or_symbols=missing, timeframe=_timeframe(timeframe), start=start,
                               end=end, adjustment=Adjustment.ALL, feed=DataFeed.IEX)
        df = _client(settings).get_stock_bars(req).df
        CACHE.mkdir(exist_ok=True)
        for s in missing:
            if s not in df.index.get_level_values(0):
                print(f"warning: no data for {s}")
                continue
            sdf = df.xs(s, level=0)[["open", "high", "low", "close", "volume"]].sort_index()
            if timeframe != "1d":
                sdf = regular_session(sdf)
            sdf.to_csv(CACHE / f"{s}_{timeframe}_{start.date()}_{end.date()}.csv")
            out[s] = sdf
    for s in out:
        out[s].index = pd.to_datetime(out[s].index, utc=True)
    return out


def regular_session(df: pd.DataFrame, tz="America/New_York") -> pd.DataFrame:
    local = df.index.tz_convert(tz)
    mins = local.hour * 60 + local.minute
    return df[(mins >= 9 * 60 + 30) & (mins < 16 * 60)]


def recent_bars(settings: Settings, symbols, timeframe="1d", lookback_days=400) -> dict:
    start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
    return fetch_bars(settings, symbols, start, timeframe=timeframe, use_cache=False)


def load_csv_dir(directory, symbols=None) -> dict:
    """Offline use: load <SYMBOL>.csv files with open/high/low/close/volume columns."""
    out = {}
    for p in sorted(Path(directory).glob("*.csv")):
        sym = p.stem.split("_")[0].upper()
        if symbols and sym not in symbols:
            continue
        df = pd.read_csv(p, index_col=0, parse_dates=True)
        df.columns = [c.lower() for c in df.columns]
        df.index = pd.to_datetime(df.index, utc=True)
        out[sym] = df[["open", "high", "low", "close", "volume"]].sort_index()
    return out
