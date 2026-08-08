from __future__ import annotations

import json
import math
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shared.realtime_market import (
    BinanceRealtimeMarketService,
    MarkPriceBook,
    MarkPriceUpdate,
    SubscriptionLedger,
    build_binance_subscription_plan,
    parse_binance_mark_price_update,
    run_realtime_market_session,
    run_realtime_market_service,
)


class FakeRealtimeController:
    def __init__(self, candidates: tuple[str, ...] = ("CANDUSDT",)) -> None:
        self.candidate_symbols = candidates
        self.manifest_event_ready = True
        self.polls = 0
        self.mark_prices: list[MarkPriceUpdate] = []
        self.evaluations: list[dict[str, object]] = []

    def poll_manifest(self, *, now_ts: float | None = None) -> dict[str, object]:
        self.polls += 1
        return {"status": "valid_unchanged", "changed": False}

    def handle_mark_price(self, update: MarkPriceUpdate) -> bool:
        self.mark_prices.append(update)
        return True

    def evaluate(
        self,
        subscription_status: dict[str, object],
        *,
        now_ts: float | None = None,
    ) -> list[dict[str, object]]:
        self.evaluations.append(dict(subscription_status))
        return []

    def stats(self) -> dict[str, object]:
        return {
            "manifest_hash": "a" * 64,
            "manifest_snapshot_hash": "b" * 64,
            "manifest_age_sec": 12.5,
            "manifest_event_ready": self.manifest_event_ready,
            "manifest_polls": self.polls,
            "manifest_failures": 0,
            "feature_evaluations": len(self.evaluations),
            "data_quality_skips": 0,
            "data_quality_skip_reasons": {"insufficient_history": 2},
            "features": {"complete": 1, "insufficient_history": 2},
            "events": {},
            "oi": {},
        }


def p2_settings(tmp: str) -> SimpleNamespace:
    return SimpleNamespace(
        realtime_features_db_path=Path(tmp) / "realtime.db",
        realtime_market_bucket_sec=60,
        realtime_market_grace_ms=2_000,
        realtime_market_flush_interval_sec=1,
        realtime_market_reconnect_sec=1,
        realtime_market_connect_timeout_sec=5,
        realtime_market_idle_timeout_sec=30,
        realtime_market_retention_days=3,
        realtime_market_symbol_refresh_sec=300,
        altcoin_contract_anomaly_enable=True,
        altcoin_contract_anomaly_realtime_enable=True,
        altcoin_contract_anomaly_subscription_batch_size=50,
        altcoin_contract_anomaly_subscription_min_interval_sec=0,
        altcoin_contract_anomaly_subscription_ack_timeout_sec=10,
        altcoin_contract_anomaly_manifest_poll_sec=1,
        altcoin_contract_anomaly_max_streams=300,
    )


