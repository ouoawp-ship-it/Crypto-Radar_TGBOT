from __future__ import annotations

import struct
import unittest
import zlib

from radars.pulse.chart import (
    CHART_COLORS,
    DISPLAY_CANDLE_LIMIT,
    PNG_SIGNATURE,
    render_pulse_chart_png,
)
from radars.pulse.chart_font_zh import missing_glyphs
from radars.pulse.simple_alert import _render_pulse_chart


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


class PulseChartTests(unittest.TestCase):
    def test_embedded_font_covers_pulse_chart_labels(self) -> None:
        self.assertEqual(
            set(),
            missing_glyphs(
                "核心主流主流加密山寨币未分类"
                "第轮事件小时已收线分钟触发参考开高低收成交量币安最近根"
            ),
        )

    def test_renders_original_mobile_chart_layout_deterministically(self) -> None:
        kwargs = {
            "symbol": "TESTUSDT",
            "candles": sample_candles(140),
            "checkpoints": [{
                "checkpoint_no": 1,
                "window_end_ts": 1_700_000_000 + 138 * 3600,
                "stage": "",
            }],
            "cycle_no": 1,
            "asset_category": "山寨币",
            "width": 1080,
            "height": 720,
        }
        first = render_pulse_chart_png(**kwargs)
        second = render_pulse_chart_png(**kwargs)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith(PNG_SIGNATURE))
        self.assertLess(len(first), 1_000_000)
        width, height, raw = decode_rgb_rows(first)
        self.assertEqual((width, height), (1080, 720))
        self.assertIn(bytes(CHART_COLORS["rising"]), raw)
        self.assertIn(bytes(CHART_COLORS["falling"]), raw)

    def test_signal_chart_fetches_hourly_history_once_and_drops_open_bar(self) -> None:
        base_ms = 1_700_000_000_000
        window_end_ms = base_ms + 6 * 3_600_000 + 30 * 60_000

        class Source:
            calls: list[dict[str, object]] = []

            @classmethod
            def klines(cls, symbol: str, **kwargs):
                cls.calls.append({"symbol": symbol, **kwargs})
                rows = []
                for index in range(7):
                    open_time = base_ms + index * 3_600_000
                    rows.append([
                        open_time,
                        "100",
                        "102",
                        "99",
                        str(100 + index),
                        "10",
                        open_time + 3_600_000 - 1,
                        str(100_000 + index),
                    ])
                return rows

        image = _render_pulse_chart(
            Source(),  # type: ignore[arg-type]
            {"symbol": "TESTUSDT", "tier": "alt"},
            {},
            1,
            window_end_ms,
        )

        self.assertIsNotNone(image)
        self.assertTrue(image.startswith(PNG_SIGNATURE))  # type: ignore[union-attr]
        self.assertEqual(len(Source.calls), 1)
        self.assertEqual(Source.calls[0]["symbol"], "TESTUSDT")
        self.assertEqual(Source.calls[0]["interval"], "1h")
        self.assertEqual(
            Source.calls[0]["limit"],
            DISPLAY_CANDLE_LIMIT + 1,
        )
        self.assertEqual(Source.calls[0]["end_time"], window_end_ms - 1)

    def test_chart_failure_degrades_to_text_only(self) -> None:
        class Source:
            @staticmethod
            def klines(*_args, **_kwargs):
                raise TimeoutError("market timeout")

        self.assertIsNone(
            _render_pulse_chart(
                Source(),  # type: ignore[arg-type]
                {"symbol": "TESTUSDT", "tier": "alt"},
                {},
                1,
                1_700_000_000_000,
            )
        )


if __name__ == "__main__":
    unittest.main()
