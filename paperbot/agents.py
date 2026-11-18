"""Agent pipeline: Scanner -> Social -> RugCheck -> Sizer -> Critic -> Executor -> Logger -> Equity.

Each agent is a small class with one job. `Pipeline.tick()` runs one cycle:
  1. Equity   marks open positions to market with real sell quotes
  2. Executor closes positions whose exit rule fired (SL / TP / trailing / time / no route)
  3. Equity   rolls the UTC day and checks the daily loss limit
  4. Scanner  finds candidates that pass the entry signal
  5. Social   scores hype proxies (X/TG links, pump.fun comments, KOTH; paid boosts count against)
  6. RugCheck screens them (authorities, holders, LP, RugCheck API, sell simulation)
  7. Sizer    decides position size under the risk limits
  8. Critic   last anti-rug gate before buying (insiders, dev history, fresh wallets, LP pulls,
              fake hype, copycats, losing streak, chasing); approves or rejects, never tunes
  9. Executor opens simulated positions from fresh Jupiter quotes
  10. Equity  writes an equity snapshot
Logger is used by every agent; everything ends up in SQLite.
"""
from __future__ import annotations

import dataclasses
import time
from datetime import datetime, timezone

from .config import LAMPORTS_PER_SOL, BotConfig
from .critic import Critic, MarketMemory, social_score
from .execution import Fill, PaperExecution
from .models import RugReport, TokenCandidate
from .risk import Account, daily_limit_hit, exit_reason, size_position
from .ratelimit import Limits
from .rugcheck import RugChecker
from .sources import DiscoveryProvider, SourceUnavailable
from .store import Store

AGENTS = ["Scanner", "Social", "RugCheck", "Sizer", "Critic", "Executor", "Logger", "Equity"]


class Logger:
    def __init__(self, store: Store, echo: bool = True):
        self.store = store
        self.echo = echo
        self.count = 0

    def log(self, agent: str, level: str, msg: str, mint: str | None = None) -> None:
        self.store.event(agent, level, msg, mint)
        self.count += 1
        if self.echo:
            ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
            print(f"{ts} [{agent:<8}] {level.upper():<5} {msg}", flush=True)
        self.status("Logger", f"{self.count} events written")

    def status(self, agent: str, summary: str, state: str = "ok") -> None:
        self.store.set(f"agent:{agent}", {"ts": time.time(), "summary": summary, "state": state})

    def trade(self, pos_id: int, cand_mint: str, symbol: str, fill: Fill, reason: str,
              token_amount: int, pnl: float | None = None) -> None:
        sol_amount = fill.in_amount / LAMPORTS_PER_SOL if fill.side == "BUY" else fill.fill_out / LAMPORTS_PER_SOL
        self.store.insert("trades", ts=time.time(), position_id=pos_id, mint=cand_mint, symbol=symbol,
                          side=fill.side, reason=reason, sol_amount=sol_amount, token_amount=token_amount,
                          quote_out=fill.quote_out, fill_out=fill.fill_out,
                          price_impact_pct=fill.price_impact_pct, slippage_cost_sol=fill.slippage_cost_sol,
                          network_fee_sol=fill.network_fee_sol,
                          rent_sol=fill.rent_sol if fill.side == "BUY" else -fill.rent_sol,
                          route=fill.route, pnl_sol=pnl)


