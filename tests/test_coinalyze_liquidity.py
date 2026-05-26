from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.coinalyze_liquidity import CoinalyzeLiquidationProvider
from paopao_radar.config import Settings


class FakeCoinalyzeSource:
    enabled = True

    def __init__(self, payload):
        self.payload = payload

    def liquidation_history(self, symbol, from_ts, to_ts, interval="1hour"):
        return self.payload

    def diagnostics(self):
        return {"enabled": True}


class CoinalyzeLiquidityTests(unittest.TestCase):
    def test_short_liquidations_create_up_bias(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), coinalyze_enable=True, coinalyze_api_key="key")
            source = FakeCoinalyzeSource([
                {"symbol": "BTCUSDT_PERP.A", "history": [{"l": 100, "s": 500}, {"l": 100, "s": 500}]}
            ])

            context = CoinalyzeLiquidationProvider(settings, source).context("BTCUSDT", 100)

        self.assertTrue(context.available)
        self.assertEqual(context.source, "CoinalyzeHistory")
        self.assertEqual(context.liquidation_bias, "up")
        self.assertIn("历史清算量", context.reason_lines[0])

    def test_empty_history_is_unavailable(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), coinalyze_enable=True, coinalyze_api_key="key")
            context = CoinalyzeLiquidationProvider(settings, FakeCoinalyzeSource([])).context("BTCUSDT", 100)

        self.assertFalse(context.available)


if __name__ == "__main__":
    unittest.main()
