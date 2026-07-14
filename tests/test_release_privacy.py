from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from dashboard.app import ROOT, _market_data_config_from_streamlit
from risk_config import DEFAULT_RISK_CONFIG, load_risk_config


class _FakeStreamlit:
    def __init__(self, secrets: dict[str, str] | None = None) -> None:
        self.secrets = secrets or {}


class ReleasePrivacyTests(unittest.TestCase):
    def test_access_code_alone_never_enables_shared_state(self) -> None:
        environment = {
            "APP_ACCESS_CODE": "configured",
            "AUTOPILOT_STATE_PATH": "/tmp/shared-state.json",
            "AUTOPILOT_STATE_ALLOWED_ROOT": "/tmp",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = _market_data_config_from_streamlit(_FakeStreamlit())

        self.assertNotIn("AUTOPILOT_STATE_PATH", config)
        self.assertNotIn("AUTOPILOT_STATE_ALLOWED_ROOT", config)

    def test_private_state_requires_both_opt_in_and_access_control(self) -> None:
        environment = {
            "AUTOPILOT_PRIVATE_STATE_ENABLED": "true",
            "AUTOPILOT_STATE_PATH": "/tmp/shared-state.json",
            "AUTOPILOT_STATE_ALLOWED_ROOT": "/tmp",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = _market_data_config_from_streamlit(_FakeStreamlit())

        self.assertNotIn("AUTOPILOT_STATE_PATH", config)

        environment["APP_ACCESS_CODE"] = "configured"
        with patch.dict(os.environ, environment, clear=True):
            config = _market_data_config_from_streamlit(_FakeStreamlit())

        self.assertEqual(config["AUTOPILOT_STATE_PATH"], "/tmp/shared-state.json")
        self.assertEqual(config["AUTOPILOT_STATE_ALLOWED_ROOT"], "/tmp")
        self.assertEqual(config["AUTOPILOT_PRIVATE_STATE_ENABLED"], "true")

    def test_explicit_private_opt_in_uses_safe_default_state_location(self) -> None:
        environment = {
            "APP_ACCESS_CODE": "configured",
            "AUTOPILOT_PRIVATE_STATE_ENABLED": "yes",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = _market_data_config_from_streamlit(_FakeStreamlit())

        self.assertEqual(
            config["AUTOPILOT_STATE_PATH"],
            str(ROOT / "data" / ".autopilot_state.json"),
        )
        self.assertEqual(config["AUTOPILOT_STATE_ALLOWED_ROOT"], str(ROOT / "data"))
        self.assertEqual(config["AUTOPILOT_PRIVATE_STATE_ENABLED"], "true")

    def test_missing_private_risk_config_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            config = load_risk_config(Path(temporary_directory) / "missing.json")

        self.assertEqual(config, DEFAULT_RISK_CONFIG)
        self.assertTrue(config)
        self.assertTrue(all(value == 0.0 for value in config.values()))

    def test_private_risk_config_can_still_override_fail_closed_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "risk.json"
            path.write_text('{"shares_bankroll": 123.0}', encoding="utf-8")
            config = load_risk_config(path)

        self.assertEqual(config["shares_bankroll"], 123.0)
        self.assertEqual(config["options_bankroll"], 0.0)

    def test_public_manifest_excludes_private_risk_values(self) -> None:
        manifest = (ROOT / "deploy" / "cloud_manifest.txt").read_text(encoding="utf-8")
        managed = {
            line.strip()
            for line in manifest.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertNotIn("config/risk_config.json", managed)
        self.assertIn("risk_config.py", managed)
        self.assertIn("tests/test_release_privacy.py", managed)


if __name__ == "__main__":
    unittest.main()
