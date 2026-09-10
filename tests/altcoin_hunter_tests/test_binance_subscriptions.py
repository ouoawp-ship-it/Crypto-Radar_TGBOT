import unittest

from radars.altcoin_hunter.adapters.base import Route
from radars.altcoin_hunter.subscription_plan import (
    StreamSpec, diff_plans, plan_subscriptions, synthetic_instruments,
)


class BinanceSubscriptionTests(unittest.TestCase):
    def test_600_1000_1500_route_shards_and_complete_denominators(self):
        for count, market_shards in ((600, 1), (1000, 2), (1500, 2)):
            with self.subTest(count=count):
                promoted = [f"COIN{i}USDT" for i in range(count // 20)]
                plan = plan_subscriptions(synthetic_instruments(count), promoted_symbols=promoted)
                self.assertEqual(len([s for s in plan.shards if s.route == Route.MARKET]), market_shards)
                self.assertEqual(len([s for s in plan.shards if s.route == Route.PUBLIC]), 1)
                self.assertTrue(all(len(s.streams) <= 800 for s in plan.shards))
                self.assertEqual(sum(len(s.streams) for s in plan.shards), count + len(promoted) + 2)
                self.assertEqual(plan.coverage["agg_trade"], {"covered": count, "total": count, "ratio": 1.0, "enabled": True})
                self.assertEqual(plan.coverage["mark_price"]["covered"], count)
                self.assertEqual(plan.coverage["basic_bbo"]["covered"], count)
                self.assertEqual(plan.coverage["promoted_bbo"]["covered"], len(promoted))
                self.assertFalse(plan.coverage_incomplete)

    def test_wire_case_and_default_mark_interval_follow_official_mapping(self):
        for seconds, mark in ((3, "!markPrice@arr"), (1, "!markPrice@arr@1s")):
            plan = plan_subscriptions(synthetic_instruments(1), mark_price_interval_sec=seconds,
                                      promoted_symbols=("COIN0USDT",), include_liquidations=True)
            streams = [stream for shard in plan.shards for stream in shard.streams]
            self.assertEqual({s.wire_name for s in streams}, {mark, "!bookTicker", "coin0usdt@aggTrade", "coin0usdt@bookTicker", "!forceOrder@arr"})
            self.assertTrue(all(s.canonical_name == s.wire_name.lower() for s in streams))
            self.assertEqual(next(s for s in streams if s.kind == "book_ticker_all").expected_interval_ms, 5000)
            self.assertEqual(next(s for s in streams if s.kind == "book_ticker").expected_interval_ms, 0)
            self.assertEqual(plan.coverage["liquidation"]["covered"], 1)

    def test_max_1024_includes_global_stream_and_rejects_1025(self):
        plan = plan_subscriptions(synthetic_instruments(1024), max_streams_per_connection=1024)
        self.assertEqual(sorted(len(s.streams) for s in plan.shards if s.route == Route.MARKET), [1, 1024])
        for cap in (0, True, 1025, 800.0):
            with self.subTest(cap=cap), self.assertRaises(ValueError):
                plan_subscriptions([], max_streams_per_connection=cap)

    def test_route_budget_exposes_uncovered_without_shrinking_eligible_pool(self):
        plan = plan_subscriptions(synthetic_instruments(1000), max_connections_per_route={Route.MARKET: 1, Route.PUBLIC: 0})
        self.assertEqual(plan.coverage["eligible_instruments"], 1000)
        self.assertEqual(plan.coverage["agg_trade"]["covered"], 799)
        self.assertEqual(plan.coverage["agg_trade"]["total"], 1000)
        self.assertEqual(plan.coverage["basic_bbo"]["covered"], 0)
        self.assertTrue(plan.coverage_incomplete)
        self.assertEqual(len(plan.uncovered_instruments), 1000)
        self.assertEqual(plan.reason, "route_capacity_uncovered")

    def test_input_order_has_no_effect(self):
        instruments = list(synthetic_instruments(1000))
        self.assertEqual(plan_subscriptions(instruments).to_dict(), plan_subscriptions(reversed(instruments)).to_dict())

    def test_ten_percent_changes_preserve_every_surviving_assignment(self):
        first = plan_subscriptions(synthetic_instruments(1000))
        second = plan_subscriptions(synthetic_instruments(1000, start_index=100), previous=first)
        changes = diff_plans(first, second)
        self.assertEqual(len(changes["streams_to_add"]), 100)
        self.assertEqual(len(changes["streams_to_remove"]), 100)
        self.assertEqual(changes["moved"], [])
        self.assertEqual(len(changes["unchanged_streams"]), 902)
        self.assertEqual(second.generation, first.generation + 1)
        self.assertLessEqual(len(changes["affected_connections"]), 2)

    def test_removal_does_not_compact_and_no_change_keeps_generation(self):
        first = plan_subscriptions(synthetic_instruments(1500))
        removed = plan_subscriptions(synthetic_instruments(1000, start_index=500), previous=first)
        self.assertEqual(diff_plans(first, removed)["moved"], [])
        unchanged = plan_subscriptions(synthetic_instruments(1000, start_index=500), previous=removed)
        self.assertEqual(unchanged.generation, removed.generation)
        self.assertEqual(unchanged.reason, "unchanged")

    def test_wrong_venue_market_and_ineligible_are_excluded(self):
        good = next(synthetic_instruments(1))
        rows = [good]
        for index, override in enumerate(({"exchange": "bybit"}, {"market": "coin_perpetual"}, {"market": "spot"}, {"quote_currency": "USDC"}, {"eligibility_status": "INELIGIBLE"}), 1):
            rows.append({**good, "symbol": f"COIN{index}USDT", "exchange_symbol": f"COIN{index}USDT", "instrument_id": str(index), **override})
        plan = plan_subscriptions(rows)
        self.assertEqual(plan.eligible_symbols, ("COIN0USDT",))
        self.assertEqual(plan.coverage["excluded_instruments"], 5)

    def test_bare_symbols_and_duplicate_symbols_fail_explicitly(self):
        with self.assertRaises(ValueError):
            plan_subscriptions(["BTCUSDT"])
        record = next(synthetic_instruments(1))
        with self.assertRaises(ValueError):
            plan_subscriptions([record, record])
        with self.assertRaises(ValueError):
            plan_subscriptions([record], promoted_symbols=("UNKNOWNUSDT",))

    def test_instrument_identity_is_preserved_and_cross_route_spec_rejected(self):
        record = {**next(synthetic_instruments(1)), "instrument_id": "verified-contract-123"}
        stream = next(s for shard in plan_subscriptions([record]).shards for s in shard.streams if s.kind == "agg_trade")
        self.assertEqual(stream.instrument_id, "verified-contract-123")
        with self.assertRaises(ValueError):
            StreamSpec(Route.PUBLIC, "coin0usdt@aggtrade", "coin0usdt@aggTrade", "COIN0USDT", "agg_trade", 100, "id")

    def test_empty_pool_creates_no_connections(self):
        plan = plan_subscriptions([])
        self.assertEqual(plan.shards, ())
        self.assertEqual(plan.coverage["eligible_instruments"], 0)


if __name__ == "__main__":
    unittest.main()
