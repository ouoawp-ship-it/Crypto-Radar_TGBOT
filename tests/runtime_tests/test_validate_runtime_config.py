from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[2]


def load_module():
    path = ROOT / "scripts" / "validate_runtime_config.py"
    spec = importlib.util.spec_from_file_location("validate_runtime_config", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ValidateRuntimeConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    @staticmethod
    def settings(**values):
        return SimpleNamespace(
            tg_bot_token="12345:" + "A" * 25,
            tg_chat_id="-10012345",
            **values,
        )

    def test_disabled_production_is_a_pure_config_preflight(self) -> None:
        result = self.module.validate_runtime_config(
            self.settings(
                altcoin_contract_anomaly_production_enable=False,
                altcoin_contract_anomaly_production_send_enable=False,
            )
        )

        self.assertEqual(
            result,
            {
                "status": "ok",
                "altcoin_production_enabled": False,
                "altcoin_real_send_enabled": False,
            },
        )

    def test_send_cannot_be_enabled_while_production_is_disabled(self) -> None:
        with self.assertRaises(self.module.AltcoinAnomalyConfigError):
            self.module.validate_runtime_config(
                self.settings(
                    altcoin_contract_anomaly_production_enable=False,
                    altcoin_contract_anomaly_production_send_enable=True,
                )
            )

    def test_enabled_production_runs_p2_and_production_config_validation(self) -> None:
        settings = self.settings(
            altcoin_contract_anomaly_production_enable=True,
            altcoin_contract_anomaly_production_send_enable=True,
        )
        with (
            patch.object(
                self.module.AltcoinAnomalyConfig,
                "from_settings",
            ) as p2_validate,
            patch.object(
                self.module.AltcoinAnomalyProductionConfig,
                "from_settings",
            ) as production_validate,
        ):
            result = self.module.validate_runtime_config(settings)

        p2_validate.assert_called_once_with(settings, realtime=True)
        production_validate.assert_called_once_with(
            settings,
            real_send_requested=True,
        )
        self.assertTrue(result["altcoin_production_enabled"])
        self.assertTrue(result["altcoin_real_send_enabled"])


if __name__ == "__main__":
    unittest.main()
