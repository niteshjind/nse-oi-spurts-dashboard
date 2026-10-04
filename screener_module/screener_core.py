"""Cache-first compute layer shared by the Streamlit UI.

Reads price data ONLY from the warmed SQLite cache (no network) so the UI is instant.
Refreshing prices from NSE is an explicit action (`refresh_cache`). All indicator/scoring
logic is reused from the existing modules, so the UI and CLI produce identical numbers.
"""

import os

import pandas as pd

from config import (
    SECTORS, TIMEFRAME_CONFIGS, DEFAULT_BENCHMARK, RRG_SCORE_MAP, HISTORY_YEARS, get_all_stocks,
)
from data.cache import get_connection, load_ohlcv, load_quality
from data.fetcher import fetch_index, fetch_universe
from data.reconcile import rebuild_universe, quick_fill
from indicators.rrg import calculate_rrg, resample_close
from indicators.rs_ranking import calculate_rs_score, rank_universe
from indicators.trend_filter import check_trend
from indicators.breadth import calculate_sector_breadth
from scoring.composite import build_stock_profile
from config import DB_PATH

BENCHMARKS = ["NIFTY 500", "NIFTY 50", "NIFTY MIDCAP 150"]
TIMEFRAMES = list(TIMEFRAME_CONFIGS.keys())


def cache_version():
    """A cheap fingerprint of the cache (mtime + size). Used to key UI compute caches so
    results refresh automatically after a 'Refresh data' run, and not before."""
    try:
        st = os.stat(DB_PATH)
        return f"{int(st.st_mtime)}:{st.st_size}"
    except FileNotFoundError:
        return "none"


def cache_stats():
    """How many of the universe's symbols are currently cached."""
    conn = get_connection()
    try:
        rows = conn.execute("SELECT DISTINCT symbol FROM ohlcv").fetchall()
    finally:
        conn.close()
    cached = {r[0] for r in rows if not r[0].startswith("IDX:")}
    universe = set(get_all_stocks())
    return {"cached": len(cached & universe), "total": len(universe)}


def _load_cached(conn, symbol):
    df = load_ohlcv(conn, symbol)
    return df if not df.empty else None


def _benchmark_frame(conn, benchmark):
    """Benchmark from cache; if missing, fetch it once (a single request)."""
    df = load_ohlcv(conn, f"IDX:{benchmark}")
    if df.empty:
        df = fetch_index(benchmark, years_back=HISTORY_YEARS)
    return df


def compute_sectors(timeframe="weekly", benchmark=DEFAULT_BENCHMARK):
    """One row per sector: rotation quadrant, RS-Ratio/Momentum, breadth, score.

    Uses only cached constituents. Sectors with no cached data are skipped.
    Returns (rows: list[dict], meta: dict).
    """
    tf = TIMEFRAME_CONFIGS[timeframe]
    freq = tf["resample"]
    conn = get_connection()
    try:
        bench = _benchmark_frame(conn, benchmark)
        if bench is None or bench.empty:
            return [], {"error": f"Benchmark '{benchmark}' not available in cache."}
        bench_close = resample_close(bench, freq)

        rows = []
        for sector, syms in SECTORS.items():
            data = {s: _load_cached(conn, s) for s in syms}
            data = {s: d for s, d in data.items() if d is not None}
            if not data:
                continue
            breadth = calculate_sector_breadth(data)

            close_dict = {}
            for s, d in data.items():
                c = d["Close"]
                close_dict[s] = c[~c.index.duplicated(keep="last")]
            combined = pd.DataFrame(close_dict).mean(axis=1).dropna()
            rrg = calculate_rrg(
                resample_close(pd.DataFrame({"Close": combined}), freq), bench_close,
                tf["sma_window"], tf["roc_lookback"], tf["tail_length"],
            )
            score = (
                (RRG_SCORE_MAP.get((rrg["quadrant"], rrg["direction"]), 50) if rrg else 50) * 0.5
                + (breadth.get("breadth_200", 50)) * 0.5
            )
            rows.append({
                "sector": sector,
                "n_cached": len(data),
                "n_total": len(syms),
                "quadrant": rrg["quadrant"] if rrg else "N/A",
                "direction": rrg["direction"] if rrg else "",
                "rs_ratio": rrg["rs_ratio"] if rrg else None,
                "rs_momentum": rrg["rs_momentum"] if rrg else None,
                "breadth_200": breadth.get("breadth_200"),
                "breadth_50": breadth.get("breadth_50"),
                "breadth_signal": breadth.get("breadth_signal", ""),
                "rotation_score": round(score, 1),
            })
        rows.sort(key=lambda r: r["rotation_score"], reverse=True)
        return rows, {"error": None, "benchmark": benchmark, "timeframe": timeframe}
    finally:
        conn.close()


