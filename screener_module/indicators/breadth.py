"""Sector breadth — % of constituents above their 50/200-DMA. Confirms real participation."""

import pandas as pd


def calculate_sector_breadth(stock_data_dict, dma_periods=(50, 200)):
    results = {}
    for period in dma_periods:
        above_count = 0
        total_count = 0

        for _symbol, df in stock_data_dict.items():
            close = df["Close"] if "Close" in getattr(df, "columns", []) else df
            if len(close) < period + 1:
                continue
            sma = close.rolling(window=period).mean()
            if pd.isna(sma.iloc[-1]):
                continue
            total_count += 1
            if close.iloc[-1] > sma.iloc[-1]:
                above_count += 1

        pct = round((above_count / total_count) * 100, 1) if total_count > 0 else 0
        results[f"breadth_{period}"] = pct
        results[f"above_{period}"] = above_count
        results[f"total_{period}"] = total_count

    b200 = results.get("breadth_200", 0)
    if b200 >= 70:
        results["breadth_signal"] = "Strong"
    elif b200 >= 40:
        results["breadth_signal"] = "Mixed"
    else:
        results["breadth_signal"] = "Weak"

    return results


def get_stock_breadth_detail(stock_data_dict, dma_period=200):
    details = []
    for symbol, df in stock_data_dict.items():
        close = df["Close"] if "Close" in getattr(df, "columns", []) else df
        if len(close) < dma_period + 1:
            continue
        sma = close.rolling(window=dma_period).mean()
        if pd.isna(sma.iloc[-1]):
            continue
        above = close.iloc[-1] > sma.iloc[-1]
        pct = ((close.iloc[-1] / sma.iloc[-1]) - 1) * 100
        details.append({
            "symbol": symbol,
            "above_dma": bool(above),
            "pct_from_dma": round(float(pct), 2),
        })
    return details
