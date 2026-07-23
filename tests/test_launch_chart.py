from __future__ import annotations

import struct
import unittest
import zlib

from paopao_radar.launch_chart import PNG_SIGNATURE, render_launch_chart_png


def sample_candles(count: int = 24) -> list[dict[str, float | int]]:
    items: list[dict[str, float | int]] = []
    for index in range(count):
        open_price = 100 + index * 0.4
        close_price = open_price + (0.8 if index % 3 else -0.3)
        items.append({
            "close_ts": 1_700_000_000 + index * 900,
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


class LaunchChartTests(unittest.TestCase):
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
