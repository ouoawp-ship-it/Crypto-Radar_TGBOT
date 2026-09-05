"""Offline regression evidence for recovery cycles and subscription admission."""
from dataclasses import replace
import unittest

from radars.altcoin_hunter.adapters.base import EnvelopeLimits, Route
from radars.altcoin_hunter.connection import AckIdStrategy, ConnectionState, validate_ack_id
from radars.altcoin_hunter.subscription_plan import plan_subscriptions, synthetic_instruments
from tests.altcoin_hunter_tests.test_binance_connection import START, activate, frame, supervisor


def acknowledge_all(connection, now_ms=None, *, reverse=False):
    now_ms = connection.now_ms if now_ms is None else now_ms
    for _ in range(20):
        requests = tuple(connection.pending.values())
        for request in reversed(requests) if reverse else requests:
            connection.on_ack({"id": request.request_id, "result": None}, epoch=request.epoch,
                              generation=request.generation, method=request.method, now_ms=now_ms)
        if connection.state == ConnectionState.ACTIVE:
            return now_ms
        now_ms += 1000
        connection.step(now_ms)
    raise AssertionError("ACK completion did not activate connection")


def recover(connection):
    deadline = connection.snapshot()["next_connect_ms"]
    connection.step(deadline)
    connection.on_open(now_ms=deadline)
    return acknowledge_all(connection)


def replacement(connection, count=1, start_index=1):
    plan = plan_subscriptions(synthetic_instruments(count, start_index=start_index),
                              max_streams_per_connection=1024)
    return next(shard for shard in plan.shards if shard.route == connection.shard.route)


def data(stream):
    return {"stream": stream, "data": {"offline": True}}


