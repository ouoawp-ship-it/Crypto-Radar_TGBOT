from __future__ import annotations

import json
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.decision_model import evaluate_decision
from paopao_radar.signal_store import SignalEventStore, append_from_push
from paopao_radar.web_services.decision import decision_for_symbol_payload, decisions_payload, decisions_stats_payload
from paopao_radar.web_services.public import public_decision_payload, public_decisions_payload, public_decisions_stats_payload


class CountingSignalEventStore(SignalEventStore):
    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "connection_count", 0)

    @contextmanager
    def connect(self):
        object.__setattr__(self, "connection_count", self.connection_count + 1)
        with super().connect() as conn:
            yield conn


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

    def test_multi_module_structure_low_risk_probe(self) -> None:
        payload = evaluate_decision([
            self.item(id=1, module="launch", score=72, excerpt="启动雷达强信号"),
            self.item(id=2, module="flow", score=68, excerpt="资金流增强"),
            self.item(id=3, module="structure", score=70, excerpt="结构突破确认站稳"),
        ], symbol="BTCUSDT")
        self.assertEqual(payload["decision"]["code"], "probe")
        self.assertEqual(payload["decision"]["label"], "可试仓")

    def test_strong_signal_with_crowding_no_chase(self) -> None:
        payload = evaluate_decision([
            self.item(id=1, module="launch", score=92, excerpt="启动强信号，短线追高风险"),
            self.item(id=2, module="flow", score=86, excerpt="连续拉升，过热拥挤"),
        ], symbol="BTCUSDT")
        self.assertEqual(payload["decision"]["code"], "avoid_chase")
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

    def test_major_symbol_dense_without_risk_is_not_risk_alert(self) -> None:
        modules = ["launch", "flow", "structure", "structure_review", "summary", "launch", "flow", "structure", "summary", "launch"]
        payload = evaluate_decision([
            self.item(id=index, module=module, score=75, excerpt="强信号 共振 结构突破确认")
            for index, module in enumerate(modules, 1)
        ], symbol="BTCUSDT")
        self.assertNotEqual(payload["decision"]["code"], "risk_alert")
        self.assertTrue(payload["calibration"]["major_symbol_adjusted"])

    def test_high_density_with_clear_funding_risk_is_risk_alert(self) -> None:
        payload = evaluate_decision([
            self.item(id=1, module="funding", score=90, excerpt="资金费率拥挤，风险加剧"),
            self.item(id=2, module="funding", score=88, excerpt="极负，结算周期缩短"),
            self.item(id=3, module="funding", score=86, excerpt="高杠杆风险"),
            self.item(id=4, module="funding", score=84, status="blocked", excerpt="假突破 破位"),
        ], symbol="ETHUSDT")
        self.assertEqual(payload["decision"]["code"], "risk_alert")
        self.assertIn("funding_crowding", payload["calibration"]["risk_triggered_by"])

    def test_decision_payload_has_explanations_and_calibration(self) -> None:
        payload = evaluate_decision([
            self.item(id=1, module="launch", score=72, excerpt="启动雷达强信号"),
            self.item(id=2, module="structure", score=70, excerpt="结构突破确认站稳"),
        ], symbol="BTCUSDT")
        self.assertIn("factor_explanations", payload)
        self.assertIn("calibration", payload)
        self.assertTrue(payload["reasons"])
        self.assertTrue(payload["watch_points"])
        self.assertIn("not_advice", payload["decision"])


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
        self.assertIn("data", payload)
        self.assertEqual(payload["symbol"], "BTCUSDT")
        self.assertEqual(payload["data"]["symbol"], "BTCUSDT")
        self.assertIn(payload["decision"]["label"], {"观察", "等待回踩", "可试仓", "禁止追高", "风险警报"})
        self.assertIn("scores", payload)
        self.assertIn("related_signals", payload)
        self.assertIn("factor_explanations", payload)
        self.assertIn("calibration", payload)

    def test_private_decisions_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            payload = decisions_payload(settings=settings, limit=10)
        self.assertTrue(payload["ok"])
        self.assertIn("data", payload)
        self.assertIn("items", payload["data"])
        self.assertGreaterEqual(payload["count"], 1)
        self.assertIn("decisions", payload)
        self.assertIn("summary", payload["data"])
        self.assertIn("distribution", payload["data"])
        self.assertTrue(all(item.get("coin") for item in payload["items"]))

    def test_public_decision_payload_is_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            payload = public_decision_payload("BTCUSDT", settings=settings)
        self.assertTrue(payload["ok"])
        self.assertIn("data", payload)
        self.assertEqual(payload["data"]["symbol"], "BTCUSDT")
        self.assert_public_safe(payload)
        self.assertIn("not_advice", payload["data"]["decision"])

    def test_public_decisions_payload_is_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            payload = public_decisions_payload(settings=settings, limit=5)
        self.assertTrue(payload["ok"])
        self.assertIn("data", payload)
        self.assertIn("items", payload["data"])
        self.assert_public_safe(payload)
        self.assertIn("decisions", payload)

    def test_decisions_stats_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            payload = decisions_stats_payload(settings=settings, limit=10)
        self.assertTrue(payload["ok"])
        self.assertIn("data", payload)
        data = payload["data"]
        self.assertIn("distribution", data)
        self.assertIn("risk_distribution", data)
        self.assertIn("observe", data["distribution"])
        ratio_sum = sum(float(item.get("ratio", 0)) for item in data["distribution"].values())
        self.assertLessEqual(abs(ratio_sum - 1), 0.01)

    def test_decision_lists_batch_symbol_queries(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            with patch(
                "paopao_radar.web_services.decision.decision_for_symbol_payload",
                side_effect=AssertionError("per-symbol query should not run"),
            ):
                listed = decisions_payload(settings=settings, limit=10)
                stats = decisions_stats_payload(settings=settings, limit=10)

        self.assertTrue(listed["ok"])
        self.assertTrue(stats["ok"])

    def test_decision_stats_reuses_one_request_connection(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            store = CountingSignalEventStore(settings.signal_events_db_path)
            with patch("paopao_radar.web_services.decision._store", return_value=store):
                payload = decisions_stats_payload(settings=settings, limit=10)

        self.assertTrue(payload["ok"])
        self.assertEqual(store.connection_count, 1)

    def test_batched_decision_keeps_full_signal_text_semantics(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            append_from_push(
                settings,
                template_id="TG_FUNDING_ALERT",
                dedup_key="decision:long-risk-text",
                status="blocked",
                sent=False,
                text="BTCUSDT " + ("x" * 1300) + " 高杠杆 风险加剧 假突破",
                ts=int(time.time()),
            )
            single = decision_for_symbol_payload("BTCUSDT", settings=settings)
            batched = decisions_payload(symbol="BTCUSDT", settings=settings, limit=1)["items"][0]

        self.assertEqual(batched["scores"], single["scores"])
        self.assertEqual(
            batched["calibration"]["risk_keywords"],
            single["calibration"]["risk_keywords"],
        )

    def test_decisions_stats_empty_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            payload = decisions_stats_payload(settings=settings, limit=10)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["total_symbols"], 0)
        self.assertEqual(payload["data"]["distribution"]["observe"]["count"], 0)

    def test_public_decisions_stats_payload_is_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            self.seed(settings)
            payload = public_decisions_stats_payload(settings=settings, limit=10)
        self.assertTrue(payload["ok"])
        self.assertIn("data", payload)
        self.assert_public_safe(payload)
        self.assertIn("distribution", payload["data"])


if __name__ == "__main__":
    unittest.main()
