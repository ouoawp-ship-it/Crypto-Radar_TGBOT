from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.charts import generate_structure_chart
from paopao_radar.structure_radar import (
    SIGNAL_PRE_BREAKOUT_NEAR,
    StructureSignal,
    normalize_candles,
)


def kline(idx: int, close: float, high: float, low: float, quote_volume: float = 1000, taker_ratio: float = 0.56):
    open_time = 1_700_000_000_000 + idx * 900_000
    volume = quote_volume / close
    return [
        open_time,
        str(close),
        str(high),
        str(low),
        str(close),
        str(volume),
        open_time + 899_999,
        str(quote_volume),
        100,
        str(volume * taker_ratio),
        str(quote_volume * taker_ratio),
        "0",
    ]


def pre_breakout_klines():
    rows = []
    for idx in range(44):
        high = 105.0 if idx in {6, 16, 27, 36} else 100.8
        low = 98.8 if idx in {9, 24, 33} else 99.3
        rows.append(kline(idx, 100 + (idx % 4) * 0.12, high, low))
    rows.append(kline(45, 104.4, 104.8, 103.8, quote_volume=2600, taker_ratio=0.66))
    return rows


class ChartTests(unittest.TestCase):
    def test_structure_chart_png_is_generated(self) -> None:
        candles = normalize_candles(pre_breakout_klines())
        signal = StructureSignal(
            symbol="TESTUSDT",
            interval="15m",
            signal_type=SIGNAL_PRE_BREAKOUT_NEAR,
            level="A",
            score=78,
            price=104.4,
            box_high=105.0,
            box_low=98.8,
            box_width_pct=6.1,
            position_in_box=90.0,
            distance_to_high_pct=0.57,
            distance_to_low_pct=5.36,
            touch_high_count=4,
            touch_low_count=3,
            atr_pct=1.1,
            atr_compressed=True,
            bb_width_pct=4.2,
            bb_compressed=True,
            volume_ratio=2.6,
            oi_change_pct_1h=4.2,
            oi_change_pct_4h=12.0,
            taker_buy_ratio=0.66,
            reason_lines=["unit test"],
        )
        with TemporaryDirectory() as tmp:
            path = Path(generate_structure_chart(signal, candles, Path(tmp)))
            payload = path.read_bytes()

        self.assertTrue(path.name.startswith("structure_TESTUSDT_15m_"))
        self.assertEqual(payload[:8], b"\x89PNG\r\n\x1a\n")


if __name__ == "__main__":
    unittest.main()
