"""Edge check: is the paper result real or luck? (idea from m3n4ce-dev/meme_traderv1)

* mean return per trade with a 90 % bootstrap interval,
* total P&L without the best 3 trades (one lucky moonshot shouldn't carry the result),
* a plain-language verdict.
"""
from __future__ import annotations

import random


def edge_check(pnl_pct: list[float], pnl_sol: list[float], resamples: int = 1000,
               min_trades: int = 30, seed: int = 1) -> dict:
    n = len(pnl_pct)
    out = {"n": n, "mean_pct": None, "ci90_low": None, "ci90_high": None,
           "pnl_sol": round(sum(pnl_sol), 6), "pnl_without_best3_sol": None, "verdict": "zatiaľ žiadne uzavreté obchody"}
    if n == 0:
        return out
    mean = sum(pnl_pct) / n
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(pnl_pct, k=n)) / n for _ in range(resamples))
    lo, hi = means[int(0.05 * resamples)], means[int(0.95 * resamples) - 1]
    best3 = sorted(pnl_sol, reverse=True)[:3]
    out.update(mean_pct=round(mean, 3), ci90_low=round(lo, 3), ci90_high=round(hi, 3),
               pnl_without_best3_sol=round(sum(pnl_sol) - sum(best3), 6))
    if n < min_trades:
        out["verdict"] = f"málo obchodov na posúdenie ({n}/{min_trades})"
    elif hi < 0:
        out["verdict"] = "stráca, a nie smolou (celý 90 % rozsah je pod 0)"
    elif lo > 0 and out["pnl_without_best3_sol"] > 0:
        out["verdict"] = "vyzerá kladne (90 % rozsah nad 0, v zisku aj bez 3 najlepších)"
    elif lo > 0:
        out["verdict"] = "kladný rozsah, ale ťahajú ho 3 najlepšie obchody"
    else:
        out["verdict"] = "zatiaľ sa nedá povedať: 90 % rozsah obsahuje 0"
    return out
