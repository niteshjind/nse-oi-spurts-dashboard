"""
NSE OI Spurts Dashboard - Flask Backend
Features:
- Live market data streaming from NSE India
- Ultra-fast in-memory cache
- 5-Day Rolling Memory Archive with automatic daily archiving and 5-day auto-pruning
- Historical Bhavcopy data fetcher (real NSE F&O archives for any past date)
- Compatible with local execution and Vercel serverless functions
"""

import csv
import io
import json
import os
import threading
import time
import zipfile
from datetime import datetime, timedelta
import requests
from flask import Flask, jsonify, render_template, request

# Setup template path to work across local and Vercel serverless environments
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")
HISTORY_FILE = os.path.join(BASE_DIR, "history_data.json")
MAX_HISTORY_DAYS = 5

app = Flask(__name__, template_folder=TEMPLATES_DIR)

# ---------------------------------------------------------------------------
# Shared in-memory cache & thread lock
# ---------------------------------------------------------------------------
data_cache = {
    "data": [],
    "last_updated": None,
    "error": None,
    "source": None,
    "is_fetching": False,
}

cache_lock = threading.Lock()
BG_POLL_INTERVAL = 2      # seconds between background NSE queries (local daemon)
SERVERLESS_CACHE_TTL = 2  # seconds for on-demand serverless cache TTL
_last_fetch_time = 0

NSE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "accept-language": "en-US,en;q=0.9,en-IN;q=0.8,en-GB;q=0.7",
    "cache-control": "max-age=0",
    "priority": "u=0, i",
    "sec-ch-ua": '"Microsoft Edge";v="129", "Not=A?Brand";v="8", "Chromium";v="129"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
}

NSE_JSON_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "en-US,en;q=0.9,en-IN;q=0.8",
    "referer": "https://www.nseindia.com/",
    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
    "x-requested-with": "XMLHttpRequest",
}


# ---------------------------------------------------------------------------
# 5-Day Historical Memory Manager
# ---------------------------------------------------------------------------
def load_history():
    """Load 5-day historical snapshots from disk or initialize with seed data."""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict) and "dates" in data and "snapshots" in data:
                    return data
        except Exception as e:
            print(f"[History] Error loading history: {e}")

    # Initialize empty history
    history = {"dates": [], "snapshots": {}}
    return history


def save_daily_snapshot(rows):
    """Save current day snapshot and prune any history older than MAX_HISTORY_DAYS (5 days)."""
    if not rows:
        return

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_label = datetime.now().strftime("%d %b %Y")
    today_time = datetime.now().strftime("%I:%M %p")

    try:
        history = load_history()

        # Save or update today's snapshot
        history["snapshots"][today_str] = {
            "date": today_str,
            "label": today_label,
            "timestamp": f"{today_label}, {today_time}",
            "count": len(rows),
            "data": rows,
        }

        # Keep dates list unique and sorted (newest first)
        if today_str not in history["dates"]:
            history["dates"].insert(0, today_str)

        # Sort dates descending (newest to oldest)
        history["dates"] = sorted(list(set(history["dates"])), reverse=True)

        # PRUNING: Automatically delete previous days older than MAX_HISTORY_DAYS (5 days)
        if len(history["dates"]) > MAX_HISTORY_DAYS:
            dates_to_keep = history["dates"][:MAX_HISTORY_DAYS]
            dates_to_remove = history["dates"][MAX_HISTORY_DAYS:]

            for old_date in dates_to_remove:
                if old_date in history["snapshots"]:
                    del history["snapshots"][old_date]

            history["dates"] = dates_to_keep

        # Write to JSON file
        with open(HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)

    except Exception as e:
        print(f"[History] Error saving snapshot: {e}")


