from __future__ import annotations

import os
import subprocess
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from shared.storage import JsonStore
from shared.telegram import (
    PRODUCTION_TOPIC_TEMPLATE_IDS,
    TOPIC_TEMPLATE_NAMES,
    topic_intro_message,
)
from shared.time_windows import closed_window


RADAR_MODULES = {
    "radars.announcement_risk.radar": "AnnouncementRiskRadar",
    "radars.capital_flow.radar": "FlowRadarEngine",
    "radars.funding_alert.radar": "FundingAlertEngine",
    "radars.pulse.radar": "PulseRadar",
    "radars.market_summary.radar": "MarketSummaryRadar",
}


class FakeAnnouncementSource:
    def __init__(self) -> None:
        self.network_calls = 0

    def announcements(self, page_size: int = 50) -> list[dict[str, object]]:
        return [{
            "title": "Binance Will Delist ABC",
            "code": "official-risk-abc",
            "releaseDate": int(time.time() * 1000),
        }][:page_size]

    def usdt_perp_symbols(self) -> list[dict[str, str]]:
        return [{"symbol": "ABCUSDT"}]


class FailingAnnouncementSource:
    def announcements(self, page_size: int = 50) -> list[dict[str, object]]:
        raise TimeoutError("private provider detail")


class FakeSummarySource:
    def usdt_perp_symbols(self) -> list[dict[str, object]]:
        return [{"symbol": "BTCUSDT", "onboardDate": 0}]

    def ticker_24h(self) -> list[dict[str, object]]:
        return [{
            "symbol": "BTCUSDT",
            "quoteVolume": "100000000",
            "lastPrice": "1",
            "priceChangePercent": "1",
        }]

    def premium_index(self) -> list[dict[str, object]]:
        return [{"symbol": "BTCUSDT", "lastFundingRate": "0.0001"}]

    def market_caps(self) -> dict[str, float]:
        return {}


class RadarPackageSplitTests(unittest.TestCase):
    def test_five_production_topics_include_announcement_risk(self) -> None:
        self.assertEqual(len(PRODUCTION_TOPIC_TEMPLATE_IDS), 5)
        self.assertIn("TG_ANNOUNCEMENT_ALERT", PRODUCTION_TOPIC_TEMPLATE_IDS)
        self.assertEqual(
            TOPIC_TEMPLATE_NAMES["TG_ANNOUNCEMENT_ALERT"],
            "公告风险",
        )
        self.assertIn(
            "Binance 官方公告",
            topic_intro_message("TG_ANNOUNCEMENT_ALERT", Settings()),
        )

    def test_market_summary_has_its_own_shared_symbol_filter(self) -> None:
        from radars.market_summary.radar import MarketSummaryRadar

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            radar = MarketSummaryRadar(
                Settings(data_dir=root, excluded_base_assets=("BTC",)),
                JsonStore(root),
            )

            self.assertTrue(radar._is_excluded_symbol("BTCUSDT"))
            self.assertFalse(radar._is_excluded_symbol("ETHUSDT"))
            self.assertEqual(
                radar._load_market_items(  # type: ignore[arg-type]
                    FakeSummarySource(),
                    closed_window(),
                ),
                [],
            )

    def test_each_radar_module_imports_without_loading_peer_radars(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        peer_modules = tuple(RADAR_MODULES)
        script = "\n".join([
            "import importlib",
            "import sys",
            "target, expected, *peers = sys.argv[1:]",
            "module = importlib.import_module(target)",
            "if not hasattr(module, expected):",
            "    raise AssertionError(f'{target} does not expose {expected}')",
            "loaded = sorted(name for name in peers if name != target and name in sys.modules)",
            "if loaded:",
            "    raise AssertionError(f'{target} eagerly loaded peer radars: {loaded}')",
        ])
        environment = dict(os.environ)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"

        for module_name, class_name in RADAR_MODULES.items():
            with self.subTest(module=module_name):
                completed = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        script,
                        module_name,
                        class_name,
                        *peer_modules,
                    ],
                    cwd=repository,
                    env=environment,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=20,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=completed.stderr or completed.stdout,
                )

    def test_canonical_engine_imports_and_old_package_is_removed(self) -> None:
        from radars.capital_flow.radar import FlowRadarEngine
        from radars.funding_alert.radar import FundingAlertEngine
        from runtime.radar_engine import RadarEngine

        self.assertTrue(callable(FlowRadarEngine))
        self.assertTrue(callable(FundingAlertEngine))
        self.assertTrue(callable(RadarEngine))
        repository = Path(__file__).resolve().parents[2]
        self.assertFalse((repository / "paopao_radar").exists())

    def test_announcement_risk_message_and_sent_only_dedup_preserve_evidence(self) -> None:
        from radars.announcement_risk.radar import (
            AnnouncementRiskRadar,
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                announcement_state_path=root / "announcement_state.json",
            )
            store = JsonStore(root)
            radar = AnnouncementRiskRadar(settings, store)
            source = FakeAnnouncementSource()

            first = radar.build_announcement_alerts(source)  # type: ignore[arg-type]
            self.assertEqual(first["template_id"], "TG_ANNOUNCEMENT_ALERT")
            self.assertEqual(len(first["alerts"]), 1)
            self.assertEqual(first["alerts"][0]["kind"], "risk")
            self.assertIn("official-risk-abc", first["messages"][0])
            self.assertIn("https://www.binance.com/", first["messages"][0])

            # A dry-run/failed delivery must not be treated as sent: until the
            # caller explicitly marks a successful delivery, the alert remains.
            before_sent = radar.build_announcement_alerts(source)  # type: ignore[arg-type]
            self.assertEqual(len(before_sent["alerts"]), 1)

            sent_alert = dict(first["alerts"][0])
            sent_alert["message_ids"] = [101]
            radar.mark_announcements_seen([sent_alert])

            after_sent = radar.build_announcement_alerts(source)  # type: ignore[arg-type]
            self.assertEqual(after_sent["alerts"], [])
            self.assertEqual(after_sent["messages"], [])

            state = store.load(settings.announcement_state_path, {})
            self.assertIn("official-risk-abc", state["seen"])
            self.assertEqual(
                state["seen"]["official-risk-abc"]["message_ids"],
                [101],
            )
            self.assertIn("ABCUSDT", state["evidence_by_symbol"])
            self.assertEqual(source.network_calls, 0)

    def test_announcement_provider_failure_is_safe_and_has_no_messages(self) -> None:
        from radars.announcement_risk.radar import (
            AnnouncementRiskRadar,
        )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            radar = AnnouncementRiskRadar(
                Settings(data_dir=root),
                JsonStore(root),
            )
            result = radar.build_announcement_alerts(  # type: ignore[arg-type]
                FailingAnnouncementSource()
            )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["error"], "TimeoutError")
        self.assertEqual(result["messages"], [])
        self.assertNotIn("private provider detail", str(result))


if __name__ == "__main__":
    unittest.main()
