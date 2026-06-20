"""Covered-call review filter.

For existing share positions only. This module does not suggest buying shares
and does not choose or place option orders.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from scoring import _bool, _float, load_candidates_csv, rank_candidates


def parse_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def filter_covered_call_candidates(
    rows: list[dict[str, Any]],
    market_bias: str = "mixed",
    min_score: int = 70,
    min_shares: int = 100,
    min_bid: float = 0.05,
    max_spread_pct: float = 0.18,
    min_open_interest: float = 500,
    min_dte: int = 14,
    max_dte: int = 60,
) -> list[dict[str, Any]]:
    ranked = rank_candidates(rows, market_bias=market_bias)
    results: list[dict[str, Any]] = []
    for row in ranked:
        if str(row.get("asset_type", "")).lower() != "equity":
            continue

        notes: list[str] = []
        eligible = True
        manual_override = _bool(row.get("manual_override"))
        shares_owned = _float(row.get("shares_owned"))
        strike = _float(row.get("strike"))
        close = _float(row.get("close"))
        cost_basis = _float(row.get("cost_basis"))
        score = int(row["score"])
        bias = str(row.get("bias"))

        if score < min_score and not manual_override:
            eligible = False
            notes.append("underlying score below covered-call threshold")
        if shares_owned < min_shares:
            eligible = False
            notes.append("fewer than 100 shares owned")
        if _float(row.get("option_bid")) < min_bid:
            eligible = False
            notes.append("option bid below minimum")
        if _float(row.get("option_spread_pct")) > max_spread_pct:
            eligible = False
            notes.append("option spread too wide")
        if _float(row.get("option_open_interest")) < min_open_interest:
            eligible = False
            notes.append("open interest too low")

        dte = parse_optional_float(row.get("days_to_expiry"))
        if dte is None:
            eligible = False
            notes.append("days to expiry missing")
        elif dte <= 0 or dte < min_dte or dte > max_dte:
            eligible = False
            notes.append("days to expiry outside covered-call review window")

        if strike and close and strike <= close:
            notes.append("strike is near or below current price, call-away risk high")
        if strike and cost_basis and strike < cost_basis:
            notes.append("strike below cost basis")

        if eligible and bias == "bullish" and score >= 75:
            review_state = "income_review"
        elif eligible and bias == "bearish":
            review_state = "defensive_review"
            notes.append("underlying weak; covered call does not fix downside risk")
        elif eligible:
            review_state = "review"
        else:
            review_state = "skip"

        results.append(
            {
                **row,
                "covered_call_review_state": review_state,
                "covered_call_notes": notes or ["eligible for manual covered-call review"],
            }
        )
    return results


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python3 covered_call_filter.py data/sample_candidates.csv [market_bias]")
        return 2
    market_bias = argv[2] if len(argv) > 2 else "mixed"
    print(json.dumps(filter_covered_call_candidates(load_candidates_csv(argv[1]), market_bias=market_bias), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
