from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Mapping

from shared.atomic_json import append_jsonl
from shared.storage import JsonStore

from .models import json_safe


OBSERVATION_SCHEMA_VERSION = 1
OBSERVATION_MODULE = "altcoin_contract_anomaly.p2"
MAX_EMITTED_EVENT_IDS = 4_000


def deterministic_event_id(
    *,
    rules_version: str,
    event_type: str,
    symbol: str,
    direction: str,
    window_end: str,
    candidate_pool_hash: str,
    candidate_snapshot_hash: str,
) -> str:
    canonical = json.dumps(
        {
            "candidate_pool_hash": str(candidate_pool_hash),
            "candidate_snapshot_hash": str(candidate_snapshot_hash),
            "direction": str(direction),
            "event_type": str(event_type),
            "rules_version": str(rules_version),
            "symbol": str(symbol).upper(),
            "window_end": str(window_end),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": OBSERVATION_SCHEMA_VERSION,
        "module": OBSERVATION_MODULE,
        "last_valid_manifest": None,
        "symbol_states": {},
        "oi_samples": {},
        "emitted_event_ids": [],
    }


class RealtimeObservationState:
    """Independent P2 dry-run state; never touches the production signal store."""

    def __init__(self, state_path: str | Path, event_path: str | Path) -> None:
        self.state_path = Path(state_path)
        self.event_path = Path(event_path)
        self.store = JsonStore(self.state_path.parent)
        self._lock = threading.RLock()
        self._state = self._load()

    def _load(self) -> dict[str, Any]:
        payload = self.store.load(self.state_path, None)
        if not isinstance(payload, dict):
            output = _default_state()
        elif (
            payload.get("schema_version") != OBSERVATION_SCHEMA_VERSION
            or payload.get("module") != OBSERVATION_MODULE
        ):
            output = _default_state()
        else:
            output = _default_state()
            manifest = payload.get("last_valid_manifest")
            output["last_valid_manifest"] = dict(manifest) if isinstance(manifest, Mapping) else None
            for key in ("symbol_states", "oi_samples"):
                value = payload.get(key)
                output[key] = dict(value) if isinstance(value, Mapping) else {}
            raw_ids = payload.get("emitted_event_ids")
            if isinstance(raw_ids, list):
                output["emitted_event_ids"] = [
                    str(value) for value in raw_ids[-MAX_EMITTED_EVENT_IDS:]
                    if isinstance(value, str) and value
                ]

        # The event line is fsynced before the state document is replaced. If a
        # process dies between those operations, recover its ID from the bounded
        # JSONL so a restart still cannot emit the same dry-run event twice.
        recovered_ids: list[str] = []
        if self.event_path.exists():
            try:
                lines = self.event_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                lines = []
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = record.get("event_id") if isinstance(record, Mapping) else None
                if isinstance(event_id, str) and event_id:
                    recovered_ids.append(event_id)
        merged_ids = [
            *output.get("emitted_event_ids", []),
            *recovered_ids,
        ]
        output["emitted_event_ids"] = list(dict.fromkeys(merged_ids))[-MAX_EMITTED_EVENT_IDS:]
        return output

    @property
    def last_valid_manifest(self) -> dict[str, Any] | None:
        with self._lock:
            value = self._state.get("last_valid_manifest")
            return dict(value) if isinstance(value, Mapping) else None

    @property
    def symbol_states(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {
                str(symbol): dict(value)
                for symbol, value in dict(self._state.get("symbol_states") or {}).items()
                if isinstance(value, Mapping)
            }

    @property
    def oi_samples(self) -> dict[str, list[dict[str, Any]]]:
        with self._lock:
            return {
                str(symbol): [dict(item) for item in value if isinstance(item, Mapping)]
                for symbol, value in dict(self._state.get("oi_samples") or {}).items()
                if isinstance(value, list)
            }

    def has_event(self, event_id: str) -> bool:
        with self._lock:
            return str(event_id) in set(self._state.get("emitted_event_ids") or [])

    def update(
        self,
        *,
        last_valid_manifest: Mapping[str, Any] | None = None,
        symbol_states: Mapping[str, Mapping[str, Any]] | None = None,
        oi_samples: Mapping[str, list[Mapping[str, Any]]] | None = None,
    ) -> None:
        with self._lock:
            if last_valid_manifest is not None:
                self._state["last_valid_manifest"] = json_safe(dict(last_valid_manifest))
            if symbol_states is not None:
                self._state["symbol_states"] = json_safe({
                    str(symbol): dict(value) for symbol, value in symbol_states.items()
                })
            if oi_samples is not None:
                self._state["oi_samples"] = json_safe({
                    str(symbol): [dict(item) for item in values]
                    for symbol, values in oi_samples.items()
                })
            self.store.save(self.state_path, json_safe(self._state))

    def record_event(
        self,
        event: Mapping[str, Any],
        *,
        symbol_states: Mapping[str, Mapping[str, Any]] | None = None,
        oi_samples: Mapping[str, list[Mapping[str, Any]]] | None = None,
    ) -> bool:
        event_id = str(event.get("event_id") or "")
        if not event_id:
            raise ValueError("dry-run event_id is required")
        with self._lock:
            emitted = list(self._state.get("emitted_event_ids") or [])
            if event_id in emitted:
                return False
            append_jsonl(
                self.event_path,
                json_safe(dict(event)),
                max_lines=MAX_EMITTED_EVENT_IDS,
            )
            emitted.append(event_id)
            self._state["emitted_event_ids"] = emitted[-MAX_EMITTED_EVENT_IDS:]
            if symbol_states is not None:
                self._state["symbol_states"] = json_safe({
                    str(symbol): dict(value) for symbol, value in symbol_states.items()
                })
            if oi_samples is not None:
                self._state["oi_samples"] = json_safe({
                    str(symbol): [dict(item) for item in values]
                    for symbol, values in oi_samples.items()
                })
            self.store.save(self.state_path, json_safe(self._state))
            return True


__all__ = [
    "OBSERVATION_MODULE",
    "OBSERVATION_SCHEMA_VERSION",
    "RealtimeObservationState",
    "deterministic_event_id",
]
