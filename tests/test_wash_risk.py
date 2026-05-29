from __future__ import annotations

import unittest

from paopao_radar.wash_risk import calculate_wash_risk


class WashRiskTests(unittest.TestCase):
    def test_high_risk_flags_volume_oi_and_cross_exchange_gaps(self) -> None:
        result = calculate_wash_risk({
            "volume_ratio": 4.0,
            "price_change_pct": 0.5,
            "oi_change_pct": 18.0,
            "taker_buy_sell_ratio": 1.02,
            "trade_count_ratio": 3.5,
            "avg_trade_usd": 40,
            "cross_exchange_confirmed": False,
            "volume_marketcap_ratio": 3.2,
            "oi_marketcap_ratio": 0.55,
            "price_1h_change_pct": 14.0,
        })

        self.assertEqual(result["risk_level"], "HIGH")
        self.assertGreaterEqual(result["wash_risk_score"], 70)
        self.assertIn("成交额暴增但价格位移很小", result["risk_reasons"])

    def test_low_risk_when_confirmation_and_direction_are_clean(self) -> None:
        result = calculate_wash_risk({
            "volume_ratio": 1.4,
            "price_change_pct": 3.5,
            "oi_change_pct": 7.0,
            "taker_buy_sell_ratio": 1.24,
            "trade_count_ratio": 1.2,
            "avg_trade_usd": 900,
            "cross_exchange_confirmed": True,
            "volume_marketcap_ratio": 0.2,
            "oi_marketcap_ratio": 0.08,
            "price_1h_change_pct": 4.0,
        })

        self.assertEqual(result["risk_level"], "LOW")
        self.assertLess(result["wash_risk_score"], 40)


if __name__ == "__main__":
    unittest.main()
