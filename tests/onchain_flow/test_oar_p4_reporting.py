from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from copy import deepcopy
from io import StringIO
from pathlib import Path

from paopao_radar.onchain_flow.ai_context import build_ai_context
from paopao_radar.onchain_flow.constants import (
    OAR_AI_CONTEXT_SCHEMA_VERSION,
    OAR_AI_PROMPT_VERSION,
)
from paopao_radar.onchain_flow.report import TokenReportService
from paopao_radar.onchain_flow.report_formatter import format_token_report
from paopao_radar.onchain_flow.report_notifier import ReportNotifier
from paopao_radar.onchain_flow.token_analysis import TokenAnalysisService

from tests.onchain_flow.analysis_support import fixture_case
from tests.onchain_flow.support import make_settings


class StaticActivity:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def execute(self, query: object) -> dict[str, object]:
        del query
        return deepcopy(self.payload)


class OarP4ReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.analyzed = TokenAnalysisService(
            self.settings,
            StaticActivity(fixture_case("accumulation")),
        ).execute(object())

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def sources(count: int = 12) -> list[dict[str, object]]:
        return [
            {
                "public_ref": f"ref-{index}",
                "module": "launch" if index % 2 else "flow",
                "symbol": "TSTUSDT",
                "score": 70 + index,
                "stage": "active",
                "severity": "high",
                "ts": 1_700_000_000 - index,
                "age_sec": index * 60,
                "summary": f"summary {index}",
                "_priority": 100 - index,
            }
            for index in range(count)
        ]

    def test_linked_context_is_bounded_stable_and_versioned(self) -> None:
        first_payload = deepcopy(self.analyzed)
        first_payload["linked_market_signals"] = self.sources()
        second_payload = deepcopy(self.analyzed)
        second_payload["linked_market_signals"] = list(
            reversed(self.sources())
        )
        first = build_ai_context(first_payload, max_chars=30000)
        second = build_ai_context(second_payload, max_chars=30000)
        self.assertEqual(OAR_AI_CONTEXT_SCHEMA_VERSION, 2)
        self.assertEqual(OAR_AI_PROMPT_VERSION, "oar-ai-prompt-v3")
        self.assertEqual(first["context_hash"], second["context_hash"])
        self.assertEqual(len(first["linked_market_signals"]), 10)
        serialized = json.dumps(first, ensure_ascii=False)
        self.assertNotIn("_priority", serialized)
        self.assertNotIn("text_html", serialized)
        self.assertNotIn("message_id", serialized)
        self.assertNotIn("topic_id", serialized)

    def test_new_linked_source_changes_context_hash(self) -> None:
        payload = deepcopy(self.analyzed)
        payload["linked_market_signals"] = self.sources(1)
        first = build_ai_context(payload, max_chars=30000)
        payload["linked_market_signals"] = self.sources(2)
        second = build_ai_context(payload, max_chars=30000)
        self.assertNotEqual(first["context_hash"], second["context_hash"])

    def test_report_shows_only_three_linked_market_signals(self) -> None:
        report = TokenReportService(
            self.settings, None
        ).build_from_analysis(
            self.analyzed,
            with_ai=False,
            linked_market_signals=self.sources(),
        )
        linked = report["report"]["rule_summary"]["linked_market_signals"]
        self.assertEqual(len(linked), 3)
        text = format_token_report(report)
        self.assertIn("关联市场信号", text)
        self.assertLessEqual(text.count("分钟前"), 3)

    def test_manual_report_without_sources_remains_available(self) -> None:
        report = TokenReportService(
            self.settings, None
        ).build_from_analysis(
            self.analyzed,
            with_ai=False,
            linked_market_signals=[],
        )
        self.assertEqual(
            report["report"]["ai_context"]["linked_market_signals"], []
        )
        self.assertEqual(
            report["report"]["rule_summary"]["linked_market_signals"], []
        )

    def test_automation_signal_links_stay_in_independent_store(self) -> None:
        report = TokenReportService(
            self.settings, None
        ).build_from_analysis(
            self.analyzed,
            with_ai=False,
            linked_market_signals=self.sources(2),
        )
        with redirect_stdout(StringIO()):
            result = ReportNotifier(self.settings).notify(
                report,
                send=False,
                confirm_real_send=False,
                source="watchlist_automation",
                linked_source_refs=["ref-1", "ref-0"],
                linked_source_modules=["launch", "flow"],
                watch_priority=90,
                watch_reason_count=2,
            )
        self.assertEqual(result.status, "dry_run")
        self.assertFalse(self.settings.main_signal_db_path.exists())
        with closing(
            sqlite3.connect(self.settings.signal_events_db_path)
        ) as conn:
            payload_raw = conn.execute(
                "SELECT payload_json FROM signals WHERE module='onchain'"
            ).fetchone()[0]
        facts = json.loads(payload_raw)["facts"]
        self.assertEqual(
            facts["linked_source_refs"], ["ref-0", "ref-1"]
        )
        self.assertEqual(facts["watch_priority"], 90)
        history = json.loads(
            self.settings.tg_push_history_path.read_text(encoding="utf-8")
        )
        audit = history[-1]["signal_records"][0]
        self.assertEqual(audit["linked_source_count"], 2)
        self.assertEqual(audit["source_modules_text"], "flow,launch")
        self.assertNotIn("linked_source_refs", audit)


if __name__ == "__main__":
    unittest.main()
