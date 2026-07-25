from __future__ import annotations

import unittest
from decimal import Decimal

import requests

from paopao_radar.onchain_flow.collectors.arkham_rest import (
    ArkhamAuthError,
    ArkhamRateLimitError,
    ArkhamResponseError,
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

    def get(
        self, url, *, params, headers, timeout, allow_redirects=True
    ):
        self.requests.append(
            {
                "url": url,
                "params": params,
                "headers": headers,
                "timeout": timeout,
                "allow_redirects": allow_redirects,
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
                FakeResponse(["ethereum", {"id": "base"}]),
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
        self.assertEqual(result["rest_capability_status"], "ok")
        self.assertEqual(
            result["websocket_check"], "not_run_p3_2a"
        )
        self.assertEqual(
            session.requests[1]["params"]["to"], "type:cex"
        )
        self.assertEqual(
            session.requests[2]["params"]["from"], "type:cex"
        )
        self.assertEqual(
            session.requests[1]["params"]["usdGte"], "10000000"
        )
        self.assertEqual(session.requests[1]["params"]["limit"], 2)
        self.assertEqual(session.requests[1]["params"]["chains"], "base")
        self.assertEqual(sleeps, [1.0])
        self.assertEqual(len(session.requests), 3)
        self.assertNotIn(
            "/health",
            {request["url"] for request in session.requests},
        )
        self.assertNotIn(
            "/ws/session-info",
            {request["url"] for request in session.requests},
        )
        for request in session.requests:
            self.assertEqual(
                request["headers"], {"API-Key": "top-secret-key"}
            )
            self.assertEqual(request["timeout"], 7.0)
        self.assertNotIn("top-secret-key", str(result))

    def test_type_cex_rejection_requires_explicit_entity_ids(self) -> None:
        session = ScriptedSession(
            [
                FakeResponse([]),
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
        self.assertEqual(
            result["rest_capability_status"],
            "explicit_cex_entity_ids_required",
        )
        self.assertEqual(
            result["websocket_check"], "not_run_p3_2a"
        )
        self.assertEqual(len(session.requests), 3)

    def test_health_remains_optional_and_schema_checked(self) -> None:
        healthy = ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=0,
            session=ScriptedSession([FakeResponse(text="ok")]),
        )
        self.assertEqual(healthy.health(), "ok")
        unhealthy = ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=0,
            session=ScriptedSession([FakeResponse(text="not-ok")]),
        )
        with self.assertRaises(ArkhamSchemaError):
            unhealthy.health()

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

    def test_retry_after_is_capped_and_redirects_are_rejected(
        self,
    ) -> None:
        sleeps: list[float] = []
        capped = ScriptedSession(
            [
                FakeResponse(
                    {},
                    status_code=429,
                    headers={"Retry-After": "999999"},
                ),
                FakeResponse([]),
            ]
        )
        ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=1,
            backoff_sec=2,
            retry_after_max_sec=60,
            session=capped,
            sleep=sleeps.append,
        ).chains()
        self.assertEqual(sleeps, [60.0])

        malformed_sleeps: list[float] = []
        malformed = ScriptedSession(
            [
                FakeResponse(
                    {},
                    status_code=429,
                    headers={"Retry-After": "not-a-number"},
                ),
                FakeResponse([]),
            ]
        )
        ArkhamRestClient(
            "https://api.arkm.com",
            "secret",
            retry=1,
            backoff_sec=2,
            retry_after_max_sec=60,
            session=malformed,
            sleep=malformed_sleeps.append,
        ).chains()
        self.assertEqual(malformed_sleeps, [2.0])

        redirected = ScriptedSession(
            [
                FakeResponse(
                    {},
                    status_code=302,
                    headers={
                        "Location": "https://evil.invalid/collect"
                    },
                )
            ]
        )
        client = ArkhamRestClient(
            "https://api.arkm.com",
            "never-leak-this",
            retry=3,
            session=redirected,
        )
        with self.assertRaises(ArkhamResponseError) as raised:
            client.chains()
        self.assertEqual(len(redirected.requests), 1)
        self.assertFalse(redirected.requests[0]["allow_redirects"])
        self.assertNotIn("evil.invalid", str(raised.exception))
        self.assertNotIn("never-leak-this", str(raised.exception))

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
