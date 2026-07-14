"""Orchestration layer for one-search Trading Autopilot analysis."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import date as datetime_date, datetime, time as datetime_time, timedelta, timezone
import hashlib
import threading
import time
from typing import Any, Callable, Mapping, Optional
from zoneinfo import ZoneInfo

from autopilot_engine import (
    DecisionResult,
    MarketContext,
    analyze_timeframe,
    build_market_context,
    evaluate_setup,
    normalize_bars,
)
from polygon_provider import (
    InvalidTickerError,
    OHLCVBar,
    PolygonProvider,
    PolygonProviderError,
    ResolvedTicker,
    resample_bars,
)
from timeframes import get_timeframe_spec, normalize_timeframe
from tradingview_integration import normalize_tradingview_symbol, tradingview_chart_url


SECTOR_ETFS = {"XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE", "XLU", "XLV", "XLY"}
TICKER_SECTOR_OVERRIDES = {
    "AAPL": "XLK",
    "AMD": "XLK",
    "AMZN": "XLY",
    "AVGO": "XLK",
    "COIN": "XLF",
    "GOOG": "XLC",
    "GOOGL": "XLC",
    "INTC": "XLK",
    "META": "XLC",
    "MSFT": "XLK",
    "NFLX": "XLC",
    "NVDA": "XLK",
    "PLTR": "XLK",
    "SMCI": "XLK",
    "TSLA": "XLY",
}
MIC_TO_TRADINGVIEW = {
    "XNAS": "NASDAQ",
    "XNYS": "NYSE",
    "ARCX": "AMEX",
    "XASE": "AMEX",
    "BATS": "CBOE",
}


class AutopilotServiceError(RuntimeError):
    """Safe error that can be shown in the product UI."""


@dataclass
class ProviderHealth:
    provider: str
    status: str
    data_label: str
    timestamp: Optional[str]
    messages: list[str]


@dataclass
class ServiceResult:
    decision: DecisionResult
    chart_bars: list[dict[str, Any]]
    chart_frames: dict[str, list[dict[str, Any]]]
    selected_timeframe: str
    journal_bars: list[dict[str, Any]]
    resolved: dict[str, Any]
    tradingview_symbol: str
    tradingview_url: str
    provider_health: ProviderHealth
    fetched_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision.to_dict(),
            "chart_bars": deepcopy(self.chart_bars),
            "chart_frames": deepcopy(self.chart_frames),
            "selected_timeframe": self.selected_timeframe,
            "journal_bars": deepcopy(self.journal_bars),
            "resolved": deepcopy(self.resolved),
            "tradingview_symbol": self.tradingview_symbol,
            "tradingview_url": self.tradingview_url,
            "provider_health": asdict(self.provider_health),
            "fetched_at": self.fetched_at,
        }


class TTLCache:
    def __init__(self, ttl_seconds: int = 300, max_items: int = 128) -> None:
        self.ttl_seconds = max(int(ttl_seconds), 1)
        self.max_items = max(int(max_items), 1)
        self._items: dict[str, tuple[float, Any]] = {}
        self._lock = threading.RLock()

    def get_or_create(self, key: str, factory: Callable[[], Any]) -> Any:
        now = time.monotonic()
        with self._lock:
            cached = self._items.get(key)
            if cached and now - cached[0] < self.ttl_seconds:
                return deepcopy(cached[1])
        value = factory()
        with self._lock:
            self._items[key] = (now, deepcopy(value))
            if len(self._items) > self.max_items:
                oldest = min(self._items.items(), key=lambda item: item[1][0])[0]
                self._items.pop(oldest, None)
        return value

    def clear(self) -> None:
        with self._lock:
            self._items.clear()


_DATA_CACHE = TTLCache(ttl_seconds=300, max_items=96)
_CHART_DATA_CACHE = TTLCache(ttl_seconds=300, max_items=128)

_ENGINE_TO_UI_TIMEFRAME = {
    "5M": "5m",
    "15M": "15m",
    "1H": "1H",
    "4H": "4H",
    "1D": "1D",
    "1W": "1W",
    "1M": "1M",
}
_UI_TO_ENGINE_TIMEFRAME = {value: key for key, value in _ENGINE_TO_UI_TIMEFRAME.items()}
_NATIVE_CHART_METHODS = {
    "1m": "bars_1m",
    "3m": "bars_3m",
    "30m": "bars_30m",
}
_RESAMPLED_CHART_SOURCES = {
    "1H": ("bars_15m", "15m", "1h"),
    "4H": ("bars_15m", "15m", "4h"),
    "1W": ("daily_bars", "1D", "1w"),
    "1M": ("daily_bars", "1D", "1mo"),
}


def _api_fingerprint(api_key: str) -> str:
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:12]


def _cache_namespace(api_key: str, opener: Any = None) -> str:
    """Return a non-secret cache namespace, isolating injected test transports."""

    namespace = _api_fingerprint(api_key)
    return namespace if opener is None else f"{namespace}:transport:{id(opener)}"


def _bar_dict(bar: OHLCVBar) -> dict[str, Any]:
    try:
        timeframe = normalize_timeframe(bar.timeframe)
    except ValueError:
        timeframe = str(bar.timeframe or "")
    return {
        "timestamp": bar.timestamp.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "vwap": bar.vwap,
        "timeframe": timeframe,
        "complete": True,
    }


_MARKET_TIMEZONE = ZoneInfo("America/New_York")


def _bar_completion_time(timestamp: datetime, timeframe: str) -> datetime:
    value = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    label = normalize_timeframe(timeframe)
    durations = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1H": 60, "4H": 240}
    if label in durations:
        duration = durations[label]
        completion = value + timedelta(minutes=duration)
        local_start = value.astimezone(_MARKET_TIMEZONE)
        regular_close = datetime.combine(
            local_start.date(), datetime_time(16, 0), tzinfo=_MARKET_TIMEZONE
        ).astimezone(timezone.utc)
        if datetime_time(9, 30) <= local_start.time() < datetime_time(16, 0):
            completion = min(completion, regular_close)
        return completion
    local = value.astimezone(_MARKET_TIMEZONE)
    if label == "1D":
        return datetime.combine(
            local.date(), datetime_time(16, 0), tzinfo=_MARKET_TIMEZONE
        ).astimezone(timezone.utc)
    if label == "1W":
        monday = local.date() - timedelta(days=local.weekday())
        return datetime.combine(
            monday + timedelta(days=4), datetime_time(16, 0), tzinfo=_MARKET_TIMEZONE
        ).astimezone(timezone.utc)
    if label == "1M":
        if local.month == 12:
            next_month = datetime(local.year + 1, 1, 1, tzinfo=_MARKET_TIMEZONE)
        else:
            next_month = datetime(local.year, local.month + 1, 1, tzinfo=_MARKET_TIMEZONE)
        return next_month.astimezone(timezone.utc)
    return value


def _completed_bars(bars: list[OHLCVBar], timeframe: str, now: datetime) -> list[OHLCVBar]:
    cutoff = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    cutoff = cutoff.astimezone(timezone.utc)
    return [bar for bar in bars if _bar_completion_time(bar.timestamp, timeframe) <= cutoff]


def _frames_for_symbol(
    provider: PolygonProvider,
    symbol: str,
    now: datetime,
    *,
    cache_namespace: str,
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    cache_key = f"{cache_namespace}:{symbol}:{now.date().isoformat()}"

    def fetch() -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
        warnings: list[str] = []
        daily: list[OHLCVBar] = []
        intraday: list[OHLCVBar] = []
        try:
            daily = provider.daily_bars(symbol, (now - timedelta(days=1_100)).date(), now.date())
        except PolygonProviderError as exc:
            warnings.append(str(exc))
        try:
            intraday = provider.bars_15m(symbol, (now - timedelta(days=45)).date(), now.date())
        except PolygonProviderError as exc:
            warnings.append(str(exc))
        five_minute: list[OHLCVBar] = []
        try:
            five_minute = provider.bars_5m(symbol, (now - timedelta(days=15)).date(), now.date())
        except PolygonProviderError as exc:
            warnings.append(str(exc))
        frames: dict[str, list[dict[str, Any]]] = {}
        daily = _completed_bars(daily, "1D", now)
        intraday = _completed_bars(intraday, "15M", now)
        five_minute = _completed_bars(five_minute, "5M", now)
        if daily:
            weekly = _completed_bars(resample_bars(daily, "1w"), "1W", now)
            monthly = _completed_bars(resample_bars(daily, "1mo"), "1M", now)
            frames["1D"] = [_bar_dict(bar) for bar in daily]
            frames["1W"] = [_bar_dict(bar) for bar in weekly]
            frames["1M"] = [_bar_dict(bar) for bar in monthly]
        if intraday:
            hourly = _completed_bars(resample_bars(intraday, "1h"), "1H", now)
            four_hour = _completed_bars(resample_bars(intraday, "4h"), "4H", now)
            frames["15M"] = [_bar_dict(bar) for bar in intraday]
            frames["1H"] = [_bar_dict(bar) for bar in hourly]
            frames["4H"] = [_bar_dict(bar) for bar in four_hour]
        if five_minute:
            frames["5M"] = [_bar_dict(bar) for bar in five_minute]
        return frames, warnings

    return _DATA_CACHE.get_or_create(cache_key, fetch)


def _chart_frames_from_engine_frames(
    frames: Mapping[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Expose decision-source frames under exact, case-sensitive UI labels."""

    output: dict[str, list[dict[str, Any]]] = {}
    for engine_label, ui_label in _ENGINE_TO_UI_TIMEFRAME.items():
        rows = frames.get(engine_label)
        if rows:
            output[ui_label] = deepcopy(list(rows))
    return output


