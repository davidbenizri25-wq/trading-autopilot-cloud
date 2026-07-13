"""Deterministic, decision-gated ranking for normalized long-option snapshots.

This module is deliberately provider- and broker-agnostic.  It ranks supplied
market observations for decision support; it never retrieves data, invents a
missing observation, creates an order, or weakens the underlying ENTER gate.

Canonical contract fields are:

``option_type``, ``expiration``, ``strike``, ``bid``, ``ask``, ``volume``,
``open_interest``, ``implied_volatility``, ``delta``, ``gamma``, ``theta``,
``vega``, and ``snapshot_timestamp``.  ``contract_symbol``, ``mid`` and
``iv_rank`` are optional.  IV is a decimal (for example, 0.32 for 32%).

Canonical context fields are ``verdict``, ``setup_state``, ``direction``,
``underlying_price``, and ``expected_holding_days``.  Supplying
``earnings_date`` (including an explicit ``None`` when no scheduled earnings
are known) makes the earnings check complete.  ``earnings_policy`` may be
``avoid`` (the default), ``allow``, or ``require``.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


DEFAULT_POLICY: Dict[str, Any] = {
    "max_spread_pct": 0.15,
    "min_volume": 100,
    "min_open_interest": 500,
    "max_projected_theta_decay_pct": 0.30,
    "preferred_abs_delta_min": 0.45,
    "preferred_abs_delta_max": 0.70,
    "min_dte": 1,
    "max_dte": 90,
    "max_snapshot_age_seconds": 15 * 60,
    "max_future_clock_skew_seconds": 60,
    "require_known_earnings": True,
}

_REQUIRED_CONTRACT_FIELDS: Tuple[str, ...] = (
    "underlying_ticker",
    "option_type",
    "expiration",
    "strike",
    "bid",
    "ask",
    "volume",
    "open_interest",
    "implied_volatility",
    "delta",
    "gamma",
    "theta",
    "vega",
    "snapshot_timestamp",
)

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,31}$")


def _number(value: Any) -> Optional[float]:
    """Return a finite float without treating booleans as market data."""

    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        parsed = float(value)
    else:
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        try:
            parsed = float(text)
        except ValueError:
            return None
    return parsed if math.isfinite(parsed) else None


def _ticker(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper().replace("/", ".")
    return text if _TICKER_RE.fullmatch(text) and ".." not in text else None


def _utc_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _calendar_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value.strip():
        text = value.strip()
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    return None


def _unique(items: Iterable[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for item in items:
        if item and item not in seen:
            result.append(item)
            seen.add(item)
    return result


def _merged_policy(overrides: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    policy = dict(DEFAULT_POLICY)
    if overrides:
        unknown = sorted(set(overrides) - set(DEFAULT_POLICY))
        if unknown:
            raise ValueError("unknown options ranking policy keys: " + ", ".join(unknown))
        policy.update(dict(overrides))

    numeric_positive = (
        "max_spread_pct",
        "min_volume",
        "min_open_interest",
        "max_projected_theta_decay_pct",
        "preferred_abs_delta_min",
        "preferred_abs_delta_max",
        "min_dte",
        "max_dte",
        "max_snapshot_age_seconds",
        "max_future_clock_skew_seconds",
    )
    for key in numeric_positive:
        number = _number(policy.get(key))
        if number is None or number < 0:
            raise ValueError("options ranking policy %s must be a non-negative number" % key)
        policy[key] = number

    if policy["max_spread_pct"] <= 0:
        raise ValueError("max_spread_pct must be greater than zero")
    if policy["max_projected_theta_decay_pct"] <= 0:
        raise ValueError("max_projected_theta_decay_pct must be greater than zero")
    if policy["max_snapshot_age_seconds"] <= 0:
        raise ValueError("max_snapshot_age_seconds must be greater than zero")
    if not 0 <= policy["preferred_abs_delta_min"] <= policy["preferred_abs_delta_max"] <= 1:
        raise ValueError("preferred absolute delta range must be between zero and one")
    if policy["min_dte"] > policy["max_dte"]:
        raise ValueError("min_dte cannot exceed max_dte")
    policy["require_known_earnings"] = bool(policy["require_known_earnings"])
    return policy


def _liquidity_quality(
    spread_pct: Optional[float],
    volume: Optional[float],
    open_interest: Optional[float],
    policy: Mapping[str, Any],
) -> str:
    if spread_pct is None or volume is None or open_interest is None:
        return "unavailable"
    if (
        spread_pct > policy["max_spread_pct"]
        or volume < policy["min_volume"]
        or open_interest < policy["min_open_interest"]
    ):
        return "poor"
    if spread_pct <= 0.05 and volume >= 1_000 and open_interest >= 5_000:
        return "excellent"
    if spread_pct <= 0.10 and volume >= 500 and open_interest >= 2_000:
        return "good"
    return "fair"


def _earnings_exposure(
    context: Mapping[str, Any],
    expiration: Optional[date],
    reference_date: date,
    require_known: bool,
) -> Tuple[Dict[str, Any], Optional[str]]:
    policy_name = str(context.get("earnings_policy", "avoid")).strip().lower()
    if policy_name not in {"avoid", "allow", "require"}:
        return (
            {
                "date": None,
                "date_known": False,
                "exposed": None,
                "policy": policy_name,
                "compatible": False,
            },
            "earnings policy is invalid",
        )

    earnings_key_supplied = "earnings_date" in context
    raw_earnings_date = context.get("earnings_date")
    earnings_date = _calendar_date(raw_earnings_date)
    if raw_earnings_date not in (None, "") and earnings_date is None:
        return (
            {
                "date": None,
                "date_known": False,
                "exposed": None,
                "policy": policy_name,
                "compatible": False,
            },
            "earnings date is invalid",
        )

    if not earnings_key_supplied:
        compatible = not require_known and policy_name != "require"
        return (
            {
                "date": None,
                "date_known": False,
                "exposed": None,
                "policy": policy_name,
                "compatible": compatible,
            },
            None if compatible else "earnings date unavailable",
        )

    exposed = bool(
        earnings_date is not None
        and expiration is not None
        and reference_date <= earnings_date <= expiration
    )
    if policy_name == "avoid":
        compatible = not exposed
        reason = "contract crosses earnings under avoid policy" if exposed else None
    elif policy_name == "require":
        compatible = exposed
        reason = None if exposed else "contract does not include required earnings event"
    else:
        compatible = True
        reason = None

    return (
        {
            "date": earnings_date.isoformat() if earnings_date else None,
            "date_known": True,
            "exposed": exposed,
            "policy": policy_name,
            "compatible": compatible,
        },
        reason,
    )


def _expected_move(
    underlying_price: Optional[float],
    implied_volatility: Optional[float],
    dte: Optional[int],
) -> Optional[Dict[str, Any]]:
    if (
        underlying_price is None
        or underlying_price <= 0
        or implied_volatility is None
        or implied_volatility <= 0
        or dte is None
        or dte <= 0
    ):
        return None
    amount = underlying_price * implied_volatility * math.sqrt(dte / 365.0)
    return {
        "amount": round(amount, 4),
        "percent": round(amount / underlying_price, 6),
        "lower_bound": round(max(0.0, underlying_price - amount), 4),
        "upper_bound": round(underlying_price + amount, 4),
        "estimated": True,
        "method": "underlying_price * implied_volatility * sqrt(DTE / 365)",
    }


def _score_contract(
    *,
    spread_pct: Optional[float],
    volume: Optional[float],
    open_interest: Optional[float],
    delta: Optional[float],
    dte: Optional[int],
    expected_holding_days: Optional[float],
    projected_theta_decay_pct: Optional[float],
    snapshot_age_seconds: Optional[float],
    incomplete_count: int,
    contract_specific_blocked: bool,
    policy: Mapping[str, Any],
) -> Tuple[float, Dict[str, float]]:
    deductions: Dict[str, float] = {}

    if spread_pct is None:
        deductions["spread"] = 30.0
    elif spread_pct <= 0.03:
        deductions["spread"] = 0.0
    elif spread_pct <= 0.05:
        deductions["spread"] = 3.0
    elif spread_pct <= 0.10:
        deductions["spread"] = 10.0
    elif spread_pct <= policy["max_spread_pct"]:
        deductions["spread"] = 20.0
    else:
        deductions["spread"] = 45.0

    if volume is None:
        deductions["volume"] = 20.0
    elif volume >= 1_000:
        deductions["volume"] = 0.0
    elif volume >= 500:
        deductions["volume"] = 3.0
    elif volume >= policy["min_volume"]:
        deductions["volume"] = 8.0
    else:
        deductions["volume"] = 25.0

    if open_interest is None:
        deductions["open_interest"] = 20.0
    elif open_interest >= 5_000:
        deductions["open_interest"] = 0.0
    elif open_interest >= 2_000:
        deductions["open_interest"] = 3.0
    elif open_interest >= policy["min_open_interest"]:
        deductions["open_interest"] = 8.0
    else:
        deductions["open_interest"] = 25.0

    if delta is None:
        deductions["delta_fit"] = 15.0
    else:
        absolute_delta = abs(delta)
        low = policy["preferred_abs_delta_min"]
        high = policy["preferred_abs_delta_max"]
        if low <= absolute_delta <= high:
            deductions["delta_fit"] = 0.0
        else:
            distance = low - absolute_delta if absolute_delta < low else absolute_delta - high
            deductions["delta_fit"] = round(min(20.0, 5.0 + distance * 50.0), 4)

    if dte is None or expected_holding_days is None:
        deductions["horizon_fit"] = 15.0
    elif dte < expected_holding_days:
        deductions["horizon_fit"] = 20.0
    elif dte < expected_holding_days * 2:
        deductions["horizon_fit"] = 10.0
    elif dte <= max(21.0, expected_holding_days * 4):
        deductions["horizon_fit"] = 0.0
    else:
        deductions["horizon_fit"] = min(10.0, (dte - max(21.0, expected_holding_days * 4)) / 10.0)

    if projected_theta_decay_pct is None:
        deductions["theta"] = 15.0
    else:
        deductions["theta"] = round(
            min(30.0, 30.0 * projected_theta_decay_pct / policy["max_projected_theta_decay_pct"]),
            4,
        )

    if snapshot_age_seconds is None:
        deductions["freshness"] = 15.0
    else:
        age_ratio = max(0.0, snapshot_age_seconds) / policy["max_snapshot_age_seconds"]
        deductions["freshness"] = round(min(15.0, age_ratio * 5.0), 4)

    if incomplete_count:
        deductions["incomplete_data"] = min(60.0, float(incomplete_count * 12))

    score = max(0.0, 100.0 - sum(deductions.values()))
    if contract_specific_blocked:
        score = min(score, 49.0)
    return round(score, 2), deductions


def _lower_rank_reasons(best: Mapping[str, Any], candidate: Mapping[str, Any]) -> List[str]:
    reasons: List[str] = []
    if not candidate.get("contract_quality_passes"):
        reasons.extend(candidate.get("contract_rejection_reasons", []))
    if candidate.get("ranked_score", 0) < best.get("ranked_score", 0):
        reasons.append("lower deterministic fit score")
    best_spread = best.get("spread_pct")
    candidate_spread = candidate.get("spread_pct")
    if best_spread is not None and candidate_spread is not None and candidate_spread > best_spread:
        reasons.append("wider bid/ask spread")
    if (candidate.get("open_interest") or 0) < (best.get("open_interest") or 0):
        reasons.append("lower open interest")
    if (candidate.get("volume") or 0) < (best.get("volume") or 0):
        reasons.append("lower volume")
    best_delta = best.get("delta")
    candidate_delta = candidate.get("delta")
    preferred_low = best.get("strike_or_delta_range", {}).get("preferred_abs_delta_min")
    preferred_high = best.get("strike_or_delta_range", {}).get("preferred_abs_delta_max")
    if None not in (best_delta, candidate_delta, preferred_low, preferred_high):
        midpoint = (preferred_low + preferred_high) / 2.0
        if abs(abs(candidate_delta) - midpoint) > abs(abs(best_delta) - midpoint):
            reasons.append("delta is farther from the preferred range center")
    return _unique(reasons) or ["stable tie-break order"]


def _evaluate_contract(
    contract: Mapping[str, Any],
    context: Mapping[str, Any],
    reference_time: datetime,
    policy: Mapping[str, Any],
    global_rejections: Sequence[str],
    provisional: bool,
) -> Dict[str, Any]:
    missing = [field for field in _REQUIRED_CONTRACT_FIELDS if contract.get(field) in (None, "")]
    invalid: List[str] = []
    contract_rejections: List[str] = []
    rationale: List[str] = []

    underlying_ticker = _ticker(contract.get("underlying_ticker"))
    if contract.get("underlying_ticker") not in (None, "") and underlying_ticker is None:
        invalid.append("underlying_ticker")
    expected_underlying = _ticker(context.get("underlying_ticker"))
    if underlying_ticker is not None and expected_underlying is not None:
        if underlying_ticker != expected_underlying:
            contract_rejections.append(
                "contract underlying does not match the analyzed security"
            )
        else:
            rationale.append("contract underlying matches the analyzed security")

    option_type_raw = contract.get("option_type")
    option_type = str(option_type_raw).strip().lower() if option_type_raw is not None else None
    if option_type not in {"call", "put"}:
        if option_type_raw not in (None, ""):
            invalid.append("option_type")
        option_type = None

    expiration_date = _calendar_date(contract.get("expiration"))
    if contract.get("expiration") not in (None, "") and expiration_date is None:
        invalid.append("expiration")
    dte = (expiration_date - reference_time.date()).days if expiration_date else None

    strike = _number(contract.get("strike"))
    bid = _number(contract.get("bid"))
    ask = _number(contract.get("ask"))
    volume = _number(contract.get("volume"))
    open_interest = _number(contract.get("open_interest"))
    implied_volatility = _number(contract.get("implied_volatility"))
    delta = _number(contract.get("delta"))
    gamma = _number(contract.get("gamma"))
    theta = _number(contract.get("theta"))
    vega = _number(contract.get("vega"))

    numeric_values = {
        "strike": strike,
        "bid": bid,
        "ask": ask,
        "volume": volume,
        "open_interest": open_interest,
        "implied_volatility": implied_volatility,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
    }
    for field, value in numeric_values.items():
        if contract.get(field) not in (None, "") and value is None:
            invalid.append(field)

    if strike is not None and strike <= 0:
        invalid.append("strike")
    if bid is not None and bid < 0:
        invalid.append("bid")
    if ask is not None and ask < 0:
        invalid.append("ask")
    if volume is not None and volume < 0:
        invalid.append("volume")
    if open_interest is not None and open_interest < 0:
        invalid.append("open_interest")
    if implied_volatility is not None and implied_volatility <= 0:
        invalid.append("implied_volatility")
    if delta is not None and not -1 <= delta <= 1:
        invalid.append("delta")
    if gamma is not None and gamma < 0:
        invalid.append("gamma")
    if vega is not None and vega < 0:
        invalid.append("vega")

    supplied_mid = "mid" in contract and contract.get("mid") not in (None, "")
    mid = _number(contract.get("mid")) if supplied_mid else None
    mid_source: Optional[str] = "snapshot" if supplied_mid else None
    if supplied_mid and (mid is None or mid <= 0):
        invalid.append("mid")
        mid = None
    elif (
        supplied_mid
        and mid is not None
        and bid is not None
        and ask is not None
        and not bid <= mid <= ask
    ):
        invalid.append("mid")
    elif not supplied_mid and bid is not None and ask is not None and ask >= bid:
        mid = round((bid + ask) / 2.0, 4)
        mid_source = "calculated_from_bid_ask"

    spread_dollars: Optional[float] = None
    spread_pct: Optional[float] = None
    if bid is not None and ask is not None:
        if ask < bid:
            contract_rejections.append("chain quote is incomplete: ask is below bid")
        else:
            spread_dollars = round(ask - bid, 4)
            if mid is not None and mid > 0:
                spread_pct = round(spread_dollars / mid, 6)

    snapshot_time = _utc_datetime(contract.get("snapshot_timestamp"))
    snapshot_age_seconds: Optional[float] = None
    if contract.get("snapshot_timestamp") not in (None, "") and snapshot_time is None:
        invalid.append("snapshot_timestamp")
    elif snapshot_time is not None:
        snapshot_age_seconds = (reference_time - snapshot_time).total_seconds()

    missing = sorted(set(missing))
    invalid = sorted(set(invalid))
    if missing:
        contract_rejections.append("chain data incomplete: missing " + ", ".join(missing))
    if invalid:
        contract_rejections.append("chain data incomplete: invalid " + ", ".join(invalid))
    if dte is not None and (dte < policy["min_dte"] or dte > policy["max_dte"]):
        contract_rejections.append(
            "DTE outside configured range %.0f-%.0f" % (policy["min_dte"], policy["max_dte"])
        )
    if snapshot_age_seconds is not None:
        if snapshot_age_seconds > policy["max_snapshot_age_seconds"]:
            contract_rejections.append("chain snapshot is stale")
        elif snapshot_age_seconds < -policy["max_future_clock_skew_seconds"]:
            contract_rejections.append("chain snapshot timestamp is implausibly in the future")

    direction = str(context.get("direction", "")).strip().lower()
    expected_type = "call" if direction == "bullish" else "put" if direction == "bearish" else None
    if option_type and expected_type:
        if option_type != expected_type:
            contract_rejections.append("contract direction does not fit the underlying thesis")
        else:
            rationale.append("%s matches the %s underlying thesis" % (option_type, direction))

    if spread_pct is None:
        contract_rejections.append("chain data incomplete: spread cannot be calculated")
    elif spread_pct > policy["max_spread_pct"]:
        contract_rejections.append(
            "excessive spread %.2f%% exceeds %.2f%% limit"
            % (spread_pct * 100.0, policy["max_spread_pct"] * 100.0)
        )
    else:
        rationale.append("spread is within the configured liquidity limit")

    if volume is not None and volume < policy["min_volume"]:
        contract_rejections.append(
            "thin volume %.0f is below %.0f minimum" % (volume, policy["min_volume"])
        )
    if open_interest is not None and open_interest < policy["min_open_interest"]:
        contract_rejections.append(
            "thin open interest %.0f is below %.0f minimum"
            % (open_interest, policy["min_open_interest"])
        )

    earnings, earnings_rejection = _earnings_exposure(
        context,
        expiration_date,
        reference_time.date(),
        bool(policy["require_known_earnings"]),
    )
    if earnings_rejection:
        contract_rejections.append(earnings_rejection)
    elif earnings["exposed"]:
        rationale.append("earnings exposure is explicitly compatible with the thesis policy")
    else:
        rationale.append("contract does not cross an incompatible earnings event")

    expected_holding_days = _number(context.get("expected_holding_days"))
    projected_theta_decay_pct: Optional[float] = None
    if theta is not None and mid is not None and mid > 0 and expected_holding_days is not None:
        projected_theta_decay_pct = abs(theta) * expected_holding_days / mid
        if projected_theta_decay_pct > policy["max_projected_theta_decay_pct"]:
            contract_rejections.append(
                "unreasonable theta: estimated %.2f%% midpoint decay over %.1f holding days exceeds %.2f%% limit"
                % (
                    projected_theta_decay_pct * 100.0,
                    expected_holding_days,
                    policy["max_projected_theta_decay_pct"] * 100.0,
                )
            )
        else:
            rationale.append("estimated theta decay fits the expected holding period")

    if delta is not None:
        absolute_delta = abs(delta)
        if policy["preferred_abs_delta_min"] <= absolute_delta <= policy["preferred_abs_delta_max"]:
            rationale.append("delta is inside the preferred range")
        else:
            rationale.append("delta is outside the preferred range and lowers the score")

    if dte is not None and expected_holding_days is not None and expected_holding_days > 0:
        if dte >= expected_holding_days * 2:
            rationale.append("DTE provides at least twice the expected holding period")
        elif dte < expected_holding_days:
            contract_rejections.append("expiration is shorter than the expected holding period")

    breakeven: Optional[float] = None
    if option_type and strike is not None and mid is not None:
        breakeven = round(strike + mid if option_type == "call" else strike - mid, 4)

    target_price = _number(context.get("target_price"))
    if target_price is not None and breakeven is not None and option_type:
        if (option_type == "call" and target_price <= breakeven) or (
            option_type == "put" and target_price >= breakeven
        ):
            contract_rejections.append("chart target does not clear the midpoint breakeven")
        else:
            rationale.append("midpoint breakeven is inside the supplied chart target")

    underlying_price = _number(context.get("underlying_price"))
    expected_move = _expected_move(underlying_price, implied_volatility, dte)
    liquidity_quality = _liquidity_quality(spread_pct, volume, open_interest, policy)
    if liquidity_quality in {"excellent", "good", "fair"}:
        rationale.append("estimated liquidity quality is %s" % liquidity_quality)

    contract_rejections = _unique(contract_rejections)
    contract_specific_blocked = bool(contract_rejections)
    score, score_deductions = _score_contract(
        spread_pct=spread_pct,
        volume=volume,
        open_interest=open_interest,
        delta=delta,
        dte=dte,
        expected_holding_days=expected_holding_days,
        projected_theta_decay_pct=projected_theta_decay_pct,
        snapshot_age_seconds=snapshot_age_seconds,
        incomplete_count=len(missing) + len(invalid),
        contract_specific_blocked=contract_specific_blocked,
        policy=policy,
    )

    quality_passes = not contract_specific_blocked
    recommendation_eligible = quality_passes and not global_rejections
    all_rejections = _unique(list(global_rejections) + contract_rejections)

    output: Dict[str, Any] = {
        "underlying_ticker": underlying_ticker,
        "contract_symbol": contract.get("contract_symbol"),
        "call_put": option_type,
        "expiration": expiration_date.isoformat() if expiration_date else None,
        "strike": strike,
        "strike_or_delta_range": {
            "strike": strike,
            "actual_delta": delta,
            "preferred_abs_delta_min": policy["preferred_abs_delta_min"],
            "preferred_abs_delta_max": policy["preferred_abs_delta_max"],
            "preferred_range_source": "ranking_policy",
        },
        "dte": dte,
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "mid_source": mid_source,
        "spread_dollars": spread_dollars,
        "spread_pct": spread_pct,
        "volume": volume,
        "open_interest": open_interest,
        "implied_volatility": implied_volatility,
        "delta": delta,
        "gamma": gamma,
        "theta": theta,
        "vega": vega,
        "breakeven": breakeven,
        "breakeven_basis": "calculated from midpoint premium" if breakeven is not None else None,
        "expected_move": expected_move,
        "earnings_exposure": earnings,
        "liquidity_quality": liquidity_quality,
        "theta_holding_period_estimate": {
            "expected_holding_days": expected_holding_days,
            "projected_midpoint_decay_pct": round(projected_theta_decay_pct, 6)
            if projected_theta_decay_pct is not None
            else None,
            "estimated": projected_theta_decay_pct is not None,
            "method": "abs(theta) * expected_holding_days / midpoint"
            if projected_theta_decay_pct is not None
            else None,
        },
        "snapshot_timestamp": snapshot_time.isoformat() if snapshot_time else None,
        "snapshot_age_seconds": round(snapshot_age_seconds, 3)
        if snapshot_age_seconds is not None
        else None,
        "fit_rationale": _unique(rationale),
        "contract_rejection_reasons": contract_rejections,
        "rejection_reasons": all_rejections,
        "contract_quality_passes": quality_passes,
        "recommendation_eligible": recommendation_eligible,
        "provisional": provisional and quality_passes,
        "ranked_score": score,
        "score_deductions": score_deductions,
        "why_ranked_lower": [],
    }

    # IV rank is intentionally conditional: absence is not represented by an
    # invented zero, percentile, or proxy.
    if "iv_rank" in contract and contract.get("iv_rank") not in (None, ""):
        iv_rank = _number(contract.get("iv_rank"))
        if iv_rank is None or not 0 <= iv_rank <= 100:
            output["iv_rank"] = None
            output["contract_quality_passes"] = False
            output["recommendation_eligible"] = False
            output["provisional"] = False
            output["contract_rejection_reasons"] = _unique(
                output["contract_rejection_reasons"] + ["chain data incomplete: invalid iv_rank"]
            )
            output["rejection_reasons"] = _unique(
                list(global_rejections) + output["contract_rejection_reasons"]
            )
            output["ranked_score"] = min(output["ranked_score"], 49.0)
        else:
            output["iv_rank"] = iv_rank
            output["iv_rank_source"] = "snapshot"
    return output


def rank_option_contracts(
    contracts: Sequence[Mapping[str, Any]],
    context: Mapping[str, Any],
    *,
    now: Optional[datetime] = None,
    policy: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Rank option snapshots while enforcing the underlying decision gate.

    ``now`` is injectable so freshness, DTE, and earnings results are fully
    deterministic in tests.  The result is ``RECOMMEND`` only when the
    underlying verdict and state are ENTER and at least one complete contract
    clears every hard block.  ARMED results retain quality rankings, but return
    ``WAIT`` with no recommendation.
    """

    if not isinstance(context, Mapping):
        raise TypeError("context must be a mapping")
    if isinstance(contracts, (str, bytes)) or not isinstance(contracts, Sequence):
        raise TypeError("contracts must be a sequence of mappings")

    resolved_policy = _merged_policy(policy)
    reference_time = _utc_datetime(now) if now is not None else datetime.now(timezone.utc)
    if reference_time is None:
        raise ValueError("now must be a datetime or ISO timestamp")

    verdict = str(context.get("verdict", "")).strip().upper()
    setup_state = str(context.get("setup_state", "")).strip().upper()
    direction = str(context.get("direction", "")).strip().lower()
    underlying_ticker = _ticker(context.get("underlying_ticker"))
    underlying_price = _number(context.get("underlying_price"))
    expected_holding_days = _number(context.get("expected_holding_days"))

    global_rejections: List[str] = []
    if setup_state == "ARMED":
        global_rejections.append("underlying setup is ARMED; wait for ENTER confirmation")
    elif verdict != "ENTER":
        global_rejections.append("underlying verdict is not ENTER")
    elif setup_state != "ENTER":
        global_rejections.append("underlying setup state is not ENTER")
    if direction not in {"bullish", "bearish"}:
        global_rejections.append("underlying decision context missing bullish or bearish direction")
    if underlying_ticker is None:
        global_rejections.append("underlying decision context missing a valid underlying_ticker")
    if underlying_price is None or underlying_price <= 0:
        global_rejections.append("underlying decision context missing a valid underlying_price")
    if expected_holding_days is None or expected_holding_days <= 0:
        global_rejections.append("underlying decision context missing valid expected_holding_days")
    if context.get("chain_complete") is False:
        global_rejections.append("options chain is marked incomplete")
    global_rejections = _unique(global_rejections)

    provisional = setup_state == "ARMED"
    evaluated: List[Dict[str, Any]] = []
    for contract in contracts:
        if not isinstance(contract, Mapping):
            raise TypeError("each option contract must be a mapping")
        evaluated.append(
            _evaluate_contract(
                contract,
                context,
                reference_time,
                resolved_policy,
                global_rejections,
                provisional,
            )
        )

    def sort_key(item: Mapping[str, Any]) -> Tuple[Any, ...]:
        spread = item.get("spread_pct")
        expiration = item.get("expiration") or "9999-12-31"
        strike = item.get("strike")
        return (
            0 if item.get("contract_quality_passes") else 1,
            -float(item.get("ranked_score") or 0),
            float(spread) if spread is not None else float("inf"),
            -float(item.get("open_interest") or 0),
            -float(item.get("volume") or 0),
            expiration,
            float(strike) if strike is not None else float("inf"),
            str(item.get("contract_symbol") or ""),
        )

    evaluated.sort(key=sort_key)
    for rank, item in enumerate(evaluated, 1):
        item["rank"] = rank

    best_quality = next((item for item in evaluated if item["contract_quality_passes"]), None)
    if best_quality is not None:
        for item in evaluated:
            if item is not best_quality:
                item["why_ranked_lower"] = _lower_rank_reasons(best_quality, item)

    eligible = [item for item in evaluated if item["recommendation_eligible"]]
    recommendation = eligible[0] if eligible else None
    if recommendation is not None:
        status = "RECOMMEND"
    elif provisional:
        status = "WAIT"
    else:
        status = "PASS"

    if not contracts:
        global_rejections.append("options chain contains no contracts")
    if not recommendation and not global_rejections and evaluated:
        top_contract_rejections = evaluated[0].get("contract_rejection_reasons", [])
        global_rejections.extend(top_contract_rejections or ["no contract passed every hard block"])
    global_rejections = _unique(global_rejections)

    recommendation_summary = None
    if recommendation is not None:
        recommendation_summary = {
            "rank": recommendation["rank"],
            "contract_symbol": recommendation["contract_symbol"],
            "call_put": recommendation["call_put"],
            "expiration": recommendation["expiration"],
            "strike_or_delta_range": recommendation["strike_or_delta_range"],
            "dte": recommendation["dte"],
            "ranked_score": recommendation["ranked_score"],
        }

    provisional_leader = None
    if provisional and best_quality is not None:
        provisional_leader = {
            "rank": best_quality["rank"],
            "contract_symbol": best_quality["contract_symbol"],
            "call_put": best_quality["call_put"],
            "expiration": best_quality["expiration"],
            "strike_or_delta_range": best_quality["strike_or_delta_range"],
            "dte": best_quality["dte"],
            "ranked_score": best_quality["ranked_score"],
            "label": "provisional only; not a recommendation",
        }

    return {
        "status": status,
        "recommendation": recommendation_summary,
        "provisional_leader": provisional_leader,
        "provisional_rankings": provisional,
        "underlying_verdict": verdict or None,
        "underlying_setup_state": setup_state or None,
        "underlying_direction": direction or None,
        "evaluated_at": reference_time.isoformat(),
        "policy": dict(resolved_policy),
        "rejection_reasons": global_rejections,
        "why_other_contracts_ranked_lower": [
            {
                "rank": item["rank"],
                "contract_symbol": item["contract_symbol"],
                "expiration": item["expiration"],
                "strike": item["strike"],
                "reasons": item["why_ranked_lower"],
            }
            for item in evaluated
            if item["rank"] > 1
        ],
        "ranked_contracts": evaluated,
    }


# Small, explicit aliases make the public entry point easy to discover without
# changing its return contract.
rank_options_contracts = rank_option_contracts
rank_contracts = rank_option_contracts


__all__ = [
    "DEFAULT_POLICY",
    "rank_option_contracts",
    "rank_options_contracts",
    "rank_contracts",
]
