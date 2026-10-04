#!/usr/bin/env python3
"""Refresh the Nifty 500 constituents CSV used to build the screener universe.

Downloads the official list from niftyindices.com and overwrites
`data/nifty500_constituents.csv` — but only after verifying the download is a real
CSV (niftyindices sometimes serves an HTML block page when rate-limiting). The existing
file is never clobbered with garbage.

The screener groups these by the CSV's "Industry" column into sectors
(see config._load_universe).

Usage:
    python data/refresh_universe.py
"""

import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import urllib.request

URL = "https://niftyindices.com/IndexConstituent/ind_nifty500list.csv"
DEST = os.path.join(os.path.dirname(os.path.abspath(__file__)), "nifty500_constituents.csv")
_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def _looks_like_csv(text):
    """A valid constituents file starts with the expected header, not HTML."""
    head = text.lstrip()[:200].lower()
    if head.startswith("<") or "<!doctype" in head or "<html" in head:
        return False
    first_line = text.lstrip().splitlines()[0] if text.strip() else ""
    return "symbol" in first_line.lower() and "industry" in first_line.lower()


def _via_curl():
    """curl uses the system cert store and tends to get through where urllib stalls."""
    try:
        out = subprocess.run(
            ["curl", "-sL", "--max-time", "60", "-A", _UA, URL],
            capture_output=True, timeout=70,
        )
        if out.returncode == 0 and out.stdout:
            return out.stdout.decode("utf-8-sig", errors="replace")
    except Exception as exc:
        print(f"  curl attempt failed: {exc}", file=sys.stderr)
    return None


def _via_urllib():
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except Exception:
        ctx = ssl.create_default_context()
    headers = {"User-Agent": _UA, "Accept": "text/csv,*/*", "Accept-Language": "en-US,en;q=0.9"}
    req = urllib.request.Request(URL, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60, context=ctx) as resp:
            return resp.read().decode("utf-8-sig", errors="replace")
    except Exception as exc:
        print(f"  urllib attempt failed: {exc}", file=sys.stderr)
    return None


def main():
    text = None
    for fetch in (_via_curl, _via_urllib):
        candidate = fetch()
        if candidate and _looks_like_csv(candidate):
            text = candidate
            break
        if candidate:
            print("  got a response but it wasn't a CSV (likely an HTML block page); "
                  "trying next method...", file=sys.stderr)

    if text is None:
        print("Refresh failed: could not download a valid Nifty 500 CSV.", file=sys.stderr)
        print("  niftyindices.com is likely rate-limiting. Existing file left untouched.",
              file=sys.stderr)
        print("  Try again later, or manually save ind_nifty500list.csv to:", file=sys.stderr)
        print(f"    {DEST}", file=sys.stderr)
        return 1

    lines = [ln for ln in text.splitlines() if ln.strip()]
    # Write to a temp file first, then atomically replace — never clobber on a bad write.
    fd, tmp = tempfile.mkstemp(suffix=".csv", dir=os.path.dirname(DEST))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write("\n".join(lines) + "\n")
        shutil.move(tmp, DEST)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)

    print(f"Wrote {len(lines) - 1} constituents to {DEST}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
