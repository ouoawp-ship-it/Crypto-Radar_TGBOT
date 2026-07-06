from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .api_core import redact_api_payload


MODULE_DISPLAY: dict[str, tuple[str, str]] = {
    "launch": ("启动雷达", "info"),
    "funding": ("资金费率", "warn"),
    "flow": ("资金流", "info"),
    "structure": ("结构雷达", "good"),
    "structure_review": ("结构复盘", "neutral"),
    "announcement": ("公告", "neutral"),
    "summary": ("资金摘要", "neutral"),
    "test": ("测试", "info"),
    "telegram": ("其他", "neutral"),
    "unknown": ("其他", "neutral"),
}
STATUS_DISPLAY: dict[str, tuple[str, str]] = {
    "sent": ("已发送", "good"),
    "dry_run": ("Dry-run", "info"),
    "skipped": ("已跳过", "neutral"),
    "blocked": ("已阻止", "warn"),
    "failed": ("失败", "bad"),
}
SEVERITY_DISPLAY: dict[str, tuple[str, str]] = {
    "critical": ("严重", "bad"),
    "error": ("错误", "bad"),
    "warning": ("警告", "warn"),
    "warn": ("警告", "warn"),
    "info": ("普通", "neutral"),
}


def _text(value: Any, limit: int = 600) -> str:
    return str(redact_api_payload(value if value is not None else ""))[:limit]


def _tone_for_status(status: str, severity: str = "") -> str:
    status_key = str(status or "").lower()
    if status_key in STATUS_DISPLAY:
        return STATUS_DISPLAY[status_key][1]
    severity_key = str(severity or "").lower()
    return SEVERITY_DISPLAY.get(severity_key, ("普通", "neutral"))[1]


def _time_label(value: Any) -> str:
    text = str(value or "")
    if not text:
        return "-"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone().strftime("%Y-%m-%d %H:%M")
    except Exception:
        return text[:19]


def _score_label(value: Any) -> str:
    if value is None or value == "":
        return "-"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return _text(value, 40) or "-"
    return str(int(number)) if number.is_integer() else f"{number:.2f}".rstrip("0").rstrip(".")


def _module_label(module: str) -> tuple[str, str]:
    key = str(module or "unknown").lower()
    return MODULE_DISPLAY.get(key, (key or "其他", "neutral"))


def _status_label(status: str) -> tuple[str, str]:
    key = str(status or "").lower()
    return STATUS_DISPLAY.get(key, (status or "未知", "neutral"))


def _severity_label(severity: str) -> tuple[str, str]:
    key = str(severity or "info").lower()
    return SEVERITY_DISPLAY.get(key, (severity or "普通", "neutral"))


def signal_display(item: dict[str, Any]) -> dict[str, Any]:
    module_label, module_tone = _module_label(str(item.get("module") or ""))
    status_label, status_tone = _status_label(str(item.get("status") or ""))
    severity_label, severity_tone = _severity_label(str(item.get("severity") or "info"))
    symbol = str(item.get("symbol") or "").upper()
    symbol_label = symbol or "全局/无币种"
    score_label = _score_label(item.get("score"))
    stage_label = _text(item.get("stage"), 80) or "-"
    title = _text(item.get("title") or item.get("signal_type") or item.get("template_id") or module_label, 120)
    summary = _text(item.get("excerpt") or item.get("text_html") or title, 260)
    card_tone = _tone_for_status(str(item.get("status") or ""), str(item.get("severity") or ""))
    badges = [
        {"label": module_label, "tone": module_tone, "kind": "module", "value": str(item.get("module") or "")},
        {"label": status_label, "tone": status_tone, "kind": "status", "value": str(item.get("status") or "")},
        {"label": symbol_label, "tone": "info" if symbol else "neutral", "kind": "symbol", "value": symbol},
    ]
    if score_label != "-":
        badges.append({"label": f"分数 {score_label}", "tone": severity_tone, "kind": "score", "value": score_label})
    if stage_label != "-":
        badges.append({"label": stage_label, "tone": "neutral", "kind": "stage", "value": stage_label})
    return {
        "title": title or f"{module_label} #{item.get('id', '')}",
        "subtitle": f"{module_label} · {symbol_label}",
        "module_label": module_label,
        "status_label": status_label,
        "severity_label": severity_label,
        "symbol_label": symbol_label,
        "time_label": _time_label(item.get("time")),
        "score_label": score_label,
        "stage_label": stage_label,
        "summary": summary,
        "badges": badges,
        "card_tone": card_tone,
        "primary_action": "查看详情",
    }


