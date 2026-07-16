from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from paopao_radar.ai_assistant import (
    BotReply,
    QueuedTelegramSender,
    SessionLockRegistry,
    TelegramBotClient,
    alert_created_text,
    build_chat_completion_payload,
    check_and_send_price_alerts,
    clear_ai_settings_cache,
    coinglass_quote_url,
    extract_ai_reply_text,
    handle_callback_query,
    handle_message,
    handle_message_reply,
    infer_telegram_parse_mode,
    is_alert_intent,
    parse_alert_request,
    price_quote_links_line,
    price_quote_table_block,
    price_text_from_quotes,
    price_reply,
    process_ai_update,
    processing_notice_for_message,
    load_ai_settings_cached,
    telegram_plain_text,
    user_facing_error,
)
from paopao_radar.config import Settings
from paopao_radar.price_alerts import AlertMarketQuote, PriceAlertStore


class AiAssistantTests(unittest.TestCase):
    def test_load_ai_settings_cached_reuses_value_until_env_signature_changes(self) -> None:
        first = Settings(ai_bot_token="first")
        second = Settings(ai_bot_token="second")
        clear_ai_settings_cache()
        try:
            with patch("paopao_radar.ai_assistant._settings_loader_is_mocked", return_value=False):
                with patch("paopao_radar.ai_assistant._env_file_signature", side_effect=[(1, 100), (1, 100), (2, 100)]):
                    with patch("paopao_radar.ai_assistant.Settings.load", side_effect=[first, second]) as load:
                        self.assertIs(load_ai_settings_cached(), first)
                        self.assertIs(load_ai_settings_cached(), first)
                        self.assertIs(load_ai_settings_cached(), second)

            self.assertEqual(load.call_count, 2)
        finally:
            clear_ai_settings_cache()

    def test_telegram_plain_text_removes_common_markdown(self) -> None:
        cleaned = telegram_plain_text(
            "## 标题\n1. **解释运行状态**：查看 `runtime-status`\n[文档](https://example.com)\n\n\n"
        )

        self.assertEqual(cleaned.splitlines()[0], "标题")
        self.assertIn("解释运行状态：查看 runtime-status", cleaned)
        self.assertIn("文档（https://example.com）", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertNotIn("`", cleaned)

    def test_telegram_bot_send_message_retries_timeout(self) -> None:
        bot = TelegramBotClient("123456:test", timeout_sec=10, send_timeout_sec=20, retry_count=2, retry_delay_sec=0)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ok": True}

        with patch(
            "paopao_radar.ai_assistant.requests.post",
            side_effect=[requests.exceptions.ReadTimeout("Read timed out."), response],
        ) as post:
            ok = bot.send_message(42, "hello")

        self.assertTrue(ok)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.kwargs["timeout"], 20)

    def test_answer_callback_query_uses_short_single_attempt_timeout(self) -> None:
        bot = TelegramBotClient("123456:test", timeout_sec=10, retry_count=3, retry_delay_sec=0)

        with patch(
            "paopao_radar.ai_assistant.requests.post",
            side_effect=requests.exceptions.ReadTimeout("Read timed out."),
        ) as post:
            with self.assertRaises(requests.exceptions.ReadTimeout):
                bot.answer_callback_query("cb-1")

        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.kwargs["timeout"], 3)

    def test_telegram_bot_send_message_with_ids_and_delete_message(self) -> None:
        bot = TelegramBotClient("123456:test", timeout_sec=10, send_timeout_sec=20, retry_count=1)
        send_response = Mock()
        send_response.raise_for_status.return_value = None
        send_response.json.return_value = {"ok": True, "result": {"message_id": 123}}
        delete_response = Mock()
        delete_response.raise_for_status.return_value = None
        delete_response.json.return_value = {"ok": True}

        with patch("paopao_radar.ai_assistant.requests.post", side_effect=[send_response, delete_response]) as post:
            message_ids = bot.send_message_with_ids(42, "hello")
            deleted = bot.delete_message(42, 123)

        self.assertEqual(message_ids, [123])
        self.assertTrue(deleted)
        self.assertTrue(post.call_args_list[0].args[0].endswith("/sendMessage"))
        self.assertTrue(post.call_args_list[1].args[0].endswith("/deleteMessage"))
        self.assertEqual(post.call_args_list[1].kwargs["json"], {"chat_id": 42, "message_id": 123})

    def test_telegram_bot_send_message_preserves_html_price_links(self) -> None:
        bot = TelegramBotClient("123456:test", timeout_sec=10, retry_count=1)
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"ok": True}

        text = 'BTCUSDT 多交易所价格\n\n合约：\n<a href="https://www.coinglass.com/tv/zh/Binance_BTCUSDT"><b>Binance</b></a> <code>BTCUSDT</code> <code>$62,184.00</code>'
        with patch("paopao_radar.ai_assistant.requests.post", return_value=response) as post:
            ok = bot.send_message(42, text)

        self.assertTrue(ok)
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["parse_mode"], "HTML")
        self.assertIn('<a href="https://www.coinglass.com/tv/zh/Binance_BTCUSDT"><b>Binance</b></a>', payload["text"])
        self.assertEqual(infer_telegram_parse_mode(text), "HTML")

    def test_queued_sender_deletes_temporary_notice_after_successful_send(self) -> None:
        class FakeBot:
            def __init__(self) -> None:
                self.messages: list[tuple[str | int, str]] = []
                self.deleted: list[tuple[str | int, int]] = []

            def send_message(
                self,
                chat_id: str | int,
                text: str,
                reply_markup: dict | None = None,
                parse_mode: str | None = None,
            ) -> bool:
                self.messages.append((chat_id, text))
                return True

            def delete_message(self, chat_id: str | int, message_id: int) -> bool:
                self.deleted.append((chat_id, message_id))
                return True

        fake_bot = FakeBot()
        sender = QueuedTelegramSender(fake_bot)  # type: ignore[arg-type]
        sender.start()
        try:
            ok = sender.send_message(42, "final reply", delete_after_send=((42, 77),))
            sender._queue.join()
        finally:
            sender.stop()

        self.assertTrue(ok)
        self.assertEqual(fake_bot.messages, [(42, "final reply")])
        self.assertEqual(fake_bot.deleted, [(42, 77)])

    def test_queued_sender_retries_transient_send_failure(self) -> None:
        class FakeBot:
            def __init__(self) -> None:
                self.calls = 0
                self.messages: list[tuple[str | int, str]] = []

            def send_message(
                self,
                chat_id: str | int,
                text: str,
                reply_markup: dict | None = None,
                parse_mode: str | None = None,
            ) -> bool:
                self.calls += 1
                if self.calls == 1:
                    raise requests.exceptions.ReadTimeout("Read timed out.")
                self.messages.append((chat_id, text))
                return True

            def delete_message(self, chat_id: str | int, message_id: int) -> bool:
                return True

        fake_bot = FakeBot()
        sender = QueuedTelegramSender(fake_bot, max_send_attempts=2, retry_delay_sec=0)  # type: ignore[arg-type]
        sender.start()
        try:
            self.assertTrue(sender.send_message(42, "final reply", context="retry_test"))
            sender._queue.join()
        finally:
            sender.stop()

        self.assertEqual(fake_bot.calls, 2)
        self.assertEqual(fake_bot.messages, [(42, "final reply")])

    def test_price_quote_table_block_aligns_all_columns_in_pre_block(self) -> None:
        rows = price_quote_table_block([
            AlertMarketQuote(exchange="binance", market_type="futures", symbol="BTCUSDT", pair="BTCUSDT", price=62178.2),
            AlertMarketQuote(exchange="okx", market_type="futures", symbol="BTCUSDT", pair="BTC-USDT-SWAP", price=62181),
        ])

        self.assertIn("<pre>", rows)
        self.assertIn("交易所   交易对               价格", rows)
        self.assertIn("Binance  BTCUSDT        $62,178.20", rows)
        self.assertIn("OKX      BTC-USDT-SWAP  $62,181.00", rows)
        self.assertNotIn("<a href=", rows)

    def test_price_quote_table_block_normalizes_prefixed_contract_price(self) -> None:
        rows = price_quote_table_block([
            AlertMarketQuote(exchange="binance", market_type="futures", symbol="MOGUSDT", pair="1000000MOGUSDT", price=0.1176),
            AlertMarketQuote(exchange="gate", market_type="futures", symbol="MOGUSDT", pair="MOG_USDT", price=0.00000012),
        ])

        self.assertIn("价格", rows)
        self.assertIn("Binance  1000000MOGUSDT  $0.0000001176", rows)
        self.assertIn("Gate     MOG_USDT          $0.00000012", rows)
        self.assertNotIn("$0.1176", rows)

    def test_price_text_from_quotes_uses_shared_widths_and_exchange_order(self) -> None:
        text = price_text_from_quotes("MOGUSDT", [
            AlertMarketQuote(exchange="gate", market_type="spot", symbol="MOGUSDT", pair="MOG_USDT", price=0.0000001185),
            AlertMarketQuote(exchange="bybit", market_type="futures", symbol="MOGUSDT", pair="1000000MOGUSDT", price=0.11834),
            AlertMarketQuote(exchange="binance", market_type="futures", symbol="MOGUSDT", pair="1000000MOGUSDT", price=0.1184),
            AlertMarketQuote(exchange="bybit", market_type="spot", symbol="MOGUSDT", pair="MOGUSDT", price=0.000000118),
        ])

        self.assertIn("Binance  1000000MOGUSDT   $0.0000001184", text)
        self.assertIn("Bybit    1000000MOGUSDT  $0.00000011834", text)
        self.assertIn("Bybit    MOGUSDT           $0.000000118", text)
        self.assertIn("Gate     MOG_USDT         $0.0000001185", text)
        self.assertLess(text.index("Binance  1000000MOGUSDT"), text.index("Bybit    1000000MOGUSDT"))

    def test_price_quote_links_line_keeps_links_outside_aligned_table(self) -> None:
        links = price_quote_links_line([
            AlertMarketQuote(exchange="binance", market_type="futures", symbol="BTCUSDT", pair="BTCUSDT", price=62178.2),
            AlertMarketQuote(exchange="okx", market_type="futures", symbol="BTCUSDT", pair="BTC-USDT-SWAP", price=62181),
        ])

        self.assertIn('K线：<a href="https://www.coinglass.com/tv/zh/Binance_BTCUSDT"><b>Binance</b></a>', links)
        self.assertIn('<a href="https://www.coinglass.com/tv/zh/OKX_BTC-USDT-SWAP"><b>OKX</b></a>', links)

    def test_coinglass_quote_url_keeps_exchange_pair_format(self) -> None:
        self.assertEqual(
            coinglass_quote_url(AlertMarketQuote(exchange="binance", market_type="futures", symbol="BTCUSDT", pair="BTCUSDT", price=1)),
            "https://www.coinglass.com/tv/zh/Binance_BTCUSDT",
        )
        self.assertEqual(
            coinglass_quote_url(AlertMarketQuote(exchange="okx", market_type="futures", symbol="BTCUSDT", pair="BTC-USDT-SWAP", price=1)),
            "https://www.coinglass.com/tv/zh/OKX_BTC-USDT-SWAP",
        )
        self.assertEqual(
            coinglass_quote_url(AlertMarketQuote(exchange="gate", market_type="spot", symbol="BTCUSDT", pair="BTC_USDT", price=1)),
            "https://www.coinglass.com/tv/zh/SPOT_Gate_BTC_USDT",
        )

    def test_alert_created_text_uses_display_number_and_html_market_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceAlertStore(Path(tmp) / "alerts.db")
            store.create_alert(
                user_id="42",
                chat_id="42",
                username="tester",
                symbol="BTC",
                exchange="binance",
                market_type="futures",
                pair="BTCUSDT",
                direction="above",
                target_price=70000,
            )
            alert = store.create_alert(
                user_id="42",
                chat_id="42",
                username="tester",
                symbol="FIL",
                exchange="bybit",
                market_type="futures",
                pair="FILUSDT",
                direction="above",
                target_price=0.82,
            )

        text = alert_created_text(alert, display_no=1)

        self.assertIn("编号：1", text)
        self.assertIn('交易所：<a href="https://www.coinglass.com/tv/zh/Bybit_FILUSDT"><b>Bybit</b></a>', text)
        self.assertIn("交易对：<code>FILUSDT</code>", text)
        self.assertNotIn(f"编号：{alert.id}", text)

    def test_price_reply_forces_html_links_without_url_buttons(self) -> None:
        settings = Settings()
        quote = AlertMarketQuote(exchange="binance", market_type="futures", symbol="BTCUSDT", pair="BTCUSDT", price=61234.5)

        with patch("paopao_radar.ai_assistant.discover_alert_markets", return_value=[quote]):
            reply = price_reply(settings, "BTC")

        self.assertEqual(reply.parse_mode, "HTML")
        self.assertIsNone(reply.reply_markup)
        self.assertIn("<pre>", reply.text)
        self.assertIn('K线：<a href="https://www.coinglass.com/tv/zh/Binance_BTCUSDT"><b>Binance</b></a>', reply.text)
        self.assertNotIn("[Binance]", reply.text)

    def test_process_ai_update_sends_processing_notice_before_slow_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )

            class FakeBot:
                def __init__(self) -> None:
                    self.notices: list[tuple[str | int, str]] = []

                def send_message_with_ids(
                    self,
                    chat_id: str | int,
                    text: str,
                    reply_markup: dict | None = None,
                    parse_mode: str | None = None,
                ) -> list[int]:
                    self.notices.append((chat_id, text))
                    return [77]

                def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
                    return True

            class FakeSender:
                def __init__(self) -> None:
                    self.messages: list[tuple[str | int, str, str, dict | None, tuple[tuple[str | int, int], ...]]] = []

                def send_message(
                    self,
                    chat_id: str | int,
                    text: str,
                    reply_markup: dict | None = None,
                    *,
                    context: str = "queued",
                    parse_mode: str | None = None,
                    delete_after_send: tuple[tuple[str | int, int], ...] = (),
                ) -> bool:
                    self.messages.append((chat_id, text, context, reply_markup, delete_after_send))
                    return True

            sender = FakeSender()
            fake_bot = FakeBot()
            update = {
                "message": {
                    "text": "BTC 现在多少钱",
                    "from": {"id": 42, "username": "tester"},
                    "chat": {"id": 42, "type": "private"},
                }
            }

            with patch("paopao_radar.ai_assistant.Settings.load", return_value=settings):
                with patch(
                    "paopao_radar.ai_assistant.price_reply",
                    return_value=BotReply(
                        'BTCUSDT 多交易所价格\n<a href="https://www.coinglass.com/tv/zh/Binance_BTCUSDT"><b>Binance</b></a> <code>BTCUSDT $62,184.00</code>',
                    ),
                ):
                    process_ai_update(
                        update,
                        fake_bot,  # type: ignore[arg-type]
                        sender,  # type: ignore[arg-type]
                        bot_username="",
                        bot_user_id="",
                        sessions={},
                        session_locks=SessionLockRegistry(),
                    )

            self.assertEqual(len(fake_bot.notices), 1)
            self.assertIn("正在并发查询五大交易所价格", fake_bot.notices[0][1])
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("BTCUSDT 多交易所价格", sender.messages[0][1])
            self.assertEqual(sender.messages[0][2], "message_reply")
            self.assertIsNone(sender.messages[0][3])
            self.assertEqual(sender.messages[0][4], ((42, 77),))

    def test_process_ai_update_acknowledges_callback_silently_before_loading_settings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )

            class FakeBot:
                def __init__(self) -> None:
                    self.answers: list[tuple[str, str]] = []

                def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
                    self.answers.append((callback_query_id, text))
                    return True

            class FakeSender:
                def __init__(self) -> None:
                    self.messages: list[tuple[str | int, str, dict | None, str | None]] = []

                def send_message(
                    self,
                    chat_id: str | int,
                    text: str,
                    reply_markup: dict | None = None,
                    *,
                    context: str = "queued",
                    parse_mode: str | None = None,
                    delete_after_send: tuple[tuple[str | int, int], ...] = (),
                ) -> bool:
                    self.messages.append((chat_id, text, reply_markup, parse_mode))
                    return True

            fake_bot = FakeBot()
            sender = FakeSender()
            answers_seen_during_settings_load: list[list[tuple[str, str]]] = []

            def load_settings() -> Settings:
                answers_seen_during_settings_load.append(list(fake_bot.answers))
                return settings

            update = {
                "callback_query": {
                    "id": "cb-1",
                    "data": "menu:home",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                }
            }

            with patch("paopao_radar.ai_assistant.Settings.load", side_effect=load_settings):
                process_ai_update(
                    update,
                    fake_bot,  # type: ignore[arg-type]
                    sender,  # type: ignore[arg-type]
                    bot_username="",
                    bot_user_id="",
                    sessions={},
                    session_locks=SessionLockRegistry(),
                )

            self.assertEqual(fake_bot.answers, [("cb-1", "")])
            self.assertEqual(answers_seen_during_settings_load, [[("cb-1", "")]])
            self.assertEqual(len(sender.messages), 1)
            self.assertIn("泡泡 AI 助手", sender.messages[0][1])

    def test_process_ai_update_callback_error_is_user_friendly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=Path(tmp) / "alerts.db",
            )

            class FakeBot:
                def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
                    return True

            class FakeSender:
                def __init__(self) -> None:
                    self.messages: list[tuple[str | int, str]] = []

                def send_message(
                    self,
                    chat_id: str | int,
                    text: str,
                    reply_markup: dict | None = None,
                    *,
                    context: str = "queued",
                    parse_mode: str | None = None,
                    delete_after_send: tuple[tuple[str | int, int], ...] = (),
                ) -> bool:
                    self.messages.append((chat_id, text))
                    return True

            sender = FakeSender()
            update = {
                "callback_query": {
                    "id": "cb-1",
                    "data": "menu:alerts",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                }
            }

            with patch("paopao_radar.ai_assistant.Settings.load", return_value=settings):
                with patch("paopao_radar.ai_assistant.handle_callback_query", side_effect=requests.exceptions.ReadTimeout("Read timed out.")):
                    process_ai_update(
                        update,
                        FakeBot(),  # type: ignore[arg-type]
                        sender,  # type: ignore[arg-type]
                        bot_username="",
                        bot_user_id="",
                        sessions={},
                        session_locks=SessionLockRegistry(),
                    )

            self.assertEqual(len(sender.messages), 1)
            self.assertIn("按钮处理超时", sender.messages[0][1])
            self.assertNotIn("ReadTimeout", sender.messages[0][1])

    def test_user_facing_error_keeps_ai_timeout_chinese(self) -> None:
        message = user_facing_error("AI 分析", RuntimeError("AI 接口响应超时（已等待 90 秒）。请稍后重试。"))

        self.assertIn("AI 分析失败：AI 接口响应超时", message)
        self.assertNotIn("RuntimeError", message)

    def test_deepseek_v4_payload_enables_thinking_mode(self) -> None:
        settings = Settings(ai_model="AI_MODEL=deepseek-v4-pro")

        payload = build_chat_completion_payload(settings, "系统提示", "用户内容")

        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertIs(payload["stream"], False)

    def test_extract_ai_reply_text_accepts_list_content(self) -> None:
        text = extract_ai_reply_text({
            "choices": [{
                "message": {
                    "content": [
                        {"type": "text", "text": "第一段"},
                        {"type": "text", "text": "第二段"},
                    ]
                }
            }]
        })

        self.assertEqual(text, "第一段\n第二段")
