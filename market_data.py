"""Read-only market data helpers for Trading Autopilot.

This module normalizes market-data bars and prepares candidate import rows.
It does not trade, manage accounts, create alerts, or persist imported data.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Optional, Union
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import urlopen


POLYGON_AGGS_BASE_URL = "https://api.polygon.io/v2/aggs/ticker"
CONFIG_KEYS = [
    "MARKET_DATA_PROVIDER",
    "ALPACA_API_KEY_ID",
    "ALPACA_API_SECRET_KEY",
    "POLYGON_API_KEY",
]
SUPPORTED_PROVIDERS = {"alpaca", "polygon"}
HTTP_STATUS_LABELS = {
    400: "bad request",
    401: "unauthorized",
    403: "forbidden",
    404: "not found",
    429: "rate limited",
}
PROVIDER_MESSAGE_MAX_CHARS = 220
PLACEHOLDER_PROVIDER_KEYS = {
    "your-polygon-key",
    "user-provider-key",
    "actual-polygon-key-here",
    "choose-your-provider-key",
    "user-real-polygon-key",
    "placeholder",
    "demo",
    "test",
}
TRADINGVIEW_IMPORT_HEADER = [
    "ticker",
    "price",
    "timeframe",
    "bias_note",
    "key_level_note",
    "ema9",
    "ema21",
    "wma50",
    "wma200",
    "sma200",
    "support1",
    "support2",
    "resistance1",
    "resistance2",
    "breakout",
    "breakdown",
    "invalid",
    "relative_volume",
    "macd_hist",
    "notes",
]


@dataclass(frozen=True)
class MarketDataBar:
    ticker: str
    timeframe: str
    timestamp: str
    open: float
    high: float
    low: float
    close: float
    volume: Union[float, str]


@dataclass(frozen=True)
class MarketDataProviderConfig:
    provider: str
    alpaca_api_key_id: str = ""
    alpaca_api_secret_key: str = ""
    polygon_api_key: str = ""


@dataclass(frozen=True)
class MarketDataResult:
    provider: str
    rows: list[dict[str, Any]]
    errors: list[str]


def _mapping_value(mapping: Optional[Mapping[str, Any]], key: str) -> str:
    if mapping is None:
        return ""
    try:
        value = mapping.get(key, "")
    except Exception:
        value = ""
    if value is None:
        return ""
    return str(value).strip()


def get_market_data_provider_config(
    secrets: Optional[Mapping[str, Any]] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    config: dict[str, str] = {}
    for key in CONFIG_KEYS:
        secret_value = _mapping_value(secrets, key)
        env_value = _mapping_value(environ, key)
        config[key] = secret_value or env_value
    return config


def configured_market_data_provider(config: dict[str, str]) -> str:
    provider = str(config.get("MARKET_DATA_PROVIDER", "") or "").strip().lower()
    if provider in {"", "none", "disabled", "off"}:
        return "disabled"
    return provider


def market_data_config_errors(config: dict[str, str]) -> list[str]:
    provider = configured_market_data_provider(config)
    if provider == "disabled":
        return []
    if provider not in SUPPORTED_PROVIDERS:
        return ["Unsupported MARKET_DATA_PROVIDER. Use alpaca, polygon, or leave blank."]
    required = {
        "alpaca": ["ALPACA_API_KEY_ID", "ALPACA_API_SECRET_KEY"],
        "polygon": ["POLYGON_API_KEY"],
    }[provider]
    errors = [f"Missing required setting: {key}" for key in required if not str(config.get(key, "") or "").strip()]
    if provider == "polygon" and is_placeholder_provider_key(str(config.get("POLYGON_API_KEY", "") or "")):
        errors.append("POLYGON_API_KEY appears to be a placeholder. Replace it in Streamlit secrets with a real provider key.")
    return errors


def polygon_timespan_for_timeframe(timeframe: str) -> tuple[int, str]:
    normalized = str(timeframe or "").strip().lower()
    mapping = {
        "15m": (15, "minute"),
        "1h": (1, "hour"),
        "4h": (4, "hour"),
        "1d": (1, "day"),
    }
    return mapping.get(normalized, (15, "minute"))


def polygon_date_range_for_timeframe(
    timeframe: str,
    now: Optional[datetime] = None,
) -> tuple[str, str]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    _, timespan = polygon_timespan_for_timeframe(timeframe)
    days = 365 if timespan == "day" else 10
    start = current - timedelta(days=days)
    return start.date().isoformat(), current.date().isoformat()


def sanitize_provider_message(message: Any) -> str:
    text = str(message or "").strip()
    if not text:
        return ""
    text = re.sub(
        r"(?i)\b(apiKey|api_key|POLYGON_API_KEY|token|password|secret)=([^&\s\"']+)",
        lambda match: f"{match.group(1)}=[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)(\"(?:apiKey|api_key|POLYGON_API_KEY|token|password|secret)\"\s*:\s*\")[^\"]+",
        lambda match: f"{match.group(1)}[redacted]",
        text,
    )
    text = re.sub(
        r"(?i)('(?:apiKey|api_key|POLYGON_API_KEY|token|password|secret)'\s*:\s*')[^']+",
        lambda match: f"{match.group(1)}[redacted]",
        text,
    )
    text = re.sub(r"(?i)\b(Bearer\s+)[A-Za-z0-9._\-]+", r"\1[redacted]", text)
    text = " ".join(text.split())
    if len(text) > PROVIDER_MESSAGE_MAX_CHARS:
        text = text[: PROVIDER_MESSAGE_MAX_CHARS - 3].rstrip() + "..."
    return text


def is_placeholder_provider_key(value: str) -> bool:
    key = str(value or "").strip().lower()
    if not key:
        return False
    if key in PLACEHOLDER_PROVIDER_KEYS:
        return True
    return any(fragment in key for fragment in ["placeholder", "your-", "choose-", "-here"])


def _read_http_error_body(error: Any) -> str:
    response = getattr(error, "response", None)
    if response is not None:
        try:
            payload = response.json()
        except Exception:
            payload = None
        if payload not in (None, ""):
            try:
                return json.dumps(payload)
            except Exception:
                return str(payload)
        text = getattr(response, "text", "")
        if text:
            return str(text)
        content = getattr(response, "content", b"")
        if content:
            if isinstance(content, str):
                return content
            try:
                return content.decode("utf-8", errors="replace")
            except Exception:
                return ""
    try:
        raw_body = error.read()
    except Exception:
        return ""
    if not raw_body:
        return ""
    if isinstance(raw_body, str):
        return raw_body
    try:
        return raw_body.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _http_error_body_detail(body: str) -> str:
    clean_body = sanitize_provider_message(body)
    if not clean_body:
        return ""
    try:
        payload = json.loads(clean_body)
    except json.JSONDecodeError:
        return clean_body
    if not isinstance(payload, dict):
        return clean_body
    details = []
    for key in ["status", "error", "message"]:
        value = payload.get(key)
        if value not in (None, ""):
            details.append(f"{key}: {sanitize_provider_message(value)}")
    return "; ".join(details)


def _http_like_status_code(error: Any) -> int:
    for attr in ["code", "status", "status_code"]:
        value = getattr(error, attr, None)
        try:
            code = int(value or 0)
        except (TypeError, ValueError):
            code = 0
        if code:
            return code
    response = getattr(error, "response", None)
    if response is not None:
        try:
            return int(getattr(response, "status_code", 0) or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _http_like_reason(error: Any) -> str:
    for attr in ["reason", "msg"]:
        reason = sanitize_provider_message(getattr(error, attr, "") or "")
        if reason:
            return reason
    response = getattr(error, "response", None)
    if response is not None:
        reason = sanitize_provider_message(getattr(response, "reason", "") or "")
        if reason:
            return reason
    return ""


def provider_http_error_message(ticker: str, error: Any) -> str:
    clean_ticker = str(ticker or "").strip().upper() or "UNKNOWN"
    code = _http_like_status_code(error)
    label = HTTP_STATUS_LABELS.get(code)
    if label is None:
        reason = _http_like_reason(error)
        label = reason.lower() if reason else "error"
    base = f"{clean_ticker}: provider HTTP {code} {label}".strip()
    detail = _http_error_body_detail(_read_http_error_body(error))
    if detail:
        return f"{base}: {detail}"
    return base


def provider_exception_message(ticker: str, error: Exception) -> str:
    if _http_like_status_code(error):
        return provider_http_error_message(ticker, error)
    clean_ticker = str(ticker or "").strip().upper() or "UNKNOWN"
    class_name = sanitize_provider_message(error.__class__.__name__)
    detail = sanitize_provider_message(str(error))
    if detail and detail != class_name:
        return f"{clean_ticker}: provider fetch failed ({class_name}): {detail}"
    return f"{clean_ticker}: provider fetch failed ({class_name})"


def build_polygon_aggs_url(
    ticker: str,
    timeframe: str,
    api_key: str,
    adjusted: bool = True,
    limit: int = 5000,
) -> str:
    multiplier, timespan = polygon_timespan_for_timeframe(timeframe)
    start_date, end_date = polygon_date_range_for_timeframe(timeframe)
    encoded_ticker = quote(str(ticker or "").strip().upper(), safe="")
    query = urlencode(
        {
            "adjusted": "true" if adjusted else "false",
            "sort": "asc",
            "limit": str(limit),
            "apiKey": api_key,
        }
    )
    return (
        f"{POLYGON_AGGS_BASE_URL}/{encoded_ticker}/range/"
        f"{multiplier}/{timespan}/{start_date}/{end_date}?{query}"
    )


def parse_polygon_aggs_response(
    payload: dict[str, Any],
    ticker: str,
    timeframe: str,
) -> list[dict[str, Any]]:
    status = str(payload.get("status", "") or "").strip().upper()
    if status in {"ERROR", "NOT_AUTHORIZED", "AUTH_ERROR"}:
        return []
    results = payload.get("results")
    if not isinstance(results, list):
        return []
    raw_bars = [row for row in results if isinstance(row, dict)]
    return normalize_market_data_bars(raw_bars, ticker=ticker, timeframe=timeframe)


def fetch_polygon_aggregate_bars(
    ticker: str,
    timeframe: str,
    api_key: str,
    opener: Any = None,
) -> list[dict[str, Any]]:
    if not str(api_key or "").strip():
        return []
    fetcher = opener or urlopen
    url = build_polygon_aggs_url(ticker=ticker, timeframe=timeframe, api_key=api_key)
    with fetcher(url) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        return []
    return parse_polygon_aggs_response(payload, ticker=ticker, timeframe=timeframe)


def fetch_readonly_market_data_rows(
    tickers: list[str],
    timeframe: str,
    config: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    provider = configured_market_data_provider(config)
    if provider == "disabled":
        return [], ["MARKET_DATA_PROVIDER is not configured."]
    if provider != "polygon":
        return [], ["Only polygon read-only fetch is implemented in v1.1.3."]
    config_errors = market_data_config_errors(config)
    if config_errors:
        return [], config_errors

    api_key = str(config.get("POLYGON_API_KEY", "") or "").strip()
    if is_placeholder_provider_key(api_key):
        return [], ["POLYGON_API_KEY appears to be a placeholder. Replace it in Streamlit secrets with a real provider key."]
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for ticker in tickers:
        clean_ticker = str(ticker or "").strip().upper()
        if not clean_ticker:
            continue
        try:
            bars = fetch_polygon_aggregate_bars(clean_ticker, timeframe, api_key)
        except HTTPError as exc:
            errors.append(provider_http_error_message(clean_ticker, exc))
            continue
        except Exception as exc:
            errors.append(provider_exception_message(clean_ticker, exc))
            continue
        if not bars:
            errors.append(f"{clean_ticker}: no provider bars returned")
            continue
        indicators = compute_market_data_indicators(bars)
        rows.append(
            {
                "ticker": clean_ticker,
                "timeframe": str(timeframe or "").strip() or "15m",
                **indicators,
            }
        )
    return rows, errors


def _first_value(row: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        if key in row and row[key] not in (None, ""):
            return row[key]
    return ""


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip().replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _format_number(value: Any) -> str:
    number = _to_float(value)
    if number is None:
        return ""
    if number.is_integer():
        return str(int(number))
    return f"{number:.4f}".rstrip("0").rstrip(".")


def normalize_market_data_bars(
    raw_bars: list[dict[str, Any]],
    ticker: str,
    timeframe: str,
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    clean_ticker = str(ticker or "").strip().upper()
    clean_timeframe = str(timeframe or "").strip() or "15m"
    for raw in raw_bars:
        open_value = _to_float(_first_value(raw, ["open", "o"]))
        high_value = _to_float(_first_value(raw, ["high", "h"]))
        low_value = _to_float(_first_value(raw, ["low", "l"]))
        close_value = _to_float(_first_value(raw, ["close", "c"]))
        if None in [open_value, high_value, low_value, close_value]:
            continue
        volume_value = _to_float(_first_value(raw, ["volume", "v"]))
        normalized.append(
            {
                "ticker": clean_ticker,
                "timeframe": clean_timeframe,
                "timestamp": str(_first_value(raw, ["timestamp", "time", "t"])).strip(),
                "open": open_value,
                "high": high_value,
                "low": low_value,
                "close": close_value,
                "volume": volume_value if volume_value is not None else "",
            }
        )
    return normalized


def _ema(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    ema_value = sum(values[:period]) / period
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        ema_value = (value - ema_value) * multiplier + ema_value
    return ema_value


def _ema_series(values: list[float], period: int) -> list[Optional[float]]:
    if len(values) < period:
        return []
    series: list[Optional[float]] = [None] * (period - 1)
    ema_value = sum(values[:period]) / period
    series.append(ema_value)
    multiplier = 2 / (period + 1)
    for value in values[period:]:
        ema_value = (value - ema_value) * multiplier + ema_value
        series.append(ema_value)
    return series


def _wma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    window = values[-period:]
    weights = list(range(1, period + 1))
    return sum(value * weight for value, weight in zip(window, weights)) / sum(weights)


def _sma(values: list[float], period: int) -> Optional[float]:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def _macd_hist(values: list[float]) -> Optional[float]:
    if len(values) < 35:
        return None
    ema12 = _ema_series(values, 12)
    ema26 = _ema_series(values, 26)
    macd_values = [
        short - long
        for short, long in zip(ema12, ema26)
        if short is not None and long is not None
    ]
    if len(macd_values) < 9:
        return None
    signal = _ema(macd_values, 9)
    if signal is None:
        return None
    return macd_values[-1] - signal


def compute_market_data_indicators(bars: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [_to_float(bar.get("close")) for bar in bars]
    close_values = [value for value in closes if value is not None]
    if not close_values:
        return {
            "close": "",
            "ema9": "",
            "ema21": "",
            "wma50": "",
            "wma200": "",
            "sma200": "",
            "macd_hist": "",
            "source_note": "READ_ONLY_MARKET_DATA",
        }
    return {
        "close": _format_number(close_values[-1]),
        "ema9": _format_number(_ema(close_values, 9)),
        "ema21": _format_number(_ema(close_values, 21)),
        "wma50": _format_number(_wma(close_values, 50)),
        "wma200": _format_number(_wma(close_values, 200)),
        "sma200": _format_number(_sma(close_values, 200)),
        "macd_hist": _format_number(_macd_hist(close_values)),
        "source_note": "READ_ONLY_MARKET_DATA",
    }


def market_data_rows_to_tradingview_import_csv(rows: list[dict[str, Any]]) -> str:
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=TRADINGVIEW_IMPORT_HEADER, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "ticker": str(row.get("ticker", "") or "").strip().upper(),
                "price": _format_number(row.get("close") or row.get("price")),
                "timeframe": str(row.get("timeframe", "") or "15m").strip() or "15m",
                "bias_note": "unclear",
                "key_level_note": "read-only market data; verify chart manually",
                "ema9": _format_number(row.get("ema9")),
                "ema21": _format_number(row.get("ema21")),
                "wma50": _format_number(row.get("wma50")),
                "wma200": _format_number(row.get("wma200")),
                "sma200": _format_number(row.get("sma200")),
                "support1": _format_number(row.get("support1")),
                "support2": _format_number(row.get("support2")),
                "resistance1": _format_number(row.get("resistance1")),
                "resistance2": _format_number(row.get("resistance2")),
                "breakout": _format_number(row.get("breakout")),
                "breakdown": _format_number(row.get("breakdown")),
                "invalid": _format_number(row.get("invalid")),
                "relative_volume": _format_number(row.get("relative_volume")),
                "macd_hist": _format_number(row.get("macd_hist")),
                "notes": "CALIBRATION ONLY — read-only market data import; verify chart manually; no orders",
            }
        )
    return output.getvalue()
