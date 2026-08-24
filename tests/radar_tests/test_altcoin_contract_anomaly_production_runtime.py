from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, call, patch

from config import Settings
from radars.altcoin_contract_anomaly.production import (
    AltcoinProductionEventProcessor,
)
from radars.altcoin_contract_anomaly.production_runtime import (
    ProductionObservationState,
    ProductionStatusWriter,
    ProductionTelegramDelivery,
    run_altcoin_production_service,
)
from radars.altcoin_contract_anomaly.realtime_state import (
    RealtimeObservationState,
)
from shared.process_lock import ProcessFileLock
from shared.storage import JsonStore
from shared.telegram import (
    PushResult,
    TelegramDeliveryDiagnostics,
    TelegramGateway,
)


def production_event(event_id: str = "event-1") -> dict[str, object]:
    return {
        "schema_version": 3,
        "rules_version": "altcoin_contract_anomaly.p2.v3",
        "event_id": event_id,
        "event_type": "short_squeeze_ignition",
        "event_name_cn": "逼空启动",
        "symbol": "ACEUSDT",
        "window_start": "2026-08-08T00:00:00+00:00",
        "window_end": "2026-08-08T00:05:00+00:00",
        "data_quality": "complete",
        "confirmed_factor_families": [
            "price_momentum",
            "volume_expansion",
        ],
        "factor_values": {},
    }


def production_settings(root: Path, **changes: object) -> Settings:
    settings = Settings(
        data_dir=root,
        altcoin_contract_anomaly_enable=True,
        altcoin_contract_anomaly_cmc_api_key="fake-cmc-key",
        altcoin_contract_anomaly_realtime_enable=True,
        altcoin_contract_anomaly_production_enable=True,
    )
    return replace(settings, **changes)


class _FakeProcessorState:
    def __init__(self) -> None:
        self.event_ids: set[str] = set()

    def known_event_ids(self) -> set[str]:
        return set(self.event_ids)


class _FakeProcessor:
    def __init__(self, *, fail: bool = False, admit: bool = True) -> None:
        self.fail = fail
        self.admit = admit
        self.state = _FakeProcessorState()
        self.submissions: list[list[dict[str, object]]] = []

    def submit(self, events):
        copied = [dict(event) for event in events]
        self.submissions.append(copied)
        if self.fail:
            raise OSError("simulated WAL failure")
        if self.admit:
            self.state.event_ids.update(
                str(event["event_id"]) for event in copied
            )
        return True


