"""Offline REST scheduling contracts. No transport or database is constructed."""
from dataclasses import replace
import unittest

from radars.altcoin_hunter.rest_budget import FakeCoordinator, make_request
from radars.altcoin_hunter.rest_scheduler import OiSamplingPlanner, RestScheduler


class VirtualClock:
    def __init__(self, value=0):
        self.value = value

    def __call__(self):
        return self.value


class BudgetTests(unittest.TestCase):
    def test_native_identity_strings_reject_subclasses_and_hidden_controls(self):
        class StringSubclass(str):
            pass
        for value in (StringSubclass("BTCUSDT"), True, 1.0, "BTC\u200bUSDT"):
            with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                make_request("openInterest", 0, instrument_id=value)

    def test_official_endpoint_weight_and_separate_funding_budget(self):
        for endpoint, instrument, weight, budget in (
            ("exchangeInfo", None, 1, "request_weight"),
            ("serverTime", None, 1, "request_weight"),
            ("openInterest", "BTCUSDT", 1, "request_weight"),
            ("fundingInfo", None, 0, "funding_requests"),
            ("bookTicker", "BTCUSDT", 2, "request_weight"),
            ("bookTicker", None, 5, "request_weight"),
        ):
            with self.subTest(endpoint=endpoint, instrument=instrument):
                request = make_request(endpoint, 0, instrument_id=instrument)
                self.assertEqual((request.logical_weight, request.budget_class), (weight, budget))
                self.assertEqual(request.method, "GET")

    def test_request_cannot_forge_weights_trade_endpoint_or_invalid_time(self):
        valid = make_request("openInterest", 0, instrument_id="BTCUSDT")
        for fields in ({"logical_weight": 0}, {"budget_class": "funding_requests"}, {"method": "POST"},
                       {"endpoint": "/fapi/v1/order"}, {"instrument_id": None}, {"priority": "AI"},
                       {"generation": True}, {"not_before_ms": -1}, {"deadline_ms": 0, "not_before_ms": 1}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                replace(valid, **fields)

    def test_funding_weight_zero_still_exhausts_500_requests_and_recovers_after_five_minutes(self):
        budget = FakeCoordinator()
        request = make_request("fundingInfo", 0)
        for _ in range(500):
            self.assertTrue(budget.reserve(request, 0).allowed)
        result = budget.reserve(request, 0)
        self.assertFalse(result.allowed)
        self.assertEqual(result.retry_at_ms, 300000)
        self.assertFalse(budget.reserve(request, 299999).allowed)
        self.assertTrue(budget.reserve(request, 300000).allowed)
        self.assertEqual(budget.diagnostics(300000)["weight_used"], 0)

    def test_high_reserve_and_ip_feedback_are_shared_and_conservative(self):
        budget = FakeCoordinator(weight_limit=5, high_reserve=2)
        normal = make_request("openInterest", 0, instrument_id="A")
        high = make_request("openInterest", 0, instrument_id="B", priority="HOT")
        for _ in range(3):
            self.assertTrue(budget.reserve(normal, 0).allowed)
        self.assertEqual(budget.reserve(normal, 0).reason, "high_reserve")
        self.assertTrue(budget.reserve(high, 0).allowed)
        budget.observe(high, {"X-MBX-USED-WEIGHT-1M": "5"}, 0)
        self.assertFalse(budget.reserve(high, 0).allowed)
        budget.observe(high, {"X-MBX-USED-WEIGHT-1M": "0"}, 0)
        self.assertEqual(budget.diagnostics(0)["weight_used"], 5)
        self.assertTrue(budget.reserve(normal, 60000).allowed)

    def test_inaccurate_book_ticker_header_is_ignored(self):
        budget = FakeCoordinator()
        request = make_request("bookTicker", 0)
        self.assertTrue(budget.reserve(request, 0).allowed)
        budget.observe(request, {"x-mbx-used-weight-1m": "999999"}, 0)
        self.assertEqual(budget.diagnostics(0)["weight_used"], 5)


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        self.clock = VirtualClock()
        self.budget = FakeCoordinator()
        self.scheduler = RestScheduler(clock=self.clock, coordinator=self.budget, jitter=lambda _request, _base: 0)

    def request(self, instrument="BTCUSDT", *, priority="NORMAL", ttl_ms=600000, generation=0):
        return make_request("openInterest", self.clock.value, instrument_id=instrument,
                            priority=priority, ttl_ms=ttl_ms, generation=generation)

    def test_response_headers_are_bounded_before_consuming_inflight_result(self):
        self.scheduler.submit(self.request())
        request, = self.scheduler.poll_due()
        for headers in ([], {str(index): "0" for index in range(65)}, {"a" * 129: "0"},
                        {"Retry-After": "0" * 4097}, {"X": "0", "x": "1"}):
            with self.subTest(headers_type=type(headers)), self.assertRaises(ValueError):
                self.scheduler.complete(request, status_code=200, response_time_ms=0, headers=headers)
            self.assertTrue(self.scheduler.contains(request.request_id))

    def test_feedback_failure_blocks_admission_until_explicit_coordinator_recovery(self):
        class BrokenFeedback(FakeCoordinator):
            def observe(self, request, headers, now_ms):
                raise RuntimeError("offline feedback unavailable")
        scheduler = RestScheduler(clock=self.clock, coordinator=BrokenFeedback())
        scheduler.submit(self.request())
        request, = scheduler.poll_due()
        scheduler.complete(request, status_code=200, response_time_ms=0)
        scheduler.submit(self.request("OTHER"))
        self.assertEqual(scheduler.poll_due(), ())
        self.assertFalse(scheduler.diagnostics()["budget_trusted"])
        scheduler.restore_budget(self.budget)
        self.assertEqual(len(scheduler.poll_due()), 1)

    def test_explicit_budget_recovery_does_not_clear_known_source_ban(self):
        self.scheduler.submit(self.request())
        request, = self.scheduler.poll_due()
        self.scheduler.complete(request, status_code=418, response_time_ms=0)
        replacement = FakeCoordinator()
        self.scheduler.restore_budget(replacement)
        self.assertEqual(replacement.diagnostics(0)["source_cooldown_until_ms"], 120000)
        self.clock.value = 119999
        self.assertEqual(self.scheduler.poll_due(), ())

    def test_oi_result_identity_is_native_and_cannot_use_string_subclass(self):
        class StringSubclass(str):
            pass
        planner = OiSamplingPlanner()
        planner.update_universe({"BTCUSDT": "NORMAL"}, 0)
        with self.assertRaises(ValueError):
            planner.record_result(StringSubclass("BTCUSDT"), 0, 0)

    def test_request_id_collision_cannot_overwrite_a_different_instrument(self):
        first = self.request("FIRST")
        second = replace(self.request("SECOND"), request_id=first.request_id)
        self.assertTrue(self.scheduler.submit(first))
        self.assertFalse(self.scheduler.submit(second))
        self.assertEqual(self.scheduler.poll_due(), (first,))

    def test_admission_coverage_counts_unique_requests_and_retries_do_not_inflate_it(self):
        self.scheduler.submit(self.request("FIRST"))
        self.scheduler.submit(self.request("SECOND"))
        first, = self.scheduler.poll_due(limit=1)
        self.scheduler.complete(first, status_code=500, response_time_ms=0)
        report = self.scheduler.diagnostics()
        self.assertEqual((report["coverage"], report["coverage_numerator"], report["coverage_denominator"]), (.5, 1, 2))
        self.assertEqual(report["coverage_basis"], "unique_admitted_requests/submitted_requests")
        self.clock.value = 1000
        for request in self.scheduler.poll_due():
            self.scheduler.complete(request, status_code=200, response_time_ms=1000)
        self.assertEqual(self.scheduler.diagnostics()["coverage"], 1)

    def test_live_admission_requires_real_shared_coordinator_and_offline_missing_fails_closed(self):
        for budget in (None, FakeCoordinator()):
            with self.subTest(budget=budget), self.assertRaisesRegex(ValueError, "live_requires_shared"):
                RestScheduler(clock=self.clock, coordinator=budget, live=True)
        scheduler = RestScheduler(clock=self.clock, coordinator=None)
        self.assertTrue(scheduler.submit(self.request()))
        self.assertEqual(scheduler.poll_due(), ())
        self.assertEqual(scheduler.diagnostics()["budget_blocked"], 1)

    def test_failed_coordinator_never_falls_back_to_unbudgeted_admission(self):
        class BrokenCoordinator(FakeCoordinator):
            def reserve(self, request, now_ms):
                raise RuntimeError("offline coordinator unavailable")
        scheduler = RestScheduler(clock=self.clock, coordinator=BrokenCoordinator())
        scheduler.submit(self.request())
        self.assertEqual(scheduler.poll_due(), ())
        self.assertEqual(scheduler.diagnostics()["queue_depth"], 1)

    def test_due_time_completion_and_bounded_diagnostics(self):
        request = replace(self.request(), not_before_ms=1000)
        self.scheduler.submit(request)
        self.assertEqual(self.scheduler.poll_due(), ())
        self.assertEqual(self.scheduler.diagnostics()["delayed"], 1)
        self.clock.value = 1000
        self.assertEqual(self.scheduler.poll_due(), (request,))
        result = self.scheduler.complete(request, status_code=200, response_time_ms=1000)
        self.assertTrue(result.accepted)
        self.assertFalse(self.scheduler.complete(request, status_code=200, response_time_ms=1000).accepted)
        report = self.scheduler.diagnostics()
        self.assertEqual((report["completed"], report["stale"], report["queue_depth"], report["inflight"]), (1, 1, 0, 0))
        self.assertIsNone(report["oldest_age_ms"])

    def test_5xx_exponential_backoff_and_maximum_attempts(self):
        self.scheduler.submit(self.request())
        attempts = []
        for now, next_time in ((0, 1000), (1000, 3000), (3000, None)):
            self.clock.value = now
            request, = self.scheduler.poll_due()
            attempts.append(request.retry_count)
            result = self.scheduler.complete(request, status_code=503, response_time_ms=now)
            self.assertEqual(result.retry_scheduled, next_time is not None)
            if next_time is not None:
                self.clock.value = next_time - 1
                self.assertEqual(self.scheduler.poll_due(), ())
        self.assertEqual(attempts, [0, 1, 2])
        self.assertEqual(result.reason, "retry_exhausted")
        self.assertEqual(self.budget.diagnostics(3000)["weight_used"], 3)

    def test_bounded_positive_jitter_cannot_advance_retry_before_exponential_floor(self):
        scheduler = RestScheduler(clock=self.clock, coordinator=self.budget, jitter=lambda _request, _base: 250)
        scheduler.submit(self.request())
        request, = scheduler.poll_due()
        scheduler.complete(request, status_code=500, response_time_ms=0)
        self.clock.value = 1249
        self.assertEqual(scheduler.poll_due(), ())
        self.clock.value = 1250
        self.assertEqual(len(scheduler.poll_due()), 1)

    def test_400_401_403_404_do_not_retry(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                self.scheduler.submit(self.request(str(status)))
                request, = self.scheduler.poll_due()
                result = self.scheduler.complete(request, status_code=status, response_time_ms=0)
                self.assertFalse(result.retry_scheduled)
                self.assertEqual(result.reason, "nonretryable_status")
        self.assertEqual(self.scheduler.diagnostics()["retries"], 0)

    def test_explicit_408_and_missing_response_timeout_retry_without_sleep(self):
        self.scheduler.submit(self.request())
        request, = self.scheduler.poll_due()
        self.assertTrue(self.scheduler.complete(request, status_code=408, response_time_ms=0).retry_scheduled)
        self.clock.value = 1000
        retry, = self.scheduler.poll_due()
        self.assertEqual(retry.retry_count, 1)
        self.clock.value = 6000
        self.assertEqual(self.scheduler.poll_due(), ())
        self.assertEqual(self.scheduler.diagnostics()["timeouts"], 2)
        self.clock.value = 8000
        self.assertEqual(self.scheduler.poll_due()[0].retry_count, 2)

    def test_429_retry_after_is_never_shortened_and_pauses_other_symbols(self):
        self.scheduler.submit(self.request())
        request, = self.scheduler.poll_due()
        result = self.scheduler.complete(request, status_code=429, response_time_ms=0, headers={"Retry-After": "2.5"})
        self.assertTrue(result.retry_scheduled)
        self.scheduler.submit(self.request("OTHER"))
        self.clock.value = 2499
        self.assertEqual(self.scheduler.poll_due(), ())
        self.clock.value = 2500
        self.assertEqual(len(self.scheduler.poll_due()), 2)

    def test_418_ban_is_shared_across_schedulers_symbols_and_budget_classes(self):
        other = RestScheduler(clock=self.clock, coordinator=self.budget)
        self.scheduler.submit(self.request())
        request, = self.scheduler.poll_due()
        self.scheduler.complete(request, status_code=418, response_time_ms=0, headers={"retry-after": "180"})
        other.submit(make_request("fundingInfo", 0, ttl_ms=600000))
        self.clock.value = 179999
        self.assertEqual(other.poll_due(), ())
        self.assertEqual(self.budget.diagnostics(179999)["source_cooldown_until_ms"], 180000)
        self.clock.value = 180000
        self.assertEqual(len(other.poll_due()), 1)

    def test_418_without_header_has_conservative_two_minute_source_cooldown(self):
        self.scheduler.submit(self.request(ttl_ms=15000))
        request, = self.scheduler.poll_due()
        result = self.scheduler.complete(request, status_code=418, response_time_ms=0)
        self.assertEqual(result.reason, "retry_after_deadline")
        self.assertEqual(self.budget.diagnostics(0)["source_cooldown_until_ms"], 120000)

    def test_long_retry_after_drops_request_instead_of_reducing_server_wait(self):
        self.scheduler.submit(self.request(ttl_ms=15000))
        request, = self.scheduler.poll_due()
        result = self.scheduler.complete(request, status_code=429, response_time_ms=0, headers={"Retry-After": "3600"})
        self.assertEqual(result.reason, "retry_after_deadline")
        self.assertEqual(self.budget.diagnostics(0)["source_cooldown_until_ms"], 3600000)

    def test_http_date_retry_after_uses_injected_time(self):
        self.scheduler.submit(self.request())
        request, = self.scheduler.poll_due()
        self.scheduler.complete(request, status_code=429, response_time_ms=0,
                                headers={"Retry-After": "Thu, 01 Jan 1970 00:00:10 GMT"})
        self.clock.value = 9999
        self.assertEqual(self.scheduler.poll_due(), ())
        self.clock.value = 10000
        self.assertEqual(len(self.scheduler.poll_due()), 1)

    def test_cancel_supersede_and_late_deadline_responses_are_rejected(self):
        old = self.request()
        self.scheduler.submit(old)
        self.scheduler.poll_due()
        new = self.request(generation=1)
        self.assertTrue(self.scheduler.submit(new))
        self.assertFalse(self.scheduler.complete(old, status_code=200, response_time_ms=0).accepted)
        self.scheduler.poll_due()
        self.scheduler.cancel(new.request_id)
        self.assertFalse(self.scheduler.complete(new, status_code=200, response_time_ms=0).accepted)
        self.assertFalse(self.scheduler.submit(new))
        late = self.request("LATE", ttl_ms=10)
        self.scheduler.submit(late)
        self.scheduler.poll_due()
        self.clock.value = 10
        self.assertEqual(self.scheduler.complete(late, status_code=200, response_time_ms=10).reason, "deadline_expired")

    def test_stale_cancelled_418_still_propagates_ip_ban(self):
        self.scheduler.submit(self.request())
        request, = self.scheduler.poll_due()
        self.scheduler.cancel(request.request_id)
        result = self.scheduler.complete(request, status_code=418, response_time_ms=0)
        self.assertFalse(result.accepted)
        self.assertEqual(self.budget.diagnostics(0)["source_cooldown_until_ms"], 120000)

    def test_queue_inflight_identity_memory_and_expiry_are_bounded(self):
        scheduler = RestScheduler(clock=self.clock, coordinator=self.budget, max_queue=3, max_inflight=1, max_identities=3)
        for index in range(3):
            self.assertTrue(scheduler.submit(self.request(str(index), ttl_ms=100)))
        self.assertFalse(scheduler.submit(self.request("overflow", ttl_ms=100)))
        self.assertEqual(len(scheduler.poll_due(limit=100)), 1)
        report = scheduler.diagnostics()
        self.assertEqual((report["queue_depth"], report["inflight"], report["identity_count"]), (2, 1, 3))
        self.clock.value = 100
        self.assertEqual(scheduler.poll_due(), ())
        self.assertEqual(scheduler.diagnostics()["stale"], 3)
        self.assertFalse(scheduler.submit(self.request("new-identity")))

    def test_weighted_fairness_makes_progress_for_both_tiers(self):
        scheduler = RestScheduler(clock=self.clock, coordinator=self.budget, max_inflight=1)
        for index in range(12):
            scheduler.submit(self.request("HIGH" + str(index), priority="HOT"))
        for index in range(4):
            scheduler.submit(self.request("NORMAL" + str(index)))
        priorities = []
        for _ in range(16):
            request, = scheduler.poll_due()
            priorities.append(request.priority)
            scheduler.complete(request, status_code=200, response_time_ms=0)
        self.assertEqual(priorities, ["HOT", "HOT", "HOT", "NORMAL"] * 4)

    def test_fairness_survives_one_request_per_budget_window(self):
        budget = FakeCoordinator(weight_limit=1, high_reserve=0)
        scheduler = RestScheduler(clock=self.clock, coordinator=budget, max_inflight=1)
        for index in range(8):
            scheduler.submit(self.request(str(index), priority="HOT" if index < 6 else "NORMAL"))
        result = []
        for minute in range(8):
            self.clock.value = minute * 60000
            request, = scheduler.poll_due()
            result.append(request.priority)
            scheduler.complete(request, status_code=200, response_time_ms=self.clock.value)
        self.assertEqual(result, ["HOT", "HOT", "HOT", "NORMAL"] * 2)


class OiPlannerTests(unittest.TestCase):
    def test_1000_instruments_high_cap_and_interval_plan_are_explicit(self):
        clock = VirtualClock()
        planner = OiSamplingPlanner()
        universe = {f"COIN{index:04}USDT": "HOT" if index < 100 else "NORMAL" for index in range(1000)}
        planner.update_universe(universe, 0)
        scheduler = RestScheduler(clock=clock, coordinator=FakeCoordinator())
        specs = planner.schedule(scheduler, 0)
        self.assertEqual(len(specs), 1000)
        self.assertEqual(sum(item.priority == "HOT" for item in specs), 80)
        self.assertEqual(sum(item.deadline_ms == 60000 for item in specs), 80)
        report = planner.coverage(0)
        self.assertEqual((report["high_requested"], report["high_selected"], report["high_overflow"]), (100, 80, 20))
        self.assertEqual(report["high_selection_coverage"], .8)
        self.assertEqual(report["coverage"], 0)
        self.assertIsNone(report["oldest_age_ms"])
        completed = 0
        while due := scheduler.poll_due():
            for request in due:
                outcome = scheduler.complete(request, status_code=200, response_time_ms=0)
                self.assertTrue(outcome.accepted)
                planner.record_result(request.instrument_id, 0, 0)
                completed += 1
        self.assertEqual(completed, 1000)
        self.assertEqual(planner.coverage(0)["coverage"], 1)
        clock.value = 60000
        self.assertEqual(len(planner.schedule(scheduler, 60000)), 80)
        self.assertEqual(planner.coverage(60000)["coverage"], .9)
        self.assertEqual(planner.coverage(60000)["oldest_age_ms"], 60000)

    def test_high_selection_prefers_extreme_and_hunter_with_stable_bounded_overflow(self):
        planner = OiSamplingPlanner(high_cap=2)
        planner.update_universe({"A": "HOT", "B": "EXTREME", "C": "HUNTER"}, 0)
        scheduler = RestScheduler(clock=VirtualClock(), coordinator=FakeCoordinator())
        requests = planner.schedule(scheduler, 0)
        self.assertEqual({item.instrument_id for item in requests if item.priority != "NORMAL"}, {"B", "C"})
        self.assertEqual(planner.coverage(0)["high_overflow"], 1)

    def test_failures_do_not_erase_last_good_and_future_or_regressing_data_is_rejected(self):
        planner = OiSamplingPlanner()
        planner.update_universe({"A": "NORMAL", "B": "HOT"}, 0)
        self.assertTrue(planner.record_result("A", 1000, 1000))
        self.assertFalse(planner.record_result("A", None, 2000, success=False))
        self.assertFalse(planner.record_result("A", 3000, 2000))
        self.assertFalse(planner.record_result("A", 999, 2000))
        report = planner.coverage(2000)
        self.assertEqual((report["coverage"], report["oldest_age_ms"], report["missing_instruments"], report["oi_failures"]), (.5, 1000, 1, 3))

    def test_empty_coverage_is_null_and_oversized_universe_keeps_previous_state(self):
        planner = OiSamplingPlanner(max_instruments=2)
        self.assertIsNone(planner.coverage(0)["coverage"])
        planner.update_universe({"A": "NORMAL"}, 0)
        with self.assertRaises(ValueError):
            planner.update_universe({"A": "NORMAL", "B": "NORMAL", "C": "NORMAL"}, 0)
        self.assertEqual(planner.coverage(0)["instruments"], 1)

    def test_pending_oi_retry_prevents_duplicate_scheduling_and_failure_does_not_gate_other_inputs(self):
        clock = VirtualClock()
        planner = OiSamplingPlanner()
        planner.update_universe({"A": "HOT"}, 0)
        scheduler = RestScheduler(clock=clock, coordinator=FakeCoordinator())
        self.assertEqual(len(planner.schedule(scheduler, 0)), 1)
        self.assertEqual(planner.schedule(scheduler, 0), ())
        request, = scheduler.poll_due()
        scheduler.complete(request, status_code=500, response_time_ms=0)
        planner.record_result("A", None, 0, success=False)
        self.assertEqual(planner.schedule(scheduler, 0), ())
        # The planner changes only OI coverage; no trade or aggregation object
        # is imported, controlled, or paused on this failure path.
        self.assertEqual(planner.coverage(0)["oi_failures"], 1)
        self.assertTrue(scheduler.contains(request.request_id))


if __name__ == "__main__":
    unittest.main()
