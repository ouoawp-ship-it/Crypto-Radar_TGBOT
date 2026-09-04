from __future__ import annotations

from dataclasses import replace
from contextlib import closing
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from radars.altcoin_hunter.aggregation import BoundedMinuteAggregator, MinuteBucket
from radars.altcoin_hunter.models import (BookTickerPayload, FundingPayload, LiquidationPayload,
                                         MarketEvent, MarkPricePayload, OpenInterestPayload,
                                         TradeEvent, TradePayload)
from radars.altcoin_hunter.storage import HunterWriter, migrate

START = 1_783_036_800_000


def trade(number: int = 1, *, minute: int = 0, offset: int = 1000, epoch: int = 0,
          symbol: str = "AAAUSDT", price: str = "10", quantity: str = "2",
          sell: bool = False, receive_offset: int = 100, sequence: int | None = None) -> TradeEvent:
    at = START + minute * 60_000 + offset
    return TradeEvent(source="binance_agg_trade", exchange="binance", market="futures",
                      instrument_id=symbol, symbol=symbol, exchange_symbol=symbol,
                      event_time_ms=at, receive_time_ms=at + receive_offset,
                      receive_monotonic_ns=(at + receive_offset) * 1_000_000,
                      source_event_id=str(number), sequence_start=number if sequence is None else sequence,
                      sequence_end=number if sequence is None else sequence, connection_epoch=epoch,
                      payload=TradePayload(price, quantity, sell))


def cover(aggregator: BoundedMinuteAggregator, *, start: int = START, minutes: int = 1,
          symbol: str = "AAAUSDT", epoch: int = 0) -> None:
    aggregator.note_connection(source="binance_agg_trade", exchange="binance", market="futures",
                               instrument_id=symbol, connection_epoch=epoch,
                               start_ms=start, end_ms=start + minutes * 60_000)


def typed_event(event_type, payload, *, minute=0, flags=()):
    exemplar = trade(minute + 1, minute=minute, offset=3000)
    return MarketEvent(source=exemplar.source, exchange=exemplar.exchange, market=exemplar.market,
                       instrument_id=exemplar.instrument_id, symbol=exemplar.symbol,
                       exchange_symbol=exemplar.exchange_symbol, event_type=event_type,
                       event_time_ms=exemplar.event_time_ms, receive_time_ms=exemplar.receive_time_ms,
                       receive_monotonic_ns=exemplar.receive_monotonic_ns,
                       source_event_id=exemplar.source_event_id, payload=payload, quality_flags=flags)


