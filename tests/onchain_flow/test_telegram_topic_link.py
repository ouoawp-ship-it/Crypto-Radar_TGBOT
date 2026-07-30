from __future__ import annotations

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paopao_radar.onchain_flow.cli import main
from paopao_radar.onchain_flow.telegram_topic_link import (
    TelegramTopicLinkError,
    parse_telegram_topic_link,
    validate_telegram_topic_link,
)
from scripts.paopao_config import ConfigManager
from tests.onchain_flow.support import make_settings


ROOT = Path(__file__).resolve().parents[2]
PRIVATE_CHAT_ID = "-1001234567890"
PRIVATE_PATH_LINK = "https://t.me/c/1234567890/42/99"
PRIVATE_THREAD_LINK = (
    "https://telegram.me/c/1234567890/99?single&thread=42"
)
PUBLIC_PATH_LINK = "https://t.me/example_group/42/99"
PUBLIC_THREAD_LINK = "https://t.me/example_group/99?thread=42"


class TelegramTopicLinkParserTests(unittest.TestCase):
    def test_private_path_and_thread_links_parse(self) -> None:
        path = parse_telegram_topic_link(PRIVATE_PATH_LINK)
        thread = parse_telegram_topic_link(PRIVATE_THREAD_LINK)
        for parsed in (path, thread):
            self.assertEqual(parsed.link_type, "private")
            self.assertEqual(parsed.channel_id, "1234567890")
            self.assertEqual(parsed.topic_id, 42)
            self.assertEqual(parsed.message_id, 99)

    def test_public_links_parse_but_require_offline_chat_proof(self) -> None:
        for link in (PUBLIC_PATH_LINK, PUBLIC_THREAD_LINK):
            with self.subTest(link=link):
                parsed = parse_telegram_topic_link(link)
                self.assertEqual(parsed.link_type, "public")
                with self.assertRaises(TelegramTopicLinkError) as caught:
                    validate_telegram_topic_link(
                        link,
                        configured_chat_id=PRIVATE_CHAT_ID,
                    )
                self.assertEqual(
                    caught.exception.code,
                    "topic_link_public_chat_unverified",
                )

    def test_private_chat_must_match_configured_supergroup(self) -> None:
        parsed = validate_telegram_topic_link(
            PRIVATE_PATH_LINK,
            configured_chat_id=PRIVATE_CHAT_ID,
        )
        self.assertEqual(parsed.topic_id, 42)
        with self.assertRaises(TelegramTopicLinkError) as caught:
            validate_telegram_topic_link(
                PRIVATE_PATH_LINK,
                configured_chat_id="-1009876543210",
            )
        self.assertEqual(caught.exception.code, "topic_link_chat_mismatch")

    def test_unsafe_domains_schemes_credentials_and_fragments_fail(self) -> None:
        cases = (
            (
                "http://t.me/c/1234567890/42/99",
                "topic_link_invalid",
            ),
            (
                "https://example.com/c/1234567890/42/99",
                "topic_link_domain_invalid",
            ),
            (
                "https://user:pass@t.me/c/1234567890/42/99",
                "topic_link_domain_invalid",
            ),
            (
                "https://t.me/c/1234567890/42/99#fragment",
                "topic_link_invalid",
            ),
            (
                "https://t.me/example_bot?start=payload",
                "topic_link_not_forum_message",
            ),
        )
        for link, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(TelegramTopicLinkError) as caught:
                    parse_telegram_topic_link(link)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn(link, str(caught.exception))

    def test_invalid_topic_and_message_numbers_fail_closed(self) -> None:
        cases = (
            (
                "https://t.me/c/1234567890/1/99",
                "topic_link_topic_invalid",
            ),
            (
                "https://t.me/c/1234567890/topic/99",
                "topic_link_topic_invalid",
            ),
            (
                "https://t.me/c/1234567890/42/message",
                "topic_link_not_forum_message",
            ),
            (
                "https://t.me/c/1234567890/99?thread=topic",
                "topic_link_topic_invalid",
            ),
        )
        for link, code in cases:
            with self.subTest(link=link):
                with self.assertRaises(TelegramTopicLinkError) as caught:
                    parse_telegram_topic_link(link)
                self.assertEqual(caught.exception.code, code)

    def test_duplicate_or_conflicting_thread_is_ambiguous(self) -> None:
        for link in (
            "https://t.me/c/1234567890/99?thread=42&thread=42",
            "https://t.me/c/1234567890/42/99?thread=43",
        ):
            with self.subTest(link=link):
                with self.assertRaises(TelegramTopicLinkError) as caught:
                    parse_telegram_topic_link(link)
                self.assertEqual(caught.exception.code, "topic_link_ambiguous")

    def test_path_and_thread_may_repeat_the_same_topic(self) -> None:
        parsed = parse_telegram_topic_link(
            "https://t.me/c/1234567890/42/99?thread=42&single"
        )
        self.assertEqual(parsed.topic_id, 42)


class TelegramTopicLinkCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".env.oi").write_text(
            "TG_BOT_TOKEN=123456:SafeToken_Value\n"
            f"TG_CHAT_ID={PRIVATE_CHAT_ID}\n",
            encoding="utf-8",
        )
        (self.root / ".env.onchain").write_text(
            "OAR_WATCH_DELIVERY_MODE=observe\n"
            "ONCHAIN_REAL_SEND=false\n",
            encoding="utf-8",
        )
        self.settings = replace(
            make_settings(self.root),
            tg_chat_id=PRIVATE_CHAT_ID,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(
        self,
        action: str,
        link: str,
    ) -> tuple[int, str]:
        output = StringIO()
        with patch("sys.stdin", StringIO(link + "\n")):
            with redirect_stdout(output):
                code = main(
                    ["telegram-topic-link", action, "--stdin"],
                    settings=self.settings,
                )
        return code, output.getvalue().strip()

    def test_check_is_offline_and_redacted(self) -> None:
        code, raw = self.run_cli("check", PRIVATE_PATH_LINK)
        payload = json.loads(raw)
        self.assertEqual(code, 0)
        self.assertEqual(
            payload,
            {
                "status": "ok",
                "link_type": "private",
                "chat_match": True,
                "topic_valid": True,
            },
        )
        self.assertNotIn("42", raw)
        self.assertNotIn(PRIVATE_PATH_LINK, raw)

    def test_bind_writes_only_topic_id_and_redacts_output(self) -> None:
        code, raw = self.run_cli("bind", PRIVATE_PATH_LINK)
        self.assertEqual(code, 0)
        self.assertEqual(raw, "TG_ONCHAIN_FLOW_TOPIC_ID=configured")
        values = ConfigManager(self.root).status()
        self.assertEqual(values["TG_ONCHAIN_FLOW_TOPIC_ID"], "configured")
        env_text = (self.root / ".env.onchain").read_text(encoding="utf-8")
        self.assertIn("TG_ONCHAIN_FLOW_TOPIC_ID=42", env_text)
        self.assertNotIn(PRIVATE_PATH_LINK, env_text)
        backups = list(self.root.glob(".env.onchain.bak.*"))
        self.assertEqual(len(backups), 1)
        self.assertNotIn(
            PRIVATE_PATH_LINK,
            backups[0].read_text(encoding="utf-8"),
        )
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(
                    PRIVATE_PATH_LINK.encode(),
                    path.read_bytes(),
                )
        if os.name != "nt":
            self.assertEqual(
                (self.root / ".env.onchain").stat().st_mode & 0o777,
                0o600,
            )

    def test_link_is_never_accepted_from_argv(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                ["telegram-topic-link", "check", PRIVATE_PATH_LINK],
                settings=self.settings,
            )
        raw = output.getvalue()
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(raw)["error"], "topic_link_invalid")
        self.assertNotIn(PRIVATE_PATH_LINK, raw)

    def test_url_option_is_rejected_without_echoing_value(self) -> None:
        output = StringIO()
        with redirect_stdout(output):
            code = main(
                [
                    "telegram-topic-link",
                    "check",
                    "--url",
                    PRIVATE_PATH_LINK,
                ],
                settings=self.settings,
            )
        raw = output.getvalue()
        self.assertEqual(code, 1)
        self.assertNotIn(PRIVATE_PATH_LINK, raw)

    def test_public_link_failure_does_not_persist_or_echo_link(self) -> None:
        before = (self.root / ".env.onchain").read_bytes()
        code, raw = self.run_cli("bind", PUBLIC_PATH_LINK)
        self.assertEqual(code, 1)
        self.assertEqual(
            json.loads(raw)["error"],
            "topic_link_public_chat_unverified",
        )
        self.assertNotIn(PUBLIC_PATH_LINK, raw)
        self.assertEqual(
            (self.root / ".env.onchain").read_bytes(),
            before,
        )
        self.assertEqual(list(self.root.glob(".env.onchain.bak.*")), [])

    def test_bind_does_not_construct_telegram_or_network_clients(self) -> None:
        with patch(
            "paopao_radar.onchain_flow.cli.ReportNotifier",
            side_effect=AssertionError("Telegram notifier created"),
        ), patch(
            "paopao_radar.onchain_flow.cli.BaseOnchainRuntime",
            side_effect=AssertionError("network runtime created"),
        ):
            code, _ = self.run_cli("bind", PRIVATE_THREAD_LINK)
        self.assertEqual(code, 0)

    def test_parser_has_no_telegram_or_update_network_surface(self) -> None:
        source = (
            ROOT
            / "paopao_radar"
            / "onchain_flow"
            / "telegram_topic_link.py"
        ).read_text(encoding="utf-8")
        for forbidden in (
            "getUpdates",
            "setWebhook",
            "deleteWebhook",
            "createForumTopic",
            "sendMessage",
            "requests.",
            "TelegramGateway",
        ):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
