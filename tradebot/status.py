"""Snapshot of the paper account and today's signals, as JSON row files for the dashboard."""
import json
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings
from .data import recent_bars
from .execution import Broker
from .indicators import rsi, sma
from .orchestrator import completed_bars

NAMES = {"SPY": "S&P 500", "QQQ": "Nasdaq 100", "IWM": "Russell 2000", "DIA": "Dow Jones",
         "GLD": "Zlato", "SLV": "Striebro", "USO": "Ropa", "TLT": "Dlhopisy USA"}
START_EQUITY = 100_000.0


def snapshot(settings: Settings, out_dir="reports/dashboard", entry_rsi=10):
    from alpaca.trading.enums import QueryOrderStatus
    from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest

    b = Broker(settings)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    a = b.account()
    pos = b.positions()
    account = [{
        "equity": round(a["equity"], 2), "cash": round(a["cash"], 2),
        "day_pnl": round(a["equity"] - a["last_equity"], 2),
        "total_pnl": round(a["equity"] - START_EQUITY, 2),
        "total_pnl_pct": round(a["equity"] / START_EQUITY - 1, 5),
        "open_positions": len(pos), "updated": now,
    }]

    h = b.client.get_portfolio_history(GetPortfolioHistoryRequest(period="1A", timeframe="1D"))
    equity = [{"day": datetime.fromtimestamp(t, timezone.utc).date().isoformat(), "equity": round(e, 2)}
              for t, e in zip(h.timestamp, h.equity) if e]

    positions = [{"symbol": s, "name": NAMES.get(s, s), "qty": p["qty"], "avg_price": round(p["avg"], 2),
                  "price": round(p["price"], 2), "value": round(p["qty"] * p["price"], 2),
                  "pnl": round(p["pnl"], 2), "pnl_pct": round(p["price"] / p["avg"] - 1, 5)}
                 for s, p in pos.items()]

    orders = [{"time": o.submitted_at.isoformat(timespec="seconds"), "symbol": o.symbol,
               "side": "nákup" if o.side.value == "buy" else "predaj",
               "type": o.order_type.value, "qty": float(o.qty or 0),
               "fill_price": float(o.filled_avg_price) if o.filled_avg_price else None,
               "status": o.status.value, "id": str(o.id)}
              for o in b.client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=30, nested=False))]

    bars = recent_bars(settings, settings.symbols, lookback_days=400)
    signals = []
    for s in settings.symbols:
        if s not in bars:
            continue
        df = completed_bars(bars[s], "1d")
        c = df["close"]
        up = bool(c.iloc[-1] > sma(c, 200).iloc[-1])
        r = float(rsi(c, 2).iloc[-1])
        if s in pos:
            state = "drží"
        elif not up:
            state = "mimo trendu"
        elif r < entry_rsi:
            state = "kúpi ráno"
        else:
            state = "čaká"
        signals.append({"symbol": s, "name": NAMES.get(s, s), "price": round(float(c.iloc[-1]), 2),
                        "trend": "rastie" if up else "klesá", "rsi2": round(r, 1), "state": state,
                        "bar_date": df.index[-1].date().isoformat()})

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, rows in dict(account=account, equity=equity, positions=positions, orders=orders,
                           signals=signals).items():
        (out / f"{name}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    return out