class BinanceSubscriptionPlanTests(unittest.TestCase):
    def test_candidates_receive_agg_trade_and_mark_price_before_base_capacity(self) -> None:
        plan = build_binance_subscription_plan(
            ["BTCUSDT", "ETHUSDT", "CANDUSDT"],
            ["dogeusdt", "CANDUSDT"],
            max_streams=6,
        )

        self.assertEqual(plan.requested_candidate_symbols, ("CANDUSDT", "DOGEUSDT"))
        self.assertEqual(plan.candidate_symbols, ("CANDUSDT", "DOGEUSDT"))
        self.assertEqual(plan.base_symbols, ("BTCUSDT", "CANDUSDT"))
        self.assertEqual(plan.union_symbols, ("BTCUSDT", "CANDUSDT", "DOGEUSDT"))
        self.assertEqual(
            plan.subscriptions,
            (
                "btcusdt@aggTrade",
                "candusdt@aggTrade",
                "dogeusdt@aggTrade",
                "candusdt@markPrice",
                "dogeusdt@markPrice",
                "!forceOrder@arr",
            ),
        )
        self.assertEqual(plan.omitted_base_symbols, ("ETHUSDT",))
        self.assertFalse(plan.candidate_capacity_degraded)
        self.assertTrue(plan.capacity_degraded)
        self.assertEqual(plan.expected_stream_count, 7)
        self.assertEqual(plan.stats()["actual_stream_count"], 6)

    def test_candidate_capacity_degradation_is_explicit_and_deterministic(self) -> None:
        first = build_binance_subscription_plan(
            ["BTCUSDT"],
            ["ZZZUSDT", "AAAUSDT"],
            max_streams=4,
        )
        second = build_binance_subscription_plan(
            ["BTCUSDT"],
            ["AAAUSDT", "ZZZUSDT", "AAAUSDT"],
            max_streams=4,
        )

        self.assertEqual(first, second)
        self.assertEqual(first.candidate_symbols, ("AAAUSDT",))
        self.assertEqual(first.omitted_candidate_symbols, ("ZZZUSDT",))
        self.assertEqual(first.base_symbols, ("BTCUSDT",))
        self.assertEqual(first.omitted_base_symbols, ())
        self.assertTrue(first.capacity_degraded)
        self.assertTrue(first.candidate_capacity_degraded)

    def test_omitted_candidate_still_competes_in_base_priority_order(self) -> None:
        plan = build_binance_subscription_plan(
            ["ZZZUSDT", "LOWUSDT"],
            ["AAAUSDT", "ZZZUSDT"],
            max_streams=4,
        )

        self.assertEqual(plan.candidate_symbols, ("AAAUSDT",))
        self.assertEqual(plan.omitted_candidate_symbols, ("ZZZUSDT",))
        self.assertEqual(plan.base_symbols, ("ZZZUSDT",))
        self.assertEqual(plan.omitted_base_symbols, ("LOWUSDT",))
        self.assertEqual(
            plan.subscriptions,
            (
                "aaausdt@aggTrade",
                "zzzusdt@aggTrade",
                "aaausdt@markPrice",
                "!forceOrder@arr",
            ),
        )

    def test_base_priority_uses_input_volume_order_and_streams_are_deduplicated(self) -> None:
        plan = build_binance_subscription_plan(
            ["HIGHUSDT", "LOWUSDT", "HIGHUSDT", "BTCUSD", ""],
            ["CANDUSDT", "candusdt", "BTCUSD"],
            max_streams=4,
        )

        self.assertEqual(plan.base_symbols, ("HIGHUSDT",))
        self.assertEqual(plan.omitted_base_symbols, ("LOWUSDT",))
        self.assertEqual(plan.candidate_symbols, ("CANDUSDT",))
        self.assertEqual(plan.subscriptions.count("!forceOrder@arr"), 1)
        self.assertEqual(len(plan.subscriptions), len(set(plan.subscriptions)))

    def test_force_order_is_retained_when_capacity_is_one(self) -> None:
        plan = build_binance_subscription_plan(
            ["BTCUSDT"],
            ["CANDUSDT"],
            max_streams=1,
        )

        self.assertEqual(plan.subscriptions, ("!forceOrder@arr",))
        self.assertEqual(plan.omitted_candidate_symbols, ("CANDUSDT",))
        self.assertEqual(plan.omitted_base_symbols, ("BTCUSDT",))
        self.assertTrue(plan.candidate_capacity_degraded)

    def test_complete_plan_reports_no_capacity_degradation(self) -> None:
        plan = build_binance_subscription_plan(
            ["BTCUSDT", "ETHUSDT"],
            ["ETHUSDT"],
            max_streams=10,
        )

        self.assertEqual(plan.expected_stream_count, 4)
        self.assertEqual(plan.actual_stream_count, 4)
        self.assertFalse(plan.capacity_degraded)
        self.assertFalse(plan.candidate_capacity_degraded)


