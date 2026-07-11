from __future__ import annotations

import unittest
from pathlib import Path

from dashboard import app as dashboard_app


ROOT = Path(__file__).resolve().parents[1]


class ProductUiContractTests(unittest.TestCase):
    def test_readiness_is_tri_state_not_false_ready(self) -> None:
        self.assertEqual(dashboard_app.workflow_readiness_status(0, 0, 0)["label"], "Waiting for data")
        self.assertEqual(dashboard_app.workflow_readiness_status(1, 2, 0)["label"], "Blocked")
        self.assertEqual(
            dashboard_app.workflow_readiness_status(1, 0, 11)["label"],
            "Reviewable with warnings",
        )
        self.assertEqual(dashboard_app.workflow_readiness_status(1, 0, 0)["label"], "Ready for review")

    def test_any_loaded_source_reports_rows_available(self) -> None:
        summary = dashboard_app.review_engine_status_summary(
            [{"ticker": "SPY"}],
            [],
            ["manual chart confirmation required"],
            "TradingView Import",
        )
        self.assertEqual(summary["session_rows_loaded"], "yes")
        self.assertEqual(summary["readiness_label"], "Reviewable with warnings")

    def test_warning_summary_is_plain_english(self) -> None:
        summary = dashboard_app.validation_warning_plain_english_summary(
            [
                "relative_volume missing",
                "manual chart confirmation required",
                "Polygon provider note",
            ]
        )
        self.assertEqual(summary["total"], 3)
        self.assertEqual(summary["groups"]["optional_field_missing"], 1)
        self.assertEqual(summary["groups"]["manual_confirmation"], 1)
        self.assertEqual(summary["groups"]["provider_context"], 1)

    def test_public_source_label_never_leaks_server_path(self) -> None:
        self.assertEqual(dashboard_app._public_source_label("/mount/src/private/data.csv"), "Local CSV")
        self.assertEqual(dashboard_app._public_source_label("TradingView Import"), "Pasted TradingView data")

    def test_beginner_information_architecture_is_compact(self) -> None:
        self.assertEqual(
            dashboard_app.BEGINNER_PAGE_NAMES,
            ["Dashboard", "Analyze", "Charts", "Review", "Journal"],
        )
        source = (ROOT / "dashboard" / "app.py").read_text(encoding="utf-8")
        self.assertNotIn("st.tabs(", source)
        self.assertIn("st.iframe(", source)
        self.assertIn("Open {ticker} in TradingView", source)

    def test_display_version_is_clean(self) -> None:
        self.assertNotIn("dev", dashboard_app.APP_DISPLAY_VERSION.lower())

    def test_calibration_cards_preserve_mobile_summary(self) -> None:
        cards = dashboard_app.build_calibration_mobile_cards(
            [
                {
                    "ticker": "spy",
                    "timeframe_checked": "1D",
                    "key_levels": "support 495 / resistance 505",
                    "dashboard_score": "78",
                    "dashboard_grade": "A-",
                    "dashboard_state": "alert",
                    "dashboard_bucket": "Bullish Options",
                    "match_status": "unclear",
                    "issue_type": "needs_manual_chart_confirmation",
                }
            ]
        )
        self.assertEqual(cards[0]["ticker"], "SPY")
        self.assertEqual(cards[0]["support"], "495")
        self.assertEqual(cards[0]["resistance"], "505")
        self.assertIn("Confirm the chart", cards[0]["next_action"])

    def test_missing_import_fields_are_not_fabricated_from_price(self) -> None:
        row = dashboard_app._candidate_from_tradingview_import_row(
            {"ticker": "SPY", "close": "500", "timeframe": "1D"},
            preserve_missing_as_zero=True,
        )
        for field in ["ema9", "ema21", "wma50", "wma200", "sma200", "support1", "resistance1"]:
            with self.subTest(field=field):
                self.assertEqual(row[field], 0.0)


if __name__ == "__main__":
    unittest.main()