class RecoveryCycleTests(unittest.TestCase):
    def test_exactly_three_connection_failures_exhaust_current_cycle(self):
        connection = supervisor(max_reconnect_attempts=3)
        connection.start(START)
        for failure in range(1, 4):
            connection.step(connection.now_ms + connection.connect_timeout_ms)
            self.assertEqual(connection.consecutive_reconnect_failures, failure)
            if failure < 3:
                connection.step(connection.snapshot()["next_connect_ms"])
        snapshot = connection.snapshot()
        self.assertEqual(snapshot["state"], "DEGRADED")
        self.assertEqual(snapshot["connection_attempts_total"], 3)
        self.assertEqual(snapshot["reconnect_budget_exhausted_total"], 1)
        self.assertIsNone(snapshot["next_connect_ms"])

    def test_tcp_open_and_immediate_active_failure_do_not_reset(self):
        connection = supervisor(max_reconnect_attempts=3)
        activate(connection)
        connection.on_close(now_ms=START + 1)
        deadline = connection.snapshot()["next_connect_ms"]
        connection.step(deadline)
        connection.on_open(now_ms=deadline)
        self.assertEqual(connection.consecutive_reconnect_failures, 1)
        acknowledge_all(connection)
        self.assertEqual(connection.consecutive_reconnect_failures, 1)
        connection.on_close(now_ms=connection.now_ms + 1)
        self.assertEqual(connection.consecutive_reconnect_failures, 2)

    def test_ack_timeout_and_subscription_rejection_continue_failure_cycle(self):
        connection = supervisor(max_reconnect_attempts=3)
        activate(connection)
        connection.on_close(now_ms=START + 1)
        connection.step(connection.snapshot()["next_connect_ms"])
        connection.on_open(now_ms=connection.now_ms)
        connection.step(connection.now_ms + connection.ack_timeout_ms)
        self.assertEqual(connection.consecutive_reconnect_failures, 2)
        connection.step(connection.snapshot()["next_connect_ms"])
        connection.on_open(now_ms=connection.now_ms)
        request_id = next(iter(connection.pending))
        connection.on_ack({"id": request_id, "code": 2}, epoch=connection.epoch, now_ms=connection.now_ms)
        self.assertEqual(connection.consecutive_reconnect_failures, 3)
        self.assertEqual(connection.state, ConnectionState.DEGRADED)

    def test_stable_window_resets_failures_without_losing_total_counts(self):
        connection = supervisor(stable_active_ms=1000)
        activate(connection)
        connection.on_close(now_ms=START + 1)
        recover(connection)
        connection.step(connection.now_ms + 999)
        self.assertEqual(connection.consecutive_reconnect_failures, 1)
        connection.step(connection.now_ms + 1)
        self.assertEqual(connection.consecutive_reconnect_failures, 0)
        self.assertEqual(connection.snapshot()["reconnect_attempts_total"], 1)
        self.assertEqual(connection.snapshot()["successful_activations_total"], 2)

    def test_expired_liveness_cannot_claim_stable_recovery(self):
        connection = supervisor(stable_active_ms=1000, idle_timeout_ms=100)
        activate(connection)
        connection.on_close(now_ms=START + 1)
        recover(connection)
        connection.step(connection.now_ms + 1000)
        self.assertEqual(connection.consecutive_reconnect_failures, 2)
        self.assertFalse(connection.snapshot()["activation_stable"])

    def test_uncertain_failure_cannot_reset_using_untrusted_silent_interval(self):
        for failure in ("gap", "malformed", "route", "ack"):
            with self.subTest(failure=failure):
                connection = supervisor(stable_active_ms=1000, max_reconnect_attempts=3)
                activate(connection)
                connection.on_close(now_ms=START + 1)
                recover(connection)
                last_good = connection.now_ms
                self.assertEqual(connection.consecutive_reconnect_failures, 1)
                failure_at = last_good + 1001
                if failure == "gap":
                    connection.report_gap(now_ms=failure_at)
                elif failure == "ack":
                    connection.on_ack({"id": None, "result": None}, epoch=connection.epoch,
                                      now_ms=failure_at)
                else:
                    connection.on_frame("malformed" if failure == "malformed" else frame(connection),
                        epoch=connection.epoch, route=Route.PUBLIC if failure == "route" else Route.MARKET,
                        now_ms=failure_at)
                self.assertEqual(connection.consecutive_reconnect_failures, 2)
                self.assertEqual(connection.counts["stable_activation"], 0)
                self.assertEqual(connection.state, ConnectionState.BACKOFF)
                self.assertIsNone(connection.snapshot()["coverage_open_since_ms"])

    def test_valid_frame_can_confirm_stability_before_later_uncertain_failure(self):
        connection = supervisor(stable_active_ms=1000, max_reconnect_attempts=3)
        activate(connection)
        connection.on_close(now_ms=START + 1)
        recover(connection)
        self.assertTrue(connection.on_frame(frame(connection), epoch=connection.epoch,
                                           route=Route.MARKET, now_ms=connection.now_ms + 1000))
        self.assertEqual(connection.consecutive_reconnect_failures, 0)
        connection.report_gap(now_ms=connection.now_ms + 1)
        self.assertEqual(connection.consecutive_reconnect_failures, 1)

    def test_100_stable_recoveries_allow_101st_and_backoff_remains_small(self):
        connection = supervisor(max_reconnect_attempts=3, stable_active_ms=100)
        activate(connection)
        delays = []
        for _ in range(100):
            connection.step(connection.now_ms + 100)
            connection.on_close(now_ms=connection.now_ms + 1)
            delays.append(connection.snapshot()["next_connect_ms"] - connection.now_ms)
            recover(connection)
        self.assertEqual(connection.snapshot()["reconnect_attempts_total"], 100)
        self.assertEqual(connection.snapshot()["successful_activations_total"], 101)
        connection.step(connection.now_ms + 100)
        connection.on_close(now_ms=connection.now_ms + 1)
        self.assertEqual(connection.state, ConnectionState.BACKOFF)
        self.assertLessEqual(max(delays), 1200)
        self.assertEqual(connection.snapshot()["reconnect_budget_exhausted_total"], 0)
        recover(connection)
        self.assertEqual(connection.snapshot()["reconnect_attempts_total"], 101)

    def test_100_planned_recycles_do_not_consume_failure_budget(self):
        connection = supervisor(max_reconnect_attempts=1, recycle_jitter_ms=0)
        activate(connection)
        for _ in range(100):
            deadline = connection.snapshot()["recycle_at_ms"]
            while connection.now_ms + 180000 < deadline:
                connection.on_frame("ping", epoch=connection.epoch, route=Route.MARKET,
                                    now_ms=connection.now_ms + 180000, frame_type="ping")
            connection.step(deadline)
            self.assertEqual(connection.consecutive_reconnect_failures, 0)
            recover(connection)
        snapshot = connection.snapshot()
        self.assertEqual(snapshot["planned_recycles_total"], 100)
        self.assertEqual(snapshot["reconnect_attempts_total"], 100)
        self.assertEqual(snapshot["reconnect_budget_exhausted_total"], 0)
        self.assertEqual(snapshot["epoch"], 101)
        self.assertNotIn("remote_close", snapshot["counts"])


