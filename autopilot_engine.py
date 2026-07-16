"""Evidence-first multi-timeframe decision engine for Trading Autopilot.

The engine is deliberately independent from Streamlit and from any market-data
vendor.  It consumes normalized OHLCV bars and returns an explainable trade
plan.  It never places orders and it never upgrades incomplete data to ENTER.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import date as datetime_date, datetime, time as datetime_time, timedelta, timezone
from functools import lru_cache
import math
import re
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from timeframes import normalize_timeframe


TIMEFRAME_WEIGHTS = {
    "1M": 4,
    "1W": 20,
    "1D": 26,
    "4H": 22,
    "1H": 8,
    "15M": 18,
    "5M": 2,
}
REQUIRED_TIMEFRAMES = ("1W", "1D", "4H", "15M")
MIN_REQUIRED_BARS = 60
MARKET_TIMEZONE = ZoneInfo("America/New_York")
MAX_REALTIME_AGE_SECONDS = 2 * 60
MAX_DELAYED_AGE_SECONDS = 30 * 60
FRAME_MAX_AGE_SECONDS = {
    "15M": MAX_DELAYED_AGE_SECONDS,
    # The most recent completed 4H/Daily bar can legitimately be from the
    # prior regular session, including a three-day weekend.
    "4H": 5 * 24 * 60 * 60,
    "1D": 5 * 24 * 60 * 60,
    # A current weekly structure bar is normally the previous Friday close.
    "1W": 14 * 24 * 60 * 60,
}
STATE_TO_VERDICT = {
    "ENTER": "ENTER",
    "FORMING": "WAIT FOR CONFIRMATION",
    "ARMED": "WAIT FOR CONFIRMATION",
    "BLOCKED": "PASS",
    "EXTENDED": "PASS",
    "INVALIDATED": "PASS",
}


@dataclass(frozen=True)
class FreshnessAssessment:
    """Pure decision-currentness result shared by service, UI, and tests."""

    valid_for_entry: bool
    effective_label: str
    timestamp: Optional[str]
    age_seconds: Optional[float]
    reasons: tuple[str, ...] = ()


def _utc_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _regular_session_open(now: datetime) -> bool:
    local = now.astimezone(MARKET_TIMEZONE)
    wall_clock = local.time().replace(tzinfo=None)
    return (
        _is_exchange_session_date(local.date())
        and datetime_time(9, 30) <= wall_clock < exchange_session_close_time(local.date())
    )


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> datetime_date:
    first = datetime_date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> datetime_date:
    if month == 12:
        cursor = datetime_date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = datetime_date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> datetime_date:
    """Return Gregorian Easter using the Meeus/Jones/Butcher algorithm."""

    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return datetime_date(year, month, day)


def _observed_fixed_holiday(
    value: datetime_date,
    *,
    observe_saturday: bool = True,
) -> datetime_date:
    if value.weekday() == 5 and observe_saturday:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


@lru_cache(maxsize=32)
def _exchange_holidays(year: int) -> frozenset[datetime_date]:
    """Return regular full-day NYSE-style closures for a calendar year.

    The calendar intentionally covers recurring full-day closures only. An
    unexpected exceptional closure therefore remains fail-closed because no
    bar will satisfy the expected-session check.
    """

    holidays = {
        # NYSE does not normally move a Saturday New Year's closure into the
        # prior calendar year (for example, Dec. 31, 2021 remained open).
        _observed_fixed_holiday(datetime_date(year, 1, 1), observe_saturday=False),
        _nth_weekday(year, 1, 0, 3),  # Martin Luther King Jr. Day
        _nth_weekday(year, 2, 0, 3),  # Washington's Birthday
        _easter_sunday(year) - timedelta(days=2),  # Good Friday
        _last_weekday(year, 5, 0),  # Memorial Day
        _observed_fixed_holiday(datetime_date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),  # Labor Day
        _nth_weekday(year, 11, 3, 4),  # Thanksgiving
        _observed_fixed_holiday(datetime_date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed_fixed_holiday(datetime_date(year, 6, 19)))
    return frozenset(holidays)


def _is_exchange_session_date(value: datetime_date) -> bool:
    return value.weekday() < 5 and value not in _exchange_holidays(value.year)


def _previous_exchange_session(value: datetime_date) -> datetime_date:
    candidate = value - timedelta(days=1)
    for _ in range(14):
        if _is_exchange_session_date(candidate):
            return candidate
        candidate -= timedelta(days=1)
    # Fourteen consecutive calendar days without a regular session is not a
    # normal U.S. equity schedule. Preserve the conservative prior date so the
    # caller's exact-session comparison fails closed.
    return candidate


def _session_week_start(value: datetime_date) -> datetime_date:
    return value - timedelta(days=value.weekday())


def _is_early_close_session(value: datetime_date) -> bool:
    thanksgiving = _nth_weekday(value.year, 11, 3, 4)
    independence_closure = _observed_fixed_holiday(datetime_date(value.year, 7, 4))
    early_before_independence = (
        _previous_exchange_session(independence_closure)
        if independence_closure.weekday() != 0
        else None
    )
    return (
        value == thanksgiving + timedelta(days=1)
        or (value.month == 12 and value.day == 24)
        or value == early_before_independence
    ) and _is_exchange_session_date(value)


def exchange_session_close_time(value: datetime_date) -> datetime_time:
    """Return the recurring regular cash-session close."""

    return datetime_time(13, 0) if _is_early_close_session(value) else datetime_time(16, 0)


def _latest_4h_bucket_completion_time(value: datetime_date) -> datetime_time:
    # A 09:30-anchored 4H aggregate completes at 13:30 even when the cash
    # session itself closes at 13:00.
    return datetime_time(13, 30) if _is_early_close_session(value) else datetime_time(16, 0)


def assess_source_freshness(
    *,
    data_label: Any,
    data_timestamp: Any,
    market_status: Any,
    now: Optional[datetime] = None,
) -> FreshnessAssessment:
    """Validate one source timestamp without trusting a precomputed label.

    This helper never upgrades a source already labelled stale or unavailable.
    A source must be inside the regular session, have a non-future timestamp,
    and agree with its real-time/delayed label before it is entry-eligible.
    """

    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at.astimezone(timezone.utc)
    original_label = str(data_label or "unavailable").strip().lower().replace("_", "-")
    supported_labels = {"real-time", "realtime", "delayed", "last-close", "stale", "unavailable"}
    if original_label == "realtime":
        original_label = "real-time"
    timestamp = _utc_datetime(data_timestamp)
    reasons: list[str] = []
    effective_label = original_label if original_label in supported_labels else "unavailable"

    if original_label not in supported_labels:
        reasons.append("Market-data currentness label is unsupported.")
    if original_label in {"stale", "unavailable"}:
        reasons.append(f"Market data is {original_label}; incomplete data cannot produce ENTER.")
    if timestamp is None:
        reasons.append("Market-data source timestamp is missing or invalid.")
        if original_label not in {"stale", "unavailable"}:
            effective_label = "unavailable"

    age_seconds: Optional[float] = None
    if timestamp is not None:
        age_seconds = (observed_at - timestamp).total_seconds()
        if age_seconds < 0:
            reasons.append("Market-data source timestamp is in the future.")
            if original_label not in {"stale", "unavailable"}:
                effective_label = "stale"

    normalized_market = str(market_status or "unknown").strip().lower()
    regular_session_open = normalized_market == "open" and _regular_session_open(observed_at)
    if not regular_session_open:
        reasons.append("The regular market is not open; current evidence cannot produce ENTER.")
        if effective_label in {"real-time", "delayed"}:
            effective_label = "last-close"
    if original_label == "last-close":
        reasons.append("Last-close evidence is planning-only and cannot produce ENTER.")
    if age_seconds is not None and age_seconds >= 0:
        if original_label == "real-time" and age_seconds > MAX_REALTIME_AGE_SECONDS:
            reasons.append("Real-time label contradicts the source timestamp age.")
            effective_label = "stale"
        elif original_label == "delayed" and age_seconds > MAX_DELAYED_AGE_SECONDS:
            reasons.append("Delayed source timestamp is stale.")
            effective_label = "stale"

    clean_timestamp = (
        timestamp.isoformat().replace("+00:00", "Z") if timestamp is not None else None
    )
    return FreshnessAssessment(
        valid_for_entry=not reasons,
        effective_label=effective_label,
        timestamp=clean_timestamp,
        age_seconds=age_seconds,
        reasons=tuple(dict.fromkeys(reasons)),
    )


def bar_completion_timestamp(timestamp: datetime, timeframe: str) -> datetime:
    """Return the expected completion instant for one provider bar start."""

    value = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    label = normalize_timeframe(timeframe)
    durations = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240}
    if label in durations:
        completion = value + timedelta(minutes=durations[label])
        local_start = value.astimezone(MARKET_TIMEZONE)
        regular_close = datetime.combine(
            local_start.date(), datetime_time(16, 0), tzinfo=MARKET_TIMEZONE
        ).astimezone(timezone.utc)
        if datetime_time(9, 30) <= local_start.time().replace(tzinfo=None) < datetime_time(16, 0):
            completion = min(completion, regular_close)
        return completion
    local = value.astimezone(MARKET_TIMEZONE)
    if label == "1D":
        return datetime.combine(
            local.date(), datetime_time(16, 0), tzinfo=MARKET_TIMEZONE
        ).astimezone(timezone.utc)
    if label == "1W":
        monday = local.date() - timedelta(days=local.weekday())
        return datetime.combine(
            monday + timedelta(days=4), datetime_time(16, 0), tzinfo=MARKET_TIMEZONE
        ).astimezone(timezone.utc)
    if label == "1M":
        if local.month == 12:
            next_month = datetime(local.year + 1, 1, 1, tzinfo=MARKET_TIMEZONE)
        else:
            next_month = datetime(local.year, local.month + 1, 1, tzinfo=MARKET_TIMEZONE)
        return next_month.astimezone(timezone.utc)
    return value


def required_frame_freshness_issues(
    bars_by_timeframe: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    now: datetime,
    market_status: Any,
    required_timeframes: Iterable[str] = REQUIRED_TIMEFRAMES,
    context: str = "Ticker",
) -> list[str]:
    """Return cadence-aware freshness blockers for required completed frames."""

    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    normalized_market = str(market_status or "unknown").strip().lower()
    issues: list[str] = []
    safe_context = re.sub(r"[^A-Za-z0-9 ._\-/]", "", str(context or "Ticker"))[:80] or "Ticker"
    for raw_label in required_timeframes:
        label = str(raw_label or "").strip().upper()
        rows = normalize_bars(bars_by_timeframe.get(label, []))
        if not rows:
            issues.append(f"{safe_context} {label} source timestamp is missing.")
            continue

        # Providers commonly include the candle that is still forming.  Entry
        # freshness must be based on the newest *completed* candle, not the
        # newest row, or current daily/weekly/intraday bars would make healthy
        # live evidence look future-dated throughout their formation period.
        completed_at: Optional[datetime] = None
        completed_started_at: Optional[datetime] = None
        saw_future_or_in_progress = False
        for row in reversed(rows):
            timestamp = row.get("timestamp")
            if not isinstance(timestamp, (int, float)) or timestamp <= 1_000_000_000:
                continue
            try:
                started_at = datetime.fromtimestamp(timestamp, timezone.utc)
                candidate_completion = bar_completion_timestamp(started_at, label)
            except (OSError, OverflowError, ValueError):
                continue
            if candidate_completion <= current:
                completed_at = candidate_completion
                completed_started_at = started_at
                break
            saw_future_or_in_progress = True

        if completed_at is None:
            if saw_future_or_in_progress:
                issues.append(f"{safe_context} {label} source timestamp is in the future.")
            else:
                issues.append(f"{safe_context} {label} source timestamp is missing.")
            continue
        age_seconds = (current - completed_at).total_seconds()
        maximum_age = FRAME_MAX_AGE_SECONDS.get(label)
        if maximum_age is not None and age_seconds > maximum_age:
            issues.append(f"{safe_context} {label} evidence is stale for its cadence.")
            continue
        if normalized_market == "open" and _regular_session_open(current):
            local_now = current.astimezone(MARKET_TIMEZONE)
            local_completion = completed_at.astimezone(MARKET_TIMEZONE)
            previous_session = _previous_exchange_session(local_now.date())
            if label == "15M":
                if (
                    local_now.time().replace(tzinfo=None) < datetime_time(9, 45)
                    or local_completion.date() != local_now.date()
                    or local_completion.time().replace(tzinfo=None) < datetime_time(9, 45)
                ):
                    issues.append(
                        f"{safe_context} 15M has no current-session completed bar after 09:45 ET."
                    )
            elif label == "4H":
                after_first_bucket = (
                    local_now.time().replace(tzinfo=None) >= datetime_time(13, 30)
                )
                expected_session = local_now.date() if after_first_bucket else previous_session
                minimum_completion = (
                    datetime_time(13, 30)
                    if after_first_bucket
                    else _latest_4h_bucket_completion_time(previous_session)
                )
                if (
                    local_completion.date() != expected_session
                    or local_completion.time().replace(tzinfo=None) < minimum_completion
                ):
                    issues.append(
                        f"{safe_context} 4H evidence is not from the latest completed exchange-session bucket."
                    )
            elif label == "1D" and local_completion.date() != previous_session:
                issues.append(
                    f"{safe_context} 1D evidence is not from the latest completed exchange session."
                )
            elif label == "1W" and completed_started_at is not None:
                prior_week_session = _previous_exchange_session(
                    _session_week_start(local_now.date())
                )
                expected_week = _session_week_start(prior_week_session)
                observed_week = _session_week_start(
                    completed_started_at.astimezone(MARKET_TIMEZONE).date()
                )
                if observed_week != expected_week:
                    issues.append(
                        f"{safe_context} 1W evidence is not from the latest completed exchange week."
                    )
    return list(dict.fromkeys(issues))


def revalidate_decision_freshness(
    decision: Mapping[str, Any] | None,
    *,
    now: Optional[datetime] = None,
    market_status: Optional[str] = None,
) -> dict[str, Any]:
    """Return a copy gated against current time; never upgrade a stale decision."""

    source = deepcopy(dict(decision or {}))
    observed_at = now or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    observed_at = observed_at.astimezone(timezone.utc)
    assessment = assess_source_freshness(
        data_label=source.get("data_label"),
        data_timestamp=source.get("data_timestamp"),
        market_status=market_status if market_status is not None else source.get("market_status"),
        now=now,
    )
    if assessment.valid_for_entry:
        return source

    source["data_label"] = assessment.effective_label
    if (
        str(source.get("market_status") or "").strip().lower() == "open"
        and not _regular_session_open(observed_at)
    ):
        source["market_status"] = "closed"
    source["verdict"] = "PASS"
    source["state"] = "BLOCKED"
    source["entry_conditions_satisfied"] = False
    source["do_this_now"] = (
        "Pass for now—current market evidence failed the freshness/session gate, so no entry decision was made."
    )
    source["primary_risk"] = assessment.reasons[0] if assessment.reasons else (
        "Current market evidence is not eligible for an entry decision."
    )
    blockers = [str(item) for item in list(source.get("blockers") or []) if str(item).strip()]
    source["blockers"] = list(dict.fromkeys([*assessment.reasons, *blockers]))
    reasons = [str(item) for item in list(source.get("reasons") or []) if str(item).strip()]
    source["reasons"] = list(dict.fromkeys([*assessment.reasons, *reasons]))[:3]
    options = source.get("options") if isinstance(source.get("options"), Mapping) else None
    if options is not None:
        gated_options = deepcopy(dict(options))
        gated_options.update(
            {
                "status": "PASS",
                "recommendation": "No contract recommendation",
                "reason": "Options are gated because the underlying evidence is not current and entry-eligible.",
                "contracts": [],
                "ranked_contracts": [],
            }
        )
        source["options"] = gated_options
    return source


def _float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return round(value, digits) if value is not None and math.isfinite(value) else None


def _clean_ticker(value: Any) -> str:
    ticker = re.sub(r"[^A-Z0-9.\-]", "", str(value or "").strip().upper())[:15]
    return ticker or "UNKNOWN"


def _timestamp_seconds(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    number = _float(value)
    if number is not None:
        if number > 10**17:
            return number / 1_000_000_000
        if number > 10**14:
            return number / 1_000_000
        if number > 10**11:
            return number / 1_000
        return number
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def normalize_bars(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, float]]:
    """Normalize and time-sort OHLCV rows, dropping malformed candles."""

    bars: list[dict[str, float]] = []
    for index, row in enumerate(rows):
        open_ = _float(row.get("open", row.get("o")))
        high = _float(row.get("high", row.get("h")))
        low = _float(row.get("low", row.get("l")))
        close = _float(row.get("close", row.get("c")))
        if None in (open_, high, low, close):
            continue
        if high < max(open_, close) or low > min(open_, close) or high < low:
            continue
        timestamp = _timestamp_seconds(row.get("timestamp", row.get("t")))
        volume = _float(row.get("volume", row.get("v")))
        bars.append(
            {
                "timestamp": timestamp if timestamp is not None else float(index),
                "open": float(open_),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": max(float(volume or 0), 0.0),
            }
        )
    return sorted(bars, key=lambda item: item["timestamp"])


def ema_series(values: list[float], period: int) -> list[Optional[float]]:
    if period <= 0 or not values:
        return []
    result: list[Optional[float]] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2 / (period + 1)
    for index in range(period, len(values)):
        current += (values[index] - current) * multiplier
        result[index] = current
    return result


def wma_series(values: list[float], period: int) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    denominator = period * (period + 1) / 2
    for index in range(period - 1, len(values)):
        window = values[index - period + 1 : index + 1]
        result[index] = sum(value * weight for weight, value in enumerate(window, 1)) / denominator
    return result


def sma_series(values: list[float], period: int) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    running = sum(values[:period])
    result[period - 1] = running / period
    for index in range(period, len(values)):
        running += values[index] - values[index - period]
        result[index] = running / period
    return result


def atr_series(bars: list[dict[str, float]], period: int = 14) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(bars)
    if len(bars) < period + 1:
        return result
    ranges: list[float] = []
    for index, bar in enumerate(bars):
        previous = bars[index - 1]["close"] if index else bar["close"]
        ranges.append(max(bar["high"] - bar["low"], abs(bar["high"] - previous), abs(bar["low"] - previous)))
    current = sum(ranges[1 : period + 1]) / period
    result[period] = current
    for index in range(period + 1, len(ranges)):
        current = ((current * (period - 1)) + ranges[index]) / period
        result[index] = current
    return result


def macd_series(values: list[float]) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    ema12 = ema_series(values, 12)
    ema26 = ema_series(values, 26)
    macd: list[Optional[float]] = [
        short - long if short is not None and long is not None else None
        for short, long in zip(ema12, ema26)
    ]
    compact = [value for value in macd if value is not None]
    compact_signal = ema_series(compact, 9)
    signal: list[Optional[float]] = [None] * len(values)
    signal_values = iter(compact_signal)
    for index, value in enumerate(macd):
        if value is not None:
            signal[index] = next(signal_values)
    histogram = [
        value - signal_value if value is not None and signal_value is not None else None
        for value, signal_value in zip(macd, signal)
    ]
    return macd, signal, histogram


def _last(values: list[Optional[float]]) -> Optional[float]:
    return next((value for value in reversed(values) if value is not None), None)


def _slope(values: list[Optional[float]], lookback: int = 5) -> Optional[float]:
    clean = [value for value in values if value is not None]
    if len(clean) <= lookback:
        return None
    return clean[-1] - clean[-1 - lookback]


def _pivot_levels(bars: list[dict[str, float]], current: float) -> tuple[list[float], list[float]]:
    supports: list[float] = []
    resistances: list[float] = []
    radius = 2
    for index in range(radius, len(bars) - radius):
        low = bars[index]["low"]
        high = bars[index]["high"]
        nearby = bars[index - radius : index + radius + 1]
        if low == min(item["low"] for item in nearby):
            supports.append(low)
        if high == max(item["high"] for item in nearby):
            resistances.append(high)
    supports.extend(item["low"] for item in bars[-20:])
    resistances.extend(item["high"] for item in bars[-20:])
    below = sorted({round(value, 6) for value in supports if value < current}, reverse=True)
    above = sorted({round(value, 6) for value in resistances if value > current})
    return below[:4], above[:4]


@dataclass
class TimeframeAnalysis:
    timeframe: str
    timestamp: Optional[str]
    bar_count: int
    close: Optional[float]
    direction: str
    trend_score: int
    ema9: Optional[float]
    wma21: Optional[float]
    wma50: Optional[float]
    wma200: Optional[float]
    sma200: Optional[float]
    vwap: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    macd_histogram: Optional[float]
    relative_volume: Optional[float]
    atr: Optional[float]
    support: list[float] = field(default_factory=list)
    resistance: list[float] = field(default_factory=list)
    prior_high: Optional[float] = None
    prior_low: Optional[float] = None
    extension_atr: Optional[float] = None
    evidence: list[str] = field(default_factory=list)


def analyze_timeframe(timeframe: str, rows: Iterable[Mapping[str, Any]]) -> TimeframeAnalysis:
    bars = normalize_bars(rows)
    label = str(timeframe or "").strip().upper()
    if len(bars) < MIN_REQUIRED_BARS:
        return TimeframeAnalysis(
            timeframe=label,
            timestamp=None,
            bar_count=len(bars),
            close=bars[-1]["close"] if bars else None,
            direction="unavailable",
            trend_score=0,
            ema9=None,
            wma21=None,
            wma50=None,
            wma200=None,
            sma200=None,
            vwap=None,
            macd=None,
            macd_signal=None,
            macd_histogram=None,
            relative_volume=None,
            atr=None,
            evidence=[
                f"Only {len(bars)} valid bars; at least {MIN_REQUIRED_BARS} are required for complete indicators."
            ],
        )

    closes = [bar["close"] for bar in bars]
    ema9_values = ema_series(closes, 9)
    wma21_values = wma_series(closes, 21)
    wma50_values = wma_series(closes, 50)
    wma200_values = wma_series(closes, 200)
    sma200_values = sma_series(closes, 200)
    macd_values, signal_values, histogram_values = macd_series(closes)
    atr_values = atr_series(bars)
    close = closes[-1]
    ema9 = _last(ema9_values)
    wma21 = _last(wma21_values)
    wma50 = _last(wma50_values)
    wma200 = _last(wma200_values)
    sma200 = _last(sma200_values)
    atr = _last(atr_values)

    score = 0
    comparisons = [(close, ema9), (ema9, wma21), (wma21, wma50), (wma50, wma200 or sma200)]
    for left, right in comparisons:
        if left is None or right is None:
            continue
        score += 1 if left > right else -1 if left < right else 0
    slope = _slope(wma21_values)
    if slope is not None:
        score += 1 if slope > 0 else -1 if slope < 0 else 0
    direction = "bullish" if score >= 2 else "bearish" if score <= -2 else "mixed"

    recent_volumes = [bar["volume"] for bar in bars[-21:-1] if bar["volume"] > 0]
    average_volume = sum(recent_volumes) / len(recent_volumes) if recent_volumes else 0
    relative_volume = bars[-1]["volume"] / average_volume if average_volume else None
    session_bars = bars[-78:] if label in {"5M", "15M", "1H", "4H"} else bars[-20:]
    volume_sum = sum(bar["volume"] for bar in session_bars)
    vwap = (
        sum(((bar["high"] + bar["low"] + bar["close"]) / 3) * bar["volume"] for bar in session_bars) / volume_sum
        if volume_sum
        else None
    )
    # The final two completed candles are reserved for break-then-hold
    # confirmation.  Structural trigger levels must predate both candles.
    structural_bars = bars[:-2]
    supports, resistances = _pivot_levels(structural_bars, close)
    prior = structural_bars[-20:]
    prior_high = max((bar["high"] for bar in prior), default=None)
    prior_low = min((bar["low"] for bar in prior), default=None)
    extension = abs(close - ema9) / atr if atr and ema9 is not None else None
    timestamp_value = bars[-1]["timestamp"]
    timestamp = None
    if timestamp_value > 1_000_000_000:
        timestamp = datetime.fromtimestamp(timestamp_value, timezone.utc).isoformat()

    evidence = [
        f"Price is {'above' if ema9 is not None and close > ema9 else 'below'} the 9 EMA.",
        f"The 21 WMA is {'rising' if slope is not None and slope > 0 else 'falling' if slope is not None and slope < 0 else 'flat or unavailable'}.",
    ]
    histogram = _last(histogram_values)
    if histogram is not None:
        evidence.append(f"MACD histogram is {'positive' if histogram > 0 else 'negative' if histogram < 0 else 'flat'}.")
    return TimeframeAnalysis(
        timeframe=label,
        timestamp=timestamp,
        bar_count=len(bars),
        close=_round(close, 4),
        direction=direction,
        trend_score=score,
        ema9=_round(ema9, 4),
        wma21=_round(wma21, 4),
        wma50=_round(wma50, 4),
        wma200=_round(wma200, 4),
        sma200=_round(sma200, 4),
        vwap=_round(vwap, 4),
        macd=_round(_last(macd_values), 5),
        macd_signal=_round(_last(signal_values), 5),
        macd_histogram=_round(histogram, 5),
        relative_volume=_round(relative_volume, 2),
        atr=_round(atr, 4),
        support=[_round(value, 4) for value in supports if _round(value, 4) is not None],
        resistance=[_round(value, 4) for value in resistances if _round(value, 4) is not None],
        prior_high=_round(prior_high, 4),
        prior_low=_round(prior_low, 4),
        extension_atr=_round(extension, 2),
        evidence=evidence,
    )


@dataclass
class MarketContext:
    regime: str = "mixed"
    spy_direction: str = "unavailable"
    qqq_direction: str = "unavailable"
    sector_symbol: Optional[str] = None
    sector_direction: str = "unavailable"
    relative_strength: Optional[float] = None
    volatility: str = "unavailable"
    breadth: str = "unavailable"
    scheduled_events: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    timestamp: Optional[str] = None


def build_market_context(
    spy: Mapping[str, TimeframeAnalysis],
    qqq: Mapping[str, TimeframeAnalysis],
    sector: Optional[Mapping[str, TimeframeAnalysis]] = None,
    *,
    sector_symbol: Optional[str] = None,
    relative_strength: Optional[float] = None,
    volatility: str = "unavailable",
    breadth: str = "unavailable",
    scheduled_events: Optional[list[str]] = None,
) -> MarketContext:
    def combined(values: Mapping[str, TimeframeAnalysis]) -> str:
        required = [values.get(key) for key in ("1D", "4H")]
        if any(item is None or item.direction == "unavailable" for item in required):
            return "unavailable"
        directions = [item.direction for item in required if item is not None]
        if all(value == "bullish" for value in directions):
            return "bullish"
        if all(value == "bearish" for value in directions):
            return "bearish"
        return "mixed"

    spy_direction = combined(spy)
    qqq_direction = combined(qqq)
    sector_direction = combined(sector or {})
    if spy_direction == qqq_direction == "bullish":
        regime = "risk-on"
    elif spy_direction == qqq_direction == "bearish":
        regime = "risk-off"
    else:
        regime = "mixed"
    evidence = [f"SPY is {spy_direction} across Daily/4H.", f"QQQ is {qqq_direction} across Daily/4H."]
    if sector_symbol:
        evidence.append(f"{sector_symbol} is {sector_direction} across Daily/4H.")
    return MarketContext(
        regime=regime,
        spy_direction=spy_direction,
        qqq_direction=qqq_direction,
        sector_symbol=sector_symbol,
        sector_direction=sector_direction,
        relative_strength=_round(relative_strength, 2),
        volatility=volatility,
        breadth=breadth,
        scheduled_events=list(scheduled_events or []),
        evidence=evidence,
        timestamp=next(
            (item.timestamp for mapping in (spy, qqq) for item in mapping.values() if item.timestamp),
            None,
        ),
    )


@dataclass
class TradePlan:
    direction: str
    setup_type: str
    trigger: Optional[float]
    entry_low: Optional[float]
    entry_high: Optional[float]
    invalidation: Optional[float]
    target_1: Optional[float]
    target_2: Optional[float]
    stretch_target: Optional[float]
    reward_to_risk: Optional[float]
    horizon: str
    confirmation: str
    target_basis: dict[str, str] = field(default_factory=dict)


@dataclass
class DecisionResult:
    ticker: str
    name: str
    exchange: str
    verdict: str
    state: str
    direction: str
    confidence: int
    confidence_explanation: str
    grade: str
    current_price: Optional[float]
    market_status: str
    data_timestamp: Optional[str]
    data_label: str
    data_source: str
    entry_conditions_satisfied: bool
    plan: TradePlan
    reasons: list[str]
    primary_risk: str
    upgrade_condition: str
    invalidation_condition: str
    do_this_now: str
    blockers: list[str]
    warnings: list[str]
    timeframes: dict[str, TimeframeAnalysis]
    market_context: MarketContext
    full_breakdown: dict[str, str]
    news: list[dict[str, Any]] = field(default_factory=list)
    earnings_date: Optional[str] = None
    days_to_earnings: Optional[int] = None
    earnings_status: str = "unresolved"
    earnings_date_status: Optional[str] = None
    earnings_checked_through: Optional[str] = None
    earnings_error_kind: Optional[str] = None
    earnings_status_code: Optional[int] = None
    earnings_attempts: Optional[int] = None
    earnings_latency_ms: Optional[float] = None
    earnings_throttled: bool = False
    options: dict[str, Any] = field(default_factory=dict)
    engine_version: str = "2.1.0"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _direction_alignment(analyses: Mapping[str, TimeframeAnalysis]) -> tuple[str, float, int, int]:
    bullish = 0
    bearish = 0
    available = 0
    for timeframe, weight in TIMEFRAME_WEIGHTS.items():
        analysis = analyses.get(timeframe)
        if not analysis or analysis.direction == "unavailable":
            continue
        available += weight
        if analysis.direction == "bullish":
            bullish += weight
        elif analysis.direction == "bearish":
            bearish += weight
    if bullish >= 55 and bullish >= bearish + 18:
        return "bullish", bullish / max(available, 1), bullish, bearish
    if bearish >= 55 and bearish >= bullish + 18:
        return "bearish", bearish / max(available, 1), bullish, bearish
    return "neutral", max(bullish, bearish) / max(available, 1), bullish, bearish


def _price_instruction(
    state: str,
    direction: str,
    plan: TradePlan,
    *,
    earnings_pending: bool = False,
) -> str:
    trigger = f"${plan.trigger:,.2f}" if plan.trigger is not None else "the defined trigger"
    invalidation = f"${plan.invalidation:,.2f}" if plan.invalidation is not None else "the defined invalidation"
    if state == "ENTER":
        return f"Entry conditions are satisfied; keep risk defined at {invalidation} and do not chase outside the entry zone."
    if state == "ARMED":
        if earnings_pending:
            return "Wait—the technical confirmation is satisfied, but the next earnings date must be verified before entry."
        relation = "above" if direction == "bullish" else "below"
        return f"Wait—enter only after a 15m close {relation} {trigger} and a successful hold or retest."
    if state == "FORMING":
        return f"Wait—the structure is forming but price has not reached a valid confirmation area near {trigger}."
    if state == "EXTENDED":
        return "Pass for now—the original move is extended and the remaining reward does not justify chasing."
    if state == "INVALIDATED":
        return "Pass—the original thesis is invalidated and should not be re-entered without a new setup."
    return "Pass—the evidence is incomplete or materially conflicted; preserve capital and wait for a cleaner setup."


def _format_level(value: Optional[float]) -> str:
    return f"${value:,.2f}" if value is not None else "unavailable"


def _build_breakdown(
    analyses: Mapping[str, TimeframeAnalysis],
    market: MarketContext,
    plan: TradePlan,
    direction: str,
    state: str,
    blockers: list[str],
    warnings: list[str],
    news: list[dict[str, Any]],
    earnings_date: Optional[str],
    earnings_status: str,
    earnings_pending: bool,
) -> dict[str, str]:
    def tf(*labels: str) -> str:
        parts = []
        for label in labels:
            item = analyses.get(label)
            if item:
                parts.append(f"{label}: {item.direction}; close {_format_level(item.close)}; 21 WMA {_format_level(item.wma21)}.")
        return " ".join(parts) or "Unavailable from the current provider response."

    daily = analyses.get("1D")
    tactical = analyses.get("15M")
    momentum = [item.macd_histogram for item in analyses.values() if item.macd_histogram is not None]
    relative_volume = tactical.relative_volume if tactical else None
    supply = daily.resistance[:2] if daily else []
    demand = daily.support[:2] if daily else []
    news_text = "; ".join(str(item.get("title", "")) for item in news[:3] if item.get("title")) or "No provider headlines returned."
    catalyst = (
        f"Next earnings: {earnings_date}."
        if earnings_status == "scheduled" and earnings_date
        else "No earnings event was returned inside the verified vendor-calendar window."
        if earnings_status == "verified_none"
        else "Next earnings date unavailable; verify before entry."
    )
    reward_text = f"{plan.reward_to_risk:.1f}:1" if plan.reward_to_risk is not None else "unavailable"
    target_labels = ", ".join(
        f"{name.replace('_', ' ').title()}: {basis}" for name, basis in plan.target_basis.items()
    ) or "Target provenance unavailable"
    return {
        "summary": f"{state} {direction} setup with trigger {_format_level(plan.trigger)}, invalidation {_format_level(plan.invalidation)}, and estimated {reward_text} reward-to-risk.",
        "market_and_sector_context": " ".join(market.evidence) + f" Overall regime: {market.regime}.",
        "monthly_weekly_structure": tf("1M", "1W"),
        "daily_structure": tf("1D"),
        "four_hour_structure": tf("4H"),
        "one_hour_fifteen_minute_confirmation": tf("1H", "15M"),
        "moving_average_analysis": "The engine uses the saved-methodology baseline: 9 EMA, 21 WMA, 50 WMA, 200 WMA, and 200 SMA when enough bars exist.",
        "macd_and_momentum": f"MACD histograms across available frames: {', '.join(f'{value:.3f}' for value in momentum) or 'unavailable'}.",
        "volume_and_relative_volume": f"15m relative volume: {relative_volume:.2f}x." if relative_volume is not None else "15m relative volume unavailable.",
        "supply_and_demand": f"Daily demand: {', '.join(_format_level(value) for value in demand) or 'unavailable'}; supply: {', '.join(_format_level(value) for value in supply) or 'unavailable'}.",
        "support_and_resistance": f"Trigger {_format_level(plan.trigger)}; entry {_format_level(plan.entry_low)}–{_format_level(plan.entry_high)}; invalidation {_format_level(plan.invalidation)}.",
        "liquidity_and_market_structure": "; ".join(warnings) if warnings else "No critical liquidity warning detected from available bars.",
        "breakout_breakdown_retest": plan.confirmation,
        "entry_and_invalidation_plan": f"Enter only inside {_format_level(plan.entry_low)}–{_format_level(plan.entry_high)} after confirmation; thesis fails at {_format_level(plan.invalidation)}.",
        "targets_and_reward_to_risk": f"Targets: {_format_level(plan.target_1)}, {_format_level(plan.target_2)}, stretch {_format_level(plan.stretch_target)}; estimated R:R {reward_text}. {target_labels}.",
        "bull_case": "Higher-timeframe structure and 15m acceptance align bullishly." if direction == "bullish" else "Bull case requires reclaiming the invalidation/trigger structure and neutralizing the bearish trend.",
        "bear_case": "Higher-timeframe structure and 15m acceptance align bearishly." if direction == "bearish" else "Bear case is a failed trigger followed by loss of the defined invalidation area.",
        "no_trade_case": "; ".join(blockers) if blockers else "No trade if confirmation fails, price becomes extended, or reward-to-risk falls below 1.8:1.",
        "earnings_news_and_catalysts": f"{catalyst} {news_text}",
        "options_analysis": "Options are ranked only from a current, complete chain after the underlying reaches ENTER; otherwise the correct result is WAIT/PASS.",
        "final_verdict": _price_instruction(
            state,
            direction,
            plan,
            earnings_pending=earnings_pending,
        ),
    }


def _required_frame_complete(item: Optional[TimeframeAnalysis]) -> bool:
    if item is None or item.bar_count < MIN_REQUIRED_BARS or item.direction == "unavailable":
        return False
    return all(
        value is not None
        for value in (
            item.ema9,
            item.wma21,
            item.wma50,
            item.macd,
            item.macd_signal,
            item.macd_histogram,
            item.atr,
        )
    )


def _opposing_structure_levels(
    analyses: Mapping[str, TimeframeAnalysis],
    direction: str,
    entry_reference: float,
) -> list[float]:
    values: set[float] = set()
    for item in analyses.values():
        candidates = item.resistance if direction == "bullish" else item.support
        for candidate in candidates:
            if not math.isfinite(candidate):
                continue
            if direction == "bullish" and candidate > entry_reference:
                values.add(candidate)
            elif direction == "bearish" and candidate < entry_reference:
                values.add(candidate)
    return sorted(values, reverse=direction == "bearish")


def evaluate_setup(
    ticker: str,
    bars_by_timeframe: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    name: str = "",
    exchange: str = "",
    market_context: Optional[MarketContext] = None,
    market_status: str = "unknown",
    data_label: str = "unavailable",
    data_source: str = "Polygon",
    data_timestamp: Optional[str] = None,
    average_daily_dollar_volume: Optional[float] = None,
    earnings_date: Optional[str] = None,
    days_to_earnings: Optional[int] = None,
    earnings_status: Optional[str] = None,
    earnings_date_status: Optional[str] = None,
    earnings_checked_through: Optional[str] = None,
    earnings_error_kind: Optional[str] = None,
    earnings_status_code: Optional[int] = None,
    earnings_attempts: Optional[int] = None,
    earnings_latency_ms: Optional[float] = None,
    earnings_throttled: bool = False,
    news: Optional[list[dict[str, Any]]] = None,
    provider_warnings: Optional[list[str]] = None,
    evaluated_at: Optional[datetime] = None,
    freshness_issues: Optional[Iterable[str]] = None,
) -> DecisionResult:
    """Evaluate a setup without relying on a generic score threshold."""

    # Materialize normalized rows once so generator inputs remain available for
    # both indicator analysis and the two-candle confirmation check.
    normalized_input = {
        str(key).strip().upper(): normalize_bars(value)
        for key, value in bars_by_timeframe.items()
    }
    analyses = {label: analyze_timeframe(label, rows) for label, rows in normalized_input.items()}
    market = market_context or MarketContext()
    freshness = assess_source_freshness(
        data_label=data_label,
        data_timestamp=data_timestamp,
        market_status=market_status,
        now=evaluated_at,
    )
    warnings = (
        ["Some supporting provider inputs were unavailable; no missing observation was inferred."]
        if any(str(item or "").strip() for item in list(provider_warnings or []))
        else []
    )
    blockers: list[str] = []
    blockers.extend(freshness.reasons)
    blockers.extend(
        str(item).strip()
        for item in list(freshness_issues or [])
        if str(item or "").strip()
    )
    for timeframe in REQUIRED_TIMEFRAMES:
        item = analyses.get(timeframe)
        if not _required_frame_complete(item):
            blockers.append(
                f"{timeframe} data is incomplete; at least {MIN_REQUIRED_BARS} valid bars and complete 9 EMA, 21/50 WMA, MACD, and ATR are required."
            )

    direction, alignment, bullish_weight, bearish_weight = _direction_alignment(analyses)
    if direction == "neutral":
        blockers.append("Weekly, Daily, 4H, and 15m structure does not establish one coherent direction.")
    elif direction in {"bullish", "bearish"}:
        opposing = "bearish" if direction == "bullish" else "bullish"
        for timeframe in ("1W", "1D", "4H"):
            item = analyses.get(timeframe)
            if item and item.direction == opposing:
                blockers.append(
                    f"{timeframe} structure is {opposing} and opposes the {direction} setup."
                )
    daily = analyses.get("1D")
    tactical = analyses.get("15M")
    current_price = tactical.close if tactical and tactical.close is not None else daily.close if daily else None
    atr = tactical.atr if tactical and tactical.atr else (daily.atr if daily else None)

    trigger: Optional[float] = None
    invalidation: Optional[float] = None
    if tactical and direction == "bullish":
        trigger = tactical.prior_high or (tactical.resistance[0] if tactical.resistance else None)
        invalidation = tactical.support[0] if tactical.support else tactical.prior_low
    elif tactical and direction == "bearish":
        trigger = tactical.prior_low or (tactical.support[0] if tactical.support else None)
        invalidation = tactical.resistance[0] if tactical.resistance else tactical.prior_high
    if current_price is None or atr is None or atr <= 0 or trigger is None or invalidation is None:
        blockers.append("A complete trigger, ATR, price, and invalidation could not be derived from observable bars.")

    entry_low: Optional[float] = None
    entry_high: Optional[float] = None
    risk: Optional[float] = None
    target_1: Optional[float] = None
    target_2: Optional[float] = None
    stretch: Optional[float] = None
    rr: Optional[float] = None
    target_basis: dict[str, str] = {}
    if None not in (trigger, invalidation, atr):
        # Keep the displayed entry zone aligned with the same 0.65 ATR
        # acceptance distance used by the ARMED/confirmation state machine.
        zone = max(float(atr) * 0.65, float(trigger) * 0.001)
        entry_low = float(trigger) - zone
        entry_high = float(trigger) + zone
        entry_reference = float(trigger)
        risk = abs(entry_reference - float(invalidation))
        if risk < float(atr) * 0.45:
            invalidation = entry_reference - float(atr) if direction == "bullish" else entry_reference + float(atr)
            risk = abs(entry_reference - invalidation)
        sign = 1.0 if direction == "bullish" else -1.0
        structure = _opposing_structure_levels(analyses, direction, entry_reference)
        if risk and structure:
            target_1 = structure[0]
            rr = abs(target_1 - entry_reference) / risk
            target_basis["target_1"] = "nearest observed opposing structure"
            if len(structure) > 1:
                target_2 = structure[1]
                target_basis["target_2"] = "observed opposing structure"
            else:
                target_2 = entry_reference + sign * max(3.0, rr + 1.0) * risk
                target_basis["target_2"] = "calculated fallback beyond observed structure"
            if len(structure) > 2:
                stretch = structure[2]
                target_basis["stretch_target"] = "observed opposing structure"
            else:
                target_2_r = abs(float(target_2) - entry_reference) / risk
                stretch = entry_reference + sign * max(4.0, target_2_r + 1.0) * risk
                target_basis["stretch_target"] = "calculated fallback beyond observed structure"
        elif risk:
            target_1 = entry_reference + sign * 2 * risk
            target_2 = entry_reference + sign * 3 * risk
            stretch = entry_reference + sign * 4 * risk
            rr = 2.0
            target_basis = {
                "target_1": "calculated fallback at 2R; no opposing structure observed",
                "target_2": "calculated fallback at 3R; no opposing structure observed",
                "stretch_target": "calculated fallback at 4R; no opposing structure observed",
            }
            warnings.append(
                "No opposing structure was observed above/below entry; displayed targets are calculated R-multiple fallbacks."
            )

    market_conflict = (direction == "bullish" and market.regime == "risk-off") or (
        direction == "bearish" and market.regime == "risk-on"
    )
    if market.spy_direction == "unavailable" or market.qqq_direction == "unavailable":
        blockers.append("SPY/QQQ market context is incomplete.")
    if market_conflict:
        blockers.append(f"The {market.regime} SPY/QQQ regime conflicts with the {direction} setup.")
    if market.sector_direction not in {"unavailable", "mixed", direction}:
        warnings.append(f"The {market.sector_symbol or 'sector ETF'} trend conflicts with the ticker direction.")
    if average_daily_dollar_volume is not None:
        if average_daily_dollar_volume < 5_000_000:
            blockers.append("Average daily dollar volume is below the minimum liquidity threshold.")
        elif average_daily_dollar_volume < 20_000_000:
            warnings.append("Liquidity is adequate for review but thin for long options.")
    else:
        warnings.append("Average daily dollar volume is unavailable.")
    normalized_earnings_status = str(
        earnings_status or ("scheduled" if earnings_date else "unresolved")
    ).strip().lower()
    if normalized_earnings_status not in {"scheduled", "verified_none", "unresolved"}:
        normalized_earnings_status = "unresolved"
    if normalized_earnings_status == "scheduled" and not earnings_date:
        normalized_earnings_status = "unresolved"
    if normalized_earnings_status == "verified_none" and earnings_date is not None:
        normalized_earnings_status = "unresolved"
    parsed_checked_through = None
    if earnings_checked_through:
        try:
            parsed_checked_through = datetime.fromisoformat(
                str(earnings_checked_through).strip()
            ).date()
        except (TypeError, ValueError):
            parsed_checked_through = None
    if normalized_earnings_status == "verified_none" and parsed_checked_through is None:
        normalized_earnings_status = "unresolved"
    if normalized_earnings_status == "scheduled":
        valid_days = (
            isinstance(days_to_earnings, int)
            and not isinstance(days_to_earnings, bool)
            and days_to_earnings >= 0
        )
        try:
            datetime.fromisoformat(str(earnings_date).strip()).date()
        except (TypeError, ValueError):
            valid_days = False
        if not valid_days:
            normalized_earnings_status = "unresolved"

    if normalized_earnings_status == "scheduled" and days_to_earnings is not None and 0 <= days_to_earnings <= 3:
        blockers.append(f"Earnings are {days_to_earnings} day(s) away, inside the hard catalyst-risk window.")
    elif normalized_earnings_status == "scheduled" and days_to_earnings is not None and 0 <= days_to_earnings <= 10:
        warnings.append(f"Earnings are {days_to_earnings} day(s) away.")
    earnings_pending = normalized_earnings_status == "unresolved"
    if earnings_pending:
        warnings.append("The next earnings date is unavailable and must be verified before entry.")
    confirmed = False
    near_trigger = False
    extended = False
    tactical_bars = normalized_input.get("15M", [])
    if tactical and current_price is not None and trigger is not None and atr and len(tactical_bars) >= 2:
        distance = abs(current_price - trigger) / atr
        near_trigger = distance <= 0.65
        break_close = tactical_bars[-2]["close"]
        hold_close = tactical_bars[-1]["close"]
        if direction == "bullish":
            hold_in_entry_zone = entry_high is not None and trigger <= hold_close <= entry_high
            confirmed = (
                break_close > trigger
                and hold_in_entry_zone
                and tactical.macd_histogram is not None
                and tactical.macd_histogram >= 0
            )
            extended = current_price > trigger + 1.25 * atr or (tactical.extension_atr or 0) > 1.8
        elif direction == "bearish":
            hold_in_entry_zone = entry_low is not None and entry_low <= hold_close <= trigger
            confirmed = (
                break_close < trigger
                and hold_in_entry_zone
                and tactical.macd_histogram is not None
                and tactical.macd_histogram <= 0
            )
            extended = current_price < trigger - 1.25 * atr or (tactical.extension_atr or 0) > 1.8

    if rr is not None and rr < 1.8:
        blockers.append(f"Estimated reward-to-risk is only {rr:.1f}:1.")
    if blockers:
        state = "BLOCKED"
    elif extended:
        state = "EXTENDED"
    elif confirmed and alignment >= 0.68 and earnings_pending:
        state = "ARMED"
    elif confirmed and alignment >= 0.68:
        state = "ENTER"
    elif near_trigger:
        state = "ARMED"
    else:
        state = "FORMING"

    confidence = round(35 + alignment * 35)
    confidence += 10 if market.regime in {"risk-on", "risk-off"} and not market_conflict else -10 if market_conflict else 0
    confidence += 15 if confirmed else 5 if near_trigger else 0
    confidence += 5 if average_daily_dollar_volume and average_daily_dollar_volume >= 20_000_000 else 0
    confidence -= min(len(warnings) * 3, 15)
    confidence = max(5, min(95, confidence))
    if state == "BLOCKED":
        confidence = min(confidence, 39)
    elif state == "ENTER":
        confidence = max(confidence, 70)
    grade = "A+" if confidence >= 90 else "A" if confidence >= 82 else "A-" if confidence >= 75 else "B" if confidence >= 65 else "C" if confidence >= 50 else "D"

    plan = TradePlan(
        direction=direction,
        setup_type="break-and-retest" if near_trigger or confirmed else "multi-timeframe continuation",
        trigger=_round(trigger, 2),
        entry_low=_round(entry_low, 2),
        entry_high=_round(entry_high, 2),
        invalidation=_round(invalidation, 2),
        target_1=_round(target_1, 2),
        target_2=_round(target_2, 2),
        stretch_target=_round(stretch, 2),
        reward_to_risk=_round(rr, 2),
        horizon="2–20 trading days",
        confirmation=(
            f"Require a 15m close {'above' if direction == 'bullish' else 'below'} {_format_level(trigger)} followed by acceptance or a successful retest."
            if direction in {"bullish", "bearish"}
            else "Wait for higher-timeframe alignment and a defined 15m trigger."
        ),
        target_basis=target_basis,
    )
    reasons = []
    if direction in {"bullish", "bearish"}:
        reasons.append(f"Weighted multi-timeframe structure is {direction} ({bullish_weight} bullish vs {bearish_weight} bearish weight).")
    reasons.append(f"SPY/QQQ context is {market.regime}.")
    reasons.append("15m confirmation is satisfied." if confirmed else "15m confirmation is not yet satisfied.")
    if blockers:
        reasons = [blockers[0], *reasons][:3]
    else:
        reasons = reasons[:3]
    primary_risk = blockers[0] if blockers else warnings[0] if warnings else "A failed 15m retest would invalidate the setup."
    upgrade = (
        "Verify the next earnings date; technical break-and-hold confirmation is already satisfied."
        if earnings_pending and confirmed
        else
        f"A confirmed 15m close {'above' if direction == 'bullish' else 'below'} {_format_level(trigger)} with a successful retest and supportive volume."
        if direction in {"bullish", "bearish"}
        else "Weekly, Daily, and 4H structure must align before a tactical trigger matters."
    )
    invalidation_condition = (
        f"A decisive close {'below' if direction == 'bullish' else 'above'} {_format_level(invalidation)}."
        if direction in {"bullish", "bearish"} and invalidation is not None
        else "No current invalidation level is defined; wait for complete provider-backed evidence."
    )
    news_rows = list(news or [])
    breakdown = _build_breakdown(
        analyses,
        market,
        plan,
        direction,
        state,
        blockers,
        warnings,
        news_rows,
        earnings_date,
        normalized_earnings_status,
        earnings_pending and confirmed,
    )
    timestamp = data_timestamp or (tactical.timestamp if tactical else None) or (daily.timestamp if daily else None)
    return DecisionResult(
        ticker=_clean_ticker(ticker),
        name=str(name or _clean_ticker(ticker)).strip(),
        exchange=str(exchange or "").strip().upper(),
        verdict=STATE_TO_VERDICT[state],
        state=state,
        direction=direction,
        confidence=confidence,
        confidence_explanation=(
            f"{confidence}% decision confidence from weighted timeframe alignment, confirmation, "
            "market context, liquidity, catalyst risk, and data quality."
        ),
        grade=grade,
        current_price=_round(current_price, 2),
        market_status=market_status,
        data_timestamp=timestamp,
        data_label=freshness.effective_label,
        data_source=data_source,
        entry_conditions_satisfied=state == "ENTER",
        plan=plan,
        reasons=reasons,
        primary_risk=primary_risk,
        upgrade_condition=upgrade,
        invalidation_condition=invalidation_condition,
        do_this_now=_price_instruction(
            state,
            direction,
            plan,
            earnings_pending=earnings_pending and confirmed,
        ),
        blockers=blockers,
        warnings=warnings,
        timeframes=analyses,
        market_context=market,
        full_breakdown=breakdown,
        news=news_rows,
        earnings_date=earnings_date,
        days_to_earnings=days_to_earnings,
        earnings_status=normalized_earnings_status,
        earnings_date_status=earnings_date_status,
        earnings_checked_through=earnings_checked_through,
        earnings_error_kind=earnings_error_kind,
        earnings_status_code=earnings_status_code,
        earnings_attempts=earnings_attempts,
        earnings_latency_ms=earnings_latency_ms,
        earnings_throttled=bool(earnings_throttled),
    )


def evaluate_tracked_plan(
    previous: Mapping[str, Any],
    current_price: float,
    *,
    target_tolerance: float = 0.001,
) -> dict[str, Any]:
    """Evaluate target/invalidation events for a previously saved plan."""

    plan = previous.get("plan", previous)
    direction = str(plan.get("direction", previous.get("direction", ""))).lower()
    invalidation = _float(plan.get("invalidation"))
    targets = [_float(plan.get(key)) for key in ("target_1", "target_2", "stretch_target")]
    events: list[str] = []
    state = str(previous.get("state", "FORMING")).upper()
    if invalidation is not None:
        failed = current_price <= invalidation if direction == "bullish" else current_price >= invalidation
        if failed:
            state = "INVALIDATED"
            events.append("invalidation reached")
    for index, target in enumerate(targets, 1):
        if target is None:
            continue
        reached = current_price >= target * (1 - target_tolerance) if direction == "bullish" else current_price <= target * (1 + target_tolerance)
        if reached:
            events.append(f"target {index} reached")
    return {"state": state, "events": events, "current_price": current_price}


def important_state_change(old_state: Any, new_state: Any) -> Optional[str]:
    old = str(old_state or "").strip().upper()
    new = str(new_state or "").strip().upper()
    if old == new or not new:
        return None
    high_value = {
        ("FORMING", "ARMED"),
        ("ARMED", "ENTER"),
        ("ARMED", "INVALIDATED"),
        ("ENTER", "EXTENDED"),
        ("ENTER", "INVALIDATED"),
    }
    return f"{old or 'NEW'} → {new}" if (old, new) in high_value or not old else None
