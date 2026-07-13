from __future__ import annotations

import unittest

from autopilot_chart import CHART_COLORS, build_autopilot_chart


class AutopilotChartTests(unittest.TestCase):
    def test_chart_requires_valid_bars(self) -> None:
        with self.assertRaises(ValueError):
            build_autopilot_chart([], {})

    def test_visual_language_is_stable(self) -> None:
        self.assertEqual(CHART_COLORS["supply"], "rgba(239,68,68,0.13)")
        self.assertEqual(CHART_COLORS["demand"], "rgba(34,197,94,0.13)")
        self.assertEqual(CHART_COLORS["wma21"], "#38BDF8")

    def test_chart_contains_methodology_and_trade_plan(self) -> None:
        rows = []
        price = 100.0
        for index in range(240):
            rows.append(
                {
                    "timestamp": 1_700_000_000_000 + index * 900_000,
                    "open": price,
                    "high": price + 1,
                    "low": price - 1,
                    "close": price + 0.25,
                    "volume": 1_000_000,
                }
            )
            price += 0.25
        decision = {
            "direction": "bullish",
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
        figure = build_autopilot_chart(rows, decision)
        names = {trace.name for trace in figure.data}
        self.assertTrue({"Price", "9 EMA", "21 WMA", "50 WMA", "200 WMA", "200 SMA"}.issubset(names))
        self.assertGreaterEqual(len(figure.layout.shapes), 7)


if __name__ == "__main__":
    unittest.main()
