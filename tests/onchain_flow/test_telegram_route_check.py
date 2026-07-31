from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import requests
from unittest.mock import patch

from paopao_radar.onchain_flow.telegram_route_check import (
    TelegramRouteChecker,
    save_route_check,
)
from paopao_radar.onchain_flow.cli import main as cli_main

from .support import make_settings


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, object]):
        self.status_code = status
        self.payload = payload

    def json(self) -> dict[str, object]:
        return self.payload


class FakeHttp:
    def __init__(self, responses: list[object]):
        self.responses = list(responses)
        self.operations: list[str] = []

    def post(self, url: str, **_kwargs: object) -> FakeResponse:
        self.operations.append(url.rsplit("/", 1)[-1])
        response = self.responses.pop(0)
        if isinstance(response, requests.exceptions.RequestException):
            raise response
        return response  # type: ignore[return-value]


def ok(result: object) -> FakeResponse:
    return FakeResponse(200, {"ok": True, "result": result})


class TelegramRouteCheckTests(unittest.TestCase):
    def settings(self, root: Path):
        return make_settings(
            root,
            tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            tg_chat_id="-1001234567890",
            tg_onchain_flow_topic_id="22",
        )

    def test_success_uses_only_four_non_persistent_operations(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = FakeHttp([
                ok({"id": 77, "username": "must-not-be-saved"}),
                ok({"id": -1001234567890, "type": "supergroup", "is_forum": True, "title": "private"}),
                ok({"status": "administrator", "can_manage_topics": True, "can_pin_messages": True}),
                ok(True),
            ])
            result = TelegramRouteChecker(
                self.settings(root), http_client=http
            ).check()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["token_auth"], "ok")
            self.assertEqual(result["chat_access"], "ok")
            self.assertEqual(result["topic_route"], "ok")
            self.assertEqual(result["telegram_http_calls"], 4)
            self.assertEqual(result["persistent_messages"], 0)
            self.assertEqual(http.operations, [
                "getMe", "getChat", "getChatMember", "sendChatAction"
            ])
            forbidden = {
                "sendMessage", "createForumTopic", "getUpdates",
                "setWebhook", "deleteWebhook", "pinChatMessage", "deleteMessage",
            }
            self.assertFalse(forbidden & set(http.operations))

            path = save_route_check(self.settings(root), result)
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("must-not-be-saved", saved)
            self.assertNotIn("-1001234567890", saved)
            self.assertNotIn("\"22\"", saved)
            self.assertNotIn("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ", saved)

    def test_permission_failure_stops_before_chat_action(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = FakeHttp([
                ok({"id": 77}),
                ok({"type": "supergroup", "is_forum": True}),
                ok({"status": "restricted", "can_send_messages": False}),
            ])
            result = TelegramRouteChecker(
                self.settings(root), http_client=http
            ).check()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error"], "telegram_send_permission_denied")
            self.assertEqual(result["telegram_http_calls"], 3)
            self.assertNotIn("sendChatAction", http.operations)

    def test_invalid_topic_is_precisely_classified(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = FakeHttp([
                ok({"id": 77}),
                ok({"type": "supergroup", "is_forum": True}),
                ok({"status": "member"}),
                FakeResponse(400, {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: message thread not found PRIVATE",
                }),
            ])
            result = TelegramRouteChecker(
                self.settings(root), http_client=http
            ).check()
            self.assertEqual(result["error"], "telegram_topic_not_found")
            self.assertEqual(result["persistent_messages"], 0)
            self.assertNotIn("PRIVATE", json.dumps(result))

    def test_auth_and_network_failures_are_safe(self) -> None:
        cases = (
            (
                FakeResponse(401, {"ok": False, "error_code": 401, "description": "secret"}),
                "telegram_auth_failed",
            ),
            (requests.exceptions.Timeout("secret"), "telegram_timeout"),
        )
        for response, expected in cases:
            with self.subTest(expected=expected), TemporaryDirectory() as tmp:
                checker = TelegramRouteChecker(
                    self.settings(Path(tmp)),
                    http_client=FakeHttp([response]),
                )
                result = checker.check()
                self.assertEqual(result["error"], expected)
                self.assertEqual(result["telegram_http_calls"], 1)
                self.assertEqual(result["persistent_messages"], 0)
                self.assertNotIn("secret", json.dumps(result))

    def test_unconfigured_route_makes_zero_http_calls(self) -> None:
        with TemporaryDirectory() as tmp:
            http = FakeHttp([])
            settings = make_settings(Path(tmp))
            result = TelegramRouteChecker(settings, http_client=http).check()
            self.assertEqual(result["error"], "telegram_not_configured")
            self.assertEqual(result["telegram_http_calls"], 0)
            self.assertEqual(http.operations, [])

    def test_cli_requires_explicit_network_gate(self) -> None:
        with TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                code = cli_main(
                    ["telegram-route-check"],
                    settings=self.settings(Path(tmp)),
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"], "allow_network_required")
            self.assertEqual(payload["telegram_http_calls"], 0)
            self.assertEqual(payload["persistent_messages"], 0)

    def test_bootstrap_reuses_valid_topic_without_creating_one(self) -> None:
        with TemporaryDirectory() as tmp:
            http = FakeHttp([
                ok({"id": 77}),
                ok({"type": "supergroup", "is_forum": True}),
                ok({"status": "administrator", "can_manage_topics": True}),
                ok(True),
            ])
            result = TelegramRouteChecker(
                self.settings(Path(tmp)),
                http_client=http,
            ).bootstrap_topic()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["topic_action"], "reused")
            self.assertEqual(result["topics_created"], 0)
            self.assertEqual(result["persistent_messages"], 0)
            self.assertNotIn("createForumTopic", http.operations)

    def test_bootstrap_creates_and_atomically_configures_missing_topic(
        self,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = FakeHttp([
                ok({"id": 77}),
                ok({"type": "supergroup", "is_forum": True}),
                ok({"status": "administrator", "can_manage_topics": True}),
                ok({"message_thread_id": 42}),
            ])
            settings = make_settings(
                root,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_onchain_flow_topic_id="",
            )
            result = TelegramRouteChecker(
                settings,
                http_client=http,
            ).bootstrap_topic()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["topic_action"], "created")
            self.assertEqual(result["topics_created"], 1)
            self.assertEqual(result["persistent_messages"], 0)
            self.assertEqual(http.operations, [
                "getMe", "getChat", "getChatMember", "createForumTopic"
            ])
            for forbidden in (
                "sendMessage",
                "sendPhoto",
                "pinChatMessage",
                "deleteMessage",
                "getUpdates",
                "setWebhook",
                "deleteWebhook",
            ):
                self.assertNotIn(forbidden, http.operations)
            env_text = (root / ".env.onchain").read_text(encoding="utf-8")
            self.assertIn("TG_ONCHAIN_FLOW_TOPIC_ID=42", env_text)
            public = json.dumps(result, ensure_ascii=False)
            self.assertNotIn("-1001234567890", public)
            self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", public)
            self.assertNotIn('"42"', public)

    def test_bootstrap_reuses_saved_main_gateway_route(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(
                root,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_onchain_flow_topic_id="",
            )
            settings.tg_topic_routes_path.parent.mkdir(
                parents=True,
                exist_ok=True,
            )
            settings.tg_topic_routes_path.write_text(
                json.dumps({
                    "routes": {
                        "TG_ONCHAIN_FLOW_ALERT": {"topic_id": 44},
                    },
                }),
                encoding="utf-8",
            )
            http = FakeHttp([
                ok({"id": 77}),
                ok({"type": "supergroup", "is_forum": True}),
                ok({"status": "member"}),
                ok(True),
            ])
            result = TelegramRouteChecker(
                settings,
                http_client=http,
            ).bootstrap_topic()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["topic_action"], "reused")
            self.assertNotIn("createForumTopic", http.operations)
            env_text = (root / ".env.onchain").read_text(encoding="utf-8")
            self.assertIn("TG_ONCHAIN_FLOW_TOPIC_ID=44", env_text)

    def test_bootstrap_repairs_stale_topic_only_with_manage_permission(
        self,
    ) -> None:
        stale = FakeResponse(400, {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: message thread not found PRIVATE",
        })
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = FakeHttp([
                ok({"id": 77}),
                ok({"type": "supergroup", "is_forum": True}),
                ok({"status": "administrator", "can_manage_topics": True}),
                stale,
                ok({"message_thread_id": 43}),
            ])
            result = TelegramRouteChecker(
                self.settings(root),
                http_client=http,
            ).bootstrap_topic()
            self.assertEqual(result["status"], "ok")
            self.assertEqual(result["topic_action"], "created")
            self.assertEqual(http.operations[-1], "createForumTopic")
            self.assertNotIn("PRIVATE", json.dumps(result))

        with TemporaryDirectory() as tmp:
            http = FakeHttp([
                ok({"id": 77}),
                ok({"type": "supergroup", "is_forum": True}),
                ok({"status": "member"}),
                stale,
            ])
            result = TelegramRouteChecker(
                self.settings(Path(tmp)),
                http_client=http,
            ).bootstrap_topic()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(
                result["error"],
                "telegram_manage_topics_permission_required",
            )
            self.assertNotIn("createForumTopic", http.operations)

    def test_bootstrap_cli_requires_explicit_network_gate(self) -> None:
        with TemporaryDirectory() as tmp:
            output = StringIO()
            with redirect_stdout(output):
                code = cli_main(
                    ["telegram-topic", "bootstrap"],
                    settings=self.settings(Path(tmp)),
                )
            payload = json.loads(output.getvalue())
            self.assertEqual(code, 1)
            self.assertEqual(payload["error"], "allow_network_required")
            self.assertEqual(payload["telegram_http_calls"], 0)

    def test_bootstrap_cli_persists_only_safe_result(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            safe = TelegramRouteChecker._empty_result()
            safe.update({
                "status": "ok",
                "topic_action": "reused",
                "topic_configured": True,
            })
            output = StringIO()
            with patch.object(
                TelegramRouteChecker,
                "bootstrap_topic",
                return_value=safe,
            ), redirect_stdout(output):
                code = cli_main(
                    ["telegram-topic", "bootstrap", "--allow-network"],
                    settings=self.settings(root),
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue())["status"], "ok")


if __name__ == "__main__":
    unittest.main()
