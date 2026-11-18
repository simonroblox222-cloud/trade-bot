"""Read-only market data sources: Jupiter quotes, DexScreener, Solana RPC, RugCheck.

Nothing here signs or sends transactions. Only GET quotes and read-only RPC calls.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Protocol

from .config import ApiConfig
from .http import HttpError, get_json, post_json
from .ratelimit import Limits, RateLimited
from .models import Holder, MintInfo, Quote, TokenCandidate


class SourceUnavailable(HttpError):
    """Network error, timeout, HTTP 429 or 5xx: the API is down, NOT an answer about the token.
    Callers must not treat it as "no route" (which would write off a position or blacklist a token)."""


def _transient(e: HttpError) -> bool:
    # 403 = blocked (e.g. Cloudflare) is an outage of the source, never "no route"
    return e.status is None or e.status in (403, 429) or e.status >= 500 or e.blocked_s > 0


class QuoteProvider(Protocol):
    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> Quote | None: ...


class DiscoveryProvider(Protocol):
    def candidates(self) -> list[TokenCandidate]: ...


class OnchainProvider(Protocol):
    def mint_info(self, mint: str) -> MintInfo | None: ...
    def wallet_tx_count(self, owner: str, limit: int = 20) -> int | None: ...
    def largest_holders(self, mint: str) -> list[Holder]: ...


class RugApiProvider(Protocol):
    def summary(self, mint: str) -> dict | None: ...


# --------------------------------------------------------------------------- Jupiter
class JupiterQuotes:
    """GET /quote only. The /swap endpoint (which builds a transaction) is never called."""

    def __init__(self, api: ApiConfig, limits: Limits | None = None):
        self.api = api
        self.limiter = (limits or Limits()).get("jupiter")

    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> Quote | None:
        if amount <= 0:
            return None
        qs = urllib.parse.urlencode({
            "inputMint": input_mint, "outputMint": output_mint, "amount": str(int(amount)),
            "slippageBps": str(slippage_bps), "swapMode": "ExactIn",
            "restrictIntermediateTokens": "true",
        })
        headers = {"x-api-key": self.api.jupiter_api_key} if self.api.jupiter_api_key else None
        try:
            data = get_json(f"{self.api.jupiter_quote_url}?{qs}", self.api.timeout_s, headers, self.limiter)
        except HttpError as e:
            if _transient(e):
                raise SourceUnavailable(f"Jupiter: {e}", e.status) from e
            return None  # 4xx: Jupiter answered, e.g. no route for this pair
        if not isinstance(data, dict) or "outAmount" not in data:
            return None
        labels = [(step.get("swapInfo") or {}).get("label", "?") for step in data.get("routePlan", [])]
        return Quote(
            input_mint=input_mint, output_mint=output_mint,
            in_amount=int(data["inAmount"]), out_amount=int(data["outAmount"]),
            other_amount_threshold=int(data.get("otherAmountThreshold", data["outAmount"])),
            price_impact_pct=float(data.get("priceImpactPct") or 0) * 100,  # API returns a fraction
            slippage_bps=int(data.get("slippageBps", slippage_bps)),
            route_labels=labels, raw=data,
        )


# --------------------------------------------------------------------------- Discovery
class MarketDiscovery:
    """New mints from DexScreener token profiles + pump.fun newest coins,
    enriched with pair stats from DexScreener (which also indexes pump.fun curves)."""

    def __init__(self, api: ApiConfig, limits: Limits | None = None):
        self.api = api
        limits = limits or Limits()
        self.lim_profiles = limits.get("dexscreener_profiles")
        self.lim_pairs = limits.get("dexscreener_pairs")
        self.lim_pumpfun = limits.get("pumpfun")
        self.last_errors: list[str] = []
        self._mints_cache: dict[str, list[str]] = {}
        self._pf_meta: dict[str, dict] = {}      # pump.fun coin fields we use (numbers / presence only)
        self._watch: dict[str, list] = {}        # mint -> [first_seen, source, misses]
        self._dead: set[str] = set()             # no pair / dead pool: don't re-add from the feeds
        self.stats = {"feeds": 0, "watchlist": 0, "pairs": 0}

    def _dexscreener_mints(self) -> list[str]:
        # non-blocking: when the budget is used up we reuse the previous list
        profiles = get_json(self.api.dexscreener_profiles_url, self.api.timeout_s,
                            limiter=self.lim_profiles, block=False)
        return [p["tokenAddress"] for p in profiles or []
                if p.get("chainId") == "solana" and p.get("tokenAddress")]

    def _pumpfun_mints(self) -> list[str]:
        coins = get_json(self.api.pumpfun_coins_url, self.api.timeout_s, limiter=self.lim_pumpfun, block=False)
        if isinstance(coins, dict):
            coins = coins.get("coins") or []
        for c in coins or []:
            if c.get("mint"):
                self._pf_meta[c["mint"]] = {
                    "replies": int(c.get("reply_count") or 0),
                    "koth": bool(c.get("king_of_the_hill_timestamp")),
                    "x": bool(c.get("twitter")), "tg": bool(c.get("telegram")), "web": bool(c.get("website")),
                    "creator": c.get("creator"),
                }
        if len(self._pf_meta) > 5000:
            self._pf_meta = dict(list(self._pf_meta.items())[-2000:])
        return [c["mint"] for c in coins or [] if c.get("mint")]

    def _boost_mints(self) -> list[str]:
        boosts = get_json(self.api.dexscreener_boosts_url, self.api.timeout_s,
                          limiter=self.lim_profiles, block=False)
        return [b["tokenAddress"] for b in boosts or []
                if b.get("chainId") == "solana" and b.get("tokenAddress")]

    def candidates(self) -> list[TokenCandidate]:
        self.last_errors = []
        sourced: dict[str, str] = {}
        for name, enabled, fn in (("dexscreener", self.api.use_dexscreener, self._dexscreener_mints),
                                  ("pumpfun", self.api.use_pumpfun, self._pumpfun_mints),
                                  ("boosts", self.api.use_boosts, self._boost_mints)):
            if not enabled:
                continue
            try:
                self._mints_cache[name] = fn()
            except RateLimited:
                pass  # budget exhausted this tick: reuse cached list
            except HttpError as e:
                if e.blocked_s:
                    self.last_errors.append(f"{name}: {e}, paused {e.blocked_s / 60:.0f} min"
                                            + ("; new pump.fun pairs still come via DexScreener" if name == "pumpfun" else ""))
                    self._mints_cache.pop(name, None)
                else:
                    self.last_errors.append(f"{name}: {e}")
            for m in self._mints_cache.get(name, []):
                sourced.setdefault(m, name)
        boosted = set(self._mints_cache.get("boosts", []))
        now = time.time()
        for m, src in sourced.items():
            if m not in self._watch and m not in self._dead:
                self._watch[m] = [now, src, 0]
        cutoff = now - self.api.watchlist_hours * 3600
        self._dead |= {m for m, w in self._watch.items() if w[2] >= 3}
        if len(self._dead) > 20_000:
            self._dead = set(list(self._dead)[-10_000:])
        self._watch = {m: w for m, w in self._watch.items() if w[0] >= cutoff and w[2] < 3}
        # newest first; the rest of the watchlist keeps being re-checked until it ages out or dies
        mints = sorted(self._watch, key=lambda m: self._watch[m][0], reverse=True)[: max(30, self.api.watchlist_size)]
        sourced = {m: self._watch[m][1] for m in mints}
        out: list[TokenCandidate] = []
        for i in range(0, len(mints), 30):  # endpoint accepts up to 30 addresses
            batch = mints[i:i + 30]
            try:
                pairs = get_json(self.api.dexscreener_tokens_url.format(mints=",".join(batch)), self.api.timeout_s,
                                 limiter=self.lim_pairs)
            except HttpError as e:
                self.last_errors.append(f"dexscreener pairs: {e}")
                continue
            found = self._best_pairs(pairs or [], set(batch), sourced, self._pf_meta)
            got = {c.mint for c in found}
            for m in batch:  # no SOL/USDC pair yet, or the pool is dead: drop after 3 tries
                w = self._watch.get(m)
                if w is not None:
                    w[2] = 0 if m in got else w[2] + 1
            for c in found:
                if c.liquidity_usd < 1000 and c.mint in self._watch:
                    self._watch[c.mint][2] = 3
            out.extend(found)
        self.stats = {"feeds": len(set().union(*map(set, self._mints_cache.values()))) if self._mints_cache else 0,
                      "watchlist": len(self._watch), "pairs": len(out)}
        for c in out:  # listed in the paid boosts feed = boosted, even if the pair doesn't say so
            if c.mint in boosted and not c.boosts_active:
                c.boosts_active = 1
        return out

    @staticmethod
    def _best_pairs(pairs: list[dict], wanted: set[str], sourced: dict[str, str],
                    pf_meta: dict[str, dict] | None = None) -> list[TokenCandidate]:
        pf_meta = pf_meta or {}
        best: dict[str, dict] = {}
        for p in pairs:
            base = (p.get("baseToken") or {}).get("address")
            quote_sym = (p.get("quoteToken") or {}).get("symbol")
            if base not in wanted or quote_sym not in ("SOL", "WSOL", "USDC"):
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            if base not in best or liq > ((best[base].get("liquidity") or {}).get("usd") or 0):
                best[base] = p
        out = []
        for mint, p in best.items():
            tx = (p.get("txns") or {}).get("m5") or {}
            pc = p.get("priceChange") or {}
            info = p.get("info") or {}
            social_types = {(s.get("type") or s.get("platform") or "").lower() for s in info.get("socials") or []}
            pf = pf_meta.get(mint, {})
            out.append(TokenCandidate(
                mint=mint, symbol=(p.get("baseToken") or {}).get("symbol", "?"),
                liquidity_usd=float((p.get("liquidity") or {}).get("usd") or 0),
                volume_h1_usd=float((p.get("volume") or {}).get("h1") or 0),
                price_change_m5_pct=float(pc.get("m5") or 0), price_change_h1_pct=float(pc.get("h1") or 0),
                buys_m5=int(tx.get("buys") or 0), sells_m5=int(tx.get("sells") or 0),
                pair_created_at_ms=p.get("pairCreatedAt"),
                price_usd=float(p["priceUsd"]) if p.get("priceUsd") else None,
                fdv_usd=p.get("fdv"), pair_address=p.get("pairAddress"), dex=p.get("dexId"),
                source=sourced.get(mint, "dexscreener"), is_bonding_curve=p.get("dexId") == "pumpfun",
                price_native=float(p["priceNative"]) if p.get("priceNative") else None,
                has_x=bool({"twitter", "x"} & social_types) or pf.get("x", False),
                has_telegram="telegram" in social_types or pf.get("tg", False),
                has_website=bool(info.get("websites")) or pf.get("web", False),
                boosts_active=int((p.get("boosts") or {}).get("active") or 0),
                pf_replies=pf.get("replies"), pf_koth=pf.get("koth", False), creator=pf.get("creator"),
            ))
        return out


# --------------------------------------------------------------------------- Solana RPC
class SolanaRpc:
    def __init__(self, api: ApiConfig, limits: Limits | None = None):
        self.api = api
        self.limiter = (limits or Limits()).get("rpc")
        self._id = 0

    def _call(self, method: str, params: list):
        self._id += 1
        res = post_json(self.api.solana_rpc_url,
                        {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
                        self.api.timeout_s, self.limiter)
        if "error" in res:
            err = res["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            code = err.get("code") if isinstance(err, dict) else None
            raise HttpError(f"RPC {method}: {str(msg)[:120]}", 429 if code == 429 else None)
        return res["result"]

    def mint_info(self, mint: str) -> MintInfo | None:
        try:
            res = self._call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        except HttpError as e:
            raise SourceUnavailable(f"Solana RPC: {e}", e.status) from e
        val = (res or {}).get("value")
        if not val:
            return None
        data = val.get("data") or {}
        if not isinstance(data, dict) or data.get("parsed", {}).get("type") != "mint":
            return None
        info = data["parsed"]["info"]
        exts = {e.get("extension"): e.get("state") or {} for e in info.get("extensions", [])}
        return MintInfo(
            decimals=int(info["decimals"]), supply=int(info["supply"]),
            mint_authority=info.get("mintAuthority"), freeze_authority=info.get("freezeAuthority"),
            token_program=data.get("program", "spl-token"), extensions=exts,
        )

    def wallet_tx_count(self, owner: str, limit: int = 20) -> int | None:
        """How many transactions a wallet has (capped at `limit`): few = freshly created wallet."""
        try:
            res = self._call("getSignaturesForAddress", [owner, {"limit": limit}])
        except HttpError:
            return None
        return len(res or [])

    def largest_holders(self, mint: str) -> list[Holder]:
        try:
            res = self._call("getTokenLargestAccounts", [mint])
            accounts = res.get("value", [])[:20]
            owners_res = self._call("getMultipleAccounts",
                                    [[a["address"] for a in accounts], {"encoding": "jsonParsed"}])
        except HttpError as e:
            # no holder data = no concentration check: never pass a token on that, retry later
            raise SourceUnavailable(f"holders: {e}", e.status) from e
        holders = []
        for acc, info in zip(accounts, owners_res.get("value", [])):
            owner = None
            try:
                owner = info["data"]["parsed"]["info"]["owner"]
            except (TypeError, KeyError):
                pass
            holders.append(Holder(owner=owner, token_account=acc["address"], amount=int(acc["amount"])))
        return holders


# --------------------------------------------------------------------------- RugCheck
class RugCheckApi:
    def __init__(self, api: ApiConfig, limits: Limits | None = None):
        self.api = api
        self.limiter = (limits or Limits()).get("rugcheck")

    def summary(self, mint: str) -> dict | None:
        try:
            return get_json(self.api.rugcheck_url.format(mint=mint), self.api.timeout_s, limiter=self.limiter)
        except HttpError:
            return None

    def report(self, mint: str) -> dict | None:
        """Full report (insiders, creator, holders, markets). Field names follow the public
        RugCheck API as known in Nov 2026; every field is read defensively."""
        try:
            return get_json(self.api.rugcheck_report_url.format(mint=mint), self.api.timeout_s, limiter=self.limiter)
        except HttpError:
            return None


class PumpFunApi:
    def __init__(self, api: ApiConfig, limits: Limits | None = None):
        self.api = api
        self.limiter = (limits or Limits()).get("pumpfun")

    def creator_coins(self, creator: str) -> list[dict] | None:
        try:
            # non-blocking: when pump.fun is blocked/paused, the Critic just notes "dev history unavailable"
            data = get_json(self.api.pumpfun_creator_coins_url.format(creator=creator), self.api.timeout_s,
                            limiter=self.limiter, block=False)
        except (HttpError, RateLimited):
            return None
        if isinstance(data, dict):
            data = data.get("coins")
        return data if isinstance(data, list) else None


class TTLCache:
    def __init__(self, ttl_s: float):
        self.ttl = ttl_s
        self._d: dict = {}

    def get(self, key):
        hit = self._d.get(key)
        if hit and time.time() - hit[0] < self.ttl:
            return hit[1]
        return None

    def put(self, key, value):
        self._d[key] = (time.time(), value)
