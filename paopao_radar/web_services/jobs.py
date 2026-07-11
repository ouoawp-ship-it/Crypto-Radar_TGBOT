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
from ..runtime_cache import invalidate as invalidate_runtime_cache
from .api_core import api_list_payload


DEFAULT_JOBS_DB_PATH = BASE_DIR / "data" / "jobs.db"
JOB_STATUSES = {"queued", "running", "success", "attention", "failed", "timeout", "cancelled"}
JOB_STDOUT_LIMIT = 12000
JOB_STDERR_LIMIT = 6000
JOB_ERROR_LIMIT = 1000
JOB_REPORT_TAIL_LIMIT = 12000
CONCURRENT_GUARD_JOB_TYPES = {
    "stable-check", "doctor", "readiness", "cleanup", "update-check", "api-self-test",
    "outcome-scan", "lifecycle-scan", "lifecycle-backfill", "lifecycle-intelligence",
    "lifecycle-replay", "lifecycle-analytics", "lifecycle-replay-rebuild",
    "lifecycle-outcome-link", "lifecycle-outcome-backfill", "lifecycle-outcome-reconcile",
    "lifecycle-outcome-refresh-analytics",
    "lifecycle-outcome-refresh-candidates", "lifecycle-outcome-incremental-backfill",
    "lifecycle-outcome-classify-gaps", "lifecycle-outcome-quality-report", "lifecycle-calibration-readiness",
}
LIFECYCLE_RESEARCH_JOB_TYPES = {
    "lifecycle-intelligence",
    "lifecycle-replay",
    "lifecycle-analytics",
    "lifecycle-replay-rebuild",
    "lifecycle-outcome-link",
    "lifecycle-outcome-backfill",
    "lifecycle-outcome-reconcile",
    "lifecycle-outcome-refresh-analytics",
    "lifecycle-outcome-refresh-candidates",
    "lifecycle-outcome-incremental-backfill",
    "lifecycle-outcome-classify-gaps",
    "lifecycle-outcome-quality-report",
    "lifecycle-calibration-readiness",
}
LIFECYCLE_OUTCOME_SCOPE_JOB_TYPES = {
    "lifecycle-outcome-link",
    "lifecycle-outcome-backfill",
    "lifecycle-outcome-reconcile",
    "lifecycle-outcome-refresh-candidates",
    "lifecycle-outcome-incremental-backfill",
    "lifecycle-outcome-classify-gaps",
    "lifecycle-outcome-quality-report",
    "lifecycle-calibration-readiness",
}
LIFECYCLE_OUTCOME_QUALITY_COMMANDS = {
    "lifecycle-outcome-refresh-candidates": "lifecycle-outcome-refresh-candidates",
    "lifecycle-outcome-incremental-backfill": "lifecycle-outcome-incremental",
    "lifecycle-outcome-classify-gaps": "lifecycle-outcome-classify-gaps",
    "lifecycle-outcome-quality-report": "lifecycle-outcome-quality",
    "lifecycle-calibration-readiness": "lifecycle-calibration-readiness",
}
ERROR_LINE_PATTERN = re.compile(r"(?i)(failed|error|traceback|timeout|exception|\u5f02\u5e38|\u9519\u8bef|\u5931\u8d25|\u8d85\u65f6)")
STABLE_CHECK_ATTENTION_RE = re.compile(r"(状态|摘要|网络重试噪声|日志稳定性):\s*(.+)")


