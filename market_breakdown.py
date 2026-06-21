"""Plain-English read-only market breakdown helpers.

This module only transforms already-fetched market-data rows. It does not call
external APIs, persist data, create alerts, or make decisions for the user.
"""

from __future__ import annotations

import re
from typing import Any, Optional


NEAR_LEVEL_PCT = 0.01
EXTENDED_EMA9_PCT = 0.03
FLAT_MACD_ABS = 0.0001


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _first_float(row: dict[str, Any], keys: list[str]) -> Optional[float]:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return value
    return None


def _price(row: dict[str, Any]) -> Optional[float]:
    return _first_float(row, ["price", "close", "last"])


def _format_number(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return "unknown"
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def parse_watchlist_text(text: str, limit: int = 20) -> list[str]:
    """Parse comma, newline, or space-separated ticker text."""

    tickers: list[str] = []
    seen: set[str] = set()
    for chunk in re.split(r"[\s,;]+", str(text or "")):
        ticker = chunk.strip().upper()
        if not ticker:
            continue
        ticker = re.sub(r"[^A-Z0-9.\-!]", "", ticker)
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        tickers.append(ticker)
        if len(tickers) >= limit:
            break
    return tickers


def latest_bar_from_bars(bars: list[dict[str, Any]]) -> dict[str, Any]:
    if not bars:
        return {}
    for bar in reversed(bars):
        if isinstance(bar, dict):
            return dict(bar)
    return {}


def compute_recent_levels_from_bars(bars: list[dict[str, Any]]) -> dict[str, Any]:
    lows: list[float] = []
    highs: list[float] = []
    for bar in bars[-60:]:
        low = _first_float(bar, ["low", "l"])
        high = _first_float(bar, ["high", "h"])
        if low is not None:
            lows.append(low)
        if high is not None:
            highs.append(high)

    recent_lows = lows[-20:]
    wider_lows = lows[-50:]
    recent_highs = highs[-20:]
    wider_highs = highs[-50:]
    return {
        "support1": min(recent_lows) if recent_lows else "",
        "support2": min(wider_lows) if wider_lows else "",
        "resistance1": max(recent_highs) if recent_highs else "",
        "resistance2": max(wider_highs) if wider_highs else "",
    }


def breakdown_trend(row: dict[str, Any]) -> dict[str, Any]:
    price = _price(row)
    ema9 = _first_float(row, ["ema9", "ema_9"])
    ema21 = _first_float(row, ["ema21", "ema_21"])
    sma200 = _first_float(row, ["sma200", "sma_200"])
    values = {"EMA9": ema9, "EMA21": ema21, "SMA200": sma200}
    available = {name: value for name, value in values.items() if value is not None}
    if price is None or not available:
        return {
            "status": "unknown",
            "summary": "Trend needs manual confirmation because price or moving averages are missing.",
        }
    above = [name for name, value in available.items() if price > value]
    below = [name for name, value in available.items() if price < value]
    if len(above) == len(available):
        status = "bullish"
        summary = "Price is above " + ", ".join(above) + "."
    elif len(below) == len(available):
        status = "bearish"
        summary = "Price is below " + ", ".join(below) + "."
    else:
        status = "mixed"
        summary = f"Trend is mixed: above {', '.join(above) or 'none'}; below {', '.join(below) or 'none'}."
    return {"status": status, "summary": summary}


def breakdown_momentum(row: dict[str, Any]) -> dict[str, Any]:
    macd = _first_float(row, ["macd_hist", "macd_histogram"])
    if macd is None:
        return {"status": "unknown", "summary": "Momentum is unknown because MACD histogram is missing."}
    if abs(macd) <= FLAT_MACD_ABS:
        return {"status": "flat", "summary": "MACD histogram is flat, so momentum needs confirmation."}
    if macd > 0:
        return {"status": "positive", "summary": "MACD histogram is positive."}
    return {"status": "negative", "summary": "MACD histogram is negative, so momentum needs confirmation."}


def breakdown_level_context(row: dict[str, Any]) -> dict[str, Any]:
    price = _price(row)
    support = _first_float(row, ["support1", "support_1"])
    resistance = _first_float(row, ["resistance1", "resistance_1"])
    bars = row.get("bars")
    if (support is None or resistance is None) and isinstance(bars, list):
        levels = compute_recent_levels_from_bars([bar for bar in bars if isinstance(bar, dict)])
        support = support if support is not None else _to_float(levels.get("support1"))
        resistance = resistance if resistance is not None else _to_float(levels.get("resistance1"))

    near_support = False
    near_resistance = False
    if price is not None and support not in (None, 0):
        near_support = abs(price - support) / abs(support) <= NEAR_LEVEL_PCT
    if price is not None and resistance not in (None, 0):
        near_resistance = abs(price - resistance) / abs(resistance) <= NEAR_LEVEL_PCT

    if price is None:
        summary = "Level context needs manual confirmation because price is missing."
    elif support is None and resistance is None:
        summary = "Support and resistance are missing; mark levels manually on the chart."
    else:
        parts = []
        if support is not None:
            parts.append(f"support near {_format_number(support)}")
        if resistance is not None:
            parts.append(f"resistance near {_format_number(resistance)}")
        summary = "Level context: " + " / ".join(parts) + "."
        if near_resistance:
            summary += " Price is near resistance."
        if near_support:
            summary += " Price is near support."

    return {
        "status": "near_resistance" if near_resistance else "near_support" if near_support else "ok",
        "summary": summary,
        "support": _format_number(support),
        "resistance": _format_number(resistance),
        "near_support": near_support,
        "near_resistance": near_resistance,
    }


def breakdown_risk_flags(row: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    price = _price(row)
    ema9 = _first_float(row, ["ema9", "ema_9"])
    sma200 = _first_float(row, ["sma200", "sma_200"])
    macd = _first_float(row, ["macd_hist", "macd_histogram"])
    levels = breakdown_level_context(row)

    if price is None:
        flags.append("missing data")
    if levels.get("near_resistance"):
        flags.append("near resistance")
    if levels.get("near_support"):
        flags.append("near support")
    if price is not None and ema9 not in (None, 0) and (price - ema9) / abs(ema9) > EXTENDED_EMA9_PCT:
        flags.append("extended above EMA9")
    if price is not None and sma200 is not None and price < sma200:
        flags.append("below SMA200")
    if macd is None:
        flags.append("missing data")
    elif macd < -FLAT_MACD_ABS:
        flags.append("negative MACD")

    deduped: list[str] = []
    for flag in flags:
        if flag not in deduped:
            deduped.append(flag)
    return deduped


def market_breakdown_bias(row: dict[str, Any]) -> str:
    trend = breakdown_trend(row)["status"]
    momentum = breakdown_momentum(row)["status"]
    if trend == "bullish" and momentum != "negative":
        return "bullish"
    if trend == "bearish" and momentum != "positive":
        return "bearish"
    if trend in {"unknown", "mixed"}:
        return "neutral/mixed"
    return "neutral/mixed"


def market_breakdown_confidence(row: dict[str, Any]) -> str:
    trend = breakdown_trend(row)["status"]
    momentum = breakdown_momentum(row)["status"]
    levels = breakdown_level_context(row)
    flags = breakdown_risk_flags(row)
    if trend in {"unknown", "mixed"} or "missing data" in flags:
        return "C"
    if trend == "bullish" and momentum == "positive" and not levels.get("near_resistance"):
        return "A"
    if trend == "bearish" and momentum == "negative" and not levels.get("near_support"):
        return "A"
    if trend in {"bullish", "bearish"} and len(flags) <= 1:
        return "B"
    return "C"


def market_breakdown_explanation(row: dict[str, Any]) -> list[str]:
    trend = breakdown_trend(row)
    momentum = breakdown_momentum(row)
    levels = breakdown_level_context(row)
    flags = breakdown_risk_flags(row)
    bullets = [trend["summary"], momentum["summary"], levels["summary"]]
    if "near resistance" in flags:
        bullets.append("Price is near resistance, so chase risk may be higher.")
    if "near support" in flags:
        bullets.append("Price is near support, so breakdown risk needs attention.")
    if "extended above EMA9" in flags:
        bullets.append("Price is extended above EMA9, so wait for confirmation or a cleaner setup.")
    if "below SMA200" in flags:
        bullets.append("Price is below SMA200, so longer-term trend context is weaker.")
    if "missing data" in flags:
        bullets.append("Some data is missing; use manual chart confirmation before relying on this row.")
    bullets.append("Manual chart confirmation is still required.")
    return bullets


def market_breakdown_next_action(row: dict[str, Any]) -> str:
    flags = breakdown_risk_flags(row)
    trend = breakdown_trend(row)["status"]
    momentum = breakdown_momentum(row)["status"]
    if "missing data" in flags:
        return "Verify chart manually before any decision."
    if "near resistance" in flags:
        return "Watch for confirmation near resistance."
    if trend == "mixed" or momentum in {"negative", "unknown", "flat"}:
        return "Review trend/momentum conflict."
    return "Verify chart manually before any decision."


def build_market_breakdown_row(row: dict[str, Any]) -> dict[str, Any]:
    trend = breakdown_trend(row)
    momentum = breakdown_momentum(row)
    levels = breakdown_level_context(row)
    risk_flags = breakdown_risk_flags(row)
    result = dict(row)
    result.update(
        {
            "ticker": str(row.get("ticker", "") or "").strip().upper() or "UNKNOWN",
            "price": _format_number(_price(row)),
            "timeframe": str(row.get("timeframe", "") or "15m").strip() or "15m",
            "bias": market_breakdown_bias(row),
            "confidence": market_breakdown_confidence(row),
            "trend": trend["status"],
            "trend_summary": trend["summary"],
            "momentum": momentum["status"],
            "momentum_summary": momentum["summary"],
            "level_context": levels["status"],
            "level_summary": levels["summary"],
            "support": levels["support"],
            "resistance": levels["resistance"],
            "risk_flags": risk_flags,
            "explanation": market_breakdown_explanation(row),
            "next_action": market_breakdown_next_action(row),
        }
    )
    return result


def build_market_breakdown_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [build_market_breakdown_row(row) for row in rows if isinstance(row, dict)]


def market_breakdown_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = {
        "total": len(rows),
        "bullish": 0,
        "bearish": 0,
        "neutral_mixed": 0,
        "needs_manual_confirmation": 0,
    }
    for row in rows:
        bias = str(row.get("bias") or market_breakdown_bias(row)).strip().lower()
        if bias == "bullish":
            summary["bullish"] += 1
        elif bias == "bearish":
            summary["bearish"] += 1
        else:
            summary["neutral_mixed"] += 1
        flags = row.get("risk_flags")
        if not isinstance(flags, list):
            flags = breakdown_risk_flags(row)
        if "missing data" in flags or row.get("trend") == "unknown":
            summary["needs_manual_confirmation"] += 1
    return summary
