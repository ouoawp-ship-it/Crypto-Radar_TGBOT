from __future__ import annotations

import time
from dataclasses import replace
from decimal import Decimal
from typing import Callable, Sequence

from .aggregator import build_rolling_snapshots
from .classifier import classify_transfer
from .collectors.evm_http import (
    BaseHttpCollector,
    FinalizedRangeConsistencyError,
    HASH_RE,
    JsonRpcClient,
    LogValidationError,
    RpcError,
    RpcResponseError,
    normalize_transfer_log,
    parse_hex_quantity,
)
from .collectors.evm_ws import WssError, WssHeadTrigger
from .config import OnchainSettings
from .constants import BASE_CHAIN_ID
from .db import OnchainStore
from .detector import detect_flows, detect_rolling_flows
from .health import DEFAULT_RUNTIME_STATUS, write_runtime_status
from .labels import LabelRegistry, load_labels_csv, validate_live_labels
from .models import ClassifiedFlow, ProcessedBlock, TokenMetadata
from .notifier import OnchainNotifier
from .price_oracle import (
    CachedPriceService,
    PriceProvider,
    build_price_provider,
)
from .scorer import (
    score_live_single_detection,
    score_rolling_detection,
)
from .token_metadata import TokenMetadataResolver


class ReorgManualInterventionRequired(RuntimeError):
    pass


class LiveConfigurationError(ValueError):
    pass


