from __future__ import annotations

import copy
import math
import struct
import unittest
import zlib

from radars.consolidation_breakout.chart import (
    ANNOTATION_COLORS,
    PNG_SIGNATURE,
    RANGE_CANDLE_LIMIT,
    THREE_PUSH_CANDLE_LIMIT,
    render_consolidation_chart_png,
)
from radars.pulse.chart import CHART_COLORS


def sample_payload(
    count: int = 320,
    *,
    interval_ms: int = 4 * 60 * 60 * 1000,
) -> dict[str, object]:
    start_ms = 1_700_000_000_000
    candles: list[dict[str, float | int]] = []
    macd: list[float] = []
    for index in range(count):
        baseline = 100.0 + index * 0.015 + math.sin(index / 9.0) * 2.4
        open_price = baseline + (0.45 if index % 4 == 0 else -0.25)
        close_price = baseline + (0.55 if index % 5 < 3 else -0.50)
        open_time = start_ms + index * interval_ms
        candles.append({
            "open_time": open_time,
            "close_time": open_time + interval_ms - 1,
            "open": open_price,
            "high": max(open_price, close_price) + 0.8,
            "low": min(open_price, close_price) - 0.8,
            "close": close_price,
            "volume": 80_000.0 + (index % 17) * 9_000.0,
        })
        macd.append(math.sin(index / 12.0) * 1.2 + math.cos(index / 29.0) * 0.3)
    return {"candles": candles, "macd": macd}


def range_event(
    payload: dict[str, object],
    *,
    close_index: int = 280,
    timeframe: str = "4h",
) -> dict[str, object]:
    candles = payload["candles"]
    assert isinstance(candles, list)
    close_candle = candles[close_index]
    assert isinstance(close_candle, dict)
    return {
        "event": "breakout_up",
        "direction": "up",
        "symbol": "TESTUSDT",
        "timeframe": timeframe,
        "close_time": close_candle["close_time"],
        "close": close_candle["close"],
        "box_upper": 108.5,
        "box_lower": 96.0,
        "box_age": 240,
    }


def three_push_event(
    payload: dict[str, object],
    *,
    close_index: int = 280,
    timeframe: str = "4h",
) -> dict[str, object]:
    candles = payload["candles"]
    macd = payload["macd"]
    assert isinstance(candles, list)
    assert isinstance(macd, list)
    price_indices = [205, 235, 265]
    macd_indices = [207, 237, 267]
    for index, value in zip(macd_indices, (1.20, 0.90, 0.62)):
        macd[index] = value
    payload["box_start_close_time"] = candles[208]["close_time"]
    close_candle = candles[close_index]
    assert isinstance(close_candle, dict)
    return {
        "event": "three_push_top_confirmed",
        "direction": "down",
        "structure": "top",
        "symbol": "TESTUSDT",
        "timeframe": timeframe,
        "close_time": close_candle["close_time"],
        "close": close_candle["close"],
        "push_close_times": [
            candles[index]["close_time"] for index in price_indices
        ],
        "push_prices": [105.2, 106.6, 108.1],
        "push_macd_close_times": [
            candles[index]["close_time"] for index in macd_indices
        ],
        "push_macd": [macd[index] for index in macd_indices],
        "neckline": 99.4,
        "invalidation": 109.0,
        "box_upper": 108.0,
        "box_lower": 96.0,
        "box_age": 72,
    }


def decode_png(png: bytes) -> tuple[int, int, bytes]:
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


