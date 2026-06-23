# Changelog

## v1.5.1-wow-ui-alert-planner-polish-dev

- Added a more polished product-style Home experience.
- Added a clearer beginner "Start Review" path.
- Improved live-market-data first-run workflow and plain-English copy.
- Improved Alert Planner guidance and draft-only safety language.
- Improved decision-support cards and next-action language.
- Added beginner-friendly labels for technical workflows.
- Reduced CSV-first friction in beginner mode.
- Preserved advanced mode and all existing workflows.
- Preserved existing scoring logic.
- Preserved read-only market-data behavior.
- Preserved no broker/order/trade execution.
- Preserved no automatic TradingView alert creation.
- Preserved no secrets or real data commits.

## v1.5.0-alert-planning-decision-support-dev

- Added Alert Planning workflow for TradingView-ready decision-support alerts.
- Added setup decision-support cards with bias, trigger, invalidation, target zones, risk/reward notes, and manual confirmation requirements.
- Added alert-plan CSV template and parser.
- Added Pine helper update for alert-ready conditions without creating live alerts.
- Added fundamentals/macro context weighting notes for setup planning.
- Preserved read-only market-data behavior.
- Preserved existing scoring logic.
- Preserved no broker/order/trade execution.
- Preserved no automatic TradingView alert creation.
- Preserved no secrets or real data commits.

## v1.4.2-live-market-data-first-run-ux-dev

- Added a Home tab "Start with Live Market Data" first-run path.
- Added Home watchlist input, timeframe selector, and Analyze with Polygon button.
- Added plain-English provider status on Home.
- Added Home-generated Market Breakdown cards from read-only Polygon data.
- Added Home-generated TradingView Import CSV bridge with clear Daily Review / Calibration next step.
- Reduced sample-data confusion by pointing users to Market Breakdown and Live Data — Read Only.
- Preserved Sample data, Advanced mode, and all existing workflows.
- Preserved existing scoring logic.
- Preserved read-only market-data behavior.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.4.1-chart-workspace-real-session-polish-dev

- Polished Chart Workspace after real-session smoke testing.
- Improved Chart Workspace row entry and multi-timeframe summary guidance.
- Improved TradingView helper setup instructions.
- Added clearer chart-review examples for SPY 1D and 15m.
- Updated the TradingView Import bridge to send one execution row per ticker, preferring 15m, while preserving higher-timeframe rows as Chart Workspace context.
- Preserved existing scoring logic.
- Preserved read-only chart workflow.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.4.0-tradingview-chart-workspace-dev

- Added read-only TradingView Chart Workspace for manual chart review capture.
- Added multi-timeframe chart review CSV template and parser.
- Added support/resistance, supply/demand, breakout/breakdown, invalidation, MA, MACD, volume, pattern, fundamentals, and macro context fields.
- Added Chart Review CSV bridge into the existing TradingView Import workflow.
- Added optional non-trading Pine helper source for manual chart value review.
- Preserved existing scoring logic.
- Preserved read-only market-data behavior.
- Preserved manual chart confirmation.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.3.1-market-breakdown-mobile-polish-dev

- Added Beginner/Advanced navigation modes to reduce tab overload.
- Added Help / Safety tab for non-coder operation.
- Improved Market Breakdown mobile card spacing and plain-English guidance.
- Added provider-derived support/resistance levels from read-only market data.
- Improved Daily Review label helper so it reflects current imported rows.
- Clarified Advanced CSV bridge as optional.
- Reduced dataframe toolbar exposure in beginner/product views where practical.
- Preserved advanced dashboard mode.
- Preserved existing scoring logic.
- Preserved read-only market-data behavior.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.3.0-live-market-breakdown-dev

- Added Live Market Breakdown experience for non-coder watchlist review.
- Added plain-English ticker breakdown cards from read-only Polygon data.
- Added trend, momentum, level, risk, and next-action explanations.
- Added watchlist analysis flow that reduces CSV-first friction.
- Kept TradingView Import CSV as an advanced/debug bridge.
- Preserved existing scoring logic.
- Preserved read-only market-data behavior.
- Preserved manual chart confirmation.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.2.0-product-ui-dev

- Added product-style Home experience for non-coder daily use.
- Added cleaner status cards and next-action guidance.
- Added simplified daily workflow sections.
- Added Live Data quick-start guidance.
- Added beginner-friendly Help / Safety section.
- Preserved advanced dashboard tabs.
- Preserved existing scoring logic.
- Preserved read-only market-data behavior.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.1.4-streamlit-width-cleanup-and-live-data-polish-dev

- Replaced deprecated Streamlit use_container_width parameters.
- Polished Live Data — Read Only success guidance after Polygon provider smoke passed.
- Added post-Polygon-success workflow notes.
- Preserved existing scoring logic.
- Preserved read-only market-data behavior.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.1.3-polygon-provider-diagnostics-hardening-dev

