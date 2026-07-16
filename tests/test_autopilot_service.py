from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import unittest
from unittest.mock import Mock, patch

from autopilot_engine import MarketContext, evaluate_setup as evaluate_engine_setup
from autopilot_service import (
    AutopilotServiceError,
    _CHART_DATA_CACHE,
    _DATA_CACHE,
    _bar_completion_time,
    _chart_frames_from_engine_frames,
    TTLCache,
    analyze_symbol,
    _completed_bars,
    _data_label,
    _frames_for_symbol,
    _regular_session_bars,
    _latest_timestamp,
    _option_contract_input,
    load_chart_bars,
    sector_etf_for_security,
    unavailable_result,
)
from polygon_provider import EarningsContext, OHLCVBar, ResolvedTicker


class AutopilotServiceTests(unittest.TestCase):
    def test_decision_intraday_frames_exclude_extended_hours(self) -> None:
        def bar(hour: int, minute: int) -> OHLCVBar:
            return OHLCVBar(
                datetime(2026, 7, 16, hour, minute, tzinfo=timezone.utc),
                100,
                101,
                99,
                100.5,
                1_000,
                "15m",
            )

        filtered = _regular_session_bars(
            [
                bar(12, 0),   # 08:00 ET pre-market
                bar(13, 30),  # 09:30 ET
                bar(19, 45),  # 15:45 ET
                bar(20, 0),   # 16:00 ET after-hours boundary
            ]
        )

        self.assertEqual(
            [item.timestamp for item in filtered],
            [bar(13, 30).timestamp, bar(19, 45).timestamp],
        )

        half_day = [
            OHLCVBar(
                datetime(2026, 11, 27, hour, minute, tzinfo=timezone.utc),
                100,
                101,
                99,
                100.5,
                1_000,
                "15m",
            )
            for hour, minute in ((17, 45), (18, 0))  # 12:45 and 13:00 ET
        ]
        self.assertEqual(_regular_session_bars(half_day), [half_day[0]])

    def _security(self, ticker: str) -> ResolvedTicker:
        return ResolvedTicker(
            ticker=ticker,
            name=ticker,
            primary_exchange="XNAS",
            market="stocks",
            locale="us",
            security_type="CS",
            active=True,
            currency_name="usd",
            composite_figi=None,
            share_class_figi=None,
            last_updated=None,
        )

    def _provider(self, earnings: EarningsContext) -> Mock:
        provider = Mock()
        provider.resolve_symbol.return_value = self._security("AAPL")
        provider.market_status.return_value = Mock(market="open")
        provider.news.return_value = []
        provider.earnings_context.return_value = earnings
        return provider

    def _frames(self) -> dict[str, list[dict[str, object]]]:
        row = {
            "timestamp": "2030-07-02T14:30:00+00:00",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
            "volume": 1_000,
            "timeframe": "15m",
            "complete": True,
        }
        return {"15M": [row], "1D": [row]}

    def test_sector_override(self) -> None:
        self.assertEqual(sector_etf_for_security(self._security("AAPL")), "XLK")
        self.assertEqual(sector_etf_for_security(self._security("TSLA")), "XLY")

    def test_data_label_is_conservative(self) -> None:
        now = datetime(2030, 1, 2, 15, 0, tzinfo=timezone.utc)
        self.assertEqual(_data_label("open", datetime(2030, 1, 2, 14, 45, tzinfo=timezone.utc), now), "delayed")
        self.assertEqual(_data_label("closed", datetime(2030, 1, 1, 21, 0, tzinfo=timezone.utc), now), "last-close")
        self.assertEqual(_data_label("open", None, now), "unavailable")
        self.assertEqual(_data_label("closed", now - timedelta(hours=97), now), "stale")
        self.assertEqual(_data_label("unknown", now - timedelta(minutes=1), now), "stale")
        self.assertEqual(_data_label("open", now + timedelta(seconds=1), now), "stale")

    def test_incomplete_bars_are_removed_and_timestamp_is_completion_time(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)

        def bar(timestamp: datetime, timeframe: str) -> OHLCVBar:
            return OHLCVBar(timestamp, 100, 101, 99, 100.5, 1_000, timeframe)

        completed_5m = bar(now - timedelta(minutes=7), "5m")
        forming_5m = bar(now - timedelta(minutes=2), "5m")
        self.assertEqual(_completed_bars([completed_5m, forming_5m], "5M", now), [completed_5m])

        completed_15m = bar(now - timedelta(minutes=22), "15m")
        forming_15m = bar(now - timedelta(minutes=7), "15m")
        filtered = _completed_bars([completed_15m, forming_15m], "15M", now)
        self.assertEqual(filtered, [completed_15m])
        latest = _latest_timestamp(
            {"15M": [{"timestamp": completed_15m.timestamp.isoformat(), "open": 100, "high": 101, "low": 99, "close": 100.5, "volume": 1_000}]}
        )
        self.assertEqual(latest, completed_15m.timestamp + timedelta(minutes=15))

    def test_minute_and_month_completion_labels_do_not_collide(self) -> None:
        started = datetime(2030, 7, 2, 14, 30, tzinfo=timezone.utc)
        self.assertEqual(_bar_completion_time(started, "1m"), started + timedelta(minutes=1))
        self.assertEqual(
            _bar_completion_time(started, "1M"),
            datetime(2030, 8, 1, 4, 0, tzinfo=timezone.utc),
        )

    def test_service_preserves_completed_five_minute_frame(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        completed = OHLCVBar(now - timedelta(minutes=7), 100, 101, 99, 100.5, 1_000, "5m")
        forming = OHLCVBar(now - timedelta(minutes=2), 100.5, 102, 100, 101, 900, "5m")

        class Provider:
            def daily_bars(self, *_args):
                return []

            def bars_15m(self, *_args):
                return []

            def bars_5m(self, *_args):
                return [completed, forming]

        frames, warnings = _frames_for_symbol(
            Provider(), "AAPL", now, cache_namespace="test-completed-5m"
        )
        self.assertEqual(warnings, [])
        self.assertEqual(len(frames["5M"]), 1)
        self.assertEqual(frames["5M"][0]["timestamp"], completed.timestamp.isoformat())

    def test_cache_avoids_repeated_factory(self) -> None:
        cache = TTLCache(ttl_seconds=60)
        calls = []
        self.assertEqual(cache.get_or_create("x", lambda: calls.append(1) or {"value": 3}), {"value": 3})
        self.assertEqual(cache.get_or_create("x", lambda: calls.append(2) or {"value": 4}), {"value": 3})
        self.assertEqual(calls, [1])

    def test_partial_error_frame_bundle_is_not_cached_and_can_recover(self) -> None:
        now = datetime(2030, 7, 16, 14, 5, tzinfo=timezone.utc)
        daily = [
            OHLCVBar(
                datetime(2030, month, day, 4, tzinfo=timezone.utc),
                100,
                101,
                99,
                100.5,
                1_000,
                "1d",
            )
            for month, day in ((6, 2), (6, 30), (7, 6), (7, 15))
        ]
        intraday = [
            OHLCVBar(
                datetime(2030, 7, 15, 13, 30, tzinfo=timezone.utc),
                100,
                101,
                99,
                100.5,
                1_000,
                "15m",
            ),
            OHLCVBar(
                datetime(2030, 7, 16, 13, 45, tzinfo=timezone.utc),
                100,
                101,
                99,
                100.5,
                1_000,
                "15m",
            ),
        ]
        five_minute = [
            OHLCVBar(
                datetime(2030, 7, 16, 13, 55, tzinfo=timezone.utc),
                100,
                101,
                99,
                100.5,
                1_000,
                "5m",
            )
        ]

        class RecoveringProvider:
            def __init__(self) -> None:
                self.calls = {"daily": 0, "15m": 0, "5m": 0}

            def _result(self, label: str, rows: list[OHLCVBar]) -> list[OHLCVBar]:
                self.calls[label] += 1
                if self.calls[label] == 1:
                    raise PolygonProviderError(
                        "fixture availability failure",
                        classification="availability",
                    )
                return rows

            def daily_bars(self, *_args):
                return self._result("daily", daily)

            def bars_15m(self, *_args):
                return self._result("15m", intraday)

            def bars_5m(self, *_args):
                return self._result("5m", five_minute)

        from polygon_provider import PolygonProviderError

        provider = RecoveringProvider()
        _DATA_CACHE.clear()
        first, first_warnings = _frames_for_symbol(
            provider,
            "AAPL",
            now,
            cache_namespace="partial-recovery",
        )
        second, second_warnings = _frames_for_symbol(
            provider,
            "AAPL",
            now,
            cache_namespace="partial-recovery",
        )
        third, third_warnings = _frames_for_symbol(
            provider,
            "AAPL",
            now,
            cache_namespace="partial-recovery",
        )

        self.assertEqual(first, {})
        self.assertEqual(len(first_warnings), 3)
        self.assertEqual(second_warnings, [])
        self.assertEqual(third_warnings, [])
        self.assertTrue(all(second.get(label) for label in ("1D", "1W", "1M", "15M", "1H", "4H", "5M")))
        self.assertEqual(second, third)
        self.assertEqual(provider.calls, {"daily": 2, "15m": 2, "5m": 2})

    def test_chart_frame_keys_use_exact_ui_labels_without_mutating_sources(self) -> None:
        frames = {
            "5M": [{"timestamp": "2030-07-02T14:30:00+00:00", "close": 100}],
            "1M": [{"timestamp": "2030-07-01T04:00:00+00:00", "close": 90}],
        }
        output = _chart_frames_from_engine_frames(frames)
        self.assertEqual(set(output), {"5m", "1M"})
        output["5m"][0]["close"] = 999
        self.assertEqual(frames["5M"][0]["close"], 100)

    def test_lazy_chart_interval_filters_forming_bar_and_reuses_cache(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        completed = OHLCVBar(now - timedelta(minutes=2), 100, 101, 99, 100.5, 1_000, "1m")
        forming = OHLCVBar(now - timedelta(seconds=30), 100.5, 102, 100, 101, 900, "1m")

        class Provider:
            calls = 0

            def __init__(self, *_args, **_kwargs):
                pass

            def bars_1m(self, *_args):
                Provider.calls += 1
                return [completed, forming]

        _CHART_DATA_CACHE.clear()
        with patch("autopilot_service.PolygonProvider", Provider):
            first = load_chart_bars("aapl", "1m", "test-key", now=now)
            first[0]["close"] = 999
            second = load_chart_bars("AAPL", "1m", "test-key", now=now)
        self.assertEqual(Provider.calls, 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0]["close"], 100.5)
        self.assertEqual(second[0]["timeframe"], "1m")
        self.assertTrue(second[0]["complete"])

    def test_empty_chart_result_is_not_cached_and_can_recover(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        recovered = OHLCVBar(now - timedelta(minutes=2), 100, 101, 99, 100.5, 1_000, "1m")

        class Provider:
            calls = 0

            def __init__(self, *_args, **_kwargs):
                pass

            def bars_1m(self, *_args):
                Provider.calls += 1
                return [] if Provider.calls == 1 else [recovered]

        _CHART_DATA_CACHE.clear()
        with patch("autopilot_service.PolygonProvider", Provider):
            first = load_chart_bars("AAPL", "1m", "test-key", now=now)
            second = load_chart_bars("AAPL", "1m", "test-key", now=now)

        self.assertEqual(first, [])
        self.assertEqual(len(second), 1)
        self.assertEqual(Provider.calls, 2)

    def test_four_hour_chart_uses_registry_lookback_and_real_resampling(self) -> None:
        now = datetime(2030, 7, 2, 19, 0, tzinfo=timezone.utc)
        started = datetime(2030, 7, 2, 13, 30, tzinfo=timezone.utc)
        source = [
            OHLCVBar(
                started + timedelta(minutes=15 * index),
                100 + index,
                101 + index,
                99 + index,
                100.5 + index,
                1_000 + index,
                "15m",
            )
            for index in range(20)
        ]

        class Provider:
            requested_start = None

            def __init__(self, *_args, **_kwargs):
                pass

            def bars_15m(self, _ticker, start, _end):
                Provider.requested_start = start
                return source

        _CHART_DATA_CACHE.clear()
        with patch("autopilot_service.PolygonProvider", Provider):
            rows = load_chart_bars("AAPL", "4H", "test-key", now=now)
        self.assertEqual(Provider.requested_start, (now - timedelta(days=730)).date())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["timeframe"], "4H")
        self.assertEqual(rows[0]["open"], 100)
        self.assertEqual(rows[0]["close"], 115.5)

    def test_selected_chart_interval_does_not_mutate_mtf_decision(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        resolved = self._security("AAPL")
        provider = unittest.mock.Mock()
        provider.resolve_symbol.return_value = resolved
        provider.market_status.return_value = unittest.mock.Mock(market="open")
        provider.news.return_value = []
        base_row = {
            "timestamp": "2030-07-02T14:30:00+00:00",
            "open": 100,
            "high": 101,
            "low": 99,
            "close": 100.5,
            "volume": 1_000,
            "timeframe": "15m",
            "complete": True,
        }
        selected_row = dict(base_row, timeframe="1m")
        frames = {"15M": [base_row], "1D": [base_row]}
        decision = unavailable_result("AAPL", "fixture")
        decision_snapshot = decision.to_dict()

        with (
            patch("autopilot_service.PolygonProvider", return_value=provider),
            patch("autopilot_service._frames_for_symbol", return_value=(frames, [])),
            patch(
                "autopilot_service._market_context",
                return_value=(MarketContext(regime="mixed"), {}, []),
            ),
            patch("autopilot_service.evaluate_setup", return_value=decision),
            patch("autopilot_service.load_chart_bars", return_value=[selected_row]),
        ):
            result = analyze_symbol(
                "AAPL",
                "test-key",
                now=now,
                include_options=False,
                selected_timeframe="1m",
            )

        self.assertEqual(decision.to_dict(), decision_snapshot)
        self.assertEqual(result.selected_timeframe, "1m")
        self.assertEqual(result.chart_bars, [selected_row])
        self.assertEqual(set(result.chart_frames), {"15m", "1D", "1m"})
        self.assertIn("interval=1", result.tradingview_url)
        serialized = result.to_dict()
        self.assertEqual(serialized["selected_timeframe"], "1m")
        self.assertEqual(serialized["chart_frames"]["1m"], [selected_row])

    def test_chart_loader_rejects_missing_or_unknown_inputs(self) -> None:
        with self.assertRaises(AutopilotServiceError):
            load_chart_bars("", "1m", "test-key")
        with self.assertRaises(AutopilotServiceError):
            load_chart_bars("AAPL", "2m", "test-key")

    def test_service_propagates_scheduled_earnings_context_into_decision(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        checked_through = date(2030, 7, 12)
        earnings = EarningsContext(
            status="scheduled",
            date=date(2030, 7, 9),
            date_status="projected",
            checked_at=now,
            checked_through=checked_through,
        )
        provider = self._provider(earnings)
        captured: dict[str, object] = {}

        def record_evaluate(*args, **kwargs):
            captured.update(kwargs)
            return evaluate_engine_setup(*args, **kwargs)

        with (
            patch("autopilot_service.PolygonProvider", return_value=provider),
            patch("autopilot_service._frames_for_symbol", return_value=(self._frames(), [])),
            patch(
                "autopilot_service._market_context",
                return_value=(MarketContext(regime="mixed"), {}, []),
            ),
            patch("autopilot_service.evaluate_setup", side_effect=record_evaluate),
            patch("autopilot_service.load_chart_bars", return_value=[]),
        ):
            result = analyze_symbol(
                "AAPL",
                "test-key",
                now=now,
                include_options=False,
            )

        provider.earnings_context.assert_called_once_with(
            "AAPL",
            now.date(),
            checked_through,
            checked_at=now,
        )
        self.assertEqual(captured["earnings_date"], "2030-07-09")
        self.assertEqual(captured["days_to_earnings"], 7)
        self.assertEqual(captured["earnings_status"], "scheduled")
        self.assertEqual(captured["earnings_date_status"], "projected")
        self.assertEqual(captured["earnings_checked_through"], "2030-07-12")
        self.assertEqual(result.decision.earnings_status, "scheduled")
        self.assertEqual(result.decision.earnings_date, "2030-07-09")
        self.assertEqual(result.provider_health.provider, "Polygon + Massive / Benzinga")
        serialized = result.to_dict()["decision"]
        self.assertEqual(serialized["days_to_earnings"], 7)
        self.assertEqual(serialized["earnings_date_status"], "projected")
        self.assertEqual(serialized["earnings_checked_through"], "2030-07-12")
        health = result.to_dict()["provider_health"]
        self.assertEqual(health["data_age_seconds"], 1320.0)
        self.assertFalse(health["stale"])
        self.assertEqual(set(health["cache_stats"]), {"analysis", "chart"})
        self.assertEqual(health["earnings_status"], "scheduled")
        self.assertIsNone(health["earnings_error_kind"])

    def test_service_never_invents_a_complete_earnings_window(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        earnings = Mock()
        earnings.status = "verified_none"
        earnings.date = None
        earnings.date_status = None
        earnings.checked_through = None
        earnings.message = "Calendar verification was incomplete."
        provider = self._provider(earnings)
        captured: dict[str, object] = {}

        def record_evaluate(*args, **kwargs):
            captured.update(kwargs)
            return evaluate_engine_setup(*args, **kwargs)

        with (
            patch("autopilot_service.PolygonProvider", return_value=provider),
            patch("autopilot_service._frames_for_symbol", return_value=(self._frames(), [])),
            patch(
                "autopilot_service._market_context",
                return_value=(MarketContext(regime="mixed"), {}, []),
            ),
            patch("autopilot_service.evaluate_setup", side_effect=record_evaluate),
            patch("autopilot_service.load_chart_bars", return_value=[]),
        ):
            result = analyze_symbol("AAPL", "test-key", now=now, include_options=False)

        self.assertEqual(captured["earnings_status"], "unresolved")
        self.assertIsNone(captured["earnings_checked_through"])
        self.assertEqual(result.decision.earnings_status, "unresolved")
        self.assertEqual(result.provider_health.provider, "Polygon + Massive / Benzinga")
        self.assertEqual(result.provider_health.earnings_error_kind, "implementation")

    def test_service_passes_stale_benchmark_freshness_into_decision_boundary(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        earnings = EarningsContext(
            status="verified_none",
            date=None,
            date_status=None,
            checked_at=now,
            checked_through=date(2030, 7, 12),
        )
        provider = self._provider(earnings)
        target_frames = self._frames()
        stale_row = dict(target_frames["1D"][0], timestamp="2020-01-02T14:30:00+00:00")
        market_frames = {
            "SPY": {"1D": [stale_row], "4H": [stale_row]},
            "QQQ": {"1D": [stale_row], "4H": [stale_row]},
        }
        captured: dict[str, object] = {}
        decision = unavailable_result("AAPL", "fixture")

        def record_evaluate(*_args, **kwargs):
            captured.update(kwargs)
            return decision

        with (
            patch("autopilot_service.PolygonProvider", return_value=provider),
            patch("autopilot_service._frames_for_symbol", return_value=(target_frames, [])),
            patch(
                "autopilot_service._market_context",
                return_value=(MarketContext(regime="risk-on"), market_frames, []),
            ),
            patch("autopilot_service.evaluate_setup", side_effect=record_evaluate),
            patch("autopilot_service.load_chart_bars", return_value=[]),
        ):
            analyze_symbol("AAPL", "test-key", now=now, include_options=False)

        issues = list(captured["freshness_issues"])
        self.assertTrue(any("SPY benchmark 1D evidence is stale" in item for item in issues))
        self.assertTrue(any("QQQ benchmark 4H evidence is stale" in item for item in issues))
        self.assertEqual(captured["evaluated_at"], now)

    def test_verified_none_passes_explicit_none_to_options_context(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        earnings = EarningsContext(
            status="verified_none",
            date=None,
            date_status=None,
            checked_at=now,
            checked_through=date(2030, 7, 12),
        )
        provider = self._provider(earnings)
        contract = Mock()
        contract.to_dict.return_value = {
            "underlying_ticker": "AAPL",
            "option_ticker": "O:AAPL300719C00100000",
        }
        provider.options_chain.return_value = [contract]
        decision = unavailable_result("AAPL", "fixture")
        decision.state = "ENTER"
        decision.verdict = "ENTER"
        decision.entry_conditions_satisfied = True
        decision.earnings_status = "verified_none"
        decision.earnings_date = None
        captured: dict[str, object] = {}
        ranked = {"status": "recommend", "contracts": [{"contract_symbol": "fixture"}]}

        def record_rank(contracts, context, *, now):
            captured["contracts"] = contracts
            captured["context"] = context
            captured["now"] = now
            return ranked

        with (
            patch("autopilot_service.PolygonProvider", return_value=provider),
            patch("autopilot_service._frames_for_symbol", return_value=(self._frames(), [])),
            patch(
                "autopilot_service._market_context",
                return_value=(MarketContext(regime="mixed"), {}, []),
            ),
            patch("autopilot_service.evaluate_setup", return_value=decision),
            patch("autopilot_service.load_chart_bars", return_value=[]),
            patch("options_ranker.rank_option_contracts", side_effect=record_rank),
        ):
            result = analyze_symbol(
                "AAPL",
                "test-key",
                now=now,
                include_options=True,
            )

        options_context = captured["context"]
        self.assertIn("earnings_date", options_context)
        self.assertIsNone(options_context["earnings_date"])
        self.assertEqual(captured["now"], now)
        self.assertEqual(result.decision.options, ranked)

    def test_massive_entitlement_is_structured_in_decision_and_provider_health(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        earnings = EarningsContext(
            status="unresolved",
            date=None,
            date_status=None,
            checked_at=now,
            checked_through=date(2030, 7, 12),
            message="Massive earnings calendar failed with HTTP 403 (forbidden).",
            error_kind="entitlement",
            status_code=403,
            attempts=1,
            latency_ms=42.5,
        )
        provider = self._provider(earnings)

        with (
            patch("autopilot_service.PolygonProvider", return_value=provider),
            patch("autopilot_service._frames_for_symbol", return_value=(self._frames(), [])),
            patch(
                "autopilot_service._market_context",
                return_value=(MarketContext(regime="mixed"), {}, []),
            ),
            patch("autopilot_service.load_chart_bars", return_value=[]),
        ):
            result = analyze_symbol("AAPL", "test-key", now=now, include_options=False)

        self.assertEqual(result.decision.earnings_status, "unresolved")
        self.assertEqual(result.decision.earnings_error_kind, "entitlement")
        self.assertEqual(result.decision.earnings_status_code, 403)
        self.assertEqual(result.provider_health.earnings_error_kind, "entitlement")
        self.assertEqual(result.provider_health.earnings_latency_ms, 42.5)

    def test_option_rank_input_requires_quote_timestamp_and_keeps_underlying(self) -> None:
        row = _option_contract_input(
            {
                "underlying_ticker": "AAPL",
                "option_ticker": "O:AAPL260821C00100000",
                "quote_timestamp": None,
                "trade_timestamp": "2030-01-02T14:59:00Z",
                "day_timestamp": "2030-01-02T14:58:00Z",
                "underlying_timestamp": "2030-01-02T14:59:30Z",
            }
        )
        self.assertEqual(row["underlying_ticker"], "AAPL")
        self.assertIsNone(row["snapshot_timestamp"])

    def test_unavailable_provider_never_enters(self) -> None:
        result = unavailable_result("AAPL", "not configured")
        self.assertEqual(result.verdict, "PASS")
        self.assertEqual(result.data_label, "unavailable")

    def test_unavailable_provider_health_sets_stale_contract_flag(self) -> None:
        now = datetime(2030, 7, 2, 15, 7, tzinfo=timezone.utc)
        provider = self._provider(
            EarningsContext(
                status="verified_none",
                date=None,
                date_status=None,
                checked_at=now,
                checked_through=date(2030, 7, 12),
            )
        )
        with (
            patch("autopilot_service.PolygonProvider", return_value=provider),
            patch("autopilot_service._frames_for_symbol", return_value=({}, [])),
            patch(
                "autopilot_service._market_context",
                return_value=(MarketContext(regime="unavailable"), {}, []),
            ),
            patch("autopilot_service.load_chart_bars", return_value=[]),
        ):
            result = analyze_symbol("AAPL", "test-key", now=now, include_options=False)

        self.assertEqual(result.provider_health.data_label, "unavailable")
        self.assertTrue(result.provider_health.stale)
        self.assertEqual(result.decision.verdict, "PASS")


if __name__ == "__main__":
    unittest.main()
