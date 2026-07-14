"""App-native annotated chart for the Trading Autopilot cockpit."""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Optional
from zoneinfo import ZoneInfo

from autopilot_engine import (
    analyze_timeframe,
    ema_series,
    macd_series,
    normalize_bars,
    sma_series,
    wma_series,
)
from timeframes import get_timeframe_spec, normalize_timeframe


# These colors were matched to David's authenticated TradingView layout without
# modifying the saved layout: white/blue candles and the existing MA palette.
CHART_COLORS = {
    "background": "#131722",
    "grid": "rgba(42,46,57,0.72)",
    "text": "#D1D4DC",
    "muted": "#787B86",
    "bull_candle": "#F8FAFC",
    "bear_candle": "#2962FF",
    "ema9": "#2962FF",
    "wma21": "#F23645",
    "wma50": "#FDD835",
    "wma200": "#AB47BC",
    "sma200": "#26A69A",
    "vwap": "#EC4899",
    "macd": "#2962FF",
    "macd_signal": "#FF9800",
    "macd_positive": "#80CBC4",
    "macd_negative": "#F48FB1",
    "supply": "rgba(242,54,69,0.13)",
    "demand": "rgba(8,153,129,0.13)",
    "liquidity": "#FBBF24",
    "fakeout": "#FB923C",
    "chop": "rgba(120,123,134,0.16)",
    "bos": "#818CF8",
    "entry": "rgba(41,98,255,0.16)",
    "bull_invalidation": "#0B7A69",
    "bear_invalidation": "#B22835",
    "target1": "#34D399",
    "target2": "#6EE7B7",
    "target3": "#A7F3D0",
    "earnings": "#D946EF",
}

_MARKET_TIMEZONE = ZoneInfo("America/New_York")


def _timestamps(bars: list[dict[str, float]]) -> list[Any]:
    result: list[Any] = []
    for bar in bars:
        value = bar["timestamp"]
        result.append(datetime.fromtimestamp(value, timezone.utc) if value > 1_000_000_000 else value)
    return result


def _number(value: Any) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result == result and result not in {float("inf"), float("-inf")} else None


def _add_level(
    figure: Any,
    value: Any,
    label: str,
    color: str,
    *,
    dash: str = "solid",
    width: int = 1,
) -> None:
    number = _number(value)
    if number is None:
        return
    figure.add_hline(
        y=number,
        line_color=color,
        line_dash=dash,
        line_width=width,
        annotation_text=label,
        annotation_position="right",
        annotation_font_color=color,
        row=1,
        col=1,
    )


def _session_vwap(bars: list[dict[str, float]]) -> list[Optional[float]]:
    """Return cumulative session VWAP, resetting by New York date."""

    output: list[Optional[float]] = []
    session: Any = None
    cumulative_value = 0.0
    cumulative_volume = 0.0
    for bar in bars:
        timestamp = bar["timestamp"]
        if timestamp > 1_000_000_000:
            current_session = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(_MARKET_TIMEZONE).date()
        else:
            current_session = "synthetic"
        if current_session != session:
            session = current_session
            cumulative_value = 0.0
            cumulative_volume = 0.0
        volume = max(float(bar.get("volume") or 0.0), 0.0)
        typical_price = (bar["high"] + bar["low"] + bar["close"]) / 3
        cumulative_value += typical_price * volume
        cumulative_volume += volume
        output.append(cumulative_value / cumulative_volume if cumulative_volume else None)
    return output


def _analysis_values(
    all_bars: list[dict[str, float]],
    timeframe: str,
    selected_analysis: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    calculated = asdict(analyze_timeframe(timeframe, all_bars))
    if selected_analysis:
        # Prefer recalculation from the exact visible bars. The MTF analysis is
        # only a fallback when the selected view is too short to derive a
        # level, and this copied mapping is never mutated.
        supplied = dict(selected_analysis)
        for key in ("close", "atr"):
            if calculated.get(key) is None and supplied.get(key) is not None:
                calculated[key] = supplied[key]
        for key in ("support", "resistance"):
            if not calculated.get(key) and supplied.get(key):
                calculated[key] = list(supplied[key])
        if calculated.get("direction") in {None, "", "unavailable"} and supplied.get("direction"):
            calculated["direction"] = supplied["direction"]
    return calculated


def _earnings_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time().replace(hour=16))
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            try:
                parsed_date = date.fromisoformat(text[:10])
            except ValueError:
                return None
            parsed = datetime.combine(parsed_date, datetime.min.time().replace(hour=16))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_MARKET_TIMEZONE)
    return parsed.astimezone(timezone.utc)


def _add_price_zone(
    figure: Any,
    value: Any,
    half_width: float,
    *,
    label: Optional[str],
    color: str,
) -> None:
    number = _number(value)
    if number is None:
        return
    figure.add_hrect(
        y0=number - half_width,
        y1=number + half_width,
        fillcolor=color,
        line_width=0,
        annotation_text=label,
        annotation_position="top left",
        row=1,
        col=1,
    )


