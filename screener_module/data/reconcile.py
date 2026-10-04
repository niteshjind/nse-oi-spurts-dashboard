"""Dual-source price reconciliation for official-grade data integrity.

For each stock we fetch BOTH:
  - yfinance (split/dividend-ADJUSTED, fresh)   -> the canonical analytical series
  - jugaad / NSE (official, UNADJUSTED)         -> an independent integrity check

The canonical series is yfinance-adjusted (continuous through corporate actions, which is
what RS / 200-DMA / RRG require). We then validate it against the official NSE series by
comparing *daily returns* (returns match across both even though absolute levels differ by
the adjustment factor — except on corporate-action days, which are rare and expected).

Outputs per symbol:
  - a cleaned daily OHLCV frame (bad ticks repaired)
  - a QC record: sources, overlap, return-agreement %, ticks repaired, and a status:
        CONFIRMED      both sources, returns agree on ~all common days
        PARTIAL        both sources, but a non-trivial fraction disagree (corp actions/stale)
        SUSPECT        both sources, low agreement even after repair -> review before use
        SINGLE-SOURCE  only one source available (noted explicitly)
"""

from datetime import date, timedelta

import numpy as np
import pandas as pd

from data.fetcher import _fetch_jugaad_stock, _fetch_yfinance

# Tolerances
_RET_TOL = 0.02          # daily returns within 2% counted as agreeing
_SPIKE = 0.30            # |log-return| above this is a candidate bad tick
_REVERT = 0.12           # ...if the next day reverses to within this of the round trip
_CONFIRMED_AT = 97.0     # agree% for CONFIRMED
_PARTIAL_AT = 85.0       # agree% for PARTIAL (else SUSPECT)


def _repair_bad_ticks(close):
    """Repair isolated single-day spikes that revert the next day (data glitches).

    Adjusted prices are continuous through splits/dividends, so a spike-and-revert in the
    adjusted series is a bad tick, not a real move. Repaired by geometric interpolation of
    the neighbours. Returns (repaired_series, n_repaired, repaired_dates).
    """
    s = close.copy().astype(float)
    logret = np.log(s / s.shift(1))
    repaired = 0
    dates = []
    vals = s.to_numpy(dtype=float, copy=True)
    lr = logret.to_numpy(dtype=float, copy=True)
    for i in range(1, len(s) - 1):
        if abs(lr[i]) > _SPIKE and abs(lr[i + 1]) > _SPIKE and (lr[i] * lr[i + 1] < 0) \
                and abs(lr[i] + lr[i + 1]) < _REVERT:
            new = float(np.sqrt(vals[i - 1] * vals[i + 1]))
            if new > 0:
                vals[i] = new
                repaired += 1
                dates.append(s.index[i])
                lr[i + 1] = np.log(vals[i + 1] / vals[i])  # keep chain consistent
    return pd.Series(vals, index=s.index), repaired, dates


def reconcile(symbol, yf_df, jug_df):
    """Combine the two source frames into a canonical frame + QC record."""
    have_yf = yf_df is not None and not yf_df.empty
    have_jug = jug_df is not None and not jug_df.empty

    if not have_yf and not have_jug:
        return None, {"sources": "", "status": "SINGLE-SOURCE", "n_rows": 0,
                      "n_common": 0, "agree_pct": None, "ticks_repaired": 0, "last_date": None}

    # Canonical: prefer yfinance (adjusted). Fall back to jugaad if yfinance missing.
    if have_yf:
        canon = yf_df.copy()
        canon_close = canon["Close"].astype(float)
    else:
        canon = jug_df.copy()
        canon_close = canon["Close"].astype(float)

    repaired_close, n_repaired, _ = _repair_bad_ticks(canon_close)
    canon = canon.copy()
    canon["Close"] = repaired_close

    qc = {
        "sources": "+".join([s for s, h in [("yfinance", have_yf), ("jugaad", have_jug)] if h]),
        "n_rows": int(len(canon)),
        "ticks_repaired": int(n_repaired),
        "last_date": str(canon.index.max().date()),
    }

    if have_yf and have_jug:
        # Compare daily returns on the overlapping trading days. Repair bad ticks in BOTH
        # series first — the official NSE feed (jugaad) also has sporadic glitches, and an
        # uncleaned spike in the *validator* would otherwise wrongly drag down agreement.
        a = repaired_close
        b, _, _ = _repair_bad_ticks(jug_df["Close"].astype(float))
        common = a.index.intersection(b.index)
        if len(common) >= 30:
            ra = a.loc[common].pct_change()
            rb = b.loc[common].pct_change()
            cmp = pd.DataFrame({"a": ra, "b": rb}).dropna()
            agree = (cmp["a"] - cmp["b"]).abs() <= _RET_TOL
            agree_pct = round(100.0 * agree.mean(), 2)
            qc["n_common"] = int(len(cmp))
            qc["agree_pct"] = agree_pct
            qc["status"] = ("CONFIRMED" if agree_pct >= _CONFIRMED_AT
                            else "PARTIAL" if agree_pct >= _PARTIAL_AT else "SUSPECT")
        else:
            qc["n_common"] = int(len(common))
            qc["agree_pct"] = None
            qc["status"] = "PARTIAL"  # too little overlap to fully confirm
    else:
        qc["n_common"] = 0
        qc["agree_pct"] = None
        qc["status"] = "SINGLE-SOURCE"

    return canon, qc


