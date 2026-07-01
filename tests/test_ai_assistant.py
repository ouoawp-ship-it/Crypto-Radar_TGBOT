from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from paopao_radar.ai_assistant import (
    build_chat_completion_payload,
    handle_message,
    is_alert_intent,
    parse_alert_request,
    telegram_plain_text,
)
from paopao_radar.config import Settings
from paopao_radar.price_alerts import PriceAlertStore


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

    def test_handle_message_creates_alert_for_allowed_user(self) -> None:
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
            self.assertIn("已创建价格提醒", reply or "")
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].symbol, "ETHUSDT")
            self.assertEqual(alerts[0].direction, "above")

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

    def test_handle_message_creates_alert_from_explicit_to_price_words(self) -> None:
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

            self.assertIn("已创建价格提醒", reply or "")
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].symbol, "BTCUSDT")
            self.assertEqual(alerts[0].direction, "below")

    def test_alert_command_creates_alert_without_natural_language_intent(self) -> None:
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

            self.assertIn("已创建价格提醒", reply or "")
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].symbol, "ETHUSDT")
            self.assertEqual(alerts[0].direction, "above")

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

    def test_group_message_with_bot_mention_creates_alert(self) -> None:
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

            self.assertIn("已创建价格提醒", reply or "")
            alerts = store.list_alerts(user_id="42")
            self.assertEqual(len(alerts), 1)
            self.assertEqual(alerts[0].chat_id, "-1001")
            self.assertEqual(alerts[0].symbol, "ETHUSDT")

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
