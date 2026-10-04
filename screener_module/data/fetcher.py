"""EOD data fetching: jugaad-data (NSE, primary) → yfinance (.NS suffix, fallback).

All results are cached in SQLite, so cold fetches are slow but every subsequent run
reads from cache in seconds. `fetch_universe` parallelises the cold fetch across a
thread pool (replaces the old serial 0.3s-sleep loop)."""

import logging
import socket
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

import pandas as pd

from config import FETCH_MAX_WORKERS
from data.cache import get_connection, needs_update, save_ohlcv, load_ohlcv

warnings.filterwarnings("ignore")
# Backstop so a hung NSE/Yahoo socket can't wedge a worker forever during throttling —
# fetches fail fast and the run continues (the symbol is just reported as failed).
socket.setdefaulttimeout(20)
# yfinance logs failed downloads (404s, "possibly delisted") to its own logger even
# when we catch the exception and fall back. Silence it — failures are reported by the CLI.
logging.getLogger("yfinance").setLevel(logging.CRITICAL)


def _fetch_jugaad_stock(symbol, from_date, to_date):
    try:
        from jugaad_data.nse import stock_df
        df = stock_df(symbol=symbol, from_date=from_date, to_date=to_date, series="EQ")
        if df is not None and not df.empty:
            df = df.rename(columns={
                "DATE": "date", "OPEN": "Open", "HIGH": "High",
                "LOW": "Low", "CLOSE": "Close", "VOLUME": "Volume",
            })
            keep = ["date", "Open", "High", "Low", "Close", "Volume"]
            df = df[[c for c in keep if c in df.columns]]
            # jugaad returns naive timestamps at 18:30 = the UTC instant of IST-midnight,
            # i.e. the *next* day's session. Add 5:30h and normalize to recover the true
            # NSE trading date (aligns with yfinance's correctly-dated midnight rows).
            df["date"] = (pd.to_datetime(df["date"]) + pd.Timedelta(hours=5, minutes=30)).dt.normalize()
            df = df.set_index("date").sort_index()
            df = df[~df.index.duplicated(keep="last")]
            if "Close" in df.columns:
                df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
                df = df[df["Close"].notna() & (df["Close"] > 0)]
            return df if not df.empty else None
    except Exception:
        pass
    return None


def _fetch_jugaad_index(symbol, from_date, to_date):
    try:
        from jugaad_data.nse import index_df
        df = index_df(symbol=symbol, from_date=from_date, to_date=to_date)
        if df is not None and not df.empty:
            date_col = next(
                (c for c in df.columns if "date" in c.lower() or "timestamp" in c.lower()),
                df.columns[0],
            )
            close_col = next((c for c in df.columns if "close" in c.lower()), None)
            open_col = next((c for c in df.columns if "open" in c.lower()), None)
            high_col = next((c for c in df.columns if "high" in c.lower()), None)
            low_col = next((c for c in df.columns if "low" in c.lower()), None)

            result = pd.DataFrame()
            # Same IST-midnight correction as the stock path (see _fetch_jugaad_stock).
            result["date"] = (pd.to_datetime(df[date_col]) + pd.Timedelta(hours=5, minutes=30)).dt.normalize()
            result["Close"] = pd.to_numeric(df[close_col], errors="coerce") if close_col else None
            result["Open"] = pd.to_numeric(df[open_col], errors="coerce") if open_col else result["Close"]
            result["High"] = pd.to_numeric(df[high_col], errors="coerce") if high_col else result["Close"]
            result["Low"] = pd.to_numeric(df[low_col], errors="coerce") if low_col else result["Close"]
            result["Volume"] = 0
            return result.set_index("date").sort_index()
    except Exception:
        pass
    return None


_YF_INDEX_TICKERS = {
    "NIFTY 50": "^NSEI",
    "NIFTY 500": "^CRSLDX",
    "NIFTY BANK": "^NSEBANK",
    "NIFTY IT": "^CNXIT",
    "NIFTY MIDCAP 150": "NIFTYMIDCAP150.NS",
}


def _fetch_yfinance(symbol, from_date, to_date, is_index=False):
    try:
        import yfinance as yf
        if is_index:
            ticker = _YF_INDEX_TICKERS.get(symbol, f"{symbol.replace(' ', '')}.NS")
        else:
            ticker = f"{symbol}.NS"
        df = yf.download(ticker, start=from_date, end=to_date, progress=False, auto_adjust=True)
        if df is not None and not df.empty:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df[["Open", "High", "Low", "Close", "Volume"]].copy()
            df.index = pd.to_datetime(df.index).tz_localize(None).normalize()
            df = df[~df.index.duplicated(keep="last")]
            # Drop forming/glitched bars: yfinance sometimes returns a trailing NaN or
            # zero close. These must never enter the cache or they poison every indicator.
            df = df[df["Close"].notna() & (df["Close"] > 0)]
            return df if not df.empty else None
    except Exception:
        pass
    return None


def fetch_stock(symbol, years_back=3, force=False):
    conn = get_connection()
    try:
        if not force and not needs_update(conn, symbol):
            df = load_ohlcv(conn, symbol)
            if not df.empty:
                return df

        to_date = date.today()
        from_date = to_date - timedelta(days=years_back * 365)

        df = _fetch_jugaad_stock(symbol, from_date, to_date)
        if df is None or df.empty:
            df = _fetch_yfinance(symbol, from_date, to_date, is_index=False)

        if df is not None and not df.empty:
            save_ohlcv(conn, symbol, df, source="stock")
            return df
        return pd.DataFrame()
    finally:
        conn.close()


def fetch_index(symbol, years_back=3, force=False):
    conn = get_connection()
    cache_key = f"IDX:{symbol}"
    try:
        if not force and not needs_update(conn, cache_key):
            df = load_ohlcv(conn, cache_key)
            if not df.empty:
                return df

        to_date = date.today()
        from_date = to_date - timedelta(days=years_back * 365)

        df = _fetch_jugaad_index(symbol, from_date, to_date)
        if df is None or df.empty:
            df = _fetch_yfinance(symbol, from_date, to_date, is_index=True)

        if df is not None and not df.empty:
            save_ohlcv(conn, cache_key, df, source="index")
            return df
        return pd.DataFrame()
    finally:
        conn.close()


def fetch_universe(symbols, years_back=3, force=False, progress_callback=None,
                   max_workers=FETCH_MAX_WORKERS):
    """Fetch a deduplicated set of symbols concurrently.

    Each worker thread uses its own SQLite connection (WAL mode handles concurrency).
    Returns (stock_data: dict[sym -> DataFrame], failed: list[sym]).
    progress_callback(done, total, sym) is invoked as each fetch completes.
    """
    unique = sorted(set(symbols))
    stock_data = {}
    failed = []
    done = 0
    total = len(unique)

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            pool.submit(fetch_stock, sym, years_back, force): sym
            for sym in unique
        }
        for fut in as_completed(futures):
            sym = futures[fut]
            try:
                df = fut.result()
            except Exception:
                df = pd.DataFrame()
            if df is not None and not df.empty:
                stock_data[sym] = df
            else:
                failed.append(sym)
            done += 1
            if progress_callback:
                progress_callback(done, total, sym)

    return stock_data, failed