class Scanner:
    def __init__(self, cfg: BotConfig, discovery: DiscoveryProvider, store: Store, log: Logger,
                 memory: MarketMemory | None = None):
        self.s = cfg.strategy
        self.memory = memory
        self.social_cfg = cfg.social
        self.cooldown_s = cfg.risk.token_cooldown_minutes * 60
        self.discovery = discovery
        self.store = store
        self.log = log
        self._logged: dict[str, float] = {}
        self._streak: dict[str, int] = {}
        self._err_logged: dict[str, float] = {}
        self._misses: dict[str, int] = {}
        self._scans = 0
        self._summary_at = 0.0          # first summary right after the first scan, then once a minute
        self._started = time.time()

    def reset_streaks(self) -> None:
        """Scanning paused (positions full / halted): confirmations must start over."""
        self._streak.clear()

    def signal(self, c: TokenCandidate, now: float) -> tuple[bool, str]:
        s = self.s
        age_min = (now - c.pair_created_at_ms / 1000) / 60 if c.pair_created_at_ms else None
        ratio = c.buys_m5 / max(c.sells_m5, 1)
        hype, _ = social_score(c, self.social_cfg)
        hot = self.social_cfg.enabled and hype >= s.hype_score   # strong organic hype eases momentum
        min_vol = s.hype_min_volume_h1_usd if hot else s.min_volume_h1_usd
        min_m5 = s.hype_min_price_change_m5_pct if hot else s.min_price_change_m5_pct
        min_ratio = s.hype_min_buy_sell_ratio_m5 if hot else s.min_buy_sell_ratio_m5
        checks = [
            (c.liquidity_usd >= s.min_liquidity_usd, f"liq ${c.liquidity_usd:,.0f}"),
            (c.volume_h1_usd >= min_vol, f"vol1h ${c.volume_h1_usd:,.0f}"),
            (age_min is not None and s.min_pair_age_minutes <= age_min <= s.max_pair_age_hours * 60,
             f"age {age_min:.0f}m" if age_min is not None else "age ?"),
            (c.price_change_m5_pct >= min_m5, f"m5 {c.price_change_m5_pct:+.1f}%"),
            (s.min_price_change_h1_pct <= c.price_change_h1_pct <= s.max_price_change_h1_pct,
             f"h1 {c.price_change_h1_pct:+.1f}%"),
            (ratio >= min_ratio, f"buys/sells {ratio:.2f}"),
            (s.allow_bonding_curve or not c.is_bonding_curve, "bonding curve" if c.is_bonding_curve else "amm"),
        ]
        # anti-spike: never buy the vertical candle itself, and after a spike wait for a pullback
        checks.append((c.price_change_m5_pct <= s.max_entry_m5_pct, f"spike m5 {c.price_change_m5_pct:+.0f}%"
                       if c.price_change_m5_pct > s.max_entry_m5_pct else "no spike candle"))
        rng = self.memory.price_range(c.mint, s.spike_window_minutes) if self.memory and c.price_native else None
        if rng and rng[2] >= 2 and rng[0] > 0:
            lo, hi, _ = rng
            spiked = (hi / lo - 1) * 100 > s.spike_range_pct
            at_top = c.price_native >= hi * (1 - s.spike_top_pct / 100)
            checks.append((not (spiked and at_top),
                           f"spike top: +{(hi / lo - 1) * 100:.0f}% in {s.spike_window_minutes:g}m, waiting for pullback"
                           if spiked and at_top else
                           (f"pullback {(1 - c.price_native / hi) * 100:.0f}% from top" if spiked else "calm range")))
        if hot:
            checks.append((True, f"hype {hype:+.1f} (looser momentum)"))
        failed = [txt for ok, txt in checks if not ok]
        return (not failed), ("momentum: " + ", ".join(t for _, t in checks)) if not failed else ", ".join(failed)

    def run(self, held: set[str]) -> list[tuple[TokenCandidate, str]]:
        now = time.time()
        cands = self.discovery.candidates()
        if self.memory is not None:
            self.memory.observe(cands)
        for err in getattr(self.discovery, "last_errors", []) or []:
            # same error from the same source: one line per 10 minutes, not one per tick
            key = err.split(" paused ")[0]
            if now - self._err_logged.get(key, 0) > 600:
                self._err_logged[key] = now
                self.log.log("Scanner", "warn", f"source error: {err}")
        blacklist = set(self.store.get("blacklist", []))
        picked, passing = [], []
        for c in cands:
            if c.mint in held or c.mint in blacklist:
                continue
            last = self.store.last_exit_ts(c.mint)
            if last and now - last < self.cooldown_s:
                continue
            ok, why = self.signal(c, now)
            if ok:
                passing.append((c, why))
            else:
                for part in why.split(", "):
                    key = {"buys/sells": "buy/sell ratio", "bonding": "bonding curve"}.get(part.split(" ")[0],
                                                                                           part.split(" ")[0])
                    if key == "spike":
                        key = "spike top (waiting for pullback)" if part.startswith("spike top") else "spike candle"
                    elif key == "h1":
                        key = "h1 too high" if c.price_change_h1_pct > self.s.max_price_change_h1_pct else "h1 falling"
                    elif key == "age":
                        age = (now - c.pair_created_at_ms / 1000) / 60 if c.pair_created_at_ms else None
                        key = "age ?" if age is None else ("too young" if age < self.s.min_pair_age_minutes
                                                            else "too old")
                    self._misses[key] = self._misses.get(key, 0) + 1
        # The signal must hold for `confirm_cycles` scans in a row before we act on it
        # (warp-id/solana-trading-bot CONSECUTIVE_FILTER_MATCHES): filters out one-off spikes.
        need = max(1, self.s.confirm_cycles)
        self._streak = {c.mint: self._streak.get(c.mint, 0) + 1 for c, _ in passing}
        waiting = 0
        for c, why in passing:
            if self._streak[c.mint] >= need:
                picked.append((c, f"{why}, confirmed {self._streak[c.mint]}x"))
            else:
                waiting += 1
        picked.sort(key=lambda p: p[0].price_change_m5_pct * min(p[0].buys_m5 / max(p[0].sells_m5, 1), 3),
                    reverse=True)
        picked = picked[: self.s.max_candidates_per_tick]
        self.log.status("Scanner", f"{len(cands)} pairs seen, {len(picked)} confirmed, {waiting} confirming")
        self._scans += 1
        if now - self._summary_at >= 60:   # one line a minute, so a quiet bot is visibly alive
            st = getattr(self.discovery, "stats", None)
            st = st if isinstance(st, dict) else {}
            top = sorted(self._misses.items(), key=lambda kv: kv[1], reverse=True)[:4]
            per = max(self._scans, 1)
            misses = ", ".join(f"{k} {v / per:.0f}" for k, v in top) or "none"
            self.log.log("Scanner", "info",
                         (f"running, {(now - self._started) / 60:.0f} min: " if self._scans > 1 else "first scan: ")
                         + f"{self._scans} scan(s), ~{len(cands)} pairs checked"
                         + (f" (watchlist {st['watchlist']})" if st.get("watchlist") else "")
                         + f", now {len(passing)} pass the signal ({waiting} still confirming)."
                         + (" Sources: " + ", ".join(f"{k} {v}" for k, v in (st.get("sources") or {}).items()) + "."
                            if st.get("sources") else "")
                         + f" Most common misses per scan: {misses}."
                         + (f" PumpPortal: {st['pumpportal']}." if st.get("pumpportal") else ""))
            self._misses, self._scans, self._summary_at = {}, 0, now
        for c, why in picked:
            if now - self._logged.get(c.mint, 0) > 300:  # don't repeat the same pick every cycle
                self._logged[c.mint] = now
                self.log.log("Scanner", "info", f"{c.symbol} [{c.source}] {why}", c.mint)
        return picked