# ---------------------------------------------------------------------------
# NSE Data Fetcher (Live)
# ---------------------------------------------------------------------------
def fetch_oi_spurts():
    """Fetch OI Spurts data from NSE India."""
    try:
        session = requests.Session()
        session.get("https://www.nseindia.com", headers=NSE_HEADERS, timeout=8)
        session.get("https://www.nseindia.com/option-chain", headers=NSE_HEADERS, timeout=8)

        resp = session.get(
            "https://www.nseindia.com/api/live-analysis-oi-spurts-underlyings",
            headers=NSE_HEADERS,
            timeout=10,
        )
        raw = resp.json()

        if isinstance(raw, dict) and "data" in raw:
            rows = raw["data"]
            normalised = []
            for item in rows:
                row = {
                    "symbol": item.get("symbol", ""),
                    "latestOI": _num(item.get("latestOI", 0)),
                    "prevOI": _num(item.get("prevOI", 0)),
                    "changeInOI": _num(item.get("changeInOI", 0)),
                    "pChangeInOI": _num(item.get("avgInOI", 0)),
                    "volume": _num(item.get("volume", 0)),
                    "futValue": _num(item.get("futValue", 0)),
                    "optValue": _num(item.get("optValue", 0)),
                    "totalValue": _num(item.get("total", 0)),
                    "premValue": _num(item.get("premValue", 0)),
                    "underlyingValue": _num(item.get("underlyingValue", 0)),
                }
                normalised.append(row)

            # Save into 5-day memory snapshot
            save_daily_snapshot(normalised)

            return normalised
        return None
    except Exception as exc:
        print(f"[NSE Fetch Error]: {exc}")
        return None


def _num(val):
    """Safely convert a value to float."""
    if val is None:
        return 0
    if isinstance(val, (int, float)):
        return val
    try:
        return float(str(val).replace(",", ""))
    except (ValueError, TypeError):
        return 0


# ---------------------------------------------------------------------------
# NSE Historical Bhavcopy Fetcher (Accurate real archive data - UDiFF format)
# ---------------------------------------------------------------------------
def _get_prev_trading_day(date_obj):
    """Get the previous trading day (skip Sat/Sun, no holiday check)."""
    prev = date_obj - timedelta(days=1)
    while prev.weekday() >= 5:  # 5=Sat, 6=Sun
        prev -= timedelta(days=1)
    return prev


def _bhav_url_udiff(date_obj):
    """Build NSE UDiFF format Bhavcopy URL (works for 2024 and later)."""
    date_str = date_obj.strftime("%Y%m%d")
    return f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{date_str}_F_0000.csv.zip"


def _download_and_parse_bhavcopy(date_obj, session):
    """
    Download the NSE F&O Bhavcopy ZIP (UDiFF format) for the given date and parse it.
    Returns a dict: { symbol: { latestOI, chgInOI, volume, underlyingValue, futValue } }
    Raises ValueError with a human-readable message if data unavailable.
    """
    url = _bhav_url_udiff(date_obj)
    date_str = date_obj.strftime("%d-%b-%Y")
    print(f"[Bhavcopy] Fetching: {url}")

    resp = session.get(url, timeout=25)
    if resp.status_code == 403:
        raise ValueError(f"NSE returned 403 Forbidden for {date_str}. Access restricted.")
    if resp.status_code == 404:
        raise ValueError(f"No Bhavcopy found for {date_str}. This may be a market holiday or the date is too recent.")
    if resp.status_code != 200:
        raise ValueError(f"NSE server returned HTTP {resp.status_code} for {date_str}.")

    # Unzip and read CSV
    try:
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        csv_name = [n for n in zf.namelist() if n.lower().endswith('.csv')]
        if not csv_name:
            raise ValueError(f"No CSV found inside ZIP for {date_str}.")
        raw_csv = zf.read(csv_name[0]).decode("utf-8", errors="ignore")
    except zipfile.BadZipFile:
        raise ValueError(f"Corrupt or invalid ZIP file for {date_str}.")

    reader = csv.DictReader(io.StringIO(raw_csv))

    symbol_data = {}  # symbol -> aggregated values

    for row in reader:
        # UDiFF format instrument type codes:
        # STF = Stock Futures, ITF = Index Futures
        # STO = Stock Options, ITO = Index Options
        instrument = row.get("FinInstrmTp", "").strip()
        if instrument not in ("STF", "ITF"):
            continue

        symbol = row.get("TckrSymb", "").strip()
        if not symbol:
            continue

        open_int = _num(row.get("OpnIntrst", 0))
        chg_in_oi = _num(row.get("ChngInOpnIntrst", 0))
        volume = _num(row.get("TtlTradgVol", 0))
        close = _num(row.get("ClsPric", 0))
        settle = _num(row.get("SttlmPric", 0))
        trf_val = _num(row.get("TtlTrfVal", 0))  # in lakhs
        underlying = _num(row.get("UndrlygPric", 0))

        if symbol not in symbol_data:
            symbol_data[symbol] = {
                "latestOI": 0,
                "chgInOI": 0,
                "volume": 0,
                "underlyingValue": 0,
                "futValue": 0,
            }

        symbol_data[symbol]["latestOI"] += open_int
        symbol_data[symbol]["chgInOI"] += chg_in_oi
        symbol_data[symbol]["volume"] += volume
        symbol_data[symbol]["futValue"] += trf_val
        # Keep the latest underlying price (non-zero)
        if underlying > 0:
            symbol_data[symbol]["underlyingValue"] = underlying
        elif close > 0 and symbol_data[symbol]["underlyingValue"] == 0:
            symbol_data[symbol]["underlyingValue"] = close

    return symbol_data


