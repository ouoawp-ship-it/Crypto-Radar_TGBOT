from __future__ import annotations

import json
import unittest
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shared.realtime_market import (
    BinanceRealtimeMarketService,
    MarkPriceBook,
    MarkPriceUpdate,
)


BASE_MS = 1_800_000_000_000


def update(
    event_offset_sec: int,
    funding_rate: float,
    *,
    symbol: str = "TESTUSDT",
) -> MarkPriceUpdate:
    return MarkPriceUpdate(
        symbol=symbol,
        mark_price=1.0,
        funding_rate=funding_rate,
        next_funding_time_ms=BASE_MS + 3_600_000,
        event_time_ms=BASE_MS + event_offset_sec * 1_000,
    )


def fill_window(
    book: MarkPriceBook,
    *,
    start_sec: int,
    end_sec: int,
    epoch: str,
    rate_at: Callable[[int], float],
    step_sec: int = 10,
) -> None:
    for offset in range(start_sec, end_sec + 1, step_sec):
        accepted = book.update(
            update(offset, float(rate_at(offset))),
            subscription_epoch=epoch,
        )
        if not accepted:
            raise AssertionError(f"test fixture update rejected at {offset=}")


class ClosedFundingWindowTests(unittest.TestCase):
    def test_history_is_bounded_per_symbol(self) -> None:
        book = MarkPriceBook(max_history_per_symbol=16)
        for offset in range(20):
            self.assertTrue(
                book.update(
                    update(offset, -0.0001),
                    subscription_epoch="epoch-1",
                )
            )

        self.assertEqual(book.stats()["history_entry_count"], 16)

    def test_change_survives_repeated_equal_ticks_until_closed_window_evaluation(self) -> None:
        book = MarkPriceBook()
        fill_window(
            book,
            start_sec=0,
            end_sec=300,
            epoch="epoch-1",
            rate_at=lambda offset: -0.0001 if offset < 120 else -0.0003,
        )

        row = book.snapshot_window(
            "TESTUSDT",
            window_end_ms=BASE_MS + 300_000,
            window_sec=300,
            subscription_epoch="epoch-1",
            epoch_started_ms=BASE_MS,
        )

        self.assertEqual(row["funding_window_quality"], "complete")
        self.assertAlmostEqual(row["funding_rate_start_5m"], -0.0001)
        self.assertAlmostEqual(row["funding_rate_end_5m"], -0.0003)
        self.assertAlmostEqual(row["funding_rate_change_5m"], -0.0002)
        self.assertTrue(row["funding_rate_changed_5m"])
        self.assertEqual(row["funding_window_start_event_time_ms"], BASE_MS)
        self.assertEqual(row["funding_window_end_event_time_ms"], BASE_MS + 300_000)

    def test_gap_limit_equal_to_window_duration_cannot_make_two_endpoints_complete(self) -> None:
        book = MarkPriceBook()
        for offset in (0, 300):
            self.assertTrue(
                book.update(
                    update(offset, -0.0001 if offset == 0 else -0.0003),
                    subscription_epoch="epoch-1",
                )
            )

        row = book.snapshot_window(
            "TESTUSDT",
            window_end_ms=BASE_MS + 300_000,
            window_sec=300,
            subscription_epoch="epoch-1",
            epoch_started_ms=BASE_MS,
            max_gap_ms=300_000,
        )

        self.assertEqual(row["funding_window_quality"], "stale")
        self.assertIsNone(row["funding_rate_change_5m"])

    def test_next_complete_unchanged_window_returns_zero_then_later_increase_is_positive(self) -> None:
        book = MarkPriceBook()
        fill_window(
            book,
            start_sec=0,
            end_sec=600,
            epoch="epoch-1",
            rate_at=lambda offset: -0.0001 if offset < 120 else -0.0003,
        )
        second = book.snapshot_window(
            "TESTUSDT",
            window_end_ms=BASE_MS + 600_000,
            subscription_epoch="epoch-1",
            epoch_started_ms=BASE_MS,
        )
        self.assertEqual(second["funding_window_quality"], "complete")
        self.assertEqual(second["funding_rate_change_5m"], 0.0)
        self.assertFalse(second["funding_rate_changed_5m"])

        fill_window(
            book,
            start_sec=610,
            end_sec=900,
            epoch="epoch-1",
            rate_at=lambda offset: -0.0003 if offset < 780 else -0.00005,
        )
        third = book.snapshot_window(
            "TESTUSDT",
            window_end_ms=BASE_MS + 900_000,
            subscription_epoch="epoch-1",
            epoch_started_ms=BASE_MS,
        )

        self.assertEqual(third["funding_window_quality"], "complete")
        self.assertAlmostEqual(third["funding_rate_change_5m"], 0.00025)
        self.assertTrue(third["funding_rate_changed_5m"])

    def test_exact_window_boundaries_are_accepted_but_stream_gap_is_incomplete(self) -> None:
        complete = MarkPriceBook()
        fill_window(
            complete,
            start_sec=0,
            end_sec=300,
            epoch="epoch-1",
            rate_at=lambda offset: -0.0001 - offset / 1_000_000_000,
            step_sec=15,
        )
        exact = complete.snapshot_window(
            "TESTUSDT",
            window_end_ms=BASE_MS + 300_000,
            subscription_epoch="epoch-1",
            epoch_started_ms=BASE_MS,
            max_gap_ms=15_000,
        )
        self.assertEqual(exact["funding_window_quality"], "complete")

        gapped = MarkPriceBook()
        for offset in (0, 10, 20, 280, 290, 300):
            self.assertTrue(
                gapped.update(
                    update(offset, -0.0001),
                    subscription_epoch="epoch-1",
                )
            )
        incomplete = gapped.snapshot_window(
            "TESTUSDT",
            window_end_ms=BASE_MS + 300_000,
            subscription_epoch="epoch-1",
            epoch_started_ms=BASE_MS,
            max_gap_ms=15_000,
        )

        self.assertEqual(incomplete["funding_window_quality"], "stale")
        self.assertIsNone(incomplete["funding_rate_change_5m"])

    def test_old_epoch_history_cannot_supply_either_funding_endpoint(self) -> None:
        book = MarkPriceBook()
        fill_window(
            book,
            start_sec=0,
            end_sec=300,
            epoch="epoch-1",
            rate_at=lambda _offset: -0.0001,
        )
        self.assertTrue(
            book.update(
                update(301, -0.0003),
                subscription_epoch="epoch-2",
            )
        )

        incomplete = book.snapshot_window(
            "TESTUSDT",
            window_end_ms=BASE_MS + 300_000,
            subscription_epoch="epoch-2",
            epoch_started_ms=BASE_MS + 301_000,
        )
        self.assertEqual(incomplete["funding_window_quality"], "insufficient_history")
        self.assertIsNone(incomplete["funding_rate_change_5m"])

        fill_window(
            book,
            start_sec=311,
            end_sec=601,
            epoch="epoch-2",
            rate_at=lambda offset: -0.0003 if offset < 500 else -0.0002,
        )
        current = book.snapshot_window(
            "TESTUSDT",
            window_end_ms=BASE_MS + 601_000,
            subscription_epoch="epoch-2",
            epoch_started_ms=BASE_MS + 301_000,
        )

        self.assertEqual(current["funding_window_quality"], "complete")
        self.assertEqual(current["subscription_epoch"], "epoch-2")
        self.assertGreaterEqual(
            current["funding_window_start_event_time_ms"],
            BASE_MS + 301_000,
        )
        self.assertAlmostEqual(current["funding_rate_change_5m"], 0.0001)


