from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Quote:
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    other_amount_threshold: int
    price_impact_pct: float
    slippage_bps: int
    route_labels: list[str] = field(default_factory=list)
    raw: dict | None = None


@dataclass
class TokenCandidate:
    mint: str
    symbol: str
    liquidity_usd: float
    volume_h1_usd: float
    price_change_m5_pct: float
    price_change_h1_pct: float
    buys_m5: int
    sells_m5: int
    pair_created_at_ms: int | None
    price_usd: float | None = None
    fdv_usd: float | None = None
    pair_address: str | None = None
    dex: str | None = None
    source: str = "dexscreener"          # dexscreener | pumpfun | mock
    is_bonding_curve: bool = False       # pump.fun curve, not yet graduated


@dataclass
class MintInfo:
    decimals: int
    supply: int
    mint_authority: str | None
    freeze_authority: str | None
    token_program: str  # "spl-token" | "spl-token-2022"
    extensions: dict[str, dict] = field(default_factory=dict)


@dataclass
class Holder:
    owner: str | None
    token_account: str
    amount: int


@dataclass
class RugReport:
    mint: str
    passed: bool
    reasons: list[str] = field(default_factory=list)   # hard fails
    warnings: list[str] = field(default_factory=list)
    rugcheck_score: float | None = None
    decimals: int | None = None
    top10_pct: float | None = None
    lp_locked_pct: float | None = None
    roundtrip_loss_pct: float | None = None
