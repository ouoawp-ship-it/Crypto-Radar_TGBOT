from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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

    def test_template_specific_topic_routes_are_recorded(self) -> None:
        with TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "push_history.json"
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=history_path,
                tg_topic_id="10",
                tg_radar_summary_topic_id="11",
                tg_launch_alert_topic_id="12",
                tg_announcement_alert_topic_id="13",
                tg_test_topic_id="14",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with redirect_stdout(StringIO()):
                gateway.send("summary", "TG_RADAR_SUMMARY", "summary:key", send=False, confirm_real_send=False)
                gateway.send("launch", "TG_LAUNCH_ALERT", "launch:key", send=False, confirm_real_send=False)
                gateway.send("announcement", "TG_ANNOUNCEMENT_ALERT", "announcement:key", send=False, confirm_real_send=False)
                gateway.send("test", "TG_TEST_MESSAGE", "test:key", send=False, confirm_real_send=False)
                gateway.send("other", "OTHER_TEMPLATE", "other:key", send=False, confirm_real_send=False)

            history = JsonStore(Path(tmp)).load(history_path, [])
            self.assertEqual([record["topic_id"] for record in history], ["11", "12", "13", "14", "10"])

    def test_auto_created_topic_is_reused_from_state(self) -> None:
        with TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "topic_routes.json"
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=route_path,
                tg_bot_token="123456:ABCDEF",
                tg_chat_id="-1001234567890",
                tg_auto_create_topics=True,
                tg_use_topic=True,
            )
            gateway = TelegramGateway(settings, store)

            created: list[str] = []

            def fake_create(name: str) -> str:
                created.append(name)
                return "42"

            with patch.object(gateway, "_create_forum_topic", side_effect=fake_create):
                self.assertEqual(gateway._ensure_topic_id_for_template("TG_RADAR_SUMMARY"), "42")

            self.assertEqual(created, ["资金摘要"])
            self.assertEqual(gateway._ensure_topic_id_for_template("TG_RADAR_SUMMARY"), "42")
            data = store.load(route_path, {})
            self.assertEqual(data["routes"]["TG_RADAR_SUMMARY"]["topic_id"], "42")

    def test_configured_topic_overrides_saved_route(self) -> None:
        with TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "topic_routes.json"
            store = JsonStore(Path(tmp))
            store.save(route_path, {
                "routes": {
                    "TG_RADAR_SUMMARY": {
                        "name": "资金摘要",
                        "topic_id": "42",
                    }
                }
            })
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=route_path,
                tg_radar_summary_topic_id="99",
            )
            gateway = TelegramGateway(settings, store)

            self.assertEqual(gateway._topic_id_for_template("TG_RADAR_SUMMARY"), "99")


if __name__ == "__main__":
    unittest.main()
