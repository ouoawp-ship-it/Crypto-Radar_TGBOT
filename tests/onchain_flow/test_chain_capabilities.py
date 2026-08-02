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


if __name__ == "__main__":
    unittest.main()
