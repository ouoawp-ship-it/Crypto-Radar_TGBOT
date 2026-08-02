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

    def test_report_explains_contract_transfers_evidence_and_group_score(self) -> None:
        payload = {
            "report": {
                "rule_summary": {
                    "token": {
                        "symbol": "cbDOGE",
                        "contract": "0xcbD06E5A2B0C65597161de254AA074E489dEb510",
                    },
                    "query": {"window": "15m", "complete": True},
                    "transfer_summary": {
                        "transfer_count": 13,
                        "total_token_amount": "27258.7541876",
                        "unique_senders": 11,
                        "unique_receivers": 10,
                    },
                    "cex_flows": {},
                    "primary_behavior": {
                        "type": "wallet_consolidation_candidate",
                        "label": "多钱包归集候选",
                        "score": 70,
                        "confidence_level": "medium",
                        "supporting_evidence": [
                            "token_amount_share_met",
                            "transaction_count_met",
                            "wallet_count_met",
                        ],
                    },
                    "wallet_groups": [
                        {
                            "group_type": "shared_target",
                            "level": "中等概率关联",
                            "score": 45,
                            "supporting_evidence": [
                                "repeated_shared_target",
                                "time_synchronized",
                            ],
                        }
                    ],
                },
                "ai": {"status": "not_requested", "result": None},
            }
        }
        text = format_token_report(payload)
        contract = "0xcbD06E5A2B0C65597161de254AA074E489dEb510"
        self.assertIn(f"https://basescan.org/token/{contract}", text)
        self.assertIn(f">{contract}</a>", text)
        self.assertIn("Transfer（转账记录）", text)
        self.assertIn("本窗口已读取的 ERC-20 转账笔数", text)
        self.assertIn("代币转账总量：27258.7541876 cbDOGE", text)
        self.assertIn("不是成交额、净流入或买卖量", text)
        self.assertIn("证据强度：中等", text)
        self.assertIn("参与的不同钱包数量达到规则门槛（+30分）", text)
        self.assertIn("符合该行为的转账笔数达到规则门槛（+20分）", text)
        self.assertIn("该行为涉及的代币数量占比达到规则门槛（+20分）", text)
        self.assertIn("共同收款地址 · 中等关联候选 · 45分", text)
        self.assertIn("多个钱包共同转入同一地址 +30分", text)
        self.assertIn("转账时间接近 +15分", text)
        self.assertIn("不能确认这些钱包属于同一主体", text)
        self.assertNotIn("token_amount_share_met", text)
        self.assertNotIn("shared_target", text)
        self.assertNotIn("not_requested", text)

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

    def test_report_makes_cex_label_coverage_explicit(self) -> None:
        report = TokenReportService(
            self.settings, None
        ).build_from_analysis(
            self.analyzed,
            with_ai=False,
            linked_market_signals=[],
        )
        coverage = report["report"]["rule_summary"]["label_coverage"]
        self.assertEqual(coverage["status"], "ok")
        self.assertEqual(coverage["classification_eligible_cex_count"], 1)
        self.assertIn("交易所标签覆盖：就绪", format_token_report(report))

        insufficient = deepcopy(self.analyzed)
        insufficient["labels"]["status"] = "insufficient_cex_coverage"
        insufficient["labels"]["classification_eligible_cex_count"] = 0
        insufficient["summary"]["inflow_count"] = 0
        insufficient["summary"]["outflow_count"] = 0
        insufficient_report = TokenReportService(
            self.settings, None
        ).build_from_analysis(
            insufficient,
            with_ai=False,
            linked_market_signals=[],
        )
        text = format_token_report(insufficient_report)
        self.assertIn("交易所标签覆盖：不足", text)
        self.assertIn("0 流入/0 提出，不代表已经确认没有入所或提币", text)

    def test_ai_context_contains_only_safe_label_coverage_counts(self) -> None:
        payload = deepcopy(self.analyzed)
        payload["labels"] = {
            "status": "insufficient_cex_coverage",
            "identity_label_count": 7,
            "classification_eligible_cex_count": 0,
            "private_path": "C:/secret/labels.csv",
        }
        context = build_ai_context(payload, max_chars=30000)
        self.assertEqual(
            context["label_coverage"],
            {
                "status": "insufficient_cex_coverage",
                "identity_label_count": 7,
                "classification_eligible_cex_count": 0,
            },
        )
        self.assertNotIn("private_path", json.dumps(context))

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