class Social:
    """Hype proxies without the X API. Counts and presence only: no text is read."""

    def __init__(self, cfg: BotConfig, log: Logger):
        self.cfg = cfg.social
        self.log = log
        self._logged: dict[str, float] = {}

    def run(self, picked: list[tuple[TokenCandidate, str]]) -> list[tuple[TokenCandidate, str]]:
        if not self.cfg.enabled:
            self.log.status("Social", "off")
            return picked
        out, weak = [], 0
        now = time.time()
        for c, why in picked:
            score, parts = social_score(c, self.cfg)
            c.social_score = score
            txt = f"social {score:+.1f} ({', '.join(parts) or 'no links, no comments'})"
            age_min = (now - c.pair_created_at_ms / 1000) / 60 if c.pair_created_at_ms else None
            fresh = age_min is not None and age_min < self.cfg.fresh_token_minutes and score >= 0
            if score >= self.cfg.min_score:
                out.append((c, f"{why} | {txt}"))
            elif fresh:   # brand-new token: links/comments usually come later; rug check + Critic still apply
                out.append((c, f"{why} | {txt}, fresh {age_min:.0f}m: socials not required yet"))
            else:
                weak += 1
                if now - self._logged.get(c.mint, 0) > 300:
                    self._logged[c.mint] = now
                    self.log.log("Social", "info", f"{c.symbol} skip: {txt} < {self.cfg.min_score:g}", c.mint)
        self.log.status("Social", f"{len(out)} with hype, {weak} too quiet or paid")
        return out


