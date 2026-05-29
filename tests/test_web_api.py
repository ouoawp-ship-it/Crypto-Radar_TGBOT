from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from paopao_radar.config import Settings
from paopao_radar.launch_score import build_mock_payload, save_launch_payload
from paopao_radar.storage import JsonStore
from paopao_radar.web_api import create_app


class WebApiTests(unittest.TestCase):
    def make_app(self, tmp: str, default_mode: str = "mock") -> TestClient:
        base = Path(tmp)
        settings = Settings(
            base_dir=base,
            data_dir=base,
            launch_radar_latest_path=base / "launch_radar_latest.json",
            oi_divergence_latest_path=base / "oi_divergence_latest.json",
            wash_risk_latest_path=base / "wash_risk_latest.json",
            signal_history_path=base / "signal_history.json",
            launch_web_mode=default_mode,
        )
        return TestClient(create_app(settings=settings, store=JsonStore(base), default_mode=default_mode))

    def test_launch_radar_mock_api_returns_items_and_stats(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_app(tmp)
            response = client.get("/api/launch-radar?mode=mock")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["items"])
        self.assertIn("stats", data)
        self.assertEqual(data["mock"], True)

    def test_launch_radar_filters_by_level_and_min_score(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_app(tmp)
            response = client.get("/api/launch-radar?mode=mock&level=A&min_score=70")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["items"])
        self.assertTrue(all(item["level"] == "A" and item["score"] >= 70 for item in data["items"]))

    def test_launch_radar_page_renders_no_secret_frontend(self) -> None:
        with TemporaryDirectory() as tmp:
            client = self.make_app(tmp)
            response = client.get("/launch-radar")

        self.assertEqual(response.status_code, 200)
        text = response.text
        self.assertIn("山寨币启动雷达", text)
        self.assertIn("暂无数据", text)
        self.assertNotIn("COINGLASS_API_KEY", text)
        self.assertNotIn("TG_BOT_TOKEN", text)

    def test_scan_failure_returns_last_success_with_stale_flag(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            settings = Settings(
                base_dir=base,
                data_dir=base,
                launch_radar_latest_path=base / "launch_radar_latest.json",
                oi_divergence_latest_path=base / "oi_divergence_latest.json",
                wash_risk_latest_path=base / "wash_risk_latest.json",
                signal_history_path=base / "signal_history.json",
                launch_web_mode="real",
            )
            store = JsonStore(base)
            save_launch_payload(settings, store, build_mock_payload())
            client = TestClient(create_app(settings=settings, store=store, default_mode="real"))

            with patch("paopao_radar.web_api.build_launch_radar_payload", side_effect=RuntimeError("source down")):
                response = client.get("/api/launch-radar?mode=real")

        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["stale"])
        self.assertTrue(data["items"])
        self.assertIn("source down", data["error"])


if __name__ == "__main__":
    unittest.main()