class SubscriptionTransitionTests(unittest.TestCase):
    def setUp(self):
        self.connection = supervisor()
        activate(self.connection)
        self.old = next(s for s in self.connection.shard.streams if s.kind == "agg_trade").wire_name
        self.new_shard = replacement(self.connection)
        self.new = next(s for s in self.new_shard.streams if s.kind == "agg_trade").wire_name

    def begin(self):
        self.connection.update_subscriptions(self.new_shard, now_ms=START + 10)

    def send(self, stream, at=None):
        connection = self.connection
        return connection.on_frame(data(stream), epoch=connection.epoch, route=connection.shard.route,
                                   now_ms=connection.now_ms if at is None else at)

    def test_retiring_before_ack_is_discarded_without_closing(self):
        self.begin()
        self.assertFalse(self.send(self.old))
        self.assertEqual(self.connection.counts["retiring_stream_frame"], 1)
        self.assertEqual(self.connection.state, ConnectionState.SUBSCRIBING)
        self.assertIsNone(self.connection.snapshot()["coverage_open_since_ms"])
        self.assertIn(self.old.lower(), self.connection.retiring_streams)

    def test_retiring_tombstone_after_ack_is_bounded_and_expires(self):
        self.begin()
        acknowledge_all(self.connection)
        self.assertFalse(self.send(self.old))
        self.assertEqual(self.connection.state, ConnectionState.ACTIVE)
        self.assertEqual(len(self.connection.snapshot()["stream_transition"]["retiring_tombstones"]), 1)
        self.assertFalse(self.send(self.old, self.connection.now_ms + 2000))
        self.assertEqual(self.connection.counts["unplanned_stream"], 1)
        self.assertEqual(self.connection.state, ConnectionState.BACKOFF)

    def test_adding_early_frame_is_discarded_and_cannot_supply_coverage(self):
        self.begin()
        self.assertFalse(self.send(self.new))
        self.assertEqual(self.connection.counts["adding_stream_early_frame"], 1)
        self.assertNotIn(self.new.lower(), self.connection.acknowledged_streams)
        self.assertIsNone(self.connection.snapshot()["coverage_open_since_ms"])

    def test_subscribe_and_unsubscribe_ack_both_orders(self):
        for order in (("SUBSCRIBE", "UNSUBSCRIBE"), ("UNSUBSCRIBE", "SUBSCRIBE")):
            with self.subTest(order=order):
                self.setUp()
                self.begin()
                generation = self.connection.transition_generation
                requests = {request.method: request for request in self.connection.pending.values()}
                for index, method in enumerate(order):
                    request = requests[method]
                    result = self.connection.on_ack({"id": request.request_id, "result": None},
                        epoch=request.epoch, generation=generation, method=method, now_ms=START + 11 + index)
                    self.assertEqual(result, "ACKNOWLEDGED")
                    self.assertEqual(self.connection.state, ConnectionState.SUBSCRIBING if index == 0 else ConnectionState.ACTIVE)
                self.assertEqual(self.connection.active_streams, self.connection.desired_streams)
                self.assertFalse(self.connection.adding_streams | self.connection.retiring_streams)
                self.assertTrue(self.send(self.new))

    def test_partial_multibatch_ack_never_reopens_coverage(self):
        connection = supervisor(150)
        activate(connection)
        # Initial subscription consumed four controls. Advance a full rolling
        # second so all six transition batches can be pending simultaneously.
        connection.update_subscriptions(replacement(connection, 150, 150), now_ms=START + 1000)
        self.assertEqual(len(connection.pending), 6)
        requests = tuple(connection.pending.values())
        connection.on_ack({"id": requests[-1].request_id, "result": None}, epoch=1,
                          generation=2, method=requests[-1].method, now_ms=START + 1000)
        self.assertEqual(connection.state, ConnectionState.SUBSCRIBING)
        self.assertIsNone(connection.snapshot()["coverage_open_since_ms"])
        acknowledge_all(connection, reverse=True)
        self.assertEqual(connection.state, ConnectionState.ACTIVE)

    def test_wrong_generation_and_method_cannot_consume_ack(self):
        self.begin()
        request = next(iter(self.connection.pending.values()))
        message = {"id": request.request_id, "result": None}
        self.assertEqual(self.connection.on_ack(message, epoch=1, generation=1, now_ms=START + 10), "UNKNOWN")
        self.assertEqual(self.connection.on_ack(message, epoch=1, method="SUBSCRIBE", now_ms=START + 10), "UNKNOWN")
        self.assertIn(request.request_id, self.connection.pending)
        self.assertEqual(self.connection.counts["stale_generation_ack"], 1)
        self.assertEqual(self.connection.counts["ack_method_mismatch"], 1)

    def test_route_mismatch_during_update_fails_and_reconnects_desired_epoch(self):
        self.begin()
        old_request = next(iter(self.connection.pending.values()))
        self.assertFalse(self.connection.on_frame(data(self.new), epoch=1, route=Route.PUBLIC, now_ms=START + 11))
        self.assertEqual(self.connection.state, ConnectionState.BACKOFF)
        self.assertFalse(self.connection.retiring_streams | self.connection.adding_streams)
        recover(self.connection)
        self.assertEqual(self.connection.epoch, 2)
        self.assertEqual(self.connection.active_streams, {s.canonical_name for s in self.new_shard.streams})
        self.assertEqual(self.connection.on_ack({"id": old_request.request_id, "result": None}, epoch=1,
                         now_ms=self.connection.now_ms), "UNKNOWN")

    def test_update_timeout_never_combines_old_and_new_coverage(self):
        self.begin()
        self.connection.step(START + 5010)
        self.assertEqual(self.connection.state, ConnectionState.BACKOFF)
        recover(self.connection)
        self.connection.stop(self.connection.now_ms + 1)
        intervals = self.connection.snapshot()["coverage"]
        self.assertEqual([interval["connection_epoch"] for interval in intervals], [1, 2])
        self.assertLess(intervals[0]["end_ms"], intervals[1]["start_ms"])
        self.assertNotEqual(intervals[0]["instruments"], intervals[1]["instruments"])

    def test_same_plan_no_transition_and_unknown_stream_after_completion_rejected(self):
        before = self.connection.snapshot()
        self.connection.update_subscriptions(self.connection.shard, now_ms=START + 1)
        self.assertEqual(self.connection.transition_generation, before["stream_transition"]["generation"])
        self.assertEqual(self.connection.snapshot()["coverage_open_since_ms"], before["coverage_open_since_ms"])
        self.begin()
        acknowledge_all(self.connection)
        self.assertFalse(self.send("unexpected@aggTrade"))
        self.assertEqual(self.connection.counts["unplanned_stream"], 1)

    def test_stop_clears_transition_and_tombstone_state(self):
        self.begin()
        self.connection.stop(START + 11)
        snapshot = self.connection.snapshot()
        self.assertTrue(snapshot["cleanup_complete"])
        for field in ("active_streams", "adding_streams", "retiring_streams", "acknowledged_streams", "retiring_tombstones"):
            self.assertFalse(snapshot["stream_transition"][field])

    def test_tombstone_capacity_fails_closed_without_forgetting_live_entries(self):
        connection = supervisor(2, max_retiring_tombstones=1)
        activate(connection)
        connection.update_subscriptions(replacement(connection, 2, 2), now_ms=START + 1)
        request = next(request for request in connection.pending.values() if request.method == "UNSUBSCRIBE")
        self.assertEqual(connection.on_ack({"id": request.request_id, "result": None}, epoch=1,
                         now_ms=START + 1), "REJECTED")
        self.assertEqual(connection.counts["retiring_tombstone_capacity_exceeded"], 1)
        self.assertIsNone(connection.snapshot()["coverage_open_since_ms"])


