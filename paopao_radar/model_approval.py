from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from .config import Settings
from .model_registry import (
    ModelRegistryStore,
    current_model,
    current_source_commit,
    runtime_model_hash,
    runtime_model_snapshot,
    utc_now,
)


def _identity(value: str, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field}_required")
    return text[:500]


def _target(store: ModelRegistryStore, model_key: str, version: str, conn: Any) -> dict[str, Any]:
    model = store.get(str(model_key), str(version), conn=conn)
    if not model:
        raise ValueError("model_not_found")
    return model


def mark_simulation_complete(
    settings: Settings | Path | str | None = None, *, model_key: str,
    version: str, metrics_snapshot: Mapping[str, Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    store = ModelRegistryStore(settings)
    if dry_run:
        model = store.get(model_key, version)
        return {"ok": bool(model), "dry_run": True, "changed": False, "model": model, "next_status": "simulation"}
    with store.transaction() as conn:
        model = _target(store, model_key, version, conn)
        if model["status"] == "simulation":
            return {"ok": True, "changed": False, "model": model}
        if model["status"] != "draft":
            return {"ok": False, "code": "invalid_status_transition", "changed": False, "model": model}
        now = utc_now()
        metadata = dict(model.get("metadata") or {})
        metadata["simulation"] = dict(metrics_snapshot or {})
        conn.execute(
            "UPDATE models SET status='simulation',metadata_json=?,updated_at=? WHERE id=? AND status='draft'",
            (__import__("json").dumps(metadata, ensure_ascii=False, sort_keys=True, separators=(",", ":")), now, int(model["id"])),
        )
        store.insert_version(conn, int(model["id"]), version, "Simulation completed", {
            "action": "simulation_complete", "previous_status": "draft", "new_status": "simulation",
            "metrics_snapshot": dict(metrics_snapshot or {}), "auto_apply": False,
        })
        return {"ok": True, "changed": True, "model": store.by_id(int(model["id"]), conn=conn)}


def approve_model(
    settings: Settings | Path | str | None = None, *, model_key: str,
    version: str, approved_by: str, reason: str,
    metrics_snapshot: Mapping[str, Any] | None = None, dry_run: bool = False,
) -> dict[str, Any]:
    actor = _identity(approved_by, "approved_by")
    explanation = _identity(reason, "reason")
    store = ModelRegistryStore(settings)
    if dry_run:
        model = store.get(model_key, version)
        allowed = bool(model and model.get("status") == "simulation")
        return {"ok": allowed, "dry_run": True, "changed": False, "model": model, "next_status": "approved" if allowed else None}
    with store.transaction() as conn:
        model = _target(store, model_key, version, conn)
        if model["status"] == "approved":
            return {"ok": True, "changed": False, "model": model, "requires_activation": True}
        if model["status"] != "simulation":
            return {"ok": False, "code": "simulation_required_before_approval", "changed": False, "model": model}
        now = utc_now()
        conn.execute("UPDATE models SET status='approved',updated_at=? WHERE id=? AND status='simulation'", (now, int(model["id"])))
        approval_id = store.insert_approval(
            conn, model_id=int(model["id"]), status="approved", approved_by=actor,
            reason=explanation, metrics=metrics_snapshot or (model.get("metadata") or {}).get("simulation") or {},
            action="manual_approval", runtime_hash_value=runtime_model_hash(),
        )
        store.insert_version(conn, int(model["id"]), version, "Manually approved for deployment review", {
            "action": "approve", "previous_status": "simulation", "new_status": "approved",
            "approval_id": approval_id, "approved_by": actor, "reason": explanation,
            "requires_separate_activation": True, "auto_apply": False,
        })
        return {
            "ok": True, "changed": True, "model": store.by_id(int(model["id"]), conn=conn),
            "approval_id": approval_id, "requires_activation": True, "auto_apply": False,
        }


def reject_model(
    settings: Settings | Path | str | None = None, *, model_key: str,
    version: str, approved_by: str, reason: str, dry_run: bool = False,
) -> dict[str, Any]:
    actor = _identity(approved_by, "approved_by")
    explanation = _identity(reason, "reason")
    store = ModelRegistryStore(settings)
    if dry_run:
        model = store.get(model_key, version)
        allowed = bool(model and model.get("status") in {"draft", "simulation", "approved"})
        return {"ok": allowed, "dry_run": True, "changed": False, "model": model, "next_status": "rejected" if allowed else None}
    with store.transaction() as conn:
        model = _target(store, model_key, version, conn)
        if model["status"] == "rejected":
            return {"ok": True, "changed": False, "model": model}
        if model["status"] not in {"draft", "simulation", "approved"}:
            return {"ok": False, "code": "production_or_deprecated_model_cannot_be_rejected", "changed": False, "model": model}
        now = utc_now()
        previous = str(model["status"])
        conn.execute("UPDATE models SET status='rejected',updated_at=? WHERE id=?", (now, int(model["id"])))
        approval_id = store.insert_approval(
            conn, model_id=int(model["id"]), status="rejected", approved_by=actor,
            reason=explanation, action="manual_rejection", runtime_hash_value=runtime_model_hash(),
        )
        store.insert_version(conn, int(model["id"]), version, "Candidate rejected", {
            "action": "reject", "previous_status": previous, "new_status": "rejected",
            "approval_id": approval_id, "approved_by": actor, "reason": explanation,
        })
        return {"ok": True, "changed": True, "model": store.by_id(int(model["id"]), conn=conn), "approval_id": approval_id}


def activate_model(
    settings: Settings | Path | str | None = None, *, model_key: str,
    version: str, approved_by: str, reason: str,
    confirm_production: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    """Move approved -> production only after a separately deployed runtime matches.

    This function never writes model code or configuration.  A hash mismatch is
    a hard stop so the registry cannot claim a candidate is live when it is not.
    """

    actor = _identity(approved_by, "approved_by")
    explanation = _identity(reason, "reason")
    store = ModelRegistryStore(settings)
    live_hash = runtime_model_hash()
    live_version = str(runtime_model_snapshot().get("model_version") or "")
    if dry_run:
        target = store.get(model_key, version)
        matches = bool(
            target and str(target.get("model_hash")) == live_hash
            and str(target.get("model_version")) == live_version
        )
        return {
            "ok": bool(target), "dry_run": True, "changed": False, "model": target,
            "runtime_hash_matches": matches, "requires_confirmation": not confirm_production,
            "manual_deployment_required": not matches,
        }
    with store.transaction() as conn:
        target = _target(store, model_key, version, conn)
        if target["status"] == "production":
            return {"ok": True, "changed": False, "model": target, "runtime_verified": str(target["model_hash"]) == live_hash}
        if target["status"] != "approved":
            return {"ok": False, "code": "manual_approval_required", "changed": False, "model": target}
        if not confirm_production:
            approval_id = store.insert_approval(
                conn, model_id=int(target["id"]), status="pending", approved_by=actor,
                reason=explanation, metrics=(target.get("metadata") or {}).get("simulation") or {},
                action="activation_intent", runtime_hash_value=live_hash,
            )
            return {
                "ok": True, "changed": False, "code": "confirmation_required", "approval_id": approval_id,
                "model": target, "requires_confirmation": True, "auto_apply": False,
            }
        if not bool((target.get("metadata") or {}).get("deployable", True)):
            return {
                "ok": False, "changed": False, "code": "candidate_not_deployable",
                "manual_deployment_required": True, "model": target,
            }
        if str(target.get("model_hash")) != live_hash or str(target.get("model_version")) != live_version:
            approval_id = store.insert_approval(
                conn, model_id=int(target["id"]), status="pending", approved_by=actor,
                reason=explanation, metrics=(target.get("metadata") or {}).get("simulation") or {},
                action="activation_waiting_for_runtime", runtime_hash_value=live_hash,
            )
            return {
                "ok": False, "changed": False, "code": "runtime_model_mismatch",
                "manual_deployment_required": True, "approval_id": approval_id,
                "target_model_hash": target.get("model_hash"), "runtime_model_hash": live_hash,
                "target_model_version": target.get("model_version"), "runtime_model_version": live_version,
                "model": target,
            }
        current_row = conn.execute(
            "SELECT * FROM models WHERE model_key=? AND status='production' LIMIT 1", (model_key,)
        ).fetchone()
        current = dict(current_row) if current_row is not None else None
        now = utc_now()
        if current and int(current["id"]) != int(target["id"]):
            conn.execute(
                "UPDATE models SET status='deprecated',deprecated_at=?,health_status='deprecated',updated_at=? WHERE id=?",
                (now, now, int(current["id"])),
            )
            store.insert_version(conn, int(current["id"]), str(current["model_version"]), "Superseded by manually activated model", {
                "action": "deprecate", "replacement_model_id": int(target["id"]), "at": now,
            })
        conn.execute(
            "UPDATE models SET status='production',released_at=?,deprecated_at=NULL,runtime_verified_at=?,source_commit=?,health_status='healthy',updated_at=? WHERE id=?",
            (now, now, current_source_commit(), now, int(target["id"])),
        )
        approval_id = store.insert_approval(
            conn, model_id=int(target["id"]), status="approved", approved_by=actor, reason=explanation,
            metrics=(target.get("metadata") or {}).get("simulation") or {}, action="manual_activation",
            previous_model_id=int(current["id"]) if current else None, current_model_id=int(target["id"]),
            runtime_hash_value=live_hash,
        )
        store.insert_version(conn, int(target["id"]), version, "Manually activated after runtime verification", {
            "action": "activate", "approval_id": approval_id, "previous_model": current.get("model_version") if current else None,
            "current_model": version, "activation_time": now, "runtime_hash": live_hash,
        })
        return {
            "ok": True, "changed": True, "approval_id": approval_id,
            "model": store.by_id(int(target["id"]), conn=conn), "runtime_verified": True,
        }


def rollback_model(
    settings: Settings | Path | str | None = None, *, model_key: str,
    version: str, approved_by: str, reason: str,
    confirm_production: bool = False, dry_run: bool = False,
) -> dict[str, Any]:
    actor = _identity(approved_by, "approved_by")
    explanation = _identity(reason, "reason")
    store = ModelRegistryStore(settings)
    live_hash = runtime_model_hash()
    live_version = str(runtime_model_snapshot().get("model_version") or "")
    if dry_run:
        target = store.get(model_key, version)
        current = current_model(settings, model_key=model_key)
        return {
            "ok": bool(target and current), "dry_run": True, "changed": False,
            "target": target, "current": current,
            "runtime_hash_matches": bool(
                target and target.get("model_hash") == live_hash
                and target.get("model_version") == live_version
            ),
            "requires_confirmation": not confirm_production,
        }
    with store.transaction() as conn:
        target = _target(store, model_key, version, conn)
        current_row = conn.execute(
            "SELECT * FROM models WHERE model_key=? AND status='production' LIMIT 1", (model_key,)
        ).fetchone()
        current = dict(current_row) if current_row is not None else None
        if current is None:
            return {"ok": False, "code": "production_model_not_found", "changed": False}
        if int(current["id"]) == int(target["id"]):
            return {"ok": True, "changed": False, "code": "already_production", "model": target}
        # Rollback is only for a version that was previously in production.
        # An approved-but-never-deployed candidate must use the separately
        # guarded activation path; accepting it here would bypass that state
        # machine under a misleading action name.
        if target["status"] != "deprecated":
            return {"ok": False, "code": "rollback_target_not_previously_production", "changed": False, "model": target}
        action = "rollback_intent" if not confirm_production else "rollback_waiting_for_runtime"
        if (
            not confirm_production
            or str(target.get("model_hash")) != live_hash
            or str(target.get("model_version")) != live_version
        ):
            approval_id = store.insert_approval(
                conn, model_id=int(target["id"]), status="pending", approved_by=actor,
                reason=explanation, action=action, previous_model_id=int(current["id"]),
                current_model_id=int(target["id"]), runtime_hash_value=live_hash,
            )
            code = "confirmation_required" if not confirm_production else "runtime_model_mismatch"
            return {
                "ok": not confirm_production, "changed": False, "code": code, "approval_id": approval_id,
                "manual_deployment_required": (
                    str(target.get("model_hash")) != live_hash
                    or str(target.get("model_version")) != live_version
                ),
                "previous_model": current["model_version"], "target_model": target["model_version"],
                "target_model_hash": target.get("model_hash"), "runtime_model_hash": live_hash,
                "target_model_version": target.get("model_version"), "runtime_model_version": live_version,
            }
        now = utc_now()
        conn.execute(
            "UPDATE models SET status='deprecated',deprecated_at=?,health_status='deprecated',updated_at=? WHERE id=?",
            (now, now, int(current["id"])),
        )
        conn.execute(
            "UPDATE models SET status='production',released_at=?,deprecated_at=NULL,runtime_verified_at=?,health_status='healthy',updated_at=? WHERE id=?",
            (now, now, now, int(target["id"])),
        )
        approval_id = store.insert_approval(
            conn, model_id=int(target["id"]), status="rollback", approved_by=actor,
            reason=explanation, action="manual_rollback", previous_model_id=int(current["id"]),
            current_model_id=int(target["id"]), runtime_hash_value=live_hash,
        )
        change = {
            "action": "rollback", "approval_id": approval_id, "rollback_time": now,
            "rollback_reason": explanation, "previous_model": current["model_version"],
            "current_model": target["model_version"], "approved_by": actor,
        }
        store.insert_version(conn, int(target["id"]), version, "Manual rollback activated", change)
        store.insert_version(conn, int(current["id"]), str(current["model_version"]), "Deprecated by manual rollback", change)
        return {
            "ok": True, "changed": True, "approval_id": approval_id,
            "rollback": change, "model": store.by_id(int(target["id"]), conn=conn),
        }


__all__ = [
    "activate_model", "approve_model", "mark_simulation_complete", "reject_model", "rollback_model",
]
