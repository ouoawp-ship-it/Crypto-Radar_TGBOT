import unittest

from .binance_capacity import measure_oi, measure_parser, measure_plans


class BinanceCapacityTests(unittest.TestCase):
    def test_600_1000_1500_route_capacity_and_minimal_migration(self):
        for count in (600, 1000, 1500):
            with self.subTest(instruments=count):
                result = measure_plans(count)
                self.assertEqual(result["market_connections"], 1 if count == 600 else 2)
                self.assertEqual(result["public_connections"], 1)
                self.assertEqual(result["uncovered_instruments"], 0)
                self.assertEqual(result["promoted"], count // 20)

    def test_2000_item_arrays_isolate_1_10_50_percent_bad_elements(self):
        for percent in (1, 10, 50):
            with self.subTest(percent=percent):
                result = measure_parser(percent)
                self.assertEqual(result["rejected_items"], 20 * percent)
                self.assertLessEqual(result["rejected_details"], 64)

    def test_oi_1500_budget_and_cap_preserve_explicit_overflow(self):
        result = measure_oi()
        self.assertEqual(result["initial"]["covered_instruments"], 1500)
        self.assertEqual(result["initial"]["high_selected"], 80)
        self.assertEqual(result["initial"]["high_overflow"], 20)
        self.assertEqual(result["due_at_60s"], 80)
