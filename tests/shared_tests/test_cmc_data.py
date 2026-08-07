from __future__ import annotations

from datetime import datetime, timezone
from email.utils import format_datetime
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import requests

from shared.cmc_data import (
    CACHE_SCHEMA_VERSION,
    CmcClient,
    CmcClientError,
)


NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc).timestamp()
NOW_ISO = "2026-08-07T12:00:00Z"


def envelope(data: object, *, timestamp: str = NOW_ISO) -> dict[str, object]:
    return {
        "status": {"timestamp": timestamp, "error_code": 0, "error_message": None},
        "data": data,
    }


def map_item(cmc_id: int, symbol: str) -> dict[str, object]:
    return {
        "id": cmc_id,
        "name": f"{symbol} Coin",
        "symbol": symbol,
        "slug": f"{symbol.lower()}-coin",
        "is_active": 1,
        "platform": {
            "name": "Ethereum",
            "symbol": "ETH",
            "slug": "ethereum",
            "token_address": f"0x{cmc_id:040x}",
        },
    }


def quote_item(
    cmc_id: int,
    symbol: str,
    market_cap: object = 12_345_678.0,
    *,
    updated_at: str = NOW_ISO,
) -> dict[str, object]:
    return {
        "id": cmc_id,
        "name": f"{symbol} Coin",
        "symbol": symbol,
        "slug": f"{symbol.lower()}-coin",
        "last_updated": updated_at,
        "quote": {
            "USD": {
                "market_cap": market_cap,
                "fully_diluted_market_cap": 999_999_999,
                "last_updated": updated_at,
            }
        },
    }


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: object,
        *,
        headers: dict[str, str] | None = None,
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.payload = payload
        self.headers = headers or {}
        self.json_error = json_error

    def json(self) -> object:
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []
        self.closed = False

    def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response  # type: ignore[return-value]

    def close(self) -> None:
        self.closed = True


