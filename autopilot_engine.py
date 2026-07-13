"""Evidence-first multi-timeframe decision engine for Trading Autopilot.

The engine is deliberately independent from Streamlit and from any market-data
vendor.  It consumes normalized OHLCV bars and returns an explainable trade
plan.  It never places orders and it never upgrades incomplete data to ENTER.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
import re
from typing import Any, Iterable, Mapping, Optional


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
STATE_TO_VERDICT = {
    "ENTER": "ENTER",
    "FORMING": "WAIT FOR CONFIRMATION",
    "ARMED": "WAIT FOR CONFIRMATION",
    "BLOCKED": "PASS",
    "EXTENDED": "PASS",
    "INVALIDATED": "PASS",
}


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
    options: dict[str, Any] = field(default_factory=dict)
    engine_version: str = "2.0.0"

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
    catalyst = f"Next earnings: {earnings_date}." if earnings_date else "Next earnings date unavailable; verify before entry."
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
    news: Optional[list[dict[str, Any]]] = None,
    provider_warnings: Optional[list[str]] = None,
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
    warnings = list(provider_warnings or [])
    blockers: list[str] = []
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
    if days_to_earnings is not None and 0 <= days_to_earnings <= 3:
        blockers.append(f"Earnings are {days_to_earnings} day(s) away, inside the hard catalyst-risk window.")
    elif days_to_earnings is not None and 0 <= days_to_earnings <= 10:
        warnings.append(f"Earnings are {days_to_earnings} day(s) away.")
    earnings_pending = earnings_date is None
    if earnings_pending:
        warnings.append("The next earnings date is unavailable and must be verified before entry.")
    if data_label.lower() in {"unavailable", "stale"}:
        blockers.append(f"Market data is {data_label.lower()}; incomplete data cannot produce ENTER.")

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
    invalidation_condition = f"A decisive close {'below' if direction == 'bullish' else 'above'} {_format_level(invalidation)}."
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
        confidence_explanation=f"{round(alignment * 100)}% weighted timeframe alignment, adjusted for confirmation, market context, liquidity, catalyst risk, and data quality.",
        grade=grade,
        current_price=_round(current_price, 2),
        market_status=market_status,
        data_timestamp=timestamp,
        data_label=data_label,
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