class MarkPriceUpdateTests(unittest.TestCase):
    @staticmethod
    def payload(
        *,
        symbol: str = "BTCUSDT",
        price: object = "50000.25",
        funding: object = "-0.0001",
        event_time: object = 1_700_000_000_000,
        next_funding_time: object = 1_700_002_800_000,
    ) -> dict[str, object]:
        return {
            "e": "markPriceUpdate",
            "E": event_time,
            "s": symbol,
            "p": price,
            "r": funding,
            "T": next_funding_time,
        }

    def test_parses_direct_and_combined_stream_payloads(self) -> None:
        direct = parse_binance_mark_price_update(self.payload())
        wrapped = parse_binance_mark_price_update({
            "stream": "btcusdt@markPrice",
            "data": self.payload(),
        })

        self.assertEqual(direct, wrapped)
        self.assertIsNotNone(direct)
        self.assertEqual(direct.symbol, "BTCUSDT")
        self.assertEqual(direct.mark_price, 50_000.25)
        self.assertEqual(direct.funding_rate, -0.0001)
        self.assertEqual(direct.source, "binance_ws_mark_price")

    def test_rejects_invalid_price_funding_symbol_and_timestamps(self) -> None:
        invalid = [
            {"e": "aggTrade"},
            self.payload(symbol="BTCUSD"),
            self.payload(price="0"),
            self.payload(price="nan"),
            self.payload(price="inf"),
            self.payload(funding=None),
            self.payload(funding="nan"),
            self.payload(event_time=0),
            self.payload(next_funding_time=0),
        ]

        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertIsNone(parse_binance_mark_price_update(payload))

    def test_book_rejects_duplicates_and_out_of_order_updates(self) -> None:
        book = MarkPriceBook()
        first = parse_binance_mark_price_update(self.payload())
        duplicate = parse_binance_mark_price_update(self.payload())
        stale = parse_binance_mark_price_update(self.payload(
            price="49999",
            event_time=1_699_999_999_999,
        ))

        self.assertTrue(book.update(first))
        self.assertFalse(book.update(duplicate))
        self.assertFalse(book.update(stale))
        self.assertEqual(book.latest("btcusdt"), first)
        self.assertEqual(book.stats()["duplicate_updates"], 1)
        self.assertEqual(book.stats()["out_of_order_updates"], 1)

    def test_funding_change_is_derived_from_a_closed_window_not_the_latest_tick(self) -> None:
        book = MarkPriceBook()
        first = parse_binance_mark_price_update(self.payload())
        changed_rate = parse_binance_mark_price_update(self.payload(
            price="50001",
            funding="-0.00025",
            event_time=1_700_000_120_000,
        ))
        trailing_equal_rate = parse_binance_mark_price_update(self.payload(
            price="50002",
            funding="-0.00025",
            event_time=1_700_000_300_000,
        ))

        self.assertTrue(book.update(first, subscription_epoch="epoch-1"))
        self.assertTrue(book.update(changed_rate, subscription_epoch="epoch-1"))
        self.assertTrue(book.update(trailing_equal_rate, subscription_epoch="epoch-1"))
        row = book.snapshot_window(
            "BTCUSDT",
            window_end_ms=1_700_000_300_000,
            subscription_epoch="epoch-1",
            epoch_started_ms=1_700_000_000_000,
            max_gap_ms=180_000,
        )

        self.assertEqual(row["funding_window_quality"], "complete")
        self.assertAlmostEqual(row["funding_rate_change_5m"], -0.00015)
        self.assertTrue(row["funding_rate_changed_5m"])
        self.assertEqual(
            row["funding_window_start_event_time_ms"],
            first.event_time_ms,
        )

    def test_book_revalidates_manually_constructed_updates(self) -> None:
        book = MarkPriceBook()
        invalid = MarkPriceUpdate(
            symbol="BTCUSDT",
            mark_price=math.nan,
            funding_rate=0.0,
            next_funding_time_ms=2,
            event_time_ms=1,
        )

        self.assertFalse(book.update(None))
        self.assertFalse(book.update(invalid))
        self.assertEqual(book.stats()["invalid_updates"], 2)

    def test_book_is_thread_safe_and_never_allows_older_data_to_win(self) -> None:
        book = MarkPriceBook()
        updates = [
            parse_binance_mark_price_update(self.payload(
                price=str(50_000 + index),
                event_time=1_700_000_000_000 + index,
            ))
            for index in range(100)
        ]
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(book.update, reversed(updates)))

        self.assertEqual(book.latest("BTCUSDT"), updates[-1])

    def test_real_controller_accepts_shared_mark_price_update_contract(self) -> None:
        from radars.altcoin_contract_anomaly.realtime import AltcoinRealtimeController

        controller = object.__new__(AltcoinRealtimeController)
        controller.manifest_consumer = SimpleNamespace(
            last_valid=SimpleNamespace(symbols=("BTCUSDT",)),
        )
        controller.mark_price_book = MarkPriceBook()
        controller._stats = {"mark_price_messages": 0, "mark_price_rejected": 0}
        update = parse_binance_mark_price_update(self.payload())

        self.assertTrue(controller.handle_mark_price(update))
        self.assertEqual(controller.mark_price_book.latest("BTCUSDT"), update)
        self.assertEqual(controller._stats["mark_price_messages"], 1)


