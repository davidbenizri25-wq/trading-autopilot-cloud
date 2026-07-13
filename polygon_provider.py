"""Read-only Polygon market-data provider primitives.

The module intentionally contains no broker, account, order, alert, or
persistence operations.  It uses only Python's standard library and accepts an
injectable URL opener so every transport path can be tested without live
network access.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union
from urllib.error import HTTPError
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen
from zoneinfo import ZoneInfo


POLYGON_BASE_URL = "https://api.polygon.io"
POLYGON_HOST = "api.polygon.io"
MAX_ERROR_MESSAGE_CHARS = 240

JsonDict = dict[str, Any]
UrlOpener = Callable[[str], Any]
DateLike = Union[str, date, datetime]

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,31}$")
_MIC_RE = re.compile(r"^[A-Z0-9]{4}$")
_SENSITIVE_NAME_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|polygon_api_key|access[_-]?token|token|password|secret)"
)
_SENSITIVE_PAIR_RE = re.compile(
    r"(?i)(api[_-]?key|apikey|polygon_api_key|access[_-]?token|token|password|secret)"
    r"(\s*[=:]\s*[\"']?)([^\s&;,\"'}]+)"
)
_BEARER_RE = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/%=\-]+")

_EXCHANGE_ALIASES = {
    "NASDAQ": "XNAS",
    "NASDAQGS": "XNAS",
    "NASDAQGM": "XNAS",
    "NASDAQCM": "XNAS",
    "XNAS": "XNAS",
    "XNGS": "XNAS",
    "XNCM": "XNAS",
    "NYSE": "XNYS",
    "XNYS": "XNYS",
    "NYSEARCA": "ARCX",
    "ARCA": "ARCX",
    "ARCX": "ARCX",
    "AMEX": "XASE",
    "NYSEAMERICAN": "XASE",
    "XASE": "XASE",
    "BATS": "BATS",
    "CBOE": "BATS",
}

_SOURCE_TIMEFRAMES = {
    "1d": (1, "day", "1d"),
    "d": (1, "day", "1d"),
    "day": (1, "day", "1d"),
    "daily": (1, "day", "1d"),
    "5m": (5, "minute", "5m"),
    "5min": (5, "minute", "5m"),
    "5minute": (5, "minute", "5m"),
    "15m": (15, "minute", "15m"),
    "15min": (15, "minute", "15m"),
    "15minute": (15, "minute", "15m"),
}

_TARGET_TIMEFRAMES = {
    "1h": "1h",
    "60m": "1h",
    "hour": "1h",
    "hourly": "1h",
    "4h": "4h",
    "240m": "4h",
    "4hour": "4h",
    "1w": "1w",
    "w": "1w",
    "week": "1w",
    "weekly": "1w",
    "1mo": "1mo",
    "1mth": "1mo",
    "month": "1mo",
    "monthly": "1mo",
}

_HTTP_LABELS = {
    400: "bad request",
    401: "unauthorized",
    403: "forbidden",
    404: "not found",
    408: "timeout",
    429: "rate limited",
    500: "provider unavailable",
    502: "provider unavailable",
    503: "provider unavailable",
    504: "provider timeout",
}


def _is_sensitive_name(value: Any) -> bool:
    return _SENSITIVE_NAME_RE.search(str(value or "")) is not None


class PolygonProviderError(RuntimeError):
    """A conservative, user-safe provider failure."""

    def __init__(
        self,
        message: str,
        *,
        operation: str = "request",
        status_code: Optional[int] = None,
    ) -> None:
        super().__init__(message)
        self.operation = operation
        self.status_code = status_code


class InvalidTickerError(PolygonProviderError, ValueError):
    """Ticker or exchange input is not safe to send to the provider."""


class SymbolNotFoundError(PolygonProviderError):
    """The reference endpoint did not return an exact matching security."""


class AmbiguousSymbolError(PolygonProviderError):
    """More than one exact security remained after conservative filtering."""


@dataclass(frozen=True)
class TickerQuery:
    ticker: str
    exchange: Optional[str] = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(frozen=True)
class ResolvedTicker:
    ticker: str
    name: str
    primary_exchange: Optional[str]
    market: Optional[str]
    locale: Optional[str]
    security_type: Optional[str]
    active: Optional[bool]
    currency_name: Optional[str]
    composite_figi: Optional[str]
    share_class_figi: Optional[str]
    last_updated: Optional[datetime]
    sic_code: Optional[str] = None
    sic_description: Optional[str] = None
    homepage_url: Optional[str] = None
    market_cap: Optional[float] = None
    weighted_shares_outstanding: Optional[int] = None
    branding_icon_url: Optional[str] = None
    branding_logo_url: Optional[str] = None

    @property
    def icon_url(self) -> Optional[str]:
        return self.branding_icon_url

    @property
    def logo_url(self) -> Optional[str]:
        return self.branding_logo_url

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["last_updated"] = _datetime_text(self.last_updated)
        data["icon_url"] = self.branding_icon_url
        data["logo_url"] = self.branding_logo_url
        return data


@dataclass(frozen=True)
class StockSnapshot:
    ticker: str
    price: Optional[float]
    price_source: Optional[str]
    change: Optional[float]
    change_percent: Optional[float]
    updated_at: Optional[datetime]
    updated_source: Optional[str]
    previous_close: Optional[float]
    day_open: Optional[float]
    day_high: Optional[float]
    day_low: Optional[float]
    day_close: Optional[float]
    day_volume: Optional[float]

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["updated_at"] = _datetime_text(self.updated_at)
        return data


@dataclass(frozen=True)
class OHLCVBar:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    timeframe: str
    vwap: Optional[float] = None
    transactions: Optional[int] = None
    source_count: int = 1

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["timestamp"] = _datetime_text(self.timestamp)
        return data


@dataclass(frozen=True)
class MarketStatus:
    market: str
    server_time: Optional[datetime]
    early_hours: Optional[bool]
    after_hours: Optional[bool]
    exchanges: dict[str, str]
    currencies: dict[str, str]

    @property
    def is_open(self) -> bool:
        return self.market.strip().lower() == "open"

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["server_time"] = _datetime_text(self.server_time)
        data["is_open"] = self.is_open
        return data


@dataclass(frozen=True)
class NewsArticle:
    article_id: str
    title: str
    published_at: Optional[datetime]
    article_url: Optional[str]
    author: Optional[str]
    description: Optional[str]
    tickers: tuple[str, ...]
    keywords: tuple[str, ...]
    publisher_name: Optional[str]
    publisher_homepage_url: Optional[str]

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["published_at"] = _datetime_text(self.published_at)
        data["tickers"] = list(self.tickers)
        data["keywords"] = list(self.keywords)
        return data


@dataclass(frozen=True)
class OptionContractSnapshot:
    underlying_ticker: str
    option_ticker: str
    contract_type: Optional[str]
    expiration_date: Optional[date]
    strike_price: Optional[float]
    exercise_style: Optional[str]
    shares_per_contract: Optional[int]
    bid: Optional[float]
    ask: Optional[float]
    midpoint: Optional[float]
    spread: Optional[float]
    spread_percent: Optional[float]
    delta: Optional[float]
    gamma: Optional[float]
    theta: Optional[float]
    vega: Optional[float]
    open_interest: Optional[int]
    volume: Optional[float]
    implied_volatility: Optional[float]
    break_even_price: Optional[float]
    quote_timestamp: Optional[datetime]
    trade_timestamp: Optional[datetime]
    day_timestamp: Optional[datetime]
    underlying_timestamp: Optional[datetime]
    data_timeframe: Optional[str]

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["expiration_date"] = (
            self.expiration_date.isoformat() if self.expiration_date is not None else None
        )
        for field_name in [
            "quote_timestamp",
            "trade_timestamp",
            "day_timestamp",
            "underlying_timestamp",
        ]:
            data[field_name] = _datetime_text(getattr(self, field_name))
        return data


def _clean_optional_text(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _finite_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _nonnegative_float(value: Any) -> Optional[float]:
    number = _finite_float(value)
    if number is None or number < 0:
        return None
    return number


def _positive_float(value: Any) -> Optional[float]:
    number = _finite_float(value)
    if number is None or number <= 0:
        return None
    return number


def _safe_int(value: Any) -> Optional[int]:
    number = _finite_float(value)
    if number is None or number < 0 or not number.is_integer():
        return None
    return int(number)


def _datetime_text(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, "") or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=timezone.utc)

    number = _finite_float(value)
    if number is not None and not isinstance(value, str):
        magnitude = abs(number)
        if magnitude >= 1e17:  # nanoseconds
            number /= 1_000_000_000
        elif magnitude >= 1e14:  # microseconds
            number /= 1_000_000
        elif magnitude >= 1e11:  # milliseconds
            number /= 1_000
        try:
            return datetime.fromtimestamp(number, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        try:
            numeric = float(text)
        except ValueError:
            return None
        magnitude = abs(numeric)
        if magnitude >= 1e17:
            numeric /= 1_000_000_000
        elif magnitude >= 1e14:
            numeric /= 1_000_000
        elif magnitude >= 1e11:
            numeric /= 1_000
        try:
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_date(value: Any) -> Optional[date]:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def normalize_exchange(exchange: Any) -> Optional[str]:
    """Return a canonical MIC-style exchange code or reject unsafe input."""

    if exchange is None or not str(exchange).strip():
        return None
    compact = re.sub(r"[\s_\-]+", "", str(exchange).strip().upper())
    if compact in _EXCHANGE_ALIASES:
        return _EXCHANGE_ALIASES[compact]
    if _MIC_RE.fullmatch(compact):
        return compact
    raise InvalidTickerError("Exchange must be a known name or four-character MIC code.")


def _qualified_exchange_token(exchange: Any) -> Optional[str]:
    """Recognize exchange tokens inside a colon-qualified ticker.

    Four-letter tickers such as AAPL must not be mistaken for arbitrary MIC
    codes.  Explicit ``exchange=`` values may still use any valid four-character
    MIC through :func:`normalize_exchange`.
    """

    compact = re.sub(r"[\s_\-]+", "", str(exchange or "").strip().upper())
    if compact in _EXCHANGE_ALIASES:
        return _EXCHANGE_ALIASES[compact]
    if _MIC_RE.fullmatch(compact) and (
        compact.startswith("X") or compact in {"ARCX", "BATS", "IEXG", "MEMX"}
    ):
        return compact
    return None


def sanitize_ticker(value: Any) -> str:
    """Normalize a US security ticker without deleting unsafe characters."""

    text = str(value or "").strip().upper()
    if text.startswith("$"):
        text = text[1:]
    text = text.replace("/", ".")
    if not text or not _TICKER_RE.fullmatch(text) or ".." in text:
        raise InvalidTickerError("Ticker must contain only letters, numbers, dots, or hyphens.")
    return text


def parse_ticker(value: Any, exchange: Any = None) -> TickerQuery:
    """Parse ``TICKER``, ``EXCHANGE:TICKER``, or ``TICKER:EXCHANGE`` input."""

    raw = str(value or "").strip()
    parsed_exchange = normalize_exchange(exchange)
    ticker_text = raw
    if ":" in raw:
        if raw.count(":") != 1 or exchange not in (None, ""):
            raise InvalidTickerError("Use one ticker and, optionally, one exchange.")
        left, right = (part.strip() for part in raw.split(":", 1))
        if not left or not right:
            raise InvalidTickerError("Ticker and exchange cannot be empty.")
        left_exchange = _qualified_exchange_token(left)
        right_exchange = _qualified_exchange_token(right)
        if left_exchange is not None and right_exchange is None:
            parsed_exchange, ticker_text = left_exchange, right
        elif right_exchange is not None and left_exchange is None:
            parsed_exchange, ticker_text = right_exchange, left
        else:
            raise InvalidTickerError("Exchange-qualified ticker input is ambiguous.")
    return TickerQuery(ticker=sanitize_ticker(ticker_text), exchange=parsed_exchange)


parse_ticker_input = parse_ticker


def _path_segment(value: str) -> str:
    return quote(value, safe="")


def build_polygon_url(
    path: str,
    params: Optional[Mapping[str, Any]] = None,
    *,
    api_key: Optional[str] = None,
) -> str:
    """Build a Polygon URL from a trusted relative path and encoded query."""

    parsed = urlsplit(str(path or ""))
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith("/")
        or "\\" in parsed.path
        or any(part == ".." for part in parsed.path.split("/"))
    ):
        raise ValueError("Polygon endpoint path must be a safe absolute path.")

    query_items: list[tuple[str, Any]] = []
    for key, value in (params or {}).items():
        if value is None:
            continue
        if _is_sensitive_name(key):
            raise ValueError("Credentials must be provided with api_key, not query params.")
        if isinstance(value, (list, tuple)):
            query_items.extend((str(key), item) for item in value if item is not None)
        else:
            query_items.append((str(key), value))
    if api_key is not None:
        clean_key = str(api_key).strip()
        if not clean_key:
            raise PolygonProviderError("Polygon API key is required.")
        query_items.append(("apiKey", clean_key))
    query = urlencode(query_items, doseq=True)
    return urlunsplit(("https", POLYGON_HOST, parsed.path, query, ""))


def redact_sensitive_text(value: Any, api_key: Optional[str] = None) -> str:
    """Redact provider credentials from URLs, JSON-like text, and exceptions."""

    text = str(value or "")
    if api_key:
        key = str(api_key)
        for candidate in {key, quote(key, safe=""), quote(key, safe="%") }:
            if candidate:
                text = text.replace(candidate, "[redacted]")
    text = _SENSITIVE_PAIR_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[redacted]", text)
    text = _BEARER_RE.sub(r"\1[redacted]", text)
    text = " ".join(text.split())
    if len(text) > MAX_ERROR_MESSAGE_CHARS:
        text = text[: MAX_ERROR_MESSAGE_CHARS - 3].rstrip() + "..."
    return text


def redact_url(url: str, api_key: Optional[str] = None) -> str:
    """Return a display-safe URL while preserving non-sensitive query fields."""

    try:
        parsed = urlsplit(str(url))
        items = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            if _is_sensitive_name(key):
                items.append((key, "[redacted]"))
            else:
                items.append((key, value))
        redacted = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(items), parsed.fragment))
    except Exception:
        redacted = str(url)
    return redact_sensitive_text(redacted, api_key=api_key)


redact_api_key = redact_sensitive_text


def build_ticker_reference_url(
    ticker: Any,
    api_key: str,
    *,
    exchange: Any = None,
    limit: int = 20,
) -> str:
    query = parse_ticker(ticker, exchange)
    bounded_limit = _bounded_limit(limit, maximum=1000)
    return build_polygon_url(
        "/v3/reference/tickers",
        {
            "ticker": query.ticker,
            "market": "stocks",
            "locale": "us",
            "active": "true",
            "limit": bounded_limit,
            "sort": "ticker",
            "order": "asc",
        },
        api_key=api_key,
    )


def build_ticker_details_url(ticker: Any, api_key: str) -> str:
    """Build the official exact-ticker reference details endpoint URL."""

    clean_ticker = parse_ticker(ticker).ticker
    return build_polygon_url(
        f"/v3/reference/tickers/{_path_segment(clean_ticker)}",
        api_key=api_key,
    )


def _normalize_watchlist_tickers(tickers: Union[str, Iterable[Any]]) -> list[str]:
    if tickers is None:
        raise ValueError("At least one watchlist ticker is required.")
    if isinstance(tickers, str):
        raw_values: Iterable[Any] = [
            value for value in re.split(r"[,\s]+", tickers.strip()) if value
        ]
    else:
        raw_values = tickers
    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        if isinstance(value, TickerQuery):
            ticker = value.ticker
        else:
            ticker = parse_ticker(value).ticker
        if ticker not in seen:
            normalized.append(ticker)
            seen.add(ticker)
    if not normalized:
        raise ValueError("At least one watchlist ticker is required.")
    return normalized


def build_stock_snapshots_url(
    tickers: Union[str, Iterable[Any]],
    api_key: str,
    *,
    include_otc: bool = False,
) -> str:
    """Build Polygon's standard multi-ticker US stocks snapshot URL."""

    normalized = _normalize_watchlist_tickers(tickers)
    if len(normalized) > 250:
        raise ValueError("One stock snapshot request supports at most 250 tickers.")
    return build_polygon_url(
        "/v2/snapshot/locale/us/markets/stocks/tickers",
        {
            "tickers": ",".join(normalized),
            "include_otc": "true" if include_otc else "false",
        },
        api_key=api_key,
    )


