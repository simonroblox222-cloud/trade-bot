"""Risk manager agent: the only component allowed to say yes to a new position."""
import math
from dataclasses import dataclass

from .config import RiskConfig


@dataclass
class RiskState:
    peak_equity: float
    day_start_equity: float
    halted: bool = False
    reason: str = ""


class RiskManager:
    def __init__(self, cfg: RiskConfig, equity: float):
        self.cfg = cfg
        self.state = RiskState(peak_equity=equity, day_start_equity=equity)

    def new_day(self, equity: float):
        self.state.day_start_equity = equity
        # the daily-loss halt resets each day, the drawdown kill switch does not
        if self.state.halted and self.state.reason == "daily_loss":
            self.state.halted, self.state.reason = False, ""

    def update(self, equity: float):
        s = self.state
        s.peak_equity = max(s.peak_equity, equity)
        if equity <= s.peak_equity * (1 - self.cfg.max_drawdown_pct):
            s.halted, s.reason = True, "max_drawdown"
        elif not s.halted and equity <= s.day_start_equity * (1 - self.cfg.max_daily_loss_pct):
            s.halted, s.reason = True, "daily_loss"

    def size(self, equity: float, cash: float, price: float, stop: float, open_positions: int) -> int:
        """Shares to buy, 0 means rejected."""
        if self.state.halted or open_positions >= self.cfg.max_open_positions:
            return 0
        if not (price > 0 and stop == stop and 0 < stop < price):
            return 0
        by_risk = equity * self.cfg.risk_per_trade / (price - stop)
        by_cap = equity * self.cfg.max_position_pct / price
        by_cash = cash / price
        return max(0, math.floor(min(by_risk, by_cap, by_cash)))
