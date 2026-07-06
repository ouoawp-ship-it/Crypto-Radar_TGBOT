from __future__ import annotations

from typing import Any

from ..config import Settings
from .jobs import create_job_payload, recent_job_payload, zh


def update_check_status_payload(*, settings: Settings | None = None) -> dict[str, Any]:
    job = recent_job_payload("update-check", settings=settings)
    return {
        "ok": True,
        "job": job,
        "message": zh(
            r"\u66f4\u65b0\u68c0\u67e5\u5df2\u79fb\u5230\u4efb\u52a1\u4e2d\u5fc3\u3002"
            r"\u8bf7\u5728\u4efb\u52a1\u4e2d\u5fc3\u53d1\u8d77\u201c\u66f4\u65b0\u68c0\u67e5\u201d\uff1b"
            r"\u771f\u6b63\u66f4\u65b0\u4ecd\u5efa\u8bae\u5728\u670d\u52a1\u5668\u6267\u884c paopao update --yes\u3002"
        ),
    }


def create_update_check_job(*, settings: Settings | None = None) -> dict[str, Any]:
    return create_job_payload("update-check", {"source": "web-update-check"}, settings=settings)
