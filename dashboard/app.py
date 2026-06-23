"""Trading Autopilot dashboard.

Runs as a Streamlit app when Streamlit is installed, otherwise prints a compact
CLI dashboard. Decision support only; no broker or order execution features.
"""

from __future__ import annotations

import csv
import html
import io
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from covered_call_filter import filter_covered_call_candidates
from alert_planning import (
    ALERT_PLAN_COLUMNS,
    alert_plan_status_summary,
    alert_plan_template_csv,
    alert_plan_to_tradingview_message,
    build_alert_plan_from_chart_review,
    build_alert_plan_from_market_breakdown,
    normalize_alert_plan_row,
    parse_alert_plan_csv,
    setup_decision_support,
)

try:
    from alert_planning import build_decision_support_card
except ImportError:

    def build_decision_support_card(row: dict[str, Any]) -> dict[str, Any]:
        """Local fallback for mixed Streamlit Cloud deploy caches."""

        ticker = str(row.get("ticker") or "UNKNOWN").strip().upper()
        timeframe = str(row.get("timeframe") or "15m").strip()
        bias = str(row.get("setup_bias") or "unclear").strip()
        setup_type = str(row.get("setup_type") or "watch_only").strip()
        status = str(row.get("status") or "draft").strip()
        trigger = str(row.get("trigger_condition") or row.get("trigger_level") or "mark manually").strip()
        invalidation = str(row.get("invalidation_level") or "mark manually").strip()
        confidence = str(row.get("confidence") or "manual_review").strip()
        cautions: list[str] = []
        if status == "needs_chart_confirmation":
            cautions.append("manual chart confirmation needed")
        if invalidation == "mark manually":
            cautions.append("invalidation level missing")
        if trigger == "mark manually":
            cautions.append("trigger level missing")
        return {
            "ticker": ticker,
            "timeframe": timeframe,
            "setup": f"{bias} {setup_type}".strip(),
            "current_read": status,
            "trigger": trigger,
            "invalidation": invalidation,
            "caution_flags": cautions or ["manual confirmation still required"],
            "context": str(row.get("manual_notes") or "decision-support draft only").strip(),
            "confidence": confidence,
            "status": status,
            "next_action": "Verify chart manually before creating any manual reminder.",
            "final_reminder": "This is not a trade, not an order, and not a live alert.",
        }

from chart_review import (
    CHART_REVIEW_COLUMNS,
    chart_review_rows_to_tradingview_import_csv,
    chart_review_summary,
    chart_review_template_csv,
    chart_review_timeframe_summary,
    normalize_chart_review_row,
    parse_chart_review_csv,
)
from market_data import (
    compute_market_data_indicators,
    configured_market_data_provider,
    fetch_readonly_market_data_rows,
    get_market_data_provider_config,
    market_data_config_errors,
    market_data_rows_to_tradingview_import_csv,
    normalize_market_data_bars,
)
from market_breakdown import (
    build_market_breakdown_row,
    build_market_breakdown_rows,
    market_breakdown_summary,
    parse_watchlist_text,
)
from options_filter import filter_options_candidates
from scoring import load_candidates_csv, rank_candidates
from tools.validate_candidates import ALL_CANDIDATE_COLUMNS, validate_candidate_rows
from shares_filter import filter_share_candidates


APP_VERSION = "1.5.2-mobile-first-run-one-click-review-dev"
SAMPLE_WARNING = "SAMPLE/EXAMPLE DATA ONLY — NOT LIVE MARKET DATA"
LEGACY_SAMPLE_WARNING = "SAMPLE DATA ONLY — NOT LIVE MARKET DATA"
USER_SUPPLIED_WARNING = "USER-SUPPLIED DATA — VERIFY MANUALLY BEFORE ANY TRADING DECISION"
TRADINGVIEW_IMPORT_WARNING = "TRADINGVIEW IMPORT DATA — VERIFY MANUALLY BEFORE ANY TRADING DECISION"
CALIBRATION_RESULTS_WARNING = "CALIBRATION RESULTS — SESSION ONLY, NOT TRADE ADVICE"
CALIBRATION_BATCH_LOG_WARNING = "CALIBRATION BATCH LOG — SESSION ONLY, NOT TRADE ADVICE"
CALIBRATION_REVIEW_WARNING = "CALIBRATION REVIEW — SESSION ONLY, NOT TRADE ADVICE"
DAILY_REVIEW_WARNING = "DAILY REVIEW — DECISION SUPPORT ONLY, NOT TRADE ADVICE"
LIVE_DATA_READONLY_WARNING = "LIVE DATA — READ ONLY, NOT TRADE ADVICE"
CHART_WORKSPACE_WARNING = "READ-ONLY CHART REVIEW — NO ORDERS, NO ALERTS"
ALERT_PLANNER_WARNING = "ALERT PLANNING ONLY — NO ALERTS ARE CREATED"
DEFAULT_DATA = ROOT / "data" / "sample_candidates.csv"
TEMPLATE_DATA = ROOT / "data" / "real_candidates_template.csv"
WORKING_DATA = ROOT / "data" / "real_candidates_WORKING.csv"
SAMPLE_DATA_NAMES = {"sample_candidates.csv", "real_candidates_template.csv"}
CALIBRATION_BATCH = ["SPY", "QQQ", "TSLA", "SMCI", "PLTR", "AI", "OKLO", "SMR", "SPCE", "INTC"]
CALIBRATION_BATCH_TEXT = "SPY, QQQ, TSLA, SMCI, PLTR, AI, OKLO, SMR, SPCE, INTC"
CALIBRATION_WARNING = "CALIBRATION ONLY — NOT LIVE TRADING OR TRADE ADVICE"
TRADINGVIEW_IMPORT_EXAMPLE = """ticker,price,timeframe,bias_note,key_level_note,ema9,ema21,sma200,support1,resistance1,breakout,breakdown,invalid,relative_volume,macd_hist,notes
EXAMPLE,100,15m,unclear,manual review needed,101,100,95,98,105,106,97,96,1.2,0.15,CALIBRATION ONLY"""
TRADINGVIEW_IMPORT_SUPPORTED_COLUMNS = [
    "ticker or symbol",
    "price, close, or last",
    "timeframe or interval",
    "bias_note or bias",
    "key_level_note or key_level",
    "notes",
    "ema9 or ema_9",
    "ema21 or ema_21",
    "wma50 or wma_50",
    "wma200 or wma_200",
    "sma200 or sma_200",
    "support1 or support_1",
    "support2 or support_2",
    "resistance1 or resistance_1",
    "resistance2 or resistance_2",
    "breakout",
    "breakdown",
    "invalid",
    "bullish_trigger",
    "bearish_trigger",
    "bullish_invalid",
    "bearish_invalid",
    "avg_volume, average_volume, or volume",
    "relative_volume, rel_volume, or rvol",
    "macd_hist or macd_histogram",
    "macd_hist_prev or previous_macd_histogram",
    "manual_override",
]
CALIBRATION_RESULT_COLUMNS = [
    "ticker",
    "manual_chart_bias",
    "timeframe_checked",
    "key_levels",
    "manual_notes",
    "dashboard_score",
    "dashboard_grade",
    "dashboard_state",
    "dashboard_bucket",
    "match_status",
    "issue_type",
    "follow_up",
]
CALIBRATION_LABEL_COLUMNS = [
    "ticker",
    "timeframe_checked",
    "manual_chart_bias",
    "match_status",
    "issue_type",
    "follow_up",
]
CALIBRATION_LABEL_EXAMPLE = """ticker,timeframe_checked,manual_chart_bias,match_status,issue_type,follow_up
EXAMPLE,15m,bullish,match,none,example label only
DEMO,15m,neutral,unclear,needs_manual_chart_confirmation,example needs review"""
CALIBRATION_MATCH_STATUSES = [
    "match",
    "false_positive",
    "false_negative",
    "bad_bucket",
    "bad_scoring",
    "bad_validation",
    "unclear",
]
CALIBRATION_ISSUE_TYPES = [
    "none",
    "needs_manual_chart_confirmation",
    "dashboard_too_bearish",
    "dashboard_too_bullish",
    "bad_bucket",
    "bad_scoring",
    "bad_validation",
    "missing_data",
    "unclear",
]
DASHBOARD_SECTION_NAMES = [
    "Market Context",
    "Bullish Options",
    "Bearish Options",
    "Directional Conflicts",
    "Pre-Confirmation",
    "Under-$25 Shares",
    "Covered Call Income",
    "Covered Call Defensive",
    "Avoid List",
    "Blocked Options",
    "Blocked Shares",
    "Blocked Covered Calls",
    "Alerts",
    "Journal Prep",
]
BEGINNER_TAB_NAMES = [
    "Home",
    "Market Breakdown",
    "Chart Workspace",
    "Alert Planner",
    "Live Data — Read Only",
    "Daily Review",
    "Calibration Results",
    "Calibration Review",
    "Help / Safety",
]
ADVANCED_TAB_NAMES = ["Home", "Market Breakdown", "Chart Workspace", "Alert Planner", "Daily Review", "Live Data — Read Only"] + DASHBOARD_SECTION_NAMES + [
    "Calibration Guide",
    "Calibration Results",
    "Calibration Batch Log",
    "Calibration Review",
]
DASHBOARD_TAB_NAMES = ADVANCED_TAB_NAMES
MANUAL_REVIEW_COLUMNS = [
    "ticker",
    "asset_type",
    "category",
    "timeframe",
    "close",
    "ema9",
    "ema21",
    "wma50",
    "sma200",
    "support1",
    "resistance1",
    "breakout",
    "breakdown",
    "invalid",
    "avg_volume",
    "relative_volume",
    "watchlist",
]


def dashboard_tab_names_for_mode(mode: str) -> list[str]:
    if str(mode or "").strip().lower() == "advanced":
        return list(ADVANCED_TAB_NAMES)
    return list(BEGINNER_TAB_NAMES)


class TradingViewImportParseError(ValueError):
    def __init__(self, errors: list[str], rows: list[dict[str, Any]] | None = None):
        super().__init__("\n".join(errors))
        self.errors = errors
        self.rows = rows or []


def _resolve_path(path: str | Path | None = None) -> Path:
    if path is None or str(path).strip() == "":
        return DEFAULT_DATA
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return candidate


def _cli_path_arg() -> Path:
    args = [arg for arg in sys.argv[1:] if not arg.startswith("-")]
    if not args:
        return DEFAULT_DATA
    return _resolve_path(args[0])


def _warning_lines(path: Path) -> list[str]:
    if path.name in SAMPLE_DATA_NAMES:
        lines = [SAMPLE_WARNING]
        if path.name == "sample_candidates.csv":
            lines.append(LEGACY_SAMPLE_WARNING)
        return lines
    return [USER_SUPPLIED_WARNING]


def _load(path: Path = DEFAULT_DATA) -> list[dict[str, Any]]:
    return load_candidates_csv(path)


def _load_csv_text(text: str) -> list[dict[str, Any]]:
    return list(csv.DictReader(io.StringIO(text)))


def _load_csv_bytes(data: bytes) -> list[dict[str, Any]]:
    return _load_csv_text(data.decode("utf-8-sig"))


def _normalize_import_header(name: str) -> str:
    normalized = (
        str(name or "")
        .replace("\ufeff", "")
        .strip()
        .lower()
        .replace(" ", "_")
        .replace("-", "_")
    )
    aliases = {
        "symbol": "ticker",
        "price": "close",
        "last": "close",
        "interval": "timeframe",
        "bias": "bias_note",
        "key_level": "key_level_note",
        "key_levels": "key_level_note",
        "ema_9": "ema9",
        "ema_21": "ema21",
        "wma_50": "wma50",
        "wma_200": "wma200",
        "sma_200": "sma200",
        "support_1": "support1",
        "support_2": "support2",
        "resistance_1": "resistance1",
        "resistance_2": "resistance2",
        "average_volume": "avg_volume",
        "volume": "avg_volume",
        "rel_volume": "relative_volume",
        "rvol": "relative_volume",
        "macd_histogram": "macd_hist",
        "previous_macd_histogram": "macd_hist_prev",
    }
    return aliases.get(normalized, normalized)


def _import_text_value(row: dict[str, Any], key: str) -> str:
    return str(row.get(key, "") or "").strip()


def _import_float(value: Any) -> float:
    text = str(value or "").strip().replace(",", "")
    if not text:
        raise ValueError("missing price/close/last")
    try:
        parsed = float(text)
    except ValueError as exc:
        raise ValueError(f"price/close/last is not numeric: {value}") from exc
    if parsed <= 0:
        raise ValueError("price/close/last must be greater than 0")
    return parsed


def _optional_import_float(row: dict[str, Any], key: str, default: float) -> float:
    text = _import_text_value(row, key).replace(",", "")
    if not text:
        return float(default)
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{key} is not numeric: {row.get(key)}") from exc


def _optional_import_int(row: dict[str, Any], key: str, default: int) -> int:
    text = _import_text_value(row, key).replace(",", "")
    if not text:
        return int(default)
    try:
        return int(float(text))
    except ValueError as exc:
        raise ValueError(f"{key} is not numeric: {row.get(key)}") from exc


def _candidate_from_tradingview_import_row(row: dict[str, Any]) -> dict[str, Any]:
    ticker = _import_text_value(row, "ticker").upper()
    if not ticker:
        raise ValueError("missing ticker/symbol")

    close = _import_float(row.get("close"))
    timeframe = _import_text_value(row, "timeframe") or "15m"
    bias_note = _import_text_value(row, "bias_note")
    key_level_note = _import_text_value(row, "key_level_note")
    notes = _import_text_value(row, "notes")

    watchlist_parts = ["TRADINGVIEW_IMPORT_SESSION_ONLY"]
    if bias_note:
        watchlist_parts.append(f"bias_note={bias_note}")
    if key_level_note:
        watchlist_parts.append(f"key_level_note={key_level_note}")
    if notes:
        watchlist_parts.append(f"notes={notes}")

    ema9 = _optional_import_float(row, "ema9", close)
    ema21 = _optional_import_float(row, "ema21", close)
    wma50 = _optional_import_float(row, "wma50", close)
    wma200 = _optional_import_float(row, "wma200", close)
    sma200 = _optional_import_float(row, "sma200", close)
    support1 = _optional_import_float(row, "support1", close)
    support2 = _optional_import_float(row, "support2", close)
    resistance1 = _optional_import_float(row, "resistance1", close)
    resistance2 = _optional_import_float(row, "resistance2", close)
    breakout = _optional_import_float(row, "breakout", close)
    breakdown = _optional_import_float(row, "breakdown", close)
    invalid = _optional_import_float(row, "invalid", close)
    bullish_trigger = _optional_import_float(row, "bullish_trigger", close)
    bearish_trigger = _optional_import_float(row, "bearish_trigger", close)
    bullish_invalid = _optional_import_float(row, "bullish_invalid", close)
    bearish_invalid = _optional_import_float(row, "bearish_invalid", close)
    avg_volume = _optional_import_int(row, "avg_volume", 0)
    relative_volume = _optional_import_float(row, "relative_volume", 1.0)
    macd_hist = _optional_import_float(row, "macd_hist", 0.0)
    macd_hist_prev = _optional_import_float(row, "macd_hist_prev", 0.0)
    manual_override = _import_text_value(row, "manual_override") or "false"

    candidate = {column: "" for column in ALL_CANDIDATE_COLUMNS}
    candidate.update(
        {
            "ticker": ticker,
            "watchlist": " | ".join(watchlist_parts),
            "asset_type": "equity",
            "category": "tradingview_import",
            "timeframe": timeframe,
            "close": close,
            "ema9": ema9,
            "ema21": ema21,
            "wma50": wma50,
            "wma200": wma200,
            "sma200": sma200,
            "macd_hist": macd_hist,
            "macd_hist_prev": macd_hist_prev,
            "support1": support1,
            "support2": support2,
            "resistance1": resistance1,
            "resistance2": resistance2,
            "breakout": breakout,
            "breakdown": breakdown,
            "invalid": invalid,
            "bullish_trigger": bullish_trigger,
            "bearish_trigger": bearish_trigger,
            "bullish_invalid": bullish_invalid,
            "bearish_invalid": bearish_invalid,
            "avg_volume": avg_volume,
            "relative_volume": relative_volume,
            "manual_override": manual_override,
        }
    )
    return candidate


def _parse_tradingview_import_text(text: str) -> list[dict[str, Any]]:
    clean_lines = [line for line in str(text or "").replace("\r\n", "\n").splitlines() if line.strip()]
    if not clean_lines:
        return []

    try:
        reader = csv.DictReader(clean_lines)
    except csv.Error as exc:
        raise TradingViewImportParseError([f"CSV could not be parsed: {exc}"]) from exc

    if not reader.fieldnames:
        raise TradingViewImportParseError(["missing CSV header row"])

    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    try:
        for row_number, raw_row in enumerate(reader, start=2):
            normalized_row: dict[str, Any] = {}
            for header, value in raw_row.items():
                if header is None:
                    continue
                normalized_header = _normalize_import_header(header)
                if normalized_header:
                    normalized_row[normalized_header] = str(value or "").strip()

            if not any(str(value or "").strip() for value in normalized_row.values()):
                continue
            try:
                rows.append(_candidate_from_tradingview_import_row(normalized_row))
            except ValueError as exc:
                errors.append(f"row {row_number}: {exc}")
    except csv.Error as exc:
        raise TradingViewImportParseError([f"CSV could not be parsed: {exc}"], rows) from exc

    if errors:
        raise TradingViewImportParseError(errors, rows)
    if not rows:
        raise TradingViewImportParseError(["no import rows found"])
    return rows


