from __future__ import annotations

import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import Settings
from radars.pulse.simple_alert import (
    SIGNAL_DIRECTIONS,
    SimpleAlertConfig,
    _bold_italic_serif,
    _follow_action,
    _format_card,
    _send_test_push,
    _series_pct,
    _volume_emoji,
    classify_template,
)
from shared.telegram import plain_fallback


class ClassifyTemplateTests(unittest.TestCase):
    def test_default_scan_limit_preserves_chart_budget(self) -> None:
        self.assertEqual(SimpleAlertConfig().scan_limit, 80)

    def test_six_templates(self) -> None:
        cases = [
            ((7.0, 31.0, 47000.0), "health_up"),
            ((18.0, 8.0, -96000.0), "false_strong"),
            ((16.0, -8.0, 221000.0), "short_covering"),
            ((-15.0, 12.0, -168000.0), "health_down"),
            ((-16.0, 6.0, 196000.0), "false_weak"),
            ((-15.0, -15.0, -238000.0), "panic_dump"),
        ]
        for values, expected in cases:
            with self.subTest(values=values):
                self.assertEqual(
                    classify_template(*values, 1.0, 5000.0), expected
                )

    def test_dropped_combos_return_none(self) -> None:
        self.assertIsNone(classify_template(10.0, -5.0, -20000.0, 1.0, 5000.0))
        self.assertIsNone(classify_template(-10.0, -5.0, 20000.0, 1.0, 5000.0))

    def test_missing_cvd_returns_none(self) -> None:
        self.assertIsNone(classify_template(10.0, 10.0, None, 1.0, 5000.0))

    def test_cvd_below_minimum_is_flat(self) -> None:
        self.assertIsNone(classify_template(10.0, 10.0, 1000.0, 1.0, 5000.0))

    def test_signal_effectiveness_direction_matches_each_template(self) -> None:
        self.assertEqual(SIGNAL_DIRECTIONS["health_up"], "long")
        self.assertEqual(SIGNAL_DIRECTIONS["false_weak"], "long")
        self.assertEqual(SIGNAL_DIRECTIONS["false_strong"], "short")
        self.assertEqual(SIGNAL_DIRECTIONS["short_covering"], "short")
        self.assertEqual(SIGNAL_DIRECTIONS["health_down"], "short")
        self.assertEqual(SIGNAL_DIRECTIONS["panic_dump"], "short")


class SeriesPctTests(unittest.TestCase):
    def test_series_pct(self) -> None:
        series = [1.0, 1.1]
        self.assertAlmostEqual(_series_pct(series, 1), 10.0)
        self.assertIsNone(_series_pct(series, 5))


class FollowActionTests(unittest.TestCase):
    def _item(self, template: str, price: float, oi: float) -> dict:
        return {
            "symbol": "TESTUSDT",
            "template": template,
            "price_map": {3: price},
            "oi_map": {3: oi},
        }

    def test_first_trigger_sends_count_one(self) -> None:
        cfg = SimpleAlertConfig()
        state: dict = {}
        action = _follow_action(state, self._item("health_up", 7.0, 31.0), cfg, 1000)
        self.assertEqual(action, (1, "health_up"))

    def test_same_template_needs_escalation(self) -> None:
        cfg = SimpleAlertConfig()
        state = {
            "TESTUSDT": {
                "event_start_ts": 0,
                "count": 1,
                "template": "health_up",
                "peak_metric": 31.0,
            }
        }
        self.assertIsNone(_follow_action(state, self._item("health_up", 7.0, 33.0), cfg, 1000))
        action = _follow_action(state, self._item("health_up", 7.0, 45.0), cfg, 1000)
        self.assertEqual(action, (2, "health_up"))

    def test_template_change_sends_next_count(self) -> None:
        cfg = SimpleAlertConfig()
        state = {
            "TESTUSDT": {
                "event_start_ts": 0,
                "count": 1,
                "template": "health_up",
                "peak_metric": 31.0,
            }
        }
        action = _follow_action(state, self._item("false_strong", 18.0, 8.0), cfg, 1000)
        self.assertEqual(action, (2, "false_strong"))

    def test_max_count_blocks_further_sends(self) -> None:
        cfg = SimpleAlertConfig(follow_max_count=3)
        state = {
            "TESTUSDT": {
                "event_start_ts": 0,
                "count": 3,
                "template": "health_up",
                "peak_metric": 31.0,
            }
        }
        self.assertIsNone(_follow_action(state, self._item("health_up", 7.0, 60.0), cfg, 1000))

    def test_expired_event_restarts(self) -> None:
        cfg = SimpleAlertConfig(follow_window_sec=7200)
        state = {
            "TESTUSDT": {
                "event_start_ts": 0,
                "count": 3,
                "template": "health_up",
                "peak_metric": 31.0,
            }
        }
        action = _follow_action(state, self._item("health_up", 7.0, 45.0), cfg, 10000)
        self.assertEqual(action, (1, "health_up"))


