from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from paopao_radar.ai_assistant import (
    build_chat_completion_payload,
    extract_ai_reply_text,
    handle_callback_query,
    handle_message,
    handle_message_reply,
    is_alert_intent,
    parse_alert_request,
    telegram_plain_text,
)
from paopao_radar.config import Settings
from paopao_radar.price_alerts import PriceAlertStore
from paopao_radar.price_alerts import AlertMarketQuote


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
        self.assertIn("AI 正常对话", reply)
        self.assertIn("AI 分析数据行情", reply)
        self.assertIn("设置价格提醒", reply)
        self.assertIn("自然语言不再创建提醒", reply)

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
        self.assertIn("AI 正常对话", reply.text)
        self.assertIsNotNone(reply.reply_markup)
        buttons = reply.reply_markup["inline_keyboard"]
        flat = [button["callback_data"] for row in buttons for button in row]
        self.assertIn("flow:alert_setup", flat)
        self.assertIn("menu:analysis", flat)
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
        self.assertIn("命令当前不支持", reply.text)
        self.assertIsNone(reply.reply_markup)

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
            self.assertIn("交易所：Bybit", exchange_reply.text if exchange_reply else "")
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
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].direction, "below")
            self.assertEqual(alerts[0].exchange, "bybit")
            self.assertEqual(alerts[0].market_type, "futures")
            self.assertEqual(alerts[0].alert_type, "target_price")

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

            self.assertIn("价格提醒已经改成手动选择模式", reply.text if reply else "")
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
        self.assertIn("命令当前不支持", reply.text)
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
            self.assertIn("手动选择模式", reply or "")
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
            self.assertIn("你是想设置 BTCUSDT", reply or "")
            self.assertIn("提醒我", reply or "")
            self.assertEqual(store.stats()["total"], 0)

    def test_analyze_command_uses_analyst_prompt_without_creating_alert(self) -> None:
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
                "text": "/analyze BTC 跌破 58000 提醒我",
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
                "text": "/analyze BTC 资金费率 -2%/1H",
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
                "text": "/ai 测试",
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
                "text": "/ai 测试",
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

            self.assertIn("手动选择模式", reply or "")
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

    def test_ai_command_keeps_assistant_route_even_when_text_mentions_price(self) -> None:
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
            response = Mock()
            response.json.return_value = {"choices": [{"message": {"content": "AI 问答结果"}}]}

            with patch("paopao_radar.ai_assistant.fetch_binance_prices") as prices:
                with patch("paopao_radar.ai_assistant.requests.post", return_value=response) as post:
                    reply = handle_message(settings, store, message)

            self.assertEqual(reply, "AI 问答结果")
            prices.assert_not_called()
            self.assertIn("用户问题：BTC 现在多少钱", post.call_args.kwargs["json"]["messages"][1]["content"])

    def test_natural_language_alert_list_pause_resume_delete(self) -> None:
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

            self.assertIn("BTCUSDT", reply_for("我的提醒有哪些") or "")
            self.assertIn("已暂停", reply_for(f"暂停提醒 {created.id}") or "")
            self.assertEqual(store.list_alerts(user_id="42")[0].status, "paused")
            self.assertIn("已恢复", reply_for(f"恢复提醒 {created.id}") or "")
            self.assertEqual(store.list_alerts(user_id="42")[0].status, "active")
            self.assertIn("已删除", reply_for(f"删除提醒 {created.id}") or "")
            self.assertEqual(store.stats()["total"], 0)

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

            self.assertIn("手动选择模式", reply or "")
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

            self.assertIn("手动选择模式", reply or "")
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

            self.assertIn("当前没有价格提醒", reply or "")

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

            self.assertIn("当前没有价格提醒", reply or "")

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

            self.assertIn("当前没有价格提醒", reply or "")

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
