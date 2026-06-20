"""Shared bankroll and risk caps for Trading Autopilot.

Decision support only. These values constrain scanners and review lists; they
do not connect to brokers or place orders.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "risk_config.json"


DEFAULT_RISK_CONFIG: dict[str, float] = {
    "options_bankroll": 1000,
    "shares_bankroll": 2500,
    "normal_option_premium_min": 50,
    "normal_option_premium_max": 75,
    "a_plus_option_premium_max": 100,
    "zero_dte_option_premium_max": 40,
    "total_open_options_exposure_max": 150,
    "normal_share_risk_max": 50,
    "a_plus_share_risk_max": 75,
    "daily_combined_hard_stop": 125,
    "weekly_combined_hard_stop": 350,
    "review_drawdown": 500,
}


def load_risk_config(path: Path | str = CONFIG_PATH) -> dict[str, float]:
    config = DEFAULT_RISK_CONFIG.copy()
    try:
        with Path(path).open() as handle:
            loaded: dict[str, Any] = json.load(handle)
    except FileNotFoundError:
        return config
    for key, value in loaded.items():
        try:
            config[key] = float(value)
        except (TypeError, ValueError):
            pass
    return config


RISK_CONFIG = load_risk_config()