class RugGuard:
    name = "RugCheck"

    def __init__(self, checker: RugChecker, store: Store, log: Logger):
        self.checker = checker
        self.store = store
        self.log = log
        self._postponed: dict[str, float] = {}

    def run(self, picked: list[tuple[TokenCandidate, str]], probe_sol: float, has_budget=lambda: True):
        passed, failed, deferred = [], 0, 0
        blacklist = set(self.store.get("blacklist", []))
        for c, why in picked:
            cached = self.checker.cache.get(c.mint)
            if cached is not None:
                if cached.passed:
                    passed.append((c, why, cached))
                continue
            if not has_budget():
                deferred += 1  # API budget reserved for open positions; retry next cycle
                continue
            rep = self.checker.check(c, int(probe_sol * LAMPORTS_PER_SOL))
            if rep.transient:
                deferred += 1
                if time.time() - self._postponed.get(c.mint, 0) > 120:   # one line, retried every cycle
                    self._postponed[c.mint] = time.time()
                    self.log.log(self.name, "warn", f"{c.symbol} check postponed, retrying automatically: "
                                 f"{rep.reasons[0]}", c.mint)
                continue
            self.store.record_rugcheck(rep, c)
            if rep.passed:
                extra = f" (warn: {'; '.join(rep.warnings)})" if rep.warnings else ""
                self.log.log(self.name, "info", f"{c.symbol} PASS round-trip {rep.roundtrip_loss_pct}%{extra}", c.mint)
                passed.append((c, why, rep))
            else:
                failed += 1
                blacklist.add(c.mint)
                self.log.log(self.name, "warn", f"{c.symbol} REJECT: {'; '.join(rep.reasons)}", c.mint)
        self.store.set("blacklist", sorted(blacklist)[-5000:])
        self.log.status(self.name, f"{len(passed)} passed, {failed} rejected"
                        + (f", {deferred} deferred (API budget or outage)" if deferred else ""))
        return passed


class Sizer:
    def __init__(self, cfg: BotConfig, log: Logger):
        self.r = cfg.risk
        self.log = log
        self._last: dict[str, tuple[float, str]] = {}

    def run(self, cand: TokenCandidate, acc: Account, tx_fee_sol: float) -> float | None:
        size, why = size_position(acc, self.r, tx_fee_sol)
        msg = f"{cand.symbol}: {why}" if size else f"{cand.symbol} skip: {why}"
        last = self._last.get(cand.mint)
        if last is None or last[1] != msg or time.time() - last[0] > 300:   # no repeat every cycle
            self._last[cand.mint] = (time.time(), msg)
            self.log.log("Sizer", "info" if size else "warn", msg, cand.mint)
        self.log.status("Sizer", why if size else f"skip: {why}")
        return size


class CriticAgent:
    name = "Critic"

    def __init__(self, cfg: BotConfig, critic: Critic, store: Store, log: Logger):
        self.cfg = cfg.critic
        self.critic = critic
        self.store = store
        self.log = log
        self._soft: dict[str, tuple[float, str]] = {}   # mint -> (ts, reason) of last soft reject
        self.approved = self.rejected = 0

    def waiting(self, c: TokenCandidate) -> bool:
        """Soft-rejected in the last 2 min (e.g. losing-streak pause, too many open positions):
        skip it before the Sizer instead of re-sizing it every cycle."""
        hit = self._soft.get(c.mint)
        if hit and time.time() - hit[0] < 120:
            self.log.status(self.name, f"waiting: {c.symbol} ({hit[1]})", "ok")
            return True
        return False

    def review(self, c: TokenCandidate, rep: RugReport, size_sol: float) -> str | None:
        """Returns the verdict text for the trade reason if approved, else None."""
        if not self.cfg.enabled:
            return "critic off"
        now = time.time()
        if self.waiting(c):
            return None
        v = self.critic.review(c, rep, size_sol, self.store.open_positions(), self.store.closed_positions(10))
        self.store.record_critic(c, v.approved, size_sol, v.reasons, v.notes)
        if v.approved:
            self.approved += 1
            self.log.log(self.name, "info", f"{c.symbol} APPROVE {size_sol:.3f} SOL"
                         + (f" (notes: {'; '.join(v.notes)})" if v.notes else ""), c.mint)
        else:
            self.rejected += 1
            if v.hard:  # rug-type signal: never consider this token again
                bl = set(self.store.get("blacklist", []))
                bl.add(c.mint)
                self.store.set("blacklist", sorted(bl)[-5000:])
            else:
                self._soft[c.mint] = (now, "; ".join(v.reasons))
            self.log.log(self.name, "warn", f"{c.symbol} REJECT: {'; '.join(v.reasons)}"
                         + ("" if v.hard else " (not blacklisted, will check again in 2 min)"), c.mint)
        cnt = self.store.critic_counts()
        self.log.status(self.name, f"{cnt['approved']} approved, {cnt['rejected']} rejected")
        return v.summary() if v.approved else None


