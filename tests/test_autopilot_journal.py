from __future__ import annotations

import copy
import json
import unittest

from autopilot_journal import (
    CALIBRATION_METHODOLOGY_VERSION,
    JOURNAL_METHODOLOGY_VERSION,
    aggregate_calibration,
    build_calibration_proposal,
    evaluate_journal_outcome,
)


def bar(day, open_price, high, low, close, complete=True):
    return {
        "timestamp": "2026-07-{0:02d}T20:00:00Z".format(day),
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "complete": complete,
    }


class AutopilotJournalTests(unittest.TestCase):
    def test_bullish_enter_target_first_excursions_and_horizon_returns(self):
        saved = {
            "ticker": "AAPL",
            "saved_at": "2026-07-01T20:00:00Z",
            "state": "ENTER",
            "direction": "bullish",
            "current_price": 100,
            "confidence": 82,
            "grade": "A",
            "plan": {
                "setup_type": "break-and-retest",
                "trigger": 100,
                "invalidation": 95,
                "target_1": 110,
                "target_2": 115,
            },
            "market_context": {"regime": "risk-on"},
            "timeframes": {
                "Weekly": {"direction": "bullish"},
                "Daily": {"direction": "bullish"},
                "4H": {"direction": "bullish"},
            },
        }
        bars = [
            bar(2, 100, 104, 98, 103),
            bar(3, 103, 111, 102, 110),
            bar(4, 110, 116, 108, 114),
        ]
        result = evaluate_journal_outcome(saved, bars, evaluation_cutoff="2026-07-04T21:00:00Z")
        self.assertEqual(result["methodology_version"], JOURNAL_METHODOLOGY_VERSION)
        self.assertEqual(result["outcome_status"], "target_first")
        self.assertEqual(result["first_terminal_event"]["target"], "target_1")
        self.assertAlmostEqual(result["mfe"]["r_multiple"], 3.2)
        self.assertAlmostEqual(result["mae"]["r_multiple"], -0.4)
        self.assertEqual(result["forward_returns"]["1d"]["directional_return_pct"], 3.0)
        self.assertEqual(result["forward_returns"]["3d"]["r_multiple"], 2.8)
        self.assertFalse(result["forward_returns"]["5d"]["available"])
        self.assertEqual(result["analysis_context"]["timeframe_alignment"], "aligned")
        json.dumps(result, allow_nan=False)

    def test_bearish_watch_uses_direction_aware_trigger_levels_returns_and_r(self):
        saved = {
            "ticker": "TSLA",
            "saved_at": "2026-07-01T20:00:00Z",
            "action": "WATCHING",
            "direction": "bearish",
            "confidence": 70,
            "plan": {
                "setup_type": "failed-rally",
                "trigger": 200,
                "invalidation": 210,
                "target_1": 180,
            },
        }
        bars = [
            bar(2, 205, 207, 201, 203),
            bar(3, 202, 204, 198, 199),
            bar(4, 198, 199, 188, 190),
            bar(5, 190, 192, 179, 181),
        ]
        result = evaluate_journal_outcome(saved, bars)
        self.assertEqual(result["outcome_status"], "target_first")
        self.assertEqual(result["activation_time"], "2026-07-03T20:00:00Z")
        self.assertEqual(result["reference_entry_price"], 200.0)
        self.assertEqual(result["mfe"]["r_multiple"], 2.1)
        self.assertEqual(result["mae"]["r_multiple"], -0.4)
        self.assertEqual(result["forward_returns"]["1d"]["raw_return_pct"], -0.5)
        self.assertEqual(result["forward_returns"]["1d"]["directional_return_pct"], 0.5)
        self.assertEqual(result["forward_returns"]["3d"]["r_multiple"], 1.9)

    def test_saved_cutoff_and_evaluation_cutoff_prevent_lookahead(self):
        saved = {
            "ticker": "SPY",
            "saved_at": "2026-07-03T20:00:00Z",
            "verdict": "ENTER",
            "direction": "bullish",
            "entry_price": 100,
            "plan": {"trigger": 100, "invalidation": 95, "target_1": 110},
        }
        bars = [
            bar(2, 100, 120, 99, 118),  # before the saved analysis: excluded
            bar(4, 100, 105, 98, 104),
            bar(5, 104, 112, 103, 111),  # after evaluation cutoff: excluded
        ]
        result = evaluate_journal_outcome(saved, bars, evaluation_cutoff="2026-07-04T23:00:00Z")
        self.assertEqual(result["outcome_status"], "triggered_active")
        self.assertEqual(result["evidence_scope"]["excluded_pre_anchor_count"], 1)
        self.assertEqual(result["evidence_scope"]["excluded_after_cutoff_count"], 1)
        self.assertEqual(result["evidence_scope"]["eligible_completed_bar_count"], 1)
        self.assertEqual(result["mfe"]["r_multiple"], 1.0)

    def test_invalidation_first_is_false_signal_and_stops_target_chronology(self):
        saved = {
            "ticker": "QQQ",
            "saved_at": "2026-07-01T20:00:00Z",
            "action": "ENTERED",
            "direction": "bullish",
            "entry_price": 100,
            "plan": {"trigger": 100, "invalidation": 95, "target_1": 110},
        }
        bars = [bar(2, 100, 103, 94, 96), bar(3, 96, 112, 96, 111)]
        result = evaluate_journal_outcome(saved, bars)
        self.assertEqual(result["outcome_status"], "invalidation_first")
        self.assertEqual(result["first_terminal_event"]["event"], "invalidation_reached")
        self.assertNotIn("target_reached", [item["event"] for item in result["events"]])
        self.assertIn("false_signal", [item["code"] for item in result["evidence_flags"]])

    def test_target_first_and_same_bar_collision_policy_are_distinct(self):
        base = {
            "ticker": "MSFT",
            "saved_at": "2026-07-01T20:00:00Z",
            "state": "ENTER",
            "direction": "bullish",
            "entry_price": 100,
            "plan": {"trigger": 100, "invalidation": 95, "target_1": 110},
        }
        target_first = evaluate_journal_outcome(
            base,
            [bar(2, 100, 111, 99, 109), bar(3, 109, 110, 94, 96)],
        )
        ambiguous = evaluate_journal_outcome(base, [bar(2, 100, 111, 94, 103)])
        self.assertEqual(target_first["outcome_status"], "target_first")
        self.assertEqual(ambiguous["outcome_status"], "ambiguous_same_bar")
        self.assertEqual(ambiguous["first_terminal_event"]["event"], "ambiguous_intrabar_order")

    def test_incomplete_and_malformed_bars_never_contribute(self):
        saved = {
            "ticker": "AMD",
            "saved_at": "2026-07-01T20:00:00Z",
            "state": "ENTER",
            "direction": "bullish",
            "entry_price": 100,
            "plan": {"invalidation": 95, "target_1": 110},
        }
        malformed = {"timestamp": "2026-07-03T20:00:00Z", "high": 101, "close": 100}
        result = evaluate_journal_outcome(
            saved,
            [bar(2, 100, 120, 90, 115, complete=False), malformed],
        )
        self.assertEqual(result["outcome_status"], "no_completed_bars")
        self.assertEqual(result["evidence_scope"]["excluded_incomplete_count"], 1)
        self.assertEqual(result["evidence_scope"]["excluded_invalid_count"], 1)
        self.assertIsNone(result["mfe"]["r_multiple"])

    def test_watch_trigger_bar_with_terminal_touch_is_ambiguous(self):
        saved = {
            "ticker": "META",
            "saved_at": "2026-07-01T20:00:00Z",
            "state": "ARMED",
            "direction": "bullish",
            "plan": {"trigger": 100, "invalidation": 95, "target_1": 110},
        }
        result = evaluate_journal_outcome(saved, [bar(2, 98, 101, 94, 99)])
        self.assertEqual(result["outcome_status"], "ambiguous_same_bar")
        self.assertTrue(result["triggered"])

    def test_calibration_groups_context_and_never_changes_thresholds(self):
        records = []
        contexts = [
            ("breakout", "risk-on", "A", 88, "bullish", "aligned", "target_first"),
            ("breakout", "risk-on", "A", 87, "bullish", "aligned", "invalidation_first"),
            ("pullback", "mixed", "B", 68, "bearish", "mixed", "not_triggered"),
        ]
        for index, (setup, regime, grade, confidence, direction, alignment, status) in enumerate(contexts):
            records.append(
                {
                    "methodology_version": JOURNAL_METHODOLOGY_VERSION,
                    "ticker": "T{0}".format(index),
                    "outcome_status": status,
                    "triggered": status != "not_triggered",
                    "mfe": {"r_multiple": index + 1},
                    "mae": {"r_multiple": -(index + 1) / 2},
                    "forward_returns": {
                        key: {
                            "available": key == "1d",
                            "directional_return_pct": index + 0.5 if key == "1d" else None,
                            "r_multiple": index / 2 if key == "1d" else None,
                        }
                        for key in ("1d", "3d", "5d", "10d", "20d")
                    },
                    "analysis_context": {
                        "setup_type": setup,
                        "market_regime": regime,
                        "grade": grade,
                        "confidence": confidence,
                        "confidence_band": "85_plus" if confidence >= 85 else "65_to_74",
                        "direction": direction,
                        "timeframe_alignment": alignment,
                        "direction_timeframe_alignment": direction + "/" + alignment,
                    },
                    "evidence_flags": (
                        [{"code": "false_signal"}] if status == "invalidation_first" else []
                    ),
                }
            )
        original = copy.deepcopy(records)
        summary = aggregate_calibration(records)
        self.assertEqual(summary["methodology_version"], CALIBRATION_METHODOLOGY_VERSION)
        self.assertFalse(summary["thresholds_changed"])
        self.assertFalse(summary["automatic_changes_allowed"])
        breakout = next(item for item in summary["groups"]["setup_type"] if item["value"] == "breakout")
        self.assertEqual(breakout["count"], 2)
        self.assertEqual(breakout["target_first_rate"], 0.5)
        self.assertEqual(breakout["evidence_flag_counts"]["false_signal"], 1)
        self.assertEqual(len(summary["groups"]["direction_timeframe_alignment"]), 2)
        self.assertEqual(records, original)
        json.dumps(summary, allow_nan=False)

    def test_proposal_is_draft_reversible_explainable_and_does_not_mutate_inputs(self):
        current = {"confirmation_bars": 1}
        proposed = {"confirmation_bars": 2}
        evidence = [{"group": "breakout", "invalidation_first_rate": 0.7}]
        originals = copy.deepcopy((current, proposed, evidence))
        record = build_calibration_proposal(
            parameter="entry_confirmation",
            current_value=current,
            proposed_value=proposed,
            rationale="Breakout false-signal evidence warrants human review.",
            evidence=evidence,
            created_at="2026-07-13T12:00:00Z",
        )
        self.assertEqual(record["status"], "draft")
        self.assertFalse(record["applied"])
        self.assertFalse(record["automatic_application"])
        self.assertTrue(record["reversible"])
        self.assertEqual(record["rollback"]["restore_value"], current)
        record["change"]["before"]["confirmation_bars"] = 999
        self.assertEqual((current, proposed, evidence), originals)
        json.dumps(record, allow_nan=False)


if __name__ == "__main__":
    unittest.main()
