from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.funding_alert import FundingAlertEngine, classify_funding_alert
from paopao_radar.storage import JsonStore


CST = timezone(timedelta(hours=8))


def ms_at(hour: int) -> int:
    return int(datetime(2026, 7, 1, hour, 0, 0, tzinfo=CST).timestamp() * 1000)


class FundingHttp:
    def __init__(self, bitget_rate: str = "-0.006") -> None:
        self.bitget_rate = bitget_rate

    def get_json(self, url: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        if "premiumIndex" in url:
            return {"symbol": "TESTUSDT", "lastFundingRate": "-0.0200", "nextFundingTime": ms_at(17)}
        if "fapi/v1/fundingRate" in url:
            return [
                {"fundingTime": ms_at(8), "fundingRate": "-0.001"},
                {"fundingTime": ms_at(12), "fundingRate": "-0.002"},
                {"fundingTime": ms_at(16), "fundingRate": "-0.004"},
            ]
        if "okx.com" in url and "funding-rate-history" not in url:
            return {
                "data": [{
                    "fundingRate": "-0.0100",
                    "prevFundingTime": str(ms_at(16)),
                    "fundingTime": str(ms_at(17)),
                }]
            }
        if "okx.com" in url:
            return {"data": [{"fundingTime": str(ms_at(16)), "fundingRate": "-0.004"}]}
        if "bybit.com" in url and "tickers" in url:
            return {"result": {"list": [{"fundingRate": "0.0001", "nextFundingTime": str(ms_at(17)), "fundingIntervalHour": "1"}]}}
        if "bybit.com" in url:
            return {"result": {"list": [{"fundingRateTimestamp": str(ms_at(16)), "fundingRate": "0.0001"}]}}
        if "current-fund-rate" in url:
            return {"data": [{"fundingRate": self.bitget_rate, "fundingRateInterval": "1", "nextUpdate": str(ms_at(17))}]}
        if "history-fund-rate" in url:
            return {"data": [{"fundingTime": str(ms_at(16)), "fundingRate": self.bitget_rate}]}
        if "contracts/TEST_USDT" in url:
            return {"funding_rate": "0.0001", "funding_interval": 3600, "funding_next_apply": int(ms_at(17) / 1000)}
        if "funding_rate" in url:
            return [{"t": int(ms_at(16) / 1000), "r": "0.0001"}]
        return {}


class FundingSource:
    def __init__(self, http: FundingHttp) -> None:
        self.http = http

    @staticmethod
    def ticker_24h() -> list[dict[str, str]]:
        return [{"symbol": "TESTUSDT", "quoteVolume": "100000000"}]


class FundingAlertTests(unittest.TestCase):
    def test_classifies_multi_exchange_negative_funding(self) -> None:
        settings = Settings(
            funding_alert_extreme_negative_pct=-0.5,
            funding_alert_min_exchange_count=2,
        )
        result = classify_funding_alert([
            {"exchange": "Binance", "funding_pct": -2.0},
            {"exchange": "OKX", "funding_pct": -1.0},
            {"exchange": "Bybit", "funding_pct": 0.01},
        ], settings)

        self.assertEqual(result["primary_kind"], "multi_negative")
        self.assertEqual(result["risk"], "极高")
        self.assertIn("多所极负共振", result["types"])

    def test_build_pushes_multi_exchange_negative_alert(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=Path(tmp) / "funding_alert_state.json",
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BINANCE", "OKX", "BYBIT"),
                funding_alert_min_exchange_count=2,
                funding_alert_cooldown_sec=3600,
            )
            store = JsonStore(Path(tmp))

            result = FundingAlertEngine(settings, store).build(FundingSource(FundingHttp()))  # type: ignore[arg-type]

            self.assertEqual(result["template_id"], "TG_FUNDING_ALERT")
            self.assertEqual(len(result["alerts"]), 1)
            self.assertIn("多所极负共振", result["messages"][0])
            self.assertIn("Binance: -2.000%/1H（极负）", result["messages"][0])

    def test_previous_state_detects_interval_shortening(self) -> None:
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "funding_alert_state.json"
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=state_path,
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BITGET",),
                funding_alert_extreme_negative_pct=-5.0,
                funding_alert_extreme_positive_pct=5.0,
                funding_alert_divergence_pct=99.0,
            )
            store = JsonStore(Path(tmp))
            store.save(state_path, {
                "symbols": {
                    "TESTUSDT": {
                        "exchanges": {
                            "Bitget": {
                                "interval_hours": 4,
                                "next_funding_time_ms": ms_at(16),
                                "next_funding_time": "2026-07-01 16:00:00",
                            }
                        }
                    }
                },
                "last_alerts": {},
            })

            result = FundingAlertEngine(settings, store).build(FundingSource(FundingHttp(bitget_rate="-0.001")))  # type: ignore[arg-type]

            self.assertEqual(len(result["alerts"]), 1)
            self.assertIn("结算周期缩短", result["messages"][0])
            self.assertIn("4H结算一次", result["messages"][0])
            self.assertIn("1H结算一次", result["messages"][0])


if __name__ == "__main__":
    unittest.main()

