import unittest
from unittest.mock import patch

from radars.altcoin_hunter.quality import HealthRollup, QualityTracker
from tests.altcoin_hunter_tests.test_aggregation import START

CONTEXT = {"source": "test", "exchange": "binance", "market": "futures", "instrument_id": "AAA"}


class QualityTests(unittest.TestCase):
    def test_prepared_generation_does_not_leak_old_gauges_into_new_observations(self):
        for instrument in ("*", "AAA"):
            for newer in ((20, 30, 2, 100), (1900, 1800, 200, 15000)):
                with self.subTest(instrument=instrument, newer=newer):
                    quality = QualityTracker()
                    context = {**CONTEXT, "instrument_id": instrument}
                    quality.record(context, "accepted_events", observed_ms=START,
                                   processing_latency_ms=900, event_latency_ms=800,
                                   queue_depth=100, checkpoint_lag_ms=5000)
                    frozen = quality.prepare(START + 60_000)
                    quality.record(context, "accepted_events", observed_ms=START + 10,
                                   processing_latency_ms=newer[0], event_latency_ms=newer[1],
                                   queue_depth=newer[2], checkpoint_lag_ms=newer[3])
                    # A real status change makes the instrument observation
                    # persistable even when its new latency is below policy.
                    if instrument != "*":
                        quality.status(context, "partial", "gap", observed_ms=START + 11)
                    quality.acknowledge(frozen)
                    subsequent = quality.prepare(START + 60_000)
                    row = next(row for row in subsequent if row.instrument_id == instrument)
                    self.assertEqual(dict(row.counters)["accepted_events"], 1)
                    self.assertEqual((row.max_processing_latency_ms, row.max_event_latency_ms,
                                      row.max_queue_depth, row.max_checkpoint_lag_ms), newer)

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
        row = next(row for row in quality.prepare(START + 60_000) if row.instrument_id == "AAA")
        self.assertEqual(len(row.status_changes), 2)
        self.assertEqual(dict(row.counters)["status_changes_truncated"], 1)

    def test_rollback_retains_rollups_and_ack_only_consumes_frozen_generation(self):
        quality = QualityTracker()
        quality.record(CONTEXT, "late_events", observed_ms=START)
        frozen = quality.prepare(START + 60_000)
        quality.record(CONTEXT, "late_events", observed_ms=START + 10)
        self.assertEqual(dict(frozen[0].counters)["late_events"], 1)
        quality.acknowledge(frozen)
        self.assertEqual(quality.stats()["late_events"], 1)
        quality.acknowledge(quality.prepare(START + 60_000))
        self.assertEqual(quality.stats()["open_quality_rollups"], 0)

    def test_retry_keeps_snapshot_while_new_counters_and_statuses_are_retained(self):
        for instrument in ("*", "AAA"):
            with self.subTest(instrument=instrument):
                context = {**CONTEXT, "instrument_id": instrument}
                quality = QualityTracker()
                quality.record(context, "late_events", observed_ms=START)
                quality.status(context, "partial", "gap", observed_ms=START + 1)
                frozen = quality.prepare(START + 60_000)
                serialized = [row.to_dict() for row in frozen]
                quality.record(context, "late_events", observed_ms=START + 10)
                quality.status(context, "complete", "", observed_ms=START + 11)
                self.assertIs(quality.prepare(START + 120_000), frozen)
                self.assertEqual([row.to_dict() for row in frozen], serialized)
                self.assertEqual(quality.stats()["late_events"], 2)
                quality.acknowledge(frozen)
                subsequent = quality.prepare(START + 120_000)
                row = next(row for row in subsequent if row.instrument_id == instrument)
                self.assertEqual(dict(row.counters)["late_events"], 1)
                self.assertEqual(row.status_changes, ((START + 11, "complete", ""),))

    def test_stale_empty_snapshot_cannot_acknowledge_a_new_empty_generation(self):
        quality = QualityTracker()
        first = quality.prepare(START + 60_000)
        quality.acknowledge(first)
        second = quality.prepare(START + 60_000)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        with self.assertRaises(ValueError):
            quality.acknowledge(first)
        self.assertIs(quality.prepare(START + 60_000), second)
        quality.acknowledge(second)

    def test_equal_nonempty_generations_have_different_acknowledgement_tokens(self):
        quality = QualityTracker()
        context = {**CONTEXT, "instrument_id": "*"}
        quality.record(context, "accepted_events", observed_ms=START)
        first = quality.prepare(START + 60_000)
        quality.acknowledge(first)
        quality.record(context, "accepted_events", observed_ms=START)
        second = quality.prepare(START + 60_000)
        self.assertEqual(first, second)
        self.assertIsNot(first, second)
        with self.assertRaises(ValueError):
            quality.acknowledge(first)
        self.assertEqual(quality.stats()["accepted_events"], 1)
        quality.acknowledge(second)

    def test_freeze_failure_keeps_active_observations_available_to_retry(self):
        quality = QualityTracker()
        quality.record(CONTEXT, "late_events", observed_ms=START)
        with patch("radars.altcoin_hunter.quality.HealthRollup", side_effect=ValueError("injected freeze failure")):
            with self.assertRaises(ValueError):
                quality.prepare(START + 60_000)
        quality.record(CONTEXT, "late_events", observed_ms=START + 1)
        self.assertEqual(quality.stats()["prepared_quality_rollups"], 0)
        self.assertEqual(quality.stats()["late_events"], 2)
        rows = quality.prepare(START + 60_000)
        self.assertTrue(all(dict(row.counters)["late_events"] == 2 for row in rows))

    def test_routine_instrument_rows_are_filtered_and_acknowledged_without_losing_source_counts(self):
        quality = QualityTracker()
        for minute in range(3):
            observed = START + minute * 60_000
            quality.record(CONTEXT, "accepted_events", observed_ms=observed,
                           amount=7, connection_epoch=0, event_latency_ms=100)
            quality.status(CONTEXT, "complete", "", observed_ms=observed + 1)
            rows = quality.prepare(observed + 60_000)
            self.assertEqual([row.instrument_id for row in rows], ["*"])
            self.assertEqual(dict(rows[0].counters)["accepted_events"], 7)
            self.assertEqual(rows[0].connection_epochs, (0,))
            self.assertEqual(rows[0].status_changes, ())
            self.assertEqual(quality.stats()["prepared_quality_rollups"], 2)
            quality.acknowledge(rows)
            self.assertEqual(quality.stats()["open_quality_rollups"], 0)
        self.assertEqual(quality.stats()["status_identity_count"], 1)

    def test_status_transitions_persist_across_minutes_without_repeating_complete(self):
        quality = QualityTracker()
        states = (("partial", "gap"), ("partial", "gap"), ("complete", ""), ("complete", ""))
        for minute, (status, reason) in enumerate(states):
            quality.status(CONTEXT, status, reason, observed_ms=START + minute * 60_000)
            rows = quality.prepare(START + (minute + 1) * 60_000)
            details = [row for row in rows if row.instrument_id == "AAA"]
            if minute == 3:
                self.assertEqual(details, [])
            else:
                self.assertEqual(len(details), 1)
                self.assertEqual(len(details[0].status_changes), 0 if minute == 1 else 1)
                if minute == 1:
                    self.assertEqual(dict(details[0].counters)["incomplete_observations"], 1)
                if minute == 2:
                    self.assertEqual(details[0].status_changes[0][1], "complete")
            quality.acknowledge(rows)

    def test_instrument_exception_counters_always_persist(self):
        counters = ("duplicate_events", "late_events", "sequence_gaps", "epoch_changes",
                    "local_data_loss", "pending_capacity", "writer_failures")
        for counter in counters:
            with self.subTest(counter=counter):
                quality = QualityTracker()
                quality.record(CONTEXT, counter, observed_ms=START)
                self.assertEqual({row.instrument_id for row in quality.prepare(START + 60_000)}, {"*", "AAA"})

    def test_latency_thresholds_keep_boundary_anomalies_and_filter_routine_details(self):
        for processing, event, expected in ((499, 1999, False), (500, 0, True), (0, 2000, True)):
            with self.subTest(processing=processing, event=event):
                quality = QualityTracker()
                quality.record(CONTEXT, "accepted_events", observed_ms=START,
                               processing_latency_ms=processing, event_latency_ms=event,
                               queue_depth=100, checkpoint_lag_ms=5000)
                rows = quality.prepare(START + 60_000)
                self.assertEqual(any(row.instrument_id == "AAA" for row in rows), expected)
                source = next(row for row in rows if row.instrument_id == "*")
                self.assertEqual((source.max_processing_latency_ms, source.max_event_latency_ms), (processing, event))
                self.assertEqual((source.max_queue_depth, source.max_checkpoint_lag_ms), (100, 5000))

    def test_strict_identity_rejects_invalid_values_in_all_fields_before_mutation(self):
        class StringSubclass(str):
            pass

        invalid = (None, True, 1, 1.5, "", " ", " AAA", "AAA ", "A\nB", "A\x7fB",
                   "A\u0085B", "A\u200bB", "A\u202eB", "x" * 129, StringSubclass("AAA"))
        for name in CONTEXT:
            for value in invalid:
                with self.subTest(name=name, value=value):
                    quality = QualityTracker()
                    context = {**CONTEXT, name: value}
                    with self.assertRaises(ValueError):
                        quality.record(context, "accepted_events", observed_ms=START)
                    with self.assertRaises(ValueError):
                        quality.status(context, "complete", "", observed_ms=START)
                    with self.assertRaises(ValueError):
                        HealthRollup(**context, minute_ms=START)
                    self.assertEqual(quality.stats()["open_quality_rollups"], 0)
                    self.assertEqual(quality.stats()["status_identity_count"], 0)
            missing = {key: value for key, value in CONTEXT.items() if key != name}
            with self.subTest(missing=name), self.assertRaises(ValueError):
                QualityTracker().record(missing, "accepted_events", observed_ms=START)

    def test_connection_epoch_evidence_is_bounded_and_overflow_counts_observations(self):
        quality = QualityTracker(max_connection_epochs=2)
        context = {**CONTEXT, "instrument_id": "*"}
        for epoch in (0, 1, 2, 2):
            quality.record(context, "accepted_events", observed_ms=START, connection_epoch=epoch)
        row = quality.prepare(START + 60_000)[0]
        self.assertEqual(row.connection_epochs, (0, 1))
        self.assertEqual(row.to_dict()["connection_epochs"], [0, 1])
        self.assertEqual(dict(row.counters)["connection_epoch_overflow_observations"], 2)
        self.assertEqual(dict(row.counters)["accepted_events"], 4)
        with self.assertRaises(ValueError):
            QualityTracker(max_connection_epochs=33)
        for epochs in ((True,), (-1,), (1.0,), (1, 1), tuple(range(33))):
            with self.subTest(epochs=epochs), self.assertRaises(ValueError):
                HealthRollup(**context, minute_ms=START, connection_epochs=epochs)

    def test_status_cache_capacity_produces_visible_source_evidence(self):
        quality = QualityTracker(max_status_identities=1)
        quality.status(CONTEXT, "complete", "", observed_ms=START)
        second = {**CONTEXT, "instrument_id": "BBB"}
        quality.status(second, "partial", "gap", observed_ms=START + 1)
        quality.status(second, "complete", "", observed_ms=START + 2)
        source = next(row for row in quality.prepare(START + 60_000) if row.instrument_id == "*")
        self.assertEqual(dict(source.counters)["status_memory_overflow"], 2)
        self.assertEqual(quality.stats()["status_identity_count"], 1)

    def test_prepared_and_active_buffers_have_independent_hard_caps(self):
        quality = QualityTracker(max_rollups=2)
        quality.record(CONTEXT, "late_events", observed_ms=START)
        frozen = quality.prepare(START + 60_000)
        for index in range(10):
            quality.record({**CONTEXT, "instrument_id": str(index)}, "late_events", observed_ms=START + 1)
        self.assertEqual(quality.stats()["open_quality_rollups"], 4)
        self.assertEqual(quality.stats()["late_events"], 11)
        quality.acknowledge(frozen)
        rows = quality.prepare(START + 60_000)
        source = next(row for row in rows if row.instrument_id == "*")
        self.assertEqual(dict(source.counters)["late_events"], 10)
        self.assertEqual(dict(source.counters)["instrument_rollup_overflow"], 9)

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
