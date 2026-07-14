from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import unittest

from presentation_export import build_presentation_payload, presentation_pdf_bytes


REPORTLAB_AVAILABLE = importlib.util.find_spec("reportlab") is not None


def complete_decision() -> dict[str, object]:
    return {
        "ticker": "AAPL",
        "name": "Apple Inc.",
        "exchange": "NASDAQ",
        "verdict": "ENTER",
        "state": "ENTER",
        "direction": "bullish",
        "confidence": 84,
        "grade": "A",
        "current_price": 210.25,
        "market_status": "open",
        "data_label": "real-time",
        "data_source": "Polygon",
        "data_timestamp": "2026-07-13T15:00:01Z",
        "plan": {
            "setup_type": "break-and-retest",
            "trigger": 210.0,
            "entry_low": 210.0,
            "entry_high": 211.0,
            "invalidation": 205.0,
            "target_1": 218.0,
            "target_2": 224.0,
            "stretch_target": 230.0,
            "reward_to_risk": 2.4,
            "horizon": "2-20 trading days",
        },
        "do_this_now": "Entry conditions are satisfied; keep risk defined.",
        "primary_risk": "A failed retest.",
        "upgrade_condition": "Stronger relative volume on the retest.",
        "invalidation_condition": "A decisive close below $205.00.",
        "reasons": [
            "Weekly trend aligns.",
            "15m confirmation passed.",
            "Market regime supports the thesis.",
        ],
        "market_context": {"regime": "risk-on"},
        "timeframes": {
            "1M": {"direction": "bullish"},
            "1W": {"direction": "bullish"},
            "1D": {"direction": "bullish"},
            "4H": {"direction": "bullish"},
            "1H": {"direction": "mixed"},
            "15M": {"direction": "bullish"},
            "5M": {"direction": "mixed"},
        },
    }


def public_payload(decision: dict[str, object] | None = None) -> dict[str, object]:
    return build_presentation_payload(
        decision or complete_decision(),
        timeframe="15m",
        tradingview_symbol="NASDAQ:AAPL",
        tradingview_url="https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL&interval=15",
        generated_at=datetime(2026, 7, 13, 15, 5, tzinfo=timezone.utc),
    )


class PresentationExportTests(unittest.TestCase):
    def test_payload_is_allowlisted_deterministic_and_presentation_ready(self) -> None:
        payload = public_payload()
        self.assertEqual(
            set(payload),
            {
                "product",
                "ticker",
                "name",
                "exchange",
                "verdict",
                "state",
                "direction",
                "confidence",
                "grade",
                "current_price",
                "market_status",
                "data_label",
                "data_source",
                "data_timestamp",
                "setup_type",
                "trigger",
                "entry_low",
                "entry_high",
                "invalidation",
                "target_1",
                "target_2",
                "stretch_target",
                "reward_to_risk",
                "horizon",
                "do_now",
                "primary_risk",
                "upgrade",
                "invalidate",
                "reasons",
                "regime",
                "earnings",
                "alignment",
                "timeframe",
                "tradingview_symbol",
                "tradingview_url",
                "generated_at",
                "disclaimer",
            },
        )
        self.assertEqual(payload["ticker"], "AAPL")
        self.assertEqual(payload["confidence"], "84%")
        self.assertEqual(payload["current_price"], "$210.25")
        self.assertEqual(payload["reward_to_risk"], "2.40:1")
        self.assertEqual(payload["earnings"], "Unresolved - entry gated")
        self.assertEqual(payload["generated_at"], "2026-07-13T15:05:00Z")
        self.assertEqual(
            [row["timeframe"] for row in payload["alignment"]],
            ["1M", "1W", "1D", "4H", "1H", "30m", "15m", "5m", "3m", "1m"],
        )
        self.assertEqual(
            [row["timeframe"] for row in payload["alignment"] if row["active"] == "yes"],
            ["15m"],
        )
        self.assertEqual(
            next(row for row in payload["alignment"] if row["timeframe"] == "30m")["direction"],
            "Unavailable",
        )

    def test_selected_execution_interval_analysis_is_exported_without_month_collision(self) -> None:
        payload = build_presentation_payload(
            complete_decision(),
            timeframe="1m",
            tradingview_symbol="NASDAQ:AAPL",
            tradingview_url="https://www.tradingview.com/chart/?symbol=NASDAQ%3AAAPL&interval=1",
            selected_analysis={"direction": "bearish"},
        )

        minute = next(row for row in payload["alignment"] if row["timeframe"] == "1m")
        monthly = next(row for row in payload["alignment"] if row["timeframe"] == "1M")
        self.assertEqual(minute["active"], "yes")
        self.assertEqual(minute["direction"], "Bearish")
        self.assertEqual(monthly["active"], "no")

    def test_payload_redacts_private_paths_credentials_diagnostics_and_state(self) -> None:
        decision = complete_decision()
        decision.update(
            {
                "name": "/Users/person/private/account.json",
                "do_this_now": "path=/Users/person/private/account.json",
                "reasons": [
                    "Traceback: provider exploded",
                    "Relative volume supports the setup.",
                    "debug:/private/tmp/provider.log",
                ],
                "api_key": "top-secret",
                "provider_error": "RuntimeError: private upstream detail",
                "watchlist": ["PRIVATE"],
                "positions": {"AAPL": {"entry": 1}},
                "journal": [{"private_note": "do not export"}],
            }
        )
        payload = public_payload(decision)
        serialized = json.dumps(payload, sort_keys=True)

        for forbidden in (
            "/Users/",
            "api_key",
            "do-not-export",
            "top-secret",
            "Traceback",
            "RuntimeError",
            "PRIVATE KEY",
            "watchlist",
            "positions",
            "journal",
            "provider_error",
            "private_note",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, serialized)
        self.assertEqual(payload["name"], "Security name unavailable")
        self.assertEqual(payload["do_now"], "No entry decision was made.")
        self.assertEqual(payload["reasons"][0], "Unavailable")
        self.assertEqual(payload["reasons"][1], "Relative volume supports the setup.")
        self.assertEqual(payload["reasons"][2], "Unavailable")

    @unittest.skipUnless(REPORTLAB_AVAILABLE, "ReportLab is not installed")
    def test_pdf_export_returns_complete_pdf_bytes(self) -> None:
        result = presentation_pdf_bytes(public_payload())
        self.assertIsInstance(result, bytes)
        self.assertTrue(result.startswith(b"%PDF-"))
        self.assertGreater(len(result), 1_000)
        self.assertIn(b"%%EOF", result[-64:])


if __name__ == "__main__":
    unittest.main()
