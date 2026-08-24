from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest

from runtime.private_control import PrivateControlService


ADMIN_ID = 123456
BOT_TOKEN = "123456:fake-control-token"


class FakeResponse:
    def __init__(self, status_code: int = 200, body: object | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": []}

    def json(self) -> object:
        return self._body


class BrokenJsonResponse(FakeResponse):
    def json(self) -> object:
        raise ValueError("raw provider body must stay private")


class FakeSession:
    def __init__(self, *results: object):
        self.results = list(results)
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, FakeResponse)
        return result


class FakeConfigManager:
    def __init__(
        self,
        *,
        fail_set: bool = False,
    ):
        self.values: dict[str, object] = {
            "TG_PRIVATE_CONTROL_ALERT_ENABLE": False,
            "PULSE_RADAR_ENABLE": True,
            "RADAR_SUMMARY_ENABLE": True,
            "FUNDING_ALERT_ENABLE": True,
            "FLOW_RADAR_ENABLE": True,
            "ANNOUNCEMENT_RISK_ENABLE": True,
        }
        self.fail_set = fail_set
        self.set_calls: list[tuple[str, str]] = []

    def status(self) -> dict[str, object]:
        return dict(self.values)

    def set(self, key: str, value: str) -> dict[str, object]:
        self.set_calls.append((key, value))
        if self.fail_set:
            raise ValueError("secret provider error")
        self.values[key] = value == "true"
        return {"status": "ok", "value": self.values[key]}


def update(
    update_id: int,
    text: str,
    *,
    chat_type: str = "private",
    chat_id: int = ADMIN_ID,
    sender_id: int = ADMIN_ID,
    is_bot: bool = False,
    forwarded: bool = False,
) -> dict[str, object]:
    message: dict[str, object] = {
        "message_id": update_id + 10,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": sender_id, "is_bot": is_bot},
        "text": text,
    }
    if forwarded:
        message["forward_origin"] = {"type": "user"}
    return {"update_id": update_id, "message": message}


