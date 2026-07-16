"""Premium, one-search Streamlit cockpit for Trading Autopilot.

The renderer deliberately accepts the Streamlit module as an argument.  That
keeps this module importable in lightweight test and CLI environments while
the existing dashboard entry point remains responsible for page setup.
"""

from __future__ import annotations

import base64
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Mapping, MutableMapping, Optional

from autopilot_chart import build_autopilot_chart
from autopilot_engine import (
    analyze_timeframe,
    assess_source_freshness,
    revalidate_decision_freshness,
)
from autopilot_journal import aggregate_calibration, evaluate_journal_outcome
from autopilot_service import (
    AutopilotServiceError,
    TTLCache,
    analyze_symbol,
    load_chart_bars,
    unavailable_result,
)
from autopilot_state import AutopilotStateStore, default_state
from presentation_export import build_presentation_payload, presentation_pdf_bytes
from polygon_provider import recent_provider_observations
from timeframes import TIMEFRAME_LABELS, get_timeframe_spec, normalize_timeframe
from tradingview_integration import (
    normalize_tradingview_symbol,
    tradingview_chart_url,
    tradingview_market_context_html,
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
    re.compile(r"/(?:Users|home|private|tmp|var|etc|mount|mnt)(?:/|\b)", re.IGNORECASE),
    re.compile(r"[A-Za-z]:\\"),
    re.compile(r"\bTraceback\b|\b(?:[A-Za-z]+Error|Exception):", re.IGNORECASE),
    re.compile(
        r"\b(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|password)\s*[:=]",
        re.IGNORECASE,
    ),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
)

_RESULT_CACHE = TTLCache(ttl_seconds=120, max_items=32)
_CHART_CACHE = TTLCache(ttl_seconds=300, max_items=96)

