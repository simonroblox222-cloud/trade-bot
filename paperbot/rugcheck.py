"""Rug / scam screening. Every hard fail goes into `reasons`; anything in reasons => rejected."""
from __future__ import annotations

from .config import SOL_MINT, RugConfig
from .models import RugReport, TokenCandidate
from .solana_keys import is_wallet
from .sources import OnchainProvider, QuoteProvider, RugApiProvider, SourceUnavailable, TTLCache


class RugChecker:
    def __init__(self, cfg: RugConfig, onchain: OnchainProvider, rug_api: RugApiProvider | None,
                 quotes: QuoteProvider, slippage_bps: int):
        self.cfg = cfg
        self.onchain = onchain
        self.rug_api = rug_api
        self.quotes = quotes
        self.slippage_bps = slippage_bps
        self.cache = TTLCache(cfg.cache_ttl_s)

    def check(self, cand: TokenCandidate, probe_lamports: int) -> RugReport:
        cached = self.cache.get(cand.mint)
        if cached is not None:
            return cached
        try:
            return self._check(cand, probe_lamports)
        except SourceUnavailable as e:  # not cached: try again next cycle
            return RugReport(mint=cand.mint, passed=False, reasons=[f"data source unavailable: {e}"],
                             transient=True)

    def _check(self, cand: TokenCandidate, probe_lamports: int) -> RugReport:
        rep = RugReport(mint=cand.mint, passed=False)
        c = self.cfg

        # 1) on-chain mint account
        mi = self.onchain.mint_info(cand.mint)
        if mi is None:
            rep.reasons.append("mint account unreadable")
        else:
            rep.decimals = mi.decimals
            rep.supply = mi.supply
            if c.require_no_mint_authority and mi.mint_authority:
                rep.reasons.append("mint authority not revoked")
            if c.require_no_freeze_authority and mi.freeze_authority:
                rep.reasons.append("freeze authority not revoked")
            for ext in mi.extensions:
                if ext in c.reject_token2022_extensions:
                    rep.reasons.append(f"token-2022 extension: {ext}")
            fee_cfg = mi.extensions.get("transferFeeConfig")
            if fee_cfg is not None:
                bps = int(((fee_cfg.get("newerTransferFee") or {}).get("transferFeeBasisPoints")) or 0)
                if bps > c.max_transfer_fee_bps:
                    rep.reasons.append(f"transfer fee {bps} bps")

            # 2) holder concentration, by wallet. Accounts owned by programs (PDAs: bonding
            #    curve, AMM pool authorities, vaults) are off-curve and left out; several
            #    accounts of one wallet count together (as chainstacklabs/pump-fun-bot does).
            by_wallet: dict[str, int] = {}
            for h in self.onchain.largest_holders(cand.mint):
                if h.owner in c.pool_owner_allowlist or not is_wallet(h.owner):
                    continue
                key = h.owner or h.token_account
                by_wallet[key] = by_wallet.get(key, 0) + h.amount
            if mi.supply > 0 and by_wallet:
                pcts = sorted((a / mi.supply * 100 for a in by_wallet.values()), reverse=True)
                rep.top10_pct = round(sum(pcts[:10]), 2)
                if rep.top10_pct > c.max_top10_holders_pct:
                    rep.reasons.append(f"top10 wallets {rep.top10_pct:.1f}% > {c.max_top10_holders_pct}%")
                if pcts[0] > c.max_single_holder_pct:
                    rep.reasons.append(f"largest wallet {pcts[0]:.1f}% > {c.max_single_holder_pct}%")
            elif not by_wallet:
                rep.warnings.append("holder data unavailable")

        # 3) liquidity
        if cand.liquidity_usd < c.min_liquidity_usd:
            rep.reasons.append(f"liquidity ${cand.liquidity_usd:,.0f} < ${c.min_liquidity_usd:,.0f}")

        # 4) RugCheck API
        if c.use_rugcheck_api and self.rug_api is not None:
            s = self.rug_api.summary(cand.mint)
            if s is None:
                (rep.reasons if c.require_rugcheck_api else rep.warnings).append("RugCheck API unavailable")
            else:
                score = s.get("score_normalised")
                rep.rugcheck_score = float(score) if score is not None else None
                if rep.rugcheck_score is not None and rep.rugcheck_score > c.max_rugcheck_score_normalised:
                    rep.reasons.append(f"RugCheck score {rep.rugcheck_score:.0f} > {c.max_rugcheck_score_normalised:.0f}")
                for r in s.get("risks") or []:
                    if c.reject_rugcheck_danger and r.get("level") == "danger":
                        rep.reasons.append(f"RugCheck danger: {r.get('name')}")
                    elif r.get("level") == "warn":
                        rep.warnings.append(f"RugCheck: {r.get('name')}")
                lp = s.get("lpLockedPct")
                rep.lp_locked_pct = float(lp) if lp is not None else None

        # 5) LP burned / locked (bonding-curve tokens have no LP to pull)
        if cand.is_bonding_curve:
            if not c.allow_bonding_curve:
                rep.reasons.append("pump.fun bonding curve (not graduated)")
        elif c.min_lp_locked_pct > 0:
            if rep.lp_locked_pct is None:
                (rep.reasons if c.require_lp_data else rep.warnings).append("LP lock status unknown")
            elif rep.lp_locked_pct < c.min_lp_locked_pct:
                rep.reasons.append(f"LP burned/locked {rep.lp_locked_pct:.0f}% < {c.min_lp_locked_pct:.0f}%")

        # 6) sell simulation: round-trip quote: buy then sell back. Catches missing sell routes,
        #    hidden taxes and extreme impact. Quotes don't require holding tokens.
        if not rep.reasons:
            buy = self.quotes.quote(SOL_MINT, cand.mint, probe_lamports, self.slippage_bps)
            sell = self.quotes.quote(cand.mint, SOL_MINT, buy.out_amount, self.slippage_bps) if buy else None
            if not buy:
                rep.reasons.append("no buy route on Jupiter")
            elif not sell:
                rep.reasons.append("no sell route on Jupiter (honeypot?)")
            else:
                loss = (1 - sell.out_amount / probe_lamports) * 100
                rep.roundtrip_loss_pct = round(loss, 2)
                if loss > c.max_roundtrip_loss_pct:
                    rep.reasons.append(f"round-trip loss {loss:.1f}% > {c.max_roundtrip_loss_pct}%")

        rep.passed = not rep.reasons
        self.cache.put(cand.mint, rep)
        return rep
