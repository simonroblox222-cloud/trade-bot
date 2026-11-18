"""Read-only market data sources: Jupiter quotes, DexScreener, Solana RPC, RugCheck.

Nothing here signs or sends transactions. Only GET quotes and read-only RPC calls.
"""
from __future__ import annotations

import time
import urllib.parse
from typing import Protocol

from .config import ApiConfig
from .http import HttpError, get_json, post_json
from .models import Holder, MintInfo, Quote, TokenCandidate


class QuoteProvider(Protocol):
    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> Quote | None: ...


class DiscoveryProvider(Protocol):
    def candidates(self) -> list[TokenCandidate]: ...


class OnchainProvider(Protocol):
    def mint_info(self, mint: str) -> MintInfo | None: ...
    def largest_holders(self, mint: str) -> list[Holder]: ...


class RugApiProvider(Protocol):
    def summary(self, mint: str) -> dict | None: ...


# --------------------------------------------------------------------------- Jupiter
class JupiterQuotes:
    """GET /quote only. The /swap endpoint (which builds a transaction) is never called."""

    def __init__(self, api: ApiConfig):
        self.api = api

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
            data = get_json(f"{self.api.jupiter_quote_url}?{qs}", self.api.timeout_s, headers)
        except HttpError:
            return None
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

    def __init__(self, api: ApiConfig):
        self.api = api
        self.last_errors: list[str] = []

    def _dexscreener_mints(self) -> list[str]:
        profiles = get_json(self.api.dexscreener_profiles_url, self.api.timeout_s)
        return [p["tokenAddress"] for p in profiles or []
                if p.get("chainId") == "solana" and p.get("tokenAddress")]

    def _pumpfun_mints(self) -> list[str]:
        coins = get_json(self.api.pumpfun_coins_url, self.api.timeout_s)
        if isinstance(coins, dict):
            coins = coins.get("coins") or []
        return [c["mint"] for c in coins or [] if c.get("mint")]

    def candidates(self) -> list[TokenCandidate]:
        self.last_errors = []
        sourced: dict[str, str] = {}
        for name, enabled, fn in (("dexscreener", self.api.use_dexscreener, self._dexscreener_mints),
                                  ("pumpfun", self.api.use_pumpfun, self._pumpfun_mints)):
            if not enabled:
                continue
            try:
                for m in fn():
                    sourced.setdefault(m, name)
            except HttpError as e:
                self.last_errors.append(f"{name}: {e}")
        mints = list(sourced)[:90]
        out: list[TokenCandidate] = []
        for i in range(0, len(mints), 30):  # endpoint accepts up to 30 addresses
            batch = mints[i:i + 30]
            try:
                pairs = get_json(self.api.dexscreener_tokens_url.format(mints=",".join(batch)), self.api.timeout_s)
            except HttpError as e:
                self.last_errors.append(f"dexscreener pairs: {e}")
                continue
            out.extend(self._best_pairs(pairs or [], set(batch), sourced))
        return out

    @staticmethod
    def _best_pairs(pairs: list[dict], wanted: set[str], sourced: dict[str, str]) -> list[TokenCandidate]:
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
            ))
        return out


# --------------------------------------------------------------------------- Solana RPC
class SolanaRpc:
    def __init__(self, api: ApiConfig):
        self.api = api
        self._id = 0

    def _call(self, method: str, params: list):
        self._id += 1
        res = post_json(self.api.solana_rpc_url,
                        {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
                        self.api.timeout_s)
        if "error" in res:
            raise HttpError(f"RPC {method}: {res['error']}")
        return res["result"]

    def mint_info(self, mint: str) -> MintInfo | None:
        try:
            res = self._call("getAccountInfo", [mint, {"encoding": "jsonParsed"}])
        except HttpError:
            return None
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

    def largest_holders(self, mint: str) -> list[Holder]:
        try:
            res = self._call("getTokenLargestAccounts", [mint])
            accounts = res.get("value", [])[:20]
            owners_res = self._call("getMultipleAccounts",
                                    [[a["address"] for a in accounts], {"encoding": "jsonParsed"}])
        except HttpError:
            return []
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
    def __init__(self, api: ApiConfig):
        self.api = api

    def summary(self, mint: str) -> dict | None:
        try:
            return get_json(self.api.rugcheck_url.format(mint=mint), self.api.timeout_s)
        except HttpError:
            return None


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
