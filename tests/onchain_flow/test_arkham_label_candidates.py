from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import requests

from paopao_radar.onchain_flow.arkham_intelligence import (
    ARKHAM_REQUEST_HARD_LIMIT,
    ArkhamIntelligenceClient,
    ArkhamIntelligenceError,
)
from paopao_radar.onchain_flow.cli import main
from paopao_radar.onchain_flow.label_candidates import (
    LabelCandidateDiscovery,
    LabelCandidateError,
    LabelCandidateStore,
    candidate_from_arkham,
    label_readiness,
)
from tests.onchain_flow.support import make_settings


ADDRESS = "0x1111111111111111111111111111111111111111"
ADDRESS_2 = "0x2222222222222222222222222222222222222222"
TOKEN = "0xcbD06E5A2B0C65597161de254AA074E489dEb510"


class FakeResponse:
    def __init__(self, status: int, payload: object):
        self.status_code = status
        self._payload = payload

    def json(self) -> object:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response  # type: ignore[return-value]


def arkham_payload(
    address: str = ADDRESS,
    *,
    predicted_only: bool = False,
    deposit: bool = False,
) -> dict[str, object]:
    value: dict[str, object] = {
        "address": address,
        "chain": "base",
    }
    if predicted_only:
        value["predictedEntity"] = {
            "id": "prediction",
            "name": "Some Exchange",
            "type": "exchange",
        }
        return value
    value["arkhamEntity"] = {
        "id": "coinbase",
        "name": "Coinbase",
        "type": "exchange",
        "service": True,
    }
    value["arkhamLabel"] = {
        "name": "Hot Wallet",
        "chainType": "evm",
    }
    if deposit:
        value["depositServiceID"] = "coinbase"
    return value


def candidate() -> dict[str, object]:
    result = candidate_from_arkham(
        arkham_payload(),
        expected_address=ADDRESS,
        observed_at=1_700_000_000,
    )
    assert result is not None
    return result


class ArkhamClientTests(unittest.TestCase):
    def client(
        self,
        responses: list[object],
        *,
        retries: int = 0,
        sleep: object | None = None,
    ) -> tuple[ArkhamIntelligenceClient, FakeSession]:
        session = FakeSession(responses)
        client = ArkhamIntelligenceClient(
            base_url="https://api.arkm.invalid",
            api_key="fake-arkham-key",
            timeout_sec=15,
            max_retries=retries,
            session=session,
            sleep=(sleep or (lambda _seconds: None)),  # type: ignore[arg-type]
        )
        return client, session

    def test_provider_check_is_explicit_and_does_not_expose_key(self) -> None:
        client, session = self.client([FakeResponse(200, {"ok": True})])
        result = client.provider_check()
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["request_count"], 1)
        self.assertNotIn(
            "fake-arkham-key",
            json.dumps(result, ensure_ascii=False),
        )
        self.assertEqual(session.calls[0]["method"], "GET")
        self.assertTrue(
            str(session.calls[0]["url"]).endswith("/networks/status")
        )

    def test_batch_and_single_fallback_are_bounded(self) -> None:
        client, session = self.client([
            FakeResponse(200, {"addresses": {ADDRESS: arkham_payload()}}),
            FakeResponse(200, arkham_payload(ADDRESS_2)),
        ])
        result = client.address_intelligence([ADDRESS, ADDRESS_2])
        self.assertEqual(set(result), {ADDRESS, ADDRESS_2})
        self.assertEqual(len(session.calls), 2)
        self.assertLessEqual(client.request_count, ARKHAM_REQUEST_HARD_LIMIT)

    def test_error_codes_are_safe_and_bounded(self) -> None:
        cases = (
            (401, "arkham_auth_failed"),
            (403, "arkham_auth_failed"),
            (402, "arkham_credit_or_subscription_required"),
            (429, "arkham_rate_limited"),
            (500, "arkham_provider_unavailable"),
        )
        for status, expected in cases:
            with self.subTest(status=status):
                client, _ = self.client([
                    FakeResponse(status, {"error": "PRIVATE BODY"})
                ])
                with self.assertRaises(ArkhamIntelligenceError) as raised:
                    client.provider_check()
                self.assertEqual(raised.exception.code, expected)
                self.assertNotIn("PRIVATE BODY", str(raised.exception))

    def test_timeout_is_classified_without_response_body(self) -> None:
        client, _ = self.client([requests.Timeout("private provider text")])
        with self.assertRaises(ArkhamIntelligenceError) as raised:
            client.provider_check()
        self.assertEqual(raised.exception.code, "arkham_timeout")
        self.assertNotIn("private provider text", str(raised.exception))

    def test_seed_queries_are_exactly_two_and_rate_spaced(self) -> None:
        sleeps: list[float] = []
        client, session = self.client([
            FakeResponse(200, {"transfers": []}),
            FakeResponse(200, {"transfers": []}),
        ], sleep=sleeps.append)
        self.assertEqual(client.seed_cex_transfers(), [])
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(sleeps, [1.1])
        params = [call["params"] for call in session.calls]
        self.assertIn("from", params[0])  # type: ignore[operator]
        self.assertIn("to", params[1])  # type: ignore[operator]

    def test_retries_count_toward_hard_request_limit(self) -> None:
        client, _ = self.client(
            [FakeResponse(500, {})] * ARKHAM_REQUEST_HARD_LIMIT,
            retries=2,
        )
        for _ in range(2):
            with self.assertRaises(ArkhamIntelligenceError):
                client.provider_check()
        self.assertEqual(client.request_count, ARKHAM_REQUEST_HARD_LIMIT)


