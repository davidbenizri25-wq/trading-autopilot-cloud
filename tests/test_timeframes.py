from __future__ import annotations

import unittest

from timeframes import (
    TIMEFRAME_LABELS,
    TIMEFRAME_REGISTRY,
    get_timeframe_spec,
    normalize_timeframe,
    tradingview_interval_for,
)


class TimeframeRegistryTests(unittest.TestCase):
    def test_registry_contains_exact_ui_labels_in_display_order(self) -> None:
        self.assertEqual(
            TIMEFRAME_LABELS,
            ("1m", "3m", "5m", "15m", "30m", "1H", "4H", "1D", "1W", "1M"),
        )
        self.assertEqual(tuple(TIMEFRAME_REGISTRY), TIMEFRAME_LABELS)

    def test_one_minute_and_one_month_are_collision_safe(self) -> None:
        self.assertEqual(normalize_timeframe("1m"), "1m")
        self.assertEqual(normalize_timeframe("1M"), "1M")
        self.assertEqual(normalize_timeframe("1 min"), "1m")
        self.assertEqual(normalize_timeframe("monthly"), "1M")
        self.assertEqual(normalize_timeframe("1mo"), "1M")
        self.assertEqual(tradingview_interval_for("1m"), "1")
        self.assertEqual(tradingview_interval_for("1M"), "M")

    def test_common_aliases_normalize_to_ui_labels(self) -> None:
        aliases = {
            "15M": "15m",
            "60m": "1H",
            "hourly": "1H",
            "240m": "4H",
            "daily": "1D",
            "weekly": "1W",
            "1mth": "1M",
        }
        for alias, expected in aliases.items():
            with self.subTest(alias=alias):
                self.assertEqual(normalize_timeframe(alias), expected)

    def test_specs_expose_source_resample_intraday_and_lookback_metadata(self) -> None:
        direct = {"1m", "3m", "5m", "15m", "30m", "1D"}
        intraday = {"1m", "3m", "5m", "15m", "30m", "1H", "4H"}
        expected_resample = {"1H": "15m", "4H": "15m", "1W": "1D", "1M": "1D"}
        for label in TIMEFRAME_LABELS:
            with self.subTest(label=label):
                spec = get_timeframe_spec(label)
                self.assertGreater(spec.lookback_days, 0)
                self.assertEqual(spec.intraday, label in intraday)
                self.assertEqual(spec.source_kind, "source" if label in direct else "resample")
                self.assertEqual(spec.resample_from, expected_resample.get(label))
                if label in direct:
                    self.assertIsNotNone(spec.polygon_multiplier)
                    self.assertIn(spec.polygon_timespan, {"minute", "day"})

    def test_invalid_values_raise_unless_an_explicit_default_is_supplied(self) -> None:
        for value in (None, "", "2H", "banana"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_timeframe(value)
        self.assertEqual(normalize_timeframe("banana", default="15m"), "15m")


if __name__ == "__main__":
    unittest.main()