def invalidate_job_runtime_cache(job_type: str = "") -> None:
    invalidate_runtime_cache("dashboard:")
    if str(job_type).startswith("lifecycle-"):
        invalidate_runtime_cache("lifecycle:")
    if job_type in {"stable-check", "doctor", "readiness", "cleanup"}:
        invalidate_runtime_cache("stable:")
    if job_type == "update-check":
        invalidate_runtime_cache("ops:")


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
    "outcome-scan": JobSpec("outcome-scan", zh(r"\u4fe1\u53f7\u7ed3\u679c\u8ffd\u8e2a\u626b\u63cf"), _python_command("outcome-scan"), 300),
    "lifecycle-scan": JobSpec("lifecycle-scan", zh(r"\u751f\u547d\u5468\u671f\u8ddf\u968f\u626b\u63cf"), _python_command("lifecycle-scan"), 300),
    "lifecycle-backfill": JobSpec("lifecycle-backfill", zh(r"\u751f\u547d\u5468\u671f\u56de\u586b"), _python_command("lifecycle-backfill", "--lookback-hours", "168"), 600),
    "lifecycle-intelligence": JobSpec("lifecycle-intelligence", "生命周期智能评价", _python_command("lifecycle-intelligence"), 900),
    "lifecycle-replay": JobSpec("lifecycle-replay", "生命周期回放回填", _python_command("lifecycle-replay-backfill", "--limit", "500"), 1200),
    "lifecycle-analytics": JobSpec("lifecycle-analytics", "生命周期历史统计", _python_command("lifecycle-analytics"), 900),
    "lifecycle-replay-rebuild": JobSpec("lifecycle-replay-rebuild", "生命周期回放强制重建", _python_command("lifecycle-replay-backfill", "--limit", "500", "--force-rebuild"), 1200),
    "lifecycle-outcome-link": JobSpec("lifecycle-outcome-link", "生命周期 Outcome 关联", _python_command("lifecycle-outcome-link", "--limit", "200"), 300),
    "lifecycle-outcome-backfill": JobSpec("lifecycle-outcome-backfill", "生命周期 Outcome 补算", _python_command("lifecycle-outcome-backfill", "--limit", "200"), 1200),
    "lifecycle-outcome-reconcile": JobSpec("lifecycle-outcome-reconcile", "生命周期 Outcome 一致性检查", _python_command("lifecycle-outcome-reconcile", "--limit", "200"), 600),
    "lifecycle-outcome-refresh-analytics": JobSpec("lifecycle-outcome-refresh-analytics", "刷新生命周期 Outcome 统计", _python_command("lifecycle-analytics", "--force-rebuild"), 900),
    "lifecycle-outcome-refresh-candidates": JobSpec("lifecycle-outcome-refresh-candidates", "刷新生命周期 Outcome 候选", _python_command("lifecycle-outcome-refresh-candidates", "--limit", "200"), 600),
    "lifecycle-outcome-incremental-backfill": JobSpec("lifecycle-outcome-incremental-backfill", "生命周期 Outcome 增量补算", _python_command("lifecycle-outcome-incremental", "--limit", "200"), 1200),
    "lifecycle-outcome-classify-gaps": JobSpec("lifecycle-outcome-classify-gaps", "生命周期 Outcome 缺口分类", _python_command("lifecycle-outcome-classify-gaps", "--limit", "200"), 600),
    "lifecycle-outcome-quality-report": JobSpec("lifecycle-outcome-quality-report", "生命周期 Outcome 数据质量报告", _python_command("lifecycle-outcome-quality"), 900),
    "lifecycle-calibration-readiness": JobSpec("lifecycle-calibration-readiness", "生命周期模型校准准入检查", _python_command("lifecycle-calibration-readiness"), 300),
    "update-check": JobSpec("update-check", zh(r"\u68c0\u67e5 GitHub \u66f4\u65b0"), ["bash", "scripts/update_server.sh", "--check"], 180),
    "api-self-test": JobSpec("api-self-test", zh(r"Web API \u81ea\u68c0"), ["internal", "api-self-test"], 60, internal=True),
}


