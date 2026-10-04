"""Absolute trend filter — the 200-DMA check that kills the "leading-in-a-downtrend" trap."""

import pandas as pd


def check_trend(close_series, dma_period=200):
    if close_series is None or len(close_series) < dma_period + 1:
        return None

    sma = close_series.rolling(window=dma_period).mean()
    current_price = close_series.iloc[-1]
    current_sma = sma.iloc[-1]

    if pd.isna(current_sma) or current_sma == 0:
        return None

    pct_from_dma = ((current_price / current_sma) - 1) * 100
    above = bool(current_price > current_sma)

    return {
        "above_200dma": above,
        "price": round(float(current_price), 2),
        "dma_200": round(float(current_sma), 2),
        "pct_from_dma": round(float(pct_from_dma), 2),
        "near_dma": abs(pct_from_dma) <= 2.0,
        "trend_score": 100 if above else 0,
    }
