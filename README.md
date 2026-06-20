# Trading Autopilot Cloud Deploy Mirror

This repository is a public Streamlit deployment mirror for Trading Autopilot.

Trading Autopilot is decision-support only. It helps review US equities/options/share candidates, validate candidate CSV rows, and run read-only dashboard smoke checks.

## Safety Boundary

- No broker connection.
- No order placement, staging, previewing, modifying, or canceling.
- No TradingView alert automation.
- No payment, billing, or subscription workflow automation.
- No secrets in this repository.
- No real candidate data in this repository.

## Streamlit Deploy Settings

- Repo: `davidbenizri25-wq/trading-autopilot-cloud`
- Branch: `main`
- Main file path: `dashboard/app.py`

Optional Streamlit secrets, if used, must be configured only in Streamlit Cloud settings and never committed to this repository.
