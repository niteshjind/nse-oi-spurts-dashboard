"""Relative Rotation Graph (RRG) — JdK RS-Ratio & RS-Momentum.

Both series are normalised (z-scored) around 100 so that >100 = outperforming the
benchmark and >100 momentum = that outperformance is improving. The (ratio, momentum)
pair places a security in one of four quadrants:

    Leading    ratio>100, momentum>100
    Improving  ratio<100, momentum>100
    Weakening  ratio>100, momentum<100
    Lagging    ratio<100, momentum<100
"""

import pandas as pd
import numpy as np


def calculate_rrg(stock_close, benchmark_close, sma_window=14, roc_lookback=52, tail_length=8):
    if stock_close is None or benchmark_close is None:
        return None
    if stock_close.empty or benchmark_close.empty:
        return None

    aligned = pd.DataFrame({"stock": stock_close, "bench": benchmark_close}).dropna()
    # Minimum points to yield ≥1 valid RS-Momentum value: rs_ratio loses (sma_window-1) to
    # its rolling mean, roc loses roc_lookback to the shift, rs_momentum loses another
    # (sma_window-1). +3 leaves a small margin (and ≥2 points for the direction read).
    min_needed = 2 * sma_window + roc_lookback + 3
    if len(aligned) < min_needed:
        return None

    raw_rs = (aligned["stock"] / aligned["bench"]) * 100

    rs_mean = raw_rs.rolling(window=sma_window).mean()
    rs_std = raw_rs.rolling(window=sma_window).std().replace(0, np.nan)
    rs_ratio = 100 + (raw_rs - rs_mean) / rs_std

    roc = rs_ratio / rs_ratio.shift(roc_lookback)
    roc_mean = roc.rolling(window=sma_window).mean()
    roc_std = roc.rolling(window=sma_window).std().replace(0, np.nan)
    rs_momentum = 100 + (roc - roc_mean) / roc_std

    result = pd.DataFrame({
        "rs_ratio": rs_ratio,
        "rs_momentum": rs_momentum,
    }).dropna()

    if result.empty:
        return None

    latest = result.iloc[-1]
    ratio_val = latest["rs_ratio"]
    momentum_val = latest["rs_momentum"]

    if ratio_val > 100 and momentum_val > 100:
        quadrant = "Leading"
    elif ratio_val < 100 and momentum_val > 100:
        quadrant = "Improving"
    elif ratio_val > 100 and momentum_val < 100:
        quadrant = "Weakening"
    else:
        quadrant = "Lagging"

    tail = result.tail(tail_length)[["rs_ratio", "rs_momentum"]].values.tolist()

    if len(tail) >= 2:
        direction = "accelerating" if tail[-1][1] > tail[-2][1] else "decelerating"
    else:
        direction = "accelerating"

    return {
        "rs_ratio": round(float(ratio_val), 2),
        "rs_momentum": round(float(momentum_val), 2),
        "quadrant": quadrant,
        "direction": direction,
        "tail": tail,
    }


def resample_close(df, freq="W"):
    """Resample a daily OHLCV frame (or close Series) to the timeframe's frequency.

    "2QE" is treated as a calendar-anchored *semi-annual* grid (June & December
    quarter-ends). A naive "6ME"/"2QE" resample anchors its bins to each series' own
    start, so series that begin in different months/quarters don't share timestamps and
    fail to align with the benchmark. Snapping to QE then keeping Jun/Dec ends gives an
    identical half-year grid for every series.
    """
    if df is None:
        return pd.Series(dtype=float)
    close = df["Close"] if "Close" in getattr(df, "columns", []) else df
    if freq == "D" or freq is None:
        return close
    if freq == "2QE":
        q = close.resample("QE").last().dropna()
        return q[q.index.month.isin([6, 12])]
    return close.resample(freq).last().dropna()
