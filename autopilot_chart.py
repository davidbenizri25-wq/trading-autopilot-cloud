"""App-native annotated chart for the Trading Autopilot cockpit."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from autopilot_engine import ema_series, macd_series, normalize_bars, sma_series, wma_series


CHART_COLORS = {
    "background": "#07111F",
    "grid": "rgba(148,163,184,0.08)",
    "text": "#DDE7F3",
    "bull_candle": "#22C55E",
    "bear_candle": "#EF4444",
    "ema9": "#F8FAFC",
    "wma21": "#38BDF8",
    "wma50": "#F59E0B",
    "wma200": "#A78BFA",
    "sma200": "#94A3B8",
    "supply": "rgba(239,68,68,0.13)",
    "demand": "rgba(34,197,94,0.13)",
    "liquidity": "#FBBF24",
    "fakeout": "#FB923C",
    "chop": "rgba(148,163,184,0.16)",
    "bos": "#818CF8",
    "entry": "rgba(14,165,233,0.16)",
    "bull_invalidation": "#166534",
    "bear_invalidation": "#7F1D1D",
    "target1": "#34D399",
    "target2": "#6EE7B7",
    "target3": "#A7F3D0",
}


def _timestamps(bars: list[dict[str, float]]) -> list[Any]:
    result: list[Any] = []
    for bar in bars:
        value = bar["timestamp"]
        result.append(datetime.fromtimestamp(value, timezone.utc) if value > 1_000_000_000 else value)
    return result


def _add_level(figure: Any, value: Any, label: str, color: str, *, dash: str = "solid", width: int = 1) -> None:
    if value is None:
        return
    figure.add_hline(
        y=float(value),
        line_color=color,
        line_dash=dash,
        line_width=width,
        annotation_text=label,
        annotation_position="right",
        annotation_font_color=color,
        row=1,
        col=1,
    )


def build_autopilot_chart(
    rows: Iterable[Mapping[str, Any]],
    decision: Mapping[str, Any],
    *,
    title: str = "",
    max_bars: int = 180,
) -> Any:
    """Return a Plotly candlestick chart synchronized to a decision plan."""

    all_bars = normalize_bars(rows)
    if not all_bars:
        raise ValueError("At least one valid OHLCV bar is required")
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

    figure = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.035,
        row_heights=[0.76, 0.24],
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
                go.Scatter(x=x, y=values, mode="lines", name=name, line={"color": color, "width": width}),
                row=1,
                col=1,
            )

    histogram_colors = [
        CHART_COLORS["bull_candle"] if value is not None and value >= 0 else CHART_COLORS["bear_candle"]
        for value in histogram
    ]
    figure.add_trace(
        go.Bar(x=x, y=histogram, name="MACD histogram", marker_color=histogram_colors, opacity=0.62),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=x, y=macd, mode="lines", name="MACD 12/26", line={"color": "#38BDF8", "width": 1.2}),
        row=2,
        col=1,
    )
    figure.add_trace(
        go.Scatter(x=x, y=signal, mode="lines", name="Signal 9", line={"color": "#F59E0B", "width": 1.1}),
        row=2,
        col=1,
    )

    plan = decision.get("plan", {}) if isinstance(decision, Mapping) else {}
    direction = str(decision.get("direction", "neutral"))
    entry_low = plan.get("entry_low")
    entry_high = plan.get("entry_high")
    if entry_low is not None and entry_high is not None:
        figure.add_hrect(
            y0=min(float(entry_low), float(entry_high)),
            y1=max(float(entry_low), float(entry_high)),
            fillcolor=CHART_COLORS["entry"],
            line_width=0,
            annotation_text="Entry zone",
            annotation_position="top left",
            row=1,
            col=1,
        )
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

    daily = decision.get("timeframes", {}).get("1D", {}) if isinstance(decision, Mapping) else {}
    for value in list(daily.get("support", []))[:2]:
        figure.add_hrect(
            y0=float(value) * 0.9975,
            y1=float(value) * 1.0025,
            fillcolor=CHART_COLORS["demand"],
            line_width=0,
            row=1,
            col=1,
        )
    for value in list(daily.get("resistance", []))[:2]:
        figure.add_hrect(
            y0=float(value) * 0.9975,
            y1=float(value) * 1.0025,
            fillcolor=CHART_COLORS["supply"],
            line_width=0,
            row=1,
            col=1,
        )

    figure.update_layout(
        title=title,
        height=720,
        margin={"l": 12, "r": 72, "t": 52, "b": 18},
        paper_bgcolor=CHART_COLORS["background"],
        plot_bgcolor=CHART_COLORS["background"],
        font={"color": CHART_COLORS["text"], "family": "Inter, ui-sans-serif, system-ui"},
        legend={"orientation": "h", "yanchor": "bottom", "y": 1.01, "xanchor": "left", "x": 0},
        xaxis_rangeslider_visible=False,
        hovermode="x unified",
        dragmode="pan",
    )
    figure.update_xaxes(showgrid=False, rangeslider_visible=False)
    figure.update_yaxes(showgrid=True, gridcolor=CHART_COLORS["grid"], side="right", zeroline=False)
    return figure
