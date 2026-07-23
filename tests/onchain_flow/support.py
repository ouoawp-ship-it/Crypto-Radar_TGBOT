from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from paopao_radar.onchain_flow.config import OnchainSettings


REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "onchain" / "p3_0_flow.jsonl"
LABELS_PATH = REPO_ROOT / "config" / "onchain" / "cex_addresses.example.csv"
CHAINS_PATH = REPO_ROOT / "config" / "onchain" / "chains.example.json"


def make_settings(root: Path, **overrides: object) -> OnchainSettings:
    data_dir = root / "data" / "onchain"
    settings = OnchainSettings(
        base_dir=root,
        data_dir=data_dir,
        db_path=data_dir / "onchain_flow.db",
        runtime_status_path=data_dir / "runtime_status.json",
        tg_push_history_path=data_dir / "tg_push_history.json",
        tg_outbox_path=data_dir / "tg_outbox.json",
        tg_topic_routes_path=data_dir / "tg_topic_routes.json",
        signal_events_path=data_dir / "signal_events.json",
        signal_events_db_path=data_dir / "onchain_signals.db",
        labels_path=LABELS_PATH,
        chains_path=CHAINS_PATH,
    )
    return replace(settings, **overrides)
