from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from autopilot_chart import CHART_COLORS, build_autopilot_chart


class AutopilotChartTests(unittest.TestCase):
    def _rows(self, *, count: int = 240, minutes: int = 15) -> list[dict[str, float]]:
        rows = []
        price = 100.0
        started = datetime(2023, 11, 14, 14, 30, tzinfo=timezone.utc)
        for index in range(count):
            rows.append(
                {
                    "timestamp": (started + timedelta(minutes=index * minutes)).timestamp() * 1_000,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price + 0.25,
                    "volume": 1_000_000 + index * 1_000,
                }
            )
            price += 0.25
        return rows

    def _decision(self) -> dict:
        return {
            "direction": "bullish",
            "earnings_date": "2023-11-17",
            "plan": {
                "trigger": 155,
                "entry_low": 154.5,
                "entry_high": 155.5,
                "invalidation": 150,
                "target_1": 165,
                "target_2": 170,
                "stretch_target": 175,
            },
            "timeframes": {"1D": {"support": [145], "resistance": [180]}},
        }

    def test_chart_requires_valid_bars(self) -> None:
        with self.assertRaises(ValueError):
            build_autopilot_chart([], {})

    def test_visual_language_is_stable(self) -> None:
        self.assertEqual(CHART_COLORS["background"], "#131722")
        self.assertEqual(CHART_COLORS["bull_candle"], "#F8FAFC")
        self.assertEqual(CHART_COLORS["bear_candle"], "#2962FF")
        self.assertEqual(CHART_COLORS["ema9"], "#2962FF")
        self.assertEqual(CHART_COLORS["wma21"], "#F23645")
        self.assertEqual(CHART_COLORS["wma50"], "#FDD835")
        self.assertEqual(CHART_COLORS["wma200"], "#AB47BC")
        self.assertEqual(CHART_COLORS["sma200"], "#26A69A")

    def test_chart_contains_methodology_and_trade_plan(self) -> None:
        figure = build_autopilot_chart(
            self._rows(),
            self._decision(),
            timeframe="15m",
            selected_analysis={
                "close": 160,
                "atr": 2.0,
                "direction": "bullish",
                "support": [145],
                "resistance": [180],
            },
        )
        names = {trace.name for trace in figure.data}
        self.assertTrue(
            {
                "Price",
                "9 EMA",
                "21 WMA",
                "50 WMA",
                "200 WMA",
                "200 SMA",
                "Session VWAP",
                "Volume",
                "MACD histogram",
                "MACD 12/26",
                "Signal 9",
            }.issubset(names)
        )
        annotations = {annotation.text for annotation in figure.layout.annotations}
        self.assertTrue({"15m demand", "15m supply", "Entry zone", "Earnings"}.issubset(annotations))
        self.assertIsNotNone(figure.layout.yaxis3)
        self.assertGreaterEqual(len(figure.layout.shapes), 9)

    def test_vwap_is_intraday_only_and_timeframe_updates_title(self) -> None:
        intraday = build_autopilot_chart(self._rows(), self._decision(), timeframe="1H")
        daily = build_autopilot_chart(self._rows(minutes=1_440), self._decision(), timeframe="1D")
        self.assertIn("Session VWAP", {trace.name for trace in intraday.data})
        self.assertNotIn("Session VWAP", {trace.name for trace in daily.data})
        self.assertIn("1H", intraday.layout.title.text)
        self.assertIn("1D", daily.layout.title.text)

    def test_compact_chart_keeps_all_panels_without_legend(self) -> None:
        figure = build_autopilot_chart(
            self._rows(),
            self._decision(),
            timeframe="4H",
            compact=True,
        )
        self.assertEqual(figure.layout.height, 440)
        self.assertFalse(figure.layout.showlegend)
        self.assertIsNotNone(figure.layout.yaxis3)


if __name__ == "__main__":
    unittest.main()
