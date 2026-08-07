from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from runtime.private_control_views import (
    MAX_VIEW_ITEMS,
    render_fault_explanations,
    render_push_records,
    render_recent_signals,
    render_unpublished_reasons,
)


SECRET = "987654321:raw-secret-token"


class PrivateControlViewsTests(unittest.TestCase):
    @staticmethod
    def create_signal_database(path: Path, *, rows: int = 10) -> None:
        with closing(sqlite3.connect(path)) as connection:
            connection.execute(
                """
                CREATE TABLE signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    module TEXT NOT NULL,
                    template_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    score REAL,
                    status TEXT NOT NULL,
                    title TEXT NOT NULL,
                    text_html TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    topic_id TEXT NOT NULL,
                    message_ids_json TEXT NOT NULL,
                    error TEXT NOT NULL
                )
                """
            )
            for index in range(rows):
                connection.execute(
                    """
                    INSERT INTO signals (
                        ts, module, template_id, symbol, stage, score, status,
                        title, text_html, dedup_key, topic_id,
                        message_ids_json, error
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        1_700_000_000 + index,
                        "launch",
                        "TG_LAUNCH_ALERT",
                        f"ASSET{index:02d}USDT",
                        "breakout",
                        70 + index,
                        "sent",
                        SECRET,
                        f"正文 {SECRET}",
                        f"dedup-{SECRET}",
                        "123456",
                        "[789]",
                        f"raw error {SECRET}",
                    ),
                )
            connection.commit()

    def test_recent_signals_uses_read_only_sqlite_and_caps_at_eight(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "signals.db"
            self.create_signal_database(path)
            before = path.read_bytes()

            text = render_recent_signals(path, limit=999)

            after = path.read_bytes()
            self.assertEqual(before, after)
            self.assertFalse(Path(f"{path}-wal").exists())
            self.assertFalse(Path(f"{path}-shm").exists())

        rows = [line for line in text.splitlines() if line.startswith("• ")]
        self.assertEqual(len(rows), MAX_VIEW_ITEMS)
        self.assertIn("ASSET09USDT", text)
        self.assertNotIn("ASSET00USDT", text)
        self.assertIn("突破确认", text)
        self.assertIn("79分", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("dedup", text)
        self.assertNotIn("123456", text)
        self.assertNotIn("789", text)
        self.assertNotIn("正文", text)

    def test_missing_or_corrupt_signal_database_degrades_without_creation(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / f"{SECRET}.db"
            missing_text = render_recent_signals(missing)
            self.assertFalse(missing.exists())
            self.assertFalse(Path(f"{missing}-wal").exists())

            corrupt = Path(tmp) / "corrupt.db"
            corrupt.write_bytes(b"not a sqlite database: raw provider error")
            before = corrupt.read_bytes()
            corrupt_text = render_recent_signals(corrupt)
            self.assertEqual(corrupt.read_bytes(), before)
            self.assertFalse(Path(f"{corrupt}-wal").exists())

        self.assertIn("尚未生成", missing_text)
        self.assertIn("暂时无法读取", corrupt_text)
        self.assertNotIn(SECRET, missing_text)
        self.assertNotIn("raw provider error", corrupt_text)

    def test_recent_signal_reader_data_is_strictly_allowlisted(self) -> None:
        text = render_recent_signals(
            {
                "items": [
                    {
                        "ts": 1_700_000_000,
                        "template_id": "TG_FLOW_RADAR",
                        "symbol": "BTCUSDT",
                        "stage": [SECRET],
                        "score": float("nan"),
                        "status": [SECRET],
                        "title": SECRET,
                        "text_html": SECRET,
                        "payload": {"path": SECRET},
                        "dedup_key": SECRET,
                        "topic_id": SECRET,
                        "message_ids": [987654321],
                    },
                    {
                        "ts": 1_800_000_000,
                        "template_id": SECRET,
                        "symbol": SECRET,
                        "status": "sent",
                    },
                ]
            }
        )

        self.assertEqual(
            len([line for line in text.splitlines() if line.startswith("• ")]),
            1,
        )
        self.assertIn("五因子资金流", text)
        self.assertIn("BTCUSDT", text)
        self.assertIn("状态未知", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("987654321", text)

    def test_push_records_from_json_are_bounded_and_redacted(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "push_history.json"
            history = [
                {
                    "ts": 1_700_000_000 + index,
                    "template_id": "TG_FUNDING_ALERT",
                    "status": "sent",
                    "reason": "telegram_api" if index < 11 else SECRET,
                    "preview": f"正文 {SECRET}",
                    "dedup_key": SECRET,
                    "delivery_id": SECRET,
                    "topic_id": 123456,
                    "message_ids": [789],
                    "reply_to_message_id": 456,
                    "raw_error": SECRET,
                }
                for index in range(12)
            ]
            path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")
            before = path.read_bytes()

            text = render_push_records(path, limit=100)

            self.assertEqual(path.read_bytes(), before)

        rows = [line for line in text.splitlines() if line.startswith("• ")]
        self.assertEqual(len(rows), MAX_VIEW_ITEMS)
        self.assertIn("资金费率警报", text)
        self.assertIn("发送成功", text)
        self.assertIn("详细原因已保留在内部运行记录中", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("正文", text)
        self.assertNotIn("123456", text)
        self.assertNotIn("789", text)
        self.assertNotIn("456", text)

    def test_push_json_missing_corrupt_or_wrong_shape_degrades_safely(self) -> None:
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / f"{SECRET}.json"
            corrupt = Path(tmp) / "corrupt.json"
            corrupt.write_text(f"{{ raw exception: {SECRET}", encoding="utf-8")

            missing_text = render_push_records(missing)
            corrupt_text = render_push_records(corrupt)
            wrong_text = render_push_records({"secret": SECRET})

        self.assertFalse(missing.exists())
        self.assertIn("尚未生成", missing_text)
        self.assertIn("暂时无法读取", corrupt_text)
        self.assertIn("暂时无法读取", wrong_text)
        self.assertNotIn(SECRET, missing_text + corrupt_text + wrong_text)

    def test_unpublished_reasons_filters_success_and_translates_known_codes(self) -> None:
        statuses = [
            ("sent", "telegram_api"),
            ("dry_run", "send_flag_not_set"),
            ("skipped", "dedup_cooldown"),
            ("blocked", "telegram_topic_not_configured"),
            ("failed", "telegram_api_failed"),
            ("partial", SECRET),
            ("uncertain", "telegram_delivery_uncertain"),
        ]
        source = [
            {
                "ts": 1_700_000_000 + index,
                "template_id": "TG_ANNOUNCEMENT_ALERT",
                "status": status,
                "reason": reason,
                "preview": SECRET,
            }
            for index, (status, reason) in enumerate(statuses)
        ]
        source.append(
            {
                "ts": 1_900_000_000,
                "template_id": "TG_ANNOUNCEMENT_ALERT",
                "status": [SECRET],
                "reason": SECRET,
            }
        )

        text = render_unpublished_reasons(source)

        rows = [line for line in text.splitlines() if line.startswith("• ")]
        self.assertEqual(len(rows), 6)
        self.assertNotIn("发送成功", text)
        self.assertIn("安全演练", text)
        self.assertIn("同类内容仍在防重复冷却期内", text)
        self.assertIn("对应的 Telegram 话题尚未配置", text)
        self.assertIn("系统已停止重试", text)
        self.assertIn("详细原因已保留在内部运行记录中", text)
        self.assertNotIn(SECRET, text)

    def test_fault_explanations_ignore_raw_details_and_cap_at_eight(self) -> None:
        health = {
            "status": "failed",
            "checks": [
                {
                    "name": name,
                    "status": "fail" if index % 2 else "warn",
                    "detail": f"raw path C:/private/{SECRET}",
                    "metrics": {
                        "token": SECRET,
                        "chat_id": 123456,
                        "exception": f"RuntimeError: {SECRET}",
                    },
                }
                for index, name in enumerate(
                    (
                        "runtime_status",
                        "signal_store_integrity",
                        "market_snapshots_integrity",
                        "realtime_features_integrity",
                        "market_snapshots_freshness",
                        "realtime_features_freshness",
                        SECRET,
                    )
                )
            ],
        }
        radar = {
            "radars": {
                "launch_alert": {
                    "state": "degraded",
                    "state_reason": SECRET,
                    "last_error_code": SECRET,
                },
                "radar_summary": {"state": "stale", "path": SECRET},
                "funding_alert": {"state": [SECRET]},
            },
            "chat_id": 123456,
        }

        text = render_fault_explanations(health, radar, limit=99)

        rows = [line for line in text.splitlines() if line.startswith("• ")]
        self.assertEqual(len(rows), MAX_VIEW_ITEMS)
        self.assertIn("启动预警：最近一轮未正常完成", text)
        self.assertIn("资金摘要：已经超过计划运行时间", text)
        self.assertIn("主循环没有正常更新", text)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("C:/private", text)
        self.assertNotIn("RuntimeError", text)
        self.assertNotIn("123456", text)

    def test_fault_explanations_have_safe_empty_and_unavailable_states(self) -> None:
        healthy = render_fault_explanations(
            {"checks": [{"name": "runtime_status", "status": "ok", "detail": SECRET}]},
            {"radars": {"launch_alert": {"state": "running", "error": SECRET}}},
        )
        with TemporaryDirectory() as tmp:
            missing = Path(tmp) / f"{SECRET}.json"
            unavailable = render_fault_explanations(missing)

        self.assertIn("当前没有需要处理", healthy)
        self.assertIn("暂时无法读取", unavailable)
        self.assertNotIn(SECRET, healthy + unavailable)


if __name__ == "__main__":
    unittest.main()
