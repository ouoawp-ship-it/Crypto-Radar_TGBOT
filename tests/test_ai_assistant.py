from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paopao_radar.ai_assistant import handle_message, parse_alert_request
from paopao_radar.config import Settings
from paopao_radar.price_alerts import PriceAlertStore


class AiAssistantTests(unittest.TestCase):
    def test_parse_chinese_alert_request(self) -> None:
        parsed = parse_alert_request("BTC 跌破 58000 提醒我")
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed.symbol, "BTCUSDT")
        self.assertEqual(parsed.direction, "below")
        self.assertEqual(parsed.target_price, 58000)

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
