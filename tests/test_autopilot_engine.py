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


def evaluate_confirmed_fixture(**earnings: object):
    """Evaluate one otherwise-confirmed setup with only earnings inputs varied."""

    bars = trend_bars()
    return evaluate_setup(
        "AAPL",
        {"1M": bars, "1W": bars, "1D": bars, "4H": bars, "1H": bars, "15M": bars, "5M": bars},
        market_context=MarketContext(
            regime="risk-on",
            spy_direction="bullish",
            qqq_direction="bullish",
        ),
        market_status="open",
        data_label="delayed",
        average_daily_dollar_volume=500_000_000,
        **earnings,
    )


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
        result = evaluate_confirmed_fixture()
        self.assertEqual(result.state, "ARMED")
        self.assertEqual(result.verdict, "WAIT FOR CONFIRMATION")
        self.assertFalse(result.entry_conditions_satisfied)
        self.assertEqual(result.earnings_status, "unresolved")
        self.assertIn("earnings date must be verified", result.do_this_now)

    def test_provider_diagnostics_are_genericized_before_reaching_the_decision(self) -> None:
        result = evaluate_confirmed_fixture(
            provider_warnings=["debug:/private/tmp/provider.log?api_key=secret"],
        )

        serialized = str(result.to_dict())
        self.assertNotIn("/private/tmp", serialized)
        self.assertNotIn("api_key", serialized)
        self.assertIn("supporting provider inputs", serialized)

    def test_verified_none_allows_same_confirmed_fixture_to_enter(self) -> None:
        unresolved = evaluate_confirmed_fixture()
        verified_none = evaluate_confirmed_fixture(
            earnings_status="verified_none",
            earnings_date=None,
            earnings_date_status=None,
            earnings_checked_through="2030-01-12",
        )

        self.assertEqual(unresolved.state, "ARMED")
        self.assertEqual(verified_none.state, "ENTER")
        self.assertEqual(verified_none.verdict, "ENTER")
        self.assertTrue(verified_none.entry_conditions_satisfied)
        self.assertEqual(verified_none.earnings_status, "verified_none")
        self.assertFalse(
            any("must be verified" in warning.lower() for warning in verified_none.warnings)
        )
        self.assertIn(
            "No earnings event was returned inside the verified vendor-calendar window.",
            verified_none.full_breakdown["earnings_news_and_catalysts"],
        )

    def test_scheduled_earnings_windows_block_or_warn_at_boundaries(self) -> None:
        hard_window = evaluate_confirmed_fixture(
            earnings_status="scheduled",
            earnings_date="2030-01-04",
            earnings_date_status="confirmed",
            days_to_earnings=3,
        )
        warning_window = evaluate_confirmed_fixture(
            earnings_status="scheduled",
            earnings_date="2030-01-11",
            earnings_date_status="projected",
            days_to_earnings=10,
        )

        self.assertEqual(hard_window.state, "BLOCKED")
        self.assertEqual(hard_window.verdict, "PASS")
        self.assertTrue(
            any("inside the hard catalyst-risk window" in blocker for blocker in hard_window.blockers)
        )
        self.assertEqual(warning_window.state, "ENTER")
        self.assertTrue(any("Earnings are 10 day(s) away." == item for item in warning_window.warnings))
        self.assertFalse(any("hard catalyst" in blocker for blocker in warning_window.blockers))

    def test_inconsistent_earnings_status_and_date_fail_closed(self) -> None:
        cases = (
            ("scheduled without date", {"earnings_status": "scheduled"}),
            (
                "verified none with date",
                {"earnings_status": "verified_none", "earnings_date": "2030-01-11"},
            ),
            ("unsupported status", {"earnings_status": "provider_guess"}),
            (
                "verified none without complete checked window",
                {"earnings_status": "verified_none"},
            ),
            (
                "scheduled without days",
                {
                    "earnings_status": "scheduled",
                    "earnings_date": "2030-01-11",
                    "earnings_date_status": "confirmed",
                },
            ),
            (
                "scheduled with negative days",
                {
                    "earnings_status": "scheduled",
                    "earnings_date": "2030-01-01",
                    "earnings_date_status": "confirmed",
                    "days_to_earnings": -1,
                },
            ),
        )
        for label, earnings in cases:
            with self.subTest(label=label):
                result = evaluate_confirmed_fixture(**earnings)
                self.assertEqual(result.earnings_status, "unresolved")
                self.assertEqual(result.state, "ARMED")
                self.assertEqual(result.verdict, "WAIT FOR CONFIRMATION")
                self.assertFalse(result.entry_conditions_satisfied)
                self.assertTrue(
                    any("must be verified" in warning.lower() for warning in result.warnings)
                )

    def test_decision_serializes_earnings_verification_fields(self) -> None:
        result = evaluate_confirmed_fixture(
            earnings_status="verified_none",
            earnings_date=None,
            earnings_date_status=None,
            earnings_checked_through="2030-01-12",
        )
        payload = result.to_dict()

        self.assertIsNone(payload["earnings_date"])
        self.assertEqual(payload["earnings_status"], "verified_none")
        self.assertIsNone(payload["earnings_date_status"])
        self.assertEqual(payload["earnings_checked_through"], "2030-01-12")

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