def load_chart_bars(
    ticker: str,
    timeframe: str,
    api_key: str,
    *,
    now: Optional[datetime] = None,
    opener: Any = None,
) -> list[dict[str, Any]]:
    """Load completed bars for one selected chart interval.

    The decision engine's base Daily/5m/15m bundle remains independently
    cached. Native execution intervals and longer resampled views use the
    registry's interval-specific lookback, then cache by provider identity,
    symbol, interval and market date. This keeps 4H/weekly/monthly charts deep
    enough for long moving averages without changing the MTF decision input.
    """

    clean_key = str(api_key or "").strip()
    if not clean_key:
        raise AutopilotServiceError(
            "Live chart data is not connected. Add POLYGON_API_KEY in the deployment secret manager."
        )
    symbol = str(ticker or "").strip().upper()
    if not symbol:
        raise AutopilotServiceError("Ticker is required for chart data.")
    try:
        selected = normalize_timeframe(timeframe)
    except ValueError as exc:
        raise AutopilotServiceError(str(exc)) from None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    namespace = _cache_namespace(clean_key, opener)
    provider = PolygonProvider(clean_key, opener=opener)

    spec = get_timeframe_spec(selected)
    if selected in _NATIVE_CHART_METHODS:
        cache_key = f"{namespace}:{symbol}:{selected}:{current.date().isoformat()}"

        def fetch_source() -> list[dict[str, Any]]:
            method = getattr(provider, _NATIVE_CHART_METHODS[selected])
            bars = method(
                symbol,
                (current - timedelta(days=spec.lookback_days)).date(),
                current.date(),
            )
            completed = _completed_bars(bars, selected, current)
            return [_bar_dict(bar) for bar in completed]

        try:
            return _CHART_DATA_CACHE.get_or_create(cache_key, fetch_source)
        except (InvalidTickerError, PolygonProviderError) as exc:
            raise AutopilotServiceError(str(exc)) from None

    if selected in _RESAMPLED_CHART_SOURCES:
        cache_key = f"{namespace}:{symbol}:{selected}:{current.date().isoformat()}"

        def fetch_resampled() -> list[dict[str, Any]]:
            method_name, source_timeframe, target_timeframe = _RESAMPLED_CHART_SOURCES[selected]
            method = getattr(provider, method_name)
            source_bars = method(
                symbol,
                (current - timedelta(days=spec.lookback_days)).date(),
                current.date(),
            )
            completed_source = _completed_bars(source_bars, source_timeframe, current)
            completed_target = _completed_bars(
                resample_bars(completed_source, target_timeframe),
                selected,
                current,
            )
            return [_bar_dict(bar) for bar in completed_target]

        try:
            return _CHART_DATA_CACHE.get_or_create(cache_key, fetch_resampled)
        except (InvalidTickerError, PolygonProviderError) as exc:
            raise AutopilotServiceError(str(exc)) from None

    engine_label = _UI_TO_ENGINE_TIMEFRAME[selected]
    try:
        frames, _ = _frames_for_symbol(
            provider,
            symbol,
            current,
            cache_namespace=namespace,
        )
    except (InvalidTickerError, PolygonProviderError) as exc:
        raise AutopilotServiceError(str(exc)) from None
    return deepcopy(frames.get(engine_label) or [])


