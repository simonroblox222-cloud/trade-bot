import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from paperbot import config as C
from paperbot import sources
from paperbot.agents import Pipeline
from paperbot.dashboard import build_state
from paperbot.execution import LiveTradingDisabled, PaperExecution, make_execution
from paperbot.mock import MockWorld
from paperbot.models import Holder, MintInfo, Quote, TokenCandidate
from paperbot.risk import Account, daily_limit_hit, exit_reason, size_position
from paperbot.rugcheck import RugChecker
from paperbot.store import Store

PKG = Path(__file__).resolve().parents[1] / "paperbot"
MINT = "Tok1111111111111111111111111111111111111111"


class FixedQuotes:
    """1 SOL = 1_000_000 raw tokens, 1% impact, optional missing sell route."""

    def __init__(self, sell_ok=True, sell_factor=1.0):
        self.sell_ok, self.sell_factor = sell_ok, sell_factor

    def quote(self, i, o, amount, slip):
        if i == C.SOL_MINT:
            out = amount // 1000 * 99 // 100
        else:
            if not self.sell_ok:
                return None
            out = int(amount * 1000 * 0.99 * self.sell_factor)
        return Quote(i, o, amount, out, out * (10_000 - slip) // 10_000, 1.0, slip, ["Fake"])


def cand(**kw):
    base = dict(mint=MINT, symbol="TOK", liquidity_usd=100_000, volume_h1_usd=100_000,
                price_change_m5_pct=5, price_change_h1_pct=20, buys_m5=50, sells_m5=20,
                pair_created_at_ms=None)
    base.update(kw)
    return TokenCandidate(**base)


class FakeChain:
    def __init__(self, mint_auth=None, freeze=None, holders=None, exts=None):
        self.mi = MintInfo(6, 1_000_000, mint_auth, freeze, "spl-token", exts or {})
        self.h = holders if holders is not None else [Holder(f"w{i}", f"a{i}", 10_000) for i in range(10)]

    def mint_info(self, m):
        return self.mi

    def largest_holders(self, m):
        return self.h


class FakeRugApi:
    def __init__(self, data):
        self.data = data

    def summary(self, m):
        return self.data


GOOD_API = {"score_normalised": 5, "risks": [], "lpLockedPct": 100}


class LiveGuardTests(unittest.TestCase):
    def test_constant_is_off(self):
        self.assertFalse(C.LIVE_TRADING_ENABLED)

    def test_live_mode_refused(self):
        with self.assertRaises(LiveTradingDisabled):
            make_execution("live", FixedQuotes(), C.RiskConfig())

    def test_config_cannot_enable_live(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('mode = "live"\n')
        try:
            with self.assertRaises(ValueError):
                C.load_config(fh.name)
        finally:
            os.unlink(fh.name)

    def test_no_signing_or_swap_code(self):
        src = "\n".join(p.read_text() for p in PKG.glob("*.py"))
        for pat in [r"Keypair", r"private_?key", r"sendTransaction", r"signTransaction", r"/swap[\"'?]",
                    r"solders", r"solana\.rpc", r"mnemonic", r"secret_key"]:
            self.assertIsNone(re.search(pat, src, re.I), pat)


class FillTests(unittest.TestCase):
    def setUp(self):
        self.r = C.RiskConfig(extra_slippage_bps=100, base_fee_sol=0.000005, priority_fee_sol=0.0001,
                              jito_tip_sol=0.0, ata_rent_sol=0.002)
        self.exe = PaperExecution(FixedQuotes(), self.r)

    def test_buy_costs(self):
        f = self.exe.simulate_buy(MINT, 1_000_000_000)
        self.assertEqual(f.quote_out, 990_000)
        self.assertEqual(f.fill_out, 980_100)          # 1% extra slippage
        self.assertAlmostEqual(f.net_sol, 1 + 0.000105 + 0.002)
        self.assertAlmostEqual(f.slippage_cost_sol, 0.01)

    def test_sell_proceeds(self):
        f = self.exe.simulate_sell(MINT, 1_000_000)
        self.assertEqual(f.quote_out, 990_000_000)
        self.assertAlmostEqual(f.net_sol, 0.9801 - 0.000105 + 0.002)

    def test_fill_below_min_out_fails(self):
        exe = PaperExecution(FixedQuotes(), C.RiskConfig(slippage_bps=50, extra_slippage_bps=100))
        self.assertIsNone(exe.simulate_buy(MINT, 1_000_000_000))

    def test_round_trip_loses_money(self):
        b = self.exe.simulate_buy(MINT, 1_000_000_000)
        s = self.exe.simulate_sell(MINT, b.fill_out)
        self.assertLess(s.net_sol, b.net_sol)


class RiskTests(unittest.TestCase):
    r = C.RiskConfig(position_size_pct=2, max_position_sol=0.5, min_position_sol=0.05,
                     max_open_positions=3, max_total_exposure_pct=30)

    def acc(self, **kw):
        base = dict(cash=10, equity=10, open_cost=0, open_count=0, halted=False)
        base.update(kw)
        return Account(**base)

    def test_size_pct(self):
        self.assertAlmostEqual(size_position(self.acc(), self.r, 0.0001)[0], 0.2)

    def test_size_cap(self):
        self.assertAlmostEqual(size_position(self.acc(cash=100, equity=100), self.r, 0.0001)[0], 0.5)

    def test_exposure_limit(self):
        self.assertAlmostEqual(size_position(self.acc(open_cost=2.9), self.r, 0.0001)[0], 0.1)
        self.assertIsNone(size_position(self.acc(open_cost=2.98), self.r, 0.0001)[0])

    def test_max_positions_and_halt(self):
        self.assertIsNone(size_position(self.acc(open_count=3), self.r, 0.0001)[0])
        self.assertIn("daily loss", size_position(self.acc(halted=True), self.r, 0.0001)[1])

    def test_exits(self):
        r = C.RiskConfig(stop_loss_pct=20, take_profit_pct=50, trailing_arm_pct=20,
                         trailing_stop_pct=15, max_hold_minutes=60)
        self.assertIn("stop-loss", exit_reason(1, 0.79, 1, 1, r))
        self.assertIn("take-profit", exit_reason(1, 1.5, 1.5, 1, r))
        self.assertIn("trailing", exit_reason(1, 1.06, 1.25, 1, r))
        self.assertIsNone(exit_reason(1, 1.06, 1.15, 1, r))        # trailing not armed
        self.assertIn("time exit", exit_reason(1, 1.0, 1.0, 61, r))
        self.assertIsNone(exit_reason(1, 1.0, 1.0, 10, r))

    def test_daily_limit(self):
        r = C.RiskConfig(daily_loss_limit_pct=5)
        self.assertTrue(daily_limit_hit(10, 9.5, r))
        self.assertFalse(daily_limit_hit(10, 9.6, r))


class RugTests(unittest.TestCase):
    def check(self, chain=None, api=GOOD_API, quotes=None, c=None, **cfg):
        rc = RugChecker(C.RugConfig(**cfg), chain or FakeChain(), FakeRugApi(api), quotes or FixedQuotes(), 300)
        return rc.check(c or cand(), 100_000_000)

    def test_pass(self):
        rep = self.check()
        self.assertTrue(rep.passed, rep.reasons)
        self.assertAlmostEqual(rep.roundtrip_loss_pct, 1.99, places=1)

    def test_authorities(self):
        self.assertIn("mint authority not revoked", self.check(FakeChain(mint_auth="X")).reasons)
        self.assertIn("freeze authority not revoked", self.check(FakeChain(freeze="X")).reasons)

    def test_token2022(self):
        rep = self.check(FakeChain(exts={"permanentDelegate": {}}))
        self.assertFalse(rep.passed)
        rep = self.check(FakeChain(exts={"transferFeeConfig": {"newerTransferFee": {"transferFeeBasisPoints": 300}}}))
        self.assertIn("transfer fee 300 bps", rep.reasons)

    def test_holders(self):
        whale = [Holder("whale", "a", 200_000)] + [Holder(f"w{i}", f"a{i}", 5_000) for i in range(9)]
        rep = self.check(FakeChain(holders=whale))
        self.assertTrue(any("largest holder" in r for r in rep.reasons))
        pool = [Holder("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "v", 600_000)] + \
               [Holder(f"w{i}", f"a{i}", 10_000) for i in range(9)]
        self.assertTrue(self.check(FakeChain(holders=pool)).passed)

    def test_bonding_curve_excludes_curve_account(self):
        curve = [Holder("curvePDA", "c", 700_000)] + [Holder(f"w{i}", f"a{i}", 10_000) for i in range(9)]
        self.assertFalse(self.check(FakeChain(holders=curve)).passed)
        rep = self.check(FakeChain(holders=curve), c=cand(is_bonding_curve=True), api={"score_normalised": 5})
        self.assertTrue(rep.passed, rep.reasons)

    def test_lp_and_api(self):
        self.assertTrue(any("LP burned/locked" in r for r in self.check(api={**GOOD_API, "lpLockedPct": 10}).reasons))
        self.assertIn("LP lock status unknown", self.check(api={"score_normalised": 1}).warnings)
        danger = {**GOOD_API, "risks": [{"name": "Freeze Authority still enabled", "level": "danger"}]}
        self.assertFalse(self.check(api=danger).passed)
        self.assertFalse(self.check(api={**GOOD_API, "score_normalised": 80}).passed)
        self.assertIn("RugCheck API unavailable", self.check(api=None).warnings)
        self.assertFalse(self.check(api=None, require_rugcheck_api=True).passed)

    def test_liquidity(self):
        self.assertFalse(self.check(c=cand(liquidity_usd=5_000)).passed)

    def test_sell_simulation(self):
        self.assertIn("no sell route on Jupiter (honeypot?)", self.check(quotes=FixedQuotes(sell_ok=False)).reasons)
        rep = self.check(quotes=FixedQuotes(sell_factor=0.85))  # 15 % hidden sell tax
        self.assertTrue(any("round-trip loss" in r for r in rep.reasons))


class JupiterParseTests(unittest.TestCase):
    def test_parse(self):
        payload = {"inAmount": "100000000", "outAmount": "123456", "otherAmountThreshold": "120000",
                   "priceImpactPct": "0.0123", "slippageBps": 300,
                   "routePlan": [{"swapInfo": {"label": "Raydium"}}, {"swapInfo": {"label": "Meteora DLMM"}}]}
        with mock.patch.object(sources, "get_json", return_value=payload) as g:
            q = sources.JupiterQuotes(C.ApiConfig()).quote(C.SOL_MINT, MINT, 100_000_000, 300)
        self.assertIn("/quote?", g.call_args[0][0])
        self.assertEqual((q.out_amount, q.other_amount_threshold), (123456, 120000))
        self.assertAlmostEqual(q.price_impact_pct, 1.23)
        self.assertEqual(q.route_labels, ["Raydium", "Meteora DLMM"])

    def test_error_returns_none(self):
        with mock.patch.object(sources, "get_json", side_effect=sources.HttpError("x")):
            self.assertIsNone(sources.JupiterQuotes(C.ApiConfig()).quote(C.SOL_MINT, MINT, 1, 300))


class PipelineTests(unittest.TestCase):
    def test_mock_run_accounting(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = C.BotConfig(db_path=os.path.join(d, "t.sqlite"))
            store = Store(cfg.db_path)
            world = MockWorld(seed=3)
            exe = make_execution("paper", world, cfg.risk)
            checker = RugChecker(cfg.rug, world, world, world, cfg.risk.slippage_bps)
            pipe = Pipeline(cfg, world, checker, exe, store, echo=False)
            for _ in range(150):
                world.step()
                acc = pipe.tick()
                self.assertGreaterEqual(acc.cash, 0)
                self.assertLessEqual(acc.open_count, cfg.risk.max_open_positions)
            trades = store.trades(10_000)
            self.assertTrue(trades)
            self.assertTrue(all(t["reason"] for t in trades))
            # cash ledger: start - buys + sells == cash
            buys = sum(p["cost_sol"] for p in store._rows("SELECT cost_sol FROM positions"))
            sells = sum(p["proceeds_sol"] or 0 for p in store.closed_positions(10_000))
            self.assertAlmostEqual(cfg.risk.starting_balance_sol - buys + sells, store.get("cash"), places=9)
            # rejected tokens never traded
            rejected = {r["mint"] for r in store.rugchecks(10_000) if not r["passed"]}
            self.assertFalse(rejected & {t["mint"] for t in trades})
            st = build_state(store)
            self.assertEqual(st["mode"], "paper")
            self.assertFalse(st["live_enabled"])
            self.assertEqual(st["account"]["wins"] + st["account"]["losses"], st["account"]["closed"])


class SourceParseTests(unittest.TestCase):
    def test_discovery_merges_sources_and_picks_deepest_pair(self):
        api = C.ApiConfig()
        pairs = [
            {"baseToken": {"address": "A", "symbol": "AAA"}, "quoteToken": {"symbol": "SOL"}, "dexId": "raydium",
             "liquidity": {"usd": 5000}, "volume": {"h1": 1}, "priceChange": {}, "txns": {}},
            {"baseToken": {"address": "A", "symbol": "AAA"}, "quoteToken": {"symbol": "SOL"}, "dexId": "pumpswap",
             "liquidity": {"usd": 90000}, "volume": {"h1": 50000}, "priceChange": {"m5": 3.5, "h1": 12},
             "txns": {"m5": {"buys": 40, "sells": 10}}, "pairCreatedAt": 1700000000000, "priceUsd": "0.001"},
            {"baseToken": {"address": "B", "symbol": "BBB"}, "quoteToken": {"symbol": "SOL"}, "dexId": "pumpfun",
             "liquidity": {"usd": 30000}, "volume": {"h1": 9000}, "priceChange": {"m5": 1}, "txns": {}},
        ]

        def fake(url, *a, **k):
            if "token-profiles" in url:
                return [{"chainId": "solana", "tokenAddress": "A"}, {"chainId": "base", "tokenAddress": "X"}]
            if "pump.fun" in url:
                return [{"mint": "B"}]
            return pairs

        with mock.patch.object(sources, "get_json", side_effect=fake):
            out = {c.mint: c for c in sources.MarketDiscovery(api).candidates()}
        self.assertEqual(set(out), {"A", "B"})
        self.assertEqual(out["A"].liquidity_usd, 90000)
        self.assertEqual((out["A"].buys_m5, out["A"].source), (40, "dexscreener"))
        self.assertTrue(out["B"].is_bonding_curve)
        self.assertEqual(out["B"].source, "pumpfun")

    def test_source_outage_is_reported_not_fatal(self):
        with mock.patch.object(sources, "get_json", side_effect=sources.HttpError("403")):
            d = sources.MarketDiscovery(C.ApiConfig())
            self.assertEqual(d.candidates(), [])
            self.assertEqual(len(d.last_errors), 2)

    def test_rpc_mint_parse(self):
        res = {"result": {"value": {"data": {"program": "spl-token-2022", "parsed": {"type": "mint", "info": {
            "decimals": 6, "supply": "1000", "mintAuthority": None, "freezeAuthority": "F",
            "extensions": [{"extension": "transferFeeConfig", "state": {}}]}}}}}}
        with mock.patch.object(sources, "post_json", return_value=res):
            mi = sources.SolanaRpc(C.ApiConfig()).mint_info(MINT)
        self.assertEqual((mi.supply, mi.freeze_authority, mi.token_program), (1000, "F", "spl-token-2022"))
        self.assertIn("transferFeeConfig", mi.extensions)


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class RateLimitTests(unittest.TestCase):
    def test_bucket_paces_to_limit(self):
        from paperbot.ratelimit import RateLimiter
        clk = FakeClock()
        lim = RateLimiter("jup", 60, clock=clk, sleep=clk.sleep)   # 1/s, burst 6
        for _ in range(66):
            lim.acquire()
        # 6 burst + 60 more at 1/s => ~60 s elapsed, never faster than the limit
        self.assertAlmostEqual(clk.t - 1000.0, 60.0, delta=0.5)
        self.assertEqual(lim.total, 66)

    def test_non_blocking_and_penalty(self):
        from paperbot.ratelimit import RateLimited, RateLimiter
        clk = FakeClock()
        lim = RateLimiter("x", 6, clock=clk, sleep=clk.sleep)       # capacity 1
        lim.acquire(block=False)
        with self.assertRaises(RateLimited):
            lim.acquire(block=False)
        lim.penalize(30)
        self.assertEqual(lim.available(), 0.0)
        lim.acquire()                                                # waits out the pause
        self.assertGreaterEqual(clk.t - 1000.0, 30.0)
        self.assertEqual(lim.snapshot()["http_429"], 1)

    def test_http_429_pauses_limiter(self):
        import io
        import urllib.error
        from paperbot import http as H
        from paperbot.ratelimit import RateLimiter
        clk = FakeClock()
        lim = RateLimiter("jup", 60, clock=clk, sleep=clk.sleep)
        err = urllib.error.HTTPError("u", 429, "Too Many", {"Retry-After": "12"}, io.BytesIO(b"slow down"))
        with mock.patch.object(H.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(H.HttpError) as cm:
                H.get_json("https://example.invalid/q", limiter=lim)
        self.assertEqual(cm.exception.status, 429)
        self.assertAlmostEqual(lim.snapshot()["paused_s"], 12.0, delta=0.1)

    def test_discovery_reuses_cache_when_rate_limited(self):
        from paperbot.ratelimit import Limits
        limits = Limits({"dexscreener_profiles": 6, "pumpfun": 6})   # 1 call burst each
        calls = []

        def fake(url, *a, **k):
            lim = k.get("limiter")
            if lim is not None:
                lim.acquire(block=k.get("block", True))
            calls.append(url)
            if "token-profiles" in url:
                return [{"chainId": "solana", "tokenAddress": "A"}]
            if "pump.fun" in url:
                return [{"mint": "B"}]
            return [{"baseToken": {"address": m, "symbol": m}, "quoteToken": {"symbol": "SOL"},
                     "liquidity": {"usd": 1}} for m in ("A", "B")]

        d = sources.MarketDiscovery(C.ApiConfig(), limits)
        with mock.patch.object(sources, "get_json", side_effect=fake):
            first = {c.mint for c in d.candidates()}
            second = {c.mint for c in d.candidates()}    # sources rate limited -> cached mints
        self.assertEqual(first, {"A", "B"})
        self.assertEqual(second, {"A", "B"})
        self.assertEqual(sum("token-profiles" in u for u in calls), 1)
        self.assertEqual(d.last_errors, [])


class MarkPacingTests(unittest.TestCase):
    def test_mark_interval_follows_jupiter_share(self):
        from paperbot.agents import EquityAgent
        with tempfile.TemporaryDirectory() as d:
            cfg = C.BotConfig(jupiter_mark_share=0.6)
            store = Store(os.path.join(d, "t.sqlite"))
            eq = EquityAgent(cfg, None, store, mock.Mock(), jupiter_rpm=55)
            self.assertAlmostEqual(eq.mark_interval_s(5), 5 * 60 / 33)
            self.assertEqual(EquityAgent(cfg, None, store, mock.Mock()).mark_interval_s(5), 0.0)


class EnvConfigTests(unittest.TestCase):
    def test_env_overrides_and_expansion(self):
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as fh:
            fh.write('[api]\nsolana_rpc_url = "https://rpc.example/?api-key=${TEST_RPC_KEY}"\n')
        try:
            with mock.patch.dict(os.environ, {"TEST_RPC_KEY": "abc"}, clear=False):
                self.assertEqual(C.load_config(fh.name).api.solana_rpc_url, "https://rpc.example/?api-key=abc")
            with mock.patch.dict(os.environ, {"PAPERBOT_RPC_URL": "https://other/", "JUPITER_API_KEY": "k"}):
                cfg = C.load_config(fh.name)
                self.assertEqual((cfg.api.solana_rpc_url, cfg.api.jupiter_api_key), ("https://other/", "k"))
        finally:
            os.unlink(fh.name)

    def test_defaults_need_no_keys(self):
        cfg = C.BotConfig()
        self.assertEqual(cfg.api.jupiter_api_key, "")
        self.assertNotIn("api-key", cfg.api.solana_rpc_url)


class BudgetTests(unittest.TestCase):
    def test_entries_deferred_without_jupiter_budget(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = C.BotConfig(db_path=os.path.join(d, "t.sqlite"))
            store = Store(cfg.db_path)
            world = MockWorld(seed=3)
            exe = make_execution("paper", world, cfg.risk)
            checker = RugChecker(cfg.rug, world, world, world, cfg.risk.slippage_bps)
            starved = mock.Mock()
            starved.available.return_value = 0.0
            starved.snapshot.return_value = {}
            starved.get.return_value = None
            pipe = Pipeline(cfg, world, checker, exe, store, echo=False, limits=starved)
            for _ in range(20):
                world.step()
                pipe.tick()
            self.assertEqual(store.trades(), [])
            self.assertEqual(store.rugchecks(), [])
            self.assertIn("deferred", store.get("agent:RugCheck")["summary"])


if __name__ == "__main__":
    unittest.main()
