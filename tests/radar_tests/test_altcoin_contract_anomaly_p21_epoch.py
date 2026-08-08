from __future__ import annotations

import unittest
from types import SimpleNamespace

from radars.altcoin_contract_anomaly.realtime import (
    AltcoinRealtimeController,
    ClosedRealtimeFeatureBuilder,
)


NOW = 1_800_000_000


class FakeFeatureStore:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows

    def recent_rows(self, **_kwargs: object) -> list[dict[str, object]]:
        return list(self.rows)


def minute_row(start: int) -> dict[str, object]:
    return {
        "exchange": "binance",
        "market": "futures",
        "symbol": "TESTUSDT",
        "bucket_start": start,
        "bucket_sec": 60,
        "trade_buy_usd": 60.0,
        "trade_sell_usd": 40.0,
        "cvd_usd": 20.0,
        "trade_count": 2,
        "price_open": 100.0,
        "price_high": 101.0,
        "price_low": 99.0,
        "price_close": 100.5,
        "long_liquidation_usd": 10.0,
        "short_liquidation_usd": 20.0,
    }


def feature_settings() -> SimpleNamespace:
    return SimpleNamespace(
        altcoin_contract_anomaly_feature_1m_window_sec=60,
        altcoin_contract_anomaly_feature_5m_window_sec=300,
        altcoin_contract_anomaly_volume_baseline_buckets=5,
        altcoin_contract_anomaly_volume_min_samples=5,
        altcoin_contract_anomaly_volume_min_coverage=1.0,
        altcoin_contract_anomaly_realtime_data_max_age_sec=180,
    )


class CandidateEpochFeatureGateTests(unittest.TestCase):
    def test_global_freshness_without_candidate_epoch_is_rejected(self) -> None:
        controller = object.__new__(AltcoinRealtimeController)
        controller.settings = feature_settings()
        controller.manifest_consumer = SimpleNamespace(
            last_valid=SimpleNamespace(symbols=("TESTUSDT",)),
        )
        status = {
            "connected": True,
            # This can be refreshed by another Symbol and must not establish
            # freshness for TESTUSDT.
            "last_receive_ms": NOW * 1_000,
            "active_candidate_symbols": ["TESTUSDT"],
            "candidate_coverage_complete": True,
            "force_order_active": True,
            "subscription_generation": 5,
            "candidate_epochs": {},
        }

        ready, quality, generation, epochs = controller._subscription_gate(
            status,
            now_ts=NOW,
        )

        self.assertFalse(ready)
        self.assertEqual(quality, "subscription_degraded")
        self.assertEqual(generation, 5)
        self.assertEqual(epochs, {})

    def test_closed_feature_history_before_current_epoch_is_ignored(self) -> None:
        cutoff = NOW - 600
        old_rows = [minute_row(start) for start in range(cutoff - 600, cutoff, 60)]
        epoch = {
            "TESTUSDT": {
                "epoch_id": "session:2:1",
                "activated_at_ms": cutoff * 1_000,
                "eligible_1m_bucket_start_ms": cutoff * 1_000,
                "eligible_5m_boundary_ms": cutoff * 1_000,
            }
        }
        old = ClosedRealtimeFeatureBuilder(
            feature_settings(),
            FakeFeatureStore(old_rows),
        ).build_many(
            ["TESTUSDT"],
            now_ts=cutoff + 60,
            candidate_epochs=epoch,
        )["TESTUSDT"]

        current_rows = [minute_row(start) for start in range(cutoff, cutoff + 660, 60)]
        current = ClosedRealtimeFeatureBuilder(
            feature_settings(),
            FakeFeatureStore(current_rows),
        ).build_many(
            ["TESTUSDT"],
            now_ts=cutoff + 660,
            candidate_epochs=epoch,
        )["TESTUSDT"]

        self.assertEqual(old["data_quality"], "insufficient_history")
        self.assertIn("closed_1m", old["missing_fields"])
        self.assertEqual(current["data_quality"], "complete")
        self.assertEqual(current["subscription_epoch"], "session:2:1")
        self.assertGreaterEqual(
            int(current["candidate_epoch_activated_at_ms"]),
            cutoff * 1_000,
        )

    def test_old_buckets_cannot_complete_current_epoch_five_minute_or_baseline(self) -> None:
        cutoff = NOW - 60
        old_rows = [minute_row(start) for start in range(cutoff - 600, cutoff, 60)]
        mixed_rows = [*old_rows, minute_row(cutoff)]
        epoch = {
            "TESTUSDT": {
                "epoch_id": "session:3:1",
                "activated_at_ms": cutoff * 1_000,
                "eligible_1m_bucket_start_ms": cutoff * 1_000,
                "eligible_5m_boundary_ms": cutoff * 1_000,
            }
        }

        row = ClosedRealtimeFeatureBuilder(
            feature_settings(),
            FakeFeatureStore(mixed_rows),
        ).build_many(
            ["TESTUSDT"],
            now_ts=NOW,
            candidate_epochs=epoch,
        )["TESTUSDT"]

        self.assertEqual(row["data_quality"], "insufficient_history")
        self.assertIn("closed_5m", row["missing_fields"])
        self.assertIn("volume_baseline", row["missing_fields"])


if __name__ == "__main__":
    unittest.main()
