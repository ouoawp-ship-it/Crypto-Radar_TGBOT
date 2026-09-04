from pathlib import Path
import tempfile
import unittest

from radars.altcoin_hunter.aggregation import BoundedMinuteAggregator
from radars.altcoin_hunter.storage import HunterWriter, migrate
from radars.altcoin_hunter.windows import RollingWindowEngine, WINDOW_MINUTES
from tests.altcoin_hunter_tests.test_aggregation import START, cover, trade


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

    def load(self, engine, minutes, *, skip=(), split_epoch=None):
        aggregator = BoundedMinuteAggregator()
        for minute in range(minutes):
            if minute in skip:
                continue
            epoch = 1 if split_epoch is not None and minute >= split_epoch else 0
            cover(aggregator, start=START + minute * 60_000, epoch=epoch)
            aggregator.ingest(trade(minute + 1, minute=minute, epoch=epoch, price=str(10 + minute)))
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
        self.assertIsNone(result["quote_volume"])
        self.assertIsNone(result["delta_quote"])

    def test_epoch_boundary_remains_incomplete(self):
        engine = self.load(RollingWindowEngine(), 5, split_epoch=3)
        result = self.query(engine, 5, 5)
        self.assertFalse(result["complete"])
        self.assertIn("connection_epoch_changed", result["quality_flags"])

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