def fetch_historical_bhavcopy(date_str):
    """
    Fetch accurate OI Spurts data from NSE Bhavcopy archive for a specific past date.
    date_str: YYYY-MM-DD
    Returns list of row dicts (same schema as live data), or raises an error.
    Only allows dates within last 6 months (older data is not maintained).
    """
    try:
        target_date = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str}. Expected YYYY-MM-DD.")

    # Reject today or future dates
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if target_date >= today:
        raise ValueError("Calendar can only be used for past dates. Use the Live tab for today's data.")

    # Reject dates older than 6 months
    six_months_ago = today - timedelta(days=183)
    if target_date < six_months_ago:
        cutoff_label = six_months_ago.strftime("%d %b %Y")
        raise ValueError(f"Historical data is only available for the past 6 months (from {cutoff_label}). Please select a more recent date.")

    # Reject weekends
    if target_date.weekday() >= 5:
        day_name = target_date.strftime("%A")
        raise ValueError(f"{date_str} is a {day_name}. NSE is closed on weekends. Please select a weekday.")

    prev_date = _get_prev_trading_day(target_date)

    # Create a session that mimics browser (needed for NSE archive)
    session = requests.Session()
    session.headers.update({
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9,en-IN;q=0.8",
        "referer": "https://www.nseindia.com/",
    })
    # Warm-up NSE cookies
    try:
        session.get("https://www.nseindia.com", timeout=8)
    except Exception:
        pass

    # Download CURRENT day Bhavcopy
    current_data = _download_and_parse_bhavcopy(target_date, session)
    if not current_data:
        raise ValueError(f"No futures data found in Bhavcopy for {date_str}.")

    # Download PREVIOUS trading day Bhavcopy for accurate prevOI
    prev_data = {}
    try:
        prev_data = _download_and_parse_bhavcopy(prev_date, session)
    except Exception as e:
        print(f"[Bhavcopy] Could not fetch prev day ({prev_date.strftime('%Y-%m-%d')}): {e}")
        # Continue — prevOI will be derived from ChngInOpnIntrst inside current bhavcopy

    result = []
    for symbol, cur in current_data.items():
        latest_oi = cur["latestOI"]
        chg_in_oi = cur["chgInOI"]

        # Best prevOI source: prior day bhavcopy latestOI
        if symbol in prev_data and prev_data[symbol]["latestOI"] > 0:
            prev_oi = prev_data[symbol]["latestOI"]
            change_in_oi = latest_oi - prev_oi
        else:
            # Fallback: derive from ChngInOpnIntrst embedded in current bhavcopy
            change_in_oi = chg_in_oi
            prev_oi = latest_oi - chg_in_oi

        if prev_oi > 0:
            p_change = (change_in_oi / prev_oi) * 100
        elif latest_oi > 0:
            p_change = 100.0  # new position opened
        else:
            p_change = 0.0

        result.append({
            "symbol": symbol,
            "latestOI": latest_oi,
            "prevOI": max(prev_oi, 0),
            "changeInOI": change_in_oi,
            "pChangeInOI": round(p_change, 2),
            "volume": cur["volume"],
            "futValue": round(cur["futValue"], 2),
            "optValue": 0,
            "totalValue": round(cur["futValue"], 2),
            "premValue": 0,
            "underlyingValue": cur["underlyingValue"],
        })

    # Sort by absolute % OI change descending (same as NSE live API)
    result.sort(key=lambda x: abs(x["pChangeInOI"]), reverse=True)
    return result


