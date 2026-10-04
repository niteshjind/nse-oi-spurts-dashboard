"""Composite Rotation Score (0-100) = RRG quadrant + RS percentile + trend + breadth."""

from config import COMPOSITE_WEIGHTS, RRG_SCORE_MAP


def compute_rotation_score(rrg_result, rs_result, trend_result, sector_breadth):
    rrg_score = 50
    if rrg_result:
        rrg_score = RRG_SCORE_MAP.get((rrg_result["quadrant"], rrg_result["direction"]), 50)

    rs_percentile = 50
    if rs_result and "percentile" in rs_result:
        rs_percentile = rs_result["percentile"]

    trend_score = 50
    if trend_result:
        trend_score = trend_result["trend_score"]

    breadth_score = 50
    if sector_breadth:
        breadth_score = sector_breadth.get("breadth_200", 50)

    w = COMPOSITE_WEIGHTS
    rotation_score = (
        rrg_score * w["rrg"]
        + rs_percentile * w["rs_rank"]
        + trend_score * w["trend"]
        + breadth_score * w["breadth"]
    )
    return round(rotation_score, 1)


def build_stock_profile(symbol, sector, rrg_result, rs_result, trend_result, sector_breadth):
    rotation_score = compute_rotation_score(rrg_result, rs_result, trend_result, sector_breadth)
    return {
        "symbol": symbol,
        "sector": sector,
        "rotation_score": rotation_score,
        "quadrant": rrg_result["quadrant"] if rrg_result else "N/A",
        "direction": rrg_result["direction"] if rrg_result else "N/A",
        "rs_ratio": rrg_result["rs_ratio"] if rrg_result else None,
        "rs_momentum": rrg_result["rs_momentum"] if rrg_result else None,
        "rs_percentile": rs_result["percentile"] if rs_result and "percentile" in rs_result else None,
        "above_200dma": trend_result["above_200dma"] if trend_result else None,
        "pct_from_dma": trend_result["pct_from_dma"] if trend_result else None,
        "sector_breadth_200": sector_breadth.get("breadth_200") if sector_breadth else None,
        "breadth_signal": sector_breadth.get("breadth_signal") if sector_breadth else None,
    }
