from dataclasses import FrozenInstanceError, replace
import json
import math
import unittest

from radars.altcoin_hunter.baselines import BaselineEngine, BaselineKey, BaselinePolicy, RollingBaseline


START = 1_800_000_000_000


class BaselineTests(unittest.TestCase):
    def policy(self, **kwargs):
        return BaselinePolicy(min_sample_count=3, min_span_ms=120_000, **kwargs)

    def test_current_value_excluded_and_zero_mad_floored(self):
        baseline = RollingBaseline(self.policy(metric_floor=2))
        for minute in range(3):
            result = baseline.evaluate_and_observe(START + minute * 60_000, "10")
            self.assertFalse(result.ready)
        result = baseline.evaluate_and_observe(START + 3 * 60_000, "30")
        self.assertEqual((result.median, result.mad, result.sample_count), (10, 0, 3))
        self.assertEqual((result.robust_z, result.unclipped_z), (6, 10))
        self.assertTrue(result.clipped)
        self.assertEqual(result.raw_value, "30")

    def test_true_median_mad_and_ewma(self):
        baseline = RollingBaseline(self.policy(ewma_alpha=0.5))
        for minute, value in enumerate((1, 2, 6)):
            baseline.evaluate_and_observe(START + minute * 60_000, value)
        result = baseline.evaluate(START + 180_000, 5)
        self.assertAlmostEqual(result.robust_z, 3 / 1.4826)
        self.assertEqual((result.median, result.mad, result.ewma), (2, 1, 3.75))

    def test_missing_observation_reduces_coverage(self):
        baseline = RollingBaseline(self.policy())
        for minute, value in enumerate((1, None, 3, 4)):
            baseline.evaluate_and_observe(START + minute * 60_000, value)
        result = baseline.evaluate(START + 240_000, 9)
        self.assertEqual(result.sample_count, 3)
        self.assertEqual(result.coverage_ratio, 0.75)
        self.assertFalse(result.ready)
        self.assertIn("insufficient_coverage", result.reason_codes)

    def test_absent_time_slots_are_not_silently_dropped(self):
        baseline = RollingBaseline(self.policy())
        for minute in (0, 1, 3):
            baseline.evaluate_and_observe(START + minute * 60_000, minute)
        result = baseline.evaluate(START + 240_000, 5)
        self.assertEqual(result.expected_sample_count, 4)
        self.assertEqual(result.coverage_ratio, 0.75)

    def test_span_is_historical_not_waiting_time(self):
        baseline = RollingBaseline(BaselinePolicy(min_sample_count=1, min_span_ms=1, min_coverage_ratio=0.01))
        baseline.evaluate_and_observe(START, 2)
        result = baseline.evaluate(START + 60_000, 3)
        self.assertFalse(result.ready)
        self.assertEqual(result.wall_clock_span_ms, 0)

    def test_policy_is_immutable_strict_and_per_window(self):
        policy = self.policy()
        with self.assertRaises(FrozenInstanceError):
            policy.min_span_ms = 0
        for kwargs in ({"sampling_stride": 0}, {"max_samples": True}, {"metric_floor": float("nan")},
                       {"min_coverage_ratio": 1.1}, {"ewma_alpha": 0}, {"baseline_version": "2"},
                       {"min_sample_count": 9999}, {"sample_interval_ms": 0}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                BaselinePolicy(**kwargs)
        engine = BaselineEngine()
        one = BaselineKey("fixture", "binance", "usdt_perpetual", "AAA", "return", 60)
        hour = replace(one, window_sec=3600)
        engine.register(one, policy)
        engine.register(hour, replace(policy, min_sample_count=10, sampling_stride=60))
        for minute in range(4):
            first = engine.evaluate_and_observe(one, START + minute * 60_000, 1)
            second = engine.evaluate_and_observe(hour, START + minute * 60_000, 1)
        self.assertTrue(first.ready)
        self.assertFalse(second.ready)

    def test_nonfinite_bool_and_unrepresentable_inputs_rejected(self):
        baseline = RollingBaseline(self.policy())
        for value in (float("nan"), "Infinity", "-Inf", "1e10000", "1e-400", True, object()):
            with self.subTest(value=str(value)), self.assertRaises(ValueError):
                baseline.evaluate_and_observe(START, value)
        self.assertEqual(baseline.retained_sample_count, 0)

    def test_finite_extremes_never_export_infinity(self):
        baseline = RollingBaseline(self.policy())
        for minute, value in enumerate(("-1.7e308", "1.7e308", "1.7e308")):
            baseline.evaluate_and_observe(START + minute * 60_000, value)
        result = baseline.evaluate(START + 180_000, "-1.7e308")
        json.dumps(result.to_dict(), allow_nan=False)

    def test_current_missing_is_null_with_reason(self):
        result = RollingBaseline(self.policy()).evaluate(START, None)
        self.assertIsNone(result.raw_value)
        self.assertIsNone(result.robust_z)
        self.assertIn("missing_current_value", result.reason_codes)

    def test_stride_bound_and_roundtrip_determinism(self):
        policy = BaselinePolicy(max_samples=3, min_sample_count=2, min_span_ms=120_000, sampling_stride=2)
        baseline = RollingBaseline(policy)
        for minute in range(12):
            baseline.evaluate_and_observe(START + minute * 60_000, str(minute))
        self.assertEqual(baseline.retained_sample_count, 3)
        key = BaselineKey("fixture", "binance", "usdt_perpetual", "AAA", "delta", 180)
        record = json.loads(json.dumps(baseline.export(key)))
        restored = RollingBaseline.restore(record)
        self.assertEqual(baseline.evaluate(START + 720_000, 20), restored.evaluate(START + 720_000, 20))
        mismatched = {**record, "updated_at_ms": record["updated_at_ms"] + 1}
        with self.assertRaises(ValueError):
            RollingBaseline.restore(mismatched)
        record["config_hash"] = "tampered"
        with self.assertRaises(ValueError):
            RollingBaseline.restore(record)

    def test_no_future_data_and_no_clock_rewind(self):
        baseline = RollingBaseline(self.policy())
        baseline.evaluate_and_observe(START, 1)
        baseline.evaluate_and_observe(START + 60_000, 999)
        past = baseline.evaluate(START + 30_000, 2)
        self.assertEqual(past.median, 1)
        with self.assertRaises(ValueError):
            baseline.evaluate_and_observe(START, 2)

    def test_engine_export_restore_and_capacity(self):
        engine = BaselineEngine(max_series=1)
        key = BaselineKey("fixture", "binance", "usdt_perpetual", "AAA", "return", 60)
        engine.register(key, self.policy())
        engine.evaluate_and_observe(key, START, 1)
        with self.assertRaises(OverflowError):
            engine.register(replace(key, instrument_id="BBB"), self.policy())
        restored = BaselineEngine(max_series=1)
        restored.restore(list(engine.export()))
        self.assertEqual(engine.export(), restored.export())
        self.assertEqual(engine.evaluate_and_observe(key, START + 60_000, 2),
                         restored.evaluate_and_observe(key, START + 60_000, 2))


if __name__ == "__main__":
    unittest.main()