def build_aggregates_url(
    ticker: Any,
    timeframe: str,
    start: DateLike,
    end: DateLike,
    api_key: str,
    *,
    adjusted: bool = True,
    limit: int = 50_000,
) -> str:
    query = parse_ticker(ticker)
    multiplier, timespan, _ = _source_timeframe(timeframe)
    start_text = _date_text(start)
    end_text = _date_text(end)
    if start_text > end_text:
        raise ValueError("Aggregate start date must not be after end date.")
    bounded_limit = _bounded_limit(limit, maximum=50_000)
    path = (
        f"/v2/aggs/ticker/{_path_segment(query.ticker)}/range/"
        f"{multiplier}/{timespan}/{start_text}/{end_text}"
    )
    return build_polygon_url(
        path,
        {
            "adjusted": "true" if adjusted else "false",
            "sort": "asc",
            "limit": bounded_limit,
        },
        api_key=api_key,
    )


def build_market_status_url(api_key: str) -> str:
    return build_polygon_url("/v1/marketstatus/now", api_key=api_key)


def build_ticker_news_url(
    ticker: Any,
    api_key: str,
    *,
    limit: int = 10,
) -> str:
    clean_ticker = parse_ticker(ticker).ticker
    return build_polygon_url(
        "/v2/reference/news",
        {
            "ticker": clean_ticker,
            "order": "desc",
            "sort": "published_utc",
            "limit": _bounded_limit(limit, maximum=1000),
        },
        api_key=api_key,
    )