class CandidateEvidenceTests(unittest.TestCase):
    def test_exact_exchange_and_deposit_evidence(self) -> None:
        cex = candidate_from_arkham(
            arkham_payload(),
            expected_address=ADDRESS,
            observed_at=123,
        )
        self.assertIsNotNone(cex)
        self.assertEqual(cex["proposed_address_type"], "hot")  # type: ignore[index]
        deposit = candidate_from_arkham(
            arkham_payload(deposit=True),
            expected_address=ADDRESS,
            observed_at=123,
        )
        self.assertEqual(
            deposit["evidence_type"],  # type: ignore[index]
            "exact_deposit_exchange_id",
        )
        self.assertEqual(
            deposit["proposed_address_type"],  # type: ignore[index]
            "deposit",
        )

    def test_predicted_only_non_base_and_address_mismatch_are_rejected(
        self,
    ) -> None:
        self.assertIsNone(candidate_from_arkham(
            arkham_payload(predicted_only=True),
            expected_address=ADDRESS,
            observed_at=1,
        ))
        wrong_chain = arkham_payload()
        wrong_chain["chain"] = "ethereum"
        self.assertIsNone(candidate_from_arkham(
            wrong_chain,
            expected_address=ADDRESS,
            observed_at=1,
        ))
        self.assertIsNone(candidate_from_arkham(
            arkham_payload(ADDRESS_2),
            expected_address=ADDRESS,
            observed_at=1,
        ))

    def test_candidate_id_is_stable(self) -> None:
        first = candidate_from_arkham(
            arkham_payload(),
            expected_address=ADDRESS,
            observed_at=1,
        )
        second = candidate_from_arkham(
            arkham_payload(),
            expected_address=ADDRESS,
            observed_at=2,
        )
        self.assertEqual(
            first["candidate_id"],  # type: ignore[index]
            second["candidate_id"],  # type: ignore[index]
        )


class CandidateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "data" / "onchain" / "label_candidates.json"
        self.labels = (
            self.root / "config" / "onchain" / "cex_addresses.private.csv"
        )
        self.store = LabelCandidateStore(self.path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_pending_is_private_and_does_not_enter_csv(self) -> None:
        self.store.merge([candidate()])
        self.assertFalse(self.labels.exists())
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("fake-arkham-key", text)
        self.assertNotIn("PRIVATE BODY", text)
        if os.name != "nt":
            self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.path.parent.stat().st_mode & 0o777, 0o700)

    def test_approve_atomically_adds_a_valid_live_label(self) -> None:
        item = candidate()
        self.store.merge([item])
        result = self.store.approve(
            str(item["candidate_id"]),
            labels_path=self.labels,
            min_confidence=0.8,
            reviewed_at=1_700_000_010,
        )
        self.assertEqual(result["candidate"]["status"], "approved")
        readiness = label_readiness(
            self.labels,
            min_confidence=0.8,
            now=1_700_000_011,
        )
        self.assertEqual(
            readiness["classification_eligible_cex_count"], 1
        )
        text = self.labels.read_text(encoding="utf-8")
        self.assertIn("arkham_api_exact+manual_review", text)
        self.assertNotIn("synthetic_fixture", text)
        if os.name != "nt":
            self.assertEqual(self.labels.stat().st_mode & 0o777, 0o600)

    def test_duplicate_rejected_and_original_csv_restored(self) -> None:
        item = candidate()
        self.store.merge([item])
        self.store.approve(
            str(item["candidate_id"]),
            labels_path=self.labels,
            min_confidence=0.8,
            reviewed_at=1_700_000_010,
        )
        original = self.labels.read_bytes()
        second_payload = arkham_payload()
        second_payload["arkhamEntity"] = {
            "id": "coinbase-secondary",
            "name": "Coinbase",
            "type": "exchange",
            "service": True,
        }
        second = candidate_from_arkham(
            second_payload,
            expected_address=ADDRESS,
            observed_at=1_700_000_019,
        )
        assert second is not None
        self.store.merge([second])
        with self.assertRaises(LabelCandidateError) as raised:
            self.store.approve(
                str(second["candidate_id"]),
                labels_path=self.labels,
                min_confidence=0.8,
                reviewed_at=1_700_000_020,
            )
        self.assertEqual(raised.exception.code, "candidate_label_duplicate")
        self.assertEqual(self.labels.read_bytes(), original)

    def test_public_example_file_is_never_modified(self) -> None:
        item = candidate()
        self.store.merge([item])
        example = self.root / "cex_addresses.example.csv"
        with self.assertRaises(LabelCandidateError) as raised:
            self.store.approve(
                str(item["candidate_id"]),
                labels_path=example,
                min_confidence=0.8,
            )
        self.assertEqual(raised.exception.code, "private_labels_file_required")
        self.assertFalse(example.exists())

    def test_reject_is_offline_and_preserves_audit(self) -> None:
        item = candidate()
        self.store.merge([item])
        rejected = self.store.reject(
            str(item["candidate_id"]),
            reviewed_at=1_700_000_100,
        )
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(len(self.store.list(status="rejected")), 1)


