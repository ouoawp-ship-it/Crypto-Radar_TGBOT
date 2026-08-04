from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from paopao_radar.onchain_flow.chain_capabilities import (
    ChainCapabilityError,
    chain_capability_report,
    load_evm_chain_specs,
    resolve_evm_chain,
)
from paopao_radar.onchain_flow.cli import build_parser, main

from tests.onchain_flow.support import make_settings


class ChainCapabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_evm_specs_expose_namespace_aware_chain_identity(self) -> None:
        _schema, specs = load_evm_chain_specs(self.settings.chains_path)
        by_slug = {item.slug: item for item in specs}

        self.assertEqual(by_slug["base"].chain_ref.key, "eip155:8453")
        self.assertEqual(by_slug["ethereum"].chain_ref.key, "eip155:1")
        self.assertEqual(by_slug["bsc"].chain_ref.family, "evm")

    def write_registry(self, chains: list[dict[str, object]]) -> Path:
        path = self.root / "chains.json"
        path.write_text(
            json.dumps({"schema_version": "test-v1", "chains": chains}),
            encoding="utf-8",
        )
        return path

    def test_base_adapter_reports_offline_readiness_without_network(self) -> None:
        path = self.write_registry(
            [
                {
                    "chain_id": 8453,
                    "name": "Base",
                    "enabled": True,
                    "confirmation_depth": 20,
                    "bootstrap_lookback_blocks": 300,
                    "reorg_lookback_blocks": 64,
                    "http_rpc_env": "ONCHAIN_BASE_HTTP_RPC_URL",
                    "explorer_tx_url": "https://base.invalid/tx/{tx_hash}",
                }
            ]
        )
        settings = replace(
            self.settings,
            chains_path=path,
            base_enable=True,
            base_http_rpc_url="https://base.invalid/v2/key",
        )
        with patch("requests.sessions.Session.request") as request:
            result = chain_capability_report(settings, environ={})

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["runtime_ready_chain_count"], 1)
        self.assertFalse(result["multichain_runtime_ready"])
        self.assertTrue(result["chains"][0]["token_activity_supported"])
        self.assertFalse(result["network_activity"])
        self.assertFalse(result["database_writes"])
        request.assert_not_called()
        self.assertNotIn("base.invalid", json.dumps(result))

    def test_base_watch_uses_registry_enable_when_collector_is_disabled(
        self,
    ) -> None:
        path = self.write_registry(
            [
                {
                    "chain_id": 8453,
                    "slug": "base",
                    "name": "Base",
                    "enabled": True,
                    "confirmation_depth": 20,
                    "bootstrap_lookback_blocks": 300,
                    "reorg_lookback_blocks": 64,
                    "http_rpc_env": "ONCHAIN_BASE_HTTP_RPC_URL",
                    "explorer_tx_url": "https://base.invalid/tx/{tx_hash}",
                }
            ]
        )
        settings = replace(
            self.settings,
            chains_path=path,
            base_enable=False,
            oar_automation_enable=True,
            base_http_rpc_url="https://base.invalid/v2/key",
        )

        result = chain_capability_report(settings, environ={})

        chain = result["chains"][0]
        self.assertFalse(settings.base_enable)
        self.assertTrue(chain["configured_enabled"])
        self.assertTrue(chain["effective_enabled"])
        self.assertEqual(chain["watch_status"], "ready_offline")
        self.assertTrue(chain["watch_supported"])

    def test_base_collector_enable_keeps_legacy_chain_ready(self) -> None:
        path = self.write_registry(
            [
                {
                    "chain_id": 8453,
                    "slug": "base",
                    "name": "Base",
                    "enabled": False,
                    "confirmation_depth": 20,
                    "bootstrap_lookback_blocks": 300,
                    "reorg_lookback_blocks": 64,
                    "http_rpc_env": "ONCHAIN_BASE_HTTP_RPC_URL",
                    "explorer_tx_url": "https://base.invalid/tx/{tx_hash}",
                }
            ]
        )
        settings = replace(
            self.settings,
            chains_path=path,
            base_enable=True,
            base_http_rpc_url="https://base.invalid/v2/key",
        )

        chain = chain_capability_report(settings, environ={})["chains"][0]

        self.assertFalse(chain["configured_enabled"])
        self.assertTrue(chain["effective_enabled"])
        self.assertTrue(chain["watch_supported"])

    def test_enabled_evm_chain_reports_watch_readiness_offline(self) -> None:
        path = self.write_registry(
            [
                {
                    "chain_id": 1,
                    "name": "Ethereum",
                    "enabled": True,
                    "confirmation_depth": 64,
                    "bootstrap_lookback_blocks": 512,
                    "reorg_lookback_blocks": 128,
                    "http_rpc_env": "ONCHAIN_ETHEREUM_HTTP_RPC_URL",
                    "explorer_tx_url": "https://eth.invalid/tx/{tx_hash}",
                }
            ]
        )
        settings = replace(self.settings, chains_path=path)
        result = chain_capability_report(
            settings,
            environ={
                "ONCHAIN_ETHEREUM_HTTP_RPC_URL": "https://eth.invalid/key"
            },
        )

        chain = result["chains"][0]
        self.assertEqual(chain["token_activity_status"], "ready_offline")
        self.assertEqual(chain["watch_status"], "ready_offline")
        self.assertEqual(chain["watch_adapter"], "evm_watch_v1")
        self.assertFalse(chain["activation_blocked"])
        self.assertTrue(chain["token_activity_supported"])
        self.assertTrue(chain["watch_supported"])
        self.assertNotIn("eth.invalid", json.dumps(result))

    def test_duplicate_chain_identity_is_rejected(self) -> None:
        path = self.write_registry(
            [
                {
                    "chain_id": 8453,
                    "name": "Base",
                    "confirmation_depth": 20,
                    "bootstrap_lookback_blocks": 300,
                    "reorg_lookback_blocks": 64,
                    "http_rpc_env": "ONCHAIN_BASE_HTTP_RPC_URL",
                    "explorer_tx_url": "https://base.invalid/tx/{tx_hash}",
                },
                {
                    "chain_id": 8453,
                    "name": "Other",
                    "confirmation_depth": 20,
                    "bootstrap_lookback_blocks": 300,
                    "reorg_lookback_blocks": 64,
                    "http_rpc_env": "ONCHAIN_OTHER_HTTP_RPC_URL",
                    "explorer_tx_url": "https://other.invalid/tx/{tx_hash}",
                },
            ]
        )
        with self.assertRaisesRegex(
            ChainCapabilityError, "chain_registry_duplicate"
        ):
            chain_capability_report(
                replace(self.settings, chains_path=path), environ={}
            )

    def test_invalid_rpc_env_name_is_rejected(self) -> None:
        path = self.write_registry(
            [
                {
                    "chain_id": 1,
                    "name": "Ethereum",
                    "confirmation_depth": 64,
                    "bootstrap_lookback_blocks": 512,
                    "reorg_lookback_blocks": 128,
                    "http_rpc_env": "SECRET",
                    "explorer_tx_url": "https://eth.invalid/tx/{tx_hash}",
                }
            ]
        )
        with self.assertRaisesRegex(
            ChainCapabilityError, "chain_registry_invalid"
        ):
            chain_capability_report(
                replace(self.settings, chains_path=path), environ={}
            )

    def test_chain_readiness_cli_is_registered_and_read_only(self) -> None:
        self.assertEqual(
            build_parser().parse_args(["chain-readiness"]).command,
            "chain-readiness",
        )
        output = io.StringIO()
        with (
            patch("requests.sessions.Session.request") as request,
            redirect_stdout(output),
        ):
            code = main(["chain-readiness"], settings=self.settings)

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertFalse(payload["network_activity"])
        self.assertFalse(payload["database_writes"])
        self.assertFalse(self.settings.data_dir.exists())
        request.assert_not_called()

    def test_repository_registry_contains_disabled_bsc_generic_evm_spec(self) -> None:
        repository_registry = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "onchain"
            / "chains.example.json"
        )
        schema, specs = load_evm_chain_specs(repository_registry)
        by_id = {spec.chain_id: spec for spec in specs}
        self.assertEqual(schema, "oar-chain-registry-v2")
        self.assertEqual(
            set(by_id),
            {1, 10, 56, 137, 8453, 42161, 43114},
        )
        bsc = by_id[56]
        self.assertFalse(bsc.enabled)
        self.assertEqual(bsc.http_rpc_env, "OAR_BSC_HTTP_RPC_URL")
        self.assertEqual(bsc.explorer_tx_url, "https://bscscan.com/tx/{tx_hash}")
        self.assertEqual(len(bsc.wrapped_native_token), 42)
        self.assertGreaterEqual(len(bsc.stablecoin_addresses), 3)
        self.assertEqual(
            {name for name, _address in bsc.dex_contracts},
            {
                "pancakeswap_v2_factory",
                "pancakeswap_v2_router",
                "pancakeswap_v3_factory",
                "pancakeswap_v3_router",
            },
        )

        expected = {
            1: (
                "ethereum",
                "OAR_ETHEREUM_ENABLE",
                "OAR_ETHEREUM_HTTP_RPC_URL",
                "https://etherscan.io/tx/{tx_hash}",
            ),
            10: (
                "optimism",
                "OAR_OPTIMISM_ENABLE",
                "OAR_OPTIMISM_HTTP_RPC_URL",
                "https://explorer.optimism.io/tx/{tx_hash}",
            ),
            137: (
                "polygon",
                "OAR_POLYGON_ENABLE",
                "OAR_POLYGON_HTTP_RPC_URL",
                "https://polygonscan.com/tx/{tx_hash}",
            ),
            42161: (
                "arbitrum",
                "OAR_ARBITRUM_ENABLE",
                "OAR_ARBITRUM_HTTP_RPC_URL",
                "https://arbiscan.io/tx/{tx_hash}",
            ),
            43114: (
                "avalanche",
                "OAR_AVALANCHE_ENABLE",
                "OAR_AVALANCHE_HTTP_RPC_URL",
                "https://subnets.avax.network/c-chain/tx/{tx_hash}",
            ),
        }
        for chain_id, values in expected.items():
            with self.subTest(chain_id=chain_id):
                spec = by_id[chain_id]
                self.assertFalse(spec.enabled)
                self.assertEqual(
                    (
                        spec.slug,
                        spec.enable_env,
                        spec.http_rpc_env,
                        spec.explorer_tx_url,
                    ),
                    values,
                )
                self.assertEqual(len(spec.wrapped_native_token), 42)

    def test_generic_evm_enable_env_is_explicit_and_read_only(self) -> None:
        repository_registry = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "onchain"
            / "chains.example.json"
        )
        settings = replace(self.settings, chains_path=repository_registry)
        rpc = "https://ethereum.invalid/private"

        with patch("requests.sessions.Session.request") as request:
            disabled = chain_capability_report(
                settings,
                environ={"OAR_ETHEREUM_HTTP_RPC_URL": rpc},
            )
            enabled = chain_capability_report(
                settings,
                environ={
                    "OAR_ETHEREUM_ENABLE": "true",
                    "OAR_ETHEREUM_HTTP_RPC_URL": rpc,
                },
            )

        disabled_eth = next(
            row for row in disabled["chains"] if row["chain_id"] == 1
        )
        enabled_eth = next(
            row for row in enabled["chains"] if row["chain_id"] == 1
        )
        self.assertTrue(disabled_eth["token_activity_supported"])
        self.assertEqual(disabled_eth["watch_status"], "disabled")
        self.assertTrue(enabled_eth["effective_enabled"])
        self.assertEqual(enabled_eth["watch_status"], "ready_offline")
        self.assertFalse(enabled["network_activity"])
        self.assertNotIn("ethereum.invalid", json.dumps(enabled))
        request.assert_not_called()

    def test_bsc_readiness_is_optional_and_does_not_call_network(self) -> None:
        repository_registry = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "onchain"
            / "chains.example.json"
        )
        disabled = replace(
            self.settings,
            chains_path=repository_registry,
            bsc_enable=False,
            bsc_http_rpc_url="",
        )
        with patch("requests.sessions.Session.request") as request:
            report = chain_capability_report(disabled, environ={})
        bsc = next(row for row in report["chains"] if row["chain_id"] == 56)
        self.assertEqual(bsc["watch_status"], "disabled")
        self.assertEqual(bsc["token_activity_status"], "rpc_not_configured")
        self.assertFalse(report["network_activity"])
        request.assert_not_called()

        enabled = replace(
            disabled,
            bsc_enable=True,
            bsc_http_rpc_url="https://bsc.invalid/key",
            bsc_confirmation_depth=21,
            bsc_reorg_lookback_blocks=80,
        )
        report = chain_capability_report(enabled, environ={})
        bsc = next(row for row in report["chains"] if row["chain_id"] == 56)
        self.assertEqual(bsc["watch_status"], "ready_offline")
        self.assertEqual(bsc["confirmation_depth"], 21)
        self.assertEqual(bsc["reorg_lookback_blocks"], 80)
        self.assertNotIn("bsc.invalid", json.dumps(report))

    def test_resolve_bsc_uses_generic_chain_spec_and_bscscan_link(self) -> None:
        repository_registry = (
            Path(__file__).resolve().parents[2]
            / "config"
            / "onchain"
            / "chains.example.json"
        )
        settings = replace(self.settings, chains_path=repository_registry)
        bsc = resolve_evm_chain(settings, "bsc")
        tx_hash = "0x" + "1" * 64
        self.assertEqual(bsc.chain_id, 56)
        self.assertEqual(
            bsc.transaction_url(tx_hash),
            f"https://bscscan.com/tx/{tx_hash}",
        )


if __name__ == "__main__":
    unittest.main()
