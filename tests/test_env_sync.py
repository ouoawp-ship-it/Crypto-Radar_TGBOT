from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def load_sync_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sync_env.py"
    spec = importlib.util.spec_from_file_location("sync_env", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnvSyncTests(unittest.TestCase):
    def test_sync_updates_defaults_and_preserves_secrets(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "TG_BOT_TOKEN=secret\nTG_CHAT_ID=-1001234567890\nRADAR_SUMMARY_MIN_INTERVAL_SEC=1800\nWEB_PORT=80\nCUSTOM_KEEP=1\n",
                encoding="utf-8",
            )
            example.write_text(
                "TG_BOT_TOKEN=\nTG_CHAT_ID=\nRADAR_SUMMARY_MIN_INTERVAL_SEC=21600\nWEB_PORT=8080\nAI_REQUEST_TIMEOUT_SEC=90\n",
                encoding="utf-8",
            )
            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")
        self.assertIn("TG_BOT_TOKEN=secret", text)
        self.assertIn("TG_CHAT_ID=-1001234567890", text)
        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC=21600", text)
        self.assertIn("WEB_PORT=8080", text)
        self.assertIn("AI_REQUEST_TIMEOUT_SEC=90", text)
        self.assertIn("CUSTOM_KEEP=1", text)
        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC", result["updated"])

if __name__ == "__main__":
    unittest.main()