class ProductionObservationStateTests(unittest.TestCase):
    def test_production_wal_is_admitted_before_p2_cursor_commit(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            processor = _FakeProcessor()
            state = ProductionObservationState(
                root / "observation.json",
                root / "events.jsonl",
                processor=processor,  # type: ignore[arg-type]
            )
            order: list[str] = []

            def submit(events):
                order.append("production_wal")
                return _FakeProcessor.submit(processor, events)

            processor.submit = submit  # type: ignore[method-assign]
            with patch.object(
                RealtimeObservationState,
                "record_event_batch",
                side_effect=lambda *_args, **_kwargs: order.append("p2_cursor") or [
                    "event-1"
                ],
            ):
                result = state.record_event_batch([production_event()])

        self.assertEqual(result, ["event-1"])
        self.assertEqual(order, ["production_wal", "p2_cursor"])

    def test_wal_failure_or_incomplete_admission_never_advances_p2_cursor(self) -> None:
        for processor in (_FakeProcessor(fail=True), _FakeProcessor(admit=False)):
            with self.subTest(fail=processor.fail, admit=processor.admit):
                with TemporaryDirectory() as directory:
                    root = Path(directory)
                    state = ProductionObservationState(
                        root / "observation.json",
                        root / "events.jsonl",
                        processor=processor,  # type: ignore[arg-type]
                    )
                    with patch.object(
                        RealtimeObservationState,
                        "record_event_batch",
                    ) as base_commit:
                        with self.assertRaises((OSError, RuntimeError)):
                            state.record_event_batch([production_event()])
                base_commit.assert_not_called()

    def test_real_queue_capacity_rejection_never_advances_p2_cursor(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            processor = AltcoinProductionEventProcessor(
                state_path=root / "production-state.json",
                outbox_path=root / "production-outbox.json",
                formatter=lambda _events, _context: ["message"],
                delivery=None,
                queue_size=1,
            )
            processor.submit([production_event("fills-capacity")])
            state = ProductionObservationState(
                root / "observation.json",
                root / "events.jsonl",
                processor=processor,
            )
            second = production_event("must-not-commit")
            second["symbol"] = "COTIUSDT"
            with patch.object(
                RealtimeObservationState,
                "record_event_batch",
            ) as base_commit:
                with self.assertRaises(OverflowError):
                    state.record_event_batch([second])
            base_commit.assert_not_called()
            self.assertNotIn(
                "must-not-commit",
                processor.state.known_event_ids(),
            )


class ProductionTelegramDeliveryTests(unittest.TestCase):
    def test_preview_mode_never_calls_telegram_api(self) -> None:
        gateway = MagicMock()
        gateway.topic_route_configured.return_value = False
        delivery = ProductionTelegramDelivery(
            gateway,
            real_send=False,
            daily_limit=50,
        )

        result = delivery(
            "preview",
            dedup_key="preview-key",
            context={"notification_kind": "first_confirmation"},
        )

        self.assertEqual(result["reason"], "production_preview_recorded")
        self.assertEqual(result["status"], "previewed")
        self.assertFalse(result["sent"])
        self.assertTrue(result["previewed"])
        gateway.send.assert_not_called()
        self.assertEqual(delivery.stats()["preview_pages"], 1)
        self.assertEqual(delivery.stats()["telegram_attempts"], 0)

    def test_real_send_failure_leaves_outbox_pending_and_cursor_unadvanced(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            gateway = MagicMock()
            gateway.topic_route_configured.return_value = True
            gateway.send.return_value = PushResult(
                status="failed",
                reason="telegram_api_failed",
                sent=False,
                diagnostics=TelegramDeliveryDiagnostics(retry_after_sec=17),
                signal_store_written=None,
            )
            delivery = ProductionTelegramDelivery(
                gateway,
                real_send=True,
                daily_limit=50,
            )
            processor = AltcoinProductionEventProcessor(
                state_path=root / "production-state.json",
                outbox_path=root / "production-outbox.json",
                formatter=lambda _events, _context: ["message"],
                delivery=delivery,
                cooldown_sec=0,
            )
            processor.submit([production_event()])
            processor.drain_once()
            snapshot = processor.state.state_snapshot()
            payload = json.loads(
                (root / "production-outbox.json").read_text(encoding="utf-8")
            )

        gateway.send.assert_called_once_with(
            "message",
            "TG_ALTCOIN_CONTRACT_ANOMALY",
            unittest.mock.ANY,
            send=True,
            confirm_real_send=True,
            cooldown_sec=7 * 86_400,
            daily_limit=50,
            parse_mode="HTML",
            enrich_market_context=False,
        )
        self.assertEqual(processor.stats()["pending_batches"], 1)
        self.assertEqual(processor.stats()["sent_batches"], 0)
        self.assertNotIn("ACEUSDT", snapshot["symbols"])
        self.assertEqual(payload["batches"][0]["retry_after_sec"], 17.0)
        self.assertEqual(payload["batches"][0]["retry_delay_sec"], 17.0)

    def test_provider_success_then_local_crash_is_permanently_quarantined(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            clock = SimpleNamespace(value=1_000.0)
            now = lambda: clock.value
            settings = Settings(
                data_dir=root,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_altcoin_contract_anomaly_topic_id="77",
                tg_push_history_path=root / "push-history.json",
                tg_outbox_path=root / "telegram-outbox.json",
                tg_topic_routes_path=root / "topic-routes.json",
                signal_events_db_path=root / "signals.db",
                tg_default_cooldown_sec=0,
            )
            first_gateway = TelegramGateway(settings, JsonStore(root))
            first_delivery = ProductionTelegramDelivery(
                first_gateway,
                real_send=True,
                daily_limit=50,
            )
            first = AltcoinProductionEventProcessor(
                state_path=root / "production-state.json",
                outbox_path=root / "production-outbox.json",
                formatter=lambda _events, _context: ["message"],
                delivery=first_delivery,
                clock=now,
            )
            first.submit([production_event("provider-side-effect")])
            with (
                patch.object(
                    first_gateway,
                    "_send_real_message_ids",
                    return_value=(True, [4321]),
                ) as first_post,
                patch.object(
                    first_gateway,
                    "_finish_delivery",
                    side_effect=OSError("simulated local crash"),
                ),
            ):
                first.drain_once()
            self.assertEqual(first_post.call_count, 1)
            self.assertEqual(first.stats()["pending_batches"], 1)

            clock.value += 5
            restarted_gateway = TelegramGateway(settings, JsonStore(root))
            restarted_delivery = ProductionTelegramDelivery(
                restarted_gateway,
                real_send=True,
                daily_limit=50,
            )
            restarted = AltcoinProductionEventProcessor(
                state_path=root / "production-state.json",
                outbox_path=root / "production-outbox.json",
                formatter=lambda _events, _context: ["message"],
                delivery=restarted_delivery,
                clock=now,
            )
            with patch.object(
                restarted_gateway,
                "_send_real_message_ids",
            ) as duplicate_post:
                restarted.drain_once()

            production_outbox = json.loads(
                (root / "production-outbox.json").read_text(encoding="utf-8")
            )
            telegram_outbox = JsonStore(root).load(
                settings.tg_outbox_path,
                [],
            )
            self.assertEqual(production_outbox["batches"][0]["status"], "quarantined")
            self.assertEqual(telegram_outbox[0]["status"], "pending")
            self.assertEqual(restarted.stats()["quarantined_batches"], 1)
            self.assertEqual(restarted.state.state_snapshot()["symbols"], {})
            duplicate_post.assert_not_called()


class ProductionRuntimePreflightTests(unittest.TestCase):
    def test_invalid_configuration_fails_before_state_network_or_websocket(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=True,
        ), patch(
            "radars.altcoin_contract_anomaly.production_runtime.CandidateManifestRefreshWorker"
        ) as refresh_type, patch(
            "radars.altcoin_contract_anomaly.production_runtime.TelegramGateway"
        ) as gateway_type, patch(
            "radars.altcoin_contract_anomaly.production_runtime.RealtimeFeatureStore"
        ) as store_type, patch(
            "radars.altcoin_contract_anomaly.production_runtime.BinanceRealtimeMarketService"
        ) as service_type:
            settings = production_settings(
                Path(directory),
                altcoin_contract_anomaly_cmc_api_key="",
            )
            runner = Mock()
            result = run_altcoin_production_service(
                settings,
                service_runner=runner,
            )

        self.assertEqual(result, 2)
        refresh_type.assert_not_called()
        gateway_type.assert_not_called()
        store_type.assert_not_called()
        service_type.assert_not_called()
        runner.assert_not_called()

    def test_busy_shared_process_lock_fails_before_refresh_or_websocket(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            root = Path(directory)
            settings = production_settings(root)
            owner = ProcessFileLock(
                settings.altcoin_contract_anomaly_realtime_lock_path
            )
            self.assertTrue(owner.acquire())
            try:
                with patch(
                    "radars.altcoin_contract_anomaly.production_runtime.CandidateManifestRefreshWorker"
                ) as refresh_type, patch(
                    "radars.altcoin_contract_anomaly.production_runtime.BinanceRealtimeMarketService"
                ) as service_type:
                    runner = Mock()
                    result = run_altcoin_production_service(
                        settings,
                        service_runner=runner,
                    )
            finally:
                owner.release()

        self.assertEqual(result, 2)
        refresh_type.assert_not_called()
        service_type.assert_not_called()
        runner.assert_not_called()

    def test_valid_warm_manifest_starts_async_refresh_before_state_and_websocket(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            root = Path(directory)
            settings = production_settings(root)
            order: list[str] = []
            refresh = MagicMock()
            refresh.start.side_effect = lambda: order.append("refresh_start")
            refresh.stop.return_value = True
            refresh.stats.return_value = {"refresh_successes": 0}
            manifest = SimpleNamespace(candidates={"ACEUSDT": {"symbol": "ACEUSDT"}})
            consumer = MagicMock()
            consumer.poll.side_effect = lambda **_kwargs: order.append(
                "manifest_preflight"
            ) or {"status": "valid_unchanged"}
            consumer.last_valid = manifest
            processor = MagicMock()
            service = MagicMock()
            status = MagicMock()
            status.stop.return_value = True
            runner = Mock(
                side_effect=lambda *_args, **_kwargs: order.append("runner") or 0
            )

            def feature_store(*_args, **_kwargs):
                order.append("state")
                return MagicMock()

            def market_service(*_args, **_kwargs):
                order.append("websocket_service")
                return service

            with (
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.CandidateManifestRefreshWorker",
                    return_value=refresh,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.CandidateManifestConsumer",
                    return_value=consumer,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.AltcoinProductionEventProcessor",
                    return_value=processor,
                ) as processor_type,
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.RealtimeFeatureStore",
                    side_effect=feature_store,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.AltcoinRealtimeController",
                    return_value=MagicMock(),
                ) as controller_type,
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.BinanceRealtimeMarketService",
                    side_effect=market_service,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.ProductionStatusWriter",
                    return_value=status,
                ),
            ):
                result = run_altcoin_production_service(
                    settings,
                    gateway=MagicMock(),
                    service_runner=runner,
                )

        self.assertEqual(result, 0)
        refresh.refresh_now.assert_not_called()
        self.assertLess(order.index("manifest_preflight"), order.index("state"))
        self.assertLess(order.index("refresh_start"), order.index("state"))
        self.assertLess(order.index("refresh_start"), order.index("websocket_service"))
        self.assertEqual(
            controller_type.call_args.kwargs["oi_budget_window_sec"],
            300,
        )
        processor_kwargs = processor_type.call_args.kwargs
        self.assertEqual(
            processor_kwargs["state_path"].name,
            "altcoin_contract_anomaly_production_state.preview.json",
        )
        self.assertEqual(
            processor_kwargs["outbox_path"].name,
            "altcoin_contract_anomaly_production_outbox.preview.json",
        )

    def test_cold_start_refresh_and_second_preflight_precede_state_and_websocket(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            root = Path(directory)
            settings = production_settings(root)
            order: list[str] = []
            refresh = MagicMock()
            refresh.refresh_now.side_effect = lambda **_kwargs: order.append(
                "refresh"
            ) or True
            refresh.stop.return_value = True
            refresh.stats.return_value = {"refresh_successes": 1}
            manifest = SimpleNamespace(candidates={"ACEUSDT": {"symbol": "ACEUSDT"}})
            consumer = MagicMock()
            poll_results = iter((
                {"status": "manifest_degraded", "reason": "manifest_missing"},
                {"status": "valid_changed"},
            ))

            def poll_manifest(**_kwargs):
                order.append("manifest_preflight")
                return next(poll_results)

            consumer.poll.side_effect = poll_manifest
            consumer.last_valid = manifest
            processor = MagicMock()
            processor.state.known_event_ids.return_value = set()
            service = MagicMock()
            status = MagicMock()
            status.stop.return_value = True
            runner_lock_state: list[bool] = []

            def run_service(*_args, **kwargs):
                order.append("runner")
                runner_lock_state.append(bool(kwargs["process_lock"].acquired))
                return 0

            runner = Mock(side_effect=run_service)

            def feature_store(*_args, **_kwargs):
                order.append("state")
                return MagicMock()

            def controller(*_args, **_kwargs):
                order.append("controller")
                return MagicMock()

            def market_service(*_args, **_kwargs):
                order.append("websocket_service")
                return service

            with (
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.CandidateManifestRefreshWorker",
                    return_value=refresh,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.CandidateManifestConsumer",
                    return_value=consumer,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.AltcoinProductionEventProcessor",
                    return_value=processor,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.RealtimeFeatureStore",
                    side_effect=feature_store,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.AltcoinRealtimeController",
                    side_effect=controller,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.BinanceRealtimeMarketService",
                    side_effect=market_service,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.ProductionStatusWriter",
                    return_value=status,
                ),
            ):
                result = run_altcoin_production_service(
                    settings,
                    gateway=MagicMock(),
                    service_runner=runner,
                )

        self.assertEqual(result, 0)
        self.assertEqual(order.count("manifest_preflight"), 2)
        self.assertLess(order.index("refresh"), order.index("state"))
        self.assertLess(order.index("manifest_preflight"), order.index("state"))
        self.assertLess(order.index("manifest_preflight"), order.index("websocket_service"))
        self.assertEqual(order[-1], "runner")
        runner.assert_called_once()
        self.assertEqual(runner_lock_state, [True])
        self.assertIs(runner.call_args.kwargs["service"], service)

    def test_stale_manifest_fails_before_gateway_state_and_websocket(self) -> None:
        with TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {},
            clear=True,
        ):
            settings = production_settings(Path(directory))
            refresh = MagicMock()
            refresh.refresh_now.return_value = False
            refresh.stop.return_value = True
            consumer = MagicMock()
            consumer.poll.return_value = {
                "status": "manifest_degraded",
                "reason": "candidate_manifest_stale",
            }
            with (
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.CandidateManifestRefreshWorker",
                    return_value=refresh,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.CandidateManifestConsumer",
                    return_value=consumer,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.TelegramGateway"
                ) as gateway_type,
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.RealtimeFeatureStore"
                ) as store_type,
                patch(
                    "radars.altcoin_contract_anomaly.production_runtime.BinanceRealtimeMarketService"
                ) as service_type,
            ):
                runner = Mock()
                result = run_altcoin_production_service(
                    settings,
                    service_runner=runner,
                )

        self.assertEqual(result, 3)
        gateway_type.assert_not_called()
        store_type.assert_not_called()
        service_type.assert_not_called()
        runner.assert_not_called()


