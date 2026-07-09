from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from paopao_radar.config import Settings
from paopao_radar.funding_sources import MultiExchangeFundingClient, funding_interval_transition


CST = timezone(timedelta(hours=8))


def ms_at(hour: int) -> int:
    return int(datetime(2026, 7, 1, hour, 0, 0, tzinfo=CST).timestamp() * 1000)


class FakeHttp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_json(self, url: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        params = dict(params or {})
        self.calls.append((url, params))
        if "premiumIndex" in url:
            return {"symbol": "BTCUSDT", "lastFundingRate": "-0.0200", "nextFundingTime": ms_at(17)}
        if "fapi/v1/fundingRate" in url:
            return [
                {"fundingTime": ms_at(8), "fundingRate": "-0.001"},
                {"fundingTime": ms_at(12), "fundingRate": "-0.002"},
                {"fundingTime": ms_at(16), "fundingRate": "-0.004"},
            ]
        if "okx.com" in url and "funding-rate-history" not in url:
            return {
                "data": [{
                    "instId": "BTC-USDT-SWAP",
                    "fundingRate": "-0.0100",
                    "prevFundingTime": str(ms_at(16)),
                    "fundingTime": str(ms_at(17)),
                }]
            }
        if "okx.com" in url:
            return {"data": [{"fundingTime": str(ms_at(16)), "fundingRate": "-0.004"}]}
        if "bybit.com" in url and "tickers" in url:
            return {
                "result": {
                    "list": [{
                        "symbol": "BTCUSDT",
                        "fundingRate": "-0.006",
                        "nextFundingTime": str(ms_at(17)),
                        "fundingIntervalHour": "1",
                    }]
                }
            }
        if "bybit.com" in url:
            return {
                "result": {
                    "list": [
                        {"fundingRateTimestamp": str(ms_at(16)), "fundingRate": "-0.004"},
                        {"fundingRateTimestamp": str(ms_at(12)), "fundingRate": "-0.002"},
                        {"fundingRateTimestamp": str(ms_at(8)), "fundingRate": "-0.001"},
                    ]
                }
            }
        if "current-fund-rate" in url:
            return {
                "data": [{
                    "symbol": "BTCUSDT",
                    "fundingRate": "-0.005",
                    "fundingRateInterval": "1",
                    "nextUpdate": str(ms_at(17)),
                }]
            }
        if "history-fund-rate" in url:
            return {"data": [{"fundingTime": str(ms_at(16)), "fundingRate": "-0.004"}]}
        if "contracts/BTC_USDT" in url:
            return {
                "name": "BTC_USDT",
                "funding_rate": "-0.003",
                "funding_interval": 3600,
                "funding_next_apply": int(ms_at(17) / 1000),
            }
        if "funding_rate" in url:
            return [{"t": int(ms_at(16) / 1000), "r": "-0.004"}]
        return {}


class FundingSourceTests(unittest.TestCase):
    def test_transition_uses_next_settlement_time(self) -> None:
        transition = funding_interval_transition(
            [
                {"time_ms": ms_at(8), "rate_pct": -0.1},
                {"time_ms": ms_at(12), "rate_pct": -0.2},
                {"time_ms": ms_at(16), "rate_pct": -0.4},
            ],
            next_time_ms=ms_at(17),
        )

        self.assertEqual(transition["previous_interval_hours"], 4)
        self.assertEqual(transition["current_interval_hours"], 1)
        self.assertIn("2026-07-01 16:00:00 4H结算一次", transition["transition_text"])
        self.assertIn("2026-07-01 17:00:00 1H结算一次", transition["transition_text"])

    def test_snapshot_normalizes_five_exchange_funding(self) -> None:
        settings = Settings(
            data_dir=Path("."),
            launch_funding_exchanges=("BINANCE", "OKX", "BYBIT", "BITGET", "GATE"),
            launch_funding_history_limit=3,
        )
        rows = MultiExchangeFundingClient(settings, FakeHttp()).snapshot("BTCUSDT")  # type: ignore[arg-type]

        self.assertEqual([row["exchange"] for row in rows], ["Binance", "OKX", "Bybit", "Bitget", "Gate"])
        self.assertEqual(rows[0]["funding_pct"], -2.0)
        self.assertEqual(rows[0]["interval_hours"], 1)
        self.assertEqual(rows[0]["last_funding_time"], "2026-07-01 16:00:00")
        self.assertEqual(rows[0]["current_interval_hours"], 1)
        self.assertEqual(rows[0]["previous_interval_hours"], 4)
        self.assertEqual(rows[0]["extreme_label"], "极负")
        self.assertIn("4H结算一次", rows[0]["funding_interval_transition"])
        self.assertTrue(all(row["next_funding_time"].endswith("17:00:00") for row in rows))


if __name__ == "__main__":
    unittest.main()
