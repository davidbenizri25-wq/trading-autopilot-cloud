from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from polygon_provider import (
    AmbiguousSymbolError,
    InvalidTickerError,
    OHLCVBar,
    PolygonProvider,
    PolygonProviderError,
    build_aggregates_url,
    build_polygon_url,
    build_stock_snapshots_url,
    fetch_15m_bars,
    fetch_5m_bars,
    fetch_daily_bars,
    fetch_market_status,
    fetch_options_chain_snapshot,
    fetch_stock_snapshots,
    fetch_ticker_news,
    parse_options_chain_snapshot,
    parse_stock_snapshots,
    parse_ticker,
    redact_sensitive_text,
    redact_url,
    resample_bars,
    resolve_symbol,
    sanitize_ticker,
)


API_KEY = "unit-secret&admin=true"


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.closed = False

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.closed = True

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def epoch_ms(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def epoch_ns(value: datetime) -> int:
    return int(value.timestamp() * 1_000_000_000)


def bar(
    timestamp: datetime,
    open_value: float,
    high: float,
    low: float,
    close: float,
    volume: float,
    *,
    timeframe: str = "15m",
) -> OHLCVBar:
    return OHLCVBar(
        timestamp=timestamp,
        open=open_value,
        high=high,
        low=low,
        close=close,
        volume=volume,
        timeframe=timeframe,
    )


class PolygonTickerAndUrlTests(unittest.TestCase):
    def test_ticker_and_optional_exchange_are_normalized_without_deleting_input(self) -> None:
        self.assertEqual(sanitize_ticker(" $brk/b "), "BRK.B")
        self.assertEqual(parse_ticker("NASDAQ:aapl").ticker, "AAPL")
        self.assertEqual(parse_ticker("NASDAQ:aapl").exchange, "XNAS")
        self.assertEqual(parse_ticker("brk.b", "NYSE").exchange, "XNYS")
        self.assertEqual(parse_ticker("aapl:xnas").ticker, "AAPL")

    def test_unsafe_or_ambiguous_ticker_input_is_rejected(self) -> None:
        for value in ["AAPL?apiKey=leak", "../AAPL", "AAPL:NYSE:EXTRA", ""]:
            with self.subTest(value=value), self.assertRaises(InvalidTickerError):
                parse_ticker(value)

    def test_url_builder_encodes_values_and_redaction_removes_the_entire_key(self) -> None:
        url = build_aggregates_url(
            "brk.b",
            "15m",
            "2026-07-01",
            "2026-07-02",
            API_KEY,
        )
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.hostname, "api.polygon.io")
        self.assertIn("/v2/aggs/ticker/BRK.B/range/15/minute/", parsed.path)
        self.assertEqual(query["apiKey"], [API_KEY])
        self.assertNotIn("admin", query)

        safe = redact_url(url, API_KEY)
        self.assertNotIn(API_KEY, safe)
        self.assertNotIn("unit-secret", safe)
        self.assertIn("apiKey=[redacted]", safe)

    def test_generic_url_builder_rejects_endpoint_injection_and_credentials_in_params(self) -> None:
        with self.assertRaises(ValueError):
            build_polygon_url("https://example.com/v1")
        with self.assertRaises(ValueError):
            build_polygon_url("/v1/../private")
        with self.assertRaises(ValueError):
            build_polygon_url("/v1/test", {"token": "not-allowed"})

    def test_redactor_handles_query_json_and_bearer_shapes(self) -> None:
        message = (
            f'https://api.polygon.io/x?apiKey={API_KEY} '
            f'{{"POLYGON_API_KEY":"{API_KEY}"}} Bearer {API_KEY}'
        )
        safe = redact_sensitive_text(message, API_KEY)
        self.assertNotIn("unit-secret", safe)
        self.assertIn("[redacted]", safe)