def sector_etf_for_security(security: ResolvedTicker) -> Optional[str]:
    ticker = security.ticker.upper()
    if ticker in SECTOR_ETFS:
        return ticker
    if ticker in TICKER_SECTOR_OVERRIDES:
        return TICKER_SECTOR_OVERRIDES[ticker]
    raw_sic = getattr(security, "sic_code", None)
    try:
        sic = int(str(raw_sic))
    except (TypeError, ValueError):
        return None
    if 6000 <= sic <= 6499:
        return "XLF"
    if 6500 <= sic <= 6553:
        return "XLRE"
    if 4900 <= sic <= 4999:
        return "XLU"
    if 1300 <= sic <= 1399 or 2900 <= sic <= 2999:
        return "XLE"
    if 2830 <= sic <= 2836 or 3841 <= sic <= 3851 or 8000 <= sic <= 8099:
        return "XLV"
    if 3570 <= sic <= 3579 or 3660 <= sic <= 3699 or 7370 <= sic <= 7379:
        return "XLK"
    if 2700 <= sic <= 2749 or 4800 <= sic <= 4899 or 7800 <= sic <= 7899:
        return "XLC"
    if 2000 <= sic <= 2111 or 5400 <= sic <= 5499:
        return "XLP"
    if 2200 <= sic <= 2599 or 5000 <= sic <= 5999 or 3710 <= sic <= 3716:
        return "XLY"
    if 1000 <= sic <= 1299 or 1400 <= sic <= 1499 or 2600 <= sic <= 2899 or 3300 <= sic <= 3399:
        return "XLB"
    if 1500 <= sic <= 1799 or 3400 <= sic <= 3569 or 3700 <= sic <= 4799:
        return "XLI"
    return None


