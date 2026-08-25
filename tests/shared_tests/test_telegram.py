from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from config import Settings
from shared.signal_store import SignalEventStore
from shared.storage import JsonStore
from shared.telegram import (
    DEFAULT_TOPIC_INTRO_VERSION,
    TOPIC_INTRO_VERSIONS,
    PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS,
    PRODUCTION_TOPIC_TEMPLATE_IDS,
    TOPIC_TEMPLATE_NAMES,
    TelegramGateway,
    intro_hash,
    plain_fallback,
    topic_intro_message,
    topic_intro_version,
    utc_ts,
)


CST = timezone(timedelta(hours=8))


class FakeTelegramResponse:
    status_code = 200

    def __init__(self, message_id: int):
        self._message_id = message_id

    def json(self) -> dict[str, object]:
        return {"ok": True, "result": {"message_id": self._message_id}}


class FakeTelegramSession:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeTelegramResponse:
        self.calls.append({"url": url, **kwargs})
        return FakeTelegramResponse(700 + len(self.calls))


class TelegramGatewayTests(unittest.TestCase):
    def test_topic_automation_defaults_are_disabled(self) -> None:
        settings = Settings()

        self.assertEqual(settings.redacted_status()["telegram"]["topic_management"], "manual_only")

    def test_topic_intro_versions_are_isolated_by_template(self) -> None:
        for template_id in (
            "TG_FUNDING_ALERT",
            "TG_LAUNCH_ALERT",
            "TG_FLOW_RADAR",
        ):
            with self.subTest(template_id=template_id):
                self.assertEqual(
                    topic_intro_version(template_id),
                    TOPIC_INTRO_VERSIONS.get(
                        template_id,
                        DEFAULT_TOPIC_INTRO_VERSION,
                    ),
                )

    def test_detailed_delete_audits_history_and_releases_dedup(self) -> None:
        with TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "push_history.json"
            store = JsonStore(Path(tmp))
            store.save(history_path, [
                {
                    "ts": utc_ts(),
                    "template_id": "TG_LAUNCH_ALERT",
                    "dedup_key": "launch:BTCUSDT:breakout",
                    "status": "sent",
                    "sent": True,
                    "message_ids": [101],
                },
                {
                    "ts": utc_ts(),
                    "template_id": "TG_LAUNCH_ALERT",
                    "dedup_key": "launch:ETHUSDT:breakout",
                    "status": "sent",
                    "sent": True,
                    "message_ids": [102],
                },
            ])
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=history_path,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(gateway, "_delete_message", side_effect=[True, False]),
                patch("shared.telegram.time.sleep"),
            ):
                result = gateway.delete_messages_detailed([101, 102])

            history = store.load(history_path, [])
            self.assertEqual(result, {"deleted_ids": [101], "failed_ids": [102]})
            self.assertTrue(history[0]["lifecycle_deleted"])
            self.assertFalse(history[1].get("lifecycle_deleted", False))
            self.assertFalse(
                gateway._recent_match(history, "launch:BTCUSDT:breakout", 3600)
            )
            self.assertTrue(
                gateway._recent_match(history, "launch:ETHUSDT:breakout", 3600)
            )

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

    def test_signal_store_failure_does_not_block_history_record(self) -> None:
        with TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "push_history.json"
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=history_path,
                signal_events_db_path=Path(tmp) / "signals.db",
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with (
                redirect_stdout(StringIO()),
                patch("shared.signal_store.append_from_push", side_effect=RuntimeError("db down")),
            ):
                result = gateway.send(
                    "BTCUSDT",
                    "TG_LAUNCH_ALERT",
                    "launch:store-failure",
                    send=False,
                    confirm_real_send=False,
                )
            history = JsonStore(Path(tmp)).load(history_path, [])

        self.assertEqual(result.status, "dry_run")
        self.assertFalse(result.signal_store_written)
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["dedup_key"], "launch:store-failure")

    def test_signal_push_writes_only_sqlite_signal_store(self) -> None:
        with TemporaryDirectory() as tmp:
            history_path = Path(tmp) / "push_history.json"
            legacy_events_path = Path(tmp) / "signal_events.json"
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=history_path,
                signal_events_db_path=Path(tmp) / "signals.db",
                tg_default_cooldown_sec=0,
            )
            store = JsonStore(Path(tmp))
            gateway = TelegramGateway(settings, store)

            with redirect_stdout(StringIO()):
                gateway.send(
                    "🚀 脉冲雷达 [GWEI](https://www.coinglass.com/tv/zh/Binance_GWEIUSDT)\n分数: 90",
                    "TG_LAUNCH_ALERT",
                    "launch:GWEI",
                    send=False,
                    confirm_real_send=False,
                )
            events = SignalEventStore(settings.signal_events_db_path).list_signals()["items"]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["symbol"], "GWEIUSDT")
        self.assertEqual(events[0]["signal_type"], "脉冲雷达")
        self.assertFalse(legacy_events_path.exists())

    def test_signal_push_forwards_structured_engine_record_to_sqlite(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                signal_events_db_path=Path(tmp) / "signals.db",
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))
            with redirect_stdout(StringIO()):
                gateway.send(
                    "BTCUSDT 75分",
                    "TG_LAUNCH_ALERT",
                    "launch:structured",
                    send=False,
                    confirm_real_send=False,
                    signal_records=[{"symbol": "BTCUSDT", "score": 91, "stage": "breakout", "price": 123}],
                )
            item = SignalEventStore(settings.signal_events_db_path).list_signals(limit=1)["items"][0]

        self.assertEqual(item["score"], 91)
        self.assertEqual(item["stage"], "breakout")
        self.assertEqual(item["ingest_mode"], "structured")
        self.assertEqual(item["payload"]["facts"]["price"], 123)

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

            with patch("shared.telegram.utc_ts", return_value=now):
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
                tg_test_topic_id="14",
                tg_flow_radar_topic_id="15",
                tg_funding_alert_topic_id="16",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with redirect_stdout(StringIO()):
                gateway.send("summary", "TG_RADAR_SUMMARY", "summary:key", send=False, confirm_real_send=False)
                gateway.send("launch", "TG_LAUNCH_ALERT", "launch:key", send=False, confirm_real_send=False)
                gateway.send("test", "TG_TEST_MESSAGE", "test:key", send=False, confirm_real_send=False)
                gateway.send("flow", "TG_FLOW_RADAR", "flow:key", send=False, confirm_real_send=False)
                gateway.send("funding", "TG_FUNDING_ALERT", "funding:key", send=False, confirm_real_send=False)
                gateway.send("other", "OTHER_TEMPLATE", "other:key", send=False, confirm_real_send=False)

            history = JsonStore(Path(tmp)).load(history_path, [])
            self.assertEqual(
                [record["topic_id"] for record in history],
                ["11", "12", "14", "15", "16", "10"],
            )

    def test_manual_topic_setup_creates_and_reuses_saved_route(self) -> None:
        with TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "topic_routes.json"
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=route_path,
                tg_bot_token="123456:ABCDEF",
                tg_chat_id="-1001234567890",
                tg_use_topic=True,
                tg_topic_intro_pin=True,
            )
            gateway = TelegramGateway(settings, store)

            created: list[str] = []

            def fake_create(name: str) -> str:
                created.append(name)
                return "42"

            with (
                patch.object(gateway, "_create_forum_topic", side_effect=fake_create),
                patch.object(gateway, "_ensure_topic_intro", return_value=True) as intro_mock,
            ):
                result = gateway.setup_topic(
                    "TG_RADAR_SUMMARY",
                    send=True,
                    confirm_real_send=True,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["topic"], "created")
            self.assertEqual(created, ["资金摘要"])
            intro_mock.assert_called_once_with(
                "TG_RADAR_SUMMARY",
                "42",
                require_pin=True,
            )
            self.assertEqual(gateway._ensure_topic_id_for_template("TG_RADAR_SUMMARY"), "42")
            data = store.load(route_path, {})
            self.assertEqual(data["routes"]["TG_RADAR_SUMMARY"]["topic_id"], "42")

    def test_manual_topic_setup_requires_both_real_send_flags(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with patch.object(gateway, "_create_forum_topic") as create_mock:
                without_send = gateway.setup_topic(
                    "TG_RADAR_SUMMARY",
                    send=False,
                    confirm_real_send=True,
                )
                without_confirm = gateway.setup_topic(
                    "TG_RADAR_SUMMARY",
                    send=True,
                    confirm_real_send=False,
                )

            self.assertEqual(without_send["reason"], "send_flag_not_set")
            self.assertEqual(
                without_confirm["reason"],
                "missing_confirm_real_send",
            )
            create_mock.assert_not_called()

    def test_pulse_topic_setup_renames_reused_topic_before_refreshing_intro(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_launch_alert_topic_id="12",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with (
                patch.object(
                    gateway,
                    "_rename_forum_topic",
                    return_value=True,
                ) as rename_mock,
                patch.object(
                    gateway,
                    "_ensure_topic_intro",
                    return_value=True,
                ) as intro_mock,
            ):
                result = gateway.setup_topic(
                    "TG_LAUNCH_ALERT",
                    send=True,
                    confirm_real_send=True,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["topic"], "reused")
            rename_mock.assert_called_once_with("12", "脉冲雷达")
            intro_mock.assert_called_once_with(
                "TG_LAUNCH_ALERT",
                "12",
                require_pin=True,
            )

    def test_pulse_topic_setup_reports_rename_failure_after_refreshing_intro(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_launch_alert_topic_id="12",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with (
                patch.object(gateway, "_rename_forum_topic", return_value=False),
                patch.object(
                    gateway,
                    "_ensure_topic_intro",
                    return_value=True,
                ) as intro_mock,
            ):
                result = gateway.setup_topic(
                    "TG_LAUNCH_ALERT",
                    send=True,
                    confirm_real_send=True,
                )

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["reason"], "telegram_topic_rename_failed")
            self.assertEqual(result["intro"], "published")
            self.assertTrue(result["pinned"])
            intro_mock.assert_called_once()

    def test_rename_forum_topic_uses_existing_thread_and_pulse_name(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))
            session = FakeTelegramSession()

            with patch("shared.telegram.requests.post", side_effect=session.post):
                renamed = gateway._rename_forum_topic("12", "脉冲雷达")

            self.assertTrue(renamed)
            self.assertTrue(str(session.calls[0]["url"]).endswith("/editForumTopic"))
            self.assertEqual(
                session.calls[0]["json"],
                {
                    "chat_id": "-1001234567890",
                    "message_thread_id": 12,
                    "name": "脉冲雷达",
                },
            )

    def test_altcoin_anomaly_topic_is_preconfigured_only_and_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "topic_routes.json"
            store = JsonStore(Path(tmp))
            store.save(route_path, {
                "routes": {
                    "TG_ALTCOIN_CONTRACT_ANOMALY": {
                        "name": "山寨合约异动",
                        "topic_id": "88",
                    },
                    "TG_RADAR_SUMMARY": {
                        "name": "资金摘要",
                        "topic_id": "42",
                    },
                }
            })
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=route_path,
                tg_topic_id="10",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_default_cooldown_sec=0,
                tg_altcoin_contract_anomaly_topic_id="",
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(gateway, "_create_forum_topic") as create_mock,
                patch.object(gateway, "_ensure_topic_intro") as intro_mock,
                patch.object(gateway, "_send_real_message_ids") as send_mock,
            ):
                setup = gateway.setup_topic(
                    "TG_ALTCOIN_CONTRACT_ANOMALY",
                    send=True,
                    confirm_real_send=True,
                )
                send = gateway.send(
                    "test",
                    "TG_ALTCOIN_CONTRACT_ANOMALY",
                    "altcoin:missing-topic",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )
                blocked_create = gateway._create_and_save_topic(
                    "TG_ALTCOIN_CONTRACT_ANOMALY"
                )

            self.assertEqual(
                TOPIC_TEMPLATE_NAMES["TG_ALTCOIN_CONTRACT_ANOMALY"],
                "山寨合约异动",
            )
            self.assertIn(
                "TG_ALTCOIN_CONTRACT_ANOMALY",
                PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS,
            )
            self.assertNotIn(
                "TG_ALTCOIN_CONTRACT_ANOMALY",
                PRODUCTION_TOPIC_TEMPLATE_IDS,
            )
            self.assertEqual(setup, {
                "status": "blocked",
                "reason": "telegram_topic_not_preconfigured",
            })
            self.assertEqual(send.status, "blocked")
            self.assertEqual(send.reason, "telegram_topic_not_configured")
            self.assertEqual(gateway._topic_id_for_template("TG_ALTCOIN_CONTRACT_ANOMALY"), "")
            self.assertEqual(gateway._topic_id_for_template("TG_RADAR_SUMMARY"), "42")
            self.assertEqual(blocked_create, "")
            create_mock.assert_not_called()
            intro_mock.assert_not_called()
            send_mock.assert_not_called()

    def test_altcoin_anomaly_explicit_topic_can_publish_and_pin_intro(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_altcoin_contract_anomaly_topic_id="77",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with (
                patch.object(gateway, "_create_forum_topic") as create_mock,
                patch.object(
                    gateway,
                    "_ensure_topic_intro",
                    return_value=True,
                ) as intro_mock,
            ):
                result = gateway.setup_topic(
                    "TG_ALTCOIN_CONTRACT_ANOMALY",
                    send=True,
                    confirm_real_send=True,
                )

            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["topic"], "reused")
            self.assertTrue(result["pinned"])
            create_mock.assert_not_called()
            intro_mock.assert_called_once_with(
                "TG_ALTCOIN_CONTRACT_ANOMALY",
                "77",
                require_pin=True,
            )

    def test_altcoin_anomaly_real_send_always_includes_preconfigured_thread(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-12345",
                tg_use_topic=False,
                tg_altcoin_contract_anomaly_topic_id="77",
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))
            session = FakeTelegramSession()

            with patch(
                "shared.telegram.requests.post",
                side_effect=session.post,
            ):
                result = gateway.send(
                    "test",
                    "TG_ALTCOIN_CONTRACT_ANOMALY",
                    "altcoin:forced-thread",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertTrue(result.sent)
            self.assertEqual(session.calls[0]["json"]["message_thread_id"], 77)

    def test_altcoin_anomaly_invalid_preconfigured_topic_blocks_before_network(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_altcoin_contract_anomaly_topic_id="not-a-topic",
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with patch("shared.telegram.requests.post") as post_mock:
                result = gateway.send(
                    "test",
                    "TG_ALTCOIN_CONTRACT_ANOMALY",
                    "altcoin:bad-thread",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.reason, "telegram_topic_not_preconfigured")
            post_mock.assert_not_called()

    def test_altcoin_anomaly_sent_outbox_is_permanent_dedup_within_retention(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = JsonStore(root)
            settings = Settings(
                data_dir=root,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_altcoin_contract_anomaly_topic_id="77",
                tg_default_cooldown_sec=0,
                tg_outbox_quarantine_sec=60,
            )
            old_ts = utc_ts() - 3_600
            store.save(settings.tg_outbox_path, [{
                "delivery_id": "already-sent",
                "ts": old_ts,
                "updated_at": old_ts,
                "template_id": "TG_ALTCOIN_CONTRACT_ANOMALY",
                "dedup_key": "altcoin:permanent-sent",
                "topic_id": "77",
                "status": "sent",
                "message_ids": [123],
            }])
            gateway = TelegramGateway(settings, store)

            with patch.object(gateway, "_send_real_message_ids") as post_mock:
                result = gateway.send(
                    "same signal",
                    "TG_ALTCOIN_CONTRACT_ANOMALY",
                    "altcoin:permanent-sent",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason, "dedup_cooldown")
            post_mock.assert_not_called()

    def test_altcoin_anomaly_unknown_provider_effect_never_exits_quarantine(self) -> None:
        for status in ("pending", "uncertain"):
            with self.subTest(status=status), TemporaryDirectory() as tmp:
                root = Path(tmp)
                store = JsonStore(root)
                settings = Settings(
                    data_dir=root,
                    tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    tg_chat_id="-1001234567890",
                    tg_altcoin_contract_anomaly_topic_id="77",
                    tg_default_cooldown_sec=0,
                    tg_outbox_quarantine_sec=60,
                )
                old_ts = utc_ts() - 3_600
                store.save(settings.tg_outbox_path, [{
                    "delivery_id": f"unknown-{status}",
                    "ts": old_ts,
                    "updated_at": old_ts,
                    "template_id": "TG_ALTCOIN_CONTRACT_ANOMALY",
                    "dedup_key": f"altcoin:unknown-{status}",
                    "topic_id": "77",
                    "status": status,
                }])
                gateway = TelegramGateway(settings, store)

                with patch.object(gateway, "_send_real_message_ids") as post_mock:
                    result = gateway.send(
                        "same signal",
                        "TG_ALTCOIN_CONTRACT_ANOMALY",
                        f"altcoin:unknown-{status}",
                        send=True,
                        confirm_real_send=True,
                        cooldown_sec=0,
                        parse_mode="HTML",
                    )

                self.assertEqual(result.status, "skipped")
                self.assertEqual(result.reason, "delivery_quarantine")
                self.assertEqual(
                    store.load(settings.tg_outbox_path, [])[0]["status"],
                    status,
                )
                post_mock.assert_not_called()

    def test_altcoin_anomaly_topic_intro_and_pin_failure_are_explicit(self) -> None:
        settings = Settings()
        intro = topic_intro_message("TG_ALTCOIN_CONTRACT_ANOMALY", settings)

        self.assertIn("【山寨合约异动｜说明】", intro)
        self.assertIn("实时确认因子共6类", intro)
        for label in (
            "价格动量",
            "成交量放大",
            "主动买卖与CVD",
            "OI变化",
            "资金费率变化",
            "多空爆仓",
        ):
            self.assertIn(label, intro)
        self.assertIn("不代表综合分数、成功率或涨跌概率", intro)
        self.assertIn("候选依据与实时确认分开展示", intro)

        with TemporaryDirectory() as tmp:
            configured = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_altcoin_contract_anomaly_topic_id="77",
            )
            gateway = TelegramGateway(configured, JsonStore(Path(tmp)))
            with (
                patch.object(gateway, "_send_real_message_ids") as send_mock,
                patch.object(gateway, "_pin_message") as pin_mock,
            ):
                self.assertFalse(gateway._ensure_topic_intro(
                    "TG_ALTCOIN_CONTRACT_ANOMALY",
                    "88",
                    require_pin=True,
                ))
            send_mock.assert_not_called()
            pin_mock.assert_not_called()
            with patch.object(
                gateway,
                "_ensure_topic_intro",
                return_value=False,
            ):
                result = gateway.setup_topic(
                    "TG_ALTCOIN_CONTRACT_ANOMALY",
                    send=True,
                    confirm_real_send=True,
                )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["reason"], "telegram_topic_intro_failed")
        self.assertNotIn("pinned", result)

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
                tg_default_cooldown_sec=0,
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
            outbox = JsonStore(Path(tmp)).load(settings.tg_outbox_path, [])
            self.assertEqual(outbox[-1]["status"], "sent")
            self.assertEqual(outbox[-1]["message_ids"], [222])
            self.assertEqual(outbox[-1]["delivery_id"], result.delivery_id)

    def test_pending_outbox_delivery_blocks_duplicate_real_send(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_outbox_path=Path(tmp) / "tg_outbox.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_test_topic_id="14",
                tg_default_cooldown_sec=0,
            )
            store.save(settings.tg_outbox_path, [{
                "delivery_id": "pending-one",
                "ts": utc_ts(),
                "updated_at": utc_ts(),
                "template_id": "TG_TEST_MESSAGE",
                "dedup_key": "outbox:duplicate",
                "status": "pending",
            }])
            gateway = TelegramGateway(settings, store)

            with patch.object(gateway, "_send_real_message_ids") as send_mock:
                result = gateway.send(
                    "test",
                    "TG_TEST_MESSAGE",
                    "outbox:duplicate",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )

            self.assertEqual(result.status, "skipped")
            self.assertEqual(result.reason, "delivery_quarantine")
            send_mock.assert_not_called()

    def test_partial_real_send_is_persisted_for_quarantine(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_outbox_path=Path(tmp) / "tg_outbox.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_test_topic_id="14",
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with patch.object(gateway, "_send_real_message_ids", return_value=(False, [301])):
                result = gateway.send(
                    "partial",
                    "TG_TEST_MESSAGE",
                    "outbox:partial",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )

            outbox = store.load(settings.tg_outbox_path, [])
            self.assertEqual(result.status, "partial")
            self.assertEqual(outbox[-1]["status"], "partial")
            self.assertEqual(outbox[-1]["completed_chunks"], 1)
            self.assertEqual(outbox[-1]["message_ids"], [301])

    def test_funding_send_binds_replacement_callback_without_duplicate_enrichment(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_outbox_path=Path(tmp) / "tg_outbox.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_funding_alert_topic_id="16",
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)
            alert = {
                "symbol": "TESTUSDT",
                "event_snapshot": {"event_no": 1},
            }

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    return_value=(True, [222]),
                ),
                patch(
                    "shared.telegram.enrich_telegram_with_market_context"
                ) as enrich_mock,
            ):
                result = gateway.send(
                    "funding",
                    "TG_FUNDING_ALERT",
                    "funding:test",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                    signal_records=[alert],
                )

            self.assertTrue(result.sent)
            self.assertIs(
                alert["_funding_delete_callback"].__self__,
                gateway,
            )
            enrich_mock.assert_not_called()

    def test_partial_funding_send_rolls_back_partial_messages(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_outbox_path=Path(tmp) / "tg_outbox.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_funding_alert_topic_id="16",
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    return_value=(False, [301]),
                ),
                patch.object(
                    gateway,
                    "delete_messages_detailed",
                    return_value={"deleted_ids": [301], "failed_ids": []},
                ) as delete_mock,
            ):
                result = gateway.send(
                    "funding",
                    "TG_FUNDING_ALERT",
                    "funding:partial",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                    signal_records=[{
                        "symbol": "TESTUSDT",
                        "event_snapshot": {"event_no": 1},
                    }],
                )

            self.assertEqual(result.status, "partial")
            delete_mock.assert_called_once_with(
                [301],
                reason="funding_partial_send_rollback",
            )

    def test_real_sender_adds_reply_payload_on_first_chunk(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_launch_alert_topic_id="12",
                tg_use_topic=True,
                tg_push_split_limit=10,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            class Response:
                status_code = 200

                @staticmethod
                def json() -> dict[str, object]:
                    return {"result": {"message_id": 222}}

            with patch("shared.telegram.requests.post", return_value=Response()) as post_mock:
                ok, message_ids = gateway._send_real_message_ids(
                    "first line\nsecond line",
                    parse_mode="HTML",
                    topic_id="12",
                    reply_to_message_id=111,
                )

            self.assertTrue(ok)
            self.assertEqual(message_ids, [222, 222, 222])
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

                @staticmethod
                def json() -> dict[str, object]:
                    return {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: reply message not found",
                    }

            class Response200:
                status_code = 200
                text = "ok"

                @staticmethod
                def json() -> dict[str, object]:
                    return {"result": {"message_id": 333}}

            with patch("shared.telegram.requests.post", side_effect=[Response400(), Response200()]) as post_mock:
                ok, message_ids = gateway._send_real_message_ids(
                    "launch",
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

    def test_send_tracks_photo_and_caption_as_one_message(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_topic_id="10",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_launch_alert_topic_id="12",
                tg_use_topic=True,
                tg_push_retry=1,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))
            png = b"\x89PNG\r\n\x1a\nmemory-only"
            caption = (
                '<a href="https://example.com"><b>TEST</b></a> · '
                "📋 <code>TESTUSDT</code>"
            )

            class Response:
                status_code = 200

                @staticmethod
                def json() -> dict[str, object]:
                    return {"result": {"message_id": 444}}

            with patch(
                "shared.telegram.requests.post",
                return_value=Response(),
            ) as post_mock:
                result = gateway.send(
                    caption,
                    "TG_LAUNCH_ALERT",
                    "pulse-photo:1:2",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                    photo=png,
                    reply_to_message_id=111,
                    enrich_market_context=False,
                )

            self.assertEqual(result.status, "sent")
            self.assertEqual(result.reason, "telegram_photo_api")
            self.assertEqual(result.message_ids, [444])
            self.assertEqual(post_mock.call_count, 1)
            request = post_mock.call_args.kwargs
            self.assertEqual(request["data"]["caption"], caption)
            self.assertEqual(request["data"]["message_thread_id"], 12)
            self.assertEqual(request["data"]["reply_to_message_id"], 111)
            self.assertTrue(request["data"]["allow_sending_without_reply"])
            self.assertNotIn("show_caption_above_media", request["data"])
            self.assertNotIn("reply_markup", request["data"])
            self.assertEqual(request["files"]["photo"][1], png)
            self.assertEqual(request["files"]["photo"][2], "image/png")
            self.assertEqual(list(Path(tmp).glob("*.png")), [])
            history = JsonStore(Path(tmp)).load(settings.tg_push_history_path, [])
            self.assertEqual(history[-1]["message_ids"], [444])

    def test_photo_sender_falls_back_once_when_reply_target_is_missing(self) -> None:
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

                @staticmethod
                def json() -> dict[str, object]:
                    return {
                        "ok": False,
                        "error_code": 400,
                        "description": "Bad Request: reply message not found",
                    }

            class Response200:
                status_code = 200

                @staticmethod
                def json() -> dict[str, object]:
                    return {"result": {"message_id": 445}}

            with patch(
                "shared.telegram.requests.post",
                side_effect=[Response400(), Response200()],
            ) as post_mock:
                ok, message_ids = gateway._send_real_photo_bytes(
                    b"\x89PNG\r\n\x1a\nphoto",
                    caption="pulse",
                    parse_mode="HTML",
                    topic_id="12",
                    reply_to_message_id=111,
                )

            self.assertTrue(ok)
            self.assertEqual(message_ids, [445])
            self.assertEqual(post_mock.call_count, 2)
            first_payload = post_mock.call_args_list[0].kwargs["data"]
            second_payload = post_mock.call_args_list[1].kwargs["data"]
            self.assertEqual(first_payload["reply_to_message_id"], 111)
            self.assertNotIn("reply_to_message_id", second_payload)
            self.assertTrue(gateway._last_delivery_diagnostics.reply_fallback_used)

    def test_send_rejects_non_png_before_network(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))
            with patch("shared.telegram.requests.post") as post_mock:
                result = gateway.send(
                    "TEST",
                    "TG_LAUNCH_ALERT",
                    "pulse-photo:1:2",
                    send=True,
                    confirm_real_send=True,
                    photo=b"not-an-image",
                )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "invalid_png")
            post_mock.assert_not_called()

    def test_send_rejects_caption_over_telegram_limit(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))
            with patch("shared.telegram.requests.post") as post_mock:
                result = gateway.send(
                    "A" * 1025,
                    "TG_LAUNCH_ALERT",
                    "pulse-photo:1:2",
                    send=True,
                    confirm_real_send=True,
                    photo=b"\x89PNG\r\n\x1a\nmemory-only",
                )

            self.assertEqual(result.status, "failed")
            self.assertEqual(result.reason, "caption_too_long")
            post_mock.assert_not_called()

    def test_pulse_topic_intro_explains_direct_replacement_and_safety(self) -> None:
        with TemporaryDirectory() as tmp:
            intro = topic_intro_message(
                "TG_LAUNCH_ALERT",
                Settings(data_dir=Path(tmp)),
            )

            for phrase in (
                "脉冲雷达使用说明",
                "直接接管原启动预警话题",
                "不再运行旧启动评分模型",
                "15分钟异动提醒",
                "完整闭合的5分钟数据",
                "最近120根1小时已收线K线和成交量图",
                "图表不可用时仍发送完整文字",
                "2小时持仓价格背离",
                "只有真实发送成功才写入跟随状态和复盘记录",
                "dry-run不会消耗信号",
                "不使用未来数据",
                "--send 与 --confirm-real-send",
                "部分发送失败会清理残缺消息",
            ):
                self.assertIn(phrase, intro)
            self.assertNotIn("最高130分", intro)
            self.assertLessEqual(len(plain_fallback(intro)), 4096)

    def test_pulse_scan_limit_does_not_change_topic_intro(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                pulse_simple_scan_limit=42,
                pulse_divergence_scan_limit=42,
            )
            intro = topic_intro_message(
                "TG_LAUNCH_ALERT",
                settings,
            )

            self.assertEqual(
                intro,
                topic_intro_message(
                    "TG_LAUNCH_ALERT",
                    Settings(data_dir=Path(tmp)),
                ),
            )
            self.assertIn("不再运行旧启动评分模型", intro)
            self.assertNotIn("最高130分", intro)
            self.assertLessEqual(len(plain_fallback(intro)), 4096)
            self.assertEqual(
                topic_intro_version("TG_LAUNCH_ALERT", settings),
                TOPIC_INTRO_VERSIONS["TG_LAUNCH_ALERT"],
            )

    def test_remaining_alert_topic_intros_hold_static_guidance(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            expected = {
                "TG_FLOW_RADAR": [
                    "分类图例",
                    "真启动 = 价格、OI、现货主动成交净额、合约主动成交净额共振",
                    "恐慌下跌 = 下跌增仓且主动卖出增强",
                    "数据来源与计算口径",
                    "价格变化 =（窗口收盘价 - 窗口开盘价）/ 窗口开盘价",
                    "主动成交净额 = taker主动买入报价额 - taker主动卖出报价额",
                    "五项全部就绪才允许进入信号推送",
                ],
                "TG_FUNDING_ALERT": [
                    "数据来源与计算口径",
                    "资金费率、结算时间和OI来自 Binance USDⓈ-M Futures",
                    "价格、OI及主动成交变化使用 Binance 已闭合15分钟窗口",
                    "周期补查仍失败时明确显示“本次未确认”",
                    "每个仍在跟踪的币种只保留一条最新消息",
                    "其他币种不受影响",
                    "扫描不等于推送",
                    "本轮首次出现时间、初始费率、价格、OI和主动成交",
                    "普通扫描只记录，不计入事件轴",
                    "删除失败会保存待清理消息编号",
                    "统一风险说明",
                    "只代表 Binance 合约市场的拥挤程度",
                ],
            }
            for template_id, phrases in expected.items():
                intro = topic_intro_message(template_id, settings)
                for phrase in phrases:
                    self.assertIn(phrase, intro)
                if template_id == "TG_FUNDING_ALERT":
                    self.assertNotIn("多交易所共振", intro)
                    self.assertNotIn("交易所偏离", intro)
                    self.assertNotIn("回复上一条", intro)
                self.assertLessEqual(len(plain_fallback(intro)), 4096)

    def test_summary_topic_intro_holds_static_legend(self) -> None:
        with TemporaryDirectory() as tmp:
            intro = topic_intro_message(
                "TG_RADAR_SUMMARY",
                Settings(data_dir=Path(tmp)),
            )

            expected = [
                "📖 图例",
                "负费率 = 空头拥挤，可能形成反向燃料",
                "🔥加速 = 费率继续变负",
                "⬇️变负 = 刚从正费率转为负费率",
                "⬆️回升 = 负费率缓和",
                "暗流 = OI增加但价格没动",
                "窗口 = 本次统计窗口内的完整收线数据",
                "背离 = OI窗口变化% - 价格窗口变化%",
                "OI·币安 = OI来自 Binance USDⓈ-M 已闭合窗口，不再使用外部聚合源改写",
                "市值 = Binance市场资料；缺失时为0分，不再使用成交额/OI倍数猜测市值",
                "链接 = 点击币种打开 CoinGlass，点击代码复制交易对，点击 TV 打开 TradingView",
                "来源：Binance Spot + Binance USDⓈ-M Futures；仅代表 Binance 市场。",
                "主动成交净额 = taker主动买入报价额 - taker主动卖出报价额",
                "主动净占比 = 主动成交净额 / 总成交额",
                "OI变化 =（窗口末OI - 窗口初OI）/ 窗口初OI",
                "只采用实时或已闭合窗口行情；不改变本模块原触发阈值；不构成投资建议。",
            ]
            for line in expected:
                self.assertIn(line, intro)
            self.assertNotIn("新闻、社交情报或 CoinGlass/Coinalyze", intro)
            self.assertLessEqual(len(plain_fallback(intro)), 4096)

    def test_normal_send_never_auto_creates_or_publishes_intro(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_topic_id="10",
                tg_test_topic_id="10",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_use_topic=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with (
                patch.object(gateway, "_create_forum_topic") as create_mock,
                patch.object(gateway, "_ensure_topic_intro") as intro_mock,
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    return_value=(True, [42]),
                ),
            ):
                result = gateway.send(
                    "test",
                    "TG_TEST_MESSAGE",
                    "manual-only-route",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )

            self.assertTrue(result.sent)
            create_mock.assert_not_called()
            intro_mock.assert_not_called()

    def test_missing_topic_fails_closed_without_network_or_main_group_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_topic_id="10",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_use_topic=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))

            with (
                patch.object(gateway, "_create_forum_topic") as create_mock,
                patch.object(gateway, "_send_real_message_ids") as send_mock,
            ):
                result = gateway.send(
                    "test",
                    "TG_TEST_MESSAGE",
                    "missing-topic",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )

            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.reason, "telegram_topic_not_configured")
            create_mock.assert_not_called()
            send_mock.assert_not_called()

    def test_manual_setup_posts_and_pins_topic_intro_once(self) -> None:
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
                tg_topic_intro_pin=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(gateway, "_send_real_message_ids", side_effect=[(True, [100]), (True, [101]), (True, [102])]) as send_mock,
                patch.object(gateway, "_pin_message", return_value=True) as pin_mock,
                patch.object(gateway, "_delete_message", return_value=True) as delete_mock,
            ):
                setup = gateway.setup_topic(
                    "TG_RADAR_SUMMARY",
                    send=True,
                    confirm_real_send=True,
                )
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

            self.assertEqual(setup["status"], "ok")
            self.assertTrue(first.sent)
            self.assertTrue(second.sent)
            self.assertEqual(send_mock.call_count, 3)
            self.assertIn("资金摘要话题说明", send_mock.call_args_list[0].args[0])
            self.assertEqual(send_mock.call_args_list[1].args[0], "summary one")
            self.assertEqual(send_mock.call_args_list[2].args[0], "summary two")
            pin_mock.assert_called_once_with(100)
            delete_mock.assert_called_once_with(101)
            data = store.load(route_path, {})
            self.assertEqual(data["intros"]["TG_RADAR_SUMMARY:11"]["message_id"], 100)
            self.assertTrue(data["intros"]["TG_RADAR_SUMMARY:11"]["pinned"])
            self.assertIn("content_hash", data["intros"]["TG_RADAR_SUMMARY:11"])
            self.assertIn("intro_version", data["intros"]["TG_RADAR_SUMMARY:11"])
            history = store.load(settings.tg_push_history_path, [])
            self.assertEqual(history[0]["deleted_message_ids"], [101])
            self.assertTrue(history[0]["lifecycle_deleted"])
            self.assertNotIn("deleted_message_ids", history[1])

    def test_summary_replacement_keeps_previous_when_new_delivery_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_radar_summary_topic_id="11",
                tg_use_topic=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    side_effect=[(True, [101]), (False, [])],
                ),
                patch.object(gateway, "_delete_message", return_value=True) as delete_mock,
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
            self.assertFalse(second.sent)
            delete_mock.assert_not_called()
            history = store.load(settings.tg_push_history_path, [])
            self.assertNotIn("deleted_message_ids", history[0])

    def test_summary_replacement_retries_failed_cleanup_on_next_success(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_radar_summary_topic_id="11",
                tg_use_topic=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    side_effect=[
                        (True, [101]),
                        (True, [102, 103]),
                        (True, [104]),
                    ],
                ),
                patch.object(
                    gateway,
                    "_delete_message",
                    side_effect=[False, True, True, True],
                ) as delete_mock,
            ):
                gateway.send(
                    "summary one",
                    "TG_RADAR_SUMMARY",
                    "summary:one",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )
                gateway.send(
                    "summary two",
                    "TG_RADAR_SUMMARY",
                    "summary:two",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )
                gateway.send(
                    "summary three",
                    "TG_RADAR_SUMMARY",
                    "summary:three",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertEqual(
                [call.args[0] for call in delete_mock.call_args_list],
                [101, 101, 102, 103],
            )
            history = store.load(settings.tg_push_history_path, [])
            self.assertTrue(history[0]["lifecycle_deleted"])
            self.assertTrue(history[1]["lifecycle_deleted"])
            self.assertNotIn("deleted_message_ids", history[2])

    def test_flow_replacement_deletes_only_previous_flow_summary(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_flow_radar_topic_id="15",
                tg_radar_summary_topic_id="11",
                tg_use_topic=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    side_effect=[(True, [101]), (True, [201]), (True, [102, 103])],
                ),
                patch.object(gateway, "_delete_message", return_value=True) as delete_mock,
            ):
                gateway.send(
                    "flow one",
                    "TG_FLOW_RADAR",
                    "flow:one",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )
                gateway.send(
                    "summary",
                    "TG_RADAR_SUMMARY",
                    "summary:one",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )
                result = gateway.send(
                    "flow two",
                    "TG_FLOW_RADAR",
                    "flow:two",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertTrue(result.sent)
            self.assertEqual(
                [call.args[0] for call in delete_mock.call_args_list],
                [101],
            )
            history = store.load(settings.tg_push_history_path, [])
            flow_one = next(record for record in history if record["dedup_key"] == "flow:one")
            summary = next(record for record in history if record["dedup_key"] == "summary:one")
            self.assertEqual(flow_one["deleted_message_ids"], [101])
            self.assertNotIn("deleted_message_ids", summary)

    def test_flow_replacement_keeps_previous_when_new_delivery_fails(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_flow_radar_topic_id="15",
                tg_use_topic=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    side_effect=[(True, [101]), (False, [])],
                ),
                patch.object(gateway, "_delete_message", return_value=True) as delete_mock,
            ):
                gateway.send(
                    "flow one",
                    "TG_FLOW_RADAR",
                    "flow:one",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )
                result = gateway.send(
                    "flow two",
                    "TG_FLOW_RADAR",
                    "flow:two",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )

            self.assertFalse(result.sent)
            delete_mock.assert_not_called()

    def test_flow_partial_delivery_rolls_back_partial_ids_and_keeps_previous(self) -> None:
        with TemporaryDirectory() as tmp:
            store = JsonStore(Path(tmp))
            settings = Settings(
                data_dir=Path(tmp),
                tg_push_history_path=Path(tmp) / "push_history.json",
                tg_topic_routes_path=Path(tmp) / "topic_routes.json",
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_flow_radar_topic_id="15",
                tg_use_topic=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    side_effect=[(True, [101]), (False, [102])],
                ),
                patch.object(gateway, "_delete_message", return_value=True) as delete_mock,
            ):
                gateway.send(
                    "flow one",
                    "TG_FLOW_RADAR",
                    "flow:one",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )
                result = gateway.send(
                    "flow two",
                    "TG_FLOW_RADAR",
                    "flow:two",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )

            self.assertFalse(result.sent)
            delete_mock.assert_called_once_with(102)
            history = store.load(settings.tg_push_history_path, [])
            flow_one = next(record for record in history if record["dedup_key"] == "flow:one")
            self.assertNotIn("deleted_message_ids", flow_one)

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
                tg_topic_intro_pin=True,
                tg_default_cooldown_sec=0,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(gateway, "_delete_message", return_value=True) as delete_mock,
                patch.object(gateway, "_send_real_message_ids", side_effect=[(True, [100]), (True, [101])]) as send_mock,
                patch.object(gateway, "_pin_message", return_value=True) as pin_mock,
            ):
                setup = gateway.setup_topic(
                    "TG_RADAR_SUMMARY",
                    send=True,
                    confirm_real_send=True,
                )
                result = gateway.send(
                    "summary",
                    "TG_RADAR_SUMMARY",
                    "summary:key",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertEqual(setup["status"], "ok")
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

    def test_topic_intro_refresh_keeps_old_intro_when_new_pin_fails(self) -> None:
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
                tg_topic_routes_path=route_path,
                tg_radar_summary_topic_id="11",
                tg_topic_intro_pin=True,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(gateway, "_send_real_message_ids", return_value=(True, [100])),
                patch.object(gateway, "_pin_message", return_value=False),
                patch.object(gateway, "_delete_message", return_value=True) as delete_mock,
            ):
                gateway._ensure_topic_intro("TG_RADAR_SUMMARY", "11")

            delete_mock.assert_called_once_with(100)
            record = store.load(route_path, {})["intros"]["TG_RADAR_SUMMARY:11"]
            self.assertEqual(record["message_id"], 99)
            self.assertEqual(record["intro_version"], "old")

    def test_topic_intro_unpins_previous_message_when_it_is_too_old_to_delete(self) -> None:
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
                tg_topic_routes_path=route_path,
                tg_radar_summary_topic_id="11",
                tg_topic_intro_pin=True,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    return_value=(True, [100]),
                ),
                patch.object(gateway, "_pin_message", return_value=True),
                patch.object(gateway, "_delete_message", return_value=False),
                patch.object(
                    gateway,
                    "_unpin_message",
                    return_value=True,
                ) as unpin_mock,
            ):
                refreshed = gateway._ensure_topic_intro(
                    "TG_RADAR_SUMMARY",
                    "11",
                    require_pin=True,
                )

            self.assertTrue(refreshed)
            unpin_mock.assert_called_once_with(99)
            record = store.load(route_path, {})["intros"]["TG_RADAR_SUMMARY:11"]
            self.assertEqual(record["message_id"], 100)
            self.assertTrue(record["pinned"])

    def test_topic_setup_republishes_corrupt_current_intro_record(self) -> None:
        with TemporaryDirectory() as tmp:
            route_path = Path(tmp) / "topic_routes.json"
            settings = Settings(
                data_dir=Path(tmp),
                tg_topic_routes_path=route_path,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_radar_summary_topic_id="11",
                tg_topic_intro_pin=False,
            )
            intro = topic_intro_message("TG_RADAR_SUMMARY", settings)
            store = JsonStore(Path(tmp))
            store.save(route_path, {
                "intros": {
                    "TG_RADAR_SUMMARY:11": {
                        "template_id": "TG_RADAR_SUMMARY",
                        "topic_id": "11",
                        "message_id": 0,
                        "pinned": True,
                        "intro_version": topic_intro_version("TG_RADAR_SUMMARY"),
                        "content_hash": intro_hash(intro),
                    }
                }
            })
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    return_value=(True, [100]),
                ) as send_mock,
                patch.object(gateway, "_pin_message", return_value=True) as pin_mock,
            ):
                result = gateway.setup_topic(
                    "TG_RADAR_SUMMARY",
                    send=True,
                    confirm_real_send=True,
                )

            self.assertEqual(result["status"], "ok")
            self.assertTrue(result["pinned"])
            send_mock.assert_called_once()
            pin_mock.assert_called_once_with(100)
            record = store.load(route_path, {})["intros"]["TG_RADAR_SUMMARY:11"]
            self.assertEqual(record["message_id"], 100)
            self.assertTrue(record["pinned"])

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
                tg_topic_intro_pin=False,
                tg_default_cooldown_sec=0,
                flow_interval_sec=3600,
            )
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    gateway,
                    "_send_real_message_ids",
                    side_effect=[(True, [100]), (True, [101])],
                ) as send_mock,
                patch.object(gateway, "_pin_message", return_value=True) as pin_mock,
            ):
                setup = gateway.setup_topic(
                    "TG_FLOW_RADAR",
                    send=True,
                    confirm_real_send=True,
                )
                result = gateway.send(
                    "flow",
                    "TG_FLOW_RADAR",
                    "flow:key",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )

            self.assertEqual(setup["status"], "ok")
            self.assertTrue(setup["pinned"])
            self.assertTrue(result.sent)
            pin_mock.assert_called_once_with(100)
            intro = send_mock.call_args_list[0].args[0]
            self.assertIn("默认每1小时扫描一次，并在整点收线后延迟5分钟发送", intro)
            self.assertIn("统计上一完整闭合窗口", intro)
            self.assertIn("使用 Binance 免费公开数据", intro)
            self.assertIn("真启动候选、吸筹观察、空头燃料、合约拉盘、挤空/止损、诱多/派发、恐慌下跌", intro)


if __name__ == "__main__":
    unittest.main()
