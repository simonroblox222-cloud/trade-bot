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

from dataclasses import dataclass

from .config import LAMPORTS_PER_SOL, LIVE_TRADING_ENABLED, SOL_MINT, RiskConfig
from .sources import QuoteProvider


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

    @property
    def net_sol(self) -> float:
        """SELL: SOL that lands in the wallet. BUY: total SOL leaving the wallet."""
        if self.side == "SELL":
            return self.fill_out / LAMPORTS_PER_SOL - self.network_fee_sol + self.rent_sol
        return self.in_amount / LAMPORTS_PER_SOL + self.network_fee_sol + self.rent_sol


class PaperExecution:
    mode = "paper"

    def __init__(self, quotes: QuoteProvider, risk: RiskConfig):
        self.quotes = quotes
        self.risk = risk

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
                    self.tx_fee_sol, self.risk.ata_rent_sol, " > ".join(q.route_labels))

    def simulate_sell(self, mint: str, token_amount: int) -> Fill | None:
        q = self.quotes.quote(mint, SOL_MINT, token_amount, self.risk.slippage_bps)
        if q is None:
            return None
        fill = self._haircut(q.out_amount)
        return Fill("SELL", token_amount, q.out_amount, fill, q.price_impact_pct,
                    (q.out_amount - fill) / LAMPORTS_PER_SOL, self.tx_fee_sol,
                    self.risk.ata_rent_sol, " > ".join(q.route_labels))


class LiveExecution:  # pragma: no cover - intentionally unusable
    """Placeholder for a future live backend (wallet, signing, Jito, confirmations).
    Must stay unusable while LIVE_TRADING_ENABLED is False."""

    def __init__(self, *_, **__):
        raise LiveTradingDisabled("Live trading is disabled in code (config.LIVE_TRADING_ENABLED = False).")


def make_execution(mode: str, quotes: QuoteProvider, risk: RiskConfig) -> PaperExecution:
    if mode == "paper":
        return PaperExecution(quotes, risk)
    if not LIVE_TRADING_ENABLED:
        raise LiveTradingDisabled(f"mode={mode!r} refused: only paper trading is available.")
    return LiveExecution(quotes, risk)  # unreachable today
