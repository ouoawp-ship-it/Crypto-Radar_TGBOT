from __future__ import annotations

import json
import time
from dataclasses import replace
from hashlib import sha256
from typing import Callable, Mapping

from .aggregator import build_rolling_snapshots
from .arkham_models import ArkhamProcessedEvent, ArkhamRawEvent
from .arkham_normalizer import (
    ARKHAM_CHAIN_IDS,
    ArkhamNormalizationError,
    normalize_arkham_transfer,
)
from .collectors.arkham_rest import ArkhamRestClient
from .config import OnchainSettings
from .constants import P3_2A_SEVERITY_VERSION
from .db import OnchainStore
from .detector import detect_flows, detect_rolling_flows
from .models import (
    ClassifiedFlow,
    DetectedFlow,
    DetectedRollingFlow,
    OnchainAlert,
    RollingFlowSnapshot,
)
from .notifier import OnchainNotifier
from .scorer import score_detection, score_rolling_detection
from .token_policy import ConfiguredTokenPolicy, STABLECOIN


class ArkhamConfigurationError(ValueError):
    pass


class ArkhamPageProcessingError(RuntimeError):
    pass


def _chain_name(chain_id: int) -> str:
    for name, known_id in ARKHAM_CHAIN_IDS.items():
        if known_id == chain_id:
            return name
    return "arkham"


def _failed_raw_event(
    payload: Mapping[str, object],
    *,
    received_at: int,
    error_type: str,
) -> ArkhamRawEvent:
    payload_json = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    payload_hash = sha256(payload_json.encode("utf-8")).hexdigest()
    nested = payload.get("transfer")
    transfer = nested if isinstance(nested, dict) else payload
    raw_id = transfer.get("id")
    transfer_id = (
        raw_id.strip()
        if isinstance(raw_id, str) and raw_id.strip()
        else f"invalid:{payload_hash}"
    )
    return ArkhamRawEvent(
        transfer_id=transfer_id,
        payload_json=payload_json,
        payload_hash=payload_hash,
        received_via="rest",
        received_at=received_at,
        processed_status="failed",
        error_type=error_type,
    )


