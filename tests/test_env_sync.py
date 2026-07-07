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
                    "COINALYZE_API_KEY=secret_key",
                    "RADAR_SUMMARY_MIN_INTERVAL_SEC=1800",
                    "RADAR_SUMMARY_MAX_DAILY_PUSH=6",
                    "FLOW_INTERVAL_SEC=900",
                    "WEB_HOST=127.0.0.1",
                    "WEB_PORT=80",
                    "WEB_ADMIN_TOKEN=",
                    "AI_REQUEST_TIMEOUT_SEC=20",
                    "AI_ALLOWED_CHAT_IDS=-1001111111111,@vip_channel",
                    "CUSTOM_KEEP=1",
                ]) + "\n",
                encoding="utf-8",
            )
            example.write_text(
                "\n".join([
                    "TG_BOT_TOKEN=",
                    "TG_CHAT_ID=",
                    "COINALYZE_API_KEY=",
                    "RADAR_SUMMARY_MIN_INTERVAL_SEC=21600",
                    "RADAR_SUMMARY_MAX_DAILY_PUSH=4",
                    "FLOW_INTERVAL_SEC=3600",
                    "WEB_HOST=0.0.0.0",
                    "WEB_PORT=8080",
                    "WEB_AUTH_MODE=password",
                    "WEB_ADMIN_USERNAME=admin",
                    "WEB_ADMIN_PASSWORD_HASH=",
                    "WEB_SESSION_SECRET=",
                    "WEB_SESSION_TTL_SEC=86400",
                    "WEB_AUTH_COOKIE_NAME=paopao_admin_session",
                    "WEB_AUTH_MAX_FAILURES=5",
                    "WEB_AUTH_LOCKOUT_SEC=600",
                    "WEB_AUTH_FAILURE_WINDOW_SEC=900",
                    "WEB_AUTH_AUDIT_LIMIT=500",
                    "WEB_SESSION_REFRESH_THRESHOLD_RATIO=0.5",
                    "WEB_ADMIN_TOKEN=",
                    "AI_REQUEST_TIMEOUT_SEC=90",
                    "AI_ALLOWED_CHAT_IDS=",
                    "NEW_NORMAL_SETTING=true",
                ]) + "\n",
                encoding="utf-8",
            )

            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")

        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC=21600", text)
        self.assertIn("RADAR_SUMMARY_MAX_DAILY_PUSH=4", text)
        self.assertIn("FLOW_INTERVAL_SEC=3600", text)
        self.assertIn("WEB_HOST=0.0.0.0", text)
        self.assertIn("WEB_PORT=8080", text)
        self.assertIn("WEB_AUTH_MODE=password", text)
        self.assertIn("WEB_ADMIN_USERNAME=admin", text)
        self.assertIn("WEB_ADMIN_PASSWORD_HASH=", text)
        self.assertIn("WEB_SESSION_SECRET=", text)
        self.assertIn("WEB_SESSION_TTL_SEC=86400", text)
        self.assertIn("WEB_AUTH_COOKIE_NAME=paopao_admin_session", text)
        self.assertIn("WEB_AUTH_MAX_FAILURES=5", text)
        self.assertIn("WEB_AUTH_LOCKOUT_SEC=600", text)
        self.assertIn("WEB_AUTH_FAILURE_WINDOW_SEC=900", text)
        self.assertIn("WEB_AUTH_AUDIT_LIMIT=500", text)
        self.assertIn("WEB_SESSION_REFRESH_THRESHOLD_RATIO=0.5", text)
        self.assertIn("WEB_ADMIN_TOKEN=", text)
        self.assertIn("AI_REQUEST_TIMEOUT_SEC=90", text)
        self.assertIn("NEW_NORMAL_SETTING=true", text)
        self.assertIn("TG_BOT_TOKEN=123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ", text)
        self.assertIn("TG_CHAT_ID=-1001234567890", text)
        self.assertIn("COINALYZE_API_KEY=secret_key", text)
        self.assertIn("AI_ALLOWED_CHAT_IDS=-1001111111111,@vip_channel", text)
        self.assertIn("CUSTOM_KEEP=1", text)
        self.assertEqual(set(result["updated"]), {
            "RADAR_SUMMARY_MIN_INTERVAL_SEC",
            "RADAR_SUMMARY_MAX_DAILY_PUSH",
            "FLOW_INTERVAL_SEC",
            "WEB_HOST",
            "WEB_PORT",
            "AI_REQUEST_TIMEOUT_SEC",
        })

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
