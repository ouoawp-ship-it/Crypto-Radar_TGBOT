from __future__ import annotations

import unittest
from decimal import Decimal

import requests

from paopao_radar.onchain_flow.collectors.arkham_rest import (
    ArkhamAuthError,
    ArkhamRateLimitError,
    ArkhamRestClient,
    ArkhamSchemaError,
    ArkhamServiceError,
)


class FakeResponse:
    def __init__(
        self,
        payload=None,
        *,
        status_code=200,
        text=None,
        headers=None,
        json_error: Exception | None = None,
    ):
        self.payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.json_error = json_error

    def json(self):
        if self.json_error is not None:
            raise self.json_error
        return self.payload


class ScriptedSession:
    def __init__(self, actions):
        self.actions = list(actions)
        self.requests = []

    def get(self, url, *, params, headers, timeout):
        self.requests.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
            }
        )
        action = self.actions.pop(0)
        if isinstance(action, Exception):
            raise action
        return action


class ArkhamRestClientTests(unittest.TestCase):
    def test_capability_check_uses_api_key_and_both_cex_queries(self) -> None:
        session = ScriptedSession(
            [
                FakeResponse(text="ok"),
                FakeResponse(["ethereum", {"id": "base"}]),
                FakeResponse(
                    {"creditPerSession": 500, "creditPerTransfer": 1}
                ),
                FakeResponse({"transfers": [], "count": 0}),
                FakeResponse(
                    {"transfers": [], "count": 0},
                    headers={"X-RateLimit-Remaining": "9"},
                ),
            ]
        )
        sleeps: list[float] = []
        client = ArkhamRestClient(
            "https://api.arkm.com",
            "top-secret-key",
            timeout_sec=7,
            retry=0,
            session=session,
            sleep=sleeps.append,
            clock=lambda: 0.0,
        )
        result = client.capability_check(
            global_usd_gte=Decimal("100000"),
            chains=("base",),
            limit=100,
        )
        self.assertTrue(result["authenticated"])
        self.assertTrue(result["type_cex_rest_supported"])
        self.assertEqual(result["supported_chain_count"], 2)
        self.assertEqual(result["current_session_credit_price"], 500)
        self.assertEqual(result["current_transfer_credit_price"], 1)
        self.assertEqual(
            session.requests[3]["params"]["to"], "type:cex"
        )
        self.assertEqual(
            session.requests[4]["params"]["from"], "type:cex"
        )
        self.assertEqual(
            session.requests[3]["params"]["usdGte"], "10000000"
        )
        self.assertEqual(session.requests[3]["params"]["limit"], 2)
        self.assertEqual(session.requests[3]["params"]["chains"], "base")
        self.assertEqual(sleeps, [1.0])
        for request in session.requests:
            self.assertEqual(
                request["headers"], {"API-Key": "top-secret-key"}
            )
            self.assertEqual(request["timeout"], 7.0)
        self.assertNotIn("top-secret-key", str(result))

    def test_type_cex_rejection_requires_explicit_entity_ids(self) -> None:
        session = ScriptedSession(
            [
                FakeResponse(text="ok"),
                FakeResponse([]),
                FakeResponse(
                    {"creditPerSession": 500, "creditPerTransfer": 1}
                ),
                FakeResponse({}, status_code=400),
                FakeResponse({}, status_code=400),
            ]
        )
        client = ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=0,
            session=session,
            sleep=lambda _seconds: None,
            clock=lambda: 0.0,
        )
        result = client.capability_check(
            global_usd_gte=Decimal("100000"),
            chains=(),
            limit=10,
        )
        self.assertFalse(result["type_cex_rest_supported"])
        self.assertTrue(result["requires_explicit_cex_entity_ids"])
        self.assertEqual(len(session.requests), 5)

    def test_auth_errors_fail_closed_and_redact_key(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                session = ScriptedSession(
                    [FakeResponse({}, status_code=status)]
                )
                client = ArkhamRestClient(
                    "https://api.arkm.com/private",
                    "never-print-this",
                    retry=5,
                    session=session,
                )
                with self.assertRaises(ArkhamAuthError) as raised:
                    client.chains()
                self.assertEqual(len(session.requests), 1)
                self.assertNotIn(
                    "never-print-this", str(raised.exception)
                )
                self.assertNotIn("/private", str(raised.exception))

    def test_429_and_5xx_retries_are_bounded(self) -> None:
        rate_session = ScriptedSession(
            [
                FakeResponse(
                    {},
                    status_code=429,
                    headers={"Retry-After": "0"},
                ),
                FakeResponse({}, status_code=429),
            ]
        )
        rate_client = ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=1,
            backoff_sec=0,
            session=rate_session,
            sleep=lambda _seconds: None,
        )
        with self.assertRaises(ArkhamRateLimitError):
            rate_client.chains()
        self.assertEqual(len(rate_session.requests), 2)

        service_session = ScriptedSession(
            [
                FakeResponse({}, status_code=503),
                FakeResponse({}, status_code=503),
            ]
        )
        service_client = ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=1,
            backoff_sec=0,
            session=service_session,
            sleep=lambda _seconds: None,
        )
        with self.assertRaises(ArkhamServiceError):
            service_client.chains()
        self.assertEqual(len(service_session.requests), 2)

    def test_timeout_and_malformed_json_are_bounded(self) -> None:
        timeout_session = ScriptedSession(
            [
                requests.Timeout("contains secret"),
                FakeResponse([]),
            ]
        )
        client = ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=1,
            backoff_sec=0,
            session=timeout_session,
            sleep=lambda _seconds: None,
        )
        self.assertEqual(client.chains(), [])
        self.assertEqual(len(timeout_session.requests), 2)

        malformed = ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=0,
            session=ScriptedSession(
                [
                    FakeResponse(
                        json_error=ValueError("bad secret document")
                    )
                ]
            ),
        )
        with self.assertRaises(ArkhamSchemaError) as raised:
            malformed.chains()
        self.assertNotIn("bad secret", str(raised.exception))

    def test_transfer_schema_and_heavy_request_budget(self) -> None:
        session = ScriptedSession(
            [
                FakeResponse({"transfers": [], "count": 0}),
                FakeResponse({"transfers": [], "count": 0}),
            ]
        )
        sleeps: list[float] = []
        client = ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=0,
            session=session,
            sleep=sleeps.append,
            clock=lambda: 10.0,
        )
        client.transfers({"to": "type:cex"})
        client.transfers({"from": "type:cex"})
        self.assertEqual(sleeps, [1.0])

        malformed = ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=0,
            session=ScriptedSession(
                [FakeResponse({"transfers": "bad", "count": 1})]
            ),
        )
        with self.assertRaises(ArkhamSchemaError):
            malformed.transfers({"to": "type:cex"})
