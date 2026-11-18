"""Social score and the pre-trade Critic. Rules only: no LLM, no text interpretation.

Social: hype proxies that cost nothing and need no X API: does the token link an X
account / Telegram / website, how many pump.fun comments it has, did it reach pump.fun
"king of the hill". Paid DexScreener boosts count AGAINST it (bought attention).

Critic: the last gate between Sizer and Executor, mainly an extra anti-rug layer for
signals the RugCheck agent does not cover:
  * RugCheck full report: already rugged, insider wallets, insider networks (bundles /
    snipers funded together), dev still holding supply, too few holders
  * dev wallet history on pump.fun: serial launcher, most earlier coins died
  * fresh wallets among the top holders (few transactions = made for this launch)
  * liquidity leaving the pool between scans
  * hype that is too fast to be organic (comments per minute, volume vs liquidity),
    paid boosts without any organic chatter
plus discipline: copycat symbol, too many open bonding-curve / same-source positions,
losing-streak pause, chasing a vertical candle, size too big for the pool, round trip
already close to its limit.

The Critic only approves or rejects. It never changes sizes or settings. Missing data
adds a note, not a rejection.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from .config import CriticConfig, RugConfig, SocialConfig
from .models import RugReport, TokenCandidate
from .solana_keys import is_wallet
from .sources import SourceUnavailable, TTLCache


# --------------------------------------------------------------------- social
def social_score(c: TokenCandidate, cfg: SocialConfig) -> tuple[float, list[str]]:
    score, parts = 0.0, []
    if c.has_x:
        score += cfg.x_link
        parts.append("X")
    if c.has_telegram:
        score += cfg.telegram_link
        parts.append("TG")
    if c.has_website:
        score += cfg.website
        parts.append("web")
    if c.pf_replies is not None:
        if c.pf_replies >= cfg.replies_many:
            score += 2
        elif c.pf_replies >= cfg.replies_some:
            score += 1
        parts.append(f"{c.pf_replies} comments")
    if c.pf_koth:
        score += cfg.king_of_the_hill
        parts.append("KOTH")
    if c.boosts_active > 0:
        score -= cfg.boost_penalty
        parts.append(f"boosted x{c.boosts_active} (-{cfg.boost_penalty:g})")
    return score, parts


# ------------------------------------------------------------ market memory
class MarketMemory:
    """What the Scanner saw in earlier scans: liquidity per mint, mints per symbol."""

    def __init__(self, window_s: float = 900.0, clock=time.time):
        self.window_s = window_s
        self.clock = clock
        self.liq: dict[str, list[tuple[float, float]]] = {}
        self.symbols: dict[str, dict[str, tuple[float, float]]] = {}  # SYMBOL -> mint -> (ts, liq)
        self.px: dict[str, list[tuple[float, float]]] = {}              # mint -> [(ts, price in SOL)]

    def observe(self, cands: list[TokenCandidate]) -> None:
        now = self.clock()
        cut = now - self.window_s
        for c in cands:
            h = [x for x in self.liq.get(c.mint, []) if x[0] >= cut]
            h.append((now, c.liquidity_usd))
            self.liq[c.mint] = h[-60:]
            if c.price_native:
                p = [x for x in self.px.get(c.mint, []) if x[0] >= cut]
                p.append((now, c.price_native))
                self.px[c.mint] = p[-120:]
            sym = (c.symbol or "").strip().upper()
            if sym:
                self.symbols.setdefault(sym, {})[c.mint] = (now, c.liquidity_usd)
        if len(self.liq) > 5000:  # forget mints not seen in the window
            self.liq = {m: h for m, h in self.liq.items() if h and h[-1][0] >= cut}
            self.px = {m: h for m, h in self.px.items() if h and h[-1][0] >= cut}
            self.symbols = {s: {m: v for m, v in d.items() if v[0] >= cut} for s, d in self.symbols.items()}

    def price_range(self, mint: str, minutes: float) -> tuple[float, float, int] | None:
        """(low, high, samples) of the price we saw for this mint in the last `minutes`."""
        cut = self.clock() - minutes * 60
        h = [p for ts, p in self.px.get(mint, []) if ts >= cut]
        return (min(h), max(h), len(h)) if h else None

    def liquidity_drop_pct(self, mint: str, current: float) -> float | None:
        cut = self.clock() - self.window_s
        h = [l for ts, l in self.liq.get(mint, []) if ts >= cut]
        if len(h) < 2 or max(h) <= 0:
            return None
        return max(0.0, (1 - current / max(h)) * 100)

    def same_symbol(self, c: TokenCandidate) -> list[tuple[str, float]]:
        cut = self.clock() - self.window_s
        d = self.symbols.get((c.symbol or "").strip().upper(), {})
        return [(m, liq) for m, (ts, liq) in d.items() if m != c.mint and ts >= cut]


# ------------------------------------------------------------------- critic
@dataclass
class Verdict:
    approved: bool
    reasons: list[str] = field(default_factory=list)   # why rejected
    notes: list[str] = field(default_factory=list)     # data missing / observations
    hard: bool = False                                 # rug-type reject: blacklist the token

    def summary(self) -> str:
        if self.approved:
            return "critic OK" + (f" ({'; '.join(self.notes)})" if self.notes else "")
        return "; ".join(self.reasons)


class Critic:
    def __init__(self, cfg: CriticConfig, rug_cfg: RugConfig, memory: MarketMemory,
                 rug_api=None, pumpfun=None, onchain=None, clock=time.time):
        self.cfg = cfg
        self.rug_cfg = rug_cfg
        self.memory = memory
        self.rug_api = rug_api
        self.pumpfun = pumpfun
        self.onchain = onchain
        self.clock = clock
        self.data_cache = TTLCache(600)   # anti-rug data checks are cached per mint

    # -- behaviour: cheap, uses only what we already know ------------------
    def behaviour(self, c: TokenCandidate, rep: RugReport, size_sol: float,
                  open_pos: list[dict], recent_closed: list[dict]) -> Verdict:
        k, v = self.cfg, Verdict(True)
        now = self.clock()

        if k.losing_streak > 0 and len(recent_closed) >= k.losing_streak:
            last = recent_closed[: k.losing_streak]
            if all((p.get("pnl_sol") or 0) <= 0 for p in last):
                until = (last[0].get("closed_at") or 0) + k.losing_streak_pause_minutes * 60
                if now < until:
                    v.reasons.append(f"{k.losing_streak} losses in a row: pause {int((until - now) / 60) + 1} min")

        if c.price_change_m5_pct > k.max_m5_pct:
            v.reasons.append(f"chasing: m5 {c.price_change_m5_pct:+.0f}% > {k.max_m5_pct:g}%")
        if c.price_change_h1_pct > k.max_h1_pct:
            v.reasons.append(f"chasing: h1 {c.price_change_h1_pct:+.0f}% > {k.max_h1_pct:g}%")

        if c.is_bonding_curve:
            n = sum(1 for p in open_pos if p.get("bonding"))
            if n >= k.max_open_bonding_curve:
                v.reasons.append(f"already {n} bonding-curve positions open")
        n_src = sum(1 for p in open_pos if p.get("source") == c.source)
        if n_src >= k.max_open_same_source:
            v.reasons.append(f"already {n_src} open positions from {c.source}")

        if c.price_usd and c.price_native and c.liquidity_usd > 0:
            sol_usd = c.price_usd / c.price_native
            share = size_sol * sol_usd / c.liquidity_usd * 100
            if share > k.max_size_vs_liquidity_pct:
                v.reasons.append(f"size {share:.1f}% of pool liquidity > {k.max_size_vs_liquidity_pct:g}%")
        else:
            v.notes.append("no SOL price: size vs pool not checked")

        if rep.roundtrip_loss_pct is not None and self.rug_cfg.max_roundtrip_loss_pct > 0:
            share = rep.roundtrip_loss_pct / self.rug_cfg.max_roundtrip_loss_pct
            if share > k.max_roundtrip_share:
                v.reasons.append(f"round trip {rep.roundtrip_loss_pct:.1f}% is {share:.0%} of its limit")

        # --- anti-rug signals that need no API call (rug-type: hard)
        hard = []
        drop = self.memory.liquidity_drop_pct(c.mint, c.liquidity_usd)
        if drop is not None and drop > k.max_liquidity_drop_pct:
            hard.append(f"liquidity left the pool: -{drop:.0f}% in {k.liquidity_window_minutes:g} min")

        if k.reject_copycat_symbol:
            held = {p["mint"] for p in open_pos if (p.get("symbol") or "").upper() == (c.symbol or "").upper()}
            others = self.memory.same_symbol(c)
            if held - {c.mint}:
                hard.append(f"copycat: ${c.symbol} already held under another mint")
            elif any(liq > c.liquidity_usd for _, liq in others):
                hard.append(f"copycat: bigger ${c.symbol} exists ({len(others)} other mints)")

        age_min = (now - c.pair_created_at_ms / 1000) / 60 if c.pair_created_at_ms else None
        if c.pf_replies is not None and age_min and age_min > 0:
            rpm = c.pf_replies / age_min
            if rpm > k.max_replies_per_minute:
                hard.append(f"hype too fast: {rpm:.1f} comments/min (bots?)")
        if c.liquidity_usd > 0 and c.volume_h1_usd / c.liquidity_usd > k.max_volume_to_liquidity_h1:
            hard.append(f"volume 1h {c.volume_h1_usd / c.liquidity_usd:.0f}x liquidity (wash trading?)")
        if c.boosts_active > 0 and not c.has_x and (c.pf_replies or 0) < k.paid_hype_min_replies:
            hard.append("paid boost without X link or organic comments")

        v.reasons.extend(hard)
        v.hard = bool(hard)
        v.approved = not v.reasons
        return v

    # -- anti-rug data: API calls, cached per mint -------------------------
    def rug_data(self, c: TokenCandidate, rep: RugReport) -> tuple[list[str], list[str]]:
        cached = self.data_cache.get(c.mint)
        if cached is not None:
            return cached
        reasons, notes = [], []
        transient = False
        try:
            creator = self._report(c, rep, reasons, notes)
            self._dev_history(c, c.creator or creator, reasons, notes)  # pump.fun's creator first
            self._fresh_wallets(c, rep, reasons, notes)
        except SourceUnavailable as e:
            notes.append(f"data source down: {e}")
            transient = True
        if not transient:
            self.data_cache.put(c.mint, (reasons, notes))
        return reasons, notes

    def _report(self, c, rep, reasons, notes) -> str | None:
        k = self.cfg
        if not (k.use_full_report and self.rug_api is not None and hasattr(self.rug_api, "report")):
            return None
        r = self.rug_api.report(c.mint)
        if not isinstance(r, dict):
            notes.append("RugCheck report unavailable")
            return None
        if r.get("rugged"):
            reasons.append("RugCheck: token marked as rugged")
        tops = [h for h in (r.get("topHolders") or []) if isinstance(h, dict)]
        if tops:
            ins = sum(float(h.get("pct") or 0) for h in tops if h.get("insider"))
            if ins > k.max_insider_pct:
                reasons.append(f"insiders hold {ins:.1f}% > {k.max_insider_pct:g}%")
        nets = r.get("insiderNetworks")
        if isinstance(nets, list):
            nets = [n for n in nets if isinstance(n, dict) and int(n.get("size") or 2) >= 2]
            if len(nets) > k.max_insider_networks:
                biggest = max(int(n.get("size") or 0) for n in nets)
                reasons.append(f"{len(nets)} insider network(s), up to {biggest} wallets (bundle/snipers)")
        elif r.get("graphInsidersDetected"):
            n = int(r.get("graphInsidersDetected") or 0)
            if n > 0 and k.max_insider_networks == 0:
                reasons.append(f"{n} insider wallets detected (bundle/snipers)")
        supply = (r.get("token") or {}).get("supply") or rep.supply
        bal = r.get("creatorBalance")
        if bal is not None and supply:
            pct = float(bal) / float(supply) * 100
            if pct > k.max_creator_pct:
                reasons.append(f"dev still holds {pct:.1f}% > {k.max_creator_pct:g}%")
        holders = r.get("totalHolders")
        if isinstance(holders, (int, float)) and holders < k.min_total_holders:
            reasons.append(f"only {int(holders)} holders < {k.min_total_holders}")
        return r.get("creator") if isinstance(r.get("creator"), str) else None

    def _dev_history(self, c, creator, reasons, notes) -> None:
        k = self.cfg
        if not k.check_dev_history or self.pumpfun is None:
            return
        if not creator:
            notes.append("dev wallet unknown")
            return
        coins = self.pumpfun.creator_coins(creator)
        if coins is None:
            notes.append("dev history unavailable (pump.fun not reachable)")
            return
        prev = [x for x in coins if isinstance(x, dict) and x.get("mint") != c.mint]
        dead = [x for x in prev if not x.get("complete")
                and float(x.get("usd_market_cap") or 0) < k.dead_coin_mcap_usd]
        if len(prev) > k.max_dev_coins:
            reasons.append(f"dev launched {len(prev)}{'+' if len(coins) >= 50 else ''} coins (serial launcher)")
        elif len(prev) >= 5 and len(dead) / len(prev) >= k.max_dev_dead_share:
            reasons.append(f"dev: {len(dead)}/{len(prev)} earlier coins died")
        elif prev:
            notes.append(f"dev: {len(prev)} earlier coins, {len(dead)} dead")

    def _fresh_wallets(self, c, rep, reasons, notes) -> None:
        k = self.cfg
        if not k.check_fresh_wallets or self.onchain is None or not hasattr(self.onchain, "wallet_tx_count"):
            return
        by_wallet: dict[str, int] = {}
        for h in self.onchain.largest_holders(c.mint):
            if not h.owner or h.owner in self.rug_cfg.pool_owner_allowlist or not is_wallet(h.owner):
                continue
            by_wallet[h.owner] = by_wallet.get(h.owner, 0) + h.amount
        if not by_wallet:
            notes.append("holders unavailable: fresh wallets not checked")
            return
        top = sorted(by_wallet.items(), key=lambda kv: kv[1], reverse=True)[: k.fresh_wallet_top_n]
        fresh, fresh_amt, checked = 0, 0, 0
        for owner, amt in top:
            n = self.onchain.wallet_tx_count(owner, limit=max(k.fresh_wallet_max_txs + 1, 10))
            if n is None:
                continue
            checked += 1
            if n <= k.fresh_wallet_max_txs:
                fresh += 1
                fresh_amt += amt
        if not checked:
            notes.append("wallet history unavailable")
            return
        pct = fresh_amt / rep.supply * 100 if rep.supply else 0.0
        if fresh > k.max_fresh_wallets or pct > k.max_fresh_wallets_pct:
            reasons.append(f"{fresh}/{checked} top holders are fresh wallets ({pct:.1f}% of supply)")
        elif fresh:
            notes.append(f"{fresh} fresh wallet(s) in top {checked}")

    def review(self, c: TokenCandidate, rep: RugReport, size_sol: float,
               open_pos: list[dict], recent_closed: list[dict]) -> Verdict:
        v = self.behaviour(c, rep, size_sol, open_pos, recent_closed)
        if not v.approved:   # don't spend API calls on a trade we already refuse
            return v
        reasons, notes = self.rug_data(c, rep)
        v.notes.extend(notes)
        if reasons:
            v.reasons.extend(reasons)
            v.hard = True
            v.approved = False
        return v