class ArkhamRestRuntime:
    def __init__(
        self,
        settings: OnchainSettings,
        *,
        client: ArkhamRestClient | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self._client_instance = client
        self.clock = clock
        self.sleep = sleep

    def _enabled_payload(self, command: str) -> dict[str, object] | None:
        if not self.settings.enable:
            return {
                "command": command,
                "status": "disabled",
                "reason": "ONCHAIN_ENABLE=false",
                "network_activity": False,
                "database_writes": False,
                "telegram_calls": False,
            }
        if not self.settings.arkham_enable:
            return {
                "command": command,
                "status": "disabled",
                "reason": "ARKHAM_ENABLE=false",
                "network_activity": False,
                "database_writes": False,
                "telegram_calls": False,
            }
        return None

    def _validate_enabled(self) -> None:
        self.settings.validate()
        if self.settings.source_mode not in {"arkham", "hybrid"}:
            raise ArkhamConfigurationError(
                "Arkham commands require ONCHAIN_SOURCE_MODE=arkham or hybrid"
            )
        if not self.settings.arkham_rest_enable:
            raise ArkhamConfigurationError("ARKHAM_REST_ENABLE is false")
        if self.settings.arkham_ws_enable:
            raise ArkhamConfigurationError(
                "P3.2A does not create Arkham WebSocket sessions"
            )
        if self.settings.real_send:
            raise ArkhamConfigurationError(
                "P3.2A requires ONCHAIN_REAL_SEND=false"
            )

    def _client(self) -> ArkhamRestClient:
        if self._client_instance is None:
            self._client_instance = ArkhamRestClient(
                self.settings.arkham_api_base_url,
                self.settings.arkham_api_key,
                timeout_sec=float(self.settings.rpc_timeout_sec),
                retry=min(self.settings.rpc_retry, 3),
                backoff_sec=float(self.settings.rpc_backoff_sec),
                sleep=self.sleep,
            )
        return self._client_instance

    def capability_check(self) -> dict[str, object]:
        disabled = self._enabled_payload("arkham-check")
        if disabled is not None:
            return disabled
        self._validate_enabled()
        return self._client().capability_check(
            global_usd_gte=self.settings.arkham_global_usd_gte,
            chains=self.settings.arkham_chains,
            limit=self.settings.arkham_rest_limit,
        )

    def process_once(self) -> dict[str, object]:
        disabled = self._enabled_payload("arkham-once")
        if disabled is not None:
            return disabled
        self._validate_enabled()
        store = OnchainStore(self.settings)
        store.migrate()
        metrics: dict[str, object] = {
            "command": "arkham-once",
            "status": "running",
            "source": "arkham",
            "streams_completed": 0,
            "pages_processed": 0,
            "transfers_received": 0,
            "unique_inserted_events": 0,
            "duplicate_events": 0,
            "unpriced_events": 0,
            "non_directional_events": 0,
            "policy_suppressed_events": 0,
            "alerts_generated": 0,
            "telegram_dry_run_count": 0,
            "telegram_delivery_failure_count": 0,
            "real_telegram_requests": 0,
            "websocket_sessions_created": 0,
        }
        for stream_name, side in (
            ("arkham_cex_inflow", "to"),
            ("arkham_cex_outflow", "from"),
        ):
            self._reconcile_stream(
                store,
                stream_name=stream_name,
                side=side,
                metrics=metrics,
            )
            metrics["streams_completed"] = (
                int(metrics["streams_completed"]) + 1
            )
        alerts = self._evaluate(store)
        metrics["alerts_generated"] = len(alerts)
        dry_run, failed = self._deliver(store)
        metrics["telegram_dry_run_count"] = dry_run
        metrics["telegram_delivery_failure_count"] = failed
        metrics["status"] = "ok"
        metrics["sqlite_integrity"] = store.integrity_check()
        return metrics

    def _selector(self) -> str:
        if self.settings.arkham_cex_filter_mode == "type_cex":
            return "type:cex"
        if not self.settings.arkham_cex_entity_ids:
            raise ArkhamConfigurationError(
                "explicit Arkham CEX entity IDs are not configured"
            )
        return ",".join(self.settings.arkham_cex_entity_ids)

    def _reconcile_stream(
        self,
        store: OnchainStore,
        *,
        stream_name: str,
        side: str,
        metrics: dict[str, object],
    ) -> None:
        state = store.arkham_sync_state(stream_name)
        query_started_ms = int(self.clock() * 1000)
        previous_timestamp = (
            state.last_timestamp_ms if state is not None else query_started_ms
        )
        previous_event_id = (
            state.last_event_id if state is not None else ""
        )
        time_gte = max(
            0,
            previous_timestamp
            - (self.settings.arkham_rest_overlap_sec * 1000),
        )
        limit = self.settings.arkham_rest_limit
        cursor_timestamp = previous_timestamp
        cursor_event_id = previous_event_id
        policy = ConfiguredTokenPolicy(self.settings)
        for page_index in range(self.settings.arkham_rest_max_pages):
            params: dict[str, object] = {
                side: self._selector(),
                "timeGte": str(time_gte),
                "usdGte": str(self.settings.arkham_global_usd_gte),
                "sortKey": "time",
                "sortDir": "asc",
                "limit": limit,
                "offset": page_index * limit,
            }
            if self.settings.arkham_chains:
                params["chains"] = ",".join(
                    self.settings.arkham_chains
                )
            payloads, count = self._client().transfers(params)
            metrics["transfers_received"] = (
                int(metrics["transfers_received"]) + len(payloads)
            )
            received_at = int(self.clock())
            events: list[ArkhamProcessedEvent] = []
            try:
                for payload in payloads:
                    events.append(
                        normalize_arkham_transfer(
                            payload,
                            token_policy=policy,
                            received_at=received_at,
                        )
                    )
            except ArkhamNormalizationError as exc:
                failed_raw = [
                    _failed_raw_event(
                        payload,
                        received_at=received_at,
                        error_type=type(exc).__name__,
                    )
                    for payload in payloads
                ]
                store.record_arkham_page_failure(
                    failed_raw,
                    stream_name=stream_name,
                    error_type=type(exc).__name__,
                )
                raise ArkhamPageProcessingError(
                    "Arkham page normalization failed"
                ) from exc
            if events:
                latest = max(
                    events,
                    key=lambda event: (
                        event.timestamp_ms,
                        event.transfer.event_id,
                    ),
                )
                cursor_timestamp = max(
                    cursor_timestamp, latest.timestamp_ms
                )
                cursor_event_id = latest.transfer.event_id
            elif page_index == 0:
                cursor_timestamp = max(
                    cursor_timestamp, query_started_ms
                )
            inserted, duplicates = store.persist_arkham_page(
                events,
                stream_name=stream_name,
                cursor_timestamp_ms=cursor_timestamp,
                last_event_id=cursor_event_id,
                last_success_at=int(self.clock()),
            )
            metrics["pages_processed"] = (
                int(metrics["pages_processed"]) + 1
            )
            metrics["unique_inserted_events"] = (
                int(metrics["unique_inserted_events"]) + inserted
            )
            metrics["duplicate_events"] = (
                int(metrics["duplicate_events"]) + duplicates
            )
            for event in events:
                status = event.raw.processed_status
                if status == "unpriced":
                    metrics["unpriced_events"] = (
                        int(metrics["unpriced_events"]) + 1
                    )
                elif status == "non_directional":
                    metrics["non_directional_events"] = (
                        int(metrics["non_directional_events"]) + 1
                    )
                elif status == "policy_suppressed":
                    metrics["policy_suppressed_events"] = (
                        int(metrics["policy_suppressed_events"]) + 1
                    )
            if not payloads or len(payloads) < limit:
                break
            if (page_index + 1) * limit >= count:
                break

    def _evaluate(self, store: OnchainStore) -> list[OnchainAlert]:
        now = int(self.clock())
        bucket = self.settings.rolling_evaluation_bucket_sec
        evaluation_time = now - (now % bucket)
        flows = [
            flow
            for flow in store.source_flows_since(
                "arkham", evaluation_time - 3600
            )
            if flow.token_policy in {"normal_token", "stablecoin"}
            and flow.attribution_quality == "arkham_entity"
        ]
        if not flows:
            return []
        metadata = store.metadata_map()
        detection_settings = replace(
            self.settings, min_label_confidence=0.0
        )
        single_detections = detect_flows(
            flows, [], metadata, detection_settings
        )
        by_event = {flow.event_id: flow for flow in flows}
        alerts: list[OnchainAlert] = []
        for detected in single_detections:
            if not detected.source_event_ids:
                continue
            flow = by_event[detected.source_event_ids[0]]
            if now - flow.block_time > self.settings.alert_max_event_age_sec:
                continue
            alert = self._arkham_single_alert(detected, flow)
            store.persist_alert_for_delivery(alert, created_at=now)
            alerts.append(alert)

        snapshots = build_rolling_snapshots(
            flows,
            evaluation_time=evaluation_time,
            evaluation_block=0,
            min_label_confidence=0.0,
            price_max_age_sec=self.settings.price_max_age_sec,
            quotes=None,
        )
        for snapshot in snapshots:
            store.upsert_snapshot(snapshot)
        for detected in detect_rolling_flows(
            snapshots, metadata, detection_settings
        ):
            alert = self._arkham_rolling_alert(detected.snapshot, detected)
            store.persist_alert_for_delivery(alert, created_at=now)
            alerts.append(alert)
        return alerts

    def _arkham_single_alert(
        self, detected: DetectedFlow, flow: ClassifiedFlow
    ) -> OnchainAlert:
        alert = score_detection(detected)
        alert = replace(
            alert,
            alert_key=(
                f"arkham:{flow.event_id}:single:"
                f"{P3_2A_SEVERITY_VERSION}"
            ),
            severity_version=P3_2A_SEVERITY_VERSION,
            gross_inflow_usd=(
                flow.amount_usd
                if flow.flow_type == "inflow"
                else None
            ),
            gross_outflow_usd=(
                flow.amount_usd
                if flow.flow_type == "outflow"
                else None
            ),
            evaluation_block=flow.block_number,
            price_source=flow.price_source,
            price_observed_at=flow.price_observed_at,
            chain_name=_chain_name(flow.chain_id),
            notification_key=(
                f"arkham:{flow.chain_id}:{flow.token_address}:"
                f"{flow.flow_type}:single"
            ),
            source="arkham",
            attribution_quality=flow.attribution_quality,
            token_policy=flow.token_policy,
            signal_context=flow.signal_context,
        )
        return self._apply_policy(alert)

    def _arkham_rolling_alert(
        self,
        snapshot: RollingFlowSnapshot,
        detected: DetectedRollingFlow,
    ) -> OnchainAlert:
        alert = score_rolling_detection(detected)
        alert = replace(
            alert,
            alert_key=(
                f"arkham:{snapshot.snapshot_key}:"
                f"{snapshot.direction}:{P3_2A_SEVERITY_VERSION}"
            ),
            severity_version=P3_2A_SEVERITY_VERSION,
            chain_name=_chain_name(snapshot.chain_id),
            notification_key=(
                f"arkham:{snapshot.chain_id}:{snapshot.token_address}:"
                f"{snapshot.direction}:{snapshot.duration_sec}"
            ),
            source="arkham",
            attribution_quality=snapshot.attribution_quality,
            token_policy=snapshot.token_policy,
            signal_context=snapshot.signal_context,
        )
        return self._apply_policy(alert)

    @staticmethod
    def _apply_policy(alert: OnchainAlert) -> OnchainAlert:
        if alert.token_policy != STABLECOIN:
            return alert
        return replace(
            alert,
            score=0,
            horizon="market_liquidity_context",
            confidence="context",
            reasons=(
                "Stablecoin exchange flow is market liquidity context.",
                "It is not a forecast of the stablecoin price.",
            ),
            signal_context="market_liquidity_context",
        )

    def _deliver(self, store: OnchainStore) -> tuple[int, int]:
        notifier = OnchainNotifier(self.settings, store)
        dry_run = 0
        failed = 0
        for alert in store.pending_delivery_alerts(source="arkham"):
            try:
                result = notifier.notify(
                    alert,
                    send=False,
                    confirm_real_send=False,
                    attempted_at=int(self.clock()),
                )
            except Exception:
                failed += 1
                continue
            if result.status == "dry_run":
                dry_run += 1
            elif result.status == "failed":
                failed += 1
        return dry_run, failed
