from __future__ import annotations

import csv
from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from paopao_radar.onchain_flow.address_intelligence import (
    AddressIntelligenceError,
    AddressIntelligenceService,
    AddressIntelligenceStore,
    ArkhamOptionalProvider,
    BehaviorInferenceProvider,
    DuneAddressProvider,
    LocalApprovedProvider,
    ManualCsvProvider,
    OliParquetProvider,
    build_candidate,
)
from paopao_radar.onchain_flow.config import SettingsValidationError
from paopao_radar.onchain_flow.cli import main as onchain_main
from paopao_radar.onchain_flow.labels import (
    LabelValidationError,
    load_labels_csv,
)
from paopao_radar.onchain_flow.labels import is_approved_label
from paopao_radar.onchain_flow.models import AddressLabel

from tests.onchain_flow.analysis_support import (
    activity,
    record,
)
from tests.onchain_flow.support import make_settings
from scripts.paopao_config import (
    ALLOWLIST,
    SECRET_KEYS,
    ConfigManager,
)


ADDRESS_A = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
ADDRESS_B = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
ADDRESS_C = "0xcccccccccccccccccccccccccccccccccccccccc"
TOKEN = "0x9999999999999999999999999999999999999999"
CEX = "0x1111111111111111111111111111111111111111"


class FakeResponse:
    def __init__(self, status_code: int, payload: object):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        if not self.responses:
            raise AssertionError("unexpected network call")
        return self.responses.pop(0)


def candidate(
    *,
    provider: str = "dune_cex",
    address: str = ADDRESS_A,
    name: str = "Exchange A",
    role: str = "cex_wallet",
    entity_type: str = "cex",
    now: int = 1000,
    expires_at: int | None = None,
) -> dict[str, object]:
    return build_candidate(
        chain_id=8453,
        address=address,
        entity_name=name,
        entity_type=entity_type,
        address_role=role,
        provider=provider,
        source_ref=f"{provider}:fixture",
        source_confidence=0.95,
        evidence_type="exact_fixture",
        evidence={"address": address, "name": name, "role": role},
        observed_at=now,
        expires_at=expires_at,
    )


class AddressIntelligenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.labels = (
            self.root / "config" / "onchain" / "cex_addresses.private.csv"
        )
        self.settings = make_settings(
            self.root,
            labels_path=self.labels,
            arkham_api_key="",
            dune_api_key="",
        )
        self.store = AddressIntelligenceStore.from_settings(self.settings)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_optional_providers_do_not_block_core(self) -> None:
        status = AddressIntelligenceService(self.settings).provider_status()
        providers = {
            item["provider"]: item
            for item in status["providers"]
        }
        self.assertEqual(
            providers["arkham_optional"]["status"],
            "optional_disabled",
        )
        self.assertEqual(
            providers["dune_cex"]["status"],
            "optional_disabled",
        )
        self.assertTrue(status["core_available"])
        self.assertFalse(status["arkham_required"])
        self.assertFalse(status["network_activity"])

    def test_local_provider_ignores_synthetic_fixture_labels(self) -> None:
        provider = LocalApprovedProvider(
            Path(__file__).resolve().parents[2]
            / "config"
            / "onchain"
            / "cex_addresses.example.csv"
        )
        self.assertFalse(provider.configured)
        self.assertEqual(
            provider.discover([{"address": CEX}]), []
        )

    def test_empty_optional_provider_urls_and_keys_are_valid(self) -> None:
        settings = replace(
            self.settings,
            arkham_api_base_url="",
            arkham_api_key="",
            dune_api_base_url="",
            dune_api_key="",
        )
        settings.validate()

    def test_provider_key_requires_safe_base_url(self) -> None:
        with self.assertRaises(SettingsValidationError):
            replace(
                self.settings,
                dune_api_base_url="",
                dune_api_key="fake",
            ).validate()

    def test_arkham_empty_key_never_constructs_client(self) -> None:
        factory = Mock(side_effect=AssertionError("network client created"))
        provider = ArkhamOptionalProvider(
            self.settings,
            client_factory=factory,
        )
        self.assertEqual(
            provider.provider_check()["status"], "optional_disabled"
        )
        self.assertEqual(provider.discover([]), [])
        factory.assert_not_called()

    def test_cli_provider_status_is_offline_and_optional(self) -> None:
        output = io.StringIO()
        with redirect_stdout(output):
            code = onchain_main(
                ["address-intelligence", "providers"],
                settings=self.settings,
            )
        self.assertEqual(code, 0)
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["network_activity"])
        self.assertFalse(payload["arkham_required"])

    def test_complete_scan_builds_local_unknown_queue(self) -> None:
        payload = activity(
            transfers=[
                record(
                    1,
                    block_time=900,
                    from_address=ADDRESS_A,
                    to_address=ADDRESS_B,
                    amount="10",
                    flow_type="unclassified",
                ),
                record(
                    2,
                    block_time=901,
                    from_address=ADDRESS_A,
                    to_address=CEX,
                    amount="20",
                    flow_type="inflow",
                ),
            ],
        )
        payload["analysis"] = {
            "complete": True,
            "primary_behavior": {
                "type": "wallet_consolidation_candidate"
            },
        }
        result = self.store.observe_complete_scan(payload, observed_at=1000)
        self.assertEqual(result["created"], 2)
        queue = self.store.unknown_queue(limit=10)
        self.assertEqual(queue[0]["address"], ADDRESS_A)
        self.assertEqual(queue[0]["window_count"], 1)
        self.assertTrue(queue[0]["known_cex_adjacent"])
        self.assertIn(
            "collector_candidate", queue[0]["behavior_roles"]
        )
        self.assertFalse(
            any("provider" in item for item in queue)
        )

    def test_partial_scan_does_not_queue_addresses(self) -> None:
        payload = activity(complete=False)
        payload["analysis"] = {"complete": True}
        self.assertEqual(
            self.store.observe_complete_scan(payload)["observed"], 0
        )
        self.assertFalse(self.settings.address_intelligence_path.exists())

    def test_same_window_is_idempotent(self) -> None:
        payload = activity(
            transfers=[
                record(
                    1,
                    block_time=900,
                    from_address=ADDRESS_A,
                    to_address=ADDRESS_B,
                    amount="10",
                    flow_type="unclassified",
                )
            ],
        )
        payload["analysis"] = {"complete": True, "primary_behavior": {}}
        self.store.observe_complete_scan(payload, observed_at=1000)
        self.store.observe_complete_scan(payload, observed_at=1001)
        queue = self.store.unknown_queue(limit=10)
        self.assertEqual(queue[0]["window_count"], 1)
        self.assertEqual(queue[0]["trigger_signal_count"], 1)

    def test_behavior_candidate_never_becomes_cex_identity(self) -> None:
        discovered = BehaviorInferenceProvider().discover([
            {
                "address": ADDRESS_A,
                "behavior_roles": ["collector_candidate"],
                "trigger_signal_count": 6,
                "window_count": 2,
                "associated_wallet_count": 4,
                "last_seen_at": 1000,
            }
        ])
        self.assertEqual(discovered[0]["entity_name"], "")
        self.assertEqual(discovered[0]["entity_type"], "")
        self.store.merge_candidates(discovered, now=1000)
        with self.assertRaises(AddressIntelligenceError) as caught:
            self.store.approve(
                str(discovered[0]["candidate_id"]),
                labels_path=self.labels,
                reviewed_at=1001,
            )
        self.assertEqual(
            caught.exception.code,
            "behavior_candidate_not_production_identity",
        )

    def test_dune_manual_csv_enters_pending_only(self) -> None:
        path = self.root / "dune.csv"
        path.write_text(
            "blockchain,address,cex_name,source\n"
            f"base,{ADDRESS_A},Coinbase,dune-export\n",
            encoding="utf-8",
        )
        imported = ManualCsvProvider("dune_cex").import_csv(
            path, observed_at=1000
        )
        self.assertEqual(imported[0]["status"], "pending")
        self.store.merge_candidates(imported, now=1000)
        self.assertFalse(self.labels.exists())

    def test_dune_automatic_sync_is_explicit_and_bounded(self) -> None:
        session = FakeSession([
            FakeResponse(200, {
                "result": {
                    "rows": [{
                        "blockchain": "base",
                        "address": ADDRESS_A,
                        "cex_name": "Coinbase",
                    }]
                }
            })
        ])
        provider = DuneAddressProvider(
            provider_name="dune_cex",
            api_key="fake-key",
            session=session,
            max_requests=2,
            max_rows=10,
        )
        rows = provider.discover([{"address": ADDRESS_A}])
        self.assertEqual(len(rows), 1)
        self.assertEqual(provider.request_count, 1)
        self.assertEqual(rows[0]["status"], "pending")
        self.assertNotIn("fake-key", json.dumps(rows))

    def test_oli_import_keeps_attester_and_non_cex_label(self) -> None:
        rows = [{
            "chain_id": "eip155:8453",
            "address": ADDRESS_A,
            "entity_name": "Bridge Project",
            "entity_type": "bridge",
            "address_role": "bridge",
            "attester": "oli-attester",
            "confidence": 0.8,
        }]
        imported = OliParquetProvider().import_parquet(
            self.root / "oli.parquet",
            row_reader=lambda _path: rows,
            observed_at=1000,
        )
        self.assertEqual(imported[0]["provider"], "oli")
        self.assertEqual(imported[0]["entity_type"], "bridge")
        self.assertNotEqual(imported[0]["entity_type"], "cex")
        self.assertEqual(imported[0]["source_ref"], "oli-attester")

    def test_conflicting_candidates_fail_closed(self) -> None:
        first = candidate(name="Exchange A")
        second = candidate(
            provider="oli",
            name="Exchange B",
        )
        self.store.merge_candidates([first, second], now=1000)
        conflicted = self.store.list_candidates(
            status="conflicted"
        )
        self.assertEqual(len(conflicted), 2)
        with self.assertRaises(AddressIntelligenceError):
            self.store.approve(
                str(first["candidate_id"]),
                labels_path=self.labels,
                reviewed_at=1001,
            )

    def test_manual_approval_writes_audited_private_label(self) -> None:
        item = candidate()
        self.store.merge_candidates([item], now=1000)
        result = self.store.approve(
            str(item["candidate_id"]),
            labels_path=self.labels,
            reviewed_at=1001,
        )
        self.assertEqual(result["candidate_status"], "approved")
        labels = load_labels_csv(self.labels)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0].review_status, "approved")
        self.assertEqual(labels[0].evidence_hash, item["evidence_hash"])
        self.assertIn("manual_review", labels[0].source)
        if os.name == "posix":
            self.assertEqual(self.labels.stat().st_mode & 0o777, 0o600)

    def test_pending_candidate_never_enters_production_csv(self) -> None:
        item = candidate()
        self.store.merge_candidates([item], now=1000)
        self.assertFalse(self.labels.exists())
        self.assertEqual(
            self.store.list_candidates()[0]["status"], "pending"
        )

    def test_external_label_without_manual_review_is_not_approved(self) -> None:
        label = AddressLabel(
            chain_id=8453,
            address=ADDRESS_A,
            entity_name="Exchange A",
            entity_type="cex",
            address_type="hot",
            source="dune_cex",
            confidence=0.99,
        )
        self.assertFalse(is_approved_label(label))

    def test_expired_candidate_is_not_approvable(self) -> None:
        item = candidate(expires_at=1001)
        self.store.merge_candidates([item], now=1000)
        self.store.expire(now=1001)
        self.assertEqual(
            self.store.list_candidates()[0]["status"], "expired"
        )
        with self.assertRaises(AddressIntelligenceError):
            self.store.approve(
                str(item["candidate_id"]),
                labels_path=self.labels,
                reviewed_at=1002,
            )

    def test_source_revocation_removes_matching_approved_label(self) -> None:
        item = candidate()
        self.store.merge_candidates([item], now=1000)
        self.store.approve(
            str(item["candidate_id"]),
            labels_path=self.labels,
            reviewed_at=1001,
        )
        result = self.store.revoke(
            str(item["candidate_id"]),
            labels_path=self.labels,
            reviewed_at=1002,
        )
        self.assertEqual(result["candidate_status"], "expired")
        with self.labels.open(encoding="utf-8") as handle:
            self.assertEqual(len(list(csv.DictReader(handle))), 0)

    def test_production_csv_validation_failure_rolls_back(self) -> None:
        self.labels.parent.mkdir(parents=True)
        self.labels.write_text(
            "chain_id,address,entity_name,entity_type,address_type,source,"
            "confidence,valid_from,valid_to\n"
            f"8453,{ADDRESS_B},Known,wallet,wallet,manual_review,"
            "0.9,1,\n",
            encoding="utf-8",
        )
        original = self.labels.read_bytes()
        item = candidate()
        self.store.merge_candidates([item], now=1000)
        with patch(
            "paopao_radar.onchain_flow.address_intelligence."
            "load_labels_csv",
            side_effect=LabelValidationError("fixture"),
        ):
            with self.assertRaises(LabelValidationError):
                self.store.approve(
                    str(item["candidate_id"]),
                    labels_path=self.labels,
                    reviewed_at=1001,
                )
        self.assertEqual(self.labels.read_bytes(), original)
        self.assertEqual(
            self.store.list_candidates()[0]["status"], "pending"
        )

    def test_provider_failure_is_isolated(self) -> None:
        payload = activity(
            transfers=[
                record(
                    1,
                    block_time=900,
                    from_address=ADDRESS_A,
                    to_address=ADDRESS_B,
                    amount="10",
                    flow_type="unclassified",
                )
            ],
        )
        payload["analysis"] = {"complete": True, "primary_behavior": {}}
        self.store.observe_complete_scan(payload, observed_at=1000)

        class FailingProvider:
            provider_name = "dune_cex"
            configured = True
            source_priority = 70
            network_required = True
            request_count = 1

            def provider_check(self) -> dict[str, object]:
                raise AssertionError

            def discover(
                self, addresses: object
            ) -> list[dict[str, object]]:
                del addresses
                raise AddressIntelligenceError("dune_rate_limited")

        service = AddressIntelligenceService(
            self.settings,
            providers=[FailingProvider()],
            store=self.store,
        )
        result = service.discover(
            provider_names=["dune_cex"],
            allow_network=True,
            limit=50,
        )
        self.assertEqual(result["status"], "ok")
        self.assertFalse(result["core_services_affected"])
        self.assertEqual(result["providers"][0]["status"], "failed")

    def test_candidate_file_contains_no_provider_payload_or_key(self) -> None:
        item = candidate()
        self.store.merge_candidates([item], now=1000)
        text = self.settings.address_intelligence_path.read_text(
            encoding="utf-8"
        )
        self.assertNotIn("Authorization", text)
        self.assertNotIn("fake-key", text)
        self.assertNotIn("provider_payload", text)

    def test_menu_and_config_support_visible_dune_review_flow(self) -> None:
        scripts = Path(__file__).resolve().parents[2] / "scripts"
        menu = (scripts / "paopao_menu.sh").read_text(encoding="utf-8")
        watch_runner = (scripts / "run_oar_watch.sh").read_text(
            encoding="utf-8"
        )
        self.assertEqual(ALLOWLIST["DUNE_API_KEY"], "onchain")
        self.assertIn("DUNE_API_KEY", SECRET_KEYS)
        self.assertIn("地址情报中心", menu)
        self.assertIn('confirm_phrase "批准地址标签"', menu)
        self.assertIn("IFS= read -r import_file", menu)
        self.assertNotIn("read -s", menu)
        self.assertNotIn("ARKHAM", watch_runner)
        self.assertNotIn("DUNE", watch_runner)

    def test_dune_key_is_saved_but_status_remains_redacted(self) -> None:
        manager = ConfigManager(self.root)
        manager.set("DUNE_API_KEY", "fake-dune-secret")
        status = manager.status()
        self.assertEqual(
            status["DUNE_API_KEY"], "configured"
        )
        self.assertNotIn("fake-dune-secret", json.dumps(status))


if __name__ == "__main__":
    unittest.main()
