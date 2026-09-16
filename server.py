"""
NSE OI Spurts Dashboard - Flask Backend
Works seamlessly both on local machines and deployed on Vercel serverless.
"""

import os
import threading
import time
from datetime import datetime
import requests
from flask import Flask, jsonify, render_template, request

# Setup template path to work across local and Vercel serverless environments
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

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


# ---------------------------------------------------------------------------
# NSE Data Fetcher
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
