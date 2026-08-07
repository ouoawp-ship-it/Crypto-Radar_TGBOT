from __future__ import annotations

import struct
import unittest
import zlib
from unittest.mock import patch

from radars.launch_warning.chart import (
    CHART_CATEGORY_LABELS,
    Canvas,
    PNG_SIGNATURE,
    _chart_category_label,
    render_launch_chart_png,
)
from radars.launch_warning.chart_font_zh import missing_glyphs


def sample_candles(count: int = 96) -> list[dict[str, float | int]]:
    items: list[dict[str, float | int]] = []
    for index in range(count):
        baseline = 100 + index * 0.035
        impulse = 1.1 if index % 9 in {1, 2, 3} else -0.45
        open_price = baseline + (0.5 if index % 4 == 0 else 0.0)
        close_price = baseline + impulse
        items.append({
            "close_ts": 1_700_000_000 + index * 3600,
            "open": open_price,
            "high": max(open_price, close_price) + 0.6,
            "low": min(open_price, close_price) - 0.6,
            "close": close_price,
            "quote_volume": 100_000 + index * 5_000,
        })
    return items


def structure_candles() -> list[dict[str, float | int]]:
    rows = [
        {
            "close_ts": 1_700_000_000 + index * 3600,
            "open": 110.0,
            "high": 111.0,
            "low": 109.0,
            "close": 110.0,
            "quote_volume": 100_000.0,
        }
        for index in range(100)
    ]
    rows[0]["low"] = 100.0
    rows[21].update(open=100.0, high=101.0, low=98.0, close=99.0)
    rows[22].update(open=109.0, high=112.0, low=108.5, close=111.0)
    rows[43].update(open=110.0, high=116.0, low=109.0, close=115.0)
    return rows


def decode_rgb_rows(png: bytes) -> tuple[int, int, bytes]:
    offset = len(PNG_SIGNATURE)
    width = 0
    height = 0
    compressed = bytearray()
    while offset < len(png):
        length = struct.unpack(">I", png[offset:offset + 4])[0]
        kind = png[offset + 4:offset + 8]
        payload = png[offset + 8:offset + 8 + length]
        offset += 12 + length
        if kind == b"IHDR":
            width, height = struct.unpack(">II", payload[:8])
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    raw = zlib.decompress(bytes(compressed))
    return width, height, raw


def rgb_pixel(raw: bytes, width: int, x: int, y: int) -> bytes:
    offset = y * (1 + width * 3) + 1 + x * 3
    return raw[offset:offset + 3]


def color_x_positions(raw: bytes, width: int, height: int, color: bytes) -> list[int]:
    positions: list[int] = []
    for y in range(height):
        row_start = y * (1 + width * 3) + 1
        for x in range(width):
            offset = row_start + x * 3
            if raw[offset:offset + 3] == color:
                positions.append(x)
    return positions


