from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.agent_intelligence import AgentInsightStore, build_agent_overview
from paopao_radar.config import Settings


class AgentIntelligenceTest(unittest.TestCase):
    now = 1_720_000_300

    def cockpit(self, *, status: str = "ready") -> dict[str, object]:
        asset = {
            "price": 65_000,
            "price_change_pct": 2.2,
            "oi_change_pct": 3.1,
            "spot_flow_usd": 8_000_000,
            "futures_flow_usd": 12_000_000,
            "funding_pct": 0.02,
            "status": "fresh",
            "updated_at": "2024-07-03T09:51:40Z",
        }
        return {
            "schema_version": "2026-07-17",
            "generated_at": "2024-07-03T09:51:40Z",
            "data_status": status,
            "coverage": {"assets": 80, "price": 80, "oi": 30, "spot_flow": 20, "futures_flow": 20},
            "overview": {
                "breadth_pct": 25,
                "spot_net_flow_usd": 20_000_000,
                "futures_net_flow_usd": 30_000_000,
                "bias": "inflow",
            },
            "assets": [
                {"symbol": "BTCUSDT", **asset},
                {"symbol": "ETHUSDT", **asset, "price": 3500, "price_change_pct": -1.0, "spot_flow_usd": -2_000_000},
            ],
        }

    def signals(self) -> list[dict[str, object]]:
        return [{
            "id": 7,
            "public_ref": "sig_1234567890abcdefabcd",
            "ts": self.now - 60,
            "time": "2024-07-03T09:50:40Z",
            "symbol": "BTCUSDT",
            "module": "flow",
            "status": "sent",
            "signal_type": "资金流异常",
            "title": "BTC 资金流异常",
        }]

    def news(self) -> list[dict[str, object]]:
        return [{
            "event_id": "binance_event",
            "published_at": "2024-07-03T09:40:00Z",
            "source": "Binance",
            "title": "Binance Will List ABC",
            "importance": "high",
            "symbols": ["ABCUSDT"],
            "data_status": "ready",
            "url": "https://www.binance.com/en/support/announcement/example",
            "rights_status": "official_link_only",
        }]

    def test_ready_directional_insights_have_resolvable_ready_evidence(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            result = build_agent_overview(
                settings,
                now_ts=self.now,
                cockpit=self.cockpit(),
                signals=self.signals(),
                news_items=self.news(),
            )

        evidence = {item["ref"]: item for item in result["evidence"]}
        insights = [result["agents"]["global"], *result["agents"]["majors"], *result["agents"]["anomalies"], *result["agents"]["messages"]]
        for insight in insights:
            for ref in insight["evidence_refs"]:
                self.assertIn(ref, evidence)
                self.assertEqual(evidence[ref]["data_status"], "ready")
        self.assertEqual(result["agents"]["global"]["state"], "strengthening")
        self.assertTrue(result["safety"]["ready_only_for_direction"])
        self.assertFalse(result["model_info"]["llm_generated"])
        self.assertIn("不构成投资建议", result["safety"]["disclaimer"])

    def test_degraded_market_trips_safety_gate(self) -> None:
        cockpit = self.cockpit(status="degraded")
        cockpit["overview"]["spot_net_flow_usd"] = None  # type: ignore[index]
        cockpit["assets"][0]["funding_pct"] = None  # type: ignore[index]
        with TemporaryDirectory() as tmp:
            result = build_agent_overview(
                Settings(data_dir=Path(tmp)), now_ts=self.now, cockpit=cockpit,
                signals=[], news_items=[],
            )

        self.assertEqual(result["agents"]["global"]["state"], "insufficient_data")
        self.assertEqual(result["agents"]["majors"][0]["data_status"], "degraded")
        self.assertIn("未生成方向结论", result["agents"]["majors"][0]["summary"])

    def test_store_expires_and_internal_contract_is_versioned(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            built = build_agent_overview(
                settings, now_ts=self.now, cockpit=self.cockpit(), signals=self.signals(), news_items=self.news(),
            )
            store = AgentInsightStore(settings.agent_insights_db_path)
            self.assertGreater(len(store.list_latest(now_ts=self.now)), 0)
            self.assertEqual(store.list_latest(now_ts=self.now + 1000), [])
        self.assertEqual(built["schema_version"], "2026-07-17")
        self.assertGreater(built["coverage"]["evidence"], 0)


if __name__ == "__main__":
    unittest.main()