- Hardened read-only Polygon/Massive diagnostics for HTTPError-like provider failures.
- Added placeholder key detection before provider calls.
- Expanded secret redaction for apiKey, api_key, POLYGON_API_KEY, token, password, secret, and bearer values.
- Added dashboard guidance for 401, 403, 429, and 400 provider responses.
- Preserved existing scoring logic.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.1.2-polygon-provider-diagnostics-dev

- Added sanitized Polygon/Massive HTTP status diagnostics for read-only provider failures.
- Added provider troubleshooting guidance for 1D key/config checks versus 15m intraday plan/entitlement checks.
- Improved Live Data — Read Only diagnostic notes without auto-refresh, downloads, or persistence.
- Preserved existing scoring logic.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.1.1-polygon-readonly-provider-smoke-dev

- Added Polygon/Massive read-only aggregate bars provider support.
- Added provider smoke flow for Live Data — Read Only.
- Added mocked provider tests for safe market-data imports.
- Added provider status and error handling improvements.
- Preserved existing scoring logic.
- Preserved no broker/order/alert/payment automation.
- Preserved no secrets or real data commits.

## v1.1.0-readonly-market-data-foundation-dev

- Added read-only market data foundation.
- Added provider configuration guidance for Streamlit secrets/environment variables.
- Added Live Data — Read Only dashboard tab.
- Added safe latest-bar normalization helpers.
- Added import-row generation from read-only market data.
- Preserved existing scoring logic.
- Preserved no broker/order/alert/payment automation.
- Preserved no real candidate data or secrets commits.

## v1.0.3-mobile-cloud-daily-use-polish-dev

- Added mobile-friendly Daily Review polish.
- Added cloud deployment smoke-test checklist.
- Added Streamlit deploy troubleshooting guide.
- Added post-deploy operator checklist.
- Improved daily-use guidance without changing scoring logic.
- Preserved decision-support-only behavior.
- Preserved no broker/order/alert/payment automation.
- Preserved no real candidate data or secrets commits.

## v1.0.2-cloud-saved-url-access-dev

- Added cloud/saved URL access guidance.
- Added Streamlit Cloud deployment checklist.
- Added phone bookmark workflow.
- Added APP_ACCESS_CODE setup guidance.
- Preserved decision-support-only dashboard behavior.
- Preserved no broker/order/alert/payment automation.
- Preserved no real candidate data or secrets commits.

## v1.0.1-daily-review-ui-dev

- Added Daily Review dashboard tab.
- Added one-page daily review workflow guidance.
- Added review cards for current candidates.
- Added label CSV template helper for current Calibration Results.
- Improved decision-support usability without changing scoring logic.
- Preserved manual TradingView confirmation.
- Preserved no broker/order/alert/payment automation.

## v1.0.0

- Released Trading Autopilot as a decision-support v1.0.
- Finalized local dashboard daily-use workflow.
- Preserved 20-row calibration baseline.
- Preserved existing scoring logic.
- Preserved manual TradingView confirmation.
- Preserved no broker/order/alert/payment automation.
- Preserved no real candidate data or calibration CSV commits.

## v1.0.0-decision-support-freeze-candidate

- Prepared Trading Autopilot as a v1.0 decision-support freeze candidate.
- Added final decision-support operating guide.
- Added final launch-readiness summary.
- Preserved 20-row calibration baseline.
- Preserved existing scoring logic.
- Preserved manual TradingView confirmation.
- Preserved no broker/order/alert/payment automation.
- Preserved no real candidate data or calibration CSV commits.

## v0.4.1-daily-use-polish-dev

- Added daily-use polish guidance.
- Added v1.0 freeze preparation checklist.
- Clarified dashboard workflow for one-ticker-at-a-time calibration.
- Clarified TradingView-assisted manual chart review boundaries.
- Preserved existing scoring logic.
- Preserved manual decision-support boundary.
- Preserved no broker/order/payment automation.

## v0.4.0-calibration-baseline-dev

- Added first 20-row calibration baseline summary.
- Added launch-readiness checklist for decision-support use.
- Documented stable evidence with 19 matches and 1 false positive.
- Documented that scoring changes are not justified yet.
- Preserved existing scoring logic.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.10-calibration-label-apply-dev

- Added CSV-based calibration label apply workflow.
- Reduced fragile manual grid editing in Calibration Results.
- Added label validation and repair guidance.
- Preserved Calibration Batch Log workflow.
- Preserved existing scoring logic.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.9-scoring-adjustment-proposal-dev

- Added scoring adjustment proposal workflow.
- Added conservative pattern summary before scoring changes.
- Added evidence thresholds for calibration-based scoring review.
- Added scoring proposal documentation.
- Preserved existing scoring logic.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.8-scoring-review-notes-dev

- Added scoring review notes for calibration batches.
- Clarified match status counts versus issue type counts.
- Added pattern guidance before scoring changes.
- Added scoring review documentation.
- Preserved session-only calibration data handling.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.7-calibration-batch-log-dev

- Added session-only calibration batch log.
- Added one-click add current calibration results to batch log.
- Added batch log review and browser-only CSV export.
- Reduced friction when calibrating one ticker at a time.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.6-autofill-calibration-context-dev