def build_options_snapshot_url(
    underlying_ticker: Any,
    api_key: str,
    *,
    limit: int = 250,
) -> str:
    clean_ticker = parse_ticker(underlying_ticker).ticker
    return build_polygon_url(
        f"/v3/snapshot/options/{_path_segment(clean_ticker)}",
        {"limit": _bounded_limit(limit, maximum=250), "sort": "ticker", "order": "asc"},
        api_key=api_key,
    )


def _date_text(value: DateLike) -> str:
    parsed = _parse_date(value)
    if parsed is None:
        raise ValueError("Date must use YYYY-MM-DD format.")
    return parsed.isoformat()


def _source_timeframe(value: Any) -> tuple[int, str, str]:
    normalized = str(value or "").strip().lower().replace(" ", "")
    try:
        return _SOURCE_TIMEFRAMES[normalized]
    except KeyError:
        raise ValueError("Polygon source timeframe must be daily, 5m, or 15m.") from None


def _target_timeframe(value: Any) -> str:
    normalized = str(value or "").strip().lower().replace(" ", "")
    try:
        return _TARGET_TIMEFRAMES[normalized]
    except KeyError:
        raise ValueError("Resample timeframe must be weekly, monthly, 1h, or 4h.") from None


def _bounded_limit(value: Any, *, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        raise ValueError("Limit must be an integer.") from None
    if parsed < 1 or parsed > maximum:
        raise ValueError(f"Limit must be between 1 and {maximum}.")
    return parsed


def _response_bytes(response: Any) -> bytes:
    body = response.read()
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    raise TypeError("Provider response body is not text or bytes.")


def _open_and_read(url: str, opener: Optional[UrlOpener]) -> bytes:
    response = opener(url) if opener is not None else urlopen(url, timeout=20)
    if hasattr(response, "__enter__"):
        with response as managed:
            return _response_bytes(managed)
    try:
        return _response_bytes(response)
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def _request_json(
    url: str,
    *,
    api_key: str,
    opener: Optional[UrlOpener],
    operation: str,
) -> JsonDict:
    try:
        raw = _open_and_read(url, opener)
    except HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        label = _HTTP_LABELS.get(code, "provider error")
        raise PolygonProviderError(
            f"Polygon {operation} failed with HTTP {code} ({label}).",
            operation=operation,
            status_code=code or None,
        ) from None
    except Exception as exc:
        error_name = exc.__class__.__name__
        raise PolygonProviderError(
            f"Polygon {operation} request failed ({error_name}).",
            operation=operation,
        ) from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise PolygonProviderError(
            f"Polygon {operation} returned an invalid JSON response.",
            operation=operation,
        ) from None
    if not isinstance(payload, dict):
        raise PolygonProviderError(
            f"Polygon {operation} returned an unexpected response shape.",
            operation=operation,
        )

    status = str(payload.get("status", "") or "").strip().upper()
    if status in {"ERROR", "NOT_AUTHORIZED", "AUTH_ERROR"}:
        raw_detail = payload.get("error") or payload.get("message") or status
        detail = redact_sensitive_text(raw_detail, api_key=api_key)
        suffix = f": {detail}" if detail else "."
        raise PolygonProviderError(
            f"Polygon {operation} returned an error{suffix}",
            operation=operation,
        )
    return payload


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _safe_public_url(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = urlsplit(text)
    except Exception:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    # If a provider-supplied display URL contains any credential, discard the
    # complete query.  This also prevents an unencoded credential containing
    # ``&`` from leaving credential fragments behind as apparent parameters.
    query = "" if any(_is_sensitive_name(key) for key, _ in query_items) else urlencode(query_items)
    fragment = "" if _is_sensitive_name(parsed.fragment) else parsed.fragment
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, fragment))


