from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def load_sync_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "scripts" / "sync_env.py"
    spec = importlib.util.spec_from_file_location("sync_env", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnvSyncTests(unittest.TestCase):
    def test_sync_updates_managed_defaults_without_touching_secrets(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "\n".join([
                    "TG_BOT_TOKEN=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    "TG_CHAT_ID=-1001234567890",
                    "COINGLASS_API_KEY=secret_key",
                    "RADAR_SUMMARY_MIN_INTERVAL_SEC=1800",
                    "RADAR_SUMMARY_MAX_DAILY_PUSH=6",
                    "FLOW_INTERVAL_SEC=900",
                    "CUSTOM_KEEP=1",
                ]) + "\n",
                encoding="utf-8",
            )
            example.write_text(
                "\n".join([
                    "TG_BOT_TOKEN=",
                    "TG_CHAT_ID=",
                    "COINGLASS_API_KEY=",
                    "RADAR_SUMMARY_MIN_INTERVAL_SEC=21600",
                    "RADAR_SUMMARY_MAX_DAILY_PUSH=4",
                    "FLOW_INTERVAL_SEC=3600",
                    "NEW_NORMAL_SETTING=true",
                ]) + "\n",
                encoding="utf-8",
            )

            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC=21600", text)
        self.assertIn("RADAR_SUMMARY_MAX_DAILY_PUSH=4", text)
        self.assertIn("FLOW_INTERVAL_SEC=3600", text)
        self.assertIn("NEW_NORMAL_SETTING=true", text)
        self.assertIn("TG_BOT_TOKEN=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ", text)
        self.assertIn("TG_CHAT_ID=-1001234567890", text)
        self.assertIn("COINGLASS_API_KEY=secret_key", text)
        self.assertIn("CUSTOM_KEEP=1", text)
        self.assertEqual(set(result["updated"]), {"RADAR_SUMMARY_MIN_INTERVAL_SEC", "RADAR_SUMMARY_MAX_DAILY_PUSH", "FLOW_INTERVAL_SEC"})

    def test_sync_keeps_custom_managed_value(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text("RADAR_SUMMARY_MIN_INTERVAL_SEC=3600\n", encoding="utf-8")
            example.write_text("RADAR_SUMMARY_MIN_INTERVAL_SEC=21600\n", encoding="utf-8")

            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC=3600", text)
        self.assertEqual(result["updated"], [])


if __name__ == "__main__":
    unittest.main()
