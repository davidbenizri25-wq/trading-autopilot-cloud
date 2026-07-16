from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from autopilot_service import CacheCoalescingTimeoutError, TTLCache
from polygon_provider import (
    PolygonProviderError,
    _request_json,
    clear_provider_observations,
    fetch_earnings_context,
    recent_provider_observations,
)


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class ProviderRetryAndObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        clear_provider_observations()

    def test_429_retry_after_is_honored_then_success_is_observable(self) -> None:
        clock = FakeClock()
        calls = 0

        def opener(url: str) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPError(
                    url,
                    429,
                    "rate limited",
                    {"Retry-After": "1.5"},
                    None,
                )
            return FakeResponse({"status": "OK", "results": []})

        payload = _request_json(
            "https://api.polygon.io/test?apiKey=do-not-observe",
            api_key="do-not-observe",
            opener=opener,
            operation="stress probe",
            sleep=clock.sleep,
            clock=clock,
        )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(calls, 2)
        self.assertEqual(clock.sleeps, [1.5])
        observation = recent_provider_observations(1)[0]
        self.assertEqual(observation["outcome"], "success")
        self.assertEqual(observation["attempts"], 2)
        self.assertEqual(observation["retries"], 1)
        self.assertTrue(observation["throttled"])
        self.assertEqual(observation["retry_after_seconds"], 1.5)
        self.assertEqual(observation["latency_ms"], 1500.0)
        self.assertNotIn("do-not-observe", repr(observation))

    def test_transient_503_uses_bounded_exponential_backoff(self) -> None:
        clock = FakeClock()
        calls = 0

        def opener(url: str) -> FakeResponse:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise HTTPError(url, 503, "unavailable", None, None)
            return FakeResponse({"status": "OK"})

        _request_json(
            "https://api.polygon.io/test",
            api_key="key",
            opener=opener,
            operation="stress probe",
            sleep=clock.sleep,
            clock=clock,
        )

        self.assertEqual(calls, 3)
        self.assertEqual(clock.sleeps, [0.25, 0.5])
        observation = recent_provider_observations(1)[0]
        self.assertEqual(observation["attempts"], 3)
        self.assertEqual(observation["latency_ms"], 750.0)
        self.assertFalse(observation["throttled"])

    def test_exhausted_503_reports_attempts_and_availability(self) -> None:
        clock = FakeClock()
        calls = 0

        def opener(url: str) -> FakeResponse:
            nonlocal calls
            calls += 1
            raise HTTPError(url, 503, "unavailable", None, None)

        with self.assertRaises(PolygonProviderError) as raised:
            _request_json(
                "https://api.polygon.io/test",
                api_key="key",
                opener=opener,
                operation="stress probe",
                sleep=clock.sleep,
                clock=clock,
            )

        self.assertEqual(calls, 3)
        self.assertEqual(clock.sleeps, [0.25, 0.5])
        self.assertEqual(raised.exception.classification, "availability")
        self.assertEqual(raised.exception.attempts, 3)
        self.assertEqual(raised.exception.to_dict()["retries"], 2)

    def test_default_transport_timeout_is_capped_at_eight_seconds(self) -> None:
        clock = FakeClock()
        observed_timeouts: list[float] = []

        def fake_urlopen(_url: object, *, timeout: float) -> FakeResponse:
            observed_timeouts.append(timeout)
            return FakeResponse({"status": "OK"})

        with patch("polygon_provider.urlopen", side_effect=fake_urlopen):
            payload = _request_json(
                "https://api.polygon.io/test",
                api_key="key",
                opener=None,
                operation="stress probe",
                transport_timeout_seconds=99,
                sleep=clock.sleep,
                clock=clock,
            )

        self.assertEqual(payload["status"], "OK")
        self.assertEqual(observed_timeouts, [8.0])

    def test_transport_retries_share_one_fifteen_second_budget(self) -> None:
        clock = FakeClock()
        observed_timeouts: list[float] = []

        def fake_urlopen(_url: object, *, timeout: float) -> FakeResponse:
            observed_timeouts.append(timeout)
            clock.now += timeout
            raise TimeoutError("fixture timeout")

        with patch("polygon_provider.urlopen", side_effect=fake_urlopen):
            with self.assertRaises(PolygonProviderError) as raised:
                _request_json(
                    "https://api.polygon.io/test",
                    api_key="key",
                    opener=None,
                    operation="stress probe",
                    max_retries=5,
                    request_budget_seconds=99,
                    sleep=clock.sleep,
                    clock=clock,
                )

        self.assertEqual(observed_timeouts, [8.0, 6.75])
        self.assertEqual(clock.sleeps, [0.25])
        self.assertEqual(clock.now, 115.0)
        self.assertEqual(raised.exception.classification, "timeout")
        self.assertEqual(raised.exception.attempts, 2)

    def test_retry_delay_that_would_consume_budget_is_not_slept(self) -> None:
        clock = FakeClock()
        calls = 0

        def opener(url: str) -> FakeResponse:
            nonlocal calls
            calls += 1
            raise HTTPError(url, 503, "unavailable", None, None)

        with self.assertRaises(PolygonProviderError) as raised:
            _request_json(
                "https://api.polygon.io/test",
                api_key="key",
                opener=opener,
                operation="stress probe",
                max_retries=5,
                request_budget_seconds=0.2,
                sleep=clock.sleep,
                clock=clock,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(raised.exception.classification, "availability")

    def test_long_server_cooldown_is_surfaced_without_blocking(self) -> None:
        clock = FakeClock()
        calls = 0

        def opener(url: str) -> FakeResponse:
            nonlocal calls
            calls += 1
            raise HTTPError(url, 429, "rate limited", {"Retry-After": "60"}, None)

        with self.assertRaises(PolygonProviderError) as raised:
            _request_json(
                "https://api.polygon.io/test",
                api_key="key",
                opener=opener,
                operation="stress probe",
                sleep=clock.sleep,
                clock=clock,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(raised.exception.classification, "throttling")
        self.assertEqual(raised.exception.retry_after_seconds, 60.0)
        self.assertTrue(raised.exception.retryable)

    def test_429_without_retry_after_does_not_spend_more_quota(self) -> None:
        clock = FakeClock()
        calls = 0

        def opener(url: str) -> FakeResponse:
            nonlocal calls
            calls += 1
            raise HTTPError(url, 429, "rate limited", None, None)

        with self.assertRaises(PolygonProviderError) as raised:
            _request_json(
                "https://api.polygon.io/test",
                api_key="key",
                opener=opener,
                operation="stress probe",
                sleep=clock.sleep,
                clock=clock,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(clock.sleeps, [])
        self.assertEqual(raised.exception.classification, "throttling")
        self.assertEqual(raised.exception.retry_after_seconds, 60.0)

    def test_429_opens_a_shared_provider_cooldown(self) -> None:
        clock = FakeClock()
        calls = 0

        def opener(url: str) -> FakeResponse:
            nonlocal calls
            calls += 1
            raise HTTPError(url, 429, "rate limited", None, None)

        with self.assertRaises(PolygonProviderError):
            _request_json(
                "https://api.polygon.io/first",
                api_key="key",
                opener=opener,
                operation="first probe",
                sleep=clock.sleep,
                clock=clock,
            )
        with self.assertRaises(PolygonProviderError) as blocked:
            _request_json(
                "https://api.polygon.io/second",
                api_key="key",
                opener=opener,
                operation="second probe",
                sleep=clock.sleep,
                clock=clock,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(blocked.exception.classification, "throttling")
        self.assertEqual(blocked.exception.retry_after_seconds, 60.0)
        self.assertEqual(recent_provider_observations(1)[0]["outcome"], "circuit_open")

    def test_simultaneous_429_is_coordinated_before_a_second_transport_call(self) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def opener(url: str) -> FakeResponse:
            nonlocal calls
            with calls_lock:
                calls += 1
            first_entered.set()
            self.assertTrue(release_first.wait(timeout=2))
            raise HTTPError(url, 429, "rate limited", {"Retry-After": "60"}, None)

        def request(operation: str) -> str:
            try:
                _request_json(
                    f"https://api.polygon.io/{operation}",
                    api_key="key",
                    opener=opener,
                    operation=operation,
                )
            except PolygonProviderError as exc:
                return exc.classification
            return "unexpected-success"

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(request, "first")
            self.assertTrue(first_entered.wait(timeout=2))
            second = executor.submit(request, "second")
            time.sleep(0.05)
            release_first.set()
            results = [first.result(timeout=2), second.result(timeout=2)]

        self.assertEqual(results, ["throttling", "throttling"])
        self.assertEqual(calls, 1)
        coordinated = recent_provider_observations(1)[0]
        self.assertEqual(coordinated["outcome"], "circuit_open")
        self.assertGreaterEqual(coordinated["latency_ms"], 40.0)

    def test_observation_reset_can_preserve_an_active_provider_cooldown(self) -> None:
        clock = FakeClock()
        calls = 0

        def opener(url: str) -> FakeResponse:
            nonlocal calls
            calls += 1
            raise HTTPError(url, 429, "rate limited", None, None)

        with self.assertRaises(PolygonProviderError):
            _request_json(
                "https://api.polygon.io/first",
                api_key="key",
                opener=opener,
                operation="first probe",
                sleep=clock.sleep,
                clock=clock,
            )
        clear_provider_observations(preserve_cooldowns=True)
        self.assertEqual(recent_provider_observations(), [])

        with self.assertRaises(PolygonProviderError) as blocked:
            _request_json(
                "https://api.polygon.io/second",
                api_key="key",
                opener=opener,
                operation="second probe",
                sleep=clock.sleep,
                clock=clock,
            )

        self.assertEqual(calls, 1)
        self.assertEqual(blocked.exception.classification, "throttling")
        self.assertEqual(recent_provider_observations(1)[0]["outcome"], "circuit_open")

    def test_massive_earnings_403_is_entitlement_not_availability(self) -> None:
        calls = 0

        def opener(request: object) -> FakeResponse:
            nonlocal calls
            calls += 1
            url = getattr(request, "full_url", str(request))
            raise HTTPError(url, 403, "forbidden", None, None)

        result = fetch_earnings_context(
            "AAPL",
            "2026-07-13",
            "2026-07-23",
            "key",
            opener=opener,
        )

        self.assertEqual(calls, 1)
        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.error_kind, "entitlement")
        self.assertEqual(result.status_code, 403)
        self.assertEqual(result.attempts, 1)
        self.assertFalse(result.throttled)
        observation = recent_provider_observations(1)[0]
        self.assertEqual(observation["provider"], "Massive")
        self.assertEqual(observation["classification"], "entitlement")

    def test_massive_payload_auth_error_remains_authentication(self) -> None:
        def opener(_request: object) -> FakeResponse:
            return FakeResponse({"status": "AUTH_ERROR", "error": "invalid credential"})

        result = fetch_earnings_context(
            "AAPL",
            "2026-07-13",
            "2026-07-23",
            "key",
            opener=opener,
        )

        self.assertEqual(result.status, "unresolved")
        self.assertEqual(result.error_kind, "authentication")
        self.assertEqual(recent_provider_observations(1)[0]["classification"], "authentication")


class CacheObservationTests(unittest.TestCase):
    def test_cache_snapshot_counts_hits_misses_expiration_and_loads(self) -> None:
        cache = TTLCache(ttl_seconds=10, max_items=2)
        monotonic_values = iter([0.0, 0.0, 1.0, 11.0, 11.0])

        import autopilot_service

        original_monotonic = autopilot_service.time.monotonic
        autopilot_service.time.monotonic = lambda: next(monotonic_values)
        try:
            self.assertEqual(cache.get_or_create("x", lambda: 1), 1)
            self.assertEqual(cache.get_or_create("x", lambda: 2), 1)
            self.assertEqual(cache.get_or_create("x", lambda: 3), 3)
        finally:
            autopilot_service.time.monotonic = original_monotonic

        self.assertEqual(
            cache.snapshot(),
            {
                "ttl_seconds": 10,
                "max_items": 2,
                "items": 1,
                "hits": 1,
                "misses": 2,
                "loads": 2,
                "load_errors": 0,
                "expirations": 1,
                "evictions": 0,
                "coalesced_waits": 0,
                "coalesced_timeouts": 0,
            },
        )

    def test_cache_snapshot_counts_factory_errors_without_values(self) -> None:
        cache = TTLCache()

        def fail() -> object:
            raise RuntimeError("fixture")

        with self.assertRaises(RuntimeError):
            cache.get_or_create("x", fail)

        snapshot = cache.snapshot()
        self.assertEqual(snapshot["misses"], 1)
        self.assertEqual(snapshot["load_errors"], 1)
        self.assertEqual(snapshot["loads"], 0)
        self.assertEqual(snapshot["items"], 0)

    def test_concurrent_identical_misses_share_one_provider_load(self) -> None:
        cache = TTLCache(ttl_seconds=30, max_items=4)
        workers = 5
        start = threading.Barrier(workers + 1)
        factory_entered = threading.Event()
        release_factory = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def factory() -> dict[str, int]:
            nonlocal calls
            with calls_lock:
                calls += 1
            factory_entered.set()
            self.assertTrue(release_factory.wait(timeout=2))
            return {"value": 7}

        def worker() -> dict[str, int]:
            start.wait(timeout=2)
            return cache.get_or_create("shared", factory)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker) for _ in range(workers)]
            start.wait(timeout=2)
            self.assertTrue(factory_entered.wait(timeout=2))
            time.sleep(0.05)
            release_factory.set()
            results = [future.result(timeout=2) for future in futures]

        self.assertEqual(calls, 1)
        self.assertEqual(results, [{"value": 7}] * workers)
        snapshot = cache.snapshot()
        self.assertEqual(snapshot["misses"], 1)
        self.assertEqual(snapshot["loads"], 1)
        self.assertEqual(snapshot["hits"], workers - 1)
        self.assertEqual(snapshot["coalesced_waits"], workers - 1)

    def test_coalesced_wait_is_bounded_and_leader_still_completes(self) -> None:
        cache = TTLCache(ttl_seconds=30, wait_timeout_seconds=0.05)
        factory_entered = threading.Event()
        release_factory = threading.Event()

        def factory() -> int:
            factory_entered.set()
            self.assertTrue(release_factory.wait(timeout=2))
            return 7

        with ThreadPoolExecutor(max_workers=2) as executor:
            leader = executor.submit(cache.get_or_create, "shared", factory)
            self.assertTrue(factory_entered.wait(timeout=2))
            follower = executor.submit(cache.get_or_create, "shared", factory)
            with self.assertRaises(CacheCoalescingTimeoutError):
                follower.result(timeout=1)
            release_factory.set()
            self.assertEqual(leader.result(timeout=2), 7)
        self.assertEqual(cache.snapshot()["coalesced_timeouts"], 1)

    def test_base_exception_leader_notifies_all_coalesced_waiters(self) -> None:
        cache = TTLCache(ttl_seconds=30, wait_timeout_seconds=1)
        workers = 3
        start = threading.Barrier(workers + 1)
        factory_entered = threading.Event()
        release_factory = threading.Event()
        calls = 0
        calls_lock = threading.Lock()

        def factory() -> int:
            nonlocal calls
            with calls_lock:
                calls += 1
            factory_entered.set()
            self.assertTrue(release_factory.wait(timeout=2))
            raise KeyboardInterrupt("fixture leader cancellation")

        def worker() -> int:
            start.wait(timeout=2)
            return cache.get_or_create("shared", factory)

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(worker) for _ in range(workers)]
            start.wait(timeout=2)
            self.assertTrue(factory_entered.wait(timeout=2))
            time.sleep(0.05)
            release_factory.set()
            for future in futures:
                with self.assertRaises(KeyboardInterrupt):
                    future.result(timeout=2)

        self.assertEqual(calls, 1)
        self.assertEqual(cache.snapshot()["load_errors"], 1)


if __name__ == "__main__":
    unittest.main()