class CmcClientTests(unittest.TestCase):
    def client(
        self,
        cache_path: Path,
        responses: list[object],
        **kwargs: object,
    ) -> tuple[CmcClient, FakeSession, list[float]]:
        session = FakeSession(responses)
        sleeps: list[float] = []
        kwargs.setdefault("min_request_interval_sec", 0)
        client = CmcClient(
            api_key="fake-test-key",
            cache_path=cache_path,
            session=session,  # type: ignore[arg-type]
            sleep_fn=sleeps.append,
            random_fn=lambda: 0.5,
            clock=lambda: NOW,
            **kwargs,
        )
        return client, session, sleeps

    def test_map_is_paginated_merged_and_cached_atomically(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            client, session, _ = self.client(
                cache_path,
                [
                    FakeResponse(200, envelope([map_item(1, "AAA"), map_item(2, "BBB")])),
                    FakeResponse(200, envelope([map_item(3, "CCC")])),
                ],
                map_page_size=2,
            )

            result = client.load_map()
            cached = client.load_map()

            self.assertEqual([entry.cmc_id for entry in result.entries], [1, 2, 3])
            self.assertEqual(result.entries[0].token_address, "0x0000000000000000000000000000000000000001")
            self.assertEqual(result.request_pages, 2)
            self.assertEqual(cached.source, "cache")
            self.assertEqual(len(session.calls), 2)
            self.assertEqual(session.calls[0]["params"], {
                "listing_status": "active",
                "start": 1,
                "limit": 2,
                "aux": "platform",
            })
            self.assertEqual(session.calls[1]["params"], {
                "listing_status": "active",
                "start": 3,
                "limit": 2,
                "aux": "platform",
            })
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(document["schema_version"], CACHE_SCHEMA_VERSION)
            self.assertEqual(document["map"]["generated_at"], NOW_ISO)
            self.assertEqual(document["map"]["data_updated_at"], NOW_ISO)
            self.assertEqual(len(document["map"]["entries"]), 3)

    def test_full_last_map_page_hits_finite_protocol_guard(self) -> None:
        with TemporaryDirectory() as tmp:
            client, _, _ = self.client(
                Path(tmp) / "cmc.json",
                [FakeResponse(200, envelope([map_item(1, "AAA")]))],
                map_page_size=1,
                max_map_pages=1,
            )

            with self.assertRaisesRegex(CmcClientError, "protocol_error"):
                client.load_map()

            self.assertFalse((Path(tmp) / "cmc.json").exists())

    def test_duplicate_id_across_map_pages_rejects_the_incomplete_catalogue(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            client, _, _ = self.client(
                cache_path,
                [
                    FakeResponse(200, envelope([map_item(1, "AAA")])),
                    FakeResponse(200, envelope([map_item(1, "AAA")])),
                ],
                map_page_size=1,
                max_map_pages=2,
            )

            with self.assertRaises(CmcClientError) as caught:
                client.load_map()

            self.assertEqual(caught.exception.kind, "protocol_error")
            self.assertFalse(cache_path.exists())

    def test_quotes_dict_response_uses_only_ids_and_circulating_market_cap(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            payload = envelope({
                "1": quote_item(1, "AAA", 12_000_000),
                "2": quote_item(2, "BBB", 23_000_000),
            })
            client, session, _ = self.client(cache_path, [FakeResponse(200, payload)])

            result = client.quotes_latest([2, 1, 2])

            self.assertEqual(result.quotes[1].market_cap_usd, 12_000_000)
            self.assertEqual(result.quotes[2].last_updated, NOW_ISO)
            self.assertEqual(result.source_by_id, {1: "network", 2: "network"})
            self.assertEqual(session.calls[0]["params"], {
                "id": "1,2",
                "convert": "USD",
                "skip_invalid": "true",
            })
            self.assertNotIn("symbol", session.calls[0]["params"])
            self.assertEqual(session.calls[0]["timeout"], (5.0, 15.0))
            self.assertEqual(session.calls[0]["headers"]["X-CMC_PRO_API_KEY"], "fake-test-key")
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(cache["quotes"]["generated_at"], NOW_ISO)
            self.assertEqual(cache["quotes"]["data_updated_at"], NOW_ISO)
            self.assertIn("expires_at", cache["quotes"])
            self.assertEqual(cache["quotes"]["entries"]["1"]["data_updated_at"], NOW_ISO)

    def test_quotes_list_response_is_supported(self) -> None:
        with TemporaryDirectory() as tmp:
            client, _, _ = self.client(
                Path(tmp) / "cmc.json",
                [FakeResponse(200, envelope([quote_item(1, "AAA"), quote_item(2, "BBB")]))],
            )

            result = client.quotes_latest([1, 2])

            self.assertEqual(tuple(result.quotes), (1, 2))

    def test_quote_batches_never_exceed_one_hundred_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            requested = list(range(1, 206))
            responses = []
            for batch in (requested[:100], requested[100:200], requested[200:]):
                responses.append(FakeResponse(200, envelope([quote_item(value, f"C{value}") for value in batch])))
            client, session, _ = self.client(Path(tmp) / "cmc.json", responses, batch_size=100)

            result = client.quotes_latest(reversed(requested))

            self.assertEqual(len(result.quotes), 205)
            self.assertEqual(result.request_batches, 3)
            sizes = [len(str(call["params"]["id"]).split(",")) for call in session.calls]
            self.assertEqual(sizes, [100, 100, 5])

    def test_quotes_require_positive_integer_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            client, session, _ = self.client(Path(tmp) / "cmc.json", [])
            for values in ([0], [-1], [True], ["1"]):
                with self.subTest(values=values):
                    with self.assertRaises(ValueError):
                        client.quotes_latest(values)  # type: ignore[arg-type]
            self.assertEqual(session.calls, [])

    def test_response_key_and_object_id_must_match(self) -> None:
        with TemporaryDirectory() as tmp:
            client, _, _ = self.client(
                Path(tmp) / "cmc.json",
                [FakeResponse(200, envelope({"1": quote_item(2, "BBB")}))],
            )

            with self.assertRaisesRegex(CmcClientError, "protocol_error"):
                client.quotes_latest([1])
            self.assertEqual(client.diagnostics()["last_error"], "protocol_error")

    def test_response_ids_must_be_a_subset_of_requested_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            client, _, _ = self.client(
                Path(tmp) / "cmc.json",
                [FakeResponse(200, envelope([quote_item(2, "BBB")]))],
            )

            with self.assertRaisesRegex(CmcClientError, "protocol_error"):
                client.quotes_latest([1])

    def test_missing_response_id_is_reported_without_guessing(self) -> None:
        with TemporaryDirectory() as tmp:
            client, _, _ = self.client(
                Path(tmp) / "cmc.json",
                [FakeResponse(200, envelope([quote_item(1, "AAA")]))],
            )

            result = client.quotes_latest([1, 2])

            self.assertEqual(tuple(result.quotes), (1,))
            self.assertEqual(result.missing_ids, (2,))

    def test_invalid_market_cap_is_never_coerced_to_zero_or_nan(self) -> None:
        with TemporaryDirectory() as tmp:
            for value in (0, -1, float("nan"), float("inf"), None):
                with self.subTest(value=value):
                    cache_path = Path(tmp) / f"{repr(value)}.json"
                    client, _, _ = self.client(
                        cache_path,
                        [FakeResponse(200, envelope([quote_item(1, "AAA", value)]))],
                    )
                    result = client.quotes_latest([1])
                    self.assertIsNone(result.quotes[1].market_cap_usd)
                    self.assertNotIn("NaN", cache_path.read_text(encoding="utf-8"))
                    self.assertNotIn("Infinity", cache_path.read_text(encoding="utf-8"))

    def test_current_v3_quote_array_extracts_only_usd_market_cap(self) -> None:
        with TemporaryDirectory() as tmp:
            item = quote_item(1, "AAA", 123.45)
            usd = dict(item["quote"]["USD"])
            usd["symbol"] = "USD"
            item["quote"] = [
                {"symbol": "EUR", "market_cap": 999, "last_updated": NOW_ISO},
                usd,
            ]
            client, _, _ = self.client(
                Path(tmp) / "cmc.json",
                [FakeResponse(200, envelope([item]))],
            )

            result = client.quotes_latest([1])

            self.assertEqual(result.quotes[1].market_cap_usd, 123.45)

    def test_authentication_and_authorization_errors_are_distinct_and_not_retried(self) -> None:
        with TemporaryDirectory() as tmp:
            cases = ((401, "authentication_error"), (403, "authorization_error"))
            for status_code, kind in cases:
                with self.subTest(status_code=status_code):
                    client, session, sleeps = self.client(
                        Path(tmp) / f"{status_code}.json",
                        [FakeResponse(status_code, {"secret": "must-not-appear"})],
                    )
                    with self.assertRaises(CmcClientError) as caught:
                        client.load_map()
                    self.assertEqual(caught.exception.kind, kind)
                    self.assertEqual(len(session.calls), 1)
                    self.assertEqual(sleeps, [])
                    self.assertNotIn("must-not-appear", str(caught.exception))

    def test_authentication_errors_are_never_hidden_by_fresh_fallback_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            map_cache = Path(tmp) / "map.json"
            first_map, _, _ = self.client(
                map_cache,
                [FakeResponse(200, envelope([map_item(1, "AAA")]))],
                cache_ttl_sec=10,
            )
            first_map.load_map()
            cached_map = CmcClient(
                api_key="expired-key",
                cache_path=map_cache,
                cache_ttl_sec=10,
                max_data_age_sec=60,
                retries=0,
                min_request_interval_sec=0,
                session=FakeSession([FakeResponse(401, {})]),  # type: ignore[arg-type]
                clock=lambda: NOW + 20,
            )
            with self.assertRaises(CmcClientError) as map_error:
                cached_map.load_map()
            self.assertEqual(map_error.exception.kind, "authentication_error")

            quote_cache = Path(tmp) / "quotes.json"
            first_quote, _, _ = self.client(
                quote_cache,
                [FakeResponse(200, envelope({"1": quote_item(1, "AAA")}))],
                cache_ttl_sec=10,
            )
            first_quote.quotes_latest([1])
            cached_quote = CmcClient(
                api_key="forbidden-key",
                cache_path=quote_cache,
                cache_ttl_sec=10,
                max_data_age_sec=60,
                retries=0,
                min_request_interval_sec=0,
                session=FakeSession([FakeResponse(403, {})]),  # type: ignore[arg-type]
                clock=lambda: NOW + 20,
            )
            with self.assertRaises(CmcClientError) as quote_error:
                cached_quote.quotes_latest([1])
            self.assertEqual(quote_error.exception.kind, "authorization_error")

    def test_successful_batches_obey_the_configured_proactive_request_interval(self) -> None:
        with TemporaryDirectory() as tmp:
            client, session, sleeps = self.client(
                Path(tmp) / "cmc.json",
                [
                    FakeResponse(200, envelope([quote_item(1, "AAA")])),
                    FakeResponse(200, envelope([quote_item(2, "BBB")])),
                    FakeResponse(200, envelope([quote_item(3, "CCC")])),
                ],
                batch_size=1,
                min_request_interval_sec=2.0,
            )

            result = client.quotes_latest([1, 2, 3])

            self.assertEqual(tuple(result.quotes), (1, 2, 3))
            self.assertEqual(len(session.calls), 3)
            self.assertEqual(sleeps, [2.0, 2.0])
            self.assertEqual(client.diagnostics()["rate_limit_waits"], 2)

    def test_retry_after_seconds_is_respected(self) -> None:
        with TemporaryDirectory() as tmp:
            client, _, sleeps = self.client(
                Path(tmp) / "cmc.json",
                [
                    FakeResponse(429, {}, headers={"Retry-After": "7"}),
                    FakeResponse(200, envelope([])),
                ],
                retries=1,
            )

            result = client.load_map()

            self.assertEqual(result.entries, ())
            self.assertEqual(sleeps, [7.0])

    def test_retry_after_http_date_is_respected(self) -> None:
        with TemporaryDirectory() as tmp:
            retry_at = format_datetime(datetime.fromtimestamp(NOW + 9, tz=timezone.utc), usegmt=True)
            client, _, sleeps = self.client(
                Path(tmp) / "cmc.json",
                [
                    FakeResponse(429, {}, headers={"Retry-After": retry_at}),
                    FakeResponse(200, envelope([])),
                ],
                retries=1,
            )

            client.load_map()

            self.assertEqual(sleeps, [9.0])

    def test_long_retry_after_fails_closed_without_blocking_or_retrying(self) -> None:
        with TemporaryDirectory() as tmp:
            client, session, sleeps = self.client(
                Path(tmp) / "cmc.json",
                [FakeResponse(429, {}, headers={"Retry-After": "3600"})],
                retries=2,
            )

            with self.assertRaises(CmcClientError) as caught:
                client.load_map()

            self.assertEqual(caught.exception.kind, "rate_limit_error")
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(sleeps, [])

    def test_http_429_monthly_credit_exhaustion_is_not_retried_as_short_rate_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            payload = {
                "status": {"error_code": 1008, "error_message": "hidden"},
                "data": None,
            }
            client, session, sleeps = self.client(
                Path(tmp) / "cmc.json",
                [FakeResponse(429, payload, headers={"Retry-After": "7"})],
                retries=2,
            )

            with self.assertRaises(CmcClientError) as caught:
                client.load_map()

            self.assertEqual(caught.exception.kind, "credit_exhausted_error")
            self.assertEqual(caught.exception.status_code, 429)
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(sleeps, [])

    def test_server_errors_use_exponential_backoff_and_jitter(self) -> None:
        with TemporaryDirectory() as tmp:
            client, session, sleeps = self.client(
                Path(tmp) / "cmc.json",
                [
                    FakeResponse(500, {}),
                    FakeResponse(503, {}),
                    FakeResponse(200, envelope([])),
                ],
                retries=2,
                backoff_base_sec=2,
            )

            client.load_map()

            self.assertEqual(len(session.calls), 3)
            self.assertEqual(sleeps, [3.0, 5.0])

    def test_network_and_protocol_errors_are_distinct(self) -> None:
        with TemporaryDirectory() as tmp:
            network_client, _, _ = self.client(
                Path(tmp) / "network.json",
                [requests.Timeout("do-not-log-this")],
                retries=0,
            )
            with self.assertRaises(CmcClientError) as network_error:
                network_client.load_map()
            self.assertEqual(network_error.exception.kind, "network_error")
            self.assertNotIn("do-not-log-this", str(network_error.exception))

            protocol_client, _, _ = self.client(
                Path(tmp) / "protocol.json",
                [FakeResponse(200, None, json_error=ValueError("response-body-secret"))],
            )
            with self.assertRaises(CmcClientError) as protocol_error:
                protocol_client.load_map()
            self.assertEqual(protocol_error.exception.kind, "protocol_error")
            self.assertNotIn("response-body-secret", repr(protocol_error.exception))

    def test_http_200_cmc_status_errors_are_classified(self) -> None:
        with TemporaryDirectory() as tmp:
            cases = (
                (1001, "authentication_error"),
                (1006, "authorization_error"),
                (1007, "rate_limit_error"),
                (1008, "credit_exhausted_error"),
            )
            for code, expected_kind in cases:
                with self.subTest(code=code):
                    payload = {"status": {"error_code": code, "error_message": "hidden"}, "data": None}
                    client, _, _ = self.client(
                        Path(tmp) / f"{code}.json",
                        [FakeResponse(200, payload)],
                        retries=0,
                    )
                    with self.assertRaises(CmcClientError) as caught:
                        client.load_map()
                    self.assertEqual(caught.exception.kind, expected_kind)
                    self.assertNotIn("hidden", str(caught.exception))

    def test_cache_only_never_uses_network_and_rejects_expired_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            client, session, _ = self.client(
                cache_path,
                [FakeResponse(200, envelope({"1": quote_item(1, "AAA")}))],
                cache_ttl_sec=10,
            )
            client.quotes_latest([1])

            later_session = FakeSession([])
            later = CmcClient(
                api_key=None,
                cache_path=cache_path,
                cache_ttl_sec=10,
                max_data_age_sec=300,
                session=later_session,  # type: ignore[arg-type]
                clock=lambda: NOW + 11,
            )
            result = later.quotes_latest([1], cache_only=True)

            self.assertEqual(result.quotes, {})
            self.assertEqual(tuple(result.stale_quotes), (1,))
            self.assertEqual(result.missing_ids, (1,))
            self.assertEqual(len(session.calls), 1)
            self.assertEqual(later_session.calls, [])

    def test_fresh_data_can_fallback_after_cache_ttl_on_temporary_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            first, _, _ = self.client(
                cache_path,
                [FakeResponse(200, envelope({"1": quote_item(1, "AAA")}))],
                cache_ttl_sec=10,
            )
            first.quotes_latest([1])

            fallback_session = FakeSession([FakeResponse(503, {})])
            fallback = CmcClient(
                api_key="fake-test-key",
                cache_path=cache_path,
                cache_ttl_sec=10,
                max_data_age_sec=60,
                retries=0,
                session=fallback_session,  # type: ignore[arg-type]
                clock=lambda: NOW + 20,
            )
            result = fallback.quotes_latest([1])

            self.assertEqual(result.source_by_id, {1: "fallback_cache"})
            self.assertEqual(result.cache_fallbacks, 1)
            self.assertEqual(result.missing_ids, ())
            self.assertEqual(fallback.diagnostics()["last_error"], "server_error")

    def test_partial_fallback_preserves_cached_ids_and_marks_new_ids_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            first, _, _ = self.client(
                cache_path,
                [FakeResponse(200, envelope([quote_item(1, "AAA")]))],
                cache_ttl_sec=10,
            )
            first.quotes_latest([1])
            fallback = CmcClient(
                api_key="fake-test-key",
                cache_path=cache_path,
                cache_ttl_sec=10,
                max_data_age_sec=60,
                retries=0,
                session=FakeSession([FakeResponse(503, {})]),  # type: ignore[arg-type]
                clock=lambda: NOW + 20,
            )

            result = fallback.quotes_latest([1, 2])

            self.assertEqual(tuple(result.quotes), (1,))
            self.assertEqual(result.source_by_id, {1: "fallback_cache"})
            self.assertEqual(result.missing_ids, (2,))
            self.assertEqual(result.cache_fallbacks, 1)

    def test_partial_fallback_is_not_dependent_on_cmc_id_batch_order(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            first, _, _ = self.client(
                cache_path,
                [FakeResponse(200, envelope([quote_item(2, "BBB")]))],
                cache_ttl_sec=10,
            )
            first.quotes_latest([2])
            fallback = CmcClient(
                api_key="fake-test-key",
                cache_path=cache_path,
                cache_ttl_sec=10,
                max_data_age_sec=60,
                retries=0,
                batch_size=1,
                min_request_interval_sec=0,
                session=FakeSession([FakeResponse(503, {})]),  # type: ignore[arg-type]
                clock=lambda: NOW + 20,
            )

            result = fallback.quotes_latest([1, 2])

            self.assertEqual(tuple(result.quotes), (2,))
            self.assertEqual(result.missing_ids, (1,))
            self.assertEqual(result.source_by_id, {2: "fallback_cache"})

    def test_complete_map_can_fallback_after_ttl_when_data_age_is_allowed(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            first, _, _ = self.client(
                cache_path,
                [FakeResponse(200, envelope([map_item(1, "AAA")]))],
                cache_ttl_sec=10,
            )
            first.load_map()

            fallback = CmcClient(
                api_key="fake-test-key",
                cache_path=cache_path,
                cache_ttl_sec=10,
                max_data_age_sec=60,
                retries=0,
                session=FakeSession([FakeResponse(503, {})]),  # type: ignore[arg-type]
                clock=lambda: NOW + 20,
            )
            result = fallback.load_map()

            self.assertEqual(result.source, "fallback_cache")
            self.assertEqual([entry.cmc_id for entry in result.entries], [1])
            self.assertEqual(result.request_pages, 0)
            self.assertEqual(fallback.diagnostics()["last_error"], "server_error")

    def test_map_older_than_maximum_age_cannot_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            first, _, _ = self.client(
                cache_path,
                [FakeResponse(200, envelope([map_item(1, "AAA")]))],
                cache_ttl_sec=10,
            )
            first.load_map()
            too_old = CmcClient(
                api_key="fake-test-key",
                cache_path=cache_path,
                cache_ttl_sec=10,
                max_data_age_sec=30,
                retries=0,
                session=FakeSession([FakeResponse(503, {})]),  # type: ignore[arg-type]
                clock=lambda: NOW + 31,
            )

            with self.assertRaises(CmcClientError) as caught:
                too_old.load_map()

            self.assertEqual(caught.exception.kind, "server_error")

    def test_future_quote_and_map_timestamps_beyond_clock_skew_are_not_fresh(self) -> None:
        with TemporaryDirectory() as tmp:
            future = "2026-08-07T12:06:41Z"
            quote_cache = Path(tmp) / "quote.json"
            quote_client, _, _ = self.client(
                quote_cache,
                [FakeResponse(200, envelope({"1": quote_item(1, "AAA", updated_at=future)}))],
            )

            with self.assertRaises(CmcClientError) as quote_error:
                quote_client.quotes_latest([1])
            self.assertEqual(quote_error.exception.kind, "protocol_error")
            self.assertFalse(quote_cache.exists())

            map_cache = Path(tmp) / "map.json"
            map_client, _, _ = self.client(
                map_cache,
                [FakeResponse(200, envelope([map_item(1, "AAA")], timestamp=future))],
                cache_ttl_sec=10,
            )
            with self.assertRaises(CmcClientError) as map_error:
                map_client.load_map()
            self.assertEqual(map_error.exception.kind, "protocol_error")
            self.assertFalse(map_cache.exists())

    def test_data_older_than_maximum_age_cannot_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            first, _, _ = self.client(
                cache_path,
                [FakeResponse(200, envelope({"1": quote_item(1, "AAA")}))],
                cache_ttl_sec=10,
            )
            first.quotes_latest([1])
            too_old = CmcClient(
                api_key="fake-test-key",
                cache_path=cache_path,
                cache_ttl_sec=10,
                max_data_age_sec=30,
                retries=0,
                session=FakeSession([FakeResponse(503, {})]),  # type: ignore[arg-type]
                clock=lambda: NOW + 31,
            )

            with self.assertRaises(CmcClientError) as caught:
                too_old.quotes_latest([1])

            self.assertEqual(caught.exception.kind, "server_error")

    def test_corrupt_cache_is_quarantined_and_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            cache_path.write_text("{broken", encoding="utf-8")
            session = FakeSession([])
            client = CmcClient(
                api_key=None,
                cache_path=cache_path,
                session=session,  # type: ignore[arg-type]
                clock=lambda: NOW,
            )

            result = client.load_map(cache_only=True)

            self.assertEqual(result.source, "cache_miss")
            self.assertEqual(session.calls, [])
            self.assertEqual(len(list(Path(tmp).glob("cmc.json.corrupt.*"))), 1)

    def test_semantically_invalid_cache_identity_is_rejected(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            cache_path.write_text(json.dumps({
                "schema_version": CACHE_SCHEMA_VERSION,
                "generated_at": NOW_ISO,
                "map": {
                    "generated_at": NOW_ISO,
                    "data_updated_at": NOW_ISO,
                    "expires_at": "2026-08-07T12:05:00Z",
                    "entries": [{
                        "cmc_id": 1,
                        "name": "AAA Coin",
                        "symbol": "AAA",
                        "slug": "aaa-coin",
                        "is_active": "false",
                    }],
                },
                "quotes": {
                    "entries": {
                        "1": {
                            "generated_at": NOW_ISO,
                            "data_updated_at": NOW_ISO,
                            "expires_at": "2026-08-07T12:05:00Z",
                            "value": {
                                "cmc_id": 2,
                                "name": "BBB Coin",
                                "symbol": "BBB",
                                "slug": "bbb-coin",
                                "market_cap_usd": 1_000_000,
                                "last_updated": NOW_ISO,
                            },
                        }
                    }
                },
            }), encoding="utf-8")
            client = CmcClient(
                api_key=None,
                cache_path=cache_path,
                session=FakeSession([]),  # type: ignore[arg-type]
                clock=lambda: NOW,
            )

            self.assertEqual(client.load_map(cache_only=True).source, "cache_miss")
            quotes = client.quotes_latest([1], cache_only=True)
            self.assertEqual(quotes.quotes, {})
            self.assertEqual(quotes.missing_ids, (1,))

            document = json.loads(cache_path.read_text(encoding="utf-8"))
            document["map"]["entries"][0]["cmc_id"] = "1"
            document["map"]["entries"][0]["is_active"] = True
            document["quotes"]["entries"]["1"]["value"]["cmc_id"] = 1.9
            cache_path.write_text(json.dumps(document), encoding="utf-8")

            self.assertEqual(client.load_map(cache_only=True).source, "cache_miss")
            strict_quotes = client.quotes_latest([1], cache_only=True)
            self.assertEqual(strict_quotes.quotes, {})

    def test_failed_atomic_replace_preserves_previous_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            original = {"schema_version": CACHE_SCHEMA_VERSION, "marker": "previous"}
            cache_path.write_text(json.dumps(original), encoding="utf-8")
            client, _, _ = self.client(
                cache_path,
                [FakeResponse(200, envelope([]))],
            )

            with patch("shared.atomic_json.os.replace", side_effect=OSError("interrupted")):
                with self.assertRaises(OSError):
                    client.load_map()

            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8")), original)
            self.assertFalse(cache_path.with_name(f"{cache_path.name}.tmp.{os.getpid()}").exists())

    def test_missing_key_is_config_error_but_cache_only_still_works(self) -> None:
        with TemporaryDirectory() as tmp:
            cache_path = Path(tmp) / "cmc.json"
            session = FakeSession([])
            client = CmcClient(
                api_key="",
                cache_path=cache_path,
                session=session,  # type: ignore[arg-type]
                clock=lambda: NOW,
            )

            self.assertEqual(client.load_map(cache_only=True).source, "cache_miss")
            with self.assertRaises(CmcClientError) as caught:
                client.load_map()

            self.assertEqual(caught.exception.kind, "config_error")
            self.assertEqual(session.calls, [])

    def test_api_key_never_appears_in_repr_errors_diagnostics_or_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            secret = "cmc-super-secret-test-value"
            cache_path = Path(tmp) / "cmc.json"
            session = FakeSession([FakeResponse(401, {"error": secret})])
            client = CmcClient(
                api_key=secret,
                cache_path=cache_path,
                session=session,  # type: ignore[arg-type]
                clock=lambda: NOW,
            )

            with self.assertRaises(CmcClientError) as caught:
                client.load_map()

            combined = f"{client!r} {caught.exception!r} {client.diagnostics()!r}"
            self.assertNotIn(secret, combined)
            self.assertFalse(cache_path.exists())

    def test_injected_session_is_not_closed_and_owned_session_is_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            injected = FakeSession([])
            client = CmcClient(
                api_key="fake",
                cache_path=Path(tmp) / "one.json",
                session=injected,  # type: ignore[arg-type]
            )
            client.close()
            self.assertFalse(injected.closed)

            with patch("shared.cmc_data.requests.Session") as factory:
                owned = CmcClient(api_key="fake", cache_path=Path(tmp) / "two.json")
                owned.close()
            factory.return_value.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
