from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from shared.process_lock import ProcessFileLock, ProcessLockError
from shared.realtime_market import (
    BinanceRealtimeMarketService,
    run_realtime_market_service,
    run_realtime_market_session,
)


class FakeController:
    candidate_symbols = ()
    manifest_event_ready = True

    def __init__(self, events: list[dict[str, object]] | None = None):
        self.events = list(events or [])

    def evaluate(self, _status: object, *, now_ts: float) -> list[dict[str, object]]:
        del now_ts
        return list(self.events)

    @staticmethod
    def stats() -> dict[str, object]:
        return {"manifest_event_ready": True}


class RecordingSink:
    def __init__(self, *, submit_error: Exception | None = None):
        self.submit_error = submit_error
        self.started = 0
        self.stopped = 0
        self.batches: list[list[dict[str, object]]] = []

    def start(self) -> None:
        self.started += 1

    def submit(self, events: list[dict[str, object]]) -> bool:
        if self.submit_error is not None:
            raise self.submit_error
        self.batches.append(events)
        return True

    def stop(self) -> None:
        self.stopped += 1

    def stats(self) -> dict[str, object]:
        return {"queued_batches": len(self.batches)}


class StartFailingSink(RecordingSink):
    def start(self) -> None:
        raise RuntimeError("sensitive startup detail")


def realtime_settings(tmp: str) -> SimpleNamespace:
    return SimpleNamespace(
        realtime_features_db_path=Path(tmp) / "realtime.db",
        realtime_market_bucket_sec=60,
        realtime_market_grace_ms=2_000,
        altcoin_contract_anomaly_enable=True,
        altcoin_contract_anomaly_realtime_enable=True,
        altcoin_contract_anomaly_subscription_batch_size=50,
        altcoin_contract_anomaly_subscription_min_interval_sec=0,
        altcoin_contract_anomaly_subscription_ack_timeout_sec=10,
        altcoin_contract_anomaly_max_streams=100,
    )