class PrivateControlTests(unittest.TestCase):
    def service(
        self,
        root: Path,
        *,
        enabled: bool = True,
        session: FakeSession | None = None,
        manager: FakeConfigManager | None = None,
        clock=lambda: 1_000.0,
        **readers: object,
    ) -> PrivateControlService:
        return PrivateControlService(
            enabled=enabled,
            bot_token=BOT_TOKEN,
            admin_user_id=ADMIN_ID,
            offset_path=root / "private_control_state.json",
            config_manager=manager or FakeConfigManager(),
            session=session,
            clock=clock,
            **readers,
        )

    def initialize(self, service: PrivateControlService) -> None:
        result = service.poll_once()
        self.assertEqual(result["status"], "initialized")

    def test_disabled_by_default_makes_zero_network_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            session = FakeSession()
            service = PrivateControlService(
                offset_path=Path(tmp) / "state.json",
                config_manager=FakeConfigManager(),
                session=session,
            )
            result = service.poll_once()

        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["network_activity"])
        self.assertEqual(result["telegram_http_calls"], 0)
        self.assertEqual(session.calls, [])

    def test_first_start_discards_old_updates_and_persists_offset(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            manager = FakeConfigManager()
            session = FakeSession(
                FakeResponse(body={"ok": True, "result": [
                    update(41, "关闭脉冲雷达"),
                    update(44, "确认关闭脉冲雷达"),
                ]})
            )
            service = self.service(root, session=session, manager=manager)
            result = service.poll_once()
            state = json.loads(
                (root / "private_control_state.json").read_text("utf-8")
            )

        self.assertEqual(result["status"], "initialized")
        self.assertEqual(result["ignored_updates"], 2)
        self.assertEqual(result["replies_sent"], 0)
        self.assertEqual(manager.set_calls, [])
        self.assertEqual(state["next_offset"], 45)
        request = session.calls[0]["json"]
        self.assertEqual(request["offset"], -1)
        self.assertEqual(request["timeout"], 0)

    def test_valid_admin_private_message_receives_chinese_menu(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = FakeSession(
                FakeResponse(),
                FakeResponse(body={"ok": True, "result": [update(1, "菜单")]}),
                FakeResponse(body={"ok": True, "result": {"message_id": 9}}),
            )
            service = self.service(root, session=session)
            self.initialize(service)
            result = service.poll_once()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["accepted_updates"], 1)
        self.assertEqual(result["replies_sent"], 1)
        get_payload = session.calls[1]["json"]
        self.assertEqual(get_payload["offset"], 0)
        self.assertEqual(get_payload["allowed_updates"], ["message"])
        self.assertEqual(get_payload["timeout"], 25)
        reply = session.calls[2]["json"]["text"]
        keyboard = session.calls[2]["json"]["reply_markup"]["keyboard"]
        self.assertIn("泡泡雷达管理", reply)
        self.assertIn("查运行", reply)
        self.assertIn("真实推送只能在 FinalShell 设置", reply)
        self.assertIn(["📡 雷达状态", "🩺 系统健康"], keyboard)

    def test_emoji_menu_buttons_keep_fixed_command_semantics(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager()
            service = self.service(
                Path(tmp),
                session=FakeSession(),
                manager=manager,
            )

            status = service.handle_update(update(1, "📡 雷达状态"))
            feature_menu = service.handle_update(update(2, "🧩 功能开关"))
            first = service.handle_update(update(3, "🚨 开启提醒"))
            second = service.handle_update(update(4, "确认开启故障提醒"))

        self.assertEqual(status.command, "radar_status")
        self.assertEqual(feature_menu.command, "feature_switches_menu")
        self.assertIn("故障提醒", feature_menu.text)
        self.assertIn("确认开启故障提醒", first.text)
        self.assertEqual(second.command, "configuration_updated")
        self.assertEqual(
            manager.set_calls,
            [("TG_PRIVATE_CONTROL_ALERT_ENABLE", "true")],
        )

    def test_group_other_user_bot_and_forward_are_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), session=FakeSession())
            cases = (
                update(1, "菜单", chat_type="supergroup"),
                update(2, "菜单", chat_id=999),
                update(3, "菜单", sender_id=999),
                update(4, "菜单", is_bot=True),
                update(5, "菜单", forwarded=True),
            )

            replies = [service.handle_update(item) for item in cases]

        self.assertEqual(replies, [None] * len(cases))

    def test_read_only_summaries_are_bounded_and_redacted(self) -> None:
        secret = "987654321:secret-token"
        with TemporaryDirectory() as tmp:
            service = self.service(
                Path(tmp),
                session=FakeSession(),
                radar_status_reader=lambda: {
                    "token": secret,
                    "radars": {
                        "launch_alert": {
                            "state": "running",
                            "delivery_mode": "dry_run",
                            "chat_id": 777,
                        },
                        "radar_summary": {"state": "degraded"},
                    },
                },
                health_reader=lambda: {
                    "status": "degraded",
                    "details": secret,
                    "checks": [
                        {"status": "ok", "raw": secret},
                        {"status": "warning"},
                        {"status": "failed"},
                    ],
                },
                delivery_quota_reader=lambda: {
                    "daily_limit": 20,
                    "sent_today": 7,
                    "private": secret,
                },
                topic_status_reader=lambda: {
                    "bot": "configured",
                    "chat": "configured",
                    "chat_id": 777,
                    "topic_id": 888,
                    "topics": {
                        "launch_alert": {"configured": True, "id": 999},
                    },
                    "token": secret,
                },
            )
            texts = [
                service.handle_update(update(1, "五雷达状态")).text,
                service.handle_update(update(2, "健康摘要")).text,
                service.handle_update(update(3, "发送额度")).text,
                service.handle_update(update(4, "话题配置")).text,
            ]

        combined = "\n".join(texts)
        self.assertIn("脉冲雷达：运行中 · 安全演练", combined)
        self.assertIn("提醒：1", combined)
        self.assertIn("剩余：13", combined)
        self.assertIn("机器人：已配置", combined)
        self.assertNotIn(secret, combined)
        self.assertNotIn("777", combined)
        self.assertNotIn("888", combined)
        self.assertNotIn("999", combined)

    def test_pulse_toggle_requires_exact_second_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager()
            service = self.service(Path(tmp), session=FakeSession(), manager=manager)

            first = service.handle_update(update(1, "关闭脉冲雷达"))
            wrong = service.handle_update(update(2, "确认关闭旧启动雷达"))
            calls_before_confirmation = list(manager.set_calls)
            second = service.handle_update(update(3, "确认关闭脉冲雷达"))

        self.assertIn("确认关闭脉冲雷达", first.text)
        self.assertEqual(calls_before_confirmation, [])
        self.assertIn("不支持", wrong.text)
        self.assertEqual(second.command, "configuration_updated")
        self.assertEqual(
            manager.set_calls,
            [("PULSE_RADAR_ENABLE", "false")],
        )

    def test_retired_launch_direction_and_ai_commands_are_unsupported(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager()
            service = self.service(Path(tmp), session=FakeSession(), manager=manager)

            replies = [
                service.handle_update(update(1, "开启方向雷达")),
                service.handle_update(update(2, "开启AI解读")),
                service.handle_update(update(3, "AI设置")),
            ]

        self.assertTrue(all(reply.command == "unsupported" for reply in replies))
        self.assertEqual(manager.set_calls, [])

    def test_confirmation_is_one_time_and_expires(self) -> None:
        now = [1_000.0]
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager()
            service = self.service(
                Path(tmp),
                session=FakeSession(),
                manager=manager,
                clock=lambda: now[0],
            )
            service.handle_update(update(1, "关闭脉冲雷达"))
            now[0] += 121
            expired = service.handle_update(update(2, "确认关闭脉冲雷达"))
            repeated = service.handle_update(update(3, "确认关闭脉冲雷达"))

        self.assertEqual(expired.command, "confirmation_invalid")
        self.assertEqual(repeated.command, "confirmation_invalid")
        self.assertEqual(manager.set_calls, [])

    def test_runtime_detail_views_are_fixed_bounded_readers(self) -> None:
        secret = "123456:private-secret"
        calls: list[str] = []

        def reader(name: str) -> object:
            def load() -> str:
                calls.append(name)
                return f"{name}：本地只读结果"

            return load

        with TemporaryDirectory() as tmp:
            service = self.service(
                Path(tmp),
                session=FakeSession(),
                recent_signals_reader=reader("最近信号"),
                push_records_reader=reader("推送记录"),
                unpublished_reasons_reader=reader("未推送原因"),
                fault_explanations_reader=reader("故障说明"),
            )
            menu = service.handle_update(update(1, "运行详情"))
            replies = [
                service.handle_update(update(index, command))
                for index, command in enumerate(
                    ("最近信号", "推送记录", "未推送原因", "故障说明"),
                    start=2,
                )
            ]

        self.assertEqual(menu.command, "runtime_details_menu")
        self.assertEqual(
            calls,
            ["最近信号", "推送记录", "未推送原因", "故障说明"],
        )
        self.assertNotIn(secret, "\n".join(reply.text for reply in replies))

    def test_each_radar_switch_requires_exact_confirmation(self) -> None:
        cases = (
            ("关闭脉冲雷达", "确认关闭脉冲雷达", "PULSE_RADAR_ENABLE"),
            ("关闭资金摘要", "确认关闭资金摘要", "RADAR_SUMMARY_ENABLE"),
            (
                "关闭资金费率警报",
                "确认关闭资金费率警报",
                "FUNDING_ALERT_ENABLE",
            ),
            ("关闭五因子资金流", "确认关闭五因子资金流", "FLOW_RADAR_ENABLE"),
            ("关闭公告风险", "确认关闭公告风险", "ANNOUNCEMENT_RISK_ENABLE"),
        )
        for request, confirmation, key in cases:
            with self.subTest(key=key), TemporaryDirectory() as tmp:
                manager = FakeConfigManager()
                service = self.service(
                    Path(tmp),
                    session=FakeSession(),
                    manager=manager,
                )
                first = service.handle_update(update(1, request))
                self.assertEqual(manager.set_calls, [])
                second = service.handle_update(update(2, confirmation))
                self.assertEqual(first.command, "confirmation_required")
                self.assertEqual(second.command, "configuration_updated")
                self.assertEqual(manager.set_calls, [(key, "false")])

    def test_fault_alert_toggle_cannot_change_real_send(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager()
            service = self.service(
                Path(tmp),
                session=FakeSession(),
                manager=manager,
            )
            service.handle_update(update(1, "开启故障提醒"))
            reply = service.handle_update(update(2, "确认开启故障提醒"))

        self.assertEqual(reply.command, "configuration_updated")
        self.assertEqual(
            manager.set_calls,
            [("TG_PRIVATE_CONTROL_ALERT_ENABLE", "true")],
        )
        self.assertTrue(all("REAL" not in key for key, _ in manager.set_calls))

    def test_config_manager_failure_is_sanitized(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager(fail_set=True)
            service = self.service(Path(tmp), session=FakeSession(), manager=manager)
            service.handle_update(update(1, "关闭公告风险"))
            reply = service.handle_update(update(2, "确认关闭公告风险"))

        self.assertEqual(reply.command, "configuration_update_failed")
        self.assertNotIn("secret provider error", reply.text)

    def test_network_timeout_has_one_attempt_and_no_secret_in_result(self) -> None:
        with TemporaryDirectory() as tmp:
            session = FakeSession(TimeoutError("contains secret response"))
            service = self.service(Path(tmp), session=session)
            result = service.poll_once()

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"], "telegram_timeout")
        self.assertEqual(result["telegram_http_calls"], 1)
        self.assertEqual(len(session.calls), 1)
        self.assertNotIn(BOT_TOKEN, json.dumps(result))
        self.assertNotIn("contains secret response", json.dumps(result))

    def test_proactive_alert_targets_only_private_admin_without_topic(self) -> None:
        with TemporaryDirectory() as tmp:
            session = FakeSession(
                FakeResponse(body={"ok": True, "result": {"message_id": 9}})
            )
            service = self.service(Path(tmp), session=session)

            sent = service.send_private_alert("固定中文故障提醒")

        self.assertTrue(sent)
        payload = session.calls[0]["json"]
        self.assertEqual(payload["chat_id"], ADMIN_ID)
        self.assertNotIn("message_thread_id", payload)
        self.assertNotIn("reply_to_message_id", payload)

    def test_http_and_invalid_json_errors_are_sanitized(self) -> None:
        cases = (
            (FakeResponse(401, {"description": "secret"}), "telegram_auth_failed"),
            (FakeResponse(409, {"description": "secret"}), "telegram_polling_conflict"),
            (FakeResponse(429, {"description": "secret"}), "telegram_rate_limited"),
            (BrokenJsonResponse(), "telegram_invalid_response"),
        )
        for response, expected in cases:
            with self.subTest(expected=expected), TemporaryDirectory() as tmp:
                service = self.service(Path(tmp), session=FakeSession(response))
                result = service.poll_once()
                self.assertEqual(result["error"], expected)
                self.assertNotIn("secret", json.dumps(result))

    def test_send_failure_is_not_retried_and_advances_offset(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = FakeSession(
                FakeResponse(),
                FakeResponse(body={"ok": True, "result": [update(8, "菜单")]}),
                FakeResponse(500, {"description": "private provider error"}),
            )
            service = self.service(root, session=session)
            self.initialize(service)
            result = service.poll_once()
            state = json.loads(
                (root / "private_control_state.json").read_text("utf-8")
            )

        self.assertEqual(result["error"], "telegram_provider_unavailable")
        self.assertEqual(result["telegram_http_calls"], 2)
        self.assertEqual(len(session.calls), 3)
        self.assertEqual(state["next_offset"], 9)
        self.assertNotIn("private provider error", json.dumps(result))

    def test_state_file_is_private_and_atomic_temp_is_removed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.service(root, session=FakeSession(FakeResponse()))
            self.initialize(service)
            state_path = root / "private_control_state.json"

            temporary_files = list(root.glob("private_control_state.json.tmp.*"))
            mode = stat.S_IMODE(state_path.stat().st_mode)

        self.assertEqual(temporary_files, [])
        if os.name == "posix":
            self.assertEqual(mode, 0o600)

    def test_missing_admin_or_transport_fails_before_network(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = FakeSession()
            no_admin = PrivateControlService(
                enabled=True,
                bot_token=BOT_TOKEN,
                admin_user_id="not-a-number",
                offset_path=root / "one.json",
                config_manager=FakeConfigManager(),
                session=session,
            ).poll_once()
            no_transport = PrivateControlService(
                enabled=True,
                bot_token=BOT_TOKEN,
                admin_user_id=ADMIN_ID,
                offset_path=root / "two.json",
                config_manager=FakeConfigManager(),
            ).poll_once()

        self.assertEqual(no_admin["error"], "private_control_admin_not_configured")
        self.assertEqual(
            no_transport["error"], "private_control_transport_not_configured"
        )
        self.assertEqual(session.calls, [])


if __name__ == "__main__":
    unittest.main()