# ---------------------------------------------------------------------------
# Background Worker Thread (Continuous Live Market Polling for Local/Dedicated)
# ---------------------------------------------------------------------------
def background_updater():
    """Continuously fetches NSE data in the background so API calls respond instantly."""
    print("[Background Worker] Started background live market poller...")
    while True:
        try:
            with cache_lock:
                data_cache["is_fetching"] = True

            rows = fetch_oi_spurts()

            with cache_lock:
                if rows:
                    data_cache["data"] = rows
                    data_cache["last_updated"] = datetime.now().strftime("%d %b %Y, %I:%M:%S %p")
                    data_cache["error"] = None
                    data_cache["source"] = "live"
                else:
                    if not data_cache["data"]:
                        data_cache["source"] = "unavailable"
                        data_cache["error"] = "Could not fetch data from NSE. Market may be closed."
                data_cache["is_fetching"] = False

        except Exception as exc:
            with cache_lock:
                data_cache["is_fetching"] = False
                data_cache["error"] = f"Worker Error: {exc}"

        time.sleep(BG_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/oi-spurts")
@app.route("/api/refresh")
def get_data():
    """Return latest data from cache, or fetch on-demand if running in serverless."""
    global _last_fetch_time
    now = time.time()

    # On Vercel serverless where background threads don't persist, fetch on-demand if cache is stale
    if not data_cache["data"] or (now - _last_fetch_time > SERVERLESS_CACHE_TTL):
        rows = fetch_oi_spurts()
        with cache_lock:
            if rows:
                data_cache["data"] = rows
                data_cache["last_updated"] = datetime.now().strftime("%d %b %Y, %I:%M:%S %p")
                data_cache["error"] = None
                data_cache["source"] = "live"
                _last_fetch_time = now
            elif not data_cache["data"]:
                data_cache["source"] = "unavailable"
                data_cache["error"] = "Could not fetch data from NSE. Market may be closed."

    with cache_lock:
        return jsonify(data_cache)


@app.route("/api/history")
def get_history_dates():
    """Return list of available historical dates (up to 5 days)."""
    history = load_history()
    result = []
    for d in history.get("dates", []):
        snap = history.get("snapshots", {}).get(d, {})
        result.append({
            "date": d,
            "label": snap.get("label", d),
            "timestamp": snap.get("timestamp", d),
            "count": snap.get("count", 0),
        })
    return jsonify({"dates": result, "max_days": MAX_HISTORY_DAYS})


@app.route("/api/history/<date_str>")
def get_history_snapshot(date_str):
    """Return snapshot data for a specific historical date."""
    history = load_history()
    if date_str in history.get("snapshots", {}):
        return jsonify(history["snapshots"][date_str])
    return jsonify({"error": f"No historical snapshot found for {date_str}"}), 404


@app.route("/api/historical/<date_str>")
def get_historical_bhavcopy(date_str):
    """
    Fetch real NSE Bhavcopy data for any past trading date.
    date_str: YYYY-MM-DD format.
    Returns accurate OI Spurts data directly from NSE F&O archives.
    """
    try:
        rows = fetch_historical_bhavcopy(date_str)
        if not rows:
            return jsonify({"error": f"No futures data found for {date_str}. This may be a market holiday."}), 404

        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            label = dt.strftime("%d %b %Y")
            weekday = dt.strftime("%A")
        except Exception:
            label = date_str
            weekday = ""

        return jsonify({
            "date": date_str,
            "label": label,
            "weekday": weekday,
            "source": "nse_bhavcopy_archive",
            "count": len(rows),
            "data": rows,
        })

    except ValueError as ve:
        return jsonify({"error": str(ve)}), 400
    except Exception as exc:
        print(f"[Historical Route Error]: {exc}")
        return jsonify({"error": f"Failed to fetch historical data: {str(exc)}"}), 500


# ---------------------------------------------------------------------------
# Main (Local Execution)
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("[Server] Starting NSE OI Spurts Dashboard...")

    # Start background polling thread for instant local performance
    worker_thread = threading.Thread(target=background_updater, daemon=True)
    worker_thread.start()

    print("[Server] Dashboard ready at http://localhost:5000")
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True, use_reloader=False)