class CandidateDiscoveryAndCliTests(unittest.TestCase):
    @staticmethod
    def activity(_query: object) -> dict[str, object]:
        return {
            "status": "ok",
            "complete": True,
            "transfers": [{
                "from": {"address": ADDRESS},
                "to": {"address": ADDRESS_2},
                "amount": "12.5",
                "block_time": 1_700_000_000,
            }],
            "diagnostics": {"transfer_count": 1},
        }

    def test_discovery_uses_fake_activity_and_never_stores_raw_payload(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            root = Path(raw)
            settings = make_settings(
                root,
                label_candidates_path=(
                    root / "data" / "onchain" / "label_candidates.json"
                ),
                arkham_api_key="fake-key",
                oar_label_candidate_max_addresses=50,
            )

            class FakeClient:
                request_count = 1

                def address_intelligence(
                    self, addresses: object
                ) -> dict[str, dict[str, object]]:
                    values = list(addresses)  # type: ignore[arg-type]
                    return {values[0]: {
                        **arkham_payload(values[0]),
                        "providerPrivatePayload": "DO NOT STORE",
                    }}

                def seed_cex_transfers(self) -> list[dict[str, object]]:
                    raise AssertionError("seed must not run")

            discovery = LabelCandidateDiscovery(
                settings,
                client_factory=lambda **_kwargs: FakeClient(),  # type: ignore[arg-type]
                activity_runner=self.activity,
                clock=lambda: 1_700_000_001,
            )
            result = discovery.discover(
                chain="base",
                contract=TOKEN,
                window="4h",
                max_addresses=50,
            )
            self.assertEqual(result["candidates_found"], 1)
            text = settings.label_candidates_path.read_text(
                encoding="utf-8"
            )
            self.assertNotIn("providerPrivatePayload", text)
            self.assertNotIn("DO NOT STORE", text)
            self.assertNotIn("fake-key", text)

    def test_cli_requires_allow_network_and_default_calls_zero(self) -> None:
        with TemporaryDirectory() as raw:
            settings = make_settings(
                Path(raw),
                arkham_api_key="fake-key",
            )
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    ["label-candidates", "provider-check"],
                    settings=settings,
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"], "allow_network_required")
            self.assertFalse(payload["network_activity"])

    def test_missing_key_is_optional_before_network(self) -> None:
        with TemporaryDirectory() as raw:
            settings = make_settings(Path(raw), arkham_api_key="")
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "label-candidates",
                        "provider-check",
                        "--allow-network",
                    ],
                    settings=settings,
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "optional_disabled")
            self.assertEqual(payload["reason"], "arkham_not_configured")
            self.assertEqual(payload["arkham_request_count"], 0)
            self.assertFalse(payload["network_activity"])

    def test_missing_key_discovery_is_optional_and_creates_nothing(
        self,
    ) -> None:
        with TemporaryDirectory() as raw:
            settings = make_settings(Path(raw), arkham_api_key="")
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "label-candidates",
                        "discover",
                        "--chain",
                        "base",
                        "--contract",
                        TOKEN,
                        "--window",
                        "4h",
                        "--allow-network",
                    ],
                    settings=settings,
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 0)
            self.assertEqual(payload["status"], "optional_disabled")
            self.assertEqual(payload["candidates_found"], 0)
            self.assertEqual(payload["created"], 0)
            self.assertEqual(payload["arkham_request_count"], 0)
            self.assertFalse(settings.label_candidates_path.exists())

    def test_settings_ranges_are_enforced(self) -> None:
        with TemporaryDirectory() as raw:
            settings = make_settings(Path(raw))
            for values in (
                {"arkham_api_timeout_sec": 0},
                {"arkham_api_timeout_sec": 61},
                {"arkham_api_max_retries": 3},
                {"oar_label_candidate_max_addresses": 101},
                {"arkham_api_base_url": "http://api.arkm.com"},
            ):
                with self.subTest(values=values):
                    with self.assertRaises(Exception):
                        replace(settings, **values).validate()


if __name__ == "__main__":
    unittest.main()