def _candidate_csv_text(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=ALL_CANDIDATE_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in ALL_CANDIDATE_COLUMNS})
    return output.getvalue()


def _calibration_csv_text(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CALIBRATION_RESULT_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in CALIBRATION_RESULT_COLUMNS})
    return output.getvalue()


def parse_calibration_review_csv(text: str) -> list[dict[str, Any]]:
    clean_lines = [line for line in str(text or "").replace("\ufeff", "").replace("\r\n", "\n").splitlines() if line.strip()]
    if not clean_lines:
        return []
    reader = csv.DictReader(clean_lines)
    rows: list[dict[str, Any]] = []
    for raw_row in reader:
        row = {column: str(raw_row.get(column, "") or "").strip() for column in CALIBRATION_RESULT_COLUMNS}
        if any(row.values()):
            rows.append(row)
    return rows


def _calibration_label_key(row: dict[str, Any]) -> str:
    ticker = str(row.get("ticker", "") or "").strip().upper()
    timeframe = str(row.get("timeframe_checked", "") or "").strip() or "15m"
    return f"{ticker}|{timeframe}"


def _valid_calibration_match_status(value: str) -> bool:
    return str(value or "").strip().lower() in CALIBRATION_MATCH_STATUSES


def _valid_calibration_issue_type(value: str) -> bool:
    return str(value or "").strip().lower() in CALIBRATION_ISSUE_TYPES


def parse_calibration_label_csv(text: str) -> list[dict[str, Any]]:
    clean_lines = [
        line
        for line in str(text or "").replace("\ufeff", "").replace("\r\n", "\n").splitlines()
        if line.strip()
    ]
    if not clean_lines:
        return []

    try:
        reader = csv.DictReader(clean_lines)
        fieldnames = [str(name or "").strip() for name in reader.fieldnames or []]
        missing_columns = [column for column in CALIBRATION_LABEL_COLUMNS if column not in fieldnames]
        rows: list[dict[str, Any]] = []
        if missing_columns:
            rows.append({"__error": f"Label CSV is missing columns: {', '.join(missing_columns)}"})

        for row_number, raw_row in enumerate(reader, start=2):
            if raw_row is None:
                continue
            normalized_raw = {str(key or "").strip(): value for key, value in raw_row.items() if key is not None}
            row = {
                column: str(normalized_raw.get(column, "") or "").strip()
                for column in CALIBRATION_LABEL_COLUMNS
            }
            row["ticker"] = row["ticker"].upper()
            row["timeframe_checked"] = row["timeframe_checked"] or "15m"
            row["match_status"] = row["match_status"].lower()
            row["issue_type"] = row["issue_type"].lower()
            row["__row_number"] = str(row_number)
            if None in raw_row:
                row["__error"] = f"row {row_number}: too many columns in label CSV"
            if any(row.get(column, "") for column in CALIBRATION_LABEL_COLUMNS) or row.get("__error"):
                rows.append(row)
        return rows
    except csv.Error as exc:
        return [{"__error": f"Label CSV could not be parsed: {exc}"}]


