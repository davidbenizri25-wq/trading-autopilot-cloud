"""Private-configurable bankroll and risk caps for Trading Autopilot.

Decision support only. These values constrain scanners and review lists; they
do not connect to brokers or place orders. Built-in values are deliberately
zero so a missing private config fails closed instead of publishing or
silently inventing personal financial limits.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "risk_config.json"


DEFAULT_RISK_CONFIG: dict[str, float] = {
    "options_bankroll": 0.0,
    "shares_bankroll": 0.0,
    "normal_option_premium_min": 0.0,
    "normal_option_premium_max": 0.0,
    "a_plus_option_premium_max": 0.0,
    "zero_dte_option_premium_max": 0.0,
    "total_open_options_exposure_max": 0.0,
    "normal_share_risk_max": 0.0,
    "a_plus_share_risk_max": 0.0,
    "daily_combined_hard_stop": 0.0,
    "weekly_combined_hard_stop": 0.0,
    "review_drawdown": 0.0,
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