def _lifecycle_job_command(spec: JobSpec, metadata: dict[str, Any]) -> list[str]:
    """Build bounded CLI arguments for authenticated lifecycle job requests."""
    raw_symbol = str(metadata.get("symbol") or "").strip().upper()
    raw_lifecycle_id = metadata.get("lifecycle_id")
    outcome_job = spec.job_type in LIFECYCLE_OUTCOME_SCOPE_JOB_TYPES
    if outcome_job and raw_symbol and not re.fullmatch(r"[A-Z0-9]{2,24}(?:USDT)?", raw_symbol):
        raise ValueError("invalid lifecycle symbol")
    if outcome_job and isinstance(raw_lifecycle_id, bool):
        raise ValueError("invalid lifecycle id")
    symbol = re.sub(r"[^A-Z0-9]", "", raw_symbol)
    if symbol and not symbol.endswith("USDT"):
        symbol += "USDT"
    if not re.fullmatch(r"[A-Z0-9]{2,24}USDT", symbol):
        if outcome_job and raw_symbol:
            raise ValueError("invalid lifecycle symbol")
        symbol = ""
    try:
        lifecycle_id = max(0, int(metadata.get("lifecycle_id") or 0))
    except (TypeError, ValueError):
        if outcome_job and raw_lifecycle_id is not None and raw_lifecycle_id != "":
            raise ValueError("invalid lifecycle id")
        lifecycle_id = 0
    if outcome_job and raw_lifecycle_id is not None and raw_lifecycle_id != "" and lifecycle_id <= 0:
        raise ValueError("invalid lifecycle id")

    if spec.job_type == "lifecycle-intelligence" and symbol:
        return _python_command("lifecycle-intelligence", "--symbol", symbol)
    if spec.job_type in {"lifecycle-replay", "lifecycle-replay-rebuild"} and (symbol or lifecycle_id):
        command = ["lifecycle-replay"]
        if lifecycle_id:
            command.extend(["--lifecycle-id", str(lifecycle_id)])
        else:
            command.extend(["--symbol", symbol])
        if spec.job_type == "lifecycle-replay-rebuild":
            command.append("--force-rebuild")
        return _python_command(*command)
    if spec.job_type in LIFECYCLE_OUTCOME_SCOPE_JOB_TYPES:
        def strict_flag(name: str) -> bool:
            value = metadata.get(name, False)
            if value in (None, ""):
                return False
            if not isinstance(value, bool):
                raise ValueError(f"invalid boolean flag: {name}")
            return value

        command = [LIFECYCLE_OUTCOME_QUALITY_COMMANDS.get(spec.job_type, spec.job_type)]
        if lifecycle_id:
            command.extend(["--lifecycle-id", str(lifecycle_id)])
        if symbol:
            command.extend(["--symbol", symbol])
        try:
            limit = max(1, min(int(metadata.get("limit") or 200), 1000))
        except (TypeError, ValueError):
            limit = 200
        command.extend(["--limit", str(limit)])
        horizon = str(metadata.get("horizon") or "").strip().lower()
        if horizon and horizon not in {"1h", "4h", "24h", "72h"}:
            raise ValueError("invalid lifecycle outcome horizon")
        if horizon in {"1h", "4h", "24h", "72h"}:
            command.extend(["--horizon", horizon])
        module = str(metadata.get("module") or "").strip().lower()
        if module and not re.fullmatch(r"[a-z0-9_.:-]{1,64}", module):
            raise ValueError("invalid lifecycle outcome module")
        if module:
            command.extend(["--module", module])
        if spec.job_type in LIFECYCLE_OUTCOME_QUALITY_COMMANDS and strict_flag("force"):
            command.append("--force")
        if strict_flag("force_relink"):
            command.append("--force-relink")
        if spec.job_type == "lifecycle-outcome-backfill" and strict_flag("force_outcome_rebuild"):
            command.append("--force-outcome-rebuild")
        if spec.job_type == "lifecycle-outcome-reconcile" and strict_flag("repair"):
            command.append("--repair")
        return _python_command(*command)
    return list(spec.command)


