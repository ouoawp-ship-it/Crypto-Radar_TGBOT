from __future__ import annotations

import json
import socket
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import requests

from config import Settings
from shared.storage import JsonStore
from shared.telegram import (
    TelegramGateway,
    chunk_text,
    classify_telegram_network_error,
    classify_telegram_response,
    telegram_chunk_diagnostics,
)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        description: str = "",
        error_code: int | None = None,
        retry_after: int | None = None,
        message_id: int | None = None,
    ):
        self.status_code = status_code
        self._description = description
        self._error_code = error_code
        self._retry_after = retry_after
        self._message_id = message_id

    def json(self) -> dict[str, object]:
        if self.status_code == 200:
            result: dict[str, object] = {}
            if self._message_id is not None:
                result["message_id"] = self._message_id
            return {"ok": True, "result": result}
        payload: dict[str, object] = {
            "ok": False,
            "error_code": self._error_code or self.status_code,
            "description": self._description,
        }
        if self._retry_after is not None:
            payload["parameters"] = {"retry_after": self._retry_after}
        return payload


class TelegramSafeClassificationTests(unittest.TestCase):
    def test_http_status_and_bad_request_classes(self) -> None:
        cases = (
            (401, "", "telegram_auth_failed"),
            (403, "", "telegram_forbidden"),
            (404, "", "telegram_endpoint_not_found"),
            (429, "", "telegram_rate_limited"),
            (500, "", "telegram_provider_unavailable"),
            (400, "Bad Request: chat not found", "telegram_chat_not_found"),
            (400, "Bad Request: bot is not a member", "telegram_bot_not_member"),
            (400, "Bad Request: not enough rights to send text messages", "telegram_send_permission_denied"),
            (400, "Bad Request: message thread not found", "telegram_topic_not_found"),
            (400, "Bad Request: TOPIC_CLOSED", "telegram_topic_closed"),
            (400, "Bad Request: can't parse entities", "telegram_parse_error"),
            (400, "Bad Request: message is too long", "telegram_message_too_long"),
            (400, "Bad Request: reply message not found", "telegram_reply_target_not_found"),
            (400, "PRIVATE PROVIDER DETAIL", "telegram_bad_request"),
        )
        for status, description, expected in cases:
            with self.subTest(expected=expected):
                actual, code, _retry = classify_telegram_response(
                    FakeResponse(
                        status,
                        description=description,
                        error_code=status,
                    )
                )
                self.assertEqual(actual, expected)
                self.assertEqual(code, status)
                if description:
                    self.assertNotIn(description, actual)

    def test_rate_limit_keeps_only_safe_retry_after(self) -> None:
        error_class, error_code, retry_after = classify_telegram_response(
            FakeResponse(
                429,
                description="PRIVATE RATE LIMIT DETAIL",
                error_code=429,
                retry_after=7,
            )
        )
        self.assertEqual(error_class, "telegram_rate_limited")
        self.assertEqual(error_code, 429)
        self.assertEqual(retry_after, 7)

    def test_network_error_classes(self) -> None:
        dns = requests.exceptions.ConnectionError(socket.gaierror(-2, "secret-host"))
        cases = (
            (requests.exceptions.Timeout("private"), "telegram_timeout"),
            (requests.exceptions.SSLError("private"), "telegram_tls_failed"),
            (dns, "telegram_dns_failed"),
            (requests.exceptions.ConnectionError("private"), "telegram_connection_failed"),
        )
        for exc, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(classify_telegram_network_error(exc), expected)

    def test_long_single_line_is_hard_split_without_empty_chunks(self) -> None:
        text = "链" * 25
        chunks = chunk_text(text, 10)
        self.assertEqual([len(item) for item in chunks], [10, 10, 5])
        self.assertTrue(all(item for item in chunks))
        self.assertTrue(all(len(item) <= 10 for item in chunks))
        diagnostics = telegram_chunk_diagnostics(text, 10)
        self.assertEqual(diagnostics["source_text_chars"], 25)
        self.assertEqual(diagnostics["max_source_line_chars"], 25)
        self.assertEqual(diagnostics["chunk_count"], 3)
        self.assertEqual(diagnostics["max_chunk_chars"], 10)

    def test_complete_html_link_line_moves_to_next_chunk(self) -> None:
        prefix = "x" * 70
        link = '<a href="https://example.com"><b>BTC</b></a> · TV'

        chunks = chunk_text(f"{prefix}\n{link}", 80)

        self.assertEqual(chunks, [prefix, link])
        self.assertTrue(all(len(item) <= 80 for item in chunks))
        self.assertEqual(chunks[1].count("<a "), chunks[1].count("</a>"))

    def test_multiple_html_link_lines_remain_parseable_across_chunks(self) -> None:
        links = [
            f'<a href="https://example.com/{index}"><b>COIN{index}</b></a> · '
            f'<a href="https://example.com/tv/{index}"><b>TV</b></a>'
            for index in range(6)
        ]

        chunks = chunk_text("\n".join(links), 180)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(item) <= 180 for item in chunks))
        for chunk in chunks:
            self.assertEqual(chunk.count("<a "), chunk.count("</a>"))
            self.assertFalse(chunk.endswith("<"))


