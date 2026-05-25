from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import TelegramGateway, utc_ts


class TelegramGatewayTests(unittest.TestCase):
    def test_dry_run_records_without_real_send(self) -> None:
        with TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "push_history.json"
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=history_path,
                tg_default_cooldown_sec=3600,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with redirect_stdout(StringIO()):
                result = gateway.send(
                    "hello",
                    "TEST_TEMPLATE",
                    "test:key",
                    send=False,
                    confirm_real_send=False,
                )

            self.assertEqual(result.status, "dry_run")
            history = JsonStore(Path(tmp)).load(history_path, [])
            self.assertEqual(len(history), 1)
            self.assertFalse(history[0]["sent"])

    def test_real_send_requires_explicit_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            result = gateway.send(
                "hello",
                "TEST_TEMPLATE",
                "test:key",
                send=True,
                confirm_real_send=False,
            )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.reason, "missing_confirm_real_send")

    def test_template_daily_limit_blocks_after_sent_count(self) -> None:
        with TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "push_history.json"
            store = JsonStore(Path(tmp))
            store.save(history_path, [{
                "ts": utc_ts(),
                "template_id": "TG_RADAR_SUMMARY",
                "dedup_key": "old",
                "status": "sent",
                "sent": True,
            }])
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=history_path,
            )
            gateway = TelegramGateway(settings, store)

            result = gateway.send(
                "hello",
                "TG_RADAR_SUMMARY",
                "new",
                send=True,
                confirm_real_send=False,
                daily_limit=1,
            )

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason, "template_daily_limit")


if __name__ == "__main__":
    unittest.main()