LONG_ACTION_JOB_TYPES = {
    "stable-check", "doctor", "readiness", "cleanup", "outcome-scan", "lifecycle-scan",
    "lifecycle-backfill", "lifecycle-intelligence", "lifecycle-replay", "lifecycle-analytics",
    "lifecycle-replay-rebuild",
    "lifecycle-outcome-link", "lifecycle-outcome-backfill", "lifecycle-outcome-reconcile",
    "lifecycle-outcome-refresh-analytics",
    "lifecycle-outcome-refresh-candidates", "lifecycle-outcome-incremental-backfill",
    "lifecycle-outcome-classify-gaps", "lifecycle-outcome-quality-report", "lifecycle-calibration-readiness",
}


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
        command = _lifecycle_job_command(spec, safe_metadata)
        with self.connect() as conn:
            # Serialize the active-job check and insert so concurrent API and
            # scheduler submissions cannot both pass the guard.
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            if allow_reuse and spec.job_type in CONCURRENT_GUARD_JOB_TYPES:
                if spec.job_type in LIFECYCLE_RESEARCH_JOB_TYPES:
                    guarded_types = sorted(LIFECYCLE_RESEARCH_JOB_TYPES)
                    placeholders = ",".join("?" for _ in guarded_types)
                    active = conn.execute(
                        f"""
                        SELECT * FROM jobs
                        WHERE job_type IN ({placeholders})
                          AND status IN ('queued', 'running')
                        ORDER BY created_at DESC, id DESC
                        LIMIT 1
                        """,
                        guarded_types,
                    ).fetchone()
                else:
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
                    _json_dumps(command),
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

    def list_jobs(
        self,
        *,
        limit: int = 50,
        status: str = "",
        job_type: str = "",
        cursor: int | None = None,
        offset: int = 0,
        sort_field: str = "id",
        sort_direction: str = "desc",
        start_ts: int | None = None,
        end_ts: int | None = None,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 200))
        where: list[str] = []
        params: list[Any] = []
        if cursor:
            where.append("id > ?" if str(sort_direction).lower() == "asc" and str(sort_field) == "id" else "id < ?")
            params.append(int(cursor))
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
        if start_ts is not None:
            where.append("created_at >= ?")
            params.append(int(start_ts))
        if end_ts is not None:
            where.append("created_at <= ?")
            params.append(int(end_ts))
        sql = "SELECT * FROM jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        allowed_sort_fields = {"id", "created_at", "updated_at", "status", "job_type", "returncode"}
        safe_sort_field = str(sort_field or "id")
        if safe_sort_field not in allowed_sort_fields:
            safe_sort_field = "id"
        safe_sort_direction = "ASC" if str(sort_direction or "").lower() == "asc" else "DESC"
        tie_direction = "ASC" if safe_sort_direction == "ASC" else "DESC"
        sql += f" ORDER BY {safe_sort_field} {safe_sort_direction}, id {tie_direction} LIMIT ? OFFSET ?"
        params.extend([safe_limit, max(0, int(offset or 0))])
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
    try:
        from .api_core import api_contract_self_test

        payload = api_contract_self_test()
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        payload = {"ok": False, "checks": [], "errors": [message]}
    errors = list(payload.get("errors") or [])
    stdout = _json_dumps(payload)
    return (0 if not errors else 1, stdout, "\n".join(errors))


