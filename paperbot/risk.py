"""Pure risk rules (no I/O) so they are easy to test."""
from __future__ import annotations

from dataclasses import dataclass

from .config import RiskConfig


@dataclass
class Account:
    cash: float
    equity: float
    open_cost: float      # sum of cost_sol of open positions
    open_count: int
    halted: bool


def size_position(acc: Account, r: RiskConfig, tx_fee_sol: float) -> tuple[float | None, str]:
    if acc.halted:
        return None, "daily loss limit hit, entries paused until UTC midnight"
    if acc.open_count >= r.max_open_positions:
        return None, f"max open positions ({r.max_open_positions})"
    size = min(acc.equity * r.position_size_pct / 100, r.max_position_sol)
    room = acc.equity * r.max_total_exposure_pct / 100 - acc.open_cost
    if room < size:
        size = room
    # keep enough cash for this buy and the later sell fee
    size = min(size, acc.cash - r.ata_rent_sol - 2 * tx_fee_sol)
    if size < r.min_position_sol:
        return None, (f"size {max(size, 0):.3f} SOL < min {r.min_position_sol} "
                      f"(exposure {acc.open_cost:.2f}/{acc.equity * r.max_total_exposure_pct / 100:.2f} SOL)")
    return round(size, 6), (f"{size:.3f} SOL = min({r.position_size_pct}% equity, cap {r.max_position_sol}), "
                            f"exposure after {(acc.open_cost + size) / acc.equity * 100:.1f}%")


def exit_reason(cost: float, value: float, peak: float, held_min: float, r: RiskConfig,
                runner: bool = False, stop_base: float | None = None) -> str | None:
    """`runner`: profit was already taken in part; the rest rides with a wider trailing stop
    and no time limit while it's in profit (so it can catch a big move)."""
    pnl = (value / cost - 1) * 100 if cost else 0.0
    # stop-loss counts the PRICE move since entry (value right after the buy), not the fixed
    # round-trip costs (impact, fees, rent) that every position starts with
    base = stop_base or cost
    move = (value / base - 1) * 100 if base else pnl
    if move <= -r.stop_loss_pct:
        return f"stop-loss {pnl:+.1f}% (price {move:+.1f}% since entry, limit -{r.stop_loss_pct}%)"
    if r.take_profit_pct > 0 and pnl >= r.take_profit_pct:
        return f"take-profit {pnl:+.1f}% (target +{r.take_profit_pct}%)"
    trail = r.runner_trailing_stop_pct if runner else r.trailing_stop_pct
    if peak >= cost * (1 + r.trailing_arm_pct / 100) and value <= peak * (1 - trail / 100):
        return (f"trailing stop {pnl:+.1f}% (peak {(peak / cost - 1) * 100:+.1f}%, "
                f"-{trail:g}% from peak{', runner' if runner else ''})")
    if held_min >= r.max_hold_minutes:
        # time limit is for positions that go nowhere or lose; one in profit is not dumped,
        # it keeps riding until the profit is gone or the trailing stop takes it
        if pnl <= r.time_exit_keep_pnl_pct:
            return f"time exit after {held_min:.0f} min at {pnl:+.1f}%"
        if not runner and value <= peak * (1 - trail / 100):
            return (f"time exit after {held_min:.0f} min at {pnl:+.1f}% "
                    f"(-{trail:g}% from peak {(peak / cost - 1) * 100:+.1f}%)")
    return None


def daily_limit_hit(day_start_equity: float, equity: float, r: RiskConfig) -> bool:
    return day_start_equity > 0 and equity <= day_start_equity * (1 - r.daily_loss_limit_pct / 100)
