from __future__ import annotations

import unittest

from .capacity import run_capacity


class CapacityTests(unittest.TestCase):
    def test_600_instruments_normal_stream(self):
        result = run_capacity(instruments=600, minutes=2, trace_memory=False)
        self.assertEqual(result["events"], 2400)
        self.assertEqual(result["bucket_count"], 1200)
        self.assertEqual(result["database_rows"]["instruments"], 600)
        self.assertGreater(result["database_bytes"], 0)

    def test_1000_instruments_duplicate_stream_and_writer_faults(self):
        result = run_capacity(instruments=1000, minutes=2, pattern="duplicates", inject_failure=True, trace_memory=False)
        self.assertGreater(result["events"], 4000)
        self.assertEqual(result["bucket_count"], 2000)
        self.assertTrue(all(result["faults"].values()))
        self.assertEqual(result["database_rows"]["instruments"], 1000)

    def test_600_instrument_burst_stream_stays_bounded(self):
        result = run_capacity(instruments=600, minutes=2, pattern="burst", trace_memory=False)
        self.assertGreater(result["events"], 2400)
        self.assertEqual(result["bucket_count"], 1200)


if __name__ == "__main__":
    unittest.main()
