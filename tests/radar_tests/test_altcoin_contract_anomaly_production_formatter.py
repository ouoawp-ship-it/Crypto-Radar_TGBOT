from __future__ import annotations

import unittest

from radars.altcoin_contract_anomaly.production_formatter import (
    TELEGRAM_TEMPLATE_ID,
    group_production_events,
    render_production_event_group,
)


def event(
    event_type: str = "short_squeeze_ignition",
    *,
    event_id: str = "event-1",
    symbol: str = "ACEUSDT",
    window_end: str = "2026-08-08T04:05:00+00:00",
    factors: list[str] | None = None,
) -> dict[str, object]:
    names = {
        "short_squeeze_ignition": "逼空启动",
        "high_leverage_anomaly": "高杠杆异动",
        "anomaly_weakening": "异动减弱",
        "candidate_condition_invalidated": "候选条件失效",
    }
    return {
        "schema_version": 2,
        "rules_version": "altcoin_contract_anomaly.p2.v2",
        "event_id": event_id,
        "event_type": event_type,
        "event_name_cn": names[event_type],
        "symbol": symbol,
        "window_start": "2026-08-08T04:00:00+00:00",
        "window_end": window_end,
        "candidate_tags": [
            "short_squeeze_candidate",
            "high_leverage_candidate",
        ],
        "confirmed_factor_families": factors or [
            "price_momentum",
            "volume_expansion",
            "open_interest",
        ],
        "candidate_snapshot": {
            "market_cap_usd": 50_000_000,
            # Deliberately stale P1 ratio. The formatter must use the coherent
            # current market cap and ratio in factor_values alongside the
            # current closed OI.
            "binance_oi_market_cap_ratio": 0.123,
        },
        "factor_values": {
            "market_cap_usd": 11_770_000,
            "oi_value_usd": 9_010_000,
            "oi_market_cap_ratio": 9_010_000 / 11_770_000,
            "funding_rate": -0.001356,
            "price_change_1m": 0.018,
            "price_change_5m": 0.032,
            "oi_change_5m": 0.047,
            "aggressive_buy_ratio_5m": 0.682,
            "volume_anomaly_multiple": 2.6,
            "cvd_5m_usd": 225_000,
            "short_liquidation_5m_usd": 120_000,
            "long_liquidation_5m_usd": 5_000,
        },
        "data_quality": "complete",
    }


class AltcoinAnomalyProductionFormatterTests(unittest.TestCase):
    def test_template_id_and_structured_values_render_without_score(self) -> None:
        self.assertEqual(TELEGRAM_TEMPLATE_ID, "TG_ALTCOIN_CONTRACT_ANOMALY")

        [text] = render_production_event_group([event()])

        self.assertIn("山寨合约异动｜首次确认", text)
        self.assertIn("候选依据：潜在逼空 + 高合约杠杆", text)
        self.assertIn("实时确认：3项", text)
        self.assertIn("确认依据：价格动量｜成交量放大｜OI变化", text)
        self.assertIn("市值：$11.77M", text)
        self.assertIn("Binance OI：$9.01M", text)
        self.assertIn("OI/市值：76.6%", text)
        self.assertIn("资金费率：-0.1356%", text)
        self.assertIn("1分钟价格：+1.8%", text)
        self.assertIn("5分钟价格：+3.2%", text)
        self.assertIn("5分钟OI：+4.7%", text)
        self.assertIn("主动买入占比：68.2%", text)
        self.assertIn("成交量：基线的2.6倍", text)
        self.assertIn("空头爆仓：$120K", text)
        self.assertIn("2026-08-08 12:05:00（北京时间）", text)
        self.assertIn("数据完整度：完整", text)
        self.assertNotIn("总分", text)
        self.assertNotIn("成功率", text)

    def test_same_symbol_and_window_events_merge_once_and_deduplicate(self) -> None:
        first = event(event_id="event-1")
        second = event(
            "high_leverage_anomaly",
            event_id="event-2",
            factors=["price_momentum", "open_interest", "liquidation"],
        )
        duplicate = dict(first)

        groups = group_production_events([second, duplicate, first])
        [text] = render_production_event_group(groups[0])

        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)
        self.assertIn("异动类型：逼空启动 + 高杠杆异动", text)
        self.assertIn("实时确认：4项", text)
        self.assertEqual(text.count("<b>ACEUSDT</b>"), 1)

        different_window = event(event_id="event-3")
        different_window["window_start"] = "2026-08-08T04:04:00+00:00"
        self.assertEqual(
            len(group_production_events([first, different_window])),
            2,
        )

    def test_notification_headings_are_explicit(self) -> None:
        cases = (
            ("new_round", "新一轮异动"),
            ("signal_expired", "信号过期"),
            ("candidate_invalidated", "候选失效"),
        )
        for kind, expected in cases:
            with self.subTest(kind=kind):
                row = event()
                row["notification_kind"] = kind
                [text] = render_production_event_group([row])
                self.assertIn(expected, text)

        [from_context] = render_production_event_group(
            [event()],
            {"notification_kind": "new_round"},
        )
        self.assertIn("新一轮异动", from_context)

        weakening = event("anomaly_weakening")
        invalidated = event("candidate_condition_invalidated")
        self.assertIn(
            "信号过期",
            render_production_event_group([weakening])[0],
        )
        self.assertIn(
            "候选失效",
            render_production_event_group([invalidated])[0],
        )

    def test_html_is_escaped_and_missing_values_are_not_fabricated(self) -> None:
        row = event(symbol="ACE<&USDT")
        row["event_name_cn"] = "逼空<&>"
        row["candidate_snapshot"] = {
            "market_cap_usd": None,
            "binance_oi_market_cap_ratio": None,
        }
        row["factor_values"] = {
            "market_cap_usd": None,
            "oi_value_usd": None,
            "oi_market_cap_ratio": None,
            "funding_rate": None,
            "price_change_1m": None,
            "price_change_5m": None,
            "oi_change_5m": None,
            "aggressive_buy_ratio_5m": None,
            "volume_anomaly_multiple": None,
            "cvd_5m_usd": None,
            "short_liquidation_5m_usd": None,
            "long_liquidation_5m_usd": None,
        }

        [text] = render_production_event_group([row])

        self.assertIn("ACE&lt;&amp;USDT", text)
        self.assertIn("逼空&lt;&amp;&gt;", text)
        self.assertNotIn("ACE<&USDT", text)
        self.assertGreaterEqual(text.count("缺数据"), 10)
        self.assertNotIn("市值：$0", text)
        self.assertNotIn("Binance OI：$0", text)

    def test_pagination_preserves_complete_lines_and_page_numbers(self) -> None:
        row = event()
        pages = render_production_event_group([row], max_chars=280)

        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 280 for page in pages))
        for index, page in enumerate(pages, start=1):
            self.assertIn(f"第{index}/{len(pages)}页", page)
            self.assertIn("<b>ACEUSDT</b>", page)
        self.assertEqual(
            sum("确认依据：价格动量｜成交量放大｜OI变化" in page for page in pages),
            1,
        )
        self.assertEqual(
            sum("数据时间：2026-08-08 12:05:00（北京时间）" in page for page in pages),
            1,
        )

    def test_conflicting_structured_values_fail_closed(self) -> None:
        first = event(event_id="one")
        second = event("high_leverage_anomaly", event_id="two")
        second["factor_values"] = {**dict(second["factor_values"]), "oi_value_usd": 1}

        with self.assertRaisesRegex(ValueError, "conflicting factor_values"):
            render_production_event_group([first, second])


if __name__ == "__main__":
    unittest.main()