def enhance_signal_item(item: dict[str, Any]) -> dict[str, Any]:
    enhanced = dict(redact_api_payload(item))
    enhanced["display"] = signal_display(enhanced)
    return enhanced


def enhance_signal_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [enhance_signal_item(item) for item in items]


def safe_json_summary(value: Any, *, default: Any) -> dict[str, Any]:
    if isinstance(value, (dict, list)):
        return {"ok": True, "value": redact_api_payload(value), "text": json.dumps(redact_api_payload(value), ensure_ascii=False, indent=2)}
    text = str(value or "").strip()
    if not text:
        return {"ok": True, "value": default, "text": json.dumps(default, ensure_ascii=False, indent=2)}
    try:
        parsed = json.loads(text)
        return {"ok": True, "value": redact_api_payload(parsed), "text": json.dumps(redact_api_payload(parsed), ensure_ascii=False, indent=2)}
    except Exception:
        return {"ok": False, "value": default, "text": _text(text, 1200)}


def signal_detail_view(item: dict[str, Any], same_symbol: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    enhanced = enhance_signal_item(item)
    display = enhanced["display"]
    payload_raw = item.get("payload", item.get("payload_json", {}))
    message_ids_raw = item.get("message_ids", item.get("message_ids_json", []))
    payload = safe_json_summary(payload_raw, default={})
    message_ids = safe_json_summary(message_ids_raw, default=[])

    def row(label: str, value: Any, code: bool = False) -> dict[str, Any]:
        return {"label": label, "value": _text(value, 2000), "code": bool(code)}

    sections = [
        {
            "title": "推送内容",
            "rows": [
                row("标题", display["title"]),
                row("摘要", display["summary"]),
                row("正文", enhanced.get("text_html") or enhanced.get("excerpt") or "", True),
            ],
        },
        {
            "title": "Telegram",
            "rows": [
                row("topic_id", enhanced.get("topic_id") or "-"),
                row("message_ids", ", ".join(str(item) for item in enhanced.get("message_ids", []) or []) or "-"),
                row("reply_to_message_id", enhanced.get("reply_to_message_id") or "-"),
                row("发送状态", display["status_label"]),
            ],
        },
        {
            "title": "信号元数据",
            "rows": [
                row("id", enhanced.get("id")),
                row("module", enhanced.get("module")),
                row("template_id", enhanced.get("template_id"), True),
                row("signal_type", enhanced.get("signal_type")),
                row("dedup_key", enhanced.get("dedup_key"), True),
                row("severity", enhanced.get("severity")),
                row("score", display["score_label"]),
                row("stage", display["stage_label"]),
            ],
        },
    ]
    return {
        "header": {
            "id": enhanced.get("id"),
            "title": display["title"],
            "subtitle": display["subtitle"],
            "badges": display["badges"],
            "card_tone": display["card_tone"],
            "time_label": display["time_label"],
        },
        "sections": sections,
        "raw": {
            "payload_json": payload["text"],
            "payload_parse_ok": payload["ok"],
            "message_ids_json": message_ids["text"],
            "message_ids_parse_ok": message_ids["ok"],
        },
        "related": {
            "same_symbol": enhance_signal_items((same_symbol or [])[:10]),
        },
    }


def signal_stats_display(stats: dict[str, Any]) -> dict[str, Any]:
    by_module = stats.get("by_module", {}) if isinstance(stats.get("by_module"), dict) else {}
    by_status = stats.get("by_status", {}) if isinstance(stats.get("by_status"), dict) else {}
    top_symbols = stats.get("top_symbols", []) if isinstance(stats.get("top_symbols"), list) else []
    return {
        "by_module_display": [
            {"module": key, "label": _module_label(key)[0], "tone": _module_label(key)[1], "count": int(value or 0)}
            for key, value in by_module.items()
        ],
        "by_status_display": [
            {"status": key, "label": _status_label(key)[0], "tone": _status_label(key)[1], "count": int(value or 0)}
            for key, value in by_status.items()
        ],
        "top_symbols_display": [
            {"symbol": str(item.get("symbol") or ""), "label": str(item.get("symbol") or "全局/无币种"), "count": int(item.get("count") or 0), "tone": "info"}
            for item in top_symbols[:12]
            if isinstance(item, dict)
        ],
    }