class TelegramSafeFallbackTests(unittest.TestCase):
    def make_gateway(self, root: Path, **overrides: object) -> TelegramGateway:
        values = {
            "data_dir": root,
            "tg_push_history_path": root / "history.json",
            "tg_outbox_path": root / "outbox.json",
            "tg_topic_routes_path": root / "routes.json",
            "tg_bot_token": "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
            "tg_chat_id": "-1001234567890",
            "tg_flow_radar_topic_id": "22",
            "tg_use_topic": True,
            "tg_push_retry": 1,
            "tg_default_cooldown_sec": 0,
        }
        values.update(overrides)
        return TelegramGateway(Settings(**values), JsonStore(root))

    def test_only_parse_error_uses_plain_text_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway = self.make_gateway(Path(tmp))
            responses = [
                FakeResponse(400, description="can't parse entities"),
                FakeResponse(200, message_id=12),
            ]
            with patch("shared.telegram.requests.post", side_effect=responses) as request:
                ok, ids = gateway._send_real_message_ids(
                    "<b>message</b>", parse_mode="HTML", topic_id="22"
                )
            self.assertTrue(ok)
            self.assertEqual(ids, [12])
            self.assertEqual(request.call_count, 2)
            self.assertNotIn("parse_mode", request.call_args_list[1].kwargs["json"])
            diagnostics = gateway._last_delivery_diagnostics
            self.assertIsNotNone(diagnostics)
            self.assertTrue(diagnostics.parse_fallback_used)  # type: ignore[union-attr]
            self.assertEqual(diagnostics.http_attempts, 2)  # type: ignore[union-attr]

    def test_only_reply_error_uses_no_reply_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway = self.make_gateway(Path(tmp))
            responses = [
                FakeResponse(400, description="reply message not found"),
                FakeResponse(200, message_id=13),
            ]
            with patch("shared.telegram.requests.post", side_effect=responses) as request:
                ok, ids = gateway._send_real_message_ids(
                    "message",
                    parse_mode="HTML",
                    topic_id="22",
                    reply_to_message_id=9,
                )
            self.assertTrue(ok)
            self.assertEqual(ids, [13])
            self.assertNotIn("reply_to_message_id", request.call_args_list[1].kwargs["json"])
            diagnostics = gateway._last_delivery_diagnostics
            self.assertTrue(diagnostics.reply_fallback_used)  # type: ignore[union-attr]
            self.assertFalse(diagnostics.parse_fallback_used)  # type: ignore[union-attr]

    def test_topic_error_has_no_fallback_and_no_description_leak(self) -> None:
        secret = "Bad Request: message thread not found PRIVATE_DESCRIPTION"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = self.make_gateway(root)
            stderr = StringIO()
            with (
                patch(
                    "shared.telegram.requests.post",
                    return_value=FakeResponse(400, description=secret),
                ) as request,
                redirect_stderr(stderr),
            ):
                result = gateway.send(
                    "message",
                    "TG_FLOW_RADAR",
                    "safe-diagnostic",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )
            self.assertEqual(result.status, "failed")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(
                result.diagnostics.telegram_error_class,  # type: ignore[union-attr]
                "telegram_topic_not_found",
            )
            serialized = json.dumps(result.diagnostics.public_dict())  # type: ignore[union-attr]
            serialized += (root / "history.json").read_text(encoding="utf-8")
            serialized += (root / "outbox.json").read_text(encoding="utf-8")
            serialized += stderr.getvalue()
            self.assertNotIn(secret, serialized)
            self.assertNotIn("PRIVATE_DESCRIPTION", serialized)

    def test_rate_limit_retry_counts_both_http_attempts(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway = self.make_gateway(Path(tmp), tg_push_retry=2)
            responses = [
                FakeResponse(429, description="private", retry_after=1),
                FakeResponse(200, message_id=14),
            ]
            with (
                patch("shared.telegram.requests.post", side_effect=responses),
                patch("shared.telegram.time.sleep"),
            ):
                ok, _ids = gateway._send_real_message_ids(
                    "message", parse_mode="HTML", topic_id="22"
                )
            self.assertTrue(ok)
            diagnostics = gateway._last_delivery_diagnostics
            self.assertEqual(diagnostics.http_attempts, 2)  # type: ignore[union-attr]
            self.assertEqual(diagnostics.completed_chunks, 1)  # type: ignore[union-attr]

    def test_read_timeout_is_uncertain_not_retried_and_quarantined(self) -> None:
        secret = "PRIVATE_TIMEOUT_DETAIL"
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = self.make_gateway(root, tg_push_retry=3)
            responses = [
                requests.exceptions.ReadTimeout(secret),
                FakeResponse(200, message_id=99),
            ]
            with patch(
                "shared.telegram.requests.post",
                side_effect=responses,
            ) as request:
                first = gateway.send(
                    "message",
                    "TG_FLOW_RADAR",
                    "uncertain-delivery",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )
                second = gateway.send(
                    "message",
                    "TG_FLOW_RADAR",
                    "uncertain-delivery",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                )

            self.assertEqual(request.call_count, 1)
            self.assertEqual(first.status, "failed")
            self.assertEqual(first.reason, "telegram_delivery_uncertain")
            self.assertEqual(
                first.diagnostics.telegram_error_class,  # type: ignore[union-attr]
                "telegram_delivery_uncertain",
            )
            self.assertEqual(
                first.diagnostics.network_error_class,  # type: ignore[union-attr]
                "telegram_timeout",
            )
            self.assertEqual(first.diagnostics.http_attempts, 1)  # type: ignore[union-attr]
            self.assertEqual(second.status, "skipped")
            self.assertEqual(second.reason, "delivery_quarantine")
            outbox = JsonStore(root).load(root / "outbox.json", [])
            self.assertEqual(outbox[-1]["status"], "uncertain")
            self.assertEqual(
                outbox[-1]["telegram_error_class"],
                "telegram_delivery_uncertain",
            )
            history = JsonStore(root).load(root / "history.json", [])
            self.assertEqual(
                history[-2]["telegram_error_class"],
                "telegram_delivery_uncertain",
            )
            serialized = json.dumps(first.diagnostics.public_dict())  # type: ignore[union-attr]
            serialized += (root / "history.json").read_text(encoding="utf-8")
            serialized += (root / "outbox.json").read_text(encoding="utf-8")
            self.assertNotIn(secret, serialized)

    def test_connection_drop_is_uncertain_and_not_retried(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway = self.make_gateway(Path(tmp), tg_push_retry=3)
            with patch(
                "shared.telegram.requests.post",
                side_effect=requests.exceptions.ConnectionError("private"),
            ) as request:
                ok, ids = gateway._send_real_message_ids(
                    "message",
                    parse_mode="HTML",
                    topic_id="22",
                )

            self.assertFalse(ok)
            self.assertEqual(ids, [])
            self.assertEqual(request.call_count, 1)
            diagnostics = gateway._last_delivery_diagnostics
            self.assertEqual(
                diagnostics.telegram_error_class,  # type: ignore[union-attr]
                "telegram_delivery_uncertain",
            )
            self.assertEqual(diagnostics.http_attempts, 1)  # type: ignore[union-attr]

    def test_connect_timeout_keeps_existing_bounded_retry(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway = self.make_gateway(Path(tmp), tg_push_retry=2)
            responses = [
                requests.exceptions.ConnectTimeout("private"),
                FakeResponse(200, message_id=17),
            ]
            with (
                patch("shared.telegram.requests.post", side_effect=responses) as request,
                patch("shared.telegram.time.sleep"),
            ):
                ok, ids = gateway._send_real_message_ids(
                    "message",
                    parse_mode="HTML",
                    topic_id="22",
                )

            self.assertTrue(ok)
            self.assertEqual(ids, [17])
            self.assertEqual(request.call_count, 2)
            diagnostics = gateway._last_delivery_diagnostics
            self.assertEqual(diagnostics.http_attempts, 2)  # type: ignore[union-attr]
            self.assertEqual(
                diagnostics.telegram_error_class,  # type: ignore[union-attr]
                "telegram_ok",
            )

    def test_photo_read_timeout_is_uncertain_and_not_retried(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway = self.make_gateway(Path(tmp), tg_push_retry=3)
            with patch(
                "shared.telegram.requests.post",
                side_effect=requests.exceptions.ReadTimeout("private"),
            ) as request:
                ok, ids = gateway._send_real_photo_bytes(
                    b"\x89PNG\r\n\x1a\nchart",
                    caption="message",
                    parse_mode="HTML",
                    topic_id="22",
                )

            self.assertFalse(ok)
            self.assertEqual(ids, [])
            self.assertEqual(request.call_count, 1)
            diagnostics = gateway._last_delivery_diagnostics
            self.assertEqual(
                diagnostics.telegram_error_class,  # type: ignore[union-attr]
                "telegram_delivery_uncertain",
            )
            self.assertEqual(diagnostics.http_attempts, 1)  # type: ignore[union-attr]

    def test_partial_delivery_persists_safe_audit_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            gateway = self.make_gateway(root, tg_push_split_limit=5)
            responses = [
                FakeResponse(200, message_id=15),
                FakeResponse(400, description="message thread not found"),
                FakeResponse(200, message_id=16),
            ]
            with patch("shared.telegram.requests.post", side_effect=responses):
                result = gateway.send(
                    "123456789",
                    "TG_FLOW_RADAR",
                    "partial-diagnostic",
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=0,
                    parse_mode="HTML",
                )
            self.assertEqual(result.status, "partial")
            history = JsonStore(root).load(root / "history.json", [])[-1]
            outbox = JsonStore(root).load(root / "outbox.json", [])[-1]
            for record in (history, outbox):
                self.assertEqual(record["telegram_http_attempts"], 2)
                self.assertEqual(record["telegram_completed_chunks"], 1)
                self.assertEqual(record["telegram_total_chunks"], 2)
                self.assertEqual(record["telegram_error_class"], "telegram_topic_not_found")

    def test_topic_intro_pin_and_delete_logs_never_include_description(self) -> None:
        secret = "PRIVATE_TELEGRAM_DESCRIPTION"
        with TemporaryDirectory() as tmp:
            gateway = self.make_gateway(Path(tmp))
            response = FakeResponse(
                400,
                description=f"Bad Request: chat not found {secret}",
            )
            stderr = StringIO()
            with (
                patch(
                    "shared.telegram.requests.post",
                    return_value=response,
                ),
                redirect_stderr(stderr),
            ):
                self.assertEqual(gateway._create_forum_topic("safe"), "")
                self.assertFalse(gateway._pin_message(1))
                self.assertFalse(gateway._delete_message(1))
            output = stderr.getvalue()
            self.assertNotIn(secret, output)
            self.assertNotIn("Bad Request", output)
            self.assertEqual(output.count("telegram_chat_not_found"), 3)


if __name__ == "__main__":
    unittest.main()
