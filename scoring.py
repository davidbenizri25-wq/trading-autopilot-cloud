"""Bias-aware scoring for Trading Autopilot v0.1.4.

Decision support only. This module never connects to brokers, prepares orders,
creates TradingView alerts, or automates execution.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


MARKET_CONTEXT_TYPES = {"future", "forex", "crypto", "index", "index_future", "commodity", "commodity_future"}
HIGH_BETA_CATEGORIES = {"high_beta_spec", "high_beta_fintech", "high_beta_mega_cap"}


def _float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace(",", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "manual_override"}


def _near_pct(price: float, level: float, pct: float) -> bool:
    return price > 0 and level > 0 and abs(price - level) / price <= pct


def _first_positive(*values: Any) -> float:
    for value in values:
        parsed = _float(value)
        if parsed > 0:
            return parsed
    return 0.0


def grade_for_score(score: int) -> str:
    if score >= 85:
        return "A+"
    if score >= 80:
        return "A"
    if score >= 75:
        return "A-"
    if score >= 70:
        return "B+"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    return "F"


@dataclass
class DirectionalLevels:
    bullish_invalid: float
    bearish_invalid: float
    bullish_trigger: float
    bearish_trigger: float
    has_bearish_invalid: bool


@dataclass
class ScoreResult:
    ticker: str
    bias: str
    score: int
    grade: str
    state: str
    setup_tags: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    risk_flags: list[str] = field(default_factory=list)
    alert_suggestions: list[str] = field(default_factory=list)
    alert_details: list[dict[str, str]] = field(default_factory=list)
    bullish_score: int = 0
    bearish_score: int = 0
    bullish_invalid: float = 0.0
    bearish_invalid: float = 0.0
    bullish_trigger: float = 0.0
    bearish_trigger: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "bias": self.bias,
            "score": self.score,
            "grade": self.grade,
            "state": self.state,
            "setup_tags": self.setup_tags,
            "reasons": self.reasons,
            "risk_flags": self.risk_flags,
            "alert_suggestions": self.alert_suggestions,
            "alert_details": self.alert_details,
            "bullish_score": self.bullish_score,
            "bearish_score": self.bearish_score,
            "bullish_invalid": self.bullish_invalid,
            "bearish_invalid": self.bearish_invalid,
            "bullish_trigger": self.bullish_trigger,
            "bearish_trigger": self.bearish_trigger,
        }


def _directional_levels(row: dict[str, Any], close: float) -> DirectionalLevels:
    invalid = _float(row.get("invalid"))
    resistance1 = _float(row.get("resistance1"))
    breakout = _float(row.get("breakout"))
    breakdown = _float(row.get("breakdown"))

    bullish_invalid = _first_positive(row.get("bullish_invalid"), invalid)
    bullish_trigger = _first_positive(row.get("bullish_trigger"), breakout)
    bearish_trigger = _first_positive(row.get("bearish_trigger"), breakdown)

    explicit_bearish_invalid = _float(row.get("bearish_invalid"))
    if explicit_bearish_invalid > 0:
        bearish_invalid = explicit_bearish_invalid
        has_bearish_invalid = True
    elif resistance1 > close > 0:
        bearish_invalid = resistance1
        has_bearish_invalid = True
    elif invalid > close > 0:
        bearish_invalid = invalid
        has_bearish_invalid = True
    else:
        bearish_invalid = 0.0
        has_bearish_invalid = False

    return DirectionalLevels(
        bullish_invalid=bullish_invalid,
        bearish_invalid=bearish_invalid,
        bullish_trigger=bullish_trigger,
        bearish_trigger=bearish_trigger,
        has_bearish_invalid=has_bearish_invalid,
    )


def _base_state(score: int, bias: str) -> str:
    if bias == "context":
        return "context_watch" if score >= 50 else "context_risk"
    if bias == "neutral":
        return "watch" if score >= 50 else "avoid"
    if score >= 75:
        return "priority_watch"
    if score >= 60:
        return "alert"
    if score >= 50:
        return "watch"
    return "avoid"


def _cap_state_at_alert(state: str) -> str:
    return "alert" if state == "priority_watch" else state


def _detect_bias(row: dict[str, Any], bullish_score: int, bearish_score: int) -> str:
    asset_type = str(row.get("asset_type", "")).strip().lower()
    category = str(row.get("category", "")).strip().lower()
    if asset_type in MARKET_CONTEXT_TYPES or category in MARKET_CONTEXT_TYPES:
        return "context"
    if bullish_score >= bearish_score + 8 and bullish_score >= 50:
        return "bullish"
    if bearish_score >= bullish_score + 8 and bearish_score >= 50:
        return "bearish"
    return "neutral"


def _unique(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item and item not in seen:
            output.append(item)
            seen.add(item)
    return output


def score_candidate(row: dict[str, Any], market_bias: str = "mixed") -> ScoreResult:
    ticker = str(row.get("ticker", "")).strip().upper()
    watchlist = str(row.get("watchlist", "")).strip()
    asset_type = str(row.get("asset_type", "")).strip().lower()
    category = str(row.get("category", "")).strip().lower()
    timeframe = str(row.get("timeframe", "")).strip() or "15m"

    close = _float(row.get("close"))
    ema9 = _float(row.get("ema9"))
    ema21 = _float(row.get("ema21"))
    wma50 = _float(row.get("wma50"))
    wma200 = _float(row.get("wma200"))
    sma200 = _float(row.get("sma200"))
    macd_hist = _float(row.get("macd_hist"))
    macd_hist_prev = _float(row.get("macd_hist_prev"))
    support1 = _float(row.get("support1"))
    resistance1 = _float(row.get("resistance1"))
    breakout = _float(row.get("breakout"))
    breakdown = _float(row.get("breakdown"))
    avg_volume = _float(row.get("avg_volume"))
    relative_volume = _float(row.get("relative_volume"), 1.0)
    manual_override = _bool(row.get("manual_override"))
    levels = _directional_levels(row, close)

    is_market_context = asset_type in MARKET_CONTEXT_TYPES or category in MARKET_CONTEXT_TYPES
    setup_tags: list[str] = []
    bullish_reasons: list[str] = []
    bearish_reasons: list[str] = []
    risk_flags: list[str] = []
    alert_details: list[dict[str, str]] = []
    bullish_score = 40
    bearish_score = 40
    cap_state_below_priority = False
    cap_final_score: int | None = None

    if watchlist == "Weekly Watchlist":
        setup_tags.append("weekly_watchlist")
        bullish_score += 4
        bearish_score += 4
    elif watchlist:
        setup_tags.append("broad_watchlist")

    if is_market_context:
        setup_tags.append("market_context")

    if asset_type == "equity":
        if avg_volume <= 0:
            risk_flags.append("underlying average volume missing")
            setup_tags.append("underlying_volume_missing")
            cap_state_below_priority = True

        if 0 < close <= 25:
            setup_tags.append("under_25_candidate")
            if avg_volume >= 1_000_000:
                bullish_score += 3
                bearish_score += 3
            else:
                risk_flags.append("under-25 name with thin volume")
                setup_tags.append("thin_underlying_volume")
                cap_state_below_priority = True
                bullish_score -= 8
                bearish_score -= 8

        if category == "under_25_candidate" and avg_volume < 1_000_000:
            if manual_override:
                risk_flags.append("manual override on thin under-25 volume")
            else:
                cap_final_score = 69

    if category in HIGH_BETA_CATEGORIES:
        setup_tags.append("high_beta")

    if close > 0 and ema9 > 0 and ema21 > 0:
        if close > ema9 > ema21:
            bullish_score += 15
            setup_tags.append("ema_9_21_bullish")
            bullish_reasons.append("price above 9 EMA and 9 EMA above 21 EMA")
        elif close < ema9 < ema21:
            bearish_score += 15
            setup_tags.append("ema_9_21_bearish")
            bearish_reasons.append("price below 9 EMA and 9 EMA below 21 EMA")
        elif ema9 > ema21:
            bullish_score += 5
            bullish_reasons.append("9 EMA above 21 EMA")
        elif ema9 < ema21:
            bearish_score += 5
            bearish_reasons.append("9 EMA below 21 EMA")

    if close > 0 and wma50 > 0:
        if close > wma50:
            bullish_score += 8
            bullish_reasons.append("price above 50 WMA")
        else:
            bearish_score += 8
            bearish_reasons.append("price below 50 WMA")

    if close > 0 and wma200 > 0 and sma200 > 0:
        if close > wma200 and close > sma200:
            bullish_score += 12
            setup_tags.append("above_200_ma")
            bullish_reasons.append("price above both 200 MA references")
        elif close < wma200 and close < sma200:
            bearish_score += 12
            setup_tags.append("below_200_ma")
            bearish_reasons.append("price below both 200 MA references")
        else:
            setup_tags.append("mixed_200_ma")

    if macd_hist > 0 and macd_hist > macd_hist_prev:
        bullish_score += 12
        setup_tags.append("macd_bullish")
        bullish_reasons.append("MACD histogram positive and improving")
    elif macd_hist < 0 and macd_hist < macd_hist_prev:
        bearish_score += 12
        setup_tags.append("macd_bearish")
        bearish_reasons.append("MACD histogram negative and worsening")

    if support1 > 0 and close > 0:
        if close < support1:
            bearish_score += 10
            bearish_reasons.append("price is below support1")
        elif _near_pct(close, support1, 0.02):
            bullish_score += 8
            bearish_score += 3
            setup_tags.append("near_support")
            bullish_reasons.append("price is near support")

    if resistance1 > 0 and close > 0:
        if close > resistance1:
            bullish_score += 10
            bullish_reasons.append("price is above resistance1")
        elif _near_pct(close, resistance1, 0.02):
            bearish_score += 8
            setup_tags.append("resistance_test")
            bearish_reasons.append("price is near resistance")

    if breakout > 0 and close > 0:
        if close > breakout:
            bullish_score += 13
            setup_tags.append("breakout_triggered")
            bullish_reasons.append("price is above bullish trigger")
        elif 0 <= (breakout - close) / close <= 0.02:
            bullish_score += 5
            setup_tags.append("breakout_watch")
            bullish_reasons.append("price is within 2 percent of bullish trigger")

    if breakdown > 0 and close > 0:
        if close < breakdown:
            bearish_score += 13
            setup_tags.append("breakdown_triggered")
            bearish_reasons.append("price is below bearish trigger")
        elif 0 <= (close - breakdown) / close <= 0.02:
            bearish_score += 5
            setup_tags.append("breakdown_watch")

    if relative_volume >= 1.5:
        bullish_score += 6
        bearish_score += 6
        setup_tags.append("relative_volume_elevated")

    if market_bias == "risk_off" and category in HIGH_BETA_CATEGORIES:
        bullish_score -= 18
        risk_flags.append("risk_off penalizes high-beta bullish setups")
    elif market_bias == "risk_on" and category in HIGH_BETA_CATEGORIES:
        bullish_score += 4

    bullish_score = max(0, min(100, int(round(bullish_score))))
    bearish_score = max(0, min(100, int(round(bearish_score))))
    bias = _detect_bias(row, bullish_score, bearish_score)
    score = max(bullish_score, bearish_score) if bias in {"bullish", "bearish", "context"} else int(round((bullish_score + bearish_score) / 2))
    if cap_final_score is not None:
        score = min(score, cap_final_score)
    state = _base_state(score, bias)

    if bias == "bullish":
        reasons = bullish_reasons or ["bullish bias from aggregate score"]
        if levels.bearish_trigger > 0 and close < levels.bearish_trigger:
            setup_tags.append("directional_conflict")
            risk_flags.append("bullish setup below breakdown")
            state = _cap_state_at_alert(state)
        if levels.bullish_invalid > 0 and close < levels.bullish_invalid:
            setup_tags.append("directional_conflict")
            risk_flags.append("bullish setup below invalidation")
            state = "avoid"
        if levels.bullish_invalid <= 0 and state == "priority_watch":
            risk_flags.append("missing bullish invalidation level")
            state = "alert"
    elif bias == "bearish":
        reasons = bearish_reasons or ["bearish bias from aggregate score"]
        if levels.bullish_trigger > 0 and close > levels.bullish_trigger:
            setup_tags.append("directional_conflict")
            risk_flags.append("bearish setup above breakout")
            state = _cap_state_at_alert(state)
        if not levels.has_bearish_invalid and state == "priority_watch":
            risk_flags.append("missing bearish invalidation level")
            state = "alert"
    elif bias == "context":
        reasons = _unique(bullish_reasons + bearish_reasons) or ["market context symbol for regime read"]
    else:
        reasons = _unique(bullish_reasons + bearish_reasons) or ["mixed signals; wait for confirmation"]

    if cap_state_below_priority and state == "priority_watch":
        state = "alert"

    def add_alert(alert_type: str, message: str) -> None:
        alert_details.append({"alert_type": alert_type, "message": message})

    if bias == "bullish":
        if ema9 > 0 and ema21 > 0:
            add_alert("ema_flip", f"{ticker} bullish 9/21 EMA flip on {timeframe}")
        if wma200 > 0 or sma200 > 0:
            add_alert("ma_reclaim_loss", f"{ticker} 200 MA reclaim on {timeframe}")
        if levels.bullish_trigger > 0:
            add_alert("bullish_trigger", f"{ticker} bullish trigger cross {levels.bullish_trigger:g} on {timeframe}")
        if levels.bullish_invalid > 0:
            add_alert("invalidation", f"{ticker} bullish invalidation breach {levels.bullish_invalid:g} on {timeframe}")
        if support1 > 0:
            add_alert("bullish_trigger", f"{ticker} support/retest near {support1:g} on {timeframe}")
        if levels.bearish_trigger > 0:
            add_alert("bearish_trigger", f"{ticker} risk alert: bearish trigger {levels.bearish_trigger:g} on {timeframe}")
    elif bias == "bearish":
        if ema9 > 0 and ema21 > 0:
            add_alert("ema_flip", f"{ticker} bearish 9/21 EMA flip on {timeframe}")
        if wma200 > 0 or sma200 > 0:
            add_alert("ma_reclaim_loss", f"{ticker} 200 MA loss on {timeframe}")
        if levels.bearish_trigger > 0:
            add_alert("bearish_trigger", f"{ticker} bearish trigger cross {levels.bearish_trigger:g} on {timeframe}")
        if levels.bearish_invalid > 0:
            add_alert("invalidation", f"{ticker} bearish invalidation breach {levels.bearish_invalid:g} on {timeframe}")
        if resistance1 > 0:
            add_alert("bearish_trigger", f"{ticker} resistance/rejection near {resistance1:g} on {timeframe}")
        if levels.bullish_trigger > 0:
            add_alert("bullish_trigger", f"{ticker} risk alert: bullish trigger {levels.bullish_trigger:g} on {timeframe}")
    elif bias == "context":
        if support1 > 0:
            add_alert("context_level", f"{ticker} context support test {support1:g} on {timeframe}")
        if resistance1 > 0:
            add_alert("context_level", f"{ticker} context resistance test {resistance1:g} on {timeframe}")
        if wma200 > 0 or sma200 > 0:
            add_alert("context_level", f"{ticker} context 200 MA relation on {timeframe}")
    else:
        if ema9 > 0 and ema21 > 0:
            add_alert("ema_flip", f"{ticker} neutral 9/21 EMA confirmation on {timeframe}")
        if levels.bullish_trigger > 0:
            add_alert("bullish_trigger", f"{ticker} neutral bullish trigger watch {levels.bullish_trigger:g} on {timeframe}")
        if levels.bearish_trigger > 0:
            add_alert("bearish_trigger", f"{ticker} neutral bearish trigger watch {levels.bearish_trigger:g} on {timeframe}")

    unique_alert_details: list[dict[str, str]] = []
    seen_alerts: set[tuple[str, str]] = set()
    for detail in alert_details:
        key = (detail["alert_type"], detail["message"])
        if key not in seen_alerts:
            unique_alert_details.append(detail)
            seen_alerts.add(key)
    alert_details = unique_alert_details
    alert_suggestions = _unique(detail["message"] for detail in alert_details)

    return ScoreResult(
        ticker=ticker,
        bias=bias,
        score=score,
        grade=grade_for_score(score),
        state=state,
        setup_tags=sorted(setup_tags),
        reasons=_unique(reasons),
        risk_flags=_unique(risk_flags),
        alert_suggestions=_unique(alert_suggestions),
        alert_details=alert_details,
        bullish_score=bullish_score,
        bearish_score=bearish_score,
        bullish_invalid=levels.bullish_invalid,
        bearish_invalid=levels.bearish_invalid,
        bullish_trigger=levels.bullish_trigger,
        bearish_trigger=levels.bearish_trigger,
    )


def load_candidates_csv(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open(newline="") as handle:
        return list(csv.DictReader(handle))


def rank_candidates(rows: list[dict[str, Any]], market_bias: str = "mixed") -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for row in rows:
        result = score_candidate(row, market_bias=market_bias)
        ranked.append({**row, **result.as_dict()})
    return sorted(ranked, key=lambda item: (-int(item["score"]), str(item["ticker"])))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("Usage: python3 scoring.py data/sample_candidates.csv [market_bias]")
        return 2
    market_bias = argv[2] if len(argv) > 2 else "mixed"
    print(json.dumps(rank_candidates(load_candidates_csv(argv[1]), market_bias=market_bias), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