class Executor:
    def __init__(self, cfg: BotConfig, exe: PaperExecution, store: Store, log: Logger):
        self.r = cfg.risk
        self.exe = exe
        self.store = store
        self.log = log

    def open(self, c: TokenCandidate, rep: RugReport, size_sol: float, signal: str,
             critic: str | None = None) -> int | None:
        res = self.exe.execute_buy(c.mint, int(size_sol * LAMPORTS_PER_SOL), self.r.max_price_impact_pct)
        if res.fill is None:
            self._fee_lost(res.fee_lost_sol)
            self.log.log("Executor", "warn", f"{c.symbol}: buy not filled: {res.error}"
                         + (f" (fee lost {res.fee_lost_sol:.5f} SOL)" if res.fee_lost_sol else ""), c.mint)
            return None
        fill = res.fill
        cost = fill.net_sol
        cash = self.store.get("cash")
        if cost > cash:
            self.log.log("Executor", "warn", f"{c.symbol}: insufficient paper cash", c.mint)
            return None
        reason = (f"{signal} | rug ok (score {rep.rugcheck_score}, top10 {rep.top10_pct}%, "
                  f"LP {rep.lp_locked_pct}%, rt {rep.roundtrip_loss_pct}%)"
                  + (f" | {critic}" if critic else ""))
        mark_value = cost  # until first mark
        # what the tokens would fetch right after the buy if the price didn't move: the
        # stop-loss is measured from here, so fixed round-trip costs don't trigger it
        friction = 2 * (fill.price_impact_pct + self.r.extra_slippage_bps / 100) / 100
        entry_value = max(0.0, size_sol * (1 - friction) - fill.network_fee_sol + fill.rent_sol)
        pid = self.store.insert("positions", mint=c.mint, symbol=c.symbol, source=c.source, status="open",
                                opened_at=time.time(), size_sol=size_sol, cost_sol=cost,
                                token_amount=fill.fill_out, decimals=rep.decimals or 0, entry_reason=reason,
                                peak_value_sol=mark_value, last_value_sol=mark_value, quote_failures=0,
                                fees_sol=fill.network_fee_sol + fill.slippage_cost_sol,
                                bonding=int(c.is_bonding_curve), entry_value_sol=entry_value)
        self.store.set("cash", cash - cost)
        self.log.trade(pid, c.mint, c.symbol, fill, reason, fill.fill_out)
        self.log.log("Executor", "trade",
                     f"BUY {c.symbol} {size_sol:.3f} SOL, impact {fill.price_impact_pct:.2f}%, "
                     f"fees {fill.network_fee_sol:.5f} + slip {fill.slippage_cost_sol:.5f} SOL via {fill.route or '?'}",
                     c.mint)
        self.log.status("Executor", f"BUY {c.symbol} {size_sol:.3f} SOL")
        return pid

    def _fee_lost(self, sol: float) -> None:
        if sol:
            self.store.set("cash", self.store.get("cash") - sol)
            self.store.set("failed_tx_fees", (self.store.get("failed_tx_fees", 0.0) or 0.0) + sol)

    def sell(self, pos: dict, mark: Fill | None, reason: str) -> bool:
        """Exit with simulated latency. A reverted/unroutable sell keeps the position open."""
        urgent = reason.startswith(("stop-loss", "trailing", "daily loss"))
        res = self.exe.execute_sell(pos["mint"], pos["token_amount"], mark,
                                    slippage_bps=self.r.exit_slippage_bps if urgent else None)
        if res.fill is None:
            self._fee_lost(res.fee_lost_sol)
            if res.fee_lost_sol:  # the failed attempt still cost this position its fee
                self.store.update_position(pos["id"], cost_sol=pos["cost_sol"] + res.fee_lost_sol,
                                           fees_sol=pos["fees_sol"] + res.fee_lost_sol)
            self.log.log("Executor", "warn", f"SELL {pos['symbol']} failed ({reason}): {res.error}; retry next mark",
                         pos["mint"])
            return False
        self.close(pos, res.fill, reason)
        return True

    def close(self, pos: dict, fill: Fill | None, reason: str) -> None:
        partial = pos.get("partial_proceeds_sol") or 0.0
        proceeds = (fill.net_sol if fill else 0.0) + partial
        pnl = proceeds - pos["cost_sol"]
        fees = pos["fees_sol"] + ((fill.network_fee_sol + fill.slippage_cost_sol) if fill else 0)
        self.store.update_position(pos["id"], status="closed", closed_at=time.time(), exit_reason=reason,
                                   proceeds_sol=proceeds, pnl_sol=pnl, pnl_pct=pnl / pos["cost_sol"] * 100,
                                   fees_sol=fees, last_value_sol=proceeds - partial)
        self.store.set("cash", self.store.get("cash") + proceeds - partial)   # partials are already in cash
        if fill:
            self.log.trade(pos["id"], pos["mint"], pos["symbol"], fill, reason, pos["token_amount"], pnl)
        self.log.log("Executor", "trade",
                     f"SELL {pos['symbol']} -> {proceeds:.4f} SOL{' (incl. partials)' if partial else ''}, P&L {pnl:+.4f} SOL "
                     f"({pnl / pos['cost_sol'] * 100:+.1f}%) | {reason}", pos["mint"])
        self.log.status("Executor", f"SELL {pos['symbol']} {pnl:+.4f} SOL")

    def sell_partial(self, pos: dict, share: float, reason: str) -> bool:
        """Sell part of a position. The token account stays open, so no rent comes back."""
        tokens = int(pos["token_amount"] * share)
        if tokens <= 0:
            return False
        res = self.exe.execute_sell(pos["mint"], tokens, None)
        if res.fill is None:
            self._fee_lost(res.fee_lost_sol)
            if res.fee_lost_sol:
                self.store.update_position(pos["id"], cost_sol=pos["cost_sol"] + res.fee_lost_sol,
                                           fees_sol=pos["fees_sol"] + res.fee_lost_sol)
            self.log.log("Executor", "warn", f"partial SELL {pos['symbol']} failed: {res.error}", pos["mint"])
            return False
        fill = dataclasses.replace(res.fill, rent_sol=0.0)
        got = fill.net_sol
        self.store.update_position(pos["id"], token_amount=pos["token_amount"] - tokens,
                                   partial_proceeds_sol=(pos.get("partial_proceeds_sol") or 0) + got,
                                   tp_level=(pos.get("tp_level") or 0) + 1,
                                   fees_sol=pos["fees_sol"] + fill.network_fee_sol + fill.slippage_cost_sol,
                                   last_value_sol=max(0.0, (pos["last_value_sol"] or 0) * (1 - share)))
        self.store.set("cash", self.store.get("cash") + got)
        self.log.trade(pos["id"], pos["mint"], pos["symbol"], fill, reason, tokens, None)
        self.log.log("Executor", "trade", f"PARTIAL SELL {pos['symbol']} {share:.0%} -> {got:.4f} SOL | {reason}",
                     pos["mint"])
        self.log.status("Executor", f"partial SELL {pos['symbol']} +{got:.4f} SOL")
        return True

    def manage_exits(self, marks: dict[int, Fill | None]) -> None:
        now = time.time()
        for pos in self.store.open_positions():
            fill = marks.get(pos["id"])
            if fill is None:
                if pos["quote_failures"] >= self.r.max_consecutive_quote_failures:
                    self.close(pos, None, f"no sell route x{pos['quote_failures']}: written off (likely rug)")
                continue
            # whole position = what's still held + what partial sales already brought in
            whole = fill.net_sol + (pos.get("partial_proceeds_sol") or 0.0)
            why = exit_reason(pos["cost_sol"], whole, pos["peak_value_sol"],
                              (now - pos["opened_at"]) / 60, self.r, runner=(pos.get("tp_level") or 0) > 0,
                              stop_base=pos.get("entry_value_sol"))
            if why:
                self.sell(pos, fill, why)
                continue
            levels = self.r.partial_take_profits or []
            lvl = pos.get("tp_level") or 0
            if lvl < len(levels):
                gain = (whole / pos["cost_sol"] - 1) * 100
                at, share = float(levels[lvl][0]), float(levels[lvl][1])
                if gain >= at and fill.net_sol * share >= self.r.min_partial_sol:
                    self.sell_partial(pos, share, f"partial take-profit {gain:+.1f}% (level +{at:g}%: "
                                                  f"sell {share:.0%} of what's held)")


