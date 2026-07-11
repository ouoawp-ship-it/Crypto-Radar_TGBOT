from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping
from urllib.parse import quote

from .config import BASE_DIR, Settings
from .decision_model import MODEL_VERSION
from .model_optimizer import (
    OPTIMIZATION_VERSION,
    get_optimization_report,
    production_model_fingerprint,
    production_model_snapshot,
)


DEFAULT_MODEL_REGISTRY_DB_PATH = BASE_DIR / "data" / "model_registry.db"
MODEL_REGISTRY_SCHEMA_VERSION = 1810
MODEL_STATUSES = frozenset({"draft", "simulation", "approved", "production", "deprecated", "rejected"})
MODEL_TYPES = {
    "threshold_tuning": ("signal-decision", "candidate-threshold"),
    "risk_control": ("lifecycle-risk", "candidate-risk"),
    "lifecycle_quality": ("lifecycle-intelligence", "candidate-lifecycle"),
    # This is deliberately not registered as a production decision model.  The
    # v1.80 scenario is a post-hoc confidence overlay, not MODULE_WEIGHTS code.
    "module_rebalance": ("simulation-policy", "offline-confidence-overlay"),
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _loads(value: Any, default: Any) -> Any:
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def canonical_parameter_payload(parameters: Mapping[str, Any]) -> dict[str, Any]:
    """Return the deployable parameter payload used for stable hashing.

    Presentation-only flags are intentionally excluded.  The production
    fingerprint therefore matches ``model_optimizer.production_model_fingerprint``.
    """

    payload = {str(key): value for key, value in dict(parameters).items() if str(key) != "immutable"}
    return json.loads(_json(payload))


def canonical_model_hash(parameters: Mapping[str, Any]) -> str:
    encoded = _json(canonical_parameter_payload(parameters)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def runtime_model_snapshot() -> dict[str, Any]:
    return production_model_snapshot()


def runtime_model_hash() -> str:
    return production_model_fingerprint()


def _git_value(base_dir: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args], cwd=str(base_dir), capture_output=True, text=True,
            timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def current_source_commit(base_dir: Path = BASE_DIR) -> str:
    return _git_value(base_dir, "rev-parse", "HEAD")


def current_release_time(base_dir: Path = BASE_DIR) -> str:
    return _git_value(base_dir, "show", "-s", "--format=%cI", "HEAD") or utc_now()


def model_registry_path(settings_or_path: Settings | Path | str | None = None) -> Path:
    if isinstance(settings_or_path, (str, Path)):
        return Path(settings_or_path)
    settings = settings_or_path or Settings.load()
    configured = getattr(settings, "model_registry_db_path", None)
    if configured:
        return Path(configured)
    return Path(settings.data_dir) / "model_registry.db"


def _deserialize(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    for key, default in (
        ("parameters_json", {}), ("metadata_json", {}), ("change_json", {}),
        ("metrics_snapshot_json", {}), ("metrics_json", {}),
    ):
        if key in item:
            item[key.removesuffix("_json")] = _loads(item.pop(key), default)
    return item


@dataclass
class ModelRegistryStore:
    settings_or_path: Settings | Path | str | None = None
    _schema_ready: bool = field(default=False, init=False, repr=False)

    @property
    def db_path(self) -> Path:
        return model_registry_path(self.settings_or_path)

    @contextmanager
    def connect(self, *, ensure_schema: bool = True) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            conn.execute("PRAGMA foreign_keys=ON")
            if ensure_schema and not self._schema_ready:
                self.ensure_schema(conn)
            yield conn
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def readonly(self) -> Iterator[sqlite3.Connection | None]:
        """Open an existing registry without creating or migrating anything."""

        if not self.db_path.exists():
            yield None
            return
        uri = "file:" + quote(self.db_path.resolve().as_posix(), safe="/:\\") + "?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
        except sqlite3.Error:
            yield None
            return
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connect() as conn:
            self._begin_immediate(conn)
            yield conn

    @staticmethod
    def _begin_immediate(conn: sqlite3.Connection, attempts: int = 5, delay: float = 0.05) -> None:
        for attempt in range(max(1, attempts)):
            try:
                conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if not any(token in str(exc).lower() for token in ("locked", "busy")) or attempt + 1 >= attempts:
                    raise
                time.sleep(delay * (2**attempt))

    def ensure_schema(self, conn: sqlite3.Connection | None = None) -> None:
        if conn is None:
            with self.connect(ensure_schema=False) as owned:
                self.ensure_schema(owned)
            return
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS models (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_key TEXT NOT NULL,
                model_version TEXT NOT NULL,
                model_type TEXT NOT NULL,
                status TEXT NOT NULL,
                parameters_json TEXT NOT NULL,
                source_version TEXT,
                description TEXT,
                model_hash TEXT NOT NULL,
                source_commit TEXT,
                released_at TEXT,
                deprecated_at TEXT,
                runtime_verified_at TEXT,
                health_status TEXT NOT NULL DEFAULT 'healthy',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(model_key, model_version)
            );
            CREATE TABLE IF NOT EXISTS model_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER NOT NULL,
                version TEXT NOT NULL,
                change_summary TEXT,
                change_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(model_id) REFERENCES models(id)
            );
            CREATE TABLE IF NOT EXISTS model_approvals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER NOT NULL,
                approval_status TEXT NOT NULL,
                approved_by TEXT,
                reason TEXT,
                metrics_snapshot_json TEXT,
                action TEXT NOT NULL DEFAULT 'approval',
                previous_model_id INTEGER,
                current_model_id INTEGER,
                runtime_hash TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(model_id) REFERENCES models(id)
            );
            CREATE TABLE IF NOT EXISTS model_performance_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                model_id INTEGER NOT NULL,
                period TEXT NOT NULL,
                sample_count INTEGER,
                success_ratio REAL,
                avg_return REAL,
                avg_drawdown REAL,
                risk_score REAL,
                metrics_json TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY(model_id) REFERENCES models(id)
            );
            CREATE INDEX IF NOT EXISTS idx_models_status ON models(model_key,status,updated_at DESC);
            CREATE UNIQUE INDEX IF NOT EXISTS ux_models_one_production
                ON models(model_key) WHERE status='production';
            CREATE INDEX IF NOT EXISTS idx_model_versions_model ON model_versions(model_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_model_approvals_model ON model_approvals(model_id,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_model_approvals_status ON model_approvals(approval_status,created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_model_performance_model_period
                ON model_performance_snapshots(model_id,period,created_at DESC);
            """
        )
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if version < MODEL_REGISTRY_SCHEMA_VERSION:
            conn.execute(f"PRAGMA user_version={MODEL_REGISTRY_SCHEMA_VERSION}")
        self._schema_ready = True

    def get(self, model_key: str, version: str = "", *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        if conn is None:
            with self.readonly() as owned:
                if owned is None:
                    return None
                try:
                    return self.get(model_key, version, conn=owned)
                except sqlite3.OperationalError:
                    return None
        if version:
            row = conn.execute(
                "SELECT * FROM models WHERE model_key=? AND model_version=?",
                (str(model_key), str(version)),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM models WHERE model_key=? ORDER BY CASE status WHEN 'production' THEN 0 ELSE 1 END, id DESC LIMIT 1",
                (str(model_key),),
            ).fetchone()
        return _deserialize(row)

    def by_id(self, model_id: int, *, conn: sqlite3.Connection | None = None) -> dict[str, Any] | None:
        if conn is None:
            with self.readonly() as owned:
                if owned is None:
                    return None
                try:
                    return self.by_id(model_id, conn=owned)
                except sqlite3.OperationalError:
                    return None
        return _deserialize(conn.execute("SELECT * FROM models WHERE id=?", (int(model_id),)).fetchone())

    def list(self, model_key: str = "", status: str = "", limit: int = 100, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
        if conn is None:
            with self.readonly() as owned:
                if owned is None:
                    return []
                try:
                    return self.list(model_key, status, limit, conn=owned)
                except sqlite3.OperationalError:
                    return []
        clauses: list[str] = []
        values: list[Any] = []
        if model_key:
            clauses.append("model_key=?")
            values.append(str(model_key))
        if status:
            clauses.append("status=?")
            values.append(str(status))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(max(1, min(int(limit), 500)))
        rows = conn.execute(
            "SELECT * FROM models" + where + " ORDER BY CASE status WHEN 'production' THEN 0 WHEN 'approved' THEN 1 WHEN 'simulation' THEN 2 ELSE 3 END, updated_at DESC,id DESC LIMIT ?",
            values,
        ).fetchall()
        return [_deserialize(row) or {} for row in rows]

    def insert_version(self, conn: sqlite3.Connection, model_id: int, version: str, summary: str, change: Mapping[str, Any]) -> None:
        conn.execute(
            "INSERT INTO model_versions(model_id,version,change_summary,change_json,created_at) VALUES(?,?,?,?,?)",
            (int(model_id), str(version), str(summary), _json(dict(change)), utc_now()),
        )

    def insert_approval(
        self, conn: sqlite3.Connection, *, model_id: int, status: str, approved_by: str,
        reason: str, metrics: Mapping[str, Any] | None = None, action: str = "approval",
        previous_model_id: int | None = None, current_model_id: int | None = None,
        runtime_hash_value: str = "",
    ) -> int:
        cursor = conn.execute(
            "INSERT INTO model_approvals(model_id,approval_status,approved_by,reason,metrics_snapshot_json,action,previous_model_id,current_model_id,runtime_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (int(model_id), str(status), str(approved_by), str(reason), _json(dict(metrics or {})),
             str(action), previous_model_id, current_model_id, str(runtime_hash_value), utc_now()),
        )
        return int(cursor.lastrowid)


def initialize_model_registry(settings: Settings | Path | str | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    path = model_registry_path(settings)
    if dry_run:
        return {"ok": True, "dry_run": True, "db_path": str(path), "would_initialize": True}
    store = ModelRegistryStore(settings)
    store.ensure_schema()
    return {"ok": True, "dry_run": False, "db_path": str(path), "schema_version": MODEL_REGISTRY_SCHEMA_VERSION}


def bootstrap_production_model(
    settings: Settings | Path | str | None = None, *, dry_run: bool = False,
    source_commit: str = "", released_at: str = "",
) -> dict[str, Any]:
    snapshot = runtime_model_snapshot()
    model_hash = runtime_model_hash()
    commit = source_commit or current_source_commit(getattr(settings, "base_dir", BASE_DIR))
    release = released_at or current_release_time(getattr(settings, "base_dir", BASE_DIR))
    preview = {
        "model_key": "signal-decision", "model_version": MODEL_VERSION,
        "model_type": "decision", "status": "production", "parameters": snapshot,
        "source_version": str(snapshot.get("model_family") or "signal-decision-v1"),
        "model_hash": model_hash, "source_commit": commit, "released_at": release,
        "runtime_verified_at": utc_now(), "health_status": "healthy",
        "metadata": {"bootstrap": True, "immutable_runtime_snapshot": True},
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "model": preview, "changed": False}
    store = ModelRegistryStore(settings)
    with store.transaction() as conn:
        existing = store.get("signal-decision", MODEL_VERSION, conn=conn)
        if existing:
            if str(existing.get("model_hash")) != model_hash:
                return {"ok": False, "code": "registered_runtime_hash_mismatch", "changed": False, "model": existing}
            return {"ok": True, "changed": False, "model": existing, "runtime_verified": True}
        current = conn.execute(
            "SELECT * FROM models WHERE model_key='signal-decision' AND status='production' LIMIT 1"
        ).fetchone()
        if current is not None:
            return {"ok": False, "code": "production_already_registered", "changed": False, "model": _deserialize(current)}
        now = utc_now()
        cursor = conn.execute(
            "INSERT INTO models(model_key,model_version,model_type,status,parameters_json,source_version,description,model_hash,source_commit,released_at,runtime_verified_at,health_status,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (preview["model_key"], preview["model_version"], preview["model_type"], preview["status"],
             _json(snapshot), preview["source_version"], "Bootstrapped immutable production runtime snapshot.",
             model_hash, commit, release, preview["runtime_verified_at"], "healthy", _json(preview["metadata"]), now, now),
        )
        model_id = int(cursor.lastrowid)
        store.insert_version(conn, model_id, MODEL_VERSION, "Initial production runtime snapshot", {
            "action": "bootstrap", "model_hash": model_hash, "source_commit": commit,
        })
        return {"ok": True, "changed": True, "model": store.by_id(model_id, conn=conn), "runtime_verified": True}


def register_production_model(*args: Any, **kwargs: Any) -> dict[str, Any]:
    return bootstrap_production_model(*args, **kwargs)


def list_models(settings: Settings | Path | str | None = None, *, model_key: str = "", status: str = "", limit: int = 100) -> list[dict[str, Any]]:
    if status and status not in MODEL_STATUSES:
        raise ValueError("invalid_model_status")
    return ModelRegistryStore(settings).list(model_key, status, limit)


def get_model(settings: Settings | Path | str | None = None, *, model_key: str = "signal-decision", version: str = "") -> dict[str, Any] | None:
    return ModelRegistryStore(settings).get(model_key, version)


def current_model(settings: Settings | Path | str | None = None, *, model_key: str = "signal-decision") -> dict[str, Any] | None:
    store = ModelRegistryStore(settings)
    with store.readonly() as conn:
        if conn is None:
            return None
        try:
            return _deserialize(conn.execute(
                "SELECT * FROM models WHERE model_key=? AND status='production' ORDER BY released_at DESC,id DESC LIMIT 1",
                (str(model_key),),
            ).fetchone())
        except sqlite3.OperationalError:
            return None


def model_history(settings: Settings | Path | str | None = None, *, model_key: str = "signal-decision", limit: int = 100) -> dict[str, Any]:
    store = ModelRegistryStore(settings)
    with store.readonly() as conn:
        if conn is None:
            return {"available": False, "models": [], "versions": [], "approvals": []}
        models = store.list(model_key=model_key, limit=limit, conn=conn)
        ids = [int(item["id"]) for item in models]
        if not ids:
            return {"available": True, "models": [], "versions": [], "approvals": []}
        marks = ",".join("?" for _ in ids)
        versions = [_deserialize(row) or {} for row in conn.execute(
            f"SELECT * FROM model_versions WHERE model_id IN ({marks}) ORDER BY created_at DESC,id DESC LIMIT ?",
            (*ids, max(1, min(int(limit), 500))),
        ).fetchall()]
        approvals = [_deserialize(row) or {} for row in conn.execute(
            f"SELECT * FROM model_approvals WHERE model_id IN ({marks}) ORDER BY created_at DESC,id DESC LIMIT ?",
            (*ids, max(1, min(int(limit), 500))),
        ).fetchall()]
    return {"available": True, "models": models, "versions": versions, "approvals": approvals}


def _flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {prefix or "value": value}
    result: dict[str, Any] = {}
    for key in sorted(value):
        path = f"{prefix}.{key}" if prefix else str(key)
        item = value[key]
        if isinstance(item, Mapping):
            result.update(_flatten(item, path))
        else:
            result[path] = item
    return result


def model_diff(
    settings: Settings | Path | str | None = None, *, model_key: str = "signal-decision",
    base_version: str = "", candidate_version: str = "",
) -> dict[str, Any]:
    store = ModelRegistryStore(settings)
    candidate = store.get(model_key, candidate_version)
    candidate_metadata = dict((candidate or {}).get("metadata") or {})
    base_key = str(candidate_metadata.get("base_model_key") or model_key)
    base_reference_version = base_version or str(candidate_metadata.get("base_model_version") or "")
    base = store.get(base_key, base_reference_version) if base_reference_version else current_model(settings, model_key=base_key)
    if not base or not candidate:
        return {"ok": False, "code": "model_not_found", "changes": []}
    authoritative_changes = list(candidate_metadata.get("factor_changes") or [])
    if authoritative_changes:
        changes = [
            {
                "parameter": str(item.get("factor") or ""),
                "old": item.get("old_value"),
                "new": item.get("new_value"),
                "impact_scope": str(candidate_metadata.get("scenario_key") or candidate.get("model_type") or "model"),
                **({"label": item.get("label")} if item.get("label") else {}),
                **({"variant": item.get("variant")} if item.get("variant") else {}),
            }
            for item in authoritative_changes
            if str(item.get("factor") or "")
        ]
        return {
            "ok": True,
            "base": {key: base.get(key) for key in ("model_key", "model_version", "model_hash", "status")},
            "candidate": {key: candidate.get(key) for key in ("model_key", "model_version", "model_hash", "status")},
            "changes": changes,
            "simulation": candidate_metadata.get("simulation") or {},
            "diff_method": "optimization_factor_changes",
        }
    left = _flatten(canonical_parameter_payload(base.get("parameters") or {}))
    right = _flatten(canonical_parameter_payload(candidate.get("parameters") or {}))
    changes = []
    for key in sorted(set(left) | set(right)):
        old, new = left.get(key), right.get(key)
        if old != new:
            changes.append({
                "parameter": key, "old": old, "new": new,
                "impact_scope": str((candidate.get("metadata") or {}).get("scenario_key") or candidate.get("model_type") or "model"),
            })
    return {
        "ok": True,
        "base": {key: base.get(key) for key in ("model_key", "model_version", "model_hash", "status")},
        "candidate": {key: candidate.get(key) for key in ("model_key", "model_version", "model_hash", "status")},
        "changes": changes,
        "simulation": (candidate.get("metadata") or {}).get("simulation") or {},
    }


def register_candidate(
    settings: Settings | Path | str | None = None, *, model_key: str = "signal-decision",
    version: str, parameters: Mapping[str, Any], source_version: str = MODEL_VERSION,
    description: str = "", model_type: str = "candidate", scenario: str = "",
    simulation_metrics: Mapping[str, Any] | None = None, status: str = "draft",
    deployable: bool = True, dry_run: bool = False,
) -> dict[str, Any]:
    if status not in {"draft", "simulation"}:
        raise ValueError("candidate_status_must_be_draft_or_simulation")
    if not str(version).strip() or not isinstance(parameters, Mapping) or not parameters:
        raise ValueError("candidate_version_and_parameters_required")
    params = canonical_parameter_payload(parameters)
    hash_value = canonical_model_hash(params)
    metadata = {
        "scenario_key": str(scenario), "simulation": dict(simulation_metrics or {}),
        "deployable": bool(deployable), "auto_apply": False,
    }
    preview = {
        "model_key": str(model_key), "model_version": str(version), "model_type": str(model_type),
        "status": status, "parameters": params, "source_version": str(source_version),
        "description": str(description), "model_hash": hash_value, "metadata": metadata,
    }
    if dry_run:
        return {"ok": True, "dry_run": True, "changed": False, "model": preview}
    store = ModelRegistryStore(settings)
    with store.transaction() as conn:
        existing = store.get(model_key, version, conn=conn)
        if existing:
            same = str(existing.get("model_hash")) == hash_value
            return {"ok": same, "changed": False, "code": "already_registered" if same else "immutable_version_conflict", "model": existing}
        now = utc_now()
        cursor = conn.execute(
            "INSERT INTO models(model_key,model_version,model_type,status,parameters_json,source_version,description,model_hash,health_status,metadata_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (model_key, version, model_type, status, _json(params), source_version, description, hash_value,
             "healthy", _json(metadata), now, now),
        )
        model_id = int(cursor.lastrowid)
        store.insert_version(conn, model_id, version, "Candidate registered", {
            "action": "register_candidate", "scenario_key": scenario, "model_hash": hash_value,
            "auto_apply": False,
        })
        return {"ok": True, "changed": True, "model": store.by_id(model_id, conn=conn)}


def register_optimization_candidates(
    settings: Settings | None = None, *, dry_run: bool = False,
    scenario: str = "", model_key: str = "", version: str = "",
    source_version: str = "", description: str = "",
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    report = get_optimization_report(loaded)
    if not report.get("ok"):
        return {"ok": False, "code": "optimization_report_unavailable", "registered": []}
    registered: list[dict[str, Any]] = []
    requested_scenario = str(scenario or "")
    for comparison in report.get("comparisons") or []:
        scenario_key = str(comparison.get("scenario_key") or "")
        if scenario_key not in MODEL_TYPES or (requested_scenario and requested_scenario != scenario_key):
            continue
        candidate_model_key, model_type = MODEL_TYPES[scenario_key]
        if model_key and model_key != candidate_model_key:
            continue
        candidate_params = dict(comparison.get("candidate_params") or {})
        signature = hashlib.sha256(_json({"scenario": scenario_key, "params": candidate_params, "source": report.get("source_signature")}).encode()).hexdigest()[:12]
        factor_changes = list(comparison.get("factor_changes") or [])
        parameters = {
            "base_model_version": MODEL_VERSION,
            "base_model_hash": runtime_model_hash(),
            "scenario_key": scenario_key,
            "changes": {
                str(item.get("factor")): item.get("new_value")
                for item in factor_changes if str(item.get("factor") or "")
            },
        }
        generated_version = version or f"candidate-{OPTIMIZATION_VERSION}-{scenario_key}-{signature}"
        result = register_candidate(
            loaded, model_key=candidate_model_key, version=generated_version,
            parameters=parameters, source_version=source_version or MODEL_VERSION,
            description=str(description or comparison.get("description") or "Optimization simulation candidate; manual approval required."),
            model_type=model_type, scenario=scenario_key,
            simulation_metrics={
                "status": comparison.get("status"), "readiness": comparison.get("readiness"),
                "delta": comparison.get("delta"), "confidence": comparison.get("confidence"),
                "source_signature": report.get("source_signature"),
            },
            status="simulation", deployable=False, dry_run=dry_run,
        )
        if result.get("ok") and result.get("model") and not dry_run:
            store = ModelRegistryStore(loaded)
            with store.transaction() as conn:
                stored = store.by_id(int(result["model"]["id"]), conn=conn) or {}
                metadata = dict(stored.get("metadata") or {})
                metadata.update({
                    "base_model_key": "signal-decision",
                    "base_model_version": MODEL_VERSION,
                    "base_model_hash": runtime_model_hash(),
                    "factor_changes": factor_changes,
                })
                conn.execute(
                    "UPDATE models SET metadata_json=?,updated_at=? WHERE id=?",
                    (_json(metadata), utc_now(), int(stored["id"])),
                )
                result["model"] = store.by_id(int(stored["id"]), conn=conn)
        elif result.get("model"):
            metadata = dict(result["model"].get("metadata") or {})
            metadata.update({
                "base_model_key": "signal-decision", "base_model_version": MODEL_VERSION,
                "base_model_hash": runtime_model_hash(), "factor_changes": factor_changes,
            })
            result["model"]["metadata"] = metadata
        registered.append(result)
    return {"ok": all(item.get("ok") for item in registered), "dry_run": dry_run, "registered": registered, "auto_apply": False}


def register_optimization_candidate(
    settings: Settings | None = None, *, model_key: str = "", version: str = "",
    scenario: str = "", source_version: str = "", description: str = "",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Register one safe candidate sourced only from a persisted v1.80 run."""

    loaded = settings or Settings.load()
    report = register_optimization_candidates(
        loaded, dry_run=dry_run, scenario=scenario, model_key=model_key,
        version=version, source_version=source_version, description=description,
    )
    if not report.get("ok"):
        return report
    matches = []
    for item in report.get("registered") or []:
        model = item.get("model") or {}
        metadata = model.get("metadata") or {}
        if model_key and str(model.get("model_key")) != str(model_key):
            continue
        if scenario and str(metadata.get("scenario_key")) != str(scenario):
            continue
        if version and str(model.get("model_version")) != str(version):
            continue
        matches.append(item)
    if len(matches) != 1:
        return {
            "ok": False, "code": "optimization_candidate_not_unique" if matches else "optimization_candidate_not_found",
            "registered": matches, "auto_apply": False,
        }
    return {**matches[0], "auto_apply": False, "source": "persisted_optimization_report"}


__all__ = [
    "DEFAULT_MODEL_REGISTRY_DB_PATH", "MODEL_REGISTRY_SCHEMA_VERSION", "MODEL_STATUSES",
    "ModelRegistryStore", "bootstrap_production_model", "canonical_model_hash",
    "canonical_parameter_payload", "current_model", "current_release_time", "current_source_commit",
    "get_model", "initialize_model_registry", "list_models", "model_diff", "model_history",
    "model_registry_path", "register_candidate", "register_optimization_candidate", "register_optimization_candidates",
    "register_production_model", "runtime_model_hash", "runtime_model_snapshot", "utc_now",
]
