"""Read-only TradingView chart review helpers.

This module normalizes manually reviewed chart context and builds copy-ready
CSV text for the existing TradingView Import bridge. It does not call external
APIs, persist data, create alerts, or execute anything.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional


CHART_REVIEW_COLUMNS = [
    "ticker",
    "timeframe",
    "price",
    "chart_bias",
    "supply_zone",
    "demand_zone",
    "support",
    "resistance",
    "breakout",
    "breakdown",
    "invalid",
    "ema9",
    "ema21",
    "wma50",
    "wma200",
    "sma200",
    "macd_hist",
    "volume_note",
    "pattern_note",
    "fundamentals_note",
    "macro_note",
    "manual_notes",
    "source",
]

TRADINGVIEW_IMPORT_COLUMNS = [
    "ticker",
    "price",
    "timeframe",
    "bias_note",
    "key_level_note",
    "ema9",
    "ema21",
    "wma50",
    "wma200",
    "sma200",
    "support1",
    "support2",
    "resistance1",
    "resistance2",
    "breakout",
    "breakdown",
    "invalid",
    "relative_volume",
    "macd_hist",
    "notes",
]

SUPPORTED_TIMEFRAMES = {"15m", "1h", "4h", "1D"}
ALLOWED_BIASES = {"bullish", "bearish", "neutral", "mixed", "unclear"}
EXECUTION_TIMEFRAME_PRIORITY = ("15m", "1h", "4h", "1D")


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_ticker(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9.\-!]", "", _clean(value).upper())
    return text or "UNKNOWN"


def _normal_timeframe(value: Any) -> str:
    text = _clean(value) or "15m"
    lowered = text.lower()
    mapping = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1D"}
    return mapping.get(lowered, text if text in SUPPORTED_TIMEFRAMES else "15m")


def _normal_bias(value: Any) -> str:
    text = _clean(value).lower()
    if text in ALLOWED_BIASES:
        return text
    return "unclear"


def _first_number_text(value: Any) -> str:
    text = _clean(value).replace(",", "")
    if not text:
        return ""
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return match.group(0) if match else ""


def normalize_chart_review_row(row: dict[str, Any]) -> dict[str, str]:
    normalized = {column: _clean(row.get(column, "")) for column in CHART_REVIEW_COLUMNS}
    normalized["ticker"] = _clean_ticker(normalized.get("ticker"))
    normalized["timeframe"] = _normal_timeframe(normalized.get("timeframe"))
    normalized["chart_bias"] = _normal_bias(normalized.get("chart_bias"))
    normalized["source"] = normalized.get("source") or "manual_chart_review"
    return normalized


def chart_review_template_csv(tickers: Optional[list[str]] = None) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CHART_REVIEW_COLUMNS, lineterminator="\n")
    writer.writeheader()
    rows = tickers or ["SPY"]
    for ticker in rows[:20]:
        writer.writerow(
            normalize_chart_review_row(
                {
                    "ticker": ticker,
                    "timeframe": "15m",
                    "chart_bias": "unclear",
                    "manual_notes": "manual chart confirmation required; no orders",
                    "source": "manual_chart_review",
                }
            )
        )
    return output.getvalue()


def parse_chart_review_csv(text: str) -> tuple[list[dict[str, str]], list[str]]:
    clean_text = _clean(text)
    if not clean_text:
        return [], ["Paste chart review CSV first."]
    reader = csv.DictReader(io.StringIO(clean_text))
    fieldnames = reader.fieldnames or []
    errors: list[str] = []
    missing = [column for column in ["ticker", "timeframe", "chart_bias"] if column not in fieldnames]
    if missing:
        errors.append("Missing required chart review columns: " + ", ".join(missing))
    rows: list[dict[str, str]] = []
    for index, raw_row in enumerate(reader, start=2):
        row = normalize_chart_review_row(dict(raw_row))
        if row["ticker"] == "UNKNOWN":
            errors.append(f"Row {index}: ticker is required.")
            continue
        rows.append(row)
    return rows, errors


def _key_level_note(row: dict[str, Any]) -> str:
    parts = []
    for label, key in [
        ("support", "support"),
        ("resistance", "resistance"),
        ("demand", "demand_zone"),
        ("supply", "supply_zone"),
        ("breakout", "breakout"),
        ("breakdown", "breakdown"),
        ("invalid", "invalid"),
    ]:
        value = _clean(row.get(key))
        if value:
            parts.append(f"{label} {value}")
    return " / ".join(parts) if parts else "manual levels required"


def _chart_notes(row: dict[str, Any]) -> str:
    parts = ["CHART REVIEW ONLY; verify manually; no orders"]
    for key in ["volume_note", "pattern_note", "fundamentals_note", "macro_note", "manual_notes"]:
        value = _clean(row.get(key))
        if value:
            parts.append(value)
    return " | ".join(parts)


def chart_review_to_import_row(row: dict[str, Any]) -> dict[str, str]:
    normalized = normalize_chart_review_row(row)
    support = _first_number_text(normalized.get("support")) or _first_number_text(normalized.get("demand_zone"))
    resistance = _first_number_text(normalized.get("resistance")) or _first_number_text(normalized.get("supply_zone"))
    breakout = _first_number_text(normalized.get("breakout")) or resistance
    breakdown = _first_number_text(normalized.get("breakdown")) or support
    invalid = _first_number_text(normalized.get("invalid")) or support
    return {
        "ticker": normalized["ticker"],
        "price": _first_number_text(normalized.get("price")),
        "timeframe": normalized["timeframe"],
        "bias_note": normalized["chart_bias"],
        "key_level_note": _key_level_note(normalized),
        "ema9": _first_number_text(normalized.get("ema9")),
        "ema21": _first_number_text(normalized.get("ema21")),
        "wma50": _first_number_text(normalized.get("wma50")),
        "wma200": _first_number_text(normalized.get("wma200")),
        "sma200": _first_number_text(normalized.get("sma200")),
        "support1": support,
        "support2": "",
        "resistance1": resistance,
        "resistance2": "",
        "breakout": breakout,
        "breakdown": breakdown,
        "invalid": invalid,
        "relative_volume": "",
        "macd_hist": _first_number_text(normalized.get("macd_hist")),
        "notes": _chart_notes(normalized),
    }


def chart_review_execution_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Return one execution/review row per ticker for the import bridge.

    Chart Workspace can hold multi-timeframe context, but Daily Review and
    Calibration Results are ticker-level workflows. Prefer the 15m row when it
    exists, then fall back to higher context timeframes.
    """

    ordered_tickers: list[str] = []
    grouped: dict[str, dict[str, dict[str, str]]] = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = normalize_chart_review_row(raw)
        ticker = row["ticker"]
        if ticker not in grouped:
            grouped[ticker] = {}
            ordered_tickers.append(ticker)
        grouped[ticker][row["timeframe"]] = row

    selected: list[dict[str, str]] = []
    for ticker in ordered_tickers:
        timeframe_rows = grouped[ticker]
        for timeframe in EXECUTION_TIMEFRAME_PRIORITY:
            if timeframe in timeframe_rows:
                selected.append(timeframe_rows[timeframe])
                break
        else:
            selected.append(next(iter(timeframe_rows.values())))
    return selected


