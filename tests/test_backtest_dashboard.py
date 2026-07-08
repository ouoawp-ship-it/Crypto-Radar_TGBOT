from __future__ import annotations

import json
import sqlite3
import time
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar import web
from paopao_radar.backtest_dashboard import (
    DecisionBacktestDashboard,
    build_backtest_payload,
    sample_quality,
)
from paopao_radar.config import Settings
from paopao_radar.outcome_tracker import OutcomeStore
from paopao_radar.web_services.backtest import (
    backtest_decision_payload,
    public_backtest_decision_payload,
    public_backtest_detail_payload,
    public_backtest_matrix_payload,
)


def make_settings(tmp: str) -> Settings:
    base = Path(tmp)
    return Settings(data_dir=base, outcome_db_path=base / "outcomes.db")


def iso(ts: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(ts))


def add_outcome(
    settings: Settings,
    *,
    signal_id: int,
    decision_code: str = "probe",
    decision_label: str = "可试仓",
    horizon: str = "1h",
    status: str = "success",
    final_return: float | None = 2.0,
    max_gain: float | None = 4.0,
    max_drawdown: float | None = -1.0,
    module: str = "flow",
    risk_level: str = "低",
    confidence: int | None = 76,
    error: str = "",
) -> None:
    store = OutcomeStore(settings.outcome_db_path)
    store.ensure_schema()
    now = int(time.time()) - 3600
    with store.connect() as conn:
        conn.execute(
            """
            INSERT INTO signal_outcomes (
                signal_id, symbol, coin, signal_time, horizon, horizon_sec, due_time,
                direction, final_return_pct, max_gain_pct, max_drawdown_pct,
                result_label, result_tone, decision_code, decision_label,
                decision_confidence, risk_level, module, signal_type, data_status,
                data_source, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'long', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'test',
                ?, 'binance', ?, ?, ?)
            """,
            (
                signal_id,
                "BTCUSDT" if decision_code != "unknown" else "OLDUSDT",
                "BTC",
                iso(now),
                horizon,
                3600,
                iso(now + 3600),
                final_return,
                max_gain,
                max_drawdown,
                "表现较强" if (final_return or 0) > 1 else "震荡",
                "good" if (final_return or 0) > 1 else "neutral",
                "" if decision_code == "unknown" else decision_code,
                "" if decision_code == "unknown" else decision_label,
                confidence,
                risk_level,
                module,
                status,
                error,
                iso(now),
                iso(now),
            ),
        )


