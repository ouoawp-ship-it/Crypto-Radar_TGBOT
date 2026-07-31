from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

import requests

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


if __name__ == "__main__":
    unittest.main()