class LaunchChartTests(unittest.TestCase):
    def test_compact_font_covers_reference_chart_labels(self) -> None:
        labels = [
            *CHART_CATEGORY_LABELS.values(),
            "未知",
            "结构转多",
            "结构转空",
            "顺势突破",
            "高估",
            "中间价",
            "低估",
            "真实数据",
            "仅使用已收线K线",
            "开高低收",
            "小时币安",
        ]

        self.assertEqual(set(), missing_glyphs("".join(labels)))

    def test_category_short_names_become_chinese_chart_labels(self) -> None:
        for short_name, expected in CHART_CATEGORY_LABELS.items():
            with self.subTest(short_name=short_name):
                self.assertEqual(expected, _chart_category_label(short_name))
        self.assertEqual("未分类", _chart_category_label("UNKNOWN CATEGORY"))
        self.assertEqual("山寨币", _chart_category_label("山寨币"))

    def test_chinese_ui_text_renders_without_runtime_font_dependency(self) -> None:
        canvas = Canvas(220, 40, (255, 255, 255))
        label = "结构转多 · 高估 · 中间价 · 低估"

        canvas.ui_text(4, 4, label, (5, 151, 126))

        self.assertGreater(canvas.ui_text_width(label), 100)
        self.assertNotEqual(
            bytes(canvas.pixels),
            bytes((255, 255, 255)) * (220 * 40),
        )

    def test_renders_deterministic_reference_style_png(self) -> None:
        candles = sample_candles()
        first = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=2,
        )
        second = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=2,
        )

        self.assertTrue(first.startswith(PNG_SIGNATURE))
        self.assertEqual(first, second)
        self.assertLess(len(first), 1_000_000)
        width, height, raw = decode_rgb_rows(first)
        self.assertEqual((width, height), (960, 540))
        self.assertEqual(len(raw), height * (1 + width * 3))
        self.assertEqual(rgb_pixel(raw, width, 959, 539), bytes((255, 255, 255)))
        self.assertIn(bytes((24, 183, 164)), raw)
        self.assertIn(bytes((255, 64, 82)), raw)
        self.assertIn(bytes((255, 244, 184)), raw)
        self.assertIn(bytes((229, 232, 236)), raw)
        self.assertIn(bytes((207, 222, 253)), raw)

    def test_production_size_matches_mobile_reference_ratio(self) -> None:
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=sample_candles(180),
            checkpoints=[],
            cycle_no=1,
            width=1600,
            height=850,
        )

        self.assertEqual(decode_rgb_rows(result)[:2], (1600, 850))

    def test_current_valuation_context_is_not_backpainted_over_history(self) -> None:
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=sample_candles(328),
            checkpoints=[],
            cycle_no=1,
            width=1600,
            height=850,
        )
        width, height, raw = decode_rgb_rows(result)
        high_band_x = color_x_positions(
            raw,
            width,
            height,
            bytes((255, 244, 184)),
        )

        self.assertTrue(high_band_x)
        plot_right = 1600 - 72
        context_width = max(112, round((plot_right - 10) * 0.16))
        candle_right = plot_right - context_width
        self.assertGreater(min(high_band_x), candle_right)

    def test_crypto_data_gap_keeps_raw_chart_and_drops_derived_overlay(self) -> None:
        rows = sample_candles()
        rows.pop(40)

        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[],
            cycle_no=1,
            asset_category="CRYPTO ALT",
        )

        width, height, raw = decode_rgb_rows(result)
        self.assertEqual((width, height), (960, 540))
        self.assertNotIn(bytes((255, 244, 184)), raw)

    def test_session_based_asset_keeps_bounded_closed_market_gap_overlay(self) -> None:
        rows = sample_candles()
        del rows[40:48]

        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[],
            cycle_no=1,
            asset_category="TOKENIZED STOCK",
        )

        self.assertIn(bytes((255, 244, 184)), decode_rgb_rows(result)[2])

    def test_leveraged_etf_uses_session_gap_policy(self) -> None:
        rows = sample_candles()
        del rows[40:48]

        result = render_launch_chart_png(
            symbol="SOXLUSDT",
            candles=rows,
            checkpoints=[],
            cycle_no=1,
            asset_category="LEVERAGED ETF",
        )

        self.assertIn(bytes((255, 244, 184)), decode_rgb_rows(result)[2])

    def test_order_block_remains_visible_above_overlapping_valuation_band(self) -> None:
        rows = sample_candles(328)
        overlay = {
            "structure_events": [],
            "active_order_blocks": [{
                "direction": "bullish",
                "zone_low": 100.2,
                "zone_high": 100.8,
                "origin_ts": rows[100]["close_ts"],
            }],
            "valuation": {
                "data_status": "complete",
                "range_low": 100.0,
                "range_high": 110.0,
                "zones": {
                    "low": {"low": 100.0, "high": 100.8},
                    "mid": {"low": 104.75, "high": 105.25},
                    "high": {"low": 109.0, "high": 110.0},
                },
            },
        }
        with patch(
            "radars.launch_warning.chart.build_smc_overlay",
            return_value=overlay,
        ):
            result = render_launch_chart_png(
                symbol="TESTUSDT",
                candles=rows,
                checkpoints=[],
                cycle_no=1,
                width=1600,
                height=850,
            )

        raw = decode_rgb_rows(result)[2]
        self.assertIn(bytes((199, 235, 226)), raw)
        self.assertIn(bytes((255, 244, 184)), raw)
        self.assertIn(bytes((221, 177, 14)), raw)

    def test_renders_confirmed_structure_and_active_order_block_layers(self) -> None:
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=structure_candles(),
            checkpoints=[],
            cycle_no=1,
        )
        raw = decode_rgb_rows(result)[2]

        self.assertIn(bytes((236, 70, 109)), raw)
        self.assertIn(bytes((5, 151, 126)), raw)
        self.assertIn(bytes((199, 235, 226)), raw)

    def test_old_lifecycle_arguments_do_not_reintroduce_clutter(self) -> None:
        candles = sample_candles()
        plain = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=1,
        )
        legacy_context = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[{
                "checkpoint_no": 99,
                "window_end_ts": candles[-1]["close_ts"],
                "stage": "failed",
            }],
            cycle_no=99,
            price_action={
                "enabled": True,
                "status": "failed_breakout_4h",
                "box_high": 999.0,
                "box_low": 1.0,
                "level": 999.0,
            },
        )

        self.assertEqual(plain, legacy_context)

    def test_rejects_incomplete_candle_series(self) -> None:
        with self.assertRaisesRegex(ValueError, "five valid candles"):
            render_launch_chart_png(
                symbol="TESTUSDT",
                candles=sample_candles(4),
                checkpoints=[],
                cycle_no=1,
            )


if __name__ == "__main__":
    unittest.main()
