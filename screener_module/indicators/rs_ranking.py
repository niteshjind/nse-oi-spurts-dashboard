"""Multi-timeframe relative-strength composite + cross-universe percentile rank."""

import pandas as pd
import numpy as np


def calculate_rs_score(stock_close, benchmark_close, periods, weights=None):
    if stock_close is None or benchmark_close is None:
        return None
    if stock_close.empty or benchmark_close.empty:
        return None

    if weights is None:
        weights = [0.15, 0.25, 0.30, 0.30]

    aligned = pd.DataFrame({"stock": stock_close, "bench": benchmark_close}).dropna()
    if len(aligned) < max(periods.values()) + 5:
        return None

    scores = []
    for lookback in periods.values():
        if len(aligned) <= lookback:
            return None
        stock_ret = aligned["stock"].iloc[-1] / aligned["stock"].iloc[-lookback - 1] - 1
        bench_ret = aligned["bench"].iloc[-1] / aligned["bench"].iloc[-lookback - 1] - 1
        scores.append(stock_ret - bench_ret)

    composite = sum(s * w for s, w in zip(scores, weights[:len(scores)]))

    return {
        "composite_rs": round(composite * 100, 4),
        "period_scores": {
            name: round(score * 100, 2)
            for name, score in zip(periods.keys(), scores)
        },
    }


def rank_universe(rs_results):
    """Assign each symbol a 0-100 percentile rank by composite RS across the universe."""
    if not rs_results:
        return {}

    composites = {
        sym: r["composite_rs"]
        for sym, r in rs_results.items()
        if r is not None
    }
    if not composites:
        return {}

    sorted_syms = sorted(composites.keys(), key=lambda s: composites[s])
    total = len(sorted_syms)

    ranked = {}
    for rank, sym in enumerate(sorted_syms):
        percentile = round((rank / max(total - 1, 1)) * 100, 1)
        ranked[sym] = {
            **rs_results[sym],
            "percentile": percentile,
            "rank": rank + 1,
            "total": total,
        }
    return ranked
