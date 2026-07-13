from __future__ import annotations

import unittest

from autopilot_engine import (
    MarketContext,
    analyze_timeframe,
    evaluate_setup,
    evaluate_tracked_plan,
    important_state_change,
    normalize_bars,
    wma_series,
)


def trend_bars(*, bullish: bool = True, count: int = 260, start: float = 100.0) -> list[dict[str, float]]:
    bars: list[dict[str, float]] = []
    direction = 1 if bullish else -1
    price = start
    for index in range(count):
        open_ = price
        close = open_ + direction * 0.35
        bars.append(
            {
                "t": 1_700_000_000_000 + index * 900_000,
                "o": open_,
                "h": max(open_, close) + 0.25,
                "l": min(open_, close) - 0.25,
                "c": close,
                "v": 2_000_000 + index * 100,
            }
        )
        price = close
    return bars


class AutopilotEngineTests(unittest.TestCase):
    def test_normalize_bars_rejects_impossible_candle(self) -> None:
        rows = trend_bars(count=2)
        rows.append({"t": 1, "o": 10, "h": 8, "l": 9, "c": 11, "v": 1})
        self.assertEqual(len(normalize_bars(rows)), 2)

    def test_uses_21_wma(self) -> None:
        values = [float(value) for value in range(1, 30)]
        expected = sum(value * weight for weight, value in enumerate(values[-21:], 1)) / sum(range(1, 22))
        self.assertAlmostEqual(wma_series(values, 21)[-1], expected)
        analysis = analyze_timeframe("1D", trend_bars())
        self.assertIsNotNone(analysis.wma21)
        self.assertEqual(analysis.direction, "bullish")

    def test_complete_aligned_setup_can_enter(self) -> None:
        bars = trend_bars()
        result = evaluate_setup(
            "AAPL",
            {"1M": bars, "1W": bars, "1D": bars, "4H": bars, "1H": bars, "15M": bars, "5M": bars},
            market_context=MarketContext(regime="risk-on", spy_direction="bullish", qqq_direction="bullish"),
            market_status="open",
            data_label="delayed",
            average_daily_dollar_volume=500_000_000,
            earnings_date="2030-12-01",
            days_to_earnings=120,
        )
        self.assertEqual(result.verdict, "ENTER")
        self.assertEqual(result.state, "ENTER")
        self.assertTrue(result.entry_conditions_satisfied)
        self.assertGreaterEqual(result.confidence, 70)
        self.assertIsNotNone(result.plan.trigger)
        self.assertIsNotNone(result.plan.invalidation)
        self.assertGreaterEqual(result.plan.reward_to_risk or 0, 1.8)
        self.assertIn("calculated fallback", result.plan.target_basis["target_1"])

    def test_required_frames_need_sixty_bars_and_complete_indicators(self) -> None:
        bars = trend_bars(count=26)
        result = evaluate_setup(
            "AAPL",
            {"1W": bars, "1D": bars, "4H": bars, "15M": bars},
            market_context=MarketContext(
                regime="risk-on", spy_direction="bullish", qqq_direction="bullish"
            ),
            data_label="delayed",
            average_daily_dollar_volume=500_000_000,
            earnings_date="2030-12-01",
            days_to_earnings=120,
        )
        self.assertEqual(result.state, "BLOCKED")
        self.assertTrue(any("at least 60 valid bars" in blocker for blocker in result.blockers))
        self.assertIsNone(result.timeframes["15M"].wma50)
        self.assertIsNone(result.timeframes["15M"].macd_histogram)

    def test_opposing_daily_structure_blocks_weighted_bullish_enter(self) -> None:
        bullish = trend_bars()
        bearish = trend_bars(bullish=False, start=300)
        result = evaluate_setup(
            "AAPL",
            {"1W": bullish, "1D": bearish, "4H": bullish, "15M": bullish},
            market_context=MarketContext(
                regime="risk-on", spy_direction="bullish", qqq_direction="bullish"
            ),
            data_label="delayed",
            average_daily_dollar_volume=500_000_000,
            earnings_date="2030-12-01",
            days_to_earnings=120,
        )
        self.assertEqual(result.state, "BLOCKED")
        self.assertTrue(any("1D structure is bearish" in blocker for blocker in result.blockers))

    def test_unknown_earnings_holds_technically_confirmed_setup_at_armed(self) -> None:
        bars = trend_bars()
        result = evaluate_setup(
            "AAPL",
            {"1W": bars, "1D": bars, "4H": bars, "15M": bars},
            market_context=MarketContext(
                regime="risk-on", spy_direction="bullish", qqq_direction="bullish"
            ),
            data_label="delayed",
            average_daily_dollar_volume=500_000_000,
        )
        self.assertEqual(result.state, "ARMED")
        self.assertEqual(result.verdict, "WAIT FOR CONFIRMATION")
        self.assertFalse(result.entry_conditions_satisfied)
        self.assertIn("earnings date must be verified", result.do_this_now)

    def test_two_completed_fifteen_minute_closes_are_required(self) -> None:
        bars = trend_bars()
        structural_trigger = max(item["h"] for item in bars[-22:-2])
        bars[-2].update(
            o=structural_trigger - 0.20,
            h=structural_trigger + 0.05,
            l=structural_trigger - 0.30,
            c=structural_trigger - 0.01,
        )
        bars[-1].update(
            o=structural_trigger + 0.01,
            h=structural_trigger + 0.20,
            l=structural_trigger - 0.05,
            c=structural_trigger + 0.04,
        )
        result = evaluate_setup(
            "AAPL",
            {"1W": bars, "1D": bars, "4H": bars, "15M": bars},
            market_context=MarketContext(
                regime="risk-on", spy_direction="bullish", qqq_direction="bullish"
            ),
            data_label="delayed",
            average_daily_dollar_volume=500_000_000,
            earnings_date="2030-12-01",
            days_to_earnings=120,
        )
        self.assertNotEqual(result.state, "ENTER")
        self.assertAlmostEqual(result.plan.trigger or 0, structural_trigger, places=2)

    def test_nearest_observed_structure_controls_reward_to_risk(self) -> None:
        bars = trend_bars()
        current = bars[-1]["c"]
        bars[-30]["h"] = current + 0.35
        result = evaluate_setup(
            "AAPL",
            {"1W": bars, "1D": bars, "4H": bars, "15M": bars},
            market_context=MarketContext(
                regime="risk-on", spy_direction="bullish", qqq_direction="bullish"
            ),
            data_label="delayed",
            average_daily_dollar_volume=500_000_000,
            earnings_date="2030-12-01",
            days_to_earnings=120,
        )
        self.assertEqual(result.state, "BLOCKED")
        self.assertLess(result.plan.reward_to_risk or 99, 1.8)
        self.assertEqual(
            result.plan.target_basis["target_1"], "nearest observed opposing structure"
        )

    def test_market_conflict_blocks_enter(self) -> None:
        bars = trend_bars()
        result = evaluate_setup(
            "AAPL",
            {"1W": bars, "1D": bars, "4H": bars, "15M": bars},
            market_context=MarketContext(regime="risk-off", spy_direction="bearish", qqq_direction="bearish"),
            data_label="delayed",
            average_daily_dollar_volume=500_000_000,
        )
        self.assertEqual(result.state, "BLOCKED")
        self.assertEqual(result.verdict, "PASS")
        self.assertTrue(any("risk-off" in blocker for blocker in result.blockers))

    def test_missing_timeframes_never_enter(self) -> None:
        result = evaluate_setup(
            "AAPL",
            {"1D": trend_bars()},
            market_context=MarketContext(regime="risk-on"),
            data_label="real-time",
        )
        self.assertEqual(result.verdict, "PASS")
        self.assertLess(result.confidence, 40)

    def test_earnings_hard_window_blocks(self) -> None:
        bars = trend_bars()
        result = evaluate_setup(
            "AAPL",
            {"1W": bars, "1D": bars, "4H": bars, "15M": bars},
            market_context=MarketContext(regime="risk-on"),
            data_label="delayed",
            average_daily_dollar_volume=500_000_000,
            earnings_date="2030-01-02",
            days_to_earnings=2,
        )
        self.assertEqual(result.state, "BLOCKED")
        self.assertTrue(any("Earnings" in blocker for blocker in result.blockers))

    def test_tracked_plan_events(self) -> None:
        plan = {
            "state": "ENTER",
            "plan": {
                "direction": "bullish",
                "invalidation": 95,
                "target_1": 110,
                "target_2": 120,
                "stretch_target": 130,
            },
        }
        self.assertEqual(evaluate_tracked_plan(plan, 94)["state"], "INVALIDATED")
        self.assertIn("target 1 reached", evaluate_tracked_plan(plan, 111)["events"])

    def test_high_value_state_change_only(self) -> None:
        self.assertEqual(important_state_change("ARMED", "ENTER"), "ARMED → ENTER")
        self.assertIsNone(important_state_change("FORMING", "FORMING"))
        self.assertIsNone(important_state_change("BLOCKED", "FORMING"))


if __name__ == "__main__":
    unittest.main()