class ProcessFileLockTests(unittest.TestCase):
    def test_same_lock_path_is_mutually_exclusive_and_reusable(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "market-stream.lock"
            market_stream = ProcessFileLock(path)
            p2_session = ProcessFileLock(path)

            self.assertTrue(market_stream.acquire())
            self.assertFalse(p2_session.acquire())
            market_stream.release()
            self.assertTrue(p2_session.acquire())
            p2_session.release()

    def test_context_manager_fails_without_blocking_when_lock_is_busy(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "market-stream.lock"
            owner = ProcessFileLock(path)
            contender = ProcessFileLock(path)
            self.assertTrue(owner.acquire())

            with self.assertRaises(ProcessLockError):
                with contender:
                    self.fail("busy lock must not enter the protected section")

            owner.release()

    def test_busy_lock_rejects_market_stream_before_service_or_websocket_factory(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "market-stream.lock"
            owner = ProcessFileLock(path)
            self.assertTrue(owner.acquire())
            try:
                with patch(
                    "shared.realtime_market.BinanceRealtimeMarketService"
                ) as service_type:
                    result = run_realtime_market_service(
                        SimpleNamespace(),
                        process_lock=ProcessFileLock(path),
                    )
            finally:
                owner.release()

        self.assertEqual(result, 1)
        service_type.assert_not_called()

    def test_market_stream_lock_blocks_p2_bounded_session_before_run(self) -> None:
        class UnusedService:
            run = Mock()

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "market-stream.lock"
            owner = ProcessFileLock(path)
            self.assertTrue(owner.acquire())
            try:
                service = UnusedService()
                result = run_realtime_market_session(
                    SimpleNamespace(),
                    duration_sec=30,
                    service=service,
                    process_lock=ProcessFileLock(path),
                )
            finally:
                owner.release()

        self.assertFalse(result["ok"])
        self.assertEqual(result["failures"], ["realtime_process_lock:busy"])
        service.run.assert_not_called()

    def test_runner_releases_lock_after_graceful_stop(self) -> None:
        class StoppingService:
            service_name = "stopping_service"

            @staticmethod
            def run(stop: threading.Event) -> None:
                stop.set()

            @staticmethod
            def stats() -> dict[str, object]:
                return {"accepted_events": 1}

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "market-stream.lock"
            result = run_realtime_market_service(
                SimpleNamespace(),
                service=StoppingService(),
                process_lock=ProcessFileLock(path),
            )
            next_owner = ProcessFileLock(path)
            acquired_after_stop = next_owner.acquire()
            next_owner.release()

        self.assertEqual(result, 0)
        self.assertTrue(acquired_after_stop)


class RealtimeEventSinkTests(unittest.TestCase):
    def test_legacy_service_has_no_controller_or_event_sink_by_default(self) -> None:
        with TemporaryDirectory() as tmp:
            service = BinanceRealtimeMarketService(realtime_settings(tmp))

        self.assertFalse(service._p2_enabled)
        self.assertIsNone(service._p2_controller)
        self.assertIsNone(service._p2_event_sink)
        self.assertNotIn("event_sink_configured", service.stats())

    def test_event_sink_requires_explicit_controller_before_store_initialization(self) -> None:
        with TemporaryDirectory() as tmp, patch(
            "shared.realtime_market.RealtimeFeatureStore"
        ) as store_type:
            with self.assertRaisesRegex(ValueError, "requires a controller"):
                BinanceRealtimeMarketService(
                    realtime_settings(tmp),
                    event_sink=RecordingSink(),
                )

        store_type.assert_not_called()

    def test_controller_events_are_forwarded_as_one_non_blocking_batch(self) -> None:
        events = [
            {"event_id": "one", "event_type": "short_squeeze_ignition"},
            {"event_id": "two", "event_type": "high_leverage_anomaly"},
        ]
        with TemporaryDirectory() as tmp:
            sink = RecordingSink()
            service = BinanceRealtimeMarketService(
                realtime_settings(tmp),
                realtime_controller=FakeController(events),
                event_sink=sink,
            )
            service._start_p2_event_sink()
            evaluated = service._evaluate_p2(now_ts=100, now_monotonic=100)
            service._submit_p2_events(evaluated)
            stats = service.stats()
            service._stop_p2_event_sink()

        self.assertEqual(sink.batches, [events])
        self.assertEqual(stats["event_sink_batches"], 1)
        self.assertEqual(stats["event_sink_events"], 2)
        self.assertEqual(stats["event_sink_failures"], 0)
        self.assertEqual(stats["event_sink"], {"queued_batches": 1})

    def test_sink_exception_is_fail_closed_and_does_not_escape(self) -> None:
        with TemporaryDirectory() as tmp:
            sink = RecordingSink(submit_error=RuntimeError("sensitive detail"))
            service = BinanceRealtimeMarketService(
                realtime_settings(tmp),
                realtime_controller=FakeController(),
                event_sink=sink,
            )
            service._start_p2_event_sink()
            service._submit_p2_events([{"event_id": "one"}])
            stats = service.stats()

        self.assertEqual(stats["event_sink_batches"], 0)
        self.assertEqual(stats["event_sink_events"], 0)
        self.assertEqual(stats["event_sink_failures"], 1)
        self.assertEqual(stats["event_sink_last_error"], "submit:RuntimeError")
        self.assertNotIn("sensitive detail", stats["last_error"])

    def test_sink_start_failure_does_not_stop_legacy_market_loop(self) -> None:
        with TemporaryDirectory() as tmp:
            sink = StartFailingSink()
            service = BinanceRealtimeMarketService(
                realtime_settings(tmp),
                realtime_controller=FakeController(),
                event_sink=sink,
            )
            stop = threading.Event()
            stop.set()
            service.run(stop)
            stats = service.stats()

        self.assertEqual(stats["event_sink_failures"], 1)
        self.assertFalse(stats["event_sink_ready"])
        self.assertEqual(stats["event_sink_last_error"], "start:RuntimeError")
        self.assertNotIn("sensitive startup detail", stats["last_error"])

    def test_service_starts_and_stops_sink_around_market_loop(self) -> None:
        with TemporaryDirectory() as tmp:
            sink = RecordingSink()
            service = BinanceRealtimeMarketService(
                realtime_settings(tmp),
                realtime_controller=FakeController(),
                event_sink=sink,
            )
            stop = threading.Event()
            stop.set()
            service.run(stop)

        self.assertEqual(sink.started, 1)
        self.assertEqual(sink.stopped, 1)
        self.assertFalse(service._p2_event_sink_ready)

    def test_runner_accepts_only_explicit_controller_and_sink_injection(self) -> None:
        observed: list[BinanceRealtimeMarketService] = []

        def stop_immediately(
            service: BinanceRealtimeMarketService,
            stop: threading.Event,
        ) -> None:
            observed.append(service)
            stop.set()

        with TemporaryDirectory() as tmp, patch.object(
            BinanceRealtimeMarketService,
            "run",
            autospec=True,
            side_effect=stop_immediately,
        ):
            controller = FakeController()
            sink = RecordingSink()
            result = run_realtime_market_service(
                realtime_settings(tmp),
                realtime_controller=controller,
                event_sink=sink,
            )

        self.assertEqual(result, 0)
        self.assertEqual(len(observed), 1)
        self.assertIs(observed[0]._p2_controller, controller)
        self.assertIs(observed[0]._p2_event_sink, sink)


if __name__ == "__main__":
    unittest.main()
