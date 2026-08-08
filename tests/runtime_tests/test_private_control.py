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
PUBLIC_REF = "sig_0123456789abcdefabcd"
AI_DEEP_LINK_COMMAND = f"/start ai_{PUBLIC_REF}"


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
            "TG_BOT_USERNAME": "VIPpao_bot",
            "TG_PRIVATE_CONTROL_ALERT_ENABLE": False,
            "LAUNCH_ALERT_ENABLE": True,
            "RADAR_SUMMARY_ENABLE": True,
            "FUNDING_ALERT_ENABLE": True,
            "FLOW_RADAR_ENABLE": True,
            "ANNOUNCEMENT_RISK_ENABLE": True,
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
        self.assertIn("泡泡雷达管理", reply)
        self.assertIn("查运行", reply)
        self.assertIn("真实推送只能在 FinalShell 设置", reply)
        self.assertIn(["📡 雷达状态", "🩺 系统健康"], keyboard)

    def test_string_admin_id_is_bounded_and_invalid_values_are_ignored(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            valid = PrivateControlService(
                enabled=True,
                bot_token=BOT_TOKEN,
                admin_user_id=str(ADMIN_ID),
                offset_path=root / "valid.json",
                config_manager=FakeConfigManager(),
                session=FakeSession(),
            )
            too_large = PrivateControlService(
                enabled=True,
                bot_token=BOT_TOKEN,
                admin_user_id="9223372036854775808",
                offset_path=root / "large.json",
                config_manager=FakeConfigManager(),
                session=FakeSession(),
            )
            very_long = PrivateControlService(
                enabled=True,
                bot_token=BOT_TOKEN,
                admin_user_id="9" * 5_000,
                offset_path=root / "long.json",
                config_manager=FakeConfigManager(),
                session=FakeSession(),
            )

            valid_reply = valid.handle_update(update(1, "/start"))
            too_large_reply = too_large.handle_update(update(2, "/start"))
            very_long_reply = very_long.handle_update(update(3, "/start"))

        self.assertEqual(valid_reply.command, "menu")
        self.assertIsNone(too_large_reply)
        self.assertIsNone(very_long_reply)

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
            first = service.handle_update(update(3, "🧭 开启方向"))
            second = service.handle_update(update(4, "确认开启方向雷达"))

        self.assertEqual(status.command, "radar_status")
        self.assertEqual(feature_menu.command, "feature_switches_menu")
        self.assertIn("方向分析", feature_menu.text)
        self.assertIn("确认开启方向雷达", first.text)
        self.assertEqual(second.command, "configuration_updated")
        self.assertEqual(
            manager.set_calls,
            [("LAUNCH_DIRECTIONAL_ENABLE", "true")],
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

    def test_admin_ai_deep_link_calls_only_injected_requester(self) -> None:
        calls: list[str] = []

        def requester(public_ref: str) -> dict[str, object]:
            calls.append(public_ref)
            return {
                "status": "completed",
                "text": "规则证据偏多，但当前阶段仍不适合追涨。",
                "private_error": "must-not-be-returned",
            }

        with TemporaryDirectory() as tmp:
            service = self.service(
                Path(tmp),
                session=FakeSession(),
                ai_on_demand_requester=requester,
            )
            reply = service.handle_update(update(1, AI_DEEP_LINK_COMMAND))

        self.assertEqual(calls, [PUBLIC_REF])
        self.assertEqual(reply.command, "ai_on_demand_completed")
        self.assertEqual(
            reply.text,
            "✅ AI 解读完成。\n\n规则证据偏多，但当前阶段仍不适合追涨。",
        )
        self.assertNotIn("must-not-be-returned", reply.text)

    def test_ai_deep_link_sends_processing_before_synchronous_interpretation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            session = FakeSession(
                FakeResponse(),
                FakeResponse(body={"ok": True, "result": [
                    update(1, AI_DEEP_LINK_COMMAND),
                ]}),
                FakeResponse(body={"ok": True, "result": {"message_id": 8}}),
                FakeResponse(body={"ok": True, "result": {"message_id": 9}}),
            )
            requester_calls: list[str] = []

            def requester(public_ref: str) -> dict[str, object]:
                requester_calls.append(public_ref)
                self.assertEqual(len(session.calls), 3)
                self.assertIn("处理中", session.calls[2]["json"]["text"])
                return {
                    "status": "completed",
                    "text": "这是基于信号快照生成的按需解读。",
                }

            service = self.service(
                root,
                session=session,
                ai_on_demand_requester=requester,
            )
            self.initialize(service)
            result = service.poll_once()

        self.assertEqual(requester_calls, [PUBLIC_REF])
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["accepted_updates"], 1)
        self.assertEqual(result["replies_sent"], 2)
        self.assertEqual(result["telegram_http_calls"], 3)
        self.assertIn("AI 解读完成", session.calls[3]["json"]["text"])

    def test_ai_deep_link_requires_private_admin_and_rejects_forward(self) -> None:
        calls: list[str] = []

        def requester(public_ref: str) -> dict[str, str]:
            calls.append(public_ref)
            return {"status": "processing"}

        with TemporaryDirectory() as tmp:
            service = self.service(
                Path(tmp),
                session=FakeSession(),
                ai_on_demand_requester=requester,
            )
            cases = (
                update(1, AI_DEEP_LINK_COMMAND, chat_type="supergroup"),
                update(2, AI_DEEP_LINK_COMMAND, chat_id=999),
                update(3, AI_DEEP_LINK_COMMAND, sender_id=999),
                update(4, AI_DEEP_LINK_COMMAND, is_bot=True),
                update(5, AI_DEEP_LINK_COMMAND, forwarded=True),
            )
            replies = [service.handle_update(item) for item in cases]

        self.assertEqual(replies, [None] * len(cases))
        self.assertEqual(calls, [])

    def test_ai_deep_link_is_strict_and_plain_start_stays_compatible(self) -> None:
        calls: list[str] = []

        def requester(public_ref: str) -> dict[str, str]:
            calls.append(public_ref)
            return {"status": "processing"}

        malformed = (
            "/start ai_sig_0123456789abcdefabc",
            "/start ai_sig_0123456789abcdefabcde",
            "/start ai_sig_0123456789ABCDEFABCD",
            "/start ai_sig_0123456789abcdefabcg",
            f"{AI_DEEP_LINK_COMMAND} extra",
            "/start arbitrary-text",
            "/start\tai_sig_0123456789abcdefabcd",
            "🤖 /start ai_sig_0123456789abcdefabcd",
            "/starter ai_sig_0123456789abcdefabcd",
        )
        with TemporaryDirectory() as tmp:
            service = self.service(
                Path(tmp),
                session=FakeSession(),
                ai_on_demand_requester=requester,
            )
            rejected = [
                service.handle_update(update(index, command))
                for index, command in enumerate(malformed, start=1)
            ]
            menu = service.handle_update(update(99, "/start"))

        self.assertEqual(calls, [])
        self.assertTrue(
            all(
                reply.command == "ai_on_demand_security_failed"
                and reply.text == "🔒 AI 解读安全校验失败。"
                for reply in rejected
            )
        )
        self.assertEqual(menu.command, "menu")
        self.assertIn("泡泡雷达管理", menu.text)

    def test_ai_deep_link_preempts_and_clears_pending_ai_secret_input(self) -> None:
        manager = FakeConfigManager()
        with TemporaryDirectory() as tmp:
            service = self.service(
                Path(tmp),
                session=FakeSession(),
                manager=manager,
                ai_on_demand_requester=lambda _public_ref: {
                    "status": "processing"
                },
            )
            pending = service.handle_update(update(1, "设置AI密钥"))
            deep_link = service.handle_update(update(2, AI_DEEP_LINK_COMMAND))
            later_text = service.handle_update(update(3, "sk-must-not-be-saved"))

        self.assertEqual(pending.command, "ai_input_required")
        self.assertEqual(deep_link.command, "ai_on_demand_processing")
        self.assertEqual(later_text.command, "unsupported")
        self.assertEqual(manager.set_calls, [])

    def test_ai_deep_link_statuses_have_fixed_sanitized_chinese_replies(self) -> None:
        cases = (
            (
                {"status": "processing"},
                "ai_on_demand_processing",
                "⏳ AI 解读处理中，请稍候。",
            ),
            (
                {"status": "cached", "text": "缓存内容"},
                "ai_on_demand_cached",
                "♻️ 已返回缓存的 AI 解读。\n\n缓存内容",
            ),
            (
                {"status": "disabled"},
                "ai_on_demand_disabled",
                "⏸️ 主动 AI 解读未开启。",
            ),
            (
                {"status": "not_configured"},
                "ai_on_demand_not_configured",
                "⚙️ AI 解读尚未配置。",
            ),
            (
                {"status": "not_found"},
                "ai_on_demand_signal_unavailable",
                "⌛ 信号记录已过期或缺失，请查看最新信号。",
            ),
            (
                {"status": "expired"},
                "ai_on_demand_signal_unavailable",
                "⌛ 信号记录已过期或缺失，请查看最新信号。",
            ),
            (
                {"status": "rate_limited"},
                "ai_on_demand_rate_limited",
                "⏱️ AI 解读请求太快，请稍后再试。",
            ),
            (
                {"status": "quota_exhausted"},
                "ai_on_demand_quota_exhausted",
                "🚫 AI 解读额度已耗尽，请稍后再试。",
            ),
            (
                {"status": "timeout"},
                "ai_on_demand_timeout",
                "⌛ AI 解读请求超时，请稍后再试。",
            ),
            (
                {"status": "security_failed"},
                "ai_on_demand_security_failed",
                "🔒 AI 解读安全校验失败。",
            ),
        )
        for result, expected_command, expected_text in cases:
            with self.subTest(status=result["status"]), TemporaryDirectory() as tmp:
                service = self.service(
                    Path(tmp),
                    session=FakeSession(),
                    ai_on_demand_requester=lambda _ref, value=result: value,
                )
                reply = service.handle_update(update(1, AI_DEEP_LINK_COMMAND))

                self.assertEqual(reply.command, expected_command)
                self.assertEqual(reply.text, expected_text)

    def test_ai_deep_link_defaults_off_and_sanitizes_invalid_requester_results(self) -> None:
        with TemporaryDirectory() as tmp:
            disabled = self.service(
                Path(tmp),
                session=FakeSession(),
            ).handle_update(update(1, AI_DEEP_LINK_COMMAND))

        self.assertEqual(disabled.command, "ai_on_demand_disabled")
        self.assertEqual(disabled.text, "⏸️ 主动 AI 解读未开启。")

        invalid_results = (
            None,
            "completed",
            {},
            {"status": "unknown", "error": "secret provider body"},
            {"status": "completed", "text": ""},
            {"status": "cached", "text": 123},
        )
        for result in invalid_results:
            with self.subTest(result=result), TemporaryDirectory() as tmp:
                service = self.service(
                    Path(tmp),
                    session=FakeSession(),
                    ai_on_demand_requester=lambda _ref, value=result: value,
                )
                reply = service.handle_update(update(1, AI_DEEP_LINK_COMMAND))

                self.assertEqual(reply.command, "ai_on_demand_security_failed")
                self.assertEqual(reply.text, "🔒 AI 解读安全校验失败。")
                self.assertNotIn("secret provider body", reply.text)

    def test_ai_deep_link_requester_exceptions_are_sanitized(self) -> None:
        def timed_out(_public_ref: str) -> dict[str, object]:
            raise TimeoutError("secret timeout body")

        def failed(_public_ref: str) -> dict[str, object]:
            raise RuntimeError("secret provider body")

        cases = (
            (
                timed_out,
                "ai_on_demand_timeout",
                "⌛ AI 解读请求超时，请稍后再试。",
            ),
            (
                failed,
                "ai_on_demand_security_failed",
                "🔒 AI 解读安全校验失败。",
            ),
        )
        for requester, expected_command, expected_text in cases:
            with self.subTest(command=expected_command), TemporaryDirectory() as tmp:
                service = self.service(
                    Path(tmp),
                    session=FakeSession(),
                    ai_on_demand_requester=requester,
                )
                reply = service.handle_update(update(1, AI_DEEP_LINK_COMMAND))

                self.assertEqual(reply.command, expected_command)
                self.assertEqual(reply.text, expected_text)
                self.assertNotIn("secret", reply.text)

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
            ("关闭启动预警", "确认关闭启动预警", "LAUNCH_ALERT_ENABLE"),
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
