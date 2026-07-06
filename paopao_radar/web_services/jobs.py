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
JOB_STATUSES = {"queued", "running", "success", "attention", "failed", "timeout", "cancelled"}
JOB_STDOUT_LIMIT = 12000
JOB_STDERR_LIMIT = 6000
JOB_ERROR_LIMIT = 1000
JOB_REPORT_TAIL_LIMIT = 12000
CONCURRENT_GUARD_JOB_TYPES = {"stable-check", "doctor", "readiness", "cleanup", "update-check", "api-self-test"}
ERROR_LINE_PATTERN = re.compile(r"(?i)(failed|error|traceback|timeout|exception|\u5f02\u5e38|\u9519\u8bef|\u5931\u8d25|\u8d85\u65f6)")
STABLE_CHECK_ATTENTION_RE = re.compile(r"(状态|摘要|网络重试噪声|日志稳定性):\s*(.+)")


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


def normalized_job_status(job: dict[str, Any] | None) -> str:
    item = job or {}
    status = str(item.get("status") or "")
    job_type = str(item.get("job_type") or "")
    try:
        returncode = int(item.get("returncode") or 0)
    except (TypeError, ValueError):
        returncode = 0
    if job_type == "stable-check" and status == "failed" and returncode == 1:
        return "attention"
    return status


def job_status_from_returncode(spec: JobSpec, returncode: int) -> str:
    if int(returncode) == 0:
        return "success"
    if spec.job_type == "stable-check" and int(returncode) == 1:
        return "attention"
    return "failed"


