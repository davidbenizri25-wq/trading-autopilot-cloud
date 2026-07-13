from __future__ import annotations

import unittest
from pathlib import Path


class SolUltraPineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = (Path(__file__).parents[1] / "tradingview" / "trading_autopilot_sol_ultra.pine").read_text(encoding="utf-8")

    def test_exact_indicator_baseline(self) -> None:
        for needle in ["ta.ema(close, 9)", "ta.wma(close, 21)", "ta.wma(close, 50)", "ta.wma(close, 200)", "ta.sma(close, 200)", "ta.macd(close, 12, 26, 9)", "ta.vwap(hlc3)"]:
            self.assertIn(needle, self.source)
        self.assertNotIn("ta.ema(close, 21)", self.source)

    def test_is_indicator_only_and_never_executes(self) -> None:
        self.assertIn("indicator(", self.source)
        self.assertNotIn("strategy(", self.source)
        self.assertNotIn("strategy.entry", self.source)
        self.assertIn("No order is placed", self.source)

    def test_level_transfer_inputs_exist(self) -> None:
        for needle in ["Trigger", "Entry zone low", "Entry zone high", "Invalidation", "Target 1", "Target 2", "Stretch target"]:
            self.assertIn(needle, self.source)


if __name__ == "__main__":
    unittest.main()
