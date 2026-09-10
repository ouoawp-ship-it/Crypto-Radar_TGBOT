"""Offline REST identity retirement and accepted-result correlation."""
from dataclasses import replace
import unittest

from radars.altcoin_hunter.rest_budget import FakeCoordinator, make_request
from radars.altcoin_hunter.rest_scheduler import Completion, OiSamplingPlanner, RestScheduler
from .test_binance_rest import VirtualClock


class RestIdentityLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        self.scheduler = RestScheduler(clock=self.clock, coordinator=FakeCoordinator(),
                                       max_identities=2, max_tombstones=2, tombstone_ttl_ms=100)

    def request(self, symbol="A", generation=0, ttl=1000):
        return make_request("openInterest", self.clock.value, instrument_id=symbol,
                            generation=generation, ttl_ms=ttl)

    def accepted(self, symbol="A", generation=0):
        request = self.request(symbol, generation)
        self.assertTrue(self.scheduler.submit(request))
        dispatched, = self.scheduler.poll_due()
        return self.scheduler.complete(dispatched, status_code=200, response_time_ms=self.clock.value)

    def valid(self, completion, **changes):
        values = dict(request_id=completion.request.request_id, generation=completion.request.generation,
                      instrument_id=completion.request.instrument_id, now_ms=self.clock.value,
                      event_time_ms=self.clock.value)
        values.update(changes)
        return self.scheduler.validate_completion(completion, **values)

    def test_queued_retirement_retains_identity_until_controlled_cancellation(self):
        request = self.request()
        self.scheduler.submit(request)
        self.assertTrue(self.scheduler.retire_identity(request.key))
        self.assertTrue(self.scheduler.contains(request.request_id))
        self.assertEqual(self.scheduler.diagnostics()["retiring_identity_count"], 1)
        self.assertEqual(self.scheduler.poll_due(), ())
        report = self.scheduler.diagnostics()
        self.assertEqual((report["identity_count"], report["tombstone_count"]), (0, 1))
        self.assertFalse(self.scheduler.complete(request, status_code=200, response_time_ms=0).accepted)

    def test_inflight_retirement_rejects_success_and_releases_after_completion(self):
        request = self.request()
        self.scheduler.submit(request)
        self.scheduler.poll_due()
        self.scheduler.retire_identity(request.key)
        self.assertEqual(self.scheduler.diagnostics()["inflight"], 1)
        self.assertFalse(self.scheduler.complete(request, status_code=200, response_time_ms=0).accepted)
        self.assertEqual(self.scheduler.diagnostics()["identity_count"], 0)

    def test_reconcile_preserves_current_work_and_never_lru_evicts_live_identity(self):
        first, second = self.request("A"), self.request("B")
        self.scheduler.submit(first)
        self.scheduler.submit(second)
        self.scheduler.reconcile_identities({first.key, second.key})
        self.assertFalse(self.scheduler.submit(self.request("C")))
        self.scheduler.reconcile_identities({second.key, self.request("C").key})
        self.scheduler.poll_due()
        self.assertTrue(self.scheduler.contains(second.request_id))
        floor = self.scheduler.diagnostics()["minimum_new_identity_generation"]
        self.assertTrue(self.scheduler.submit(self.request("C", floor)))

    def test_reconcile_is_validated_atomically_and_rejects_unknown_keys(self):
        request = self.request()
        self.scheduler.submit(request)
        for keys in ([request.key, request.key], [("/fapi/v1/order", "A")], [(True, "A")],
                     [request.key, self.request("B").key, self.request("C").key]):
            with self.subTest(keys=keys), self.assertRaises(ValueError):
                self.scheduler.reconcile_identities(keys)
            self.assertTrue(self.scheduler.contains(request.request_id))

    def test_tombstone_capacity_and_ttl_are_bounded_without_reusing_old_generations(self):
        old = self.accepted()
        self.scheduler.retire_identity(old.request.key)
        for index in range(5):
            floor = self.scheduler.diagnostics()["minimum_new_identity_generation"]
            current = self.accepted("NEW" + str(index), floor)
            self.scheduler.retire_identity(current.request.key)
        report = self.scheduler.diagnostics()
        self.assertEqual(report["tombstone_count"], 2)
        self.assertEqual(report["tombstones_evicted"], 4)
        self.clock.value = 100
        self.assertEqual(self.scheduler.diagnostics()["tombstone_count"], 0)
        self.assertFalse(self.scheduler.submit(self.request("A", 0)))
        floor = self.scheduler.diagnostics()["minimum_new_identity_generation"]
        self.assertTrue(self.scheduler.submit(self.request("A", floor)))
        self.assertFalse(self.scheduler.complete(old.request, status_code=200, response_time_ms=100).accepted)
        self.assertFalse(self.valid(old))

    def test_more_than_4096_historical_identities_do_not_fill_active_capacity(self):
        for index in range(4100):
            floor = self.scheduler.diagnostics()["minimum_new_identity_generation"]
            request = self.request("HISTORY" + str(index), floor)
            self.assertTrue(self.scheduler.submit(request))
            self.scheduler.cancel(request.request_id)
            self.scheduler.retire_identity(request.key)
        report = self.scheduler.diagnostics()
        self.assertEqual((report["identity_count"], report["tombstone_count"]), (0, 2))
        self.assertTrue(self.scheduler.submit(self.request("CURRENT", report["minimum_new_identity_generation"])))

    def test_accepted_completion_requires_exact_request_key_generation_and_scheduler(self):
        accepted = self.accepted()
        self.assertTrue(self.valid(accepted))
        for changes in ({"request_id": "wrong"}, {"instrument_id": "B"}, {"generation": 1},
                        {"endpoint": "/fapi/v1/time"}, {"event_time_ms": 1}):
            with self.subTest(changes=changes):
                self.assertFalse(self.valid(accepted, **changes))
        self.assertFalse(self.valid(replace(accepted)))
        other = RestScheduler(clock=self.clock, coordinator=FakeCoordinator())
        self.assertFalse(other.validate_completion(accepted, request_id=accepted.request.request_id,
                                                  generation=0, instrument_id="A", now_ms=0, event_time_ms=0))

    def test_completion_expires_on_new_generation_retirement_and_deadline(self):
        accepted = self.accepted()
        self.scheduler.submit(self.request(generation=1))
        self.assertFalse(self.valid(accepted))
        current, = self.scheduler.poll_due()
        newer = self.scheduler.complete(current, status_code=200, response_time_ms=0)
        self.assertTrue(self.valid(newer))
        self.scheduler.retire_identity(current.key)
        self.assertFalse(self.valid(newer))
        accepted_b = self.accepted("B", self.scheduler.diagnostics()["minimum_new_identity_generation"])
        self.clock.value = 1000
        self.assertFalse(self.valid(accepted_b))

    def test_retired_inflight_timeout_does_not_retry_or_forget_other_identity(self):
        request = self.request()
        self.scheduler.submit(request)
        self.scheduler.poll_due()
        self.scheduler.retire_identity(request.key)
        self.clock.value = 1000
        self.assertEqual(self.scheduler.poll_due(), ())
        self.assertEqual(self.scheduler.diagnostics()["retries"], 0)
        self.assertEqual(self.scheduler.diagnostics()["identity_count"], 0)

    def test_inflight_retirement_survives_tombstone_ttl_until_request_is_terminal(self):
        request = self.request(ttl=1000)
        self.scheduler.submit(request)
        self.scheduler.poll_due()
        self.scheduler.retire_identity(request.key)
        self.clock.value = 101
        report = self.scheduler.diagnostics()
        self.assertEqual((report["identity_count"], report["retiring_identity_count"],
                          report["tombstone_count"], report["inflight"]), (1, 1, 0, 1))
        self.assertFalse(self.scheduler.submit(self.request(generation=1)))
        result = self.scheduler.complete(request, status_code=200, response_time_ms=101)
        self.assertEqual(result.reason, "retired_identity")
        self.assertFalse(self.scheduler.owns_completion(result))
        self.assertEqual(self.scheduler.diagnostics()["tombstone_count"], 1)
        self.clock.value = 200
        self.assertEqual(self.scheduler.diagnostics()["tombstone_count"], 1)
        self.clock.value = 201
        self.assertEqual(self.scheduler.diagnostics()["tombstone_count"], 0)

    def test_readding_identity_during_retirement_cannot_resurrect_inflight_work(self):
        old = self.request()
        self.scheduler.submit(old)
        self.scheduler.poll_due()
        self.scheduler.reconcile_identities(set())
        self.scheduler.reconcile_identities({old.key})
        self.assertFalse(self.scheduler.submit(self.request(generation=1)))
        self.assertFalse(self.scheduler.complete(old, status_code=200, response_time_ms=0).accepted)
        self.assertTrue(self.scheduler.submit(self.request(generation=1)))
        current, = self.scheduler.poll_due()
        receipt = self.scheduler.complete(current, status_code=200, response_time_ms=0)
        self.assertTrue(self.valid(receipt))
        self.assertFalse(self.scheduler.complete(old, status_code=200, response_time_ms=0).accepted)
        self.assertTrue(self.valid(receipt))

    def test_duplicate_response_and_stale_retry_cannot_replace_accepted_receipt(self):
        request = self.request(ttl=10000)
        self.scheduler.submit(request)
        self.scheduler.poll_due()
        failed = self.scheduler.complete(request, status_code=500, response_time_ms=0)
        self.assertTrue(failed.retry_scheduled)
        self.assertFalse(self.valid(failed))
        self.clock.value = 2000
        retry, = self.scheduler.poll_due()
        accepted = self.scheduler.complete(retry, status_code=200, response_time_ms=2000)
        self.assertTrue(self.valid(accepted))
        for stale in (request, retry):
            self.assertFalse(self.scheduler.complete(stale, status_code=200, response_time_ms=2000).accepted)
            self.assertTrue(self.valid(accepted))
        self.assertFalse(self.scheduler.owns_completion(failed))
        self.assertEqual(self.scheduler.diagnostics()["completion_receipt_count"], 1)

    def test_malformed_completion_is_not_a_receipt_and_cannot_consume_pending_work(self):
        request = self.request()
        self.scheduler.submit(request)
        self.scheduler.poll_due()
        self.assertFalse(self.scheduler.owns_completion(Completion(None, True, False, "completed")))
        with self.assertRaisesRegex(ValueError, "request_spec_required"):
            self.scheduler.complete(None, status_code=200, response_time_ms=0)
        self.assertTrue(self.scheduler.contains(request.request_id))

    def test_completion_correlation_rejects_non_native_identity_and_generation_types(self):
        accepted = self.accepted()
        for changes in ({"request_id": True}, {"instrument_id": None}, {"generation": True},
                        {"generation": 0.0}, {"event_time_ms": 0.0}, {"event_time_ms": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.valid(accepted, **changes)
        self.assertTrue(self.valid(accepted))

    def test_oi_planner_uses_generation_floor_after_historical_identity_retirement(self):
        old = self.accepted("HISTORICAL", generation=9999)
        self.scheduler.retire_identity(old.request.key)
        planner = OiSamplingPlanner()
        planner.update_universe({"CURRENT": "HOT"}, 0)
        requests = planner.schedule(self.scheduler, 0)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].generation, 10000)
        current, = self.scheduler.poll_due()
        accepted = self.scheduler.complete(current, status_code=200, response_time_ms=0)
        self.assertTrue(planner.record_completion(self.scheduler, accepted, 0, 0))
        self.assertEqual(planner.coverage(0)["coverage"], 1)

    def test_oi_result_without_request_correlation_is_rejected(self):
        planner = OiSamplingPlanner()
        planner.update_universe({"A": "NORMAL"}, 0)
        with self.assertRaisesRegex(ValueError, "correlation"):
            planner.record_result("A", 0, 0)
        self.assertEqual(planner.coverage(0)["coverage"], 0)

    def test_oi_result_from_stale_generation_never_updates_coverage(self):
        planner = OiSamplingPlanner()
        planner.update_universe({"A": "NORMAL"}, 0)
        planner.schedule(self.scheduler, 0)
        current, = self.scheduler.poll_due()
        accepted = self.scheduler.complete(current, status_code=200, response_time_ms=0)
        self.assertTrue(planner.record_completion(self.scheduler, accepted, 0, 0))
        self.clock.value = 1
        self.scheduler.submit(self.request(generation=current.generation + 1))
        self.assertFalse(planner.record_completion(self.scheduler, accepted, 1, 1))
        self.assertEqual(planner.coverage(1)["oldest_age_ms"], 1)


if __name__ == "__main__":
    unittest.main()
