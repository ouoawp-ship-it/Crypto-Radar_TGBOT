from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Callable, Mapping

from config.settings import Settings
from shared.atomic_json import locked_write_json
from shared.process_lock import ProcessFileLock
from shared.realtime_market import (
    BinanceRealtimeMarketService,
    RealtimeFeatureStore,
    run_realtime_market_service,
)
from shared.storage import JsonStore
from shared.telegram import TelegramGateway

from .configuration import (
    AltcoinAnomalyConfig,
    AltcoinAnomalyConfigError,
    AltcoinAnomalyProductionConfig,
)
from .production import (
    AltcoinProductionEventProcessor,
    CandidateManifestRefreshWorker,
)
from .production_formatter import (
    TELEGRAM_TEMPLATE_ID,
    render_production_event_group,
)
from .radar import scan_candidate_pool
from .realtime import AltcoinRealtimeController, CandidateManifestConsumer
from .realtime_state import RealtimeObservationState


PRODUCTION_RUNTIME_SCHEMA_VERSION = 1


def _utc_iso(timestamp: float | None = None) -> str:
    return datetime.fromtimestamp(
        float(time.time() if timestamp is None else timestamp),
        timezone.utc,
    ).isoformat()


class ProductionObservationState(RealtimeObservationState):
    """Commit the production WAL before advancing the P2 evaluation cursor."""

    def __init__(
        self,
        state_path: str | Path,
        event_path: str | Path,
        *,
        processor: AltcoinProductionEventProcessor,
    ) -> None:
        super().__init__(state_path, event_path)
        self._processor = processor

    def record_event_batch(
        self,
        events: list[Mapping[str, Any]],
        *,
        last_valid_manifest: Mapping[str, Any] | None = None,
        symbol_states: Mapping[str, Mapping[str, Any]] | None = None,
        oi_samples: Mapping[str, list[Mapping[str, Any]]] | None = None,
    ) -> list[str]:
        if events:
            self._processor.submit(events)
            expected = {
                str(event.get("event_id") or "")
                for event in events
                if str(event.get("event_id") or "")
            }
            if not expected.issubset(self._processor.state.known_event_ids()):
                raise RuntimeError("production event WAL admission incomplete")
        return super().record_event_batch(
            events,
            last_valid_manifest=last_valid_manifest,
            symbol_states=symbol_states,
            oi_samples=oi_samples,
        )


