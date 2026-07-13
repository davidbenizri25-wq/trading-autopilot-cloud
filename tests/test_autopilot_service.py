from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest

from autopilot_service import (
    TTLCache,
    _completed_bars,
    _data_label,
    _frames_for_symbol,
    _latest_timestamp,
    _option_contract_input,
    sector_etf_for_security,
    unavailable_result,
)
from polygon_provider import OHLCVBar, ResolvedTicker


class AutopilotServiceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