class PolygonReferenceAndAggregateTests(unittest.TestCase):
    def test_exact_symbol_resolution_uses_details_endpoint_and_enriches_metadata(self) -> None:
        seen: list[str] = []

        def opener(url: str) -> FakeResponse:
            seen.append(url)
            return FakeResponse(
                {
                    "status": "OK",
                    "results": {
                        "ticker": "AAPL",
                        "name": "Apple Inc.",
                        "market": "stocks",
                        "locale": "us",
                        "primary_exchange": "XNAS",
                        "type": "CS",
                        "active": True,
                        "currency_name": "usd",
                        "composite_figi": "BBG000B9XRY4",
                        "last_updated_utc": "2026-07-10T00:00:00Z",
                        "sic_code": "3571",
                        "sic_description": "Electronic Computers",
                        "homepage_url": "https://www.apple.com/?campaign=reference",
                        "market_cap": 3_200_000_000_000,
                        "weighted_shares_outstanding": 15_100_000_000,
                        "branding": {
                            "icon_url": f"https://api.polygon.io/v1/reference/company-branding/icon?apiKey={API_KEY}",
                            "logo_url": "https://api.polygon.io/v1/reference/company-branding/logo.svg",
                        },
                    },
                }
            )

        result = resolve_symbol("AAPL", API_KEY, opener=opener)
        self.assertEqual(urlsplit(seen[0]).path, "/v3/reference/tickers/AAPL")
        self.assertNotIn("ticker", parse_qs(urlsplit(seen[0]).query))
        self.assertEqual(result.sic_code, "3571")
        self.assertEqual(result.sic_description, "Electronic Computers")
        self.assertEqual(result.homepage_url, "https://www.apple.com/?campaign=reference")
        self.assertEqual(result.market_cap, 3_200_000_000_000)
        self.assertEqual(result.weighted_shares_outstanding, 15_100_000_000)
        self.assertEqual(
            result.branding_icon_url,
            "https://api.polygon.io/v1/reference/company-branding/icon",
        )
        self.assertNotIn(API_KEY, result.branding_icon_url)
        self.assertTrue(result.branding_logo_url.endswith("logo.svg"))
        self.assertEqual(result.icon_url, result.branding_icon_url)
        self.assertEqual(result.logo_url, result.branding_logo_url)
        self.assertEqual(result.to_dict()["sic_code"], "3571")
        self.assertEqual(result.to_dict()["icon_url"], result.branding_icon_url)

    def test_symbol_resolution_uses_reference_endpoint_and_exchange_filter(self) -> None:
        seen: list[str] = []

        def opener(url: str) -> FakeResponse:
            seen.append(url)
            return FakeResponse(
                {
                    "status": "OK",
                    "results": [
                        {
                            "ticker": "AAPL",
                            "name": "Apple Inc.",
                            "market": "stocks",
                            "locale": "us",
                            "primary_exchange": "XNAS",
                            "type": "CS",
                            "active": True,
                            "currency_name": "usd",
                            "composite_figi": "BBG000B9XRY4",
                            "last_updated_utc": "2026-07-10T00:00:00Z",
                        }
                    ],
                }
            )

        result = resolve_symbol("NASDAQ:AAPL", API_KEY, opener=opener)
        self.assertEqual(result.ticker, "AAPL")
        self.assertEqual(result.primary_exchange, "XNAS")
        self.assertEqual(result.name, "Apple Inc.")
        self.assertEqual(result.last_updated.isoformat(), "2026-07-10T00:00:00+00:00")
        self.assertIn("/v3/reference/tickers", seen[0])
        self.assertEqual(parse_qs(urlsplit(seen[0]).query)["ticker"], ["AAPL"])

    def test_symbol_resolution_is_conservative_when_exact_matches_are_ambiguous(self) -> None:
        payload = {
            "status": "OK",
            "results": [
                {
                    "ticker": "TEST",
                    "name": "One",
                    "primary_exchange": "XNAS",
                    "active": True,
                    "composite_figi": "ONE",
                },
                {
                    "ticker": "TEST",
                    "name": "Two",
                    "primary_exchange": "XNAS",
                    "active": True,
                    "composite_figi": "TWO",
                },
            ],
        }
        with self.assertRaises(AmbiguousSymbolError):
            resolve_symbol("NASDAQ:TEST", API_KEY, opener=lambda _url: FakeResponse(payload))

    def test_daily_and_15m_fetches_use_only_requested_aggregate_ranges(self) -> None:
        seen: list[str] = []
        start_time = datetime(2026, 7, 10, 13, 30, tzinfo=timezone.utc)

        def opener(url: str) -> FakeResponse:
            seen.append(url)
            return FakeResponse(
                {
                    "status": "OK",
                    "results": [
                        {
                            "t": epoch_ms(start_time),
                            "o": 100,
                            "h": 103,
                            "l": 99,
                            "c": 102,
                            "v": 1200,
                            "vw": 101.5,
                            "n": 25,
                        },
                        {"t": epoch_ms(start_time + timedelta(minutes=15)), "o": 102, "h": 101},
                    ],
                }
            )

        daily = fetch_daily_bars(
            "AAPL", "2026-07-01", "2026-07-10", API_KEY, opener=opener
        )
        intraday = fetch_15m_bars(
            "AAPL", "2026-07-10", "2026-07-10", API_KEY, opener=opener
        )
        self.assertEqual(len(daily), 1)
        self.assertEqual(daily[0].timeframe, "1d")
        self.assertEqual(daily[0].vwap, 101.5)
        self.assertEqual(intraday[0].timeframe, "15m")
        self.assertIn("/range/1/day/", seen[0])
        self.assertIn("/range/15/minute/", seen[1])

    def test_five_minute_fetch_uses_exact_source_timeframe(self) -> None:
        seen: list[str] = []

        def opener(url: str) -> FakeResponse:
            seen.append(url)
            return FakeResponse(
                {
                    "status": "OK",
                    "results": [
                        {"t": 1_700_000_000_000, "o": 100, "h": 101, "l": 99, "c": 100.5, "v": 10_000}
                    ],
                }
            )

        bars = fetch_5m_bars("AAPL", "2026-07-01", "2026-07-02", "test-key", opener=opener)
        self.assertEqual(bars[0].timeframe, "5m")
        self.assertIn("/range/5/minute/", seen[0])

    def test_aggregate_fetch_rejects_silent_truncation(self) -> None:
        payload = {
            "status": "OK",
            "results": [],
            "next_url": "https://api.polygon.io/v2/aggs/ticker/AAPL?cursor=next",
        }
        with self.assertRaises(PolygonProviderError) as caught:
            fetch_daily_bars(
                "AAPL",
                "2026-07-01",
                "2026-07-02",
                API_KEY,
                opener=lambda _url: FakeResponse(payload),
            )
        self.assertIn("narrower date range", str(caught.exception))

    def test_client_repr_never_displays_the_api_key(self) -> None:
        provider = PolygonProvider(API_KEY, opener=lambda _url: FakeResponse({"status": "OK"}))
        self.assertNotIn(API_KEY, repr(provider))
        self.assertIn("[redacted]", repr(provider))


