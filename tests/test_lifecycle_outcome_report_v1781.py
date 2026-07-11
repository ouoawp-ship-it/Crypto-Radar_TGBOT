from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.lifecycle_intelligence_store import IntelligenceStore
from paopao_radar.lifecycle_outcome_report import (
    build_lifecycle_outcome_coverage_report,
    write_lifecycle_outcome_coverage_report,
)
from paopao_radar.lifecycle_store import LifecycleStore


class LifecycleOutcomeReportTests(unittest.TestCase):
    def test_report_groups_quality_data_and_contains_no_sensitive_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            settings = Settings(
                data_dir=base,
                lifecycle_db_path=base / "lifecycle.db",
                outcome_db_path=base / "outcomes.db",
                signal_events_db_path=base / "signals.db",
            )
            lifecycle_store = LifecycleStore(settings.lifecycle_db_path)
            lifecycle_store.ensure_schema()
            with lifecycle_store.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO signal_lifecycles (
                        id, symbol, first_signal_id, first_signal_at, first_signal_module,
                        first_signal_level, current_state, latest_signal_id, is_active,
                        created_at, updated_at
                    ) VALUES (1, 'BTCUSDT', 11, '2026-07-01T00:00:00+00:00', 'flow',
                              '15m', 'upgraded_1h', 11, 1,
                              '2026-07-01T00:00:00+00:00', '2026-07-01T04:00:00+00:00')
                    """
                )
            store = IntelligenceStore(settings)
            store.ensure_schema()
            with store.transaction() as conn:
                store.upsert_outcome_coverage({
                    "lifecycle_id": 1,
                    "symbol": "BTCUSDT",
                    "candidate_signal_count": 1,
                    "linked_signal_count": 1,
                    "linked_outcome_count": 4,
                    "horizon_1h_status": "success",
                    "horizon_4h_status": "success",
                    "horizon_24h_status": "not_due",
                    "horizon_72h_status": "not_due",
                    "linked_horizon_count": 4,
                    "mature_horizon_count": 2,
                    "link_coverage_ratio": 1.0,
                    "maturity_ratio": 1.0,
                    "coverage_label": "完整关联",
                    "maturity_label": "部分成熟",
                    "reasons": {"reason_counts": {}, "token": "must-not-leak"},
                }, conn=conn)

            report = build_lifecycle_outcome_coverage_report(settings)
            json_path = base / "coverage.json"
            markdown_path = base / "coverage.md"
            write_lifecycle_outcome_coverage_report(
                settings, json_path=json_path, markdown_path=markdown_path,
            )
            serialized = json.dumps(report, ensure_ascii=False)

            self.assertEqual(report["summary"]["lifecycle_count"], 1)
            self.assertEqual(report["by_module"][0]["first_signal_module"], "flow")
            self.assertEqual(report["by_first_signal_level"][0]["first_signal_level"], "15m")
            self.assertNotIn("must-not-leak", serialized)
            self.assertTrue(json_path.exists())
            self.assertIn("关联覆盖率与数据成熟度", markdown_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
