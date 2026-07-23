from __future__ import annotations

from pathlib import Path

from .aggregator import build_windows
from .classifier import classify_transfer
from .collectors.replay import ReplayCollector
from .config import OnchainSettings
from .db import OnchainStore
from .detector import detect_flows
from .labels import LabelRegistry, load_labels_csv
from .models import ReplaySummary
from .notifier import OnchainNotifier
from .scorer import score_detection


def replay_fixture(
    settings: OnchainSettings,
    fixture_path: Path,
    *,
    send: bool = False,
    confirm_real_send: bool = False,
    notify: bool = True,
) -> ReplaySummary:
    settings.validate()
    collector = ReplayCollector(fixture_path)
    labels = load_labels_csv(settings.labels_path)
    registry = LabelRegistry(labels)
    store = OnchainStore(settings)
    store.migrate()
    store.replace_labels(labels)
    for metadata in collector.data.metadata:
        store.upsert_token_metadata(metadata)
    metadata_map = store.metadata_map()

    transfers = list(collector.collect())
    for transfer in transfers:
        store.upsert_transfer(transfer)
        metadata = metadata_map.get(
            (transfer.chain_id, transfer.token_address)
        )
        store.upsert_flow(classify_transfer(transfer, metadata, registry))

    flows = store.finalized_flows()
    windows = build_windows(
        flows,
        min_label_confidence=settings.min_label_confidence,
    )
    store.replace_windows(windows)
    detections = detect_flows(flows, windows, metadata_map, settings)
    alerts = [score_detection(detection) for detection in detections]
    store.sync_alerts(alerts)
    active_alerts = store.active_alerts()
    if notify:
        notifier = OnchainNotifier(settings, store)
        for alert in active_alerts:
            notifier.notify(
                alert,
                send=send,
                confirm_real_send=confirm_real_send,
            )

    final_by_event = {transfer.event_id: transfer for transfer in transfers}
    unique_ids = set(final_by_event)
    return ReplaySummary(
        fixture=collector.data.name,
        transfers_seen=len(transfers),
        unique_transfers=len(unique_ids),
        duplicate_deliveries=len(transfers) - len(unique_ids),
        orphaned_transfers=sum(
            1 for transfer in final_by_event.values() if transfer.removed
        ),
        classified_flows=len(flows),
        windows=len(windows),
        alerts=len(active_alerts),
        alert_keys=tuple(alert.alert_key for alert in active_alerts),
    )