class PolygonResamplingTests(unittest.TestCase):
    def test_weekly_and_monthly_resampling_preserves_ohlcv_semantics(self) -> None:
        timestamps = [
            datetime(2026, 6, 29, 16, tzinfo=timezone.utc),
            datetime(2026, 6, 30, 16, tzinfo=timezone.utc),
            datetime(2026, 7, 1, 16, tzinfo=timezone.utc),
            datetime(2026, 7, 6, 16, tzinfo=timezone.utc),
        ]
        source = [
            bar(timestamps[0], 10, 12, 9, 11, 100, timeframe="1d"),
            bar(timestamps[1], 11, 13, 10, 12, 200, timeframe="1d"),
            bar(timestamps[2], 12, 14, 11, 13, 300, timeframe="1d"),
            bar(timestamps[3], 13, 15, 12, 14, 400, timeframe="1d"),
        ]

        weekly = resample_bars(source, "weekly")
        self.assertEqual(len(weekly), 2)
        self.assertEqual(weekly[0].open, 10)
        self.assertEqual(weekly[0].high, 14)
        self.assertEqual(weekly[0].low, 9)
        self.assertEqual(weekly[0].close, 13)
        self.assertEqual(weekly[0].volume, 600)
        self.assertEqual(weekly[0].source_count, 3)

        monthly = resample_bars(source, "monthly")
        self.assertEqual(len(monthly), 2)
        self.assertEqual(monthly[0].close, 12)
        self.assertEqual(monthly[0].volume, 300)
        self.assertEqual(monthly[1].open, 12)
        self.assertEqual(monthly[1].close, 14)
        self.assertEqual(monthly[1].volume, 700)

    def test_intraday_resampling_is_anchored_to_new_york_market_open(self) -> None:
        # July is EDT, so 09:30 New York is 13:30 UTC.
        start = datetime(2026, 7, 13, 13, 30, tzinfo=timezone.utc)
        source = [
            bar(
                start + timedelta(minutes=15 * index),
                100 + index,
                101 + index,
                99 + index,
                100.5 + index,
                10 + index,
            )
            for index in range(6)
        ]
        hourly = resample_bars(source, "1h")
        self.assertEqual(len(hourly), 2)
        self.assertEqual(hourly[0].timestamp, start)
        self.assertEqual(hourly[0].open, 100)
        self.assertEqual(hourly[0].close, 103.5)
        self.assertEqual(hourly[0].source_count, 4)
        self.assertEqual(hourly[1].timestamp, start + timedelta(hours=1))

        four_hour = resample_bars(source, "4h")
        self.assertEqual(len(four_hour), 1)
        self.assertEqual(four_hour[0].timestamp, start)
        self.assertEqual(four_hour[0].source_count, 6)


