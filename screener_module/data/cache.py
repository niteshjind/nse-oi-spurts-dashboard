"""SQLite EOD cache. WAL mode + busy_timeout make per-thread connections safe for
the parallel fetcher (each thread opens its own connection)."""

import os
import shutil
import sqlite3
from datetime import date

import pandas as pd

from config import DB_PATH


def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    # WAL allows concurrent readers + one writer; busy_timeout retries on lock
    # instead of raising "database is locked" under the ThreadPoolExecutor fetch.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv (
            symbol TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL,
            high REAL,
            low REAL,
            close REAL,
            volume INTEGER,
            source TEXT DEFAULT 'stock',
            PRIMARY KEY (symbol, date)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fetch_log (
            symbol TEXT PRIMARY KEY,
            last_fetched TEXT NOT NULL
        )
    """)
    # Per-symbol data-quality record from dual-source reconciliation.
    conn.execute("""
        CREATE TABLE IF NOT EXISTS data_quality (
            symbol TEXT PRIMARY KEY,
            sources TEXT,          -- e.g. "yfinance+jugaad", "yfinance", "jugaad"
            n_rows INTEGER,
            n_common INTEGER,      -- overlapping trading days both sources covered
            agree_pct REAL,        -- % of common days where daily returns matched
            ticks_repaired INTEGER,
            last_date TEXT,
            status TEXT,           -- CONFIRMED | PARTIAL | SUSPECT | SINGLE-SOURCE
            validated_on TEXT
        )
    """)
    conn.commit()
    return conn


def save_quality(conn, symbol, qc):
    conn.execute(
        "INSERT OR REPLACE INTO data_quality "
        "(symbol, sources, n_rows, n_common, agree_pct, ticks_repaired, last_date, status, validated_on) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (symbol, qc.get("sources"), qc.get("n_rows"), qc.get("n_common"),
         qc.get("agree_pct"), qc.get("ticks_repaired"), qc.get("last_date"),
         qc.get("status"), str(date.today())),
    )
    conn.commit()


def load_quality(conn, symbol=None):
    if symbol:
        row = conn.execute("SELECT * FROM data_quality WHERE symbol=?", (symbol,)).fetchone()
        if not row:
            return None
        cols = [c[0] for c in conn.execute("SELECT * FROM data_quality LIMIT 0").description]
        return dict(zip(cols, row))
    rows = conn.execute("SELECT * FROM data_quality").fetchall()
    cols = [c[0] for c in conn.execute("SELECT * FROM data_quality LIMIT 0").description]
    return [dict(zip(cols, r)) for r in rows]


def needs_update(conn, symbol):
    row = conn.execute(
        "SELECT last_fetched FROM fetch_log WHERE symbol = ?", (symbol,)
    ).fetchone()
    if row is None:
        return True
    last = date.fromisoformat(row[0])
    return (date.today() - last).days >= 1


def _num(*vals):
    """First non-null numeric value, else None."""
    for v in vals:
        if v is None:
            continue
        if isinstance(v, float) and pd.isna(v):
            continue
        return v
    return None


def save_ohlcv(conn, symbol, df, source="stock"):
    if df is None or df.empty:
        return
    records = []
    for idx, row in df.iterrows():
        # NOTE: a pandas Timestamp IS-A datetime.date, so `isinstance(idx, date)` is always
        # True — the old code therefore stored the full timestamp (with time/tz), letting
        # 00:00 (yfinance) and 18:30 (jugaad) rows for the same session coexist and corrupt
        # the series. Always coerce to a pure calendar date.
        dt = pd.Timestamp(idx).date()
        records.append((
            symbol, str(dt),
            _num(row.get("Open"), row.get("open")),
            _num(row.get("High"), row.get("high")),
            _num(row.get("Low"), row.get("low")),
            _num(row.get("Close"), row.get("close")),
            _num(row.get("Volume"), row.get("volume")) or 0,
            source,
        ))
    conn.executemany(
        "INSERT OR REPLACE INTO ohlcv (symbol, date, open, high, low, close, volume, source) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        records,
    )
    conn.execute(
        "INSERT OR REPLACE INTO fetch_log (symbol, last_fetched) VALUES (?, ?)",
        (symbol, str(date.today())),
    )
    conn.commit()


def load_ohlcv(conn, symbol, from_date=None):
    query = "SELECT date, open, high, low, close, volume FROM ohlcv WHERE symbol = ?"
    params = [symbol]
    if from_date:
        query += " AND date >= ?"
        params.append(str(from_date))
    query += " ORDER BY date"
    df = pd.read_sql_query(query, conn, params=params)
    if df.empty:
        return df
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date")
    df = df[~df.index.duplicated(keep="last")]
    df.columns = ["Open", "High", "Low", "Close", "Volume"]
    return df


def get_cached_symbols(conn):
    rows = conn.execute("SELECT DISTINCT symbol FROM ohlcv").fetchall()
    return {r[0] for r in rows}


def count_stocks(conn=None):
    """Number of distinct non-index symbols with cached data."""
    own = conn is None
    conn = conn or get_connection()
    try:
        return conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM ohlcv WHERE symbol NOT LIKE 'IDX:%'"
        ).fetchone()[0]
    finally:
        if own:
            conn.close()


_BACKUP = DB_PATH + ".backup"


def backup_db():
    """WAL-safe snapshot of the cache DB before a risky operation (e.g. a refresh).

    Checkpoints the write-ahead log into the main file, then copies it. Returns the
    backup path, or None if there is nothing to back up. Never raises.
    """
    try:
        if not os.path.exists(DB_PATH):
            return None
        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        shutil.copy2(DB_PATH, _BACKUP)
        return _BACKUP
    except Exception:
        return None


def restore_db():
    """Restore the cache DB from the last backup. Removes stale WAL/SHM side-files so the
    restored database is used as-is. Returns True if a restore happened."""
    try:
        if not os.path.exists(_BACKUP):
            return False
        for ext in ("-wal", "-shm"):
            p = DB_PATH + ext
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        shutil.copy2(_BACKUP, DB_PATH)
        return True
    except Exception:
        return False
