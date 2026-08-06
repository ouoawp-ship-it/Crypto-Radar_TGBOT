from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from runtime.private_control import PrivateControlService


ADMIN_ID = 123456
BOT_TOKEN = "123456:fake-private-control-token"


class FakeResponse:
    def __init__(self, status_code: int = 200, body: object | None = None):
        self.status_code = status_code
        self._body = body if body is not None else {"ok": True, "result": {}}

    def json(self) -> object:
        return self._body


class RoutingSession:
    def __init__(
        self,
        *update_batches: list[dict[str, object]],
        delete_status: int = 200,
    ) -> None:
        self.update_batches = list(update_batches)
        self.delete_status = delete_status
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, **kwargs: object) -> FakeResponse:
        method = url.rsplit("/", 1)[-1]
        self.calls.append({"method": method, "url": url, **kwargs})
        if method == "getUpdates":
            updates = self.update_batches.pop(0) if self.update_batches else []
            return FakeResponse(body={"ok": True, "result": updates})
        if method == "deleteMessage" and self.delete_status != 200:
            return FakeResponse(
                self.delete_status,
                {"ok": False, "error_code": self.delete_status},
            )
        return FakeResponse(body={"ok": True, "result": {"message_id": 999}})


class FakeConfigManager:
    def __init__(self, *, fail_keys: set[str] | None = None) -> None:
        self.values: dict[str, object] = {
            "LAUNCH_FUSION_ENABLE": True,
            "LAUNCH_DIRECTIONAL_ENABLE": True,
            "LAUNCH_AI_INTERPRETER_ENABLE": False,
            "AI_API_KEY": "not_configured",
            "AI_BASE_URL": "not_configured",
            "AI_MODEL": "not_configured",
            "AI_OPERATOR_PROMPT": "default",
        }
        self.fail_keys = fail_keys or set()
        self.set_calls: list[tuple[str, str]] = []
        self.prompt_calls: list[str] = []
        self.clear_prompt_calls = 0

    def status(self) -> dict[str, object]:
        return dict(self.values)

    def set(self, key: str, value: str) -> dict[str, object]:
        self.set_calls.append((key, value))
        if key in self.fail_keys:
            raise ValueError(f"provider rejected private value: {value}")
        if key == "LAUNCH_AI_INTERPRETER_ENABLE":
            self.values[key] = value == "true"
        else:
            self.values[key] = "configured" if value else "not_configured"
        return {"status": "ok", "value": self.values[key]}

    def set_ai_prompt(self, value: str) -> dict[str, object]:
        self.prompt_calls.append(value)
        if "AI_OPERATOR_PROMPT" in self.fail_keys:
            raise ValueError(f"provider rejected private prompt: {value}")
        self.values["AI_OPERATOR_PROMPT"] = "configured"
        return {"status": "ok", "value": "configured"}

    def clear_ai_prompt(self) -> dict[str, object]:
        self.clear_prompt_calls += 1
        self.values["AI_OPERATOR_PROMPT"] = "default"
        return {"status": "ok", "value": "default"}


def update(
    update_id: int,
    text: str,
    *,
    chat_type: str = "private",
    chat_id: int = ADMIN_ID,
    sender_id: int = ADMIN_ID,
) -> dict[str, object]:
    return {
        "update_id": update_id,
        "message": {
            "message_id": update_id + 100,
            "chat": {"id": chat_id, "type": chat_type},
            "from": {"id": sender_id, "is_bot": False},
            "text": text,
        },
    }


