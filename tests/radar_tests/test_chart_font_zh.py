from __future__ import annotations

import unittest

from radars.launch_warning.chart_font_zh import (
    GLYPH_ADVANCES,
    GLYPH_HEIGHT,
    GLYPH_ORDER,
    GLYPH_WIDTH,
    glyph_alpha,
    missing_glyphs,
)


FORMAL_CHART_TEXT = (
    "开收量结构转空多顺势高估中间价低估已收线"
    "币安永续合约小时真实数据"
)
EXISTING_RUNTIME_COPY = "仅使用已收线K线未分类"
NUMERIC_CHART_TEXT = "开 0.05062 +1.23% 20:00"
REQUIRED_PUNCTUATION = ".:%+-"


class ChartFontZhTests(unittest.TestCase):
    def test_formal_chart_copy_has_complete_glyph_coverage(self) -> None:
        self.assertEqual(
            set(),
            missing_glyphs(
                FORMAL_CHART_TEXT
                + EXISTING_RUNTIME_COPY
                + NUMERIC_CHART_TEXT
                + REQUIRED_PUNCTUATION
            ),
        )

    def test_every_declared_glyph_has_raster_data_and_advance(self) -> None:
        self.assertEqual(len(GLYPH_ORDER), len(set(GLYPH_ORDER)))
        self.assertEqual(len(GLYPH_ORDER), len(GLYPH_ADVANCES))

        for character in GLYPH_ORDER:
            with self.subTest(character=character):
                glyph = glyph_alpha(character)
                self.assertIsNotNone(glyph)
                assert glyph is not None
                alpha, advance = glyph
                self.assertEqual(GLYPH_WIDTH * GLYPH_HEIGHT, len(alpha))
                self.assertTrue(any(alpha))
                self.assertGreater(advance, 0)


if __name__ == "__main__":
    unittest.main()
