from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from ..config import BASE_DIR, Settings


DEFAULT_JOBS_DB_PATH = BASE_DIR / "data" / "jobs.db"
JOB_STATUSES = {"queued", "running", "success", "failed", "timeout", "cancelled"}
JOB_STDOUT_LIMIT = 12000
JOB_STDERR_LIMIT = 6000
JOB_ERROR_LIMIT = 1000


def zh(text: str) -> str:
    return text.encode("utf-8").decode("unicode_escape")


@dataclass(frozen=True)
class JobSpec:
    job_type: str
    label: str
    command: list[str]
    timeout_sec: int
    internal: bool = False


def _python_command(*args: str) -> list[str]:
    return [sys.executable, "main.py", *args]


JOB_SPECS: dict[str, JobSpec] = {
    "stable-check": JobSpec("stable-check", zh(r"\u7a33\u5b9a\u7248\u9a8c\u6536"), _python_command("stable-check"), 180),
    "doctor": JobSpec("doctor", zh(r"\u73af\u5883\u8bca\u65ad doctor"), _python_command("doctor"), 90),
    "readiness": JobSpec("readiness", zh(r"\u8bfb\u53d6\u771f\u5b9e\u63a8\u9001\u51c6\u5907\u5ea6"), _python_command("readiness"), 90),
    "cleanup": JobSpec("cleanup", zh(r"\u6e05\u7406\u8fd0\u884c\u5783\u573e"), _python_command("cleanup", "--force-cleanup"), 120),
    "update-check": JobSpec("update-check", zh(r"\u68c0\u67e5 GitHub \u66f4\u65b0"), ["bash", "scripts/update_server.sh", "--check"], 180),
    "api-self-test": JobSpec("api-self-test", zh(r"Web API \u81ea\u68c0"), ["internal", "api-self-test"], 60, internal=True),
}
LONG_ACTION_JOB_TYPES = {"stable-check", "doctor", "readiness", "cleanup"}


TOKEN_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{25,}\b"), "<redacted:telegram-token>"),
    (re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{12,}\b"), "<redacted:api-key>"),
    (re.compile(r"(?im)^.*(?:token|api_key|apikey|secret|password).*$"), "<redacted:sensitive-line>"),
)


def redact_text(value: Any) -> str:
    text = str(value or "")
    for pattern, replacement in TOKEN_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(value: str, default: Any) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return default


def _now() -> int:
    return int(time.time())


def _tail(value: Any, limit: int) -> str:
    return redact_text(value)[-limit:]


def _limit(value: Any, limit: int) -> str:
    return redact_text(value)[:limit]