class SubscriptionLedgerTests(unittest.TestCase):
    def test_batches_commands_and_updates_active_only_after_success_ack(self) -> None:
        ledger = SubscriptionLedger(batch_size=2, min_interval_sec=0)
        ledger.set_desired(["btcusdt@aggTrade", "ethusdt@aggTrade"])
        generation = ledger.reset_connection()

        first = ledger.next_command(now_monotonic=1)
        self.assertEqual(first.request_id, 1)
        self.assertEqual(first.generation, generation)
        self.assertEqual(first.method, "SUBSCRIBE")
        self.assertEqual(first.streams, ("!forceOrder@arr", "btcusdt@aggTrade"))
        self.assertEqual(ledger.active_subscriptions, frozenset())
        self.assertEqual(first.payload(), {
            "method": "SUBSCRIBE",
            "params": ["!forceOrder@arr", "btcusdt@aggTrade"],
            "id": 1,
        })

        ack = ledger.handle_ack({"result": None, "id": 1})
        self.assertEqual(ack.status, "success")
        self.assertEqual(ledger.active_subscriptions, frozenset(first.streams))
        second = ledger.next_command(now_monotonic=1)
        self.assertEqual(second.request_id, 2)
        self.assertEqual(second.streams, ("ethusdt@aggTrade",))

    def test_minimum_interval_is_enforced_between_command_batches(self) -> None:
        ledger = SubscriptionLedger(batch_size=1, min_interval_sec=1)
        ledger.set_desired(["btcusdt@aggTrade"])

        self.assertIsNotNone(ledger.next_command(now_monotonic=10))
        self.assertIsNone(ledger.next_command(now_monotonic=10.999))
        self.assertIsNotNone(ledger.next_command(now_monotonic=11))

    def test_out_of_order_and_duplicate_acks_are_idempotent(self) -> None:
        ledger = SubscriptionLedger(batch_size=1, min_interval_sec=0)
        ledger.set_desired(["btcusdt@aggTrade"])
        first = ledger.next_command(now_monotonic=1)
        second = ledger.next_command(now_monotonic=1)

        self.assertEqual(ledger.handle_ack({"result": None, "id": second.request_id}).status, "success")
        self.assertEqual(ledger.handle_ack({"result": None, "id": first.request_id}).status, "success")
        self.assertEqual(ledger.handle_ack({"result": None, "id": second.request_id}).status, "duplicate")
        self.assertEqual(ledger.stats()["duplicate_acks"], 1)
        self.assertEqual(ledger.active_subscriptions, ledger.desired_subscriptions)

    def test_failed_ack_does_not_change_active_and_is_retried_with_new_id(self) -> None:
        ledger = SubscriptionLedger(batch_size=10, min_interval_sec=0)
        ledger.set_desired(["btcusdt@aggTrade"])
        command = ledger.next_command(now_monotonic=1)

        ack = ledger.handle_ack({"code": 2, "msg": "bad request", "id": command.request_id})
        self.assertEqual(ack.status, "failure")
        self.assertIn("2:bad request", ack.error)
        self.assertEqual(ledger.active_subscriptions, frozenset())
        retry = ledger.next_command(now_monotonic=1)
        self.assertGreater(retry.request_id, command.request_id)
        self.assertEqual(retry.streams, command.streams)

    def test_unsubscribe_failure_keeps_active_and_force_order_is_protected(self) -> None:
        ledger = SubscriptionLedger(batch_size=10, min_interval_sec=0)
        ledger.set_desired(["btcusdt@aggTrade", "ethusdt@aggTrade"])
        initial = ledger.next_command(now_monotonic=1)
        ledger.handle_ack({"result": None, "id": initial.request_id})
        ledger.set_desired(["btcusdt@aggTrade"])

        remove = ledger.next_command(now_monotonic=2)
        self.assertEqual(remove.method, "UNSUBSCRIBE")
        self.assertEqual(remove.streams, ("ethusdt@aggTrade",))
        ledger.handle_ack({"code": 1, "msg": "temporary", "id": remove.request_id})
        self.assertIn("ethusdt@aggTrade", ledger.active_subscriptions)
        retry = ledger.next_command(now_monotonic=2)
        ledger.handle_ack({"result": None, "id": retry.request_id})
        self.assertNotIn("ethusdt@aggTrade", ledger.active_subscriptions)
        self.assertIn("!forceOrder@arr", ledger.active_subscriptions)
        self.assertNotIn("!forceOrder@arr", ledger.pending_unsubscribe)

    def test_ack_timeout_is_explicit_and_releases_stream_for_retry(self) -> None:
        ledger = SubscriptionLedger(batch_size=10, min_interval_sec=0, ack_timeout_sec=5)
        ledger.set_desired(["btcusdt@aggTrade"])
        command = ledger.next_command(now_monotonic=10)

        self.assertEqual(ledger.expire_timeouts(now_monotonic=14.999), [])
        self.assertEqual(ledger.expire_timeouts(now_monotonic=15), [command])
        self.assertEqual(ledger.stats()["ack_timeouts"], 1)
        retry = ledger.next_command(now_monotonic=15)
        self.assertGreater(retry.request_id, command.request_id)
        self.assertEqual(ledger.handle_ack({"result": None, "id": command.request_id}).status, "duplicate")

    def test_reset_connection_clears_active_and_pending_but_preserves_desired(self) -> None:
        ledger = SubscriptionLedger(batch_size=1, min_interval_sec=0)
        ledger.set_desired(["btcusdt@aggTrade"])
        first = ledger.next_command(now_monotonic=1)
        ledger.handle_ack({"result": None, "id": first.request_id})
        pending = ledger.next_command(now_monotonic=1)
        desired = ledger.desired_subscriptions

        generation = ledger.reset_connection()

        self.assertGreaterEqual(generation, 1)
        self.assertEqual(ledger.desired_subscriptions, desired)
        self.assertEqual(ledger.active_subscriptions, frozenset())
        self.assertEqual(ledger.pending_requests, {})
        rebuilt = ledger.next_command(now_monotonic=1)
        self.assertEqual(rebuilt.generation, generation)
        self.assertGreater(rebuilt.request_id, pending.request_id)

    def test_send_failure_unknown_and_invalid_acks_are_reported(self) -> None:
        ledger = SubscriptionLedger(batch_size=10, min_interval_sec=0)
        command = ledger.next_command(now_monotonic=1)

        failure = ledger.mark_send_failed(command.request_id, RuntimeError("closed"))
        self.assertEqual(failure.status, "failure")
        self.assertIn("RuntimeError", failure.error)
        self.assertEqual(ledger.handle_ack({"result": None, "id": 999}).status, "unknown")
        self.assertEqual(ledger.handle_ack({"result": None, "id": True}).status, "invalid")
        self.assertEqual(ledger.stats()["unknown_acks"], 1)
        self.assertEqual(ledger.stats()["invalid_acks"], 1)

    def test_unchanged_desired_set_does_not_create_more_commands_after_coverage(self) -> None:
        ledger = SubscriptionLedger(batch_size=10, min_interval_sec=0)
        first_delta = ledger.set_desired(["BTCUSDT@aggTrade"])
        command = ledger.next_command(now_monotonic=1)
        ledger.handle_ack({"result": None, "id": command.request_id})
        second_delta = ledger.set_desired(["btcusdt@aggtrade", "!forceOrder@arr"])

        self.assertTrue(first_delta["changed"])
        self.assertFalse(second_delta["changed"])
        self.assertIsNone(ledger.next_command(now_monotonic=2))