class FormatCardTests(unittest.TestCase):
    def test_pair_uses_bold_italic_serif_without_changing_digits(self) -> None:
        self.assertEqual(
            _bold_italic_serif("STORJ/USDT"),
            "𝑺𝑻𝑶𝑹𝑱/𝑼𝑺𝑫𝑻",
        )
        self.assertEqual(
            _bold_italic_serif("1000PEPE/USDT"),
            "1000𝑷𝑬𝑷𝑬/𝑼𝑺𝑫𝑻",
        )

    def test_card_contains_fields_and_no_double_sign(self) -> None:
        cfg = SimpleAlertConfig()
        item = {
            "symbol": "CETUSUSDT",
            "base": "CETUS",
            "tier": "alt",
            "tier_label": "山寨币",
            "threshold": 15.0,
            "template": "health_up",
            "current_price": 0.0215,
            "current_oi_usd": 2520000.0,
            "quote_volume_24h": 1e8,
            "price_map": {1: 2.71, 3: 7.06, 6: 15.08, 12: 16.32, 288: 23.82},
            "oi_map": {1: 1.53, 3: 31.11, 6: 57.85, 12: 60.56, 288: 72.79},
            "volume_map": {1: 0.17, 3: 1.29, 6: 79.84, 12: 64.30},
            "futures_flow": {1: 12000.0, 3: 45000.0, 12: 180000.0, 288: 320000.0},
            "spot_flow": {1: 38000.0, 3: 147000.0, 12: 367100.0, 288: 578000.0},
            "cvd_net_15m": 47000.0,
            "market_cap": 16600000.0,
            "long_short_ratio": 2.15,
        }
        with patch("radars.pulse.simple_alert.time.time", return_value=0):
            text = _format_card(item, 1, cfg)
        self.assertIn("健康上涨提醒 (第1次)", text)
        self.assertIn("上涨 31.11% ↗️", text)
        self.assertIn("市值", text)
        self.assertIn("当前持仓: $2.52M", text)
        self.assertIn("多空比", text)
        self.assertIn("偏多，大户多头持仓占优", text)
        self.assertIn("📌 结论: 健康上涨，新多进场", text)
        self.assertIn("组合判断: 价格↑ · 持仓↑ · CVD↑", text)
        self.assertIn("15分钟CVD方向: 上升", text)
        self.assertIn("<pre>", text)
        self.assertIn("数据来源: 币安 Binance", text)
        self.assertIn("tradingview.com/chart", text)
        self.assertIn("coinglass.com/tv/zh", text)
        self.assertIn(
            "𝑪𝑬𝑻𝑼𝑺/𝑼𝑺𝑫𝑻 (<code>CETUS</code>) 🟢 上涨 31.11% ↗️",
            text,
        )
        self.assertNotIn("<b>CETUS/USDT</b>", text)
        self.assertNotIn("📋<code>CETUS</code>", text)
        self.assertIn("⏰ 提醒时间: 1970-01-01 08:00:00 (北京时间)", text)
        self.assertNotIn("(UTC+8)", text)
        self.assertLessEqual(len(plain_fallback(text)), 1024)


    def test_card_marks_missing_spot(self) -> None:
        cfg = SimpleAlertConfig()
        item = {
            "symbol": "XUSDT", "base": "X", "tier": "alt", "tier_label": "山寨币",
            "threshold": 15.0, "template": "health_up",
            "current_price": 0.1, "current_oi_usd": 1000000.0, "quote_volume_24h": 1e8,
            "price_map": {1: 1.0, 3: 15.0, 6: 20.0, 12: 18.0, 288: 30.0},
            "oi_map": {1: 1.0, 3: 16.0, 6: 20.0, 12: 18.0, 288: 40.0},
            "volume_map": {1: 1.0, 3: 2.0, 6: 2.0, 12: 2.0},
            "futures_flow": {1: 10000.0, 3: 5000000.0, 12: 6000000.0, 288: 40000000.0},
            "spot_flow": {1: None, 3: None, 12: None, 288: None},
            "cvd_net_15m": 5000000.0,
            "market_cap": 10000000.0, "long_short_ratio": 1.2,
        }
        text = _format_card(item, 1, cfg)
        self.assertIn("无币安现货", text)
        self.assertIn("现货—", text)
        self.assertIn("合约口径净", text)


class VolumeEmojiTests(unittest.TestCase):
    def test_progressive_tiers(self) -> None:
        cases = [
            (None, "➡️"),
            (0.17, "➡️"),
            (1.99, "➡️"),
            (2.0, "🔺"),
            (4.99, "🔺"),
            (5.0, "⚡"),
            (19.99, "⚡"),
            (20.0, "💥"),
            (79.84, "💥"),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(_volume_emoji(value), expected)


class TestPushTests(unittest.TestCase):
    def test_test_push_reuses_current_chart_delivery_path(self) -> None:
        chart = b"\x89PNG\r\n\x1a\npulse-test"
        source = SimpleNamespace(close=MagicMock())
        gateway = MagicMock()
        gateway.send.return_value = SimpleNamespace(
            status="sent",
            reason="telegram_photo_api",
            sent=True,
        )

        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            with (
                patch(
                    "radars.pulse.simple_alert.BinanceDataSource",
                    return_value=source,
                ),
                patch(
                    "radars.pulse.simple_alert._render_pulse_chart",
                    return_value=chart,
                ) as render_mock,
                patch(
                    "radars.pulse.simple_alert.closed_window",
                    return_value=SimpleNamespace(end_ms=1_700_000_000_000),
                ),
                redirect_stdout(StringIO()),
            ):
                code = _send_test_push(
                    settings,
                    gateway,
                    send=True,
                    confirm_real_send=True,
                )

        self.assertEqual(code, 0)
        source.close.assert_called_once_with()
        render_mock.assert_called_once()
        kwargs = gateway.send.call_args.kwargs
        self.assertEqual(kwargs["photo"], chart)
        self.assertFalse(kwargs["enrich_market_context"])
        self.assertNotIn("link_preview_url", kwargs)


if __name__ == "__main__":
    unittest.main()