class AggregationTests(unittest.TestCase):
    def test_out_of_order_within_grace_uses_event_time_and_exact_amounts(self):
        aggregator = BoundedMinuteAggregator()
        cover(aggregator)
        for event in (trade(3, offset=5000, price="12", sell=True), trade(1, price="10"),
                      trade(2, offset=3000, price="11")):
            self.assertTrue(aggregator.ingest(event))
        bucket = aggregator.prepare(START + 62_000).buckets[0]
        self.assertEqual((bucket.price_open, bucket.price_high, bucket.price_low, bucket.price_close), ("10", "12", "10", "12"))
        self.assertEqual((Decimal(bucket.buy_quote), Decimal(bucket.sell_quote), Decimal(bucket.delta_quote)), (42, 24, 18))
        self.assertEqual(Decimal(bucket.base_quantity), 6)
        self.assertTrue(bucket.complete)
        self.assertEqual(bucket.gap_count, 0)

    def test_same_millisecond_ohlc_uses_numeric_sequence_not_lexical_id(self):
        aggregator = BoundedMinuteAggregator()
        for number in (10, 2, 9):
            aggregator.ingest(trade(number, price=str(number)))
        bucket = aggregator.prepare(START + 62_000).buckets[0]
        self.assertEqual((bucket.price_open, bucket.price_close), ("2", "10"))
        self.assertEqual((bucket.first_source_event_id, bucket.last_source_event_id), ("2", "10"))

    def test_gap_classified_only_after_grace_and_reorder_can_fill(self):
        aggregator = BoundedMinuteAggregator()
        cover(aggregator)
        aggregator.ingest(trade(1))
        aggregator.ingest(trade(3, offset=3000))
        self.assertIsNone(aggregator.prepare(START + 61_999))
        aggregator.ingest(trade(2, offset=2000, receive_offset=59_500))
        bucket = aggregator.prepare(START + 62_000).buckets[0]
        self.assertTrue(bucket.complete)
        self.assertEqual(bucket.late_count, 1)

    def test_unfilled_sequence_gap_marks_incomplete(self):
        aggregator = BoundedMinuteAggregator()
        cover(aggregator)
        aggregator.ingest(trade(1))
        aggregator.ingest(trade(4, offset=3000))
        bucket = aggregator.prepare(START + 62_000).buckets[0]
        self.assertEqual(bucket.gap_count, 2)
        self.assertIn("sequence_gap", bucket.quality_flags)

    def test_event_existence_is_not_liveness(self):
        aggregator = BoundedMinuteAggregator()
        aggregator.ingest(trade())
        bucket = aggregator.prepare(START + 62_000).buckets[0]
        self.assertFalse(bucket.complete)
        self.assertEqual(bucket.coverage_ms, 0)

    def test_duplicate_before_close_counted_in_bucket_and_never_in_notional(self):
        aggregator = BoundedMinuteAggregator()
        cover(aggregator)
        self.assertTrue(aggregator.ingest(trade()))
        self.assertFalse(aggregator.ingest(trade()))
        bucket = aggregator.prepare(START + 62_000).buckets[0]
        self.assertEqual(bucket.duplicate_count, 1)
        self.assertEqual(bucket.trade_count, 1)
        self.assertEqual(Decimal(bucket.quote_volume), 20)

    def test_missing_sequence_is_not_complete(self):
        aggregator = BoundedMinuteAggregator()
        cover(aggregator)
        aggregator.ingest(replace(trade(), sequence_start=None, sequence_end=None))
        self.assertIn("sequence_unavailable", aggregator.prepare(START + 62_000).buckets[0].quality_flags)

    def test_epoch_change_cannot_make_a_complete_minute(self):
        aggregator = BoundedMinuteAggregator()
        cover(aggregator)
        cover(aggregator, epoch=1)
        aggregator.ingest(trade(1))
        aggregator.ingest(trade(2, offset=4000, epoch=1))
        bucket = aggregator.prepare(START + 62_000).buckets[0]
        self.assertEqual(bucket.connection_epochs, (0, 1))
        self.assertEqual(bucket.connection_epoch, -1)
        self.assertFalse(bucket.complete)

    def test_commit_failure_keeps_identical_batch_then_real_commit_is_idempotent(self):
        aggregator = BoundedMinuteAggregator()
        cover(aggregator)
        aggregator.ingest(trade())
        pending = aggregator.prepare(START + 62_000)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            with HunterWriter(path) as writer:
                with patch.object(writer, "_commit_transaction", side_effect=RuntimeError("injected commit failure")):
                    with self.assertRaises(RuntimeError):
                        writer.commit_batch(pending)
                self.assertIs(aggregator.prepare(START + 63_000), pending)
                self.assertEqual(aggregator.stats()["pending_events"], 1)
                committed = writer.commit_batch(pending)
                self.assertEqual(committed.receipt.bucket_count, 1)
                self.assertTrue(writer.commit_batch(pending).receipt.already_committed)
                aggregator.acknowledge(pending.batch_id)
                self.assertEqual(aggregator.stats()["pending_events"], 0)

    def test_unknown_ack_does_not_release_batch(self):
        aggregator = BoundedMinuteAggregator()
        aggregator.ingest(trade())
        pending = aggregator.prepare(START + 62_000)
        with self.assertRaises(ValueError):
            aggregator.acknowledge("wrong")
        self.assertIs(aggregator.prepare(START + 63_000), pending)

    def test_restart_watermark_rejects_old_buckets(self):
        first = BoundedMinuteAggregator()
        first.ingest(trade())
        pending = first.prepare(START + 62_000)
        restored = BoundedMinuteAggregator()
        restored.restore_checkpoints(pending.checkpoints)
        self.assertFalse(restored.ingest(trade(2)))
        self.assertTrue(restored.ingest(trade(3, minute=1)))
        self.assertEqual(restored.stats()["late_events"], 1)

    def test_future_event_does_not_allocate_instrument_or_bucket(self):
        aggregator = BoundedMinuteAggregator(max_future_skew_ms=2000)
        event = replace(trade(), receive_time_ms=START, receive_monotonic_ns=START * 1_000_000,
                        event_time_ms=START + 5000)
        self.assertFalse(aggregator.ingest(event))
        self.assertEqual(aggregator.stats()["open_buckets"], 0)
        self.assertEqual(aggregator.stats()["instrument_count"], 0)

    def test_capacity_rejects_without_evicting_uncommitted_ids(self):
        aggregator = BoundedMinuteAggregator(max_event_ids=2, max_pending_events=2)
        aggregator.ingest(trade(1))
        aggregator.ingest(trade(2, offset=2000))
        self.assertFalse(aggregator.ingest(trade(3, offset=3000)))
        self.assertEqual(aggregator.stats()["retained_event_ids"], 2)
        self.assertEqual(aggregator.stats()["pending_events"], 2)
        self.assertFalse(aggregator.ingest(trade(1)))

    def test_dropped_tail_from_local_capacity_cannot_claim_complete_bucket(self):
        for kwargs in ({"max_pending_events": 2}, {"max_event_ids": 2}):
            aggregator = BoundedMinuteAggregator(**kwargs)
            cover(aggregator)
            aggregator.ingest(trade(1))
            aggregator.ingest(trade(2, offset=2000))
            self.assertFalse(aggregator.ingest(trade(3, offset=3000)))
            bucket = aggregator.prepare(START + 62_000).buckets[0]
            self.assertFalse(bucket.complete)
            self.assertIn("local_data_loss", bucket.quality_flags)

    def test_default_accepts_1000_instruments_and_bounds_later_admission(self):
        aggregator = BoundedMinuteAggregator(max_instruments=1000)
        for index in range(1000):
            self.assertTrue(aggregator.ingest(trade(symbol=f"A{index}USDT")))
        self.assertFalse(aggregator.ingest(trade(symbol="EXTRAUSDT")))
        self.assertEqual(aggregator.stats()["instrument_count"], 1000)

    def test_no_zero_fill_for_empty_minute(self):
        aggregator = BoundedMinuteAggregator()
        cover(aggregator, minutes=3)
        aggregator.ingest(trade(1))
        aggregator.ingest(trade(2, minute=2))
        pending = aggregator.prepare(START + 182_000)
        self.assertEqual([b.start_ms for b in pending.buckets], [START, START + 120_000])

    def test_sealed_minute_late_event_cannot_change_pending_fingerprint(self):
        aggregator = BoundedMinuteAggregator()
        aggregator.ingest(trade(1))
        pending = aggregator.prepare(START + 62_000)
        before = pending.to_dict()
        self.assertFalse(aggregator.ingest(trade(2, offset=1500, receive_offset=62_000)))
        self.assertEqual(pending.to_dict(), before)
        self.assertEqual(pending.buckets[0].late_count, 0)
        self.assertEqual(aggregator.stats()["late_events"], 1)

    def test_serialized_derived_flags_and_nonfinite_amounts_validated(self):
        aggregator = BoundedMinuteAggregator()
        aggregator.ingest(trade())
        bucket = aggregator.prepare(START + 62_000).buckets[0]
        self.assertEqual(MinuteBucket.from_dict(bucket.to_dict()), bucket)
        for key, value in (("quote_volume", "NaN"), ("complete", True), ("duplicate_count", True),
                           ("coverage_ratio", 1.5), ("base_quantity", "Infinity")):
            row = bucket.to_dict()
            row[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                MinuteBucket.from_dict(row)

    def test_event_and_processing_latency_are_separate(self):
        aggregator = BoundedMinuteAggregator()
        aggregator.ingest(trade(), processing_time_ms=START + 1150)
        pending = aggregator.prepare(START + 62_000)
        row = next(row for row in pending.health_rollups if row.instrument_id == "AAAUSDT")
        self.assertEqual(row.max_event_latency_ms, 100)
        self.assertEqual(row.max_processing_latency_ms, 50)

    def test_healthy_minutes_persist_source_counts_and_epoch_without_per_instrument_rows(self):
        aggregator = BoundedMinuteAggregator()
        for minute in range(2):
            cover(aggregator, start=START + minute * 60_000)
            aggregator.ingest(trade(minute + 1, minute=minute, offset=3000))
            pending = aggregator.prepare(START + (minute + 1) * 60_000 + 2000)
            self.assertTrue(pending.buckets[0].complete)
            self.assertFalse(any(row.instrument_id == "AAAUSDT" for row in pending.health_rollups))
            source = next(row for row in pending.health_rollups if row.source == "binance_agg_trade")
            self.assertEqual(dict(source.counters)["accepted_events"], 1)
            self.assertEqual(dict(source.counters)["connection_observations"], 1)
            self.assertEqual(source.connection_epochs, (0,))
            aggregator.acknowledge(pending.batch_id)

    def test_no_output_prepare_releases_empty_generation_before_later_health(self):
        aggregator = BoundedMinuteAggregator()
        self.assertIsNone(aggregator.prepare(START + 62_000))
        self.assertIsNone(aggregator.prepare(START + 62_000))
        context = {"source": "test", "exchange": "binance", "market": "futures", "instrument_id": "*"}
        aggregator.quality.record(context, "late_events", observed_ms=START)
        pending = aggregator.prepare(START + 62_000)
        self.assertEqual(dict(pending.health_rollups[0].counters)["late_events"], 1)
        aggregator.acknowledge(pending.batch_id)
        self.assertEqual(aggregator.quality.stats()["open_quality_rollups"], 0)

    def test_identical_health_deltas_get_distinct_batch_ids_and_both_commit(self):
        aggregator = BoundedMinuteAggregator()
        context = {"source": "test", "exchange": "binance", "market": "futures", "instrument_id": "*"}
        aggregator.quality.record(context, "accepted_events", observed_ms=START,
                                  processing_latency_ms=20, event_latency_ms=30)
        first = aggregator.prepare(START + 62_000)
        aggregator.quality.record(context, "accepted_events", observed_ms=START,
                                  processing_latency_ms=20, event_latency_ms=30)
        self.assertIs(aggregator.prepare(START + 62_000), first)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hunter.db"
            migrate(path)
            with HunterWriter(path) as writer:
                writer.commit_batch(first)
                aggregator.acknowledge(first.batch_id)
                second = aggregator.prepare(START + 62_000)
                self.assertEqual(first.health_rollups, second.health_rollups)
                self.assertNotEqual(first.batch_id, second.batch_id)
                self.assertFalse(writer.commit_batch(second).receipt.already_committed)
                self.assertTrue(writer.commit_batch(second).receipt.already_committed)
                aggregator.acknowledge(second.batch_id)
            with closing(sqlite3.connect(path)) as connection:
                row = json.loads(connection.execute("SELECT record_json FROM health_rollups_1m").fetchone()[0])
            self.assertEqual(row["counters"]["accepted_events"], 2)
            self.assertEqual((row["max_processing_latency_ms"], row["max_event_latency_ms"]), (20, 30))

    def test_non_trade_missing_and_flagged_payloads_keep_instrument_health_evidence(self):
        missing_payloads = (
            ("mark_price", MarkPricePayload(None, missing_reason="source_unavailable")),
            ("funding", FundingPayload(None, missing_reason="source_unavailable")),
            ("open_interest", OpenInterestPayload(None, missing_reason="source_unavailable")),
            ("book_ticker", BookTickerPayload("9", None, missing_reason="source_unavailable")),
            ("liquidation", LiquidationPayload(None, "1", "buy", missing_reason="source_unavailable")),
        )
        for event_type, payload in missing_payloads:
            with self.subTest(event_type=event_type):
                aggregator = BoundedMinuteAggregator()
                self.assertFalse(aggregator.ingest(typed_event(event_type, payload, flags=("source_degraded",))))
                pending = aggregator.prepare(START + 62_000)
                self.assertEqual(pending.buckets, ())
                instrument = next(row for row in pending.health_rollups if row.instrument_id == "AAAUSDT")
                self.assertEqual(dict(instrument.counters)["missing_payload_events"], 1)
                self.assertEqual(dict(instrument.counters)["flagged_quality_events"], 1)
                evidence = json.loads(instrument.status_changes[0][2])
                self.assertEqual(evidence["event_type"], event_type)
                self.assertEqual(evidence["missing_reason"], "source_unavailable")
                self.assertEqual(evidence["quality_flags"], ["source_degraded"])

    def test_non_trade_quality_flag_with_complete_metrics_needs_no_missing_reason(self):
        aggregator = BoundedMinuteAggregator()
        aggregator.ingest(typed_event("funding", FundingPayload("0.001"), flags=("source_degraded",)))
        pending = aggregator.prepare(START + 62_000)
        instrument = next(row for row in pending.health_rollups if row.instrument_id == "AAAUSDT")
        self.assertNotIn("missing_payload_events", dict(instrument.counters))
        self.assertEqual(dict(instrument.counters)["flagged_quality_events"], 1)
        evidence = json.loads(instrument.status_changes[0][2])
        self.assertIsNone(evidence["missing_reason"])
        self.assertEqual(evidence["quality_flags"], ["source_degraded"])

    def test_non_trade_quality_recovery_is_per_type_and_not_repeated_each_minute(self):
        aggregator = BoundedMinuteAggregator()
        events = (
            typed_event("funding", FundingPayload(None, missing_reason="source_unavailable")),
            typed_event("open_interest", OpenInterestPayload("100"), minute=1),
            typed_event("funding", FundingPayload("0.001"), minute=2),
            typed_event("funding", FundingPayload("0.001"), minute=3),
        )
        for minute, event in enumerate(events):
            aggregator.ingest(event)
            pending = aggregator.prepare(START + (minute + 1) * 60_000 + 2000)
            detail = [row for row in pending.health_rollups if row.instrument_id == "AAAUSDT"]
            if minute in (1, 3):
                self.assertEqual(detail, [])
            elif minute == 2:
                self.assertEqual(detail[0].status_changes[0][1], "complete")
                self.assertEqual(json.loads(detail[0].status_changes[0][2])["event_type"], "funding")
            aggregator.acknowledge(pending.batch_id)

    def test_future_non_trade_event_is_rejected_and_retains_typed_failure_evidence(self):
        aggregator = BoundedMinuteAggregator()
        event = typed_event("funding", FundingPayload("0.001"))
        event = replace(event, event_time_ms=event.receive_time_ms + 2001)
        self.assertFalse(aggregator.ingest(event))
        pending = aggregator.prepare(START + 62_000)
        self.assertEqual(pending.buckets, ())
        self.assertEqual(aggregator.stats()["instrument_count"], 0)
        detail = next(row for row in pending.health_rollups if row.instrument_id == "AAAUSDT")
        self.assertEqual(dict(detail.counters)["future_events"], 1)
        self.assertTrue(json.loads(detail.status_changes[0][2])["future_event"])

    def test_non_trade_health_reason_is_bounded_with_visible_truncation(self):
        aggregator = BoundedMinuteAggregator()
        flags = tuple(f"{index}" + "异" * 60 for index in range(32))
        aggregator.ingest(typed_event("funding", FundingPayload("0.001"), flags=flags))
        detail = next(row for row in aggregator.prepare(START + 62_000).health_rollups if row.instrument_id == "AAAUSDT")
        self.assertLessEqual(len(detail.status_changes[0][2]), 2048)
        evidence = json.loads(detail.status_changes[0][2])
        self.assertGreater(evidence["quality_flags_omitted"], 0)
        self.assertEqual(dict(detail.counters)["health_reason_truncated"], 1)

    def test_full_length_unicode_missing_reason_roundtrips_without_ascii_expansion_failure(self):
        aggregator = BoundedMinuteAggregator()
        missing = "\U0001f6d1" * 256
        aggregator.ingest(typed_event("funding", FundingPayload(None, missing_reason=missing)))
        detail = next(row for row in aggregator.prepare(START + 62_000).health_rollups if row.instrument_id == "AAAUSDT")
        self.assertEqual(json.loads(detail.status_changes[0][2])["missing_reason"], missing)
        self.assertNotIn("health_reason_truncated", dict(detail.counters))


if __name__ == "__main__":
    unittest.main()