class PolygonStockSnapshotTests(unittest.TestCase):
    def test_stock_snapshot_url_batches_encoded_deduplicated_tickers(self) -> None:
        url = build_stock_snapshots_url("aapl, MSFT aapl", API_KEY)
        parsed = urlsplit(url)
        query = parse_qs(parsed.query)
        self.assertEqual(
            parsed.path,
            "/v2/snapshot/locale/us/markets/stocks/tickers",
        )
        self.assertEqual(query["tickers"], ["AAPL,MSFT"])
        self.assertEqual(query["include_otc"], ["false"])

    def test_batched_watchlist_snapshots_parse_price_change_and_updated_source(self) -> None:
        now = datetime(2026, 7, 13, 15, 0, tzinfo=timezone.utc)
        fixtures = {
            "AAPL": {
                "ticker": "AAPL",
                "todaysChange": 2.5,
                "todaysChangePerc": 1.25,
                "updated": epoch_ns(now),
                "lastTrade": {"p": 202.5, "t": epoch_ns(now - timedelta(seconds=1))},
                "day": {"o": 200, "h": 203, "l": 199, "c": 202, "v": 2_000_000},
                "prevDay": {"c": 200},
            },
            "MSFT": {
                "ticker": "MSFT",
                "todaysChange": -1.5,
                "todaysChangePerc": -0.35,
                "updated": 0,
                "min": {"c": 420.25, "t": epoch_ns(now - timedelta(minutes=1))},
                "day": {"o": 422, "h": 423, "l": 419, "c": 420, "v": 900_000},
                "prevDay": {"c": 421.75},
            },
            "SPY": {
                "ticker": "SPY",
                "day": {"c": 610.5, "t": epoch_ns(now - timedelta(minutes=2))},
                "prevDay": {"c": 608.0},
            },
        }
        seen: list[str] = []

        def opener(url: str) -> FakeResponse:
            seen.append(url)
            requested = parse_qs(urlsplit(url).query)["tickers"][0].split(",")
            return FakeResponse(
                {
                    "status": "OK",
                    "tickers": [fixtures[ticker] for ticker in reversed(requested)]
                    + [{"ticker": "UNREQUESTED", "lastTrade": {"p": 1}}],
                }
            )

        snapshots = fetch_stock_snapshots(
            ["aapl", "MSFT", "AAPL", "SPY"],
            API_KEY,
            opener=opener,
            batch_size=2,
        )
        self.assertEqual(len(seen), 2)
        self.assertEqual([snapshot.ticker for snapshot in snapshots], ["AAPL", "MSFT", "SPY"])

        apple, microsoft, spy = snapshots
        self.assertEqual(apple.price, 202.5)
        self.assertEqual(apple.price_source, "last_trade")
        self.assertEqual(apple.change, 2.5)
        self.assertEqual(apple.change_percent, 1.25)
        self.assertEqual(apple.updated_at, now)
        self.assertEqual(apple.updated_source, "snapshot")
        self.assertEqual(apple.previous_close, 200)
        self.assertEqual(apple.day_volume, 2_000_000)

        self.assertEqual(microsoft.price, 420.25)
        self.assertEqual(microsoft.price_source, "minute_close")
        self.assertEqual(microsoft.updated_source, "minute")
        self.assertEqual(spy.price, 610.5)
        self.assertEqual(spy.price_source, "day_close")
        self.assertEqual(spy.updated_source, "day")

    def test_snapshot_parser_keeps_missing_values_explicit(self) -> None:
        snapshots = parse_stock_snapshots(
            {
                "tickers": [
                    {
                        "ticker": "AAPL",
                        "lastTrade": {"p": -1, "t": 0},
                        "day": {"v": -5},
                    }
                ]
            }
        )
        self.assertEqual(len(snapshots), 1)
        self.assertIsNone(snapshots[0].price)
        self.assertIsNone(snapshots[0].price_source)
        self.assertIsNone(snapshots[0].updated_at)
        self.assertIsNone(snapshots[0].change)
        self.assertIsNone(snapshots[0].day_volume)


