"""Central configuration: data paths, timeframe windows, scoring weights, and the
sector → constituents universe.

This is a strict v1 sector-rotation *screener* — EOD only, CLI only, with CSV export.
Explicitly out of scope for v1 (see README "v2 roadmap"):
  - FII/DII flow integration   (needs fragile NSDL/SEBI scraping)
  - backtesting engine         (this is a screener, not a backtester)
  - GUI / web dashboard        (CLI + CSV export only)
"""

import csv
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "cache.db")

# Universe = the official NSE Nifty 500, grouped by NSE's own "Industry" macro-sector
# classification. Refresh with `python data/refresh_universe.py` (re-downloads the CSV).
NIFTY500_CSV = os.path.join(DATA_DIR, "nifty500_constituents.csv")

DEFAULT_BENCHMARK = "NIFTY 500"

# Parallel fetch tuning (replaces the old 0.3s inter-fetch sleep).
FETCH_MAX_WORKERS = 8

# Years of daily history to fetch. The longer timeframes (quarterly/semi-annual/annual)
# resample to few points, so they need many years of underlying data to be computable.
# 7 years supports weekly→semi-annual fully; annual is data-limited by free retail sources.
HISTORY_YEARS = 7

TIMEFRAME_CONFIGS = {
    "weekly": {
        "resample": "W",
        "sma_window": 14,
        "roc_lookback": 52,
        "rs_periods": {"1M": 4, "3M": 13, "6M": 26, "12M": 52},
        "tail_length": 8,
        "min_history_days": 400,
    },
    "monthly": {
        "resample": "ME",
        "sma_window": 10,
        "roc_lookback": 12,
        "rs_periods": {"1M": 1, "3M": 3, "6M": 6, "12M": 12},
        "tail_length": 6,
        "min_history_days": 800,
    },
    # Long-timeframe windows are sized to be computable with ~7 years of history
    # (RRG needs ≈ 2·sma_window + roc_lookback points after resampling).
    "quarterly": {
        "resample": "QE",
        "sma_window": 4,
        "roc_lookback": 4,
        "rs_periods": {"1Q": 1, "2Q": 2, "4Q": 4, "8Q": 8},
        "tail_length": 5,
        "min_history_days": 1000,
    },
    # "2QE" (every 2 quarter-ends), NOT "6ME": 6ME anchors its bins to each series'
    # own start date, so series starting in different months don't align with the
    # benchmark and RRG returns None. 2QE snaps to calendar quarter-ends for everyone.
    "semi-annual": {
        "resample": "2QE",
        "sma_window": 3,
        "roc_lookback": 2,
        "rs_periods": {"6M": 1, "1Y": 2, "2Y": 4, "3Y": 6},
        "tail_length": 5,
        "min_history_days": 1500,
    },
    "annual": {
        "resample": "YE",
        "sma_window": 2,
        "roc_lookback": 1,
        "rs_periods": {"1Y": 1, "2Y": 2, "3Y": 3},
        "tail_length": 4,
        "min_history_days": 2000,
    },
}

RS_WEIGHTS = {"1": 0.15, "2": 0.25, "3": 0.30, "4": 0.30}

COMPOSITE_WEIGHTS = {
    "rrg": 0.25,
    "rs_rank": 0.35,
    "trend": 0.20,
    "breadth": 0.20,
}

RRG_SCORE_MAP = {
    ("Leading", "accelerating"): 100,
    ("Leading", "decelerating"): 70,
    ("Improving", "accelerating"): 80,
    ("Improving", "decelerating"): 40,
    ("Weakening", "accelerating"): 50,
    ("Weakening", "decelerating"): 30,
    ("Lagging", "accelerating"): 20,
    ("Lagging", "decelerating"): 10,
}

def _load_universe(csv_path=NIFTY500_CSV):
    """Build {sector -> [symbols]} from the Nifty 500 constituents CSV, grouped by
    NSE's "Industry" column. Returns an ordered dict (largest sector first)."""
    groups = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            sym = (row.get("Symbol") or "").strip()
            industry = (row.get("Industry") or "").strip() or "Uncategorised"
            if sym:
                groups.setdefault(industry, []).append(sym)
    # stable, largest-sector-first ordering
    return {
        name: sorted(syms)
        for name, syms in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    }


try:
    SECTORS = _load_universe()
except FileNotFoundError:
    raise FileNotFoundError(
        f"Universe file not found: {NIFTY500_CSV}\n"
        "Run `python data/refresh_universe.py` to download the Nifty 500 list."
    )

BENCHMARK_SYMBOLS = {
    "NIFTY 500": "NIFTY 500",
    "NIFTY 50": "NIFTY 50",
    "NIFTY MIDCAP 150": "NIFTY MIDCAP 150",
}


def get_all_stocks():
    """Return the deduplicated, sorted universe across every sector."""
    seen = set()
    for stocks in SECTORS.values():
        for s in stocks:
            seen.add(s)
    return sorted(seen)
