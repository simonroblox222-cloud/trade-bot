"""Correctness tests on synthetic data. They check mechanics, not profitability."""
import numpy as np
import pandas as pd
import pytest

from tradebot import orchestrator
from tradebot.backtest import run_backtest
from tradebot.config import RiskConfig, Settings
from tradebot.indicators import rsi
from tradebot.risk import RiskManager
from tradebot.strategies import DonchianTrend, OpeningRangeBreakout, RSI2MeanReversion


def make_daily(n=600, drift=0.0005, vol=0.012, seed=1):
    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(drift, vol, n)))
    open_ = close * (1 + rng.normal(0, 0.002, n))
    high = np.maximum(open_, close) * (1 + abs(rng.normal(0, 0.004, n)))
    low = np.minimum(open_, close) * (1 - abs(rng.normal(0, 0.004, n)))
    idx = pd.bdate_range("2020-01-01", periods=n, tz="UTC") + pd.Timedelta(hours=5)
    return pd.DataFrame(dict(open=open_, high=high, low=low, close=close, volume=1e6), index=idx)


def test_rsi_bounds():
    r = rsi(make_daily()["close"], 2).dropna()
    assert r.between(0, 100).all()


@pytest.mark.parametrize("strategy", [RSI2MeanReversion(), DonchianTrend()])
def test_signals_have_no_lookahead(strategy):
    df = make_daily()
    full = strategy.generate(df)
    for cut in (300, 450, 599):
        partial = strategy.generate(df.iloc[:cut])
        pd.testing.assert_frame_equal(full.iloc[:cut], partial)


def test_risk_sizing_caps():
    rm = RiskManager(RiskConfig(risk_per_trade=0.01, max_position_pct=0.2), 100_000)
    # 1 % risk / $2 stop distance = 500 shares, but 20 % cap at $100 = 200 shares
    assert rm.size(100_000, 100_000, 100, 98, 0) == 200
    # wide stop: risk limit binds, 1000 / 10 = 100 shares
    assert rm.size(100_000, 100_000, 100, 90, 0) == 100
    assert rm.size(100_000, 100_000, 100, 101, 0) == 0  # stop above price
    assert rm.size(100_000, 100_000, 100, 90, 5) == 0   # too many positions


def test_kill_switch_and_daily_halt():
    rm = RiskManager(RiskConfig(max_daily_loss_pct=0.03, max_drawdown_pct=0.15), 100_000)
    rm.update(96_000)
    assert rm.state.reason == "daily_loss"
    rm.new_day(96_000)
    assert not rm.state.halted
    rm.update(84_000)
    assert rm.state.reason == "max_drawdown"
    rm.new_day(84_000)
    assert rm.state.halted


def test_backtest_accounting_is_consistent():
    bars = {"AAA": make_daily(seed=1), "BBB": make_daily(seed=2)}
    res = run_backtest(bars, RSI2MeanReversion(), 100_000)
    assert len(res.trades) > 0
    assert res.equity.iloc[-1] == pytest.approx(100_000 + res.trades.pnl.sum(), rel=1e-9)
    # entries fill strictly after the signal bar
    sig = RSI2MeanReversion().generate(bars["AAA"])
    for t in res.trades[res.trades.symbol == "AAA"].entry_time:
        prev = bars["AAA"].index[bars["AAA"].index.get_loc(t) - 1]
        assert sig.loc[prev, "entry"]


def make_intraday(days=5, seed=3):
    rng = np.random.default_rng(seed)
    frames = []
    for d in pd.bdate_range("2024-03-04", periods=days):
        idx = pd.date_range(d + pd.Timedelta(hours=9, minutes=30), periods=78, freq="5min", tz="America/New_York")
        c = 100 * np.exp(np.cumsum(rng.normal(0.0002, 0.002, len(idx))))
        frames.append(pd.DataFrame(dict(open=c, high=c * 1.001, low=c * 0.999, close=c, volume=1e5), index=idx))
    return pd.concat(frames).tz_convert("UTC")


def test_orb_is_flat_every_night():
    bars = {"AAA": make_intraday()}
    res = run_backtest(bars, OpeningRangeBreakout(), 100_000)
    if len(res.trades):
        assert (res.trades.entry_time.dt.date == res.trades.exit_time.dt.date).all()


def test_completed_bars_drops_forming_bar():
    df = make_intraday(days=1)
    now = df.index[10] + pd.Timedelta(minutes=2)
    out = orchestrator.completed_bars(df, "5m", now)
    assert out.index[-1] == df.index[9]


class FakeBroker:
    def __init__(self):
        self.orders = []

    def account(self):
        return dict(equity=100_000, cash=100_000, last_equity=100_000, buying_power=200_000, blocked=False)

    def positions(self):
        return {}

    def peak_equity(self):
        return 100_000

    def has_open_order(self, s):
        return False

    def buy(self, s, qty, stop):
        self.orders.append(("buy", s, qty, stop))

    def sell_all(self, s):
        self.orders.append(("sell", s))

    def flatten(self):
        self.orders.append(("flatten",))


def test_run_once_sends_risk_sized_order(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    df = make_daily()
    strat = RSI2MeanReversion()
    # find a day with an entry signal and pretend "today" is the next day
    sig = strat.generate(df)
    t = sig.index[sig.entry][-1]
    monkeypatch.setattr(orchestrator, "recent_bars", lambda *a, **k: {"AAA": df.loc[:t]})
    monkeypatch.setattr(orchestrator, "completed_bars", lambda d, tf, now=None: d)
    s = Settings(api_key="x", secret_key="y")
    s.symbols = ["AAA"]
    b = FakeBroker()
    orchestrator.run_once(s, strat, broker=b)
    assert b.orders and b.orders[0][0] == "buy"
    _, _, qty, stop = b.orders[0]
    assert qty * (df.loc[t, "close"] - stop) <= 1000 + 1e-6


def test_completed_daily_bars_before_and_after_close():
    df = make_daily(10)
    last = df.index[-1]
    morning = pd.Timestamp(last.date(), tz="America/New_York") + pd.Timedelta(hours=9)
    evening = pd.Timestamp(last.date(), tz="America/New_York") + pd.Timedelta(hours=17)
    assert orchestrator.completed_bars(df, "1d", morning.tz_convert("UTC")).index[-1] == df.index[-2]
    assert orchestrator.completed_bars(df, "1d", evening.tz_convert("UTC")).index[-1] == last
