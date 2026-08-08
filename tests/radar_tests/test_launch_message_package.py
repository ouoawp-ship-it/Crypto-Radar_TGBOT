from __future__ import annotations

import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from runtime.cli import push_launch_messages
from config import Settings
from shared.storage import JsonStore
from shared.telegram import PushResult, TelegramGateway, plain_fallback
from shared.signal_store import signal_public_ref


class FakeEngine:
    def __init__(
        self,
        events: list[str],
        *,
        commit_status: str = "committed",
        commit_exception: Exception | None = None,
    ) -> None:
        self.events = events
        self.commit_status = commit_status
        self.commit_exception = commit_exception
        self.pending_cleanups: list[dict[str, object]] = []

    def pending_launch_package_cleanups(self, *, limit: int) -> list[dict[str, object]]:
        self.events.append(f"pending:{limit}")
        return self.pending_cleanups[:limit]

    def commit_launch_package(
        self,
        _alert: dict[str, object],
        message_ids: list[int],
    ) -> dict[str, object]:
        self.events.append("commit")
        self.events.append(f"commit_ids:{message_ids}")
        if self.commit_exception is not None:
            raise self.commit_exception
        return {
            "status": self.commit_status,
            "cycle_id": 7,
            "delete_message_ids": [101],
        }

    def complete_launch_package_cleanup(
        self,
        *,
        cycle_id: int,
        deleted_ids: list[int],
        failed_ids: list[int],
        expire_latest: bool = False,
    ) -> dict[str, object]:
        self.events.append(
            f"complete:{cycle_id}:{deleted_ids}:{failed_ids}:{expire_latest}"
        )
        return {"status": "complete"}

    def mark_launch_pushed(self, _alerts: list[dict[str, object]]) -> None:
        self.events.append("mark")

    def reconcile_launch_topic_messages(
        self,
        *,
        deleted_ids: list[int],
    ) -> dict[str, int]:
        self.events.append(f"reconcile:{deleted_ids}")
        return {
            "cycles_updated": len(deleted_ids),
            "message_ids_removed": len(deleted_ids),
            "state_records_updated": len(deleted_ids),
        }


class FakeGateway:
    def __init__(
        self,
        events: list[str],
        result: PushResult,
        *,
        photo_result: PushResult | None = None,
    ) -> None:
        self.events = events
        self.result = result
        self.photo_result = photo_result or PushResult(
            "sent",
            "telegram_photo_api",
            True,
            [202],
        )
        self.topic_cleanup_candidates: list[int] = []
        self.topic_undeletable_candidates: list[int] = []
        self.latest_topic_message_ids: list[int] = []
        self.send_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def send(self, *args: object, **kwargs: object) -> PushResult:
        self.send_calls.append((args, kwargs))
        if kwargs.get("photo") is not None:
            self.events.append("photo")
            return self.photo_result
        self.events.append("send")
        return self.result

    def delete_messages_detailed(
        self,
        message_ids: list[int],
        *,
        reason: str = "",
    ) -> dict[str, list[int]]:
        self.events.append(f"reason:{reason}")
        self.events.append(f"delete:{message_ids}")
        return {"deleted_ids": message_ids, "failed_ids": []}

    def launch_topic_cleanup_candidates(
        self,
        *,
        keep_message_ids: list[int] | None = None,
    ) -> list[int]:
        self.events.append(f"topic_candidates:{keep_message_ids}")
        return list(self.topic_cleanup_candidates)

    def latest_launch_topic_message_ids(self) -> list[int]:
        self.events.append("latest_topic_messages")
        return list(self.latest_topic_message_ids)

    def launch_topic_cleanup_plan(
        self,
        *,
        keep_message_ids: list[int] | None = None,
    ) -> dict[str, list[int]]:
        self.events.append(f"topic_candidates:{keep_message_ids}")
        return {
            "deletable_ids": list(self.topic_cleanup_candidates),
            "undeletable_ids": list(self.topic_undeletable_candidates),
        }

    def mark_history_messages_undeletable(
        self,
        message_ids: list[int],
        *,
        reason: str = "",
    ) -> None:
        self.events.append(f"undeletable:{reason}:{message_ids}")