def apply_calibration_labels_to_rows(
    rows: list[dict[str, Any]],
    labels: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    current_rows = [_normalize_calibration_result_row(row) for row in rows]
    if not labels:
        return current_rows, ["No calibration label rows found."]

    key_indexes = {_calibration_label_key(row): index for index, row in enumerate(current_rows)}
    errors: list[str] = []
    prepared_labels: list[dict[str, Any]] = []

    for fallback_index, raw_label in enumerate(labels, start=1):
        row_number = str(raw_label.get("__row_number", fallback_index) or fallback_index)
        if raw_label.get("__error"):
            errors.append(str(raw_label["__error"]))
            continue

        label = {
            column: str(raw_label.get(column, "") or "").strip()
            for column in CALIBRATION_LABEL_COLUMNS
        }
        label["ticker"] = label["ticker"].upper()
        label["timeframe_checked"] = label["timeframe_checked"] or "15m"
        label["match_status"] = label["match_status"].lower()
        label["issue_type"] = label["issue_type"].lower()

        if not label["ticker"]:
            errors.append(f"row {row_number}: ticker is required")
            continue
        if not label["manual_chart_bias"]:
            errors.append(f"row {row_number}: manual_chart_bias is required")
            continue
        if not _valid_calibration_match_status(label["match_status"]):
            valid_values = ", ".join(CALIBRATION_MATCH_STATUSES)
            errors.append(f"row {row_number}: invalid match_status {label['match_status']!r}; use one of: {valid_values}")
            continue
        if not _valid_calibration_issue_type(label["issue_type"]):
            valid_values = ", ".join(CALIBRATION_ISSUE_TYPES)
            errors.append(f"row {row_number}: invalid issue_type {label['issue_type']!r}; use one of: {valid_values}")
            continue

        key = _calibration_label_key(label)
        if key not in key_indexes:
            errors.append(f"row {row_number}: no current Calibration Results row matches {key}")
            continue
        prepared_labels.append(label)

    if errors:
        return current_rows, errors

    updated_rows = [dict(row) for row in current_rows]
    for label in prepared_labels:
        target = updated_rows[key_indexes[_calibration_label_key(label)]]
        if label["manual_chart_bias"]:
            target["manual_chart_bias"] = label["manual_chart_bias"]
        target["match_status"] = label["match_status"]
        target["issue_type"] = label["issue_type"]
        target["follow_up"] = label["follow_up"]
    return updated_rows, []


CALIBRATION_SUMMARY_MATCH_STATUSES = [
    "match",
    "unclear",
    "false_positive",
    "false_negative",
    "bad_bucket",
    "bad_scoring",
    "bad_validation",
]

CALIBRATION_SUMMARY_ISSUE_TYPES = [
    "none",
    "needs_manual_chart_confirmation",
    "bad_scoring",
    "bad_bucket",
    "bad_validation",
    "missing_data",
    "unclear",
]


def calibration_match_status_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {status: 0 for status in CALIBRATION_SUMMARY_MATCH_STATUSES}
    for row in rows:
        status = str(row.get("match_status", "") or "unclear").strip() or "unclear"
        counts[status] = counts.get(status, 0) + 1
    return counts


def calibration_issue_type_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts = {issue_type: 0 for issue_type in CALIBRATION_SUMMARY_ISSUE_TYPES}
    for row in rows:
        issue_type = str(row.get("issue_type", "") or "unclear").strip() or "unclear"
        counts[issue_type] = counts.get(issue_type, 0) + 1
    return counts


def scoring_review_notes_from_calibration_rows(rows: list[dict[str, Any]]) -> list[str]:
    match_counts = calibration_match_status_counts(rows)
    issue_counts = calibration_issue_type_counts(rows)
    review_rows = calibration_rows_needing_review(rows)
    total_rows = len(rows)
    issue_count = sum(count for issue_type, count in issue_counts.items() if issue_type != "none")
    notes: list[str] = []

    if match_counts.get("false_positive", 0) > 0:
        notes.append("False positives are present. Review whether filters or scoring are too aggressive before changing logic.")
    if issue_counts.get("bad_scoring", 0) > 0:
        notes.append("bad_scoring issue types are present. Look for repeated patterns before changing scoring.")
    if total_rows > 0 and match_counts.get("match", 0) >= max(1, total_rows - 1) and issue_count <= 1:
        notes.append("Most reviewed rows matched manual reads. Avoid broad scoring changes from limited issues.")
    if total_rows < 20:
        notes.append("Fewer than 20 calibration rows reviewed. Treat this as early evidence, not a final scoring basis.")
    if review_rows:
        notes.append("Resolve manual confirmation rows before scoring changes.")

    notes.extend(
        [
            "Do not change scoring from one ticker.",
            "Keep manual TradingView confirmation.",
            "No broker/order automation.",
        ]
    )
    return notes


def calibration_problem_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    problem_rows: list[dict[str, Any]] = []
    for row in rows:
        match_status = str(row.get("match_status", "") or "unclear").strip() or "unclear"
        issue_type = str(row.get("issue_type", "") or "unclear").strip() or "unclear"
        if match_status != "match" or issue_type != "none":
            problem_rows.append(row)
    return problem_rows


def calibration_evidence_level(rows: list[dict[str, Any]]) -> str:
    total_rows = len(rows)
    problem_count = len(calibration_problem_rows(rows))
    if total_rows < 20:
        return "early"
    if total_rows >= 50:
        return "stronger_sample"
    if problem_count < 3:
        return "stable"
    return "review_needed"


def _calibration_rows_text(rows: list[dict[str, Any]]) -> str:
    text_parts: list[str] = []
    for row in rows:
        text_parts.append(str(row.get("follow_up", "") or ""))
        text_parts.append(str(row.get("manual_notes", "") or ""))
    return " ".join(text_parts).lower()


def scoring_adjustment_proposal_from_calibration_rows(rows: list[dict[str, Any]]) -> list[str]:
    match_counts = calibration_match_status_counts(rows)
    issue_counts = calibration_issue_type_counts(rows)
    problem_rows = calibration_problem_rows(rows)
    evidence_level = calibration_evidence_level(rows)
    combined_text = _calibration_rows_text(rows)
    notes = ["No automatic scoring changes are made here."]

    if evidence_level == "early":
        notes.append("Evidence level: early. Fewer than 20 rows reviewed; collect more calibration rows before changing scoring.")
    if match_counts.get("false_positive", 0) > 0:
        notes.append("False positives are present. Review whether scoring or filters are too aggressive.")
    if match_counts.get("false_negative", 0) > 0:
        notes.append("False negatives are present. Review whether scoring is missing valid setups.")
    if issue_counts.get("bad_scoring", 0) > 0:
        notes.append("bad_scoring issue types are present. Create a targeted scoring hypothesis, then retest before changing logic.")
    if issue_counts.get("bad_bucket", 0) > 0:
        notes.append("bad_bucket issue types are present. Review dashboard grouping before scoring changes.")
    if len(rows) > 0 and match_counts.get("match", 0) >= max(1, len(rows) - 1) and len(problem_rows) == 1:
        notes.append("Most rows matched manual reads. Avoid broad scoring changes from a single issue.")
    if (
        ("negative macd" in combined_text or "macd -" in combined_text)
        and (
            "above key moving averages" in combined_text
            or "above key mas" in combined_text
            or "above moving averages" in combined_text
            or "price above" in combined_text
            or "above ema" in combined_text
        )
    ):
        notes.append("Watch for a repeated pattern: negative MACD may be overweighted when price remains above key moving averages.")
    if "near resistance" in combined_text:
        notes.append("Watch for a repeated pattern: near-resistance context may need separate caution labeling rather than directional reversal.")

    notes.extend(
        [
            "Do not change scoring from one ticker.",
            "Keep manual TradingView confirmation.",
            "No broker/order automation.",
        ]
    )
    return notes


def summarize_calibration_results(rows: list[dict[str, Any]]) -> dict[str, Any]:
    match_status_counts = calibration_match_status_counts(rows)
    issue_type_counts = calibration_issue_type_counts(rows)
    summary: dict[str, Any] = {
        "total_rows": len(rows),
        "match_status_counts": match_status_counts,
        "issue_type_counts": issue_type_counts,
    }
    for status in CALIBRATION_SUMMARY_MATCH_STATUSES:
        summary[f"{status}_count"] = match_status_counts.get(status, 0)
    return summary


def calibration_rows_needing_review(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    needing_review: list[dict[str, Any]] = []
    for row in rows:
        issue_type = str(row.get("issue_type", "")).strip()
        match_status = str(row.get("match_status", "")).strip()
        if issue_type == "needs_manual_chart_confirmation" or match_status == "unclear":
            needing_review.append(row)
    return needing_review


def _current_session_calibration_rows(st: Any) -> list[dict[str, Any]]:
    rows = getattr(st, "session_state", {}).get("calibration_results_rows", [])
    if rows is None:
        return []
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")

    session_rows: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        row = {column: str(raw_row.get(column, "") or "").strip() for column in CALIBRATION_RESULT_COLUMNS}
        if any(row.values()):
            session_rows.append(row)
    return session_rows


def _example_manual_row() -> dict[str, Any]:
    return {
        "ticker": "EXAMPLE",
        "watchlist": "EXAMPLE_ONLY_DO_NOT_TRADE",
        "asset_type": "equity",
        "category": "manual_example",
        "timeframe": "15m",
        "close": 25.0,
        "ema9": 25.5,
        "ema21": 25.0,
        "wma50": 24.5,
        "wma200": 23.0,
        "sma200": 23.0,
        "macd_hist": 0.10,
        "macd_hist_prev": 0.05,
        "support1": 24.0,
        "support2": 23.0,
        "resistance1": 27.0,
        "resistance2": 29.0,
        "breakout": 27.0,
        "breakdown": 24.0,
        "invalid": 23.5,
        "avg_volume": 1000000,
        "relative_volume": 1.0,
        "manual_override": "false",
    }


def _manual_form_row(st: Any) -> dict[str, Any] | None:
    st.sidebar.markdown("**Manual Entry checklist**")
    st.sidebar.write("- Enter ticker.")
    st.sidebar.write("- Enter price/close.")
    st.sidebar.write("- Add key levels if known.")
    st.sidebar.write("- Review validation.")
    st.sidebar.write("- Download CSV only if you want your own copy.")
    with st.sidebar.form("manual_candidate_form"):
        st.caption("Quick Add Candidate — session-only until you download CSV.")
        st.caption("Step 1: add ticker, price, and timeframe. Step 2: review validation and edit chart levels if needed.")
        ticker = st.text_input("Ticker *", value="", help="Example: TSLA").strip().upper()
        asset_type = st.selectbox("Asset type *", ["equity", "index", "future", "forex", "crypto", "commodity"])
        timeframe = st.selectbox("Timeframe *", ["15m", "30m", "1h", "4h", "1D", "1W"])
        close = st.number_input("Price / close *", min_value=0.0, value=0.0, step=0.01, format="%.2f")
        category = st.text_input("Category", value="manual_candidate")
        bias_hint = st.selectbox("Bias note", ["", "bullish", "bearish", "neutral", "context"])
        state_note = st.selectbox("Review state note", ["watch", "alert", "priority_watch", "avoid", "context_watch"])
        setup_note = st.text_input("Setup note", value="manual entry")

        ema9 = ema21 = wma50 = wma200 = sma200 = close
        macd_hist = 0.0
        macd_hist_prev = 0.0
        support1 = support2 = resistance1 = resistance2 = breakout = breakdown = invalid = close
        avg_volume = 0
        relative_volume = 1.0

        with st.expander("Advanced levels and indicators", expanded=False):
            st.caption("Optional chart fields. Enter 0 if unknown, then verify manually.")
            ema9 = st.number_input("EMA 9", min_value=0.0, value=close, step=0.01, format="%.2f")
            ema21 = st.number_input("EMA 21", min_value=0.0, value=close, step=0.01, format="%.2f")
            wma50 = st.number_input("WMA 50", min_value=0.0, value=close, step=0.01, format="%.2f")
            wma200 = st.number_input("WMA 200", min_value=0.0, value=close, step=0.01, format="%.2f")
            sma200 = st.number_input("SMA 200", min_value=0.0, value=close, step=0.01, format="%.2f")
            macd_hist = st.number_input("MACD histogram", value=0.0, step=0.01, format="%.2f")
            macd_hist_prev = st.number_input("Previous MACD histogram", value=0.0, step=0.01, format="%.2f")
            support1 = st.number_input("Support 1", min_value=0.0, value=close, step=0.01, format="%.2f")
            support2 = st.number_input("Support 2", min_value=0.0, value=close, step=0.01, format="%.2f")
            resistance1 = st.number_input("Resistance 1", min_value=0.0, value=close, step=0.01, format="%.2f")
            resistance2 = st.number_input("Resistance 2", min_value=0.0, value=close, step=0.01, format="%.2f")
            breakout = st.number_input("Bullish trigger / breakout", min_value=0.0, value=close, step=0.01, format="%.2f")
            breakdown = st.number_input("Bearish trigger / breakdown", min_value=0.0, value=close, step=0.01, format="%.2f")
            invalid = st.number_input("Invalidation", min_value=0.0, value=close, step=0.01, format="%.2f")
            avg_volume = st.number_input("Average volume", min_value=0, value=0, step=1000)
            relative_volume = st.number_input("Relative volume", min_value=0.0, value=1.0, step=0.1, format="%.2f")

        notes = st.text_area("Notes", value="")
        submitted = st.form_submit_button("Add Candidate")

    if not submitted:
        return None
    if not ticker:
        st.sidebar.error("Ticker is required before a manual row can be added.")
        return None
    if close <= 0:
        st.sidebar.error("Price / close must be greater than 0 before a manual row can be added.")
        return None

    row = {column: "" for column in ALL_CANDIDATE_COLUMNS}
    row.update(
        {
            "ticker": ticker,
            "watchlist": notes or "MANUAL_SESSION_ONLY",
            "asset_type": asset_type,
            "category": category,
            "timeframe": timeframe,
            "close": close,
            "ema9": ema9,
            "ema21": ema21,
            "wma50": wma50,
            "wma200": wma200,
            "sma200": sma200,
            "macd_hist": macd_hist,
            "macd_hist_prev": macd_hist_prev,
            "support1": support1,
            "support2": support2,
            "resistance1": resistance1,
            "resistance2": resistance2,
            "breakout": breakout,
            "breakdown": breakdown,
            "invalid": invalid,
            "bullish_trigger": breakout,
            "bearish_trigger": breakdown,
            "bullish_invalid": invalid,
            "avg_volume": avg_volume,
            "relative_volume": relative_volume,
            "manual_override": "false",
        }
    )
    if bias_hint or setup_note or state_note:
        row["watchlist"] = (
            f"MANUAL_SESSION_ONLY | bias={bias_hint} | setup={setup_note} | "
            f"state={state_note} | notes={notes}"
        )
    return row


def _validation_repair_tips(messages: list[str]) -> list[str]:
    tips: list[str] = []
    for message in messages:
        lowered = message.lower()
        if "missing required columns" in lowered:
            tips.append("Use the template header or Download CSV, then paste/upload with all required columns.")
        elif "ticker is blank" in lowered:
            tips.append("Add a ticker symbol, such as TSLA, in the ticker field or table.")
        elif "asset_type is blank" in lowered:
            tips.append("Choose an asset type, usually equity for stocks or index for SPY/QQQ.")
        elif "is not numeric" in lowered:
            tips.append("Replace text in price, level, volume, and option fields with plain numbers only.")
        elif "candidate file has no rows" in lowered:
            tips.append("Add a manual row, upload a CSV, paste CSV text, or use the sample row for testing.")
        elif "optional options field blank" in lowered:
            tips.append("Blank options fields are acceptable unless you are reviewing options candidates.")
        elif "optional column missing" in lowered:
            tips.append("Optional columns can be added later; use Download CSV to get the full column set.")
    unique: list[str] = []
    for tip in tips:
        if tip not in unique:
            unique.append(tip)
    return unique


def _validation_fix_guidance(messages: list[str]) -> list[str]:
    lowered = " | ".join(messages).lower()
    tips: list[str] = []

    if "ticker" in lowered or "missing required columns" in lowered:
        tips.append("Missing ticker: enter a ticker symbol.")
    if "close" in lowered or "price" in lowered or "missing required columns" in lowered:
        tips.append("Missing close/price: enter the current price/close.")
    if "timeframe" in lowered or "missing required columns" in lowered:
        tips.append("Missing timeframe: choose 15m, 30m, 1h, 4h, 1D, or 1W.")
    level_fields = [
        "ema9",
        "ema21",
        "wma50",
        "wma200",
        "sma200",
        "support",
        "resistance",
        "breakout",
        "breakdown",
        "invalid",
        "moving average",
        "level",
    ]
    if "missing required columns" in lowered or any(field in lowered for field in level_fields):
        tips.append("Missing moving average / level fields: enter 0 if unknown, then verify manually.")
    if "candidate file has no rows" in lowered:
        tips.append("Add one manual row or use the sample row for testing.")

    for tip in _validation_repair_tips(messages):
        if tip not in tips:
            tips.append(tip)
    return tips


def _show_validation_feedback(st: Any, rows: list[dict[str, Any]], user_supplied: bool) -> None:
    if not user_supplied:
        return
    errors, warnings = validate_candidate_rows(rows, ALL_CANDIDATE_COLUMNS)
    st.subheader("Candidate Validation")
    columns = st.columns(2)
    columns[0].metric("Blocking issues", len(errors))
    columns[1].metric("Warnings", len(warnings))
    if errors:
        st.error("Validation found issues. Repair them before using these rows for decision support.")
        for error in errors:
            st.write(f"- {error}")
    else:
        st.success("Candidate validation found no blocking errors.")
    if warnings:
        with st.expander("Validation warnings", expanded=bool(errors)):
            for warning in warnings:
                st.write(f"- {warning}")

    st.subheader("How to fix this")
    if errors:
        for tip in _validation_fix_guidance(errors + warnings):
            st.write(f"- {tip}")
    else:
        st.write("No blocking validation errors. Still verify charts manually before any trading decision.")


def _show_download(st: Any, rows: list[dict[str, Any]], user_supplied: bool) -> None:
    if not user_supplied:
        return
    st.download_button(
        "Download CSV",
        data=_candidate_csv_text(rows),
        file_name="trading_autopilot_candidates.csv",
        mime="text/csv",
        help="Browser-only export. The app does not write this CSV to disk.",
    )


def _rows_from_editor_output(edited_rows: Any) -> list[dict[str, Any]]:
    if hasattr(edited_rows, "to_dict"):
        records = edited_rows.to_dict("records")
        return [dict(row) for row in records]
    return [dict(row) for row in edited_rows]


def _bucket(rows: list[dict[str, Any]], state: str | None = None, bias: str | None = None) -> list[dict[str, Any]]:
    output = rows
    if state:
        output = [row for row in output if row.get("state") == state]
    if bias:
        output = [row for row in output if row.get("bias") == bias]
    return output


def _line(row: dict[str, Any]) -> str:
    return f"{row['ticker']} | {row['bias']} | {row['score']} | {row['grade']} | {row['state']}"


def build_dashboard_sections(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    ranked = rank_candidates(rows)
    options = filter_options_candidates(rows)
    shares = filter_share_candidates(rows)
    covered = filter_covered_call_candidates(rows)

    directional_conflicts = [row for row in ranked if "directional_conflict" in row.get("setup_tags", [])]
    clean_alerts = [row for row in ranked if row["state"] == "alert" and "directional_conflict" not in row.get("setup_tags", [])]
    option_reviews = [row for row in options if row["options_review_state"] == "review"]
    share_reviews = [row for row in shares if row["shares_review_state"] == "review"]
    covered_income = [row for row in covered if row["covered_call_review_state"] == "income_review"]
    covered_defensive = [row for row in covered if row["covered_call_review_state"] == "defensive_review"]
    review_tickers = {row["ticker"] for row in option_reviews + share_reviews + covered_income + covered_defensive}
    journal_prep = [
        row
        for row in ranked
        if row["ticker"] in review_tickers
        and row["bias"] != "context"
        and row["state"] != "avoid"
        and "directional_conflict" not in row.get("setup_tags", [])
    ]

    return {
        "Market Context": [row for row in ranked if row["bias"] == "context"],
        "Bullish Options": [row for row in option_reviews if row["candidate_side"] == "call_review"],
        "Bearish Options": [row for row in option_reviews if row["candidate_side"] == "put_review"],
        "Directional Conflicts": directional_conflicts,
        "Pre-Confirmation": clean_alerts,
        "Under-$25 Shares": share_reviews,
        "Covered Call Income": covered_income,
        "Covered Call Defensive": covered_defensive,
        "Avoid List": [row for row in ranked if row["state"] == "avoid"],
        "Blocked Options": [row for row in options if row["options_review_state"] == "skip"],
        "Blocked Shares": [row for row in shares if row["shares_review_state"] == "skip"],
        "Blocked Covered Calls": [row for row in covered if row["covered_call_review_state"] == "skip"],
        "Alerts": [
            {"ticker": row["ticker"], "bias": row["bias"], "score": row["score"], "grade": row["grade"], "alert": suggestion}
            for row in ranked
            for suggestion in row.get("alert_suggestions", [])
        ],
        "Journal Prep": journal_prep,
    }


def _dashboard_bucket_for_ticker(ticker: str, sections: dict[str, list[dict[str, Any]]]) -> str:
    target = str(ticker or "").strip().upper()
    if not target:
        return "unbucketed"
    for bucket in DASHBOARD_SECTION_NAMES:
        for row in sections.get(bucket, []):
            if str(row.get("ticker", "")).strip().upper() == target:
                return bucket
    return "unbucketed"


def _daily_review_card_bucket(ticker: str, sections: dict[str, list[dict[str, Any]]]) -> str:
    target = str(ticker or "").strip().upper()
    if not target:
        return "unbucketed"
    for bucket in DASHBOARD_SECTION_NAMES:
        for row in sections.get(bucket, []):
            if str(row.get("ticker", "")).strip().upper() == target:
                return bucket
    return "unbucketed"


def build_daily_review_cards(
    rows: list[dict[str, Any]],
    sections: dict[str, list[dict[str, Any]]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rank_candidates(rows):
        if row.get("bias") == "context":
            continue
        ticker = str(row.get("ticker", "") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        cards.append(
            {
                "ticker": ticker,
                "bias": row.get("bias", ""),
                "score": row.get("score", ""),
                "grade": row.get("grade", ""),
                "state": row.get("state", ""),
                "bucket": _daily_review_card_bucket(ticker, sections),
                "timeframe": row.get("timeframe", ""),
                "close": row.get("close", ""),
            }
        )
        if len(cards) >= limit:
            break
    return cards


def daily_review_status_summary(
    rows: list[dict[str, Any]],
    validation_errors: list[str],
    validation_warnings: list[str],
    calibration_rows: list[dict[str, Any]],
    batch_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    blocking_issues = len(validation_errors)
    candidate_count = len(rows)
    return {
        "candidate_count": candidate_count,
        "blocking_issues": blocking_issues,
        "warnings": len(validation_warnings),
        "calibration_results_rows": len(calibration_rows),
        "batch_log_rows": len(batch_rows),
        "ready_for_review": blocking_issues == 0 and candidate_count > 0,
    }


def daily_review_next_action(summary: dict[str, Any]) -> str:
    if int(summary.get("candidate_count", 0) or 0) == 0:
        return "Import or paste a ticker row first."
    if int(summary.get("blocking_issues", 0) or 0) > 0:
        return "Fix blocking validation issues before review."
    if int(summary.get("calibration_results_rows", 0) or 0) == 0:
        return "Open Calibration Results to create review rows."
    if int(summary.get("batch_log_rows", 0) or 0) == 0:
        return "Apply labels, then add rows to Calibration Batch Log."
    return "Open Calibration Review and use Calibration Batch Log."


def app_product_status_summary(
    rows: list[dict[str, Any]],
    validation_errors: list[str],
    validation_warnings: list[str],
    calibration_rows: list[dict[str, Any]],
    batch_rows: list[dict[str, Any]],
    provider_status: str,
) -> dict[str, Any]:
    candidate_count = len(rows)
    blocking_issues = len(validation_errors)
    provider = str(provider_status or "disabled").strip().lower() or "disabled"
    if provider == "polygon":
        provider_label = "Connected to Polygon"
    elif provider == "disabled":
        provider_label = "Manual import mode"
    else:
        provider_label = provider

    ready_status = "Ready" if candidate_count > 0 and blocking_issues == 0 else "Needs Fix"
    if candidate_count == 0:
        ready_status = "Review Next"

    return {
        "app_version": APP_VERSION,
        "provider_status": provider,
        "provider_label": provider_label,
        "candidate_count": candidate_count,
        "blocking_issues": blocking_issues,
        "warnings": len(validation_warnings),
        "calibration_results_rows": len(calibration_rows),
        "batch_log_rows": len(batch_rows),
        "ready_status": ready_status,
        "ready_for_review": candidate_count > 0 and blocking_issues == 0,
        "live_data_status": "Live Data Ready" if provider == "polygon" else "Manual Confirmation Required",
    }


def product_next_best_action(summary: dict[str, Any]) -> str:
    if int(summary.get("candidate_count", 0) or 0) == 0:
        provider = str(summary.get("provider_status", "") or "").strip().lower()
        if provider == "polygon":
            return "Start with Live Market Data or Market Breakdown."
        return "Live data is not connected. Use Sample data, manual import, or Market Breakdown EXAMPLE mode."
    if int(summary.get("blocking_issues", 0) or 0) > 0:
        return "Fix blocking validation issues before reviewing."
    provider = str(summary.get("provider_status", "") or "").strip().lower()
    if provider == "disabled":
        return "Manual import works. Add provider secrets only if you want read-only live data."
    if provider == "polygon":
        return "Live data is connected. Review Market Breakdown cards and capture manual chart notes."
    if int(summary.get("calibration_results_rows", 0) or 0) == 0:
        return "Open Calibration Results after importing rows."
    if int(summary.get("batch_log_rows", 0) or 0) == 0:
        return "Apply labels and add rows to Batch Log when ready."
    return "Open Calibration Review to summarize your session."


def build_product_review_cards(
    rows: list[dict[str, Any]],
    sections: dict[str, list[dict[str, Any]]],
    limit: int = 6,
) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rank_candidates(rows):
        if row.get("bias") == "context":
            continue
        ticker = str(row.get("ticker", "") or "").strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        cards.append(
            {
                "ticker": ticker,
                "bias": row.get("bias", ""),
                "score": row.get("score", ""),
                "grade": row.get("grade", ""),
                "state": row.get("state", ""),
                "bucket": _daily_review_card_bucket(ticker, sections),
            }
        )
        if len(cards) >= limit:
            break
    return cards


def calibration_label_template_csv(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=CALIBRATION_LABEL_COLUMNS, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "ticker": str(row.get("ticker", "") or "").strip().upper(),
                "timeframe_checked": str(row.get("timeframe_checked", "") or "15m").strip() or "15m",
                "manual_chart_bias": str(row.get("manual_chart_bias", "") or "unclear").strip() or "unclear",
                "match_status": str(row.get("match_status", "") or "unclear").strip() or "unclear",
                "issue_type": str(row.get("issue_type", "") or "needs_manual_chart_confirmation").strip()
                or "needs_manual_chart_confirmation",
                "follow_up": str(row.get("follow_up", "") or "confirm chart manually before scoring changes").strip()
                or "confirm chart manually before scoring changes",
            }
        )
    return output.getvalue()


def _calibration_label_keys(rows: list[dict[str, Any]]) -> set[str]:
    return {_calibration_label_key(row) for row in rows if _calibration_label_key(row).strip("|")}


def current_label_template_rows(st: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    current_rows = [_normalize_calibration_result_row(row) for row in rows if isinstance(row, dict)]
    session_rows = _current_session_calibration_rows(st)
    if not current_rows:
        return []
    if not session_rows:
        return current_rows

    current_keys = _calibration_label_keys(current_rows)
    session_keys = _calibration_label_keys(session_rows)
    if current_keys and session_keys == current_keys:
        return session_rows
    return current_rows


def label_template_staleness_warning(template_rows: list[dict[str, Any]], current_rows: list[dict[str, Any]]) -> str:
    template_keys = _calibration_label_keys(template_rows)
    current_keys = _calibration_label_keys(current_rows)
    if not current_keys:
        return "Open Calibration Results after importing rows to create label-ready rows."
    if template_keys and template_keys != current_keys:
        return "Label template may be stale. Open Calibration Results to refresh current rows."
    return ""


def _format_optional_number(value: Any) -> str:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return ""
    try:
        number = float(text)
    except ValueError:
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def _extract_import_note_from_watchlist(watchlist: str, key: str) -> str:
    prefix = f"{key}="
    for part in str(watchlist or "").split(" | "):
        text = part.strip()
        if text.startswith(prefix):
            return text[len(prefix) :].strip()
    return ""


def _calibration_key_levels_from_row(row: dict[str, Any]) -> str:
    support1 = _format_optional_number(row.get("support1"))
    resistance1 = _format_optional_number(row.get("resistance1"))
    close = _format_optional_number(row.get("close"))
    if support1 and resistance1 and (support1 != close or resistance1 != close):
        return f"support {support1} / resistance {resistance1}"

    key_level_note = str(row.get("key_level_note", "") or "").strip()
    if not key_level_note:
        key_level_note = _extract_import_note_from_watchlist(str(row.get("watchlist", "") or ""), "key_level_note")
    return key_level_note or "not sure yet"


def _clean_imported_note(note: str) -> str:
    text = str(note or "").strip()
    for prefix in ["CALIBRATION ONLY —", "CALIBRATION ONLY -", "CALIBRATION ONLY"]:
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    return text.strip(" ;")


def _calibration_manual_notes_from_row(row: dict[str, Any]) -> str:
    watchlist = str(row.get("watchlist", "") or "")
    category = str(row.get("category", "") or "").strip()
    is_import = category == "tradingview_import" or "TRADINGVIEW_IMPORT_SESSION_ONLY" in watchlist
    imported_note = _extract_import_note_from_watchlist(watchlist, "notes")
    if not is_import and not imported_note:
        return "CALIBRATION ONLY — verify manually"

    ticker = str(row.get("ticker", "") or "").strip().upper()
    timeframe = str(row.get("timeframe", "") or "").strip()
    heading_parts = [part for part in [ticker, timeframe, "rich import"] if part]
    note_parts = [f"CALIBRATION ONLY — {' '.join(heading_parts) or 'rich import'}"]

    for label, key in [
        ("price", "close"),
        ("EMA9", "ema9"),
        ("EMA21", "ema21"),
        ("SMA200", "sma200"),
        ("MACD", "macd_hist"),
    ]:
        value = _format_optional_number(row.get(key))
        if value:
            note_parts.append(f"{label} {value}")

    cleaned_note = _clean_imported_note(imported_note)
    if cleaned_note and "rich import" not in cleaned_note.lower():
        note_parts.append(cleaned_note)
    if not any("no orders" in part.lower() for part in note_parts):
        note_parts.append("no orders")
    return "; ".join(note_parts)


def build_calibration_result_rows(rows: list[dict[str, Any]], sections: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    result_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rank_candidates(rows):
        if row.get("bias") == "context":
            continue
        ticker = str(row.get("ticker", "")).strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        result_rows.append(
            {
                "ticker": ticker,
                "manual_chart_bias": "unclear",
                "timeframe_checked": str(row.get("timeframe", "") or "15m"),
                "key_levels": _calibration_key_levels_from_row(row),
                "manual_notes": _calibration_manual_notes_from_row(row),
                "dashboard_score": row.get("score", ""),
                "dashboard_grade": row.get("grade", ""),
                "dashboard_state": row.get("state", ""),
                "dashboard_bucket": _dashboard_bucket_for_ticker(ticker, sections),
                "match_status": "unclear",
                "issue_type": "needs_manual_chart_confirmation",
                "follow_up": "confirm chart manually before scoring changes",
            }
        )
    return result_rows


def _normalize_calibration_result_row(row: dict[str, Any]) -> dict[str, Any]:
    return {column: str(row.get(column, "") or "").strip() for column in CALIBRATION_RESULT_COLUMNS}


def _calibration_batch_log_rows(st: Any) -> list[dict[str, Any]]:
    rows = getattr(st, "session_state", {}).get("calibration_batch_log_rows", [])
    if rows is None:
        return []
    if hasattr(rows, "to_dict"):
        rows = rows.to_dict("records")
    normalized_rows: list[dict[str, Any]] = []
    for raw_row in rows:
        if not isinstance(raw_row, dict):
            continue
        row = _normalize_calibration_result_row(raw_row)
        if any(row.values()):
            normalized_rows.append(row)
    return normalized_rows


def _calibration_batch_row_key(row: dict[str, Any]) -> str:
    normalized_row = _normalize_calibration_result_row(row)
    ticker = normalized_row.get("ticker", "").strip().upper()
    timeframe = normalized_row.get("timeframe_checked", "").strip()
    if ticker:
        return f"{ticker}|{timeframe}"
    return str(sorted(normalized_row.items()))


def merge_calibration_batch_log_rows(
    existing_rows: list[dict[str, Any]],
    new_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    key_indexes: dict[str, int] = {}

    for raw_row in existing_rows:
        row = _normalize_calibration_result_row(raw_row)
        if not any(row.values()):
            continue
        key = _calibration_batch_row_key(row)
        key_indexes[key] = len(merged)
        merged.append(row)

    for raw_row in new_rows:
        row = _normalize_calibration_result_row(raw_row)
        if not any(row.values()):
            continue
        key = _calibration_batch_row_key(row)
        if key in key_indexes:
            merged[key_indexes[key]] = row
        else:
            key_indexes[key] = len(merged)
            merged.append(row)
    return merged


def build_top_review_summary(rows: list[dict[str, Any]], sections: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    ranked_non_context = [row for row in rank_candidates(rows) if row["bias"] != "context"][:5]
    return {
        "top_candidates": ranked_non_context,
        "bullish_options_review_count": len(sections["Bullish Options"]),
        "bearish_options_review_count": len(sections["Bearish Options"]),
        "under_25_shares_review_count": len(sections["Under-$25 Shares"]),
        "blocked_options_count": len(sections["Blocked Options"]),
        "blocked_shares_count": len(sections["Blocked Shares"]),
        "journal_prep_count": len(sections["Journal Prep"]),
    }


def _summary_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": row.get("ticker", ""),
        "bias": row.get("bias", ""),
        "score": row.get("score", ""),
        "grade": row.get("grade", ""),
        "state": row.get("state", ""),
    }


def _print_top_review_summary(summary: dict[str, Any]) -> None:
    print("\nTop Review Summary")
    print("- Top 5 ranked non-context candidates")
    if not summary["top_candidates"]:
        print("  - none")
    else:
        for row in summary["top_candidates"]:
            print(f"  - {_line(row)}")
    print(f"- Bullish options review count: {summary['bullish_options_review_count']}")
    print(f"- Bearish options review count: {summary['bearish_options_review_count']}")
    print(f"- Under-$25 shares review count: {summary['under_25_shares_review_count']}")
    print(f"- Blocked options count: {summary['blocked_options_count']}")
    print(f"- Blocked shares count: {summary['blocked_shares_count']}")
    print(f"- Journal prep count: {summary['journal_prep_count']}")


def cli_dashboard(path: Path = DEFAULT_DATA) -> None:
    path = _resolve_path(path)
    rows = _load(path)
    sections = build_dashboard_sections(rows)
    summary = build_top_review_summary(rows, sections)

    print(f"Trading Autopilot Dashboard {APP_VERSION}")
    print(f"Candidate file: {path}")
    for warning in _warning_lines(path):
        print(warning)
    print("Decision support only. No broker connection or order execution.")
    _print_top_review_summary(summary)

    for title in [
        "Market Context",
        "Bullish Options",
        "Bearish Options",
        "Directional Conflicts",
        "Pre-Confirmation",
        "Under-$25 Shares",
        "Covered Call Income",
        "Covered Call Defensive",
        "Avoid List",
        "Blocked Options",
        "Blocked Shares",
        "Blocked Covered Calls",
    ]:
        items = sections[title]
        print(f"\n{title}")
        if not items:
            print("- none")
            continue
        for row in items:
            suffix = ""
            if title.startswith("Covered Call") or title == "Blocked Covered Calls":
                suffix = f" | {row['covered_call_review_state']}"
            elif title == "Blocked Options":
                suffix = f" | {row['candidate_side']} | {'; '.join(row['options_filter_notes'])}"
            elif title == "Blocked Shares":
                suffix = f" | {'; '.join(row['share_filter_notes'])}"
            print(f"- {_line(row)}{suffix}")

    print("\nAlerts")
    for row in sections["Alerts"]:
        print(f"- {row['ticker']} | {row['bias']} | {row['score']} | {row['grade']} | {row['alert']}")

    print("\nJournal Prep")
    for row in sections["Journal Prep"][:5]:
        print(f"- {_line(row)}")


def _configured_access_code(st: Any) -> str | None:
    access_code = None
    try:
        access_code = st.secrets.get("APP_ACCESS_CODE")
    except Exception:
        access_code = None
    if not access_code:
        access_code = os.environ.get("APP_ACCESS_CODE")
    access_code = str(access_code or "").strip()
    return access_code or None


def _require_streamlit_access(st: Any) -> None:
    access_code = _configured_access_code(st)
    if not access_code:
        return

    entered = st.text_input("Access code", type="password")
    if entered != access_code:
        st.info("Enter the access code to view Trading Autopilot.")
        st.stop()


def _streamlit_candidate_source(st: Any, initial_path: Path) -> tuple[str, Path | None, list[dict[str, Any]], bool]:
    initial_path = _resolve_path(initial_path)
    source_options = [
        "Sample data",
        "real_candidates_template.csv",
        _review_engine_label(),
        "Upload CSV",
        "Paste CSV",
        "TradingView Import",
        "Manual Entry",
        "Custom CSV path",
    ]
    if _has_review_engine_session_rows(st):
        source_options.insert(2, "Review Engine Session")

    if _has_review_engine_session_rows(st):
        default_source = "Review Engine Session"
    elif initial_path.name == "sample_candidates.csv":
        default_source = "Sample data"
    elif initial_path.name == "real_candidates_template.csv":
        default_source = "real_candidates_template.csv"
    else:
        default_source = "Custom CSV path"

    if st.session_state.get("candidate_source") not in source_options:
        st.session_state.candidate_source = default_source

    source = st.sidebar.radio(
        "Candidate source",
        source_options,
        key="candidate_source",
    )
    st.session_state.tradingview_import_errors = []
    if source == "Sample data":
        return source, DEFAULT_DATA, _load(DEFAULT_DATA), False
    if source == "real_candidates_template.csv":
        return source, TEMPLATE_DATA, _load(TEMPLATE_DATA), False
    if source == "Review Engine Session":
        import_text = _current_review_engine_csv(st)
        st.sidebar.success("Session rows loaded from Send to Review Engine.")
        st.sidebar.caption(f"Source: {st.session_state.get('review_engine_source_label', 'session')}")
        if st.sidebar.button("Clear Review Engine Session"):
            st.session_state.review_engine_csv = ""
            st.session_state.review_engine_source_label = ""
            st.session_state.review_engine_loaded = False
            st.session_state.candidate_source = _review_engine_label()
            st.rerun()
        if not import_text.strip():
            st.info("No Review Engine session rows are loaded yet.")
            st.stop()
        try:
            import_rows = _parse_tradingview_import_text(import_text)
        except TradingViewImportParseError as exc:
            st.session_state.tradingview_import_errors = exc.errors
            import_rows = exc.rows
        else:
            st.session_state.tradingview_import_errors = []
        return "Review Engine Session", None, import_rows, True
    if source == _review_engine_label():
        st.sidebar.caption("Paste copy/paste table text here. This is the beginner-friendly TradingView Import path.")
        with st.sidebar.expander("Supported table fields", expanded=False):
            for column in TRADINGVIEW_IMPORT_SUPPORTED_COLUMNS:
                st.write(f"- {column}")
        st.sidebar.code(TRADINGVIEW_IMPORT_EXAMPLE, language="csv")
        import_text = st.sidebar.text_area(_review_engine_label(), height=180)
        if not import_text.strip():
            st.info("Paste rows into the Review Engine Paste Box to start review.")
            st.stop()
        try:
            import_rows = _parse_tradingview_import_text(import_text)
        except TradingViewImportParseError as exc:
            st.session_state.tradingview_import_errors = exc.errors
            import_rows = exc.rows
        else:
            st.session_state.tradingview_import_errors = []
        return _review_engine_label(), None, import_rows, True
    if source == "Upload CSV":
        uploaded = st.sidebar.file_uploader("Upload CSV", type=["csv"])
        if uploaded is None:
            st.info("Upload a CSV to review candidates.")
            st.stop()
        return str(uploaded.name or "uploaded CSV"), None, _load_csv_bytes(uploaded.getvalue()), True
    if source == "Paste CSV":
        pasted = st.sidebar.text_area("Paste CSV", height=180)
        if not pasted.strip():
            st.info("Paste CSV text to review candidates.")
            st.stop()
        return "pasted CSV", None, _load_csv_text(pasted), True
    if source == "TradingView Import":
        st.sidebar.caption("Paste copied/exported TradingView or scanner-style CSV rows. Session-only.")
        with st.sidebar.expander("Supported import columns", expanded=False):
            for column in TRADINGVIEW_IMPORT_SUPPORTED_COLUMNS:
                st.write(f"- {column}")
        st.sidebar.code(TRADINGVIEW_IMPORT_EXAMPLE, language="csv")
        import_text = st.sidebar.text_area("TradingView Import", height=180)
        if not import_text.strip():
            st.info("Paste TradingView or scanner rows to start import.")
            st.stop()
        try:
            import_rows = _parse_tradingview_import_text(import_text)
        except TradingViewImportParseError as exc:
            st.session_state.tradingview_import_errors = exc.errors
            import_rows = exc.rows
        else:
            st.session_state.tradingview_import_errors = []
        return "TradingView Import", None, import_rows, True
    if source == "Manual Entry":
        st.sidebar.info("Phone tip: add a quick row first, then repair or enrich it in the table.")
        st.session_state.setdefault("manual_candidates", [])
        new_row = _manual_form_row(st)
        if new_row is not None:
            st.session_state.manual_candidates.append(new_row)
        left, right = st.sidebar.columns(2)
        if left.button("Use Sample Row"):
            st.session_state.manual_candidates.append(_example_manual_row())
        if right.button("Clear Manual Candidates"):
            st.session_state.manual_candidates = []
        manual_rows = list(st.session_state.manual_candidates)
        return "Manual Entry", None, manual_rows, True
    custom_default = initial_path if default_source == "Custom CSV path" else WORKING_DATA
    custom_path = st.sidebar.text_input("Custom CSV path", value=str(custom_default))
    path = _resolve_path(custom_path)
    return str(path), path, _load(path), True


def _streamlit_warning_lines(path: Path | None, user_supplied: bool) -> list[str]:
    if user_supplied:
        return [USER_SUPPLIED_WARNING]
    if path is None:
        return [SAMPLE_WARNING, "To use live market data, open Market Breakdown or Live Data — Read Only."]
    if path.name in SAMPLE_DATA_NAMES:
        return [SAMPLE_WARNING, "To use live market data, open Market Breakdown or Live Data — Read Only."]
    lines = _warning_lines(path)
    if path.name in SAMPLE_DATA_NAMES:
        lines.append("To use live market data, open Market Breakdown or Live Data — Read Only.")
    return lines


def _show_tradingview_import_repair(st: Any, source_label: str) -> None:
    if source_label not in {"TradingView Import", _review_engine_label(), "Review Engine Session"}:
        return
    errors = list(st.session_state.get("tradingview_import_errors", []))
    if not errors:
        return
    st.subheader("Review Engine Paste Box Repair")
    st.caption("TradingView Import Repair is the advanced technical name for this helper.")
    st.error("Fix the import rows below, then paste again.")
    for error in errors:
        st.write(f"- {error}")
    st.write("- Missing ticker/symbol: add a ticker or symbol column value.")
    st.write("- Missing price/close/last: add a numeric price, close, or last value.")
    st.write("- Blank timeframe/interval is okay; the dashboard defaults it to 15m.")


def _show_phone_workflow(st: Any, source_label: str, user_supplied: bool) -> None:
    if not user_supplied:
        return
    with st.expander("Phone Workflow", expanded=source_label == "Manual Entry"):
        st.write("1. Add or import candidate rows.")
        st.write("2. Fix any Candidate Validation issues with How to fix this.")
        st.write("3. Review Top Review Summary and dashboard tabs.")
        st.write("4. Confirm charts manually in TradingView before any decision.")
        st.write("5. Use Download CSV only when you want your own browser export.")


def _show_manual_entry_empty_state(st: Any) -> None:
    st.info("No manual candidates yet. Add one row or use the sample row for testing.")


def _show_top_review_summary(st: Any, summary: dict[str, Any]) -> None:
    st.subheader("Top Review Summary")
    metric_columns = st.columns(3)
    metrics = [
        ("Bullish options", summary["bullish_options_review_count"]),
        ("Bearish options", summary["bearish_options_review_count"]),
        ("Under-$25 shares", summary["under_25_shares_review_count"]),
        ("Blocked options", summary["blocked_options_count"]),
        ("Blocked shares", summary["blocked_shares_count"]),
        ("Journal prep", summary["journal_prep_count"]),
    ]
    for index, (label, value) in enumerate(metrics):
        metric_columns[index % 3].metric(label, value)

    top_candidates = [_summary_row(row) for row in summary["top_candidates"]]
    if top_candidates:
        st.dataframe(top_candidates, width="stretch", hide_index=True)
    else:
        st.caption("No ranked non-context candidates.")


def _inject_product_styles(st: Any) -> None:
    st.markdown(
        """
        <style>
        :root {
            --ta-card-bg: rgba(15, 23, 42, 0.58);
            --ta-card-border: rgba(148, 163, 184, 0.28);
            --ta-text-soft: rgba(226, 232, 240, 0.76);
            --ta-success: #22c55e;
            --ta-info: #38bdf8;
            --ta-warning: #f59e0b;
            --ta-danger: #ef4444;
        }
        .block-container {
            padding-top: 2rem;
            max-width: 1180px;
        }
        .ta-hero {
            border: 1px solid rgba(56, 189, 248, 0.28);
            border-radius: 8px;
            padding: 1.25rem;
            margin: 0 0 1rem 0;
            background:
                linear-gradient(135deg, rgba(14, 165, 233, 0.16), rgba(34, 197, 94, 0.08)),
                rgba(15, 23, 42, 0.52);
        }
        .ta-hero-kicker {
            color: var(--ta-info);
            font-size: 0.82rem;
            font-weight: 800;
            letter-spacing: 0;
            text-transform: uppercase;
            margin-bottom: 0.35rem;
        }
        .ta-hero-title {
            font-size: clamp(2rem, 5vw, 3.35rem);
            font-weight: 850;
            line-height: 1.03;
            margin-bottom: 0.45rem;
        }
        .ta-hero-subtitle {
            color: var(--ta-text-soft);
            font-size: 1.05rem;
            max-width: 840px;
            line-height: 1.45;
        }
        .ta-badge-row {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            margin-top: 0.95rem;
        }
        .ta-badge {
            border: 1px solid var(--ta-card-border);
            border-radius: 999px;
            padding: 0.34rem 0.62rem;
            background: rgba(15, 23, 42, 0.62);
            font-size: 0.84rem;
            font-weight: 700;
        }
        .ta-badge-success { border-color: rgba(34, 197, 94, 0.55); color: #bbf7d0; }
        .ta-badge-info { border-color: rgba(56, 189, 248, 0.55); color: #bae6fd; }
        .ta-badge-warning { border-color: rgba(245, 158, 11, 0.55); color: #fde68a; }
        .ta-badge-neutral { color: #e2e8f0; }
        .ta-safety-strip {
            border: 1px solid rgba(245, 158, 11, 0.32);
            border-left: 4px solid var(--ta-warning);
            border-radius: 8px;
            padding: 0.8rem 0.9rem;
            margin: 0.85rem 0;
            background: rgba(245, 158, 11, 0.09);
            color: #fde68a;
            font-weight: 650;
        }
        .product-card, .ta-step-card, .ta-feature-card, .breakdown-card {
            border: 1px solid var(--ta-card-border);
            border-radius: 8px;
            padding: 0.95rem;
            min-height: 118px;
            margin-bottom: 0.75rem;
            background: var(--ta-card-bg);
            box-shadow: 0 12px 36px rgba(2, 6, 23, 0.18);
        }
        .product-card-title, .ta-card-title, .breakdown-card-title {
            font-weight: 800;
            margin-bottom: 0.35rem;
        }
        .product-card-body, .ta-card-body, .breakdown-card-meta {
            color: var(--ta-text-soft);
            line-height: 1.45;
        }
        .ta-step-number {
            width: 2rem;
            height: 2rem;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            border-radius: 999px;
            margin-bottom: 0.5rem;
            background: rgba(56, 189, 248, 0.16);
            color: #bae6fd;
            font-weight: 800;
        }
        .ta-action-hint {
            color: #bae6fd;
            font-weight: 750;
            margin-top: 0.45rem;
        }
        .product-card-success, .ta-card-success { border-left: 4px solid var(--ta-success); }
        .product-card-warning, .ta-card-warning { border-left: 4px solid var(--ta-warning); }
        .product-card-error, .ta-card-error { border-left: 4px solid var(--ta-danger); }
        .product-card-info, .ta-card-info { border-left: 4px solid var(--ta-info); }
        .stAlert {
            border-radius: 8px;
        }
        div[data-testid="stMetric"] {
            border: 1px solid var(--ta-card-border);
            border-radius: 8px;
            padding: 0.7rem 0.8rem;
            background: rgba(15, 23, 42, 0.38);
        }
        @media (max-width: 700px) {
            .block-container { padding-left: 1rem; padding-right: 1rem; }
            .ta-hero { padding: 1rem; }
            .ta-hero-title { font-size: 2rem; }
            .ta-step-card, .ta-feature-card, .product-card { min-height: unset; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _status_class(status: str) -> str:
    return {
        "success": "success",
        "warning": "warning",
        "error": "error",
    }.get(status, "info")


def _render_hero(st: Any, title: str, subtitle: str, status_text: str) -> None:
    st.markdown(
        f"""
        <div class="ta-hero">
          <div class="ta-hero-kicker">Start Review</div>
          <div class="ta-hero-title">{html.escape(title)}</div>
          <div class="ta-hero-subtitle">{html.escape(subtitle)}</div>
          <div class="ta-badge-row">
            <span class="ta-badge ta-badge-success">{html.escape(status_text)}</span>
            <span class="ta-badge ta-badge-info">Decision-support only</span>
            <span class="ta-badge ta-badge-warning">No broker connected</span>
            <span class="ta-badge ta-badge-neutral">Manual confirmation required</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_status_badge(st: Any, label: str, value: str, status: str = "info") -> None:
    badge_class = f"ta-badge-{_status_class(status)}"
    st.markdown(
        f"<span class='ta-badge {badge_class}'>{html.escape(label)}: {html.escape(str(value))}</span>",
        unsafe_allow_html=True,
    )


def _render_step_card(st: Any, step_number: int, title: str, body: str) -> None:
    st.markdown(
        f"""
        <div class="ta-step-card ta-card-info">
          <div class="ta-step-number">{step_number}</div>
          <div class="ta-card-title">{html.escape(title)}</div>
          <div class="ta-card-body">{html.escape(body)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_feature_card(st: Any, title: str, body: str, action_hint: str, status: str = "info") -> None:
    st.markdown(
        f"""
        <div class="ta-feature-card ta-card-{_status_class(status)}">
          <div class="ta-card-title">{html.escape(title)}</div>
          <div class="ta-card-body">{html.escape(body)}</div>
          <div class="ta-action-hint">{html.escape(action_hint)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_safety_strip(st: Any) -> None:
    st.markdown(
        "<div class='ta-safety-strip'>Decision-support only. No broker connection, no orders, no automatic alerts, no payment workflows.</div>",
        unsafe_allow_html=True,
    )


def _beginner_term_help() -> list[dict[str, str]]:
    return [
        {"term": "TradingView Import", "plain": "Review Engine Paste Box", "meaning": "Paste rows into the dashboard review engine."},
        {"term": "CSV", "plain": "copy/paste table text", "meaning": "A simple text table you can copy between sections."},
        {"term": "Calibration", "plain": "Accuracy Review", "meaning": "Compare the dashboard read with your manual chart read."},
        {"term": "Blocking issue", "plain": "fix-before-review problem", "meaning": "A missing or invalid field that must be repaired first."},
        {"term": "Alert Planner", "plain": "draft reminder planner", "meaning": "A manual draft only. No live alert is created."},
        {"term": "Pine helper", "plain": "chart value helper", "meaning": "A manual helper for chart values. It does not create alerts."},
        {"term": "Chart Workspace", "plain": "manual chart notes", "meaning": "Where you capture support, resistance, bias, and context."},
    ]


def _review_engine_label() -> str:
    return "Review Engine Paste Box"


def _current_review_engine_csv(st: Any) -> str:
    return str(st.session_state.get("review_engine_csv", "") or "").strip()


def _has_review_engine_session_rows(st: Any) -> bool:
    return bool(_current_review_engine_csv(st))


def _send_rows_to_review_session(st: Any, csv_text: str, source_label: str) -> None:
    clean_csv = str(csv_text or "").strip()
    if not clean_csv:
        return
    st.session_state.review_engine_csv = clean_csv
    st.session_state.review_engine_source_label = str(source_label or "Session Rows").strip() or "Session Rows"
    st.session_state.review_engine_loaded = True
    st.session_state.candidate_source = "Review Engine Session"


def review_engine_status_summary(
    rows: list[dict[str, Any]],
    validation_errors: list[str],
    validation_warnings: list[str],
    source_label: str,
) -> dict[str, Any]:
    source = str(source_label or "").strip() or "unknown"
    session_loaded = source == "Review Engine Session"
    row_count = len(rows)
    blocking_count = len(validation_errors)
    if blocking_count:
        next_step = "Fix blocking issues"
    elif row_count:
        next_step = "Open Daily Review / Calibration Results"
    else:
        next_step = "Analyze Live Market Data"
    return {
        "session_rows_loaded": "yes" if session_loaded and row_count else "no",
        "source": source,
        "row_count": row_count,
        "blocking_issues": blocking_count,
        "warnings": len(validation_warnings),
        "next_step": next_step,
        "ready_for_review": bool(row_count and not blocking_count),
    }


def _show_review_engine_status(
    st: Any,
    rows: list[dict[str, Any]],
    validation_errors: list[str],
    validation_warnings: list[str],
    source_label: str,
) -> None:
    status = review_engine_status_summary(rows, validation_errors, validation_warnings, source_label)
    st.subheader("Review Engine Status")
    columns = st.columns(4)
    columns[0].metric("Session rows loaded", status["session_rows_loaded"])
    columns[1].metric("Source", status["source"])
    columns[2].metric("Blocking issues", status["blocking_issues"])
    columns[3].metric("Next step", status["next_step"])
    if status["ready_for_review"]:
        st.success("Rows are ready for Daily Review. Manual chart confirmation still applies.")
    elif status["blocking_issues"]:
        st.error("Fix blocking issues before Daily Review.")
    else:
        st.info("Start with Live Market Data, Market Breakdown, Chart Workspace, or the Review Engine Paste Box.")


def _show_review_engine_explainer(st: Any) -> None:
    st.subheader("Review Engine Paste Box")
    st.write("Friendly name: Review Engine Paste Box.")
    st.write("Technical name: TradingView Import.")
    st.write("What it means: copy/paste table text goes through the same validation engine as the advanced import workflow.")
    st.write("Nothing is written to disk. Nothing is downloaded. No orders or alerts are created.")


def _show_start_review_flow(st: Any, provider: str, config: dict[str, str], config_errors: list[str]) -> None:
    provider_text = _plain_live_data_provider_status(provider, config_errors)
    st.subheader("Start Review")
    st.caption("Analyze live market data, read plain-English cards, then send rows into the Review Engine.")
    steps = [
        ("Enter tickers", "Start with SPY, QQQ, AAPL or your watchlist."),
        ("Choose timeframe", "1D is the default first-run view."),
        ("Analyze Live Market Data", "Use read-only Polygon data when connected."),
        ("Read cards", "Review bias, trend, momentum, levels, and risk flags."),
        ("Send rows to Review Engine", "Load rows into validation without manual copy/paste."),
        ("Open Daily Review / Calibration Results", "Check status, labels, and accuracy review."),
    ]
    for start in range(0, len(steps), 3):
        columns = st.columns(3)
        for offset, (title, body) in enumerate(steps[start : start + 3], start=start + 1):
            with columns[offset - start - 1]:
                _render_step_card(st, offset, title, body)
    if provider_text.endswith("Polygon"):
        st.success(provider_text)
    else:
        st.info("Live data is not connected. You can still use Sample data or manual import.")
    _show_review_engine_explainer(st)


def _render_product_card(st: Any, title: str, body: str, status: str = "info") -> None:
    status_class = {
        "success": "product-card-success",
        "warning": "product-card-warning",
        "error": "product-card-error",
    }.get(status, "product-card-info")
    st.markdown(
        f"""
        <div class="product-card {status_class}">
          <div class="product-card-title">{title}</div>
          <div class="product-card-body">{body}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _plain_live_data_provider_status(provider: str, config_errors: list[str]) -> str:
    clean_provider = str(provider or "disabled").strip().lower() or "disabled"
    if clean_provider == "polygon" and not config_errors:
        return "Live data connected: Polygon"
    return "Live data not connected"


def _show_product_home(st: Any, rows: list[dict[str, Any]], sections: dict[str, list[dict[str, Any]]], source_label: str) -> None:
    validation_errors, validation_warnings = validate_candidate_rows(rows, ALL_CANDIDATE_COLUMNS)
    calibration_rows = _current_session_calibration_rows(st)
    batch_rows = _calibration_batch_log_rows(st)
    config = _market_data_config_from_streamlit(st)
    provider = configured_market_data_provider(config)
    config_errors = market_data_config_errors(config)
    provider_status = "missing" if provider != "disabled" and config_errors else provider
    summary = app_product_status_summary(
        rows,
        validation_errors,
        validation_warnings,
        calibration_rows,
        batch_rows,
        provider_status,
    )
    provider_text = _plain_live_data_provider_status(provider, config_errors)

    _render_hero(
        st,
        "Trading Autopilot",
        "Live market data, chart review, alert planning, and daily decision support — without broker connection or order execution.",
        provider_text,
    )
    _render_safety_strip(st)
    _show_start_review_flow(st, provider, config, config_errors)

    st.subheader("Beginner actions")
    action_columns = st.columns(5)
    action_cards = [
        (
            "Start with Live Market Data",
            "Analyze SPY, QQQ, AAPL with Polygon.",
            "Use Home or Market Breakdown.",
            "success" if summary["provider_status"] == "polygon" else "info",
        ),
        ("Open Market Breakdown", "Plain-English ticker cards.", "Use the Market Breakdown tab.", "info"),
        ("Open Chart Workspace", "Capture support, resistance, bias, and notes.", "Use Chart Workspace.", "info"),
        ("Open Alert Planner", "Draft manual alert plans. No alerts are created.", "Use Alert Planner.", "warning"),
        ("Open Daily Review", "Review validated rows.", "Use Daily Review after paste/validation.", "info"),
    ]
    for column, (title, body, action_hint, status) in zip(action_columns, action_cards):
        with column:
            _render_feature_card(st, title, body, action_hint, status)

    st.caption("Use the tabs below: Market Breakdown / Chart Workspace / Alert Planner / Daily Review.")

    metric_columns = st.columns(3)
    metrics = [
        ("App version", summary["app_version"]),
        ("Provider status", summary["provider_status"]),
        ("Candidates", summary["candidate_count"]),
        ("Blocking issues", summary["blocking_issues"]),
        ("Calibration rows", summary["calibration_results_rows"]),
        ("Batch Log rows", summary["batch_log_rows"]),
    ]
    for index, (label, value) in enumerate(metrics):
        metric_columns[index % 3].metric(label, value)

    _show_review_engine_status(st, rows, validation_errors, validation_warnings, source_label)

    status_columns = st.columns(3)
    status_columns[0].metric("Ready status", summary["ready_status"])
    status_columns[1].metric("Review Next", product_next_best_action(summary))
    status_columns[2].metric("Live Data", summary["live_data_status"])
    st.caption("Ready means the current rows have no blocking validation issues. Manual Confirmation Required always applies.")

    _show_home_live_market_data(st, provider, config, config_errors)

    st.subheader("Next Best Action")
    st.info(product_next_best_action(summary))

    st.subheader("Top Review Cards")
    cards = build_product_review_cards(rows, sections, limit=6)
    if cards:
        for card in cards:
            _render_product_card(
                st,
                f"{card.get('ticker', 'UNKNOWN')} — {card.get('bias', 'unknown')} {card.get('grade', '')}",
                (
                    f"Score {card.get('score', 'n/a')} · state {card.get('state', 'n/a')} · "
                    f"bucket {card.get('bucket', 'unbucketed')}"
                ),
                "success" if str(card.get("state", "")).lower() in {"alert", "priority_watch"} else "info",
            )
    else:
        st.info("No review cards yet. Start with Live Market Data, Market Breakdown, or the Review Engine Paste Box.")

    st.subheader("Beginner translation")
    st.caption("TradingView Import = Review Engine Paste Box.")
    st.table(_beginner_term_help())

    st.subheader("Beginner help")
    st.write("- Start on Home.")
    st.write("- Use Start with Live Market Data if Polygon is connected.")
    st.write("- Open Market Breakdown when you want the same plain-English cards in a dedicated tab.")
    st.write("- Use Chart Workspace when you want to capture manual TradingView chart notes.")
    st.write("- Use Alert Planner when you want manual alert ideas after chart confirmation.")
    st.write("- If Live Data says polygon, Market Breakdown can analyze a watchlist from read-only provider rows.")
    st.write("- If Live Data is disabled, manual import still works.")
    st.write("- Do not worry about advanced tabs at first; they remain available for deeper review.")

    st.subheader("What this app will not do")
    for item in [
        "Place trades.",
        "Connect brokers.",
        "Create alerts automatically.",
        "Manage payments.",
        "Replace chart confirmation.",
    ]:
        st.write(f"- {item}")


def _show_daily_review(st: Any, rows: list[dict[str, Any]], sections: dict[str, list[dict[str, Any]]], source_label: str) -> None:
    st.warning(DAILY_REVIEW_WARNING)

    validation_errors, validation_warnings = validate_candidate_rows(rows, ALL_CANDIDATE_COLUMNS)
    base_calibration_rows = build_calibration_result_rows(rows, sections)
    session_rows = current_label_template_rows(st, base_calibration_rows)
    batch_rows = _calibration_batch_log_rows(st)
    status_summary = daily_review_status_summary(
        rows,
        validation_errors,
        validation_warnings,
        session_rows,
        batch_rows,
    )

    st.subheader("Today’s Review Flow")
    for step in [
        "Use Start with Live Market Data or Market Breakdown first if you want plain-English ticker explanations.",
        "Use Chart Workspace to capture manual support/resistance, bias, fundamentals, and macro notes.",
        "Use Alert Planner only for manual alert ideas after chart confirmation.",
        "Import row.",
        "Fix blocking issues.",
        "Review card.",
        "Apply labels.",
        "Add to Batch Log.",
        "Review batch.",
        "Stop if unsure.",
    ]:
        st.write(f"- {step}")

    st.subheader("Daily Review status summary")
    metric_columns = st.columns(3)
    metrics = [
        ("Candidates", status_summary["candidate_count"]),
        ("Blocking issues", status_summary["blocking_issues"]),
        ("Warnings", status_summary["warnings"]),
        ("Calibration rows", status_summary["calibration_results_rows"]),
        ("Batch Log rows", status_summary["batch_log_rows"]),
        ("Ready", "yes" if status_summary["ready_for_review"] else "no"),
    ]
    for index, (label, value) in enumerate(metrics):
        metric_columns[index % 3].metric(label, value)

    _show_review_engine_status(st, rows, validation_errors, validation_warnings, source_label)

    st.subheader("Next Best Action")
    next_action = daily_review_next_action(status_summary)
    if status_summary["ready_for_review"]:
        st.success(next_action)
    else:
        st.info(next_action)

    st.subheader("60-second workflow")
    for step in [
        "1. Use Send to Review Engine or paste into the Review Engine Paste Box.",
        "2. Confirm Blocking issues = 0.",
        "3. Review Top Review Summary.",
        "4. Open Accuracy Review (Calibration Results).",
        "5. Apply labels with Calibration Labels CSV.",
        "6. Add to Calibration Batch Log.",
        "7. Open Calibration Review.",
        "8. Keep manual TradingView confirmation.",
        "9. Do not place orders from the app.",
    ]:
        st.write(step)

    st.subheader("Current validation status")
    metric_columns = st.columns(2)
    metric_columns[0].metric("Blocking issues", len(validation_errors))
    metric_columns[1].metric("Warnings", len(validation_warnings))
    if validation_errors:
        st.error("Fix blocking validation issues before review.")
    else:
        st.success("Ready for review. Still verify charts manually.")

    st.subheader("Review Cards")
    cards = build_daily_review_cards(rows, sections)
    if cards:
        st.table(cards)
    else:
        st.info("No non-context ranked candidates to review yet.")

    st.subheader("Copy/Paste Helpers")
    staleness_warning = label_template_staleness_warning(session_rows, base_calibration_rows)
    if staleness_warning:
        st.info(staleness_warning)
    if session_rows:
        st.caption("Edit manual_chart_bias, match_status, issue_type, and follow_up before applying.")
        st.write("Label CSV Template")
        st.caption("Current imported rows only. Copy this into Calibration Labels CSV if you want to label rows faster.")
        st.code(calibration_label_template_csv(session_rows), language="csv")
    else:
        st.caption("Open Calibration Results to create label-ready rows.")

    st.subheader("Stop Conditions")
    for stop_condition in [
        "Blocking issues are not 0.",
        "Values are missing or invented.",
        "Dashboard and chart disagree and you cannot explain why.",
        "A broker/order/payment/credential panel appears.",
        "You feel rushed or unclear.",
        "Any order, alert, broker, or payment action would be required.",
    ]:
        st.write(f"- {stop_condition}")


def _market_data_config_from_streamlit(st: Any) -> dict[str, str]:
    return get_market_data_provider_config(secrets=getattr(st, "secrets", None), environ=os.environ)


def _example_readonly_market_data_csv(timeframe: str) -> str:
    raw_bars = []
    for index in range(220):
        close = 95 + (index * 0.04)
        raw_bars.append(
            {
                "timestamp": f"EXAMPLE-{index + 1}",
                "open": close - 0.2,
                "high": close + 0.4,
                "low": close - 0.5,
                "close": close,
                "volume": 100000 + index,
            }
        )
    normalized = normalize_market_data_bars(raw_bars, ticker="EXAMPLE", timeframe=timeframe)
    indicators = compute_market_data_indicators(normalized)
    return market_data_rows_to_tradingview_import_csv(
        [
            {
                "ticker": "EXAMPLE",
                "timeframe": timeframe,
                **indicators,
            }
        ]
    )


def _parse_market_data_tickers(text: str) -> list[str]:
    tickers: list[str] = []
    seen: set[str] = set()
    for chunk in text.replace(",", "\n").splitlines():
        ticker = chunk.strip().upper()
        if not ticker or ticker in seen:
            continue
        seen.add(ticker)
        tickers.append(ticker)
    return tickers


def _market_breakdown_config_from_streamlit(st: Any) -> dict[str, str]:
    return _market_data_config_from_streamlit(st)


def _market_breakdown_rows_from_provider(
    tickers: list[str],
    timeframe: str,
    config: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    rows, errors = fetch_readonly_market_data_rows(tickers, timeframe, config)
    return build_market_breakdown_rows(rows), errors


def _breakdown_rows_to_import_csv(rows: list[dict[str, Any]]) -> str:
    return market_data_rows_to_tradingview_import_csv(rows)


def _example_market_breakdown_rows(timeframe: str) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for ticker, start, drift in [
        ("EXAMPLE", 95.0, 0.08),
        ("DEMO", 110.0, -0.05),
        ("MIXED", 50.0, 0.0),
    ]:
        raw_bars = []
        for index in range(220):
            close = start + (index * drift)
            if ticker == "MIXED":
                close = start + ((-1) ** index) * 0.35 + (index * 0.005)
            raw_bars.append(
                {
                    "timestamp": f"{ticker}-{index + 1}",
                    "open": close - 0.2,
                    "high": close + 0.45,
                    "low": close - 0.55,
                    "close": close,
                    "volume": 100000 + index,
                }
            )
        normalized = normalize_market_data_bars(raw_bars, ticker=ticker, timeframe=timeframe)
        indicators = compute_market_data_indicators(normalized)
        examples.append(
            build_market_breakdown_row(
                {
                    "ticker": ticker,
                    "timeframe": timeframe,
                    **indicators,
                }
            )
        )
    return examples


def _render_breakdown_card(st: Any, row: dict[str, Any]) -> None:
    st.markdown(
        """
        <style>
        .breakdown-card {
            border: 1px solid rgba(148, 163, 184, 0.35);
            border-radius: 8px;
            padding: 0.8rem 0.9rem;
            margin: 0.65rem 0;
        }
        .breakdown-card-title {
            font-size: 1.05rem;
            font-weight: 700;
            margin-bottom: 0.2rem;
        }
        .breakdown-card-meta {
            color: #64748b;
            font-size: 0.9rem;
            margin-bottom: 0.4rem;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    ticker = html.escape(str(row.get("ticker", "UNKNOWN")))
    meta = (
        f"Bias: {row.get('bias', 'unknown')} · Confidence: {row.get('confidence', 'C')} · "
        f"Price: {row.get('price', 'unknown')} · {row.get('timeframe', 'unknown')}"
    )
    meta = html.escape(meta)
    st.markdown(
        f"<div class='breakdown-card'><div class='breakdown-card-title'>{ticker}</div>"
        f"<div class='breakdown-card-meta'>{meta}</div></div>",
        unsafe_allow_html=True,
    )

    summary_columns = st.columns(3)
    summary_columns[0].caption("Trend")
    summary_columns[0].info(str(row.get("trend_summary", "Trend needs manual confirmation.")))
    summary_columns[1].caption("Momentum")
    summary_columns[1].info(str(row.get("momentum_summary", "Momentum needs manual confirmation.")))
    summary_columns[2].caption("Levels")
    summary_columns[2].info(str(row.get("level_summary", "Levels need manual confirmation.")))

    flags = row.get("risk_flags")
    if not isinstance(flags, list):
        flags = []
    if flags:
        st.warning("Risk flags: " + ", ".join(str(flag) for flag in flags))
    else:
        st.success("Risk flags: none from available read-only fields")

    st.write("Why this read:")
    explanation = row.get("explanation")
    if not isinstance(explanation, list):
        explanation = ["Manual chart confirmation is still required."]
    for bullet in explanation:
        st.write(f"- {bullet}")
    st.write("What I’d check next")
    st.info(str(row.get("next_action", "Verify chart manually before any decision.")))


def _show_home_live_market_data(st: Any, provider: str, config: dict[str, str], config_errors: list[str]) -> None:
    st.subheader("Start with Live Market Data")
    st.caption("This uses read-only Polygon data. Type a watchlist, read the cards, then verify charts manually.")

    provider_text = _plain_live_data_provider_status(provider, config_errors)
    if provider_text.endswith("Polygon"):
        st.success(provider_text)
    else:
        st.info(provider_text)
        st.write("Live data is not connected. You can still use Sample data or manual import.")

    input_columns = st.columns([2, 1])
    watchlist = input_columns[0].text_area(
        "Enter tickers",
        value="SPY, QQQ, AAPL",
        height=90,
        key="home_live_market_watchlist",
        help="Comma- or line-separated ticker symbols. Limited to 20.",
    )
    timeframe = input_columns[1].selectbox(
        "Timeframe",
        options=["15m", "1h", "4h", "1D"],
        index=3,
        key="home_live_market_timeframe",
    )

    st.caption("Compatibility note: Analyze with Polygon = Analyze Live Market Data.")
    analyze_clicked = st.button("Analyze Live Market Data", type="primary", key="home_analyze_with_polygon")
    if analyze_clicked:
        tickers = parse_watchlist_text(watchlist, limit=20)
        if not tickers:
            st.warning("Enter at least one ticker.")
            st.session_state.home_live_market_rows = []
            st.session_state.home_live_market_csv = ""
            st.session_state.home_live_market_errors = []
        elif provider != "polygon" or config_errors:
            st.warning("Live data is not connected. You can still use Sample data or manual import.")
            st.session_state.home_live_market_rows = []
            st.session_state.home_live_market_csv = ""
            st.session_state.home_live_market_errors = list(config_errors)
        else:
            rows, errors = _market_breakdown_rows_from_provider(tickers, timeframe, config)
            st.session_state.home_live_market_rows = rows
            st.session_state.home_live_market_csv = _breakdown_rows_to_import_csv(rows) if rows else ""
            st.session_state.home_live_market_errors = errors

    errors = st.session_state.get("home_live_market_errors", [])
    if errors:
        with st.expander("Provider messages", expanded=True):
            for error in errors:
                st.write(f"- {error}")

    rows = st.session_state.get("home_live_market_rows", [])
    if rows:
        summary = market_breakdown_summary(rows)
        metric_columns = st.columns(5)
        metric_columns[0].metric("Tickers analyzed", summary["total"])
        metric_columns[1].metric("Bullish", summary["bullish"])
        metric_columns[2].metric("Bearish", summary["bearish"])
        metric_columns[3].metric("Neutral/Mixed", summary["neutral_mixed"])
        metric_columns[4].metric("Needs manual confirmation", summary["needs_manual_confirmation"])

        st.subheader("Live Market Breakdown Cards")
        for row in rows:
            _render_breakdown_card(st, row)

        generated_csv = st.session_state.get("home_live_market_csv", "")
        if generated_csv:
            st.subheader("Copy into Review Engine")
            st.success("Rows are ready. Send them to the Review Engine to unlock Daily Review / Calibration Results.")
            if st.button("Send to Review Engine", type="primary", key="home_send_to_review_engine"):
                _send_rows_to_review_session(st, generated_csv, "Live Market Data")
                st.success("Rows sent to the Review Engine. Opening the session review source.")
                st.rerun()
            with st.expander("Advanced: copy/paste table text", expanded=False):
                st.caption("Optional bridge. TradingView Import = Review Engine Paste Box.")
                st.code(generated_csv, language="csv")

    with st.expander("What should I click?", expanded=True):
        st.write("- Want live cards? Start with Live Market Data.")
        st.write("- Want chart notes? Chart Workspace.")
        st.write("- Want alert ideas? Alert Planner.")
        st.write("- Want daily rows? Send to Review Engine, then open Daily Review.")
        st.write("- Technical name: TradingView Import = Review Engine Paste Box.")
        st.write("- Want calibration? Calibration Results / Review.")


def _show_market_breakdown(st: Any) -> None:
    st.warning("READ ONLY MARKET DATA — VERIFY CHARTS MANUALLY")
    st.header("Market Breakdown")
    st.caption("Live read-only ticker analysis. Decision-support only — no orders.")

    config = _market_breakdown_config_from_streamlit(st)
    provider = configured_market_data_provider(config)
    config_errors = market_data_config_errors(config)

    status_columns = st.columns(2)
    status_columns[0].metric("Provider status", "missing" if provider != "disabled" and config_errors else provider)
    status_columns[1].metric("Mode", "Read-only")
    if provider == "disabled":
        st.info("Polygon is not configured. Use EXAMPLE mode or the Review Engine Paste Box.")
    elif config_errors:
        st.error("Provider is configured but not ready.")
        for error in config_errors:
            st.write(f"- {error}")
    elif provider == "polygon":
        st.success("Polygon read-only data connected.")
    else:
        st.warning("Only polygon is supported for the current read-only breakdown fetch path.")

    st.caption("Beginner flow: type tickers, analyze, read cards, then verify charts manually.")
    watchlist = st.text_area("Enter tickers", value="SPY, QQQ, AAPL", height=90, key="market_breakdown_watchlist")
    timeframe = st.selectbox("Breakdown timeframe", options=["15m", "1h", "4h", "1D"], index=3, key="market_breakdown_timeframe")

    analyze_clicked = st.button("Analyze Watchlist", type="primary")
    example_clicked = st.button("Generate EXAMPLE Breakdown")
    if analyze_clicked:
        tickers = parse_watchlist_text(watchlist, limit=20)
        if not tickers:
            st.warning("Enter at least one ticker.")
        elif provider == "disabled" or config_errors:
            st.warning("Provider is not ready. Use Generate EXAMPLE Breakdown or the manual import workflow.")
        else:
            rows, errors = _market_breakdown_rows_from_provider(tickers, timeframe, config)
            st.session_state.market_breakdown_errors = errors
            st.session_state.market_breakdown_rows = rows
            st.session_state.market_breakdown_import_csv = _breakdown_rows_to_import_csv(rows) if rows else ""
    if example_clicked:
        rows = _example_market_breakdown_rows(timeframe)
        st.session_state.market_breakdown_errors = []
        st.session_state.market_breakdown_rows = rows
        st.session_state.market_breakdown_import_csv = _breakdown_rows_to_import_csv(rows)

    errors = st.session_state.get("market_breakdown_errors", [])
    if errors:
        st.subheader("Provider messages")
        for error in errors:
            st.write(f"- {error}")
        st.caption("If provider data is unavailable, use Generate EXAMPLE Breakdown or manual chart confirmation.")

    rows = st.session_state.get("market_breakdown_rows", [])
    if not rows:
        st.info("Enter a watchlist and click Analyze Watchlist, or use Generate EXAMPLE Breakdown.")
        return

    summary = market_breakdown_summary(rows)
    st.subheader("Market Breakdown Summary")
    metric_columns = st.columns(5)
    metric_columns[0].metric("Tickers analyzed", summary["total"])
    metric_columns[1].metric("Bullish", summary["bullish"])
    metric_columns[2].metric("Bearish", summary["bearish"])
    metric_columns[3].metric("Neutral/Mixed", summary["neutral_mixed"])
    metric_columns[4].metric("Needs manual confirmation", summary["needs_manual_confirmation"])

    st.subheader("Top Breakdown")
    top_rows = [
        {
            "ticker": row.get("ticker"),
            "bias": row.get("bias"),
            "confidence": row.get("confidence"),
            "price": row.get("price"),
            "timeframe": row.get("timeframe"),
            "next_action": row.get("next_action"),
        }
        for row in rows
    ]
    st.table(top_rows)

    st.subheader("Breakdown Cards")
    for row in rows:
        _render_breakdown_card(st, row)

    generated_csv = st.session_state.get("market_breakdown_import_csv", "")
    if generated_csv:
        st.subheader("Send to Review")
        st.caption("Load these rows into Candidate Validation, Daily Review, and Calibration Results without copy/paste.")
        if st.button("Send to Review Engine", type="primary", key="market_breakdown_send_to_review_engine"):
            _send_rows_to_review_session(st, generated_csv, "Market Breakdown")
            st.success("Rows sent to the Review Engine. Opening the session review source.")
            st.rerun()

    with st.expander("Advanced: TradingView Import CSV", expanded=False):
        st.caption("Advanced CSV bridge — optional")
        st.caption("Most users can ignore this. It is for copying rows into the Review Engine Paste Box.")
        st.caption("TradingView Import = Review Engine Paste Box.")
        if generated_csv:
            st.code(generated_csv, language="csv")
        else:
            st.info("No copy/paste table text generated yet.")


def _manual_chart_review_row_from_streamlit(st: Any, default_timeframe: str) -> dict[str, str]:
    row: dict[str, str] = {}
    field_help = {
        "supply_zone": "Manual supply/resistance zone from chart review.",
        "demand_zone": "Manual demand/support zone from chart review.",
        "breakout": "Level that would confirm an upside break. Verify manually.",
        "breakdown": "Level that would confirm a downside break. Verify manually.",
        "invalid": "Level that invalidates the chart read. Verify manually.",
        "fundamentals_note": "Plain-English business/news context. Do not fabricate.",
        "macro_note": "Plain-English market/rate/sector context. Do not fabricate.",
    }
    with st.form("chart_workspace_manual_form", clear_on_submit=False):
        st.caption("Manual fields are session-only. Confirm all chart values yourself.")
        first = st.columns(4)
        row["ticker"] = first[0].text_input("ticker", value="", key="chart_review_ticker")
        row["timeframe"] = first[1].selectbox(
            "timeframe",
            options=["15m", "1h", "4h", "1D"],
            index=["15m", "1h", "4h", "1D"].index(default_timeframe) if default_timeframe in ["15m", "1h", "4h", "1D"] else 0,
            key="chart_review_timeframe",
        )
        row["price"] = first[2].text_input("price", value="", key="chart_review_price")
        row["chart_bias"] = first[3].selectbox(
            "chart_bias",
            options=["unclear", "bullish", "bearish", "neutral", "mixed"],
            index=0,
            key="chart_review_bias",
        )

        levels = st.columns(4)
        for index, field in enumerate(["supply_zone", "demand_zone", "support", "resistance"]):
            row[field] = levels[index].text_input(field, value="", key=f"chart_review_{field}", help=field_help.get(field))

        triggers = st.columns(3)
        for index, field in enumerate(["breakout", "breakdown", "invalid"]):
            row[field] = triggers[index].text_input(field, value="", key=f"chart_review_{field}", help=field_help.get(field))

        averages = st.columns(5)
        for index, field in enumerate(["ema9", "ema21", "wma50", "wma200", "sma200"]):
            row[field] = averages[index].text_input(field, value="", key=f"chart_review_{field}")

        row["macd_hist"] = st.text_input("macd_hist", value="", key="chart_review_macd_hist")
        row["volume_note"] = st.text_input("volume_note", value="", key="chart_review_volume_note")
        row["pattern_note"] = st.text_input("pattern_note", value="", key="chart_review_pattern_note")
        row["fundamentals_note"] = st.text_area(
            "fundamentals_note",
            value="",
            height=70,
            key="chart_review_fundamentals_note",
            help=field_help["fundamentals_note"],
        )
        row["macro_note"] = st.text_area(
            "macro_note",
            value="",
            height=70,
            key="chart_review_macro_note",
            help=field_help["macro_note"],
        )
        row["manual_notes"] = st.text_area("manual_notes", value="", height=80, key="chart_review_manual_notes")
        row["source"] = st.text_input("source", value="manual_chart_review", key="chart_review_source")
        submitted = st.form_submit_button("Add Manual Chart Review Row")
    if not submitted:
        return {}
    return normalize_chart_review_row(row)


def _show_chart_workspace(st: Any) -> None:
    st.warning(CHART_WORKSPACE_WARNING)
    st.header("Chart Workspace")
    st.caption("Manual TradingView chart review capture. Decision-support only. No downloads, no persistence.")
    st.info("1D/4H/1H are context. 15m is usually the execution row.")

    st.subheader("Start Chart Review")
    guide_columns = st.columns(4)
    with guide_columns[0]:
        _render_step_card(st, 1, "1D", "Main bias and broad market context.")
    with guide_columns[1]:
        _render_step_card(st, 2, "4H", "Structure, trend, and larger support/resistance.")
    with guide_columns[2]:
        _render_step_card(st, 3, "1H", "Setup quality and near-term structure.")
    with guide_columns[3]:
        _render_step_card(st, 4, "15m", "Trigger row for review and alert planning.")

    st.subheader("Copy values from TradingView helper")
    for item in [
        "Ticker and timeframe.",
        "Current price.",
        "EMA9, EMA21, WMA50, WMA200, and SMA200 when visible.",
        "Recent support and resistance.",
        "Breakout, breakdown, and invalidation levels.",
        "MACD histogram and volume context.",
        "Manual chart bias and notes.",
    ]:
        st.write(f"- {item}")
    st.caption("Examples below are examples only. Replace them with values you manually verify.")
    st.write("- The bridge sends one execution row per ticker to the Review Engine. Context rows stay here for review.")
    st.write("- Stop if a broker, order, alert, publish, payment, or credential screen appears.")

    st.subheader("Chart Review CSV Template")
    st.caption("Copy this template, fill only verified chart values, then paste it below.")
    st.code(chart_review_template_csv(["SPY", "QQQ"]), language="csv")

    st.subheader("SPY example rows")
    st.caption("Example only. Replace these with values you manually verify on your own chart.")
    st.code(
        "\n".join(
            [
                ",".join(CHART_REVIEW_COLUMNS),
                "SPY,1D,746.90,bullish,,672.04,672.04,758.45,758.45,672.04,672.04,743.26,741.05,738.05,697.41,684.49,-1.47,manual volume check,helper visible,manual context only,manual context only,verify chart manually; no orders,tradingview_readonly_helper",
                "SPY,15m,746.90,neutral,,743.35,743.35,748.50,748.50,743.35,743.35,746.96,747.00,746.89,746.67,747.96,-0.1388,manual volume check,helper visible,manual context only,manual context only,verify chart manually; no orders,tradingview_readonly_helper",
            ]
        ),
        language="csv",
    )

    pasted = st.text_area("Paste Chart Review CSV", height=180, key="chart_review_csv_paste")
    parse_clicked = st.button("Parse Chart Review CSV")
    if parse_clicked:
        rows, errors = parse_chart_review_csv(pasted)
        st.session_state.chart_workspace_errors = errors
        if rows:
            st.session_state.chart_workspace_rows = rows
        if errors:
            st.error("Chart review CSV needs repair before using the bridge.")
            for error in errors:
                st.write(f"- {error}")
        elif rows:
            st.success("Chart review rows parsed into session memory.")

    with st.expander("Manual single-row entry", expanded=False):
        manual_row = _manual_chart_review_row_from_streamlit(st, "15m")
        if manual_row:
            rows = list(st.session_state.get("chart_workspace_rows", []))
            rows.append(manual_row)
            st.session_state.chart_workspace_rows = rows
            st.session_state.chart_workspace_errors = []
            st.success("Manual chart review row added to session memory.")

    st.subheader("Fundamentals / Macro Context Notes")
    st.caption("Use this section for context you manually verify. Do not fabricate news, earnings, macro, or catalyst details.")
    st.write("- Fundamentals note: business, earnings, product, or headline context you have verified.")
    st.write("- Macro note: index, rate, sector, event, or risk context you have verified.")
    st.write("- These notes do not change scoring and do not trigger any automated action.")

    rows = st.session_state.get("chart_workspace_rows", [])
    errors = st.session_state.get("chart_workspace_errors", [])
    if errors:
        st.subheader("Chart Review Repair")
        for error in errors:
            st.write(f"- {error}")
    if not rows:
        st.info("Paste chart review CSV or add a manual row. Nothing is written to disk.")
        return

    normalized_rows = [normalize_chart_review_row(row) for row in rows]
    summary = chart_review_summary(normalized_rows)
    st.subheader("Multi-Timeframe Review Summary")
    metric_columns = st.columns(5)
    metric_columns[0].metric("Rows", summary["total"])
    metric_columns[1].metric("Bullish", summary["bullish"])
    metric_columns[2].metric("Bearish", summary["bearish"])
    metric_columns[3].metric("Neutral/Mixed", summary["neutral_or_mixed"])
    metric_columns[4].metric("Missing levels", summary["missing_levels"])
    st.table(chart_review_timeframe_summary(normalized_rows))

    st.subheader("Chart Review Rows")
    ordered_rows = [{column: row.get(column, "") for column in CHART_REVIEW_COLUMNS} for row in normalized_rows]
    st.table(ordered_rows)

    st.subheader("Review Engine Bridge")
    bridge_csv = chart_review_rows_to_tradingview_import_csv(normalized_rows)
    st.caption("TradingView Import Bridge remains available as the advanced technical path.")
    st.caption(
        "Send this into the Review Engine if you want validation, Daily Review, and Accuracy Review. "
        "The bridge keeps one execution row per ticker, preferring 15m. Higher-timeframe rows remain Chart Workspace context."
    )
    if st.button("Send to Review Engine", type="primary", key="chart_workspace_send_to_review_engine"):
        _send_rows_to_review_session(st, bridge_csv, "Chart Workspace")
        st.success("Chart Workspace rows sent to the Review Engine.")
        st.rerun()
    with st.expander("Advanced: Review Engine Paste Box text", expanded=False):
        st.caption("Technical name: TradingView Import CSV.")
        st.code(bridge_csv, language="csv")
    st.warning("Manual chart confirmation is still required. Do not place orders from this app.")


def _alert_plan_rows_from_chart_workspace(st: Any) -> list[dict[str, str]]:
    rows = st.session_state.get("chart_workspace_rows", [])
    if not isinstance(rows, list):
        return []
    return build_alert_plan_from_chart_review([row for row in rows if isinstance(row, dict)])


def _alert_plan_rows_from_market_breakdown(st: Any) -> list[dict[str, str]]:
    rows = st.session_state.get("market_breakdown_rows", []) or st.session_state.get("home_live_market_rows", [])
    if not isinstance(rows, list):
        return []
    return build_alert_plan_from_market_breakdown([row for row in rows if isinstance(row, dict)])


def _render_alert_plan_card(st: Any, row: dict[str, Any]) -> None:
    normalized = normalize_alert_plan_row(row)
    decision = setup_decision_support(normalized)
    card = build_decision_support_card(normalized)
    ticker = html.escape(normalized.get("ticker", "UNKNOWN"))
    meta = html.escape(
        f"{normalized.get('timeframe', '15m')} · {normalized.get('setup_bias', 'unclear')} · "
        f"{normalized.get('setup_type', 'watch_only')} · {normalized.get('status', 'draft')}"
    )
    st.markdown(
        f"""
        <div class="breakdown-card">
          <div class="breakdown-card-title">{ticker}</div>
          <div class="breakdown-card-meta">{meta}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    columns = st.columns(3)
    columns[0].caption("Trigger")
    columns[0].info(decision["trigger_summary"])
    columns[1].caption("Invalidation")
    columns[1].info(decision["invalidation_summary"])
    columns[2].caption("Targets")
    columns[2].info(decision["target_summary"])

    st.table(
        [
            {
                "ticker/timeframe": f"{card['ticker']} {card['timeframe']}",
                "setup": card["setup"],
                "confidence": card["confidence"],
                "status": card["status"],
                "next_action": card["next_action"],
            }
        ]
    )

    st.caption("Fundamentals / macro context")
    st.write(card["context"])
    warnings = decision["safety_warnings"]
    if warnings:
        st.warning("Safety warnings: " + " | ".join(str(item) for item in warnings))
    st.write("Next action")
    st.info(decision["next_action"])
    st.write("TradingView alert message draft")
    st.caption("Final manual confirmation required. This is not a trade. This is not an order. This is not a live alert. This is a draft reminder plan.")
    message = alert_plan_to_tradingview_message(normalized)
    st.code(message)
    st.caption(card["final_reminder"])


def _show_alert_planner(st: Any) -> None:
    st.warning(ALERT_PLANNER_WARNING)
    st.header("Alert Planner")
    st.caption("Draft manual TradingView alert ideas. Nothing is created automatically.")
    _render_safety_strip(st)

    st.subheader("How this works")
    how_columns = st.columns(5)
    how_steps = [
        ("Build from Chart Workspace or paste a template.", "Use only values you verified."),
        ("Confirm chart levels manually.", "Support, resistance, trigger, invalidation."),
        ("Review trigger / invalidation / targets.", "Check the draft plan before copying."),
        ("Copy the draft message only if you choose.", "No browser automation is used here."),
        ("You manually decide later.", "No alert is created by this app."),
    ]
    for index, (title, body) in enumerate(how_steps, start=1):
        with how_columns[index - 1]:
            _render_step_card(st, index, title, body)

    st.info("This is not a trade. This is not an order. This is not a live alert. This is a draft reminder plan.")
    st.write("- You must manually verify the chart and give final confirmation before creating any alert.")
    st.write("- Stop if a broker, order, alert, publish, payment, or credential screen appears.")

    st.subheader("Build from Chart Workspace")
    chart_rows = _alert_plan_rows_from_chart_workspace(st)
    market_rows = _alert_plan_rows_from_market_breakdown(st)
    builder_columns = st.columns(2)
    with builder_columns[0]:
        if st.button("Build Alert Plans from Chart Workspace"):
            if chart_rows:
                st.session_state.alert_plan_rows = chart_rows
                st.success("Draft alert plans built from session Chart Workspace rows.")
            else:
                st.info("No Chart Workspace rows found in this session.")
    with builder_columns[1]:
        if st.button("Build Alert Plans from Market Breakdown"):
            if market_rows:
                st.session_state.alert_plan_rows = market_rows
                st.success("Draft alert plans built from session Market Breakdown rows.")
            else:
                st.info("No Market Breakdown rows found in this session.")

    st.subheader("Quick Draft Alert Plan")
    st.caption("Manual draft only. This is not a live alert. This is not a trade. You manually decide later.")
    with st.form("quick_draft_alert_plan", clear_on_submit=False):
        ticker = st.text_input("ticker", value="", key="quick_alert_ticker")
        timeframe = st.selectbox("timeframe", options=["15m", "1h", "4h", "1D"], index=0, key="quick_alert_timeframe")
        setup_bias = st.selectbox(
            "setup_bias",
            options=["unclear", "bullish", "bearish", "neutral", "mixed"],
            index=0,
            key="quick_alert_setup_bias",
        )
        setup_type = st.selectbox(
            "setup_type",
            options=["watch_only", "breakout", "breakdown", "pullback", "reversal", "continuation", "unclear"],
            index=0,
            key="quick_alert_setup_type",
        )
        trigger_level = st.text_input("trigger_level", value="", key="quick_alert_trigger_level")
        invalidation_level = st.text_input("invalidation_level", value="", key="quick_alert_invalidation_level")
        target_1 = st.text_input("target_1", value="", key="quick_alert_target_1")
        target_2 = st.text_input("target_2", value="", key="quick_alert_target_2")
        chart_confirmation = st.selectbox(
            "chart_confirmation",
            options=["needs_manual_confirmation", "yes", "confirmed"],
            index=0,
            key="quick_alert_chart_confirmation",
        )
        fundamentals_context = st.text_area("fundamentals_context", value="", height=70, key="quick_alert_fundamentals_context")
        macro_context = st.text_area("macro_context", value="", height=70, key="quick_alert_macro_context")
        manual_notes = st.text_area("manual_notes", value="", height=80, key="quick_alert_manual_notes")
        quick_submitted = st.form_submit_button("Create Draft Alert Plan")
    if quick_submitted:
        if not str(ticker or "").strip():
            st.warning("Enter a ticker before creating a draft alert plan.")
        else:
            quick_row = normalize_alert_plan_row(
                {
                    "ticker": ticker,
                    "timeframe": timeframe,
                    "setup_bias": setup_bias,
                    "setup_type": setup_type,
                    "trigger_level": trigger_level,
                    "invalidation_level": invalidation_level,
                    "target_1": target_1,
                    "target_2": target_2,
                    "chart_confirmation": chart_confirmation,
                    "fundamentals_context": fundamentals_context,
                    "macro_context": macro_context,
                    "manual_notes": manual_notes or "quick draft alert plan; verify chart manually; no orders",
                }
            )
            rows = [row for row in st.session_state.get("alert_plan_rows", []) if isinstance(row, dict)]
            rows.append(quick_row)
            st.session_state.alert_plan_rows = rows
            st.session_state.alert_plan_errors = []
            st.success("Quick draft alert plan created in session memory. No live alert was created.")

    with st.expander("Advanced: paste alert-plan table text", expanded=False):
        st.caption("Template only. Fill values you manually verify. Nothing is written to disk.")
        st.code(alert_plan_template_csv(["SPY"]), language="csv")
        pasted = st.text_area("Manual Alert Plan CSV", height=180, key="manual_alert_plan_csv")
        if st.button("Parse Alert Plan CSV"):
            rows, errors = parse_alert_plan_csv(pasted)
            st.session_state.alert_plan_errors = errors
            if rows:
                st.session_state.alert_plan_rows = rows
            if errors:
                st.error("Alert plan table text needs repair before use.")
                for error in errors:
                    st.write(f"- {error}")
            elif rows:
                st.success("Alert plan rows parsed into session memory.")

    rows = st.session_state.get("alert_plan_rows", [])
    errors = st.session_state.get("alert_plan_errors", [])
    if errors:
        st.subheader("Alert Plan Repair")
        for error in errors:
            st.write(f"- {error}")
    if not rows:
        st.info("Build from Chart Workspace, build from Market Breakdown, create a Quick Draft Alert Plan, or use the advanced table-text parser.")
    else:
        normalized_rows = [normalize_alert_plan_row(row) for row in rows if isinstance(row, dict)]
        summary = alert_plan_status_summary(normalized_rows)
        st.subheader("Alert Plan Summary")
        metric_columns = st.columns(4)
        for index, (label, value) in enumerate(
            [
                ("Total", summary.get("total", 0)),
                ("Needs chart confirmation", summary.get("needs_chart_confirmation", 0)),
                ("Ready for manual alert", summary.get("ready_for_manual_alert", 0)),
                ("Draft", summary.get("draft", 0)),
            ]
        ):
            metric_columns[index % 4].metric(label, value)

        st.subheader("Alert Plan Cards")
        for row in normalized_rows:
            _render_alert_plan_card(st, row)

    st.subheader("Decision Support Checklist")
    for item in [
        "Chart confirmation complete.",
        "Support/resistance level marked.",
        "Invalidation clear.",
        "Risk/reward acceptable.",
        "Macro/fundamental context not conflicting.",
        "No earnings/catalyst surprise unchecked.",
        "Manual final confirmation required.",
    ]:
        st.write(f"- {item}")

    st.subheader("Stop Conditions")
    for item in [
        "Alert trigger is stale.",
        "Support/resistance invalidated.",
        "Macro/fundamental context changed.",
        "Chart disagrees across timeframes.",
        "User is unsure/rushed.",
        "Broker/order/payment/alert screen appears unexpectedly.",
    ]:
        st.write(f"- {item}")


def _show_live_data_readonly(st: Any) -> None:
    st.warning(LIVE_DATA_READONLY_WARNING)
    st.write("This tab can prepare import rows from a market-data provider when configured.")
    st.write("For a more user-friendly explanation, use Market Breakdown.")
    st.write("For manual chart-review notes and multi-timeframe context, use Chart Workspace.")
    st.write("It does not place orders.")
    st.write("It does not connect brokers.")
    st.write("It does not create alerts.")
    st.write("It does not scrape TradingView.")

    config = _market_data_config_from_streamlit(st)
    provider = configured_market_data_provider(config)
    config_errors = market_data_config_errors(config)
    st.subheader("Provider status")
    st.metric("MARKET_DATA_PROVIDER", provider)
    st.warning("Read-only provider data may be delayed depending on your market-data plan.")
    st.info("Provider diagnostic tip: try 1D first to verify key/config. If 1D works but 15m fails, your market-data plan may not include intraday bars.")
    st.write("- 401 usually means missing, invalid, or unauthorized key.")
    st.write("- 403 usually means the key is valid but lacks plan/entitlement access.")
    st.write("- 429 means the provider rate limit was hit.")
    st.write("- 400 means the provider rejected the request.")
    st.caption("Basic/free plans may be delayed or end-of-day depending on provider plan and entitlements.")
    st.caption("This tab does not auto-refresh, download provider data, or persist generated rows.")

    if provider == "disabled":
        st.info("Manual import mode")
        st.write("Manual import works. Add provider secrets only if you want read-only live data.")
        st.write("- Supported providers: alpaca, polygon.")
        st.write("- Do not commit secrets.")
    elif config_errors:
        st.error("Read-only market data provider is not ready.")
        for error in config_errors:
            st.write(f"- {error}")
    else:
        st.success("Read-only provider configuration detected.")
        if provider == "polygon":
            st.success("Connected to Polygon")
        if provider != "polygon":
            st.caption("Only polygon read-only fetch is implemented for the current provider path.")

    st.subheader("Generate read-only rows")
    st.write("Copy into Review Engine, confirm Blocking issues = 0, then verify chart manually.")
    st.caption("TradingView Import = Review Engine Paste Box.")
    st.caption("No orders are created. Data is not persisted, downloaded, or auto-refreshed.")

    tickers_text = st.text_area("Tickers to fetch", height=100, key="readonly_market_data_tickers")
    timeframe = st.selectbox("Timeframe", options=["15m", "1h", "4h", "1D"], index=0, key="readonly_market_data_timeframe")

    if st.button("Prepare Read-Only Import Rows"):
        tickers = _parse_market_data_tickers(tickers_text)
        if not tickers:
            st.warning("Enter one or more tickers first.")
        elif provider == "disabled" or config_errors:
            st.warning("Provider is not configured yet. Use EXAMPLE mode for the safe smoke test.")
        else:
            rows, errors = fetch_readonly_market_data_rows(tickers, timeframe, config)
            if errors:
                st.subheader("Provider messages")
                for error in errors:
                    st.write(f"- {error}")
            if rows:
                st.session_state.readonly_market_data_csv = market_data_rows_to_tradingview_import_csv(rows)
            else:
                st.warning("No read-only provider rows were generated.")

    if st.button("Generate EXAMPLE Read-Only Import Row"):
        st.session_state.readonly_market_data_csv = _example_readonly_market_data_csv(timeframe)

    generated_csv = st.session_state.get("readonly_market_data_csv", "")
    if generated_csv:
        st.subheader("Generated Review Engine Rows")
        st.success("Provider rows generated successfully.")
        st.success("Provider CSV generated successfully. Rows are ready for the Review Engine.")
        if st.button("Send to Review Engine", type="primary", key="readonly_send_to_review_engine"):
            _send_rows_to_review_session(st, generated_csv, "Live Data — Read Only")
            st.success("Rows sent to the Review Engine.")
            st.rerun()
        st.write("- Next: Send to Review Engine or choose Review Engine Paste Box in the sidebar.")
        st.write("- Technical name: TradingView Import = Review Engine Paste Box.")
        st.write("- Then confirm Blocking issues = 0.")
        st.write("- Then use Daily Review / Calibration Results.")
        st.write("- Verify chart manually before any decision.")
        st.write("- No orders are created.")
        with st.expander("Advanced: copy/paste table text", expanded=False):
            st.code(generated_csv, language="csv")
        st.caption("Copy this into the Review Engine Paste Box only if you prefer manual copy/paste. Verify charts manually.")


def _show_help_safety(st: Any) -> None:
    st.warning("HELP / SAFETY — DECISION SUPPORT ONLY")
    st.header("Help / Safety")
    st.caption("A non-coder guide for using Trading Autopilot without touching broker/order workflows.")

    st.subheader("What this app does")
    for item in [
        "Helps review tickers.",
        "Fetches read-only market data when Polygon is configured.",
        "Creates import rows for the advanced workflow.",
        "Explains dashboard reads in plain English.",
        "Drafts manual alert plans for chart-confirmed setups.",
        "Helps calibration and review notes stay organized.",
    ]:
        st.write(f"- {item}")

    st.subheader("What this app does not do")
    for item in [
        "No orders.",
        "No broker connection.",
        "No auto-trading.",
        "No TradingView alerts.",
        "No financial advice.",
    ]:
        st.write(f"- {item}")

    st.subheader("How to use")
    for step in [
        "1. Start on Home.",
        "2. Use Live Data or manual import.",
        "3. Confirm Blocking issues = 0.",
        "4. Read Market Breakdown.",
        "5. Capture chart notes in Chart Workspace.",
        "6. Draft manual alert ideas in Alert Planner if useful.",
        "7. Verify chart manually.",
        "8. Use Calibration Review if tracking accuracy.",
    ]:
        st.write(step)

    st.subheader("Beginner translation")
    st.caption("TradingView Import = Review Engine Paste Box.")
    st.table(_beginner_term_help())

    st.subheader("Stop if")
    for item in [
        "Blocking issues appear.",
        "Data looks wrong.",
        "Provider fails.",
        "You feel rushed.",
        "Anything asks for broker/order/payment.",
    ]:
        st.write(f"- {item}")


def _show_calibration_guide(st: Any) -> None:
    st.warning(CALIBRATION_WARNING)
    st.subheader("First batch tickers")
    st.write(CALIBRATION_BATCH_TEXT)
    st.dataframe(
        [{"ticker": ticker, "status": "pending_manual_review"} for ticker in CALIBRATION_BATCH],
        width="stretch",
        hide_index=True,
    )
    st.subheader("Calibration steps")
    for step in [
        "1. Manually check chart.",
        "2. Compare dashboard score/grade/state.",
        "3. Mark match status.",
        "4. Record issue type if wrong.",
        "5. Do not place orders from the app.",
    ]:
        st.write(step)
    st.info("Use docs/v030_watchlist_calibration.md and data/calibration_template.csv.")


def _calibration_results_signature(rows: list[dict[str, Any]]) -> str:
    parts = [
        "|".join(
            [
                str(row.get("ticker", "")),
                str(row.get("dashboard_score", "")),
                str(row.get("dashboard_grade", "")),
                str(row.get("dashboard_state", "")),
                str(row.get("dashboard_bucket", "")),
            ]
        )
        for row in rows
    ]
    return "\n".join(parts)


def _show_calibration_results(st: Any, rows: list[dict[str, Any]], sections: dict[str, list[dict[str, Any]]]) -> None:
    st.warning(CALIBRATION_RESULTS_WARNING)
    base_rows = build_calibration_result_rows(rows, sections)
    if not base_rows:
        st.info("Import or enter candidates first, then return here to capture calibration results.")
        return

    signature = _calibration_results_signature(base_rows)
    if st.session_state.get("calibration_results_signature") != signature or "calibration_results_rows" not in st.session_state:
        st.session_state.calibration_results_rows = base_rows
        st.session_state.calibration_results_signature = signature

    column_config = {}
    if hasattr(st, "column_config"):
        column_config = {
            "match_status": st.column_config.SelectboxColumn("match_status", options=CALIBRATION_MATCH_STATUSES),
            "issue_type": st.column_config.SelectboxColumn("issue_type", options=CALIBRATION_ISSUE_TYPES),
        }

    edited_rows = st.data_editor(
        st.session_state.calibration_results_rows,
        column_order=CALIBRATION_RESULT_COLUMNS,
        width="stretch",
        hide_index=True,
        num_rows="fixed",
        column_config=column_config,
        height=360,
    )
    st.session_state.calibration_results_rows = _rows_from_editor_output(edited_rows)
    st.download_button(
        "Download Calibration CSV",
        data=_calibration_csv_text(st.session_state.calibration_results_rows),
        file_name="trading_autopilot_calibration_results.csv",
        mime="text/csv",
        help="Browser-only export. The app does not write calibration results to disk.",
    )
    st.caption("Session-only. Nothing is written to disk.")

    st.subheader("Apply Calibration Labels")
    st.caption("Paste labels CSV to update manual_chart_bias, match_status, issue_type, and follow_up without grid cell editing.")
    st.code(CALIBRATION_LABEL_EXAMPLE, language="csv")
    label_csv_text = st.text_area("Calibration Labels CSV", height=160, key="calibration_labels_csv")
    st.caption("Session-only. Nothing is written to disk.")

    apply_labels = st.button("Apply Labels to Current Calibration Results")
    apply_labels_and_add = st.button("Apply Labels and Add to Batch Log")
    if apply_labels or apply_labels_and_add:
        labels = parse_calibration_label_csv(label_csv_text)
        updated_rows, label_errors = apply_calibration_labels_to_rows(
            st.session_state.calibration_results_rows,
            labels,
        )
        if label_errors:
            st.subheader("Calibration Label Repair")
            st.error("Calibration labels were not applied. Repair the CSV and try again.")
            for error in label_errors:
                st.write(f"- {error}")
        else:
            st.session_state.calibration_results_rows = updated_rows
            if apply_labels_and_add:
                st.session_state.calibration_batch_log_rows = merge_calibration_batch_log_rows(
                    _calibration_batch_log_rows(st),
                    _current_session_calibration_rows(st),
                )
                st.success("Applied labels and added current calibration results to session batch log.")
            else:
                st.success("Applied calibration labels to current results.")

    if st.button("Add Current Calibration Results to Batch Log"):
        st.session_state.calibration_batch_log_rows = merge_calibration_batch_log_rows(
            _calibration_batch_log_rows(st),
            _current_session_calibration_rows(st),
        )
        st.success("Added current calibration results to session batch log.")


def _show_calibration_batch_log(st: Any) -> None:
    st.warning(CALIBRATION_BATCH_LOG_WARNING)
    rows = _calibration_batch_log_rows(st)
    if not rows:
        st.info("Add rows from Calibration Results to start a session batch log.")
        return

    column_config = {}
    if hasattr(st, "column_config"):
        column_config = {
            "match_status": st.column_config.SelectboxColumn("match_status", options=CALIBRATION_MATCH_STATUSES),
            "issue_type": st.column_config.SelectboxColumn("issue_type", options=CALIBRATION_ISSUE_TYPES),
        }

    edited_rows = st.data_editor(
        rows,
        column_order=CALIBRATION_RESULT_COLUMNS,
        width="stretch",
        hide_index=True,
        num_rows="dynamic",
        column_config=column_config,
        height=360,
    )
    rows = merge_calibration_batch_log_rows([], _rows_from_editor_output(edited_rows))
    st.session_state.calibration_batch_log_rows = rows

    _show_calibration_summary_sections(st, rows)
    _show_scoring_review_notes(st, rows)

    st.subheader("Rows needing manual confirmation")
    review_rows = calibration_rows_needing_review(rows)
    if review_rows:
        st.dataframe(review_rows, width="stretch", hide_index=True)
    else:
        st.success("No unclear or needs_manual_chart_confirmation rows found.")

    st.download_button(
        "Download Batch Log CSV",
        data=_calibration_csv_text(rows),
        file_name="trading_autopilot_calibration_batch_log.csv",
        mime="text/csv",
        help="Browser-only export. The app does not write calibration batch logs to disk.",
    )
    if st.button("Clear Calibration Batch Log"):
        st.session_state.calibration_batch_log_rows = []
        st.success("Cleared session calibration batch log.")


def _calibration_score_value(row: dict[str, Any]) -> float:
    try:
        return float(str(row.get("dashboard_score", "")).strip() or 0)
    except ValueError:
        return 0.0


def _show_calibration_summary_sections(st: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    summary = summarize_calibration_results(rows)

    st.subheader("Match Status Summary")
    metrics = [
        ("Total rows", summary["total_rows"]),
        ("Match Status: match", summary["match_count"]),
        ("Match Status: unclear", summary["unclear_count"]),
        ("Match Status: false_positive", summary["false_positive_count"]),
        ("Match Status: false_negative", summary["false_negative_count"]),
        ("Match Status: bad_bucket", summary["bad_bucket_count"]),
        ("Match Status: bad_scoring", summary["bad_scoring_count"]),
        ("Match Status: bad_validation", summary["bad_validation_count"]),
    ]
    metric_columns = st.columns(4)
    for index, (label, value) in enumerate(metrics):
        metric_columns[index % 4].metric(label, value)

    st.subheader("Issue Type Summary")
    issue_rows = [
        {"issue_type": issue_type, "count": count}
        for issue_type, count in summary["issue_type_counts"].items()
    ]
    st.dataframe(issue_rows, width="stretch", hide_index=True)
    return summary


def _show_scoring_review_notes(st: Any, rows: list[dict[str, Any]]) -> None:
    st.subheader("Scoring Review Notes")
    for note in scoring_review_notes_from_calibration_rows(rows):
        st.write(f"- {note}")


def _show_scoring_adjustment_proposal(st: Any, rows: list[dict[str, Any]]) -> None:
    st.subheader("Scoring Adjustment Proposal")
    st.warning("SCORING PROPOSAL ONLY — NO SCORING LOGIC CHANGED")

    evidence_level = calibration_evidence_level(rows)
    problem_count = len(calibration_problem_rows(rows))
    metric_columns = st.columns(2)
    metric_columns[0].metric("Evidence level", evidence_level)
    metric_columns[1].metric("Problem row count", problem_count)

    for note in scoring_adjustment_proposal_from_calibration_rows(rows):
        st.write(f"- {note}")


def _show_calibration_review(st: Any) -> None:
    st.warning(CALIBRATION_REVIEW_WARNING)
    batch_rows = _calibration_batch_log_rows(st)
    use_batch_log_rows = False
    if batch_rows:
        st.subheader("Calibration Batch Log")
        st.caption("Reviews accumulated session rows from Calibration Batch Log. Nothing is written to disk.")
        use_batch_log_rows = st.button("Use Calibration Batch Log", type="primary")

    session_rows = _current_session_calibration_rows(st)
    use_session_rows = False
    if session_rows:
        st.subheader("Current session results")
        st.caption("Uses the editable rows currently in Calibration Results. Nothing is written to disk.")
        use_session_rows = st.button("Use Current Session Calibration Results")

    uploaded = st.file_uploader("Upload Calibration CSV", type=["csv"], key="calibration_review_upload")
    pasted = st.text_area("Paste Calibration CSV", height=180, key="calibration_review_paste")

    rows: list[dict[str, Any]] = []
    if use_batch_log_rows:
        rows = batch_rows
    elif use_session_rows:
        rows = session_rows
    elif uploaded is not None:
        rows = parse_calibration_review_csv(uploaded.getvalue().decode("utf-8-sig"))
    elif pasted.strip():
        rows = parse_calibration_review_csv(pasted)
    else:
        st.info("Use current session results, use Calibration Batch Log, upload a Calibration CSV, or paste Calibration CSV text to review results.")
        _show_scoring_review_notes(st, [])
        _show_scoring_adjustment_proposal(st, [])
        return

    if not rows:
        st.warning("No calibration rows found in the selected calibration source.")
        _show_scoring_review_notes(st, [])
        _show_scoring_adjustment_proposal(st, [])
        return

    _show_calibration_summary_sections(st, rows)

    st.subheader("Top scoring rows")
    top_rows = sorted(rows, key=_calibration_score_value, reverse=True)[:10]
    st.dataframe(top_rows, width="stretch", hide_index=True)

    st.subheader("Rows needing manual confirmation")
    review_rows = calibration_rows_needing_review(rows)
    if review_rows:
        st.dataframe(review_rows, width="stretch", hide_index=True)
    else:
        st.success("No unclear or needs_manual_chart_confirmation rows found.")

    _show_scoring_review_notes(st, rows)
    _show_scoring_adjustment_proposal(st, rows)


def streamlit_dashboard(path: Path = DEFAULT_DATA) -> None:
    import streamlit as st

    st.set_page_config(page_title="Trading Autopilot", layout="wide")
    _require_streamlit_access(st)
    _inject_product_styles(st)

    source_label, selected_path, rows, user_supplied = _streamlit_candidate_source(st, path)
    sections = build_dashboard_sections(rows)
    summary = build_top_review_summary(rows, sections)

    st.title("Trading Autopilot")
    st.caption(f"Version: {APP_VERSION}")
    if selected_path is not None:
        st.caption(f"Candidate file: {selected_path}")
    else:
        st.caption(f"Candidate source: {source_label}")
    for warning in _streamlit_warning_lines(selected_path, user_supplied):
        st.warning(warning)
    if source_label in {"TradingView Import", _review_engine_label(), "Review Engine Session"}:
        st.warning(TRADINGVIEW_IMPORT_WARNING)
    st.caption("Decision support only. No broker connection or order execution.")
    view_mode = st.radio(
        "View mode",
        options=["Beginner", "Advanced"],
        index=0,
        horizontal=True,
        help="Beginner mode shows the few tabs most people need. Advanced mode shows all technical review tabs. Switching modes does not change data or scoring.",
    )
    if view_mode == "Beginner":
        st.caption("Beginner mode shows the few tabs most people need.")
    else:
        st.caption("Advanced mode shows all technical review tabs.")
    _show_phone_workflow(st, source_label, user_supplied)
    _show_tradingview_import_repair(st, source_label)

    if source_label == "Manual Entry":
        if rows:
            edited_rows = st.data_editor(
                rows,
                column_order=MANUAL_REVIEW_COLUMNS,
                width="stretch",
                hide_index=True,
                num_rows="dynamic",
                height=320,
            )
            st.session_state.manual_candidates = _rows_from_editor_output(edited_rows)
            rows = list(st.session_state.manual_candidates)
            sections = build_dashboard_sections(rows)
            summary = build_top_review_summary(rows, sections)
        else:
            _show_manual_entry_empty_state(st)

    _show_validation_feedback(st, rows, user_supplied)
    _show_download(st, rows, user_supplied)
    _show_top_review_summary(st, summary)

    tab_names = dashboard_tab_names_for_mode(view_mode)
    tabs = st.tabs(tab_names)
    for tab, name in zip(tabs, tab_names):
        with tab:
            if name == "Home":
                _show_product_home(st, rows, sections, source_label)
            elif name == "Market Breakdown":
                _show_market_breakdown(st)
            elif name == "Chart Workspace":
                _show_chart_workspace(st)
            elif name == "Alert Planner":
                _show_alert_planner(st)
            elif name == "Daily Review":
                _show_daily_review(st, rows, sections, source_label)
            elif name == "Live Data — Read Only":
                _show_live_data_readonly(st)
            elif name == "Calibration Guide":
                _show_calibration_guide(st)
            elif name == "Calibration Results":
                _show_calibration_results(st, rows, sections)
            elif name == "Calibration Batch Log":
                _show_calibration_batch_log(st)
            elif name == "Calibration Review":
                _show_calibration_review(st)
            elif name == "Help / Safety":
                _show_help_safety(st)
            else:
                st.dataframe(sections[name], width="stretch")


def main() -> None:
    path = _cli_path_arg()
    try:
        import streamlit  # noqa: F401
    except ImportError:
        cli_dashboard(path)
    else:
        try:
            from streamlit.runtime.scriptrunner import get_script_run_ctx
        except Exception:
            get_script_run_ctx = None
        if get_script_run_ctx and get_script_run_ctx() is not None:
            streamlit_dashboard(path)
        else:
            cli_dashboard(path)


if __name__ == "__main__":
    main()
