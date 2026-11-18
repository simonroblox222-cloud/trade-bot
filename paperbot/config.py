"""Configuration.

LIVE TRADING IS HARD-DISABLED. `LIVE_TRADING_ENABLED` is a code constant, not a
config value: no config file, env var or CLI flag can switch it on. Enabling live
mode later requires a deliberate code change (and implementing LiveExecutor).
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Final

LIVE_TRADING_ENABLED: Final[bool] = False

SOL_MINT: Final = "So11111111111111111111111111111111111111112"
LAMPORTS_PER_SOL: Final = 1_000_000_000


@dataclass
class ApiConfig:
    jupiter_quote_url: str = "https://lite-api.jup.ag/swap/v1/quote"
    jupiter_api_key: str = ""  # optional, sent as x-api-key (needed for api.jup.ag)
    solana_rpc_url: str = "https://api.mainnet-beta.solana.com"
    rugcheck_url: str = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
    dexscreener_profiles_url: str = "https://api.dexscreener.com/token-profiles/latest/v1"
    dexscreener_tokens_url: str = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
    # unofficial pump.fun frontend API (newest coins); may be rate limited / change without notice
    pumpfun_coins_url: str = ("https://frontend-api-v3.pump.fun/coins?offset=0&limit=50"
                              "&sort=created_timestamp&order=DESC&includeNsfw=false")
    use_dexscreener: bool = True
    use_pumpfun: bool = True
    timeout_s: float = 10.0


@dataclass
class RateLimitConfig:
    """Requests per minute per API. Defaults sit just under the free plans (Nov 2026,
    verify against current docs). 0 = unlimited. With a free Helius/QuickNode RPC
    raise `rpc` (e.g. Helius free ~10 req/s -> 500)."""
    jupiter: float = 55              # free tier ~60 rpm
    dexscreener_profiles: float = 55  # token-profiles: 60 rpm
    dexscreener_pairs: float = 250    # tokens/pairs: 300 rpm
    pumpfun: float = 20               # unofficial API, be gentle
    rugcheck: float = 20              # no published limit for keyless use
    rpc: float = 90                   # public mainnet-beta is strict, esp. getTokenLargestAccounts


@dataclass
class RugConfig:
    require_no_mint_authority: bool = True
    require_no_freeze_authority: bool = True
    reject_token2022_extensions: list[str] = field(default_factory=lambda: [
        "permanentDelegate", "transferHook", "nonTransferable", "defaultAccountState",
    ])
    max_transfer_fee_bps: int = 0
    min_liquidity_usd: float = 20_000
    max_top10_holders_pct: float = 30.0
    max_single_holder_pct: float = 10.0
    # owners of AMM vaults – excluded from holder concentration
    pool_owner_allowlist: list[str] = field(default_factory=lambda: [
        "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1",  # Raydium AMM v4 authority
        "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL",  # Raydium CPMM authority
    ])
    use_rugcheck_api: bool = True
    require_rugcheck_api: bool = False  # if True, API outage = reject
    max_rugcheck_score_normalised: float = 50.0
    reject_rugcheck_danger: bool = True
    min_lp_locked_pct: float = 90.0  # burned counts as locked (RugCheck lpLockedPct); 0 = ignore
    require_lp_data: bool = False    # True = reject when LP lock info is unknown
    # pump.fun bonding-curve tokens have no LP yet (curve program holds reserves)
    allow_bonding_curve: bool = True
    max_roundtrip_loss_pct: float = 8.0  # buy->sell quote round trip (tax/honeypot heuristic)
    cache_ttl_s: int = 600


@dataclass
class RiskConfig:
    starting_balance_sol: float = 10.0
    position_size_pct: float = 2.0       # % of equity per trade
    max_position_sol: float = 0.5
    min_position_sol: float = 0.05
    max_open_positions: int = 5
    max_total_exposure_pct: float = 30.0  # open positions cost / equity
    daily_loss_limit_pct: float = 5.0     # vs equity at UTC day start
    close_all_on_daily_limit: bool = False
    stop_loss_pct: float = 20.0
    take_profit_pct: float = 50.0
    trailing_stop_pct: float = 15.0       # from peak, once armed
    trailing_arm_pct: float = 20.0        # arm trailing after +X %
    max_hold_minutes: float = 120.0
    token_cooldown_minutes: float = 60.0
    max_price_impact_pct: float = 3.0
    # costs
    slippage_bps: int = 300               # sent to Jupiter (tx would fail below threshold)
    extra_slippage_bps: int = 100         # pessimistic fill vs. quote (latency, MEV)
    base_fee_sol: float = 0.000005
    priority_fee_sol: float = 0.0001
    jito_tip_sol: float = 0.0             # optional, per transaction
    ata_rent_sol: float = 0.00203928      # token account rent, refunded on close
    max_consecutive_quote_failures: int = 3  # then position written off (no route = likely rug)


@dataclass
class StrategyConfig:
    min_liquidity_usd: float = 20_000
    min_volume_h1_usd: float = 25_000
    min_pair_age_minutes: float = 30
    max_pair_age_hours: float = 72
    min_price_change_m5_pct: float = 2.0
    min_price_change_h1_pct: float = 5.0
    max_price_change_h1_pct: float = 300.0
    min_buy_sell_ratio_m5: float = 1.2
    max_candidates_per_tick: int = 5
    allow_bonding_curve: bool = True      # trade pump.fun tokens before graduation


@dataclass
class BotConfig:
    mode: str = "paper"
    # minimum pause between cycles; the real pace is set by the API rate limits
    tick_seconds: float = 2.0
    # skip new rug checks when Jupiter budget is below this (keeps exits responsive)
    min_jupiter_budget_for_entry: float = 3.0
    # share of the Jupiter rate limit used to re-price open positions; the rest goes to new entries.
    # e.g. 55 rpm * 0.6 = 33 marks/min -> with 5 open positions each is re-priced every ~9 s
    jupiter_mark_share: float = 0.6
    db_path: str = "paperbot.sqlite"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    api: ApiConfig = field(default_factory=ApiConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    rug: RugConfig = field(default_factory=RugConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)


def _apply(obj, data: dict) -> None:
    known = {f.name: f for f in fields(obj)}
    for key, value in data.items():
        if key not in known:
            raise ValueError(f"Unknown config key: {type(obj).__name__}.{key}")
        current = getattr(obj, key)
        if hasattr(current, "__dataclass_fields__") and isinstance(value, dict):
            _apply(current, value)
        else:
            setattr(obj, key, value)


ENV_OVERRIDES = {
    # env var -> ApiConfig field. Keeps RPC/API keys out of files in the repo.
    "PAPERBOT_RPC_URL": "solana_rpc_url",
    "JUPITER_API_KEY": "jupiter_api_key",
    "PAPERBOT_JUPITER_QUOTE_URL": "jupiter_quote_url",
}


def _apply_env(cfg: "BotConfig") -> None:
    for f in fields(cfg.api):
        v = getattr(cfg.api, f.name)
        if isinstance(v, str) and "$" in v:
            setattr(cfg.api, f.name, os.path.expandvars(v))  # e.g. "...?api-key=${HELIUS_API_KEY}"
    for env, attr in ENV_OVERRIDES.items():
        if os.environ.get(env):
            setattr(cfg.api, attr, os.environ[env])


def load_config(path: str | Path | None) -> BotConfig:
    cfg = BotConfig()
    if path:
        with open(path, "rb") as fh:
            _apply(cfg, tomllib.load(fh))
    _apply_env(cfg)
    if cfg.mode != "paper":
        raise ValueError("Only mode = \"paper\" is supported. Live trading is disabled in code.")
    return cfg
