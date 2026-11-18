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
    dexscreener_boosts_url: str = "https://api.dexscreener.com/token-boosts/latest/v1"
    rugcheck_report_url: str = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
    # unofficial: coins created by a wallet (dev history). Unverified endpoint, failures only add a note.
    pumpfun_creator_coins_url: str = ("https://frontend-api-v3.pump.fun/coins/user-created-coins/{creator}"
                                      "?offset=0&limit=50&includeNsfw=true")
    # free extra discovery sources (no key)
    geckoterminal_urls: list = field(default_factory=lambda: [
        "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=1",
        "https://api.geckoterminal.com/api/v2/networks/solana/new_pools?page=2",
        "https://api.geckoterminal.com/api/v2/networks/solana/trending_pools?page=1",
    ])
    dexscreener_takeovers_url: str = "https://api.dexscreener.com/community-takeovers/latest/v1"
    # pump.fun graduations + creator wallets over PumpPortal's free websocket (replaces the
    # Cloudflare-protected pump.fun API for discovery)
    pumpportal_ws_url: str = "wss://pumpportal.fun/api/data"
    use_dexscreener: bool = True
    use_geckoterminal: bool = True
    use_takeovers: bool = True
    use_pumpportal: bool = True
    use_pumpfun: bool = True
    use_boosts: bool = True             # boosted tokens as a discovery source (flagged as paid hype)
    # "latest" feeds only show tokens for a few minutes, usually before they're old enough to
    # pass the signal. Keep re-checking every mint we've seen for this long (DexScreener pairs).
    watchlist_hours: float = 12.0
    watchlist_size: int = 240           # mints re-checked per scan (30 per DexScreener call)
    timeout_s: float = 6.0             # a slow API must not stall the whole cycle


@dataclass
class RateLimitConfig:
    """Requests per minute per API. Defaults sit just under the free plans (Nov 2026,
    verify against current docs). 0 = unlimited. With a free Helius/QuickNode RPC
    raise `rpc` (e.g. Helius free ~10 req/s -> 500)."""
    jupiter: float = 55              # free tier ~60 rpm
    dexscreener_profiles: float = 55  # token-profiles: 60 rpm
    dexscreener_pairs: float = 250    # tokens/pairs: 300 rpm
    pumpfun: float = 20               # unofficial API, be gentle
    geckoterminal: float = 10         # documented 30 rpm, but shared per IP and 429s early; pools change slowly
    rugcheck: float = 20              # no published limit for keyless use
    rpc: float = 40                   # public mainnet-beta is strict (429s early, esp. getTokenLargestAccounts)
    rpc_private: float = 300          # used instead when solana_rpc_url is your own endpoint (free Helius: 10 req/s)


@dataclass
class SocialConfig:
    """Hype proxies without the X API: links, pump.fun comments, king of the hill.
    Paid DexScreener boosts count AGAINST a token."""
    enabled: bool = True
    min_score: float = 0.5             # at least one link (X, Telegram or website)
    fresh_token_minutes: float = 30.0  # younger tokens pass without links (they rarely have any yet),
                                       # unless a paid boost pushes the score below 0
    x_link: float = 1.0
    telegram_link: float = 0.5
    website: float = 0.5
    replies_some: int = 20            # pump.fun comments for +1
    replies_many: int = 100           # ... for +2
    king_of_the_hill: float = 1.0
    boost_penalty: float = 1.0