class ProductionStatusWriterTests(unittest.TestCase):
    def test_status_is_atomic_credential_free_and_reports_refresh_and_delivery(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "production-status.json"
            service = MagicMock()
            service.stats.return_value = {
                "candidate_coverage_complete": True,
                "active_stream_count": 5,
            }
            controller = MagicMock()
            controller.stats.return_value = {
                "manifest_event_ready": True,
                "manifest_hash": "pool-hash",
                "manifest_snapshot_hash": "snapshot-hash",
                "manifest_age_sec": 12.5,
                "candidate_count": 2,
                "manifest_last_error": "",
            }
            refresher = MagicMock()
            refresher.stats.return_value = {
                "refresh_attempts": 2,
                "refresh_successes": 1,
                "refresh_failures": 1,
            }
            processor = MagicMock()
            processor.stats.return_value = {"pending_batches": 1}
            delivery = MagicMock()
            delivery.stats.return_value = {
                "real_send_enabled": False,
                "route_configured": False,
                "preview_pages": 1,
            }
            lock = MagicMock()
            lock.acquired = True
            writer = ProductionStatusWriter(
                path,
                interval_sec=30,
                service=service,
                controller=controller,
                refresher=refresher,
                processor=processor,
                delivery=delivery,
                process_lock=lock,
            )

            self.assertTrue(writer.write_once())
            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(payload["module"], "altcoin_contract_anomaly")
        self.assertEqual(payload["mode"], "production")
        self.assertTrue(payload["manifest"]["valid"])
        self.assertEqual(payload["manifest"]["candidate_count"], 2)
        self.assertEqual(payload["refresh"]["refresh_successes"], 1)
        self.assertEqual(payload["processor"]["pending_batches"], 1)
        self.assertFalse(payload["telegram"]["real_send_enabled"])
        rendered = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("fake-cmc-key", rendered)
        self.assertNotIn("bot_token", rendered.lower())


if __name__ == "__main__":
    unittest.main()
