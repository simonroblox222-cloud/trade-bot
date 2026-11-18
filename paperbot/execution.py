"""Fill simulation.

PaperExecution turns a Jupiter quote into a pessimistic simulated fill:
  * Jupiter's outAmount already contains AMM/LP fees and price impact,
  * on top we subtract `extra_slippage_bps` (latency / MEV / quote staleness),
  * every transaction pays base fee + priority fee (+ optional Jito tip),
  * a BUY pays rent for the token account, a full SELL gets it back.

There is no code path that builds, signs or sends a transaction. `LiveExecution`
exists only as the seam for a future implementation and refuses to construct.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from .config import LAMPORTS_PER_SOL, LIVE_TRADING_ENABLED, SOL_MINT, RiskConfig
from .sources import QuoteProvider, SourceUnavailable


class LiveTradingDisabled(RuntimeError):
    pass


@dataclass
class Fill:
    side: str                 # BUY | SELL
    in_amount: int            # lamports (BUY) or raw tokens (SELL)
    quote_out: int            # raw tokens (BUY) or lamports (SELL)
    fill_out: int
    price_impact_pct: float
    slippage_cost_sol: float
    network_fee_sol: float
    rent_sol: float           # +paid (BUY) / refunded (SELL)
    route: str
    min_out: int = 0          # quote's otherAmountThreshold: a real swap reverts below it

    @property
    def net_sol(self) -> float:
        """SELL: SOL that lands in the wallet. BUY: total SOL leaving the wallet."""
        if self.side == "SELL":
            return self.fill_out / LAMPORTS_PER_SOL - self.network_fee_sol + self.rent_sol
        return self.in_amount / LAMPORTS_PER_SOL + self.network_fee_sol + self.rent_sol


class PaperExecution:
    mode = "paper"

    def __init__(self, quotes: QuoteProvider, risk: RiskConfig, sleep=time.sleep):
        self.quotes = quotes
        self.risk = risk
        self.sleep = sleep

    @property
    def tx_fee_sol(self) -> float:
        r = self.risk
        return r.base_fee_sol + r.priority_fee_sol + r.jito_tip_sol

    def _haircut(self, amount: int) -> int:
        return amount * (10_000 - self.risk.extra_slippage_bps) // 10_000

    def simulate_buy(self, mint: str, lamports: int) -> Fill | None:
        q = self.quotes.quote(SOL_MINT, mint, lamports, self.risk.slippage_bps)
        if q is None or q.out_amount <= 0:
            return None
        fill = self._haircut(q.out_amount)
        if fill < q.other_amount_threshold:  # a real swap would revert; we'd only lose the fee
            return None
        slip_sol = (q.out_amount - fill) / q.out_amount * lamports / LAMPORTS_PER_SOL
        return Fill("BUY", lamports, q.out_amount, fill, q.price_impact_pct, slip_sol,
                    self.tx_fee_sol, self.risk.ata_rent_sol, " > ".join(q.route_labels),
                    q.other_amount_threshold)

    def simulate_sell(self, mint: str, token_amount: int) -> Fill | None:
        q = self.quotes.quote(mint, SOL_MINT, token_amount, self.risk.slippage_bps)
        if q is None:
            return None
        fill = self._haircut(q.out_amount)
        return Fill("SELL", token_amount, q.out_amount, fill, q.price_impact_pct,
                    (q.out_amount - fill) / LAMPORTS_PER_SOL, self.tx_fee_sol,
                    self.risk.ata_rent_sol, " > ".join(q.route_labels), q.other_amount_threshold)

    # -- orders with simulated latency --------------------------------------------
    # A real order lands ~1-3 s after the decision (meme_traderv1 measured that this
    # delay alone erased its paper edge). The order's minOut comes from the decision
    # quote; we re-quote after `latency_seconds` and fill at the worse of the two.
    # If the later price is below minOut the swap would revert: no fill, fee lost.

    def _settle(self, first: Fill, later: Fill | None) -> ExecResult:
        if later is None:
            return ExecResult(None, "no route after latency (order not sent)", 0.0)
        if later.fill_out < first.min_out:
            return ExecResult(None, "price moved past slippage during latency: swap reverted",
                              self.risk.base_fee_sol + self.risk.priority_fee_sol)
        return ExecResult(later if later.fill_out < first.fill_out else first, "", 0.0)

    def execute_buy(self, mint: str, lamports: int, max_impact_pct: float | None = None) -> ExecResult:
        try:
            return self._execute_buy(mint, lamports, max_impact_pct)
        except SourceUnavailable as e:
            return ExecResult(None, f"quote API unavailable: {e}", 0.0)

    def execute_sell(self, mint: str, token_amount: int, first: Fill | None = None) -> ExecResult:
        try:
            return self._execute_sell(mint, token_amount, first)
        except SourceUnavailable as e:
            return ExecResult(None, f"quote API unavailable: {e}", 0.0)

    def _execute_buy(self, mint: str, lamports: int, max_impact_pct: float | None) -> ExecResult:
        first = self.simulate_buy(mint, lamports)
        if first is None:
            return ExecResult(None, "no buy route / fill below minOut", 0.0)
        if max_impact_pct is not None and first.price_impact_pct > max_impact_pct:
            return ExecResult(None, f"price impact {first.price_impact_pct:.2f}% > {max_impact_pct}%", 0.0)
        if self.risk.latency_seconds <= 0:
            return ExecResult(first, "", 0.0)
        self.sleep(self.risk.latency_seconds)
        return self._settle(first, self.simulate_buy(mint, lamports))

    def _execute_sell(self, mint: str, token_amount: int, first: Fill | None) -> ExecResult:
        first = first or self.simulate_sell(mint, token_amount)
        if first is None:
            return ExecResult(None, "no sell route", 0.0)
        if self.risk.latency_seconds <= 0:
            return ExecResult(first, "", 0.0)
        self.sleep(self.risk.latency_seconds)
        return self._settle(first, self.simulate_sell(mint, token_amount))


@dataclass
class ExecResult:
    fill: Fill | None
    error: str
    fee_lost_sol: float       # network fee burned by a reverted swap


class LiveExecution:  # pragma: no cover - intentionally unusable
    """Placeholder for a future live backend (wallet, signing, Jito, confirmations).
    Must stay unusable while LIVE_TRADING_ENABLED is False."""

    def __init__(self, *_, **__):
        raise LiveTradingDisabled("Live trading is disabled in code (config.LIVE_TRADING_ENABLED = False).")


def make_execution(mode: str, quotes: QuoteProvider, risk: RiskConfig, sleep=time.sleep) -> PaperExecution:
    if mode == "paper":
        return PaperExecution(quotes, risk, sleep)
    if not LIVE_TRADING_ENABLED:
        raise LiveTradingDisabled(f"mode={mode!r} refused: only paper trading is available.")
    return LiveExecution(quotes, risk)  # unreachable today