class PolygonContextNewsAndOptionsTests(unittest.TestCase):
    def test_market_status_and_news_are_normalized_with_timestamps(self) -> None:
        responses = [
            {
                "market": "open",
                "serverTime": "2026-07-13T14:30:00Z",
                "earlyHours": False,
                "afterHours": False,
                "exchanges": {"nasdaq": "open", "nyse": "open"},
                "currencies": {"fx": "open"},
            },
            {
                "status": "OK",
                "results": [
                    {
                        "id": "article-1",
                        "title": "AAPL product update",
                        "author": "Reporter",
                        "published_utc": "2026-07-13T14:00:00Z",
                        "article_url": "https://news.example/article-1",
                        "description": "Material context.",
                        "tickers": ["AAPL"],
                        "keywords": ["technology"],
                        "publisher": {
                            "name": "Example News",
                            "homepage_url": "https://news.example",
                        },
                    }
                ],
            },
        ]

        def opener(_url: str) -> FakeResponse:
            return FakeResponse(responses.pop(0))

        status = fetch_market_status(API_KEY, opener=opener)
        news = fetch_ticker_news("AAPL", API_KEY, opener=opener)
        self.assertTrue(status.is_open)
        self.assertEqual(status.exchanges["nasdaq"], "open")
        self.assertEqual(status.server_time.isoformat(), "2026-07-13T14:30:00+00:00")
        self.assertEqual(news[0].article_id, "article-1")
        self.assertEqual(news[0].tickers, ("AAPL",))
        self.assertEqual(news[0].publisher_name, "Example News")

    def test_options_snapshot_parses_quotes_greeks_liquidity_iv_and_timestamps(self) -> None:
        payload = {
            "status": "OK",
            "results": [
                {
                    "break_even_price": 195.25,
                    "details": {
                        "ticker": "O:AAPL260821C00190000",
                        "contract_type": "call",
                        "exercise_style": "american",
                        "expiration_date": "2026-08-21",
                        "shares_per_contract": 100,
                        "strike_price": 190,
                    },
                    "greeks": {"delta": 0.58, "gamma": 0.03, "theta": -0.08, "vega": 0.19},
                    "implied_volatility": 0.31,
                    "open_interest": 1520,
                    "day": {"volume": 340, "last_updated": 1_720_000_000_000_000_000},
                    "last_quote": {
                        "bid": 5.1,
                        "ask": 5.4,
                        "midpoint": 5.25,
                        "last_updated": 1_720_000_001_000_000_000,
                        "timeframe": "REAL-TIME",
                    },
                    "last_trade": {"sip_timestamp": 1_720_000_002_000_000_000},
                    "underlying_asset": {
                        "ticker": "AAPL",
                        "last_updated": 1_720_000_003_000_000_000,
                        "timeframe": "REAL-TIME",
                    },
                }
            ],
        }
        contracts = parse_options_chain_snapshot(payload, "AAPL")
        self.assertEqual(len(contracts), 1)
        contract = contracts[0]
        self.assertEqual(contract.option_ticker, "O:AAPL260821C00190000")
        self.assertEqual(contract.contract_type, "call")
        self.assertEqual(contract.bid, 5.1)
        self.assertEqual(contract.ask, 5.4)
        self.assertAlmostEqual(contract.midpoint, 5.25)
        self.assertAlmostEqual(contract.spread, 0.3)
        self.assertAlmostEqual(contract.spread_percent, 0.3 / 5.25 * 100)
        self.assertEqual(contract.delta, 0.58)
        self.assertEqual(contract.gamma, 0.03)
        self.assertEqual(contract.theta, -0.08)
        self.assertEqual(contract.vega, 0.19)
        self.assertEqual(contract.open_interest, 1520)
        self.assertEqual(contract.volume, 340)
        self.assertEqual(contract.implied_volatility, 0.31)
        self.assertIsNotNone(contract.quote_timestamp)
        self.assertIsNotNone(contract.trade_timestamp)
        self.assertIsNotNone(contract.day_timestamp)
        self.assertIsNotNone(contract.underlying_timestamp)

    def test_options_snapshot_drops_contract_for_wrong_underlying(self) -> None:
        payload = {
            "results": [
                {
                    "details": {
                        "ticker": "O:MSFT260821C00190000",
                        "contract_type": "call",
                        "expiration_date": "2026-08-21",
                        "strike_price": 190,
                    },
                    "last_quote": {"bid": 5.1, "ask": 5.4},
                    "underlying_asset": {"ticker": "MSFT"},
                }
            ]
        }
        self.assertEqual(parse_options_chain_snapshot(payload, "AAPL"), [])

    def test_options_fetch_follows_only_polygon_pagination_and_reauthenticates(self) -> None:
        seen: list[str] = []
        base_contract = {
            "details": {
                "ticker": "O:SPY260821P00500000",
                "contract_type": "put",
                "expiration_date": "2026-08-21",
                "strike_price": 500,
            },
            "last_quote": {"bid": 4.0, "ask": 4.4},
            "underlying_asset": {"ticker": "SPY"},
        }

        def opener(url: str) -> FakeResponse:
            seen.append(url)
            if len(seen) == 1:
                return FakeResponse(
                    {
                        "status": "OK",
                        "results": [base_contract],
                        "next_url": "https://api.polygon.io/v3/snapshot/options/SPY?cursor=abc&apiKey=old",
                    }
                )
            return FakeResponse({"status": "OK", "results": []})

        contracts = fetch_options_chain_snapshot(
            "SPY", API_KEY, opener=opener, max_pages=2
        )
        self.assertEqual(len(contracts), 1)
        second_query = parse_qs(urlsplit(seen[1]).query)
        self.assertEqual(second_query["cursor"], ["abc"])
        self.assertEqual(second_query["apiKey"], [API_KEY])
        self.assertNotIn("old", seen[1])

    def test_options_fetch_rejects_non_polygon_pagination(self) -> None:
        payload = {
            "status": "OK",
            "results": [],
            "next_url": "https://evil.example/steal",
        }
        with self.assertRaises(PolygonProviderError):
            fetch_options_chain_snapshot(
                "SPY", API_KEY, opener=lambda _url: FakeResponse(payload)
            )

    def test_options_fetch_reports_incomplete_page_limited_chain(self) -> None:
        payload = {
            "status": "OK",
            "results": [],
            "next_url": "https://api.polygon.io/v3/snapshot/options/SPY?cursor=more",
        }
        with self.assertRaises(PolygonProviderError) as caught:
            fetch_options_chain_snapshot(
                "SPY",
                API_KEY,
                opener=lambda _url: FakeResponse(payload),
                max_pages=1,
            )
        self.assertIn("may be incomplete", str(caught.exception))