def _row_to_job(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["command"] = _json_loads(str(item.pop("command_json", "[]")), [])
    item["metadata"] = _json_loads(str(item.pop("metadata_json", "{}")), {})
    return item


@dataclass
class JobStore:
    db_path: Path = DEFAULT_JOBS_DB_PATH

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            self._ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              job_type TEXT NOT NULL,
              label TEXT NOT NULL DEFAULT '',
              status TEXT NOT NULL,
              command_json TEXT NOT NULL DEFAULT '[]',
              created_at INTEGER NOT NULL,
              started_at INTEGER,
              finished_at INTEGER,
              updated_at INTEGER NOT NULL,
              duration_ms INTEGER,
              returncode INTEGER,
              stdout_tail TEXT NOT NULL DEFAULT '',
              stderr_tail TEXT NOT NULL DEFAULT '',
              error TEXT NOT NULL DEFAULT '',
              pid INTEGER,
              metadata_json TEXT NOT NULL DEFAULT '{}'
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_type_created ON jobs(job_type, created_at DESC)")

    def create_job(self, job_type: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        spec = validate_job_type(job_type)
        now = _now()
        safe_metadata = sanitize_metadata(metadata or {})
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO jobs (
                  job_type, label, status, command_json, created_at, updated_at, metadata_json
                ) VALUES (?, ?, 'queued', ?, ?, ?, ?)
                """,
                (
                    spec.job_type,
                    spec.label,
                    _json_dumps(spec.command),
                    now,
                    now,
                    _json_dumps(safe_metadata),
                ),
            )
            job_id = int(cur.lastrowid)
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return _row_to_job(row) or {}

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return _row_to_job(row)

    def list_jobs(self, *, limit: int = 50, status: str = "", job_type: str = "") -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 200))
        where: list[str] = []
        params: list[Any] = []
        if status:
            where.append("status = ?")
            params.append(status)
        if job_type:
            where.append("job_type = ?")
            params.append(job_type)
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(safe_limit)
        with self.connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [job for row in rows if (job := _row_to_job(row)) is not None]

    def mark_running(self, job_id: int) -> dict[str, Any] | None:
        now = _now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
            if not row or row["status"] != "queued":
                return _row_to_job(row)
            conn.execute(
                """
                UPDATE jobs
                SET status = 'running', started_at = ?, updated_at = ?, pid = ?
                WHERE id = ?
                """,
                (now, now, os.getpid(), int(job_id)),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return _row_to_job(row)

    def finish_job(
        self,
        job_id: int,
        *,
        status: str,
        returncode: int | None = None,
        stdout_tail: Any = "",
        stderr_tail: Any = "",
        error: Any = "",
    ) -> dict[str, Any] | None:
        if status not in JOB_STATUSES:
            raise ValueError(f"invalid job status: {status}")
        now = _now()
        with self.connect() as conn:
            row = conn.execute("SELECT started_at FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
            started_at = int(row["started_at"] or now) if row else now
            duration_ms = max(0, int((now - started_at) * 1000))
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, updated_at = ?, duration_ms = ?, returncode = ?,
                    stdout_tail = ?, stderr_tail = ?, error = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    now,
                    duration_ms,
                    returncode,
                    _tail(stdout_tail, JOB_STDOUT_LIMIT),
                    _tail(stderr_tail, JOB_STDERR_LIMIT),
                    _limit(error, JOB_ERROR_LIMIT),
                    int(job_id),
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return _row_to_job(row)

    def cancel_job(self, job_id: int) -> dict[str, Any]:
        now = _now()
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
            job = _row_to_job(row)
            if not job:
                return {"ok": False, "message": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "code": "not_found"}
            if job["status"] == "queued":
                conn.execute(
                    """
                    UPDATE jobs
                    SET status = 'cancelled', finished_at = ?, updated_at = ?, duration_ms = 0
                    WHERE id = ?
                    """,
                    (now, now, int(job_id)),
                )
                row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
                return {"ok": True, "job": _row_to_job(row), "message": zh(r"\u4efb\u52a1\u5df2\u53d6\u6d88")}
            if job["status"] == "running":
                return {"ok": False, "job": job, "message": zh(r"\u6682\u4e0d\u652f\u6301\u53d6\u6d88\u8fd0\u884c\u4e2d\u7684\u4efb\u52a1"), "code": "running_not_cancelable"}
            return {"ok": False, "job": job, "message": zh(r"\u53ea\u80fd\u53d6\u6d88\u6392\u961f\u4e2d\u7684\u4efb\u52a1"), "code": "not_queued"}


def validate_job_type(job_type: str) -> JobSpec:
    key = str(job_type or "").strip()
    spec = JOB_SPECS.get(key)
    if not spec:
        raise ValueError(zh(r"\u4e0d\u652f\u6301\u7684\u4efb\u52a1\u7c7b\u578b"))
    return spec


def sanitize_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for key, value in metadata.items():
        text_key = str(key)
        if re.search(r"(?i)(token|api_key|apikey|secret|password)", text_key):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            safe[text_key] = redact_text(value) if isinstance(value, str) else value
        else:
            safe[text_key] = redact_text(_json_dumps(value))
    return safe


def _preflight_command(spec: JobSpec) -> str:
    if spec.job_type == "update-check":
        script = BASE_DIR / "scripts" / "update_server.sh"
        if not script.exists():
            return zh(r"\u672a\u627e\u5230\u66f4\u65b0\u811a\u672c scripts/update_server.sh")
        if not shutil.which("bash"):
            return zh(r"\u5f53\u524d\u73af\u5883\u6ca1\u6709 bash\uff0c\u8bf7\u5728\u670d\u52a1\u5668\u4f7f\u7528 paopao update --yes")
    return ""


def _run_api_self_test() -> tuple[int, str, str]:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    started = time.time()
    try:
        from .. import web as web_module

        probes = [
            ("summary", lambda: web_module.summary_payload()),
            ("web logs", lambda: web_module.logs_payload("web", 50)),
            ("signal stats", lambda: web_module.signals_stats_payload(86400)),
        ]
        for name, func in probes:
            item_started = time.time()
            try:
                payload = func()
                checks.append({"name": name, "ok": bool(payload.get("ok", True)), "elapsed_ms": int((time.time() - item_started) * 1000)})
            except Exception as exc:
                message = f"{name}: {type(exc).__name__}: {exc}"
                errors.append(message)
                checks.append({"name": name, "ok": False, "error": message})
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    payload = {
        "ok": not errors,
        "elapsed_ms": int((time.time() - started) * 1000),
        "checks": checks,
        "errors": errors,
    }
    stdout = _json_dumps(payload)
    return (0 if not errors else 1, stdout, "\n".join(errors))


def execute_job(store: JobStore, job_id: int) -> dict[str, Any] | None:
    job = store.mark_running(job_id)
    if not job:
        return None
    if job.get("status") != "running":
        return job
    try:
        spec = validate_job_type(str(job["job_type"]))
    except ValueError as exc:
        return store.finish_job(job_id, status="failed", returncode=2, error=str(exc), stderr_tail=str(exc))
    preflight_error = _preflight_command(spec)
    if preflight_error:
        return store.finish_job(job_id, status="failed", returncode=127, error=preflight_error, stderr_tail=preflight_error)
    if spec.internal:
        try:
            returncode, stdout, stderr = _run_api_self_test()
            return store.finish_job(
                job_id,
                status="success" if returncode == 0 else "failed",
                returncode=returncode,
                stdout_tail=stdout,
                stderr_tail=stderr,
                error="" if returncode == 0 else zh(r"Web API \u81ea\u68c0\u672a\u901a\u8fc7"),
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            return store.finish_job(job_id, status="failed", returncode=1, error=message, stderr_tail=message)
    try:
        completed = subprocess.run(
            spec.command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout_sec,
            shell=False,
        )
        return store.finish_job(
            job_id,
            status="success" if completed.returncode == 0 else "failed",
            returncode=int(completed.returncode),
            stdout_tail=completed.stdout,
            stderr_tail=completed.stderr,
            error="" if completed.returncode == 0 else completed.stderr or completed.stdout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        message = zh(r"\u4efb\u52a1\u8d85\u65f6\uff1a") + f"{spec.timeout_sec}s"
        return store.finish_job(
            job_id,
            status="timeout",
            returncode=124,
            stdout_tail=stdout,
            stderr_tail=stderr or message,
            error=message,
        )
    except OSError as exc:
        message = f"{type(exc).__name__}: {exc}"
        return store.finish_job(job_id, status="failed", returncode=127, stderr_tail=message, error=message)


def run_job_async(store: JobStore, job_id: int) -> None:
    thread = threading.Thread(target=execute_job, args=(store, int(job_id)), daemon=True)
    thread.start()


def run_job_sync_for_tests(store: JobStore, job_id: int) -> dict[str, Any] | None:
    return execute_job(store, int(job_id))


def store_for_settings(settings: Settings | None = None) -> JobStore:
    loaded = settings or Settings.load()
    return JobStore(loaded.web_jobs_db_path)


def create_job_payload(job_type: str, metadata: dict[str, Any] | None = None, *, settings: Settings | None = None, start: bool = True) -> dict[str, Any]:
    store = store_for_settings(settings)
    try:
        job = store.create_job(job_type, metadata or {})
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "error": str(exc), "code": "invalid_job_type"}
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        return {"ok": False, "message": message, "error": message, "code": "job_create_failed"}
    if start:
        try:
            run_job_async(store, int(job["id"]))
        except Exception as exc:
            store.finish_job(int(job["id"]), status="failed", returncode=1, error=f"{type(exc).__name__}: {exc}")
            job = store.get_job(int(job["id"])) or job
    return {"ok": True, "job": job, "message": zh(r"\u4efb\u52a1\u5df2\u521b\u5efa\uff0c\u6b63\u5728\u540e\u53f0\u6267\u884c")}


def jobs_payload(*, limit: int = 50, status: str = "", job_type: str = "", settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    jobs = store.list_jobs(limit=limit, status=status, job_type=job_type)
    return {"ok": True, "jobs": jobs, "count": len(jobs), "message": zh(r"\u5df2\u8bfb\u53d6\u4efb\u52a1\u8bb0\u5f55")}


def job_detail_payload(job_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    job = store.get_job(int(job_id or 0))
    if not job:
        return {"ok": False, "message": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "error": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "code": "not_found"}
    return {"ok": True, "job": job, "message": zh(r"\u5df2\u8bfb\u53d6\u4efb\u52a1\u8be6\u60c5")}


def cancel_job_payload(job_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    result = store.cancel_job(int(job_id or 0))
    result.setdefault("message", zh(r"\u4efb\u52a1\u53d6\u6d88\u8bf7\u6c42\u5df2\u5904\u7406"))
    return result


def recent_job_payload(job_type: str, *, settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    jobs = store.list_jobs(limit=1, job_type=job_type)
    return jobs[0] if jobs else {}
