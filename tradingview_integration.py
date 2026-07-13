"""Safe, read-only TradingView handoff helpers.

The public TradingView widget does not expose a user's private layouts,
watchlists, Pine scripts, or account state.  This module intentionally keeps
the integration one-way: Trading Autopilot selects a symbol/timeframe and the
chart or full TradingView handoff displays it.  No credentials, alerts,
orders, or broker surfaces are involved.
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlencode


DEFAULT_TRADINGVIEW_SYMBOL = "SPY"
TRADINGVIEW_INTERVALS = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
    "45m": "45",
    "1h": "60",
    "2h": "120",
    "4h": "240",
    "1d": "D",
    "1w": "W",
    "1mo": "M",
}
TRADINGVIEW_EXCHANGES = {
    "XNAS": "NASDAQ",
    "XNGS": "NASDAQ",
    "XNCM": "NASDAQ",
    "NASDAQGS": "NASDAQ",
    "NASDAQGM": "NASDAQ",
    "NASDAQCM": "NASDAQ",
    "XNYS": "NYSE",
    "ARCX": "AMEX",
    "XASE": "AMEX",
    "NYSEARCA": "AMEX",
    "BATS": "CBOE",
}


def _safe_symbol_piece(value: Any, *, allow_colon: bool = True) -> str:
    """Return a conservative TradingView-safe symbol fragment."""

    text = str(value or "").strip().upper().replace(" ", "")
    allowed = r"[^A-Z0-9._!:/\-]" if allow_colon else r"[^A-Z0-9._!\-]"
    return re.sub(allowed, "", text)[:64]


def normalize_tradingview_symbol(
    value: Any,
    exchange: Any = None,
    *,
    fallback: str = DEFAULT_TRADINGVIEW_SYMBOL,
) -> str:
    """Normalize a symbol without guessing its exchange.

    An explicit ``EXCHANGE:SYMBOL`` value is preserved.  When an exchange is
    supplied separately, it is joined to the ticker.  Otherwise the ticker is
    passed to TradingView as-is so the widget can resolve it.
    """

    symbol = _safe_symbol_piece(value)
    safe_fallback = _safe_symbol_piece(fallback) or DEFAULT_TRADINGVIEW_SYMBOL
    if not symbol:
        return safe_fallback
    if ":" in symbol:
        exchange_part, ticker_part = symbol.split(":", 1)
        exchange_part = _safe_symbol_piece(exchange_part, allow_colon=False)
        exchange_part = TRADINGVIEW_EXCHANGES.get(exchange_part, exchange_part)
        ticker_part = _safe_symbol_piece(ticker_part, allow_colon=False)
        return f"{exchange_part}:{ticker_part}" if exchange_part and ticker_part else safe_fallback
    exchange_part = _safe_symbol_piece(exchange, allow_colon=False)
    exchange_part = TRADINGVIEW_EXCHANGES.get(exchange_part, exchange_part)
    return f"{exchange_part}:{symbol}" if exchange_part else symbol


def candidate_tradingview_symbol(row: Mapping[str, Any] | None) -> str:
    """Resolve the best explicit TradingView symbol available on a row."""

    row = row or {}
    explicit = row.get("tradingview_symbol") or row.get("tv_symbol")
    if explicit:
        return normalize_tradingview_symbol(explicit)
    ticker = row.get("underlying_ticker") or row.get("underlying") or row.get("ticker")
    exchange = row.get("exchange") or row.get("primary_exchange")
    return normalize_tradingview_symbol(ticker, exchange)


def tradingview_interval(timeframe: Any) -> str:
    """Map app timeframe labels to TradingView widget intervals."""

    value = str(timeframe or "1D").strip().lower().replace(" ", "")
    aliases = {
        "60m": "1h",
        "240m": "4h",
        "day": "1d",
        "daily": "1d",
        "week": "1w",
        "weekly": "1w",
        "month": "1mo",
        "monthly": "1mo",
    }
    return TRADINGVIEW_INTERVALS.get(aliases.get(value, value), "D")


def tradingview_chart_url(symbol: Any, timeframe: Any = None) -> str:
    """Build a full TradingView chart handoff URL."""

    safe_symbol = normalize_tradingview_symbol(symbol)
    query = {"symbol": safe_symbol}
    if timeframe not in (None, ""):
        query["interval"] = tradingview_interval(timeframe)
    return "https://www.tradingview.com/chart/?" + urlencode(query)


def _safe_watchlist(symbols: Iterable[Any], selected_symbol: str) -> list[str]:
    watchlist: list[str] = []
    for raw in [selected_symbol, *list(symbols)]:
        symbol = normalize_tradingview_symbol(raw)
        if symbol not in watchlist:
            watchlist.append(symbol)
        if len(watchlist) >= 30:
            break
    return watchlist


def build_tradingview_widget_config(
    symbol: Any,
    timeframe: Any,
    *,
    watchlist: Iterable[Any] = (),
    compact: bool = False,
    theme: str = "dark",
) -> dict[str, Any]:
    """Return the public Advanced Chart widget configuration."""

    selected_symbol = normalize_tradingview_symbol(symbol)
    selected_theme = "light" if str(theme).lower() == "light" else "dark"
    return {
        "autosize": True,
        "symbol": selected_symbol,
        "interval": tradingview_interval(timeframe),
        "timezone": "exchange",
        "theme": selected_theme,
        "style": "1",
        "locale": "en",
        "backgroundColor": "#07111f" if selected_theme == "dark" else "#ffffff",
        "gridColor": "rgba(148, 163, 184, 0.08)" if selected_theme == "dark" else "rgba(15, 23, 42, 0.08)",
        "withdateranges": True,
        "hide_side_toolbar": bool(compact),
        "allow_symbol_change": True,
        "save_image": False,
        "calendar": False,
        "details": not compact,
        "hotlist": False,
        "watchlist": _safe_watchlist(watchlist, selected_symbol),
        "support_host": "https://www.tradingview.com",
    }


def tradingview_widget_html(
    symbol: Any,
    timeframe: Any,
    *,
    watchlist: Iterable[Any] = (),
    compact: bool = False,
    theme: str = "dark",
) -> str:
    """Build injection-safe HTML for Streamlit's components iframe."""

    config = build_tradingview_widget_config(
        symbol,
        timeframe,
        watchlist=watchlist,
        compact=compact,
        theme=theme,
    )
    config_json = json.dumps(config, ensure_ascii=True, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      html, body, .tradingview-widget-container, .tradingview-widget-container__widget {{
        width: 100%; height: 100%; margin: 0; overflow: hidden; background: #07111f;
      }}
    </style>
  </head>
  <body>
    <div class="tradingview-widget-container">
      <div class="tradingview-widget-container__widget"></div>
      <script type="text/javascript"
        src="https://s3.tradingview.com/external-embedding/embed-widget-advanced-chart.js"
        async>{config_json}</script>
    </div>
  </body>
</html>"""