def _pct_change(rows: list[dict[str, Any]], periods: int = 20) -> Optional[float]:
    bars = normalize_bars(rows)
    if len(bars) <= periods or bars[-1]["close"] <= 0 or bars[-1 - periods]["close"] <= 0:
        return None
    return (bars[-1]["close"] / bars[-1 - periods]["close"] - 1) * 100


def _average_daily_dollar_volume(rows: list[dict[str, Any]]) -> Optional[float]:
    bars = normalize_bars(rows)[-20:]
    if not bars:
        return None
    values = [bar["close"] * bar["volume"] for bar in bars if bar["volume"] > 0]
    return sum(values) / len(values) if values else None


def _latest_timestamp(frames: Mapping[str, list[dict[str, Any]]]) -> Optional[datetime]:
    for label in ("15M", "1D"):
        rows = normalize_bars(frames.get(label, []))
        if rows and rows[-1]["timestamp"] > 1_000_000_000:
            started_at = datetime.fromtimestamp(rows[-1]["timestamp"], timezone.utc)
            return _bar_completion_time(started_at, label)
    return None


def _option_contract_input(contract: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "underlying_ticker": contract.get("underlying_ticker"),
        "contract_symbol": contract.get("option_ticker"),
        "option_type": contract.get("contract_type"),
        "expiration": contract.get("expiration_date"),
        "strike": contract.get("strike_price"),
        "bid": contract.get("bid"),
        "ask": contract.get("ask"),
        "mid": contract.get("midpoint"),
        "volume": contract.get("volume"),
        "open_interest": contract.get("open_interest"),
        "implied_volatility": contract.get("implied_volatility"),
        "delta": contract.get("delta"),
        "gamma": contract.get("gamma"),
        "theta": contract.get("theta"),
        "vega": contract.get("vega"),
        "snapshot_timestamp": contract.get("quote_timestamp"),
    }


