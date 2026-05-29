from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.launch_score import build_item, build_mock_payload, save_launch_payload
from paopao_radar.storage import JsonStore


class LaunchScoreTests(unittest.TestCase):
    def make_settings(self, tmp: str) -> Settings:
        base = Path(tmp)
        return Settings(
            base_dir=base,
            data_dir=base,
            launch_radar_latest_path=base / "launch_radar_latest.json",
            oi_divergence_latest_path=base / "oi_divergence_latest.json",
            wash_risk_latest_path=base / "wash_risk_latest.json",
            signal_history_path=base / "signal_history.json",
            launch_watch_history_limit=20,
        )

    def test_launch_score_classifies_short_squeeze_fuel(self) -> None:
        item = build_item({
            "rank": 1,
            "symbol": "FIDAUSDT",
            "oi_change_pct": 18.5,
            "price_change_pct": 2.1,
            "divergence_ratio": 8.8,
            "funding_rate": -0.0041,
            "taker_buy_sell_ratio": 1.18,
            "long_short_ratio": 0.82,
            "oi_marketcap_ratio": 0.242,
            "volume_ratio": 2.6,
            "cross_exchange_confirmed": True,
        })

        self.assertEqual(item["signal_type"], "SHORT_SQUEEZE_FUEL")
        self.assertIn(item["level"], {"A", "S"})
        self.assertGreaterEqual(item["score"], 75)

    def test_save_launch_payload_writes_all_web_state_files(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.make_settings(tmp)
            store = JsonStore(Path(tmp))
            payload = build_mock_payload()

            save_launch_payload(settings, store, payload)

            self.assertTrue(settings.launch_radar_latest_path.exists())
            self.assertTrue(settings.oi_divergence_latest_path.exists())
            self.assertTrue(settings.wash_risk_latest_path.exists())
            self.assertTrue(settings.signal_history_path.exists())
            self.assertTrue(store.load(settings.launch_radar_latest_path, {})["items"])
            self.assertTrue(store.load(settings.signal_history_path, []))


if __name__ == "__main__":
    unittest.main()