class BaseOnchainRuntime:
    def __init__(
        self,
        settings: OnchainSettings,
        *,
        rpc: JsonRpcClient | None = None,
        http_collector: BaseHttpCollector | None = None,
        wss_trigger: WssHeadTrigger | None = None,
        price_provider: PriceProvider | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.settings = settings
        self.clock = clock
        self.sleep = sleep
        self._rpc = rpc
        self._http_collector = http_collector
        self._wss_trigger = wss_trigger
        self._price_provider = price_provider
        self.metrics = dict(DEFAULT_RUNTIME_STATUS)
        self.metrics.update(
            {
                "http_provider_configured": bool(
                    settings.base_http_rpc_url
                ),
                "wss_provider_configured": bool(
                    settings.base_wss_rpc_url
                ),
            }
        )

    def _client(self) -> JsonRpcClient:
        if self._rpc is None:
            self._rpc = JsonRpcClient(
                self.settings.base_http_rpc_url,
                timeout_sec=float(self.settings.rpc_timeout_sec),
                retry=self.settings.rpc_retry,
                backoff_sec=float(self.settings.rpc_backoff_sec),
                sleep=self.sleep,
                rate_limit_per_second=(
                    self.settings.rpc_rate_limit_per_second
                ),
            )
        return self._rpc

    def _collector(self) -> BaseHttpCollector:
        if self._http_collector is None:
            self._http_collector = BaseHttpCollector(
                self._client(), self.settings
            )
        return self._http_collector

    def _trigger(self) -> WssHeadTrigger:
        if self._wss_trigger is None:
            self._wss_trigger = WssHeadTrigger(self.settings)
        return self._wss_trigger

    def provider_check(self) -> dict[str, object]:
        self.settings.validate()
        if not self.settings.base_http_rpc_url:
            raise LiveConfigurationError("Base HTTP RPC is not configured")
        return self._collector().provider_check()

    def record_failure(self, error: Exception, *, mode: str) -> None:
        self.metrics.update(
            {
                "mode": mode,
                "status": "failed",
                "last_error_type": type(error).__name__,
                "rpc_error_count": (
                    self._rpc.error_count
                    if self._rpc is not None
                    else self.metrics["rpc_error_count"]
                ),
            }
        )
        write_runtime_status(self.settings.runtime_status_path, self.metrics)

    def _live_labels(self):
        labels = load_labels_csv(self.settings.labels_path)
        active = validate_live_labels(
            labels,
            min_confidence=self.settings.min_label_confidence,
            chain_id=BASE_CHAIN_ID,
            timestamp=int(self.clock()),
        )
        return labels, active

    def process_once(
        self,
        *,
        send: bool = False,
        confirm_real_send: bool = False,
        mode: str = "once",
    ) -> dict[str, object]:
        self.settings.validate()
        if not self.settings.enable:
            raise LiveConfigurationError("ONCHAIN_ENABLE is false")
        if not self.settings.base_enable:
            raise LiveConfigurationError("ONCHAIN_BASE_ENABLE is false")
        if not self.settings.base_http_rpc_url:
            raise LiveConfigurationError("Base HTTP RPC is not configured")
        labels, active_labels = self._live_labels()
        registry = LabelRegistry(labels)
        cex_addresses = [label.address for label in active_labels]
        store = OnchainStore(self.settings)
        store.migrate()
        store.replace_labels(labels)
        client = self._client()
        chain_id = client.chain_id()
        if chain_id != BASE_CHAIN_ID:
            raise RpcResponseError("configured provider chain ID is not Base")
        head = client.block_number()
        target = max(0, head - self.settings.base_confirmation_depth)
        self.metrics.update(
            {
                "mode": mode,
                "status": "running",
                "latest_head": head,
                "target_finalized": target,
            }
        )
        self._reconcile_reorg(store, client)
        attempted_deliveries: set[str] = set()
        self._drain_durable_evaluation(
            store,
            client,
            registry,
            send=send,
            confirm_real_send=confirm_real_send,
            attempted_deliveries=attempted_deliveries,
        )
        cursor = store.cursor(BASE_CHAIN_ID)
        if cursor is None:
            start = max(
                0,
                target - self.settings.base_bootstrap_lookback_blocks,
            )
        else:
            start = cursor.last_finalized_block + 1
        if start <= target:
            range_start = start
            while range_start <= target:
                range_end = min(
                    target,
                    range_start + self.settings.rpc_max_block_range - 1,
                )
                self._process_range(
                    store=store,
                    registry=registry,
                    cex_addresses=cex_addresses,
                    start_block=range_start,
                    end_block=range_end,
                    last_seen_head=head,
                )
                self._drain_durable_evaluation(
                    store,
                    client,
                    registry,
                    send=send,
                    confirm_real_send=confirm_real_send,
                    attempted_deliveries=attempted_deliveries,
                )
                range_start = range_end + 1
        cursor = store.cursor(BASE_CHAIN_ID)
        if cursor is None:
            raise RuntimeError("finalized cursor was not initialized")
        store.update_head_status(
            BASE_CHAIN_ID,
            last_seen_head=head,
            provider_status="ok",
            updated_at=int(self.clock()),
        )
        cursor = store.cursor(BASE_CHAIN_ID)
        if cursor is None:
            raise RuntimeError("finalized cursor disappeared")
        self._drain_durable_evaluation(
            store,
            client,
            registry,
            send=send,
            confirm_real_send=confirm_real_send,
            attempted_deliveries=attempted_deliveries,
        )
        now = int(self.clock())
        self.metrics.update(
            {
                "status": "ok",
                "cursor_block": cursor.last_finalized_block,
                "cursor_lag_blocks": max(
                    0, target - cursor.last_finalized_block
                ),
                "last_success_at": now,
                "last_error_type": "",
                "rpc_error_count": client.error_count,
            }
        )
        write_runtime_status(self.settings.runtime_status_path, self.metrics)
        return dict(self.metrics)

    def _process_range(
        self,
        *,
        store: OnchainStore,
        registry: LabelRegistry,
        cex_addresses: Sequence[str],
        start_block: int,
        end_block: int,
        last_seen_head: int,
    ) -> list[ClassifiedFlow]:
        client = self._client()
        logs = self._collector().fetch_cex_logs(
            start_block, end_block, cex_addresses
        )
        blocks: list[ProcessedBlock] = []
        block_times: dict[int, int] = {}
        for block_number in range(start_block, end_block + 1):
            block = client.get_block(block_number)
            block_hash = str(block.get("hash") or "")
            if not HASH_RE.fullmatch(block_hash):
                raise RpcResponseError("finalized block hash is malformed")
            timestamp = parse_hex_quantity(
                block.get("timestamp"), "block timestamp"
            )
            block_times[block_number] = timestamp
            blocks.append(
                ProcessedBlock(
                    chain_id=BASE_CHAIN_ID,
                    block_number=block_number,
                    block_hash=block_hash.lower(),
                    block_time=timestamp,
                    processed_at=int(self.clock()),
                )
            )
        transfers_by_id = {}
        for log in logs:
            try:
                block_number = parse_hex_quantity(
                    log.get("blockNumber"), "log block number"
                )
                if not start_block <= block_number <= end_block:
                    raise FinalizedRangeConsistencyError(
                        "log block number is outside requested range"
                    )
                header = blocks[block_number - start_block]
                log_block_hash = str(log.get("blockHash") or "")
                if log_block_hash.lower() != header.block_hash.lower():
                    raise FinalizedRangeConsistencyError(
                        "log block hash does not match canonical header"
                    )
                transfer = normalize_transfer_log(
                    log,
                    block_time=block_times[block_number],
                )
            except FinalizedRangeConsistencyError:
                raise
            except (KeyError, IndexError, LogValidationError, RpcError) as exc:
                raise FinalizedRangeConsistencyError(
                    "finalized log failed canonical validation"
                ) from exc
            existing = transfers_by_id.get(transfer.event_id)
            if existing is not None and existing != transfer:
                raise FinalizedRangeConsistencyError(
                    "duplicate event key has conflicting canonical contents"
                )
            transfers_by_id[transfer.event_id] = transfer
        transfers = list(transfers_by_id.values())
        resolver = TokenMetadataResolver(
            client, store, clock=self.clock
        )
        metadata: dict[str, TokenMetadata] = {}
        for token_address in sorted(
            {
                transfer.token_address
                for transfer in transfers
                if not transfer.removed
            }
        ):
            metadata[token_address] = resolver.resolve(
                BASE_CHAIN_ID, token_address
            )
        provider = (
            self._price_provider
            if self._price_provider is not None
            else build_price_provider(self.settings)
        )
        price_service = CachedPriceService(
            self.settings,
            store,
            provider,
            clock=self.clock,
        )
        quotes = price_service.quotes(
            BASE_CHAIN_ID,
            [
                address
                for address, token in metadata.items()
                if token.metadata_status == "verified_erc20"
            ],
        )
        flows: list[ClassifiedFlow] = []
        priced = 0
        unpriced = 0
        for transfer in transfers:
            if transfer.removed:
                continue
            token = metadata.get(transfer.token_address)
            quote = quotes.get(transfer.token_address)
            if token is not None and quote is not None:
                token = replace(
                    token,
                    price_usd=quote.price_usd,
                    volume_24h_usd=quote.volume_24h_usd,
                    price_source=quote.source,
                    price_observed_at=quote.freshness_timestamp,
                )
                store.upsert_token_metadata(token)
                priced += 1
            elif token is not None:
                token = replace(
                    token,
                    price_usd=None,
                    volume_24h_usd=None,
                    price_source="",
                    price_observed_at=0,
                )
                unpriced += 1
            flows.append(classify_transfer(transfer, token, registry))
        inserted, duplicates = store.commit_finalized_range(
            blocks=blocks,
            transfers=transfers,
            flows=flows,
            last_seen_head=last_seen_head,
            provider_status="ok",
            updated_at=int(self.clock()),
        )
        self.metrics["logs_received"] = int(
            self.metrics["logs_received"]
        ) + len(logs)
        self.metrics["duplicate_count"] = int(
            self.metrics["duplicate_count"]
        ) + duplicates
        self.metrics["priced_count"] = int(
            self.metrics["priced_count"]
        ) + priced
        self.metrics["unpriced_count"] = int(
            self.metrics["unpriced_count"]
        ) + unpriced
        self.metrics["unique_inserted_count"] = int(
            self.metrics.get("unique_inserted_count", 0)
        ) + inserted
        return flows

    def _reconcile_reorg(
        self, store: OnchainStore, client: JsonRpcClient
    ) -> None:
        cursor = store.cursor(BASE_CHAIN_ID)
        if cursor is None:
            return
        current = client.get_block(cursor.last_finalized_block)
        current_hash = str(current.get("hash") or "").lower()
        if current_hash == cursor.finalized_block_hash.lower():
            return
        candidates = store.processed_blocks_desc(
            BASE_CHAIN_ID,
            cursor.last_finalized_block,
            self.settings.base_reorg_lookback_blocks + 1,
        )
        for candidate in candidates[1:]:
            provider_block = client.get_block(candidate.block_number)
            provider_hash = str(provider_block.get("hash") or "").lower()
            if provider_hash == candidate.block_hash.lower():
                orphaned = store.rollback_to_block(
                    BASE_CHAIN_ID,
                    candidate.block_number,
                    int(self.clock()),
                )
                self.metrics["orphan_count"] = int(
                    self.metrics["orphan_count"]
                ) + orphaned
                return
        self.metrics["status"] = "reorg_manual_intervention_required"
        self.metrics["last_error_type"] = (
            "ReorgManualInterventionRequired"
        )
        write_runtime_status(self.settings.runtime_status_path, self.metrics)
        raise ReorgManualInterventionRequired(
            "no common ancestor within configured reorg lookback"
        )

    def _drain_durable_evaluation(
        self,
        store: OnchainStore,
        client: JsonRpcClient,
        registry: LabelRegistry,
        *,
        send: bool,
        confirm_real_send: bool,
        attempted_deliveries: set[str] | None = None,
    ) -> None:
        boundary = store.durable_evaluation_boundary(BASE_CHAIN_ID)
        if boundary is None:
            return
        target_block = boundary.block_number
        target_time = boundary.block_time
        bucket = self.settings.rolling_evaluation_bucket_sec
        evaluation_time = target_time - (target_time % bucket)
        provider = (
            self._price_provider
            if self._price_provider is not None
            else build_price_provider(self.settings)
        )
        price_service = CachedPriceService(
            self.settings,
            store,
            provider,
            clock=self.clock,
        )
        self._repair_pending_metadata(
            store,
            client,
            registry,
        )
        metadata = store.metadata_map()
        single_alerts = self._evaluate_pending_single_events(
            store,
            price_service,
            metadata,
        )
        rolling_flows = store.finalized_flows_since(
            BASE_CHAIN_ID, evaluation_time - 3600
        )
        active_tokens = sorted(
            {
                flow.token_address
                for flow in rolling_flows
                if flow.amount is not None
            }
        )
        rolling_quotes = price_service.quotes(
            BASE_CHAIN_ID,
            active_tokens,
            force_refresh=True,
        )
        for address, quote in rolling_quotes.items():
            token = metadata.get((BASE_CHAIN_ID, address))
            if token is None:
                continue
            token = replace(
                token,
                price_usd=quote.price_usd,
                volume_24h_usd=quote.volume_24h_usd,
                price_source=quote.source,
                price_observed_at=quote.freshness_timestamp,
            )
            store.upsert_token_metadata(token)
            metadata[(BASE_CHAIN_ID, address)] = token
        snapshots = build_rolling_snapshots(
            rolling_flows,
            evaluation_time=evaluation_time,
            evaluation_block=target_block,
            min_label_confidence=self.settings.min_label_confidence,
            price_max_age_sec=self.settings.price_max_age_sec,
            quotes={
                (BASE_CHAIN_ID, address): quote
                for address, quote in rolling_quotes.items()
            },
        )
        for snapshot in snapshots:
            store.upsert_snapshot(snapshot)
        rolling_alerts = [
            score_rolling_detection(item)
            for item in detect_rolling_flows(
                snapshots, metadata, self.settings
            )
        ]
        now = int(self.clock())
        for alert in rolling_alerts:
            store.persist_alert_for_delivery(alert, created_at=now)
        self._deliver_pending(
            store,
            send=send,
            confirm_real_send=confirm_real_send,
            attempted_deliveries=attempted_deliveries,
        )
        self.metrics["alerts_generated"] = int(
            self.metrics["alerts_generated"]
        ) + len(single_alerts) + len(rolling_alerts)

    def _repair_pending_metadata(
        self,
        store: OnchainStore,
        client: JsonRpcClient,
        registry: LabelRegistry,
    ) -> None:
        pending = store.pending_metadata_flows(BASE_CHAIN_ID)
        if not pending:
            return
        now = int(self.clock())
        resolver = TokenMetadataResolver(client, store, clock=self.clock)
        for flow in pending:
            transfer = store.finalized_transfer(flow.event_id)
            if transfer is None:
                continue
            token = resolver.resolve(flow.chain_id, flow.token_address)
            if token.metadata_status in {"rpc_failed", "incomplete"}:
                store.persist_single_decision(
                    event_id=flow.event_id,
                    decision_status="pending_metadata",
                    attempted_at=now,
                    decision_reason=(
                        f"metadata_{token.metadata_status}"
                    ),
                )
                continue
            if token.metadata_status not in {
                "verified",
                "verified_erc20",
            }:
                store.persist_single_decision(
                    event_id=flow.event_id,
                    decision_status="suppressed",
                    attempted_at=now,
                    decision_reason=(
                        f"metadata_{token.metadata_status}"
                    ),
                )
                continue
            repaired = classify_transfer(transfer, token, registry)
            store.update_flow_valuation(repaired)
            if repaired.flow_type not in {"inflow", "outflow"}:
                store.persist_single_decision(
                    event_id=flow.event_id,
                    decision_status="suppressed",
                    attempted_at=now,
                    decision_reason=(
                        f"classification_{repaired.flow_type}"
                    ),
                )

    def _evaluate_pending_single_events(
        self,
        store: OnchainStore,
        price_service: CachedPriceService,
        metadata: dict[tuple[int, str], TokenMetadata],
    ) -> list[object]:
        pending = store.pending_single_flows(BASE_CHAIN_ID)
        if not pending:
            return []
        now = int(self.clock())
        addresses = sorted({flow.token_address for flow in pending})
        quotes = price_service.quotes(
            BASE_CHAIN_ID,
            addresses,
            force_refresh=True,
        )
        alerts = []
        for flow in pending:
            if now - flow.block_time > self.settings.alert_max_event_age_sec:
                store.persist_single_decision(
                    event_id=flow.event_id,
                    decision_status="suppressed",
                    attempted_at=now,
                    decision_reason="catchup_suppressed",
                    catchup_suppression_reason="event_too_old",
                )
                continue
            token = metadata.get((flow.chain_id, flow.token_address))
            if (
                token is None
                or token.metadata_status
                not in {"verified", "verified_erc20"}
                or flow.amount is None
                or flow.label_confidence
                < self.settings.min_label_confidence
            ):
                store.persist_single_decision(
                    event_id=flow.event_id,
                    decision_status="suppressed",
                    attempted_at=now,
                    decision_reason="ineligible",
                )
                continue
            quote = quotes.get(flow.token_address)
            if quote is None:
                store.persist_single_decision(
                    event_id=flow.event_id,
                    decision_status="pending_price",
                    attempted_at=now,
                    decision_reason="fresh_price_unavailable",
                )
                continue
            valued = replace(
                flow,
                amount_usd=flow.amount * quote.price_usd,
                price_status="available",
                price_source=quote.source,
                price_observed_at=quote.freshness_timestamp,
            )
            store.update_flow_valuation(valued)
            token = replace(
                token,
                price_usd=quote.price_usd,
                volume_24h_usd=quote.volume_24h_usd,
                price_source=quote.source,
                price_observed_at=quote.freshness_timestamp,
            )
            store.upsert_token_metadata(token)
            metadata[(flow.chain_id, flow.token_address)] = token
            detections = detect_flows(
                [valued], [], metadata, self.settings
            )
            if not detections:
                store.persist_single_decision(
                    event_id=flow.event_id,
                    decision_status="suppressed",
                    attempted_at=now,
                    decision_reason="below_threshold",
                )
                continue
            alert = score_live_single_detection(detections[0], valued)
            store.persist_single_decision(
                event_id=flow.event_id,
                decision_status="evaluated",
                attempted_at=now,
                decision_reason="alert_created",
                alert=alert,
            )
            alerts.append(alert)
        return alerts

    def _deliver_pending(
        self,
        store: OnchainStore,
        *,
        send: bool,
        confirm_real_send: bool,
        attempted_deliveries: set[str] | None = None,
    ) -> None:
        notifier = OnchainNotifier(self.settings, store)
        for alert in store.pending_delivery_alerts():
            if (
                attempted_deliveries is not None
                and alert.alert_key in attempted_deliveries
            ):
                continue
            if attempted_deliveries is not None:
                attempted_deliveries.add(alert.alert_key)
            now = int(self.clock())
            if store.delivery_in_cooldown(
                alert.notification_key or alert.alert_key,
                now=now,
                cooldown_sec=self.settings.alert_cooldown_sec,
                excluding_alert_key=alert.alert_key,
            ):
                store.record_delivery(
                    alert.alert_key,
                    status="cooldown_suppressed",
                    sent=False,
                    reason="onchain_notification_cooldown",
                    attempted_at=now,
                )
                continue
            try:
                result = notifier.notify(
                    alert,
                    send=send,
                    confirm_real_send=confirm_real_send,
                    attempted_at=now,
                )
            except Exception:
                self.metrics["telegram_delivery_failure_count"] = int(
                    self.metrics["telegram_delivery_failure_count"]
                ) + 1
                continue
            if result.status == "dry_run":
                self.metrics["telegram_dry_run_count"] = int(
                    self.metrics["telegram_dry_run_count"]
                ) + 1
            elif result.status == "failed":
                self.metrics["telegram_delivery_failure_count"] = int(
                    self.metrics["telegram_delivery_failure_count"]
                ) + 1

    def run_live(
        self,
        *,
        duration_minutes: float | None,
        send: bool = False,
        confirm_real_send: bool = False,
    ) -> dict[str, object]:
        started = self.clock()
        deadline = (
            started + (duration_minutes * 60)
            if duration_minutes is not None
            else None
        )
        trigger = self._trigger()
        while deadline is None or self.clock() < deadline:
            try:
                if (
                    self.settings.base_wss_rpc_url
                    and not trigger.connected
                ):
                    trigger.connect()
                    self.metrics["wss_connected"] = True
                self.process_once(
                    send=send,
                    confirm_real_send=confirm_real_send,
                    mode="live",
                )
                if not self.settings.base_wss_rpc_url:
                    self.metrics["status"] = "degraded_http_polling"
                    write_runtime_status(
                        self.settings.runtime_status_path, self.metrics
                    )
                    self.sleep(float(self.settings.rpc_poll_sec))
                    continue
                trigger.receive_head()
            except WssError:
                trigger.close()
                self.metrics["wss_connected"] = False
                self.metrics["status"] = "degraded_http_polling"
                self.metrics["reconnect_count"] = int(
                    self.metrics["reconnect_count"]
                ) + 1
                self.process_once(
                    send=send,
                    confirm_real_send=confirm_real_send,
                    mode="live",
                )
                self.metrics["status"] = "degraded_http_polling"
                write_runtime_status(
                    self.settings.runtime_status_path, self.metrics
                )
                self.sleep(float(self.settings.wss_reconnect_sec))
        trigger.close()
        self.metrics["wss_connected"] = False
        write_runtime_status(self.settings.runtime_status_path, self.metrics)
        return dict(self.metrics)
