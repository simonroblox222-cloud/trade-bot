"""Agent pipeline: Scanner -> RugCheck -> Sizer -> Executor -> Logger -> Equity.

Each agent is a small class with one job. `Pipeline.tick()` runs one cycle:
  1. Equity   marks open positions to market with real sell quotes
  2. Executor closes positions whose exit rule fired (SL / TP / trailing / time / no route)
  3. Equity   rolls the UTC day and checks the daily loss limit
  4. Scanner  finds candidates that pass the entry signal
  5. RugCheck screens them (authorities, holders, LP, RugCheck API, sell simulation)
  6. Sizer    decides position size under the risk limits
  7. Executor opens simulated positions from fresh Jupiter quotes
  8. Equity   writes an equity snapshot
Logger is used by every agent; everything ends up in SQLite.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

from .config import LAMPORTS_PER_SOL, BotConfig
from .execution import Fill, PaperExecution
from .models import RugReport, TokenCandidate
from .risk import Account, daily_limit_hit, exit_reason, size_position
from .ratelimit import Limits
from .rugcheck import RugChecker
from .sources import DiscoveryProvider
from .store import Store

AGENTS = ["Scanner", "RugCheck", "Sizer", "Executor", "Logger", "Equity"]


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
    def __init__(self, cfg: BotConfig, discovery: DiscoveryProvider, store: Store, log: Logger):
        self.s = cfg.strategy
        self.cooldown_s = cfg.risk.token_cooldown_minutes * 60
        self.discovery = discovery
        self.store = store
        self.log = log
        self._logged: dict[str, float] = {}

    def signal(self, c: TokenCandidate, now: float) -> tuple[bool, str]:
        s = self.s
        age_min = (now - c.pair_created_at_ms / 1000) / 60 if c.pair_created_at_ms else None
        ratio = c.buys_m5 / max(c.sells_m5, 1)
        checks = [
            (c.liquidity_usd >= s.min_liquidity_usd, f"liq ${c.liquidity_usd:,.0f}"),
            (c.volume_h1_usd >= s.min_volume_h1_usd, f"vol1h ${c.volume_h1_usd:,.0f}"),
            (age_min is not None and s.min_pair_age_minutes <= age_min <= s.max_pair_age_hours * 60,
             f"age {age_min:.0f}m" if age_min is not None else "age ?"),
            (c.price_change_m5_pct >= s.min_price_change_m5_pct, f"m5 {c.price_change_m5_pct:+.1f}%"),
            (s.min_price_change_h1_pct <= c.price_change_h1_pct <= s.max_price_change_h1_pct,
             f"h1 {c.price_change_h1_pct:+.1f}%"),
            (ratio >= s.min_buy_sell_ratio_m5, f"buys/sells {ratio:.2f}"),
            (s.allow_bonding_curve or not c.is_bonding_curve, "bonding curve" if c.is_bonding_curve else "amm"),
        ]
        failed = [txt for ok, txt in checks if not ok]
        return (not failed), ("momentum: " + ", ".join(t for _, t in checks)) if not failed else ", ".join(failed)

    def run(self, held: set[str]) -> list[tuple[TokenCandidate, str]]:
        now = time.time()
        cands = self.discovery.candidates()
        for err in getattr(self.discovery, "last_errors", []) or []:
            self.log.log("Scanner", "warn", f"source error: {err}")
        blacklist = set(self.store.get("blacklist", []))
        picked = []
        for c in cands:
            if c.mint in held or c.mint in blacklist:
                continue
            last = self.store.last_exit_ts(c.mint)
            if last and now - last < self.cooldown_s:
                continue
            ok, why = self.signal(c, now)
            if ok:
                picked.append((c, why))
        picked.sort(key=lambda p: p[0].price_change_m5_pct * min(p[0].buys_m5 / max(p[0].sells_m5, 1), 3),
                    reverse=True)
        picked = picked[: self.s.max_candidates_per_tick]
        self.log.status("Scanner", f"{len(cands)} pairs seen, {len(picked)} passed signal")
        for c, why in picked:
            if now - self._logged.get(c.mint, 0) > 300:  # don't repeat the same pick every cycle
                self._logged[c.mint] = now
                self.log.log("Scanner", "info", f"{c.symbol} [{c.source}] {why}", c.mint)
        return picked


class RugGuard:
    name = "RugCheck"

    def __init__(self, checker: RugChecker, store: Store, log: Logger):
        self.checker = checker
        self.store = store
        self.log = log

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
                        + (f", {deferred} deferred (API budget)" if deferred else ""))
        return passed


class Sizer:
    def __init__(self, cfg: BotConfig, log: Logger):
        self.r = cfg.risk
        self.log = log

    def run(self, cand: TokenCandidate, acc: Account, tx_fee_sol: float) -> float | None:
        size, why = size_position(acc, self.r, tx_fee_sol)
        self.log.log("Sizer", "info" if size else "warn", f"{cand.symbol}: {why}" if size else f"{cand.symbol} skip: {why}",
                     cand.mint)
        self.log.status("Sizer", why if size else f"skip: {why}")
        return size


class Executor:
    def __init__(self, cfg: BotConfig, exe: PaperExecution, store: Store, log: Logger):
        self.r = cfg.risk
        self.exe = exe
        self.store = store
        self.log = log

    def open(self, c: TokenCandidate, rep: RugReport, size_sol: float, signal: str) -> int | None:
        fill = self.exe.simulate_buy(c.mint, int(size_sol * LAMPORTS_PER_SOL))
        if fill is None:
            self.log.log("Executor", "warn", f"{c.symbol}: no buy route / fill below minOut", c.mint)
            return None
        if fill.price_impact_pct > self.r.max_price_impact_pct:
            self.log.log("Executor", "warn",
                         f"{c.symbol}: price impact {fill.price_impact_pct:.2f}% > {self.r.max_price_impact_pct}%", c.mint)
            return None
        cost = fill.net_sol
        cash = self.store.get("cash")
        if cost > cash:
            self.log.log("Executor", "warn", f"{c.symbol}: insufficient paper cash", c.mint)
            return None
        reason = (f"{signal} | rug ok (score {rep.rugcheck_score}, top10 {rep.top10_pct}%, "
                  f"LP {rep.lp_locked_pct}%, rt {rep.roundtrip_loss_pct}%)")
        mark_value = cost  # until first mark
        pid = self.store.insert("positions", mint=c.mint, symbol=c.symbol, source=c.source, status="open",
                                opened_at=time.time(), size_sol=size_sol, cost_sol=cost,
                                token_amount=fill.fill_out, decimals=rep.decimals or 0, entry_reason=reason,
                                peak_value_sol=mark_value, last_value_sol=mark_value, quote_failures=0,
                                fees_sol=fill.network_fee_sol + fill.slippage_cost_sol)
        self.store.set("cash", cash - cost)
        self.log.trade(pid, c.mint, c.symbol, fill, reason, fill.fill_out)
        self.log.log("Executor", "trade",
                     f"BUY {c.symbol} {size_sol:.3f} SOL, impact {fill.price_impact_pct:.2f}%, "
                     f"fees {fill.network_fee_sol:.5f} + slip {fill.slippage_cost_sol:.5f} SOL via {fill.route or '?'}",
                     c.mint)
        self.log.status("Executor", f"BUY {c.symbol} {size_sol:.3f} SOL")
        return pid

    def close(self, pos: dict, fill: Fill | None, reason: str) -> None:
        proceeds = fill.net_sol if fill else 0.0
        pnl = proceeds - pos["cost_sol"]
        fees = pos["fees_sol"] + ((fill.network_fee_sol + fill.slippage_cost_sol) if fill else 0)
        self.store.update_position(pos["id"], status="closed", closed_at=time.time(), exit_reason=reason,
                                   proceeds_sol=proceeds, pnl_sol=pnl, pnl_pct=pnl / pos["cost_sol"] * 100,
                                   fees_sol=fees, last_value_sol=proceeds)
        self.store.set("cash", self.store.get("cash") + proceeds)
        if fill:
            self.log.trade(pos["id"], pos["mint"], pos["symbol"], fill, reason, pos["token_amount"], pnl)
        self.log.log("Executor", "trade",
                     f"SELL {pos['symbol']} -> {proceeds:.4f} SOL, P&L {pnl:+.4f} SOL "
                     f"({pnl / pos['cost_sol'] * 100:+.1f}%) | {reason}", pos["mint"])
        self.log.status("Executor", f"SELL {pos['symbol']} {pnl:+.4f} SOL")

    def manage_exits(self, marks: dict[int, Fill | None]) -> None:
        now = time.time()
        for pos in self.store.open_positions():
            fill = marks.get(pos["id"])
            if fill is None:
                if pos["quote_failures"] >= self.r.max_consecutive_quote_failures:
                    self.close(pos, None, f"no sell route x{pos['quote_failures']}: written off (likely rug)")
                continue
            why = exit_reason(pos["cost_sol"], fill.net_sol, pos["peak_value_sol"],
                              (now - pos["opened_at"]) / 60, self.r)
            if why:
                self.close(pos, fill, why)


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

    def mark(self) -> dict[int, Fill | None]:
        marks: dict[int, Fill | None] = {}
        open_pos = self.store.open_positions()
        now = time.time()
        if now - self.last_mark < self.mark_interval_s(len(open_pos)):
            return marks  # not due yet; exits are evaluated on the next mark
        self.last_mark = now
        for pos in open_pos:
            fill = self.exe.simulate_sell(pos["mint"], pos["token_amount"])
            marks[pos["id"]] = fill
            if fill is None:
                self.store.update_position(pos["id"], quote_failures=pos["quote_failures"] + 1)
                self.log.log("Equity", "warn", f"{pos['symbol']}: no sell quote ({pos['quote_failures'] + 1})",
                             pos["mint"])
            else:
                v = fill.net_sol
                self.store.update_position(pos["id"], last_value_sol=v, quote_failures=0,
                                           peak_value_sol=max(pos["peak_value_sol"], v))
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
                 exe: PaperExecution, store: Store, echo: bool = True, limits: Limits | None = None):
        self.cfg = cfg
        self.limits = limits or Limits()
        self.store = store
        self.exe = exe
        self.log = Logger(store, echo)
        self.scanner = Scanner(cfg, discovery, store, self.log)
        self.rug = RugGuard(checker, store, self.log)
        self.sizer = Sizer(cfg, self.log)
        self.executor = Executor(cfg, exe, store, self.log)
        jup = self.limits.get("jupiter")
        self.equity = EquityAgent(cfg, exe, store, self.log, jup.per_minute if jup else None)
        store.set("mode", exe.mode)

    def tick(self) -> Account:
        marks = self.equity.mark()
        self.executor.manage_exits(marks)
        acc = self.equity.check_day()
        if acc.halted and self.cfg.risk.close_all_on_daily_limit:
            for pos in self.store.open_positions():
                self.executor.close(pos, self.exe.simulate_sell(pos["mint"], pos["token_amount"]),
                                    "daily loss limit: close all")
        if not acc.halted and acc.open_count >= self.cfg.risk.max_open_positions:
            self.log.status("Scanner", f"idle: {acc.open_count}/{self.cfg.risk.max_open_positions} positions open")
        elif not acc.halted:
            held = {p["mint"] for p in self.store.open_positions()}
            picked = self.scanner.run(held)
            probe = max(self.cfg.risk.min_position_sol, acc.equity * self.cfg.risk.position_size_pct / 100)
            budget = lambda: self.limits.available("jupiter") >= self.cfg.min_jupiter_budget_for_entry
            for cand, signal, rep in self.rug.run(picked, min(probe, self.cfg.risk.max_position_sol), budget):
                acc = self.equity.account()
                size = self.sizer.run(cand, acc, self.exe.tx_fee_sol)
                if size:
                    self.executor.open(cand, rep, size, signal)
        else:
            self.log.status("Scanner", "paused (daily loss limit)", "halted")
        self.store.set("last_tick", time.time())
        self.store.set("api_stats", self.limits.snapshot())
        return self.equity.snapshot()
