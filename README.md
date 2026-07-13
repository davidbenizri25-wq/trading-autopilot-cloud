# Trading Autopilot

Trading Autopilot is David's read-only trading decision cockpit. One ticker search resolves the exact US security and exchange, retrieves authorized Polygon market data, evaluates SPY/QQQ and sector context, runs the multi-timeframe evidence engine, draws the trade plan, and returns one calm outcome: **ENTER**, **WAIT FOR CONFIRMATION**, or **PASS**.

It never connects to a broker, places an order, or spends money.

## Daily workflow

1. Open the app.
2. Enter a ticker or an exchange-qualified ticker such as `NASDAQ:AAPL`.
3. Review the decision card and one-sentence instruction.
4. Inspect the synchronized chart, trigger, entry zone, invalidation, targets, and evidence.
5. Mark the setup as entered, watching, passed, or closed when useful.

No CSV, manual indicator entry, manual level entry, source selector, or sample workflow is required on the main screen.

## What the cockpit evaluates

- Monthly, Weekly, Daily, 4H, 1H, 15m, and 5m evidence, with Weekly/Daily/4H/15m weighted most heavily.
- The saved chart baseline: 9 EMA, **21 WMA**, 50 WMA, 200 WMA, 200 SMA, MACD 12/26/9, and intraday VWAP.
- SPY, QQQ, the relevant sector ETF, relative strength, liquidity, data freshness, earnings availability, news, structure, momentum, volume, extension, and reward-to-risk.
- A six-state explainable setup model: BLOCKED, FORMING, ARMED, ENTER, EXTENDED, and INVALIDATED.
- Real Polygon option snapshots only after the underlying setup clears its own gates. Missing or stale Greeks, quotes, liquidity, chain completeness, or earnings context cannot be promoted into a recommendation.

Every visible conclusion is derived from provider observations or explicitly labelled as calculated. Missing inputs stay unavailable.

## TradingView

The app provides:

- An app-native annotated candlestick and MACD chart using the saved methodology baseline.
- Exact symbol, exchange, and 15-minute handoff to the signed-in TradingView browser session.
- An official embedded TradingView view in Advanced.
- The indicator-only `tradingview/trading_autopilot_sol_ultra.pine` helper for consistent indicators and transferred plan levels.

Private TradingView layouts and drawings are not exposed through the public widget API and are never claimed as synchronized. The original TradingView layout must not be modified; any account-side rollout belongs in a duplicate layout named `Trading Autopilot | David`.

## Personal state and monitoring

The cockpit records recent searches, watchlist choices, saved analyses and plans, tracked positions, in-app state changes, alerts, journal actions, calibration data, and UI preferences.

- With `APP_ACCESS_CODE` configured, the app uses a private server-side state file by default.
- `AUTOPILOT_STATE_PATH` and `AUTOPILOT_STATE_ALLOWED_ROOT` can override that location.
- Without private access configuration, state stays session-only so a public visitor cannot inherit or overwrite personal data.
- Provider keys and other credentials are rejected from saved state.

Monitoring runs when the app refreshes a tracked symbol. Only meaningful transitions such as FORMING → ARMED, ARMED → ENTER, target reached, or invalidation reached create events; ordinary price noise does not.

## Data configuration

Production secrets belong in Streamlit's secret manager, never in Git:

```toml
MARKET_DATA_PROVIDER = "polygon"
POLYGON_API_KEY = "..."
APP_ACCESS_CODE = "..." # recommended for durable personal state
```

The app distinguishes Real-time, Delayed, Last close, Stale, and Unavailable data with exact timestamps. Provider errors are sanitized before display. Stale, future-dated, incomplete, or currently forming bars cannot create an ENTER result.

