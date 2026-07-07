from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.decision_model import evaluate_decision
from paopao_radar.signal_store import append_from_push
from paopao_radar.web_services.decision import decision_for_symbol_payload, decisions_payload
from paopao_radar.web_services.public import public_decision_payload, public_decisions_payload


class DecisionModelTests(unittest.TestCase):
    def item(self, **kwargs):
        base = {
            "id": 1,
            "symbol": "BTCUSDT",
            "module": "summary",
            "status": "sent",
            "score": 35,
            "stage": "",
            "signal_type": "test",
            "excerpt": "普通关注信号",
            "time": "2026-07-06T00:00:00+00:00",
        }
        base.update(kwargs)
        return base

    def test_single_weak_signal_observe(self) -> None:
        payload = evaluate_decision([self.item(score=30, module="summary", excerpt="单一关注")], symbol="BTCUSDT")
        self.assertEqual(payload["decision"]["code"], "observe")
        self.assertEqual(payload["decision"]["label"], "观察")

    def test_multi_module_structure_low_risk_trial_position(self) -> None:
        payload = evaluate_decision([
            self.item(id=1, module="launch", score=72, excerpt="启动雷达强信号"),
            self.item(id=2, module="flow", score=68, excerpt="资金流增强"),
            self.item(id=3, module="structure", score=70, excerpt="结构突破确认站稳"),
        ], symbol="BTCUSDT")
        self.assertEqual(payload["decision"]["code"], "trial_position")
        self.assertEqual(payload["decision"]["label"], "可试仓")

    def test_strong_signal_with_crowding_no_chase(self) -> None:
        payload = evaluate_decision([
            self.item(id=1, module="launch", score=92, excerpt="启动强信号，短线追高风险"),
            self.item(id=2, module="flow", score=86, excerpt="连续拉升，过热拥挤"),
        ], symbol="BTCUSDT")
        self.assertEqual(payload["decision"]["code"], "no_chase")
        self.assertEqual(payload["decision"]["label"], "禁止追高")

    def test_dense_funding_risk_alert(self) -> None:
        payload = evaluate_decision([
            self.item(id=1, module="funding", score=80, excerpt="资金费率拥挤，风险加剧"),
            self.item(id=2, module="funding", score=82, excerpt="极负，结算周期缩短"),
            self.item(id=3, module="funding", score=78, excerpt="高杠杆风险"),
        ], symbol="BTCUSDT")
        self.assertEqual(payload["decision"]["code"], "risk_alert")
        self.assertEqual(payload["decision"]["label"], "风险警报")

    def test_strong_launch_without_structure_wait_pullback(self) -> None:
        payload = evaluate_decision([
            self.item(id=1, module="launch", score=88, excerpt="启动雷达强信号，已经拉升"),
        ], symbol="BTCUSDT")
        self.assertEqual(payload["decision"]["code"], "wait_pullback")
        self.assertEqual(payload["decision"]["label"], "等待回踩")

    def test_failed_blocked_lower_confidence(self) -> None:
        clean = evaluate_decision([
            self.item(id=1, module="launch", score=80, excerpt="启动信号"),
            self.item(id=2, module="structure", score=75, excerpt="结构确认突破"),
        ], symbol="BTCUSDT")
        dirty = evaluate_decision([
            self.item(id=1, module="launch", score=80, status="blocked", excerpt="启动信号 blocked"),
            self.item(id=2, module="structure", score=75, status="failed", excerpt="结构确认失败"),
        ], symbol="BTCUSDT")
        self.assertLess(dirty["decision"]["confidence"], clean["decision"]["confidence"])
        self.assertGreater(dirty["scores"]["failure_penalty"], 0)


class DecisionApiTests(unittest.TestCase):
    def make_settings(self, tmp: str) -> Settings:
        return Settings(
            data_dir=Path(tmp),
            signal_events_path=Path(tmp) / "signal_events.json",
            signal_events_db_path=Path(tmp) / "signals.db",
        )

    def seed(self, settings: Settings) -> None:
        now = int(time.time())
        append_from_push(settings, template_id="TG_LAUNCH_RADAR", dedup_key="decision:btc:launch", status="sent", sent=True, text="BTCUSDT 启动雷达强信号", ts=now - 30)
        append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="decision:btc:flow", status="sent", sent=True, text="BTCUSDT 资金流增强", ts=now - 20)
        append_from_push(settings, template_id="TG_STRUCTURE_RADAR", dedup_key="decision:btc:structure", status="sent", sent=True, text="BTCUSDT 结构突破确认站稳", ts=now - 10)
        append_from_push(settings, template_id="TG_FUNDING_ALERT", dedup_key="decision:eth:risk", status="blocked", sent=False, text="ETHUSDT funding 拥挤 风险加剧", ts=now - 5)

    def assert_public_safe(self, payload: dict) -> None:
        text = json.dumps(payload, ensure_ascii=False)
        for forbidden in (
            "payload_json",
            "text_html",
            "dedup_key",
            "message_ids",
            "topic_id",
            "reply_to_message_id",
            "WEB_ADMIN_TOKEN",
            "BOT_TOKEN",
            "TELEGRAM",
            "jobs",
            "audit",
            "config",
            "logs",
        ):
            self.assertNotIn(forbidden, text)

    def test_private_decision_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            payload = decision_for_symbol_payload("BTC", settings=settings, window_sec=86400, limit=20)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["symbol"], "BTCUSDT")
        self.assertIn(payload["decision"]["label"], {"观察", "等待回踩", "可试仓", "禁止追高", "风险警报"})
        self.assertIn("scores", payload)
        self.assertIn("related_signals", payload)

    def test_private_decisions_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            payload = decisions_payload(settings=settings, limit=10)
        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(payload["count"], 1)
        self.assertIn("decisions", payload)

    def test_public_decision_payload_is_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            payload = public_decision_payload("BTCUSDT", settings=settings)
        self.assertTrue(payload["ok"])
        self.assert_public_safe(payload)
        self.assertIn("not_advice", payload["decision"])

    def test_public_decisions_payload_is_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            payload = public_decisions_payload(settings=settings, limit=5)
        self.assertTrue(payload["ok"])
        self.assert_public_safe(payload)
        self.assertIn("decisions", payload)


if __name__ == "__main__":
    unittest.main()