@dataclass
class JobStore:
    db_path: Path = DEFAULT_JOBS_DB_PATH
    stdout_tail_chars: int = JOB_STDOUT_LIMIT
    stderr_tail_chars: int = JOB_STDERR_LIMIT

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

    def create_job(self, job_type: str, metadata: dict[str, Any] | None = None, *, allow_reuse: bool = True) -> dict[str, Any]:
        spec = validate_job_type(job_type)
        now = _now()
        safe_metadata = sanitize_metadata(metadata or {})
        with self.connect() as conn:
            if allow_reuse and spec.job_type in CONCURRENT_GUARD_JOB_TYPES:
                active = conn.execute(
                    """
                    SELECT * FROM jobs
                    WHERE job_type = ? AND status IN ('queued', 'running')
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                    """,
                    (spec.job_type,),
                ).fetchone()
                if active:
                    job = _row_to_job(active) or {}
                    job["reused"] = True
                    return job
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
        job = _row_to_job(row) or {}
        job["reused"] = False
        return job

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return _row_to_job(row)

    def list_jobs(self, *, limit: int = 50, status: str = "", job_type: str = "") -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 200))
        where: list[str] = []
        params: list[Any] = []
        if status:
            if status == "attention":
                where.append("(status = ? OR (job_type = 'stable-check' AND status = 'failed' AND returncode = 1))")
                params.append(status)
            elif status == "failed":
                where.append("(status = ? AND NOT (job_type = 'stable-check' AND returncode = 1))")
                params.append(status)
            else:
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
                    _tail(stdout_tail, max(1000, int(self.stdout_tail_chars or JOB_STDOUT_LIMIT))),
                    _tail(stderr_tail, max(1000, int(self.stderr_tail_chars or JOB_STDERR_LIMIT))),
                    _limit(error, JOB_ERROR_LIMIT),
                    int(job_id),
                ),
            )
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (int(job_id),)).fetchone()
        return _row_to_job(row)

    def stats(self, *, limit_recent: int = 10) -> dict[str, Any]:
        with self.connect() as conn:
            total = int(conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"])
            status_rows = conn.execute("SELECT status, COUNT(*) AS c FROM jobs GROUP BY status").fetchall()
            type_rows = conn.execute("SELECT job_type, COUNT(*) AS c FROM jobs GROUP BY job_type ORDER BY c DESC, job_type").fetchall()
            recent_failed_rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('failed', 'timeout')
                  AND NOT (job_type = 'stable-check' AND status = 'failed' AND returncode = 1)
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit_recent or 10)),),
            ).fetchall()
            recent_attention_rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'attention'
                   OR (job_type = 'stable-check' AND status = 'failed' AND returncode = 1)
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit_recent or 10)),),
            ).fetchall()
            recent_running_rows = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status IN ('queued', 'running')
                ORDER BY updated_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit_recent or 10)),),
            ).fetchall()
            latest_rows = conn.execute(
                """
                SELECT * FROM jobs
                ORDER BY updated_at DESC, id DESC
                LIMIT 500
                """
            ).fetchall()
            attention_compat = int(
                conn.execute(
                    """
                    SELECT COUNT(*) AS c FROM jobs
                    WHERE job_type = 'stable-check' AND status = 'failed' AND returncode = 1
                    """
                ).fetchone()["c"]
            )
        counts = {status: 0 for status in JOB_STATUSES}
        for row in status_rows:
            counts[str(row["status"])] = int(row["c"])
        if attention_compat:
            counts["failed"] = max(0, counts.get("failed", 0) - attention_compat)
            counts["attention"] = counts.get("attention", 0) + attention_compat
        last_success_by_type: dict[str, dict[str, Any]] = {}
        last_attention_by_type: dict[str, dict[str, Any]] = {}
        last_failed_by_type: dict[str, dict[str, Any]] = {}
        for row in latest_rows:
            job = _row_to_job(row) or {}
            job_type = str(job.get("job_type") or "")
            status = normalized_job_status(job)
            if status == "success" and job_type not in last_success_by_type:
                last_success_by_type[job_type] = compact_job(job)
            if status == "attention" and job_type not in last_attention_by_type:
                last_attention_by_type[job_type] = compact_job(job)
            if status in {"failed", "timeout"} and job_type not in last_failed_by_type:
                last_failed_by_type[job_type] = compact_job(job)
        recent_failed = [compact_job(_row_to_job(row) or {}) for row in recent_failed_rows]
        recent_attention = [compact_job(_row_to_job(row) or {}) for row in recent_attention_rows]
        recent_running = [compact_job(_row_to_job(row) or {}) for row in recent_running_rows]
        return {
            "total": total,
            "running": counts.get("running", 0),
            "queued": counts.get("queued", 0),
            "success": counts.get("success", 0),
            "attention": counts.get("attention", 0),
            "failed": counts.get("failed", 0),
            "timeout": counts.get("timeout", 0),
            "cancelled": counts.get("cancelled", 0),
            "recent_failed": recent_failed,
            "recent_attention": recent_attention,
            "recent_running": recent_running,
            "by_type": {str(row["job_type"]): int(row["c"]) for row in type_rows},
            "by_status": counts,
            "last_success_by_type": last_success_by_type,
            "last_attention_by_type": last_attention_by_type,
            "last_failed_by_type": last_failed_by_type,
        }

    def cleanup_jobs(self, *, retention_days: int = 30, limit: int = 500) -> dict[str, Any]:
        safe_days = max(1, int(retention_days or 30))
        safe_limit = max(50, int(limit or 500))
        cutoff = _now() - safe_days * 86400
        with self.connect() as conn:
            total_before = int(conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"])
            old_ids = [
                int(row["id"])
                for row in conn.execute(
                    """
                    SELECT id FROM jobs
                    WHERE status NOT IN ('queued', 'running') AND created_at < ?
                    """,
                    (cutoff,),
                ).fetchall()
            ]
            overflow_ids = [
                int(row["id"])
                for row in conn.execute(
                    """
                    SELECT id FROM jobs
                    WHERE status NOT IN ('queued', 'running')
                    ORDER BY created_at DESC, id DESC
                    LIMIT -1 OFFSET ?
                    """,
                    (safe_limit,),
                ).fetchall()
            ]
            delete_ids = sorted(set(old_ids + overflow_ids))
            if delete_ids:
                placeholders = ",".join("?" for _ in delete_ids)
                conn.execute(f"DELETE FROM jobs WHERE id IN ({placeholders})", delete_ids)
            kept_count = int(conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"])
        return {
            "ok": True,
            "path": str(self.db_path),
            "deleted_count": max(0, total_before - kept_count),
            "kept_count": kept_count,
            "retention_days": safe_days,
            "limit": safe_limit,
        }

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


def compact_job(job: dict[str, Any]) -> dict[str, Any]:
    status = normalized_job_status(job)
    return {
        "id": job.get("id"),
        "job_type": job.get("job_type", ""),
        "label": job.get("label", ""),
        "status": status,
        "returncode": job.get("returncode"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "updated_at": job.get("updated_at"),
        "duration_ms": job.get("duration_ms"),
        "error_summary": extract_job_error_summary(job),
    }


def extract_job_error_summary(job: dict[str, Any] | None) -> str:
    item = job or {}
    status = normalized_job_status(item)
    if status == "success":
        return ""
    if status == "attention" and str(item.get("job_type") or "") == "stable-check":
        summary = stable_check_attention_summary(str(item.get("stdout_tail") or ""))
        return summary or zh(r"\u7a33\u5b9a\u7248\u81ea\u68c0\uff1a\u57fa\u672c\u53ef\u8fd0\u884c\uff0c\u5efa\u8bae\u5173\u6ce8\u3002")
    candidates: list[str] = []
    error = str(item.get("error") or "").strip()
    if error:
        candidates.append(error)
    stderr_lines = [line.strip() for line in str(item.get("stderr_tail") or "").splitlines() if line.strip()]
    if stderr_lines:
        candidates.append(stderr_lines[-1])
    stdout_lines = [line.strip() for line in str(item.get("stdout_tail") or "").splitlines() if line.strip()]
    for line in reversed(stdout_lines):
        if ERROR_LINE_PATTERN.search(line):
            candidates.append(line)
            break
    if not candidates:
        return ""
    return _limit(candidates[0], 500)


def stable_check_attention_summary(stdout: str) -> str:
    status_text = ""
    summary_text = ""
    noise_text = ""
    for raw_line in str(stdout or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        match = STABLE_CHECK_ATTENTION_RE.search(line)
        if not match:
            continue
        key = match.group(1)
        value = match.group(2).strip()
        if key == "状态" and not status_text:
            status_text = value
        elif key == "摘要" and not summary_text:
            summary_text = value
        elif key in {"网络重试噪声", "日志稳定性"} and ("超时" in value or "关注" in value) and not noise_text:
            noise_text = value
    parts = []
    if status_text:
        parts.append(f"稳定版自检：{status_text}")
    if summary_text:
        parts.append(f"原因：{summary_text}")
    if noise_text:
        parts.append(f"观察项：{noise_text}")
    if parts:
        parts.append("影响：非阻断，服务仍可运行")
    return _limit("；".join(parts), 500)


def job_next_action(job: dict[str, Any] | None) -> str:
    item = job or {}
    job_type = str(item.get("job_type") or "")
    status = normalized_job_status(item)
    if status in {"queued", "running"}:
        return zh(r"\u4efb\u52a1\u8fd8\u5728\u6267\u884c\uff0c\u7b49\u5f85\u5b8c\u6210\u540e\u518d\u67e5\u770b\u8be6\u60c5\u3002")
    if status == "success":
        if job_type == "update-check":
            return zh(r"\u66f4\u65b0\u68c0\u67e5\u5df2\u5b8c\u6210\uff1b\u771f\u6b63\u66f4\u65b0\u8bf7\u5728\u670d\u52a1\u5668\u6267\u884c paopao update --yes\u3002")
        return zh(r"\u4efb\u52a1\u5df2\u6210\u529f\uff0c\u53ef\u7ee7\u7eed\u89c2\u5bdf\u6216\u4fdd\u7559\u62a5\u544a\u3002")
    if status == "attention":
        return zh(r"\u8fd9\u662f\u89c2\u5bdf\u9879\uff0c\u4e0d\u662f\u4ee3\u7801\u9519\u8bef\u3002\u82e5\u63a8\u9001\u548c AI Bot \u6b63\u5e38\uff0c\u53ef\u7ee7\u7eed\u89c2\u5bdf\uff1b\u5982\u679c\u8d85\u65f6\u6301\u7eed\u589e\u52a0\uff0c\u518d\u68c0\u67e5\u670d\u52a1\u5668\u5230 Telegram/API \u7684\u7f51\u7edc\u3002")
    if job_type == "stable-check":
        return zh(r"\u5148\u67e5\u770b\u672c\u4efb\u52a1\u7684 stderr/stdout\uff0c\u518d\u6253\u5f00\u8bca\u65ad\u62a5\u544a\u6309\u5904\u7406\u6e05\u5355\u6392\u67e5\u3002")
    if job_type == "update-check":
        return zh(r"\u66f4\u65b0\u68c0\u67e5\u5931\u8d25\u65f6\uff0c\u4f18\u5148\u68c0\u67e5\u670d\u52a1\u5668\u7f51\u7edc/GitHub \u8bbf\u95ee\uff1b\u9700\u8981\u66f4\u65b0\u65f6\u4ecd\u6267\u884c paopao update --yes\u3002")
    if status in {"failed", "timeout"}:
        return zh(r"\u6253\u5f00\u4efb\u52a1\u8be6\u60c5\u67e5\u770b\u9519\u8bef\u6458\u8981\u548c stderr_tail\uff1b\u5fc5\u8981\u65f6\u518d\u5230\u65e5\u5fd7\u4e2d\u5fc3\u6309\u65f6\u95f4\u70b9\u6392\u67e5\u3002")
    return zh(r"\u53ef\u7ee7\u7eed\u67e5\u770b\u4efb\u52a1\u8be6\u60c5\u6216\u91cd\u8dd1\u540c\u7c7b\u4efb\u52a1\u3002")


def job_report_payload_from_item(job: dict[str, Any]) -> dict[str, Any]:
    command = job.get("command") if isinstance(job.get("command"), list) else []
    metadata = job.get("metadata") if isinstance(job.get("metadata"), dict) else {}
    status = normalized_job_status(job)
    report = {
        "id": job.get("id"),
        "job_type": job.get("job_type", ""),
        "label": job.get("label", ""),
        "status": status,
        "returncode": job.get("returncode"),
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "duration_ms": job.get("duration_ms"),
        "command": [redact_text(part) for part in command],
        "error_summary": extract_job_error_summary(job),
        "stdout_tail": _tail(job.get("stdout_tail", ""), JOB_REPORT_TAIL_LIMIT),
        "stderr_tail": _tail(job.get("stderr_tail", ""), JOB_REPORT_TAIL_LIMIT),
        "metadata": sanitize_metadata(metadata),
        "next_action": job_next_action(job),
    }
    lines = [
        zh(r"\u6ce1\u6ce1\u96f7\u8fbe\u540e\u53f0\u4efb\u52a1\u62a5\u544a"),
        f"id: {report['id']}",
        f"job_type: {report['job_type']}",
        f"status: {report['status']}",
        f"returncode: {report['returncode']}",
        f"created_at: {report['created_at']}",
        f"started_at: {report['started_at']}",
        f"finished_at: {report['finished_at']}",
        f"duration_ms: {report['duration_ms']}",
        "command: " + " ".join(report["command"]),
        "error_summary: " + str(report["error_summary"] or ""),
        "next_action: " + str(report["next_action"] or ""),
        "",
        "stdout_tail:",
        str(report["stdout_tail"] or ""),
        "",
        "stderr_tail:",
        str(report["stderr_tail"] or ""),
    ]
    report["text"] = redact_text("\n".join(lines))
    return report


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
        status = job_status_from_returncode(spec, int(completed.returncode))
        return store.finish_job(
            job_id,
            status=status,
            returncode=int(completed.returncode),
            stdout_tail=completed.stdout,
            stderr_tail=completed.stderr,
            error="" if status in {"success", "attention"} else completed.stderr or completed.stdout,
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
    return JobStore(
        loaded.web_jobs_db_path,
        stdout_tail_chars=max(1000, int(getattr(loaded, "web_jobs_stdout_tail_chars", JOB_STDOUT_LIMIT) or JOB_STDOUT_LIMIT)),
        stderr_tail_chars=max(1000, int(getattr(loaded, "web_jobs_stderr_tail_chars", JOB_STDERR_LIMIT) or JOB_STDERR_LIMIT)),
    )


def enrich_job(job: dict[str, Any]) -> dict[str, Any]:
    item = dict(job)
    item["status"] = normalized_job_status(item)
    item["error_summary"] = extract_job_error_summary(item)
    item["next_action"] = job_next_action(item)
    return item


def create_job_payload(job_type: str, metadata: dict[str, Any] | None = None, *, settings: Settings | None = None, start: bool = True) -> dict[str, Any]:
    store = store_for_settings(settings)
    try:
        job = store.create_job(job_type, metadata or {})
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "error": str(exc), "code": "invalid_job_type"}
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        return {"ok": False, "message": message, "error": message, "code": "job_create_failed"}
    reused = bool(job.get("reused"))
    if start and not reused:
        try:
            run_job_async(store, int(job["id"]))
        except Exception as exc:
            store.finish_job(int(job["id"]), status="failed", returncode=1, error=f"{type(exc).__name__}: {exc}")
            job = store.get_job(int(job["id"])) or job
    return {
        "ok": True,
        "job": enrich_job(job),
        "reused": reused,
        "message": zh(r"\u5df2\u6709\u540c\u7c7b\u578b\u4efb\u52a1\u6b63\u5728\u8fd0\u884c\uff0c\u5df2\u8fd4\u56de\u73b0\u6709\u4efb\u52a1") if reused else zh(r"\u4efb\u52a1\u5df2\u521b\u5efa\uff0c\u6b63\u5728\u540e\u53f0\u6267\u884c"),
    }


def jobs_payload(*, limit: int = 50, status: str = "", job_type: str = "", settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    jobs = store.list_jobs(limit=limit, status=status, job_type=job_type)
    return {"ok": True, "jobs": [enrich_job(job) for job in jobs], "count": len(jobs), "message": zh(r"\u5df2\u8bfb\u53d6\u4efb\u52a1\u8bb0\u5f55")}


def job_detail_payload(job_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    job = store.get_job(int(job_id or 0))
    if not job:
        return {"ok": False, "message": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "error": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "code": "not_found"}
    return {"ok": True, "job": enrich_job(job), "message": zh(r"\u5df2\u8bfb\u53d6\u4efb\u52a1\u8be6\u60c5")}


def cancel_job_payload(job_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    result = store.cancel_job(int(job_id or 0))
    result.setdefault("message", zh(r"\u4efb\u52a1\u53d6\u6d88\u8bf7\u6c42\u5df2\u5904\u7406"))
    return result


def recent_job_payload(job_type: str, *, settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    jobs = store.list_jobs(limit=1, job_type=job_type)
    return enrich_job(jobs[0]) if jobs else {}


def jobs_stats_payload(*, settings: Settings | None = None) -> dict[str, Any]:
    try:
        store = store_for_settings(settings)
        stats = store.stats()
        return {"ok": True, **stats, "message": zh(r"\u5df2\u8bfb\u53d6\u540e\u53f0\u4efb\u52a1\u7edf\u8ba1")}
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        return {
            "ok": False,
            "total": 0,
            "running": 0,
            "queued": 0,
            "success": 0,
            "attention": 0,
            "failed": 0,
            "timeout": 0,
            "cancelled": 0,
            "recent_failed": [],
            "recent_attention": [],
            "recent_running": [],
            "by_type": {},
            "by_status": {},
            "last_success_by_type": {},
            "last_attention_by_type": {},
            "last_failed_by_type": {},
            "error": message,
            "message": message,
        }


def job_report_payload(job_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    job = store.get_job(int(job_id or 0))
    if not job:
        return {"ok": False, "message": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "error": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "code": "not_found"}
    return {"ok": True, "report": job_report_payload_from_item(job), "message": zh(r"\u5df2\u751f\u6210\u4efb\u52a1\u62a5\u544a")}


def rerun_job_payload(job_id: int, *, settings: Settings | None = None, start: bool = True) -> dict[str, Any]:
    store = store_for_settings(settings)
    job = store.get_job(int(job_id or 0))
    if not job:
        return {"ok": False, "message": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "error": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "code": "not_found"}
    try:
        validate_job_type(str(job.get("job_type") or ""))
    except ValueError as exc:
        return {"ok": False, "message": str(exc), "error": str(exc), "code": "invalid_job_type"}
    result = create_job_payload(str(job["job_type"]), {"source": "api/jobs/rerun", "rerun_from": int(job["id"])}, settings=settings, start=start)
    result["rerun_from"] = int(job["id"])
    return result


def cleanup_jobs_payload(
    *,
    retention_days: int | None = None,
    limit: int | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    store = store_for_settings(loaded)
    result = store.cleanup_jobs(
        retention_days=int(retention_days if retention_days not in {None, ""} else loaded.web_jobs_retention_days),
        limit=int(limit if limit not in {None, ""} else loaded.web_jobs_limit),
    )
    result["message"] = zh(r"\u65e7\u4efb\u52a1\u6e05\u7406\u5b8c\u6210")
    return result