class FakeController:
    def __init__(self) -> None:
        self.candidate_symbols = ("CANDUSDT",)
        self.manifest_event_ready = True
        self.mark_price_book = MarkPriceBook()

    def handle_mark_price(
        self,
        value: MarkPriceUpdate,
        *,
        subscription_epoch: str = "",
    ) -> bool:
        return self.mark_price_book.update(
            value,
            subscription_epoch=subscription_epoch,
        )

    def poll_manifest(self, *, now_ts: float | None = None) -> dict[str, object]:
        return {"status": "valid_unchanged", "changed": False}

    def evaluate(
        self,
        _subscription_status: dict[str, object],
        *,
        now_ts: float | None = None,
    ) -> list[dict[str, object]]:
        return []

    def stats(self) -> dict[str, object]:
        return {"manifest_event_ready": True}


def service_settings(root: Path) -> SimpleNamespace:
    return SimpleNamespace(
        realtime_features_db_path=root / "realtime.db",
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


def ack_last_command(
    service: BinanceRealtimeMarketService,
    ws: Mock,
    *,
    now_sec: float,
) -> dict[str, object]:
    payload = json.loads(ws.send.call_args.args[0])
    with patch("shared.realtime_market.time.time", return_value=now_sec):
        service._on_message(ws, json.dumps({"result": None, "id": payload["id"]}))
    return payload


class CandidateSubscriptionEpochTests(unittest.TestCase):
    def make_service(
        self,
        root: Path,
    ) -> tuple[BinanceRealtimeMarketService, FakeController, Mock]:
        controller = FakeController()
        service = BinanceRealtimeMarketService(
            service_settings(root),
            realtime_controller=controller,
        )
        service._apply_p2_subscription_plan(["BTCUSDT"])
        ws = Mock()
        with patch("shared.realtime_market.time.time", return_value=1_800_000_000):
            service._on_open(ws, [])
        ack_last_command(service, ws, now_sec=1_800_000_000)
        return service, controller, ws

    def test_remove_then_readd_requires_a_new_ack_and_creates_a_new_epoch(self) -> None:
        with TemporaryDirectory() as tmp:
            service, controller, ws = self.make_service(Path(tmp))
            first = service._p2_subscription_status()["candidate_epochs"]["CANDUSDT"]

            controller.candidate_symbols = ()
            with patch("shared.realtime_market.time.time", return_value=1_800_000_001):
                service._apply_p2_subscription_plan(["BTCUSDT"])
            remove = service._send_next_p2_control(ws)
            self.assertEqual(remove.method, "UNSUBSCRIBE")
            service._on_message(ws, json.dumps({"result": None, "id": remove.request_id}))
            self.assertEqual(service._p2_subscription_status()["candidate_epochs"], {})

            controller.candidate_symbols = ("CANDUSDT",)
            with patch("shared.realtime_market.time.time", return_value=1_800_000_002):
                service._apply_p2_subscription_plan(["BTCUSDT"])
            self.assertFalse(service._p2_subscription_status()["candidate_coverage_complete"])
            subscribe = service._send_next_p2_control(ws)
            self.assertEqual(subscribe.method, "SUBSCRIBE")
            with patch("shared.realtime_market.time.time", return_value=1_800_000_002):
                service._on_message(
                    ws,
                    json.dumps({"result": None, "id": subscribe.request_id}),
                )
            second = service._p2_subscription_status()["candidate_epochs"]["CANDUSDT"]

        self.assertNotEqual(first["epoch_id"], second["epoch_id"])
        self.assertGreater(second["subscription_generation"], first["subscription_generation"])
        self.assertEqual(second["last_agg_trade_event_ms"], 0)
        self.assertEqual(second["last_mark_price_event_ms"], 0)

    def test_reconnect_clears_epoch_and_old_ack_cannot_restore_coverage(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = FakeController()
            service = BinanceRealtimeMarketService(
                service_settings(root),
                realtime_controller=controller,
            )
            service._apply_p2_subscription_plan(["BTCUSDT"])
            first_ws = Mock()
            with patch("shared.realtime_market.time.time", return_value=1_800_000_000):
                service._on_open(first_ws, [])
            old_command = json.loads(first_ws.send.call_args.args[0])

            second_ws = Mock()
            with patch("shared.realtime_market.time.time", return_value=1_800_000_010):
                service._on_open(second_ws, [])
            new_command = json.loads(second_ws.send.call_args.args[0])
            self.assertNotEqual(old_command["id"], new_command["id"])
            self.assertEqual(service._p2_subscription_status()["candidate_epochs"], {})

            service._on_message(
                second_ws,
                json.dumps({"result": None, "id": old_command["id"]}),
            )
            self.assertEqual(service._p2_subscription_status()["candidate_epochs"], {})
            self.assertFalse(service._p2_subscription_status()["candidate_coverage_complete"])

            with patch("shared.realtime_market.time.time", return_value=1_800_000_010):
                service._on_message(
                    second_ws,
                    json.dumps({"result": None, "id": new_command["id"]}),
                )
            epoch = service._p2_subscription_status()["candidate_epochs"]["CANDUSDT"]

        self.assertIn(":", epoch["epoch_id"])
        self.assertEqual(epoch["last_agg_trade_event_ms"], 0)
        self.assertEqual(epoch["last_mark_price_event_ms"], 0)
        self.assertEqual(service.stats()["mark_price_data_coverage_ratio"], 0.0)

    def test_stale_unsubscribe_ack_after_readd_retires_epoch_until_reconfirmed(self) -> None:
        with TemporaryDirectory() as tmp:
            service, controller, ws = self.make_service(Path(tmp))

            controller.candidate_symbols = ()
            service._apply_p2_subscription_plan(["BTCUSDT"])
            old_unsubscribe = service._send_next_p2_control(ws)
            self.assertEqual(old_unsubscribe.method, "UNSUBSCRIBE")

            controller.candidate_symbols = ("CANDUSDT",)
            service._apply_p2_subscription_plan(["BTCUSDT"])
            new_subscribe = service._send_next_p2_control(ws)
            self.assertEqual(new_subscribe.method, "SUBSCRIBE")
            with patch("shared.realtime_market.time.time", return_value=1_800_000_010):
                service._on_message(
                    ws,
                    json.dumps({"result": None, "id": new_subscribe.request_id}),
                )
            self.assertTrue(
                service._p2_subscription_status()["candidate_coverage_complete"]
            )

            with patch("shared.realtime_market.time.time", return_value=1_800_000_011):
                service._on_message(
                    ws,
                    json.dumps({"result": None, "id": old_unsubscribe.request_id}),
                )
            degraded = service._p2_subscription_status()
            self.assertFalse(degraded["candidate_coverage_complete"])
            self.assertEqual(degraded["candidate_epochs"], {})

            reconcile = service._send_next_p2_control(ws)
            self.assertEqual(reconcile.method, "SUBSCRIBE")
            with patch("shared.realtime_market.time.time", return_value=1_800_000_012):
                service._on_message(
                    ws,
                    json.dumps({"result": None, "id": reconcile.request_id}),
                )
            recovered = service._p2_subscription_status()

        self.assertTrue(recovered["candidate_coverage_complete"])
        self.assertIn("CANDUSDT", recovered["candidate_epochs"])

    def test_other_symbol_data_never_satisfies_candidate_epoch_freshness(self) -> None:
        with TemporaryDirectory() as tmp:
            service, _controller, ws = self.make_service(Path(tmp))
            epoch = service._p2_subscription_status()["candidate_epochs"]["CANDUSDT"]
            other_event_ms = int(epoch["activated_at_ms"]) + 10_000
            service._on_message(ws, json.dumps({
                "e": "aggTrade",
                "E": other_event_ms,
                "s": "BTCUSDT",
                "a": 1,
                "p": "50000",
                "q": "0.001",
                "T": other_event_ms,
                "m": False,
            }))
            current = service._p2_subscription_status()["candidate_epochs"]["CANDUSDT"]

        self.assertGreaterEqual(service._p2_last_market_receive_ms, other_event_ms)
        self.assertEqual(current["last_agg_trade_event_ms"], 0)
        self.assertEqual(current["last_mark_price_event_ms"], 0)
        self.assertEqual(service.stats()["mark_price_data_coverage_ratio"], 0.0)

    def test_current_epoch_candidate_data_updates_only_current_coverage(self) -> None:
        with TemporaryDirectory() as tmp:
            service, controller, ws = self.make_service(Path(tmp))
            epoch = service._p2_subscription_status()["candidate_epochs"]["CANDUSDT"]
            event_ms = int(epoch["activated_at_ms"]) + 10_000
            service._on_message(ws, json.dumps({
                "e": "markPriceUpdate",
                "E": event_ms,
                "s": "CANDUSDT",
                "p": "2.5",
                "r": "-0.0002",
                "T": event_ms + 3_600_000,
            }))
            service._on_message(ws, json.dumps({
                "e": "aggTrade",
                "E": event_ms + 1,
                "s": "CANDUSDT",
                "a": 1,
                "p": "2.5",
                "q": "10",
                "T": event_ms + 1,
                "m": False,
            }))
            status = service._p2_subscription_status()
            current = status["candidate_epochs"]["CANDUSDT"]

        self.assertEqual(current["last_mark_price_event_ms"], event_ms)
        self.assertEqual(current["last_agg_trade_event_ms"], event_ms + 1)
        self.assertEqual(
            controller.mark_price_book.snapshot("CANDUSDT")["subscription_epoch"],
            current["epoch_id"],
        )
        self.assertEqual(service.stats()["mark_price_data_coverage_ratio"], 1.0)


if __name__ == "__main__":
    unittest.main()
