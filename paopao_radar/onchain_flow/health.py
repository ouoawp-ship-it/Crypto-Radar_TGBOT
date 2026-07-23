from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


DEFAULT_RUNTIME_STATUS: dict[str, object] = {
    "mode": "disabled",
    "status": "not_started",
    "latest_head": 0,
    "target_finalized": 0,
    "cursor_block": 0,
    "cursor_lag_blocks": 0,
    "last_success_at": 0,
    "last_error_type": "",
    "http_provider_configured": False,
    "wss_provider_configured": False,
    "wss_connected": False,
    "reconnect_count": 0,
    "rpc_error_count": 0,
    "logs_received": 0,
    "duplicate_count": 0,
    "skipped_indexed_transfer_count": 0,
    "orphan_count": 0,
    "priced_count": 0,
    "unpriced_count": 0,
    "alerts_generated": 0,
    "telegram_dry_run_count": 0,
    "telegram_delivery_failure_count": 0,
}


def read_runtime_status(path: Path) -> dict[str, object]:
    if not path.exists():
        return dict(DEFAULT_RUNTIME_STATUS)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**DEFAULT_RUNTIME_STATUS, "status": "unreadable"}
    if not isinstance(payload, dict):
        return {**DEFAULT_RUNTIME_STATUS, "status": "unreadable"}
    return {**DEFAULT_RUNTIME_STATUS, **payload}


def write_runtime_status(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