def _resolved_ticker_from_mapping(raw_value: Any) -> Optional[ResolvedTicker]:
    raw = _mapping(raw_value)
    try:
        ticker = sanitize_ticker(raw.get("ticker"))
    except InvalidTickerError:
        return None
    branding = _mapping(raw.get("branding"))
    return ResolvedTicker(
        ticker=ticker,
        name=str(raw.get("name") or ticker).strip(),
        primary_exchange=_clean_optional_text(raw.get("primary_exchange")),
        market=_clean_optional_text(raw.get("market")),
        locale=_clean_optional_text(raw.get("locale")),
        security_type=_clean_optional_text(raw.get("type")),
        active=_safe_bool(raw.get("active")),
        currency_name=_clean_optional_text(raw.get("currency_name")),
        composite_figi=_clean_optional_text(raw.get("composite_figi")),
        share_class_figi=_clean_optional_text(raw.get("share_class_figi")),
        last_updated=_parse_datetime(raw.get("last_updated_utc")),
        sic_code=_clean_optional_text(raw.get("sic_code")),
        sic_description=_clean_optional_text(raw.get("sic_description")),
        homepage_url=_safe_public_url(raw.get("homepage_url")),
        market_cap=_nonnegative_float(raw.get("market_cap")),
        weighted_shares_outstanding=_safe_int(raw.get("weighted_shares_outstanding")),
        branding_icon_url=_safe_public_url(branding.get("icon_url")),
        branding_logo_url=_safe_public_url(branding.get("logo_url")),
    )


def parse_ticker_reference_response(payload: Mapping[str, Any]) -> list[ResolvedTicker]:
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    resolved: list[ResolvedTicker] = []
    for raw_value in results:
        item = _resolved_ticker_from_mapping(raw_value)
        if item is not None:
            resolved.append(item)
    return resolved


def parse_ticker_details_response(payload: Mapping[str, Any]) -> Optional[ResolvedTicker]:
    """Parse one `/v3/reference/tickers/{ticker}` response."""

    return _resolved_ticker_from_mapping(payload.get("results"))


