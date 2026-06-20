"""Under-$25 share review filter.

Decision support only. Produces manual long-share review ideas and never buys,
sells, shorts, stages, previews, or submits orders.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from risk_config import RISK_CONFIG
from scoring import _bool, _float, load_candidates_csv, rank_candidates


MIN_UNDERLYING_AVG_VOLUME = 750_000
MIN_UNDER_25_AVG_VOLUME = 1_000_000


def filter_share_candidates(rows: list[dict[str, Any]], market_bias: str = "mixed") -> list[dict[str, Any]]:
    ranked = rank_candidates(rows, market_bias=market_bias)
    results: list[dict[str, Any]] = []
    for row in ranked:
        if str(row.get("asset_type", "")).strip().lower() != "equity":
            continue

        notes: list[str] = []
        eligible = True
        manual_override = _bool(row.get("manual_override"))
        score = int(row["score"])
        bias = str(row["bias"])
        close = _float(row.get("close"))
        avg_volume = _float(row.get("avg_volume"))
        bullish_invalid = _float(row.get("bullish_invalid"))
        tags = set(row.get("setup_tags", []))
        estimated_position_cost = round(close * 100, 2) if close > 0 else 0.0
        preferred_under_20 = 0 < close <= 20

        if "directional_conflict" in tags:
            eligible = False
            notes.append("directional conflict blocks shares review")
        if avg_volume <= 0:
            eligible = False
            notes.append("average volume missing")
        elif avg_volume < MIN_UNDERLYING_AVG_VOLUME:
            eligible = False
            notes.append("average volume below share threshold")
        if 0 < close <= 25 and avg_volume < MIN_UNDER_25_AVG_VOLUME:
            eligible = False
            notes.append("under-$25 average volume below 1M threshold")
        if "thin_underlying_volume" in tags:
            if manual_override:
                notes.append("manual override: thin underlying volume")
            else:
                eligible = False
                notes.append("thin underlying volume blocks shares review")
        if bias != "bullish":
            eligible = False
            notes.append("share filter is for bullish long share ideas")
        if score < 70:
            eligible = False
            notes.append("score below B+ shares threshold")
        if close > 25 and not manual_override:
            eligible = False
            notes.append("share candidate above $25 without manual override")
        if estimated_position_cost > RISK_CONFIG["shares_bankroll"] and not manual_override:
            eligible = False
            notes.append("100-share cost exceeds shares bankroll")

        risk_to_invalid: float | None = None
        if bias == "bullish" and close > 0 and 0 < bullish_invalid < close:
            risk_to_invalid = round(100 * (close - bullish_invalid), 2)
            if risk_to_invalid > RISK_CONFIG["a_plus_share_risk_max"] and not manual_override:
                eligible = False
                notes.append("risk to invalidation above $75 cap")
        else:
            eligible = False
            notes.append("risk to invalidation unavailable")

        review_state = "review" if eligible else "skip"
        filter_notes = notes or ["eligible for manual under-$25 share review"]
        results.append(
            {
                **row,
                "preferred_under_20": preferred_under_20,
                "estimated_position_cost": estimated_position_cost,
                "risk_to_invalid": risk_to_invalid,
                "shares_review_state": review_state,
                "share_filter_note": "; ".join(filter_notes),
                "share_filter_notes": filter_notes,
            }
        )
    return results


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python3 shares_filter.py data/sample_candidates.csv [market_bias]")
        return 2
    market_bias = argv[2] if len(argv) > 2 else "mixed"
    print(json.dumps(filter_share_candidates(load_candidates_csv(argv[1]), market_bias=market_bias), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
