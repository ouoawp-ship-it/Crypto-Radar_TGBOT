from __future__ import annotations

from typing import Any

from ..config import Settings
from .api_core import api_ok, redact_api_payload
from .jobs import jobs_payload, jobs_stats_payload
from .ops import update_check_status_payload


def _compact_services(summary: dict[str, Any]) -> dict[str, Any]:
    services = dict(summary.get("services") or {})
    return {
        "main": services.get("main", {}),
        "structure": services.get("structure", {}),
        "web": services.get("web", {}),
        "ai": services.get("ai", {}),
    }


def _compact_resources(server_status: dict[str, Any]) -> dict[str, Any]:
    disks = list(server_status.get("disks") or [])
    primary_disk = disks[0] if disks else {}
    return {
        "cpu": server_status.get("cpu", {}),
        "memory": server_status.get("memory", {}),
        "disk": primary_disk,
        "disks": disks[:4],
    }


def dashboard_payload(*, settings: Settings | None = None) -> dict[str, Any]:
    loaded = settings or Settings.load()

    from .. import web as web_module

    summary = web_module.summary_payload()
    git = dict(summary.get("git") or web_module.git_info())
    server_status = web_module.server_status_payload()
    signal_stats = web_module.signals_stats_payload(window_sec=86400, settings=loaded)
    signal_latest = web_module.signals_payload(limit=5, settings=loaded)
    job_stats = jobs_stats_payload(settings=loaded)
    job_latest = jobs_payload(limit=5, settings=loaded)
    update_status = update_check_status_payload(settings=loaded)

    recent_errors = list(summary.get("recent_errors") or [])
    recent_failed_jobs = list(job_stats.get("recent_failed") or [])
    warning_count = len(recent_errors) + len(recent_failed_jobs)
    problem_status = "attention" if warning_count else "ok"

    data = {
        "generated_at": summary.get("updated_at") or web_module.now_text(),
        "version": {
            "version": git.get("version", ""),
            "commit": git.get("commit", ""),
            "branch": git.get("branch", ""),
        },
        "services": _compact_services(summary),
        "signals": {
            "total_24h": signal_stats.get("total", 0),
            "sent_24h": signal_stats.get("sent", 0),
            "failed_24h": signal_stats.get("failed", 0),
            "top_symbols": signal_stats.get("top_symbols", []),
            "latest": signal_latest.get("items", []),
        },
        "jobs": {
            "running": int(job_stats.get("running", 0) or 0) + int(job_stats.get("queued", 0) or 0),
            "failed_recent": recent_failed_jobs,
            "latest": job_latest.get("jobs", []),
            "stats": job_stats,
        },
        "problems": {
            "status": problem_status,
            "critical": 0,
            "warning": warning_count,
        },
        "resources": _compact_resources(server_status),
        "update": {
            "current_version": update_status.get("current_version") or git.get("version", ""),
            "current_commit": update_status.get("current_commit") or git.get("commit", ""),
            "latest_check_job": update_status.get("latest_check_job") or update_status.get("job") or {},
            "update_available": update_status.get("update_available"),
            "summary": update_status.get("summary", ""),
        },
    }
    return api_ok(
        redact_api_payload(data),
        message="Dashboard payload loaded",
        summary=summary,
    )
