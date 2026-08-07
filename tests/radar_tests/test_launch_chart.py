from __future__ import annotations

import struct
import unittest
import zlib
from unittest.mock import patch

from radars.launch_warning.chart import (
    CHART_CATEGORY_LABELS,
    CHART_COLORS,
    CHART_CONFIRMATION_LABELS,
    Canvas,
    DISPLAY_CANDLE_LIMIT,
    MAX_DISPLAY_BLOCKS,
    MAX_EVENT_BADGES,
    MAX_STRUCTURE_EVENTS,
    PNG_SIGNATURE,
    _footer_text,
    _select_display_blocks,
    _select_structure_events,
    _smc_alignment_label,
    _smc_filter_header,
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
            "供给区需求区",
            "结构暂停K线不连续",
            "72小时高位中位低位",
            "成交量最近120根1小时K线SMC仅作过滤72H位置参考同向支持冲突中性观察暂停通过数据不足高周期结构偏多偏空缺",
            *CHART_CONFIRMATION_LABELS.values(),
            "扫高扫低失效",
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

    def test_footer_reports_actual_visible_closed_candle_count(self) -> None:
        self.assertIn("最近5根1小时K线", _footer_text(5))
        self.assertIn("最近96根1小时K线", _footer_text(96))
        self.assertIn("最近120根1小时K线", _footer_text(120))

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
        self.assertEqual(
            rgb_pixel(raw, width, 959, 539),
            bytes(CHART_COLORS["background"]),
        )
        self.assertIn(bytes(CHART_COLORS["rising"]), raw)
        self.assertIn(bytes(CHART_COLORS["falling"]), raw)
        self.assertIn(bytes(CHART_COLORS["valuation_high"]), raw)
        self.assertIn(bytes(CHART_COLORS["valuation_mid"]), raw)
        self.assertIn(bytes(CHART_COLORS["valuation_low"]), raw)
        self.assertIn(bytes((28, 112, 82)), raw)
        self.assertIn(bytes((116, 49, 53)), raw)

    def test_production_size_uses_mobile_readable_reference_ratio(self) -> None:
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=sample_candles(180),
            checkpoints=[],
            cycle_no=1,
            width=1080,
            height=720,
        )

        self.assertEqual(decode_rgb_rows(result)[:2], (1080, 720))

    def test_supported_widths_keep_header_and_live_price_visible(self) -> None:
        for width, height in ((480, 320), (640, 420), (1600, 850)):
            with self.subTest(width=width):
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
            bytes(CHART_COLORS["valuation_high"]),
        )

        self.assertTrue(high_band_x)
        plot_left = 52
        plot_right = 1600 - 70
        context_width = max(104, round((plot_right - plot_left) * 0.12))
        candle_right = plot_right - context_width - 10
        context_left = candle_right + 10
        self.assertGreaterEqual(min(high_band_x), context_left)
        self.assertLessEqual(max(high_band_x), plot_right)

    def test_full_history_feeds_smc_but_only_recent_bars_are_visible(self) -> None:
        rows = sample_candles(328)
        changed_history = [dict(row) for row in rows]
        changed_history[0].update(open=1.0, high=2.0, low=0.5, close=1.5)
        calls: list[int] = []

        def empty_overlay(candles, **_kwargs):
            calls.append(len(candles))
            return {
                "status": "ready",
                "structure_events": [],
                "active_order_blocks": [],
                "valuation": {"data_status": "insufficient_history", "zones": {}},
            }

        with patch(
            "radars.launch_warning.chart.build_smc_overlay",
            side_effect=empty_overlay,
        ):
            original = render_launch_chart_png(
                symbol="TESTUSDT",
                candles=rows,
                checkpoints=[],
                cycle_no=1,
            )
            changed = render_launch_chart_png(
                symbol="TESTUSDT",
                candles=changed_history,
                checkpoints=[],
                cycle_no=1,
            )

        self.assertEqual(calls, [328, 328])
        self.assertEqual(original, changed)

    def test_nearby_block_selector_keeps_one_zone_per_side(self) -> None:
        last_ts = 10_000
        blocks = [
            {
                "direction": "bearish",
                "zone_low": 101.0,
                "zone_high": 102.0,
                "broken_at_ts": 9_000,
            },
            {
                "direction": "bearish",
                "zone_low": 105.0,
                "zone_high": 106.0,
                "broken_at_ts": 9_100,
            },
            {
                "direction": "bearish",
                "zone_low": 140.0,
                "zone_high": 141.0,
                "broken_at_ts": 9_900,
            },
            {
                "direction": "bullish",
                "zone_low": 98.0,
                "zone_high": 99.0,
                "broken_at_ts": 9_200,
            },
            {
                "direction": "bullish",
                "zone_low": 94.0,
                "zone_high": 95.0,
                "broken_at_ts": 9_300,
            },
            {
                "direction": "bullish",
                "zone_low": 90.0,
                "zone_high": 91.0,
                "broken_at_ts": 9_400,
            },
        ]

        selected = _select_display_blocks(
            blocks,
            current_price=100.0,
            last_close_ts=last_ts,
        )

        self.assertLessEqual(len(selected), MAX_DISPLAY_BLOCKS)
        self.assertEqual(sum(item["direction"] == "bearish" for item in selected), 1)
        self.assertEqual(sum(item["direction"] == "bullish" for item in selected), 1)
        self.assertNotIn(140.0, {item["zone_low"] for item in selected})

    def test_structure_selector_enforces_visual_limit(self) -> None:
        events = [
            {
                "kind": "swing",
                "direction": "bullish" if index % 2 else "bearish",
                "event": "structure_turn",
                "level": 100.0 + index,
                "confirmed_at_ts": 1_000 + index * 10,
                "broken_at_ts": 1_005 + index * 10,
            }
            for index in range(8)
        ]
        structures = _select_structure_events(
            events,
            first_close_ts=900,
            last_close_ts=2_000,
        )
        self.assertEqual(len(structures), MAX_STRUCTURE_EVENTS)
        self.assertEqual(MAX_EVENT_BADGES, 3)

    def test_smc_alignment_is_an_explainable_display_only_filter(self) -> None:
        overlay = {
            "status": "ready",
            "structure_events": [{
                "kind": "swing",
                "direction": "bearish",
                "level": 100.0,
                "confirmed_at_ts": 1_800,
                "broken_at_ts": 1_900,
            }],
        }

        self.assertEqual(
            _smc_alignment_label(
                overlay,
                {"direction": "down"},
                last_close_ts=2_000,
            ),
            "SMC参考：同向",
        )
        self.assertEqual(
            _smc_alignment_label(
                overlay,
                {"direction": "up"},
                last_close_ts=2_000,
            ),
            "SMC参考：冲突",
        )
        self.assertEqual(
            _smc_alignment_label(
                {
                    "status": "ready",
                    "structure_events": [{
                        **overlay["structure_events"][0],
                        "confirmed_at_ts": 1_950,
                        "broken_at_ts": 1_900,
                    }],
                },
                {"direction": "down"},
                last_close_ts=2_000,
            ),
            "SMC参考：中性",
        )

    def test_business_smc_filter_header_has_explicit_multiframe_status(self) -> None:
        text, color = _smc_filter_header({
            "status": "supportive",
            "one_hour_structure": "bullish",
            "four_hour_structure": "neutral",
        }, fallback="unused")
        self.assertEqual(
            text,
            "结构过滤：同向支持 · 1小时偏多/4小时中性",
        )
        self.assertEqual(color, (42, 204, 150))

        text, color = _smc_filter_header({
            "status": "insufficient",
            "one_hour_structure": "unavailable",
            "four_hour_structure": "unavailable",
        }, fallback="unused")
        self.assertEqual(
            text,
            "结构过滤：数据不足 · 1小时缺数据/4小时缺数据",
        )
        self.assertEqual(color, CHART_COLORS["valuation_high_text"])

    def test_smc_filter_changes_only_chart_header(self) -> None:
        kwargs = {
            "symbol": "TESTUSDT",
            "candles": sample_candles(120),
            "checkpoints": [],
            "cycle_no": 1,
            "width": 1080,
            "height": 720,
        }
        supportive = render_launch_chart_png(**kwargs, smc_filter={
            "status": "supportive",
            "one_hour_structure": "bullish",
            "four_hour_structure": "bullish",
        })
        conflicting = render_launch_chart_png(**kwargs, smc_filter={
            "status": "conflicting",
            "one_hour_structure": "bearish",
            "four_hour_structure": "bearish",
        })
        width, height, supportive_raw = decode_rgb_rows(supportive)
        _, _, conflicting_raw = decode_rgb_rows(conflicting)
        row_bytes = width * 3
        body_start = 96 * row_bytes
        self.assertNotEqual(
            supportive_raw[:body_start],
            conflicting_raw[:body_start],
        )
        self.assertEqual(
            supportive_raw[body_start:],
            conflicting_raw[body_start:],
        )

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
        self.assertNotIn(bytes(CHART_COLORS["valuation_high"]), raw)

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

        self.assertIn(
            bytes(CHART_COLORS["valuation_high"]),
            decode_rgb_rows(result)[2],
        )

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

        self.assertIn(
            bytes(CHART_COLORS["valuation_high"]),
            decode_rgb_rows(result)[2],
        )

    def test_order_block_remains_visible_above_overlapping_valuation_band(self) -> None:
        rows = sample_candles(328)
        overlay = {
            "structure_events": [],
            "active_order_blocks": [{
                "direction": "bullish",
                "zone_low": 100.2,
                "zone_high": 100.8,
                "origin_ts": rows[100]["close_ts"],
                "confirmed_at_ts": rows[110]["close_ts"],
                "broken_at_ts": rows[120]["close_ts"],
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
        self.assertIn(bytes(CHART_COLORS["demand_border"]), raw)
        self.assertIn(bytes(CHART_COLORS["valuation_high"]), raw)
        self.assertIn(bytes(CHART_COLORS["valuation_high_text"]), raw)

    def test_renders_confirmed_structure_and_active_order_block_layers(self) -> None:
        rows = structure_candles()
        overlay = {
            "status": "ready",
            "structure_events": [
                {
                    "kind": "swing",
                    "direction": "bearish",
                    "event": "structure_turn",
                    "level": 109.5,
                    "origin_ts": rows[10]["close_ts"],
                    "confirmed_at_ts": rows[25]["close_ts"],
                    "broken_at_ts": rows[30]["close_ts"],
                },
                {
                    "kind": "swing",
                    "direction": "bullish",
                    "event": "structure_turn",
                    "level": 111.5,
                    "origin_ts": rows[45]["close_ts"],
                    "confirmed_at_ts": rows[60]["close_ts"],
                    "broken_at_ts": rows[65]["close_ts"],
                },
            ],
            "active_order_blocks": [{
                "kind": "swing",
                "direction": "bullish",
                "side": "demand",
                "zone_low": 107.5,
                "zone_high": 108.5,
                "origin_ts": rows[50]["close_ts"],
                "confirmed_at_ts": rows[60]["close_ts"],
                "broken_at_ts": rows[65]["close_ts"],
                "state": "active",
            }],
            "valuation": {"data_status": "insufficient_history", "zones": {}},
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
            )
        raw = decode_rgb_rows(result)[2]

        self.assertIn(bytes(CHART_COLORS["structure_bear"]), raw)
        self.assertIn(bytes(CHART_COLORS["structure_bull"]), raw)
        self.assertIn(bytes(CHART_COLORS["demand_border"]), raw)

    def test_compact_lifecycle_context_is_visible_without_changing_smc(self) -> None:
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
                "window_end_ts": candles[-2]["close_ts"],
                "stage": "failed",
            }],
            cycle_no=99,
            price_action={
                "enabled": True,
                "status": "failed_breakout_4h",
                "box_high": 104.0,
                "box_low": 102.0,
                "level": 103.0,
                "box_start_ts": candles[-12]["close_ts"],
                "box_end_ts": candles[-4]["close_ts"],
                "trigger_window_end_ts": candles[-3]["close_ts"],
                "event_window_end_ts": candles[-2]["close_ts"],
                "confirmation_ends": {
                    "15m": candles[-8]["close_ts"],
                    "1h": candles[-6]["close_ts"],
                    "4h": candles[-4]["close_ts"],
                },
            },
        )

        self.assertNotEqual(plain, legacy_context)
        raw = decode_rgb_rows(legacy_context)[2]
        self.assertIn(bytes((255, 92, 92)), raw)
        self.assertIn(bytes((60, 135, 181)), raw)
        self.assertNotIn(bytes((207, 106, 255)), raw)
        self.assertNotIn(bytes((42, 204, 150)), raw)
        self.assertNotIn(bytes((79, 145, 255)), raw)

    def test_order_block_starts_when_break_is_known_not_at_source_candle(self) -> None:
        rows = sample_candles(288)
        overlay = {
            "status": "ready",
            "structure_events": [],
            "active_order_blocks": [{
                "direction": "bullish",
                "zone_low": 100.2,
                "zone_high": 100.8,
                "origin_ts": rows[20]["close_ts"],
                "confirmed_at_ts": rows[80]["close_ts"],
                "broken_at_ts": rows[200]["close_ts"],
            }],
            "valuation": {"data_status": "insufficient_history", "zones": {}},
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
            )

        width, height, raw = decode_rgb_rows(result)
        positions = color_x_positions(
            raw,
            width,
            height,
            bytes(CHART_COLORS["demand_border"]),
        )
        visible_rows = rows[-DISPLAY_CANDLE_LIMIT:]
        plot_left = 44
        plot_right = width - 62
        context_width = max(76, round((plot_right - plot_left) * 0.16))
        candle_right = plot_right - context_width - 10
        slot = (candle_right - plot_left) / len(visible_rows)
        relative_break = 200 - (len(rows) - len(visible_rows))
        expected_break_x = plot_left + round((relative_break + 0.5) * slot)
        self.assertTrue(positions)
        self.assertGreaterEqual(min(positions), expected_break_x - 1)

    def test_structure_line_starts_at_pivot_confirmation_not_origin(self) -> None:
        rows = sample_candles(288)
        overlay = {
            "status": "ready",
            "structure_events": [{
                "kind": "swing",
                "direction": "bullish",
                "event": "structure_turn",
                "level": 105.0,
                "origin_ts": rows[20]["close_ts"],
                "confirmed_at_ts": rows[100]["close_ts"],
                "broken_at_ts": rows[200]["close_ts"],
            }],
            "active_order_blocks": [],
            "valuation": {"data_status": "insufficient_history", "zones": {}},
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
            )

        width, height, raw = decode_rgb_rows(result)
        positions = color_x_positions(
            raw,
            width,
            height,
            bytes(CHART_COLORS["structure_bull"]),
        )
        visible_rows = rows[-DISPLAY_CANDLE_LIMIT:]
        plot_left = 44
        plot_right = width - 62
        context_width = max(76, round((plot_right - plot_left) * 0.16))
        candle_right = plot_right - context_width - 10
        slot = (candle_right - plot_left) / len(visible_rows)
        relative_confirmed = max(0, 100 - (len(rows) - len(visible_rows)))
        expected_confirmed_x = plot_left + round((relative_confirmed + 0.5) * slot)
        self.assertTrue(positions)
        self.assertGreaterEqual(min(positions), expected_confirmed_x - 1)

    def test_stale_trigger_levels_do_not_flatten_visible_price_action(self) -> None:
        rows = sample_candles(96)
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
                "box_start_ts": rows[0]["close_ts"] - 10 * 3600,
                "box_end_ts": rows[0]["close_ts"] - 2 * 3600,
                "trigger_window_end_ts": rows[0]["close_ts"] - 3600,
            },
        )

        self.assertEqual(plain, stale)

    def test_trigger_border_remains_visible_over_overlapping_smc_block(self) -> None:
        rows = sample_candles(96)
        overlay = {
            "status": "ready",
            "structure_events": [],
            "active_order_blocks": [{
                "direction": "bullish",
                "zone_low": 101.0,
                "zone_high": 103.0,
                "origin_ts": rows[45]["close_ts"],
                "confirmed_at_ts": rows[50]["close_ts"],
                "broken_at_ts": rows[55]["close_ts"],
            }],
            "valuation": {"data_status": "insufficient_history", "zones": {}},
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
                price_action={
                    "enabled": True,
                    "box_high": 102.5,
                    "box_low": 101.5,
                    "level": 102.0,
                    "box_start_ts": rows[60]["close_ts"],
                    "box_end_ts": rows[80]["close_ts"],
                    "trigger_window_end_ts": rows[81]["close_ts"],
                },
            )

        raw = decode_rgb_rows(result)[2]
        self.assertIn(bytes(CHART_COLORS["demand_border"]), raw)
        self.assertIn(bytes((60, 135, 181)), raw)
        self.assertIn(bytes((86, 205, 220)), raw)

    def test_malformed_or_future_derived_objects_are_not_drawn(self) -> None:
        rows = sample_candles(96)
        overlay = {
            "status": "ready",
            "structure_events": [{
                "kind": "swing",
                "direction": "bullish",
                "event": "structure_turn",
                "level": 102.0,
                "confirmed_at_ts": rows[80]["close_ts"],
                "broken_at_ts": rows[70]["close_ts"],
            }],
            "active_order_blocks": [{
                "direction": "bullish",
                "zone_low": 101.0,
                "zone_high": 102.0,
                "broken_at_ts": 0,
            }],
            "valuation": {"data_status": "insufficient_history", "zones": {}},
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
            )

        raw = decode_rgb_rows(result)[2]
        self.assertNotIn(bytes(CHART_COLORS["demand_border"]), raw)
        self.assertNotIn(bytes(CHART_COLORS["structure_bull"]), raw)

    def test_unselected_valid_blocks_do_not_expand_visible_price_scale(self) -> None:
        rows = sample_candles(96)
        visible_blocks = [
            {
                "direction": "bullish",
                "zone_low": 100.0,
                "zone_high": 100.5,
                "broken_at_ts": rows[40]["close_ts"],
            },
            {
                "direction": "bearish",
                "zone_low": 104.0,
                "zone_high": 104.5,
                "broken_at_ts": rows[41]["close_ts"],
            },
        ]
        base_overlay = {
            "status": "ready",
            "structure_events": [],
            "active_order_blocks": visible_blocks,
            "valuation": {"data_status": "insufficient_history", "zones": {}},
        }
        unselected_blocks = [
            {
                "direction": "bullish",
                "zone_low": 80.0,
                "zone_high": 81.0,
                "broken_at_ts": rows[42]["close_ts"],
            },
            {
                "direction": "bearish",
                "zone_low": 130.0,
                "zone_high": 131.0,
                "broken_at_ts": rows[43]["close_ts"],
            },
        ]
        with patch(
            "radars.launch_warning.chart.build_smc_overlay",
            return_value=base_overlay,
        ):
            without_hidden = render_launch_chart_png(
                symbol="TESTUSDT",
                candles=rows,
                checkpoints=[],
                cycle_no=1,
            )
        with patch(
            "radars.launch_warning.chart.build_smc_overlay",
            return_value={
                **base_overlay,
                "active_order_blocks": [*unselected_blocks, *visible_blocks],
            },
        ):
            with_hidden = render_launch_chart_png(
                symbol="TESTUSDT",
                candles=rows,
                checkpoints=[],
                cycle_no=1,
            )

        self.assertEqual(without_hidden, with_hidden)

    def test_pre_window_event_badge_does_not_move_guide_to_first_candle(self) -> None:
        rows = sample_candles(180)
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[{
                "checkpoint_no": 7,
                "window_end_ts": rows[10]["close_ts"],
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

    def test_future_lifecycle_checkpoint_is_not_moved_to_latest_candle(self) -> None:
        rows = sample_candles(96)
        plain = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[],
            cycle_no=1,
        )
        future = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[{
                "checkpoint_no": 99,
                "window_end_ts": rows[-1]["close_ts"] + 3600,
                "stage": "failed",
            }],
            cycle_no=1,
        )

        self.assertEqual(plain, future)

    def test_clustered_lifecycle_events_show_only_three_recent_badges(self) -> None:
        rows = sample_candles(96)
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=rows,
            checkpoints=[
                {
                    "checkpoint_no": index,
                    "window_end_ts": rows[-2]["close_ts"],
                    "stage": stage,
                }
                for index, stage in enumerate(
                    ("primed", "breakout", "launched", "failed"),
                    start=1,
                )
            ],
            cycle_no=4,
        )

        raw = decode_rgb_rows(result)[2]
        self.assertNotIn(bytes((73, 143, 255)), raw)
        for color in ((246, 189, 22), (207, 106, 255), (255, 92, 92)):
            self.assertIn(bytes(color), raw)

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