def build_autopilot_chart(
    rows: Iterable[Mapping[str, Any]],
    decision: Mapping[str, Any],
    *,
    title: str = "",
    max_bars: int = 180,
    timeframe: str = "15m",
    selected_analysis: Optional[Mapping[str, Any]] = None,
    earnings_date: Any = None,
    compact: bool = False,
) -> Any:
    """Return a TradingView-inspired price, volume, and MACD decision chart.

    ``timeframe`` controls chart-only analysis and annotations. The supplied
    multi-timeframe ``decision`` remains read-only, so changing the visible
    interval cannot alter the ENTER / WAIT / PASS result.
    """

    all_bars = normalize_bars(rows)
    if not all_bars:
        raise ValueError("At least one valid OHLCV bar is required")
    selected = normalize_timeframe(timeframe or "15m")
    spec = get_timeframe_spec(selected)
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError as exc:  # pragma: no cover - deployment dependency guard
        raise RuntimeError("Plotly is required for the app-native chart") from exc

    all_closes = [bar["close"] for bar in all_bars]
    display_count = max(40, int(max_bars))
    bars = all_bars[-display_count:]
    closes = all_closes[-display_count:]
    x = _timestamps(bars)
    ema9 = ema_series(all_closes, 9)[-display_count:]
    wma21 = wma_series(all_closes, 21)[-display_count:]
    wma50 = wma_series(all_closes, 50)[-display_count:]
    wma200 = wma_series(all_closes, 200)[-display_count:]
    sma200 = sma_series(all_closes, 200)[-display_count:]
    all_macd, all_signal, all_histogram = macd_series(all_closes)
    macd = all_macd[-display_count:]
    signal = all_signal[-display_count:]
    histogram = all_histogram[-display_count:]
    analysis = _analysis_values(all_bars, selected, selected_analysis)

    figure = make_subplots(
        rows=3,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.026 if compact else 0.032,
        row_heights=[0.67, 0.14, 0.19] if compact else [0.66, 0.15, 0.19],
    )
    figure.add_trace(
        go.Candlestick(
            x=x,
            open=[bar["open"] for bar in bars],
            high=[bar["high"] for bar in bars],
            low=[bar["low"] for bar in bars],
            close=closes,
            name="Price",
            increasing_line_color=CHART_COLORS["bull_candle"],
            increasing_fillcolor=CHART_COLORS["bull_candle"],
            decreasing_line_color=CHART_COLORS["bear_candle"],
            decreasing_fillcolor=CHART_COLORS["bear_candle"],
        ),
        row=1,
        col=1,
    )
    for values, name, color, width in [
        (ema9, "9 EMA", CHART_COLORS["ema9"], 1.2),
        (wma21, "21 WMA", CHART_COLORS["wma21"], 1.6),
        (wma50, "50 WMA", CHART_COLORS["wma50"], 1.4),
        (wma200, "200 WMA", CHART_COLORS["wma200"], 1.7),
        (sma200, "200 SMA", CHART_COLORS["sma200"], 1.0),
    ]:
        if any(value is not None for value in values):
            figure.add_trace(
                go.Scatter(
                    x=x,
                    y=values,
                    mode="lines",
                    name=name,
                    line={"color": color, "width": width},
                    hovertemplate=f"{name} %{{y:.2f}}<extra></extra>",
                ),
                row=1,
                col=1,
            )

    if spec.intraday:
        vwap = _session_vwap(all_bars)[-display_count:]
        if any(value is not None for value in vwap):
            figure.add_trace(
                go.Scatter(
                    x=x,
                    y=vwap,
                    mode="lines",
                    name="Session VWAP",
                    line={"color": CHART_COLORS["vwap"], "width": 1.5, "dash": "dot"},
                    hovertemplate="VWAP %{y:.2f}<extra></extra>",
                ),
                row=1,
                col=1,
            )

    volume_colors = [
        CHART_COLORS["bull_candle"] if bar["close"] >= bar["open"] else CHART_COLORS["bear_candle"]
        for bar in bars
    ]
    figure.add_trace(
        go.Bar(
            x=x,
            y=[bar["volume"] for bar in bars],
            name="Volume",
            marker_color=volume_colors,
            opacity=0.56,
            hovertemplate="Volume %{y:,.0f}<extra></extra>",
        ),
        row=2,
        col=1,
    )

    histogram_colors = [
        CHART_COLORS["macd_positive"] if value is not None and value >= 0 else CHART_COLORS["macd_negative"]
        for value in histogram
    ]
    figure.add_trace(
        go.Bar(x=x, y=histogram, name="MACD histogram", marker_color=histogram_colors, opacity=0.62),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=x, y=macd, mode="lines", name="MACD 12/26", line={"color": CHART_COLORS["macd"], "width": 1.2}),
        row=3,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=x, y=signal, mode="lines", name="Signal 9", line={"color": CHART_COLORS["macd_signal"], "width": 1.1}),
        row=3,
        col=1,
    )

    plan = decision.get("plan", {}) if isinstance(decision, Mapping) else {}
    direction = str(decision.get("direction", "neutral")) if isinstance(decision, Mapping) else "neutral"
    entry_low = _number(plan.get("entry_low")) if isinstance(plan, Mapping) else None
    entry_high = _number(plan.get("entry_high")) if isinstance(plan, Mapping) else None
    if entry_low is not None and entry_high is not None:
        figure.add_hrect(
            y0=min(entry_low, entry_high),
            y1=max(entry_low, entry_high),
            fillcolor=CHART_COLORS["entry"],
            line_width=0,
            annotation_text="Entry zone",
            annotation_position="top left",
            row=1,
            col=1,
        )
    if isinstance(plan, Mapping):
        _add_level(figure, plan.get("trigger"), "Trigger / BOS", CHART_COLORS["bos"], dash="dash", width=2)
        _add_level(
            figure,
            plan.get("invalidation"),
            "Invalidation",
            CHART_COLORS["bull_invalidation"] if direction == "bullish" else CHART_COLORS["bear_invalidation"],
            width=2,
        )
        _add_level(figure, plan.get("target_1"), "T1", CHART_COLORS["target1"], dash="dot")
        _add_level(figure, plan.get("target_2"), "T2", CHART_COLORS["target2"], dash="dot")
        _add_level(figure, plan.get("stretch_target"), "Stretch", CHART_COLORS["target3"], dash="dot")

    atr = _number(analysis.get("atr"))
    reference_price = _number(analysis.get("close")) or closes[-1]
    half_width = max((atr or 0.0) * 0.12, reference_price * 0.0015)
    for index, value in enumerate(list(analysis.get("support") or [])[:2]):
        _add_price_zone(
            figure,
            value,
            half_width,
            label=f"{selected} demand" if index == 0 else None,
            color=CHART_COLORS["demand"],
        )
    for index, value in enumerate(list(analysis.get("resistance") or [])[:2]):
        _add_price_zone(
            figure,
            value,
            half_width,
            label=f"{selected} supply" if index == 0 else None,
            color=CHART_COLORS["supply"],
        )
    if str(analysis.get("direction") or "").lower() == "mixed" and atr:
        figure.add_hrect(
            y0=reference_price - atr * 0.35,
            y1=reference_price + atr * 0.35,
            fillcolor=CHART_COLORS["chop"],
            line_width=0,
            annotation_text=f"{selected} chop / no-trade",
            annotation_position="bottom left",
            row=1,
            col=1,
        )

    requested_earnings = earnings_date
    if requested_earnings is None and isinstance(decision, Mapping):
        requested_earnings = decision.get("earnings_date")
    marker = _earnings_datetime(requested_earnings)
    dated_x = [value for value in x if isinstance(value, datetime)]
    if marker is not None and dated_x:
        span = max(dated_x[-1] - dated_x[0], timedelta(days=1))
        earliest = dated_x[0] - span * 0.05
        latest = dated_x[-1] + max(span * 0.20, timedelta(days=14))
        if earliest <= marker <= latest:
            figure.add_vline(
                x=marker,
                line_color=CHART_COLORS["earnings"],
                line_dash="dash",
                line_width=1.5,
                annotation_text="Earnings",
                annotation_position="top",
                annotation_font_color=CHART_COLORS["earnings"],
                row=1,
                col=1,
            )

    chart_title = title or f"{selected} · annotated decision chart"
    figure.update_layout(
        title=chart_title,
        height=440 if compact else 760,
        margin={"l": 8 if compact else 12, "r": 50 if compact else 72, "t": 42 if compact else 58, "b": 12},
        paper_bgcolor=CHART_COLORS["background"],
        plot_bgcolor=CHART_COLORS["background"],
        font={"color": CHART_COLORS["text"], "family": "Inter, ui-sans-serif, system-ui"},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.01,
            "xanchor": "left",
            "x": 0,
            "font": {"size": 9 if compact else 11},
        },
        showlegend=not compact,
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        dragmode="pan",
        bargap=0.08,
    )
    figure.update_xaxes(showgrid=False, rangeslider_visible=False, color=CHART_COLORS["muted"])
    figure.update_yaxes(
        showgrid=True,
        gridcolor=CHART_COLORS["grid"],
        side="right",
        zeroline=False,
        color=CHART_COLORS["muted"],
    )
    figure.update_yaxes(title_text="Price", row=1, col=1)
    figure.update_yaxes(title_text="Vol", showticklabels=not compact, row=2, col=1)
    figure.update_yaxes(title_text="MACD", showticklabels=not compact, row=3, col=1)
    return figure


__all__ = ["CHART_COLORS", "build_autopilot_chart"]