class ConsolidationBreakoutChartTests(unittest.TestCase):
    def test_range_chart_is_deterministic_and_telegram_safe(self) -> None:
        payload = sample_payload()
        event = range_event(payload)
        candles = payload["candles"]
        assert isinstance(candles, list)
        payload["box_start_close_time"] = candles[40]["close_time"]

        first = render_consolidation_chart_png(
            event=event,
            chart_payload=payload,
        )
        second = render_consolidation_chart_png(
            event=event,
            chart_payload=payload,
        )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith(PNG_SIGNATURE))
        self.assertLess(len(first), 10 * 1024 * 1024)
        width, height, raw = decode_png(first)
        self.assertEqual((width, height), (1200, 760))
        self.assertIn(bytes(CHART_COLORS["price_rising"]), raw)
        self.assertIn(bytes(CHART_COLORS["price_falling"]), raw)
        self.assertIn(bytes(ANNOTATION_COLORS["box"]), raw)
        self.assertIn(bytes(ANNOTATION_COLORS["macd"]), raw)

    def test_renderer_strictly_discards_candles_after_event_close(self) -> None:
        full_payload = sample_payload()
        event = range_event(full_payload)
        candles = full_payload["candles"]
        macd = full_payload["macd"]
        assert isinstance(candles, list)
        assert isinstance(macd, list)
        full_payload["box_start_close_time"] = candles[40]["close_time"]
        truncated_payload = {
            "candles": copy.deepcopy(candles[:281]),
            "macd": list(macd[:281]),
            "box_start_close_time": candles[40]["close_time"],
        }

        full = render_consolidation_chart_png(
            event=event,
            chart_payload=full_payload,
        )
        truncated = render_consolidation_chart_png(
            event=event,
            chart_payload=truncated_payload,
        )

        self.assertEqual(full, truncated)

    def test_three_push_marks_independent_price_and_macd_pivots(self) -> None:
        payload = sample_payload()
        event = three_push_event(payload)
        independent = render_consolidation_chart_png(
            event=event,
            chart_payload=payload,
        )
        same_bar_event = copy.deepcopy(event)
        same_bar_event["push_macd_close_times"] = list(
            same_bar_event["push_close_times"]
        )
        same_bar = render_consolidation_chart_png(
            event=same_bar_event,
            chart_payload=payload,
        )

        self.assertNotEqual(independent, same_bar)
        _width, _height, raw = decode_png(independent)
        self.assertIn(bytes(ANNOTATION_COLORS["price_push"]), raw)
        self.assertIn(bytes(ANNOTATION_COLORS["macd_push"]), raw)
        self.assertIn(bytes(ANNOTATION_COLORS["neckline"]), raw)
        self.assertIn(bytes(ANNOTATION_COLORS["invalidation"]), raw)
        self.assertIn(bytes(ANNOTATION_COLORS["volume_push"]), raw)

    def test_three_push_chart_also_discards_future_candles(self) -> None:
        payload = sample_payload()
        event = three_push_event(payload)
        candles = payload["candles"]
        macd = payload["macd"]
        assert isinstance(candles, list)
        assert isinstance(macd, list)
        truncated = {
            "candles": copy.deepcopy(candles[:281]),
            "macd": list(macd[:281]),
        }

        self.assertEqual(
            render_consolidation_chart_png(
                event=event,
                chart_payload=payload,
            ),
            render_consolidation_chart_png(
                event=event,
                chart_payload=truncated,
            ),
        )

    def test_three_push_without_frozen_box_start_does_not_invent_extent(self) -> None:
        payload = sample_payload()
        event = three_push_event(payload)
        payload.pop("box_start_close_time", None)
        unknown_extent = render_consolidation_chart_png(
            event=event,
            chart_payload=payload,
        )
        candles = payload["candles"]
        assert isinstance(candles, list)
        payload["box_start_close_time"] = candles[161]["close_time"]
        known_extent = render_consolidation_chart_png(
            event=event,
            chart_payload=payload,
        )

        self.assertNotEqual(unknown_extent, known_extent)

    def test_long_range_and_all_configured_timeframes_render(self) -> None:
        self.assertEqual(RANGE_CANDLE_LIMIT, 264)
        self.assertEqual(THREE_PUSH_CANDLE_LIMIT, 120)
        for timeframe, interval_ms in (
            ("4h", 4 * 60 * 60 * 1000),
            ("1d", 24 * 60 * 60 * 1000),
            ("1w", 7 * 24 * 60 * 60 * 1000),
        ):
            with self.subTest(timeframe=timeframe):
                payload = sample_payload(interval_ms=interval_ms)
                event = range_event(payload, timeframe=timeframe)
                candles = payload["candles"]
                assert isinstance(candles, list)
                payload["box_start_close_time"] = candles[40]["close_time"]
                image = render_consolidation_chart_png(
                    event=event,
                    chart_payload=payload,
                )
                self.assertTrue(image.startswith(PNG_SIGNATURE))
                self.assertLess(len(image), 10 * 1024 * 1024)

    def test_optional_breakout_start_marker_changes_snapshot(self) -> None:
        payload = sample_payload()
        event = range_event(payload)
        candles = payload["candles"]
        assert isinstance(candles, list)
        without_marker = render_consolidation_chart_png(
            event=event,
            chart_payload=payload,
        )
        payload["breakout_start_close_time"] = candles[277]["close_time"]
        with_marker = render_consolidation_chart_png(
            event=event,
            chart_payload=payload,
        )
        self.assertNotEqual(without_marker, with_marker)

    def test_rejects_invalid_dimensions_or_misaligned_macd(self) -> None:
        payload = sample_payload()
        event = range_event(payload)
        with self.assertRaisesRegex(ValueError, "dimensions"):
            render_consolidation_chart_png(
                event=event,
                chart_payload=payload,
                width=799,
            )

        broken_payload = copy.deepcopy(payload)
        macd = broken_payload["macd"]
        assert isinstance(macd, list)
        macd.pop()
        with self.assertRaisesRegex(ValueError, "align"):
            render_consolidation_chart_png(
                event=event,
                chart_payload=broken_payload,
            )

    def test_requires_the_exact_event_candle(self) -> None:
        payload = sample_payload(20)
        event = range_event(payload, close_index=10)
        candles = payload["candles"]
        macd = payload["macd"]
        assert isinstance(candles, list)
        assert isinstance(macd, list)
        del candles[10]
        del macd[10]

        with self.assertRaisesRegex(ValueError, "absent"):
            render_consolidation_chart_png(
                event=event,
                chart_payload=payload,
            )


if __name__ == "__main__":
    unittest.main()
