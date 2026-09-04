from pathlib import Path
import tempfile
import unittest

from radars.altcoin_hunter.aggregation import BoundedMinuteAggregator
from radars.altcoin_hunter.storage import HunterWriter, migrate
from radars.altcoin_hunter.windows import RollingWindowEngine, WINDOW_MINUTES
from tests.altcoin_hunter_tests.test_aggregation import START, cover, trade


ANALYTICAL_FIELDS = (
    "price_open", "price_high", "price_low", "price_close", "price_return_ratio",
    "buy_quote", "sell_quote", "quote_volume", "delta_quote", "taker_buy_ratio",
    "delta_ratio", "trade_count",
)


class WindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        path = Path(self.directory.name) / "hunter.db"
        migrate(path)
        self.writer = HunterWriter(path).open()
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.writer.close)

    def query(self, engine, minutes, end_minutes):
        return engine.query(source="binance_agg_trade", exchange="binance", market="futures",
                            instrument_id="AAAUSDT", end_ms=START + end_minutes * 60_000,
                            window_minutes=minutes)

    def load(self, engine, minutes, *, skip=(), split_epoch=None, coverage_ms=None):
        aggregator = BoundedMinuteAggregator()
        sequence = 0
        for minute in range(minutes):
            if minute in skip:
                continue
            epoch = 1 if split_epoch is not None and minute >= split_epoch else 0
            if coverage_ms is None:
                cover(aggregator, start=START + minute * 60_000, epoch=epoch)
            else:
                start = START + minute * 60_000
                aggregator.note_connection(source="binance_agg_trade", exchange="binance", market="futures",
                    instrument_id="AAAUSDT", connection_epoch=epoch,
                    start_ms=start, end_ms=start + coverage_ms[minute])
            # A missing minute is separate from a missing source sequence. Keep
            # sequence contiguous across admitted events so skip isolates time.
            sequence += 1
            aggregator.ingest(trade(minute + 1, minute=minute, epoch=epoch,
                sequence=sequence, price=str(10 + minute)))
            pending = aggregator.prepare(START + (minute + 1) * 60_000 + 2000)
            committed = self.writer.commit_batch(pending)
            engine.ingest_committed(committed)
            aggregator.acknowledge(pending.batch_id)
        return engine

    def test_all_six_windows_from_real_committed_receipts(self):
        engine = self.load(RollingWindowEngine(), 60)
        for minutes in WINDOW_MINUTES:
            with self.subTest(minutes=minutes):
                result = self.query(engine, minutes, 60)
                self.assertTrue(result["complete"])
                self.assertEqual(result["observed_minutes"], minutes)
                self.assertEqual(result["price_close"], "69")

    def test_five_complete_minutes_report_full_observed_and_time_coverage(self):
        engine = self.load(RollingWindowEngine(), 5)
        result = self.query(engine, 5, 5)
        self.assertTrue(result["complete"])
        self.assertEqual(result["observed_minutes"], 5)
        self.assertEqual(result["expected_minutes"], 5)
        self.assertEqual(result["observed_minute_ratio"], 1)
        self.assertEqual(result["observed_coverage_ms"], 300_000)
        self.assertEqual(result["expected_coverage_ms"], 300_000)
        self.assertEqual(result["time_coverage_ratio"], 1)
        self.assertEqual(result["complete_minutes"], 5)
        self.assertEqual(result["incomplete_minutes"], 0)
        self.assertNotIn("coverage_ratio", result)

    def test_five_ten_second_buckets_do_not_report_full_time_coverage(self):
        engine = self.load(RollingWindowEngine(), 5, coverage_ms=(10_000,) * 5)
        result = self.query(engine, 5, 5)
        self.assertFalse(result["complete"])
        self.assertEqual(result["observed_minutes"], 5)
        self.assertEqual(result["expected_minutes"], 5)
        self.assertEqual(result["observed_minute_ratio"], 1)
        self.assertEqual(result["observed_coverage_ms"], 50_000)
        self.assertEqual(result["expected_coverage_ms"], 300_000)
        self.assertAlmostEqual(result["time_coverage_ratio"], 1 / 6)
        self.assertEqual(result["complete_minutes"], 0)
        self.assertEqual(result["incomplete_minutes"], 5)
        for field in ANALYTICAL_FIELDS:
            with self.subTest(field=field):
                self.assertIsNone(result[field])
        self.assertNotIn("coverage_ratio", result)

    def test_complete_and_partial_minutes_have_distinct_counts_and_time_ratio(self):
        engine = self.load(RollingWindowEngine(), 5, coverage_ms=(60_000, 10_000, 60_000, 20_000, 60_000))
        result = self.query(engine, 5, 5)
        self.assertFalse(result["complete"])
        self.assertEqual(result["observed_minute_ratio"], 1)
        self.assertEqual(result["observed_coverage_ms"], 210_000)
        self.assertEqual(result["expected_coverage_ms"], 300_000)
        self.assertEqual(result["time_coverage_ratio"], 0.7)
        self.assertEqual(result["complete_minutes"], 3)
        self.assertEqual(result["incomplete_minutes"], 2)
        for field in ANALYTICAL_FIELDS:
            with self.subTest(field=field):
                self.assertIsNone(result[field])

    def test_uncommitted_batch_cannot_enter_window(self):
        engine = RollingWindowEngine()
        aggregator = BoundedMinuteAggregator()
        aggregator.ingest(trade())
        with self.assertRaises(TypeError):
            engine.ingest_committed(aggregator.prepare(START + 62_000))

    def test_missing_minute_never_becomes_zero_or_complete(self):
        engine = self.load(RollingWindowEngine(), 5, skip=(2,))
        result = self.query(engine, 5, 5)
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_minutes"], (START + 120_000,))
        self.assertEqual(result["observed_minutes"], 4)
        self.assertEqual(result["expected_minutes"], 5)
        self.assertEqual(result["observed_minute_ratio"], 0.8)
        self.assertEqual(result["observed_coverage_ms"], 240_000)
        self.assertEqual(result["expected_coverage_ms"], 300_000)
        self.assertEqual(result["time_coverage_ratio"], 0.8)
        self.assertEqual(result["complete_minutes"], 4)
        self.assertEqual(result["incomplete_minutes"], 0)
        self.assertIsNone(result["quote_volume"])
        self.assertIsNone(result["delta_quote"])

    def test_epoch_boundary_remains_incomplete(self):
        engine = self.load(RollingWindowEngine(), 5, split_epoch=3)
        result = self.query(engine, 5, 5)
        self.assertFalse(result["complete"])
        self.assertIn("connection_epoch_changed", result["quality_flags"])
        self.assertEqual(result["observed_minute_ratio"], 1)
        self.assertEqual(result["time_coverage_ratio"], 1)
        self.assertEqual(result["complete_minutes"], 4)
        self.assertEqual(result["incomplete_minutes"], 1)
        for field in ANALYTICAL_FIELDS:
            with self.subTest(field=field):
                self.assertIsNone(result[field])

    def test_future_buckets_do_not_leak_into_earlier_window(self):
        engine = self.load(RollingWindowEngine(), 5)
        result = self.query(engine, 1, 2)
        self.assertTrue(result["complete"])
        self.assertEqual(result["price_close"], "11")

    def test_retention_is_hard_bounded_at_120_minutes(self):
        engine = self.load(RollingWindowEngine(), 125)
        self.assertEqual(engine.stats()["minute_buckets"], 120)
        self.assertFalse(self.query(engine, 1, 1)["complete"])

    def test_malformed_anchor_or_unknown_window_rejected(self):
        engine = RollingWindowEngine()
        with self.assertRaises(ValueError):
            self.query(engine, 2, 1)
        with self.assertRaises(ValueError):
            engine.query(source="s", exchange="e", market="m", instrument_id="i", end_ms=True, window_minutes=1)

    def test_finite_buckets_whose_window_overflows_analytical_float_degrade(self):
        aggregator = BoundedMinuteAggregator()
        engine = RollingWindowEngine()
        for minute in range(3):
            cover(aggregator, start=START + minute * 60_000)
            aggregator.ingest(trade(minute + 1, minute=minute, price="1e308", quantity="1"))
            pending = aggregator.prepare(START + (minute + 1) * 60_000 + 2000)
            engine.ingest_committed(self.writer.commit_batch(pending))
            aggregator.acknowledge(pending.batch_id)
        result = self.query(engine, 3, 3)
        self.assertFalse(result["complete"])
        self.assertIn("numeric_overflow", result["quality_flags"])
        self.assertIsNone(result["quote_volume"])


if __name__ == "__main__":
    unittest.main()
