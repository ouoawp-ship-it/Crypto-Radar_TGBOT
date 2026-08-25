from __future__ import annotations

import struct
import unittest
import zlib
from unittest.mock import patch

from radars.pulse.chart import (
    CHART_COLORS,
    Canvas,
    DISPLAY_CANDLE_LIMIT,
    PNG_SIGNATURE,
    render_pulse_chart_png,
)
from radars.pulse.chart_font_zh import missing_glyphs
from radars.pulse.simple_alert import (
    _pulse_chart_checkpoints,
    _render_pulse_chart,
)


def sample_candles(count: int = 96) -> list[dict[str, float | int]]:
    items: list[dict[str, float | int]] = []
    running_cvd = 0.0
    for index in range(count):
        baseline = 100 + index * 0.035
        impulse = 1.1 if index % 9 in {1, 2, 3} else -0.45
        open_price = baseline + (0.5 if index % 4 == 0 else 0.0)
        close_price = baseline + impulse
        oi_open = 8_000_000 + index * 35_000
        oi_close = oi_open + (45_000 if index % 5 < 3 else -30_000)
        cvd_delta = (60_000 + index * 1_000) * (1 if index % 5 < 3 else -1)
        cvd_open = running_cvd
        cvd_close = cvd_open + cvd_delta
        cvd_high = max(cvd_open, cvd_close) + abs(cvd_delta) * 0.2
        cvd_low = min(cvd_open, cvd_close) - abs(cvd_delta) * 0.15
        items.append({
            "close_ts": 1_700_000_000 + index * 3600,
            "open": open_price,
            "high": max(open_price, close_price) + 0.6,
            "low": min(open_price, close_price) - 0.6,
            "close": close_price,
            "quote_volume": 100_000 + index * 5_000,
            "oi_open": oi_open,
            "oi_high": max(oi_open, oi_close) + 18_000,
            "oi_low": min(oi_open, oi_close) - 16_000,
            "oi_close": oi_close,
            "oi_value": oi_close,
            "cvd_delta": cvd_delta,
            "cvd_open": cvd_open,
            "cvd_high": cvd_high,
            "cvd_low": cvd_low,
            "cvd_close": cvd_close,
        })
        running_cvd = cvd_close
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

    def test_volume_suffixes_use_ascii_fallback_instead_of_box_glyph(self) -> None:
        self.assertEqual(5, Canvas.ui_text_width("M"))
        self.assertEqual(5, Canvas.ui_text_width("B"))

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
            "signal_change_pct": 21.32,
            "signal_oi_change_pct": 9.04,
            "width": 1440,
            "height": 720,
        }
        first = render_pulse_chart_png(**kwargs)
        second = render_pulse_chart_png(**kwargs)

        self.assertEqual(first, second)
        self.assertTrue(first.startswith(PNG_SIGNATURE))
        self.assertLess(len(first), 1_000_000)
        width, height, raw = decode_rgb_rows(first)
        self.assertEqual((width, height), (1440, 720))
        self.assertIn(bytes(CHART_COLORS["rising"]), raw)
        self.assertIn(bytes(CHART_COLORS["falling"]), raw)
        self.assertIn(bytes(CHART_COLORS["oi"]), raw)

    def test_signal_chart_adds_closed_15m_tail_and_drops_open_bars(self) -> None:
        base_ms = 1_699_999_200_000
        window_end_ms = base_ms + 6 * 3_600_000 + 45 * 60_000

        class Source:
            calls: list[dict[str, object]] = []
            oi_calls: list[dict[str, object]] = []

            @classmethod
            def klines(cls, symbol: str, **kwargs):
                cls.calls.append({"symbol": symbol, **kwargs})
                rows = []
                interval_ms = 15 * 60_000
                for index in range(28):
                    open_time = base_ms + index * interval_ms
                    quote_volume = 5_620_000 + index
                    open_price = 100 + index * 0.1
                    close_price = open_price + 0.1
                    rows.append([
                        open_time,
                        str(open_price),
                        str(close_price + 0.2),
                        str(open_price - 0.2),
                        str(close_price),
                        "10",
                        open_time + interval_ms - 1,
                        str(quote_volume),
                        100,
                        "6",
                        str(quote_volume * 0.6),
                    ])
                return rows

            @classmethod
            def open_interest_hist(cls, symbol: str, **kwargs):
                cls.oi_calls.append({"symbol": symbol, **kwargs})
                return [
                    {
                        "timestamp": base_ms + index * 15 * 60_000,
                        "sumOpenInterestValue": str(8_000_000 + index * 25_000),
                    }
                    for index in range(28)
                ]

        with patch(
            "radars.pulse.simple_alert.render_pulse_chart_png",
            side_effect=render_pulse_chart_png,
        ) as render_mock:
            image = _render_pulse_chart(
                Source(),  # type: ignore[arg-type]
                {
                    "symbol": "TESTUSDT",
                    "tier": "alt",
                    "price_map": {3: 21.32},
                    "oi_map": {3: 9.04},
                    "current_oi_usd": 8_700_000,
                },
                {},
                1,
                window_end_ms,
            )

        self.assertIsNotNone(image)
        self.assertTrue(image.startswith(PNG_SIGNATURE))  # type: ignore[union-attr]
        self.assertEqual(len(Source.calls), 1)
        self.assertEqual(Source.calls[0]["symbol"], "TESTUSDT")
        self.assertEqual(Source.calls[0]["interval"], "15m")
        self.assertEqual(
            Source.calls[0]["limit"],
            DISPLAY_CANDLE_LIMIT * 4 + 4,
        )
        self.assertEqual(Source.calls[0]["end_time"], window_end_ms - 1)
        self.assertEqual(len(Source.oi_calls), 1)
        self.assertEqual(Source.oi_calls[0]["symbol"], "TESTUSDT")
        self.assertEqual(Source.oi_calls[0]["period"], "15m")
        self.assertEqual(
            Source.oi_calls[0]["limit"],
            DISPLAY_CANDLE_LIMIT * 4 + 4,
        )
        self.assertEqual(
            Source.oi_calls[0]["start_time"],
            base_ms - 15 * 60_000,
        )
        self.assertEqual(Source.oi_calls[0]["end_time"], window_end_ms)
        rendered_candles = render_mock.call_args.kwargs["candles"]
        first_oi = rendered_candles[0]
        self.assertEqual(first_oi["oi_open"], 8_000_000)
        self.assertEqual(first_oi["oi_high"], 8_100_000)
        self.assertEqual(first_oi["oi_low"], 8_000_000)
        self.assertEqual(first_oi["oi_close"], 8_100_000)
        latest_oi = rendered_candles[-1]
        self.assertEqual(latest_oi["oi_open"], 8_650_000)
        self.assertEqual(latest_oi["oi_high"], 8_700_000)
        self.assertEqual(latest_oi["oi_low"], 8_650_000)
        self.assertEqual(latest_oi["oi_close"], 8_700_000)
        first_cvd = rendered_candles[0]
        self.assertEqual(first_cvd["cvd_open"], 0.0)
        self.assertEqual(first_cvd["cvd_low"], 0.0)
        self.assertEqual(first_cvd["cvd_high"], first_cvd["cvd_close"])
        latest_cvd = rendered_candles[-1]
        self.assertAlmostEqual(
            latest_cvd["cvd_close"] - latest_cvd["cvd_open"],
            latest_cvd["cvd_delta"],
        )

    def test_current_event_keeps_the_real_signal_window_time(self) -> None:
        signal_close_ts = 1_777_300_200

        checkpoints = _pulse_chart_checkpoints(
            {},
            "TESTUSDT",
            1,
            signal_close_ts,
        )

        self.assertEqual(checkpoints[0]["checkpoint_no"], 1)
        self.assertEqual(checkpoints[0]["window_end_ts"], signal_close_ts)

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
