from __future__ import annotations

import struct
import unittest
import zlib

from paopao_radar.launch_chart import (
    PNG_SIGNATURE,
    compact_smc_chart_frame,
    render_launch_chart_png,
)


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

    def test_renders_compact_one_hour_smc_overlay(self) -> None:
        candles = sample_candles()
        ts = [int(item["close_ts"]) for item in candles]
        result = render_launch_chart_png(
            symbol="TESTUSDT",
            candles=candles,
            checkpoints=[],
            cycle_no=1,
            price_action={
                "enabled": True,
                "status": "confirmed_1h",
                "smc": {
                    "enabled": True,
                    "status": "bos_confirmed",
                    "htf_bias": {"direction": "up"},
                    "snapshot": {
                        "timeframes": {
                            "1h": {
                                "dealing_range": {
                                    "high": 109.5,
                                    "low": 99.2,
                                    "equilibrium": 104.35,
                                    "start_ts": ts[1],
                                    "end_ts": ts[-1],
                                },
                                "swings": [
                                    {
                                        "kind": "high",
                                        "label": "HH",
                                        "level": 106.5,
                                        "ts": ts[8],
                                    },
                                    {
                                        "kind": "low",
                                        "label": "HL",
                                        "level": 102.0,
                                        "ts": ts[11],
                                    },
                                ],
                                "structures": [
                                    {
                                        "type": "BOS",
                                        "direction": "up",
                                        "level": 104.0,
                                        "source_ts": ts[4],
                                        "event_ts": ts[9],
                                    },
                                    {
                                        "type": "CHOCH",
                                        "direction": "down",
                                        "level": 103.0,
                                        "source_ts": ts[8],
                                        "event_ts": ts[13],
                                    },
                                    {
                                        "type": "MSS",
                                        "direction": "up",
                                        "level": 106.0,
                                        "source_ts": ts[13],
                                        "event_ts": ts[18],
                                    },
                                ],
                                "liquidity": [{
                                    "type": "BSL",
                                    "level": 107.0,
                                    "formed_ts": ts[10],
                                    "event_ts": ts[19],
                                    "status": "swept",
                                }],
                                "fvgs": [{
                                    "direction": "up",
                                    "bottom": 104.5,
                                    "top": 105.2,
                                    "formed_ts": ts[14],
                                    "mitigated_ts": ts[20],
                                    "status": "active",
                                }],
                                "order_blocks": [{
                                    "direction": "up",
                                    "bottom": 102.5,
                                    "top": 103.4,
                                    "formed_ts": ts[12],
                                    "invalidated_ts": 0,
                                    "status": "active",
                                }],
                                "breaker_blocks": [{
                                    "direction": "down",
                                    "bottom": 105.5,
                                    "top": 106.2,
                                    "formed_ts": ts[16],
                                    "status": "active",
                                }],
                                "mitigation_blocks": [{
                                    "direction": "up",
                                    "bottom": 102.5,
                                    "top": 103.4,
                                    "event_ts": ts[17],
                                }],
                            },
                        },
                    },
                },
            },
        )
        raw = decode_rgb_rows(result)[2]

        for color in (
            (44, 151, 119),
            (66, 124, 218),
            (246, 189, 22),
            (196, 202, 211),
            (214, 96, 255),
            (238, 196, 72),
            (255, 126, 67),
        ):
            self.assertIn(bytes(color), raw)

    def test_compact_smc_frame_keeps_only_latest_actionable_items(self) -> None:
        result = compact_smc_chart_frame({
            "swings": [{"ts": index} for index in range(8)],
            "structures": [{"event_ts": index} for index in range(5)],
            "liquidity": [
                {"type": "BSL", "status": "broken", "formed_ts": 1},
                {"type": "BSL", "status": "active", "formed_ts": 2},
                {"type": "SSL", "status": "swept", "formed_ts": 3},
            ],
            "fvgs": [
                {"status": "mitigated", "formed_ts": 1},
                {"status": "active", "formed_ts": 2},
                {"status": "active", "formed_ts": 3},
            ],
            "order_blocks": [
                {"status": "invalidated", "formed_ts": 1},
                {"status": "active", "formed_ts": 2},
            ],
            "breaker_blocks": [
                {"status": "mitigated", "formed_ts": 1},
                {"status": "active", "formed_ts": 2},
            ],
            "mitigation_blocks": [{"event_ts": 4}],
            "dealing_range": {"high": 10, "low": 1},
        })

        self.assertEqual(len(result["swings"]), 4)
        self.assertEqual(len(result["structures"]), 1)
        self.assertEqual(len(result["liquidity"]), 2)
        self.assertEqual(result["fvgs"], [{"status": "active", "formed_ts": 3}])
        self.assertEqual(len(result["order_blocks"]), 1)
        self.assertEqual(len(result["breaker_blocks"]), 1)
        self.assertEqual(result["mitigation_blocks"], [])
        self.assertEqual(result["dealing_range"], {})

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