class EquityAgent:
    def __init__(self, cfg: BotConfig, exe: PaperExecution, store: Store, log: Logger,
                 jupiter_rpm: float | None = None):
        self.r = cfg.risk
        self.mark_rpm = jupiter_rpm * cfg.jupiter_mark_share if jupiter_rpm else None
        self.last_mark = 0.0
        self.exe = exe
        self.store = store
        self.log = log
        if self.store.get("cash") is None:
            self.store.set("cash", self.r.starting_balance_sol)
            self.store.set("starting_balance", self.r.starting_balance_sol)

    def mark_interval_s(self, open_count: int) -> float:
        """How often all open positions can be re-priced within the Jupiter budget share."""
        return open_count * 60.0 / self.mark_rpm if self.mark_rpm else 0.0

    def quick_check(self, discovery) -> bool:
        """Between Jupiter marks, watch DexScreener prices (cheap, 30 tokens per call). If an
        open position looks like it is dropping fast, ask for a Jupiter mark right now, so the
        stop-loss reacts in seconds instead of waiting for the next scheduled mark."""
        prices_fn = getattr(discovery, "prices", None)
        open_pos = self.store.open_positions()
        if not open_pos or prices_fn is None:
            return False
        prices = prices_fn([p["mint"] for p in open_pos])
        for p in open_pos:
            px = prices.get(p["mint"])
            if not px or p["decimals"] is None or not p["last_value_sol"]:
                continue
            est = px * p["token_amount"] / 10 ** p["decimals"]
            whole = est + (p.get("partial_proceeds_sol") or 0.0)
            near_stop = whole <= p["cost_sol"] * (1 - self.r.stop_loss_pct * 0.6 / 100)
            falling = est <= p["last_value_sol"] * 0.95   # -5 % since the last Jupiter mark
            if near_stop or falling:
                return True
        return False

    def mark(self, force: bool = False) -> dict[int, Fill | None]:
        marks: dict[int, Fill | None] = {}
        open_pos = self.store.open_positions()
        now = time.time()
        if not force and now - self.last_mark < self.mark_interval_s(len(open_pos)):
            return marks  # not due yet; exits are evaluated on the next mark
        if force and now - self.last_mark < 3.0:   # forced marks at most every 3 s (Jupiter budget)
            return marks
        self.last_mark = now
        for pos in open_pos:
            try:
                fill = self.exe.simulate_sell(pos["mint"], pos["token_amount"])
            except SourceUnavailable as e:
                # API outage is not a missing route: don't count it towards a write-off
                self.log.log("Equity", "warn", f"{pos['symbol']}: mark skipped, {e}", pos["mint"])
                marks[pos["id"]] = None
                continue
            marks[pos["id"]] = fill
            if fill is None:
                self.store.update_position(pos["id"], quote_failures=pos["quote_failures"] + 1)
                self.log.log("Equity", "warn", f"{pos['symbol']}: no sell quote ({pos['quote_failures'] + 1})",
                             pos["mint"])
            else:
                v = fill.net_sol   # what's still held; the peak tracks the whole position
                whole = v + (pos.get("partial_proceeds_sol") or 0.0)
                self.store.update_position(pos["id"], last_value_sol=v, quote_failures=0,
                                           peak_value_sol=max(pos["peak_value_sol"], whole))
        return marks

    def account(self) -> Account:
        open_pos = self.store.open_positions()
        cash = self.store.get("cash")
        pos_value = sum(p["last_value_sol"] or 0 for p in open_pos)
        return Account(cash=cash, equity=cash + pos_value, open_cost=sum(p["cost_sol"] for p in open_pos),
                       open_count=len(open_pos), halted=bool(self.store.get("halted", False)))

    def check_day(self) -> Account:
        acc = self.account()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self.store.get("day") != today:
            self.store.set("day", today)
            self.store.set("day_start_equity", acc.equity)
            self.store.set("halted", False)
            self.log.log("Equity", "info", f"new UTC day {today}, start equity {acc.equity:.4f} SOL")
            acc.halted = False
        start = self.store.get("day_start_equity")
        if not acc.halted and daily_limit_hit(start, acc.equity, self.r):
            self.store.set("halted", True)
            acc.halted = True
            self.log.log("Equity", "error",
                         f"DAILY LOSS LIMIT: equity {acc.equity:.4f} vs start {start:.4f} SOL "
                         f"(-{self.r.daily_loss_limit_pct}%), new entries paused")
        return acc

    def snapshot(self) -> Account:
        acc = self.account()
        self.store.record_equity(acc.cash, acc.equity - acc.cash)
        st = self.store.stats()
        self.log.status("Equity", f"equity {acc.equity:.4f} SOL, W/L {int(st['wins'])}/{int(st['losses'])}",
                        "halted" if acc.halted else "ok")
        return acc