class PolygonFailureSafetyTests(unittest.TestCase):
    def test_http_error_message_does_not_echo_url_body_or_api_key(self) -> None:
        def opener(url: str) -> FakeResponse:
            raise HTTPError(
                url=url,
                code=401,
                msg=f"bad apiKey={API_KEY}",
                hdrs={},
                fp=None,
            )

        with self.assertRaises(PolygonProviderError) as caught:
            fetch_daily_bars(
                "AAPL", "2026-07-01", "2026-07-02", API_KEY, opener=opener
            )
        message = str(caught.exception)
        self.assertIn("HTTP 401", message)
        self.assertIn("unauthorized", message)
        self.assertNotIn(API_KEY, message)
        self.assertNotIn("unit-secret", message)

    def test_provider_error_payload_is_redacted(self) -> None:
        payload = {
            "status": "ERROR",
            "error": f"invalid POLYGON_API_KEY={API_KEY}",
        }
        with self.assertRaises(PolygonProviderError) as caught:
            fetch_market_status(API_KEY, opener=lambda _url: FakeResponse(payload))
        message = str(caught.exception)
        self.assertNotIn(API_KEY, message)
        self.assertNotIn("unit-secret", message)
        self.assertIn("[redacted]", message)


if __name__ == "__main__":
    unittest.main()
