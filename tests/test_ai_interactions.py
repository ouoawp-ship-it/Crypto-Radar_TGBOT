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
    build_symbol_dossier_reply,
    build_chat_completion_payload,
    check_and_send_price_alerts,
    classify_user_intent,
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
from paopao_radar.signal_store import SignalEventStore, append_from_push


class AiAssistantTests(unittest.TestCase):
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

    def test_signal_detail_analyze_deep_link_builds_symbol_dossier(self) -> None:
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
                "text": "/start analyze_BTC",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            with patch(
                "paopao_radar.ai_assistant.build_symbol_dossier_reply",
                return_value="BTC dossier",
            ) as build_reply:
                reply = handle_message_reply(settings, store, message, sessions={})

        self.assertIsNotNone(reply)
        assert reply is not None
        self.assertEqual(reply.text, "BTC dossier")
        build_reply.assert_called_once_with(settings, store, "42", "BTCUSDT 怎么看")

    def test_signal_aware_deep_link_preserves_public_reference(self) -> None:
        reference = "sig_0123456789abcdefabcd"
        analyze = classify_user_intent(f"/start analyze_BTC_{reference}")
        alert = classify_user_intent(f"/start alert_BTC_{reference}")

        self.assertEqual(analyze.kind, "dossier")
        self.assertEqual(analyze.symbol, "BTCUSDT")
        self.assertEqual(analyze.signal_ref, reference)
        self.assertIn(f"signal_ref={reference}", analyze.prompt)
        self.assertEqual(alert.kind, "alert_deep")
        self.assertEqual(alert.signal_ref, reference)

    def test_symbol_dossier_loads_only_matching_requested_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_db_path=Path(tmp) / "signals.db",
                ai_price_alerts_db_path=Path(tmp) / "alerts.db",
            )
            append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="ai-context:btc",
                status="sent",
                sent=True,
                text="BTCUSDT\n启动雷达\n分数: 88",
                ts=1_000,
            )
            signal = SignalEventStore(settings.signal_events_db_path).symbol_timeline("BTCUSDT", limit=1)[0]
            reference = str(signal["public_ref"])
            store = PriceAlertStore(settings.ai_price_alerts_db_path)

            with patch(
                "paopao_radar.ai_assistant.build_symbol_dossier",
                return_value={"symbol": "BTCUSDT", "snapshot": {}, "history": [], "verdict": {}},
            ), patch(
                "paopao_radar.ai_assistant.format_symbol_dossier_report",
                side_effect=lambda dossier: str((dossier.get("requested_signal") or {}).get("public_ref") or "missing"),
            ):
                reply = build_symbol_dossier_reply(settings, store, "42", f"BTCUSDT 怎么看 signal_ref={reference}")

        self.assertEqual(reply, reference)

    def test_signal_detail_alert_deep_link_prefills_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reference = "sig_0123456789abcdefabcd"
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
            message = {
                "text": f"/start alert_BTC_{reference}",
                "from": {"id": 42, "username": "tester"},
                "chat": {"id": 42, "type": "private"},
            }

            first = handle_message_reply(settings, store, message, sessions=sessions)  # type: ignore[arg-type]
            self.assertIsNotNone(first)
            self.assertIn("BTCUSDT", first.text if first else "")
            self.assertEqual(sessions["42:42"]["state"], "alert_kind")
            self.assertEqual(sessions["42:42"]["prefill_symbol"], "BTCUSDT")
            self.assertEqual(sessions["42:42"]["source_signal_ref"], reference)

            quotes = [
                AlertMarketQuote(exchange="binance", market_type="spot", symbol="BTCUSDT", pair="BTCUSDT", price=61230),
                AlertMarketQuote(exchange="bybit", market_type="futures", symbol="BTCUSDT", pair="BTCUSDT", price=61234.5),
            ]
            with patch("paopao_radar.ai_assistant.discover_alert_markets", return_value=quotes):
                advanced = handle_callback_query(
                    settings,
                    store,
                    {
                        "data": "alert:kind:target_price",
                        "from": {"id": 42, "username": "tester"},
                        "message": {"chat": {"id": 42, "type": "private"}},
                    },
                    sessions=sessions,  # type: ignore[arg-type]
                )

        self.assertIsNotNone(advanced)
        self.assertIn("已识别币种：BTCUSDT", advanced.text if advanced else "")
        self.assertEqual(sessions["42:42"]["state"], "alert_market")

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

    def test_price_alert_scan_keeps_alert_active_when_send_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "alerts.db"
            settings = Settings(
                data_dir=Path(tmp),
                ai_price_alerts_enable=True,
                ai_price_alerts_db_path=db_path,
            )
            store = PriceAlertStore(db_path)
            alert = store.create_alert(
                user_id="42",
                chat_id="42",
                username="tester",
                symbol="BTC",
                exchange="binance",
                market_type="futures",
                pair="BTCUSDT",
                direction="above",
                target_price=60000,
            )

            class FailingSender:
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
                    return False

            with patch("paopao_radar.ai_assistant.fetch_price_alert_prices", return_value={alert.price_key: 61000.0}):
                result = check_and_send_price_alerts(settings, store, FailingSender())  # type: ignore[arg-type]

            updated = store.get_alert(alert.id)
            self.assertFalse(result["ok"])
            self.assertEqual(result["triggered"], 0)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, "active")
            self.assertEqual(updated.trigger_count, 0)

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