def launch_payload() -> dict[str, object]:
    alert = {
        "symbol": "TESTUSDT",
        "stage": "breakout",
        "launch_message_package_v2": True,
        "reply_to_message_id": 101,
        "launch_lifecycle": {
            "cycle_id": 7,
            "observation_id": 12,
        },
        "launch_package": {
            "checkpoint_reasons": ["stage_changed"],
        },
    }
    return {"messages": ["message"], "alerts": [alert]}


def launch_ai_payload() -> dict[str, object]:
    payload = launch_payload()
    alert = payload["alerts"][0]
    alert.update({
        "discovery_score": 72,
        "directional_readiness": {
            "status": "多头候选",
            "direction": "bullish",
            "stage": "forming",
            "data_complete": True,
            "bullish_evidence_score": 81,
            "bearish_evidence_score": 10,
        },
        "launch_phase": {
            "timing_stage": "forming",
            "execution_status": "wait_confirmation",
        },
    })
    return payload


class LaunchMessagePackageTests(unittest.TestCase):
    def test_ai_on_demand_button_uses_opaque_ref_and_stores_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            gateway = FakeGateway(
                events,
                PushResult("sent", "telegram_api", True, [201]),
            )
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_ai_interpreter_enable=True,
                launch_ai_auto_enable=False,
                ai_api_key="fake-private-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
                tg_bot_username="VIPpao_bot",
                tg_private_control_enable=True,
                tg_private_control_admin_user_id="123",
            )

            push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                gateway,  # type: ignore[arg-type]
                launch_ai_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            kwargs = gateway.send_calls[0][1]
            button = kwargs["url_button"]
            expected_ref = signal_public_ref(
                "launch-package:7:12",
                "TESTUSDT",
            )
            self.assertEqual(
                button.url,
                f"https://t.me/VIPpao_bot?start=ai_{expected_ref}",
            )
            record = kwargs["signal_records"][0]
            self.assertEqual(
                record["ai_context_snapshot"]["rule_result"]["direction"],
                "bullish",
            )
            self.assertNotIn("fake-private-key", str(record))
            self.assertNotIn("provider.invalid", str(record))

    def test_invalid_private_admin_id_omits_button_without_blocking_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            gateway = FakeGateway(
                events,
                PushResult("sent", "telegram_api", True, [201]),
            )
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-private-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
                tg_bot_username="VIPpao_bot",
                tg_private_control_enable=True,
                tg_private_control_admin_user_id="not-a-user-id",
            )

            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                gateway,  # type: ignore[arg-type]
                launch_ai_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertEqual(pushes[0]["status"], "sent")
            self.assertIsNone(gateway.send_calls[0][1]["url_button"])

    def test_button_message_is_rolled_back_when_ai_snapshot_was_not_saved(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            result = PushResult("sent", "telegram_api", True, [201])
            result.signal_store_written = True
            result.ai_snapshot_ready = False
            gateway = FakeGateway(events, result)
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-private-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
                tg_bot_username="VIPpao_bot",
                tg_private_control_enable=True,
                tg_private_control_admin_user_id="123",
            )

            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                gateway,  # type: ignore[arg-type]
                launch_ai_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertEqual(pushes[0]["status"], "ai_snapshot_persist_failed")
            self.assertIn("reason:launch_ai_snapshot_persist_rollback", events)
            self.assertIn("delete:[201]", events)
            self.assertNotIn("commit", events)

    def test_post_delivery_ledger_failure_returns_ids_for_ai_card_rollback(self) -> None:
        for failure_point in ("_finish_delivery", "_append_history_record"):
            with (
                self.subTest(failure_point=failure_point),
                TemporaryDirectory() as tmp,
            ):
                events: list[str] = []
                settings = Settings(
                    data_dir=Path(tmp),
                    launch_message_package_v2_enable=True,
                    launch_ai_interpreter_enable=True,
                    ai_api_key="fake-private-key",
                    ai_base_url="https://provider.invalid/v1",
                    ai_model="fake-model",
                    tg_bot_username="VIPpao_bot",
                    tg_private_control_enable=True,
                    tg_private_control_admin_user_id="123",
                    tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                    tg_chat_id="-1001234567890",
                    tg_launch_alert_topic_id="12",
                    tg_default_cooldown_sec=0,
                )
                gateway = TelegramGateway(settings, JsonStore(Path(tmp)))
                engine = FakeEngine(events)

                with (
                    redirect_stderr(StringIO()),
                    patch.object(
                        gateway,
                        "_send_real_message_ids",
                        return_value=(True, [201]),
                    ),
                    patch.object(
                        gateway,
                        failure_point,
                        side_effect=OSError("local ledger unavailable"),
                    ),
                    patch.object(
                        gateway,
                        "delete_messages_detailed",
                        return_value={"deleted_ids": [201], "failed_ids": []},
                    ) as delete_mock,
                ):
                    pushes, _cleanup = push_launch_messages(
                        settings,
                        engine,  # type: ignore[arg-type]
                        gateway,
                        launch_ai_payload(),
                        SimpleNamespace(send=True, confirm_real_send=True),
                    )

                self.assertEqual(pushes[0]["status"], "ai_snapshot_persist_failed")
                delete_mock.assert_called_once_with(
                    [201],
                    reason="launch_ai_snapshot_persist_rollback",
                )
                self.assertNotIn("commit", events)

    def test_ai_snapshot_is_safe_when_button_route_is_not_ready(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            gateway = FakeGateway(
                events,
                PushResult("sent", "telegram_api", True, [201]),
            )
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-private-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
                tg_bot_username="",
                tg_private_control_enable=True,
                tg_private_control_admin_user_id=123,
            )

            push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                gateway,  # type: ignore[arg-type]
                launch_ai_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            kwargs = gateway.send_calls[0][1]
            self.assertIsNone(kwargs["url_button"])
            self.assertIn(
                "ai_context_snapshot",
                kwargs["signal_records"][0],
            )

    def test_failed_latest_package_is_retained_without_cleanup_requests(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            engine = FakeEngine(events)
            engine.pending_cleanups = [{
                "cycle_id": 7,
                "message_ids": [202],
                "expire_latest": True,
            }]
            gateway = FakeGateway(
                events,
                PushResult("sent", "telegram_api", True, [201]),
            )
            gateway.latest_topic_message_ids = [202]
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )

            pushes, cleanup = push_launch_messages(
                settings,
                engine,  # type: ignore[arg-type]
                gateway,  # type: ignore[arg-type]
                {"messages": [], "alerts": []},
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertEqual(pushes, [])
            self.assertNotIn("latest_topic_messages", events)
            self.assertFalse(any(event.startswith("pending:") for event in events))
            self.assertNotIn("reason:launch_cycle_expired", events)
            self.assertNotIn("delete:[202]", events)
            self.assertFalse(any(event.startswith("complete:") for event in events))
            self.assertFalse(cleanup["enabled"])
            self.assertEqual(cleanup["mode"], "retain_history_reply_chain")

    def test_expired_package_history_is_never_automatically_deleted(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            engine = FakeEngine(events)
            engine.pending_cleanups = [{
                "cycle_id": 7,
                "message_ids": list(range(100, 125)),
                "expire_latest": True,
            }]
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_message_cleanup_limit=20,
            )

            pushes, cleanup = push_launch_messages(
                settings,
                engine,  # type: ignore[arg-type]
                FakeGateway(events, PushResult("sent", "telegram_api", True, [201])),  # type: ignore[arg-type]
                {"messages": [], "alerts": []},
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertEqual(pushes, [])
            self.assertFalse(any(event.startswith("pending:") for event in events))
            self.assertFalse(any(event.startswith("delete:") for event in events))
            self.assertFalse(any(event.startswith("complete:") for event in events))
            self.assertEqual(cleanup["deleted_messages"], 0)

    def test_new_message_replies_then_commits_without_deleting_history(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            pushes, cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(events, PushResult("sent", "telegram_api", True, [201])),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertLess(events.index("send"), events.index("commit"))
            self.assertFalse(any(event.startswith("delete:") for event in events))
            self.assertFalse(any(event.startswith("complete:") for event in events))
            self.assertEqual(pushes[0]["status"], "sent")
            self.assertTrue(pushes[0]["reply_target_configured"])
            self.assertTrue(pushes[0]["previous_messages_retained"])
            self.assertNotIn("reply_to", pushes[0])
            self.assertEqual(cleanup["deleted_messages"], 0)

    def test_send_failure_never_commits_or_deletes_old_package(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(events, PushResult("failed", "telegram_api_failed")),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertIn("send", events)
            self.assertNotIn("commit", events)
            self.assertFalse(any(event.startswith("delete:") for event in events))
            self.assertEqual(pushes[0]["status"], "failed")

    def test_new_package_retains_previous_and_unrelated_topic_messages(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            gateway = FakeGateway(
                events,
                PushResult("sent", "telegram_api", True, [201]),
            )
            gateway.topic_cleanup_candidates = [88, 89]
            gateway.topic_undeletable_candidates = [77]

            pushes, cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                gateway,  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )
            self.assertIn("commit", events)
            self.assertFalse(any(event.startswith("delete:") for event in events))
            self.assertNotIn("reason:launch_package_replaced", events)
            self.assertNotIn("topic_candidates:[201]", events)
            self.assertNotIn("delete:[88, 89]", events)
            self.assertNotIn(
                "undeletable:telegram_delete_window_expired:[77]",
                events,
            )
            self.assertLess(events.index("mark"), events.index("reconcile:[]"))
            self.assertNotIn("topic_history_replaced_count", pushes[0])
            self.assertEqual(cleanup["topic_history_deleted"], 0)
            self.assertEqual(cleanup["topic_history_undeletable"], 0)
            self.assertEqual(
                cleanup["topic_state_reconciliation"]["message_ids_removed"],
                0,
            )
            self.assertTrue(pushes[0]["previous_messages_retained"])

    def test_first_package_is_standalone_and_followup_uses_reply_target(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            first = launch_payload()
            first["alerts"][0]["reply_to_message_id"] = 0  # type: ignore[index]
            first_events: list[str] = []
            first_gateway = FakeGateway(
                first_events,
                PushResult("sent", "telegram_api", True, [201]),
            )
            push_launch_messages(
                settings,
                FakeEngine(first_events),  # type: ignore[arg-type]
                first_gateway,  # type: ignore[arg-type]
                first,
                SimpleNamespace(send=True, confirm_real_send=True),
            )
            self.assertIsNone(
                first_gateway.send_calls[0][1]["reply_to_message_id"]
            )

            followup_events: list[str] = []
            followup_gateway = FakeGateway(
                followup_events,
                PushResult("sent", "telegram_api", True, [202]),
            )
            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(followup_events),  # type: ignore[arg-type]
                followup_gateway,  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )
            self.assertEqual(
                followup_gateway.send_calls[0][1]["reply_to_message_id"],
                101,
            )
            self.assertTrue(pushes[0]["reply_target_configured"])

    def test_partial_send_is_rolled_back_without_touching_old_package(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(
                    events,
                    PushResult("failed", "telegram_api_failed", False, [201]),
                ),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertNotIn("commit", events)
            self.assertIn("delete:[201]", events)
            self.assertNotIn("delete:[101]", events)
            self.assertEqual(pushes[0]["rollback_deleted"], 1)

    def test_commit_failure_rolls_back_new_message_and_keeps_old_package(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(events, commit_status="rejected"),  # type: ignore[arg-type]
                FakeGateway(events, PushResult("sent", "telegram_api", True, [201])),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertIn("commit", events)
            self.assertIn("delete:[201]", events)
            self.assertNotIn("delete:[101]", events)
            self.assertEqual(pushes[0]["status"], "package_commit_failed")

    def test_commit_exception_rolls_back_new_message_and_keeps_old_package(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(
                    events,
                    commit_exception=OSError("private database path"),
                ),  # type: ignore[arg-type]
                FakeGateway(
                    events,
                    PushResult("sent", "telegram_api", True, [201]),
                ),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertIn("commit", events)
            self.assertIn("reason:launch_package_commit_exception_rollback", events)
            self.assertIn("delete:[201]", events)
            self.assertNotIn("delete:[101]", events)
            self.assertEqual(pushes[0]["status"], "package_commit_failed")
            self.assertEqual(pushes[0]["package_commit"], "local_error")
            self.assertEqual(pushes[0]["package_commit_error"], "OSError")
            self.assertNotIn("private database path", str(pushes[0]))

    def test_chart_and_text_are_committed_as_one_photo_caption_message(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_chart_v2_enable=True,
            )
            payload = launch_payload()
            payload["alerts"][0]["chart_png_bytes"] = b"\x89PNG\r\n\x1a\nchart"  # type: ignore[index]
            gateway = FakeGateway(
                events,
                PushResult("sent", "telegram_api", True, [201]),
                photo_result=PushResult(
                    "sent",
                    "telegram_photo_api",
                    True,
                    [202],
                ),
            )
            pushes, cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                gateway,  # type: ignore[arg-type]
                payload,
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertLess(events.index("photo"), events.index("commit"))
            self.assertNotIn("send", events)
            self.assertIn("commit_ids:[202]", events)
            self.assertEqual(pushes[0]["status"], "sent")
            self.assertEqual(cleanup["charts_sent"], 1)
            self.assertNotIn("chart_png_bytes", payload["alerts"][0])  # type: ignore[index]
            text = str(gateway.send_calls[0][0][0])
            kwargs = gateway.send_calls[0][1]
            self.assertLessEqual(len(plain_fallback(text)), 1024)
            self.assertEqual(kwargs["photo"], b"\x89PNG\r\n\x1a\nchart")
            self.assertEqual(kwargs["reply_to_message_id"], 101)
            self.assertFalse(kwargs["enrich_market_context"])

    def test_photo_failure_retains_old_package_without_sending_separate_text(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_chart_v2_enable=True,
            )
            payload = launch_payload()
            payload["alerts"][0]["chart_png_bytes"] = b"\x89PNG\r\n\x1a\nchart"  # type: ignore[index]
            pushes, cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(
                    events,
                    PushResult("sent", "telegram_api", True, [201]),
                    photo_result=PushResult(
                        "failed",
                        "telegram_photo_api_failed",
                        False,
                        [],
                    ),
                ),  # type: ignore[arg-type]
                payload,
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertNotIn("commit", events)
            self.assertNotIn("send", events)
            self.assertIn("photo", events)
            self.assertNotIn("delete:[201]", events)
            self.assertNotIn("delete:[101]", events)
            self.assertEqual(pushes[0]["status"], "failed")
            self.assertEqual(cleanup["chart_failures"], 1)
            self.assertNotIn("chart_png_bytes", payload["alerts"][0])  # type: ignore[index]

    def test_missing_chart_skips_package_without_sending_or_deleting(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_chart_v2_enable=True,
            )
            pushes, cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(events, PushResult("sent", "telegram_api", True, [201])),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertNotIn("send", events)
            self.assertNotIn("commit", events)
            self.assertFalse(any(event.startswith("delete:") for event in events))
            self.assertEqual(pushes[0]["status"], "skipped")
            self.assertEqual(cleanup["chart_failures"], 1)


if __name__ == "__main__":
    unittest.main()
