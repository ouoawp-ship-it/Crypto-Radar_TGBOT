from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from pathlib import Path

from .aggregator import build_windows
from .classifier import classify_transfer
from .collectors.replay import ReplayCollector
from .config import BASE_DIR, OnchainSettings
from .db import OnchainStore
from .detector import detect_flows
from .labels import LabelRegistry, load_labels_csv
from .models import ReplaySummary
from .notifier import OnchainNotifier
from .scorer import score_detection


def isolated_replay_settings(
    settings: OnchainSettings, fixture_path: Path
) -> OnchainSettings:
    fixture = fixture_path.resolve()
    digest = sha256(fixture.read_bytes()).hexdigest()[:12]
    safe_stem = "".join(
        character
        if character.isalnum() or character in {"-", "_"}
        else "_"
        for character in fixture.stem
    ).strip("_") or "fixture"
    replay_dir = (
        settings.base_dir
        / "data"
        / "onchain"
        / "replay"
        / f"{safe_stem}-{digest}"
    ).resolve()
    live_data_dir = settings.data_dir.resolve()
    live_paths = [path.resolve() for path in settings.writable_paths]
    contains_live_path = any(
        path == replay_dir or replay_dir in path.parents
        for path in live_paths
    )
    if replay_dir == live_data_dir or contains_live_path:
        raise ValueError("replay storage collides with a live on-chain path")
    replay = replace(
        settings,
        enable=False,
        base_enable=False,
        real_send=False,
        data_dir=replay_dir,
        db_path=replay_dir / "onchain_flow.db",
        runtime_status_path=replay_dir / "runtime_status.json",
        tg_push_history_path=replay_dir / "tg_push_history.json",
        tg_outbox_path=replay_dir / "tg_outbox.json",
        tg_topic_routes_path=replay_dir / "tg_topic_routes.json",
        signal_events_path=replay_dir / "signal_events.json",
        signal_events_db_path=replay_dir / "onchain_signals.db",
        labels_path=(
            BASE_DIR
            / "config"
            / "onchain"
            / "cex_addresses.example.csv"
        ),
    )
    replay.validate()
    return replay


def replay_fixture(
    settings: OnchainSettings,
    fixture_path: Path,
    *,
    send: bool = False,
    confirm_real_send: bool = False,
    notify: bool = True,
) -> ReplaySummary:
    settings.validate()
    settings = isolated_replay_settings(settings, fixture_path)
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
            if store.delivery_completed(alert.alert_key):
                continue
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
        replay_directory=str(settings.data_dir),
    )
