from __future__ import annotations

import os
from pathlib import Path
import unittest
from unittest.mock import patch

from dashboard.cockpit import (
    BREAKDOWN_SECTIONS,
    build_decision_brief,
    build_home_snapshot,
    currentness_label,
    format_price,
    format_timestamp,
    options_table_rows,
    public_text,
    resolve_state_path,
)


ROOT = Path(__file__).resolve().parents[1]
COCKPIT = ROOT / "dashboard" / "cockpit.py"


def complete_decision(**overrides):
    decision = {
        "ticker": "AAPL",
        "name": "Apple Inc.",
        "exchange": "NASDAQ",
        "verdict": "ENTER",
        "state": "ENTER",
        "direction": "bullish",
        "confidence": 84,
        "confidence_explanation": "84% weighted alignment with current confirmation.",
        "grade": "A",
        "current_price": 210.25,
        "market_status": "open",
        "data_timestamp": "2026-07-13T15:00:01Z",
        "data_label": "real-time",
        "data_source": "Polygon",
        "entry_conditions_satisfied": True,
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
            "horizon": "2–20 trading days",
        },
        "reasons": ["Weekly trend aligns.", "15m confirmation passed.", "Market regime supports the thesis."],
        "primary_risk": "A failed retest.",
        "upgrade_condition": "Stronger relative volume on the retest.",
        "invalidation_condition": "A decisive close below $205.00.",
        "do_this_now": "Entry conditions are satisfied; keep risk defined below $205.00.",
        "warnings": [],
        "full_breakdown": {key: f"Evidence for {key}." for key, _ in BREAKDOWN_SECTIONS},
        "market_context": {"regime": "risk-on"},
        "options": {},
    }
    decision.update(overrides)
    return decision


