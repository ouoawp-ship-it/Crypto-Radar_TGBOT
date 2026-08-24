from __future__ import annotations

import unittest

from radars.pulse.divergence import (
    DivergenceConfig,
    SIGNAL_DIRECTIONS,
    _row,
    classify,
    format_card,
)


class ClassifyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cfg = DivergenceConfig()

    def test_build(self) -> None:
        self.assertEqual(classify(27.92, 0.61, 27.3, self.cfg), "build")
        self.assertEqual(classify(14.90, -15.23, 30.1, self.cfg), "extreme")

    def test_panic(self) -> None:
        self.assertEqual(classify(-12.48, -4.82, -7.7, self.cfg), "panic")
        self.assertIsNone(classify(3.0, 2.0, 1.0, self.cfg))

    def test_resonance(self) -> None:
        self.assertEqual(classify(67.42, 33.78, 33.6, self.cfg), "resonance")
        self.assertEqual(classify(-13.13, 19.77, -32.9, self.cfg), "breakout")
        self.assertEqual(classify(-13.69, 0.68, -14.4, self.cfg), "extreme")
        self.assertEqual(classify(1.36, -10.24, 11.6, self.cfg), "extreme")

    def test_none(self) -> None:
        self.assertIsNone(classify(1.0, 0.5, 0.5, self.cfg))
        self.assertIsNone(classify(-2.0, -1.0, -1.0, self.cfg))
        self.assertEqual(classify(8.79, 1.36, 7.4, self.cfg), "pressure")

    def test_directional_categories_do_not_treat_every_signal_as_long(self) -> None:
        self.assertEqual(SIGNAL_DIRECTIONS["build"], "long")
        self.assertEqual(SIGNAL_DIRECTIONS["breakout"], "long")
        self.assertEqual(SIGNAL_DIRECTIONS["resonance"], "long")
        self.assertEqual(SIGNAL_DIRECTIONS["pressure"], "short")
        self.assertEqual(SIGNAL_DIRECTIONS["panic"], "short")
        self.assertNotIn("extreme", SIGNAL_DIRECTIONS)


class FormatTests(unittest.TestCase):
    def test_row(self) -> None:
        line = _row(1, {"coin": "ICX", "oi_pct": 27.92, "price_pct": 0.61, "divergence": 27.3})
        self.assertIn("ICX", line)
        self.assertIn("持仓+27.92%", line)
        self.assertIn("+0.61%", line)
        self.assertIn("背离 +27.3", line)

    def test_card_contains_sections_and_legend(self) -> None:
        analysis = {
            "build": [{"coin": "ICX", "oi_pct": 27.92, "price_pct": 0.61, "divergence": 27.3}],
            "pressure": [],
            "breakout": [],
            "panic": [],
            "resonance": [],
            "extreme": [],
        }
        from types import SimpleNamespace
        fake_window = SimpleNamespace(end=SimpleNamespace(strftime=lambda fmt: "08-11 18:00"))
        text = format_card(analysis, DivergenceConfig(), fake_window)
        self.assertIn("庄家建仓信号", text)
        self.assertIn("背离信号解读", text)
        self.assertIn("背离度 = 持仓变化% - 价格变化%", text)


if __name__ == "__main__":
    unittest.main()
