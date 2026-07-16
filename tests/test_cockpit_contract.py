from __future__ import annotations

import json
import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
import tempfile
from contextlib import nullcontext
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dashboard.cockpit import (
    _active_timeframe,
    _apply_tracking_action,
    _chart_rows_for,
    _CHART_CACHE,
    _compact_market_value,
    _decision_rail_html,
    _initialize_state,
    _presentation_mode,
    _safe_analysis_record,
    _render_advanced,
    _set_active_timeframe,
    _sync_presentation_toggle,
    _ticker_from_search_choice,
    BREAKDOWN_SECTIONS,
    advanced_provider_diagnostics,
    build_decision_brief,
    build_home_snapshot,
    build_timeframe_alignment,
    currentness_label,
    earnings_context_label,
    entry_action_allowed,
    format_price,
    format_timestamp,
    options_empty_state_message,
    options_table_rows,
    public_text,
    release_build_label,
    revalidated_provider_health,
    resolve_state_path,
)


ROOT = Path(__file__).resolve().parents[1]
COCKPIT = ROOT / "dashboard" / "cockpit.py"
DECISION_NOW = datetime(2026, 7, 13, 15, 1, 0, tzinfo=timezone.utc)


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
    def test_advanced_provider_diagnostics_are_bounded_and_secrets_safe(self) -> None:
        secret = "super-secret-provider-key"
        diagnostics = advanced_provider_diagnostics(
            {
                "provider": "Polygon + Massive / Benzinga",
                "status": "connected",
                "data_label": "stale",
                "timestamp": "2026-07-14T14:00:00Z",
                "data_age_seconds": 320.5,
                "stale": True,
                "messages": [
                    f"HTTP 429 api_key={secret} /Users/person/private/report.json",
                    "Massive Benzinga earnings failed with HTTP 403",
                ],
                "cache_stats": {
                    "analysis": {"hits": 3, "misses": 2, "loads": 2, "load_errors": 0},
                    "chart": {"hits": 5, "misses": 4, "loads": 4, "coalesced_waits": 1},
                },
                "earnings_status": "unresolved",
                "earnings_error_kind": "entitlement",
                "earnings_status_code": 403,
                "earnings_attempts": 1,
                "earnings_latency_ms": 42.5,
                "earnings_throttled": False,
            },
            [
                {
                    "provider": "Polygon",
                    "operation": f"https://provider.example/{secret}/AAPL",
                    "outcome": "error",
                    "classification": "throttling",
                    "status_code": 429,
                    "attempts": 2,
                    "retries": 1,
                    "latency_ms": 125.5,
                    "throttled": True,
                    "observed_at": "2026-07-14T14:01:00Z",
                    "symbol": "AAPL",
                    "api_key": secret,
                }
            ],
        )
        serialized = json.dumps(diagnostics)
        for forbidden in [secret, "/Users/person", "provider.example", "AAPL", "api_key"]:
            self.assertNotIn(forbidden, serialized)
        self.assertEqual(diagnostics["message_categories"], {"entitlement": 1, "throttling": 1})
        self.assertEqual(diagnostics["requests"]["throttled_count"], 1)
        self.assertEqual(diagnostics["requests"]["maximum_latency_ms"], 125.5)
        self.assertLessEqual(len(diagnostics["requests"]["recent"]), 5)

    def test_presentation_advanced_view_never_reads_provider_diagnostics(self) -> None:
        class FakeStreamlit:
            def __init__(self) -> None:
                self.session_state = {}

            def expander(self, *_args, **_kwargs):
                return nullcontext()

            def markdown(self, *_args, **_kwargs):
                return None

            def write(self, *_args, **_kwargs):
                return None

            def caption(self, *_args, **_kwargs):
                return None

            def toggle(self, *_args, **_kwargs):
                return False

        brief = {
            "source": "Polygon",
            "ticker": "AAPL",
            "safe_decision": complete_decision(),
        }
        with patch(
            "dashboard.cockpit.recent_provider_observations",
            side_effect=AssertionError("presentation mode must not read diagnostics"),
        ):
            _render_advanced(
                FakeStreamlit(),
                {"provider_health": {}, "tradingview_symbol": "NASDAQ:AAPL"},
                {},
                brief,
                "15m",
                presentation=True,
            )

    def test_presentation_state_is_isolated_from_session_only_private_state(self) -> None:
        private_state = {"watchlist": ["AAPL"], "positions": {"AAPL": {"status": "open"}}}
        st = SimpleNamespace(
            session_state={
                "_autopilot_state_identity": "session-only",
                "_autopilot_personal_state": private_state,
            }
        )

        presentation_state, store = _initialize_state(st, {}, presentation=True)

        self.assertIsNone(store)
        self.assertEqual(presentation_state.get("watchlist"), [])
        self.assertEqual(presentation_state.get("positions"), {})
        self.assertIs(st.session_state["_autopilot_personal_state"], private_state)
        self.assertIsNot(presentation_state, private_state)

    def test_presentation_tradingview_widget_receives_no_private_watchlist(self) -> None:
        class FakeStreamlit:
            def __init__(self) -> None:
                self.session_state = {}

            def expander(self, *_args, **_kwargs):
                return nullcontext()

            def markdown(self, *_args, **_kwargs):
                return None

            def write(self, *_args, **_kwargs):
                return None

            def caption(self, *_args, **_kwargs):
                return None

            def toggle(self, *_args, **_kwargs):
                return True

            def iframe(self, *_args, **_kwargs):
                return None

        brief = {
            "source": "Polygon",
            "ticker": "AAPL",
            "safe_decision": complete_decision(),
        }
        with (
            patch("dashboard.cockpit.tradingview_widget_html", return_value="<html></html>") as widget,
            patch("dashboard.cockpit.tradingview_market_context_html", return_value="<html></html>"),
        ):
            _render_advanced(
                FakeStreamlit(),
                {"provider_health": {}, "tradingview_symbol": "NASDAQ:AAPL"},
                {"watchlist": ["PRIVATE"]},
                brief,
                "15m",
                presentation=True,
            )

        self.assertEqual(widget.call_args.kwargs["watchlist"], [])

    def test_presentation_toggle_synchronizes_session_and_query(self) -> None:
        st = SimpleNamespace(
            session_state={"_autopilot_presentation_toggle": True},
            query_params={},
        )
        _sync_presentation_toggle(st)
        self.assertTrue(st.session_state["_autopilot_presentation"])
        self.assertEqual(st.query_params["view"], "presentation")

        st.session_state["_autopilot_presentation_toggle"] = False
        _sync_presentation_toggle(st)
        self.assertFalse(st.session_state["_autopilot_presentation"])
        self.assertNotIn("view", st.query_params)

    def test_active_presentation_repairs_a_missing_shareable_query_marker(self) -> None:
        st = SimpleNamespace(
            session_state={"_autopilot_presentation": True},
            query_params={},
        )

        self.assertTrue(_presentation_mode(st))
        self.assertEqual(st.query_params["view"], "presentation")

    def test_empty_options_copy_distinguishes_not_run_from_no_observations(self) -> None:
        blocked = complete_decision(
            verdict="PASS",
            state="BLOCKED",
            options={},
        )
        blocked_copy = options_empty_state_message(blocked)
        self.assertIn("screening was skipped", blocked_copy)
        self.assertIn("ENTER or ARMED", blocked_copy)
        self.assertNotIn("cleared", blocked_copy)
        self.assertNotIn("liquidity", blocked_copy)

        entered = complete_decision(options={"status": "pass", "ranked_contracts": []})
        entered_copy = options_empty_state_message(entered)
        self.assertIn("no usable contract observations", entered_copy.lower())
        self.assertNotIn("cleared", entered_copy)

    def test_empty_dashboard_chart_result_is_not_cached(self) -> None:
        payload = {"resolved": {"ticker": "AAPL"}, "chart_frames": {}}
        recovered = [{"timestamp": "2026-07-16T14:00:00Z", "close": 210.0}]
        _CHART_CACHE.clear()
        with patch("dashboard.cockpit.load_chart_bars", side_effect=[[], recovered]) as loader:
            first = _chart_rows_for(payload, "1m", "test-key")
            second = _chart_rows_for(payload, "1m", "test-key")

        self.assertEqual(first, [])
        self.assertEqual(second, recovered)
        self.assertEqual(loader.call_count, 2)

    def test_stale_price_and_levels_are_historically_labeled(self) -> None:
        brief = build_decision_brief(
            complete_decision(data_label="stale"),
            now=DECISION_NOW,
        )
        self.assertEqual(brief["price_label"], "Last observed price")
        rail = _decision_rail_html(brief)
        self.assertIn("Historical levels · not actionable", rail)

    def test_market_ribbon_compacts_provider_unavailable_messages(self) -> None:
        self.assertEqual(
            _compact_market_value("Unavailable from the configured stock feed"),
            "Unavailable",
        )
        self.assertEqual(_compact_market_value("mixed"), "Mixed")

    def test_fail_closed_brief_never_invents_an_unavailable_invalidation(self) -> None:
        brief = build_decision_brief(
            complete_decision(
                data_label="unavailable",
                data_source="Unavailable",
                current_price=None,
                invalidation_condition="A decisive close above unavailable.",
            ),
            now=DECISION_NOW,
        )

        self.assertEqual(brief["verdict"], "PASS")
        self.assertEqual(
            brief["invalidate"],
            "No current invalidation level is available; wait for complete provider-backed evidence.",
        )

    def test_earnings_status_names_entitlement_availability_and_implementation(self) -> None:
        expected = {
            "entitlement": "Unresolved · Vendor entitlement required",
            "availability": "Unresolved · Provider temporarily unavailable",
            "implementation": "Unresolved · Application validation issue",
        }
        for error_kind, label in expected.items():
            with self.subTest(error_kind=error_kind):
                self.assertEqual(
                    earnings_context_label(
                        {
                            "earnings_status": "unresolved",
                            "earnings_error_kind": error_kind,
                        }
                    ),
                    label,
                )

    def test_safe_private_record_preserves_structured_earnings_status(self) -> None:
        record = _safe_analysis_record(
            {
                "decision": complete_decision(
                    earnings_status="unresolved",
                    earnings_date=None,
                    earnings_date_status=None,
                    earnings_checked_through="2026-07-23",
                    earnings_error_kind="entitlement",
                ),
                "provider_health": {},
            }
        )
        self.assertEqual(record["earnings_status"], "unresolved")
        self.assertEqual(record["earnings_checked_through"], "2026-07-23")
        self.assertEqual(record["earnings_error_kind"], "entitlement")

    def test_release_build_label_uses_only_valid_public_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            deploy = root / "deploy"
            deploy.mkdir()
            managed = root / "dashboard" / "app.py"
            managed.parent.mkdir()
            managed.write_text("verified release content\n", encoding="utf-8")
            file_digest = hashlib.sha256(managed.read_bytes()).hexdigest()
            manifest = hashlib.sha256(
                json.dumps(
                    [{"path": "dashboard/app.py", "sha256": file_digest}],
                    ensure_ascii=True,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            state_path = deploy / ".cloud-mirror-state.json"
            state_path.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "canonical_repository": "davidbenizri25-wq/trading-elite-system",
                        "version": "2.1.0-premium-terminal",
                        "canonical_commit": "a" * 40,
                        "managed_paths": ["dashboard/app.py"],
                        "manifest_sha256": manifest,
                        "files": {"dashboard/app.py": file_digest},
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(
                release_build_label(state_path),
                f"v2.1.0 · source aaaaaaa · manifest {manifest[:7]}",
            )
            mismatched_state = json.loads(state_path.read_text(encoding="utf-8"))
            mismatched_state["managed_paths"].append("polygon_provider.py")
            state_path.write_text(json.dumps(mismatched_state), encoding="utf-8")
            self.assertEqual(release_build_label(state_path), "v2.1.0")
            mismatched_state["managed_paths"] = ["dashboard/app.py"]
            state_path.write_text(json.dumps(mismatched_state), encoding="utf-8")
            managed.write_text("tampered release content\n", encoding="utf-8")
            self.assertEqual(release_build_label(state_path), "v2.1.0")
            managed.write_text("verified release content\n", encoding="utf-8")
            state_path.write_text('{"canonical_commit":"/private/tmp/leak"}', encoding="utf-8")
            self.assertEqual(release_build_label(state_path), "v2.1.0")
            state_path.write_text("[]", encoding="utf-8")
            self.assertEqual(release_build_label(state_path), "v2.1.0")

            real_state = root / "real-state.json"
            real_state.write_text(
                json.dumps(
                    {
                        "schema_version": 3,
                        "canonical_repository": "davidbenizri25-wq/trading-elite-system",
                        "version": "2.1.0-premium-terminal",
                        "canonical_commit": "a" * 40,
                        "managed_paths": ["dashboard/app.py"],
                        "manifest_sha256": manifest,
                        "files": {"dashboard/app.py": file_digest},
                    }
                ),
                encoding="utf-8",
            )
            state_path.unlink()
            state_path.symlink_to(real_state)
            self.assertEqual(release_build_label(state_path), "v2.1.0")

    def test_breakdown_contract_contains_exactly_all_twenty_one_sections(self) -> None:
        self.assertEqual(len(BREAKDOWN_SECTIONS), 21)
        self.assertEqual(BREAKDOWN_SECTIONS[0], ("summary", "1. One-paragraph summary"))
        self.assertEqual(
            BREAKDOWN_SECTIONS[-1],
            ("final_verdict", "21. Final verdict in plain English"),
        )
        self.assertEqual(len({key for key, _ in BREAKDOWN_SECTIONS}), 21)

    def test_top_card_brief_contains_every_decision_field(self) -> None:
        brief = build_decision_brief(complete_decision(), now=DECISION_NOW)
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
                brief = build_decision_brief(
                    complete_decision(data_label=data_label),
                    now=DECISION_NOW,
                )
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

    def test_entry_recording_requires_a_current_explicit_enter_decision(self) -> None:
        self.assertTrue(
            entry_action_allowed(build_decision_brief(complete_decision(), now=DECISION_NOW))
        )
        self.assertFalse(
            entry_action_allowed(
                build_decision_brief(
                    complete_decision(data_label="stale"),
                    now=DECISION_NOW,
                )
            )
        )
        self.assertFalse(
            entry_action_allowed(
                build_decision_brief(
                    complete_decision(
                        verdict="WAIT FOR CONFIRMATION",
                        state="ARMED",
                        entry_conditions_satisfied=False,
                    ),
                    now=DECISION_NOW,
                )
            )
        )
        self.assertFalse(
            entry_action_allowed(
                build_decision_brief(
                    complete_decision(current_price=None),
                    now=DECISION_NOW,
                )
            )
        )

    def test_cached_enter_is_re_aged_at_render_boundary(self) -> None:
        brief = build_decision_brief(
            complete_decision(),
            now=datetime(2026, 7, 13, 15, 4, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(brief["verdict"], "PASS")
        self.assertEqual(brief["state"], "BLOCKED")
        self.assertEqual(brief["currentness"], "Stale — decision gated")
        self.assertFalse(entry_action_allowed(brief))

    def test_off_session_reaging_synchronizes_copy_and_provider_health(self) -> None:
        after_close = datetime(2026, 7, 13, 20, 1, 0, tzinfo=timezone.utc)
        decision = complete_decision(
            data_timestamp="2026-07-13T20:00:00Z",
            full_breakdown={
                **complete_decision()["full_breakdown"],
                "final_verdict": "ENTER NOW",
                "bull_case": "Buy the trigger.",
            },
        )

        brief = build_decision_brief(decision, now=after_close)
        health = revalidated_provider_health(
            {
                "provider": "Polygon",
                "status": "connected",
                "data_label": "real-time",
                "timestamp": "2026-07-13T20:00:00Z",
            },
            brief["safe_decision"],
            now=after_close,
        )

        self.assertEqual(brief["verdict"], "PASS")
        self.assertEqual(brief["market_status"], "Closed")
        self.assertEqual(brief["currentness"], "Last close")
        self.assertNotIn("ENTER NOW", brief["safe_decision"]["full_breakdown"]["final_verdict"])
        self.assertNotIn("Buy the trigger", brief["safe_decision"]["full_breakdown"]["bull_case"])
        self.assertEqual(health["data_label"], "last-close")
        self.assertEqual(health["data_age_seconds"], 60.0)
        self.assertTrue(health["stale"])

    def test_tracking_boundary_rejects_stale_non_enter_entry(self) -> None:
        st = SimpleNamespace(session_state={})
        mode = _apply_tracking_action(
            st,
            {"decision": complete_decision(data_label="stale")},
            "entered",
        )
        self.assertEqual(mode, "blocked")
        self.assertNotIn("_autopilot_personal_state", st.session_state)

    def test_public_text_never_echoes_paths_traces_or_credentials(self) -> None:
        unsafe = [
            "/Users/person/private/state.json",
            "/var/app/data.json",
            "file:///Users/person/private/state.json",
            "path=/Users/person/private/state.json",
            "debug:/private/tmp/state.json",
            "RuntimeError: provider exploded",
            "api_key=do-not-show-this",
            "-----BEGIN " + "PRIVATE KEY-----",
        ]
        for value in unsafe:
            with self.subTest(value=value):
                self.assertEqual(public_text(value), "Unavailable")
        self.assertEqual(public_text("15m confirmation is not yet satisfied."), "15m confirmation is not yet satisfied.")

    def test_safe_business_warnings_remain_visible_and_do_not_claim_a_gate(self) -> None:
        decision = complete_decision(
            warnings=["Earnings are 7 day(s) away."],
            primary_risk="Earnings are 7 day(s) away.",
        )
        brief = build_decision_brief(decision, now=DECISION_NOW)

        self.assertEqual(brief["verdict"], "ENTER")
        self.assertEqual(brief["primary_risk"], "Earnings are 7 day(s) away.")
        self.assertNotIn("gated conservatively", brief["primary_risk"].lower())

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
        model = build_home_snapshot(state, now=DECISION_NOW)
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

    def test_one_minute_and_one_month_stay_distinct_in_alignment_and_query_state(self) -> None:
        decision = complete_decision(
            timeframes={
                "1M": {
                    "direction": "bullish",
                    "trend_score": 3,
                    "macd_histogram": 0.4,
                    "close": 210.25,
                    "support": [200.0],
                    "resistance": [220.0],
                }
            }
        )
        monthly = build_timeframe_alignment(decision, selected_timeframe="1M")
        minute = build_timeframe_alignment(
            decision,
            selected_timeframe="1m",
            selected_analysis={
                "direction": "bearish",
                "trend_score": -3,
                "macd_histogram": -0.4,
                "close": 209.0,
                "support": [205.0],
                "resistance": [210.0],
            },
        )
        self.assertEqual(next(row for row in monthly if row["timeframe"] == "1M")["active"], "yes")
        self.assertEqual(next(row for row in minute if row["timeframe"] == "1M")["active"], "no")
        self.assertEqual(next(row for row in minute if row["timeframe"] == "1m")["active"], "yes")
        self.assertEqual(next(row for row in minute if row["timeframe"] == "1m")["direction"], "Bearish")
        self.assertEqual(next(row for row in minute if row["timeframe"] == "3m")["direction"], "Unavailable")

        decision_with_four_hour = complete_decision(
            timeframes={
                "4H": {
                    "direction": "bullish",
                    "trend_score": 3,
                    "macd_histogram": 0.3,
                    "close": 210.0,
                    "support": [205.0],
                    "resistance": [220.0],
                }
            }
        )
        four_hour = build_timeframe_alignment(
            decision_with_four_hour,
            selected_timeframe="4H",
            selected_analysis={
                "direction": "unavailable",
                "trend_score": None,
                "macd_histogram": None,
                "support": [],
                "resistance": [],
            },
        )
        self.assertEqual(
            next(row for row in four_hour if row["timeframe"] == "4H")["direction"],
            "Bullish",
        )

        for raw, expected in (("1m", "1m"), ("1M", "1M"), (["15m", "1M"], "1M")):
            with self.subTest(raw=raw):
                st = SimpleNamespace(session_state={}, query_params={"tf": raw})
                self.assertEqual(_active_timeframe(st), expected)
                self.assertEqual(st.session_state["_autopilot_timeframe"], expected)

        st = SimpleNamespace(session_state={}, query_params={})
        self.assertEqual(_set_active_timeframe(st, "1m"), "1m")
        self.assertEqual(st.query_params["tf"], "1m")
        self.assertEqual(_set_active_timeframe(st, "1M"), "1M")
        self.assertEqual(st.query_params["tf"], "1M")

        missing_query_api = SimpleNamespace(session_state={})
        self.assertEqual(_active_timeframe(missing_query_api), "15m")

    def test_autocomplete_choice_extracts_ticker_without_restricting_new_symbols(self) -> None:
        self.assertEqual(
            _ticker_from_search_choice("AAPL · Apple Inc. · NASDAQ"),
            "AAPL",
        )
        self.assertEqual(_ticker_from_search_choice("RIVN"), "RIVN")

    def test_streamlit_contract_is_form_first_mobile_safe_and_redesigned(self) -> None:
        source = COCKPIT.read_text(encoding="utf-8")
        for required in [
            "def render_cockpit(",
            'st.form("autopilot_ticker_search"',
            "form_submit_button",
            'width="stretch"',
            "st.segmented_control(",
            "options=list(TIMEFRAME_LABELS)",
            'selection_mode="single"',
            "st.selectbox(",
            "accept_new_options=True",
            "_ticker_from_search_choice(query)",
            '"Presentation Mode"',
            "build_presentation_payload(",
            "presentation_pdf_bytes(",
            "st.download_button(",
            '"Download decision brief · PDF"',
            '"toImageButtonOptions"',
            "tradingview_chart_url(symbol, label)",
            "Open {brief['ticker']} in TradingView · {label}",
            '"I entered"',
            '"I’m watching"',
            '"I passed"',
            '"Close trade"',
            'st.expander("Advanced"',
            "Market-context 2 × 2 preset",
            "AutopilotStateStore",
            '"AUTOPILOT_STATE_PATH"',
        ]:
            with self.subTest(required=required):
                self.assertIn(required, source)
        for forbidden in [
            "use_container_width",
            "st.tabs(",
            "st.file_uploader(",
            "st.exception(",
            "streamlit.components.v1",
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
