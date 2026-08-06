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
        ai_ready: bool = True,
        directional_enabled: bool = False,
        fusion_enabled: bool = True,
        fail_set: bool = False,
    ):
        self.values: dict[str, object] = {
            "LAUNCH_FUSION_ENABLE": fusion_enabled,
            "LAUNCH_DIRECTIONAL_ENABLE": directional_enabled,
            "LAUNCH_AI_INTERPRETER_ENABLE": False,
            "AI_API_KEY": "configured" if ai_ready else "not_configured",
            "AI_BASE_URL": "configured" if ai_ready else "not_configured",
            "AI_MODEL": "fake-model" if ai_ready else "not_configured",
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
                    update(41, "开启方向雷达"),
                    update(44, "确认开启方向雷达"),
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
        self.assertIn("管理员私聊菜单", reply)
        self.assertIn("五雷达状态", reply)
        self.assertIn("不能切换真实推送模式", reply)
        self.assertIn(["五雷达状态", "健康摘要"], keyboard)

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
        self.assertIn("启动预警：运行中 · 安全演练", combined)
        self.assertIn("提醒：1", combined)
        self.assertIn("剩余：13", combined)
        self.assertIn("机器人：已配置", combined)
        self.assertNotIn(secret, combined)
        self.assertNotIn("777", combined)
        self.assertNotIn("888", combined)
        self.assertNotIn("999", combined)

    def test_directional_toggle_requires_exact_second_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager()
            service = self.service(Path(tmp), session=FakeSession(), manager=manager)

            first = service.handle_update(update(1, "开启方向雷达"))
            wrong = service.handle_update(update(2, "确认打开方向雷达"))
            calls_before_confirmation = list(manager.set_calls)
            second = service.handle_update(update(3, "确认开启方向雷达"))

        self.assertIn("确认开启方向雷达", first.text)
        self.assertEqual(calls_before_confirmation, [])
        self.assertIn("不支持", wrong.text)
        self.assertEqual(second.command, "configuration_updated")
        self.assertEqual(
            manager.set_calls,
            [("LAUNCH_DIRECTIONAL_ENABLE", "true")],
        )

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
            service.handle_update(update(1, "关闭方向雷达"))
            now[0] += 121
            expired = service.handle_update(update(2, "确认关闭方向雷达"))
            repeated = service.handle_update(update(3, "确认关闭方向雷达"))

        self.assertEqual(expired.command, "confirmation_invalid")
        self.assertEqual(repeated.command, "confirmation_invalid")
        self.assertEqual(manager.set_calls, [])

    def test_ai_enable_is_refused_until_configuration_is_complete(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager(
                ai_ready=False,
                directional_enabled=True,
            )
            service = self.service(Path(tmp), session=FakeSession(), manager=manager)

            reply = service.handle_update(update(1, "开启AI解读"))
            confirmation = service.handle_update(update(2, "确认开启AI解读"))

        self.assertEqual(reply.command, "ai_not_ready")
        self.assertEqual(confirmation.command, "confirmation_invalid")
        self.assertEqual(manager.set_calls, [])

    def test_ai_toggle_uses_only_allowlisted_fixed_key(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager(
                ai_ready=True,
                directional_enabled=True,
            )
            service = self.service(Path(tmp), session=FakeSession(), manager=manager)
            service.handle_update(update(1, "开启AI解读"))
            reply = service.handle_update(update(2, "确认开启AI解读"))
            unsupported = service.handle_update(
                update(3, "set MAIN_BOT_DELIVERY_MODE=real; rm -rf /")
            )

        self.assertEqual(reply.command, "configuration_updated")
        self.assertEqual(unsupported.command, "unsupported")
        self.assertEqual(
            manager.set_calls,
            [("LAUNCH_AI_INTERPRETER_ENABLE", "true")],
        )
        self.assertTrue(all("REAL" not in key for key, _ in manager.set_calls))

    def test_ai_enable_requires_directional_radar_first(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager(ai_ready=True)
            service = self.service(Path(tmp), session=FakeSession(), manager=manager)

            reply = service.handle_update(update(1, "开启AI解读"))

        self.assertEqual(reply.command, "directional_not_enabled")
        self.assertEqual(manager.set_calls, [])

    def test_directional_enable_requires_fusion_foundation(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager(fusion_enabled=False)
            service = self.service(Path(tmp), session=FakeSession(), manager=manager)

            reply = service.handle_update(update(1, "开启方向雷达"))

        self.assertEqual(
            reply.command,
            "directional_prerequisite_not_ready",
        )
        self.assertEqual(manager.set_calls, [])

    def test_config_manager_failure_is_sanitized(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = FakeConfigManager(fail_set=True)
            service = self.service(Path(tmp), session=FakeSession(), manager=manager)
            service.handle_update(update(1, "关闭AI解读"))
            reply = service.handle_update(update(2, "确认关闭AI解读"))

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