## Run locally

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run dashboard/app.py
```

Run the complete verification suite:

```bash
.venv/bin/python -m pytest -q
```

## Repository ownership and cloud release

`davidbenizri25-wq/trading-elite-system` is the canonical private source of truth. `davidbenizri25-wq/trading-autopilot-cloud` is a generated public deployment mirror containing only the explicit allowlist in `deploy/cloud_manifest.txt`.

Check or update the public mirror from the canonical repository:

```bash
python3 tools/sync_cloud_mirror.py --target /absolute/path/to/trading-autopilot-cloud --mode check
python3 tools/sync_cloud_mirror.py --target /absolute/path/to/trading-autopilot-cloud --mode apply
```

The sync tool refuses unsafe targets, never copies Git metadata or secrets, and only removes files previously recorded as managed. See `docs/SOURCE_OF_TRUTH.md` for the branch, review, deployment, and rollback procedure.

## Safety boundary

- Decision support only; no orders or broker connection.
- No credentials in source, logs, browser output, exported state, or error text.
- No invented prices, candles, levels, options data, news, or private TradingView synchronization.
- No ENTER from incomplete required timeframes, stale evidence, an unfinished trigger candle, or conflicting higher-timeframe structure.
- A PASS is a successful outcome when the evidence is weak.

<details>
<summary>Historical v1.x compatibility documentation</summary>

The material below is retained only as an auditable record for the frozen v1.x regression suite. It does not describe the default 2.0 cockpit workflow.

# Trading Autopilot v1.5.4-mobile-calibration-warning-polish-dev

Trading Autopilot is a decision-support dashboard for scanner ranking, watchlist review, helper levels, review planning, and journaling.

## Sample Data Warning

`data/sample_candidates.csv` is fake/example data for testing only. It is not live market data.

## Common Commands

```bash
python3 -m unittest discover -s tests
python3 scoring.py data/sample_candidates.csv
python3 options_filter.py data/sample_candidates.csv
python3 shares_filter.py data/sample_candidates.csv
python3 covered_call_filter.py data/sample_candidates.csv
python3 dashboard/app.py
python3 dashboard/app.py data/real_candidates_template.csv
python3 tools/validate_candidates.py data/real_candidates_template.csv
python3 tools/generate_pine_levels.py
```

## Review Surfaces

- `Home` tab: product UI for non-coders with Start with Live Market Data, status cards, Next Best Action, review cards, and beginner safety guidance.
- `Market Breakdown` tab: enter a watchlist and get plain-English breakdown cards with bias, confidence, trend, momentum, levels, risk flags, and next action.
- `Chart Workspace` tab: capture manual TradingView chart review notes, multi-timeframe levels, fundamentals, and macro context.
- `Alert Planner` tab: draft manual TradingView alert plans and message text without creating live alerts.
- `Send to Review Engine`: session-only handoff from Home, Market Breakdown, Chart Workspace, or Live Data into validation and Daily Review.
- `Review Engine Paste Box`: beginner-friendly name for the technical TradingView Import path.
- Beginner mode is the default and shows only the core daily-use tabs.
- Advanced mode preserves every technical review tab.
- `Help / Safety` tab explains what to click, when to stop, and what the app never does.
- `scoring.py`: bullish, bearish, neutral, and context bias scoring.
- `options_filter.py`: manual call/put review candidates.
- `shares_filter.py`: under-$25 bullish long-share candidates.
- `covered_call_filter.py`: covered-call review.
- `dashboard/app.py`: Streamlit or CLI dashboard tabs.
- `tradingview/autopilot_helper_v0_1.pine`: TradingView indicator helper.
- `tradingview/trading_autopilot_chart_helper_v140.pine`: optional read-only chart review helper source, indicator-only and manual-use only.
- `tradingview/generated_level_maps.pine`: generated level map output.
- `docs/tradingview_layout_rollout_report.md`: TradingView layout rollout inventory and safety notes.
- `playbook/`: trading playbook, setup definitions, risk rules, avoid rules, workflow, and TradingView style audit.

## Cloud / Phone Access

- Streamlit app path: `dashboard/app.py`.
- Streamlit dependency file: `requirements.txt`.
- Codespaces environment: `.devcontainer/devcontainer.json`.
- Phone/cloud setup guide: `docs/cloud_mobile_setup.md`.
- Optional Streamlit secret or environment variable: `APP_ACCESS_CODE`.
- `real_candidates_WORKING.csv` remains local-only and ignored.
- TradingView helper install remains paused/manual-only.

## Cloud / Saved URL Access

- Streamlit app path: `dashboard/app.py`.
- Current deployment guide: `docs/cloud_saved_url_access.md`.
- Existing cloud/mobile guide: `docs/cloud_mobile_setup.md`.
- Optional Streamlit secret: `APP_ACCESS_CODE`.
- Do not commit `.streamlit/secrets.toml`.
- Do not commit real calibration CSVs.
- Cloud dashboard remains decision-support only.
- Manual TradingView confirmation is required.
- Manual chart confirmation required.
- No broker/order/alert/payment automation.

## Cloud Smoke Test / Mobile Daily Use

- Cloud smoke test: `docs/cloud_smoke_test.md`.
- Deploy troubleshooting: `docs/streamlit_deploy_troubleshooting.md`.
- Post-deploy operator checklist: `docs/post_deploy_operator_checklist.md`.
- Product UI guide: `docs/product_ui.md`.
- Wow UI product polish: `docs/wow_ui_product_polish.md`.
- Mobile first-run one-click review: `docs/mobile_first_run_one_click_review.md`.
- Mobile/sidebar post-handoff polish: `docs/mobile_sidebar_post_handoff_polish.md`.
- Mobile Calibration Results warning polish: `docs/mobile_calibration_warning_polish.md`.
- Market Breakdown mobile polish: `docs/market_breakdown_mobile_polish.md`.
- TradingView Chart Workspace guide: `docs/tradingview_chart_workspace.md`.
- Alert Planner guide: `docs/alert_planner.md`.
- TradingView alert safety: `docs/tradingview_alert_safety.md`.
- Decision-support workflow: `docs/decision_support_workflow.md`.
- Chart Review CSV Bridge guide: `docs/chart_review_csv_bridge.md`.
- Fundamentals and macro context guide: `docs/fundamentals_macro_context.md`.
- Non-coder quickstart: `docs/non_coder_quickstart.md`.
- Live Market Breakdown guide: `docs/live_market_breakdown.md`.
- Non-coder market review: `docs/non_coder_market_review.md`.
- Use Home first.
- Home includes a product-style Start Review flow.
- Start with Live Market Data exists on Home.
- Home includes watchlist input, timeframe selector, Analyze Live Market Data, and the compatibility phrase Analyze with Polygon.
- Home shows `Live data connected: Polygon` or `Live data not connected`.
- Live data is not connected means you can still use Sample data or manual import.
- Home-generated rows can use `Send to Review Engine` to unlock Daily Review / Calibration Results without manual copy/paste.
- Beginner translation explains TradingView Import = Review Engine Paste Box.
- Review Engine Status shows whether session rows are loaded, source, row count, blocking issues, warnings, and next step.
- Phone tip explains how to collapse the sidebar when it covers the page.
- Rows sent to Review Engine show a clear success state and point to Daily Review / Calibration Results.
- Calibration Results mobile cards summarize ticker, bias, score, grade, state, levels, and next action without horizontal scrolling.
- warnings versus blocking issues are explained in plain English so users know warnings are review notes, not stop signs.
- The Advanced editable grid remains available for full calibration editing.
- Home tab exists.
- Market Breakdown tab exists.
- Chart Workspace tab exists.
- Alert Planner tab exists.
- TradingView alert message drafts exist.
- Alert Planner is draft-only and says no alerts are created.
- Alert Plan CSV template exists.
- Beginner mode is default.
- Advanced mode keeps all tabs.
- Help / Safety tab exists.
- Watchlist input exists.
- Plain-English breakdown cards exist.
- Manual chart-review CSV template exists.
- Multi-timeframe chart review summary exists.
- Optional TradingView Import bridge exists for chart review rows.
- Optional advanced CSV bridge exists.
- Product UI for non-coders.
- Next Best Action guidance exists.
- Start Review guidance exists.
- Advanced tabs remain available.
- Daily Review tab now includes status summary and next best action.
- Use fake EXAMPLE row for cloud smoke test.
- Cloud dashboard remains decision-support only.
- No broker/order/alert/payment automation.
- No real calibration CSVs committed.
- `APP_ACCESS_CODE` remains optional and must not be committed.

## Live Data — Read Only

- Dashboard tab: `Live Data — Read Only`.
- Start with Live Market Data is the clearest first-run Home path when Polygon is configured.
- For a more user-friendly explanation, use `Market Breakdown` first.
- Market-data provider support is read-only.
- Provider config uses Streamlit secrets or environment variables.
- Supported provider names for config: `alpaca`, `polygon`.
- Use `docs/readonly_market_data.md`.
- Provider setup notes: `docs/market_data_provider_setup.md`.
- v1.1.3 hardens provider diagnostics for placeholder keys, HTTP-like 401/403/429/400 errors, and secret redaction.
- v1.1.4 confirms Polygon provider smoke passed for SPY 1D and SPY 15m.
- If provider rows generate, use `Send to Review Engine` or paste into the Review Engine Paste Box and continue with Daily Review.
- If provider fetch fails later, use the EXAMPLE fallback and check provider diagnostics.
- Market Breakdown can use the same read-only Polygon provider to create watchlist analysis cards.
- Market Breakdown shows bias/confidence/trend/momentum/level/risk explanations.
- Provider rows include provider-derived support/resistance from recent highs/lows when possible.
- Provider-derived support/resistance is read-only context only and must be verified manually.
- TradingView Import CSV remains an optional advanced/debug bridge.
- Advanced copy/paste table text is optional; most users can ignore it unless they are doing validation/calibration.
- Home shows Live Data as a simple product feature while preserving advanced tabs.
- Home can generate Market Breakdown cards and Send to Review Engine from read-only Polygon data.
- No broker/order/alert/payment automation.
- No TradingView scraping or sync.
- Do not commit API keys.
- Do not commit `.streamlit/secrets.toml`.
- Generated rows still require manual chart confirmation.

## TradingView Chart Workspace

- Dashboard tab: `Chart Workspace`.
- Use it to capture manual chart review context for `15m`, `1h`, `4h`, and `1D`.
- Fields include ticker, timeframe, price, chart bias, supply/demand, support/resistance, breakout, breakdown, invalidation, EMA9, EMA21, WMA50, WMA200, SMA200, MACD histogram, volume notes, pattern notes, fundamentals notes, macro notes, manual notes, and source.
- The chart-review CSV template is copy-ready.
- The chart-review-to-TradingView-Import bridge is optional and session-only.
- The bridge sends one execution row per ticker, preferring `15m`, so `1h`, `4h`, and `1D` rows stay as context in Chart Workspace.
- Use the SPY 1D/15m examples as shape examples only; replace values with chart-confirmed values.
- Use `docs/tradingview_chart_workspace.md`.
- Use `docs/chart_review_csv_bridge.md`.
- Use `docs/fundamentals_macro_context.md`.
- Optional Pine helper source: `tradingview/trading_autopilot_chart_helper_v140.pine`.
- Pine helper is indicator-only, has no alertcondition, has no strategy, and must not be published by automation.
- Manual TradingView confirmation remains required.
- No broker/order/alert/payment automation.

## Alert Planner

- Dashboard tab: `Alert Planner`.
- Use it after Market Breakdown and Chart Workspace to draft manual alert ideas.
- Alert Planner shows `ALERT PLANNING ONLY — NO ALERTS ARE CREATED`.
- Alert Planner uses decision-support cards and final manual confirmation reminders.
- Fields include ticker, timeframe, setup bias, setup type, trigger, invalidation, targets, risk/reward note, chart confirmation, fundamentals context, macro context, news catalyst, volume confirmation, manual notes, and status.
- Alert Planner can build draft rows from Chart Workspace or Market Breakdown session rows.
- Alert Planner includes a Quick Draft Alert Plan form and an advanced Manual Alert Plan CSV template/parser.
- Alert message drafts include `Decision support only. Verify chart manually. No orders.`
- Copy alert message drafts manually only after chart confirmation and final user approval.
- Pine helper source: `tradingview/trading_autopilot_chart_helper_v150.pine`.
- Pine v1.5.0 is indicator-only and includes alert-ready conditions as code only.
- No automatic TradingView alert creation/editing/enabling/deleting.
- No webhook automation.
- No broker/order/payment automation.
- Use `docs/alert_planner.md`.
- Use `docs/tradingview_alert_safety.md`.
- Use `docs/decision_support_workflow.md`.

## TradingView Layout Rollout

The TradingView v0.1.4 layout rollout report is recorded at `docs/tradingview_layout_rollout_report.md`. Automated TradingView helper rollout is paused and manual install is now required; use `docs/manual_tradingview_helper_install.md`.

Prior verification found a 265-line mixed Pine buffer instead of the clean 57-line helper. Manual install is now required.

## Mobile Candidate Intake

- Use the dashboard `Manual Entry` source to add candidate rows from a phone when you do not already have a CSV.
- Use `Quick Add Candidate` for fast phone entry, then edit levels in the table.
- Use `Advanced levels and indicators` only when you want to enter full chart levels immediately.
- Use the dashboard `How to fix this` guidance to repair ticker, price, timeframe, numeric field, and missing-column problems.
- Upload/paste/manual candidate data stays session-only in Streamlit memory and is not written to disk by the app.
- `Download CSV` is a browser-only export controlled by the user.
- See `docs/mobile_candidate_intake.md` for the phone workflow and data-safety checklist.
- The app remains decision-support only.

## Mobile Import / Export

- Manual Entry is available in the dashboard.
- Upload/Paste/Manual data is session-only.
- `Download CSV` is browser-controlled.
- See `docs/mobile_import_export.md`.
- `real_candidates_WORKING.csv` remains local-only and ignored.
- TradingView confirmation remains manual.
- No broker/order automation.

## Watchlist Calibration

- v0.3 calibration starts with SPY, QQQ, TSLA, SMCI, PLTR, AI, OKLO, SMR, SPCE, INTC.
- Use `docs/v030_watchlist_calibration.md`.
- Use `data/calibration_template.csv`.
- Calibration compares manual chart read vs dashboard output.
- TradingView confirmation remains manual.
- No broker/order automation.
- No real candidate data committed.

## TradingView Import Bridge

- Use the dashboard `TradingView Import` source to paste copied/exported TradingView or scanner-style CSV rows.
- Supported simple columns include `ticker` or `symbol`, `price`, `close`, or `last`, `timeframe` or `interval`, `bias_note` or `bias`, `key_level_note` or `key_level`, and `notes`.
- Use `docs/tradingview_import_bridge.md`.
- Imported rows convert in memory to candidate rows for dashboard review.
- Import data is session-only and is not written to disk by the app.
- Use `TradingView Import Repair` and `Candidate Validation` before reviewing output.
- TradingView confirmation remains manual.
- No live TradingView sync.
- No TradingView scraping.
- No TradingView alert creation/editing.
- No broker/order automation.
- No real candidate data committed.

## Rich TradingView / Scanner Import

- `TradingView Import` now accepts optional chart/scanner fields.
- Supported examples include EMA, SMA, support/resistance, breakout/breakdown, invalidation, relative volume, and MACD histogram.
- More complete imports make calibration more useful.
- Use `docs/tradingview_import_bridge.md`.
- Rows stay session-only.
- TradingView confirmation remains manual.
- No live TradingView sync.
- No TradingView scraping.
- No TradingView alert creation/editing.
- No broker/order automation.
- No real candidate data committed.

## Calibration Results

- Use the dashboard tab: `Calibration Results`.
- Use it after `Manual Entry`, `Upload CSV`, `Paste CSV`, or `TradingView Import`.
- Records manual chart read vs dashboard output.
- Auto-filled calibration key_levels and manual_notes can come from rich TradingView/scanner imports.
- You can still edit key_levels and manual_notes in `Calibration Results` before review or download.
- Use `Add Current Calibration Results to Batch Log` to collect rows across repeated imports.
- `Download Calibration CSV` is browser-controlled.
- Use `docs/calibration_results_capture.md`.
- Calibration results are session-only.
- TradingView confirmation remains manual.
- No live TradingView sync.
- No TradingView scraping.
- No broker/order automation.
- No real candidate data committed.

## Calibration Label Apply

- Use the dashboard section: `Apply Calibration Labels` inside `Calibration Results`.
- Paste labels CSV instead of editing grid cells one by one.
- Helps Codex/dashboard automation avoid fragile grid editing.
- Use `docs/calibration_label_apply.md`.
- Label data is session-only.
- No broker/order automation.
- No real calibration CSVs committed.

## Calibration Batch Log

- Use the dashboard tab: `Calibration Batch Log`.
- Add current Calibration Results rows to a session-only batch.
- Same ticker + timeframe replaces the older row.
- Use Calibration Review -> Use Calibration Batch Log to review accumulated rows.
- Download Batch Log CSV is browser-controlled.
- Use `docs/calibration_batch_log.md`.
- Session-only.
- No broker/order automation.
- No real candidate data committed.

## Calibration Review

- Use the dashboard tab: `Calibration Review`.
- Click `Use Current Session Calibration Results` to review editable rows from the current `Calibration Results` session.
- Click `Use Calibration Batch Log` to review accumulated batch rows.
- Auto-filled calibration key_levels and manual_notes carry into current-session review.
- No download/upload is needed for same-session review.
- Upload or paste downloaded Calibration CSV remains available for older files.
- Session-only review data stays in memory.
- Summarizes match status and issue type patterns.
- Use `docs/calibration_review.md`.
- No scoring changes from one ticker.
- TradingView confirmation remains manual.
- No live TradingView sync.
- No TradingView scraping.
- No broker/order automation.
- No real candidate data committed.

## Scoring Review Notes

- Dashboard shows Scoring Review Notes inside Calibration Review.
- Use after Calibration Batch Log or uploaded/pasted Calibration CSV.
- Separates match_status counts from issue_type counts.
- Helps decide whether scoring needs future adjustment.
- Does not change scoring automatically.
- Use `docs/scoring_review_notes.md`.
- Session-only.
- No broker/order automation.
- No real calibration CSVs committed.

## Scoring Adjustment Proposal

- Dashboard shows Scoring Adjustment Proposal inside Calibration Review.
- Uses Calibration Batch Log or uploaded/pasted Calibration CSV.
- Shows evidence level and conservative proposal notes.
- Does not change scoring automatically.
- Use `docs/scoring_adjustment_proposal.md`.
- Session-only.
- No broker/order automation.
- No real calibration CSVs committed.

## Calibration Baseline / Daily Use

- First 20-row baseline is documented in `docs/calibration_baseline_v040.md`.
- Daily checklist is in `docs/daily_use_checklist.md`.
- Baseline result: 20 rows, 19 matches, 1 false positive, stable evidence.
- No scoring change is recommended yet.
- Dashboard is decision-support only.
- TradingView confirmation remains manual.
- No broker/order/alert automation.
- No real calibration CSVs committed.

## Daily Use / v1.0 Prep

- Use `docs/daily_use_checklist.md` for the daily dashboard flow.
- Use `docs/v1_freeze_checklist.md` as the pre-release safety checklist.
- Use `docs/tradingview_readonly_workflow.md` for TradingView-assisted chart review.
- Dashboard remains decision-support only.
- TradingView chart review can provide values, but broker/order/payment workflows remain blocked.
- No scoring changes are recommended yet from the 20-row baseline.
- No real calibration CSVs committed.

## v1.0 Decision-Support Release

- Use `docs/v100_decision_support_freeze.md` for the final operating guide and launch-readiness summary.
- Use `docs/operator_quickstart.md` for the fastest local dashboard start and daily review checklist.
- Use `docs/release_v1_0_0.md` for the final v1.0 release summary.
- Current release: `1.0.0`.
- This is the decision-support v1.0 release.
- Dashboard remains decision-support only.
- Manual TradingView confirmation is required.
- This freezes the validated decision-support workflow, not scoring logic.
- Pine helper values remain manual/read-only inputs.
- Calibration Label Apply, Batch Log, Calibration Review, Scoring Review Notes, and Scoring Adjustment Proposal remain session-only review tools.
- The 20-row baseline remains 19 matches, 1 false positive, stable evidence, and no scoring change recommended yet.
- No scoring changes were made from the 20-row baseline.
- No broker/order/alert/payment automation exists.
- Real calibration CSVs and candidate data remain local-only and uncommitted.
- No real candidate data, downloaded calibration CSVs, or secrets are committed.

## Daily Review UI

- Dashboard tab: `Daily Review`.
- Shows 60-second workflow.
- Shows current review cards.
- Shows label CSV template when Calibration Results rows exist.
- Use `docs/daily_review_ui.md`.
- Decision-support only.
- Manual TradingView confirmation remains required.
- No broker/order/alert/payment automation.
- No scoring logic changes.

## v0.2.0-dev Candidate Intake

Use `data/sample_candidates.csv` only for scanner/dashboard testing. For real watchlist review, start from `data/real_candidates_template.csv`, copy it locally to `data/real_candidates_WORKING.csv`, and fill that working file with your current TradingView watchlist candidates. Do not commit `data/real_candidates_WORKING.csv` or any real candidate export.

Run the local review flow with:

```bash
python3 tools/validate_candidates.py data/real_candidates_WORKING.csv
python3 dashboard/app.py data/real_candidates_WORKING.csv
```

The v0.2 workflow is documented in `docs/v020_candidate_intake.md`. It keeps TradingView confirmation manual.

## v0.1.4 Notes

This version keeps the v0.1.3 repo hygiene baseline and adds hard liquidity gates, direction-aware alerts, cleaner dashboard buckets, the restored detailed TradingView audit, and a Pine helper under `tradingview/` using the chart color scheme.

## v0.1.3 Notes

This version keeps the v0.1.2 directional-conflict baseline and adds repo hygiene, richer playbook documentation, ask-first option premium accounting, explicit earnings status handling, and stricter covered-call DTE checks.

## Codex Cloud Continuity

- Use `AGENTS.md` for Codex project rules and safety boundaries.
- Use `docs/PROJECT_STATUS.md` for current project status and next phases.
- Use `docs/codex_task_prompts.md` for future ready-to-copy Codex task prompts.
- Codex Cloud repo: `davidbenizri25-wq/trading-elite-system`.
- Branch: `main`.

</details>
