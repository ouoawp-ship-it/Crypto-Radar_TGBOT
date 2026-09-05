import hashlib
import json
import socket
import unittest
from unittest.mock import patch

from radars.altcoin_hunter.adapters.base import FakeTransport, Route
from radars.altcoin_hunter.aggregation import BoundedMinuteAggregator
from radars.altcoin_hunter.connection import ConnectionState, ConnectionSupervisor, run_connection_scenario
from radars.altcoin_hunter.models import TradeEvent, TradePayload
from radars.altcoin_hunter.subscription_plan import plan_subscriptions, synthetic_instruments


START = 1704067200000


def supervisor(count=1, **kwargs):
    plan = plan_subscriptions(synthetic_instruments(count), max_streams_per_connection=1024)
    return ConnectionSupervisor(next(s for s in plan.shards if s.route == Route.MARKET), **kwargs)


def activate(connection, at=START):
    connection.start(at)
    connection.on_open(now_ms=at)
    for _ in range(10):
        for request_id in tuple(connection.pending):
            connection.on_ack({"id": request_id, "result": None}, epoch=connection.epoch, now_ms=at)
        if connection.state == ConnectionState.ACTIVE:
            return at
        at += 1000
        connection.step(at)
    raise AssertionError("failed activation")


def frame(connection):
    return {"stream": connection.shard.streams[0].wire_name, "data": {"fixture": True}}


