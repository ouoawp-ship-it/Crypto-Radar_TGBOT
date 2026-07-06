from __future__ import annotations

import re
import subprocess
from typing import Any

from ..config import BASE_DIR, Settings
from .jobs import create_job_payload, recent_job_payload, redact_text, zh


VERSION_RE = re.compile(r"(?:当前版本|current).*?:\s*([^\s(]+)\s*\(([^)]+)\)", re.IGNORECASE)
REMOTE_RE = re.compile(r"(?:GitHub版本|remote|github).*?:\s*([^\s(]+)\s*\(([^)]+)\)", re.IGNORECASE)


def _current_version() -> str:
    path = BASE_DIR / "VERSION"
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _current_commit() -> str:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            shell=False,
        )
        return completed.stdout.strip() if completed.returncode == 0 else ""
    except Exception:
        return ""


def parse_update_check_output(stdout: Any, stderr: Any = "") -> dict[str, Any]:
    text = redact_text(stdout)
    err = redact_text(stderr)
    result: dict[str, Any] = {
        "current_version": "",
        "current_commit": "",
        "remote_version": "",
        "remote_commit": "",
        "branch": "",
        "update_available": None,
        "summary": "",
        "warnings": [],
        "errors": [],
        "command_hint": "服务器执行 paopao update --yes",
    }
    for line in text.splitlines():
        clean = line.strip()
        if not clean:
            continue
        current_match = VERSION_RE.search(clean)
        if current_match:
            result["current_version"] = current_match.group(1)
            result["current_commit"] = current_match.group(2)
        remote_match = REMOTE_RE.search(clean)
        if remote_match:
            result["remote_version"] = remote_match.group(1)
            result["remote_commit"] = remote_match.group(2)
        lower = clean.lower()
        if "already" in lower or "最新版本" in clean or "不需要更新" in clean:
            result["update_available"] = False
            result["summary"] = clean
        if "updating" in lower or "有更新" in clean or "发现新版本" in clean:
            result["update_available"] = True
            result["summary"] = clean
        if "warning" in lower or "警告" in clean:
            result["warnings"].append(clean[:300])
        if "error" in lower or "failed" in lower or "错误" in clean or "失败" in clean:
            result["errors"].append(clean[:300])
        if "branch" in lower or "分支" in clean:
            result["branch"] = clean.split(":", 1)[-1].strip() if ":" in clean else clean
    if err.strip():
        for line in err.splitlines():
            clean = line.strip()
            if clean:
                result["errors"].append(clean[:300])
    if result["update_available"] is None and result["current_version"] and result["remote_version"]:
        result["update_available"] = (
            result["current_version"] != result["remote_version"]
            or bool(result["current_commit"] and result["remote_commit"] and result["current_commit"] != result["remote_commit"])
        )
    if not result["summary"]:
        if result["update_available"] is True:
            result["summary"] = "检测到 GitHub 有可用更新。"
        elif result["update_available"] is False:
            result["summary"] = "当前已经是最新版本。"
        else:
            result["summary"] = "尚未获得可判断的更新检查结果。"
    return result


def update_check_status_payload(*, settings: Settings | None = None) -> dict[str, Any]:
    job = recent_job_payload("update-check", settings=settings)
    parsed = parse_update_check_output(job.get("stdout_tail", "") if job else "", job.get("stderr_tail", "") if job else "")
    return {
        "ok": True,
        "job": job,
        "current_version": parsed.get("current_version") or _current_version(),
        "current_commit": parsed.get("current_commit") or _current_commit(),
        "latest_check_job": job,
        "update_available": parsed.get("update_available"),
        "summary": parsed.get("summary"),
        "next_action": (
            zh(r"\u5982\u679c\u786e\u8ba4\u9700\u8981\u66f4\u65b0\uff0c\u8bf7\u5728\u670d\u52a1\u5668\u6267\u884c paopao update --yes\u3002")
            if parsed.get("update_available") is True
            else zh(r"\u6682\u65e0\u9700\u8981\u6267\u884c Web \u81ea\u66f4\u65b0\uff1b\u8981\u771f\u6b63\u66f4\u65b0\u4ecd\u4f7f\u7528\u670d\u52a1\u5668\u547d\u4ee4\u3002")
        ),
        "command_hint": "服务器执行 paopao update --yes",
        "parsed": parsed,
        "message": zh(
            r"\u66f4\u65b0\u68c0\u67e5\u5df2\u79fb\u5230\u4efb\u52a1\u4e2d\u5fc3\u3002"
            r"\u8bf7\u5728\u4efb\u52a1\u4e2d\u5fc3\u53d1\u8d77\u201c\u66f4\u65b0\u68c0\u67e5\u201d\uff1b"
            r"\u771f\u6b63\u66f4\u65b0\u4ecd\u5efa\u8bae\u5728\u670d\u52a1\u5668\u6267\u884c paopao update --yes\u3002"
        ),
    }


def create_update_check_job(*, settings: Settings | None = None) -> dict[str, Any]:
    return create_job_payload("update-check", {"source": "web-update-check"}, settings=settings)
