from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from main import build_launch_report, format_launch_report, format_observe_report
from storage import JsonStore


class LaunchReportTests(unittest.TestCase):
    def test_launch_report_summarizes_scores_and_buckets(self) -> None:
        settings = Settings(base_dir=Path("."), data_dir=Path("data"))
        report = build_launch_report([
            {
                "top_score": 20,
                "scanned": 2,
                "alert_count": 0,
                "buckets": {"idle": 2, "watching": 0},
                "top_symbols": ["BTCUSDT", "ETHUSDT"],
            },
            {
                "top_score": 60,
                "scanned": 2,
                "alert_count": 1,
                "buckets": {"idle": 1, "primed": 1},
                "top_symbols": ["ETHUSDT", "BTCUSDT"],
            },
        ], settings)

        self.assertEqual(report["records"], 2)
        self.assertEqual(report["total_scanned"], 4)
        self.assertEqual(report["total_alerts"], 1)
        self.assertEqual(report["max_top_score"], 60)
        self.assertEqual(report["avg_top_score"], 40)
        self.assertEqual(report["buckets"]["primed"], 1)
        self.assertEqual(report["top_symbols"][0], ("BTCUSDT", 2))

    def test_launch_report_low_score_suggestion_after_enough_samples(self) -> None:
        settings = Settings(base_dir=Path("."), data_dir=Path("data"), launch_watch_score=45)
        records = [
            {"top_score": 0, "scanned": 2, "alert_count": 0, "buckets": {"idle": 2}, "top_symbols": ["BTCUSDT"]}
            for _ in range(5)
        ]

        report = build_launch_report(records, settings)

        self.assertIn("无需下调", report["suggestion"])

    def test_launch_report_ignores_excluded_symbols_in_frequency(self) -> None:
        settings = Settings(
            base_dir=Path("."),
            data_dir=Path("data"),
            excluded_base_assets=("XAU", "XAG"),
        )
        report = build_launch_report([
            {
                "top_score": 10,
                "scanned": 3,
                "alert_count": 0,
                "buckets": {"idle": 3},
                "top_symbols": ["XAUUSDT", "BTCUSDT", "XAGUSDT"],
            }
        ], settings)

        self.assertEqual(report["top_symbols"], [("BTCUSDT", 1)])

    def test_format_launch_report_handles_empty_history(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                base_dir=Path(tmp),
                data_dir=Path(tmp),
                launch_watch_history_path=Path(tmp) / "launch_watch_history.json",
            )
            store = JsonStore(Path(tmp))

            text = format_launch_report(settings, store, record_limit=10, top_n=5)

            self.assertIn("暂无启动观察历史", text)

    def test_format_observe_report_includes_session_status(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                base_dir=Path(tmp),
                data_dir=Path(tmp),
                launch_watch_history_path=Path(tmp) / "launch_watch_history.json",
            )
            store = JsonStore(Path(tmp))

            text = format_observe_report(
                settings,
                store,
                record_limit=10,
                top_n=5,
                started_at="2026-05-25 19:00:00",
                cycles=2,
                failures=1,
                status="running",
                last_error="Timeout",
            )

            self.assertIn("状态: running", text)
            self.assertIn("已跑轮数: 2", text)
            self.assertIn("错误次数: 1", text)
            self.assertIn("最近错误: Timeout", text)
            self.assertIn("dry-run", text)


if __name__ == "__main__":
    unittest.main()
