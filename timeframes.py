"""Canonical timeframe definitions shared by market data and chart handoffs.

The UI deliberately distinguishes ``1m`` (one minute) from ``1M`` (one
month).  Callers must normalize through this module instead of case-folding
labels themselves, otherwise those two intervals collide.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class TimeframeSpec:
    """One supported chart interval and its provider retrieval strategy."""

    label: str
    tradingview_interval: str
    intraday: bool
    lookback_days: int
    polygon_multiplier: Optional[int] = None
    polygon_timespan: Optional[str] = None
    resample_from: Optional[str] = None

    @property
    def source_kind(self) -> str:
        return "resample" if self.resample_from else "source"

    @property
    def source_timeframe(self) -> str:
        return self.resample_from or self.label


_SPECS = (
    TimeframeSpec("1m", "1", True, 7, polygon_multiplier=1, polygon_timespan="minute"),
    TimeframeSpec("3m", "3", True, 14, polygon_multiplier=3, polygon_timespan="minute"),
    TimeframeSpec("5m", "5", True, 21, polygon_multiplier=5, polygon_timespan="minute"),
    TimeframeSpec("15m", "15", True, 45, polygon_multiplier=15, polygon_timespan="minute"),
    TimeframeSpec("30m", "30", True, 90, polygon_multiplier=30, polygon_timespan="minute"),
    TimeframeSpec("1H", "60", True, 180, resample_from="15m"),
    TimeframeSpec("4H", "240", True, 730, resample_from="15m"),
    TimeframeSpec("1D", "D", False, 1_100, polygon_multiplier=1, polygon_timespan="day"),
    TimeframeSpec("1W", "W", False, 3_650, resample_from="1D"),
    TimeframeSpec("1M", "M", False, 7_300, resample_from="1D"),
)

TIMEFRAME_LABELS: tuple[str, ...] = tuple(spec.label for spec in _SPECS)
TIMEFRAME_REGISTRY: Mapping[str, TimeframeSpec] = MappingProxyType(
    {spec.label: spec for spec in _SPECS}
)
TRADINGVIEW_INTERVALS: Mapping[str, str] = MappingProxyType(
    {spec.label: spec.tradingview_interval for spec in _SPECS}
)


_ALIASES = {
    "1m": "1m",
    "1min": "1m",
    "1minute": "1m",
    "minute": "1m",
    "3m": "3m",
    "3min": "3m",
    "3minute": "3m",
    "5m": "5m",
    "5min": "5m",
    "5minute": "5m",
    "15m": "15m",
    "15min": "15m",
    "15minute": "15m",
    "30m": "30m",
    "30min": "30m",
    "30minute": "30m",
    "1h": "1H",
    "60m": "1H",
    "hour": "1H",
    "hourly": "1H",
    "4h": "4H",
    "240m": "4H",
    "4hour": "4H",
    "1d": "1D",
    "d": "1D",
    "day": "1D",
    "daily": "1D",
    "1w": "1W",
    "w": "1W",
    "week": "1W",
    "weekly": "1W",
    "1mo": "1M",
    "1mon": "1M",
    "1mth": "1M",
    "1month": "1M",
    "month": "1M",
    "monthly": "1M",
}


def _normalize_nonempty(value: Any) -> str:
    if isinstance(value, TimeframeSpec):
        return value.label
    compact = "".join(str(value or "").strip().split())
    if not compact:
        raise ValueError("Timeframe is required.")

    # Exact UI labels are resolved before case-folding.  This is the crucial
    # distinction between 1m (minute) and 1M (month).
    if compact in TIMEFRAME_REGISTRY:
        return compact
    label = _ALIASES.get(compact.lower())
    if label is None:
        raise ValueError(f"Unsupported timeframe: {compact}")
    return label


def normalize_timeframe(value: Any, *, default: Any = None) -> str:
    """Return an exact UI label, optionally falling back to ``default``."""

    try:
        return _normalize_nonempty(value)
    except ValueError:
        if default is None:
            raise
        return _normalize_nonempty(default)


def get_timeframe_spec(value: Any, *, default: Any = None) -> TimeframeSpec:
    """Resolve a timeframe to its immutable registry entry."""

    return TIMEFRAME_REGISTRY[normalize_timeframe(value, default=default)]


def tradingview_interval_for(value: Any, *, default: Any = "1D") -> str:
    """Return TradingView's official interval token for a supported label."""

    return get_timeframe_spec(value, default=default).tradingview_interval


__all__ = [
    "TIMEFRAME_LABELS",
    "TIMEFRAME_REGISTRY",
    "TRADINGVIEW_INTERVALS",
    "TimeframeSpec",
    "get_timeframe_spec",
    "normalize_timeframe",
    "tradingview_interval_for",
]
