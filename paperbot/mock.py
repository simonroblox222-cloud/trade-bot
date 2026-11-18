"""Offline synthetic market for demos and tests (`--mock`).

Implements the same provider interfaces as the real sources, with constant-product
pools so quotes have realistic price impact. A share of tokens are scams:
mint/freeze authority, concentrated holders, unlocked LP, honeypot (no sell route),
sell tax and delayed rug pulls (liquidity removed after entry). Scams the RugCheck agent
can't see are left for the Critic: insider bundles, serial-launcher devs, fresh-wallet
holders, bot-driven hype, paid boosts and copycat symbols.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass, field

from .config import LAMPORTS_PER_SOL, SOL_MINT
from .models import Holder, MintInfo, Quote, TokenCandidate

SOL_USD = 150.0
LP_FEE = 0.0025
KINDS = ["good", "good", "good", "neutral", "neutral", "dump", "rug_later", "honeypot",
         "mint_auth", "freeze_auth", "whale", "lp_unlocked", "tax",
         "insider", "serial_dev", "fresh_wallets", "fake_hype", "copycat"]
CRITIC_KINDS = {"insider", "serial_dev", "fresh_wallets", "fake_hype", "copycat"}
ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


@dataclass
class MockToken:
    mint: str
    symbol: str
    kind: str
    sol_res: float          # lamports
    tok_res: float          # raw token units
    decimals: int = 6
    supply: int = 1_000_000_000 * 10**6
    created_ms: int = 0
    drift: float = 0.0
    bonding: bool = False
    rug_at: int | None = None
    creator: str = ""
    has_x: bool = False
    has_tg: bool = False
    has_web: bool = False
    boosts: int = 0
    replies: int = 0
    koth: bool = False
    history: list[float] = field(default_factory=list)   # price per tick
    vol_hist: list[float] = field(default_factory=list)  # USD volume per tick

    @property
    def price(self) -> float:  # lamports per raw token
        return self.sol_res / self.tok_res


class MockWorld:
    def __init__(self, seed: int = 7, spawn_per_tick: float = 0.8):
        self.rng = random.Random(seed)
        self.tokens: dict[str, MockToken] = {}
        self.t = 0
        self.spawn_per_tick = spawn_per_tick
        self.owner_kind: dict[str, str] = {}
        for _ in range(6):
            self._spawn()

    # ------------------------------------------------------------------ world
    def _spawn(self) -> None:
        r = self.rng
        mint = "".join(r.choice(ALPHABET) for _ in range(43)) + "pump"[: r.choice([0, 4])]
        kind = r.choice(KINDS)
        symbol = "".join(r.choice("ABCDEFGHIJKLMNOPRSTUVWXYZ") for _ in range(r.randint(3, 6)))
        if kind == "copycat" and self.tokens:
            symbol = r.choice(list(self.tokens.values())).symbol   # same ticker, new mint
        organic = kind in ("good", "neutral", "dump") or r.random() < 0.6
        sol = r.uniform(80, 600) * LAMPORTS_PER_SOL
        tok = r.uniform(0.3, 0.8) * 1e15
        tk = MockToken(
            mint=mint, symbol=symbol,
            kind=kind, sol_res=sol, tok_res=tok,
            creator=("devSERIAL" + str(r.randint(1, 3))) if kind == "serial_dev" or (kind == "rug_later" and r.random() < 0.5)
            else "dev" + mint[:8],
            has_x=(organic and r.random() < 0.85) and kind != "fake_hype", has_tg=r.random() < 0.5,
            has_web=r.random() < 0.4, boosts=r.choice([0, 0, 0, 10]) if kind != "fake_hype" else 30,
            replies=r.randint(0, 150) if kind != "fake_hype" else r.randint(4000, 9000),
            koth=r.random() < 0.3,
            created_ms=int((time.time() - r.uniform(40 * 60, 20 * 3600)) * 1000),
            drift={"good": 0.006, "neutral": -0.002, "dump": -0.012}.get(kind, r.uniform(-0.006, 0.008)),
            bonding=mint.endswith("pump") and r.random() < 0.5,
            rug_at=self.t + r.randint(6, 25) if kind == "rug_later" else None,
        )
        # warm-up history so m5/h1 changes exist, biased to look like momentum
        p = tk.price
        hist = []
        for _ in range(60):
            hist.append(p)
            p *= 1 + r.gauss(0.004, 0.02)
        tk.history = hist
        tk.vol_hist = [r.uniform(500, 3000) for _ in range(60)]
        self._set_price(tk, hist[-1])
        if kind == "copycat" and self.tokens:  # copy is smaller than the original
            tk.sol_res *= 0.5
        self.tokens[mint] = tk

    @staticmethod
    def _set_price(tk: MockToken, new_price: float) -> None:
        k = tk.sol_res * tk.tok_res
        tk.sol_res = (k * new_price) ** 0.5
        tk.tok_res = k / tk.sol_res

    def step(self) -> None:
        """Advance the synthetic market by one simulated minute (call once per tick)."""
        self.t += 1
        r = self.rng
        if r.random() < self.spawn_per_tick:
            self._spawn()
        for tk in list(self.tokens.values()):
            if tk.rug_at is not None and self.t >= tk.rug_at:
                tk.sol_res = max(tk.sol_res * 0.02, 0.5 * LAMPORTS_PER_SOL)  # LP pulled
                tk.rug_at = None
                tk.drift = -0.05
            self._set_price(tk, tk.price * max(0.05, 1 + r.gauss(tk.drift, 0.035)))
            tk.history.append(tk.price)
            tk.vol_hist.append(abs(r.gauss(2000, 1500)))
            tk.history = tk.history[-120:]
            tk.vol_hist = tk.vol_hist[-120:]

    # ------------------------------------------------------------- providers
    def candidates(self) -> list[TokenCandidate]:
        out = []
        for tk in list(self.tokens.values())[-40:]:  # only recently listed pairs are "new"
            h = tk.history
            m5 = (h[-1] / h[-6] - 1) * 100
            h1 = (h[-1] / h[-61] - 1) * 100 if len(h) > 60 else (h[-1] / h[0] - 1) * 100
            up = m5 > 0
            buys = self.rng.randint(20, 80) if up else self.rng.randint(5, 40)
            sells = self.rng.randint(5, 35) if up else self.rng.randint(20, 80)
            price_native = tk.price * 10**tk.decimals / LAMPORTS_PER_SOL
            bonding = tk.mint.endswith("pump")
            out.append(TokenCandidate(
                price_native=price_native, price_usd=price_native * SOL_USD,
                has_x=tk.has_x, has_telegram=tk.has_tg, has_website=tk.has_web, boosts_active=tk.boosts,
                pf_replies=tk.replies if bonding else None, pf_koth=tk.koth and bonding,
                creator=tk.creator if bonding else None,
                mint=tk.mint, symbol=tk.symbol, liquidity_usd=2 * tk.sol_res / LAMPORTS_PER_SOL * SOL_USD,
                volume_h1_usd=sum(tk.vol_hist[-60:]) * 3, price_change_m5_pct=m5, price_change_h1_pct=h1,
                buys_m5=buys, sells_m5=sells, pair_created_at_ms=tk.created_ms,
                source="pumpfun" if tk.mint.endswith("pump") else "dexscreener", dex="mock",
                is_bonding_curve=tk.bonding,
            ))
        return out

    def quote(self, input_mint: str, output_mint: str, amount: int, slippage_bps: int) -> Quote | None:
        buying = input_mint == SOL_MINT
        tk = self.tokens.get(output_mint if buying else input_mint)
        if tk is None or amount <= 0:
            return None
        if not buying and tk.kind == "honeypot":
            return None
        x, y = (tk.sol_res, tk.tok_res) if buying else (tk.tok_res, tk.sol_res)
        dx = amount * (1 - LP_FEE)
        out = y * dx / (x + dx)
        if not buying and tk.kind == "tax":
            out *= 0.85
        spot = y / x
        impact = max(0.0, (1 - (out / amount) / spot) * 100 - LP_FEE * 100)
        out_i = int(out)
        return Quote(input_mint, output_mint, amount, out_i, int(out_i * (1 - slippage_bps / 10_000)),
                     impact, slippage_bps, ["MockAMM"])

    def mint_info(self, mint: str) -> MintInfo | None:
        tk = self.tokens.get(mint)
        if tk is None:
            return None
        return MintInfo(tk.decimals, tk.supply,
                        "AuThOrItY111" if tk.kind == "mint_auth" else None,
                        "FrEeZe111" if tk.kind == "freeze_auth" else None, "spl-token", {})

    def largest_holders(self, mint: str) -> list[Holder]:
        tk = self.tokens.get(mint)
        if tk is None:
            return []
        pool = Holder("5Q544fKrFoe6tsEbD7S8EmxGTJYAKtTVhAW5Q5pge4j1", "vault", int(tk.supply * 0.6))
        if tk.bonding:
            pool = Holder("GpMZbSM2GgvTKHJirzeGfMFoaZ8UR2X7F4v8vHTvxFbL", "curve", int(tk.supply * 0.6))  # a PDA
        big = 0.25 if tk.kind == "whale" else 0.03
        hs = [Holder(f"{mint[:6]}w{i}", f"a{i}", int(tk.supply * (big if i == 0 else 0.012))) for i in range(12)]
        for h in hs:
            self.owner_kind[h.owner] = tk.kind
        return [pool, *hs]

    # Critic data ---------------------------------------------------------
    def wallet_tx_count(self, owner: str, limit: int = 20) -> int | None:
        kind = self.owner_kind.get(owner)
        if kind == "fresh_wallets" and int(owner[-1]) < 6:
            return 1
        return min(limit, 3 + sum(map(ord, owner)) % 400)

    def report(self, mint: str) -> dict | None:
        tk = self.tokens.get(mint)
        if tk is None:
            return None
        ins = tk.kind == "insider"
        return {
            "rugged": False, "creator": tk.creator, "totalHolders": 80 if ins else 400 + len(tk.history) * 7,
            "token": {"supply": tk.supply, "decimals": tk.decimals},
            "creatorBalance": int(tk.supply * (0.09 if ins else 0.0)),
            "topHolders": [{"address": f"h{i}", "pct": 3.0, "insider": ins and i < 6} for i in range(10)],
            "insiderNetworks": [{"id": "n1", "size": 14, "type": "transfer"}] if ins else [],
        }

    def creator_coins(self, creator: str) -> list[dict] | None:
        if creator.startswith("devSERIAL"):
            return [{"mint": f"old{i}", "complete": False, "usd_market_cap": 3000} for i in range(30)]
        return [{"mint": "first", "complete": True, "usd_market_cap": 90_000}]

    def summary(self, mint: str) -> dict | None:
        tk = self.tokens.get(mint)
        if tk is None:
            return None
        risks = []
        if tk.kind == "lp_unlocked":
            risks.append({"name": "Large Amount of LP Unlocked", "level": "danger"})
        if tk.kind == "rug_later" and self.rng.random() < 0.3:  # RugCheck catches only some
            risks.append({"name": "Creator history of rugged tokens", "level": "danger"})
        return {"score_normalised": self.rng.randint(1, 30) + (60 if risks else 0), "risks": risks,
                "lpLockedPct": 20.0 if tk.kind == "lp_unlocked" else 100.0}