- Added automatic calibration key-level context from imported rows.
- Added automatic calibration notes from rich TradingView/scanner imports.
- Reduced manual editing in Calibration Results and Calibration Review.
- Preserved one-click current-session Calibration Review.
- Preserved session-only handling for user-supplied data.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.5-one-click-calibration-review-dev

- Added one-click review of current session calibration results.
- Reduced download/upload friction in Calibration Review.
- Kept browser-only Calibration CSV download available.
- Preserved session-only handling for calibration data.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.4-calibration-review-dev

- Added calibration review workflow for downloaded calibration results.
- Added session-only calibration CSV upload/paste review.
- Added match status and issue type summaries.
- Added scoring review notes guidance.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.3-rich-import-fields-dev

- Added richer optional TradingView/scanner import fields.
- Added support for moving averages, levels, volume, and MACD fields.
- Added import field guidance for more meaningful calibration.
- Preserved session-only handling for user-supplied data.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.2-calibration-results-dev

- Added dashboard calibration results capture.
- Added editable calibration rows from current dashboard candidates.
- Added browser-only calibration CSV download.
- Added calibration result documentation.
- Preserved session-only handling for user-supplied data.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.1-tradingview-import-dev

- Added safe TradingView/scanner import bridge.
- Added in-memory conversion from pasted/exported rows to candidate rows.
- Added flexible header mapping for ticker, price, close, timeframe, and notes.
- Added import repair guidance for phone/cloud use.
- Preserved session-only handling for user-supplied data.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.3.0-watchlist-calibration-dev

- Added guided watchlist calibration workflow.
- Added calibration template for first ticker batch.
- Added dashboard guidance for manual chart-vs-dashboard comparison.
- Added calibration documentation and safety boundary.
- Preserved manual TradingView confirmation.
- Preserved decision-support boundary.

## v0.2.3-mobile-ux-dev

- Improved mobile manual-entry flow.
- Added clearer validation repair guidance in the dashboard.
- Added import/export instructions for phone use.
- Added safer manual-entry empty-state guidance.
- Preserved session-only handling for upload, paste, and manual candidate data.
- Preserved decision-support boundary.

## v0.2.2-mobile-intake-dev

- Added mobile-friendly manual candidate entry.
- Added in-dashboard candidate validation feedback.
- Added in-memory candidate table editing flow.
- Added CSV download/export from dashboard.
- Kept uploaded, pasted, and manually entered candidate data session-only.
- Preserved decision-support boundary.

## v0.2.1-cloud-dev

- Added Streamlit Cloud readiness.
- Added GitHub Codespaces/devcontainer environment.
- Added phone-friendly dashboard input flow.
- Added cloud-safe CSV upload/paste support.
- Kept TradingView helper install paused/manual-only.
- Preserved decision-support boundary.

## v0.2.0-dev

- Began real watchlist calibration workflow.
- Added candidate intake template for real TradingView watchlist review.
- Added dashboard support for user-supplied candidate CSV path.
- Kept TradingView helper install paused/manual-only.
- Preserved decision-support boundary.

## v0.1.4-manual-tradingview-install

- Paused browser automation rollout after mixed Pine buffer detection.
- Added manual helper install guide.
- Preserved safety boundary.

## v0.1.4-layout-rollout

- Added TradingView layout inventory and rollout report.
- Verified local Trading Autopilot Helper v0.1.4 source and chart colors.
- Stopped automatic TradingView layout rollout after Pine Editor add-to-chart
  behavior proved unsafe on the first layout.
- Rolled back the failed chart insertion and preserved the decision-support
  boundary.

## v0.1.4

- Added hard liquidity gates for options and under-$25 share review.
- Restored real detailed TradingView audit.
- Made alert suggestions direction-aware.
- Cleaned dashboard buckets for conflicts and blocked candidates.
- Moved and fixed Pine helper style and version.

## v0.1.3

- Cleaned repo structure so playbook documentation lives under `playbook/`.
- Restored richer setup definitions, avoid rules, and TradingView style audit.
- Hardened option premium accounting with ask-first risk cost fields.
- Preserved bullish/bearish option direction even when candidates are skipped.
- Added explicit earnings status handling for options review.
- Tightened covered-call DTE handling for missing and 0 DTE contracts.
- Added repository validator and v0.1.3 regression tests.

## v0.1.2

- Added directional conflict gates so conflicted bullish or bearish setups cannot
  remain clean priority candidates.
- Added sample-data warnings in the dashboard and README.
- Added Pine level-map generation without overwriting the main helper indicator.
- Expanded dashboard buckets for context, options, shares, covered calls, alerts,
  and journal prep.

## v0.1.1

- Added explicit bullish, bearish, neutral, and context bias detection.
- Hardened risk configuration for options, shares, covered calls, and hard-stop
  accounting.
- Allowed bearish setups to score positively rather than only counting as risk.
- Tightened options and share filters around score, premium, price, and
  invalidation rules.

## v0.1.0

- Established the baseline local Trading Autopilot decision-support repo.
- Added sample scanner, scoring, options review, shares review, covered-call
  review, journal template, dashboard, and Pine helper surfaces.