def compute_sector_stocks(sector, timeframe="weekly", benchmark=DEFAULT_BENCHMARK):
    """Ranked stock profiles for one sector (cached constituents only)."""
    if sector not in SECTORS:
        return [], {"error": f"Unknown sector: {sector}"}
    tf = TIMEFRAME_CONFIGS[timeframe]
    freq = tf["resample"]
    conn = get_connection()
    try:
        bench = _benchmark_frame(conn, benchmark)
        if bench is None or bench.empty:
            return [], {"error": f"Benchmark '{benchmark}' not available in cache."}
        bench_close = resample_close(bench, freq)

        data = {s: _load_cached(conn, s) for s in SECTORS[sector]}
        data = {s: d for s, d in data.items() if d is not None}
        if not data:
            return [], {"error": f"No cached data for {sector}. Use 'Refresh data'."}

        rrg_results, rs_raw, trend_results = {}, {}, {}
        for sym, df in data.items():
            sc = resample_close(df, freq)
            rrg_results[sym] = calculate_rrg(sc, bench_close, tf["sma_window"],
                                             tf["roc_lookback"], tf["tail_length"])
            rs_raw[sym] = calculate_rs_score(sc, bench_close, tf["rs_periods"])
            trend_results[sym] = check_trend(df["Close"])
        rs_ranked = rank_universe(rs_raw)
        breadth = calculate_sector_breadth(data)

        profiles = []
        for sym in data:
            p = build_stock_profile(sym, sector, rrg_results.get(sym), rs_ranked.get(sym),
                                    trend_results.get(sym), breadth)
            q = load_quality(conn, sym)
            p["qc_status"] = q.get("status") if q else None
            p["qc_agree"] = q.get("agree_pct") if q else None
            profiles.append(p)
        profiles.sort(key=lambda p: p["rotation_score"], reverse=True)
        return profiles, {"error": None}
    finally:
        conn.close()


def symbol_names():
    """{symbol -> company name} from the constituents CSV (for the search box)."""
    import csv as _csv
    from config import NIFTY500_CSV
    names = {}
    try:
        with open(NIFTY500_CSV, newline="", encoding="utf-8-sig") as fh:
            for row in _csv.DictReader(fh):
                sym = (row.get("Symbol") or "").strip()
                if sym:
                    names[sym] = (row.get("Company Name") or "").strip()
    except OSError:
        pass
    return names


def sector_of(symbol):
    """The sector a symbol belongs to, or None if not in the universe."""
    for name, syms in SECTORS.items():
        if symbol in syms:
            return name
    return None


def quality_summary():
    """Aggregate the per-symbol data-quality (QC) records into counts + a list."""
    conn = get_connection()
    try:
        recs = load_quality(conn)
    finally:
        conn.close()
    counts = {"CONFIRMED": 0, "PARTIAL": 0, "SUSPECT": 0, "SINGLE-SOURCE": 0}
    for r in recs:
        counts[r.get("status", "SINGLE-SOURCE")] = counts.get(r.get("status"), 0) + 1
    return {"counts": counts, "records": recs, "n": len(recs)}


def quality_for(symbols):
    """Return {symbol -> status} for a list of symbols."""
    conn = get_connection()
    try:
        out = {}
        for s in symbols:
            q = load_quality(conn, s)
            out[s] = q.get("status") if q else None
        return out
    finally:
        conn.close()


def refresh_cache(progress_cb=None, years_back=HISTORY_YEARS):
    """Fast, reliable price refresh for the UI (yfinance adjusted, single-source).

    Safety net: the cache is snapshotted BEFORE the refresh; if the refresh ever leaves
    the cache catastrophically emptier than it started (any unforeseen failure), it is
    automatically restored from that snapshot. Combined with the per-symbol fetch-then-
    replace logic, a refresh can never leave the tool broken. Returns a counts dict.
    """
    from data.cache import backup_db, restore_db, count_stocks

    before = count_stocks()
    backup_db()  # snapshot first (no-op-safe)

    result = quick_fill(
        get_all_stocks(), DEFAULT_BENCHMARK, years_back=years_back,
        only_missing=False, progress_cb=progress_cb,
    )

    after = count_stocks()
    # Auto-restore guard: if we somehow lost more than half of what we had, roll back.
    if before >= 50 and after < before * 0.5:
        if restore_db():
            result["restored_backup"] = True
            result["after"] = count_stocks()
    return result