class DecisionBacktestDashboardTests(unittest.TestCase):
    def test_aggregation_uses_success_only_and_keeps_unknown(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            add_outcome(settings, signal_id=1, decision_code="probe", final_return=2.0, max_gain=5.0, max_drawdown=-1.0)
            add_outcome(settings, signal_id=2, decision_code="probe", final_return=-1.0, max_gain=1.0, max_drawdown=-4.0)
            add_outcome(settings, signal_id=3, decision_code="probe", status="pending", final_return=None, max_gain=None, max_drawdown=None)
            add_outcome(settings, signal_id=4, decision_code="probe", status="unavailable", final_return=None, max_gain=None, max_drawdown=None)
            add_outcome(settings, signal_id=5, decision_code="unknown", final_return=0.5, max_gain=1.0, max_drawdown=-0.2)

            payload = build_backtest_payload(settings=settings, horizon="1h", window_sec=10**7)
            groups = {item["key"]: item for item in payload["decision_groups"]}
            probe = groups["probe"]
            unknown = groups["unknown"]

        self.assertEqual(probe["total_count"], 4)
        self.assertEqual(probe["success_count"], 2)
        self.assertEqual(probe["pending_count"], 1)
        self.assertEqual(probe["unavailable_count"], 1)
        self.assertEqual(probe["coverage_ratio"], 0.5)
        self.assertEqual(probe["avg_final_return_pct"], 0.5)
        self.assertEqual(probe["median_final_return_pct"], 0.5)
        self.assertEqual(probe["avg_max_gain_pct"], 3.0)
        self.assertEqual(probe["avg_max_drawdown_pct"], -2.5)
        self.assertEqual(probe["positive_ratio"], 0.5)
        self.assertEqual(probe["strong_ratio"], 0.5)
        self.assertEqual(probe["drawdown_ratio"], 0.5)
        self.assertIsNotNone(probe["avg_gain_drawdown_ratio"])
        self.assertIn("expectancy_score", probe)
        self.assertEqual(unknown["label"], "未识别")

    def test_sample_quality_rules(self) -> None:
        self.assertEqual(sample_quality(0, 1.0), "样本不足")
        self.assertEqual(sample_quality(10, 0.2), "观察中")
        self.assertEqual(sample_quality(20, 0.5), "可参考")
        self.assertEqual(sample_quality(50, 0.7), "较可信")

    def test_model_diagnosis_probe_and_risk_alert_paths(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            for idx in range(12):
                add_outcome(settings, signal_id=idx + 1, decision_code="risk_alert", decision_label="风险警报", final_return=-2.5, max_gain=0.5, max_drawdown=-4.0)
            payload = build_backtest_payload(settings=settings, horizon="1h", window_sec=10**7)
            diagnosis = payload["model_diagnosis"]

        self.assertTrue(any("可试仓样本不足" in item for item in diagnosis["calibration_hints"]))
        self.assertTrue(any("风险警报" in item for item in diagnosis["strengths"]))

    def test_risk_alert_overly_conservative_diagnosis(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            for idx in range(12):
                add_outcome(settings, signal_id=idx + 1, decision_code="risk_alert", decision_label="风险警报", final_return=2.5, max_gain=4.0, max_drawdown=-0.5)
            payload = build_backtest_payload(settings=settings, horizon="1h", window_sec=10**7)

        self.assertTrue(any("过度保守" in item for item in payload["model_diagnosis"]["weaknesses"]))

    def test_wait_pullback_diagnosis(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            for idx in range(12):
                add_outcome(settings, signal_id=idx + 1, decision_code="wait_pullback", decision_label="等待回踩", final_return=0.4, max_gain=2.0, max_drawdown=-2.2)
            payload = build_backtest_payload(settings=settings, horizon="1h", window_sec=10**7)

        self.assertTrue(any("等待回踩" in item for item in payload["model_diagnosis"]["strengths"]))

    def test_public_payloads_and_redaction(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(tmp)
            add_outcome(settings, signal_id=1, decision_code="probe", error="WEB_ADMIN_TOKEN BOT_TOKEN")
            private_payload = backtest_decision_payload(settings=settings, horizon="1h", window_sec=10**7)
            public_payload = public_backtest_decision_payload(settings=settings, horizon="1h", window_sec=10**7)
            matrix = public_backtest_matrix_payload(settings=settings, window_sec=10**7)
            detail = public_backtest_detail_payload(settings=settings, decision="probe", horizon="1h", limit=5, window_sec=10**7)

        self.assertTrue(private_payload["ok"])
        self.assertTrue(public_payload["ok"])
        self.assertTrue(matrix["ok"])
        self.assertTrue(detail["ok"])
        self.assertIn("items", matrix["data"])
        self.assertIn("items", detail["data"])
        self.assertNotIn("data_source", detail["items"][0])
        serialized = json.dumps({"public": public_payload, "matrix": matrix, "detail": detail}, ensure_ascii=False)
        for forbidden in ("WEB_ADMIN_TOKEN", "BOT_TOKEN", "payload_json", "text_html", "dedup_key", "message_ids", "topic_id", "jobs", "audit", "config", "logs", "Authorization", "Cookie", "chat_id", "api_key"):
            self.assertNotIn(forbidden, serialized)

    def test_web_routes_public_and_private_auth(self) -> None:
        def make_handler(path: str):
            handler = object.__new__(web.WebHandler)
            handler.path = path
            handler.headers = {}
            handler.server = type("Server", (), {"admin_token": "secret", "settings": Settings(web_auth_mode="password")})()
            handler.wfile = BytesIO()
            handler.send_response = lambda status: statuses.append(status)
            handler.send_header = lambda key, value: headers.append((key, value))
            handler.end_headers = lambda: None
            return handler

        statuses: list[int] = []
        headers: list[tuple[str, str]] = []
        public_summary = make_handler("/public-api/backtest/decision?horizon=1h")
        with patch("paopao_radar.web.public_backtest_decision_payload", return_value={"ok": True, "data": {"summary": {}}}):
            web.WebHandler.do_GET(public_summary)
        self.assertEqual(statuses[-1], 200)

        statuses.clear()
        public_matrix = make_handler("/public-api/backtest/decision/matrix")
        with patch("paopao_radar.web.public_backtest_matrix_payload", return_value={"ok": True, "data": {"items": []}}):
            web.WebHandler.do_GET(public_matrix)
        self.assertEqual(statuses[-1], 200)

        statuses.clear()
        public_detail = make_handler("/public-api/backtest/decision/detail?decision=risk_alert")
        with patch("paopao_radar.web.public_backtest_detail_payload", return_value={"ok": True, "data": {"items": []}}):
            web.WebHandler.do_GET(public_detail)
        self.assertEqual(statuses[-1], 200)

        for path in ("/api/backtest/decision", "/api/backtest/decision/matrix", "/api/backtest/decision/detail?decision=risk_alert"):
            statuses.clear()
            web.WebHandler.do_GET(make_handler(path))
            self.assertEqual(statuses[-1], 401)

    def test_frontend_contract_contains_chinese_backtest_copy(self) -> None:
        html = web.PUBLIC_INDEX_HTML + web.INDEX_HTML
        for text in ("决策回测", "模型诊断", "样本质量", "平均最终涨跌", "正收益比例", "平均最大回撤", "等待回踩", "可试仓", "禁止追高", "风险警报"):
            self.assertIn(text, html)
        for path in ("/public-api/backtest/decision", "/public-api/backtest/decision/matrix", "/public-api/backtest/decision/detail", "/api/backtest/decision"):
            self.assertIn(path, html)
        for english in ("Backtest Dashboard", "Win Rate", "Risk Level", "Confidence"):
            self.assertNotIn(english, web.PUBLIC_INDEX_HTML)


if __name__ == "__main__":
    unittest.main()
