"""Session-only TradingView alert planning helpers.

This module turns manual chart review rows or read-only market breakdown rows
into draft alert plans. It does not call external APIs, persist data, create
live alerts, or execute anything.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Optional


ALERT_PLAN_COLUMNS = [
    "ticker",
    "timeframe",
    "setup_bias",
    "setup_type",
    "trigger_level",
    "trigger_condition",
    "support",
    "resistance",
    "demand_zone",
    "supply_zone",
    "invalidation_level",
    "target_1",
    "target_2",
    "risk_reward_note",
    "confidence",
    "chart_confirmation",
    "fundamentals_context",
    "macro_context",
    "news_catalyst",
    "sector_context",
    "earnings_risk",
    "rates_context",
    "index_context",
    "risk_environment",
    "volume_confirmation",
    "alert_message",
    "manual_notes",
    "status",
]

ALLOWED_SETUP_BIASES = {"bullish", "bearish", "neutral", "mixed", "unclear"}
ALLOWED_SETUP_TYPES = {
    "breakout",
    "breakdown",
    "pullback",
    "reversal",
    "continuation",
    "watch_only",
    "unclear",
}
ALLOWED_STATUSES = {
    "draft",
    "needs_chart_confirmation",
    "ready_for_manual_alert",
    "active_manual_only",
    "rejected",
}
CONFIRMED_CHART_VALUES = {"yes", "confirmed", "chart confirmed", "manual confirmed"}
SAFETY_SUFFIX = "Decision support only. Verify chart manually. No orders."


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _clean_ticker(value: Any) -> str:
    text = re.sub(r"[^A-Za-z0-9.\-!]", "", _clean(value).upper())
    return text or "UNKNOWN"


def _normal_timeframe(value: Any) -> str:
    text = _clean(value) or "15m"
    lowered = text.lower()
    mapping = {"15m": "15m", "1h": "1h", "4h": "4h", "1d": "1D"}
    return mapping.get(lowered, text)


def _normal_choice(value: Any, allowed: set[str], fallback: str) -> str:
    text = _clean(value).lower().replace(" ", "_")
    return text if text in allowed else fallback


def _first_number_text(*values: Any) -> str:
    for value in values:
        text = _clean(value).replace(",", "")
        if not text:
            continue
        match = re.search(r"-?\d+(?:\.\d+)?", text)
        if match:
            return match.group(0)
    return ""


def _confirmed(value: Any) -> bool:
    return _clean(value).lower() in CONFIRMED_CHART_VALUES


def _invalidation_for_bias(row: dict[str, Any], bias: str) -> str:
    if _clean(row.get("invalidation_level")):
        return _clean(row.get("invalidation_level"))
    if bias == "bullish":
        return _first_number_text(row.get("invalid"), row.get("support"), row.get("demand_zone"))
    if bias == "bearish":
        return _first_number_text(row.get("invalid"), row.get("resistance"), row.get("supply_zone"))
    return _first_number_text(row.get("invalid"), row.get("support"), row.get("resistance"))


def _trigger_from_row(row: dict[str, Any], bias: str) -> str:
    if _clean(row.get("trigger_level")):
        return _clean(row.get("trigger_level"))
    if bias == "bullish":
        return _first_number_text(row.get("breakout"), row.get("resistance"), row.get("resistance1"))
    if bias == "bearish":
        return _first_number_text(row.get("breakdown"), row.get("support"), row.get("support1"))
    return _first_number_text(row.get("breakout"), row.get("breakdown"), row.get("resistance"), row.get("support"))


def _setup_type_from_row(row: dict[str, Any], bias: str) -> str:
    explicit = _normal_choice(row.get("setup_type"), ALLOWED_SETUP_TYPES, "")
    if explicit:
        return explicit
    if bias == "bullish" and _first_number_text(row.get("breakout"), row.get("resistance"), row.get("resistance1")):
        return "breakout"
    if bias == "bearish" and _first_number_text(row.get("breakdown"), row.get("support"), row.get("support1")):
        return "breakdown"
    if bias in {"neutral", "mixed", "unclear"}:
        return "watch_only"
    return "unclear"


def _trigger_condition(row: dict[str, Any], bias: str, setup_type: str, trigger: str) -> str:
    if _clean(row.get("trigger_condition")):
        return _clean(row.get("trigger_condition"))
    if not trigger:
        return "mark a trigger level manually"
    if setup_type == "breakout" or bias == "bullish":
        return f"manual alert candidate if price confirms above {trigger}"
    if setup_type == "breakdown" or bias == "bearish":
        return f"manual alert candidate if price confirms below {trigger}"
    return f"manual alert candidate near {trigger}"


def _target_for_bias(row: dict[str, Any], bias: str, target_key: str) -> str:
    if _clean(row.get(target_key)):
        return _clean(row.get(target_key))
    if bias == "bullish":
        return _first_number_text(row.get("resistance2"), row.get("resistance"), row.get("supply_zone"))
    if bias == "bearish":
        return _first_number_text(row.get("support2"), row.get("support"), row.get("demand_zone"))
    return ""


def _normalized_status(row: dict[str, Any], trigger: str, invalidation: str) -> str:
    status = _normal_choice(row.get("status"), ALLOWED_STATUSES, "draft")
    if status == "rejected":
        return status
    if not trigger or not invalidation:
        return "needs_chart_confirmation"
    if not _confirmed(row.get("chart_confirmation")):
        return "needs_chart_confirmation"
    if status in {"ready_for_manual_alert", "active_manual_only"}:
        return status
    return "ready_for_manual_alert"


def normalize_alert_plan_row(row: dict[str, Any]) -> dict[str, str]:
    normalized = {column: _clean(row.get(column, "")) for column in ALERT_PLAN_COLUMNS}
    normalized["ticker"] = _clean_ticker(row.get("ticker"))
    normalized["timeframe"] = _normal_timeframe(row.get("timeframe"))
    normalized["setup_bias"] = _normal_choice(row.get("setup_bias") or row.get("chart_bias") or row.get("bias"), ALLOWED_SETUP_BIASES, "unclear")
    normalized["setup_type"] = _setup_type_from_row(row, normalized["setup_bias"])
    normalized["support"] = _first_number_text(row.get("support"), row.get("support1")) or normalized["support"]
    normalized["resistance"] = _first_number_text(row.get("resistance"), row.get("resistance1")) or normalized["resistance"]
    normalized["demand_zone"] = _clean(row.get("demand_zone")) or normalized["demand_zone"]
    normalized["supply_zone"] = _clean(row.get("supply_zone")) or normalized["supply_zone"]
    normalized["trigger_level"] = _trigger_from_row(row, normalized["setup_bias"])
    normalized["trigger_condition"] = _trigger_condition(
        row,
        normalized["setup_bias"],
        normalized["setup_type"],
        normalized["trigger_level"],
    )
    normalized["invalidation_level"] = _invalidation_for_bias(row, normalized["setup_bias"])
    normalized["target_1"] = _target_for_bias(row, normalized["setup_bias"], "target_1")
    normalized["target_2"] = _target_for_bias(row, normalized["setup_bias"], "target_2")
    normalized["confidence"] = _clean(row.get("confidence")) or "manual_review"
    normalized["chart_confirmation"] = _clean(row.get("chart_confirmation")) or "needs_manual_confirmation"
    normalized["risk_reward_note"] = _clean(row.get("risk_reward_note")) or "mark trigger, invalidation, and targets manually before final use"
    normalized["manual_notes"] = _clean(row.get("manual_notes")) or _clean(row.get("notes"))
    normalized["volume_confirmation"] = _clean(row.get("volume_confirmation")) or _clean(row.get("volume_note"))
    normalized["fundamentals_context"] = _clean(row.get("fundamentals_context")) or _clean(row.get("fundamentals_note"))
    normalized["macro_context"] = _clean(row.get("macro_context")) or _clean(row.get("macro_note"))
    normalized["status"] = _normalized_status(row, normalized["trigger_level"], normalized["invalidation_level"])
    normalized["alert_message"] = _clean(row.get("alert_message")) or alert_plan_to_tradingview_message(normalized)
    return normalized


def alert_plan_template_csv(tickers: list[str] | None = None) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=ALERT_PLAN_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for ticker in (tickers or ["SPY"])[:20]:
        writer.writerow(
            normalize_alert_plan_row(
                {
                    "ticker": ticker,
                    "timeframe": "15m",
                    "setup_bias": "unclear",
                    "setup_type": "watch_only",
                    "manual_notes": "draft manual alert plan; verify chart manually; no orders",
                }
            )
        )
    return output.getvalue()


def parse_alert_plan_csv(text: str) -> tuple[list[dict[str, str]], list[str]]:
    clean_text = _clean(text)
    if not clean_text:
        return [], ["Paste alert plan CSV first."]
    reader = csv.DictReader(io.StringIO(clean_text))
    fieldnames = reader.fieldnames or []
    errors: list[str] = []
    for column in ["ticker", "timeframe", "setup_bias"]:
        if column not in fieldnames:
            errors.append(f"Missing required alert plan column: {column}")
    rows: list[dict[str, str]] = []
    for index, raw_row in enumerate(reader, start=2):
        row = normalize_alert_plan_row(dict(raw_row))
        if row["ticker"] == "UNKNOWN":
            errors.append(f"Row {index}: ticker is required.")
            continue
        rows.append(row)
    return rows, errors


def build_alert_plan_from_chart_review(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    plans: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bias = _normal_choice(row.get("chart_bias"), ALLOWED_SETUP_BIASES, "unclear")
        plans.append(
            normalize_alert_plan_row(
                {
                    **row,
                    "setup_bias": bias,
                    "support": row.get("support"),
                    "resistance": row.get("resistance"),
                    "invalidation_level": row.get("invalid"),
                    "fundamentals_context": row.get("fundamentals_note"),
                    "macro_context": row.get("macro_note"),
                    "volume_confirmation": row.get("volume_note"),
                    "manual_notes": row.get("manual_notes"),
                    "status": "needs_chart_confirmation",
                }
            )
        )
    return plans


def build_alert_plan_from_market_breakdown(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    plans: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        bias = _normal_choice(row.get("bias"), ALLOWED_SETUP_BIASES, "unclear")
        setup_type = "breakout" if bias == "bullish" else "breakdown" if bias == "bearish" else "watch_only"
        plans.append(
            normalize_alert_plan_row(
                {
                    **row,
                    "setup_bias": bias,
                    "setup_type": setup_type,
                    "support": row.get("support") or row.get("support1"),
                    "resistance": row.get("resistance") or row.get("resistance1"),
                    "trigger_level": row.get("resistance") or row.get("resistance1") if bias == "bullish" else row.get("support") or row.get("support1"),
                    "invalidation_level": row.get("support") or row.get("support1") if bias == "bullish" else row.get("resistance") or row.get("resistance1"),
                    "manual_notes": "drafted from read-only market breakdown; verify chart manually; no orders",
                    "status": "needs_chart_confirmation",
                }
            )
        )
    return plans


def alert_plan_status_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
    summary = {status: 0 for status in sorted(ALLOWED_STATUSES)}
    summary["total"] = 0
    for row in rows:
        normalized = normalize_alert_plan_row(row) if isinstance(row, dict) else {}
        status = normalized.get("status", "draft")
        summary[status] = summary.get(status, 0) + 1
        summary["total"] += 1
    return summary


def alert_plan_to_tradingview_message(row: dict[str, Any]) -> str:
    ticker = _clean_ticker(row.get("ticker"))
    timeframe = _normal_timeframe(row.get("timeframe"))
    bias = _normal_choice(row.get("setup_bias"), ALLOWED_SETUP_BIASES, "unclear")
    setup_type = _normal_choice(row.get("setup_type"), ALLOWED_SETUP_TYPES, "watch_only")
    trigger = _clean(row.get("trigger_level")) or "manual trigger level required"
    invalidation = _clean(row.get("invalidation_level")) or "manual invalidation required"
    return (
        f"{ticker} {timeframe} {setup_type} {bias} manual alert candidate. "
        f"Trigger: {trigger}. Invalidation: {invalidation}. {SAFETY_SUFFIX}"
    )


def alert_plan_safety_warnings(row: dict[str, Any]) -> list[str]:
    normalized = normalize_alert_plan_row(row)
    warnings: list[str] = []
    if not normalized["trigger_level"]:
        warnings.append("Trigger level is missing; mark it manually before considering any alert.")
    if not normalized["invalidation_level"]:
        warnings.append("Invalidation level is missing; define the stop condition manually.")
    if not _confirmed(normalized.get("chart_confirmation")):
        warnings.append("Chart confirmation is missing; keep status as needs_chart_confirmation.")
    if normalized["setup_bias"] in {"unclear", "mixed"}:
        warnings.append("Setup bias is not clear; use watch_only until the chart is clearer.")
    warnings.extend(context_conflict_flags(normalized))
    warnings.append("Manual alert candidate only. Decision support only. No orders.")
    deduped: list[str] = []
    for warning in warnings:
        if warning not in deduped:
            deduped.append(warning)
    return deduped


def context_conflict_flags(row: dict[str, Any]) -> list[str]:
    normalized = {key: _clean(value).lower() for key, value in row.items()}
    bias = _normal_choice(row.get("setup_bias") or row.get("bias"), ALLOWED_SETUP_BIASES, "unclear")
    flags: list[str] = []
    macro = normalized.get("macro_context", "")
    fundamentals = normalized.get("fundamentals_context", "")
    earnings = normalized.get("earnings_risk", "")
    catalyst = normalized.get("news_catalyst", "")
    risk_environment = normalized.get("risk_environment", "")
    rates = normalized.get("rates_context", "")
    index_context = normalized.get("index_context", "")

    combined_macro = " ".join([macro, risk_environment, rates, index_context])
    if bias == "bullish" and "risk-off" in combined_macro:
        flags.append("Bullish setup conflicts with risk-off context.")
    if bias == "bearish" and "risk-on" in combined_macro:
        flags.append("Bearish setup conflicts with risk-on context.")
    if "earnings" in earnings or "unknown" in earnings:
        flags.append("Earnings/catalyst timing needs manual confirmation.")
    if not catalyst:
        flags.append("News catalyst is unknown; confirm manually.")
    if bias == "bullish" and "weak" in fundamentals:
        flags.append("Bullish setup conflicts with weak fundamentals context.")
    if bias == "bearish" and "strong" in fundamentals:
        flags.append("Bearish setup conflicts with strong fundamentals context.")
    return flags


def setup_decision_support(row: dict[str, Any]) -> dict[str, Any]:
    normalized = normalize_alert_plan_row(row)
    trigger = normalized["trigger_level"] or "missing trigger"
    invalidation = normalized["invalidation_level"] or "missing invalidation"
    targets = [value for value in [normalized["target_1"], normalized["target_2"]] if value]
    warnings = alert_plan_safety_warnings(normalized)
    if normalized["status"] == "ready_for_manual_alert":
        next_action = "If still valid, user may manually create a TradingView alert after final confirmation."
    elif not normalized["trigger_level"] or not normalized["invalidation_level"]:
        next_action = "Mark support/resistance before considering this alert."
    else:
        next_action = "Verify chart manually before setting any alert."
    return {
        "ticker": normalized["ticker"],
        "timeframe": normalized["timeframe"],
        "setup_bias": normalized["setup_bias"],
        "setup_type": normalized["setup_type"],
        "trigger_summary": f"Trigger: {trigger}",
        "invalidation_summary": f"Invalidation: {invalidation}",
        "target_summary": "Targets: " + (", ".join(targets) if targets else "mark manually"),
        "risk_reward_note": normalized["risk_reward_note"],
        "confidence": normalized["confidence"],
        "next_action": next_action,
        "stop_conditions": [
            "Alert trigger is stale.",
            "Support/resistance is invalidated.",
            "Macro/fundamental context changed.",
            "Chart disagrees across timeframes.",
            "User is unsure or rushed.",
        ],
        "safety_warnings": warnings,
    }