class ProductionTelegramDelivery:
    """Telegram adapter with an explicit no-network preview mode."""

    def __init__(
        self,
        gateway: TelegramGateway,
        *,
        real_send: bool,
        daily_limit: int,
        dedup_cooldown_sec: int = 7 * 86_400,
    ) -> None:
        self.gateway = gateway
        self.real_send = bool(real_send)
        self.daily_limit = int(daily_limit)
        self.dedup_cooldown_sec = max(1, int(dedup_cooldown_sec))
        self._lock = threading.RLock()
        self._stats = {
            "preview_pages": 0,
            "telegram_attempts": 0,
            "telegram_sent_pages": 0,
            "telegram_failures": 0,
            "last_delivery_at": "",
            "last_error_class": "",
        }

    @property
    def route_configured(self) -> bool:
        return self.gateway.topic_route_configured(TELEGRAM_TEMPLATE_ID)

    def __call__(
        self,
        page: str,
        *,
        dedup_key: str,
        context: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        del context
        if not self.real_send:
            with self._lock:
                self._stats["preview_pages"] += 1
                self._stats["last_delivery_at"] = _utc_iso()
                self._stats["last_error_class"] = ""
            # The module-owned WAL remains the durable preview record.  No
            # Telegram history, API call, or signal store is touched.
            return {
                "status": "previewed",
                "sent": False,
                "previewed": True,
                "reason": "production_preview_recorded",
            }

        with self._lock:
            self._stats["telegram_attempts"] += 1
        result = self.gateway.send(
            page,
            TELEGRAM_TEMPLATE_ID,
            dedup_key,
            send=True,
            confirm_real_send=True,
            cooldown_sec=self.dedup_cooldown_sec,
            daily_limit=self.daily_limit,
            parse_mode="HTML",
            enrich_market_context=False,
        )
        ledger_safe = result.signal_store_written is not False
        delivered = bool(result.sent and result.status == "sent" and ledger_safe)
        recovered = result.reason == "dedup_cooldown"
        quarantined = result.reason == "delivery_quarantine"
        retry_after_sec = (
            result.diagnostics.retry_after_sec
            if result.diagnostics is not None
            else None
        )
        with self._lock:
            if delivered:
                self._stats["telegram_sent_pages"] += 1
                self._stats["last_delivery_at"] = _utc_iso()
                self._stats["last_error_class"] = ""
            elif not recovered:
                self._stats["telegram_failures"] += 1
                self._stats["last_error_class"] = (
                    "telegram_local_ledger_failed"
                    if not ledger_safe
                    else str(result.reason or "telegram_delivery_failed")[:100]
                )
        if recovered:
            return {"status": "skipped", "reason": "dedup_cooldown"}
        if quarantined:
            return {
                "status": "quarantined",
                "sent": False,
                "reason": "delivery_quarantine",
            }
        if not ledger_safe:
            return {
                "status": "failed",
                "sent": False,
                "reason": "telegram_local_ledger_failed",
                "retry_after_sec": retry_after_sec,
            }
        return {
            "status": result.status,
            "sent": result.sent,
            "reason": result.reason,
            "retry_after_sec": retry_after_sec,
        }

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return {
                **self._stats,
                "real_send_enabled": self.real_send,
                "route_configured": self.route_configured,
            }


class ProductionStatusWriter:
    """Periodically publish one credential-free production readiness record."""

    def __init__(
        self,
        path: str | Path,
        *,
        interval_sec: int,
        service: BinanceRealtimeMarketService,
        controller: AltcoinRealtimeController,
        refresher: CandidateManifestRefreshWorker,
        processor: AltcoinProductionEventProcessor,
        delivery: ProductionTelegramDelivery,
        process_lock: ProcessFileLock,
    ) -> None:
        self.path = Path(path)
        self.interval_sec = max(1, int(interval_sec))
        self.service = service
        self.controller = controller
        self.refresher = refresher
        self.processor = processor
        self.delivery = delivery
        self.process_lock = process_lock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._running = False
        self._last_error_class = ""

    def _payload(self) -> dict[str, Any]:
        service_stats = self.service.stats()
        controller_stats = self.controller.stats()
        refresh_stats = self.refresher.stats()
        manifest_age = controller_stats.get("manifest_age_sec")
        return {
            "schema_version": PRODUCTION_RUNTIME_SCHEMA_VERSION,
            "module": "altcoin_contract_anomaly",
            "mode": "production",
            "updated_at": _utc_iso(),
            "running": bool(self._running),
            "process_lock_acquired": self.process_lock.acquired,
            "manifest": {
                "valid": bool(controller_stats.get("manifest_event_ready")),
                "hash": str(controller_stats.get("manifest_hash") or ""),
                "snapshot_hash": str(
                    controller_stats.get("manifest_snapshot_hash") or ""
                ),
                "age_sec": manifest_age,
                "candidate_count": int(
                    controller_stats.get("candidate_count") or 0
                ),
                "last_error": str(
                    controller_stats.get("manifest_last_error") or ""
                )[:160],
            },
            "refresh": refresh_stats,
            "service": service_stats,
            "processor": self.processor.stats(),
            "telegram": self.delivery.stats(),
            "last_error_class": self._last_error_class,
        }

    def write_once(self) -> bool:
        try:
            locked_write_json(self.path, self._payload())
        except Exception as exc:
            self._last_error_class = type(exc).__name__
            return False
        self._last_error_class = ""
        return True

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._running = True
        self._stop.clear()
        self.write_once()
        self._thread = threading.Thread(
            target=self._run,
            name="altcoin-production-status",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_sec):
            self.write_once()

    def stop(self, timeout: float = 10.0) -> bool:
        self._running = False
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(max(0.0, float(timeout)))
        self.write_once()
        return not bool(thread is not None and thread.is_alive())


def _print_failure(reason: str, *, error_class: str = "") -> None:
    print(json.dumps({
        "module": "altcoin_contract_anomaly",
        "mode": "production",
        "status": "blocked",
        "reason": str(reason),
        "error_class": str(error_class),
    }, ensure_ascii=False))


def _write_blocked_status(path: str | Path, reason: str) -> None:
    try:
        locked_write_json(path, {
            "schema_version": PRODUCTION_RUNTIME_SCHEMA_VERSION,
            "module": "altcoin_contract_anomaly",
            "mode": "production",
            "updated_at": _utc_iso(),
            "running": False,
            "status": "blocked",
            "reason": str(reason)[:100],
            "manifest": {},
            "refresh": {},
            "service": {},
            "processor": {},
            "telegram": {},
        })
    except Exception:
        pass


def run_altcoin_production_service(
    settings: Settings,
    *,
    duration_sec: float = 0,
    real_send_requested: bool = False,
    gateway: TelegramGateway | None = None,
    scan_callable: Callable[..., Mapping[str, Any]] = scan_candidate_pool,
    service_runner: Callable[..., int] = run_realtime_market_service,
) -> int:
    """Run the sole market-stream connection with explicit production wiring."""

    process_lock: ProcessFileLock | None = None
    production: AltcoinAnomalyProductionConfig | None = None
    refresher: CandidateManifestRefreshWorker | None = None
    status_writer: ProductionStatusWriter | None = None
    processor: AltcoinProductionEventProcessor | None = None
    try:
        production = AltcoinAnomalyProductionConfig.from_settings(
            settings,
            real_send_requested=real_send_requested,
        )
        # Validate the unchanged P2 feature contract before swapping in the
        # longer production Manifest lifetime and independent observation WAL.
        AltcoinAnomalyConfig.from_settings(settings, realtime=True)
        if production.send_enabled != bool(real_send_requested):
            raise AltcoinAnomalyConfigError(
                "生产Telegram开关与CLI双重发送门不一致"
            )

        process_lock = ProcessFileLock(production.realtime_lock_path)
        if not process_lock.acquire():
            _print_failure("realtime_process_lock_busy")
            return 2

        runtime_settings = replace(
            settings,
            altcoin_contract_anomaly_manifest_max_age_sec=(
                production.manifest_max_age_sec
            ),
        )
        refresher = CandidateManifestRefreshWorker(
            runtime_settings,
            interval_sec=production.manifest_refresh_sec,
            retry_sec=production.manifest_retry_sec,
            max_manifest_age_sec=production.manifest_max_age_sec,
            scan_callable=scan_callable,
        )
        manifest_consumer = CandidateManifestConsumer(runtime_settings)
        preflight = manifest_consumer.poll(now_ts=time.time())
        if not str(preflight.get("status") or "").startswith("valid_"):
            # A cold start must obtain one complete Manifest before any
            # controller, state or WebSocket is constructed.  A warm restart
            # may reuse a still-valid atomic Manifest and refresh in parallel.
            refresher.refresh_now(
                timeout=max(120.0, production.manifest_retry_sec * 2.0)
            )
            preflight = manifest_consumer.poll(now_ts=time.time())
        else:
            refresher.start()
        if not str(preflight.get("status") or "").startswith("valid_"):
            _write_blocked_status(
                production.status_path,
                "candidate_manifest_unavailable",
            )
            _print_failure("candidate_manifest_unavailable")
            return 3

        telegram_gateway = gateway or TelegramGateway(
            runtime_settings,
            JsonStore(runtime_settings.data_dir),
        )
        delivery = ProductionTelegramDelivery(
            telegram_gateway,
            real_send=real_send_requested,
            daily_limit=production.daily_limit,
            dedup_cooldown_sec=max(
                production.cooldown_sec,
                int(runtime_settings.tg_push_history_retention_days) * 86_400,
            ),
        )

        def candidate_lookup(symbol: str) -> Mapping[str, Any] | None:
            manifest = manifest_consumer.last_valid
            if manifest is None:
                return None
            row = manifest.candidates.get(str(symbol).upper())
            return dict(row) if isinstance(row, Mapping) else None

        processor = AltcoinProductionEventProcessor(
            state_path=(
                production.state_path
                if real_send_requested
                else production.preview_state_path
            ),
            outbox_path=(
                production.outbox_path
                if real_send_requested
                else production.preview_outbox_path
            ),
            formatter=render_production_event_group,
            delivery=delivery,
            candidate_lookup=candidate_lookup,
            cooldown_sec=production.cooldown_sec,
            max_messages_per_hour=production.hourly_limit,
            queue_size=production.queue_size,
        )
        observation_state = ProductionObservationState(
            production.observation_state_path,
            production.observation_event_path,
            processor=processor,
        )
        feature_store = RealtimeFeatureStore(runtime_settings.realtime_features_db_path)
        controller = AltcoinRealtimeController(
            runtime_settings,
            feature_store=feature_store,
            observation_state=observation_state,
            manifest_consumer=manifest_consumer,
            oi_budget_window_sec=production.oi_budget_window_sec,
        )
        service = BinanceRealtimeMarketService(
            runtime_settings,
            store=feature_store,
            realtime_controller=controller,
            event_sink=processor,
        )
        status_writer = ProductionStatusWriter(
            production.status_path,
            interval_sec=production.status_interval_sec,
            service=service,
            controller=controller,
            refresher=refresher,
            processor=processor,
            delivery=delivery,
            process_lock=process_lock,
        )
        status_writer.start()
        service_code = service_runner(
            runtime_settings,
            duration_sec=max(0.0, float(duration_sec or 0.0)),
            service=service,
            process_lock=process_lock,
        )
        status_ok = status_writer.stop()
        status_writer = None
        refresh_ok = refresher.stop()
        refresher = None
        processor_ok = processor.stop()
        processor = None
        return int(service_code) if status_ok and refresh_ok and processor_ok else 1
    except AltcoinAnomalyConfigError as exc:
        if production is not None:
            _write_blocked_status(production.status_path, "configuration_error")
        _print_failure("configuration_error", error_class=type(exc).__name__)
        return 2
    except Exception as exc:
        if production is not None:
            _write_blocked_status(production.status_path, "internal_error")
        _print_failure("internal_error", error_class=type(exc).__name__)
        return 1
    finally:
        if status_writer is not None:
            status_writer.stop()
        if refresher is not None:
            refresher.stop()
        if processor is not None:
            processor.stop()
        if process_lock is not None:
            process_lock.release()


__all__ = [
    "PRODUCTION_RUNTIME_SCHEMA_VERSION",
    "ProductionObservationState",
    "ProductionStatusWriter",
    "ProductionTelegramDelivery",
    "run_altcoin_production_service",
]
