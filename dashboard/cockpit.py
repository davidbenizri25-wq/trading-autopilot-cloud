"""Premium, one-search Streamlit cockpit for Trading Autopilot.

The renderer deliberately accepts the Streamlit module as an argument.  That
keeps this module importable in lightweight test and CLI environments while
the existing dashboard entry point remains responsible for page setup.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import html
import os
import re
from typing import Any, Callable, Mapping, MutableMapping, Optional

from autopilot_chart import build_autopilot_chart
from autopilot_journal import aggregate_calibration, evaluate_journal_outcome
from autopilot_service import (
    AutopilotServiceError,
    TTLCache,
    analyze_symbol,
    unavailable_result,
)
from autopilot_state import AutopilotStateStore, default_state
from tradingview_integration import (
    normalize_tradingview_symbol,
    tradingview_chart_url,
    tradingview_widget_html,
)


BREAKDOWN_SECTIONS: tuple[tuple[str, str], ...] = (
    ("summary", "1. One-paragraph summary"),
    ("market_and_sector_context", "2. Market and sector context"),
    ("monthly_weekly_structure", "3. Monthly / Weekly structure"),
    ("daily_structure", "4. Daily structure"),
    ("four_hour_structure", "5. 4H structure"),
    ("one_hour_fifteen_minute_confirmation", "6. 1H / 15m confirmation"),
    ("moving_average_analysis", "7. Moving-average analysis"),
    ("macd_and_momentum", "8. MACD and momentum"),
    ("volume_and_relative_volume", "9. Volume and relative-volume analysis"),
    ("supply_and_demand", "10. Supply and demand"),
    ("support_and_resistance", "11. Support and resistance"),
    ("liquidity_and_market_structure", "12. Liquidity and market structure"),
    ("breakout_breakdown_retest", "13. Breakout, breakdown and retest conditions"),
    ("entry_and_invalidation_plan", "14. Entry and invalidation plan"),
    ("targets_and_reward_to_risk", "15. Targets and reward-to-risk"),
    ("bull_case", "16. Bull case"),
    ("bear_case", "17. Bear case"),
    ("no_trade_case", "18. No-trade case"),
    ("earnings_news_and_catalysts", "19. Earnings, news and catalysts"),
    ("options_analysis", "20. Options analysis"),
    ("final_verdict", "21. Final verdict in plain English"),
)

CURRENTNESS_LABELS = {
    "real-time": "Real-time",
    "realtime": "Real-time",
    "delayed": "Delayed",
    "last-close": "Last close",
    "last_close": "Last close",
    "stale": "Stale — decision gated",
    "unavailable": "Unavailable",
}

STATE_PRIORITY = {
    "ENTER": 0,
    "ARMED": 1,
    "FORMING": 2,
    "EXTENDED": 3,
    "BLOCKED": 4,
    "INVALIDATED": 5,
}

_UNSAFE_PUBLIC_TEXT = (
    re.compile(r"(?:^|\s)/(?:Users|home|private|tmp|var|etc|mount|mnt)/", re.IGNORECASE),
    re.compile(r"\b[A-Za-z]:\\"),
    re.compile(r"\bTraceback\b|\b(?:[A-Za-z]+Error|Exception):", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password)\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

_RESULT_CACHE = TTLCache(ttl_seconds=120, max_items=32)


COCKPIT_CSS = """
<style>
  :root {
    --ap-bg: #06101d;
    --ap-panel: rgba(12, 25, 42, 0.92);
    --ap-panel-soft: rgba(15, 31, 51, 0.78);
    --ap-line: rgba(148, 163, 184, 0.17);
    --ap-text: #e8f0fa;
    --ap-muted: #95a8bd;
    --ap-cyan: #38bdf8;
    --ap-green: #34d399;
    --ap-amber: #fbbf24;
    --ap-red: #fb7185;
  }
  .stApp {
    background:
      radial-gradient(circle at 12% -8%, rgba(14, 165, 233, 0.14), transparent 33rem),
      radial-gradient(circle at 92% 4%, rgba(99, 102, 241, 0.10), transparent 28rem),
      var(--ap-bg);
    color: var(--ap-text);
  }
  [data-testid="stAppViewContainer"] .main .block-container {
    max-width: 1240px;
    padding-top: 1.4rem;
    padding-bottom: 5rem;
  }
  [data-testid="stSidebar"] { display: none; }
  .ap-kicker {
    color: var(--ap-cyan);
    font-size: 0.74rem;
    font-weight: 800;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    margin-bottom: 0.35rem;
  }
  .ap-title {
    color: var(--ap-text);
    font-size: clamp(1.75rem, 4vw, 2.75rem);
    font-weight: 760;
    line-height: 1.04;
    letter-spacing: -0.045em;
    margin: 0;
  }
  .ap-subtitle {
    color: var(--ap-muted);
    max-width: 47rem;
    margin: 0.6rem 0 1.2rem;
    font-size: 0.96rem;
  }
  div[data-testid="stForm"] {
    border: 1px solid var(--ap-line);
    border-radius: 1.15rem;
    background: rgba(9, 20, 35, 0.84);
    padding: 0.85rem;
    box-shadow: 0 18px 55px rgba(0, 0, 0, 0.24);
  }
  div[data-testid="stTextInput"] input {
    min-height: 3.55rem;
    border-radius: 0.85rem;
    border: 1px solid rgba(56, 189, 248, 0.28);
    background: #0b192a;
    color: var(--ap-text);
    font-size: 1.18rem;
    font-weight: 650;
    letter-spacing: 0.015em;
  }
  div[data-testid="stTextInput"] input:focus {
    border-color: rgba(56, 189, 248, 0.82);
    box-shadow: 0 0 0 0.2rem rgba(56, 189, 248, 0.12);
  }
  .stButton > button, .stLinkButton > a, div[data-testid="stFormSubmitButton"] button {
    min-height: 3rem;
    border-radius: 0.78rem;
    font-weight: 750;
  }
  .ap-decision {
    border: 1px solid var(--ap-line);
    border-radius: 1.2rem;
    padding: clamp(1rem, 3vw, 1.45rem);
    background: linear-gradient(145deg, rgba(14, 30, 49, 0.97), rgba(8, 19, 33, 0.98));
    box-shadow: 0 24px 70px rgba(0, 0, 0, 0.28);
    margin: 1.15rem 0 0.8rem;
  }
  .ap-decision.enter { border-color: rgba(52, 211, 153, 0.48); }
  .ap-decision.wait { border-color: rgba(251, 191, 36, 0.46); }
  .ap-decision.pass { border-color: rgba(251, 113, 133, 0.38); }
  .ap-decision-top {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 1rem;
    flex-wrap: wrap;
  }
  .ap-symbol {
    color: var(--ap-text);
    font-size: clamp(1.45rem, 3vw, 2rem);
    font-weight: 800;
    letter-spacing: -0.03em;
  }
  .ap-security { color: var(--ap-muted); margin-top: 0.22rem; font-size: 0.9rem; }
  .ap-verdict {
    border-radius: 999px;
    padding: 0.55rem 0.9rem;
    font-size: 0.78rem;
    font-weight: 900;
    letter-spacing: 0.08em;
    white-space: nowrap;
  }
  .enter .ap-verdict { color: #a7f3d0; background: rgba(16, 185, 129, 0.14); }
  .wait .ap-verdict { color: #fde68a; background: rgba(245, 158, 11, 0.14); }
  .pass .ap-verdict { color: #fecdd3; background: rgba(244, 63, 94, 0.12); }
  .ap-now {
    color: var(--ap-text);
    font-size: clamp(1.02rem, 2.4vw, 1.23rem);
    font-weight: 710;
    line-height: 1.48;
    margin: 1.05rem 0 0.78rem;
  }
  .ap-meta { color: var(--ap-muted); font-size: 0.82rem; }
  .ap-grid {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 0.72rem;
    margin: 0.75rem 0 1rem;
  }
  .ap-cell, .ap-home-card {
    border: 1px solid var(--ap-line);
    border-radius: 0.92rem;
    background: var(--ap-panel-soft);
    padding: 0.83rem;
    min-width: 0;
  }
  .ap-label {
    color: var(--ap-muted);
    font-size: 0.67rem;
    font-weight: 760;
    letter-spacing: 0.08em;
    text-transform: uppercase;
  }
  .ap-value {
    color: var(--ap-text);
    font-size: 0.98rem;
    font-weight: 720;
    line-height: 1.32;
    margin-top: 0.28rem;
    overflow-wrap: anywhere;
  }
  .ap-section-title {
    color: var(--ap-text);
    font-size: 1.08rem;
    font-weight: 760;
    margin: 1.35rem 0 0.55rem;
  }
  .ap-callout {
    border-left: 3px solid var(--ap-amber);
    border-radius: 0.55rem;
    background: rgba(245, 158, 11, 0.08);
    color: var(--ap-text);
    padding: 0.85rem 0.95rem;
    margin: 0.55rem 0;
  }
  .ap-callout.risk { border-left-color: var(--ap-red); background: rgba(244, 63, 94, 0.07); }
  .ap-callout.upgrade { border-left-color: var(--ap-green); background: rgba(16, 185, 129, 0.07); }
  .ap-table-wrap { overflow-x: auto; border: 1px solid var(--ap-line); border-radius: 0.9rem; }
  .ap-table { width: 100%; border-collapse: collapse; min-width: 1120px; font-size: 0.78rem; }
  .ap-table th { color: var(--ap-muted); background: #0a1727; text-align: left; padding: 0.7rem; white-space: nowrap; }
  .ap-table td { color: var(--ap-text); border-top: 1px solid var(--ap-line); padding: 0.7rem; vertical-align: top; }
  .ap-rank-row { display: flex; justify-content: space-between; gap: 0.7rem; padding: 0.52rem 0; border-bottom: 1px solid var(--ap-line); }
  .ap-rank-row:last-child { border-bottom: 0; }
  .ap-muted { color: var(--ap-muted); }
  @media (max-width: 860px) {
    [data-testid="stAppViewContainer"] .main .block-container { padding: 0.8rem 0.72rem 4rem; }
    .ap-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
    .ap-decision { border-radius: 1rem; }
  }
  @media (max-width: 520px) {
    .ap-grid { grid-template-columns: 1fr; gap: 0.5rem; }
    .ap-title { font-size: 1.75rem; }
    .ap-subtitle { font-size: 0.9rem; }
    div[data-testid="stTextInput"] input { min-height: 3.3rem; font-size: 1.05rem; }
    .stButton > button, .stLinkButton > a, div[data-testid="stFormSubmitButton"] button { min-height: 3.2rem; }
  }
</style>
"""


def _number(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def public_text(value: Any, fallback: str = "Unavailable", *, max_length: int = 800) -> str:
    """Return bounded display text without paths, traces, or credential-like material."""

    text = " ".join(str(value or "").split())
    if not text:
        return fallback
    if len(text) > max_length:
        text = text[: max_length - 1].rstrip() + "…"
    if any(pattern.search(text) for pattern in _UNSAFE_PUBLIC_TEXT):
        return fallback
    return text


def format_price(value: Any) -> str:
    number = _number(value)
    return f"${number:,.2f}" if number is not None else "Unavailable"


def format_ratio(value: Any) -> str:
    number = _number(value)
    return f"{number:.2f}:1" if number is not None else "Unavailable"


def format_percent(value: Any, *, decimal_input: bool = False, digits: int = 1) -> str:
    number = _number(value)
    if number is None:
        return "Unavailable"
    if decimal_input:
        number *= 100
    return f"{number:,.{digits}f}%"


def format_integer(value: Any) -> str:
    number = _number(value)
    return f"{int(round(number)):,}" if number is not None else "Unavailable"


def format_timestamp(value: Any) -> str:
    """Format an ISO timestamp precisely in UTC without echoing invalid input."""

    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return "Unavailable"
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return "Unavailable"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%b %d, %Y · %H:%M:%S UTC")


def currentness_label(value: Any) -> str:
    normalized = str(value or "unavailable").strip().lower().replace(" ", "-")
    return CURRENTNESS_LABELS.get(normalized, "Unavailable")


def verdict_label(value: Any) -> str:
    normalized = str(value or "PASS").strip().upper().replace("_", " ")
    if normalized == "ENTER":
        return "ENTER"
    if normalized in {"WAIT", "WAIT FOR CONFIRMATION", "ARMED", "FORMING"}:
        return "WAIT"
    return "PASS"


def resolve_state_path(config: Mapping[str, Any] | Any | None) -> Optional[str]:
    """Resolve the durable state location only from explicit config or environment."""

    configured = _config_value(config, "AUTOPILOT_STATE_PATH")
    value = configured if configured not in (None, "") else os.environ.get("AUTOPILOT_STATE_PATH")
    cleaned = str(value or "").strip()
    return cleaned or None


def _config_value(config: Mapping[str, Any] | Any | None, key: str, default: Any = None) -> Any:
    if config is None:
        return default
    if isinstance(config, Mapping):
        return config.get(key, default)
    return getattr(config, key, default)


def _safe_decision(decision: Mapping[str, Any] | None) -> dict[str, Any]:
    """Prepare a fail-closed, display-safe decision mapping."""

    source = deepcopy(dict(decision or {}))
    data_label = str(source.get("data_label") or "unavailable").strip().lower()
    source_name = str(source.get("data_source") or "Unavailable").strip().lower()
    price = _number(source.get("current_price"))
    fail_closed = data_label in {"unavailable", "stale"} or source_name == "unavailable" or price is None
    if fail_closed:
        source["verdict"] = "PASS"
        source["state"] = "BLOCKED"
        source["entry_conditions_satisfied"] = False
        source["do_this_now"] = (
            "Pass for now—current market evidence is unavailable or stale, so no entry decision was made."
        )
        source["primary_risk"] = "The current market evidence is not complete enough for an entry decision."

    warnings = source.get("warnings")
    if isinstance(warnings, list) and warnings:
        source["warnings"] = ["Some supporting provider inputs were unavailable; the decision was gated conservatively."]
        if source.get("primary_risk") in warnings:
            source["primary_risk"] = source["warnings"][0]

    breakdown = deepcopy(source.get("full_breakdown"))
    if not isinstance(breakdown, Mapping):
        breakdown = {}
    else:
        breakdown = dict(breakdown)
    if warnings:
        breakdown["liquidity_and_market_structure"] = (
            "Some supporting provider inputs were unavailable. No missing observation was inferred, "
            "and the verdict was gated conservatively."
        )
    if fail_closed:
        breakdown["final_verdict"] = source["do_this_now"]
    source["full_breakdown"] = breakdown
    return source


def build_decision_brief(decision: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build every value required by the top decision card."""

    safe = _safe_decision(decision)
    plan = safe.get("plan") if isinstance(safe.get("plan"), Mapping) else {}
    reasons = [public_text(item, "Evidence unavailable") for item in list(safe.get("reasons") or [])[:3]]
    while len(reasons) < 3:
        reasons.append("No additional provider-backed reason is available.")
    verdict = verdict_label(safe.get("verdict"))
    state = public_text(str(safe.get("state") or "BLOCKED").upper(), "BLOCKED")
    ticker = public_text(str(safe.get("ticker") or "UNRESOLVED").upper(), "UNRESOLVED", max_length=24)
    entry_low = plan.get("entry_low")
    entry_high = plan.get("entry_high")
    entry_zone = (
        f"{format_price(entry_low)} – {format_price(entry_high)}"
        if _number(entry_low) is not None and _number(entry_high) is not None
        else "Unavailable"
    )
    confidence = _number(safe.get("confidence"))
    confidence_text = f"{max(0, min(100, int(round(confidence))))}%" if confidence is not None else "Unavailable"
    return {
        "verdict": verdict,
        "verdict_class": "enter" if verdict == "ENTER" else "wait" if verdict == "WAIT" else "pass",
        "ticker": ticker,
        "name": public_text(safe.get("name"), ticker, max_length=100),
        "exchange": public_text(safe.get("exchange"), "Exchange unresolved", max_length=32),
        "state": state,
        "direction": public_text(str(safe.get("direction") or "neutral").title(), "Neutral", max_length=24),
        "confidence": confidence_text,
        "confidence_explanation": public_text(
            safe.get("confidence_explanation"),
            "Confidence is unavailable because current evidence is incomplete.",
            max_length=420,
        ),
        "grade": public_text(safe.get("grade"), "Unavailable", max_length=8),
        "current_price": format_price(safe.get("current_price")),
        "market_status": public_text(str(safe.get("market_status") or "unknown").replace("_", " ").title(), "Unknown"),
        "timestamp": format_timestamp(safe.get("data_timestamp")),
        "currentness": currentness_label(safe.get("data_label")),
        "source": public_text(safe.get("data_source"), "Unavailable", max_length=48),
        "setup_type": public_text(plan.get("setup_type"), "Unavailable", max_length=80),
        "entry_satisfied": "Yes" if bool(safe.get("entry_conditions_satisfied")) and verdict == "ENTER" else "No",
        "trigger": format_price(plan.get("trigger")),
        "entry_zone": entry_zone,
        "invalidation": format_price(plan.get("invalidation")),
        "target_1": format_price(plan.get("target_1")),
        "target_2": format_price(plan.get("target_2")),
        "stretch_target": format_price(plan.get("stretch_target")),
        "reward_to_risk": format_ratio(plan.get("reward_to_risk")),
        "horizon": public_text(plan.get("horizon"), "Unavailable", max_length=80),
        "reasons": reasons,
        "primary_risk": public_text(
            safe.get("primary_risk"),
            "Current evidence is incomplete; preserve capital.",
            max_length=480,
        ),
        "upgrade": public_text(
            safe.get("upgrade_condition"),
            "Wait for complete higher-timeframe and 15m confirmation.",
            max_length=480,
        ),
        "invalidate": public_text(
            safe.get("invalidation_condition"),
            "The thesis is invalid without a defined, current invalidation level.",
            max_length=480,
        ),
        "do_now": public_text(
            safe.get("do_this_now"),
            "Pass for now—current evidence is incomplete.",
            max_length=520,
        ),
        "safe_decision": safe,
    }


def options_table_rows(options: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Return presentation rows without inventing unavailable chain observations."""

    options = options or {}
    raw_rows = options.get("ranked_contracts")
    if not isinstance(raw_rows, list):
        raw_rows = options.get("contracts") if isinstance(options.get("contracts"), list) else []
    rows: list[dict[str, str]] = []
    for index, raw in enumerate(raw_rows[:12], 1):
        if not isinstance(raw, Mapping):
            continue
        expected_move = raw.get("expected_move") if isinstance(raw.get("expected_move"), Mapping) else {}
        earnings = raw.get("earnings_exposure") if isinstance(raw.get("earnings_exposure"), Mapping) else {}
        fit = raw.get("fit_rationale") if isinstance(raw.get("fit_rationale"), list) else []
        lower = raw.get("why_ranked_lower") if isinstance(raw.get("why_ranked_lower"), list) else []
        rows.append(
            {
                "Rank": format_integer(raw.get("rank") or index),
                "Contract": public_text(raw.get("contract_symbol"), "Unavailable", max_length=40),
                "Type": public_text(raw.get("call_put") or raw.get("option_type"), "Unavailable", max_length=12).upper(),
                "Expiration": public_text(raw.get("expiration"), "Unavailable", max_length=16),
                "DTE": format_integer(raw.get("dte")),
                "Strike": format_price(raw.get("strike")),
                "Bid": format_price(raw.get("bid")),
                "Ask": format_price(raw.get("ask")),
                "Mid": format_price(raw.get("mid")),
                "Spread $": format_price(raw.get("spread_dollars")),
                "Spread %": format_percent(raw.get("spread_pct"), decimal_input=True),
                "Volume": format_integer(raw.get("volume")),
                "Open interest": format_integer(raw.get("open_interest")),
                "IV": format_percent(raw.get("implied_volatility"), decimal_input=True),
                "IV rank": format_percent(raw.get("iv_rank"), digits=1),
                "Delta": _format_decimal(raw.get("delta"), 3),
                "Gamma": _format_decimal(raw.get("gamma"), 4),
                "Theta": _format_decimal(raw.get("theta"), 4),
                "Vega": _format_decimal(raw.get("vega"), 4),
                "Breakeven": format_price(raw.get("breakeven")),
                "Expected move": format_price(expected_move.get("amount")),
                "Earnings": _earnings_label(earnings),
                "Liquidity": public_text(raw.get("liquidity_quality"), "Unavailable", max_length=20).title(),
                "Fit": public_text("; ".join(str(item) for item in fit), "Unavailable", max_length=420),
                "Why lower": public_text("; ".join(str(item) for item in lower), "Top ranked", max_length=320),
            }
        )
    return rows


def _format_decimal(value: Any, digits: int) -> str:
    number = _number(value)
    return f"{number:.{digits}f}" if number is not None else "Unavailable"


def _earnings_label(value: Mapping[str, Any]) -> str:
    if not value or not bool(value.get("date_known")):
        return "Unavailable"
    date = public_text(value.get("date"), "No scheduled date", max_length=16)
    if value.get("exposed") is True:
        return f"Exposed · {date}"
    if value.get("exposed") is False:
        return f"Not exposed · {date}"
    return date


def build_home_snapshot(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Build the source-backed home model from persisted personal state."""

    state = state or {}
    raw_analyses = state.get("last_analyses") if isinstance(state.get("last_analyses"), Mapping) else {}
    analyses: list[dict[str, Any]] = []
    for ticker_key, raw in raw_analyses.items():
        if not isinstance(raw, Mapping):
            continue
        decision = raw.get("decision") if isinstance(raw.get("decision"), Mapping) else raw
        safe = _safe_decision(decision)
        safe["ticker"] = str(safe.get("ticker") or ticker_key).upper()
        safe["saved_at"] = raw.get("saved_at") or safe.get("saved_at")
        if isinstance(raw.get("provider_health"), Mapping):
            safe["provider_health"] = dict(raw.get("provider_health") or {})
        analyses.append(safe)
    analyses.sort(
        key=lambda item: (
            STATE_PRIORITY.get(str(item.get("state") or "BLOCKED").upper(), 9),
            -(_number(item.get("confidence")) or 0),
            str(item.get("ticker") or ""),
        )
    )

    newest = max(analyses, key=lambda item: str(item.get("saved_at") or ""), default={})
    market_context = newest.get("market_context") if isinstance(newest.get("market_context"), Mapping) else {}
    regime = str(market_context.get("regime") or "unavailable").strip().lower()
    regime_guidance = {
        "risk-on": "Conditions favor selective bullish exposure with defined risk.",
        "risk-off": "Conditions favor selective bearish exposure with defined risk.",
        "mixed": "Conditions favor reduced exposure and stricter confirmation.",
    }.get(regime, "Current SPY / QQQ regime is unavailable until a live refresh completes.")

    watchlist = [str(item).upper() for item in list(state.get("watchlist") or []) if str(item).strip()]
    by_ticker = {str(item.get("ticker") or "").upper(): item for item in analyses}
    watchlist_rows = [by_ticker[ticker] for ticker in watchlist if ticker in by_ticker]
    watchlist_rows.sort(
        key=lambda item: (
            STATE_PRIORITY.get(str(item.get("state") or "BLOCKED").upper(), 9),
            -(_number(item.get("confidence")) or 0),
        )
    )
    unreviewed = [ticker for ticker in watchlist if ticker not in by_ticker]

    recent = []
    for raw in list(state.get("recent_searches") or [])[:8]:
        if not isinstance(raw, Mapping):
            continue
        recent.append(
            {
                "ticker": public_text(raw.get("ticker") or raw.get("query"), "Unresolved", max_length=24),
                "searched_at": format_timestamp(raw.get("searched_at")),
            }
        )

    state_changes = []
    for raw in list(state.get("state_changes") or [])[-8:][::-1]:
        if not isinstance(raw, Mapping):
            continue
        transition = str(raw.get("transition") or raw.get("event_type") or "State changed")
        transition = transition.replace("_TO_", " → ").replace("_", " ").title()
        state_changes.append(
            {
                "ticker": public_text(raw.get("ticker"), "Ticker", max_length=24),
                "transition": public_text(transition, "State changed", max_length=80),
                "recorded_at": format_timestamp(raw.get("recorded_at")),
            }
        )

    catalysts = []
    for item in analyses:
        earnings = item.get("earnings_date")
        if earnings:
            catalysts.append(
                {
                    "ticker": public_text(item.get("ticker"), "Ticker", max_length=24),
                    "message": f"Earnings date: {public_text(earnings, 'Unavailable', max_length=20)}",
                }
            )
        elif str(item.get("state") or "").upper() in {"ENTER", "ARMED"}:
            catalysts.append(
                {
                    "ticker": public_text(item.get("ticker"), "Ticker", max_length=24),
                    "message": "Earnings date unavailable — entry remains gated.",
                }
            )

    provider_health = newest.get("provider_health") if isinstance(newest.get("provider_health"), Mapping) else {}
    data_label = provider_health.get("data_label") or newest.get("data_label")
    data_source = newest.get("data_source") or provider_health.get("provider")
    return {
        "regime": regime.upper().replace("-", " ") if regime != "unavailable" else "Unavailable",
        "regime_guidance": regime_guidance,
        "watchlist": watchlist_rows,
        "unreviewed_watchlist": unreviewed,
        "recent": recent,
        "state_changes": state_changes,
        "catalysts": catalysts[:6],
        "enter_candidates": [item for item in analyses if str(item.get("state")).upper() == "ENTER"][:5],
        "armed_candidates": [item for item in analyses if str(item.get("state")).upper() == "ARMED"][:5],
        "invalidated": [item for item in analyses if str(item.get("state")).upper() == "INVALIDATED"][:5],
        "positions": build_position_snapshot(state),
        "data_health": f"{public_text(data_source, 'Unavailable', max_length=48)} · {currentness_label(data_label)}",
        "last_refresh": format_timestamp(
            provider_health.get("timestamp") or newest.get("data_timestamp") or newest.get("saved_at")
        ),
    }


def build_position_snapshot(state: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """Build conservative position-management cards from saved plans and fresh analyses."""

    state = state or {}
    raw_positions = state.get("positions") if isinstance(state.get("positions"), Mapping) else {}
    analyses = state.get("last_analyses") if isinstance(state.get("last_analyses"), Mapping) else {}
    rows: list[dict[str, str]] = []
    for ticker_key, raw_position in raw_positions.items():
        if not isinstance(raw_position, Mapping):
            continue
        ticker = str(raw_position.get("ticker") or ticker_key).upper()
        raw_analysis = analyses.get(ticker) if isinstance(analyses.get(ticker), Mapping) else {}
        decision = raw_analysis.get("decision") if isinstance(raw_analysis.get("decision"), Mapping) else raw_analysis
        safe = _safe_decision(decision if isinstance(decision, Mapping) else {})
        direction = str(raw_position.get("direction") or safe.get("direction") or "unknown").lower()
        current = _number(safe.get("current_price"))
        entry = _number(raw_position.get("entry_price"))
        invalidation = _number(raw_position.get("invalidation"))
        target = _number(raw_position.get("target_1"))
        crossed = False
        if current is not None and invalidation is not None:
            crossed = current <= invalidation if direction == "bullish" else current >= invalidation if direction == "bearish" else False
        latest_state = str(safe.get("state") or "").upper()
        thesis = "Broken" if crossed or latest_state == "INVALIDATED" else "Intact" if current is not None else "Awaiting fresh data"
        progress = None
        if current is not None and entry is not None and target is not None and target != entry:
            signed = (current - entry) / (target - entry)
            progress = max(-2.0, min(2.0, signed)) * 100
        proximity = abs(current - invalidation) / current * 100 if current and invalidation is not None else None
        if thesis == "Broken":
            action = "Exit analysis — the saved thesis is invalidated; review before taking any action."
        elif proximity is not None and proximity <= 1.0:
            action = "Tighten-risk review — price is within 1% of invalidation."
        elif progress is not None and progress >= 90:
            action = "Reduce / tighten-risk review — price is near or through Target 1."
        elif thesis == "Intact":
            action = "Hold review — the saved thesis remains intact on current evidence."
        else:
            action = "Wait for fresh evidence before changing the position plan."
        rows.append(
            {
                "ticker": public_text(ticker, "Ticker", max_length=24),
                "thesis": thesis,
                "current": format_price(current),
                "target_progress": f"{progress:.0f}%" if progress is not None else "Unavailable",
                "invalidation_proximity": f"{proximity:.1f}%" if proximity is not None else "Unavailable",
                "action": action,
            }
        )
    return sorted(rows, key=lambda row: (row["thesis"] != "Broken", row["ticker"]))


def _unavailable_payload(query: str) -> dict[str, Any]:
    clean = re.sub(r"[^A-Za-z0-9.\-]", "", str(query or "").upper())[:14] or "UNRESOLVED"
    decision = unavailable_result(
        clean,
        "Current provider evidence is unavailable; no entry decision was made.",
    ).to_dict()
    symbol = normalize_tradingview_symbol(clean)
    return {
        "decision": decision,
        "chart_bars": [],
        "journal_bars": [],
        "resolved": {"ticker": clean},
        "tradingview_symbol": symbol,
        "tradingview_url": tradingview_chart_url(symbol, "15m"),
        "provider_health": {
            "provider": "Unavailable",
            "status": "unavailable",
            "data_label": "unavailable",
            "timestamp": None,
            "messages": [],
        },
        "fetched_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _cached_analysis(query: str, provider_key: str, *, include_options: bool = True) -> dict[str, Any]:
    fingerprint = hashlib.sha256(provider_key.encode("utf-8")).hexdigest()[:16]
    cache_key = f"{fingerprint}:{query.upper()}:{int(bool(include_options))}"

    def create() -> dict[str, Any]:
        return analyze_symbol(query, provider_key, include_options=include_options).to_dict()

    return _RESULT_CACHE.get_or_create(cache_key, create)


def _initialize_state(st: Any, config: Mapping[str, Any] | Any | None) -> tuple[dict[str, Any], Optional[AutopilotStateStore]]:
    session = st.session_state
    path = resolve_state_path(config)
    session_key = hashlib.sha256(path.encode("utf-8")).hexdigest() if path else "session-only"
    if session.get("_autopilot_state_identity") == session_key and "_autopilot_personal_state" in session:
        return session["_autopilot_personal_state"], session.get("_autopilot_state_store")

    store: Optional[AutopilotStateStore] = None
    state = default_state()
    mode = "session-only"
    if path:
        try:
            allowed_root = _config_value(config, "AUTOPILOT_STATE_ALLOWED_ROOT")
            store = AutopilotStateStore(path, allowed_root=allowed_root or None)
            state = store.load()
            mode = "persistent"
        except Exception:
            store = None
            state = default_state()
            mode = "session-only"
    session["_autopilot_state_identity"] = session_key
    session["_autopilot_state_store"] = store
    session["_autopilot_personal_state"] = state
    session["_autopilot_persistence_mode"] = mode
    return state, store


def _safe_analysis_record(payload: Mapping[str, Any]) -> dict[str, Any]:
    decision = _safe_decision(payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {})
    plan = decision.get("plan") if isinstance(decision.get("plan"), Mapping) else {}
    market = decision.get("market_context") if isinstance(decision.get("market_context"), Mapping) else {}
    options = decision.get("options") if isinstance(decision.get("options"), Mapping) else {}
    timeframes = decision.get("timeframes") if isinstance(decision.get("timeframes"), Mapping) else {}
    health = payload.get("provider_health") if isinstance(payload.get("provider_health"), Mapping) else {}
    return {
        "ticker": decision.get("ticker"),
        "name": decision.get("name"),
        "exchange": decision.get("exchange"),
        "verdict": decision.get("verdict"),
        "state": decision.get("state"),
        "direction": decision.get("direction"),
        "confidence": decision.get("confidence"),
        "grade": decision.get("grade"),
        "current_price": decision.get("current_price"),
        "market_status": decision.get("market_status"),
        "data_timestamp": decision.get("data_timestamp"),
        "data_label": decision.get("data_label"),
        "data_source": decision.get("data_source"),
        "entry_conditions_satisfied": bool(decision.get("entry_conditions_satisfied")),
        "plan": deepcopy(dict(plan)),
        "market_context": deepcopy(dict(market)),
        "earnings_date": decision.get("earnings_date"),
        "options": deepcopy(dict(options)),
        "timeframes": {
            str(label): {"direction": value.get("direction")}
            for label, value in timeframes.items()
            if isinstance(value, Mapping)
        },
        "tradingview_symbol": payload.get("tradingview_symbol"),
        "provider_health": {
            "provider": health.get("provider"),
            "status": health.get("status"),
            "data_label": health.get("data_label"),
            "timestamp": health.get("timestamp"),
        },
    }


def _monitoring_snapshot(record: Mapping[str, Any]) -> dict[str, Any]:
    plan = record.get("plan") if isinstance(record.get("plan"), Mapping) else {}
    return {
        "state": record.get("state"),
        "direction": record.get("direction"),
        "current_price": record.get("current_price"),
        "confidence": record.get("confidence"),
        "invalidation": plan.get("invalidation"),
        "target_1": plan.get("target_1"),
        "target_2": plan.get("target_2"),
        "target_3": plan.get("stretch_target"),
    }


def _persist_analysis(st: Any, payload: Mapping[str, Any]) -> None:
    session = st.session_state
    record = _safe_analysis_record(payload)
    ticker = str(record.get("ticker") or "").upper()
    if not ticker or ticker == "UNRESOLVED":
        return
    journal_bars = [dict(row) for row in list(payload.get("journal_bars") or []) if isinstance(row, Mapping)]
    store = session.get("_autopilot_state_store")
    if store is not None:
        try:
            store.remember_search(ticker, ticker=ticker)
            store.save_analysis(ticker, record)
            store.record_monitoring_update(ticker, _monitoring_snapshot(record))
            store.update(
                lambda state: (
                    _append_meaningful_verdict(state, record),
                    _update_calibration_outcomes(state, ticker, journal_bars) if journal_bars else None,
                )
            )
            session["_autopilot_personal_state"] = store.load()
            return
        except Exception:
            session["_autopilot_state_store"] = None
            session["_autopilot_persistence_mode"] = "session-only"
    _session_save_analysis(session, ticker, record)
    state = deepcopy(session.get("_autopilot_personal_state") or default_state())
    _append_meaningful_verdict(state, record)
    if journal_bars:
        _update_calibration_outcomes(state, ticker, journal_bars)
    session["_autopilot_personal_state"] = state


def _append_meaningful_verdict(state: MutableMapping[str, Any], record: Mapping[str, Any]) -> None:
    """Snapshot first and changed provider-backed verdicts for later calibration."""

    if str(record.get("data_label") or "").lower() in {"unavailable", "stale", ""}:
        return
    ticker = str(record.get("ticker") or "").upper()
    setup_state = str(record.get("state") or "").upper()
    journal = state.setdefault("journal", [])
    previous = next(
        (
            item
            for item in reversed(journal)
            if isinstance(item, Mapping)
            and item.get("action") == "VERDICT"
            and str(item.get("ticker") or "").upper() == ticker
        ),
        None,
    )
    if previous and str(previous.get("state") or "").upper() == setup_state:
        return
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    journal.append(
        {
            "action": "VERDICT",
            "ticker": ticker,
            "saved_at": timestamp,
            "verdict": record.get("verdict"),
            "state": setup_state,
            "direction": record.get("direction"),
            "current_price": record.get("current_price"),
            "confidence": record.get("confidence"),
            "grade": record.get("grade"),
            "plan": deepcopy(record.get("plan") or {}),
            "market_context": deepcopy(record.get("market_context") or {}),
            "timeframes": deepcopy(record.get("timeframes") or {}),
        }
    )
    del journal[:-2000]


def _update_calibration_outcomes(
    state: MutableMapping[str, Any],
    ticker: str,
    completed_daily_bars: list[dict[str, Any]],
) -> None:
    """Refresh journal evidence without changing strategy thresholds."""

    calibration = state.setdefault("calibration", {"results": [], "updated_at": None})
    existing = [dict(item) for item in list(calibration.get("results") or []) if isinstance(item, Mapping)]
    by_key = {str(item.get("source_key") or ""): item for item in existing if item.get("source_key")}
    changed = False
    for entry in list(state.get("journal") or []):
        if not isinstance(entry, Mapping) or str(entry.get("ticker") or "").upper() != ticker:
            continue
        timestamp = next(
            (
                entry.get(key)
                for key in ("entered_at", "recorded_at", "saved_at", "closed_at", "data_timestamp")
                if entry.get(key)
            ),
            None,
        )
        if not timestamp:
            continue
        source_key = hashlib.sha256(f"{ticker}:{timestamp}:{entry.get('action') or entry.get('tracking_status')}".encode("utf-8")).hexdigest()[:20]
        try:
            outcome = evaluate_journal_outcome(entry, completed_daily_bars)
        except (TypeError, ValueError):
            continue
        outcome["source_key"] = source_key
        by_key[source_key] = outcome
        changed = True
    if changed:
        calibration["results"] = list(by_key.values())[-500:]
        calibration["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _session_save_analysis(session: MutableMapping[str, Any], ticker: str, record: Mapping[str, Any]) -> None:
    state = deepcopy(session.get("_autopilot_personal_state") or default_state())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    record_copy = deepcopy(dict(record))
    record_copy["ticker"] = ticker
    record_copy["saved_at"] = now
    state.setdefault("last_analyses", {})[ticker] = record_copy
    state["recent_searches"] = [
        item for item in state.get("recent_searches", []) if item.get("ticker") != ticker
    ]
    state["recent_searches"].insert(0, {"query": ticker, "ticker": ticker, "searched_at": now})
    del state["recent_searches"][50:]
    previous = state.setdefault("monitoring", {}).get(ticker)
    current = _monitoring_snapshot(record_copy)
    if previous and previous.get("state") != current.get("state"):
        transition = f"{previous.get('state')}_TO_{current.get('state')}"
        state.setdefault("state_changes", []).append(
            {"event_type": "state_change", "ticker": ticker, "transition": transition, "recorded_at": now}
        )
        del state["state_changes"][:-1000]
    current["observed_at"] = now
    state["monitoring"][ticker] = current
    state["updated_at"] = now
    session["_autopilot_personal_state"] = state


def _tracking_snapshot(payload: Mapping[str, Any]) -> dict[str, Any]:
    decision = _safe_decision(payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {})
    plan = decision.get("plan") if isinstance(decision.get("plan"), Mapping) else {}
    market = decision.get("market_context") if isinstance(decision.get("market_context"), Mapping) else {}
    options = decision.get("options") if isinstance(decision.get("options"), Mapping) else {}
    timeframes = decision.get("timeframes") if isinstance(decision.get("timeframes"), Mapping) else {}
    recommendation = options.get("recommendation") if isinstance(options.get("recommendation"), Mapping) else None
    chart_rows = []
    for row in list(payload.get("chart_bars") or [])[-180:]:
        if isinstance(row, Mapping):
            chart_rows.append(
                {
                    key: row.get(key)
                    for key in ("timestamp", "open", "high", "low", "close", "volume", "vwap")
                    if row.get(key) is not None
                }
            )
    return {
        "original_verdict": decision.get("verdict"),
        "original_state": decision.get("state"),
        "direction": decision.get("direction"),
        "setup_type": plan.get("setup_type"),
        "trigger": plan.get("trigger"),
        "entry_low": plan.get("entry_low"),
        "entry_high": plan.get("entry_high"),
        "invalidation": plan.get("invalidation"),
        "target_1": plan.get("target_1"),
        "target_2": plan.get("target_2"),
        "stretch_target": plan.get("stretch_target"),
        "market_context": deepcopy(dict(market)),
        "confidence": decision.get("confidence"),
        "grade": decision.get("grade"),
        "timeframes": {
            str(label): {"direction": value.get("direction")}
            for label, value in timeframes.items()
            if isinstance(value, Mapping)
        },
        "suggested_contract": deepcopy(dict(recommendation)) if recommendation else None,
        "original_chart": {
            "timeframe": "15m",
            "tradingview_symbol": payload.get("tradingview_symbol"),
            "bars": chart_rows,
        },
    }


def _apply_tracking_action(st: Any, payload: Mapping[str, Any], action: str) -> str:
    session = st.session_state
    state = deepcopy(session.get("_autopilot_personal_state") or default_state())
    decision = _safe_decision(payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {})
    ticker = str(decision.get("ticker") or "").upper()
    details = _tracking_snapshot(payload)
    store = session.get("_autopilot_state_store")
    if store is not None:
        try:
            if action == "entered":
                store.mark_entered(ticker, decision.get("current_price"), details=details)
            elif action == "watching":
                store.mark_watching(ticker, details)
            elif action == "passed":
                store.mark_passed(ticker, details)
            elif action == "closed":
                store.close_trade(ticker, decision.get("current_price"))
            session["_autopilot_personal_state"] = store.load()
            return "persistent"
        except Exception:
            session["_autopilot_state_store"] = None
            session["_autopilot_persistence_mode"] = "session-only"
    _session_tracking_action(state, ticker, action, details, decision.get("current_price"))
    session["_autopilot_personal_state"] = state
    return "session-only"


def _set_high_value_alert(st: Any, ticker: str, enabled: bool) -> str:
    """Enable only meaningful in-app setup transitions; never create an external alert."""

    session = st.session_state
    store = session.get("_autopilot_state_store")
    details = {"events": ["FORMING_TO_ARMED", "ARMED_TO_ENTER", "ARMED_TO_INVALIDATED", "target_reached", "invalidation_reached"]}
    if store is not None:
        try:
            store.set_alert_enabled(ticker, "high_value_state_change", enabled, details=details)
            session["_autopilot_personal_state"] = store.load()
            return "persistent"
        except Exception:
            session["_autopilot_state_store"] = None
            session["_autopilot_persistence_mode"] = "session-only"
    state = deepcopy(session.get("_autopilot_personal_state") or default_state())
    key = f"{ticker}:high_value_state_change"
    state.setdefault("alerts", {}).setdefault("enabled", {})[key] = {
        **details,
        "ticker": ticker,
        "alert_type": "high_value_state_change",
        "enabled": bool(enabled),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    session["_autopilot_personal_state"] = state
    return "session-only"


def _session_tracking_action(
    state: MutableMapping[str, Any],
    ticker: str,
    action: str,
    details: Mapping[str, Any],
    price: Any,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    plans = state.setdefault("saved_plans", {})
    plan_key = f"{ticker}:tracking"
    plan = deepcopy(plans.get(plan_key) or {"plan_id": plan_key, "ticker": ticker})
    plan.update(deepcopy(dict(details)))
    plan.update({"tracking_status": action.upper(), "status_changed_at": now})
    plans[plan_key] = plan
    entry = {"action": action.upper(), "ticker": ticker, "recorded_at": now}
    state.setdefault("activity_log", []).append(entry)
    del state["activity_log"][:-1000]
    if action == "watching":
        watchlist = state.setdefault("watchlist", [])
        if ticker not in watchlist:
            watchlist.append(ticker)
    elif action == "entered":
        position = deepcopy(dict(details))
        position.update({"ticker": ticker, "status": "OPEN", "entered_at": now})
        if _number(price) is not None:
            position["entry_price"] = float(price)
        state.setdefault("positions", {})[ticker] = position
        state.setdefault("journal", []).append({"action": "ENTERED", **deepcopy(position)})
    elif action == "passed":
        state.setdefault("journal", []).append(entry)
    elif action == "closed":
        position = state.setdefault("positions", {}).pop(ticker, None)
        if position:
            closed = deepcopy(position)
            closed.update({"action": "CLOSED", "status": "CLOSED", "closed_at": now})
            if _number(price) is not None:
                closed["exit_price"] = float(price)
            state.setdefault("journal", []).append(closed)
    del state.setdefault("journal", [])[:-2000]
    state["updated_at"] = now


def _render_header(st: Any) -> None:
    st.markdown(
        """
        <div class="ap-kicker">Trading Autopilot</div>
        <h1 class="ap-title">Your decision cockpit</h1>
        <p class="ap-subtitle">Search once. Get the market context, chart, trade plan and a clear next action.</p>
        """,
        unsafe_allow_html=True,
    )


def _render_search(st: Any) -> tuple[bool, str]:
    with st.form("autopilot_ticker_search", clear_on_submit=False):
        query = st.text_input(
            "Ticker",
            value=str(st.session_state.get("_autopilot_search_query") or ""),
            placeholder="Ticker or exchange:ticker — press Enter",
            max_chars=32,
            label_visibility="collapsed",
        )
        submitted = st.form_submit_button("Analyze", type="primary", width="stretch")
    return submitted, str(query or "").strip()


def _render_home(st: Any, state: Mapping[str, Any]) -> None:
    model = build_home_snapshot(state)
    st.markdown('<div class="ap-section-title">Today at a glance</div>', unsafe_allow_html=True)
    cells = [
        ("SPY / QQQ regime", model["regime"]),
        ("Exposure posture", model["regime_guidance"]),
        ("Data health", model["data_health"]),
        ("Last refresh", model["last_refresh"]),
    ]
    st.markdown(_html_grid(cells), unsafe_allow_html=True)

    left, middle, right = st.columns(3, gap="medium")
    with left:
        st.markdown("#### Ranked watchlist")
        if model["watchlist"]:
            st.markdown(_ranked_setup_rows(model["watchlist"]), unsafe_allow_html=True)
        elif model["unreviewed_watchlist"]:
            st.caption("Waiting for a current review: " + ", ".join(model["unreviewed_watchlist"][:8]))
        else:
            st.caption("Mark a setup as I’m watching to build your list.")
    with middle:
        st.markdown("#### Best current states")
        candidates = [*model["enter_candidates"], *model["armed_candidates"]]
        if candidates:
            st.markdown(_ranked_setup_rows(candidates[:6]), unsafe_allow_html=True)
        else:
            st.caption("No current ENTER or ARMED setup is saved.")
    with right:
        st.markdown("#### Recent searches")
        if model["recent"]:
            for item in model["recent"][:6]:
                st.markdown(f"**{public_text(item['ticker'])}**  ")
                st.caption(item["searched_at"])
        else:
            st.caption("Your provider-backed searches will appear here.")

    lower_left, lower_right = st.columns(2, gap="medium")
    with lower_left:
        st.markdown("#### Meaningful state changes")
        if model["state_changes"]:
            for item in model["state_changes"][:6]:
                st.markdown(f"**{item['ticker']}** · {item['transition']}")
                st.caption(item["recorded_at"])
        else:
            st.caption("No high-value state change has been recorded yet.")
    with lower_right:
        st.markdown("#### Earnings and catalysts")
        if model["catalysts"]:
            for item in model["catalysts"]:
                st.markdown(f"**{item['ticker']}** · {item['message']}")
        else:
            st.caption("No provider-backed catalyst warning is currently saved. Verify earnings before entry.")

    if model["positions"]:
        st.markdown("#### Open position management")
        for item in model["positions"]:
            st.markdown(
                _html_grid(
                    [
                        ("Ticker", item["ticker"]),
                        ("Thesis", item["thesis"]),
                        ("Current price", item["current"]),
                        ("Target 1 progress", item["target_progress"]),
                        ("Invalidation distance", item["invalidation_proximity"]),
                        ("Current analysis", item["action"]),
                    ]
                ),
                unsafe_allow_html=True,
            )
    if model["invalidated"]:
        st.markdown("#### Recently invalidated")
        st.markdown(_ranked_setup_rows(model["invalidated"]), unsafe_allow_html=True)

    st.caption("Decision support only. No orders are placed.")


def _render_decision_card(st: Any, brief: Mapping[str, Any]) -> None:
    symbol_line = brief["ticker"]
    if brief["exchange"] != "Exchange unresolved":
        symbol_line += f" · {brief['exchange']}"
    st.markdown(
        f"""
        <section class="ap-decision {html.escape(str(brief['verdict_class']))}">
          <div class="ap-decision-top">
            <div>
              <div class="ap-symbol">{html.escape(str(symbol_line))}</div>
              <div class="ap-security">{html.escape(str(brief['name']))}</div>
            </div>
            <div class="ap-verdict">{html.escape(str(brief['verdict']))}</div>
          </div>
          <div class="ap-now">{html.escape(str(brief['do_now']))}</div>
          <div class="ap-meta">{html.escape(str(brief['source']))} · {html.escape(str(brief['currentness']))} · {html.escape(str(brief['timestamp']))}</div>
        </section>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        _html_grid(
            [
                ("Current setup state", brief["state"]),
                ("Direction", brief["direction"]),
                ("Confidence", brief["confidence"]),
                ("Setup grade", brief["grade"]),
                ("Current price", brief["current_price"]),
                ("Market status", brief["market_status"]),
                ("Setup type", brief["setup_type"]),
                ("Entry conditions satisfied", brief["entry_satisfied"]),
            ]
        ),
        unsafe_allow_html=True,
    )
    st.caption("Confidence: " + str(brief["confidence_explanation"]))


def _render_trade_plan(st: Any, brief: Mapping[str, Any]) -> None:
    st.markdown('<div class="ap-section-title">Trade plan</div>', unsafe_allow_html=True)
    st.markdown(
        _html_grid(
            [
                ("Exact trigger", brief["trigger"]),
                ("Entry zone", brief["entry_zone"]),
                ("Invalidation / stop", brief["invalidation"]),
                ("Target 1", brief["target_1"]),
                ("Target 2", brief["target_2"]),
                ("Stretch target", brief["stretch_target"]),
                ("Estimated reward-to-risk", brief["reward_to_risk"]),
                ("Expected trade horizon", brief["horizon"]),
            ]
        ),
        unsafe_allow_html=True,
    )
    st.markdown("**Three strongest reasons**")
    for reason in brief["reasons"]:
        st.markdown("- " + str(reason))
    st.markdown(_callout("Primary risk", brief["primary_risk"], "risk"), unsafe_allow_html=True)
    st.markdown(_callout("What would upgrade the setup", brief["upgrade"], "upgrade"), unsafe_allow_html=True)
    st.markdown(_callout("What would invalidate it", brief["invalidate"], "risk"), unsafe_allow_html=True)


def _render_tracking_actions(st: Any, payload: Mapping[str, Any], brief: Mapping[str, Any]) -> None:
    st.markdown('<div class="ap-section-title">Track this plan</div>', unsafe_allow_html=True)
    state = st.session_state.get("_autopilot_personal_state") or {}
    positions = state.get("positions") if isinstance(state.get("positions"), Mapping) else {}
    has_position = brief["ticker"] in positions
    has_price = brief["current_price"] != "Unavailable"
    columns = st.columns(4, gap="small")
    clicked: Optional[str] = None
    with columns[0]:
        if st.button("I entered", key=f"cockpit_entered_{brief['ticker']}", width="stretch", disabled=not has_price):
            clicked = "entered"
    with columns[1]:
        if st.button("I’m watching", key=f"cockpit_watching_{brief['ticker']}", width="stretch"):
            clicked = "watching"
    with columns[2]:
        if st.button("I passed", key=f"cockpit_passed_{brief['ticker']}", width="stretch"):
            clicked = "passed"
    with columns[3]:
        if st.button(
            "Close trade",
            key=f"cockpit_closed_{brief['ticker']}",
            width="stretch",
            disabled=not has_position,
        ):
            clicked = "closed"
    if clicked:
        mode = _apply_tracking_action(st, payload, clicked)
        messages = {
            "entered": "Entry snapshot saved. This did not place an order.",
            "watching": "Setup added to your watchlist.",
            "passed": "Pass decision saved for your journal and calibration.",
            "closed": "Tracked trade closed and moved to your journal.",
        }
        st.success(messages[clicked])
        if mode == "session-only":
            st.caption("Saved for this session only.")

    refreshed_state = st.session_state.get("_autopilot_personal_state") or state
    enabled_alerts = (
        refreshed_state.get("alerts", {}).get("enabled", {})
        if isinstance(refreshed_state.get("alerts"), Mapping)
        else {}
    )
    alert_key = f"{brief['ticker']}:high_value_state_change"
    alert_enabled = bool(
        isinstance(enabled_alerts.get(alert_key), Mapping)
        and enabled_alerts.get(alert_key, {}).get("enabled")
    )
    alert_label = "Disable high-value in-app alerts" if alert_enabled else "Enable high-value in-app alerts"
    if st.button(alert_label, key=f"cockpit_alert_{brief['ticker']}", width="stretch"):
        mode = _set_high_value_alert(st, str(brief["ticker"]), not alert_enabled)
        st.success("High-value in-app alerts enabled." if not alert_enabled else "High-value in-app alerts disabled.")
        if mode == "session-only":
            st.caption("Alert preference is saved for this session only.")
    st.caption("Alerts are limited to setup-state, target, invalidation, earnings/news thesis changes; ordinary price movement is ignored.")


def _render_chart(st: Any, payload: Mapping[str, Any], brief: Mapping[str, Any]) -> None:
    st.markdown('<div class="ap-section-title">Annotated setup chart</div>', unsafe_allow_html=True)
    rows = payload.get("chart_bars") if isinstance(payload.get("chart_bars"), list) else []
    if rows:
        try:
            figure = build_autopilot_chart(
                rows,
                brief["safe_decision"],
                title=f"{brief['ticker']} · 15m decision chart",
            )
            st.plotly_chart(
                figure,
                width="stretch",
                config={"displaylogo": False, "scrollZoom": True, "responsive": True},
            )
        except Exception:
            st.info("The annotated chart is unavailable for this response. No visual level was inferred.")
    else:
        st.info("Chart unavailable because current price bars were not returned. No visual level was inferred.")

    symbol = normalize_tradingview_symbol(payload.get("tradingview_symbol") or brief["ticker"])
    url = tradingview_chart_url(symbol, "15m")
    st.link_button(
        f"Open {brief['ticker']} in my TradingView · 15m",
        url,
        width="stretch",
    )
    st.caption(f"Opens {public_text(symbol, brief['ticker'], max_length=72)} at the 15m interval.")


def _render_options(st: Any, decision: Mapping[str, Any]) -> None:
    st.markdown('<div class="ap-section-title">Options decision</div>', unsafe_allow_html=True)
    options = decision.get("options") if isinstance(decision.get("options"), Mapping) else {}
    rows = options_table_rows(options)
    status = str(options.get("status") or "unavailable").strip().upper()
    if not rows:
        messages = {
            "WAIT": "Options: wait — the underlying setup is not confirmed, so no contract is recommended.",
            "PASS": "Options: pass — no complete contract cleared every liquidity, freshness and risk gate.",
            "UNAVAILABLE": "Options: unavailable — a current, complete chain was not returned.",
        }
        st.info(messages.get(status, "Options: unavailable — a current, complete chain was not returned."))
        return
    if status == "RECOMMEND":
        st.success("A current contract cleared the underlying ENTER gate and the contract-quality gates.")
    elif status == "WAIT":
        st.warning("Contract rankings are provisional only. Wait for the underlying setup to reach ENTER.")
    else:
        st.info("No contract is recommended. The table is shown only to explain why the chain did not qualify.")
    st.markdown(_options_html(rows), unsafe_allow_html=True)
    st.caption("Missing Greeks, liquidity, IV rank or earnings data remain Unavailable; they are never estimated as observed values.")


def _render_breakdown(st: Any, decision: Mapping[str, Any]) -> None:
    st.markdown('<div class="ap-section-title">Complete analysis</div>', unsafe_allow_html=True)
    safe = _safe_decision(decision)
    breakdown = safe.get("full_breakdown") if isinstance(safe.get("full_breakdown"), Mapping) else {}
    for key, title in BREAKDOWN_SECTIONS:
        with st.expander(title, expanded=False):
            text = public_text(
                breakdown.get(key),
                "This section is unavailable because the current provider response did not contain enough evidence.",
                max_length=1_800,
            )
            st.write(text)


def _render_advanced(st: Any, payload: Mapping[str, Any], state: Mapping[str, Any], brief: Mapping[str, Any]) -> None:
    with st.expander("Advanced", expanded=False):
        st.markdown("**Methodology shown on the app-native chart**")
        st.write("9 EMA · 21 WMA · 50 WMA · 200 WMA · 200 SMA · MACD 12/26/9")
        st.caption("The 21-period line is a weighted moving average, not an exponential moving average.")
        health = payload.get("provider_health") if isinstance(payload.get("provider_health"), Mapping) else {}
        st.markdown("**Source health**")
        st.write(
            f"{public_text(health.get('provider') or brief['source'], 'Unavailable', max_length=48)} · "
            f"{currentness_label(health.get('data_label'))} · "
            f"{format_timestamp(health.get('timestamp'))}"
        )
        persistence = st.session_state.get("_autopilot_persistence_mode")
        st.write("Personal state: saved across sessions" if persistence == "persistent" else "Personal state: this session only")
        st.markdown("**Interactive TradingView view**")
        symbol = normalize_tradingview_symbol(payload.get("tradingview_symbol") or brief["ticker"])
        watchlist = list(state.get("watchlist") or []) if isinstance(state, Mapping) else []
        try:
            from streamlit.components.v1 import html as render_component_html

            render_component_html(
                tradingview_widget_html(symbol, "15m", watchlist=watchlist, compact=True),
                height=560,
                scrolling=False,
            )
        except Exception:
            st.info("The embedded TradingView view is unavailable. Use the 15m handoff above.")
        st.caption(
            "The official chart handoff opens the correct symbol and interval. Private TradingView layouts "
            "and drawings remain in your signed-in TradingView session and are not claimed as synchronized."
        )
        outcomes = (
            list(state.get("calibration", {}).get("results", []))
            if isinstance(state.get("calibration"), Mapping)
            else []
        )
        st.markdown("**Automatic journal and calibration**")
        if outcomes:
            try:
                summary = aggregate_calibration([item for item in outcomes if isinstance(item, Mapping)])
            except (TypeError, ValueError):
                summary = {}
            overall = summary.get("overall") if isinstance(summary.get("overall"), Mapping) else {}
            status_counts = overall.get("status_counts") if isinstance(overall.get("status_counts"), Mapping) else {}
            st.write(
                f"{int(overall.get('count') or 0)} evaluated snapshot(s) · "
                f"{int(status_counts.get('target_first') or 0)} target-first · "
                f"{int(status_counts.get('invalidation_first') or 0)} invalidation-first"
            )
            st.caption("MFE, MAE, trigger chronology, and 1/3/5/10/20-day outcomes use only later completed bars. Thresholds never change automatically.")
        else:
            st.caption("Outcome evidence will appear after a tracked setup receives later completed Daily bars. Thresholds never change automatically.")


def _html_grid(items: list[tuple[str, Any]]) -> str:
    cells = []
    for label, value in items:
        cells.append(
            '<div class="ap-cell">'
            f'<div class="ap-label">{html.escape(public_text(label, "Label", max_length=80))}</div>'
            f'<div class="ap-value">{html.escape(public_text(value, "Unavailable", max_length=520))}</div>'
            "</div>"
        )
    return '<div class="ap-grid">' + "".join(cells) + "</div>"


def _callout(label: str, value: Any, class_name: str) -> str:
    return (
        f'<div class="ap-callout {html.escape(class_name)}">'
        f'<div class="ap-label">{html.escape(label)}</div>'
        f'<div>{html.escape(public_text(value, "Unavailable", max_length=520))}</div>'
        "</div>"
    )


def _ranked_setup_rows(rows: list[Mapping[str, Any]]) -> str:
    output = ['<div class="ap-home-card">']
    for row in rows:
        ticker = public_text(str(row.get("ticker") or "Ticker").upper(), "Ticker", max_length=24)
        state = public_text(str(row.get("state") or "Unavailable").upper(), "Unavailable", max_length=24)
        confidence = format_percent(row.get("confidence"), digits=0)
        grade = public_text(row.get("grade"), "—", max_length=8)
        output.append(
            '<div class="ap-rank-row">'
            f"<strong>{html.escape(ticker)}</strong>"
            f'<span>{html.escape(state)} · {html.escape(confidence)} · {html.escape(grade)}</span>'
            "</div>"
        )
    output.append("</div>")
    return "".join(output)


def _options_html(rows: list[Mapping[str, str]]) -> str:
    if not rows:
        return ""
    headers = list(rows[0].keys())
    head = "".join(f"<th>{html.escape(header)}</th>" for header in headers)
    body = "".join(
        "<tr>" + "".join(f"<td>{html.escape(str(row.get(header, 'Unavailable')))}</td>" for header in headers) + "</tr>"
        for row in rows
    )
    return f'<div class="ap-table-wrap"><table class="ap-table"><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>'


def _provider_key(config: Mapping[str, Any] | Any | None) -> str:
    configured = _config_value(config, "POLYGON_API_KEY")
    value = configured if configured not in (None, "") else os.environ.get("POLYGON_API_KEY")
    return str(value or "").strip()


def _valid_query(query: str) -> bool:
    if not query or len(query) > 32 or query.count(":") > 1:
        return False
    return bool(re.fullmatch(r"\$?[A-Za-z0-9][A-Za-z0-9.\-]*(?::[A-Za-z0-9][A-Za-z0-9.\-]*)?", query))


def _run_search(st: Any, query: str, config: Mapping[str, Any] | Any | None) -> dict[str, Any]:
    st.session_state["_autopilot_search_query"] = query
    if not _valid_query(query):
        st.warning("Enter one valid ticker, optionally qualified by exchange, such as NASDAQ:AAPL.")
        return _unavailable_payload(query)
    provider_key = _provider_key(config)
    with st.status("Analyzing the setup…", expanded=True) as progress:
        st.write("Resolving the symbol and exchange")
        if not provider_key:
            payload = _unavailable_payload(query)
            st.write("Current market evidence is unavailable; the result is safely gated to PASS")
            progress.update(label="Analysis unavailable — PASS", state="complete", expanded=False)
            return payload
        st.write("Refreshing market, sector and multi-timeframe evidence")
        try:
            payload = _cached_analysis(query, provider_key, include_options=True)
        except AutopilotServiceError:
            payload = _unavailable_payload(query)
        except Exception:
            payload = _unavailable_payload(query)
        st.write("Building the decision, chart and options review")
        decision = _safe_decision(payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {})
        label = verdict_label(decision.get("verdict"))
        progress.update(label=f"Analysis complete · {label}", state="complete", expanded=False)
        return payload


def _prepare_home_context(st: Any, config: Mapping[str, Any] | Any | None) -> None:
    """Warm market context and refresh a bounded watchlist once per app session."""

    session = st.session_state
    state = session.get("_autopilot_personal_state") or {}
    if session.get("_autopilot_home_refresh_attempted"):
        return
    session["_autopilot_home_refresh_attempted"] = True
    provider_key = _provider_key(config)
    if not provider_key:
        return
    watchlist = [str(item).upper() for item in list(state.get("watchlist") or []) if str(item).strip()]
    symbols = list(dict.fromkeys(["SPY", *watchlist[:5]]))
    with st.spinner("Refreshing market regime and tracked setups…"):
        for symbol in symbols:
            try:
                payload = _cached_analysis(symbol, provider_key, include_options=False)
            except Exception:
                continue
            _persist_analysis(st, payload)


def render_cockpit(st: Any, config: Mapping[str, Any] | Any | None = None) -> None:
    """Render the complete one-search cockpit using Streamlit 1.50-compatible APIs."""

    st.markdown(COCKPIT_CSS, unsafe_allow_html=True)
    state, _ = _initialize_state(st, config)
    _render_header(st)
    submitted, query = _render_search(st)

    if submitted:
        payload = _run_search(st, query, config)
        st.session_state["_autopilot_active_result"] = payload
        _persist_analysis(st, payload)

    payload = st.session_state.get("_autopilot_active_result")
    if not isinstance(payload, Mapping):
        _prepare_home_context(st, config)
        state = st.session_state.get("_autopilot_personal_state") or state
        _render_home(st, state)
        return

    decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
    brief = build_decision_brief(decision)
    _render_decision_card(st, brief)
    _render_trade_plan(st, brief)
    _render_tracking_actions(st, payload, brief)
    _render_chart(st, payload, brief)
    _render_options(st, brief["safe_decision"])
    _render_breakdown(st, brief["safe_decision"])
    state = st.session_state.get("_autopilot_personal_state") or state
    _render_advanced(st, payload, state, brief)
    st.caption("Trading Autopilot provides decision support only. It does not connect to a broker or place orders.")


__all__ = [
    "BREAKDOWN_SECTIONS",
    "build_decision_brief",
    "build_home_snapshot",
    "currentness_label",
    "format_percent",
    "format_price",
    "format_ratio",
    "format_timestamp",
    "options_table_rows",
    "public_text",
    "render_cockpit",
    "resolve_state_path",
    "verdict_label",
]
