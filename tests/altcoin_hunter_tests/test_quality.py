import unittest

from radars.altcoin_hunter.quality import QualityTracker
from tests.altcoin_hunter_tests.test_aggregation import START

CONTEXT = {"source": "test", "exchange": "binance", "market": "futures", "instrument_id": "AAA"}


class QualityTests(unittest.TestCase):
    def test_source_rollup_and_instrument_counts_are_not_double_counted(self):
        quality = QualityTracker()
        quality.record(CONTEXT, "duplicate_events", observed_ms=START, amount=2)
        rows = quality.prepare(START + 60_000)
        self.assertEqual({row.instrument_id for row in rows}, {"AAA", "*"})
        self.assertTrue(all(dict(row.counters)["duplicate_events"] == 2 for row in rows))
        self.assertEqual(quality.stats()["duplicate_events"], 2)

    def test_status_changes_are_compressed_and_hard_bounded(self):
        quality = QualityTracker(max_status_changes=2)
        quality.status(CONTEXT, "partial", "gap", observed_ms=START)
        quality.status(CONTEXT, "partial", "gap", observed_ms=START + 1)
        quality.status(CONTEXT, "complete", "", observed_ms=START + 2)
        quality.status(CONTEXT, "partial", "missing", observed_ms=START + 3)
        row = quality.prepare(START + 60_000)[0]
        self.assertEqual(len(row.status_changes), 2)
        self.assertEqual(dict(row.counters)["status_changes_truncated"], 1)

    def test_rollback_retains_rollups_and_ack_only_subtracts_frozen_counts(self):
        quality = QualityTracker()
        quality.record(CONTEXT, "late_events", observed_ms=START)
        frozen = quality.prepare(START + 60_000)
        quality.record(CONTEXT, "late_events", observed_ms=START + 10)
        self.assertEqual(dict(frozen[0].counters)["late_events"], 1)
        quality.acknowledge(frozen)
        self.assertEqual(quality.stats()["late_events"], 1)
        quality.acknowledge(quality.prepare(START + 60_000))
        self.assertEqual(quality.stats()["open_quality_rollups"], 0)

    def test_capacity_is_explicit_without_unbounded_new_series(self):
        quality = QualityTracker(max_rollups=2)
        for index in range(100):
            quality.record({**CONTEXT, "instrument_id": str(index)}, "accepted_events", observed_ms=START)
        self.assertEqual(quality.stats()["open_quality_rollups"], 2)
        self.assertGreater(quality.stats()["quality_overflow"], 0)

    def test_no_health_row_until_observation_occurs(self):
        self.assertEqual(QualityTracker().prepare(START + 60_000), ())

    def test_queue_and_checkpoint_gauges_take_max_instead_of_sum(self):
        quality = QualityTracker()
        for queue, lag in ((4, 2000), (9, 1000), (2, 3000)):
            quality.record(CONTEXT, "health_observations", observed_ms=START, queue_depth=queue, checkpoint_lag_ms=lag)
        for row in quality.prepare(START + 60_000):
            self.assertEqual(row.max_queue_depth, 9)
            self.assertEqual(row.max_checkpoint_lag_ms, 3000)
            self.assertEqual(row.to_dict()["max_checkpoint_lag_ms"], 3000)

    def test_timestamps_and_latency_reject_bool_float_and_seconds(self):
        quality = QualityTracker()
        for observed in (True, float(START), START // 1000):
            with self.subTest(observed=observed), self.assertRaises(ValueError):
                quality.record(CONTEXT, "accepted_events", observed_ms=observed)
            with self.assertRaises(ValueError):
                quality.status(CONTEXT, "partial", "gap", observed_ms=observed)
        for value in (True, 0.5, -1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                quality.record(CONTEXT, "accepted_events", observed_ms=START, processing_latency_ms=value)


if __name__ == "__main__":
    unittest.main()
