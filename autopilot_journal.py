"""Evidence-safe outcome journaling and calibration summaries.

This module deliberately evaluates observations, not hypothetical fills.  It
uses only completed OHLC bars whose completion timestamp is strictly later
than the saved analysis timestamp.  When OHLC data cannot establish intrabar
order, the result is explicitly marked ambiguous instead of choosing the most
favourable path.

All public inputs and outputs are JSON-compatible mappings/lists/scalars.  No
function in this module mutates its inputs or changes strategy thresholds.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from collections import OrderedDict
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping, Optional, Sequence, Tuple


JOURNAL_METHODOLOGY_VERSION = "1.0.0"
CALIBRATION_METHODOLOGY_VERSION = "1.0.0"
PROPOSAL_RECORD_VERSION = "1.0.0"
TRADING_DAY_HORIZONS = (1, 3, 5, 10, 20)


def _as_number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _rounded(value: Optional[float], digits: int = 6) -> Optional[float]:
    if value is None or not math.isfinite(value):
        return None
    rounded = round(value, digits)
    return 0.0 if rounded == 0 else rounded


def _parse_timestamp(value: Any) -> Optional[datetime]:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    elif isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number):
            return None
        if abs(number) > 10_000_000_000:
            number /= 1000.0
        try:
            parsed = datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed = datetime.strptime(text, "%Y-%m-%d")
            except ValueError:
                return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _first(mapping: Mapping[str, Any], keys: Sequence[str]) -> Any:
    for key in keys:
        if key in mapping and mapping.get(key) is not None:
            return mapping.get(key)
    return None


def _first_number(mappings: Iterable[Mapping[str, Any]], keys: Sequence[str]) -> Optional[float]:
    for mapping in mappings:
        number = _as_number(_first(mapping, keys))
        if number is not None:
            return number
    return None


def _direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"bull", "bullish", "long", "up", "uptrend"}:
        return "bullish"
    if text in {"bear", "bearish", "short", "down", "downtrend"}:
        return "bearish"
    return "unknown"


def _tracking_action(saved: Mapping[str, Any]) -> str:
    raw = _first(saved, ("tracking_status", "action", "verdict", "decision", "state"))
    value = str(raw or "").strip().upper()
    if value == "VERDICT":
        value = str(saved.get("verdict") or saved.get("state") or "").strip().upper()
    if value in {"ENTER", "ENTERED", "OPEN"}:
        return "ENTER"
    if value in {"PASS", "PASSED", "BLOCKED", "INVALIDATED"}:
        return "PASS"
    if value in {"WATCH", "WATCHING", "WAIT", "ARMED", "FORMING", "EXTENDED"}:
        return "WATCH"
    return "WATCH"


def _saved_timestamp(saved: Mapping[str, Any]) -> datetime:
    candidates = (
        "saved_at",
        "recorded_at",
        "analysis_timestamp",
        "evaluated_at",
        "created_at",
        "entered_at",
        "closed_at",
        "data_timestamp",
    )
    parsed = _parse_timestamp(_first(saved, candidates))
    if parsed is None:
        raise ValueError("saved analysis requires a valid saved_at or equivalent timestamp")
    return parsed


def _targets(plan: Mapping[str, Any]) -> list:
    candidates = []
    for key, label in (
        ("target_1", "target_1"),
        ("target_2", "target_2"),
        ("stretch_target", "stretch_target"),
        ("target_3", "target_3"),
    ):
        number = _as_number(plan.get(key))
        if number is not None:
            candidates.append((label, number))
    if candidates:
        return candidates
    raw = plan.get("targets")
    if isinstance(raw, Mapping):
        for label, value in raw.items():
            number = _as_number(value)
            if number is not None:
                candidates.append((str(label), number))
    elif isinstance(raw, (list, tuple)):
        for index, value in enumerate(raw, 1):
            number = _as_number(value)
            if number is not None:
                candidates.append(("target_{0}".format(index), number))
    else:
        number = _as_number(raw)
        if number is not None:
            candidates.append(("target_1", number))
    return candidates


def _bar_timestamp(bar: Mapping[str, Any]) -> Tuple[Optional[datetime], str]:
    # A completion/end timestamp is the strongest evidence that a bar was
    # available at the cutoff.  Provider timestamps are a fallback.
    for key in ("end_time", "close_time", "completed_at"):
        if key in bar:
            parsed = _parse_timestamp(bar.get(key))
            if parsed is not None:
                return parsed, key
    for key in ("timestamp", "time", "datetime", "date", "t"):
        if key in bar:
            parsed = _parse_timestamp(bar.get(key))
            if parsed is not None:
                return parsed, key
    return None, ""


def _explicit_completion(bar: Mapping[str, Any]) -> Optional[bool]:
    for key in ("complete", "completed", "is_complete", "is_completed", "final"):
        if key in bar:
            value = bar.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, (int, float)) and value in (0, 1):
                return bool(value)
            text = str(value).strip().lower()
            if text in {"true", "yes", "complete", "completed", "final", "1"}:
                return True
            if text in {"false", "no", "open", "forming", "partial", "0"}:
                return False
    return None


def _normalize_bars(
    bars: Sequence[Mapping[str, Any]],
    *,
    anchor: datetime,
    cutoff: Optional[datetime],
) -> Tuple[list, dict]:
    counters = {
        "input_bar_count": len(bars),
        "eligible_completed_bar_count": 0,
        "excluded_pre_anchor_count": 0,
        "excluded_after_cutoff_count": 0,
        "excluded_incomplete_count": 0,
        "excluded_invalid_count": 0,
        "completion_assumed_count": 0,
        "deduplicated_bar_count": 0,
    }
    eligible = []
    for sequence, raw in enumerate(bars):
        if not isinstance(raw, Mapping):
            counters["excluded_invalid_count"] += 1
            continue
        timestamp, timestamp_field = _bar_timestamp(raw)
        high = _as_number(raw.get("high"))
        low = _as_number(raw.get("low"))
        close = _as_number(raw.get("close"))
        open_price = _as_number(raw.get("open"))
        if timestamp is None or high is None or low is None or close is None or high < low:
            counters["excluded_invalid_count"] += 1
            continue
        if close < low or close > high or (open_price is not None and (open_price < low or open_price > high)):
            counters["excluded_invalid_count"] += 1
            continue
        explicit = _explicit_completion(raw)
        if explicit is False:
            counters["excluded_incomplete_count"] += 1
            continue
        if timestamp <= anchor:
            counters["excluded_pre_anchor_count"] += 1
            continue
        if cutoff is not None and timestamp > cutoff:
            counters["excluded_after_cutoff_count"] += 1
            continue
        if explicit is None:
            # Historical OHLC inputs without an explicit completion flag are
            # accepted as completed observations, but the assumption is
            # counted and exposed to the caller.
            counters["completion_assumed_count"] += 1
        eligible.append(
            {
                "timestamp_dt": timestamp,
                "timestamp": _iso(timestamp),
                "timestamp_field": timestamp_field,
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "sequence": sequence,
            }
        )
    eligible.sort(key=lambda item: (item["timestamp_dt"], item["sequence"]))

    # Duplicate provider rows for the same completed interval are merged into
    # one conservative OHLC envelope.  They never count as extra trading days.
    deduped = []
    for item in eligible:
        if deduped and item["timestamp_dt"] == deduped[-1]["timestamp_dt"]:
            existing = deduped[-1]
            existing["high"] = max(existing["high"], item["high"])
            existing["low"] = min(existing["low"], item["low"])
            existing["close"] = item["close"]
            if existing["open"] is None:
                existing["open"] = item["open"]
            counters["deduplicated_bar_count"] += 1
        else:
            deduped.append(dict(item))
    counters["eligible_completed_bar_count"] = len(deduped)
    return deduped, counters


def _level_hit(bar: Mapping[str, Any], level: Optional[float], direction: str, favourable: bool) -> bool:
    if level is None:
        return False
    if direction == "bullish":
        return bar["high"] >= level if favourable else bar["low"] <= level
    if direction == "bearish":
        return bar["low"] <= level if favourable else bar["high"] >= level
    return False


def _context_value(saved: Mapping[str, Any], key: str) -> Any:
    plan = saved.get("plan") if isinstance(saved.get("plan"), Mapping) else {}
    market = saved.get("market_context") if isinstance(saved.get("market_context"), Mapping) else {}
    if key == "setup_type":
        return _first(plan, ("setup_type", "setup")) or _first(saved, ("setup_type", "setup"))
    if key == "market_regime":
        return _first(market, ("regime", "market_regime")) or _first(saved, ("market_regime", "regime"))
    return saved.get(key)


def _confidence_band(value: Any) -> Optional[str]:
    number = _as_number(value)
    if number is None:
        return None
    if number < 50:
        return "under_50"
    if number < 65:
        return "50_to_64"
    if number < 75:
        return "65_to_74"
    if number < 85:
        return "75_to_84"
    return "85_plus"


def _timeframe_alignment(saved: Mapping[str, Any], direction: str) -> Optional[str]:
    direct = _first(saved, ("timeframe_alignment", "alignment_label"))
    if isinstance(direct, str) and direct.strip():
        return direct.strip().lower()
    number = _as_number(_first(saved, ("alignment", "alignment_score")))
    if number is not None:
        ratio = number / 100.0 if number > 1 else number
        if ratio >= 0.99:
            return "aligned"
        if ratio >= 0.67:
            return "mostly_aligned"
        if ratio >= 0.34:
            return "mixed"
        return "opposed"
    timeframes = saved.get("timeframes")
    if not isinstance(timeframes, Mapping) or direction not in {"bullish", "bearish"}:
        return None
    observed = []
    for value in timeframes.values():
        raw_direction = value.get("direction") if isinstance(value, Mapping) else value
        normalized = _direction(raw_direction)
        if normalized in {"bullish", "bearish"}:
            observed.append(normalized)
    if not observed:
        return None
    ratio = sum(1 for value in observed if value == direction) / float(len(observed))
    if ratio == 1:
        return "aligned"
    if ratio >= 0.67:
        return "mostly_aligned"
    if ratio >= 0.34:
        return "mixed"
    return "opposed"


def _signal_flags(
    *,
    saved: Mapping[str, Any],
    action: str,
    direction: str,
    entry_price: Optional[float],
    trigger: Optional[float],
    risk: Optional[float],
    status: str,
    mae_r: Optional[float],
) -> list:
    flags = []
    if action == "ENTER" and entry_price is not None and trigger is not None and risk:
        signed_distance = entry_price - trigger if direction == "bullish" else trigger - entry_price
        distance_r = signed_distance / risk
        if distance_r <= -0.10:
            flags.append(
                {
                    "code": "early_entry",
                    "explanation": "The recorded entry preceded the planned trigger by at least 0.10R.",
                    "metric": {"entry_vs_trigger_r": _rounded(distance_r)},
                }
            )
        if distance_r >= 0.25:
            flags.append(
                {
                    "code": "chasing",
                    "explanation": "The recorded entry was at least 0.25R beyond the planned trigger.",
                    "metric": {"entry_vs_trigger_r": _rounded(distance_r)},
                }
            )
    state = str(saved.get("state") or "").upper()
    warnings = " ".join(str(item) for item in saved.get("warnings", []) if item is not None).lower()
    if state == "EXTENDED" or "chase" in warnings or "extended" in warnings:
        if not any(item["code"] == "chasing" for item in flags):
            flags.append(
                {
                    "code": "chasing",
                    "explanation": "The saved analysis itself identified an extended or chase condition.",
                    "metric": {"saved_state": state or None},
                }
            )
    if status == "invalidation_first":
        flags.append(
            {
                "code": "false_signal",
                "explanation": "The setup triggered and reached invalidation before any target.",
                "metric": {"terminal_status": status},
            }
        )
    if action == "ENTER" and mae_r is not None and mae_r <= -0.75 and status != "target_first":
        flags.append(
            {
                "code": "early_adverse_excursion",
                "explanation": "The entered setup experienced at least 0.75R adverse excursion before proving a target-first outcome.",
                "metric": {"mae_r": _rounded(mae_r)},
            }
        )
    return flags


def evaluate_journal_outcome(
    saved_analysis: Mapping[str, Any],
    later_bars: Sequence[Mapping[str, Any]],
    *,
    evaluation_cutoff: Any = None,
) -> dict:
    """Evaluate a saved ENTER/WATCH/PASS record against later completed bars.

    ``saved_analysis`` must include ``saved_at`` (or an equivalent timestamp).
    Bars at or before that timestamp are always excluded.  ``evaluation_cutoff``
    is optional, but when supplied no bar completing after it can contribute.
    An explicit ``complete=False`` bar is never used.
    """

    if not isinstance(saved_analysis, Mapping):
        raise TypeError("saved_analysis must be a mapping")
    if not isinstance(later_bars, Sequence) or isinstance(later_bars, (str, bytes, bytearray)):
        raise TypeError("later_bars must be a sequence of OHLC mappings")
    anchor = _saved_timestamp(saved_analysis)
    cutoff = _parse_timestamp(evaluation_cutoff)
    if evaluation_cutoff is not None and cutoff is None:
        raise ValueError("evaluation_cutoff must be a valid timestamp")
    if cutoff is not None and cutoff < anchor:
        raise ValueError("evaluation_cutoff cannot precede the saved analysis")

    normalized, counters = _normalize_bars(later_bars, anchor=anchor, cutoff=cutoff)
    plan = saved_analysis.get("plan") if isinstance(saved_analysis.get("plan"), Mapping) else saved_analysis
    action = _tracking_action(saved_analysis)
    direction = _direction(_first(plan, ("direction",)) or _first(saved_analysis, ("direction",)))
    trigger = _first_number((plan, saved_analysis), ("trigger", "trigger_price", "breakout_level"))
    invalidation = _first_number(
        (plan, saved_analysis), ("invalidation", "invalidation_level", "stop", "stop_price")
    )
    targets = _targets(plan)
    explicit_entry = _first_number(
        (saved_analysis, plan), ("entry_price", "filled_price", "average_entry", "current_price")
    )
    entry_price = explicit_entry if action == "ENTER" else trigger
    if entry_price is None:
        entry_price = trigger
    risk = None
    risk_warning = None
    if entry_price is not None and invalidation is not None and direction in {"bullish", "bearish"}:
        geometrically_valid = (
            invalidation < entry_price if direction == "bullish" else invalidation > entry_price
        )
        if geometrically_valid:
            risk = abs(entry_price - invalidation)
            if risk == 0:
                risk = None
        else:
            risk_warning = "Invalidation is on the non-adverse side of the reference entry; R metrics are unavailable."

    events = []
    active_bars = []
    activation_time = anchor if action == "ENTER" and entry_price is not None else None
    trigger_bar_time = None
    if activation_time is not None:
        events.append(
            {
                "event": "entered",
                "timestamp": _iso(anchor),
                "price": _rounded(entry_price),
                "evidence": "recorded entry at saved-analysis cutoff",
            }
        )

    terminal_status = None
    first_terminal = None
    hit_targets = set()
    invalidation_recorded = False
    for bar in normalized:
        just_triggered = False
        if activation_time is None:
            if direction in {"bullish", "bearish"} and _level_hit(bar, trigger, direction, True):
                activation_time = bar["timestamp_dt"]
                trigger_bar_time = bar["timestamp_dt"]
                just_triggered = True
                events.append(
                    {
                        "event": "trigger_reached",
                        "timestamp": bar["timestamp"],
                        "price": _rounded(trigger),
                        "evidence": "completed OHLC range touched the trigger",
                    }
                )
            else:
                continue
        active_bars.append(bar)
        target_hits = [
            (label, level)
            for label, level in targets
            if label not in hit_targets and _level_hit(bar, level, direction, True)
        ]
        invalidation_hit = (
            not invalidation_recorded and _level_hit(bar, invalidation, direction, False)
        )

        # Trigger plus another level in one OHLC bar is also order-ambiguous:
        # the bar range cannot prove whether invalidation preceded activation.
        if (target_hits and invalidation_hit) or (just_triggered and (target_hits or invalidation_hit)):
            touched = [label for label, _ in target_hits]
            if invalidation_hit:
                touched.append("invalidation")
            events.append(
                {
                    "event": "ambiguous_intrabar_order",
                    "timestamp": bar["timestamp"],
                    "levels": touched,
                    "evidence": "one completed OHLC bar touched multiple decision levels; path order is unknowable",
                }
            )
            terminal_status = terminal_status or "ambiguous_same_bar"
            first_terminal = first_terminal or {
                "event": "ambiguous_intrabar_order",
                "timestamp": bar["timestamp"],
                "levels": touched,
            }
            break

        if invalidation_hit:
            invalidation_recorded = True
            events.append(
                {
                    "event": "invalidation_reached",
                    "timestamp": bar["timestamp"],
                    "price": _rounded(invalidation),
                    "evidence": "completed OHLC range touched invalidation",
                }
            )
            if terminal_status is None:
                terminal_status = "invalidation_first"
                first_terminal = {
                    "event": "invalidation_reached",
                    "timestamp": bar["timestamp"],
                    "price": _rounded(invalidation),
                }
            break

        if target_hits:
            for label, level in target_hits:
                hit_targets.add(label)
                events.append(
                    {
                        "event": "target_reached",
                        "target": label,
                        "timestamp": bar["timestamp"],
                        "price": _rounded(level),
                        "evidence": "completed OHLC range touched the target",
                    }
                )
            if terminal_status is None:
                terminal_status = "target_first"
                first_label, first_level = target_hits[0]
                first_terminal = {
                    "event": "target_reached",
                    "target": first_label,
                    "timestamp": bar["timestamp"],
                    "price": _rounded(first_level),
                }

    if not normalized:
        status = "no_completed_bars"
    elif activation_time is None:
        status = "not_triggered"
    elif terminal_status is not None:
        status = terminal_status
    else:
        status = "triggered_active"

    # Excursion and forward-return measurements are signal observations through
    # the evidence cutoff.  They are not presented as realized trade P&L.
    observation_bars = []
    if activation_time is not None:
        observation_bars = [bar for bar in normalized if bar["timestamp_dt"] >= activation_time]
    mfe = {"price": None, "amount": None, "percent": None, "r_multiple": None}
    mae = {"price": None, "amount": None, "percent": None, "r_multiple": None}
    mfe_r = None
    mae_r = None
    if observation_bars and entry_price is not None and entry_price != 0 and direction in {"bullish", "bearish"}:
        if direction == "bullish":
            favourable_price = max(bar["high"] for bar in observation_bars)
            adverse_price = min(bar["low"] for bar in observation_bars)
            favourable_amount = max(0.0, favourable_price - entry_price)
            adverse_amount = min(0.0, adverse_price - entry_price)
        else:
            favourable_price = min(bar["low"] for bar in observation_bars)
            adverse_price = max(bar["high"] for bar in observation_bars)
            favourable_amount = max(0.0, entry_price - favourable_price)
            adverse_amount = min(0.0, entry_price - adverse_price)
        mfe_r = favourable_amount / risk if risk else None
        mae_r = adverse_amount / risk if risk else None
        mfe = {
            "price": _rounded(favourable_price),
            "amount": _rounded(favourable_amount),
            "percent": _rounded(favourable_amount / entry_price * 100),
            "r_multiple": _rounded(mfe_r),
        }
        mae = {
            "price": _rounded(adverse_price),
            "amount": _rounded(adverse_amount),
            "percent": _rounded(adverse_amount / entry_price * 100),
            "r_multiple": _rounded(mae_r),
        }

    sessions = OrderedDict()
    for bar in observation_bars:
        session_date = bar["timestamp_dt"].date().isoformat()
        sessions[session_date] = {
            "date": session_date,
            "timestamp": bar["timestamp"],
            "close": bar["close"],
        }
    session_rows = list(sessions.values())
    forward_returns = {}
    for horizon in TRADING_DAY_HORIZONS:
        key = "{0}d".format(horizon)
        if entry_price is None or entry_price == 0 or len(session_rows) < horizon:
            forward_returns[key] = {
                "available": False,
                "trading_date": None,
                "close": None,
                "raw_return_pct": None,
                "directional_return_pct": None,
                "r_multiple": None,
            }
            continue
        row = session_rows[horizon - 1]
        raw_move = row["close"] - entry_price
        directional_move = raw_move if direction == "bullish" else -raw_move
        forward_returns[key] = {
            "available": True,
            "trading_date": row["date"],
            "close": _rounded(row["close"]),
            "raw_return_pct": _rounded(raw_move / entry_price * 100),
            "directional_return_pct": _rounded(directional_move / entry_price * 100),
            "r_multiple": _rounded(directional_move / risk) if risk else None,
        }

    alignment = _timeframe_alignment(saved_analysis, direction)
    context = {
        "setup_type": _context_value(saved_analysis, "setup_type"),
        "market_regime": _context_value(saved_analysis, "market_regime"),
        "grade": saved_analysis.get("grade"),
        "confidence": _as_number(saved_analysis.get("confidence")),
        "confidence_band": _confidence_band(saved_analysis.get("confidence")),
        "direction": direction,
        "timeframe_alignment": alignment,
        "direction_timeframe_alignment": (
            "{0}/{1}".format(direction, alignment) if alignment and direction != "unknown" else None
        ),
    }
    flags = _signal_flags(
        saved=saved_analysis,
        action=action,
        direction=direction,
        entry_price=entry_price,
        trigger=trigger,
        risk=risk,
        status=status,
        mae_r=mae_r,
    )
    evaluation_end = cutoff or (normalized[-1]["timestamp_dt"] if normalized else anchor)
    warnings = []
    if direction == "unknown":
        warnings.append("Direction is unavailable; direction-aware level and return evaluation is limited.")
    if risk_warning:
        warnings.append(risk_warning)
    if counters["completion_assumed_count"]:
        warnings.append("Some historical OHLC rows lacked an explicit completion flag; this assumption is disclosed in evidence_scope.")

    result = {
        "methodology_version": JOURNAL_METHODOLOGY_VERSION,
        "ticker": str(saved_analysis.get("ticker") or saved_analysis.get("symbol") or "").upper(),
        "plan_id": saved_analysis.get("plan_id") or saved_analysis.get("analysis_id"),
        "tracking_action": action,
        "outcome_status": status,
        "triggered": activation_time is not None,
        "direction": direction,
        "reference_entry_price": _rounded(entry_price),
        "initial_risk": _rounded(risk),
        "trigger": _rounded(trigger),
        "invalidation": _rounded(invalidation),
        "targets": [{"label": label, "price": _rounded(level)} for label, level in targets],
        "activation_time": _iso(activation_time) if activation_time is not None else None,
        "trigger_bar_time": _iso(trigger_bar_time) if trigger_bar_time is not None else None,
        "first_terminal_event": first_terminal,
        "events": events,
        "mfe": mfe,
        "mae": mae,
        "forward_returns": forward_returns,
        "analysis_context": context,
        "evidence_flags": flags,
        "warnings": warnings,
        "evidence_scope": {
            "saved_analysis_cutoff": _iso(anchor),
            "evaluation_cutoff": _iso(evaluation_end),
            "strictly_later_bars_only": True,
            "completed_bars_only": True,
            "returns_are_signal_observations_not_realized_pnl": True,
            "intrabar_order_policy": "ambiguous when one OHLC bar cannot prove event order",
            **counters,
        },
    }
    # Enforce the module's public serialization contract before returning.
    json.dumps(result, allow_nan=False)
    return result


def evaluate_saved_analysis(
    saved_analysis: Mapping[str, Any],
    later_bars: Sequence[Mapping[str, Any]],
    *,
    evaluation_cutoff: Any = None,
) -> dict:
    """Compatibility alias with a name suited to batch journal workflows."""

    return evaluate_journal_outcome(
        saved_analysis,
        later_bars,
        evaluation_cutoff=evaluation_cutoff,
    )


def _average(values: Iterable[Any]) -> Optional[float]:
    numbers = [_as_number(value) for value in values]
    usable = [value for value in numbers if value is not None]
    return _rounded(sum(usable) / len(usable)) if usable else None


def _group_metrics(rows: Sequence[Mapping[str, Any]]) -> dict:
    statuses = {
        "target_first": 0,
        "invalidation_first": 0,
        "ambiguous_same_bar": 0,
        "triggered_active": 0,
        "not_triggered": 0,
        "no_completed_bars": 0,
    }
    flag_counts = {}
    for row in rows:
        status = str(row.get("outcome_status") or "")
        statuses[status] = statuses.get(status, 0) + 1
        for flag in row.get("evidence_flags", []):
            if isinstance(flag, Mapping) and flag.get("code"):
                code = str(flag["code"])
                flag_counts[code] = flag_counts.get(code, 0) + 1
    triggered = sum(1 for row in rows if row.get("triggered"))
    resolved = statuses.get("target_first", 0) + statuses.get("invalidation_first", 0)
    returns = {}
    for horizon in TRADING_DAY_HORIZONS:
        key = "{0}d".format(horizon)
        returns[key] = {
            "average_directional_return_pct": _average(
                row.get("forward_returns", {}).get(key, {}).get("directional_return_pct")
                for row in rows
            ),
            "average_r_multiple": _average(
                row.get("forward_returns", {}).get(key, {}).get("r_multiple") for row in rows
            ),
            "available_count": sum(
                1 for row in rows if row.get("forward_returns", {}).get(key, {}).get("available")
            ),
        }
    return {
        "count": len(rows),
        "triggered_count": triggered,
        "trigger_rate": _rounded(triggered / len(rows)) if rows else None,
        "resolved_unambiguous_count": resolved,
        "target_first_rate": (
            _rounded(statuses.get("target_first", 0) / resolved) if resolved else None
        ),
        "invalidation_first_rate": (
            _rounded(statuses.get("invalidation_first", 0) / resolved) if resolved else None
        ),
        "status_counts": statuses,
        "average_mfe_r": _average(row.get("mfe", {}).get("r_multiple") for row in rows),
        "average_mae_r": _average(row.get("mae", {}).get("r_multiple") for row in rows),
        "forward_returns": returns,
        "evidence_flag_counts": dict(sorted(flag_counts.items())),
    }


def aggregate_calibration(outcomes: Sequence[Mapping[str, Any]]) -> dict:
    """Build read-only calibration evidence grouped by observable context.

    The summary reports evidence only.  ``thresholds_changed`` and
    ``automatic_changes_allowed`` are always false.
    """

    if not isinstance(outcomes, Sequence) or isinstance(outcomes, (str, bytes, bytearray)):
        raise TypeError("outcomes must be a sequence of journal outcome mappings")
    rows = [copy.deepcopy(row) for row in outcomes if isinstance(row, Mapping)]
    dimensions = (
        "setup_type",
        "market_regime",
        "grade",
        "confidence_band",
        "direction",
        "timeframe_alignment",
        "direction_timeframe_alignment",
    )
    groups = {}
    for dimension in dimensions:
        buckets = {}
        for row in rows:
            context = row.get("analysis_context") if isinstance(row.get("analysis_context"), Mapping) else {}
            value = context.get(dimension)
            if value is None or str(value).strip() == "":
                continue
            label = str(value)
            buckets.setdefault(label, []).append(row)
        groups[dimension] = [
            {"value": value, **_group_metrics(bucket_rows)}
            for value, bucket_rows in sorted(buckets.items(), key=lambda item: item[0].lower())
        ]
    result = {
        "methodology_version": CALIBRATION_METHODOLOGY_VERSION,
        "journal_methodology_versions": sorted(
            {str(row.get("methodology_version")) for row in rows if row.get("methodology_version")}
        ),
        "outcome_count": len(rows),
        "overall": _group_metrics(rows),
        "groups": groups,
        "evidence_notes": [
            "Target-first and invalidation-first rates exclude ambiguous same-bar outcomes.",
            "Forward returns measure signal behaviour from the reference entry; they are not realized P&L.",
            "Early-entry, chasing, and false-signal flags are evidence prompts, not threshold changes.",
        ],
        "thresholds_changed": False,
        "automatic_changes_allowed": False,
    }
    json.dumps(result, allow_nan=False)
    return result


def build_calibration_proposal(
    *,
    parameter: str,
    current_value: Any,
    proposed_value: Any,
    rationale: str,
    evidence: Sequence[Any],
    created_at: Any,
    proposal_id: Optional[str] = None,
) -> dict:
    """Return an explainable, reversible draft proposal without applying it.

    ``current_value`` and ``proposed_value`` may be any JSON-compatible value.
    The helper has no reference to runtime settings and therefore cannot mutate
    a threshold.  Callers must implement a separate explicit review/apply step.
    """

    name = str(parameter or "").strip()
    reason = str(rationale or "").strip()
    created = _parse_timestamp(created_at)
    if not name:
        raise ValueError("parameter is required")
    if not reason:
        raise ValueError("rationale is required")
    if created is None:
        raise ValueError("created_at must be a valid timestamp")
    before = copy.deepcopy(current_value)
    after = copy.deepcopy(proposed_value)
    evidence_copy = copy.deepcopy(list(evidence))
    canonical = json.dumps(
        {
            "parameter": name,
            "before": before,
            "after": after,
            "rationale": reason,
            "evidence": evidence_copy,
            "created_at": _iso(created),
        },
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    identifier = str(proposal_id or "").strip() or "proposal-" + hashlib.sha256(
        canonical.encode("utf-8")
    ).hexdigest()[:12]
    record = {
        "record_version": PROPOSAL_RECORD_VERSION,
        "calibration_methodology_version": CALIBRATION_METHODOLOGY_VERSION,
        "proposal_id": identifier,
        "created_at": _iso(created),
        "status": "draft",
        "applied": False,
        "automatic_application": False,
        "requires_explicit_review": True,
        "reversible": True,
        "change": {"parameter": name, "before": before, "after": after},
        "rationale": reason,
        "evidence": evidence_copy,
        "rollback": {
            "parameter": name,
            "restore_value": copy.deepcopy(before),
            "instruction": "Restore the recorded before value through a separate explicit review action.",
        },
    }
    json.dumps(record, allow_nan=False)
    return record


def make_calibration_proposal(**kwargs: Any) -> dict:
    """Alias for callers that prefer ``make`` terminology."""

    return build_calibration_proposal(**kwargs)