class AckStrategyTests(unittest.TestCase):
    def test_both_strategies_roundtrip_duplicate_and_stale(self):
        for strategy in AckIdStrategy:
            with self.subTest(strategy=strategy):
                connection = supervisor(ack_id_strategy=strategy)
                connection.start(START)
                connection.on_open(now_ms=START)
                request = next(iter(connection.pending.values()))
                expected_type = int if strategy == AckIdStrategy.INTEGER else str
                self.assertIs(type(request.request_id), expected_type)
                self.assertEqual(connection.snapshot()["ack_id_strategy"], strategy.value)
                acknowledge_all(connection)
                self.assertEqual(connection.on_ack({"id": request.request_id, "result": None}, epoch=1, now_ms=START), "DUPLICATE")
                self.assertEqual(connection.on_ack({"id": request.request_id, "result": None}, epoch=0, now_ms=START), "UNKNOWN")

    def test_equivalent_id_of_wrong_json_type_is_not_accepted(self):
        for strategy in AckIdStrategy:
            connection = supervisor(ack_id_strategy=strategy)
            connection.start(START)
            connection.on_open(now_ms=START)
            request_id = next(iter(connection.pending))
            wrong = str(request_id) if type(request_id) is int else 1
            self.assertEqual(connection.on_ack({"id": wrong, "result": None}, epoch=1, now_ms=START), "UNKNOWN")
            self.assertIn(request_id, connection.pending)
            self.assertEqual(connection.state, ConnectionState.SUBSCRIBING)
            acknowledge_all(connection)

    def test_invalid_ids_rejected(self):
        for invalid in (True, False, None, "", " ", "a b", "x" * 65, "a\n", "\x00", "币", 0, -1, 1.0):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(ValueError): validate_ack_id(invalid)
                connection = supervisor()
                connection.start(START)
                connection.on_open(now_ms=START)
                self.assertEqual(connection.on_ack({"id": invalid, "result": None}, epoch=1, now_ms=START), "UNKNOWN")
                self.assertEqual(connection.counts["malformed_ack"], 1)

    def test_string_ids_are_deterministic_bounded_and_unique_across_epoch(self):
        runs = []
        for _ in range(2):
            connection = supervisor(100, ack_id_strategy="STRING")
            activate(connection)
            connection.on_close(now_ms=START + 1)
            recover(connection)
            ids = [item["message"]["id"] for item in connection.transport.actions
                   if item["action"] == "send" and "id" in item["message"]]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(all(identifier.isascii() and len(identifier) <= 64 for identifier in ids))
            runs.append(ids)
        self.assertEqual(*runs)

    def test_previous_transition_ack_is_stale_under_both_strategies(self):
        for strategy in AckIdStrategy:
            with self.subTest(strategy=strategy):
                connection = supervisor(ack_id_strategy=strategy)
                activate(connection)
                old_request = connection._acknowledged_ids[0]
                connection.update_subscriptions(replacement(connection), now_ms=START + 1)
                self.assertEqual(connection.on_ack({"id": old_request.request_id, "result": None},
                    epoch=1, now_ms=START + 1), "UNKNOWN")
                self.assertEqual(connection.counts["stale_generation_ack"], 1)
                self.assertEqual(connection.state, ConnectionState.SUBSCRIBING)
                self.assertIsNone(connection.snapshot()["coverage_open_since_ms"])
                acknowledge_all(connection)

    def test_duplicate_ack_still_requires_matching_method(self):
        for strategy in AckIdStrategy:
            with self.subTest(strategy=strategy):
                connection = supervisor(ack_id_strategy=strategy)
                activate(connection)
                request = connection._acknowledged_ids[0]
                self.assertEqual(connection.on_ack({"id": request.request_id, "result": None},
                    epoch=1, method="UNSUBSCRIBE", now_ms=START + 1), "UNKNOWN")
                self.assertEqual(connection.counts["ack_method_mismatch"], 1)
                self.assertEqual(connection.counts["duplicate_ack"], 0)

    def test_ack_after_pong_deadline_cannot_open_coverage(self):
        connection = supervisor(1000, pong_timeout_ms=10)
        connection.start(START)
        connection.on_open(now_ms=START)
        request_id = next(iter(connection.pending))
        connection.on_frame("server-ping", epoch=1, route=Route.MARKET,
                            now_ms=START + 1, frame_type="ping")
        self.assertEqual(connection.on_ack({"id": request_id, "result": None}, epoch=1,
                                          now_ms=START + 11), "EXPIRED")
        self.assertEqual(connection.counts["pong_timeout"], 1)
        self.assertEqual(connection.state, ConnectionState.BACKOFF)
        self.assertIsNone(connection.snapshot()["coverage_open_since_ms"])

    def test_ack_after_another_pending_deadline_cannot_revive_transition(self):
        connection = supervisor(1000)
        connection.start(START)
        connection.on_open(now_ms=START)
        first_request = next(iter(connection.pending.values()))
        connection.step(START + 1000)
        later_request = next(request for request in connection.pending.values()
                             if request.sent_at_ms > first_request.sent_at_ms)
        self.assertLess(first_request.expires_at_ms, later_request.expires_at_ms)
        self.assertEqual(connection.on_ack({"id": later_request.request_id, "result": None}, epoch=1,
                                          now_ms=first_request.expires_at_ms), "EXPIRED")
        self.assertEqual(connection.counts["ack_timeout"], 1)
        self.assertEqual(connection.state, ConnectionState.BACKOFF)
        self.assertIsNone(connection.snapshot()["coverage_open_since_ms"])

    def test_ack_at_recycle_deadline_cannot_delay_planned_rotation(self):
        connection = supervisor(recycle_jitter_ms=0)
        activate(connection)
        request_id = connection._acknowledged_ids[0].request_id
        deadline = connection.snapshot()["recycle_at_ms"]
        while connection.now_ms + 180000 < deadline:
            connection.on_frame("ping", epoch=1, route=Route.MARKET,
                                now_ms=connection.now_ms + 180000, frame_type="ping")
        self.assertEqual(connection.on_ack({"id": request_id, "result": None}, epoch=1,
                                          now_ms=deadline), "EXPIRED")
        self.assertEqual(connection.snapshot()["planned_recycles_total"], 1)
        self.assertEqual(connection.consecutive_reconnect_failures, 0)
        self.assertIsNone(connection.snapshot()["coverage_open_since_ms"])


