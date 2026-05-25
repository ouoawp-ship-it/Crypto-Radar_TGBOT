from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from config import Settings


LEGACY_STATE_FILES = {
    "bn_signal_history.json": "bn_signal_history.json",
    "fr_snapshot.json": "funding_snapshot.legacy.json",
    "heat_history.json": "heat_history.legacy.json",
}


def legacy_state_report(settings: Settings) -> list[dict[str, Any]]:
    report: list[dict[str, Any]] = []
    for source_name, target_name in LEGACY_STATE_FILES.items():
        source = settings.base_dir / source_name
        target = settings.data_dir / target_name
        source_exists = source.exists()
        target_exists = target.exists()
        if not source_exists:
            action = "missing"
        elif target_exists:
            action = "skip_target_exists"
        else:
            action = "copy_available"
        report.append({
            "source": str(source),
            "target": str(target),
            "source_exists": source_exists,
            "target_exists": target_exists,
            "source_size": source.stat().st_size if source_exists else 0,
            "target_size": target.stat().st_size if target_exists else 0,
            "action": action,
        })
    return report


def migrate_legacy_state(settings: Settings, apply: bool = False) -> dict[str, Any]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    actions: list[dict[str, Any]] = []
    for item in legacy_state_report(settings):
        action = dict(item)
        if item["action"] == "copy_available" and apply:
            source = Path(item["source"])
            target = Path(item["target"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            action["action"] = "copied"
            action["target_exists"] = True
            action["target_size"] = target.stat().st_size
        elif item["action"] == "copy_available":
            action["action"] = "dry_run_copy_available"
        actions.append(action)
    return {
        "applied": apply,
        "actions": actions,
    }
