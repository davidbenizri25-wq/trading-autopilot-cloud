"""Options review filter for Trading Autopilot.

Decision support only. Produces manual review candidates and never creates,
stages, previews, or submits option orders.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from risk_config import RISK_CONFIG
from scoring import _bool, _float, load_candidates_csv, rank_candidates


MIN_UNDERLYING_AVG_VOLUME = 1_000_000


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


def _premium_costs(row: dict[str, Any]) -> dict[str, float | None]:
    option_mid = parse_optional_float(row.get("option_mid"))
    option_ask = parse_optional_float(row.get("option_ask"))
    option_bid = parse_optional_float(row.get("option_bid"))
    option_mid_cost = round(option_mid * 100, 2) if option_mid is not None else None
    option_ask_cost = round(option_ask * 100, 2) if option_ask is not None else None
    option_bid_cost = round(option_bid * 100, 2) if option_bid is not None else None
    premium_cost_for_risk = option_ask_cost if option_ask_cost is not None else option_mid_cost if option_mid_cost is not None else option_bid_cost
    return {
        "option_mid_cost": option_mid_cost,
        "option_ask_cost": option_ask_cost,
        "option_bid_cost": option_bid_cost,
        "premium_cost_for_risk": premium_cost_for_risk,
    }


def _earnings_status(days_to_earnings: float | None, avoid_earnings_within_days: int) -> str:
    if days_to_earnings is None:
        return "unknown"
    if days_to_earnings == 0:
        return "today"
    if 0 < days_to_earnings <= avoid_earnings_within_days:
        return "within_window"
    return "clear"


def filter_options_candidates(
    rows: list[dict[str, Any]],
    market_bias: str = "mixed",
    avoid_earnings_within_days: int | None = None,
    min_underlying_avg_volume: int = MIN_UNDERLYING_AVG_VOLUME,
) -> list[dict[str, Any]]:
    if avoid_earnings_within_days is None:
        avoid_earnings_within_days = int(RISK_CONFIG.get("avoid_earnings_within_days", 10))
    ranked = rank_candidates(rows, market_bias=market_bias)
    results: list[dict[str, Any]] = []
    for row in ranked:
        if str(row.get("asset_type", "")).strip().lower() != "equity":
            continue

        notes: list[str] = []
        eligible = True
        manual_override = _bool(row.get("manual_override"))
        score = int(row["score"])
        grade = str(row["grade"])
        state = str(row["state"])
        bias = str(row["bias"])
        dte = _float(row.get("days_to_expiry"))
        premium_costs = _premium_costs(row)
        premium_cost_for_risk = premium_costs["premium_cost_for_risk"]
        open_exposure = _float(row.get("open_options_exposure"))
        avg_volume = _float(row.get("avg_volume"))
        tags = set(row.get("setup_tags", []))
        risk_flags = set(row.get("risk_flags", []))

        side = "call_review" if bias == "bullish" else "put_review" if bias == "bearish" else "none"

        # underlying average volume gate
        if avg_volume <= 0:
            if manual_override:
                notes.append("manual override: underlying average volume missing")
            else:
                eligible = False
                notes.append("underlying average volume missing")
        elif avg_volume < min_underlying_avg_volume:
            if manual_override:
                notes.append("manual override: underlying average volume below options threshold")
            else:
                eligible = False
                notes.append("underlying average volume below options threshold")

        if "thin_underlying_volume" in tags or "under-25 name with thin volume" in risk_flags:
            if manual_override:
                notes.append("manual override: thin underlying volume")
            else:
                eligible = False
                notes.append("thin underlying volume blocks options review")

        if "directional_conflict" in tags:
            eligible = False
            notes.append("directional conflict blocks options review")
        if bias not in {"bullish", "bearish"}:
            eligible = False
            notes.append("neutral/context bias skipped for options review")
        if score < 75:
            eligible = False
            notes.append("score below A- options threshold")
        if dte <= 1 and (score < 85 or state != "priority_watch"):
            eligible = False
            notes.append("0DTE/1DTE requires A+ priority_watch")
        if _float(row.get("option_volume")) < 1000:
            eligible = False
            notes.append("option volume too low")
        if _float(row.get("option_open_interest")) < 1000:
            eligible = False
            notes.append("open interest too low")
        if _float(row.get("option_spread_pct")) > 0.15:
            eligible = False
            notes.append("option spread too wide")
        if dte <= 0 or dte > 60:
            eligible = False
            notes.append("days to expiry outside review window")
        days_to_earnings = parse_optional_float(row.get("days_to_earnings"))
        earnings_status = _earnings_status(days_to_earnings, avoid_earnings_within_days)
        if earnings_status == "today":
            eligible = False
            notes.append("earnings today")
        elif earnings_status == "within_window":
            eligible = False
            notes.append(f"earnings within {avoid_earnings_within_days} days")
        elif earnings_status == "unknown":
            notes.append("earnings date unknown")
        if _float(row.get("iv_rank")) > 80:
            eligible = False
            notes.append("IV rank too high")

        if dte <= 1:
            cap = RISK_CONFIG["zero_dte_option_premium_max"]
        elif grade == "A+":
            cap = RISK_CONFIG["a_plus_option_premium_max"]
        else:
            cap = RISK_CONFIG["normal_option_premium_max"]

        if premium_cost_for_risk is None:
            eligible = False
            notes.append("option premium unavailable")
        elif premium_cost_for_risk > 200 and not manual_override:
            eligible = False
            notes.append("premium cost above $200 hard skip")
        elif premium_cost_for_risk > cap and not manual_override:
            eligible = False
            notes.append("premium cost above configured cap")
        elif dte > 1 and premium_cost_for_risk < RISK_CONFIG["normal_option_premium_min"]:
            eligible = False
            notes.append("premium below normal $50-$75 target")

        if premium_cost_for_risk is not None and open_exposure + premium_cost_for_risk > RISK_CONFIG["total_open_options_exposure_max"] and not manual_override:
            eligible = False
            notes.append("would exceed total open options exposure cap")

        review_state = "review" if eligible else "skip"
        results.append(
            {
                **row,
                "candidate_side": side,
                "option_premium_cost": premium_cost_for_risk,
                "option_mid_cost": premium_costs["option_mid_cost"],
                "option_ask_cost": premium_costs["option_ask_cost"],
                "option_bid_cost": premium_costs["option_bid_cost"],
                "premium_cost_for_risk": premium_cost_for_risk,
                "earnings_status": earnings_status,
                "options_review_state": review_state,
                "options_filter_notes": notes or ["eligible for manual options-chain review"],
            }
        )
    return results


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python3 options_filter.py data/sample_candidates.csv [market_bias]")
        return 2
    market_bias = argv[2] if len(argv) > 2 else "mixed"
    print(json.dumps(filter_options_candidates(load_candidates_csv(argv[1]), market_bias=market_bias), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