def _data_label(market: str, latest: Optional[datetime], now: datetime) -> str:
    if latest is None:
        return "unavailable"
    current = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    observed = latest if latest.tzinfo is not None else latest.replace(tzinfo=timezone.utc)
    age_seconds = (current.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    if age_seconds < 0:
        return "stale"
    market_label = str(market or "").strip().lower()
    if market_label == "closed":
        return "last-close" if age_seconds <= 96 * 60 * 60 else "stale"
    if market_label != "open":
        return "stale"
    age_minutes = age_seconds / 60
    if age_minutes <= 2:
        return "real-time"
    if age_minutes <= 30:
        return "delayed"
    return "stale"


def _market_context(
    provider: PolygonProvider,
    ticker_frames: Mapping[str, list[dict[str, Any]]],
    sector_symbol: Optional[str],
    now: datetime,
    namespace: str,
) -> tuple[MarketContext, dict[str, dict[str, list[dict[str, Any]]]], list[str]]:
    all_frames: dict[str, dict[str, list[dict[str, Any]]]] = {}
    warnings: list[str] = []
    for symbol in ["SPY", "QQQ", sector_symbol]:
        if not symbol or symbol in all_frames:
            continue
        frames, errors = _frames_for_symbol(provider, symbol, now, cache_namespace=namespace)
        all_frames[symbol] = frames
        warnings.extend(errors)
    spy_analyses = {label: analyze_timeframe(label, rows) for label, rows in all_frames.get("SPY", {}).items()}
    qqq_analyses = {label: analyze_timeframe(label, rows) for label, rows in all_frames.get("QQQ", {}).items()}
    sector_analyses = {
        label: analyze_timeframe(label, rows)
        for label, rows in all_frames.get(sector_symbol or "", {}).items()
    }
    ticker_change = _pct_change(list(ticker_frames.get("1D", [])))
    benchmarks = [
        change
        for change in [
            _pct_change(all_frames.get("SPY", {}).get("1D", [])),
            _pct_change(all_frames.get("QQQ", {}).get("1D", [])),
            _pct_change(all_frames.get(sector_symbol or "", {}).get("1D", [])),
        ]
        if change is not None
    ]
    relative_strength = ticker_change - (sum(benchmarks) / len(benchmarks)) if ticker_change is not None and benchmarks else None
    context = build_market_context(
        spy_analyses,
        qqq_analyses,
        sector_analyses,
        sector_symbol=sector_symbol,
        relative_strength=relative_strength,
        volatility="unavailable from the configured stock feed",
        breadth="unavailable from the configured stock feed",
        scheduled_events=[],
    )
    return context, all_frames, warnings


def analyze_symbol(
    query: str,
    api_key: str,
    *,
    now: Optional[datetime] = None,
    opener: Any = None,
    include_options: bool = True,
    selected_timeframe: str = "15m",
) -> ServiceResult:
    """Resolve, fetch, analyze, and package one security search."""

    clean_key = str(api_key or "").strip()
    if not clean_key:
        raise AutopilotServiceError("Live analysis is not connected. Add POLYGON_API_KEY in the deployment secret manager.")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    requested_timeframe = selected_timeframe if str(selected_timeframe or "").strip() else "15m"
    try:
        selected = normalize_timeframe(requested_timeframe)
    except ValueError as exc:
        raise AutopilotServiceError(str(exc)) from None
    provider = PolygonProvider(clean_key, opener=opener)
    try:
        resolved = provider.resolve_symbol(query)
    except InvalidTickerError as exc:
        raise AutopilotServiceError(str(exc)) from None
    except PolygonProviderError as exc:
        raise AutopilotServiceError(str(exc)) from None

    namespace = _cache_namespace(clean_key, opener)
    frames, provider_messages = _frames_for_symbol(provider, resolved.ticker, current, cache_namespace=namespace)
    sector_symbol = sector_etf_for_security(resolved)
    market_context, _, market_messages = _market_context(provider, frames, sector_symbol, current, namespace)
    provider_messages.extend(market_messages)
    try:
        market_status = provider.market_status()
        market_label = market_status.market
    except PolygonProviderError as exc:
        market_label = "unknown"
        provider_messages.append(str(exc))

    news_rows: list[dict[str, Any]] = []
    try:
        news_rows = [article.to_dict() for article in provider.news(resolved.ticker, limit=8)]
    except PolygonProviderError as exc:
        provider_messages.append(str(exc))
    earnings_window_end = current.date() + timedelta(days=10)
    earnings = provider.earnings_context(
        resolved.ticker,
        current.date(),
        earnings_window_end,
        checked_at=current,
    )
    earnings_status = str(getattr(earnings, "status", "unresolved") or "unresolved").strip().lower()
    earnings_date_value = getattr(earnings, "date", None)
    earnings_checked_value = getattr(earnings, "checked_through", None)
    if earnings_status not in {"scheduled", "verified_none", "unresolved"}:
        earnings_status = "unresolved"
    if earnings_status == "scheduled" and not isinstance(earnings_date_value, datetime_date):
        earnings_status = "unresolved"
    if earnings_status == "verified_none" and earnings_date_value is not None:
        earnings_status = "unresolved"
    complete_calendar_window = (
        isinstance(earnings_checked_value, datetime_date)
        and earnings_checked_value >= earnings_window_end
    )
    scheduled_date_in_window = (
        isinstance(earnings_date_value, datetime_date)
        and current.date() <= earnings_date_value <= earnings_window_end
    )
    if earnings_status == "verified_none" and not complete_calendar_window:
        earnings_status = "unresolved"
    if earnings_status == "scheduled" and (
        not complete_calendar_window or not scheduled_date_in_window
    ):
        earnings_status = "unresolved"
    earnings_message = getattr(earnings, "message", None)
    if earnings_status == "unresolved" and isinstance(earnings_message, str) and earnings_message:
        provider_messages.append(earnings_message)
    earnings_date = earnings_date_value.isoformat() if isinstance(earnings_date_value, datetime_date) else None
    days_to_earnings = (
        (earnings_date_value - current.date()).days
        if isinstance(earnings_date_value, datetime_date)
        else None
    )
    earnings_checked_through = (
        earnings_checked_value.isoformat()
        if isinstance(earnings_checked_value, datetime_date)
        else None
    )
    latest = _latest_timestamp(frames)
    label = _data_label(market_label, latest, current)
    latest_text = latest.isoformat().replace("+00:00", "Z") if latest else None
    decision = evaluate_setup(
        resolved.ticker,
        frames,
        name=resolved.name,
        exchange=resolved.primary_exchange or "",
        market_context=market_context,
        market_status=market_label,
        data_label=label,
        data_source="Polygon",
        data_timestamp=latest_text,
        average_daily_dollar_volume=_average_daily_dollar_volume(frames.get("1D", [])),
        earnings_date=earnings_date,
        days_to_earnings=days_to_earnings,
        earnings_status=earnings_status,
        earnings_date_status=(
            str(getattr(earnings, "date_status", "") or "").strip().lower() or None
        ),
        earnings_checked_through=earnings_checked_through,
        news=news_rows,
        provider_warnings=provider_messages,
    )

    if include_options and decision.state in {"ENTER", "ARMED"}:
        try:
            raw_contracts = [
                _option_contract_input(contract.to_dict())
                for contract in provider.options_chain(resolved.ticker, max_pages=4)
            ]
            from options_ranker import rank_option_contracts

            options_context: dict[str, Any] = {
                "verdict": decision.verdict,
                "setup_state": decision.state,
                "direction": decision.direction,
                "underlying_ticker": resolved.ticker,
                "underlying_price": decision.current_price,
                "expected_holding_days": 10,
                "target_price": decision.plan.target_2,
                "earnings_policy": "avoid",
                "chain_complete": True,
            }
            if decision.earnings_status == "scheduled" and decision.earnings_date:
                options_context["earnings_date"] = decision.earnings_date
            elif decision.earnings_status == "verified_none":
                options_context["earnings_date"] = None
            decision.options = rank_option_contracts(raw_contracts, options_context, now=current)
        except (ImportError, PolygonProviderError, TypeError, ValueError) as exc:
            decision.options = {
                "status": "unavailable",
                "recommendation": "No contract recommendation",
                "reason": str(exc),
                "contracts": [],
            }
    elif include_options:
        decision.options = {
            "status": "wait" if decision.state in {"FORMING", "ARMED"} else "pass",
            "recommendation": "No contract recommendation",
            "reason": "The underlying setup must reach ENTER before a contract can be recommended.",
            "contracts": [],
        }

    exchange = MIC_TO_TRADINGVIEW.get(str(resolved.primary_exchange or "").upper(), resolved.primary_exchange or "")
    tv_symbol = normalize_tradingview_symbol(resolved.ticker, exchange)
    chart_frames = _chart_frames_from_engine_frames(frames)
    try:
        selected_rows = load_chart_bars(
            resolved.ticker,
            selected,
            clean_key,
            now=current,
            opener=opener,
        )
    except AutopilotServiceError as exc:
        selected_rows = []
        provider_messages.append(str(exc))
    if selected_rows:
        chart_frames[selected] = deepcopy(selected_rows)
    chart_bars = deepcopy(chart_frames.get(selected) or [])
    provider_health = ProviderHealth(
        provider="Polygon + Benzinga" if earnings_status != "unresolved" else "Polygon",
        status="connected" if frames else "error",
        data_label=label,
        timestamp=latest_text,
        messages=provider_messages,
    )
    return ServiceResult(
        decision=decision,
        chart_bars=chart_bars,
        chart_frames=chart_frames,
        selected_timeframe=selected,
        journal_bars=deepcopy(frames.get("1D") or []),
        resolved=resolved.to_dict(),
        tradingview_symbol=tv_symbol,
        tradingview_url=tradingview_chart_url(tv_symbol, selected),
        provider_health=provider_health,
        fetched_at=current.isoformat().replace("+00:00", "Z"),
    )


def unavailable_result(query: str, message: str) -> DecisionResult:
    """Build a truthful PASS result when a live provider is not available."""

    result = evaluate_setup(
        query,
        {},
        market_context=MarketContext(regime="unavailable"),
        market_status="unknown",
        data_label="unavailable",
        data_source="Unavailable",
        provider_warnings=[message],
    )
    return result