def chart_review_rows_to_import_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [chart_review_to_import_row(row) for row in chart_review_execution_rows(rows)]


def chart_review_rows_to_tradingview_import_csv(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=TRADINGVIEW_IMPORT_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in chart_review_rows_to_import_rows(rows):
        writer.writerow(row)
    return output.getvalue()


def chart_review_timeframe_summary(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    summary: dict[tuple[str, str], dict[str, str]] = {}
    for raw in rows:
        row = normalize_chart_review_row(raw)
        key = (row["ticker"], row["timeframe"])
        summary[key] = {
            "ticker": row["ticker"],
            "timeframe": row["timeframe"],
            "chart_bias": row["chart_bias"],
            "support": row.get("support") or row.get("demand_zone") or "manual",
            "resistance": row.get("resistance") or row.get("supply_zone") or "manual",
            "manual_notes": row.get("manual_notes") or "manual confirmation required",
        }
    return list(summary.values())


def chart_review_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    result = {
        "total": 0,
        "bullish": 0,
        "bearish": 0,
        "neutral_or_mixed": 0,
        "unclear": 0,
        "missing_levels": 0,
    }
    for raw in rows:
        row = normalize_chart_review_row(raw)
        result["total"] += 1
        bias = row["chart_bias"]
        if bias == "bullish":
            result["bullish"] += 1
        elif bias == "bearish":
            result["bearish"] += 1
        elif bias in {"neutral", "mixed"}:
            result["neutral_or_mixed"] += 1
        else:
            result["unclear"] += 1
        if not (row.get("support") or row.get("demand_zone")) or not (row.get("resistance") or row.get("supply_zone")):
            result["missing_levels"] += 1
    return result
