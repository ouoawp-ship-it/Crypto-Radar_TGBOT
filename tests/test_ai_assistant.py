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
    telegram_plain_text,
)
from paopao_radar.config import Settings
from paopao_radar.price_alerts import AlertMarketQuote, PriceAlertStore


class AiAssistantTests(unittest.TestCase):
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

    def test_parse_chinese_alert_request(self) -> None:
        parsed = parse_alert_request("BTC 跌破 58000 提醒我")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.symbol, "BTCUSDT")
        self.assertEqual(parsed.direction, "below")
        self.assertEqual(parsed.target_price, 58000)

    def test_alert_intent_requires_explicit_create_words(self) -> None:
        self.assertFalse(is_alert_intent("BTC 跌破 58000"))
        self.assertFalse(is_alert_intent("ETH 突破 4200"))
        self.assertTrue(is_alert_intent("BTC 跌破 58000 提醒我"))
        self.assertTrue(is_alert_intent("ETH 涨到 4200 通知我"))

    def test_start_message_explains_core_features(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_allow_group_chat=True,
                ai_allowed_chat_ids=("-1001",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "/start",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            reply = handle_message(settings, store, message)

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("泡泡 AI 助手", reply)
        self.assertIn("看行情", reply)
        self.assertIn("设置价格提醒", reply)
        self.assertIn("按钮确认", reply)

    def test_handle_message_reply_start_has_home_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_allow_group_chat=True,
                ai_allowed_chat_ids=("-1001",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "/start",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            reply = handle_message_reply(settings, store, message, sessions={})

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("泡泡 AI 助手", reply.text)
        self.assertIsNotNone(reply.reply_markup)
        buttons = reply.reply_markup["inline_keyboard"]
        flat = [button["callback_data"] for row in buttons for button in row]
        labels = [button["text"] for row in buttons for button in row]
        self.assertIn("flow:alert_setup", flat)
        self.assertIn("menu:price_query", flat)
        self.assertIn("menu:alerts", flat)
        self.assertIn("menu:help", flat)
        self.assertIn("使用说明", labels)
        self.assertNotIn("menu:assistant", flat)
        self.assertNotIn("泡泡 AI 助手", labels)
        self.assertNotIn("menu:analysis", flat)
        self.assertNotIn("menu:group", flat)

    def test_paopao_command_is_not_start_alias(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "/paopao",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            reply = handle_message_reply(settings, store, message, sessions={})

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("只保留 /start", reply.text)
        self.assertIsNotNone(reply.reply_markup)

    def test_button_alert_setup_flow_requires_final_confirm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            sessions: dict[str, dict[str, object]] = {}
            callback = {
                "data": "flow:alert_setup",
                "from": {"id": 42, "username": "tester"},
                "message": {"chat": {"id": 42, "type": "private"}},
            }

            first = handle_callback_query(settings, store, callback, sessions=sessions)  # type: ignore[arg-type]
            self.assertIsNotNone(first)
            self.assertIn("请选择要创建的监控类型", first.text if first else "")
            self.assertEqual(sessions["42:42"]["state"], "alert_kind")

            kind_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": "alert:kind:target_price",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions=sessions,  # type: ignore[arg-type]
            )
            self.assertIn("已选择：目标价提醒", kind_reply.text if kind_reply else "")
            self.assertEqual(sessions["42:42"]["state"], "alert_symbol")

            quotes = [
                AlertMarketQuote(exchange="binance", market_type="spot", symbol="BTCUSDT", pair="BTCUSDT", price=61230),
                AlertMarketQuote(exchange="bybit", market_type="futures", symbol="BTCUSDT", pair="BTCUSDT", price=61234.5),
            ]
            with patch("paopao_radar.ai_assistant.discover_alert_markets", return_value=quotes):
                coin_reply = handle_message_reply(
                    settings,
                    store,
                    {"text": "BTC", "from": {"id": 42, "username": "tester"}, "chat": {"id": 42, "type": "private"}},
                    sessions=sessions,  # type: ignore[arg-type]
                )
            self.assertIn("已识别币种：BTCUSDT", coin_reply.text if coin_reply else "")
            self.assertEqual(sessions["42:42"]["state"], "alert_market")

            market_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": "alert:market:futures",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions=sessions,  # type: ignore[arg-type]
            )
            self.assertIn("请选择交易所", market_reply.text if market_reply else "")

            exchange_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": "alert:exchange:bybit:futures:BTCUSDT",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions=sessions,  # type: ignore[arg-type]
            )
            self.assertIn('交易所：<a href="https://www.coinglass.com/tv/zh/Bybit_BTCUSDT"><b>Bybit</b></a>', exchange_reply.text if exchange_reply else "")
            self.assertIn("交易对：<code>BTCUSDT</code>", exchange_reply.text if exchange_reply else "")
            self.assertEqual(sessions["42:42"]["state"], "alert_price")

            fresh_quote = AlertMarketQuote(exchange="bybit", market_type="futures", symbol="BTCUSDT", pair="BTCUSDT", price=61234.5)
            with patch("paopao_radar.ai_assistant.fetch_alert_market_quote", return_value=fresh_quote):
                confirm_reply = handle_message_reply(
                    settings,
                    store,
                    {"text": "58000", "from": {"id": 42, "username": "tester"}, "chat": {"id": 42, "type": "private"}},
                    sessions=sessions,  # type: ignore[arg-type]
                )

            self.assertIn("请选择触发后的提醒方式", confirm_reply.text if confirm_reply else "")
            self.assertEqual(store.stats()["total"], 0)

            repeat_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": "alert:repeat:once",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions=sessions,  # type: ignore[arg-type]
            )
            self.assertIn("请确认添加监控提醒", repeat_reply.text if repeat_reply else "")
            self.assertIn("价格 低于或等于 $58,000.00", repeat_reply.text if repeat_reply else "")

            create_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": "alert:confirm_pending",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions=sessions,  # type: ignore[arg-type]
            )

            self.assertIn("已创建监控提醒", create_reply.text if create_reply else "")
            self.assertIn("编号：1", create_reply.text if create_reply else "")
            self.assertIn('<a href="https://www.coinglass.com/tv/zh/Bybit_BTCUSDT"><b>Bybit</b></a>', create_reply.text if create_reply else "")
            self.assertIn("交易对：<code>BTCUSDT</code>", create_reply.text if create_reply else "")
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].direction, "below")
            self.assertEqual(alerts[0].exchange, "bybit")
            self.assertEqual(alerts[0].market_type, "futures")
            self.assertEqual(alerts[0].alert_type, "target_price")

    def test_alerts_menu_has_delete_buttons(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            created = store.create_alert(
                user_id="42",
                chat_id="42",
                username="tester",
                symbol="FIL",
                exchange="bybit",
                market_type="futures",
                pair="FILUSDT",
                direction="above",
                target_price=0.784,
            )
            query = {
                "data": "menu:alerts",
                "from": {"id": 42, "username": "tester"},
                "message": {"chat": {"id": 42, "type": "private"}},
            }

            reply = handle_callback_query(settings, store, query, sessions={})

            self.assertIsNotNone(reply)
            assert reply is not None
            self.assertIn("你的价格提醒", reply.text)
            self.assertIn("1. 目标价提醒", reply.text)
            self.assertIn('<a href="https://www.coinglass.com/tv/zh/Bybit_FILUSDT"><b>Bybit</b></a>', reply.text)
            self.assertIn("<code>FILUSDT</code>", reply.text)
            flat = [button for row in reply.reply_markup["inline_keyboard"] for button in row]  # type: ignore[index]
            self.assertIn({"text": "暂停1", "callback_data": f"alert:pause:{created.id}"}, flat)
            self.assertIn({"text": "删除1", "callback_data": f"alert:delete:{created.id}"}, flat)

            pause_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": f"alert:pause:{created.id}",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions={},
            )

            self.assertIsNotNone(pause_reply)
            assert pause_reply is not None
            self.assertIn("提醒 1 已暂停", pause_reply.text)
            self.assertEqual(store.list_alerts(user_id="42")[0].status, "paused")
            pause_flat = [button for row in pause_reply.reply_markup["inline_keyboard"] for button in row]  # type: ignore[index]
            self.assertIn({"text": "恢复1", "callback_data": f"alert:resume:{created.id}"}, pause_flat)

            resume_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": f"alert:resume:{created.id}",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions={},
            )

            self.assertIsNotNone(resume_reply)
            assert resume_reply is not None
            self.assertIn("提醒 1 已恢复", resume_reply.text)
            self.assertEqual(store.list_alerts(user_id="42")[0].status, "active")

            delete_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": f"alert:delete:{created.id}",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions={},
            )

            self.assertIsNotNone(delete_reply)
            assert delete_reply is not None
            self.assertIn("已删除提醒 1", delete_reply.text)
            self.assertEqual(store.stats()["total"], 0)

    def test_alert_display_numbers_reflow_after_delete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            older = store.create_alert(
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
            newer = store.create_alert(
                user_id="42",
                chat_id="42",
                username="tester",
                symbol="ETH",
                exchange="okx",
                market_type="spot",
                pair="ETH-USDT",
                direction="below",
                target_price=3000,
            )

            first_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": "menu:alerts",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions={},
            )
            assert first_reply is not None
            self.assertIn("1. 目标价提醒", first_reply.text)
            self.assertIn("<code>ETH-USDT</code>", first_reply.text)
            first_flat = [button for row in first_reply.reply_markup["inline_keyboard"] for button in row]  # type: ignore[index]
            self.assertIn({"text": "删除1", "callback_data": f"alert:delete:{newer.id}"}, first_flat)
            self.assertIn({"text": "删除2", "callback_data": f"alert:delete:{older.id}"}, first_flat)

            delete_reply = handle_callback_query(
                settings,
                store,
                {
                    "data": f"alert:delete:{newer.id}",
                    "from": {"id": 42, "username": "tester"},
                    "message": {"chat": {"id": 42, "type": "private"}},
                },
                sessions={},
            )

            assert delete_reply is not None
            self.assertIn("已删除提醒 1", delete_reply.text)
            self.assertIn("1. 目标价提醒", delete_reply.text)
            self.assertIn("<code>BTCUSDT</code>", delete_reply.text)
            self.assertNotIn("2. 目标价提醒", delete_reply.text)
            delete_flat = [button for row in delete_reply.reply_markup["inline_keyboard"] for button in row]  # type: ignore[index]
            self.assertIn({"text": "删除1", "callback_data": f"alert:delete:{older.id}"}, delete_flat)

    def test_handle_message_reply_natural_alert_routes_to_manual_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            reply = handle_message_reply(
                settings,
                store,
                {
                    "text": "ETH 突破 4200 提醒我",
                    "from": {"id": 42, "username": "tester"},
                    "chat": {"id": 42, "type": "private"},
                },
                sessions={},
            )

            self.assertIn("手动选择交易所", reply.text if reply else "")
            self.assertEqual(store.stats()["total"], 0)
            markup = reply.reply_markup if reply else {}
            flat = [button["callback_data"] for row in markup["inline_keyboard"] for button in row]
            self.assertIn("flow:alert_setup", flat)

    def test_id_command_is_not_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_allow_group_chat=True,
                ai_allowed_chat_ids=("-1001",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "/id",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": -1001, "type": "supergroup"},
                "reply_to_message": {"from": {"id": 819, "is_bot": True, "username": "v8pao_bot"}},
            }

            reply = handle_message_reply(settings, store, message, bot_username="v8pao_bot", bot_user_id="819")

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertIn("只保留 /start", reply.text)
        self.assertNotIn("你的用户 ID", reply.text)
        self.assertNotIn("当前聊天 ID", reply.text)

    def test_handle_message_routes_symbol_dossier_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "GWEI 怎么看",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            with patch("paopao_radar.ai_assistant.build_symbol_dossier_reply", return_value="GWEI 档案") as dossier:
                reply = handle_message(settings, store, message)

            self.assertEqual(reply, "GWEI 档案")
            dossier.assert_called_once()

    def test_handle_message_routes_alert_words_to_manual_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "ETH 突破 4200 提醒我",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            reply = handle_message(settings, store, message)

            self.assertIsNotNone(reply)
            self.assertIn("手动选择交易所", reply or "")
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 0)

    def test_handle_message_does_not_create_alert_from_forwarded_signal(self) -> None:
        signal_text = "\n".join(
            [
                "🚀 启动雷达 [GWEI](https://www.coinglass.com/tv/zh/Binance_GWEIUSDT)",
                "阶段: 提前预警",
                "分数: 70",
                "",
                "触发明细",
                "15m价格: +3.0%",
                "1h价格: +11.0%",
                "15m OI: +3.3%",
                "1h OI: +13.1%",
                "成交量: 1.2x 均值",
                "",
                "风险",
                "跌回突破位则启动失败；同币同阶段会进入冷却",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": signal_text,
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            reply = handle_message(settings, store, message)

            self.assertNotIn("已创建价格提醒", reply or "")
            self.assertEqual(store.stats()["total"], 0)

    def test_forwarded_signal_without_command_uses_analyst_prompt(self) -> None:
        signal_text = "\n".join(
            [
                "🚀 启动雷达 GWEI",
                "阶段: 提前预警",
                "分数: 70",
                "15m价格: +3.0%",
                "1h OI: +13.1%",
                "成交量: 1.2x 均值",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            prompt_path = Path(tmp) / "ai_prompts.json"
            prompt_path.write_text(
                '{"assistant_prompt":"普通助手提示词","analyst_prompt":"专业分析师提示词"}',
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
                ai_provider_enable=True,
                ai_api_key="sk-test",
                ai_base_url="https://api.example.com",
                ai_model="deepseek-v4-pro",
                ai_prompts_path=prompt_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": signal_text,
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }
            response = Mock()
            response.json.return_value = {"choices": [{"message": {"content": "自动分析结果"}}]}

            with patch("paopao_radar.ai_assistant.requests.post", return_value=response) as post:
                reply = handle_message(settings, store, message)

            self.assertEqual(reply, "自动分析结果")
            self.assertEqual(store.stats()["total"], 0)
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["messages"][0]["content"], "专业分析师提示词")
            self.assertIn("用户提供的数据：", payload["messages"][1]["content"])
            self.assertIn("GWEI", payload["messages"][1]["content"])

    def test_ambiguous_alert_like_text_asks_for_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
                ai_provider_enable=True,
                ai_api_key="sk-test",
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "BTC 跌破 58000",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            with patch("paopao_radar.ai_assistant.requests.post") as post:
                reply = handle_message(settings, store, message)

            post.assert_not_called()
            self.assertIn("设置价格提醒", reply or "")
            self.assertIn("帮我分析这段", reply or "")
            self.assertEqual(store.stats()["total"], 0)

    def test_analysis_intent_uses_analyst_prompt_without_creating_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            prompt_path = Path(tmp) / "ai_prompts.json"
            prompt_path.write_text(
                '{"assistant_prompt":"普通助手提示词","analyst_prompt":"专业分析师提示词"}',
                encoding="utf-8",
            )
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
                ai_provider_enable=True,
                ai_api_key="sk-test",
                ai_base_url="https://api.example.com",
                ai_model="deepseek-v4-pro",
                ai_prompts_path=prompt_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "分析这段：BTC 跌破 58000 提醒我",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }
            response = Mock()
            response.json.return_value = {"choices": [{"message": {"content": "分析结果"}}]}

            with patch("paopao_radar.ai_assistant.requests.post", return_value=response) as post:
                reply = handle_message(settings, store, message)

            self.assertEqual(reply, "分析结果")
            self.assertEqual(store.stats()["total"], 0)
            payload = post.call_args.kwargs["json"]
            self.assertEqual(payload["model"], "deepseek-v4-pro")
            self.assertEqual(payload["thinking"], {"type": "enabled"})
            self.assertEqual(payload["reasoning_effort"], "high")
            self.assertIs(payload["stream"], False)
            self.assertEqual(payload["messages"][0]["content"], "专业分析师提示词")
            self.assertIn("用户提供的数据：", payload["messages"][1]["content"])
            self.assertIn("BTC 跌破 58000 提醒我", payload["messages"][1]["content"])

    def test_empty_ai_content_retries_without_thinking(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
                ai_provider_enable=True,
                ai_api_key="sk-test",
                ai_base_url="https://api.example.com",
                ai_model="deepseek-v4-pro",
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "分析这段：BTC 资金费率 -2%/1H",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }
            first = Mock()
            first.json.return_value = {
                "choices": [{
                    "message": {"content": "", "reasoning_content": "内部思考"},
                    "finish_reason": "stop",
                }]
            }
            second = Mock()
            second.json.return_value = {"choices": [{"message": {"content": "最终分析正文"}}]}

            with patch("paopao_radar.ai_assistant.requests.post", side_effect=[first, second]) as post:
                reply = handle_message(settings, store, message)

            self.assertEqual(reply, "最终分析正文")
            self.assertEqual(post.call_count, 2)
            retry_payload = post.call_args.kwargs["json"]
            self.assertEqual(retry_payload["thinking"], {"type": "disabled"})
            self.assertNotIn("reasoning_effort", retry_payload)

    def test_ai_provider_error_includes_response_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
                ai_provider_enable=True,
                ai_api_key="sk-test",
                ai_base_url="https://api.example.com",
                ai_model="deepseek-v4-pro",
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "测试 AI 接口",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }
            response = Mock()
            response.status_code = 400
            response.reason = "Bad Request"
            response.text = '{"error":"invalid model"}'
            response.raise_for_status.side_effect = requests.HTTPError("bad request")

            with patch("paopao_radar.ai_assistant.requests.post", return_value=response):
                reply = handle_message(settings, store, message)

            self.assertIn("invalid model", reply or "")
            self.assertIn("400 Bad Request", reply or "")

    def test_ai_provider_timeout_uses_readable_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
                ai_provider_enable=True,
                ai_api_key="sk-test",
                ai_base_url="https://api.example.com",
                ai_model="deepseek-v4-pro",
                ai_request_timeout_sec=90,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "测试 AI 接口",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            with patch(
                "paopao_radar.ai_assistant.requests.post",
                side_effect=requests.exceptions.ReadTimeout("Read timed out."),
            ):
                reply = handle_message(settings, store, message)

            self.assertIn("AI 接口响应超时", reply or "")
            self.assertIn("90 秒", reply or "")
            self.assertIn("deepseek-v4-flash", reply or "")

    def test_handle_message_routes_explicit_to_price_words_to_manual_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "BTC 跌到 58000 通知我",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            reply = handle_message(settings, store, message)

            self.assertIn("手动选择交易所", reply or "")
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 0)

    def test_natural_language_price_query(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "BTC 现在多少钱",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            quote = AlertMarketQuote(exchange="binance", market_type="futures", symbol="BTCUSDT", pair="BTCUSDT", price=61234.5)
            with patch("paopao_radar.ai_assistant.discover_alert_markets", return_value=[quote]):
                reply = handle_message(settings, store, message)

            self.assertIn("BTCUSDT 多交易所价格", reply or "")
            self.assertIn("合约", reply or "")
            self.assertIn("$61,234.50", reply or "")
            self.assertIn("<pre>", reply or "")
            self.assertIn("交易所", reply or "")
            self.assertIn("Binance  BTCUSDT  $61,234.50", reply or "")
            self.assertIn('K线：<a href="https://www.coinglass.com/tv/zh/Binance_BTCUSDT"><b>Binance</b></a>', reply or "")

    def test_slash_ai_command_no_longer_routes_to_ai_or_price(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
                ai_provider_enable=True,
                ai_api_key="sk-test",
                ai_base_url="https://api.example.com",
                ai_model="deepseek-v4-pro",
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "/ai BTC 现在多少钱",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }
            with patch("paopao_radar.ai_assistant.fetch_binance_prices") as prices:
                with patch("paopao_radar.ai_assistant.requests.post") as post:
                    reply = handle_message(settings, store, message)

            self.assertIn("只保留 /start", reply or "")
            prices.assert_not_called()
            post.assert_not_called()

    def test_natural_language_no_longer_mutates_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            created = store.create_alert(
                user_id="42",
                chat_id="42",
                username="tester",
                symbol="BTC",
                direction="above",
                target_price=60000,
                source="test",
                note="unit",
            )

            def reply_for(text: str) -> str | None:
                return handle_message(
                    settings,
                    store,
                    {"text": text, "from": {"id": 42, "username": "tester"}, "chat": {"id": 42, "type": "private"}},
                )

            self.assertIn("我的提醒", reply_for("我的提醒有哪些") or "")
            self.assertNotIn("已暂停", reply_for(f"暂停提醒 {created.id}") or "")
            self.assertEqual(store.list_alerts(user_id="42")[0].status, "active")
            self.assertNotIn("已删除", reply_for(f"删除提醒 {created.id}") or "")
            self.assertEqual(store.stats()["total"], 1)

    def test_alert_command_routes_to_manual_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "/alert ETH 高于 4200",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            reply = handle_message(settings, store, message)

            self.assertIn("只保留 /start", reply or "")
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 0)

    def test_group_message_without_bot_mention_is_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_allow_group_chat=True,
                ai_allowed_chat_ids=("-1001",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "ETH 突破 4200 提醒我",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": -1001, "type": "supergroup"},
            }

            reply = handle_message(settings, store, message, bot_username="v8pao_bot", bot_user_id="819")

            self.assertIsNone(reply)
            self.assertEqual(store.stats()["total"], 0)

    def test_group_message_with_bot_mention_routes_alert_to_manual_flow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_allow_group_chat=True,
                ai_allowed_chat_ids=("-1001",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "@v8pao_bot ETH 突破 4200 提醒我",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": -1001, "type": "supergroup"},
            }

            reply = handle_message(settings, store, message, bot_username="v8pao_bot", bot_user_id="819")

            self.assertIn("手动选择交易所", reply or "")
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 0)

    def test_group_mention_is_rejected_when_chat_is_not_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_allow_group_chat=True,
                ai_allowed_chat_ids=("-1002",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "@v8pao_bot ETH 突破 4200 提醒我",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": -1001, "type": "supergroup"},
            }

            reply = handle_message(settings, store, message, bot_username="v8pao_bot", bot_user_id="819")

            self.assertIn("这个群没有开通", reply or "")
            self.assertEqual(store.stats()["total"], 0)

    def test_group_allowed_chat_can_match_username(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_allow_group_chat=True,
                ai_allowed_chat_ids=("@allowed_group",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "@v8pao_bot /alerts",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": -1001, "type": "supergroup", "username": "allowed_group"},
            }

            reply = handle_message(settings, store, message, bot_username="v8pao_bot", bot_user_id="819")

            self.assertIn("只保留 /start", reply or "")

    def test_group_reply_to_bot_message_is_handled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_allow_group_chat=True,
                ai_allowed_chat_ids=("-1001",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "/alerts",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": -1001, "type": "supergroup"},
                "reply_to_message": {"from": {"id": 819, "is_bot": True, "username": "v8pao_bot"}},
            }

            reply = handle_message(settings, store, message, bot_username="v8pao_bot", bot_user_id="819")

            self.assertIn("只保留 /start", reply or "")

    def test_group_command_with_bot_suffix_is_handled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_allow_group_chat=True,
                ai_allowed_chat_ids=("-1001",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "/alerts@v8pao_bot",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": -1001, "type": "supergroup"},
            }

            reply = handle_message(settings, store, message, bot_username="v8pao_bot", bot_user_id="819")

            self.assertIn("只保留 /start", reply or "")

    def test_handle_message_rejects_unlisted_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_assistant_enable=True,
                ai_bot_token="123456:test",
                ai_admin_user_ids=("42",),
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            message = {
                "text": "BTC 跌破 58000 提醒我",
                "from": {"id": 7, "username": "guest"},
                "chat": {"id": 7, "type": "private"},
            }

            reply = handle_message(settings, store, message)

            self.assertIn("没有使用", reply or "")
            self.assertEqual(store.stats()["total"], 0)


if __name__ == "__main__":
    unittest.main()
