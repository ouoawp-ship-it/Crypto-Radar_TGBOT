from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from http.cookies import SimpleCookie
from typing import Any


PASSWORD_HASH_ALGORITHM = "pbkdf2_sha256"
DEFAULT_PASSWORD_ITERATIONS = 260_000


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


def create_session_value(username: str, secret: str, *, ttl_sec: int = 86_400, now: int | None = None) -> tuple[str, str]:
    issued_at = int(time.time() if now is None else now)
    csrf = secrets.token_urlsafe(24)
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
    if not value or not secret or "." not in value:
        return None
    payload_b64, signature = value.split(".", 1)
    if not hmac.compare_digest(_sign_session(payload_b64, secret), signature):
        return None
    try:
        payload = json.loads(_b64_decode(payload_b64).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    username = str(payload.get("username", ""))
    if expected_username and username != expected_username:
        return None
    expires_at = int(payload.get("exp", 0) or 0)
    current = int(time.time() if now is None else now)
    if expires_at < current:
        return None
    return payload


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