class CockpitContractTests(unittest.TestCase):
    def test_breakdown_contract_contains_exactly_all_twenty_one_sections(self) -> None:
        self.assertEqual(len(BREAKDOWN_SECTIONS), 21)
        self.assertEqual(BREAKDOWN_SECTIONS[0], ("summary", "1. One-paragraph summary"))
        self.assertEqual(
            BREAKDOWN_SECTIONS[-1],
            ("final_verdict", "21. Final verdict in plain English"),
        )
        self.assertEqual(len({key for key, _ in BREAKDOWN_SECTIONS}), 21)

    def test_top_card_brief_contains_every_decision_field(self) -> None:
        brief = build_decision_brief(complete_decision())
        required = {
            "verdict",
            "state",
            "direction",
            "confidence",
            "confidence_explanation",
            "grade",
            "current_price",
            "market_status",
            "timestamp",
            "currentness",
            "source",
            "setup_type",
            "entry_satisfied",
            "trigger",
            "entry_zone",
            "invalidation",
            "target_1",
            "target_2",
            "stretch_target",
            "reward_to_risk",
            "horizon",
            "reasons",
            "primary_risk",
            "upgrade",
            "invalidate",
            "do_now",
        }
        self.assertTrue(required.issubset(brief))
        self.assertEqual(brief["verdict"], "ENTER")
        self.assertEqual(brief["entry_satisfied"], "Yes")
        self.assertEqual(brief["entry_zone"], "$210.00 – $211.00")
        self.assertEqual(len(brief["reasons"]), 3)

    def test_unavailable_or_stale_market_evidence_forces_pass(self) -> None:
        for data_label in ("unavailable", "stale"):
            with self.subTest(data_label=data_label):
                brief = build_decision_brief(complete_decision(data_label=data_label))
                self.assertEqual(brief["verdict"], "PASS")
                self.assertEqual(brief["state"], "BLOCKED")
                self.assertEqual(brief["entry_satisfied"], "No")
                self.assertIn("no entry decision", brief["do_now"].lower())

    def test_source_currentness_and_exact_timestamp_are_explicit(self) -> None:
        self.assertEqual(currentness_label("real-time"), "Real-time")
        self.assertEqual(currentness_label("delayed"), "Delayed")
        self.assertEqual(currentness_label("last-close"), "Last close")
        self.assertEqual(currentness_label("unknown-value"), "Unavailable")
        self.assertEqual(format_timestamp("2026-07-13T15:00:01Z"), "Jul 13, 2026 · 15:00:01 UTC")
        self.assertEqual(format_timestamp("not a timestamp"), "Unavailable")
        self.assertEqual(format_price(None), "Unavailable")

    def test_public_text_never_echoes_paths_traces_or_credentials(self) -> None:
        unsafe = [
            "/Users/person/private/state.json",
            "/var/app/data.json",
            "RuntimeError: provider exploded",
            "api_key=do-not-show-this",
            "-----BEGIN " + "PRIVATE KEY-----",
        ]
        for value in unsafe:
            with self.subTest(value=value):
                self.assertEqual(public_text(value), "Unavailable")
        self.assertEqual(public_text("15m confirmation is not yet satisfied."), "15m confirmation is not yet satisfied.")

    def test_options_rows_preserve_unavailable_fields_instead_of_inventing_values(self) -> None:
        rows = options_table_rows(
            {
                "status": "RECOMMEND",
                "ranked_contracts": [
                    {
                        "rank": 1,
                        "contract_symbol": "AAPL260821C00210000",
                        "call_put": "call",
                        "expiration": "2026-08-21",
                        "dte": 39,
                        "strike": 210,
                        "bid": 5.0,
                        "ask": 5.2,
                        "mid": 5.1,
                        "spread_dollars": 0.2,
                        "spread_pct": 0.2 / 5.1,
                        "volume": 1400,
                        "open_interest": 5200,
                        "implied_volatility": 0.31,
                        "delta": 0.56,
                        "gamma": 0.04,
                        "theta": -0.05,
                        "vega": 0.13,
                        "breakeven": 215.1,
                        "expected_move": {"amount": 9.4},
                        "earnings_exposure": {"date_known": False},
                        "liquidity_quality": "good",
                        "fit_rationale": ["call matches the bullish thesis"],
                        "why_ranked_lower": [],
                    }
                ],
            }
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["Contract"], "AAPL260821C00210000")
        self.assertEqual(rows[0]["IV rank"], "Unavailable")
        self.assertEqual(rows[0]["Earnings"], "Unavailable")
        self.assertNotIn("0.0%", rows[0]["IV rank"])

    def test_home_snapshot_ranks_watchlist_and_surfaces_state_changes(self) -> None:
        entered = complete_decision(ticker="MSFT", confidence=88, grade="A")
        armed = complete_decision(
            ticker="NVDA",
            verdict="WAIT FOR CONFIRMATION",
            state="ARMED",
            confidence=76,
            grade="A-",
            entry_conditions_satisfied=False,
        )
        state = {
            "watchlist": ["NVDA", "MSFT"],
            "recent_searches": [{"ticker": "MSFT", "searched_at": "2026-07-13T15:00:01Z"}],
            "last_analyses": {
                "NVDA": {**armed, "saved_at": "2026-07-13T14:59:00Z"},
                "MSFT": {**entered, "saved_at": "2026-07-13T15:00:01Z"},
            },
            "state_changes": [
                {
                    "ticker": "MSFT",
                    "transition": "ARMED_TO_ENTER",
                    "recorded_at": "2026-07-13T15:00:01Z",
                }
            ],
        }
        model = build_home_snapshot(state)
        self.assertEqual([item["ticker"] for item in model["watchlist"]], ["MSFT", "NVDA"])
        self.assertEqual(model["enter_candidates"][0]["ticker"], "MSFT")
        self.assertEqual(model["armed_candidates"][0]["ticker"], "NVDA")
        self.assertEqual(model["state_changes"][0]["transition"], "Armed → Enter")
        self.assertEqual(model["regime"], "RISK ON")

    def test_state_path_comes_only_from_named_config_or_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(resolve_state_path({}))
            self.assertEqual(resolve_state_path({"AUTOPILOT_STATE_PATH": "/tmp/cockpit.json"}), "/tmp/cockpit.json")
        with patch.dict(os.environ, {"AUTOPILOT_STATE_PATH": "/tmp/from-env.json"}, clear=True):
            self.assertEqual(resolve_state_path({}), "/tmp/from-env.json")
            self.assertEqual(
                resolve_state_path({"AUTOPILOT_STATE_PATH": "/tmp/from-config.json"}),
                "/tmp/from-config.json",
            )

    def test_streamlit_contract_is_form_first_mobile_safe_and_not_legacy(self) -> None:
        source = COCKPIT.read_text(encoding="utf-8")
        for required in [
            "def render_cockpit(",
            'st.form("autopilot_ticker_search"',
            "form_submit_button",
            'width="stretch"',
            "Open {brief['ticker']} in my TradingView · 15m",
            '"I entered"',
            '"I’m watching"',
            '"I passed"',
            '"Close trade"',
            'st.expander("Advanced"',
            "AutopilotStateStore",
            '"AUTOPILOT_STATE_PATH"',
        ]:
            with self.subTest(required=required):
                self.assertIn(required, source)
        for forbidden in [
            "use_container_width",
            "st.tabs(",
            "st.file_uploader(",
            "st.selectbox(",
            "st.exception(",
            "/Users/",
        ]:
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, source)

    def test_main_screen_contains_no_manual_source_choice_or_fake_opportunity_flow(self) -> None:
        source = COCKPIT.read_text(encoding="utf-8").lower()
        self.assertNotIn("choose a data source", source)
        self.assertNotIn("upload csv", source)
        self.assertNotIn("demo candidate", source)
        self.assertIn("current provider evidence is unavailable", source)
        self.assertIn("no entry decision was made", source)


if __name__ == "__main__":
    unittest.main()
