from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import TelegramGateway, utc_ts


CST = timezone(timedelta(hours=8))


class TelegramGatewayTests(unittest.TestCase):
    def test_send_photo_dry_run_records_without_real_send(self) -> None:
        with TemporaryDirectory() as tmp:
            photo = Path(tmp) / "chart.png"
            photo.write_bytes(b"\x89PNG\r\n\x1a\n")
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "history.json",
                tg_topic_routes_path=Path(tmp) / "routes.json",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            result = gateway.send_photo(
                photo,
                "caption",
                "TG_STRUCTURE_RADAR",
                "photo:unit",
                send=False,
                confirm_real_send=False,
            )
            history = JsonStore(Path(tmp)).load(settings.tg_push_history_path, [])

        self.assertEqual(result.status, "dry_run")
        self.assertFalse(result.sent)
        self.assertEqual(history[-1]["template_id"], "TG_STRUCTURE_RADAR")

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

    def test_template_daily_limit_uses_cst_day_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "push_history.json"
            store = JsonStore(Path(tmp))
            store.save(history_path, [{
                "ts": int(datetime(2026, 5, 26, 10, 0, tzinfo=CST).timestamp()),
                "template_id": "TG_RADAR_SUMMARY",
                "dedup_key": "previous-cst-day",
                "status": "sent",
                "sent": True,
            }])
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=history_path,
            )
            gateway = TelegramGateway(settings, store)
            now = int(datetime(2026, 5, 27, 0, 5, tzinfo=CST).timestamp())

            with patch("paopao_radar.telegram.utc_ts", return_value=now):
                result = gateway.send(
                    "hello",
                    "TG_RADAR_SUMMARY",
                    "new-cst-day",
                    send=True,
                    confirm_real_send=False,
                    daily_limit=1,
                )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.reason, "missing_confirm_real_send")

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
                tg_flow_radar_topic_id="15",
                tg_structure_topic_id="16",
                tg_structure_review_topic_id="17",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with redirect_stdout(StringIO()):
                gateway.send("summary", "TG_RADAR_SUMMARY", "summary:key", send=False, confirm_real_send=False)
                gateway.send("launch", "TG_LAUNCH_ALERT", "launch:key", send=False, confirm_real_send=False)
                gateway.send("announcement", "TG_ANNOUNCEMENT_ALERT", "announcement:key", send=False, confirm_real_send=False)
                gateway.send("test", "TG_TEST_MESSAGE", "test:key", send=False, confirm_real_send=False)
                gateway.send("flow", "TG_FLOW_RADAR", "flow:key", send=False, confirm_real_send=False)
                gateway.send("structure", "TG_STRUCTURE_RADAR", "structure:key", send=False, confirm_real_send=False)
                gateway.send("review", "TG_STRUCTURE_REVIEW", "review:key", send=False, confirm_real_send=False)
                gateway.send("other", "OTHER_TEMPLATE", "other:key", send=False, confirm_real_send=False)

            history = JsonStore(Path(tmp)).load(history_path, [])
            self.assertEqual([record["topic_id"] for record in history], ["11", "12", "13", "14", "15", "16", "17", "10"])

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

    def test_send_passes_reply_message_id_to_real_sender(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_launch_alert_topic_id="12",
                tg_auto_create_topics=False,
                tg_default_cooldown_sec=0,
                tg_topic_intro_enable=False,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with patch.object(gateway, "_send_real_message_ids", return_value=(True, [222])) as send_mock:
                result = gateway.send(
                    "launch",
                    "TG_LAUNCH_ALERT",
                    "launch:key",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                    reply_to_message_id=111,
                )

            self.assertTrue(result.sent)
            self.assertEqual(send_mock.call_args.kwargs["reply_to_message_id"], 111)

    def test_real_sender_adds_reply_payload_on_first_chunk(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_use_topic=True,
                tg_push_split_limit=10,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            class Response:
                status_code = 200

                @staticmethod
                def json() -> dict[str, object]:
                    return {"result": {"message_id": 222}}

            with patch("paopao_radar.telegram.requests.post", return_value=Response()) as post_mock:
                ok, message_ids = gateway._send_real_message_ids(
                    "first line\nsecond line",
                    parse_mode="HTML",
                    topic_id="12",
                    reply_to_message_id=111,
                )

            self.assertTrue(ok)
            self.assertEqual(message_ids, [222, 222])
            first_payload = post_mock.call_args_list[0].kwargs["json"]
            second_payload = post_mock.call_args_list[1].kwargs["json"]
            self.assertEqual(first_payload["reply_to_message_id"], 111)
            self.assertTrue(first_payload["allow_sending_without_reply"])
            self.assertEqual(first_payload["message_thread_id"], 12)
            self.assertNotIn("reply_to_message_id", second_payload)

    def test_real_sender_falls_back_when_reply_target_invalid(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_use_topic=True,
                tg_push_retry=1,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            class Response400:
                status_code = 400
                text = "bad reply"

                @staticmethod
                def json() -> dict[str, object]:
                    return {}

            class Response200:
                status_code = 200
                text = "ok"

                @staticmethod
                def json() -> dict[str, object]:
                    return {"result": {"message_id": 333}}

            with patch("paopao_radar.telegram.requests.post", side_effect=[Response400(), Response200()]) as post_mock:
                ok, message_ids = gateway._send_real_message_ids(
                    "structure",
                    parse_mode="HTML",
                    topic_id="12",
                    reply_to_message_id=111,
                )

            self.assertTrue(ok)
            self.assertEqual(message_ids, [333])
            first_payload = post_mock.call_args_list[0].kwargs["json"]
            second_payload = post_mock.call_args_list[1].kwargs["json"]
            self.assertEqual(first_payload["reply_to_message_id"], 111)
            self.assertNotIn("reply_to_message_id", second_payload)

    def test_auto_create_precedes_default_topic_for_known_templates(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_topic_id="10",
                tg_bot_token="123456:ABCDEF",
                tg_chat_id="-1001234567890",
                tg_auto_create_topics=True,
                tg_use_topic=True,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with patch.object(gateway, "_create_forum_topic", return_value="42"):
                self.assertEqual(gateway._ensure_topic_id_for_template("TG_TEST_MESSAGE"), "42")

    def test_default_topic_is_fallback_when_auto_create_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_topic_id="10",
                tg_bot_token="123456:ABCDEF",
                tg_chat_id="-1001234567890",
                tg_auto_create_topics=True,
                tg_use_topic=True,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with patch.object(gateway, "_create_forum_topic", return_value=""):
                self.assertEqual(gateway._ensure_topic_id_for_template("TG_TEST_MESSAGE"), "10")

    def test_real_send_posts_and_pins_topic_intro_once(self) -> None:
        with TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "topic_routes.json"
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=route_path,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_radar_summary_topic_id="11",
                tg_use_topic=True,
                tg_topic_intro_enable=True,
                tg_topic_intro_pin=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(gateway, "_send_real_message_ids", side_effect=[(True, [100]), (True, [101]), (True, [102])]) as send_mock,
                patch.object(gateway, "_pin_message", return_value=True) as pin_mock,
            ):
                first = gateway.send(
                    "summary one",
                    "TG_RADAR_SUMMARY",
                    "summary:one",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )
                second = gateway.send(
                    "summary two",
                    "TG_RADAR_SUMMARY",
                    "summary:two",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertTrue(first.sent)
            self.assertTrue(second.sent)
            self.assertEqual(send_mock.call_count, 3)
            self.assertIn("资金摘要话题说明", send_mock.call_args_list[0].args[0])
            self.assertEqual(send_mock.call_args_list[1].args[0], "summary one")
            self.assertEqual(send_mock.call_args_list[2].args[0], "summary two")
            pin_mock.assert_called_once_with(100)
            data = store.load(route_path, {})
            self.assertEqual(data["intros"]["TG_RADAR_SUMMARY:11"]["message_id"], 100)
            self.assertTrue(data["intros"]["TG_RADAR_SUMMARY:11"]["pinned"])
            self.assertIn("content_hash", data["intros"]["TG_RADAR_SUMMARY:11"])
            self.assertIn("intro_version", data["intros"]["TG_RADAR_SUMMARY:11"])

    def test_topic_intro_refreshes_when_content_version_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "topic_routes.json"
            store = JsonStore(Path(tmp))
            store.save(route_path, {
                "intros": {
                    "TG_RADAR_SUMMARY:11": {
                        "template_id": "TG_RADAR_SUMMARY",
                        "topic_id": "11",
                        "message_id": 99,
                        "pinned": True,
                        "intro_version": "old",
                        "content_hash": "old",
                    }
                }
            })
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=route_path,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_radar_summary_topic_id="11",
                tg_use_topic=True,
                tg_topic_intro_enable=True,
                tg_topic_intro_pin=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(gateway, "_delete_message", return_value=True) as delete_mock,
                patch.object(gateway, "_send_real_message_ids", side_effect=[(True, [100]), (True, [101])]) as send_mock,
                patch.object(gateway, "_pin_message", return_value=True) as pin_mock,
            ):
                result = gateway.send(
                    "summary",
                    "TG_RADAR_SUMMARY",
                    "summary:key",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertTrue(result.sent)
            delete_mock.assert_called_once_with(99)
            pin_mock.assert_called_once_with(100)
            self.assertEqual(send_mock.call_count, 2)
            self.assertIn("扫描和发送频率", send_mock.call_args_list[0].args[0])
            self.assertEqual(send_mock.call_args_list[1].args[0], "summary")
            data = store.load(route_path, {})
            record = data["intros"]["TG_RADAR_SUMMARY:11"]
            self.assertEqual(record["message_id"], 100)
            self.assertTrue(record["pinned"])
            self.assertNotEqual(record["content_hash"], "old")

    def test_flow_intro_mentions_hourly_schedule_and_all_categories(self) -> None:
        with TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "topic_routes.json"
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=route_path,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_flow_radar_topic_id="15",
                tg_use_topic=True,
                tg_topic_intro_enable=True,
                tg_topic_intro_pin=False,
                tg_default_cooldown_sec=0,
                flow_interval_sec=3600,
            )
            gateway = TelegramGateway(settings, store)

            with patch.object(gateway, "_send_real_message_ids", side_effect=[(True, [100]), (True, [101])]) as send_mock:
                result = gateway.send(
                    "flow",
                    "TG_FLOW_RADAR",
                    "flow:key",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertTrue(result.sent)
            intro = send_mock.call_args_list[0].args[0]
            self.assertIn("默认每1小时扫描一次，并在整点收线后延迟5分钟发送", intro)
            self.assertIn("统计上一完整闭合窗口", intro)
            self.assertIn("使用 Binance 免费公开数据", intro)
            self.assertIn("真启动候选、吸筹观察、空头燃料、合约拉盘、挤空/止损、诱多/派发、恐慌下跌", intro)


if __name__ == "__main__":
    unittest.main()
