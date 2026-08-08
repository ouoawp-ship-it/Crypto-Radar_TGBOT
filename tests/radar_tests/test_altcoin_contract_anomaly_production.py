from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest

from radars.altcoin_contract_anomaly.production import (
    AltcoinProductionEventProcessor,
    CandidateManifestRefreshWorker,
)
from radars.altcoin_contract_anomaly.production_state import (
    ProductionStateCorruptError,
)


class MutableClock:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = float(value)

    def __call__(self) -> float:
        return self.value


def event(
    event_id: str,
    *,
    symbol: str = "ACEUSDT",
    event_type: str = "short_squeeze_ignition",
    window_start: str = "2026-08-08T00:00:00+00:00",
    window_end: str = "2026-08-08T00:05:00+00:00",
) -> dict[str, object]:
    return {
        "schema_version": 3,
        "rules_version": "altcoin_contract_anomaly.p2.v3",
        "event_id": event_id,
        "event_type": event_type,
        "event_name_cn": "测试事件",
        "symbol": symbol,
        "window_start": window_start,
        "window_end": window_end,
        "data_quality": "complete",
        "confirmed_factor_families": ["price_momentum", "volume_expansion"],
        "factor_values": {},
    }


class CandidateManifestRefreshWorkerTests(unittest.TestCase):
    def test_refresh_success_failure_and_stale_stats_keep_last_good_summary(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            clock = MutableClock(10_000.0)
            mode = {"failure": False}
            calls = []

            def scan(settings):
                calls.append(settings)
                if mode["failure"]:
                    raise TimeoutError("redacted upstream failure")
                return {
                    "generated_at": datetime.fromtimestamp(
                        clock(), timezone.utc
                    ).isoformat(),
                    "candidate_symbols": ["ACEUSDT", "COTIUSDT"],
                }

            settings = SimpleNamespace(
                data_dir=root,
                altcoin_contract_anomaly_candidate_snapshot_path=root / "manifest.json",
            )
            worker = CandidateManifestRefreshWorker(
                settings,
                interval_sec=3600,
                max_manifest_age_sec=600,
                scan_callable=scan,
                clock=clock,
            )
            worker.start()
            self.assertTrue(worker.wait_for_generation(0, timeout=2))
            first = worker.stats()
            self.assertEqual(first["refresh_successes"], 1)
            self.assertEqual(first["candidate_count"], 2)
            self.assertFalse(first["manifest_stale"])

            mode["failure"] = True
            clock.value += 300
            generation = int(worker.stats()["generation"])
            worker.request_refresh()
            self.assertTrue(worker.wait_for_generation(generation, timeout=2))
            failed = worker.stats()
            self.assertEqual(failed["refresh_failures"], 1)
            self.assertEqual(failed["last_error_class"], "TimeoutError")
            self.assertEqual(failed["candidate_count"], 2)

            clock.value += 301
            self.assertTrue(worker.stats()["manifest_stale"])
            worker.stop()
            self.assertEqual(len(calls), 2)

    def test_refresh_requests_are_single_flight(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            lock = threading.Lock()
            concurrent = {"current": 0, "maximum": 0, "calls": 0}

            def scan(_settings):
                with lock:
                    concurrent["current"] += 1
                    concurrent["maximum"] = max(
                        concurrent["maximum"], concurrent["current"]
                    )
                    concurrent["calls"] += 1
                time.sleep(0.03)
                with lock:
                    concurrent["current"] -= 1
                return {
                    "generated_at": datetime.fromtimestamp(
                        now, timezone.utc
                    ).isoformat(),
                    "candidate_symbols": [],
                }

            settings = SimpleNamespace(
                data_dir=root,
                altcoin_contract_anomaly_candidate_snapshot_path=root / "manifest.json",
            )
            worker = CandidateManifestRefreshWorker(
                settings,
                interval_sec=3600,
                max_manifest_age_sec=600,
                scan_callable=scan,
                clock=lambda: now,
            )
            worker.start()
            for _ in range(10):
                worker.request_refresh()
            self.assertTrue(worker.wait_for_generation(0, timeout=2))
            worker.stop()
            self.assertEqual(concurrent["maximum"], 1)
            self.assertGreaterEqual(concurrent["calls"], 1)

    def test_refresh_now_runs_scan_on_worker_and_stop_reports_timeout(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            now = time.time()
            entered = threading.Event()
            release = threading.Event()
            scan_threads = []

            def scan(_settings):
                scan_threads.append(threading.current_thread().name)
                entered.set()
                release.wait(2)
                return {
                    "generated_at": datetime.fromtimestamp(
                        now, timezone.utc
                    ).isoformat(),
                    "candidate_symbols": ["ACEUSDT"],
                }

            settings = SimpleNamespace(
                data_dir=root,
                altcoin_contract_anomaly_candidate_snapshot_path=root / "manifest.json",
            )
            worker = CandidateManifestRefreshWorker(
                settings,
                interval_sec=3600,
                retry_sec=1,
                max_manifest_age_sec=600,
                scan_callable=scan,
                clock=lambda: now,
            )
            worker.start()
            self.assertTrue(entered.wait(1))
            self.assertFalse(worker.stop(timeout=0.01))
            self.assertTrue(worker.stats()["stop_timed_out"])
            release.set()
            self.assertTrue(worker.stop(timeout=2))
            self.assertEqual(scan_threads, ["altcoin-candidate-refresh"])

            immediate = CandidateManifestRefreshWorker(
                settings,
                interval_sec=3600,
                retry_sec=1,
                max_manifest_age_sec=600,
                scan_callable=lambda _settings: {
                    "generated_at": datetime.fromtimestamp(
                        now, timezone.utc
                    ).isoformat(),
                    "candidate_symbols": [],
                },
                clock=lambda: now,
            )
            self.assertTrue(immediate.refresh_now(timeout=2))
            self.assertTrue(immediate.stop())


class AltcoinProductionEventProcessorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.clock = MutableClock()
        self.deliveries: list[tuple[str, str, dict[str, object]]] = []

    def tearDown(self) -> None:
        self.directory.cleanup()

    @property
    def state_path(self) -> Path:
        return self.root / "production-state.json"

    @property
    def outbox_path(self) -> Path:
        return self.root / "production-outbox.json"

    @staticmethod
    def formatter(events, context):
        return [
            f"{context['notification_kind']}:{events[0]['symbol']}:"
            f"{','.join(item['event_type'] for item in events)}"
        ]

    def delivery(self, page, *, dedup_key, context):
        self.deliveries.append((page, dedup_key, context))
        return {"status": "sent", "reason": "telegram_api", "sent": True}

    def processor(self, **overrides) -> AltcoinProductionEventProcessor:
        options = {
            "state_path": self.state_path,
            "outbox_path": self.outbox_path,
            "formatter": self.formatter,
            "delivery": self.delivery,
            "candidate_lookup": lambda symbol: {
                "symbol": symbol,
                "market_cap_usd": 10_000_000.0,
            },
            "cooldown_sec": 60,
            "max_messages_per_hour": 20,
            "clock": self.clock,
        }
        options.update(overrides)
        return AltcoinProductionEventProcessor(**options)

    def test_submit_is_durable_before_return_and_restart_delivers(self) -> None:
        processor = self.processor(delivery=None)
        self.assertTrue(processor.submit([event("durable-1")]))
        payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["batches"][0]["events"][0]["event_id"], "durable-1")

        restarted = self.processor()
        restarted.drain_once()
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(restarted.stats()["pending_batches"], 0)

    def test_same_symbol_window_is_merged_and_candidate_snapshot_is_attached(self) -> None:
        captured = []

        def formatter(events, context):
            captured.append((events, context))
            return ["merged"]

        processor = self.processor(formatter=formatter)
        processor.submit([
            event("merge-1", event_type="short_squeeze_ignition"),
            event("merge-2", event_type="high_leverage_anomaly"),
        ])
        processor.drain_once()
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(len(captured[0][0]), 2)
        self.assertEqual(captured[0][1]["notification_kind"], "first_confirmation")
        for item in captured[0][0]:
            self.assertEqual(item["notification_kind"], "first_confirmation")
            self.assertEqual(item["candidate_snapshot"]["symbol"], "ACEUSDT")

    def test_first_cooldown_expiration_new_round_and_invalidation(self) -> None:
        processor = self.processor()
        processor.submit([event("first")])
        processor.drain_once()

        self.clock.value += 10
        processor.submit([
            event(
                "cooldown",
                window_start="2026-08-08T00:05:00+00:00",
                window_end="2026-08-08T00:10:00+00:00",
            )
        ])
        processor.drain_once()
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(processor.stats()["cooldown_suppressed"], 1)

        processor.submit([
            event(
                "expired",
                event_type="anomaly_weakening",
                window_start="2026-08-08T00:10:00+00:00",
                window_end="2026-08-08T00:15:00+00:00",
            )
        ])
        processor.drain_once()
        self.assertEqual(self.deliveries[-1][2]["notification_kind"], "signal_expired")

        self.clock.value += 61
        processor.submit([
            event(
                "new-round",
                window_start="2026-08-08T00:15:00+00:00",
                window_end="2026-08-08T00:20:00+00:00",
            )
        ])
        processor.drain_once()
        self.assertEqual(self.deliveries[-1][2]["notification_kind"], "new_round")

        processor.submit([
            event(
                "invalidated",
                event_type="candidate_condition_invalidated",
                window_start="2026-08-08T00:20:00+00:00",
                window_end="2026-08-08T00:25:00+00:00",
            )
        ])
        processor.drain_once()
        self.assertEqual(
            self.deliveries[-1][2]["notification_kind"],
            "candidate_invalidated",
        )
        symbol_state = processor.state.state_snapshot()["symbols"]["ACEUSDT"]
        self.assertFalse(symbol_state["active"])

    def test_delivery_failure_does_not_advance_cursor_and_restart_recovers(self) -> None:
        failed = self.processor(
            delivery=lambda *_args, **_kwargs: {
                "status": "failed",
                "reason": "telegram_api_failed",
                "sent": False,
            }
        )
        failed.submit([event("retry-1")])
        failed.drain_once()
        self.assertEqual(failed.stats()["pending_batches"], 1)
        self.assertEqual(failed.stats()["sent_batches"], 0)
        self.assertNotIn("ACEUSDT", failed.state.state_snapshot()["symbols"])

        restarted = self.processor()
        restarted.drain_once()
        self.assertEqual(len(self.deliveries), 0)
        self.assertEqual(restarted.stats()["pending_batches"], 1)
        self.clock.value += 5
        restarted.drain_once()
        self.assertEqual(restarted.stats()["pending_batches"], 0)
        self.assertTrue(
            restarted.state.state_snapshot()["symbols"]["ACEUSDT"]["active"]
        )

    def test_retry_deadline_and_provider_retry_after_survive_restart(self) -> None:
        calls = []

        def rate_limited(_page, **_kwargs):
            calls.append(self.clock())
            return {
                "status": "failed",
                "reason": "telegram_rate_limited",
                "sent": False,
                "retry_after_sec": 30,
            }

        failed = self.processor(
            delivery=rate_limited,
            retry_base_sec=5,
            retry_max_sec=20,
        )
        failed.submit([event("retry-after")])
        failed.drain_once()
        payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
        batch = payload["batches"][0]
        self.assertEqual(batch["attempts"], 1)
        self.assertEqual(batch["retry_delay_sec"], 30.0)
        self.assertEqual(batch["next_attempt_at_ts"], self.clock() + 30)

        restarted = self.processor(
            delivery=rate_limited,
            retry_base_sec=5,
            retry_max_sec=20,
        )
        restarted.drain_once()
        self.clock.value += 29
        restarted.drain_once()
        self.assertEqual(calls, [1_000.0])
        self.clock.value += 1
        restarted.drain_once()
        self.assertEqual(calls, [1_000.0, 1_030.0])

    def test_exponential_retry_is_bounded_and_not_polled_early(self) -> None:
        calls = []

        def unavailable(_page, **_kwargs):
            calls.append(self.clock())
            return {"status": "failed", "reason": "provider_unavailable"}

        processor = self.processor(
            delivery=unavailable,
            retry_base_sec=5,
            retry_max_sec=12,
        )
        processor.submit([event("bounded-retry")])
        expected_delays = [5.0, 10.0, 12.0, 12.0]
        for expected_delay in expected_delays:
            processor.drain_once()
            payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
            batch = payload["batches"][0]
            self.assertEqual(batch["retry_delay_sec"], expected_delay)
            before_calls = len(calls)
            self.clock.value += expected_delay - 0.1
            processor.drain_once()
            self.assertEqual(len(calls), before_calls)
            self.clock.value += 0.1
        self.assertEqual(len(calls), 4)

    def test_preview_batch_never_advances_real_lifecycle_cursor(self) -> None:
        processor = self.processor(
            delivery=lambda *_args, **_kwargs: {
                "status": "previewed",
                "reason": "production_preview_recorded",
                "sent": False,
                "previewed": True,
            }
        )
        processor.submit([event("preview-only")])
        processor.drain_once()

        payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["batches"][0]["status"], "previewed")
        self.assertEqual(processor.stats()["previewed_batches"], 1)
        snapshot = processor.state.state_snapshot()
        self.assertEqual(snapshot["processed_event_ids"], [])
        self.assertEqual(snapshot["sent_batch_ids"], [])
        self.assertEqual(snapshot["symbols"], {})

    def test_provider_quarantine_is_not_evicted_by_terminal_retention(self) -> None:
        processor = self.processor(
            queue_size=1,
            delivery=lambda *_args, **_kwargs: {
                "status": "quarantined",
                "reason": "delivery_quarantine",
            },
        )
        processor.submit([event("unknown-provider-effect")])
        processor.drain_once()
        self.assertEqual(processor.stats()["quarantined_batches"], 1)

        processor._delivery = self.delivery
        for index in range(2):
            processor.submit([
                event(
                    f"later-sent-{index}",
                    symbol=f"LATER{index}USDT",
                    window_start=f"2026-08-08T00:{5 + index * 5:02d}:00+00:00",
                    window_end=f"2026-08-08T00:{10 + index * 5:02d}:00+00:00",
                )
            ])
            processor.drain_once()

        restarted = self.processor(queue_size=1, delivery=None)
        payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
        self.assertIn("unknown-provider-effect", restarted.state.known_event_ids())
        self.assertEqual(
            sum(batch["status"] == "quarantined" for batch in payload["batches"]),
            1,
        )

    def test_provider_quarantine_blocks_later_windows_and_survives_restart(self) -> None:
        processor = self.processor(
            delivery=lambda *_args, **_kwargs: {
                "status": "quarantined",
                "reason": "delivery_quarantine",
            },
        )
        processor.submit([event("quarantine-first")])
        processor.drain_once()
        self.assertEqual(processor.stats()["quarantined_symbols"], 1)

        processor._delivery = self.delivery
        processor.submit([
            event(
                "quarantine-next-window",
                window_start="2026-08-08T00:05:00+00:00",
                window_end="2026-08-08T00:10:00+00:00",
            )
        ])
        processor.drain_once()
        self.assertEqual(self.deliveries, [])
        payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
        later = next(
            batch
            for batch in payload["batches"]
            if "quarantine-next-window" in batch.get("event_ids", [])
        )
        self.assertEqual(later["status"], "suppressed")
        self.assertEqual(later["suppressed_reason"], "symbol_quarantined")

        restarted = self.processor()
        self.assertEqual(restarted.stats()["quarantined_symbols"], 1)
        restarted.submit([
            event(
                "quarantine-after-restart",
                window_start="2026-08-08T00:10:00+00:00",
                window_end="2026-08-08T00:15:00+00:00",
            )
        ])
        restarted.drain_once()
        self.assertEqual(self.deliveries, [])
        payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
        after_restart = next(
            batch
            for batch in payload["batches"]
            if "quarantine-after-restart" in batch.get("event_ids", [])
        )
        self.assertEqual(after_restart["status"], "suppressed")
        self.assertEqual(
            after_restart["suppressed_reason"],
            "symbol_quarantined",
        )

    def test_worker_survives_local_cursor_failure_and_dedup_recovers(self) -> None:
        accepted: set[str] = set()

        def deduplicating_delivery(_page, *, dedup_key, context):
            del context
            if dedup_key in accepted:
                return {"status": "skipped", "reason": "dedup_cooldown"}
            accepted.add(dedup_key)
            return {"status": "sent", "reason": "telegram_api", "sent": True}

        processor = self.processor(
            delivery=deduplicating_delivery,
            retry_base_sec=0.01,
            retry_max_sec=0.05,
            poll_interval_sec=0.01,
        )
        processor.submit([event("worker-recovers")])
        pending = processor.state.pending_batches()[0]
        processor.state.set_pages(
            str(pending["batch_id"]),
            processor._format_batch(pending),
        )
        original_save = processor.state._save_outbox
        failed_once = {"value": False}

        def flaky_save():
            if not failed_once["value"]:
                failed_once["value"] = True
                raise OSError("simulated local WAL outage")
            return original_save()

        processor.state._save_outbox = flaky_save  # type: ignore[method-assign]
        processor.start()
        try:
            deadline = time.monotonic() + 2.0
            while (
                processor.stats()["pending_batches"]
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            stats = processor.stats()
            self.assertTrue(stats["running"])
            self.assertEqual(stats["pending_batches"], 0)
            self.assertEqual(stats["worker_failures"], 1)
            self.assertEqual(stats["recovered_pages"], 1)
            self.assertEqual(len(accepted), 1)
        finally:
            self.assertTrue(processor.stop(timeout=2))

    def test_sent_event_is_not_repeated_after_restart(self) -> None:
        first = self.processor()
        first.submit([event("once")])
        first.drain_once()
        self.assertEqual(len(self.deliveries), 1)

        restarted = self.processor()
        restarted.submit([event("once")])
        restarted.drain_once()
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(restarted.stats()["duplicate_events"], 1)

    def test_page_crash_recovers_via_gateway_dedup_then_finishes_batch(self) -> None:
        accepted: set[str] = set()
        crash = {"now": True}

        def two_pages(_events, _context):
            return ["page-one", "page-two"]

        def crash_delivery(_page, *, dedup_key, context):
            del context
            if dedup_key in accepted:
                return {"status": "skipped", "reason": "dedup_cooldown"}
            accepted.add(dedup_key)
            if crash["now"]:
                crash["now"] = False
                raise SystemExit("simulated process crash after Telegram accepted page")
            return {"status": "sent", "reason": "telegram_api", "sent": True}

        first = self.processor(formatter=two_pages, delivery=crash_delivery)
        first.submit([event("page-crash")])
        with self.assertRaises(SystemExit):
            first.drain_once()

        restarted = self.processor(formatter=two_pages, delivery=crash_delivery)
        restarted.drain_once()
        stats = restarted.stats()
        self.assertEqual(stats["pending_batches"], 0)
        self.assertEqual(stats["recovered_pages"], 1)
        self.assertEqual(len(accepted), 2)

    def test_terminal_outbox_is_replayed_if_state_cursor_write_crashes(self) -> None:
        first = self.processor()
        first.submit([event("cursor-crash")])
        original_save = first.state._save_state

        def crash_save():
            raise RuntimeError("simulated cursor crash")

        first.state._save_state = crash_save  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            first.drain_once()
        first.state._save_state = original_save  # type: ignore[method-assign]

        delivered_before = len(self.deliveries)
        restarted = self.processor()
        restarted.submit([event("cursor-crash")])
        restarted.drain_once()
        self.assertEqual(len(self.deliveries), delivered_before)
        self.assertEqual(restarted.stats()["duplicate_events"], 1)
        self.assertTrue(
            restarted.state.state_snapshot()["symbols"]["ACEUSDT"]["active"]
        )

    def test_global_rate_limit_holds_pending_until_window_expires(self) -> None:
        processor = self.processor(max_messages_per_hour=1, cooldown_sec=0)
        processor.submit([event("rate-1")])
        processor.drain_once()
        processor.submit([
            event(
                "rate-2",
                symbol="COTIUSDT",
                window_start="2026-08-08T00:05:00+00:00",
                window_end="2026-08-08T00:10:00+00:00",
            )
        ])
        processor.drain_once()
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(processor.stats()["pending_batches"], 1)

        self.clock.value += 3601
        processor.drain_once()
        self.assertEqual(len(self.deliveries), 2)
        self.assertEqual(processor.stats()["pending_batches"], 0)

    def test_wal_write_failure_is_reported_not_acknowledged(self) -> None:
        processor = self.processor()

        def fail_save():
            raise OSError("disk full")

        processor.state._save_outbox = fail_save  # type: ignore[method-assign]
        with self.assertRaises(OSError):
            processor.submit([event("wal-failure")])
        self.assertEqual(processor.stats()["queue_rejections"], 1)
        self.assertEqual(len(self.deliveries), 0)

    def test_multi_symbol_submit_is_one_atomic_wal_write(self) -> None:
        processor = self.processor(delivery=None)
        original_save = processor.state._save_outbox
        calls = {"count": 0}

        def counted_save():
            calls["count"] += 1
            original_save()

        processor.state._save_outbox = counted_save  # type: ignore[method-assign]
        processor.submit([
            event("atomic-a", symbol="ACEUSDT"),
            event("atomic-b", symbol="COTIUSDT"),
        ])
        self.assertEqual(calls["count"], 1)
        payload = json.loads(self.outbox_path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["batches"]), 2)
        self.assertEqual(
            {batch["symbol"] for batch in payload["batches"]},
            {"ACEUSDT", "COTIUSDT"},
        )

    def test_durable_spool_atomically_rejects_capacity_without_dropping_pending(self) -> None:
        processor = self.processor(delivery=None, queue_size=10)
        for index in range(10):
            processor.submit([
                event(
                    f"spool-{index}",
                    symbol=f"TOKEN{index}USDT",
                )
            ])
        before = self.outbox_path.read_text(encoding="utf-8")
        with self.assertRaises(OverflowError):
            processor.submit([
                event("spool-overflow-a", symbol="OVERFLOWAUSDT"),
                event("spool-overflow-b", symbol="OVERFLOWBUSDT"),
            ])
        self.assertEqual(self.outbox_path.read_text(encoding="utf-8"), before)
        self.assertEqual(processor.stats()["queue_rejections"], 1)
        restarted = self.processor(delivery=None, queue_size=10)
        self.assertEqual(restarted.stats()["pending_batches"], 10)
        known = restarted.state.known_event_ids()
        self.assertEqual(known, {f"spool-{index}" for index in range(10)})

    def test_corrupt_state_or_outbox_fails_closed_without_overwrite(self) -> None:
        for filename in ("production-state.json", "production-outbox.json"):
            with self.subTest(filename=filename), TemporaryDirectory() as directory:
                root = Path(directory)
                damaged = root / filename
                damaged.write_text("{not-json-and-must-be-preserved", encoding="utf-8")
                with self.assertRaises(ProductionStateCorruptError):
                    AltcoinProductionEventProcessor(
                        state_path=root / "production-state.json",
                        outbox_path=root / "production-outbox.json",
                        formatter=self.formatter,
                        delivery=self.delivery,
                    )
                quarantined = list(root.glob(f"{filename}.corrupt.*"))
                self.assertEqual(len(quarantined), 1)
                self.assertEqual(
                    quarantined[0].read_text(encoding="utf-8"),
                    "{not-json-and-must-be-preserved",
                )
                # A service restart must remain blocked until an operator
                # restores a valid primary document; quarantine is not a
                # one-restart bypass back to an empty production cursor.
                with self.assertRaises(ProductionStateCorruptError):
                    AltcoinProductionEventProcessor(
                        state_path=root / "production-state.json",
                        outbox_path=root / "production-outbox.json",
                        formatter=self.formatter,
                        delivery=self.delivery,
                    )


if __name__ == "__main__":
    unittest.main()