def execute_job(store: JobStore, job_id: int) -> dict[str, Any] | None:
    job = store.mark_running(job_id)
    if not job:
        return None
    if job.get("status") != "running":
        return job
    job_type = str(job.get("job_type") or "")
    invalidate_job_runtime_cache(job_type)

    def finish_job(**kwargs: Any) -> dict[str, Any] | None:
        result = store.finish_job(job_id, **kwargs)
        invalidate_job_runtime_cache(job_type)
        return result

    try:
        spec = validate_job_type(str(job["job_type"]))
    except ValueError as exc:
        return finish_job(status="failed", returncode=2, error=str(exc), stderr_tail=str(exc))
    try:
        command = _lifecycle_job_command(spec, dict(job.get("metadata") or {}))
    except ValueError as exc:
        return finish_job(status="failed", returncode=2, error=str(exc), stderr_tail=str(exc))
    preflight_error = _preflight_command(spec)
    if preflight_error:
        return finish_job(status="failed", returncode=127, error=preflight_error, stderr_tail=preflight_error)
    if spec.internal:
        try:
            returncode, stdout, stderr = _run_api_self_test()
            return finish_job(
                status="success" if returncode == 0 else "failed",
                returncode=returncode,
                stdout_tail=stdout,
                stderr_tail=stderr,
                error="" if returncode == 0 else zh(r"Web API \u81ea\u68c0\u672a\u901a\u8fc7"),
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            return finish_job(status="failed", returncode=1, error=message, stderr_tail=message)
    try:
        completed = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=spec.timeout_sec,
            shell=False,
        )
        status = job_status_from_returncode(spec, int(completed.returncode))
        return finish_job(
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
        return finish_job(
            status="timeout",
            returncode=124,
            stdout_tail=stdout,
            stderr_tail=stderr or message,
            error=message,
        )
    except OSError as exc:
        message = f"{type(exc).__name__}: {exc}"
        return finish_job(status="failed", returncode=127, stderr_tail=message, error=message)


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
        code = "invalid_job_scope" if job_type in LIFECYCLE_OUTCOME_SCOPE_JOB_TYPES else "invalid_job_type"
        return {"ok": False, "message": str(exc), "error": str(exc), "code": code}
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        return {"ok": False, "message": message, "error": message, "code": "job_create_failed"}
    invalidate_job_runtime_cache(job_type)
    reused = bool(job.get("reused"))
    active_type = str(job.get("job_type") or "")
    if reused and job_type in LIFECYCLE_RESEARCH_JOB_TYPES and active_type != job_type:
        return {
            "ok": False,
            "job_id": int(job.get("id") or 0),
            "job": enrich_job(job),
            "reused": False,
            "blocked_by_job_type": active_type,
            "message": "已有其他生命周期研究任务正在运行，请等待完成后重试。",
            "code": "lifecycle_research_busy",
        }
    if start and not reused:
        try:
            run_job_async(store, int(job["id"]))
        except Exception as exc:
            store.finish_job(int(job["id"]), status="failed", returncode=1, error=f"{type(exc).__name__}: {exc}")
            invalidate_job_runtime_cache(job_type)
            job = store.get_job(int(job["id"])) or job
    return {
        "ok": True,
        "job_id": int(job.get("id") or 0),
        "job": enrich_job(job),
        "reused": reused,
        "message": zh(r"\u5df2\u6709\u540c\u7c7b\u578b\u4efb\u52a1\u6b63\u5728\u8fd0\u884c\uff0c\u5df2\u8fd4\u56de\u73b0\u6709\u4efb\u52a1") if reused else zh(r"\u4efb\u52a1\u5df2\u521b\u5efa\uff0c\u6b63\u5728\u540e\u53f0\u6267\u884c"),
    }


_LIFECYCLE_SCHEDULER_LOCK = threading.RLock()
_LIFECYCLE_SCHEDULER_THREAD: threading.Thread | None = None
_LIFECYCLE_SCHEDULER_STOP = threading.Event()
_LIFECYCLE_OUTCOME_SCHEDULER_FIRST_SEEN: dict[tuple[str, str], int] = {}


def lifecycle_intelligence_scheduler_tick(
    *,
    settings: Settings | None = None,
    now: int | None = None,
    start: bool = True,
) -> dict[str, Any]:
    """Submit due lifecycle research jobs without running work in the web thread."""
    loaded = settings or Settings.load()
    intelligence_enabled = bool(getattr(loaded, "lifecycle_intelligence_enable", True))
    outcome_enabled = bool(getattr(loaded, "lifecycle_outcome_backfill_enable", True))
    incremental_enabled = bool(getattr(loaded, "lifecycle_outcome_incremental_enable", True))
    if not intelligence_enabled and not outcome_enabled and not incremental_enabled:
        return {"ok": True, "enabled": False, "submitted": [], "jobs": []}
    timestamp = int(_now() if now is None else now)
    store = store_for_settings(loaded)
    schedule: list[tuple[str, int]] = []
    if intelligence_enabled:
        schedule.extend((
            ("lifecycle-intelligence", max(60, int(getattr(loaded, "lifecycle_intelligence_interval_sec", 900) or 900))),
            ("lifecycle-replay", max(60, int(getattr(loaded, "lifecycle_replay_interval_sec", 3600) or 3600))),
            ("lifecycle-analytics", max(60, int(getattr(loaded, "lifecycle_analytics_interval_sec", 21600) or 21600))),
        ))
    if outcome_enabled and not incremental_enabled:
        # Legacy scheduler compatibility. Once the candidate state machine is
        # enabled, the bounded incremental worker replaces this broad scan.
        schedule.append((
            "lifecycle-outcome-backfill",
            max(300, int(getattr(loaded, "lifecycle_outcome_backfill_interval_sec", 3600) or 3600)),
        ))
    if outcome_enabled or incremental_enabled:
        schedule.append(("lifecycle-outcome-reconcile", 86400))
    if incremental_enabled:
        incremental_interval = max(
            300,
            int(getattr(loaded, "lifecycle_outcome_incremental_interval_sec", 3600) or 3600),
        )
        schedule.extend((
            ("lifecycle-outcome-refresh-candidates", 900),
            ("lifecycle-outcome-incremental-backfill", incremental_interval),
            ("lifecycle-outcome-quality-report", 21600),
            ("lifecycle-calibration-readiness", 21600),
        ))
    submitted: list[str] = []
    results: list[dict[str, Any]] = []
    for job_type, interval_sec in schedule:
        recent = store.list_jobs(limit=1, job_type=job_type)
        last_created = int((recent[0] if recent else {}).get("created_at") or 0)
        if job_type in LIFECYCLE_OUTCOME_SCOPE_JOB_TYPES and not last_created:
            # A web-service restart must not immediately launch a historical
            # backfill or reconciliation job.  Give operators one full
            # configured interval to deploy, inspect, and run the documented
            # phased backfill manually.  The guard is process-local on purpose:
            # once a scheduled job exists, its durable created_at controls the
            # following ticks.
            scheduler_key = (str(getattr(loaded, "web_jobs_db_path", "")), job_type)
            with _LIFECYCLE_SCHEDULER_LOCK:
                first_seen = _LIFECYCLE_OUTCOME_SCHEDULER_FIRST_SEEN.setdefault(scheduler_key, timestamp)
            if timestamp - first_seen < interval_sec:
                continue
        if last_created and timestamp - last_created < interval_sec:
            continue
        metadata: dict[str, Any] = {
            "source": "lifecycle-research-scheduler",
            "scheduled_at": timestamp,
        }
        if job_type in {
            "lifecycle-outcome-refresh-candidates",
            "lifecycle-outcome-incremental-backfill",
        }:
            metadata["limit"] = max(
                1,
                min(
                    int(getattr(loaded, "lifecycle_outcome_incremental_batch_size", 200) or 200),
                    1000,
                ),
            )
        result = create_job_payload(
            job_type,
            metadata,
            settings=loaded,
            start=start,
        )
        results.append(result)
        if result.get("code") == "lifecycle_research_busy":
            break
        if result.get("ok"):
            active_type = str((result.get("job") or {}).get("job_type") or "")
            if not result.get("reused") or active_type == job_type:
                submitted.append(job_type)
            # Keep SQLite pressure bounded: one research job may be active at a
            # time. A reused job from another dimension blocks this tick.
            break
    return {
        "ok": all(item.get("ok", False) or item.get("code") == "lifecycle_research_busy" for item in results),
        "enabled": True,
        "submitted": submitted,
        "jobs": results,
    }


def start_lifecycle_intelligence_scheduler(*, settings: Settings | None = None) -> threading.Thread | None:
    """Start one daemon scheduler per web process; workers remain subprocess jobs."""
    loaded = settings or Settings.load()
    if not (
        bool(getattr(loaded, "lifecycle_intelligence_enable", True))
        or bool(getattr(loaded, "lifecycle_outcome_backfill_enable", True))
        or bool(getattr(loaded, "lifecycle_outcome_incremental_enable", True))
    ):
        return None
    global _LIFECYCLE_SCHEDULER_THREAD
    with _LIFECYCLE_SCHEDULER_LOCK:
        if _LIFECYCLE_SCHEDULER_THREAD and _LIFECYCLE_SCHEDULER_THREAD.is_alive():
            return _LIFECYCLE_SCHEDULER_THREAD
        _LIFECYCLE_SCHEDULER_STOP.clear()

        def run() -> None:
            while not _LIFECYCLE_SCHEDULER_STOP.is_set():
                try:
                    lifecycle_intelligence_scheduler_tick(settings=loaded)
                except Exception as exc:
                    sys.stderr.write(f"[web] lifecycle intelligence scheduler failed: {type(exc).__name__}: {exc}\n")
                _LIFECYCLE_SCHEDULER_STOP.wait(60)

        _LIFECYCLE_SCHEDULER_THREAD = threading.Thread(
            target=run,
            name="lifecycle-intelligence-scheduler",
            daemon=True,
        )
        _LIFECYCLE_SCHEDULER_THREAD.start()
        return _LIFECYCLE_SCHEDULER_THREAD


def jobs_payload(
    *,
    limit: int = 50,
    status: str = "",
    job_type: str = "",
    cursor: int | None = None,
    offset: int = 0,
    sort_field: str = "id",
    sort_direction: str = "desc",
    start_ts: int | None = None,
    end_ts: int | None = None,
    pagination: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    sort: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    store = store_for_settings(settings)
    jobs = store.list_jobs(
        limit=limit,
        status=status,
        job_type=job_type,
        cursor=cursor,
        offset=offset,
        sort_field=sort_field,
        sort_direction=sort_direction,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    enriched = [enrich_job(job) for job in jobs]
    next_cursor = enriched[-1]["id"] if enriched else None
    return api_list_payload(
        enriched,
        count=len(enriched),
        next_cursor=next_cursor,
        message=zh(r"\u5df2\u8bfb\u53d6\u4efb\u52a1\u8bb0\u5f55"),
        pagination=pagination,
        filters=filters,
        sort=sort,
        alias="jobs",
    )


def job_detail_payload(job_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    job = store.get_job(int(job_id or 0))
    if not job:
        return {"ok": False, "message": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "error": zh(r"\u4efb\u52a1\u4e0d\u5b58\u5728"), "code": "not_found"}
    return {"ok": True, "job": enrich_job(job), "message": zh(r"\u5df2\u8bfb\u53d6\u4efb\u52a1\u8be6\u60c5")}


def cancel_job_payload(job_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    store = store_for_settings(settings)
    result = store.cancel_job(int(job_id or 0))
    if result.get("ok"):
        job = result.get("job") if isinstance(result.get("job"), dict) else {}
        invalidate_job_runtime_cache(str(job.get("job_type") or ""))
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
    original_metadata = dict(job.get("metadata") or {})
    metadata = {
        key: original_metadata[key]
        for key in (
            "symbol", "lifecycle_id", "limit", "horizon", "force_relink",
            "force_outcome_rebuild", "repair", "module", "force",
        )
        if key in original_metadata
    }
    metadata.update({"source": "api/jobs/rerun", "rerun_from": int(job["id"])})
    result = create_job_payload(str(job["job_type"]), metadata, settings=settings, start=start)
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
    invalidate_job_runtime_cache("cleanup")
    result["message"] = zh(r"\u65e7\u4efb\u52a1\u6e05\u7406\u5b8c\u6210")
    return result