DEFAULT_TIMEFRAME = "15m"
MULTI_TIMEFRAME_LABELS: tuple[str, ...] = ("1D", "4H", "1H", "15m")
APP_DISPLAY_VERSION = "2.1.0"
_RELEASE_STATE_PATH = Path(__file__).resolve().parents[1] / "deploy" / ".cloud-mirror-state.json"


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
    max-width: 1500px;
    padding-top: 0.85rem;
    padding-bottom: 5rem;
  }
  [data-testid="stSidebar"] { display: none; }
  html, body, .stApp { overflow-x: hidden; }
  header[data-testid="stHeader"] { background: transparent; }
  .ap-topbar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 1rem;
    border-bottom: 1px solid var(--ap-line);
    padding: 0.2rem 0 0.75rem;
    margin-bottom: 0.8rem;
  }
  .ap-brand {
    display: flex;
    align-items: center;
    gap: 0.68rem;
    color: var(--ap-text);
    font-weight: 820;
    letter-spacing: -0.015em;
  }
  .ap-brand-mark {
    display: grid;
    place-items: center;
    width: 2rem;
    height: 2rem;
    border: 1px solid rgba(56, 189, 248, 0.42);
    border-radius: 0.62rem;
    color: var(--ap-cyan);
    background: rgba(56, 189, 248, 0.09);
    box-shadow: 0 0 28px rgba(56, 189, 248, 0.12);
  }
  .ap-top-meta { color: var(--ap-muted); font-size: 0.76rem; text-align: right; }
  .ap-ribbon {
    display: flex;
    gap: 0.45rem;
    align-items: center;
    overflow-x: auto;
    scrollbar-width: none;
    padding: 0.15rem 0 0.6rem;
    margin-bottom: 0.2rem;
  }
  .ap-ribbon::-webkit-scrollbar { display: none; }
  .ap-ribbon-chip {
    flex: 0 0 auto;
    border: 1px solid var(--ap-line);
    border-radius: 999px;
    color: var(--ap-muted);
    background: rgba(10, 23, 39, 0.8);
    font-size: 0.72rem;
    font-weight: 720;
    padding: 0.38rem 0.66rem;
    white-space: nowrap;
  }
  .ap-ribbon-chip strong { color: var(--ap-text); margin-left: 0.25rem; }
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
    animation: apVerdictIn 240ms ease-out both;
  }
  @keyframes apVerdictIn {
    from { transform: translateY(5px); opacity: 0.72; }
    to { transform: translateY(0); opacity: 1; }
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
  [data-testid="stSegmentedControl"] {
    overflow-x: auto;
    scrollbar-width: none;
    padding-bottom: 0.15rem;
  }
  [data-testid="stSegmentedControl"]::-webkit-scrollbar { display: none; }
  [data-testid="stSegmentedControl"] > div { min-width: max-content; flex-wrap: nowrap !important; }
  [data-testid="stSegmentedControl"] button {
    min-width: 3.35rem;
    min-height: 2.45rem;
    border-radius: 0.7rem !important;
    font-weight: 790;
    letter-spacing: 0.01em;
  }
  .ap-chart-head {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 0.75rem;
    margin: 0.45rem 0 0.3rem;
  }
  .ap-chart-title { color: var(--ap-text); font-size: 1.05rem; font-weight: 780; }
  .ap-chart-note { color: var(--ap-muted); font-size: 0.75rem; }
  .ap-rail {
    border: 1px solid var(--ap-line);
    border-radius: 1rem;
    padding: 0.15rem 0.85rem 0.85rem;
    background: rgba(7, 17, 31, 0.62);
  }
  .ap-rr {
    position: relative;
    border-left: 2px solid rgba(148, 163, 184, 0.26);
    margin: 0.85rem 0.35rem 0.9rem;
    padding-left: 0.9rem;
  }
  .ap-rr-row { display: flex; justify-content: space-between; gap: 0.8rem; padding: 0.28rem 0; font-size: 0.78rem; }
  .ap-rr-row strong { color: var(--ap-text); }
  .ap-rr-row.target { color: var(--ap-green); }
  .ap-rr-row.entry { color: var(--ap-cyan); }
  .ap-rr-row.stop { color: var(--ap-red); }
  .ap-heatmap {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 0.5rem;
    margin: 0.55rem 0 1rem;
  }
  .ap-tf-cell {
    border: 1px solid var(--ap-line);
    border-radius: 0.8rem;
    background: var(--ap-panel-soft);
    padding: 0.72rem;
    min-width: 0;
  }
  .ap-tf-cell.active { border-color: rgba(56, 189, 248, 0.72); box-shadow: inset 0 0 0 1px rgba(56, 189, 248, 0.18); }
  .ap-tf-cell.bullish { background: linear-gradient(145deg, rgba(16, 185, 129, 0.12), rgba(12, 25, 42, 0.9)); }
  .ap-tf-cell.bearish { background: linear-gradient(145deg, rgba(244, 63, 94, 0.11), rgba(12, 25, 42, 0.9)); }
  .ap-tf-label { color: var(--ap-muted); font-size: 0.69rem; font-weight: 820; }
  .ap-tf-read { color: var(--ap-text); font-size: 0.84rem; font-weight: 760; margin-top: 0.26rem; }
  .ap-tf-detail { color: var(--ap-muted); font-size: 0.66rem; line-height: 1.35; margin-top: 0.25rem; }
  .ap-confluence {
    display: grid;
    grid-template-columns: minmax(150px, 0.7fr) minmax(0, 2fr);
    gap: 0.8rem;
    align-items: center;
    border: 1px solid var(--ap-line);
    border-radius: 0.95rem;
    background: var(--ap-panel-soft);
    padding: 0.9rem;
  }
  .ap-meter { height: 0.55rem; border-radius: 999px; background: rgba(148, 163, 184, 0.15); overflow: hidden; }
  .ap-meter > span { display: block; height: 100%; border-radius: inherit; background: linear-gradient(90deg, var(--ap-cyan), var(--ap-green)); }
  .ap-confluence-list { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.4rem; }
  .ap-confluence-item { color: var(--ap-muted); font-size: 0.72rem; }
  .ap-confluence-item strong { color: var(--ap-text); display: block; margin-top: 0.12rem; }
  .ap-scenarios { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0.7rem; }
  .ap-scenario { border: 1px solid var(--ap-line); border-radius: 0.9rem; background: var(--ap-panel-soft); padding: 0.9rem; }
  .ap-scenario.bull { border-top-color: var(--ap-green); }
  .ap-scenario.base { border-top-color: var(--ap-cyan); }
  .ap-scenario.bear { border-top-color: var(--ap-red); }
  .ap-scenario h4 { margin: 0 0 0.35rem; color: var(--ap-text); font-size: 0.82rem; }
  .ap-scenario p { margin: 0; color: var(--ap-muted); font-size: 0.76rem; line-height: 1.45; }
  .ap-private-note { color: var(--ap-muted); font-size: 0.72rem; margin-top: 0.4rem; }
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
    .ap-heatmap { display: flex; overflow-x: auto; padding-bottom: 0.3rem; scroll-snap-type: x proximity; }
    .ap-tf-cell { min-width: 9rem; scroll-snap-align: start; }
    .ap-scenarios { grid-template-columns: 1fr; }
    .ap-confluence { grid-template-columns: 1fr; }
    .ap-confluence-list { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  }
  @media (max-width: 520px) {
    .ap-grid { grid-template-columns: 1fr; gap: 0.5rem; }
    .ap-title { font-size: 1.75rem; }
    .ap-subtitle { font-size: 0.9rem; }
    div[data-testid="stTextInput"] input, div[data-testid="stSelectbox"] input { min-height: 3.3rem; font-size: 1.05rem; }
    .stButton > button, .stLinkButton > a, div[data-testid="stFormSubmitButton"] button { min-height: 3.2rem; }
    .ap-topbar { align-items: flex-start; }
    .ap-top-meta { display: none; }
    .ap-ribbon { margin-left: -0.72rem; margin-right: -0.72rem; padding-left: 0.72rem; padding-right: 0.72rem; }
    .ap-confluence-list { grid-template-columns: 1fr; }
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


def _html_data_url(markup: str) -> str:
    """Wrap trusted local widget markup in an isolated iframe data URL."""

    encoded = base64.b64encode(str(markup).encode("utf-8")).decode("ascii")
    return f"data:text/html;base64,{encoded}"


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


def advanced_provider_diagnostics(
    health: Mapping[str, Any] | None,
    observations: Optional[list[Mapping[str, Any]]] = None,
) -> dict[str, Any]:
    """Build one bounded diagnostics summary without URLs, symbols, payloads, or secrets."""

    source = health if isinstance(health, Mapping) else {}
    raw_messages = source.get("messages") if isinstance(source.get("messages"), list) else []
    message_categories: dict[str, int] = {}
    category_patterns = (
        ("throttling", (r"\b429\b", r"rate.?limit", r"too many requests")),
        ("authentication", (r"\b401\b", r"authentication", r"unauthori[sz]ed")),
        ("entitlement", (r"entitlement", r"not entitled", r"benzinga.*403", r"massive.*403")),
        ("authorization", (r"\b403\b", r"forbidden", r"authorization")),
        ("availability", (r"\b5\d\d\b", r"service unavailable", r"bad gateway")),
        ("timeout", (r"timeout", r"timed out")),
        ("transport", (r"network", r"connection", r"dns", r"urlerror")),
        ("invalid_response", (r"invalid response", r"contract mismatch", r"status.?error")),
    )
    for raw_message in raw_messages[:20]:
        message = str(raw_message or "").lower()
        matched = "other"
        for category, patterns in category_patterns:
            if any(re.search(pattern, message) for pattern in patterns):
                matched = category
                break
        message_categories[matched] = message_categories.get(matched, 0) + 1

    cache: dict[str, dict[str, int]] = {}
    raw_cache = source.get("cache_stats") if isinstance(source.get("cache_stats"), Mapping) else {}
    cache_fields = ("hits", "misses", "loads", "load_errors", "expirations", "coalesced_waits")
    for cache_name in ("analysis", "chart"):
        raw_stats = raw_cache.get(cache_name) if isinstance(raw_cache.get(cache_name), Mapping) else {}
        cache[cache_name] = {
            field: max(int(raw_stats[field]), 0)
            for field in cache_fields
            if isinstance(raw_stats.get(field), int) and not isinstance(raw_stats.get(field), bool)
        }

    allowed_classifications = {
        "success",
        "throttling",
        "authentication",
        "authorization",
        "entitlement",
        "request",
        "not_found",
        "availability",
        "timeout",
        "transport",
        "client",
        "invalid_response",
        "provider_response",
        "implementation",
        "provider",
    }
    raw_observations = list(observations or [])[-20:]
    classifications: dict[str, int] = {}
    outcomes: dict[str, int] = {}
    total_retries = 0
    throttled = 0
    maximum_latency_ms = 0.0
    recent: list[dict[str, Any]] = []
    recent_start = max(len(raw_observations) - 5, 0)
    for observation_index, raw in enumerate(raw_observations):
        if not isinstance(raw, Mapping):
            continue
        classification = str(raw.get("classification") or "provider").strip().lower()
        if classification not in allowed_classifications:
            classification = "provider"
        outcome = str(raw.get("outcome") or "unknown").strip().lower()
        if outcome not in {"success", "error", "circuit_open", "unknown"}:
            outcome = "unknown"
        classifications[classification] = classifications.get(classification, 0) + 1
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        retries = raw.get("retries")
        if isinstance(retries, int) and not isinstance(retries, bool):
            total_retries += max(retries, 0)
        is_throttled = raw.get("throttled") is True
        throttled += int(is_throttled)
        latency = _number(raw.get("latency_ms"))
        if latency is not None and latency >= 0:
            maximum_latency_ms = max(maximum_latency_ms, latency)
        if observation_index >= recent_start:
            status_code = raw.get("status_code")
            recent.append(
                {
                    "classification": classification,
                    "outcome": outcome,
                    "status_code": status_code
                    if isinstance(status_code, int) and not isinstance(status_code, bool)
                    else None,
                    "attempts": max(int(raw.get("attempts") or 1), 1)
                    if isinstance(raw.get("attempts"), int)
                    and not isinstance(raw.get("attempts"), bool)
                    else 1,
                    "retries": max(retries, 0)
                    if isinstance(retries, int) and not isinstance(retries, bool)
                    else 0,
                    "latency_ms": round(latency, 1) if latency is not None and latency >= 0 else None,
                    "throttled": is_throttled,
                    "observed_at": format_timestamp(raw.get("observed_at")),
                }
            )

    age = _number(source.get("data_age_seconds"))
    earnings_latency = _number(source.get("earnings_latency_ms"))
    earnings_error = str(source.get("earnings_error_kind") or "none").strip().lower()
    allowed_earnings_errors = {
        "none",
        "entitlement",
        "availability",
        "throttling",
        "authentication",
        "authorization",
        "timeout",
        "transport",
        "invalid_response",
        "provider_response",
        "implementation",
        "client",
    }
    if earnings_error not in allowed_earnings_errors:
        earnings_error = "other"
    return {
        "provider": public_text(source.get("provider"), "Unavailable", max_length=48),
        "status": public_text(source.get("status"), "unknown", max_length=24),
        "currentness": currentness_label(source.get("data_label")),
        "timestamp": format_timestamp(source.get("timestamp")),
        "data_age_seconds": round(age, 1) if age is not None and age >= 0 else None,
        "stale": source.get("stale") is True,
        "message_categories": dict(sorted(message_categories.items())),
        "cache": cache,
        "earnings": {
            "status": public_text(source.get("earnings_status"), "unresolved", max_length=24),
            "error_kind": earnings_error,
            "status_code": source.get("earnings_status_code")
            if isinstance(source.get("earnings_status_code"), int)
            and not isinstance(source.get("earnings_status_code"), bool)
            else None,
            "attempts": source.get("earnings_attempts")
            if isinstance(source.get("earnings_attempts"), int)
            and not isinstance(source.get("earnings_attempts"), bool)
            else None,
            "latency_ms": round(earnings_latency, 1)
            if earnings_latency is not None and earnings_latency >= 0
            else None,
            "throttled": source.get("earnings_throttled") is True,
        },
        "requests": {
            "count": len([item for item in raw_observations if isinstance(item, Mapping)]),
            "classifications": dict(sorted(classifications.items())),
            "outcomes": dict(sorted(outcomes.items())),
            "total_retries": total_retries,
            "throttled_count": throttled,
            "maximum_latency_ms": round(maximum_latency_ms, 1),
            "recent": recent,
        },
    }


def earnings_context_label(decision: Mapping[str, Any] | None) -> str:
    source = decision if isinstance(decision, Mapping) else {}
    status = str(source.get("earnings_status") or "unresolved").strip().lower()
    if status == "scheduled" and source.get("earnings_date"):
        date_status = public_text(source.get("earnings_date_status"), "scheduled", max_length=16).title()
        return f"{date_status} · {public_text(source.get('earnings_date'), max_length=16)}"
    if status == "verified_none":
        through = public_text(source.get("earnings_checked_through"), "the checked window", max_length=20)
        return f"No event in vendor calendar through {through}"
    diagnostic = str(source.get("earnings_error_kind") or "").strip().lower()
    diagnostic_labels = {
        "entitlement": "Vendor entitlement required",
        "availability": "Provider temporarily unavailable",
        "throttling": "Provider throttled",
        "authentication": "Provider authentication issue",
        "authorization": "Provider authorization issue",
        "timeout": "Provider timed out",
        "transport": "Provider connection issue",
        "invalid_response": "Provider response contract mismatch",
        "provider_response": "Provider rejected the request",
        "implementation": "Application validation issue",
        "client": "Application request issue",
    }
    if diagnostic in diagnostic_labels:
        return f"Unresolved · {diagnostic_labels[diagnostic]}"
    return "Unresolved · entry gated"


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


def _safe_decision(
    decision: Mapping[str, Any] | None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Prepare a fail-closed, display-safe decision mapping."""

    # Currentness is evidence, not durable state. Re-age every decision at the
    # render boundary so a cached/session-held ENTER cannot outlive its source.
    source = revalidate_decision_freshness(decision, now=now)
    data_label = str(source.get("data_label") or "unavailable").strip().lower()
    source_name = str(source.get("data_source") or "Unavailable").strip().lower()
    price = _number(source.get("current_price"))
    fail_closed = (
        data_label in {"unavailable", "stale", "last-close", "last_close"}
        or source_name == "unavailable"
        or price is None
    )
    if fail_closed:
        source["verdict"] = "PASS"
        source["state"] = "BLOCKED"
        source["entry_conditions_satisfied"] = False
        source["do_this_now"] = (
            "Pass for now—current market evidence is unavailable or stale, so no entry decision was made."
        )
        source["primary_risk"] = "The current market evidence is not complete enough for an entry decision."
        source["invalidation_condition"] = (
            "No current invalidation level is available; wait for complete provider-backed evidence."
        )

    warnings = source.get("warnings")
    if isinstance(warnings, list):
        safe_warnings = [
            public_text(item, "A supporting input was unavailable.", max_length=320)
            for item in warnings
            if str(item or "").strip()
        ]
        if source.get("primary_risk") in warnings:
            warning_index = warnings.index(source.get("primary_risk"))
            if warning_index < len(safe_warnings):
                source["primary_risk"] = safe_warnings[warning_index]
        source["warnings"] = safe_warnings

    breakdown = deepcopy(source.get("full_breakdown"))
    if not isinstance(breakdown, Mapping):
        breakdown = {}
    else:
        breakdown = dict(breakdown)
    if fail_closed:
        unavailable_scenario = (
            "This scenario is not actionable until fresh regular-session provider evidence is restored."
        )
        breakdown.update(
            {
                "entry_and_invalidation_plan": source["do_this_now"],
                "targets_and_reward_to_risk": (
                    "Previously calculated levels are historical only and are not an active trade plan."
                ),
                "bull_case": unavailable_scenario,
                "bear_case": unavailable_scenario,
                "no_trade_case": source["do_this_now"],
                "options_analysis": (
                    "Options are gated because the underlying evidence is not current and entry-eligible."
                ),
                "final_verdict": source["do_this_now"],
            }
        )
    source["full_breakdown"] = breakdown
    return source


def revalidated_provider_health(
    health: Mapping[str, Any] | None,
    decision: Mapping[str, Any] | None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Re-age display health from the same evidence used by the decision gate."""

    source = deepcopy(dict(health or {}))
    evidence = decision if isinstance(decision, Mapping) else {}
    assessment = assess_source_freshness(
        data_label=evidence.get("data_label") or source.get("data_label"),
        data_timestamp=evidence.get("data_timestamp") or source.get("timestamp"),
        market_status=evidence.get("market_status"),
        now=now,
    )
    source["data_label"] = assessment.effective_label
    source["timestamp"] = assessment.timestamp or source.get("timestamp")
    source["data_age_seconds"] = (
        round(assessment.age_seconds, 3)
        if assessment.age_seconds is not None and assessment.age_seconds >= 0
        else None
    )
    source["stale"] = not assessment.valid_for_entry
    return source


def build_decision_brief(
    decision: Mapping[str, Any] | None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build every value required by the top decision card."""

    safe = _safe_decision(decision, now=now)
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
    data_label = str(safe.get("data_label") or "unavailable").strip().lower()
    price_label = (
        "Last observed price"
        if data_label in {"stale", "last-close", "last_close"}
        else "Price unavailable"
        if data_label == "unavailable"
        else "Current price"
    )
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
        "price_label": price_label,
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
        "earnings": earnings_context_label(safe),
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


def entry_action_allowed(brief: Mapping[str, Any] | None) -> bool:
    """Allow entry recording only for a current, explicit ENTER decision."""

    brief = brief or {}
    return bool(
        brief.get("verdict") == "ENTER"
        and brief.get("state") == "ENTER"
        and brief.get("entry_satisfied") == "Yes"
        and brief.get("current_price") not in (None, "", "Unavailable")
        and brief.get("timestamp") not in (None, "", "Unavailable")
        and brief.get("currentness") in {"Real-time", "Delayed", "Last close"}
        and brief.get("source") not in (None, "", "Unavailable")
    )


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


def options_empty_state_message(decision: Mapping[str, Any] | None) -> str:
    """Explain an empty options result without implying an unperformed screen."""

    source = decision if isinstance(decision, Mapping) else {}
    options = source.get("options") if isinstance(source.get("options"), Mapping) else {}
    status = str(options.get("status") or "unavailable").strip().upper()
    state = str(source.get("state") or "BLOCKED").strip().upper()
    if state not in {"ENTER", "ARMED"}:
        return (
            "Options: not run — screening was skipped because the underlying setup "
            "did not reach an options-eligible ENTER or ARMED state."
        )
    if status == "WAIT":
        return "Options: wait — the underlying setup is not confirmed, so no contract is recommended."
    if status == "PASS":
        return "Options: pass — no usable contract observations were returned for evaluation."
    return "Options: unavailable — a current, complete chain was not returned."


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


def build_home_snapshot(
    state: Mapping[str, Any] | None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Build the source-backed home model from persisted personal state."""

    state = state or {}
    raw_analyses = state.get("last_analyses") if isinstance(state.get("last_analyses"), Mapping) else {}
    analyses: list[dict[str, Any]] = []
    for ticker_key, raw in raw_analyses.items():
        if not isinstance(raw, Mapping):
            continue
        decision = raw.get("decision") if isinstance(raw.get("decision"), Mapping) else raw
        safe = _safe_decision(decision, now=now)
        safe["ticker"] = str(safe.get("ticker") or ticker_key).upper()
        safe["saved_at"] = raw.get("saved_at") or safe.get("saved_at")
        if isinstance(raw.get("provider_health"), Mapping):
            safe["provider_health"] = revalidated_provider_health(
                raw.get("provider_health"),
                safe,
                now=now,
            )
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
        earnings_status = str(item.get("earnings_status") or "unresolved").lower()
        if earnings_status == "scheduled" and earnings:
            catalysts.append(
                {
                    "ticker": public_text(item.get("ticker"), "Ticker", max_length=24),
                    "message": f"Earnings date: {public_text(earnings, 'Unavailable', max_length=20)}",
                }
            )
        elif earnings_status == "verified_none":
            catalysts.append(
                {
                    "ticker": public_text(item.get("ticker"), "Ticker", max_length=24),
                    "message": earnings_context_label(item),
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
        "positions": build_position_snapshot(state, now=now),
        "data_health": f"{public_text(data_source, 'Unavailable', max_length=48)} · {currentness_label(data_label)}",
        "last_refresh": format_timestamp(
            provider_health.get("timestamp") or newest.get("data_timestamp") or newest.get("saved_at")
        ),
    }


def build_position_snapshot(
    state: Mapping[str, Any] | None,
    *,
    now: Optional[datetime] = None,
) -> list[dict[str, str]]:
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
        safe = _safe_decision(decision if isinstance(decision, Mapping) else {}, now=now)
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


def _unavailable_payload(query: str, timeframe: str = DEFAULT_TIMEFRAME) -> dict[str, Any]:
    clean = re.sub(r"[^A-Za-z0-9.\-]", "", str(query or "").upper())[:14] or "UNRESOLVED"
    label = normalize_timeframe(timeframe, default=DEFAULT_TIMEFRAME)
    decision = unavailable_result(
        clean,
        "Current provider evidence is unavailable; no entry decision was made.",
    ).to_dict()
    symbol = normalize_tradingview_symbol(clean)
    return {
        "decision": decision,
        "chart_bars": [],
        "chart_frames": {},
        "selected_timeframe": label,
        "journal_bars": [],
        "resolved": {"ticker": clean},
        "tradingview_symbol": symbol,
        "tradingview_url": tradingview_chart_url(symbol, label),
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


def _initialize_state(
    st: Any,
    config: Mapping[str, Any] | Any | None,
    *,
    presentation: bool = False,
) -> tuple[dict[str, Any], Optional[AutopilotStateStore]]:
    session = st.session_state
    if presentation:
        state = session.get("_autopilot_presentation_state")
        if not isinstance(state, Mapping):
            state = default_state()
            session["_autopilot_presentation_state"] = state
        return state, None

    private_state_enabled = _truthy(_config_value(config, "AUTOPILOT_PRIVATE_STATE_ENABLED"))
    path = resolve_state_path(config) if private_state_enabled else None
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
    raw_health = payload.get("provider_health") if isinstance(payload.get("provider_health"), Mapping) else {}
    health = revalidated_provider_health(raw_health, decision)
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
        "earnings_status": decision.get("earnings_status"),
        "earnings_date_status": decision.get("earnings_date_status"),
        "earnings_checked_through": decision.get("earnings_checked_through"),
        "earnings_error_kind": decision.get("earnings_error_kind"),
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
            "data_age_seconds": health.get("data_age_seconds"),
            "stale": health.get("stale") is True,
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
    selected_timeframe = normalize_timeframe(
        payload.get("selected_timeframe"), default=DEFAULT_TIMEFRAME
    )
    chart_rows = []
    for row in _frame_rows(payload, selected_timeframe)[-180:]:
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
            "timeframe": selected_timeframe,
            "tradingview_symbol": payload.get("tradingview_symbol"),
            "bars": chart_rows,
        },
    }


def _apply_tracking_action(st: Any, payload: Mapping[str, Any], action: str) -> str:
    session = st.session_state
    state = deepcopy(session.get("_autopilot_personal_state") or default_state())
    raw_decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
    # Re-age again at the action boundary. A page may remain open after the
    # last render, and recording an entry must never trust that older verdict.
    decision = _safe_decision(revalidate_decision_freshness(raw_decision))
    if action == "entered" and not entry_action_allowed(build_decision_brief(decision)):
        return "blocked"
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


_SEARCH_OPTIONS: tuple[str, ...] = (
    "SPY · SPDR S&P 500 ETF Trust · NYSE Arca",
    "QQQ · Invesco QQQ Trust · NASDAQ",
    "AAPL · Apple Inc. · NASDAQ",
    "MSFT · Microsoft Corporation · NASDAQ",
    "NVDA · NVIDIA Corporation · NASDAQ",
    "AMZN · Amazon.com Inc. · NASDAQ",
    "META · Meta Platforms Inc. · NASDAQ",
    "GOOGL · Alphabet Inc. · NASDAQ",
    "TSLA · Tesla Inc. · NASDAQ",
    "AMD · Advanced Micro Devices Inc. · NASDAQ",
    "AVGO · Broadcom Inc. · NASDAQ",
    "NFLX · Netflix Inc. · NASDAQ",
    "PLTR · Palantir Technologies Inc. · NASDAQ",
    "COIN · Coinbase Global Inc. · NASDAQ",
    "IWM · iShares Russell 2000 ETF · NYSE Arca",
    "DIA · SPDR Dow Jones Industrial Average ETF · NYSE Arca",
)


def _query_value(st: Any, key: str) -> Optional[str]:
    params = getattr(st, "query_params", None)
    if params is None:
        return None
    try:
        value = params.get(key)
    except Exception:
        return None
    if isinstance(value, (list, tuple)):
        value = value[-1] if value else None
    text = str(value or "").strip()
    return text or None


def _set_query_values(st: Any, **updates: Any) -> None:
    params = getattr(st, "query_params", None)
    if params is None:
        return
    for key, raw_value in updates.items():
        try:
            if raw_value in (None, ""):
                if key in params:
                    del params[key]
            else:
                params[key] = str(raw_value)
        except Exception:
            continue


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on", "presentation"}


def _presentation_mode(st: Any) -> bool:
    requested = _query_value(st, "view") == "presentation" or _truthy(
        _query_value(st, "presentation")
    )
    if requested:
        st.session_state["_autopilot_presentation"] = True
    enabled = bool(st.session_state.get("_autopilot_presentation", requested))
    if enabled and not requested:
        # Presentation is privacy-sensitive and shareable. Keep its URL marker
        # invariant even if browser history/manual query editing drops it; the
        # explicit toggle callback remains the authoritative way to turn it off.
        _set_query_values(st, view="presentation", presentation=None)
    return enabled


def _sync_presentation_toggle(st: Any) -> None:
    """Synchronize the widget, session flag and shareable query parameter."""

    enabled = bool(st.session_state.get("_autopilot_presentation_toggle"))
    st.session_state["_autopilot_presentation"] = enabled
    _set_query_values(st, view="presentation" if enabled else None, presentation=None)


def _active_timeframe(st: Any) -> str:
    query_timeframe = _query_value(st, "tf")
    session_timeframe = st.session_state.get("_autopilot_timeframe")
    selected = normalize_timeframe(query_timeframe or session_timeframe, default=DEFAULT_TIMEFRAME)
    st.session_state["_autopilot_timeframe"] = selected
    return selected


def _set_active_timeframe(st: Any, value: Any) -> str:
    selected = normalize_timeframe(value, default=DEFAULT_TIMEFRAME)
    st.session_state["_autopilot_timeframe"] = selected
    _set_query_values(st, tf=selected)
    return selected


def _ticker_from_search_choice(value: Any) -> str:
    text = str(value or "").strip()
    if " · " in text:
        text = text.split(" · ", 1)[0]
    return text.strip()


def _engine_timeframe_label(timeframe: str) -> Optional[str]:
    return {
        "5m": "5M",
        "15m": "15M",
        "1H": "1H",
        "4H": "4H",
        "1D": "1D",
        "1W": "1W",
        "1M": "1M",
    }.get(normalize_timeframe(timeframe, default=DEFAULT_TIMEFRAME))


def _frame_rows(payload: Mapping[str, Any], timeframe: str) -> list[dict[str, Any]]:
    label = normalize_timeframe(timeframe, default=DEFAULT_TIMEFRAME)
    frames = payload.get("chart_frames") if isinstance(payload.get("chart_frames"), Mapping) else {}
    values = frames.get(label)
    if isinstance(values, list):
        return [dict(row) for row in values if isinstance(row, Mapping)]
    if label == DEFAULT_TIMEFRAME and isinstance(payload.get("chart_bars"), list):
        return [dict(row) for row in payload.get("chart_bars", []) if isinstance(row, Mapping)]
    return []


def _chart_rows_for(
    payload: MutableMapping[str, Any],
    timeframe: str,
    provider_key: str,
) -> list[dict[str, Any]]:
    label = normalize_timeframe(timeframe, default=DEFAULT_TIMEFRAME)
    rows = _frame_rows(payload, label)
    ticker = str(payload.get("resolved", {}).get("ticker") or "").strip().upper() if isinstance(payload.get("resolved"), Mapping) else ""
    requires_registry_depth = label in {"1H", "4H", "1W", "1M"}
    if (not rows or requires_registry_depth) and ticker and provider_key:
        fingerprint = hashlib.sha256(provider_key.encode("utf-8")).hexdigest()[:16]
        cache_key = f"{fingerprint}:{ticker}:{label}"

        def create() -> list[dict[str, Any]]:
            return load_chart_bars(ticker, label, provider_key)

        try:
            rows = _CHART_CACHE.get_or_create(cache_key, create, cache_if=bool)
        except Exception:
            rows = []
        if rows:
            frames = dict(payload.get("chart_frames") or {})
            frames[label] = deepcopy(rows)
            payload["chart_frames"] = frames
    return rows


def _selected_chart_rows(
    st: Any,
    payload: MutableMapping[str, Any],
    timeframe: str,
    provider_key: str,
) -> list[dict[str, Any]]:
    label = normalize_timeframe(timeframe, default=DEFAULT_TIMEFRAME)
    rows = _chart_rows_for(payload, label, provider_key)
    payload["chart_bars"] = deepcopy(rows)
    payload["selected_timeframe"] = label
    st.session_state["_autopilot_active_result"] = payload
    return rows


def build_timeframe_alignment(
    decision: Optional[Mapping[str, Any]],
    *,
    selected_timeframe: str = DEFAULT_TIMEFRAME,
    selected_analysis: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, str]]:
    safe = _safe_decision(decision)
    frames = safe.get("timeframes") if isinstance(safe.get("timeframes"), Mapping) else {}
    selected = normalize_timeframe(selected_timeframe, default=DEFAULT_TIMEFRAME)
    rows: list[dict[str, str]] = []
    for label in ("1M", "1W", "1D", "4H", "1H", "30m", "15m", "5m", "3m", "1m"):
        engine_label = _engine_timeframe_label(label)
        item = frames.get(engine_label) if engine_label and isinstance(frames.get(engine_label), Mapping) else {}
        if label == selected and isinstance(selected_analysis, Mapping):
            merged_item = dict(item)
            for key, value in selected_analysis.items():
                if value is None:
                    continue
                if key == "direction" and str(value).strip().lower() in {
                    "",
                    "unavailable",
                    "unknown",
                }:
                    continue
                if key in {"support", "resistance"} and not value:
                    continue
                merged_item[key] = value
            item = merged_item
        direction = public_text(item.get("direction"), "unavailable", max_length=24).lower()
        trend_score = _number(item.get("trend_score"))
        macd_hist = _number(item.get("macd_histogram"))
        close = _number(item.get("close"))
        supports = [value for value in list(item.get("support") or []) if _number(value) is not None]
        resistances = [value for value in list(item.get("resistance") or []) if _number(value) is not None]
        ma_read = (
            "Aligned up"
            if trend_score is not None and trend_score >= 2
            else "Aligned down"
            if trend_score is not None and trend_score <= -2
            else "Mixed"
        )
        momentum = (
            "Positive"
            if macd_hist is not None and macd_hist > 0
            else "Negative"
            if macd_hist is not None and macd_hist < 0
            else "Flat / unavailable"
        )
        level_status = "No level data"
        if close is not None and supports:
            level_status = f"Above {format_price(max(float(value) for value in supports))} support"
        if close is not None and resistances and min(float(value) for value in resistances) <= close:
            level_status = "At / through resistance"
        rows.append(
            {
                "timeframe": label,
                "direction": direction.title(),
                "ma_alignment": ma_read,
                "momentum": momentum,
                "level_status": level_status,
                "active": "yes" if label == selected else "no",
            }
        )
    return rows


def _alignment_html(
    decision: Mapping[str, Any],
    selected_timeframe: str,
    selected_analysis: Optional[Mapping[str, Any]] = None,
) -> str:
    cells: list[str] = []
    for row in build_timeframe_alignment(
        decision,
        selected_timeframe=selected_timeframe,
        selected_analysis=selected_analysis,
    ):
        direction = row["direction"].lower()
        class_name = "bullish" if direction == "bullish" else "bearish" if direction == "bearish" else "mixed"
        active = " active" if row["active"] == "yes" else ""
        cells.append(
            f'<div class="ap-tf-cell {class_name}{active}">'
            f'<div class="ap-tf-label">{html.escape(row["timeframe"])}</div>'
            f'<div class="ap-tf-read">{html.escape(row["direction"])}</div>'
            f'<div class="ap-tf-detail">{html.escape(row["ma_alignment"])} · {html.escape(row["momentum"])}<br>{html.escape(row["level_status"])}</div>'
            "</div>"
        )
    return '<div class="ap-heatmap">' + "".join(cells) + "</div>"


def _confluence_html(decision: Mapping[str, Any]) -> str:
    safe = _safe_decision(decision)
    confidence = int(max(0, min(100, round(_number(safe.get("confidence")) or 0))))
    market = safe.get("market_context") if isinstance(safe.get("market_context"), Mapping) else {}
    plan = safe.get("plan") if isinstance(safe.get("plan"), Mapping) else {}
    items = [
        ("Timeframes", "Weighted hierarchy"),
        ("Trigger", "Confirmed" if safe.get("entry_conditions_satisfied") else "Waiting"),
        ("Market", public_text(market.get("regime"), "Unavailable", max_length=24).title()),
        ("R:R", format_ratio(plan.get("reward_to_risk"))),
        ("Data", currentness_label(safe.get("data_label"))),
        ("Earnings", earnings_context_label(safe)),
    ]
    detail = "".join(
        f'<div class="ap-confluence-item">{html.escape(label)}<strong>{html.escape(value)}</strong></div>'
        for label, value in items
    )
    return (
        '<div class="ap-confluence">'
        f'<div><div class="ap-label">Explainable confluence</div><div class="ap-value">{confidence}%</div><div class="ap-meter"><span style="width:{confidence}%"></span></div></div>'
        f'<div class="ap-confluence-list">{detail}</div>'
        "</div>"
    )


def _scenario_html(decision: Mapping[str, Any]) -> str:
    safe = _safe_decision(decision)
    breakdown = safe.get("full_breakdown") if isinstance(safe.get("full_breakdown"), Mapping) else {}
    cards = (
        ("bull", "Bull case", public_text(breakdown.get("bull_case"), "Unavailable", max_length=520)),
        ("base", "Base / no-trade case", public_text(breakdown.get("no_trade_case"), "Unavailable", max_length=520)),
        ("bear", "Bear case", public_text(breakdown.get("bear_case"), "Unavailable", max_length=520)),
    )
    return '<div class="ap-scenarios">' + "".join(
        f'<div class="ap-scenario {kind}"><h4>{html.escape(title)}</h4><p>{html.escape(text)}</p></div>'
        for kind, title, text in cards
    ) + "</div>"


def _reward_ladder_html(brief: Mapping[str, Any]) -> str:
    rows = (
        ("target", "Stretch target", brief.get("stretch_target")),
        ("target", "Target 2", brief.get("target_2")),
        ("target", "Target 1", brief.get("target_1")),
        ("entry", "Trigger", brief.get("trigger")),
        ("entry", "Entry zone", brief.get("entry_zone")),
        ("stop", "Invalidation", brief.get("invalidation")),
    )
    return '<div class="ap-rr">' + "".join(
        f'<div class="ap-rr-row {kind}"><span>{html.escape(label)}</span><strong>{html.escape(public_text(value))}</strong></div>'
        for kind, label, value in rows
    ) + "</div>"


def _market_ribbon_html(decision: Mapping[str, Any]) -> str:
    safe = _safe_decision(decision)
    market = safe.get("market_context") if isinstance(safe.get("market_context"), Mapping) else {}
    sector_symbol = public_text(market.get("sector_symbol"), "Sector", max_length=16)
    chips = (
        ("Regime", _compact_market_value(str(market.get("regime") or "unavailable").replace("-", " "))),
        ("SPY", _compact_market_value(market.get("spy_direction"))),
        ("QQQ", _compact_market_value(market.get("qqq_direction"))),
        (sector_symbol, _compact_market_value(market.get("sector_direction"))),
        ("Volatility", _compact_market_value(market.get("volatility"))),
        ("Breadth", _compact_market_value(market.get("breadth"))),
        ("Data", currentness_label(safe.get("data_label"))),
    )
    return '<div class="ap-ribbon">' + "".join(
        f'<div class="ap-ribbon-chip">{html.escape(label)}<strong>{html.escape(value)}</strong></div>'
        for label, value in chips
    ) + "</div>"


def _compact_market_value(value: Any) -> str:
    """Keep ribbon values scannable without weakening unavailable states."""

    cleaned = public_text(str(value or "unavailable").replace("-", " ").title(), "Unavailable")
    normalized = cleaned.lower()
    if "unavailable" in normalized or "not returned" in normalized or "not configured" in normalized:
        return "Unavailable"
    return cleaned


def release_build_label(state_path: Path | None = None) -> str:
    """Return a public marker only when deployed content verifies end to end."""

    path = state_path or _RELEASE_STATE_PATH
    if path.is_symlink() or not path.is_file():
        return f"v{APP_DISPLAY_VERSION}"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return f"v{APP_DISPLAY_VERSION}"
    if not isinstance(state, Mapping):
        return f"v{APP_DISPLAY_VERSION}"
    if (
        state.get("schema_version") != 3
        or state.get("canonical_repository") != "davidbenizri25-wq/trading-elite-system"
        or state.get("version") != "2.1.0-premium-terminal"
    ):
        return f"v{APP_DISPLAY_VERSION}"
    canonical = str(state.get("canonical_commit") or "").strip().lower()
    manifest = str(state.get("manifest_sha256") or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", canonical) or not re.fullmatch(r"[0-9a-f]{64}", manifest):
        return f"v{APP_DISPLAY_VERSION}"
    raw_files = state.get("files")
    if not isinstance(raw_files, Mapping) or not raw_files:
        return f"v{APP_DISPLAY_VERSION}"
    raw_managed_paths = state.get("managed_paths")
    if not isinstance(raw_managed_paths, list) or not raw_managed_paths:
        return f"v{APP_DISPLAY_VERSION}"
    normalized_files: dict[str, str] = {}
    for raw_relative, raw_digest in raw_files.items():
        relative = str(raw_relative or "").strip().replace("\\", "/")
        digest = str(raw_digest or "").strip().lower()
        relative_path = Path(relative)
        if (
            not relative
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            return f"v{APP_DISPLAY_VERSION}"
        if relative in normalized_files:
            return f"v{APP_DISPLAY_VERSION}"
        normalized_files[relative] = digest
    normalized_managed_paths: list[str] = []
    for raw_relative in raw_managed_paths:
        relative = str(raw_relative or "").strip().replace("\\", "/")
        relative_path = Path(relative)
        if not relative or relative_path.is_absolute() or ".." in relative_path.parts:
            return f"v{APP_DISPLAY_VERSION}"
        normalized_managed_paths.append(relative)
    if (
        len(set(normalized_managed_paths)) != len(normalized_managed_paths)
        or normalized_managed_paths != sorted(normalized_managed_paths)
        or normalized_managed_paths != sorted(normalized_files)
    ):
        return f"v{APP_DISPLAY_VERSION}"
    manifest_payload = [
        {"path": relative, "sha256": normalized_files[relative]}
        for relative in sorted(normalized_files)
    ]
    expected_manifest = hashlib.sha256(
        json.dumps(
            manifest_payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    if not hmac.compare_digest(manifest, expected_manifest):
        return f"v{APP_DISPLAY_VERSION}"
    root = path.parent.parent
    for relative, expected_digest in normalized_files.items():
        deployed = root.joinpath(*Path(relative).parts)
        if deployed.is_symlink() or not deployed.is_file():
            return f"v{APP_DISPLAY_VERSION}"
        try:
            actual_digest = hashlib.sha256(deployed.read_bytes()).hexdigest()
        except OSError:
            return f"v{APP_DISPLAY_VERSION}"
        if not hmac.compare_digest(actual_digest, expected_digest):
            return f"v{APP_DISPLAY_VERSION}"
    return f"v{APP_DISPLAY_VERSION} · source {canonical[:7]} · manifest {manifest[:7]}"


def _render_timeframe_control(st: Any, current: str) -> str:
    selected = normalize_timeframe(current, default=DEFAULT_TIMEFRAME)
    key = "_autopilot_timeframe_segment"
    if st.session_state.get(key) not in TIMEFRAME_LABELS:
        st.session_state[key] = selected
    st.markdown('<div class="ap-section-title">Chart timeframe</div>', unsafe_allow_html=True)
    chosen = st.segmented_control(
        "Chart timeframe",
        options=list(TIMEFRAME_LABELS),
        key=key,
        selection_mode="single",
        label_visibility="collapsed",
        width="stretch",
    )
    chosen = normalize_timeframe(chosen, default=selected)
    return _set_active_timeframe(st, chosen)


def _decision_rail_html(brief: Mapping[str, Any]) -> str:
    reasons = "".join(
        f"<li>{html.escape(public_text(reason, 'Evidence unavailable', max_length=360))}</li>"
        for reason in list(brief.get("reasons") or [])[:3]
    )
    metrics = _html_grid(
        [
            ("State", brief.get("state")),
            ("Direction", brief.get("direction")),
            ("Confidence", brief.get("confidence")),
            ("Grade", brief.get("grade")),
            ("Price", brief.get("current_price")),
            ("R:R", brief.get("reward_to_risk")),
        ]
    )
    currentness = str(brief.get("currentness") or "")
    title = (
        "Historical levels · not actionable"
        if currentness in {"Stale — decision gated", "Last close", "Unavailable"}
        else "Decision rail"
    )
    return (
        '<div class="ap-rail">'
        f'<div class="ap-section-title">{html.escape(title)}</div>'
        + metrics
        + _reward_ladder_html(brief)
        + f'<div class="ap-label">Why this decision</div><ol>{reasons}</ol>'
        + _callout("Primary risk", brief.get("primary_risk"), "risk")
        + _callout("What upgrades it", brief.get("upgrade"), "upgrade")
        + _callout("What invalidates it", brief.get("invalidate"), "risk")
        + "</div>"
    )


def _render_header(st: Any, *, presentation: bool) -> bool:
    mode_label = "Showcase view" if presentation else "David's decision terminal"
    build_label = release_build_label()
    st.markdown(
        f"""
        <div class="ap-topbar">
          <div class="ap-brand"><span class="ap-brand-mark">TA</span><span>Trading Autopilot</span></div>
          <div class="ap-top-meta">{html.escape(mode_label)}<br>{html.escape(build_label)}<br>Decision support only</div>
        </div>
        <div class="ap-kicker">Chart-first market intelligence</div>
        <h1 class="ap-title">Search. Read the setup. Decide.</h1>
        <p class="ap-subtitle">One ticker turns into a synchronized chart, multi-timeframe evidence, and an ENTER / WAIT / PASS action.</p>
        """,
        unsafe_allow_html=True,
    )

    toggle_key = "_autopilot_presentation_toggle"
    query_requested = _query_value(st, "view") == "presentation" or _truthy(
        _query_value(st, "presentation")
    )
    query_synced_key = "_autopilot_presentation_query_synced"
    if toggle_key not in st.session_state:
        st.session_state[toggle_key] = bool(presentation)
    elif query_requested and not st.session_state.get(query_synced_key):
        # A newly opened shareable URL must win over stale widget state from a
        # previous normal-mode render in the same browser session.
        st.session_state[toggle_key] = True
    st.session_state[query_synced_key] = query_requested
    enabled = bool(
        st.toggle(
            "Presentation Mode",
            key=toggle_key,
            help="Hides personal watchlist, journal, positions and tracking controls for a clean shareable view.",
            on_change=_sync_presentation_toggle,
            args=(st,),
        )
    )
    if enabled != presentation:
        _sync_presentation_toggle(st)
        rerun = getattr(st, "rerun", None)
        if callable(rerun):
            rerun()
    if enabled:
        st.markdown(
            "<style>header[data-testid='stHeader'] { background: transparent; } .ap-private-only { display:none !important; }</style>",
            unsafe_allow_html=True,
        )
    return enabled


def _render_search(st: Any) -> tuple[bool, str]:
    current = str(st.session_state.get("_autopilot_search_query") or _query_value(st, "symbol") or "").strip()
    options = list(_SEARCH_OPTIONS)
    current_index = None
    if current:
        match = next((index for index, value in enumerate(options) if value.upper().startswith(current.upper() + " ·")), None)
        if match is None:
            options.insert(0, current.upper())
            current_index = 0
        else:
            current_index = match
    with st.form("autopilot_ticker_search", clear_on_submit=False):
        search_column, action_column = st.columns([5.6, 1.25], gap="small")
        with search_column:
            query = st.selectbox(
                "Ticker search",
                options=options,
                index=current_index,
                placeholder="Search ticker, company or exchange — press Enter",
                accept_new_options=True,
                label_visibility="collapsed",
            )
        with action_column:
            submitted = st.form_submit_button("Analyze", type="primary", width="stretch")
    try:
        st.html(
            """
            <script>
            (() => {
              const root = window.parent.document;
              if (window.parent.__tradingAutopilotSlashShortcut) return;
              window.parent.__tradingAutopilotSlashShortcut = true;
              root.addEventListener('keydown', (event) => {
                const tag = (event.target && event.target.tagName || '').toLowerCase();
                if (event.key === '/' && !['input','textarea','select'].includes(tag)) {
                  const input = root.querySelector('[data-testid="stSelectbox"] input');
                  if (input) { event.preventDefault(); input.focus(); }
                }
              });
            })();
            </script>
            """,
            unsafe_allow_javascript=True,
        )
    except Exception:
        pass
    return submitted, _ticker_from_search_choice(query)


def _render_home(st: Any, state: Mapping[str, Any], *, presentation: bool = False) -> None:
    if presentation:
        st.markdown('<div class="ap-section-title">Start with one symbol</div>', unsafe_allow_html=True)
        st.markdown(
            _html_grid(
                [
                    ("1 · Search", "Type any listed US ticker or choose a company suggestion."),
                    ("2 · Read", "Compare the chart, timeframe alignment, trigger and invalidation."),
                    ("3 · Decide", "Use ENTER, WAIT or PASS as decision support—not an order."),
                ]
            ),
            unsafe_allow_html=True,
        )
        st.info("Try SPY, QQQ, AAPL, NVDA or TSLA. Presentation Mode keeps personal watchlists, positions, journals and tracking controls out of view.")
        st.caption("Decision support only. No orders are placed.")
        return
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
                (brief["price_label"], brief["current_price"]),
                ("Market", brief["market_status"]),
                ("Setup", brief["setup_type"]),
                ("Entry confirmed", brief["entry_satisfied"]),
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
    entry_enabled = entry_action_allowed(brief)
    columns = st.columns(4, gap="small")
    clicked: Optional[str] = None
    with columns[0]:
        if st.button(
            "I entered",
            key=f"cockpit_entered_{brief['ticker']}",
            width="stretch",
            disabled=not entry_enabled,
        ):
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
        if mode == "blocked":
            st.warning("Entry snapshot was not saved because the current decision is not ENTER.")
        else:
            st.success(messages[clicked])
            if mode == "session-only":
                st.caption("Saved for this session only.")
    if not entry_enabled:
        st.caption("Entry recording unlocks only when current evidence reaches ENTER. This never places an order.")

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


def _render_chart(
    st: Any,
    payload: Mapping[str, Any],
    brief: Mapping[str, Any],
    timeframe: str,
    rows: list[dict[str, Any]],
) -> None:
    label = normalize_timeframe(timeframe, default=DEFAULT_TIMEFRAME)
    spec = get_timeframe_spec(label)
    chart_note = (
        f"{'Intraday' if spec.intraday else 'Higher timeframe'} · completed provider bars · decision levels remain unchanged"
        if rows
        else f"{'Intraday' if spec.intraday else 'Higher timeframe'} · provider bars unavailable · no chart levels inferred"
    )
    st.markdown(
        f'<div class="ap-chart-head"><div><div class="ap-chart-title">{html.escape(str(brief["ticker"]))} · {html.escape(label)} setup chart</div>'
        f'<div class="ap-chart-note">{html.escape(chart_note)}</div></div></div>',
        unsafe_allow_html=True,
    )
    if rows:
        try:
            decision = brief["safe_decision"]
            frames = decision.get("timeframes") if isinstance(decision.get("timeframes"), Mapping) else {}
            engine_label = _engine_timeframe_label(label)
            selected_analysis = frames.get(engine_label) if engine_label and isinstance(frames.get(engine_label), Mapping) else None
            figure = build_autopilot_chart(
                rows,
                decision,
                title=f"{brief['ticker']} · {label}",
                timeframe=label,
                selected_analysis=selected_analysis,
                earnings_date=decision.get("earnings_date"),
            )
            st.plotly_chart(
                figure,
                width="stretch",
                key=f"autopilot_chart_{brief['ticker']}_{label}",
                config={
                    "displaylogo": False,
                    "scrollZoom": True,
                    "responsive": True,
                    "toImageButtonOptions": {
                        "format": "png",
                        "filename": f"trading-autopilot-{str(brief['ticker']).lower()}-{label}",
                        "scale": 2,
                    },
                },
            )
        except Exception:
            st.info("The annotated chart is unavailable for this response. No visual level was inferred.")
    else:
        st.info("Chart unavailable because current price bars were not returned. No visual level was inferred.")

    symbol = normalize_tradingview_symbol(payload.get("tradingview_symbol") or brief["ticker"])
    url = tradingview_chart_url(symbol, label)
    st.link_button(
        f"Open {brief['ticker']} in TradingView · {label}",
        url,
        width="stretch",
    )
    st.caption(
        f"Opens {public_text(symbol, brief['ticker'], max_length=72)} at {label}. "
        "Your signed-in TradingView drawings and private layouts stay in TradingView; the app does not claim two-way sync."
    )


def _render_multi_timeframe_view(
    st: Any,
    payload: MutableMapping[str, Any],
    brief: Mapping[str, Any],
    provider_key: str,
) -> None:
    enabled = st.toggle(
        "4-chart multi-timeframe view",
        key="_autopilot_multi_timeframe",
        help="Compare Daily, 4H, 1H and 15m without changing the decision engine or the selected single chart.",
    )
    if not enabled:
        return
    st.markdown('<div class="ap-section-title">Multi-timeframe chart wall</div>', unsafe_allow_html=True)
    columns = st.columns(2, gap="small")
    for index, label in enumerate(MULTI_TIMEFRAME_LABELS):
        with columns[index % 2]:
            rows = _chart_rows_for(payload, label, provider_key)
            if not rows:
                st.info(f"{label} bars unavailable.")
                continue
            try:
                decision = brief["safe_decision"]
                frames = decision.get("timeframes") if isinstance(decision.get("timeframes"), Mapping) else {}
                engine_label = _engine_timeframe_label(label)
                selected_analysis = frames.get(engine_label) if engine_label and isinstance(frames.get(engine_label), Mapping) else None
                figure = build_autopilot_chart(
                    rows,
                    decision,
                    title=f"{brief['ticker']} · {label}",
                    max_bars=120,
                    timeframe=label,
                    selected_analysis=selected_analysis,
                    earnings_date=decision.get("earnings_date"),
                    compact=True,
                )
                st.plotly_chart(
                    figure,
                    width="stretch",
                    key=f"autopilot_multi_{brief['ticker']}_{label}",
                    config={"displaylogo": False, "responsive": True, "scrollZoom": True},
                )
            except Exception:
                st.info(f"{label} chart unavailable.")


def _render_export(
    st: Any,
    payload: Mapping[str, Any],
    brief: Mapping[str, Any],
    timeframe: str,
    selected_analysis: Optional[Mapping[str, Any]] = None,
) -> None:
    symbol = normalize_tradingview_symbol(payload.get("tradingview_symbol") or brief["ticker"])
    tradingview_url = tradingview_chart_url(symbol, timeframe)
    public_payload = build_presentation_payload(
        brief["safe_decision"],
        timeframe=timeframe,
        tradingview_symbol=symbol,
        tradingview_url=tradingview_url,
        selected_analysis=selected_analysis,
    )
    st.markdown('<div class="ap-section-title">Present or export</div>', unsafe_allow_html=True)
    left, right = st.columns([1, 1], gap="small")
    with left:
        try:
            pdf_bytes = presentation_pdf_bytes(public_payload)
        except Exception:
            st.info("PDF export is temporarily unavailable. The chart PNG control remains available.")
        else:
            st.download_button(
                "Download decision brief · PDF",
                data=pdf_bytes,
                file_name=f"trading-autopilot-{str(brief['ticker']).lower()}-{timeframe}.pdf",
                mime="application/pdf",
                width="stretch",
            )
    with right:
        st.link_button("Open synchronized TradingView chart", tradingview_url, width="stretch")
    st.caption(
        "Use the camera button in the chart toolbar for a high-resolution PNG. "
        "Presentation Mode and the PDF exclude watchlists, positions, journals, provider errors, paths and credentials."
    )


def _render_options(st: Any, decision: Mapping[str, Any]) -> None:
    st.markdown('<div class="ap-section-title">Options decision</div>', unsafe_allow_html=True)
    options = decision.get("options") if isinstance(decision.get("options"), Mapping) else {}
    rows = options_table_rows(options)
    status = str(options.get("status") or "unavailable").strip().upper()
    if not rows:
        st.info(options_empty_state_message(decision))
        return
    if status == "RECOMMEND":
        st.success("A current contract cleared the underlying ENTER gate and the contract-quality gates.")
    elif status == "WAIT":
        st.warning("Contract rankings are provisional only. Wait for the underlying setup to reach ENTER.")
    else:
        st.info("No contract is recommended. The table is shown only to explain why the chain did not qualify.")
    top = rows[0]
    st.markdown("**Top contract at a glance**")
    st.markdown(
        _html_grid(
            [
                ("Contract", top.get("Contract")),
                ("Expiration / DTE", f"{top.get('Expiration')} · {top.get('DTE')} DTE"),
                ("Strike / type", f"{top.get('Strike')} · {top.get('Type')}"),
                ("Bid / ask", f"{top.get('Bid')} / {top.get('Ask')}"),
                ("Spread", f"{top.get('Spread $')} · {top.get('Spread %')}"),
                ("Liquidity", top.get("Liquidity")),
                ("Greeks", f"Δ {top.get('Delta')} · Γ {top.get('Gamma')} · Θ {top.get('Theta')}"),
                ("Breakeven", top.get("Breakeven")),
            ]
        ),
        unsafe_allow_html=True,
    )
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


def _render_advanced(
    st: Any,
    payload: Mapping[str, Any],
    state: Mapping[str, Any],
    brief: Mapping[str, Any],
    timeframe: str,
    *,
    presentation: bool = False,
) -> None:
    with st.expander("Advanced", expanded=False):
        st.markdown("**Methodology shown on the app-native chart**")
        st.write("9 EMA · 21 WMA · 50 WMA · 200 WMA · 200 SMA · MACD 12/26/9")
        st.caption("The 21-period line is a weighted moving average, not an exponential moving average.")
        raw_health = payload.get("provider_health") if isinstance(payload.get("provider_health"), Mapping) else {}
        health = revalidated_provider_health(raw_health, brief.get("safe_decision"))
        st.markdown("**Source health**")
        st.write(
            f"{public_text(health.get('provider') or brief['source'], 'Unavailable', max_length=48)} · "
            f"{currentness_label(health.get('data_label'))} · "
            f"{format_timestamp(health.get('timestamp'))}"
        )
        st.write(f"Earnings calendar: {earnings_context_label(brief['safe_decision'])}")
        if not presentation:
            diagnostics = advanced_provider_diagnostics(
                health,
                recent_provider_observations(20),
            )
            st.markdown("**Provider diagnostics**")
            age = diagnostics.get("data_age_seconds")
            message_categories = diagnostics.get("message_categories") or {}
            category_text = ", ".join(
                f"{public_text(name, 'other', max_length=24)} {int(count)}"
                for name, count in message_categories.items()
            ) or "none"
            st.write(
                f"Data age: {age:.1f}s" if isinstance(age, (int, float)) else "Data age: unavailable"
            )
            st.caption(
                f"Stale flag: {'yes' if diagnostics.get('stale') else 'no'} · "
                f"Safe provider-warning categories: {category_text}"
            )
            for cache_name in ("analysis", "chart"):
                stats = diagnostics.get("cache", {}).get(cache_name, {})
                st.caption(
                    f"{cache_name.title()} cache · hits {int(stats.get('hits') or 0)} · "
                    f"misses {int(stats.get('misses') or 0)} · loads {int(stats.get('loads') or 0)} · "
                    f"errors {int(stats.get('load_errors') or 0)} · "
                    f"coalesced {int(stats.get('coalesced_waits') or 0)}"
                )
            requests = diagnostics.get("requests") or {}
            classifications = requests.get("classifications") or {}
            classification_text = ", ".join(
                f"{public_text(name, 'provider', max_length=24)} {int(count)}"
                for name, count in classifications.items()
            ) or "none observed"
            st.caption(
                f"Provider requests: {int(requests.get('count') or 0)} · "
                f"retries {int(requests.get('total_retries') or 0)} · "
                f"throttled {int(requests.get('throttled_count') or 0)} · "
                f"max latency {float(requests.get('maximum_latency_ms') or 0):.1f}ms · "
                f"{classification_text}"
            )
            for observation in list(requests.get("recent") or [])[-3:]:
                if not isinstance(observation, Mapping):
                    continue
                st.caption(
                    f"Latest request · {public_text(observation.get('classification'), 'provider', max_length=24)} · "
                    f"{public_text(observation.get('outcome'), 'unknown', max_length=16)} · "
                    f"HTTP {observation.get('status_code') if observation.get('status_code') is not None else '—'} · "
                    f"attempts {int(observation.get('attempts') or 1)} · "
                    f"{float(observation.get('latency_ms') or 0):.1f}ms"
                )
            earnings = diagnostics.get("earnings") or {}
            earnings_latency = earnings.get("latency_ms")
            earnings_latency_label = (
                f"{float(earnings_latency):.1f}ms"
                if isinstance(earnings_latency, (int, float))
                else "unavailable"
            )
            st.caption(
                f"Earnings request · {public_text(earnings.get('status'), 'unresolved', max_length=24)} · "
                f"{public_text(earnings.get('error_kind'), 'none', max_length=24)} · "
                f"HTTP {earnings.get('status_code') if earnings.get('status_code') is not None else '—'} · "
                f"attempts {earnings.get('attempts') if earnings.get('attempts') is not None else '—'} · "
                f"latency {earnings_latency_label}"
            )
            st.caption(
                "Secrets-safe summary only. Provider URLs, symbols, payloads, credentials, prices, and contracts are excluded."
            )
            persistence = st.session_state.get("_autopilot_persistence_mode")
            st.write("Personal state: saved across sessions" if persistence == "persistent" else "Personal state: this session only")
        st.markdown("**Focused ticker-analysis preset**")
        symbol = normalize_tradingview_symbol(payload.get("tradingview_symbol") or brief["ticker"])
        watchlist = [] if presentation else (
            list(state.get("watchlist") or []) if isinstance(state, Mapping) else []
        )
        load_tradingview = st.toggle(
            "Load official TradingView widgets",
            value=False,
            key=f"_autopilot_load_tradingview_{brief['ticker']}_{int(presentation)}",
            help="Loads TradingView's external chart scripts only when you want the embedded views.",
        )
        if load_tradingview:
            try:
                st.iframe(
                    _html_data_url(
                        tradingview_widget_html(
                            symbol,
                            timeframe,
                            watchlist=watchlist,
                            compact=True,
                        )
                    ),
                    height=560,
                    width="stretch",
                )
            except Exception:
                st.info(f"The embedded TradingView view is unavailable. Use the {timeframe} handoff above.")
            st.markdown("**Market-context 2 × 2 preset**")
            st.caption("SPY Daily · SPY 4H · QQQ Daily · QQQ 4H")
            try:
                st.iframe(
                    _html_data_url(tradingview_market_context_html()),
                    height=880,
                    width="stretch",
                )
            except Exception:
                st.info("The market-context preset is unavailable. The app-native SPY/QQQ regime remains visible above.")
        else:
            st.caption("Embedded TradingView charts are on demand so ticker and timeframe changes stay fast.")
        st.caption(
            "The official chart handoff opens the correct symbol and interval. Private TradingView layouts "
            "and drawings remain in your signed-in TradingView session and are not claimed as synchronized."
        )
        outcomes = (
            list(state.get("calibration", {}).get("results", []))
            if not presentation and isinstance(state.get("calibration"), Mapping)
            else []
        )
        if presentation:
            return
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


def _run_search(
    st: Any,
    query: str,
    config: Mapping[str, Any] | Any | None,
    *,
    timeframe: str = DEFAULT_TIMEFRAME,
) -> dict[str, Any]:
    st.session_state["_autopilot_search_query"] = query
    if not _valid_query(query):
        st.warning("Enter one valid ticker, optionally qualified by exchange, such as NASDAQ:AAPL.")
        return _unavailable_payload(query, timeframe)
    provider_key = _provider_key(config)
    with st.status("Analyzing the setup…", expanded=True) as progress:
        st.write("Resolving the symbol and exchange")
        if not provider_key:
            payload = _unavailable_payload(query, timeframe)
            st.write("Current market evidence is unavailable; the result is safely gated to PASS")
            progress.update(label="Analysis unavailable — PASS", state="complete", expanded=False)
            return payload
        st.write("Refreshing market, sector and multi-timeframe evidence")
        try:
            payload = _cached_analysis(query, provider_key, include_options=True)
        except AutopilotServiceError:
            payload = _unavailable_payload(query, timeframe)
        except Exception:
            payload = _unavailable_payload(query, timeframe)
        payload = deepcopy(dict(payload))
        selected = normalize_timeframe(timeframe, default=DEFAULT_TIMEFRAME)
        frames = payload.get("chart_frames") if isinstance(payload.get("chart_frames"), Mapping) else {}
        if selected in frames:
            payload["chart_bars"] = deepcopy(list(frames.get(selected) or []))
        payload["selected_timeframe"] = selected
        symbol = normalize_tradingview_symbol(payload.get("tradingview_symbol") or query)
        payload["tradingview_url"] = tradingview_chart_url(symbol, selected)
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
    presentation = _presentation_mode(st)
    state, _ = _initialize_state(st, config, presentation=presentation)
    presentation = _render_header(st, presentation=presentation)
    if presentation:
        state, _ = _initialize_state(st, config, presentation=True)
    timeframe = _active_timeframe(st)
    submitted, query = _render_search(st)

    query_symbol = _query_value(st, "symbol")
    if (
        not submitted
        and query_symbol
        and not isinstance(st.session_state.get("_autopilot_active_result"), Mapping)
        and not st.session_state.get("_autopilot_query_bootstrapped")
    ):
        submitted = True
        query = query_symbol
        st.session_state["_autopilot_query_bootstrapped"] = True

    if submitted:
        _set_query_values(st, symbol=query.upper(), tf=timeframe, view="presentation" if presentation else None)
        payload = _run_search(st, query, config, timeframe=timeframe)
        st.session_state["_autopilot_active_result"] = payload
        if not presentation:
            _persist_analysis(st, payload)

    payload = st.session_state.get("_autopilot_active_result")
    if not isinstance(payload, Mapping):
        if not presentation:
            _prepare_home_context(st, config)
            state = st.session_state.get("_autopilot_personal_state") or state
        _render_home(st, state, presentation=presentation)
        return

    payload = deepcopy(dict(payload))
    decision = payload.get("decision") if isinstance(payload.get("decision"), Mapping) else {}
    brief = build_decision_brief(decision)
    st.markdown(_market_ribbon_html(brief["safe_decision"]), unsafe_allow_html=True)
    _render_decision_card(st, brief)

    timeframe = _render_timeframe_control(st, timeframe)
    provider_key = _provider_key(config)
    rows = _selected_chart_rows(st, payload, timeframe, provider_key)
    try:
        selected_analysis = asdict(analyze_timeframe(timeframe, rows)) if rows else {}
    except Exception:
        selected_analysis = {}
    chart_column, rail_column = st.columns([2.05, 1], gap="medium")
    with chart_column:
        _render_chart(st, payload, brief, timeframe, rows)
    with rail_column:
        st.markdown(_decision_rail_html(brief), unsafe_allow_html=True)

    _render_multi_timeframe_view(st, payload, brief, provider_key)
    st.markdown('<div class="ap-section-title">Timeframe alignment</div>', unsafe_allow_html=True)
    st.markdown(
        _alignment_html(brief["safe_decision"], timeframe, selected_analysis),
        unsafe_allow_html=True,
    )
    st.markdown(_confluence_html(brief["safe_decision"]), unsafe_allow_html=True)
    st.markdown('<div class="ap-section-title">Scenario map</div>', unsafe_allow_html=True)
    st.markdown(_scenario_html(brief["safe_decision"]), unsafe_allow_html=True)
    _render_options(st, brief["safe_decision"])
    if not presentation:
        _render_tracking_actions(st, payload, brief)
    _render_breakdown(st, brief["safe_decision"])
    if not presentation:
        state = st.session_state.get("_autopilot_personal_state") or state
    _render_advanced(st, payload, state, brief, timeframe, presentation=presentation)
    _render_export(st, payload, brief, timeframe, selected_analysis)
    st.caption("Trading Autopilot provides decision support only. It does not connect to a broker or place orders.")


__all__ = [
    "BREAKDOWN_SECTIONS",
    "advanced_provider_diagnostics",
    "build_decision_brief",
    "build_home_snapshot",
    "build_timeframe_alignment",
    "currentness_label",
    "format_percent",
    "format_price",
    "format_ratio",
    "format_timestamp",
    "options_empty_state_message",
    "options_table_rows",
    "entry_action_allowed",
    "public_text",
    "revalidated_provider_health",
    "render_cockpit",
    "resolve_state_path",
    "verdict_label",
]
