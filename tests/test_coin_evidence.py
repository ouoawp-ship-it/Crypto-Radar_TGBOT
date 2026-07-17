from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.coin_evidence import build_kline_chart, build_snapshot_series
from paopao_radar.config import Settings
from paopao_radar.market_cockpit import MarketSnapshotStore
from paopao_radar.web_services.public import public_coin_context_payload


def sample_kline(ts: int, open_price: float, close_price: float) -> list[object]:
    return [
        ts,
        str(open_price),
        str(max(open_price, close_price) * 1.01),
        str(min(open_price, close_price) * 0.99),
        str(close_price),
        "100",
        ts + 899_999,
        "1000000",
        100,
        "50",
        "550000",
        "0",
    ]


class CoinEvidenceTests(unittest.TestCase):
    @staticmethod
    def snapshot(_settings: Settings, symbol: str) -> dict[str, object]:
        return {
            "symbol": symbol,
            "coin": symbol[:-4],
            "updated_at": 4_600,
            "price": 110,
            "price_24h_pct": 10,
            "quote_volume": 100_000_000,
            "oi_value": 1_100_000,
            "funding_pct": 0.01,
        }

    @staticmethod
    def chart(_settings: Settings, _symbol: str, market: str, interval: str, bars: int) -> list[list[object]]:
        assert market == "futures"
        assert interval == "15m"
        return [sample_kline(1_000_000 + index * 900_000, 100 + index, 101 + index) for index in range(bars)]

    def test_kline_chart_is_bounded_and_keeps_source_metadata(self) -> None:
        payload = build_kline_chart(
            [sample_kline(1_000_000 + index * 900_000, 100, 101) for index in range(300)],
            market_type="spot",
            interval="15m",
            requested=96,
        )

        self.assertEqual(payload["source"], "binance_spot_klines")
        self.assertEqual(payload["coverage"], {"requested": 96, "returned": 96})
        self.assertEqual(len(payload["points"]), 96)
        self.assertEqual(payload["points"][0]["taker_buy_quote_volume"], 550_000)

    def test_snapshot_series_marks_partial_coverage_as_degraded(self) -> None:
        series = build_snapshot_series([
            {"observed_at": 1_000, "updated_at": "a", "price": 100, "sources": ["batch"]},
            {"observed_at": 2_000, "updated_at": "b", "price": 101, "sources": ["batch"]},
        ])

        self.assertEqual(series["data_status"], "degraded")
        self.assertEqual(series["coverage"]["price"], 2)
        self.assertEqual(series["coverage"]["spot_flow"], 0)
        self.assertTrue(series["warnings"])

    def test_coin_context_combines_chart_snapshot_series_and_provenance(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_db_path=Path(tmp) / "signals.db",
                market_snapshots_db_path=Path(tmp) / "market.db",
                ai_bot_username="paopao_ai_bot",
            )
            store = MarketSnapshotStore(settings.market_snapshots_db_path)
            store.append_many([
                {
                    "symbol": "BTCUSDT", "observed_at": 1_000, "source": "flow_radar",
                    "price": 100, "oi_usd": 1_000_000,
                    "spot_inflow_usd": 600_000, "spot_outflow_usd": 500_000,
                    "spot_flow_usd": 100_000, "futures_flow_usd": 80_000,
                    "funding_pct": 0.01,
                },
                {
                    "symbol": "BTCUSDT", "observed_at": 4_600, "source": "flow_radar",
                    "price": 110, "oi_usd": 1_100_000,
                    "spot_inflow_usd": 700_000, "spot_outflow_usd": 520_000,
                    "spot_flow_usd": 180_000, "futures_flow_usd": 120_000,
                    "funding_pct": 0.015,
                },
            ])
            payload = public_coin_context_payload(
                "BTC",
                settings=settings,
                snapshot_loader=self.snapshot,
                chart_loader=self.chart,
                market_type="futures",
                interval="15m",
                bars=48,
                now_ts=4_600,
            )

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertEqual(data["chart"]["coverage"], {"requested": 48, "returned": 48})
        self.assertEqual(data["series"]["coverage"]["points"], 2)
        self.assertEqual(data["series"]["points"][-1]["spot_flow_usd"], 180_000)
        self.assertEqual(data["evidence_coverage"]["chart_points"], 48)
        self.assertIn("share_url", data["actions"])
        serialized = json.dumps(payload, ensure_ascii=False).lower()
        for forbidden in ("api_key", "bot_token", "password", "coverage_json"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
