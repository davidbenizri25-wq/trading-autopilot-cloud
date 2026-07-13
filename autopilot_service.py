"""Orchestration layer for one-search Trading Autopilot analysis."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone
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


def _api_fingerprint(api_key: str) -> str:
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:12]


def _bar_dict(bar: OHLCVBar) -> dict[str, Any]:
    return {
        "timestamp": bar.timestamp.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "vwap": bar.vwap,
        "complete": True,
    }


_MARKET_TIMEZONE = ZoneInfo("America/New_York")


def _bar_completion_time(timestamp: datetime, timeframe: str) -> datetime:
    value = timestamp if timestamp.tzinfo is not None else timestamp.replace(tzinfo=timezone.utc)
    value = value.astimezone(timezone.utc)
    label = str(timeframe or "").strip().upper()
    if label in {"5M", "15M", "1H", "4H"}:
        duration = {"5M": 5, "15M": 15, "1H": 60, "4H": 240}[label]
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
) -> ServiceResult:
    """Resolve, fetch, analyze, and package one security search."""

    clean_key = str(api_key or "").strip()
    if not clean_key:
        raise AutopilotServiceError("Live analysis is not connected. Add POLYGON_API_KEY in the deployment secret manager.")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    current = current.astimezone(timezone.utc)
    provider = PolygonProvider(clean_key, opener=opener)
    try:
        resolved = provider.resolve_symbol(query)
    except InvalidTickerError as exc:
        raise AutopilotServiceError(str(exc)) from None
    except PolygonProviderError as exc:
        raise AutopilotServiceError(str(exc)) from None

    namespace = _api_fingerprint(clean_key)
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
        earnings_date=None,
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
            if decision.earnings_date:
                options_context["earnings_date"] = decision.earnings_date
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
    chart_bars = deepcopy(frames.get("15M") or frames.get("1D") or [])
    provider_health = ProviderHealth(
        provider="Polygon",
        status="connected" if frames else "error",
        data_label=label,
        timestamp=latest_text,
        messages=provider_messages,
    )
    return ServiceResult(
        decision=decision,
        chart_bars=chart_bars,
        journal_bars=deepcopy(frames.get("1D") or []),
        resolved=resolved.to_dict(),
        tradingview_symbol=tv_symbol,
        tradingview_url=tradingview_chart_url(tv_symbol, "15m"),
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
