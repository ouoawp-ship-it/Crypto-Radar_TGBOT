from __future__ import annotations

import struct
import unittest
import zlib

from radars.launch_warning.chart import (
    CHART_CATEGORY_LABELS,
    CHART_CONFIRMATION_LABELS,
    CHART_STATUS_LABELS,
    Canvas,
    PNG_SIGNATURE,
    _chart_category_label,
    render_launch_chart_png,
)
from radars.launch_warning.chart_font_zh import missing_glyphs


def sample_candles(count: int = 24) -> list[dict[str, float | int]]:
    items: list[dict[str, float | int]] = []
    for index in range(count):
        open_price = 100 + index * 0.4
        close_price = open_price + (0.8 if index % 3 else -0.3)
        items.append({
            "close_ts": 1_700_000_000 + index * 3600,
            "open": open_price,
            "high": max(open_price, close_price) + 0.5,
            "low": min(open_price, close_price) - 0.5,
            "close": close_price,
            "quote_volume": 100_000 + index * 5_000,
        })
    return items


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


class LaunchChartTests(unittest.TestCase):
    def test_compact_font_covers_every_chinese_chart_label(self) -> None:
        labels = [
            *CHART_CATEGORY_LABELS.values(),
            *CHART_STATUS_LABELS.values(),
            *CHART_CONFIRMATION_LABELS.values(),
            "未知",
            "1小时结构 · 15分钟触发",
            "第 123 轮",
            "事件 123",
            "形态",
            "整理区间",
            "关键位",
            "扫高",
            "扫低",
            "失效",
            "币安 · 1小时已收线 · 15分钟触发",
        ]

        self.assertEqual(set(), missing_glyphs("".join(labels)))

    def test_category_short_names_become_chinese_chart_labels(self) -> None:
        for short_name, expected in CHART_CATEGORY_LABELS.items():
            with self.subTest(short_name=short_name):
                self.assertEqual(expected, _chart_category_label(short_name))
        self.assertEqual("未分类", _chart_category_label("UNKNOWN CATEGORY"))
        self.assertEqual("山寨币", _chart_category_label("山寨币"))

    def test_chinese_ui_text_renders_without_runtime_font_dependency(self) -> None:
        canvas = Canvas(180, 40, (9, 12, 16))
        label = "事件 123 · 1小时确认"

        canvas.ui_text(4, 4, label, (105, 167, 255))

        self.assertGreater(canvas.ui_text_width(label), 80)
        self.assertNotEqual(
            bytes(canvas.pixels),
            bytes((9, 12, 16)) * (180 * 40),
        )

    def test_renders_deterministic_png_with_event_markers(self) -> None:
        candles = sample_candles()
        checkpoints = [
            {
                "checkpoint_no": 1,
                "window_end_ts": candles[4]["close_ts"],
                "stage": "primed",
            },
            {
                "checkpoint_no": 2,
                "window_end_ts": candles[14]["close_ts"],
                "stage": "breakout",
            },
        ]
        first = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=checkpoints,
            cycle_no=2,
        )
        second = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=checkpoints,
            cycle_no=2,
        )

        self.assertTrue(first.startswith(PNG_SIGNATURE))
        self.assertEqual(first, second)
        self.assertLess(len(first), 1_000_000)
        width, height, raw = decode_rgb_rows(first)
        self.assertEqual((width, height), (960, 540))
        self.assertEqual(len(raw), height * (1 + width * 3))
        self.assertIn(bytes((73, 143, 255)), raw)
        self.assertIn(bytes((246, 189, 22)), raw)

    def test_event_before_visible_window_is_clipped_but_rendered(self) -> None:
        candles = sample_candles()
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[{
                "checkpoint_no": 1,
                "window_end_ts": int(candles[0]["close_ts"]) - 3600,
                "stage": "primed",
            }],
            cycle_no=1,
        )

        self.assertTrue(result.startswith(PNG_SIGNATURE))
        self.assertIn(bytes((73, 143, 255)), decode_rgb_rows(result)[2])

    def test_event_marker_uses_compact_badge_and_muted_guide(self) -> None:
        candles = sample_candles()
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[{
                "checkpoint_no": 1,
                "window_end_ts": candles[4]["close_ts"],
                "stage": "primed",
            }],
            cycle_no=1,
        )

        width, _, raw = decode_rgb_rows(result)
        event_x = 54 + round((4 + 0.5) * ((960 - 92 - 54) / 24))
        badge_x = event_x - 15 // 2
        badge_y = 76 + 6

        self.assertEqual(
            rgb_pixel(raw, width, event_x, 76),
            bytes((36, 67, 116)),
        )
        self.assertEqual(
            rgb_pixel(raw, width, badge_x, badge_y),
            bytes((73, 143, 255)),
        )
        self.assertEqual(
            rgb_pixel(raw, width, badge_x + 1, badge_y + 1),
            bytes((15, 20, 27)),
        )

    def test_renders_price_action_box_level_and_confirmations(self) -> None:
        candles = sample_candles()
        price_action = {
            "enabled": True,
            "status": "confirmed_4h",
            "direction": "up",
            "lookback": 16,
            "box_high": 106.5,
            "box_low": 99.2,
            "level": 106.5,
            "box_start_ts": candles[0]["close_ts"],
            "box_end_ts": candles[15]["close_ts"],
            "trigger_window_end_ts": candles[16]["close_ts"],
            "event_window_end_ts": candles[23]["close_ts"],
            "confirmation_ends": {
                "15m": candles[16]["close_ts"],
                "1h": candles[20]["close_ts"],
                "4h": candles[23]["close_ts"],
            },
            "timeframes": {
                "15m": {
                    "box_high": 106.5,
                    "box_low": 99.2,
                },
            },
        }

        without_overlay = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=1,
        )
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=1,
            price_action=price_action,
        )
        raw = decode_rgb_rows(result)[2]

        self.assertNotEqual(result, without_overlay)
        self.assertIn(bytes((60, 101, 139)), raw)
        self.assertIn(bytes((86, 205, 220)), raw)
        self.assertIn(bytes((42, 204, 150)), raw)
        self.assertIn(bytes((79, 145, 255)), raw)
        self.assertIn(bytes((207, 106, 255)), raw)

    def test_renders_false_breakout_liquidity_sweep_marker(self) -> None:
        candles = sample_candles()
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=1,
            price_action={
                "enabled": True,
                "status": "false_breakout_1h",
                "direction": "up",
                "lookback": 16,
                "box_high": 106.5,
                "box_low": 99.2,
                "level": 106.5,
                "box_start_ts": candles[0]["close_ts"],
                "box_end_ts": candles[15]["close_ts"],
                "trigger_window_end_ts": candles[16]["close_ts"],
                "event_window_end_ts": candles[20]["close_ts"],
                "confirmation_ends": {
                    "15m": candles[16]["close_ts"],
                },
                "timeframes": {},
            },
        )

        self.assertIn(
            bytes((255, 159, 67)),
            decode_rgb_rows(result)[2],
        )

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
