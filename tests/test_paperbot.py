import os
import re
import time
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


def b58encode(b: bytes) -> str:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n, out = int.from_bytes(b, "big"), ""
    while n:
        n, r = divmod(n, 58)
        out = alphabet[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\0"))) + out


# real ed25519 public keys (generated with `cryptography`) and two known PDAs
WALLET_A = b58encode(bytes.fromhex("b9b67c38ccadf8c162a490ea2b8589cbfd09e221ecab774086f09167189115ac"))
WALLET_B = b58encode(bytes.fromhex("8bed07e04764e32b6c7e426d2eb524e027471756ae76a463940d9f9e53f6be0e"))
PDA_RAYDIUM = "5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1"
PDA_CPMM = "GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL"

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


class SteppedQuotes(FixedQuotes):
    """Every call returns a price `step` worse than the previous one (market moving against us)."""

    def __init__(self, step):
        super().__init__()
        self.step, self.n = step, 0

    def quote(self, i, o, amount, slip):
        q = super().quote(i, o, amount, slip)
        f = (1 - self.step) ** self.n
        self.n += 1
        out = int(q.out_amount * f)
        # minOut stays relative to *this* quote, like a real one
        return Quote(i, o, amount, out, out * (10_000 - slip) // 10_000, q.price_impact_pct, slip, ["Fake"])


class LatencyTests(unittest.TestCase):
    def exe(self, step, latency=2.0, slip=300):
        slept = []
        r = C.RiskConfig(latency_seconds=latency, slippage_bps=slip, extra_slippage_bps=100)
        return PaperExecution(SteppedQuotes(step), r, sleep=slept.append), slept

    def test_fill_at_worse_later_price(self):
        exe, slept = self.exe(0.01)
        res = exe.execute_buy(MINT, 1_000_000_000)
        self.assertEqual(slept, [2.0])
        self.assertEqual(res.fill.fill_out, int(990_000 * 0.99) * 9900 // 10_000)

    def test_revert_when_price_moves_past_slippage(self):
        exe, _ = self.exe(0.05)                       # 5 % move > 3 % slippage
        res = exe.execute_buy(MINT, 1_000_000_000)
        self.assertIsNone(res.fill)
        self.assertIn("reverted", res.error)
        self.assertAlmostEqual(res.fee_lost_sol, 0.000105)

    def test_sell_uses_mark_as_first_quote(self):
        exe, slept = self.exe(0.01)
        mark = exe.simulate_sell(MINT, 1_000_000)
        res = exe.execute_sell(MINT, 1_000_000, mark)
        self.assertLess(res.fill.fill_out, mark.fill_out)
        self.assertEqual(slept, [2.0])

    def test_no_latency_no_sleep(self):
        exe, slept = self.exe(0.05, latency=0)
        self.assertIsNotNone(exe.execute_buy(MINT, 1_000_000_000).fill)
        self.assertEqual(slept, [])

    def test_impact_checked_before_sending(self):
        exe, slept = self.exe(0.0)
        res = exe.execute_buy(MINT, 1_000_000_000, max_impact_pct=0.5)   # FixedQuotes impact = 1 %
        self.assertIsNone(res.fill)
        self.assertEqual((slept, res.fee_lost_sol), ([], 0.0))


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
        whale = [Holder(WALLET_A, "a", 200_000)] + [Holder(f"w{i}", f"a{i}", 5_000) for i in range(9)]
        rep = self.check(FakeChain(holders=whale))
        self.assertTrue(any("largest wallet" in r for r in rep.reasons))

    def test_program_owned_accounts_excluded(self):
        # PDAs (pool authority, bonding curve) are off-curve: left out even with no allowlist
        pdas = [Holder(PDA_RAYDIUM, "v", 600_000), Holder(PDA_CPMM, "c", 300_000)]
        rest = [Holder(f"w{i}", f"a{i}", 10_000) for i in range(9)]
        self.assertTrue(self.check(FakeChain(holders=pdas + rest), pool_owner_allowlist=[]).passed)
        # a real ed25519 wallet with the same balance is counted
        self.assertFalse(self.check(FakeChain(holders=[Holder(WALLET_B, "x", 600_000)] + rest),
                                    pool_owner_allowlist=[]).passed)

    def test_accounts_of_one_wallet_add_up(self):
        split = [Holder(WALLET_A, "a1", 60_000), Holder(WALLET_A, "a2", 60_000)] + \
                [Holder(f"w{i}", f"a{i}", 5_000) for i in range(8)]
        rep = self.check(FakeChain(holders=split))
        self.assertTrue(any("largest wallet 12.0%" in r for r in rep.reasons), rep.reasons)

    def test_bonding_curve_flag(self):
        curve = [Holder(PDA_CPMM, "c", 700_000)] + [Holder(f"w{i}", f"a{i}", 10_000) for i in range(9)]
        rep = self.check(FakeChain(holders=curve), c=cand(is_bonding_curve=True), api={"score_normalised": 5})
        self.assertTrue(rep.passed, rep.reasons)
        rep = self.check(FakeChain(holders=curve), c=cand(is_bonding_curve=True), allow_bonding_curve=False)
        self.assertIn("pump.fun bonding curve (not graduated)", rep.reasons)

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


class SolanaKeyTests(unittest.TestCase):
    def test_on_curve(self):
        from paperbot.solana_keys import b58decode, is_on_curve, is_wallet
        self.assertEqual(b58encode(b58decode(WALLET_A)), WALLET_A)
        self.assertTrue(is_wallet(WALLET_A) and is_wallet(WALLET_B))
        self.assertFalse(is_wallet(PDA_RAYDIUM) or is_wallet(PDA_CPMM))
        self.assertTrue(is_on_curve(b58decode("TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA")))
        self.assertTrue(is_wallet(None) and is_wallet("not-base58!") and is_wallet("abc"))


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

    def test_no_route_vs_outage(self):
        jq = sources.JupiterQuotes(C.ApiConfig())
        with mock.patch.object(sources, "get_json", side_effect=sources.HttpError("no route", 400)):
            self.assertIsNone(jq.quote(C.SOL_MINT, MINT, 1, 300))
        for status in (None, 429, 503):
            with mock.patch.object(sources, "get_json", side_effect=sources.HttpError("down", status)):
                with self.assertRaises(sources.SourceUnavailable):
                    jq.quote(C.SOL_MINT, MINT, 1, 300)


class OutageTests(unittest.TestCase):
    def test_outage_does_not_write_off_or_blacklist(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = C.BotConfig(db_path=os.path.join(d, "t.sqlite"))
            store = Store(cfg.db_path)
            world = MockWorld(seed=3)
            exe = make_execution("paper", world, cfg.risk, sleep=lambda _s: None)
            checker = RugChecker(cfg.rug, world, world, world, cfg.risk.slippage_bps)
            pipe = Pipeline(cfg, world, checker, exe, store, echo=False)
            while not store.open_positions():
                world.step()
                pipe.tick()
            n_checks = len(store.rugchecks(10_000))
            with mock.patch.object(world, "quote", side_effect=sources.SourceUnavailable("down")):
                for _ in range(6):
                    world.step()
                    pipe.tick()
            self.assertTrue(store.open_positions())                    # nothing written off
            self.assertTrue(all(p["quote_failures"] == 0 for p in store.open_positions()))
            # tokens that needed a Jupiter quote were postponed, never rejected as "no route"
            during = store.rugchecks(10_000)[: len(store.rugchecks(10_000)) - n_checks]
            self.assertFalse(any("route" in r for rc in during for r in rc["reasons"]))
            self.assertFalse(any(rc["passed"] for rc in during))


class PipelineTests(unittest.TestCase):
    def test_mock_run_accounting(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = C.BotConfig(db_path=os.path.join(d, "t.sqlite"))
            store = Store(cfg.db_path)
            world = MockWorld(seed=3)
            exe = make_execution("paper", world, cfg.risk, sleep=lambda _s: None)
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
            # reverted sells are already in cost_sol; reverted buys only in failed_tx_fees
            failed_buys = (store.get("failed_tx_fees", 0.0) or 0.0) - sum(
                p["cost_sol"] - p["size_sol"] - cfg.risk.ata_rent_sol - (cfg.risk.base_fee_sol + cfg.risk.priority_fee_sol)
                for p in store._rows("SELECT cost_sol, size_sol FROM positions"))
            self.assertAlmostEqual(cfg.risk.starting_balance_sol - buys + sells - failed_buys, store.get("cash"), places=9)
            # rejected tokens never traded
            rejected = {r["mint"] for r in store.rugchecks(10_000) if not r["passed"]}
            self.assertFalse(rejected & {t["mint"] for t in trades})
            st = build_state(store)
            self.assertEqual(st["mode"], "paper")
            self.assertFalse(st["live_enabled"])
            self.assertEqual(st["account"]["wins"] + st["account"]["losses"], st["account"]["closed"])


class ConfirmTests(unittest.TestCase):
    def test_signal_needs_consecutive_scans(self):
        from paperbot.agents import Scanner
        good = cand(pair_created_at_ms=int((time.time() - 3600) * 1000))
        feed = [[good], [good], [], [good], [good], [good]]
        disc = mock.Mock()
        disc.candidates.side_effect = feed
        disc.last_errors = []
        with tempfile.TemporaryDirectory() as d:
            store = Store(os.path.join(d, "t.sqlite"))
            sc = Scanner(C.BotConfig(), disc, store, mock.Mock())
            got = [len(sc.run(set())) for _ in feed]
        self.assertEqual(got, [0, 0, 0, 0, 0, 1])   # streak broken by the empty scan

    def test_edge_check(self):
        from paperbot.edge import edge_check
        self.assertIn("málo", edge_check([5] * 10, [0.01] * 10)["verdict"])
        lose = edge_check([-5 + i % 3 for i in range(40)], [-0.01] * 40)
        self.assertLess(lose["ci90_high"], 0)
        self.assertIn("stráca", lose["verdict"])
        lucky = edge_check([300, 250, 200] + [-4] * 37, [0.6, 0.5, 0.4] + [-0.008] * 37)
        self.assertLess(lucky["pnl_without_best3_sol"], 0)
        self.assertNotIn("vyzerá kladne", lucky["verdict"])


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
            self.assertEqual(len(d.last_errors), 3)  # dexscreener, pumpfun, boosts

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


class LowLimitTests(unittest.TestCase):
    def test_entries_possible_with_small_jupiter_bucket(self):
        from paperbot.ratelimit import Limits
        with tempfile.TemporaryDirectory() as d:
            cfg = C.BotConfig(db_path=os.path.join(d, "t.sqlite"), min_jupiter_budget_for_entry=4)
            store = Store(cfg.db_path)
            world = MockWorld(seed=3)
            exe = make_execution("paper", world, cfg.risk, sleep=lambda _s: None)
            checker = RugChecker(cfg.rug, world, world, world, cfg.risk.slippage_bps)
            limits = Limits({"jupiter": 30})          # bucket capacity 3 < 4
            pipe = Pipeline(cfg, world, checker, exe, store, echo=False, limits=limits)
            for _ in range(6):
                world.step()
                pipe.tick()
            self.assertTrue(store.rugchecks())


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
            exe = make_execution("paper", world, cfg.risk, sleep=lambda _s: None)
            checker = RugChecker(cfg.rug, world, world, world, cfg.risk.slippage_bps)
            starved = mock.Mock()
            starved.available.return_value = 0.0
            starved.snapshot.return_value = {}
            starved.get.return_value = mock.Mock(capacity=5.0, per_minute=55.0)
            pipe = Pipeline(cfg, world, checker, exe, store, echo=False, limits=starved)
            for _ in range(20):
                world.step()
                pipe.tick()
            self.assertEqual(store.trades(), [])
            self.assertEqual(store.rugchecks(), [])
            self.assertIn("deferred", store.get("agent:RugCheck")["summary"])


# ------------------------------------------------------------ social + critic
from paperbot.critic import Critic, MarketMemory, social_score  # noqa: E402
from paperbot.models import RugReport  # noqa: E402


class SocialTests(unittest.TestCase):
    def test_score_counts_links_comments_koth_and_penalises_boosts(self):
        cfg = C.SocialConfig()
        s, parts = social_score(cand(has_x=True, has_telegram=True, pf_replies=150, pf_koth=True), cfg)
        self.assertEqual(s, 1 + 0.5 + 2 + 1)
        self.assertIn("KOTH", parts)
        s2, parts2 = social_score(cand(has_x=True, boosts_active=10), cfg)
        self.assertEqual(s2, 0.0)
        self.assertTrue(any("boosted" in p for p in parts2))
        self.assertEqual(social_score(cand(), cfg)[0], 0.0)

    def test_dexscreener_socials_and_pumpfun_meta_parsed(self):
        pair = {"chainId": "solana", "baseToken": {"address": MINT, "symbol": "TOK"},
                "quoteToken": {"address": C.SOL_MINT, "symbol": "SOL"}, "liquidity": {"usd": 50_000}, "dexId": "raydium",
                "priceUsd": "0.0015", "priceNative": "0.00001", "boosts": {"active": 2},
                "info": {"socials": [{"type": "twitter", "url": "x"}, {"type": "telegram", "url": "t"}],
                         "websites": [{"url": "w"}]}}
        meta = {MINT: {"replies": 42, "koth": True, "x": False, "tg": False, "web": False, "creator": "DEV"}}
        c = sources.MarketDiscovery._best_pairs([pair], {MINT}, {MINT: "pumpfun"}, meta)[0]
        self.assertEqual((c.has_x, c.has_telegram, c.has_website, c.boosts_active), (True, True, True, 2))
        self.assertEqual((c.pf_replies, c.pf_koth, c.creator, c.price_native), (42, True, "DEV", 0.00001))

    def test_social_agent_filters_quiet_tokens(self):
        from paperbot.agents import Logger, Social
        with tempfile.TemporaryDirectory() as d:
            sa = Social(C.BotConfig(), Logger(Store(os.path.join(d, "t.sqlite")), echo=False))
            loud, quiet = cand(has_x=True), cand(mint="Q" * 43)
            out = sa.run([(loud, "sig"), (quiet, "sig")])
        self.assertEqual([c.mint for c, _ in out], [MINT])
        self.assertIn("social +1.0", out[0][1])


class FakeReport:
    def __init__(self, rep):
        self.rep = rep

    def report(self, m):
        return self.rep


class FakePump:
    def __init__(self, coins):
        self.coins = coins

    def creator_coins(self, c):
        return self.coins


class FakeWallets(FakeChain):
    def __init__(self, tx_counts):
        super().__init__(holders=[Holder(f"w{i}", f"a{i}", 20_000) for i in range(8)])
        self.tx = tx_counts

    def wallet_tx_count(self, owner, limit=20):
        return self.tx.get(owner, 100)


def rugrep(**kw):
    base = dict(mint=MINT, passed=True, supply=1_000_000, roundtrip_loss_pct=2.0)
    base.update(kw)
    return RugReport(**base)


class CriticTests(unittest.TestCase):
    def critic(self, **kw):
        cfg = C.BotConfig()
        return Critic(cfg.critic, cfg.rug, kw.pop("memory", MarketMemory()), **kw)

    def test_clean_trade_approved_missing_data_only_notes(self):
        v = self.critic().review(cand(), rugrep(), 0.2, [], [])
        self.assertTrue(v.approved, v.reasons)
        self.assertTrue(any("SOL price" in n for n in v.notes))
        v = self.critic(rug_api=FakeReport(None), pumpfun=FakePump(None)).review(
            cand(creator="DEV"), rugrep(), 0.2, [], [])
        self.assertTrue(v.approved)
        self.assertIn("RugCheck report unavailable", v.notes)

    def test_insiders_bundles_and_dev_balance_rejected(self):
        rep = {"rugged": False, "creatorBalance": 90_000, "token": {"supply": 1_000_000}, "totalHolders": 50,
               "topHolders": [{"pct": 6, "insider": True}, {"pct": 6, "insider": True}],
               "insiderNetworks": [{"size": 12}]}
        v = self.critic(rug_api=FakeReport(rep)).review(cand(), rugrep(), 0.2, [], [])
        self.assertFalse(v.approved)
        self.assertTrue(v.hard)
        txt = "; ".join(v.reasons)
        for part in ("insiders hold 12.0%", "insider network", "dev still holds 9.0%", "only 50 holders"):
            self.assertIn(part, txt)
        self.assertFalse(self.critic(rug_api=FakeReport({"rugged": True})).review(cand(), rugrep(), 0.2, [], []).approved)

    def test_dev_history(self):
        serial = [{"mint": f"m{i}", "usd_market_cap": 99_999, "complete": False} for i in range(20)]
        dead = [{"mint": f"m{i}", "usd_market_cap": 500, "complete": False} for i in range(6)]
        ok = [{"mint": "m0", "complete": True, "usd_market_cap": 1e6}]
        r = lambda coins: self.critic(pumpfun=FakePump(coins)).review(cand(creator="DEV"), rugrep(), 0.2, [], [])
        self.assertIn("serial launcher", "; ".join(r(serial).reasons))
        self.assertIn("6/6 earlier coins died", "; ".join(r(dead).reasons))
        self.assertTrue(r(ok).approved)

    def test_fresh_wallets_among_top_holders(self):
        fresh = FakeWallets({"w0": 1, "w1": 0, "w2": 3})
        v = self.critic(onchain=fresh).review(cand(), rugrep(), 0.2, [], [])
        self.assertFalse(v.approved)
        self.assertIn("3/8 top holders are fresh", v.reasons[0])
        self.assertTrue(self.critic(onchain=FakeWallets({"w0": 1})).review(cand(), rugrep(), 0.2, [], []).approved)

    def test_liquidity_pull_between_scans(self):
        clock = FakeClock()
        mem = MarketMemory(900, clock=clock)
        mem.observe([cand(liquidity_usd=100_000)])
        clock.t += 60
        mem.observe([cand(liquidity_usd=60_000)])
        v = self.critic(memory=mem).review(cand(liquidity_usd=60_000), rugrep(), 0.2, [], [])
        self.assertIn("liquidity left the pool: -40%", v.reasons[0])

    def test_fast_and_paid_hype(self):
        now_ms = int(time.time() * 1000)
        bots = cand(pf_replies=3000, pair_created_at_ms=now_ms - 60 * 60_000)
        self.assertIn("hype too fast", self.critic().review(bots, rugrep(), 0.2, [], []).reasons[0])
        wash = cand(volume_h1_usd=3_000_000, liquidity_usd=100_000)
        self.assertIn("wash trading", self.critic().review(wash, rugrep(), 0.2, [], []).reasons[0])
        paid = cand(boosts_active=10, pf_replies=2)
        self.assertIn("paid boost", self.critic().review(paid, rugrep(), 0.2, [], []).reasons[0])
        self.assertTrue(self.critic().review(cand(boosts_active=10, has_x=True), rugrep(), 0.2, [], []).approved)

    def test_copycat_symbol(self):
        mem = MarketMemory()
        mem.observe([cand(mint="ORIG" + "1" * 39, symbol="tok", liquidity_usd=500_000)])
        v = self.critic(memory=mem).review(cand(), rugrep(), 0.2, [], [])
        self.assertIn("copycat", v.reasons[0])
        held = [{"mint": "OTHER", "symbol": "TOK", "source": "x"}]
        self.assertIn("copycat", self.critic().review(cand(), rugrep(), 0.2, held, []).reasons[0])

    def test_discipline_rules_are_soft(self):
        now = time.time()
        losses = [{"pnl_sol": -0.01, "closed_at": now - 60}] * 3
        v = self.critic().review(cand(), rugrep(), 0.2, [], losses)
        self.assertIn("3 losses in a row", v.reasons[0])
        self.assertFalse(v.hard)
        self.assertTrue(self.critic().review(cand(), rugrep(), 0.2, [], [{"pnl_sol": -0.01, "closed_at": now - 7200}] * 3).approved)
        self.assertIn("chasing", self.critic().review(cand(price_change_m5_pct=45), rugrep(), 0.2, [], []).reasons[0])
        bc = [{"mint": f"b{i}", "symbol": f"B{i}", "source": "pumpfun", "bonding": 1} for i in range(2)]
        self.assertIn("bonding-curve", self.critic().review(cand(is_bonding_curve=True, source="dexscreener"),
                                                            rugrep(), 0.2, bc, []).reasons[0])
        big = cand(price_usd=150.0, price_native=1.0, liquidity_usd=10_000)  # 1 SOL = $150 = 1.5% of pool
        self.assertTrue(self.critic().review(big, rugrep(), 1.0, [], []).approved)
        self.assertIn("of pool liquidity", self.critic().review(big, rugrep(), 2.0, [], []).reasons[0])
        self.assertIn("of its limit", self.critic().review(cand(), rugrep(roundtrip_loss_pct=6.0), 0.2, [], []).reasons[0])

    def test_rejects_stop_trade_before_executor_and_are_stored(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = C.BotConfig(db_path=os.path.join(d, "t.sqlite"))
            store = Store(cfg.db_path)
            world = MockWorld(seed=5)
            exe = make_execution("paper", world, cfg.risk, sleep=lambda _s: None)
            checker = RugChecker(cfg.rug, world, world, world, cfg.risk.slippage_bps)
            mem = MarketMemory()
            crit = Critic(cfg.critic, cfg.rug, mem, rug_api=world, pumpfun=world, onchain=world)
            pipe = Pipeline(cfg, world, checker, exe, store, echo=False, critic=crit)
            for _ in range(250):
                world.step()
                pipe.tick()
            decisions = store.critic(10_000)
            self.assertTrue(any(not r["approved"] for r in decisions))
            self.assertTrue(any(r["approved"] for r in decisions))
            self.assertTrue(all(r["reasons"] for r in decisions if not r["approved"]))
            bought = {p["mint"] for p in store._rows("SELECT mint FROM positions")}
            approved = {r["mint"] for r in decisions if r["approved"]}
            self.assertLessEqual(bought, approved)   # nothing is bought without the Critic's OK
            kinds = {world.tokens[m].kind for m in bought}
            self.assertFalse(kinds & {"insider", "serial_dev", "fresh_wallets"})
            st = build_state(store)
            self.assertEqual([a["name"] for a in st["agents"]][4], "Critic")
            self.assertEqual(sum(st["critic_counts"].values()), len(decisions))


if __name__ == "__main__":
    unittest.main()