class Pipeline:
    def __init__(self, cfg: BotConfig, discovery: DiscoveryProvider, checker: RugChecker,
                 exe: PaperExecution, store: Store, echo: bool = True, limits: Limits | None = None,
                 critic: Critic | None = None):
        self.cfg = cfg
        self.limits = limits or Limits()
        self.store = store
        self.exe = exe
        self.log = Logger(store, echo)
        self.memory = critic.memory if critic else MarketMemory(cfg.critic.liquidity_window_minutes * 60)
        self.scanner = Scanner(cfg, discovery, store, self.log, self.memory)
        self.social = Social(cfg, self.log)
        self.rug = RugGuard(checker, store, self.log)
        self.sizer = Sizer(cfg, self.log)
        self.critic = CriticAgent(cfg, critic or Critic(cfg.critic, cfg.rug, self.memory), store, self.log)
        self.executor = Executor(cfg, exe, store, self.log)
        jup = self.limits.get("jupiter")
        self.equity = EquityAgent(cfg, exe, store, self.log, jup.per_minute if jup else None)
        store.set("mode", exe.mode)

    def tick(self) -> Account:
        started = time.time()
        try:
            return self._tick()
        finally:
            took = time.time() - started
            if took > 20:  # say why a cycle was slow instead of looking frozen
                # sources paused for minutes (e.g. pump.fun behind Cloudflare) are skipped, not waited for
                waits = {n: v.get("paused_s") for n, v in self.limits.snapshot().items()
                         if v.get("paused_s") and v.get("paused_s") < 120}
                self.log.log("Logger", "warn", f"cycle took {took:.0f}s"
                             + (f" (APIs pausing: {', '.join(f'{n} {s:.0f}s' for n, s in waits.items())})" if waits
                                else " (slow API responses / timeouts; slow APIs are skipped for 15 s after a timeout)"))

    def _tick(self) -> Account:
        force = self.equity.quick_check(self.scanner.discovery)
        marks = self.equity.mark(force=force)
        self.executor.manage_exits(marks)
        acc = self.equity.check_day()
        if acc.halted and self.cfg.risk.close_all_on_daily_limit:
            for pos in self.store.open_positions():
                self.executor.sell(pos, None, "daily loss limit: close all")
        if acc.halted or acc.open_count >= self.cfg.risk.max_open_positions:
            self.scanner.reset_streaks()
        if not acc.halted and acc.open_count >= self.cfg.risk.max_open_positions:
            self.log.status("Scanner", f"idle: {acc.open_count}/{self.cfg.risk.max_open_positions} positions open")
        elif not acc.halted:
            held = {p["mint"] for p in self.store.open_positions()}
            picked = self.social.run(self.scanner.run(held))
            probe = max(self.cfg.risk.min_position_sol, acc.equity * self.cfg.risk.position_size_pct / 100)
            jup = self.limits.get("jupiter")
            # never ask for more than the bucket can hold, or entries would wait forever
            need = min(self.cfg.min_jupiter_budget_for_entry, jup.capacity) if jup else 0.0
            budget = lambda: self.limits.available("jupiter") >= need
            for cand, signal, rep in self.rug.run(picked, min(probe, self.cfg.risk.max_position_sol), budget):
                if self.critic.waiting(cand):
                    continue
                acc = self.equity.account()
                size = self.sizer.run(cand, acc, self.exe.tx_fee_sol)
                if not size:
                    continue
                verdict = self.critic.review(cand, rep, size)
                if verdict is not None:
                    self.executor.open(cand, rep, size, signal, verdict)
        else:
            self.log.status("Scanner", "paused (daily loss limit)", "halted")
        self.store.set("last_tick", time.time())
        self.store.set("api_stats", self.limits.snapshot())
        return self.equity.snapshot()
