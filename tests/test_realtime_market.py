from __future__ import annotations

import unittest
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from paopao_radar.realtime_market import (
    BinanceRealtimeMarketService,
    BybitRealtimeMarketService,
    OkxRealtimeMarketService,
    RealtimeFeatureAggregator,
    RealtimeMarketPipeline,
    RealtimeFeatureStore,
    binance_stream_subscriptions,
    build_realtime_radar_boards,
    build_realtime_market_services,
    parse_binance_market_event,
    parse_bybit_market_events,
    parse_okx_market_events,
    select_bybit_realtime_symbols,
    select_okx_realtime_contracts,
    select_realtime_symbols,
)
from paopao_radar.web_services.public import public_radar_boards_payload, public_realtime_market_payload


class BinanceMarketEventTests(unittest.TestCase):
    def test_builds_deduplicated_bounded_subscriptions(self) -> None:
        subscriptions = binance_stream_subscriptions(
            ["BTCUSDT", "ethusdt", "BTCUSDT", "BTCUSD", ""],
            limit=2,
        )

        self.assertEqual(subscriptions, ["btcusdt@aggTrade", "ethusdt@aggTrade", "!forceOrder@arr"])

    def test_selects_liquid_usdt_perpetual_symbols(self) -> None:
        rows = [
            {"symbol": "LOWUSDT", "quoteVolume": "50"},
            {"symbol": "BTCUSDT", "quoteVolume": "1000"},
            {"symbol": "ETHUSDT", "quoteVolume": "500"},
            {"symbol": "XAUUSDT", "quoteVolume": "5000"},
            {"symbol": "BTCUSD", "quoteVolume": "9000"},
        ]

        selected = select_realtime_symbols(
            rows,
            valid_symbols={"LOWUSDT", "BTCUSDT", "ETHUSDT", "XAUUSDT"},
            excluded_base_assets={"XAU"},
            min_quote_volume=100,
            limit=2,
        )

        self.assertEqual(selected, ["BTCUSDT", "ETHUSDT"])

    def test_selects_bybit_linear_perpetuals_by_turnover(self) -> None:
        symbols = select_bybit_realtime_symbols(
            {
                "result": {"list": [
                    {"symbol": "BTCUSDT", "quoteCoin": "USDT", "settleCoin": "USDT", "status": "Trading", "contractType": "LinearPerpetual"},
                    {"symbol": "ETHUSDC", "quoteCoin": "USDC", "settleCoin": "USDC", "status": "Trading", "contractType": "LinearPerpetual"},
                    {"symbol": "OLDUSDT", "quoteCoin": "USDT", "settleCoin": "USDT", "status": "Closed", "contractType": "LinearPerpetual"},
                ]},
            },
            {"result": {"list": [{"symbol": "BTCUSDT", "turnover24h": "1000000"}]}},
            min_quote_volume=100,
            limit=10,
        )

        self.assertEqual(symbols, ["BTCUSDT"])

    def test_selects_okx_linear_usdt_swaps_and_preserves_contract_values(self) -> None:
        symbols, specs = select_okx_realtime_contracts(
            {"data": [
                {"instId": "BTC-USDT-SWAP", "instType": "SWAP", "ctType": "linear", "settleCcy": "USDT", "state": "live", "ctVal": "0.01", "ctValCcy": "BTC"},
                {"instId": "ETH-USD-SWAP", "instType": "SWAP", "ctType": "inverse", "settleCcy": "ETH", "state": "live", "ctVal": "100", "ctValCcy": "USD"},
            ]},
            {"data": [{"instId": "BTC-USDT-SWAP", "last": "100000", "vol24h": "1000"}]},
            min_quote_volume=100,
            limit=10,
        )

        self.assertEqual(symbols, ["BTCUSDT"])
        self.assertEqual(specs["BTC-USDT-SWAP"]["ct_val"], 0.01)
        self.assertEqual(specs["BTC-USDT-SWAP"]["ct_val_ccy"], "BTC")

    def test_parses_aggregate_trade_aggressor_side_and_notional(self) -> None:
        buy = parse_binance_market_event({
            "e": "aggTrade",
            "E": 1_700_000_000_100,
            "s": "BTCUSDT",
            "a": 11,
            "p": "50000",
            "q": "0.20",
            "T": 1_700_000_000_000,
            "m": False,
        })
        sell = parse_binance_market_event({
            "stream": "btcusdt@aggTrade",
            "data": {
                "e": "aggTrade",
                "E": 1_700_000_000_200,
                "s": "BTCUSDT",
                "a": 12,
                "p": "50000",
                "q": "0.10",
                "T": 1_700_000_000_100,
                "m": True,
            },
        })

        self.assertIsNotNone(buy)
        self.assertEqual(buy.side, "buy")
        self.assertEqual(buy.notional_usd, 10_000)
        self.assertEqual(buy.event_id, "binance:trade:BTCUSDT:11")
        self.assertIsNotNone(sell)
        self.assertEqual(sell.side, "sell")
        self.assertEqual(sell.notional_usd, 5_000)

    def test_parses_liquidation_from_executed_quantity_and_average_price(self) -> None:
        long_liquidation = parse_binance_market_event({
            "e": "forceOrder",
            "E": 1_700_000_010_100,
            "o": {
                "s": "ETHUSDT",
                "S": "SELL",
                "q": "5",
                "p": "2000",
                "ap": "1995",
                "z": "2",
                "T": 1_700_000_010_000,
            },
        })
        short_liquidation = parse_binance_market_event({
            "e": "forceOrder",
            "E": 1_700_000_020_100,
            "o": {
                "s": "ETHUSDT",
                "S": "BUY",
                "q": "3",
                "p": "2010",
                "ap": "0",
                "z": "0",
                "T": 1_700_000_020_000,
            },
        })

        self.assertIsNotNone(long_liquidation)
        self.assertEqual(long_liquidation.position_side, "long")
        self.assertEqual(long_liquidation.notional_usd, 3_990)
        self.assertIsNotNone(short_liquidation)
        self.assertEqual(short_liquidation.position_side, "short")
        self.assertEqual(short_liquidation.notional_usd, 6_030)

    def test_rejects_unknown_or_invalid_payloads(self) -> None:
        self.assertIsNone(parse_binance_market_event({"e": "markPriceUpdate"}))
        self.assertIsNone(parse_binance_market_event({
            "e": "aggTrade", "s": "BTCUSDT", "p": "0", "q": "1", "T": 1, "m": False,
        }))

    def test_normalizes_bybit_trade_batch_and_liquidation_position_side(self) -> None:
        trades = parse_bybit_market_events({
            "topic": "publicTrade.BTCUSDT",
            "data": [
                {"T": 1_000, "s": "BTCUSDT", "S": "Buy", "v": "0.1", "p": "100", "i": "t1"},
                {"T": 1_001, "s": "BTCUSDT", "S": "Sell", "v": "0.2", "p": "100", "i": "t2"},
            ],
        })
        liquidations = parse_bybit_market_events({
            "topic": "allLiquidation.BTCUSDT",
            "data": [{"T": 1_002, "s": "BTCUSDT", "S": "Buy", "v": "0.3", "p": "90"}],
        })

        self.assertEqual([event.side for event in trades], ["buy", "sell"])
        self.assertEqual(trades[0].notional_usd, 10)
        self.assertEqual(liquidations[0].position_side, "long")
        self.assertEqual(liquidations[0].notional_usd, 27)

    def test_normalizes_okx_swap_contract_size_without_treating_contracts_as_coins(self) -> None:
        payload = {
            "arg": {"channel": "trades", "instId": "BTC-USDT-SWAP"},
            "data": [{
                "instId": "BTC-USDT-SWAP", "tradeId": "10",
                "px": "100000", "sz": "2", "side": "buy", "ts": "1000",
            }],
        }
        events = parse_okx_market_events(payload, contract_specs={
            "BTC-USDT-SWAP": {"ct_val": 0.01, "ct_val_ccy": "BTC"},
        })

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].symbol, "BTCUSDT")
        self.assertEqual(events[0].quantity, 0.02)
        self.assertEqual(events[0].notional_usd, 2_000)
        self.assertEqual(parse_okx_market_events(payload, contract_specs={}), [])


