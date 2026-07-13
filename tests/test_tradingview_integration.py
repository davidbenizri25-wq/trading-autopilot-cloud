from __future__ import annotations

import json
import re
import unittest

from tradingview_integration import (
    build_tradingview_widget_config,
    candidate_tradingview_symbol,
    normalize_tradingview_symbol,
    tradingview_chart_url,
    tradingview_interval,
    tradingview_widget_html,
)


class TradingViewIntegrationTests(unittest.TestCase):
    def test_symbol_normalization_preserves_explicit_exchange(self) -> None:
        self.assertEqual(normalize_tradingview_symbol(" nasdaq:aapl "), "NASDAQ:AAPL")
        self.assertEqual(normalize_tradingview_symbol("AAPL", "NASDAQ"), "NASDAQ:AAPL")
        self.assertEqual(normalize_tradingview_symbol("AAPL", "XNAS"), "NASDAQ:AAPL")
        self.assertEqual(normalize_tradingview_symbol("SPY", "ARCX"), "AMEX:SPY")

    def test_symbol_normalization_removes_markup(self) -> None:
        value = normalize_tradingview_symbol('AAPL\"><script>alert(1)</script>')
        self.assertNotIn("<", value)
        self.assertNotIn(">", value)
        self.assertNotIn('"', value)

    def test_candidate_prefers_explicit_symbol_and_underlying(self) -> None:
        self.assertEqual(
            candidate_tradingview_symbol({"ticker": "AAPL", "tradingview_symbol": "NASDAQ:AAPL"}),
            "NASDAQ:AAPL",
        )
        self.assertEqual(
            candidate_tradingview_symbol({"ticker": "AAPL240119C00200000", "underlying_ticker": "AAPL"}),
            "AAPL",
        )

    def test_timeframe_mapping(self) -> None:
        expected = {"15m": "15", "30m": "30", "1h": "60", "4h": "240", "1D": "D", "1W": "W"}
        for timeframe, interval in expected.items():
            with self.subTest(timeframe=timeframe):
                self.assertEqual(tradingview_interval(timeframe), interval)

    def test_chart_url_is_encoded(self) -> None:
        self.assertEqual(
            tradingview_chart_url("NASDAQ:AAPL"),
            "https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL",
        )
        self.assertEqual(
            tradingview_chart_url("NASDAQ:AAPL", "15m"),
            "https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL&interval=15",
        )

    def test_widget_config_deduplicates_and_limits_watchlist(self) -> None:
        config = build_tradingview_widget_config("AAPL", "4h", watchlist=["AAPL", "MSFT", "MSFT"])
        self.assertEqual(config["symbol"], "AAPL")
        self.assertEqual(config["interval"], "240")
        self.assertEqual(config["watchlist"], ["AAPL", "MSFT"])
        self.assertFalse(config["save_image"])

    def test_widget_html_contains_only_serialized_safe_config(self) -> None:
        widget = tradingview_widget_html('AAPL</script><script>alert(1)</script>', "15m")
        self.assertEqual(widget.count("embed-widget-advanced-chart.js"), 1)
        self.assertNotIn("</script><script>alert", widget)
        match = re.search(r"async>(\{.*\})</script>", widget)
        self.assertIsNotNone(match)
        config = json.loads(match.group(1))
        self.assertEqual(config["interval"], "15")


if __name__ == "__main__":
    unittest.main()
