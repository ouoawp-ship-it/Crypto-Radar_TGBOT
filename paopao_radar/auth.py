from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import hmac
import json
import secrets
import time
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

from .atomic_json import locked_read_json, locked_update_json


PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
DEFAULT_PASSWORD_ITERATIONS = 260_000
AUTH_STATE_FILE = "admin_auth_state.json"
AUTH_AUDIT_FILE = "admin_auth_audit.json"


def _b64_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def generate_password_hash(password: str, *, iterations: int = DEFAULT_PASSWORD_ITERATIONS) -> str:
    text = str(password or "")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", text.encode("utf-8"), salt, int(iterations))
    return f"{PASSWORD_HASH_ALGORITHM}${int(iterations)}${_b64_encode(salt)}${_b64_encode(digest)}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt_text, digest_text = str(stored_hash or "").split("$", 3)
        if algorithm != PASSWORD_HASH_ALGORITHM:
            return False
        iterations = int(iterations_text)
        salt = _b64_decode(salt_text)
        expected = _b64_decode(digest_text)
    except Exception:
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password or "").encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def generate_session_secret() -> str:
    return secrets.token_urlsafe(48)


def _sign_session(payload_b64: str, secret: str) -> str:
    digest = hmac.new(str(secret or "").encode("utf-8"), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64_encode(digest)


def create_session_value(
    username: str,
    secret: str,
    *,
    ttl_sec: int = 86_400,
    now: int | None = None,
    csrf: str = "",
) -> tuple[str, str]:
    issued_at = int(time.time() if now is None else now)
    csrf = str(csrf or secrets.token_urlsafe(24))
    payload = {
        "username": str(username or ""),
        "iat": issued_at,
        "exp": issued_at + max(60, int(ttl_sec or 86_400)),
        "csrf": csrf,
    }
    payload_b64 = _b64_encode(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_sign_session(payload_b64, secret)}", csrf


def verify_session_value(
    value: str,
    secret: str,
    *,
    expected_username: str = "",
    now: int | None = None,
) -> dict[str, Any] | None:
    payload, _reason = verify_session_value_detailed(
        value,
        secret,
        expected_username=expected_username,
        now=now,
    )
    return payload


def verify_session_value_detailed(
    value: str,
    secret: str,
    *,
    expected_username: str = "",
    now: int | None = None,
) -> tuple[dict[str, Any] | None, str]:
    if not value or not secret or "." not in value:
        return None, "missing"
    payload_b64, signature = value.split(".", 1)
    if not hmac.compare_digest(_sign_session(payload_b64, secret), signature):
        return None, "bad_signature"
    try:
        payload = json.loads(_b64_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None, "bad_payload"
    if not isinstance(payload, dict):
        return None, "bad_payload"
    username = str(payload.get("username", ""))
    if expected_username and username != expected_username:
        return None, "bad_username"
    expires_at = int(payload.get("exp", 0) or 0)
    current = int(time.time() if now is None else now)
    if expires_at < current:
        return None, "expired"
    return payload, "ok"


def cookie_value(cookie_header: str, name: str) -> str:
    if not cookie_header or not name:
        return ""
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel is not None else ""


def build_session_cookie(name: str, value: str, *, max_age: int, secure: bool) -> str:
    parts = [
        f"{name}={value}",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        f"Max-Age={max(60, int(max_age or 86_400))}",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def build_clear_cookie(name: str, *, secure: bool) -> str:
    parts = [
        f"{name}=",
        "Path=/",
        "HttpOnly",
        "SameSite=Lax",
        "Max-Age=0",
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def admin_auth_state_path(data_dir: Path) -> Path:
    return Path(data_dir) / AUTH_STATE_FILE


def admin_auth_audit_path(data_dir: Path) -> Path:
    return Path(data_dir) / AUTH_AUDIT_FILE


def iso_timestamp(ts: int | float | None = None) -> str:
    value = time.time() if ts is None else float(ts)
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def hash_for_audit(secret: str, value: str) -> str:
    text = str(value or "")
    if not text:
        text = "unknown"
    key = str(secret or "paopao-auth-audit").encode("utf-8")
    digest = hmac.new(key, text.encode("utf-8"), hashlib.sha256).hexdigest()
    return digest[:32]


def auth_failure_key(secret: str, username: str, ip: str) -> str:
    return hash_for_audit(secret, f"{str(username or '').strip().lower()}|{str(ip or '').strip()}")


def check_auth_lockout(
    data_dir: Path,
    secret: str,
    username: str,
    ip: str,
    *,
    window_sec: int,
    now: int | None = None,
) -> dict[str, Any]:
    current = int(time.time() if now is None else now)
    key = auth_failure_key(secret, username, ip)
    path = admin_auth_state_path(Path(data_dir))
    state = locked_read_json(path, {"failures": {}})
    failures = state.get("failures") if isinstance(state, dict) else {}
    if not isinstance(failures, dict):
        failures = {}
    entry = failures.get(key)
    if not isinstance(entry, dict):
        return {"locked": False, "retry_after_sec": 0, "key": key, "count": 0}
    locked_until = int(entry.get("locked_until", 0) or 0)
    if locked_until > current:
        return {
            "locked": True,
            "retry_after_sec": max(1, locked_until - current),
            "key": key,
            "count": int(entry.get("count", 0) or 0),
        }
    first_failed_at = int(entry.get("first_failed_at", 0) or 0)
    if first_failed_at and current - first_failed_at > max(1, int(window_sec or 900)):
        cleanup_result: dict[str, int] = {"count": 0, "locked_until": 0}

        def remove_expired(raw_state: Any) -> dict[str, Any]:
            normalized = raw_state if isinstance(raw_state, dict) else {"failures": {}}
            current_failures = normalized.get("failures")
            if not isinstance(current_failures, dict):
                current_failures = {}
            current_entry = current_failures.get(key)
            if not isinstance(current_entry, dict):
                normalized["failures"] = current_failures
                return normalized
            entry_first_failed_at = int(current_entry.get("first_failed_at", 0) or 0)
            entry_locked_until = int(current_entry.get("locked_until", 0) or 0)
            expired = (
                entry_locked_until <= current
                and entry_first_failed_at
                and current - entry_first_failed_at > max(1, int(window_sec or 900))
            )
            if expired:
                current_failures.pop(key, None)
            else:
                cleanup_result["count"] = int(current_entry.get("count", 0) or 0)
                cleanup_result["locked_until"] = entry_locked_until
            normalized["failures"] = current_failures
            return normalized

        locked_update_json(path, remove_expired, {"failures": {}})
        if cleanup_result["locked_until"] > current:
            return {
                "locked": True,
                "retry_after_sec": max(1, cleanup_result["locked_until"] - current),
                "key": key,
                "count": cleanup_result["count"],
            }
        return {"locked": False, "retry_after_sec": 0, "key": key, "count": cleanup_result["count"]}
    return {"locked": False, "retry_after_sec": 0, "key": key, "count": int(entry.get("count", 0) or 0)}


def record_auth_failure(
    data_dir: Path,
    secret: str,
    username: str,
    ip: str,
    *,
    max_failures: int,
    lockout_sec: int,
    window_sec: int,
    now: int | None = None,
) -> dict[str, Any]:
    current = int(time.time() if now is None else now)
    key = auth_failure_key(secret, username, ip)
    limit = max(1, int(max_failures or 5))
    path = admin_auth_state_path(Path(data_dir))
    result = {"locked": False, "locked_until": 0, "count": 0}

    def add_failure(raw_state: Any) -> dict[str, Any]:
        state = raw_state if isinstance(raw_state, dict) else {"failures": {}}
        failures = state.get("failures")
        if not isinstance(failures, dict):
            failures = {}
        entry = failures.get(key)
        if not isinstance(entry, dict):
            entry = {"count": 0, "first_failed_at": current, "last_failed_at": 0, "locked_until": 0}
        first_failed_at = int(entry.get("first_failed_at", current) or current)
        if current - first_failed_at > max(1, int(window_sec or 900)):
            entry = {"count": 0, "first_failed_at": current, "last_failed_at": 0, "locked_until": 0}
        count = int(entry.get("count", 0) or 0) + 1
        locked_until = int(entry.get("locked_until", 0) or 0)
        locked = False
        if count >= limit:
            locked = True
            locked_until = current + max(1, int(lockout_sec or 600))
        entry.update({
            "count": count,
            "first_failed_at": int(entry.get("first_failed_at", current) or current),
            "last_failed_at": current,
            "locked_until": locked_until,
        })
        failures[key] = entry
        state["failures"] = failures
        result.update({"locked": locked, "locked_until": locked_until, "count": count})
        return state

    locked_update_json(path, add_failure, {"failures": {}})
    return {
        "locked": bool(result["locked"]),
        "retry_after_sec": max(0, int(result["locked_until"]) - current) if result["locked"] else 0,
        "key": key,
        "count": int(result["count"]),
    }


def clear_auth_failures(data_dir: Path, secret: str, username: str, ip: str) -> None:
    key = auth_failure_key(secret, username, ip)
    path = admin_auth_state_path(Path(data_dir))

    def remove_failure(raw_state: Any) -> dict[str, Any]:
        state = raw_state if isinstance(raw_state, dict) else {"failures": {}}
        failures = state.get("failures")
        if not isinstance(failures, dict):
            failures = {}
        failures.pop(key, None)
        state["failures"] = failures
        return state

    locked_update_json(path, remove_failure, {"failures": {}})


def append_auth_audit(
    data_dir: Path,
    *,
    event: str,
    username: str = "",
    ip: str = "",
    user_agent: str = "",
    result: str = "",
    reason: str = "",
    limit: int = 500,
    secret: str = "",
    now: int | float | None = None,
) -> None:
    record = {
        "time": iso_timestamp(now),
        "event": str(event or ""),
        "username": str(username or "")[:80],
        "ip_hash": hash_for_audit(secret, ip),
        "user_agent_hash": hash_for_audit(secret, user_agent),
        "result": str(result or ""),
        "reason": str(reason or ""),
    }
    path = admin_auth_audit_path(Path(data_dir))
    max_count = max(1, int(limit or 500))

    def append_record(raw_records: Any) -> list[dict[str, Any]]:
        records = list(raw_records) if isinstance(raw_records, list) else []
        records.append(record)
        if len(records) > max_count:
            records = records[-max_count:]
        return records

    locked_update_json(path, append_record, [])


def auth_audit_payload(data_dir: Path, *, limit: int = 50) -> dict[str, Any]:
    records = locked_read_json(admin_auth_audit_path(Path(data_dir)), [])
    if not isinstance(records, list):
        records = []
    safe_limit = max(1, min(int(limit or 50), 500))
    items = [dict(item) for item in records[-safe_limit:] if isinstance(item, dict)]
    items.reverse()
    return {
        "ok": True,
        "records": items,
        "items": items,
        "count": len(items),
        "message": "已读取后台登录审计",
    }