@dataclass
class CriticConfig:
    """Last anti-rug gate before the Executor. Rules only, never changes parameters."""
    enabled: bool = True
    # RugCheck full report
    use_full_report: bool = True
    max_insider_pct: float = 10.0          # % of supply held by flagged insiders
    max_insider_networks: int = 0          # connected wallet clusters (bundles)
    max_creator_pct: float = 5.0           # dev still holds more -> dump risk
    min_total_holders: int = 100
    # dev wallet history (pump.fun)
    check_dev_history: bool = True
    max_dev_coins: int = 10                # serial launcher
    max_dev_dead_share: float = 0.8        # share of dev's earlier coins that died (>= 5 coins)
    dead_coin_mcap_usd: float = 10_000
    # fresh wallets among top holders
    check_fresh_wallets: bool = True
    fresh_wallet_top_n: int = 8
    fresh_wallet_max_txs: int = 5          # wallet with <= this many txs is "fresh"
    max_fresh_wallets: int = 2
    max_fresh_wallets_pct: float = 8.0
    # liquidity pulls and fake hype
    max_liquidity_drop_pct: float = 25.0   # vs max seen in the last window
    liquidity_window_minutes: float = 15.0
    max_replies_per_minute: float = 5.0    # pump.fun comments / minute of age
    max_volume_to_liquidity_h1: float = 15.0
    # behaviour
    reject_copycat_symbol: bool = True
    max_open_bonding_curve: int = 4
    max_open_same_source: int = 6
    losing_streak: int = 3
    losing_streak_pause_minutes: float = 10.0
    max_m5_pct: float = 60.0               # chasing a vertical candle
    max_h1_pct: float = 400.0
    max_size_vs_liquidity_pct: float = 2.0
    max_roundtrip_share: float = 0.6       # rug-check round trip loss vs its limit
    paid_hype_min_replies: int = 20        # boosted tokens need this much organic chatter or an X link


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
    max_open_positions: int = 8
    max_total_exposure_pct: float = 30.0  # open positions cost / equity
    daily_loss_limit_pct: float = 5.0     # vs equity at UTC day start
    close_all_on_daily_limit: bool = False
    # fast in, fast out: bank small gains and move on to the next token
    stop_loss_pct: float = 12.0
    take_profit_pct: float = 0.0          # sell everything at this gain; 0 = never, let the rest run
    # sell in parts on the way up: [gain %, share of the tokens still held]. The rest rides
    # with the trailing stop. [] = off.
    partial_take_profits: list = field(default_factory=lambda: [[12.0, 0.5], [40.0, 0.3]])
    min_partial_sol: float = 0.03         # skip a partial sale smaller than this (fees)
    trailing_stop_pct: float = 7.0        # from peak, once armed
    runner_trailing_stop_pct: float = 20.0  # after the first partial sale: room for a big move
    trailing_arm_pct: float = 10.0        # arm trailing after +X %
    max_hold_minutes: float = 30.0
    time_exit_keep_pnl_pct: float = 1.0      # past max hold, keep a position only while above this profit
    token_cooldown_minutes: float = 20.0
    max_price_impact_pct: float = 3.0
    # costs
    slippage_bps: int = 300               # sent to Jupiter (tx would fail below threshold)
    exit_slippage_bps: int = 1500         # stop-loss / trailing exits: sell even if price slips (no revert loop in a dump)
    extra_slippage_bps: int = 100         # pessimistic fill vs. quote (latency, MEV)
    base_fee_sol: float = 0.000005
    priority_fee_sol: float = 0.0001
    jito_tip_sol: float = 0.0             # optional, per transaction
    ata_rent_sol: float = 0.00203928      # token account rent, refunded on close
    latency_seconds: float = 2.0          # decision -> landed swap; re-quote after this, fill at the worse price
    max_consecutive_quote_failures: int = 3  # then position written off (no route = likely rug)


@dataclass
class StrategyConfig:
    min_liquidity_usd: float = 20_000
    # "active" paper defaults: trade any pair that is moving up right now with buyers in charge.
    # Most memecoins bleed over the hour, so h1 only has to be "not crashing". Rug checks and
    # the Critic are unchanged. For fewer, stronger signals raise m5/h1/ratio/volume.
    min_volume_h1_usd: float = 10_000
    min_pair_age_minutes: float = 5
    max_pair_age_hours: float = 168
    min_price_change_m5_pct: float = 0.5
    min_price_change_h1_pct: float = -15.0
    max_price_change_h1_pct: float = 500.0
    min_buy_sell_ratio_m5: float = 1.0
    # strong organic hype (Social score, paid boosts count against it) eases the momentum checks
    hype_score: float = 2.5               # e.g. X link + 100 pump.fun comments, or X + TG + KOTH
    hype_min_price_change_m5_pct: float = 0.0
    hype_min_buy_sell_ratio_m5: float = 1.0
    hype_min_volume_h1_usd: float = 5_000
    # don't buy the top of a spike (live: 7/7 losses bought right after a +20 % candle)
    max_entry_m5_pct: float = 10.0        # m5 above this = we'd be buying the spike candle itself
    spike_window_minutes: float = 10.0    # look at the price range we saw in this window
    spike_range_pct: float = 15.0         # range wider than this = it just spiked ...
    spike_top_pct: float = 3.0            # ... then only buy once it is this far below the top (pullback)
    max_candidates_per_tick: int = 5
    allow_bonding_curve: bool = True      # trade pump.fun tokens before graduation
    confirm_cycles: int = 2               # signal must pass this many scans in a row


@dataclass
class BotConfig:
    mode: str = "paper"
    # minimum pause between cycles; the real pace is set by the API rate limits
    tick_seconds: float = 2.0
    # skip new rug checks when Jupiter budget is below this (keeps exits responsive)
    min_jupiter_budget_for_entry: float = 4.0  # rug round trip (2) + buy with latency (2)
    # share of the Jupiter rate limit used to re-price open positions; the rest goes to new entries.
    # e.g. 55 rpm * 0.6 = 33 marks/min -> with 5 open positions each is re-priced every ~9 s
    jupiter_mark_share: float = 0.75
    db_path: str = "paperbot.sqlite"
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787
    api: ApiConfig = field(default_factory=ApiConfig)
    rate_limits: RateLimitConfig = field(default_factory=RateLimitConfig)
    social: SocialConfig = field(default_factory=SocialConfig)
    critic: CriticConfig = field(default_factory=CriticConfig)
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
    if "api.mainnet-beta.solana.com" not in cfg.api.solana_rpc_url and cfg.rate_limits.rpc == RateLimitConfig.rpc:
        cfg.rate_limits.rpc = cfg.rate_limits.rpc_private   # own RPC (e.g. free Helius key): faster rug checks
    if cfg.mode != "paper":
        raise ValueError("Only mode = \"paper\" is supported. Live trading is disabled in code.")
    return cfg