class BinanceConnectionTests(unittest.TestCase):
    def test_active_requires_every_ack_and_partial_ack_does_not_cover(self):
        connection = supervisor(100)
        connection.start(START)
        connection.on_open(now_ms=START)
        pending = tuple(connection.pending)
        connection.on_ack({"id": pending[0], "result": None}, epoch=1, now_ms=START)
        self.assertEqual(connection.state, ConnectionState.SUBSCRIBING)
        self.assertIsNone(connection.snapshot()["coverage_open_since_ms"])
        for request in pending[1:]:
            connection.on_ack({"id": request, "result": None}, epoch=1, now_ms=START + 1)
        self.assertEqual(connection.state, ConnectionState.ACTIVE)

    def test_control_frames_batches_and_pongs_share_rolling_budget(self):
        connection = supervisor(1000)
        connection.start(START)
        connection.on_open(now_ms=START)
        sent = [a["message"] for a in connection.transport.actions if a["action"] == "send"]
        self.assertEqual(len(sent), 8)
        self.assertTrue(all(len(m["params"]) <= 50 for m in sent))
        connection.on_frame("heartbeat", epoch=1, route=Route.MARKET, now_ms=START + 100, frame_type="ping")
        self.assertEqual(connection.counts["pong_sent"], 0)
        connection.step(START + 999)
        self.assertEqual(connection.counts["controls_sent"], 8)
        connection.step(START + 1000)
        self.assertEqual(connection.counts["pong_sent"], 1)
        self.assertLessEqual(connection.snapshot()["control_peak_per_second"], 8)

    def test_unknown_duplicate_and_stale_ack_are_distinct(self):
        connection = supervisor()
        activate(connection)
        self.assertEqual(connection.on_ack({"id": 1, "result": None}, epoch=1, now_ms=START), "DUPLICATE")
        self.assertEqual(connection.on_ack({"id": 999, "result": None}, epoch=1, now_ms=START), "UNKNOWN")
        self.assertEqual(connection.on_ack({"id": 1, "result": None}, epoch=0, now_ms=START), "UNKNOWN")
        self.assertEqual(connection.state, ConnectionState.ACTIVE)

    def test_boolean_ack_id_and_nonnull_result_are_rejected(self):
        for message in ({"id": True, "result": None}, {"id": 1, "result": ["partial"]}, {"id": 1, "code": 2}):
            connection = supervisor()
            connection.start(START)
            connection.on_open(now_ms=START)
            connection.on_ack(message, epoch=1, now_ms=START + 1)
            self.assertEqual(connection.state, ConnectionState.BACKOFF)
            self.assertEqual(connection.pending, {})

    def test_ack_timeout_retains_epoch_until_successful_reconnect(self):
        connection = supervisor()
        connection.start(START)
        connection.on_open(now_ms=START)
        connection.step(START + 5000)
        self.assertEqual(connection.state, ConnectionState.BACKOFF)
        self.assertEqual(connection.epoch, 1)
        deadline = connection.snapshot()["next_connect_ms"]
        connection.step(deadline)
        self.assertEqual(connection.epoch, 1)
        connection.on_open(now_ms=deadline)
        self.assertEqual(connection.epoch, 2)
        self.assertTrue(all(request.epoch == 2 for request in connection.pending.values()))
        self.assertTrue(all(request.request_id > 1 for request in connection.pending.values()))

    def test_route_mismatch_malformed_and_loss_close_coverage(self):
        for failure in ("route", "malformed", "loss"):
            connection = supervisor()
            activate(connection)
            connection.on_frame(frame(connection), epoch=1, route=Route.MARKET, now_ms=START + 10)
            if failure == "route":
                connection.on_frame(frame(connection), epoch=1, route=Route.PUBLIC, now_ms=START + 20)
            elif failure == "malformed":
                connection.on_frame("broken", epoch=1, route=Route.MARKET, now_ms=START + 20)
            else:
                connection.report_gap(now_ms=START + 20)
            self.assertEqual(connection.state, ConnectionState.BACKOFF)
            intervals = connection.snapshot()["coverage"]
            self.assertEqual(intervals[0]["end_ms"], START + 10)
            self.assertIsNone(connection.snapshot()["coverage_open_since_ms"])

    def test_idle_deadline_closes_at_deadline_not_late_tick(self):
        connection = supervisor(idle_timeout_ms=1000)
        activate(connection)
        connection.step(START + 5000)
        self.assertEqual(connection.snapshot()["coverage"][0]["end_ms"], START + 1000)
        self.assertEqual(connection.counts["idle_timeout"], 1)

    def test_frame_after_idle_deadline_cannot_retroactively_restore_coverage(self):
        connection = supervisor(idle_timeout_ms=1000)
        activate(connection)
        self.assertFalse(connection.on_frame(frame(connection), epoch=1, route=Route.MARKET, now_ms=START + 1500))
        self.assertEqual(connection.state, ConnectionState.BACKOFF)

    def test_late_stop_does_not_extend_expired_liveness_lease(self):
        connection = supervisor(idle_timeout_ms=1000)
        activate(connection)
        connection.stop(START + 50000)
        self.assertEqual(connection.snapshot()["coverage"][0]["end_ms"], START + 1000)

    def test_blocked_pong_deadline_is_enforced(self):
        connection = supervisor(1000, pong_timeout_ms=10)
        connection.start(START)
        connection.on_open(now_ms=START)
        connection.on_frame("server-ping", epoch=1, route=Route.MARKET, now_ms=START + 1, frame_type="ping")
        connection.step(START + 11)
        self.assertEqual(connection.state, ConnectionState.BACKOFF)
        self.assertEqual(connection.counts["pong_timeout"], 1)

    def test_invalid_route_value_closes_coverage_without_raising(self):
        connection = supervisor()
        activate(connection)
        self.assertFalse(connection.on_frame(frame(connection), epoch=1, route="private", now_ms=START + 10))
        self.assertEqual(connection.state, ConnectionState.BACKOFF)

    def test_recycle_occurs_before_24_hours_with_seeded_jitter(self):
        result = run_connection_scenario("recycle", seed=5)
        self.assertEqual(result["counts"]["scheduled_recycle"], 1)
        recycle = next(t for t in result["transitions"] if t["to"] == "RECYCLING")
        self.assertLess(recycle["at_ms"] - START, 24 * 3600000)
        self.assertGreaterEqual(recycle["at_ms"] - START, 85500000 - 120000)

    def test_100_reconnects_and_ten_percent_ack_loss_are_deterministic(self):
        recipe = {"name": "reconnect_storm", "reconnects": 100, "instruments": 1000}
        first = run_connection_scenario(recipe, seed=7)
        self.assertEqual(first, run_connection_scenario(recipe, seed=7))
        self.assertEqual(first["counts"]["reconnect_attempts"], 100)
        self.assertEqual(first["epoch"], 101)
        self.assertLessEqual(first["pending_ack_peak"], 128)
        self.assertLessEqual(len(first["transitions"]), 256)
        loss = run_connection_scenario({"name": "ack_loss", "instruments": 1000}, seed=7)
        self.assertGreater(loss["ack_loss_observations"]["omitted"], 0)
        self.assertAlmostEqual(loss["ack_loss_observations"]["actual_ratio"], 0.1, delta=1 / 21)
        self.assertEqual(loss["state"], "BACKOFF")

    def test_reconnect_budget_is_hard_bounded(self):
        # The budget limits consecutive failed recovery attempts, not lifetime
        # successful reconnects. Three failed opens exhaust this recovery cycle.
        result = run_connection_scenario({"name": "connect_failures", "failures": 3, "max_reconnect_attempts": 3}, seed=1)
        self.assertEqual(result["connection_attempts_total"], 3)
        self.assertEqual(result["reconnect_attempts_total"], 2)
        self.assertEqual(result["consecutive_reconnect_failures"], 3)
        self.assertEqual(result["epoch"], 0)
        self.assertEqual(result["state"], "DEGRADED")
        self.assertIsNone(result["next_connect_ms"])

    def test_undrained_coverage_evictions_are_counted_and_never_reconstructed(self):
        connection = supervisor(max_reconnect_attempts=300)
        activate(connection)
        digest = hashlib.sha256()
        for _ in range(300):
            connection.step(connection.now_ms + connection.stable_active_ms)
            connection.on_close(now_ms=connection.now_ms + 10)
            interval = connection.snapshot()["coverage"][-1]
            digest.update(json.dumps(interval, sort_keys=True, separators=(",", ":")).encode() + b"\n")
            deadline = connection.snapshot()["next_connect_ms"]
            connection.step(deadline)
            connection.on_open(now_ms=deadline)
            for request in tuple(connection.pending):
                connection.on_ack({"id": request, "result": None}, epoch=connection.epoch, now_ms=deadline)
        connection.stop(connection.now_ms + 10)
        interval = connection.snapshot()["coverage"][-1]
        digest.update(json.dumps(interval, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        snapshot = connection.snapshot()
        self.assertEqual(snapshot["counts"]["coverage_intervals"], 301)
        self.assertEqual(len(snapshot["coverage"]), 256)
        self.assertEqual(snapshot["coverage_interval_evicted"], 45)
        self.assertEqual(snapshot["counts"]["coverage_interval_evicted"], 45)
        self.assertEqual(snapshot["diagnostics"]["counters"]["coverage_interval_evicted"], 45)
        self.assertTrue(snapshot["coverage_evidence_lost"])
        self.assertTrue(snapshot["coverage_drain_required"])
        self.assertEqual(snapshot["coverage_digest"], digest.hexdigest())
        records = list(connection.iter_coverage_records())
        self.assertEqual([record["connection_epoch"] for record in records], list(range(46, 302)))
        drained = connection.snapshot()
        self.assertEqual(drained["coverage"], [])
        self.assertFalse(drained["coverage_drain_required"])
        self.assertTrue(drained["coverage_evidence_lost"])
        self.assertEqual(drained["coverage_interval_evicted"], 45)
        self.assertEqual(drained["coverage_digest"], digest.hexdigest())

    def test_stop_clears_pending_work_and_cannot_be_restarted_by_late_ack(self):
        connection = supervisor()
        connection.start(START)
        connection.on_open(now_ms=START)
        connection.stop(START + 1)
        connection.on_ack({"id": True}, epoch=1, now_ms=START + 2)
        connection.step(START + 100000)
        self.assertEqual(connection.state, ConnectionState.STOPPED)
        self.assertTrue(connection.snapshot()["cleanup_complete"])
        self.assertFalse(connection.transport.opened)
        self.assertEqual(connection.epoch, 1)

    def test_coverage_identity_and_previous_subscription_are_frozen(self):
        rows = list(synthetic_instruments(1))
        rows[0]["instrument_id"] = "original-verified-id"
        first = plan_subscriptions(rows)
        connection = ConnectionSupervisor(next(s for s in first.shards if s.route == Route.MARKET))
        activate(connection)
        second = plan_subscriptions(synthetic_instruments(1, start_index=1), previous=first)
        connection.update_subscriptions(next(s for s in second.shards if s.route == Route.MARKET), now_ms=START + 10)
        coverage = list(connection.iter_coverage_records())
        self.assertEqual(len(coverage), 1)
        self.assertEqual(coverage[0]["instrument_id"], "original-verified-id")
        self.assertEqual(coverage[0]["source"], "binance_usdm_agg_trade")
        self.assertEqual(coverage[0]["market"], "usdt_perpetual")
        self.assertEqual(connection.state, ConnectionState.SUBSCRIBING)

    def test_reconnect_does_not_bridge_coverage_epochs(self):
        connection = supervisor()
        activate(connection)
        connection.on_close(now_ms=START + 10)
        deadline = connection.snapshot()["next_connect_ms"]
        connection.step(deadline)
        connection.on_open(now_ms=deadline)
        for request in tuple(connection.pending):
            connection.on_ack({"id": request, "result": None}, epoch=2, now_ms=deadline)
        connection.stop(deadline + 10)
        records = list(connection.iter_coverage_records())
        self.assertEqual([r["connection_epoch"] for r in records], [1, 2])
        self.assertLess(records[0]["end_ms"], records[1]["start_ms"])

    def test_strict_time_and_only_fake_transport(self):
        connection = supervisor()
        connection.start(START)
        with self.assertRaises(ValueError): connection.step(START - 1)
        with self.assertRaises(ValueError): connection.step(True)
        with self.assertRaises(ValueError): supervisor(transport=object())

    def test_simulator_uses_no_socket(self):
        with patch.object(socket, "socket", side_effect=AssertionError("no network")):
            self.assertEqual(run_connection_scenario("normal")["network_calls"], 0)

    def test_simulator_default_cap_and_single_shard_scope_are_explicit(self):
        result = run_connection_scenario({"name": "normal", "instruments": 1000})
        self.assertEqual(result["required_streams"], 800)
        self.assertEqual(result["subscription_plan"]["coverage"]["eligible_instruments"], 1000)
        self.assertEqual(result["simulation_scope"], "single_selected_shard_not_full_market_connections")

    def test_simulated_coverage_enters_p1a_without_trades_or_database(self):
        connection = supervisor()
        activate(connection)
        connection.stop(START + 60000)
        aggregator = BoundedMinuteAggregator()
        records = list(connection.iter_coverage_records())
        self.assertEqual(len(records), 1)
        self.assertTrue(aggregator.note_connection(**records[0]))
        pending = aggregator.prepare(START + 62000)
        self.assertEqual(pending.buckets, ())
        self.assertTrue(any(r.source == "binance_usdm_agg_trade" for r in pending.health_rollups))

    def test_simulated_coverage_identity_yields_complete_p1a_bucket(self):
        record = {**next(synthetic_instruments(1)), "instrument_id": "verified-id"}
        plan = plan_subscriptions([record])
        connection = ConnectionSupervisor(next(s for s in plan.shards if s.route == Route.MARKET))
        activate(connection)
        connection.stop(START + 60000)
        aggregator = BoundedMinuteAggregator()
        event = TradeEvent(exchange="binance", market="usdt_perpetual", instrument_id="verified-id",
            symbol="COIN0USDT", exchange_symbol="COIN0USDT", source="binance_usdm_agg_trade",
            source_event_id="1", event_time_ms=START + 10, receive_time_ms=START + 20,
            receive_monotonic_ns=20_000_000, connection_epoch=1, sequence_start=1, sequence_end=1,
            payload=TradePayload("10", "2", False))
        aggregator.ingest(event)
        for coverage in connection.iter_coverage_records():
            self.assertTrue(aggregator.note_connection(**coverage))
        bucket = aggregator.prepare(START + 62000).buckets[0]
        self.assertTrue(bucket.complete)
        self.assertEqual(bucket.instrument_id, "verified-id")

    def test_reconnect_coverage_never_makes_a_mixed_epoch_p1a_minute_complete(self):
        connection = supervisor()
        activate(connection)
        connection.on_close(now_ms=START + 30000)
        deadline = connection.snapshot()["next_connect_ms"]
        connection.step(deadline)
        connection.on_open(now_ms=deadline)
        for request in tuple(connection.pending):
            connection.on_ack({"id": request, "result": None}, epoch=2, now_ms=deadline)
        connection.stop(START + 60000)
        aggregator = BoundedMinuteAggregator()
        for epoch, offset in ((1, 10000), (2, 40000)):
            aggregator.ingest(TradeEvent(exchange="binance", market="usdt_perpetual", instrument_id="COIN0USDT",
                symbol="COIN0USDT", exchange_symbol="COIN0USDT", source="binance_usdm_agg_trade",
                source_event_id=str(epoch), event_time_ms=START + offset, receive_time_ms=START + offset,
                receive_monotonic_ns=offset * 1_000_000, connection_epoch=epoch,
                sequence_start=epoch, sequence_end=epoch, payload=TradePayload("10", "2", False)))
        for coverage in connection.iter_coverage_records():
            aggregator.note_connection(**coverage)
        bucket = aggregator.prepare(START + 62000).buckets[0]
        self.assertFalse(bucket.complete)
        self.assertIn("connection_epoch_changed", bucket.quality_flags)


if __name__ == "__main__":
    unittest.main()