class PrivateControlAiConfigTests(unittest.TestCase):
    def service(
        self,
        root: Path,
        *,
        manager: FakeConfigManager | None = None,
        session: RoutingSession | None = None,
        clock=lambda: 1_000.0,
    ) -> PrivateControlService:
        return PrivateControlService(
            enabled=True,
            bot_token=BOT_TOKEN,
            admin_user_id=ADMIN_ID,
            offset_path=root / "private_control_state.json",
            config_manager=manager or FakeConfigManager(),
            session=session or RoutingSession(),
            clock=clock,
        )

    @staticmethod
    def _keyboard_text(reply: object) -> str:
        keyboard = getattr(reply, "keyboard", None) or ()
        return "\n".join(str(value) for row in keyboard for value in row)

    def test_ai_submenu_is_redacted_and_exposes_all_requested_actions(self) -> None:
        manager = FakeConfigManager()
        manager.values.update(
            AI_API_KEY="configured",
            AI_BASE_URL="configured",
            AI_MODEL="configured",
            AI_OPERATOR_PROMPT="configured",
        )
        with TemporaryDirectory() as tmp:
            reply = self.service(Path(tmp), manager=manager).handle_update(
                update(1, "AI设置")
            )

        self.assertIsNotNone(reply)
        self.assertEqual(reply.command, "ai_settings_menu")
        self.assertIn("AI", reply.text)
        self.assertIn("密钥", reply.text)
        self.assertIn("接口", reply.text)
        self.assertIn("模型", reply.text)
        self.assertIn("提示词", reply.text)
        keyboard = self._keyboard_text(reply)
        for action in (
            "设置AI密钥",
            "设置AI接口",
            "设置AI模型",
            "设置AI提示词",
            "恢复默认提示词",
            "开启AI",
            "关闭AI",
        ):
            self.assertIn(action, keyboard)

    def test_invalid_prompt_status_is_shown_as_safe_default_fallback(self) -> None:
        manager = FakeConfigManager()
        manager.values["AI_OPERATOR_PROMPT"] = "invalid"
        with TemporaryDirectory() as tmp:
            reply = self.service(Path(tmp), manager=manager).handle_update(
                update(1, "AI状态")
            )

        self.assertIn("异常，已回退系统默认", reply.text)
        self.assertNotIn("自定义", reply.text)

    def test_pending_secret_input_uses_fixed_key_and_never_echoes_value(self) -> None:
        secret = "sk-fake-private-secret-never-echo"
        manager = FakeConfigManager()
        with TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), manager=manager)
            request = service.handle_update(update(1, "设置AI密钥"))
            reply = service.handle_update(update(2, secret))

        self.assertEqual(request.command, "ai_input_required")
        self.assertNotIn(secret, request.text)
        self.assertEqual(reply.command, "ai_configuration_updated")
        self.assertTrue(getattr(reply, "delete_source_message", False))
        self.assertEqual(manager.set_calls, [("AI_API_KEY", secret)])
        self.assertNotIn(secret, reply.text)
        self.assertIn("已配置", reply.text)

    def test_group_and_other_user_cannot_start_or_complete_pending_input(self) -> None:
        secret = "sk-fake-admin-only"
        manager = FakeConfigManager()
        with TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), manager=manager)
            self.assertIsNone(
                service.handle_update(update(1, "设置AI密钥", chat_type="supergroup"))
            )
            self.assertIsNone(
                service.handle_update(update(2, secret, sender_id=999999))
            )
            service.handle_update(update(3, "设置AI密钥"))
            self.assertIsNone(
                service.handle_update(update(4, secret, chat_id=999999, sender_id=999999))
            )
            accepted = service.handle_update(update(5, secret))

        self.assertEqual(accepted.command, "ai_configuration_updated")
        self.assertEqual(manager.set_calls, [("AI_API_KEY", secret)])

    def test_secret_is_not_written_to_offset_or_sent_back_and_source_is_deleted(self) -> None:
        secret = "sk-fake-never-persist-in-update-state"
        manager = FakeConfigManager()
        session = RoutingSession(
            [],
            [update(10, "设置AI密钥"), update(11, secret)],
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            service = self.service(root, manager=manager, session=session)
            initialized = service.poll_once()
            result = service.poll_once()
            state_text = (root / "private_control_state.json").read_text("utf-8")

        self.assertEqual(initialized["status"], "initialized")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(manager.set_calls, [("AI_API_KEY", secret)])
        self.assertNotIn(secret, state_text)
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        delete_calls = [call for call in session.calls if call["method"] == "deleteMessage"]
        self.assertEqual(len(delete_calls), 1)
        self.assertEqual(delete_calls[0]["json"]["message_id"], 111)
        sent_text = "\n".join(
            str(call["json"].get("text", ""))
            for call in session.calls
            if call["method"] == "sendMessage"
        )
        self.assertNotIn(secret, sent_text)
        self.assertNotIn("手动删除", sent_text)

    def test_source_delete_failure_does_not_undo_valid_configuration(self) -> None:
        secret = "sk-fake-delete-failure"
        manager = FakeConfigManager()
        session = RoutingSession(
            [],
            [update(20, "设置AI密钥"), update(21, secret)],
            delete_status=500,
        )
        with TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), manager=manager, session=session)
            service.poll_once()
            result = service.poll_once()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(manager.set_calls, [("AI_API_KEY", secret)])
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertEqual(
            [call["method"] for call in session.calls].count("deleteMessage"),
            1,
        )
        sent_text = "\n".join(
            str(call["json"].get("text", ""))
            for call in session.calls
            if call["method"] == "sendMessage"
        )
        self.assertNotIn(secret, sent_text)
        self.assertIn("手动删除", sent_text)

    def test_url_and_model_validation_failures_are_sanitized(self) -> None:
        cases = (
            ("设置AI接口", "https://bad.example/?secret=value", "AI_BASE_URL"),
            ("设置AI模型", "bad model private value", "AI_MODEL"),
        )
        for request, value, key in cases:
            with self.subTest(key=key), TemporaryDirectory() as tmp:
                manager = FakeConfigManager(fail_keys={key})
                service = self.service(Path(tmp), manager=manager)
                service.handle_update(update(1, request))
                reply = service.handle_update(update(2, value))

                self.assertEqual(reply.command, "configuration_update_failed")
                self.assertTrue(getattr(reply, "delete_source_message", False))
                self.assertNotIn(value, reply.text)
                self.assertNotIn("provider rejected", reply.text)

    def test_blank_ai_values_are_rejected_without_clearing_old_configuration(self) -> None:
        for request in (
            "设置AI密钥",
            "设置AI接口",
            "设置AI模型",
            "设置AI提示词",
        ):
            with self.subTest(request=request), TemporaryDirectory() as tmp:
                manager = FakeConfigManager()
                service = self.service(Path(tmp), manager=manager)
                service.handle_update(update(1, request))
                reply = service.handle_update(update(2, "   "))

                self.assertEqual(reply.command, "configuration_update_failed")
                self.assertTrue(reply.delete_source_message)
                self.assertEqual(manager.set_calls, [])
                self.assertEqual(manager.prompt_calls, [])

    def test_multiline_prompt_uses_prompt_store_and_is_not_echoed(self) -> None:
        prompt = "第一行：重点说明现货确认。\n第二行：语言更简短。"
        manager = FakeConfigManager()
        with TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), manager=manager)
            service.handle_update(update(1, "设置AI提示词"))
            reply = service.handle_update(update(2, prompt))

        self.assertEqual(manager.prompt_calls, [prompt])
        self.assertEqual(reply.command, "ai_configuration_updated")
        self.assertTrue(getattr(reply, "delete_source_message", False))
        self.assertNotIn(prompt, reply.text)

    def test_pending_input_can_be_cancelled_or_expire_without_writing(self) -> None:
        now = [1_000.0]
        manager = FakeConfigManager()
        with TemporaryDirectory() as tmp:
            service = self.service(
                Path(tmp),
                manager=manager,
                clock=lambda: now[0],
            )
            service.handle_update(update(1, "设置AI密钥"))
            cancelled = service.handle_update(update(2, "取消"))
            service.handle_update(update(3, "设置AI模型"))
            now[0] += 301
            expired = service.handle_update(update(4, "fake-model"))

        self.assertEqual(cancelled.command, "input_cancelled")
        self.assertEqual(expired.command, "input_expired")
        self.assertEqual(manager.set_calls, [])

    def test_ai_enable_still_requires_exact_second_confirmation(self) -> None:
        manager = FakeConfigManager()
        manager.values.update(
            AI_API_KEY="configured",
            AI_BASE_URL="configured",
            AI_MODEL="configured",
        )
        with TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), manager=manager)
            first = service.handle_update(update(1, "开启AI解读"))
            before = list(manager.set_calls)
            second = service.handle_update(update(2, "确认开启AI解读"))

        self.assertEqual(first.command, "confirmation_required")
        self.assertEqual(before, [])
        self.assertEqual(second.command, "configuration_updated")
        self.assertEqual(
            manager.set_calls,
            [("LAUNCH_AI_INTERPRETER_ENABLE", "true")],
        )

    def test_restore_default_prompt_requires_exact_second_confirmation(self) -> None:
        manager = FakeConfigManager()
        manager.values["AI_OPERATOR_PROMPT"] = "configured"
        with TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), manager=manager)
            first = service.handle_update(update(1, "恢复默认提示词"))
            self.assertEqual(manager.clear_prompt_calls, 0)
            second = service.handle_update(update(2, "确认恢复默认AI提示词"))

        self.assertEqual(first.command, "confirmation_required")
        self.assertEqual(second.command, "configuration_updated")
        self.assertEqual(manager.clear_prompt_calls, 1)

    def test_non_sensitive_menu_message_is_not_deleted(self) -> None:
        session = RoutingSession([], [update(1, "AI设置")])
        with TemporaryDirectory() as tmp:
            service = self.service(Path(tmp), session=session)
            service.poll_once()
            result = service.poll_once()

        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [call["method"] for call in session.calls].count("deleteMessage"),
            0,
        )


if __name__ == "__main__":
    unittest.main()