def quick_fill(symbols, benchmark, years_back=7, only_missing=True, progress_cb=None,
               max_workers=10):
    """Fast single-source fill using yfinance (adjusted) only — clean and correct, but not
    yet cross-validated by jugaad (QC status SINGLE-SOURCE). Use to unblock quickly; a later
    dual-source pass upgrades those records to CONFIRMED/PARTIAL/SUSPECT."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import timedelta
    from data.cache import get_connection, save_ohlcv, save_quality
    from data.fetcher import _fetch_yfinance, fetch_index

    fetch_index(benchmark, years_back=years_back, force=True)
    conn = get_connection()
    have = set()
    if only_missing:
        rows = conn.execute("SELECT DISTINCT symbol FROM ohlcv WHERE symbol NOT LIKE 'IDX:%'").fetchall()
        have = {r[0] for r in rows}
    conn.close()

    todo = [s for s in sorted(set(symbols)) if s not in have]
    to_date = date.today()
    from_date = to_date - timedelta(days=years_back * 365)
    total = len(todo)
    done = 0
    counts = {"updated": 0, "failed": 0}

    def _one(sym):
        # Fetch FIRST. Only touch the cache if we actually got valid data — never delete
        # good rows on a failed/empty fetch (that's what could blank out the tool).
        yf = _fetch_yfinance(sym, from_date, to_date, is_index=False)
        canon, qc = reconcile(sym, yf, None)
        if canon is None or canon.empty or canon["Close"].notna().sum() < 30:
            return sym, "failed"
        c = get_connection()
        try:
            c.execute("DELETE FROM ohlcv WHERE symbol=?", (sym,))
            save_ohlcv(c, sym, canon, source="stock")
            save_quality(c, sym, qc)
        finally:
            c.close()
        return sym, "updated"

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = {pool.submit(_one, s): s for s in todo}
        for f in as_completed(futs):
            try:
                _, status = f.result()
            except Exception:
                status = "failed"
            counts[status] = counts.get(status, 0) + 1
            done += 1
            if progress_cb:
                progress_cb(done, total, futs[f])
    counts["total"] = total
    return counts


def fetch_reconciled(symbol, years_back=7):
    """Fetch both sources for one symbol and return (canonical_df, qc)."""
    to_date = date.today()
    from_date = to_date - timedelta(days=years_back * 365)
    yf_df = _fetch_yfinance(symbol, from_date, to_date, is_index=False)
    jug_df = _fetch_jugaad_stock(symbol, from_date, to_date)
    return reconcile(symbol, yf_df, jug_df)


def rebuild_universe(symbols, benchmark, years_back=7, progress_cb=None, max_workers=8,
                     resume=False):
    """Rebuild the price cache from scratch via dual-source reconciliation.

    Every symbol is re-fetched from both sources, reconciled, and stored with a QC record.
    Idempotent per symbol (clears that symbol's rows then re-inserts), so a re-run repairs
    a partial cache without corrupting the good rows. With resume=True, symbols already
    validated today are skipped — use it to finish an interrupted rebuild. Returns a QC
    summary dict.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from datetime import date
    from data.cache import get_connection, save_ohlcv, save_quality
    from data.fetcher import fetch_index

    conn = get_connection()
    done_today = set()
    if resume:
        rows = conn.execute(
            "SELECT symbol FROM data_quality WHERE validated_on=? AND n_rows>0",
            (str(date.today()),),
        ).fetchall()
        done_today = {r[0] for r in rows}
    conn.close()
    # NOTE: we deliberately do NOT purge the cache up front. Each symbol is replaced only
    # after its fetch succeeds (see _one), so a failed/interrupted rebuild can never blank
    # out the existing good data.

    # --- benchmark (yfinance index, single clean source) ---
    fetch_index(benchmark, years_back=years_back, force=True)

    unique = [s for s in sorted(set(symbols)) if s not in done_today]
    total = len(unique)
    done = 0
    summary = {"CONFIRMED": 0, "PARTIAL": 0, "SUSPECT": 0, "SINGLE-SOURCE": 0,
               "FAILED": 0, "skipped": len(done_today)}

    def _one(sym):
        canon, qc = fetch_reconciled(sym, years_back=years_back)
        # Replace only on a valid fetch; never delete good rows on failure.
        if canon is None or canon.empty or canon["Close"].notna().sum() < 30:
            return sym, {"status": "FAILED", "n_rows": 0}
        c = get_connection()
        try:
            c.execute("DELETE FROM ohlcv WHERE symbol=?", (sym,))
            save_ohlcv(c, sym, canon, source="stock")
            save_quality(c, sym, qc)
        finally:
            c.close()
        return sym, qc

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_one, s): s for s in unique}
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                _, qc = fut.result()
                status = qc.get("status") or "FAILED"
                if qc.get("n_rows", 0) == 0:
                    status = "FAILED"
            except Exception:
                status = "FAILED"
            summary[status] = summary.get(status, 0) + 1
            done += 1
            if progress_cb:
                progress_cb(done, total, sym)

    return summary