class RealtimeFeatureAggregatorTests(unittest.TestCase):
    def test_builds_realtime_cvd_and_liquidation_boards(self) -> None:
        boards = build_realtime_radar_boards([
            {"symbol": "BTCUSDT", "cvd_usd": 500, "long_liquidation_usd": 100, "short_liquidation_usd": 20, "bucket_start": 60, "bucket_sec": 60},
            {"exchange": "bybit", "symbol": "BTCUSDT", "cvd_usd": 200, "long_liquidation_usd": 50, "short_liquidation_usd": 10, "bucket_start": 60, "bucket_sec": 60},
            {"symbol": "ETHUSDT", "cvd_usd": -300, "long_liquidation_usd": 10, "short_liquidation_usd": 200, "bucket_start": 60, "bucket_sec": 60},
        ], limit=2)

        self.assertEqual(boards[0]["key"], "realtime_futures_flow")
        self.assertEqual(boards[0]["positive"]["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(boards[0]["positive"]["items"][0]["value"], 700)
        self.assertEqual(boards[0]["negative"]["items"][0]["symbol"], "ETHUSDT")
        self.assertEqual(boards[1]["key"], "realtime_liquidations")
        self.assertEqual(boards[1]["positive"]["items"][0]["symbol"], "ETHUSDT")
        self.assertEqual(boards[1]["negative"]["items"][0]["symbol"], "BTCUSDT")

    def test_aggregates_trade_cvd_and_liquidations_in_closed_buckets(self) -> None:
        aggregator = RealtimeFeatureAggregator(bucket_sec=60)
        payloads = [
            {"e": "aggTrade", "s": "BTCUSDT", "a": 1, "p": "100", "q": "3", "T": 61_000, "m": False},
            {"e": "aggTrade", "s": "BTCUSDT", "a": 2, "p": "100", "q": "1", "T": 62_000, "m": True},
            {"e": "forceOrder", "E": 63_100, "o": {"s": "BTCUSDT", "S": "SELL", "q": "2", "p": "90", "ap": "90", "z": "2", "T": 63_000}},
        ]
        for payload in payloads:
            self.assertTrue(aggregator.add(parse_binance_market_event(payload)))

        self.assertEqual(aggregator.finalize_ready(119_999), [])
        rows = aggregator.finalize_ready(120_000)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["bucket_start"], 60)
        self.assertEqual(row["trade_buy_usd"], 300)
        self.assertEqual(row["trade_sell_usd"], 100)
        self.assertEqual(row["cvd_usd"], 200)
        self.assertEqual(row["trade_count"], 2)
        self.assertEqual(row["price_open"], 100)
        self.assertEqual(row["price_high"], 100)
        self.assertEqual(row["price_low"], 100)
        self.assertEqual(row["price_close"], 100)
        self.assertEqual(row["long_liquidation_usd"], 180)
        self.assertEqual(row["short_liquidation_usd"], 0)
        self.assertEqual(row["liquidation_count"], 1)

        self.assertFalse(aggregator.add(parse_binance_market_event(payloads[0])))
        self.assertEqual(aggregator.stats()["late_events"], 1)

    def test_finalized_watermark_is_scoped_to_each_symbol(self) -> None:
        aggregator = RealtimeFeatureAggregator(bucket_sec=60)
        btc = parse_binance_market_event({
            "e": "aggTrade", "s": "BTCUSDT", "a": 1,
            "p": "100", "q": "1", "T": 61_000, "m": False,
        })
        eth = parse_binance_market_event({
            "e": "aggTrade", "s": "ETHUSDT", "a": 2,
            "p": "10", "q": "1", "T": 61_500, "m": False,
        })

        self.assertTrue(aggregator.add(btc))
        self.assertEqual(len(aggregator.finalize_ready(120_000)), 1)
        self.assertFalse(aggregator.add(btc))
        self.assertTrue(aggregator.add(eth))

        stats = aggregator.stats()
        self.assertEqual(stats["late_events"], 1)
        self.assertEqual(stats["open_buckets"], 1)

    def test_ohlc_uses_event_time_when_trades_arrive_out_of_order(self) -> None:
        aggregator = RealtimeFeatureAggregator(bucket_sec=60)
        later = parse_binance_market_event({
            "e": "aggTrade", "s": "BTCUSDT", "a": 2,
            "p": "110", "q": "1", "T": 62_000, "m": False,
        })
        earlier = parse_binance_market_event({
            "e": "aggTrade", "s": "BTCUSDT", "a": 1,
            "p": "100", "q": "1", "T": 61_000, "m": False,
        })

        self.assertTrue(aggregator.add(later))
        self.assertTrue(aggregator.add(earlier))
        row = aggregator.finalize_ready(120_000)[0]

        self.assertEqual(row["price_open"], 100)
        self.assertEqual(row["price_close"], 110)
        self.assertEqual(row["price_high"], 110)
        self.assertEqual(row["price_low"], 100)


class RealtimeFeatureStoreTests(unittest.TestCase):
    def test_migrates_existing_feature_table_with_minute_price_columns(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "realtime.db"
            conn = sqlite3.connect(path)
            conn.execute(
                """
                CREATE TABLE realtime_market_features (
                    exchange TEXT NOT NULL, market TEXT NOT NULL, symbol TEXT NOT NULL,
                    bucket_start INTEGER NOT NULL, bucket_sec INTEGER NOT NULL,
                    trade_buy_usd REAL NOT NULL DEFAULT 0, trade_sell_usd REAL NOT NULL DEFAULT 0,
                    cvd_usd REAL NOT NULL DEFAULT 0, trade_count INTEGER NOT NULL DEFAULT 0,
                    long_liquidation_usd REAL NOT NULL DEFAULT 0,
                    short_liquidation_usd REAL NOT NULL DEFAULT 0,
                    liquidation_count INTEGER NOT NULL DEFAULT 0,
                    last_event_ms INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
                    PRIMARY KEY(exchange, market, symbol, bucket_start, bucket_sec)
                )
                """
            )
            conn.commit()
            conn.close()

            store = RealtimeFeatureStore(path)
            with store.connect() as migrated:
                columns = {row[1] for row in migrated.execute("PRAGMA table_info(realtime_market_features)")}

        self.assertTrue({"price_open", "price_high", "price_low", "price_close"}.issubset(columns))

    def test_replaces_same_finalized_bucket_without_double_counting(self) -> None:
        row = {
            "exchange": "binance",
            "market": "futures",
            "symbol": "BTCUSDT",
            "bucket_start": 60,
            "bucket_sec": 60,
            "trade_buy_usd": 300.0,
            "trade_sell_usd": 100.0,
            "cvd_usd": 200.0,
            "trade_count": 2,
            "price_open": 100.0,
            "price_high": 110.0,
            "price_low": 90.0,
            "price_close": 105.0,
            "long_liquidation_usd": 180.0,
            "short_liquidation_usd": 0.0,
            "liquidation_count": 1,
            "last_event_ms": 63_000,
        }
        with TemporaryDirectory() as tmp:
            store = RealtimeFeatureStore(Path(tmp) / "realtime.db")
            self.assertEqual(store.replace_many([row]), 1)
            self.assertEqual(store.replace_many([row]), 1)

            items = store.latest_by_symbol(now_ts=130, max_age_sec=120)
            recent = store.recent_rows(now_ts=130, window_sec=120)
            health = store.health_summary(now_ts=130, fresh_sec=120)

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["cvd_usd"], 200)
        self.assertEqual(items[0]["liquidation_count"], 1)
        self.assertEqual(items[0]["price_close"], 105)
        self.assertEqual(len(recent), 1)
        self.assertEqual(health["status"], "ready")
        self.assertEqual(health["feature_count"], 1)
        self.assertEqual(health["symbol_count"], 1)
        self.assertEqual(health["age_sec"], 10)
        self.assertEqual(health["exchanges"]["binance"]["status"], "ready")
        self.assertEqual(health["exchanges"]["binance"]["symbol_count"], 1)

    def test_service_tracks_open_and_subscription_acknowledgement(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                realtime_features_db_path=Path(tmp) / "realtime.db",
                realtime_market_bucket_sec=60,
                realtime_market_grace_ms=2000,
            )
            service = BinanceRealtimeMarketService(settings)
            ws = Mock()

            service._on_open(ws, ["btcusdt@aggTrade", "!forceOrder@arr"])
            service._on_message(ws, '{"result":null,"id":1}')

        subscription = ws.send.call_args.args[0]
        self.assertIn('"method":"SUBSCRIBE"', subscription)
        self.assertEqual(service.stats()["open_count"], 1)
        self.assertEqual(service.stats()["subscription_acks"], 1)

    def test_bybit_and_okx_services_normalize_messages_through_shared_pipeline(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                realtime_features_db_path=Path(tmp) / "realtime.db",
                realtime_market_bucket_sec=60,
                realtime_market_grace_ms=2000,
            )
            bybit = BybitRealtimeMarketService(settings)
            bybit_ws = Mock()
            bybit._on_open(bybit_ws, ["publicTrade.BTCUSDT", "allLiquidation.BTCUSDT"])
            bybit._on_message(bybit_ws, '{"op":"subscribe","success":true}')
            bybit._on_message(bybit_ws, '{"topic":"publicTrade.BTCUSDT","data":[{"T":1000,"s":"BTCUSDT","S":"Buy","v":"0.1","p":"100","i":"t1"}]}')

            okx = OkxRealtimeMarketService(settings)
            okx._connection_context = {
                "contract_specs": {"BTC-USDT-SWAP": {"ct_val": 0.01, "ct_val_ccy": "BTC"}},
            }
            okx_ws = Mock()
            okx._on_open(okx_ws, [{"channel": "trades", "instId": "BTC-USDT-SWAP"}])
            okx._on_message(okx_ws, '{"event":"subscribe","arg":{"channel":"trades","instId":"BTC-USDT-SWAP"}}')
            okx._on_message(okx_ws, '{"arg":{"channel":"trades","instId":"BTC-USDT-SWAP"},"data":[{"instId":"BTC-USDT-SWAP","tradeId":"10","px":"100000","sz":"2","side":"buy","ts":"1000"}]}')

        self.assertIn('"op":"subscribe"', bybit_ws.send.call_args_list[0].args[0])
        self.assertEqual(bybit.stats()["accepted_events"], 1)
        self.assertEqual(bybit.stats()["subscription_acks"], 1)
        self.assertIn('"channel":"trades"', okx_ws.send.call_args_list[0].args[0])
        self.assertEqual(okx.stats()["accepted_events"], 1)
        self.assertEqual(okx.stats()["subscription_acks"], 1)

    def test_multi_exchange_service_builder_respects_optional_exchange_flags(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                realtime_features_db_path=Path(tmp) / "realtime.db",
                realtime_market_bucket_sec=60,
                realtime_market_grace_ms=2000,
                realtime_bybit_enable=True,
                realtime_okx_enable=False,
            )
            services = build_realtime_market_services(settings)

        self.assertEqual(
            [service.service_name for service in services],
            ["binance_realtime_market", "bybit_realtime_market"],
        )

    def test_service_reuses_symbol_metadata_across_fast_reconnects(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = SimpleNamespace(
                realtime_features_db_path=Path(tmp) / "realtime.db",
                realtime_market_bucket_sec=60,
                realtime_market_grace_ms=2000,
                realtime_market_symbol_refresh_sec=300,
            )
            service = BinanceRealtimeMarketService(settings)
            service._load_connection = Mock(return_value=(
                ["BTCUSDT"], ["btcusdt@aggTrade", "!forceOrder@arr"], {},
            ))

            first = service._connection_definition()
            second = service._connection_definition()

        self.assertEqual(first, second)
        service._load_connection.assert_called_once_with()

    def test_service_rejects_replayed_events_from_buckets_persisted_before_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "realtime.db"
            store = RealtimeFeatureStore(path)
            store.replace_many([{
                "exchange": "binance", "market": "futures", "symbol": "BTCUSDT",
                "bucket_start": 60, "bucket_sec": 60,
                "trade_buy_usd": 100, "trade_sell_usd": 0, "cvd_usd": 100,
                "trade_count": 1, "long_liquidation_usd": 0,
                "short_liquidation_usd": 0, "liquidation_count": 0,
                "last_event_ms": 61_000,
            }])
            settings = SimpleNamespace(
                realtime_features_db_path=path,
                realtime_market_bucket_sec=60,
                realtime_market_grace_ms=2000,
            )
            service = BinanceRealtimeMarketService(settings, store=store)

            replayed = {"e": "aggTrade", "s": "BTCUSDT", "a": 2, "p": "100", "q": "1", "T": 61_500, "m": False}
            current = {"e": "aggTrade", "s": "BTCUSDT", "a": 3, "p": "100", "q": "1", "T": 121_000, "m": False}

            self.assertFalse(service.pipeline.handle_message(replayed))
            self.assertTrue(service.pipeline.handle_message(current))
            self.assertEqual(service.stats()["late_events"], 1)

    def test_public_payload_exposes_fresh_features_and_explicit_empty_state(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "realtime.db"
            settings = SimpleNamespace(realtime_features_db_path=path)
            empty = public_realtime_market_payload(settings=settings, now_ts=130)
            store = RealtimeFeatureStore(path)
            store.replace_many([{
                "exchange": "binance", "market": "futures", "symbol": "BTCUSDT",
                "bucket_start": 60, "bucket_sec": 60,
                "trade_buy_usd": 300, "trade_sell_usd": 100, "cvd_usd": 200,
                "trade_count": 2, "price_open": 100, "price_high": 110,
                "price_low": 95, "price_close": 105, "long_liquidation_usd": 180,
                "short_liquidation_usd": 0, "liquidation_count": 1,
                "last_event_ms": 63_000,
            }])
            payload = public_realtime_market_payload(
                symbol="BTCUSDT", settings=settings, now_ts=130, max_age_sec=120,
            )

        self.assertTrue(empty["ok"])
        self.assertEqual(empty["data"]["data_status"], "unavailable")
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["data_status"], "ready")
        self.assertEqual(payload["data"]["items"][0]["cvd_usd"], 200)
        self.assertEqual(payload["data"]["items"][0]["price_close"], 105)
        self.assertEqual(payload["data"]["items"][0]["price_change_pct"], 5)
        self.assertEqual(payload["data"]["items"][0]["age_sec"], 10)

    def test_radar_boards_append_realtime_boards_without_replacing_rest_fallback(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "realtime.db"
            settings = SimpleNamespace(cockpit_v2_mode="enabled", realtime_features_db_path=path)
            RealtimeFeatureStore(path).replace_many([{
                "exchange": "binance", "market": "futures", "symbol": "BTCUSDT",
                "bucket_start": 60, "bucket_sec": 60,
                "trade_buy_usd": 300, "trade_sell_usd": 100, "cvd_usd": 200,
                "trade_count": 2, "long_liquidation_usd": 180,
                "short_liquidation_usd": 0, "liquidation_count": 1,
                "last_event_ms": 63_000,
            }])
            cockpit = {
                "schema_version": "test", "generated_at": "", "window_sec": 3600,
                "data_status": "ready", "warnings": [], "coverage": {"assets": 1},
                "readiness": {}, "boards": [{"key": "price"}], "methodology": {},
            }
            with patch("paopao_radar.web_services.public._market_cockpit_raw", return_value=cockpit):
                payload = public_radar_boards_payload(settings=settings, now_ts=130)

        keys = [board["key"] for board in payload["data"]["boards"]]
        self.assertEqual(keys, ["price", "realtime_futures_flow", "realtime_liquidations"])
        self.assertEqual(payload["data"]["coverage"]["realtime"], 1)

    def test_pipeline_parses_json_and_flushes_only_closed_buckets(self) -> None:
        with TemporaryDirectory() as tmp:
            store = RealtimeFeatureStore(Path(tmp) / "realtime.db")
            pipeline = RealtimeMarketPipeline(store, bucket_sec=60, grace_ms=2_000)
            accepted = pipeline.handle_message(
                '{"e":"aggTrade","s":"BTCUSDT","a":5,"p":"100","q":"2","T":61000,"m":false}'
            )

            self.assertTrue(accepted)
            self.assertEqual(pipeline.flush(now_ms=121_999), 0)
            self.assertEqual(pipeline.flush(now_ms=122_000), 1)
            items = store.latest_by_symbol(now_ts=130, max_age_sec=120)

        self.assertEqual(items[0]["cvd_usd"], 200)


if __name__ == "__main__":
    unittest.main()
