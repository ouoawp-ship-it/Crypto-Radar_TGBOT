from __future__ import annotations

import inspect
import struct
import unittest
import zlib

from radars.launch_warning.chart import (
    CHART_CATEGORY_LABELS,
    CHART_COLORS,
    CHART_CONFIRMATION_LABELS,
    CHART_STATUS_LABELS,
    Canvas,
    DISPLAY_CANDLE_LIMIT,
    MAX_EVENT_BADGES,
    PNG_SIGNATURE,
    _chart_category_label,
    _footer_text,
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
    return width, height, zlib.decompress(bytes(compressed))


def rgb_pixel(raw: bytes, width: int, x: int, y: int) -> bytes:
    offset = y * (1 + width * 3) + 1 + x * 3
    return raw[offset:offset + 3]


class LaunchChartTests(unittest.TestCase):
    def test_compact_font_covers_every_runtime_chart_label(self) -> None:
        labels = [
            *CHART_CATEGORY_LABELS.values(),
            *CHART_CONFIRMATION_LABELS.values(),
            *CHART_STATUS_LABELS.values(),
            "未分类真实数据仅使用已收线K线",
            "开高低收成交量小时币安最近根",
            "第轮事件形态参考整理关键位",
        ]
        self.assertEqual(set(), missing_glyphs("".join(labels)))

    def test_category_short_names_become_chinese_chart_labels(self) -> None:
        for short_name, expected in CHART_CATEGORY_LABELS.items():
            with self.subTest(short_name=short_name):
                self.assertEqual(expected, _chart_category_label(short_name))
        self.assertEqual("未分类", _chart_category_label("UNKNOWN CATEGORY"))
        self.assertEqual("山寨币", _chart_category_label("山寨币"))

    def test_chinese_ui_text_renders_without_runtime_font_dependency(self) -> None:
        canvas = Canvas(260, 40, (255, 255, 255))
        label = "1小时已收线 · 15分钟触发参考"
        canvas.ui_text(4, 4, label, (5, 151, 126))
        self.assertGreater(canvas.ui_text_width(label), 100)
        self.assertNotEqual(
            bytes(canvas.pixels),
            bytes((255, 255, 255)) * (260 * 40),
        )

    def test_footer_reports_actual_visible_closed_candle_count(self) -> None:
        self.assertIn("最近5根1小时已收线K线", _footer_text(5))
        self.assertIn("最近96根1小时已收线K线", _footer_text(96))
        self.assertIn("最近120根1小时已收线K线", _footer_text(120))

    def test_renders_deterministic_mobile_png_with_candles_and_volume(self) -> None:
        kwargs = {
            "symbol": "TESTUSDT",
            "candles": sample_candles(),
            "checkpoints": [],
            "cycle_no": 2,
        }
        first = render_launch_chart_png(**kwargs)
        second = render_launch_chart_png(**kwargs)

        self.assertTrue(first.startswith(PNG_SIGNATURE))
        self.assertEqual(first, second)
        self.assertLess(len(first), 1_000_000)
        width, height, raw = decode_rgb_rows(first)
        self.assertEqual((width, height), (960, 540))
        self.assertEqual(len(raw), height * (1 + width * 3))
        self.assertEqual(
            rgb_pixel(raw, width, 959, 539),
            bytes(CHART_COLORS["background"]),
        )
        self.assertIn(bytes(CHART_COLORS["rising"]), raw)
        self.assertIn(bytes(CHART_COLORS["falling"]), raw)
        self.assertIn(bytes((28, 112, 82)), raw)
        self.assertIn(bytes((116, 49, 53)), raw)

    def test_supported_sizes_keep_header_and_live_price_visible(self) -> None:
        for width, height in ((480, 320), (640, 420), (1080, 720), (1600, 850)):
            with self.subTest(width=width, height=height):
                result = render_launch_chart_png(
                    symbol="LONGSYMBOLUSDT",
                    candles=sample_candles(180),
                    checkpoints=[],
                    cycle_no=12,
                    width=width,
                    height=height,
                )
                actual_width, actual_height, raw = decode_rgb_rows(result)
                self.assertEqual((actual_width, actual_height), (width, height))
                self.assertIn(bytes(CHART_COLORS["header"]), raw)
                self.assertIn(bytes(CHART_COLORS["rising"]), raw)

    def test_only_visible_closed_history_changes_pixels(self) -> None:
        rows = sample_candles(DISPLAY_CANDLE_LIMIT + 40)
        changed = [dict(row) for row in rows]
        for row in changed[:-DISPLAY_CANDLE_LIMIT]:
            row["high"] = float(row["high"]) * 5
            row["low"] = float(row["low"]) / 5

        original = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[],
            cycle_no=1,
        )
        old_history_changed = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=changed,
            checkpoints=[],
            cycle_no=1,
        )
        self.assertEqual(original, old_history_changed)

    def test_15_minute_box_and_key_level_remain_presentation_only(self) -> None:
        candles = sample_candles()
        trigger_end = int(candles[-2]["close_ts"])
        price_action = {
            "enabled": True,
            "status": "breakout_15m",
            "direction": "up",
            "trigger_window_end_ts": trigger_end,
            "box_start_ts": trigger_end - 16 * 15 * 60,
            "box_end_ts": trigger_end - 15 * 60,
            "box_high": 103.0,
            "box_low": 101.0,
            "level": 103.0,
            "lookback": 16,
        }
        plain = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=1,
        )
        annotated = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=1,
            price_action=price_action,
        )
        self.assertNotEqual(plain, annotated)
        self.assertIn(bytes(CHART_COLORS["accent"]), decode_rgb_rows(annotated)[2])

    def test_stale_trigger_levels_do_not_flatten_visible_price_action(self) -> None:
        rows = sample_candles()
        plain = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[],
            cycle_no=1,
        )
        stale = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[],
            cycle_no=1,
            price_action={
                "enabled": True,
                "box_high": 10_000.0,
                "box_low": 9_000.0,
                "level": 9_500.0,
                "box_start_ts": int(rows[0]["close_ts"]) - 10 * 3600,
                "box_end_ts": int(rows[0]["close_ts"]) - 2 * 3600,
                "trigger_window_end_ts": int(rows[0]["close_ts"]) - 3600,
            },
        )
        self.assertEqual(plain, stale)

    def test_lifecycle_and_trigger_context_remain_visible(self) -> None:
        candles = sample_candles()
        plain = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=1,
        )
        contextual = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[{
                "checkpoint_no": 99,
                "window_end_ts": int(candles[-2]["close_ts"]),
                "stage": "failed",
            }],
            cycle_no=99,
            price_action={
                "enabled": True,
                "status": "failed_breakout_4h",
                "box_high": 104.0,
                "box_low": 102.0,
                "level": 103.0,
                "box_start_ts": int(candles[-12]["close_ts"]),
                "box_end_ts": int(candles[-4]["close_ts"]),
                "trigger_window_end_ts": int(candles[-3]["close_ts"]),
            },
        )
        self.assertNotEqual(plain, contextual)
        raw = decode_rgb_rows(contextual)[2]
        self.assertIn(bytes((255, 92, 92)), raw)
        self.assertIn(bytes((60, 135, 181)), raw)

    def test_future_lifecycle_checkpoint_is_not_drawn(self) -> None:
        candles = sample_candles()
        last_close_ts = int(candles[-1]["close_ts"])
        plain = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=1,
        )
        future = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[{
                "checkpoint_no": 1,
                "window_end_ts": last_close_ts + 3600,
                "stage": "launched",
            }],
            cycle_no=1,
        )
        self.assertEqual(plain, future)

    def test_pre_window_event_badge_does_not_move_guide_to_first_candle(self) -> None:
        rows = sample_candles(180)
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[{
                "checkpoint_no": 7,
                "window_end_ts": int(rows[10]["close_ts"]),
                "stage": "primed",
            }],
            cycle_no=7,
        )
        width, height, raw = decode_rgb_rows(result)
        event_color = bytes((73, 143, 255))
        positions: list[tuple[int, int]] = []
        for y in range(height):
            row_start = y * (1 + width * 3) + 1
            for x in range(width):
                offset = row_start + x * 3
                if raw[offset:offset + 3] == event_color:
                    positions.append((x, y))
        self.assertTrue(positions)
        self.assertTrue(all(y < 117 for _x, y in positions))

    def test_clustered_lifecycle_events_show_only_recent_badges(self) -> None:
        candles = sample_candles()
        checkpoints = [
            {
                "checkpoint_no": index,
                "window_end_ts": int(candles[-2]["close_ts"]),
                "stage": stage,
            }
            for index, stage in enumerate(
                ("primed", "breakout", "launched", "failed"),
                start=1,
            )
        ]
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=checkpoints,
            cycle_no=1,
        )
        raw = decode_rgb_rows(result)[2]
        self.assertEqual(MAX_EVENT_BADGES, 3)
        self.assertNotIn(bytes((73, 143, 255)), raw)
        for color in ((246, 189, 22), (207, 106, 255), (255, 92, 92)):
            self.assertIn(bytes(color), raw)

    def test_invalid_inputs_fail_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least five"):
            render_launch_chart_png(
                symbol="TESTUSDT",
                candles=sample_candles(4),
                checkpoints=[],
                cycle_no=1,
            )
        with self.assertRaisesRegex(ValueError, "unsupported chart dimensions"):
            render_launch_chart_png(
                symbol="TESTUSDT",
                candles=sample_candles(),
                checkpoints=[],
                cycle_no=1,
                width=300,
            )

    def test_chart_source_has_no_network_calls(self) -> None:
        source = inspect.getsource(render_launch_chart_png).lower()
        self.assertNotIn("requests", source)
        self.assertNotIn("urllib", source)
        self.assertNotIn("http", source)


if __name__ == "__main__":
    unittest.main()