class CombinedEnvelopeConnectionTests(unittest.TestCase):
    def test_standard_and_bounded_unknown_fields_are_accepted(self):
        for extras in ({}, {"future": 1}, {"future": {"nested": [1, "ok"]}, "another": True}):
            with self.subTest(extras=extras):
                connection = supervisor()
                activate(connection)
                self.assertTrue(connection.on_frame({**frame(connection), **extras}, epoch=1, route=Route.MARKET, now_ms=START + 1))
                self.assertEqual(connection.state, ConnectionState.ACTIVE)
                self.assertEqual(connection.counts["unknown_envelope_field"], len(extras))
                self.assertNotIn("nested", str(connection.snapshot()["diagnostics"]))

    def test_missing_wrong_shape_and_oversized_envelopes_fail_closed(self):
        deep = {}
        for _ in range(10): deep = {"nested": deep}
        malformed = [
            {"data": {}}, {"stream": "coin0usdt@aggTrade"},
            {"stream": 1, "data": {}}, {"stream": "coin0usdt@aggTrade", "data": 1},
            {"stream": "coin0usdt@aggTrade", "data": {}, "future": deep},
            {"stream": "coin0usdt@aggTrade", "data": {}, "future": "x" * 4097},
            {"stream": "coin0usdt@aggTrade", "data": {}, **{f"extra_{index}": 1 for index in range(40)}},
        ]
        for message in malformed:
            with self.subTest(shape=tuple(message)):
                connection = supervisor()
                activate(connection)
                self.assertFalse(connection.on_frame(message, epoch=1, route=Route.MARKET, now_ms=START + 1))
                self.assertEqual(connection.state, ConnectionState.BACKOFF)
                self.assertEqual(connection.counts["malformed_payload"], 1)

    def test_explicit_smaller_total_byte_limit_is_enforced(self):
        connection = supervisor(envelope_limits=EnvelopeLimits(max_payload_bytes=100))
        activate(connection)
        self.assertFalse(connection.on_frame({**frame(connection), "extra": "x" * 80}, epoch=1,
                                            route=Route.MARKET, now_ms=START + 1))
        self.assertEqual(connection.counts["oversized_payload"], 1)


if __name__ == "__main__":
    unittest.main()
