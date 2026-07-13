from __future__ import annotations

import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from autopilot_state import (
    SCHEMA_VERSION,
    AutopilotStateStore,
    InvalidTickerError,
    SecretDataError,
    StatePathError,
    detect_state_changes,
)


class AutopilotStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.path = self.root / "personal_state.json"
        self.store = AutopilotStateStore(self.path, allowed_root=self.root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_default_schema_contains_all_personal_state_sections(self) -> None:
        state = self.store.load()
        self.assertEqual(state["schema_version"], SCHEMA_VERSION)
        for key in [
            "profile",
            "chart_preferences",
            "preferred_timeframes",
            "default_trade_horizon",
            "watchlist",
            "recent_searches",
            "saved_plans",
            "positions",
            "alerts",
            "journal",
            "calibration",
            "ui_preferences",
            "last_analyses",
            "monitoring",
        ]:
            self.assertIn(key, state)
        self.assertTrue(self.path.is_file())
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_watchlist_is_normalized_deduplicated_and_validated(self) -> None:
        self.store.add_watchlist(" aapl ")
        self.store.add_to_watchlist("AAPL")
        self.store.add_watchlist("BRK.B")
        self.assertEqual(self.store.load()["watchlist"], ["AAPL", "BRK.B"])
        self.assertEqual(self.store.remove_from_watchlist("aapl"), ["BRK.B"])
        with self.assertRaises(InvalidTickerError):
            self.store.add_watchlist("../../secret")

    def test_search_analysis_plan_and_trade_lifecycle(self) -> None:
        first = self.store.remember_search("aapl")
        self.store.remember_search("AAPL")
        self.assertEqual(first["ticker"], "AAPL")
        self.assertEqual(len(self.store.load()["recent_searches"]), 1)

        analysis = self.store.save_analysis("aapl", {"state": "ARMED", "confidence": 82})
        plan = self.store.save_plan(
            "AAPL",
            {"trigger": 205.0, "invalidation": 196.0, "targets": [214.0, 222.0]},
            plan_id="aapl-swing-1",
        )
        self.assertEqual(analysis["ticker"], "AAPL")
        self.assertEqual(plan["plan_id"], "aapl-swing-1")

        self.assertEqual(self.store.mark_watching("AAPL")["tracking_status"], "WATCHING")
        position = self.store.mark_entered("AAPL", entry_price=205.5, details={"setup": "break_retest"})
        self.assertEqual(position["status"], "OPEN")
        self.assertIn("AAPL", self.store.load()["positions"])
        closed = self.store.close_trade("AAPL", exit_price=216.0, outcome="target reached")
        state = self.store.load()
        self.assertEqual(closed["status"], "CLOSED")
        self.assertNotIn("AAPL", state["positions"])
        self.assertEqual(state["journal"][-1]["outcome"], "target reached")

        passed = self.store.mark_passed("MSFT", {"reason": "extended"})
        self.assertEqual(passed["tracking_status"], "PASSED")

    def test_secret_fields_and_values_are_rejected_before_write(self) -> None:
        original = self.store.load()
        with self.assertRaises(SecretDataError):
            self.store.save_analysis("SPY", {"api_key": "do-not-write-this"})
        with self.assertRaises(SecretDataError):
            self.store.save_analysis("SPY", {"accessToken": "do-not-write-this-either"})
        with self.assertRaises(SecretDataError):
            self.store.save_analysis("SPY", {"providerKey": "still-do-not-write-this"})
        with self.assertRaises(SecretDataError):
            self.store.save_plan("SPY", {"notes": "Bearer abcdefghijklmnopqrstuvwxyz"})
        self.assertEqual(self.store.load(), original)
        disk_text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("do-not-write-this", disk_text)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", disk_text)

    def test_alert_details_cannot_override_trusted_identity_or_enabled_state(self) -> None:
        record = self.store.set_alert_enabled(
            "AAPL",
            "entry",
            True,
            details={"ticker": "MSFT", "alert_type": "other", "enabled": False},
        )
        self.assertEqual(record["ticker"], "AAPL")
        self.assertEqual(record["alert_type"], "entry")
        self.assertTrue(record["enabled"])
        stored = self.store.load()["alerts"]["enabled"]["AAPL:entry"]
        self.assertEqual(stored, record)

    def test_corrupt_file_recovers_without_copying_or_exposing_contents(self) -> None:
        leaked_marker = "PRIVATE-MARKER-SHOULD-DISAPPEAR"
        self.path.write_text('{"profile": "broken", "notes": "' + leaked_marker, encoding="utf-8")
        recovered = self.store.load()
        self.assertEqual(recovered["schema_version"], SCHEMA_VERSION)
        self.assertEqual(self.store.last_recovery_reason, "invalid_json")
        self.assertNotIn(leaked_marker, json.dumps(recovered))
        self.assertNotIn(leaked_marker, self.path.read_text(encoding="utf-8"))
        self.assertEqual(list(self.root.glob("*.corrupt*")), [])

    def test_store_instances_share_a_lock_and_do_not_lose_updates(self) -> None:
        second_store = AutopilotStateStore(self.path, allowed_root=self.root)
        symbols = [f"S{index:02d}" for index in range(40)]

        def add(index: int) -> None:
            (self.store if index % 2 else second_store).add_watchlist(symbols[index])

        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(add, range(len(symbols))))
        state = self.store.load()
        self.assertEqual(set(state["watchlist"]), set(symbols))
        self.assertEqual(len(state["watchlist"]), len(symbols))
        self.assertGreaterEqual(state["revision"], len(symbols))
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_meaningful_state_target_and_invalidation_events(self) -> None:
        forming_to_armed = detect_state_changes("FORMING", "ARMED", ticker="spy")
        self.assertEqual(forming_to_armed[0]["transition"], "FORMING_TO_ARMED")

        events = detect_state_changes(
            {"ticker": "AAPL", "state": "ARMED", "price": 199, "target_1": 210, "invalidation": 190},
            {"ticker": "AAPL", "state": "ENTER", "price": 211, "target_1": 210, "invalidation": 190},
        )
        self.assertEqual([event["event_type"] for event in events], ["state_change", "target_reached"])

        invalidated = detect_state_changes(
            {"state": "ARMED", "price": 195, "invalidation": 190},
            {"state": "INVALIDATED", "price": 189, "invalidation": 190},
            ticker="AAPL",
        )
        self.assertEqual(
            [event["event_type"] for event in invalidated],
            ["state_change", "invalidation_reached"],
        )

    def test_monitoring_baseline_records_only_high_value_changes(self) -> None:
        self.assertEqual(
            self.store.record_monitoring_update("SPY", {"state": "FORMING", "price": 600, "target_1": 610}),
            [],
        )
        self.assertEqual(
            self.store.record_monitoring_update("SPY", {"state": "FORMING", "price": 601, "target_1": 610}),
            [],
        )
        events = self.store.record_monitoring_update(
            "SPY", {"state": "ARMED", "price": 602, "target_1": 610}
        )
        self.assertEqual(events[0]["transition"], "FORMING_TO_ARMED")
        self.assertEqual(len(self.store.load()["alerts"]["events"]), 1)

    def test_export_is_valid_json_and_path_is_conservative(self) -> None:
        self.store.add_watchlist("QQQ")
        exported = self.store.export_state()
        self.assertEqual(json.loads(exported)["watchlist"], ["QQQ"])
        destination = self.root / "exports" / "state-export.json"
        self.store.export_state(destination)
        self.assertEqual(json.loads(destination.read_text(encoding="utf-8"))["watchlist"], ["QQQ"])
        with self.assertRaises(StatePathError):
            AutopilotStateStore(self.root / ".." / "escape.json", allowed_root=self.root)
        with self.assertRaises(StatePathError):
            self.store.export_state(self.root.parent / "outside.json")

    def test_symbolic_link_state_path_is_rejected(self) -> None:
        target = self.root / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = self.root / "linked.json"
        link.symlink_to(target)
        with self.assertRaises(StatePathError):
            AutopilotStateStore(link, allowed_root=self.root)


if __name__ == "__main__":
    unittest.main()