def _canonical_result_exchange(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    try:
        return normalize_exchange(value)
    except InvalidTickerError:
        return value.strip().upper() or None


def _select_reference_candidate(
    query: TickerQuery,
    candidates: Iterable[ResolvedTicker],
) -> ResolvedTicker:
    exact = [
        item
        for item in candidates
        if item.ticker == query.ticker
        and item.active is not False
        and (item.market is None or item.market.lower() == "stocks")
        and (item.locale is None or item.locale.lower() == "us")
    ]
    if query.exchange:
        exact = [
            item
            for item in exact
            if _canonical_result_exchange(item.primary_exchange) == query.exchange
        ]
    if not exact:
        qualifier = f" on {query.exchange}" if query.exchange else ""
        raise SymbolNotFoundError(
            f"No active Polygon stock matched {query.ticker}{qualifier}.",
            operation="symbol resolution",
        )
    unique = {
        (
            item.ticker,
            _canonical_result_exchange(item.primary_exchange),
            item.composite_figi or item.share_class_figi or item.name,
        )
        for item in exact
    }
    if len(unique) > 1:
        raise AmbiguousSymbolError(
            f"Polygon returned multiple matches for {query.ticker}; specify an exchange.",
            operation="symbol resolution",
        )
    return exact[0]


def resolve_symbol(
    ticker: Any,
    api_key: str,
    *,
    exchange: Any = None,
    opener: Optional[UrlOpener] = None,
) -> ResolvedTicker:
    query = parse_ticker(ticker, exchange)

    if query.exchange is None:
        url = build_ticker_details_url(query.ticker, api_key)
        try:
            payload = _request_json(
                url,
                api_key=api_key,
                opener=opener,
                operation="symbol resolution",
            )
        except PolygonProviderError as exc:
            if exc.status_code == 404:
                raise SymbolNotFoundError(
                    f"No active Polygon stock matched {query.ticker}.",
                    operation="symbol resolution",
                    status_code=404,
                ) from None
            raise
        if isinstance(payload.get("results"), list):
            # Backward-compatible tolerance for injected fixtures and cached
            # list responses; the live details endpoint returns one mapping.
            return _select_reference_candidate(
                query,
                parse_ticker_reference_response(payload),
            )
        exact = parse_ticker_details_response(payload)
        if (
            exact is None
            or exact.ticker != query.ticker
            or exact.active is False
            or (exact.market is not None and exact.market.lower() != "stocks")
            or (exact.locale is not None and exact.locale.lower() != "us")
        ):
            raise SymbolNotFoundError(
                f"No active Polygon stock matched {query.ticker}.",
                operation="symbol resolution",
            )
        return exact

    url = build_ticker_reference_url(query.ticker, api_key)
    payload = _request_json(url, api_key=api_key, opener=opener, operation="symbol resolution")
    return _select_reference_candidate(query, parse_ticker_reference_response(payload))


def parse_aggregate_bars(payload: Mapping[str, Any], timeframe: str) -> list[OHLCVBar]:
    _, _, normalized_timeframe = _source_timeframe(timeframe)
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    bars: list[OHLCVBar] = []
    for raw_value in results:
        raw = _mapping(raw_value)
        timestamp = _parse_datetime(raw.get("t"))
        open_value = _finite_float(raw.get("o"))
        high_value = _finite_float(raw.get("h"))
        low_value = _finite_float(raw.get("l"))
        close_value = _finite_float(raw.get("c"))
        volume = _nonnegative_float(raw.get("v"))
        if None in {timestamp, open_value, high_value, low_value, close_value, volume}:
            continue
        assert timestamp is not None
        assert open_value is not None
        assert high_value is not None
        assert low_value is not None
        assert close_value is not None
        assert volume is not None
        if high_value < max(open_value, low_value, close_value) or low_value > min(
            open_value, high_value, close_value
        ):
            continue
        bars.append(
            OHLCVBar(
                timestamp=timestamp,
                open=open_value,
                high=high_value,
                low=low_value,
                close=close_value,
                volume=volume,
                timeframe=normalized_timeframe,
                vwap=_finite_float(raw.get("vw")),
                transactions=_safe_int(raw.get("n")),
            )
        )
    return sorted(bars, key=lambda bar: bar.timestamp)


def fetch_aggregate_bars(
    ticker: Any,
    timeframe: str,
    start: DateLike,
    end: DateLike,
    api_key: str,
    *,
    opener: Optional[UrlOpener] = None,
    adjusted: bool = True,
    limit: int = 50_000,
) -> list[OHLCVBar]:
    url = build_aggregates_url(
        ticker,
        timeframe,
        start,
        end,
        api_key,
        adjusted=adjusted,
        limit=limit,
    )
    payload = _request_json(url, api_key=api_key, opener=opener, operation="aggregate bars")
    if payload.get("next_url"):
        raise PolygonProviderError(
            "Polygon aggregate bars exceeded the safe response limit; use a narrower date range.",
            operation="aggregate bars",
        )
    return parse_aggregate_bars(payload, timeframe)


fetch_aggregates = fetch_aggregate_bars


def fetch_daily_bars(
    ticker: Any,
    start: DateLike,
    end: DateLike,
    api_key: str,
    *,
    opener: Optional[UrlOpener] = None,
) -> list[OHLCVBar]:
    return fetch_aggregate_bars(ticker, "1d", start, end, api_key, opener=opener)


def fetch_15m_bars(
    ticker: Any,
    start: DateLike,
    end: DateLike,
    api_key: str,
    *,
    opener: Optional[UrlOpener] = None,
) -> list[OHLCVBar]:
    return fetch_aggregate_bars(ticker, "15m", start, end, api_key, opener=opener)


def fetch_5m_bars(
    ticker: Any,
    start: DateLike,
    end: DateLike,
    api_key: str,
    *,
    opener: Optional[UrlOpener] = None,
) -> list[OHLCVBar]:
    return fetch_aggregate_bars(ticker, "5m", start, end, api_key, opener=opener)


def _coerce_bar(value: Union[OHLCVBar, Mapping[str, Any]], timeframe: str = "") -> Optional[OHLCVBar]:
    if isinstance(value, OHLCVBar):
        return value
    raw = _mapping(value)
    timestamp = _parse_datetime(raw.get("timestamp", raw.get("t")))
    open_value = _finite_float(raw.get("open", raw.get("o")))
    high_value = _finite_float(raw.get("high", raw.get("h")))
    low_value = _finite_float(raw.get("low", raw.get("l")))
    close_value = _finite_float(raw.get("close", raw.get("c")))
    volume = _nonnegative_float(raw.get("volume", raw.get("v")))
    if None in {timestamp, open_value, high_value, low_value, close_value, volume}:
        return None
    assert timestamp is not None
    assert open_value is not None
    assert high_value is not None
    assert low_value is not None
    assert close_value is not None
    assert volume is not None
    if high_value < max(open_value, low_value, close_value) or low_value > min(
        open_value, high_value, close_value
    ):
        return None
    return OHLCVBar(
        timestamp=timestamp,
        open=open_value,
        high=high_value,
        low=low_value,
        close=close_value,
        volume=volume,
        timeframe=str(raw.get("timeframe") or timeframe),
        vwap=_finite_float(raw.get("vwap", raw.get("vw"))),
        transactions=_safe_int(raw.get("transactions", raw.get("n"))),
        source_count=_safe_int(raw.get("source_count")) or 1,
    )


def _bucket_start(timestamp: datetime, target: str, market_timezone: ZoneInfo) -> datetime:
    local = timestamp.astimezone(market_timezone)
    if target == "1w":
        local_date = local.date() - timedelta(days=local.weekday())
        bucket = datetime.combine(local_date, time.min, tzinfo=market_timezone)
    elif target == "1mo":
        bucket = datetime(local.year, local.month, 1, tzinfo=market_timezone)
    else:
        hours = 1 if target == "1h" else 4
        session_anchor = datetime.combine(local.date(), time(9, 30), tzinfo=market_timezone)
        elapsed_seconds = (local - session_anchor).total_seconds()
        bucket_number = math.floor(elapsed_seconds / (hours * 3600))
        bucket = session_anchor + timedelta(hours=bucket_number * hours)
    return bucket.astimezone(timezone.utc)


def resample_bars(
    bars: Iterable[Union[OHLCVBar, Mapping[str, Any]]],
    timeframe: str,
    *,
    market_timezone: str = "America/New_York",
) -> list[OHLCVBar]:
    """Resample OHLCV data using exchange-local calendar/session buckets.

    Weekly buckets start Monday, monthly buckets start on day one, and 1h/4h
    intraday buckets are anchored to the 09:30 New York regular-session open.
    Empty periods are not fabricated.
    """

    target = _target_timeframe(timeframe)
    try:
        exchange_tz = ZoneInfo(market_timezone)
    except Exception:
        raise ValueError("Unknown market timezone.") from None

    normalized = [bar for value in bars if (bar := _coerce_bar(value)) is not None]
    normalized.sort(key=lambda bar: bar.timestamp)
    grouped: dict[datetime, list[OHLCVBar]] = {}
    for bar in normalized:
        grouped.setdefault(_bucket_start(bar.timestamp, target, exchange_tz), []).append(bar)

    output: list[OHLCVBar] = []
    for bucket, rows in sorted(grouped.items()):
        total_volume = sum(row.volume for row in rows)
        weighted_vwap_numerator = sum(
            row.vwap * row.volume for row in rows if row.vwap is not None and row.volume > 0
        )
        weighted_vwap_denominator = sum(
            row.volume for row in rows if row.vwap is not None and row.volume > 0
        )
        transaction_values = [row.transactions for row in rows if row.transactions is not None]
        output.append(
            OHLCVBar(
                timestamp=bucket,
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                volume=total_volume,
                timeframe=target,
                vwap=(
                    weighted_vwap_numerator / weighted_vwap_denominator
                    if weighted_vwap_denominator > 0
                    else None
                ),
                transactions=sum(transaction_values) if transaction_values else None,
                source_count=sum(row.source_count for row in rows),
            )
        )
    return output


def resample_weekly(bars: Iterable[Union[OHLCVBar, Mapping[str, Any]]]) -> list[OHLCVBar]:
    return resample_bars(bars, "1w")


def resample_monthly(bars: Iterable[Union[OHLCVBar, Mapping[str, Any]]]) -> list[OHLCVBar]:
    return resample_bars(bars, "1mo")


def resample_1h(bars: Iterable[Union[OHLCVBar, Mapping[str, Any]]]) -> list[OHLCVBar]:
    return resample_bars(bars, "1h")


def resample_4h(bars: Iterable[Union[OHLCVBar, Mapping[str, Any]]]) -> list[OHLCVBar]:
    return resample_bars(bars, "4h")


def _market_timestamp(value: Any) -> Optional[datetime]:
    parsed = _parse_datetime(value)
    if parsed is None or parsed.year < 1990:
        return None
    return parsed


def _snapshot_price(
    last_trade: Mapping[str, Any],
    minute: Mapping[str, Any],
    day: Mapping[str, Any],
    previous_day: Mapping[str, Any],
) -> tuple[Optional[float], Optional[str]]:
    candidates = [
        ("last_trade", last_trade.get("p")),
        ("minute_close", minute.get("c")),
        ("day_close", day.get("c")),
        ("previous_close", previous_day.get("c")),
    ]
    for source, raw_value in candidates:
        price = _positive_float(raw_value)
        if price is not None:
            return price, source
    return None, None


def _snapshot_updated_at(
    raw: Mapping[str, Any],
    last_trade: Mapping[str, Any],
    minute: Mapping[str, Any],
    day: Mapping[str, Any],
) -> tuple[Optional[datetime], Optional[str]]:
    candidates = [
        ("snapshot", raw.get("updated")),
        ("last_trade", last_trade.get("t")),
        ("minute", minute.get("t")),
        ("day", day.get("t")),
    ]
    for source, raw_value in candidates:
        timestamp = _market_timestamp(raw_value)
        if timestamp is not None:
            return timestamp, source
    return None, None


def parse_stock_snapshots(
    payload: Mapping[str, Any],
    requested_tickers: Optional[Union[str, Iterable[Any]]] = None,
) -> list[StockSnapshot]:
    """Normalize Polygon's batched US-stock snapshot response.

    The selected price source is explicit so a minute/day/previous-close
    fallback can never be mistaken for a last trade.
    """

    raw_results = payload.get("tickers")
    if not isinstance(raw_results, list):
        return []
    requested = (
        _normalize_watchlist_tickers(requested_tickers)
        if requested_tickers is not None
        else None
    )
    requested_set = set(requested or [])
    snapshots: dict[str, StockSnapshot] = {}
    provider_order: list[str] = []
    for raw_value in raw_results:
        raw = _mapping(raw_value)
        try:
            ticker = sanitize_ticker(raw.get("ticker"))
        except InvalidTickerError:
            continue
        if requested is not None and ticker not in requested_set:
            continue
        last_trade = _mapping(raw.get("lastTrade", raw.get("last_trade")))
        minute = _mapping(raw.get("min", raw.get("minute")))
        day = _mapping(raw.get("day"))
        previous_day = _mapping(raw.get("prevDay", raw.get("previous_day")))
        price, price_source = _snapshot_price(last_trade, minute, day, previous_day)
        updated_at, updated_source = _snapshot_updated_at(raw, last_trade, minute, day)
        snapshot = StockSnapshot(
            ticker=ticker,
            price=price,
            price_source=price_source,
            change=_finite_float(raw.get("todaysChange", raw.get("todays_change"))),
            change_percent=_finite_float(
                raw.get("todaysChangePerc", raw.get("todays_change_percent"))
            ),
            updated_at=updated_at,
            updated_source=updated_source,
            previous_close=_positive_float(previous_day.get("c")),
            day_open=_positive_float(day.get("o")),
            day_high=_positive_float(day.get("h")),
            day_low=_positive_float(day.get("l")),
            day_close=_positive_float(day.get("c")),
            day_volume=_nonnegative_float(day.get("v")),
        )
        existing = snapshots.get(ticker)
        if existing is None:
            snapshots[ticker] = snapshot
            provider_order.append(ticker)
        elif snapshot.updated_at is not None and (
            existing.updated_at is None or snapshot.updated_at > existing.updated_at
        ):
            snapshots[ticker] = snapshot

    order = requested if requested is not None else provider_order
    return [snapshots[ticker] for ticker in order if ticker in snapshots]


def fetch_stock_snapshots(
    tickers: Union[str, Iterable[Any]],
    api_key: str,
    *,
    opener: Optional[UrlOpener] = None,
    batch_size: int = 250,
    include_otc: bool = False,
) -> list[StockSnapshot]:
    """Fetch watchlist snapshots in bounded batches, preserving input order."""

    normalized = _normalize_watchlist_tickers(tickers)
    bounded_batch_size = _bounded_limit(batch_size, maximum=250)
    snapshots: dict[str, StockSnapshot] = {}
    for start in range(0, len(normalized), bounded_batch_size):
        batch = normalized[start : start + bounded_batch_size]
        url = build_stock_snapshots_url(
            batch,
            api_key,
            include_otc=include_otc,
        )
        payload = _request_json(
            url,
            api_key=api_key,
            opener=opener,
            operation="stock snapshots",
        )
        if not isinstance(payload.get("tickers"), list):
            raise PolygonProviderError(
                "Polygon stock snapshots returned an unexpected response shape.",
                operation="stock snapshots",
            )
        for snapshot in parse_stock_snapshots(payload, batch):
            snapshots[snapshot.ticker] = snapshot
    return [snapshots[ticker] for ticker in normalized if ticker in snapshots]


fetch_watchlist_snapshots = fetch_stock_snapshots


def _string_mapping(value: Any) -> dict[str, str]:
    raw = _mapping(value)
    return {str(key): str(item) for key, item in raw.items() if item is not None}


def parse_market_status(payload: Mapping[str, Any]) -> MarketStatus:
    return MarketStatus(
        market=str(payload.get("market") or "unknown").strip().lower(),
        server_time=_parse_datetime(payload.get("serverTime", payload.get("server_time"))),
        early_hours=_safe_bool(payload.get("earlyHours", payload.get("early_hours"))),
        after_hours=_safe_bool(payload.get("afterHours", payload.get("after_hours"))),
        exchanges=_string_mapping(payload.get("exchanges")),
        currencies=_string_mapping(payload.get("currencies")),
    )


def fetch_market_status(
    api_key: str,
    *,
    opener: Optional[UrlOpener] = None,
) -> MarketStatus:
    url = build_market_status_url(api_key)
    payload = _request_json(url, api_key=api_key, opener=opener, operation="market status")
    return parse_market_status(payload)


get_market_status = fetch_market_status


def _text_tuple(value: Any, *, ticker_values: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    output: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if not text:
            continue
        if ticker_values:
            try:
                text = sanitize_ticker(text)
            except InvalidTickerError:
                continue
        output.append(text)
    return tuple(output)


def parse_ticker_news(payload: Mapping[str, Any]) -> list[NewsArticle]:
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    articles: list[NewsArticle] = []
    for raw_value in results:
        raw = _mapping(raw_value)
        title = str(raw.get("title") or "").strip()
        article_id = str(raw.get("id") or "").strip()
        if not title or not article_id:
            continue
        publisher = _mapping(raw.get("publisher"))
        articles.append(
            NewsArticle(
                article_id=article_id,
                title=title,
                published_at=_parse_datetime(raw.get("published_utc")),
                article_url=_clean_optional_text(raw.get("article_url")),
                author=_clean_optional_text(raw.get("author")),
                description=_clean_optional_text(raw.get("description")),
                tickers=_text_tuple(raw.get("tickers"), ticker_values=True),
                keywords=_text_tuple(raw.get("keywords")),
                publisher_name=_clean_optional_text(publisher.get("name")),
                publisher_homepage_url=_clean_optional_text(publisher.get("homepage_url")),
            )
        )
    return sorted(
        articles,
        key=lambda article: article.published_at or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )


def fetch_ticker_news(
    ticker: Any,
    api_key: str,
    *,
    opener: Optional[UrlOpener] = None,
    limit: int = 10,
) -> list[NewsArticle]:
    url = build_ticker_news_url(ticker, api_key, limit=limit)
    payload = _request_json(url, api_key=api_key, opener=opener, operation="ticker news")
    return parse_ticker_news(payload)


get_ticker_news = fetch_ticker_news


def _quote_metrics(
    bid: Optional[float], ask: Optional[float], provider_midpoint: Optional[float]
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    if bid is not None and ask is not None and ask >= bid:
        midpoint = (bid + ask) / 2
        spread = ask - bid
        spread_percent = (spread / midpoint * 100) if midpoint > 0 else None
        return midpoint, spread, spread_percent
    return provider_midpoint, None, None


def parse_options_chain_snapshot(
    payload: Mapping[str, Any],
    underlying_ticker: Any = None,
) -> list[OptionContractSnapshot]:
    fallback_underlying = ""
    if underlying_ticker not in (None, ""):
        fallback_underlying = parse_ticker(underlying_ticker).ticker
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    contracts: list[OptionContractSnapshot] = []
    for raw_value in results:
        raw = _mapping(raw_value)
        details = _mapping(raw.get("details"))
        quote_data = _mapping(raw.get("last_quote"))
        trade_data = _mapping(raw.get("last_trade"))
        day_data = _mapping(raw.get("day"))
        greeks = _mapping(raw.get("greeks"))
        underlying_data = _mapping(raw.get("underlying_asset"))

        option_ticker_raw = details.get("ticker") or raw.get("ticker")
        try:
            option_ticker = sanitize_ticker(option_ticker_raw)
        except InvalidTickerError:
            # Polygon option symbols begin with O: and intentionally do not use
            # the equity ticker grammar.  Preserve only a tightly constrained
            # provider symbol rather than deleting characters.
            option_ticker = str(option_ticker_raw or "").strip().upper()
            if not re.fullmatch(r"O:[A-Z0-9.\-]{1,40}", option_ticker):
                continue

        underlying_raw = underlying_data.get("ticker") or fallback_underlying
        try:
            clean_underlying = sanitize_ticker(underlying_raw)
        except InvalidTickerError:
            continue
        if fallback_underlying and clean_underlying != fallback_underlying:
            # Never allow a mixed/corrupt provider page to cross an option
            # contract into analysis for another underlying security.
            continue

        bid = _nonnegative_float(quote_data.get("bid"))
        ask = _nonnegative_float(quote_data.get("ask"))
        provider_midpoint = _nonnegative_float(quote_data.get("midpoint"))
        midpoint, spread, spread_percent = _quote_metrics(bid, ask, provider_midpoint)
        timeframe_value = quote_data.get("timeframe") or underlying_data.get("timeframe")
        contracts.append(
            OptionContractSnapshot(
                underlying_ticker=clean_underlying,
                option_ticker=option_ticker,
                contract_type=_clean_optional_text(details.get("contract_type")),
                expiration_date=_parse_date(details.get("expiration_date")),
                strike_price=_nonnegative_float(details.get("strike_price")),
                exercise_style=_clean_optional_text(details.get("exercise_style")),
                shares_per_contract=_safe_int(details.get("shares_per_contract")),
                bid=bid,
                ask=ask,
                midpoint=midpoint,
                spread=spread,
                spread_percent=spread_percent,
                delta=_finite_float(greeks.get("delta")),
                gamma=_finite_float(greeks.get("gamma")),
                theta=_finite_float(greeks.get("theta")),
                vega=_finite_float(greeks.get("vega")),
                open_interest=_safe_int(raw.get("open_interest")),
                volume=_nonnegative_float(day_data.get("volume")),
                implied_volatility=_nonnegative_float(raw.get("implied_volatility")),
                break_even_price=_nonnegative_float(
                    raw.get("break_even_price", raw.get("break_even"))
                ),
                quote_timestamp=_parse_datetime(quote_data.get("last_updated")),
                trade_timestamp=_parse_datetime(
                    trade_data.get("sip_timestamp", trade_data.get("last_updated"))
                ),
                day_timestamp=_parse_datetime(day_data.get("last_updated")),
                underlying_timestamp=_parse_datetime(underlying_data.get("last_updated")),
                data_timeframe=_clean_optional_text(timeframe_value),
            )
        )
    return contracts


parse_options_snapshot = parse_options_chain_snapshot


def _authorized_next_url(next_url: Any, api_key: str) -> Optional[str]:
    text = str(next_url or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if parsed.scheme != "https" or parsed.hostname != POLYGON_HOST:
        raise PolygonProviderError("Polygon options pagination returned an unsafe next URL.")
    items = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if not _is_sensitive_name(key)
    ]
    items.append(("apiKey", str(api_key).strip()))
    return urlunsplit(("https", POLYGON_HOST, parsed.path, urlencode(items), ""))


def fetch_options_chain_snapshot(
    underlying_ticker: Any,
    api_key: str,
    *,
    opener: Optional[UrlOpener] = None,
    limit: int = 250,
    max_pages: int = 10,
) -> list[OptionContractSnapshot]:
    clean_underlying = parse_ticker(underlying_ticker).ticker
    page_limit = _bounded_limit(max_pages, maximum=100)
    url: Optional[str] = build_options_snapshot_url(clean_underlying, api_key, limit=limit)
    contracts: list[OptionContractSnapshot] = []
    seen_urls: set[str] = set()
    for _ in range(page_limit):
        if not url:
            return contracts
        if url in seen_urls:
            raise PolygonProviderError(
                "Polygon options pagination repeated a page; the chain may be incomplete.",
                operation="options chain snapshot",
            )
        seen_urls.add(url)
        payload = _request_json(
            url,
            api_key=api_key,
            opener=opener,
            operation="options chain snapshot",
        )
        contracts.extend(parse_options_chain_snapshot(payload, clean_underlying))
        url = _authorized_next_url(payload.get("next_url"), api_key)
    if url:
        raise PolygonProviderError(
            "Polygon options pagination reached the configured page limit; the chain may be incomplete.",
            operation="options chain snapshot",
        )
    return contracts


fetch_options_snapshot = fetch_options_chain_snapshot


class PolygonProvider:
    """Small read-only client with an injectable transport."""

    __slots__ = ("__api_key", "_opener")

    def __init__(self, api_key: str, *, opener: Optional[UrlOpener] = None) -> None:
        clean_key = str(api_key or "").strip()
        if not clean_key:
            raise PolygonProviderError("Polygon API key is required.")
        self.__api_key = clean_key
        self._opener = opener

    def __repr__(self) -> str:
        return "PolygonProvider(api_key=[redacted])"

    def resolve_symbol(self, ticker: Any, exchange: Any = None) -> ResolvedTicker:
        return resolve_symbol(
            ticker,
            self.__api_key,
            exchange=exchange,
            opener=self._opener,
        )

    def daily_bars(self, ticker: Any, start: DateLike, end: DateLike) -> list[OHLCVBar]:
        return fetch_daily_bars(
            ticker,
            start,
            end,
            self.__api_key,
            opener=self._opener,
        )

    fetch_daily_bars = daily_bars

    def bars_5m(self, ticker: Any, start: DateLike, end: DateLike) -> list[OHLCVBar]:
        return fetch_5m_bars(
            ticker,
            start,
            end,
            self.__api_key,
            opener=self._opener,
        )

    fetch_5m_bars = bars_5m

    def bars_15m(self, ticker: Any, start: DateLike, end: DateLike) -> list[OHLCVBar]:
        return fetch_15m_bars(
            ticker,
            start,
            end,
            self.__api_key,
            opener=self._opener,
        )

    fetch_15m_bars = bars_15m

    def stock_snapshots(
        self,
        tickers: Union[str, Iterable[Any]],
        *,
        batch_size: int = 250,
        include_otc: bool = False,
    ) -> list[StockSnapshot]:
        return fetch_stock_snapshots(
            tickers,
            self.__api_key,
            opener=self._opener,
            batch_size=batch_size,
            include_otc=include_otc,
        )

    fetch_stock_snapshots = stock_snapshots
    watchlist_snapshots = stock_snapshots
    batch_stock_snapshots = stock_snapshots

    def market_status(self) -> MarketStatus:
        return fetch_market_status(self.__api_key, opener=self._opener)

    fetch_market_status = market_status

    def news(self, ticker: Any, *, limit: int = 10) -> list[NewsArticle]:
        return fetch_ticker_news(
            ticker,
            self.__api_key,
            opener=self._opener,
            limit=limit,
        )

    fetch_ticker_news = news

    def options_chain(
        self,
        underlying_ticker: Any,
        *,
        limit: int = 250,
        max_pages: int = 10,
    ) -> list[OptionContractSnapshot]:
        return fetch_options_chain_snapshot(
            underlying_ticker,
            self.__api_key,
            opener=self._opener,
            limit=limit,
            max_pages=max_pages,
        )

    fetch_options_chain_snapshot = options_chain


# Compatibility-oriented descriptive aliases.  They keep integrations readable
# without duplicating any transport or parsing behavior.
Bar = OHLCVBar
OptionSnapshot = OptionContractSnapshot
StockTickerSnapshot = StockSnapshot
TickerResolution = ResolvedTicker
build_polygon_aggs_url = build_aggregates_url
fetch_bars = fetch_aggregate_bars
fetch_news = fetch_ticker_news
fetch_options_chain = fetch_options_chain_snapshot
fetch_batched_stock_snapshots = fetch_stock_snapshots
get_stock_snapshots = fetch_stock_snapshots
parse_ticker_and_exchange = parse_ticker
resample_ohlcv_bars = resample_bars
resolve_ticker = resolve_symbol


__all__ = [
    "AmbiguousSymbolError",
    "Bar",
    "MarketStatus",
    "NewsArticle",
    "OHLCVBar",
    "OptionContractSnapshot",
    "OptionSnapshot",
    "PolygonProvider",
    "PolygonProviderError",
    "ResolvedTicker",
    "StockSnapshot",
    "StockTickerSnapshot",
    "SymbolNotFoundError",
    "TickerQuery",
    "TickerResolution",
    "InvalidTickerError",
    "build_aggregates_url",
    "build_market_status_url",
    "build_options_snapshot_url",
    "build_polygon_aggs_url",
    "build_polygon_url",
    "build_stock_snapshots_url",
    "build_ticker_news_url",
    "build_ticker_details_url",
    "build_ticker_reference_url",
    "fetch_15m_bars",
    "fetch_5m_bars",
    "fetch_aggregate_bars",
    "fetch_aggregates",
    "fetch_bars",
    "fetch_batched_stock_snapshots",
    "fetch_daily_bars",
    "fetch_market_status",
    "fetch_news",
    "fetch_options_chain",
    "fetch_options_chain_snapshot",
    "fetch_options_snapshot",
    "fetch_stock_snapshots",
    "fetch_ticker_news",
    "fetch_watchlist_snapshots",
    "get_market_status",
    "get_stock_snapshots",
    "get_ticker_news",
    "normalize_exchange",
    "parse_aggregate_bars",
    "parse_market_status",
    "parse_options_chain_snapshot",
    "parse_options_snapshot",
    "parse_stock_snapshots",
    "parse_ticker",
    "parse_ticker_and_exchange",
    "parse_ticker_input",
    "parse_ticker_details_response",
    "parse_ticker_news",
    "parse_ticker_reference_response",
    "redact_api_key",
    "redact_sensitive_text",
    "redact_url",
    "resample_1h",
    "resample_4h",
    "resample_bars",
    "resample_monthly",
    "resample_ohlcv_bars",
    "resample_weekly",
    "resolve_symbol",
    "resolve_ticker",
    "sanitize_ticker",
]
