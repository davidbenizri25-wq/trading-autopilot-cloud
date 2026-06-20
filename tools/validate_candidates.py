"""Validate candidate CSV files for the v0.2 intake workflow.

Decision support only. This script validates local CSV shape and never connects
to brokers, TradingView, alerts, credentials, or order workflows.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any


REQUIRED_COLUMNS = [
    "ticker",
    "watchlist",
    "asset_type",
    "category",
    "timeframe",
    "close",
    "ema9",
    "ema21",
    "wma50",
    "wma200",
    "sma200",
    "macd_hist",
    "macd_hist_prev",
    "support1",
    "support2",
    "resistance1",
    "resistance2",
    "breakout",
    "breakdown",
    "invalid",
    "avg_volume",
    "relative_volume",
]

OPTIONAL_OPTIONS_COLUMNS = [
    "option_volume",
    "option_open_interest",
    "option_spread_pct",
    "option_bid",
    "option_mid",
    "option_ask",
    "days_to_expiry",
    "days_to_earnings",
    "iv_rank",
    "delta",
    "open_options_exposure",
]

OPTIONAL_SHARE_COLUMNS = [
    "shares_owned",
    "cost_basis",
    "strike",
    "manual_override",
]

NUMERIC_COLUMNS = {
    "close",
    "ema9",
    "ema21",
    "wma50",
    "wma200",
    "sma200",
    "macd_hist",
    "macd_hist_prev",
    "support1",
    "support2",
    "resistance1",
    "resistance2",
    "breakout",
    "breakdown",
    "invalid",
    "bullish_invalid",
    "bearish_invalid",
    "bullish_trigger",
    "bearish_trigger",
    "avg_volume",
    "relative_volume",
    "option_volume",
    "option_open_interest",
    "option_spread_pct",
    "option_bid",
    "option_mid",
    "option_ask",
    "days_to_expiry",
    "days_to_earnings",
    "iv_rank",
    "delta",
    "open_options_exposure",
    "shares_owned",
    "cost_basis",
    "strike",
}


def _parse_number(value: Any) -> bool:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return True
    try:
        float(text)
    except ValueError:
        return False
    return True


ALL_CANDIDATE_COLUMNS = REQUIRED_COLUMNS + [
    column
    for column in OPTIONAL_OPTIONS_COLUMNS + OPTIONAL_SHARE_COLUMNS + [
        "bullish_invalid",
        "bearish_invalid",
        "bullish_trigger",
        "bearish_trigger",
    ]
    if column not in REQUIRED_COLUMNS
]


def _validate_rows(columns: list[str], rows: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    missing = [column for column in REQUIRED_COLUMNS if column not in columns]
    if missing:
        errors.append(f"missing required columns: {', '.join(missing)}")
        return errors, warnings

    if not rows:
        warnings.append("candidate file has no rows yet")

    optional_columns = OPTIONAL_OPTIONS_COLUMNS + OPTIONAL_SHARE_COLUMNS
    for optional in optional_columns:
        if optional not in columns:
            warnings.append(f"optional column missing: {optional}")

    for row_number, row in enumerate(rows, start=2):
        ticker = str(row.get("ticker", "")).strip()
        asset_type = str(row.get("asset_type", "")).strip()
        if not ticker:
            errors.append(f"row {row_number}: ticker is blank")
        if not asset_type:
            errors.append(f"row {row_number}: asset_type is blank")
        for column in NUMERIC_COLUMNS.intersection(row):
            if not _parse_number(row.get(column)):
                errors.append(f"row {row_number}: {column} is not numeric")
        for optional in OPTIONAL_OPTIONS_COLUMNS:
            if optional in row and str(row.get(optional, "")).strip() == "":
                warnings.append(f"row {row_number}: optional options field blank: {optional}")

    return errors, warnings


def validate_candidate_rows(rows: list[dict[str, Any]], columns: list[str] | None = None) -> tuple[list[str], list[str]]:
    if columns is None:
        seen: list[str] = []
        for row in rows:
            for column in row:
                if column not in seen:
                    seen.append(column)
        columns = seen or list(ALL_CANDIDATE_COLUMNS)
    normalized = [{column: row.get(column, "") for column in columns} for row in rows]
    return _validate_rows(list(columns), normalized)


def validate_candidate_csv_text(text: str) -> tuple[list[str], list[str]]:
    try:
        reader = csv.DictReader(text.splitlines())
        columns = reader.fieldnames or []
        rows = list(reader)
    except csv.Error as exc:
        return [f"CSV could not be parsed: {exc}"], []
    return _validate_rows(columns, rows)

def validate_candidate_file(path: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        return [f"{path} does not exist"], warnings

    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        columns = reader.fieldnames or []
        rows = list(reader)

    return _validate_rows(columns, rows)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print("Usage: python3 tools/validate_candidates.py path/to/candidates.csv")
        return 2

    path = Path(args[0]).expanduser()
    errors, warnings = validate_candidate_file(path)
    for warning in warnings:
        print(f"Warning: {warning}")
    if errors:
        print("Candidate validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Candidate validation passed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