class BinanceRealtimeMarketP2ServiceTests(unittest.TestCase):
    def test_p2_disabled_does_not_construct_controller_or_change_legacy_stats(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = p2_settings(tmp)
            settings.altcoin_contract_anomaly_realtime_enable = False
            with patch(
                "radars.altcoin_contract_anomaly.realtime.AltcoinRealtimeController"
            ) as controller_class:
                service = BinanceRealtimeMarketService(settings)

            ws = Mock()
            service._on_open(ws, ["btcusdt@aggTrade", "!forceOrder@arr"])
            payload = json.loads(ws.send.call_args.args[0])

        controller_class.assert_not_called()
        self.assertEqual(payload["method"], "SUBSCRIBE")
        self.assertEqual(payload["params"], ["btcusdt@aggTrade", "!forceOrder@arr"])
        self.assertNotIn("altcoin_contract_anomaly_realtime_enabled", service.stats())

    def test_legacy_service_never_auto_enables_p2_when_both_gates_are_true(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = p2_settings(tmp)
            with patch(
                "radars.altcoin_contract_anomaly.realtime.AltcoinRealtimeController"
            ) as controller_class:
                service = BinanceRealtimeMarketService(settings)

            ws = Mock()
            service._on_open(ws, ["btcusdt@aggTrade", "!forceOrder@arr"])
            payload = json.loads(ws.send.call_args.args[0])

        controller_class.assert_not_called()
        self.assertFalse(service._p2_enabled)
        self.assertIsNone(service._p2_controller)
        self.assertEqual(payload["params"], ["btcusdt@aggTrade", "!forceOrder@arr"])
        self.assertNotIn("altcoin_contract_anomaly_realtime_enabled", service.stats())

    def test_unbounded_market_stream_runner_stays_legacy_with_both_gates_true(self) -> None:
        observed_services: list[BinanceRealtimeMarketService] = []

        def stop_immediately(
            service: BinanceRealtimeMarketService,
            stop: threading.Event,
        ) -> None:
            observed_services.append(service)
            stop.set()

        with TemporaryDirectory() as tmp:
            settings = p2_settings(tmp)
            with patch.object(
                BinanceRealtimeMarketService,
                "run",
                autospec=True,
                side_effect=stop_immediately,
            ), patch(
                "radars.altcoin_contract_anomaly.realtime.AltcoinRealtimeController"
            ) as controller_class:
                code = run_realtime_market_service(settings, duration_sec=0)

        self.assertEqual(code, 0)
        controller_class.assert_not_called()
        self.assertEqual(len(observed_services), 1)
        self.assertFalse(observed_services[0]._p2_enabled)
        self.assertIsNone(observed_services[0]._p2_controller)

    def test_explicit_controller_requires_both_gates_before_state_initialization(self) -> None:
        for disabled_gate in (
            "altcoin_contract_anomaly_enable",
            "altcoin_contract_anomaly_realtime_enable",
        ):
            with self.subTest(disabled_gate=disabled_gate), TemporaryDirectory() as tmp:
                settings = p2_settings(tmp)
                setattr(settings, disabled_gate, False)
                controller = FakeRealtimeController()
                websocket_factory = Mock()
                with patch("shared.realtime_market.RealtimeFeatureStore") as store_type:
                    with self.assertRaisesRegex(ValueError, "explicit P2 controller"):
                        BinanceRealtimeMarketService(
                            settings,
                            realtime_controller=controller,
                            websocket_app_factory=websocket_factory,
                        )

                store_type.assert_not_called()
                websocket_factory.assert_not_called()

    def test_p2_open_uses_ack_driven_plan_on_the_existing_socket(self) -> None:
        with TemporaryDirectory() as tmp:
            controller = FakeRealtimeController(("CANDUSDT",))
            service = BinanceRealtimeMarketService(
                p2_settings(tmp),
                realtime_controller=controller,
            )
            service._apply_p2_subscription_plan(["BTCUSDT"])
            ws = Mock()

            service._on_open(ws, ["ignored@aggTrade"])
            self.assertGreater(service._p2_last_market_receive_mono, 0)
            self.assertEqual(service._p2_last_market_receive_ms, 0)
            command = json.loads(ws.send.call_args.args[0])
            self.assertEqual(service._p2_ledger.active_subscriptions, frozenset())
            service._on_message(ws, json.dumps({"result": None, "id": command["id"]}))

            status = service._p2_subscription_status()

        self.assertEqual(service.open_count, 1)
        self.assertEqual(set(command["params"]), {
            "btcusdt@aggTrade",
            "candusdt@aggTrade",
            "candusdt@markPrice",
            "!forceOrder@arr",
        })
        self.assertTrue(status["candidate_coverage_complete"])
        self.assertTrue(status["force_order_active"])
        self.assertEqual(service.subscription_acks, 1)

    def test_manifest_change_sends_delta_without_closing_or_replacing_socket(self) -> None:
        with TemporaryDirectory() as tmp:
            controller = FakeRealtimeController(("AAAUSDT",))
            service = BinanceRealtimeMarketService(
                p2_settings(tmp),
                realtime_controller=controller,
            )
            service._apply_p2_subscription_plan(["BTCUSDT"])
            ws = Mock()
            service._on_open(ws, [])
            initial = json.loads(ws.send.call_args.args[0])
            service._on_message(ws, json.dumps({"result": None, "id": initial["id"]}))

            controller.candidate_symbols = ("BBBUSDT",)
            now_mono = time.monotonic() + 1
            service._poll_p2_manifest(now_ts=100, now_monotonic=now_mono, force=True)
            unsubscribe = service._send_next_p2_control(ws, now_monotonic=now_mono)
            service._on_message(ws, json.dumps({"result": None, "id": unsubscribe.request_id}))
            subscribe = service._send_next_p2_control(ws, now_monotonic=now_mono)
            service._on_message(ws, json.dumps({"result": None, "id": subscribe.request_id}))

            status = service._p2_subscription_status()

        self.assertEqual(unsubscribe.method, "UNSUBSCRIBE")
        self.assertEqual(set(unsubscribe.streams), {"aaausdt@aggTrade", "aaausdt@markPrice"})
        self.assertEqual(subscribe.method, "SUBSCRIBE")
        self.assertEqual(set(subscribe.streams), {"bbbusdt@aggTrade", "bbbusdt@markPrice"})
        self.assertEqual(service.open_count, 1)
        ws.close.assert_not_called()
        self.assertTrue(status["candidate_coverage_complete"])
        self.assertEqual(status["active_candidate_symbols"], ["BBBUSDT"])

    def test_service_reports_overall_and_candidate_capacity_separately(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = p2_settings(tmp)
            settings.altcoin_contract_anomaly_max_streams = 4
            service = BinanceRealtimeMarketService(
                settings,
                realtime_controller=FakeRealtimeController(("CANDUSDT",)),
            )
            plan = service._apply_p2_subscription_plan(["HIGHUSDT", "LOWUSDT"])

            status = service._p2_subscription_status()
            stats = service.stats()

        self.assertTrue(plan.capacity_degraded)
        self.assertFalse(plan.candidate_capacity_degraded)
        self.assertTrue(status["capacity_degraded"])
        self.assertFalse(status["candidate_capacity_degraded"])
        self.assertTrue(stats["base_capacity_trimmed"])

    def test_mark_price_and_market_data_freshness_exclude_ack_and_pong(self) -> None:
        with TemporaryDirectory() as tmp:
            controller = FakeRealtimeController(("CANDUSDT",))
            service = BinanceRealtimeMarketService(
                p2_settings(tmp),
                realtime_controller=controller,
            )
            service._apply_p2_subscription_plan(["BTCUSDT"])
            ws = Mock()
            service._on_open(ws, [])
            initial = json.loads(ws.send.call_args.args[0])
            service._on_message(ws, json.dumps({"result": None, "id": initial["id"]}))
            event_time = int(time.time() * 1000)
            service._on_message(ws, json.dumps({
                "e": "markPriceUpdate",
                "E": event_time,
                "s": "CANDUSDT",
                "p": "2.5",
                "r": "-0.0002",
                "T": event_time + 3_600_000,
            }))
            market_time = service._p2_last_market_receive_ms

            service._on_message(ws, "pong")
            service._on_message(ws, json.dumps({"result": None, "id": initial["id"]}))
            service._on_message(ws, json.dumps({
                "e": "markPriceUpdate",
                "E": event_time - 1,
                "s": "CANDUSDT",
                "p": "2.4",
                "r": "-0.0003",
                "T": event_time + 3_600_000,
            }))
            self.assertEqual(service._p2_last_market_receive_ms, market_time)
            service._on_message(ws, json.dumps({
                "e": "aggTrade",
                "E": event_time + 1,
                "s": "CANDUSDT",
                "a": 9,
                "p": "2.6",
                "q": "10",
                "T": event_time + 1,
                "m": False,
            }))

            stats = service.stats()

        self.assertEqual(len(controller.mark_prices), 1)
        self.assertIsInstance(controller.mark_prices[0], MarkPriceUpdate)
        self.assertEqual(controller.mark_prices[0].symbol, "CANDUSDT")
        self.assertEqual(stats["mark_price_messages"], 2)
        self.assertEqual(stats["mark_price_rejected"], 1)
        self.assertEqual(stats["agg_trade_messages"], 1)
        self.assertEqual(stats["force_order_subscription_count"], 1)
        self.assertEqual(stats["manifest_age_sec"], 12.5)
        self.assertEqual(stats["feature_coverage"]["complete"], 1)
        self.assertEqual(
            stats["data_quality_skip_reasons"]["insufficient_history"],
            2,
        )
        self.assertEqual(stats["mark_price_data_symbol_count"], 1)
        self.assertEqual(stats["mark_price_data_coverage_ratio"], 1.0)
        self.assertGreater(stats["last_market_receive_ms"], market_time)

    def test_run_uses_one_websocket_while_manifest_changes_in_connection(self) -> None:
        class ChangingController(FakeRealtimeController):
            def __init__(self) -> None:
                super().__init__(())

            def poll_manifest(self, *, now_ts: float | None = None) -> dict[str, object]:
                self.polls += 1
                if self.polls == 1:
                    self.candidate_symbols = ("AAAUSDT",)
                    return {"status": "valid_changed", "changed": True}
                if self.polls == 2:
                    self.candidate_symbols = ("BBBUSDT",)
                    return {"status": "valid_changed", "changed": True}
                return {"status": "valid_unchanged", "changed": False}

        class FakeWebSocket:
            def __init__(self, _url: str, **callbacks: object) -> None:
                self.callbacks = callbacks
                self.closed = threading.Event()
                self.sent: list[dict[str, object]] = []
                self.trade_id = 0

            def send(self, message: str) -> None:
                payload = json.loads(message)
                self.sent.append(payload)
                if "id" in payload:
                    self.callbacks["on_message"](
                        self,
                        json.dumps({"result": None, "id": payload["id"]}),
                    )

            def run_forever(self, **_kwargs: object) -> None:
                self.callbacks["on_open"](self)
                while not self.closed.wait(0.05):
                    self.trade_id += 1
                    now_ms = int(time.time() * 1000)
                    self.callbacks["on_message"](self, json.dumps({
                        "e": "aggTrade",
                        "E": now_ms,
                        "s": "BTCUSDT",
                        "a": self.trade_id,
                        "p": "50000",
                        "q": "0.001",
                        "T": now_ms,
                        "m": False,
                    }))

            def close(self) -> None:
                self.closed.set()

        with TemporaryDirectory() as tmp:
            controller = ChangingController()
            created: list[FakeWebSocket] = []

            def factory(url: str, **callbacks: object) -> FakeWebSocket:
                instance = FakeWebSocket(url, **callbacks)
                created.append(instance)
                return instance

            service = BinanceRealtimeMarketService(
                p2_settings(tmp),
                realtime_controller=controller,
                websocket_app_factory=factory,
            )
            service._load_connection = Mock(return_value=(
                ["BTCUSDT"],
                ["btcusdt@aggTrade", "!forceOrder@arr"],
                {},
            ))
            stop = threading.Event()
            # Allow three one-second service ticks so slower CI workers still
            # observe both halves of the unsubscribe/subscribe delta.
            timer = threading.Timer(3.4, stop.set)
            timer.start()
            try:
                service.run(stop)
            finally:
                timer.cancel()

            methods = [str(payload.get("method")) for payload in created[0].sent]
            stats = service.stats()

        self.assertEqual(len(created), 1)
        self.assertEqual(service.connection_attempts, 1)
        self.assertEqual(service.open_count, 1)
        self.assertIn("UNSUBSCRIBE", methods)
        self.assertGreaterEqual(methods.count("SUBSCRIBE"), 2)
        self.assertGreaterEqual(controller.polls, 2)
        self.assertGreater(stats["agg_trade_messages"], 0)
        self.assertNotEqual(stats["last_error"], "unexpected_disconnect")

    def test_stuck_websocket_runner_is_an_explicit_shutdown_failure(self) -> None:
        release = threading.Event()

        class StuckWebSocket:
            def __init__(self, _url: str, **callbacks: object) -> None:
                self.callbacks = callbacks
                self.close_calls = 0

            def send(self, message: str) -> None:
                payload = json.loads(message)
                if "id" in payload:
                    self.callbacks["on_message"](
                        self,
                        json.dumps({"result": None, "id": payload["id"]}),
                    )

            def run_forever(self, **_kwargs: object) -> None:
                self.callbacks["on_open"](self)
                release.wait()

            def close(self) -> None:
                self.close_calls += 1

        with TemporaryDirectory() as tmp:
            controller = FakeRealtimeController(("CANDUSDT",))
            created: list[StuckWebSocket] = []

            def factory(url: str, **callbacks: object) -> StuckWebSocket:
                instance = StuckWebSocket(url, **callbacks)
                created.append(instance)
                return instance

            service = BinanceRealtimeMarketService(
                p2_settings(tmp),
                websocket_app_factory=factory,
                realtime_controller=controller,
            )
            service._load_connection = Mock(return_value=(
                ["BTCUSDT"],
                ["btcusdt@aggTrade", "!forceOrder@arr"],
                {},
            ))
            stop = threading.Event()
            timer = threading.Timer(0.1, stop.set)
            timer.start()
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "websocket_runner_shutdown_timeout",
                ):
                    service.run(stop)
            finally:
                timer.cancel()
                release.set()

        self.assertEqual(len(created), 1)
        self.assertGreaterEqual(created[0].close_calls, 1)
        self.assertEqual(service.stats()["runner_shutdown_timeouts"], 1)

    def test_bounded_session_is_silent_and_returns_stats_and_events(self) -> None:
        class WaitingService:
            @staticmethod
            def run(stop: threading.Event) -> None:
                stop.wait()

            @staticmethod
            def stats() -> dict[str, object]:
                return {"service": "test", "agg_trade_messages": 3}

            @staticmethod
            def recent_events() -> list[dict[str, object]]:
                return [{"event_id": "event-1", "dry_run": True}]

        with patch("builtins.print") as printer:
            result = run_realtime_market_session(
                SimpleNamespace(),
                duration_sec=0.02,
                service=WaitingService(),
            )

        printer.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["stats"]["agg_trade_messages"], 3)
        self.assertEqual(result["events"][0]["event_id"], "event-1")
        self.assertGreaterEqual(result["duration_sec_actual"], 0.01)

    def test_bounded_session_reports_live_end_health_before_intentional_shutdown(self) -> None:
        class CoverageService:
            def __init__(self) -> None:
                self.stop: threading.Event | None = None

            def run(self, stop: threading.Event) -> None:
                self.stop = stop
                stop.wait()

            def stats(self) -> dict[str, object]:
                return {
                    "candidate_coverage_complete": bool(
                        self.stop is not None and not self.stop.is_set()
                    ),
                    "feature_evaluations": int(
                        self.stop is not None and self.stop.is_set()
                    ),
                }

            @staticmethod
            def recent_events() -> list[dict[str, object]]:
                return []

        result = run_realtime_market_session(
            SimpleNamespace(),
            duration_sec=0.02,
            service=CoverageService(),
        )

        self.assertTrue(result["stats"]["candidate_coverage_complete"])
        self.assertTrue(result["stats"]["candidate_coverage_complete_at_stop"])
        self.assertEqual(result["stats"]["feature_evaluations"], 1)

    def test_bounded_session_sanitizes_worker_failure_and_rejects_bad_duration(self) -> None:
        class FailingService:
            @staticmethod
            def run(_stop: threading.Event) -> None:
                raise RuntimeError("sensitive upstream body")

            @staticmethod
            def stats() -> dict[str, object]:
                return {}

            @staticmethod
            def recent_events() -> list[dict[str, object]]:
                return []

        result = run_realtime_market_session(
            SimpleNamespace(),
            duration_sec=1,
            service=FailingService(),
        )

        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"], ["binance_realtime_market:RuntimeError"])
        self.assertNotIn("sensitive", json.dumps(result))
        with self.assertRaises(ValueError):
            run_realtime_market_session(SimpleNamespace(), duration_sec=0)


if __name__ == "__main__":
    unittest.main()
