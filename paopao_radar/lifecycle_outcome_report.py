from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from .atomic_json import atomic_write_text
from .config import BASE_DIR, Settings
from .lifecycle_outcomes import HORIZONS, lifecycle_outcome_status


DEFAULT_JSON_PATH = BASE_DIR / "docs" / "generated" / "lifecycle_outcome_coverage_latest.json"
DEFAULT_MARKDOWN_PATH = BASE_DIR / "docs" / "generated" / "lifecycle_outcome_coverage_latest.md"


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _group(rows: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get(key) or "unknown")].append(row)
    output: list[dict[str, Any]] = []
    for label, items in grouped.items():
        candidates = sum(int(item.get("candidate_signal_count") or 0) for item in items)
        linked = sum(int(item.get("linked_signal_count") or 0) for item in items)
        mature = sum(int(item.get("mature_horizon_count") or 0) for item in items)
        due = sum(
            1
            for item in items
            for horizon in HORIZONS
            if str(item.get(f"horizon_{horizon}_status") or "missing") != "not_due"
        )
        output.append({
            key: label,
            "lifecycle_count": len(items),
            "candidate_signal_count": candidates,
            "linked_signal_count": linked,
            "link_coverage_ratio": _ratio(linked, candidates),
            "mature_horizon_count": mature,
            "maturity_ratio": _ratio(mature, due),
        })
    return sorted(output, key=lambda item: (-int(item["lifecycle_count"]), str(item[key])))


def build_lifecycle_outcome_coverage_report(settings: Settings | None = None) -> dict[str, Any]:
    loaded = settings or Settings.load()
    summary_result = lifecycle_outcome_status(loaded)
    summary = dict(summary_result.get("data") or {})
    rows: list[dict[str, Any]] = []
    db_path = Path(loaded.lifecycle_db_path)
    if db_path.exists():
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='lifecycle_outcome_coverage'"
            ).fetchone()
            if exists:
                rows = [
                    dict(row)
                    for row in conn.execute(
                        """
                        SELECT c.lifecycle_id, c.symbol, c.candidate_signal_count,
                               c.linked_signal_count, c.linked_outcome_count,
                               c.horizon_1h_status, c.horizon_4h_status,
                               c.horizon_24h_status, c.horizon_72h_status,
                               c.mature_horizon_count, c.unlinked_reason,
                               l.first_signal_module, l.first_signal_level
                        FROM lifecycle_outcome_coverage AS c
                        JOIN signal_lifecycles AS l ON l.id = c.lifecycle_id
                        ORDER BY c.lifecycle_id
                        """
                    ).fetchall()
                ]
        finally:
            conn.close()
    unavailable_symbols = sorted({
        str(row.get("symbol") or "")
        for row in rows
        if any(str(row.get(f"horizon_{horizon}_status") or "") == "unavailable" for horizon in HORIZONS)
    })
    error_symbols = sorted({
        str(row.get("symbol") or "")
        for row in rows
        if any(str(row.get(f"horizon_{horizon}_status") or "") == "error" for horizon in HORIZONS)
    })
    return {
        "version": "v1.78.1",
        "summary": summary,
        "horizons": summary.get("horizons", {}),
        "unlinked_reasons": summary.get("unlinked_reasons", {}),
        "by_module": _group(rows, "first_signal_module"),
        "by_first_signal_level": _group(rows, "first_signal_level"),
        "unavailable_symbols": unavailable_symbols,
        "real_error_summary": {"count": len(error_symbols), "symbols": error_symbols},
        "data_rules": {
            "coverage_is_not_maturity": True,
            "not_due_is_failure": False,
            "pending_is_failure": False,
            "unavailable_is_loss": False,
            "returns_include_statuses": ["success"],
        },
    }


def _markdown(data: dict[str, Any]) -> str:
    summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
    return "\n".join([
        "# Lifecycle Outcome Coverage Latest",
        "",
        f"- 生命周期总数：{summary.get('lifecycle_count', 0)}",
        f"- 已关联生命周期：{summary.get('linked_lifecycle_count', 0)}",
        f"- 已关联 Outcome：{summary.get('linked_outcome_count', 0)}",
        f"- 关联覆盖率：{summary.get('link_coverage_ratio', 0)}",
        f"- 已成熟生命周期：{summary.get('mature_lifecycle_count', 0)}",
        f"- 数据成熟度：{summary.get('maturity_ratio', 0)}",
        "",
        "关联覆盖率与数据成熟度是两个不同指标。尚未到期和 pending 不是失败，unavailable 不等于亏损；只有 success Outcome 才参与成熟收益统计。",
        "",
        "仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。",
        "",
    ])


def write_lifecycle_outcome_coverage_report(
    settings: Settings | None = None,
    *,
    json_path: str | Path | None = None,
    markdown_path: str | Path | None = None,
) -> dict[str, Any]:
    data = build_lifecycle_outcome_coverage_report(settings)
    target_json = Path(json_path) if json_path is not None else DEFAULT_JSON_PATH
    target_markdown = Path(markdown_path) if markdown_path is not None else DEFAULT_MARKDOWN_PATH
    atomic_write_text(target_json, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    atomic_write_text(target_markdown, _markdown(data))
    return data
