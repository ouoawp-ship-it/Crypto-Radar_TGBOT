from __future__ import annotations

import heapq
import json
import math
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote


MAX_VIEW_ITEMS = 8
_MAX_JSON_BYTES = 4 * 1024 * 1024
_CST = timezone(timedelta(hours=8))

_RADAR_LABELS = {
    "TG_LAUNCH_ALERT": "启动预警",
    "TG_RADAR_SUMMARY": "资金摘要",
    "TG_FUNDING_ALERT": "资金费率警报",
    "TG_FLOW_RADAR": "五因子资金流",
    "TG_ANNOUNCEMENT_ALERT": "公告风险",
    "launch": "启动预警",
    "summary": "资金摘要",
    "funding": "资金费率警报",
    "flow": "五因子资金流",
    "announcement": "公告风险",
}
_TEMPLATE_IDS = tuple(key for key in _RADAR_LABELS if key.startswith("TG_"))
_STATUS_LABELS = {
    "sent": "发送成功",
    "dry_run": "安全演练",
    "skipped": "已跳过",
    "blocked": "已阻止",
    "failed": "发送失败",
    "partial": "部分成功",
    "no_candidates": "本轮无候选",
    "no_new_alerts": "本轮无新提醒",
    "not_requested": "本轮未请求推送",
}
_UNPUBLISHED_STATUSES = frozenset(
    {"dry_run", "skipped", "blocked", "failed", "partial"}
)
_REASON_LABELS = {
    "telegram_api": "Telegram 已接收消息",
    "telegram_photo_api": "Telegram 已接收图片消息",
    "telegram_api_failed": "Telegram 接口调用失败",
    "telegram_photo_api_failed": "Telegram 图片接口调用失败",
    "dedup_cooldown": "同类内容仍在防重复冷却期内",
    "template_daily_limit": "该类消息今天的发送额度已用完",
    "global_hourly_limit": "本小时发送额度已用完",
    "send_flag_not_set": "当前未启用真实发送",
    "missing_confirm_real_send": "未完成真实发送的第二重确认",
    "telegram_not_configured": "Telegram 机器人或目标群配置不完整",
    "telegram_topic_not_configured": "对应的 Telegram 话题尚未配置",
    "delivery_quarantine": "上一轮投递尚未安全收口",
    "invalid_telegram_config": "Telegram 配置无效或不完整",
    "invalid_png": "图片格式无效",
    "photo_too_large": "图片大小超过 Telegram 限制",
    "caption_too_long": "图片说明超过 Telegram 限制",
    "chart_unavailable": "图表暂时无法生成",
    "chart_generation_failed": "图表生成失败",
}
_STAGE_LABELS = {
    "idle": "等待观察",
    "watching": "观察中",
    "primed": "启动准备",
    "breakout": "突破确认",
    "launched": "启动加速",
    "risk": "风险阶段",
    "cooling": "降温观察",
    "failed": "本轮结束",
    "first_seen": "首次异动",
    "active": "持续活跃",
    "crowding_intensifying": "拥挤加剧",
    "high_risk_active": "高危活跃",
    "risk_release": "风险释放",
    "heat_decay": "热度衰减",
    "observation_ended": "观察结束",
    "opportunity": "机会公告",
    "数据不足": "数据不足",
    "观察": "观察",
    "真启动候选": "真启动候选",
    "吸筹观察": "吸筹观察",
    "空头燃料": "空头燃料",
    "合约拉盘": "合约拉盘",
    "挤空/止损": "挤空/止损",
    "诱多/派发": "诱多/派发",
    "恐慌下跌": "恐慌下跌",
}
_SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{2,24}USDT")

_HEALTH_EXPLANATIONS = {
    "runtime_status": "主循环没有正常更新，或最近一轮运行失败。",
    "signal_store_integrity": "信号记录尚未就绪或完整性异常。",
    "market_snapshots_integrity": "市场快照尚未就绪或完整性异常。",
    "realtime_features_integrity": "实时行情记录尚未就绪或完整性异常。",
    "market_snapshots_freshness": "市场快照超过新鲜度要求。",
    "realtime_features_freshness": "实时行情数据缺失或已经过期。",
    "signal_effectiveness": "信号结果追踪尚未就绪或存在积压。",
    "launch_outcomes": "启动周期结果评估尚未就绪或存在积压。",
    "database_backup": "数据库备份尚未就绪或未通过恢复校验。",
    "disk_space": "数据盘可用空间低于安全阈值。",
    "upstream_sources": "上游数据源最近一次观测处于降级状态。",
}
_HEALTH_WARNING = frozenset({"warn", "warning", "degraded", "stale"})
_HEALTH_FAILURE = frozenset({"fail", "failed", "error", "critical"})
_RADAR_STATE_EXPLANATIONS = {
    "degraded": "最近一轮未正常完成，当前处于降级状态。",
    "stale": "已经超过计划运行时间。",
    "not_running": "主 BOT 当前未处于活动循环。",
    "not_initialized": "运行状态尚未初始化。",
    "waiting_first_cycle": "尚未完成首次运行。",
    "unknown": "当前状态暂时无法确认。",
}


def _limit(value: Any) -> int:
    if isinstance(value, bool):
        return MAX_VIEW_ITEMS
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return MAX_VIEW_ITEMS
    return max(1, min(MAX_VIEW_ITEMS, parsed))


def _timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if not math.isfinite(float(value)):
        return 0
    parsed = int(value)
    return parsed if 0 < parsed <= 4_102_444_800 else 0


def _time_text(value: Any) -> str:
    timestamp = _timestamp(value)
    if not timestamp:
        return "时间未知"
    try:
        return datetime.fromtimestamp(timestamp, _CST).strftime("%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "时间未知"


def _radar_label(item: Mapping[str, Any]) -> str:
    template = item.get("template_id")
    module = item.get("module")
    if isinstance(template, str) and template in _RADAR_LABELS:
        return _RADAR_LABELS[template]
    if isinstance(module, str) and module in _RADAR_LABELS:
        return _RADAR_LABELS[module]
    return ""


def _records(value: Any) -> Sequence[Any] | None:
    if isinstance(value, Mapping):
        for key in ("items", "records", "history"):
            items = value.get(key)
            if isinstance(items, Sequence) and not isinstance(
                items, (str, bytes, bytearray)
            ):
                return items
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return None


def _latest(
    records: Sequence[Any],
    *,
    limit: int,
    accepted: Any,
) -> list[Mapping[str, Any]]:
    candidates = (
        (_timestamp(item.get("ts")), index, item)
        for index, item in enumerate(records)
        if isinstance(item, Mapping) and accepted(item)
    )
    return [
        item
        for _timestamp_value, _index, item in heapq.nlargest(
            limit,
            candidates,
            key=lambda candidate: (candidate[0], candidate[1]),
        )
    ]


def _json_source(source: Any) -> tuple[Any, str]:
    if isinstance(source, (str, os.PathLike)):
        try:
            path = Path(source)
            if not path.is_file():
                return None, "missing"
            if path.stat().st_size > _MAX_JSON_BYTES:
                return None, "invalid"
            return json.loads(path.read_text(encoding="utf-8")), "ok"
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None, "invalid"
    if isinstance(source, Mapping) or (
        isinstance(source, Sequence)
        and not isinstance(source, (str, bytes, bytearray))
    ):
        return source, "ok"
    return None, "invalid"


def _signal_source(source: Any, limit: int) -> tuple[list[Mapping[str, Any]], str]:
    if not isinstance(source, (str, os.PathLike)):
        records = _records(source)
        if records is None:
            return [], "invalid"
        return _latest(
            records,
            limit=limit,
            accepted=lambda item: bool(_radar_label(item)),
        ), "ok"

    try:
        path = Path(source)
        if not path.is_file():
            return [], "missing"
        encoded_path = quote(path.resolve().as_posix(), safe="/:")
        uri = f"file:{encoded_path}?mode=ro"
        placeholders = ",".join("?" for _ in _TEMPLATE_IDS)
        with closing(sqlite3.connect(uri, uri=True, timeout=5.0)) as connection:
            connection.row_factory = sqlite3.Row
            with closing(connection.execute("PRAGMA query_only=ON")) as pragma:
                pragma.fetchone()
            with closing(
                connection.execute(
                    f"""
                    SELECT ts, module, template_id, symbol, stage, score, status
                    FROM signals
                    WHERE template_id IN ({placeholders})
                    ORDER BY ts DESC, id DESC
                    LIMIT ?
                    """,
                    (*_TEMPLATE_IDS, limit),
                )
            ) as cursor:
                items = [dict(row) for row in cursor.fetchall()]
        return items, "ok"
    except (OSError, TypeError, ValueError, sqlite3.Error):
        return [], "invalid"


def _symbol_text(value: Any) -> str:
    if not isinstance(value, str):
        return "综合市场"
    normalized = value.strip().upper()
    return normalized if _SYMBOL_PATTERN.fullmatch(normalized) else "综合市场"


def _score_text(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 100:
        return ""
    return f"{int(score)}分" if score.is_integer() else f"{score:.1f}分"


def _status_text(value: Any) -> str:
    return _STATUS_LABELS.get(value, "状态未知") if isinstance(value, str) else "状态未知"


def _reason_text(value: Any) -> str:
    if not isinstance(value, str) or not value:
        return "原因未记录"
    return _REASON_LABELS.get(value, "详细原因已保留在内部运行记录中")


def render_recent_signals(source: Any, *, limit: int = MAX_VIEW_ITEMS) -> str:
    """Render recent logical signals from a read-only SQLite path or reader data."""

    safe_limit = _limit(limit)
    items, state = _signal_source(source, safe_limit)
    lines = ["📡 最近信号"]
    if state == "missing":
        return "\n".join((*lines, "信号记录尚未生成。"))
    if state != "ok":
        return "\n".join((*lines, "信号记录暂时无法读取，请稍后再试。"))
    if not items:
        return "\n".join((*lines, "暂时没有可显示的信号。"))

    for item in items[:safe_limit]:
        parts = [
            _time_text(item.get("ts")),
            _radar_label(item),
            _symbol_text(item.get("symbol")),
        ]
        stage_value = item.get("stage")
        stage = (
            _STAGE_LABELS.get(stage_value, "")
            if isinstance(stage_value, str)
            else ""
        )
        score = _score_text(item.get("score"))
        parts.extend(value for value in (stage, score) if value)
        parts.append(_status_text(item.get("status")))
        lines.append("• " + "｜".join(parts))
    return "\n".join(lines)


def _push_items(source: Any, limit: int, *, unpublished_only: bool) -> tuple[list[Mapping[str, Any]], str]:
    value, state = _json_source(source)
    if state != "ok":
        return [], state
    records = _records(value)
    if records is None:
        return [], "invalid"

    def accepted(item: Mapping[str, Any]) -> bool:
        if not _radar_label(item):
            return False
        status = item.get("status")
        return not unpublished_only or (
            isinstance(status, str) and status in _UNPUBLISHED_STATUSES
        )

    return _latest(records, limit=limit, accepted=accepted), "ok"


def _render_push_page(source: Any, *, limit: int, unpublished_only: bool) -> str:
    safe_limit = _limit(limit)
    items, state = _push_items(source, safe_limit, unpublished_only=unpublished_only)
    title = "⏭ 最近未推送原因" if unpublished_only else "📨 最近推送记录"
    if state == "missing":
        return f"{title}\n推送记录尚未生成。"
    if state != "ok":
        return f"{title}\n推送记录暂时无法读取，请稍后再试。"
    if not items:
        empty = "最近没有未推送记录。" if unpublished_only else "暂时没有推送记录。"
        return f"{title}\n{empty}"

    lines = [title]
    for item in items[:safe_limit]:
        lines.append(
            "• "
            + "｜".join(
                (
                    _time_text(item.get("ts")),
                    _radar_label(item),
                    _status_text(item.get("status")),
                    _reason_text(item.get("reason")),
                )
            )
        )
    return "\n".join(lines)


def render_push_records(source: Any, *, limit: int = MAX_VIEW_ITEMS) -> str:
    """Render bounded push decisions from a JSON path or reader data."""

    return _render_push_page(source, limit=limit, unpublished_only=False)


def render_unpublished_reasons(source: Any, *, limit: int = MAX_VIEW_ITEMS) -> str:
    """Render bounded dry-run, skipped, blocked, failed, and partial decisions."""

    return _render_push_page(source, limit=limit, unpublished_only=True)


def _health_checks(value: Any) -> Sequence[Any] | None:
    if isinstance(value, Mapping):
        checks = value.get("checks")
        if isinstance(checks, Sequence) and not isinstance(
            checks, (str, bytes, bytearray)
        ):
            return checks
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return value
    return None


def _radar_items(value: Any) -> Mapping[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    radars = value.get("radars")
    return radars if isinstance(radars, Mapping) else None


def render_fault_explanations(
    health_source: Any,
    radar_source: Any | None = None,
    *,
    limit: int = MAX_VIEW_ITEMS,
) -> str:
    """Render allowlisted Chinese faults without forwarding raw diagnostic fields."""

    safe_limit = _limit(limit)
    health_value, health_state = _json_source(health_source)
    checks = _health_checks(health_value) if health_state == "ok" else None
    health_valid = checks is not None

    radar_requested = radar_source is not None
    radar_value: Any = None
    radar_state = "ok"
    if radar_requested:
        radar_value, radar_state = _json_source(radar_source)
    elif isinstance(health_value, Mapping) and isinstance(
        health_value.get("radars"), Mapping
    ):
        radar_value = health_value
        radar_requested = True
    radars = _radar_items(radar_value) if radar_state == "ok" else None
    radar_valid = not radar_requested or radars is not None

    lines = ["🩺 中文故障说明"]
    if not health_valid and not (radar_requested and radars is not None):
        return "\n".join((*lines, "故障信息暂时无法读取，请稍后再试。"))

    issues: list[str] = []
    if radars is not None:
        for key in (
            "launch_alert",
            "radar_summary",
            "funding_alert",
            "flow_radar",
            "announcement_risk",
        ):
            item = radars.get(key)
            if not isinstance(item, Mapping):
                continue
            state = item.get("state")
            explanation = (
                _RADAR_STATE_EXPLANATIONS.get(state, "")
                if isinstance(state, str)
                else ""
            )
            label = {
                "launch_alert": "启动预警",
                "radar_summary": "资金摘要",
                "funding_alert": "资金费率警报",
                "flow_radar": "五因子资金流",
                "announcement_risk": "公告风险",
            }[key]
            if explanation:
                issues.append(f"{label}：{explanation}")

    if checks is not None:
        for item in checks:
            if not isinstance(item, Mapping):
                continue
            status = item.get("status")
            if not isinstance(status, str):
                continue
            if status in _HEALTH_FAILURE:
                level = "异常"
            elif status in _HEALTH_WARNING:
                level = "提醒"
            else:
                continue
            name = item.get("name")
            explanation = _HEALTH_EXPLANATIONS.get(
                name if isinstance(name, str) else "",
                "其他本地检查发现异常。",
            )
            issues.append(f"{level}：{explanation}")
            if len(issues) >= safe_limit:
                break

    partial = not health_valid or not radar_valid
    if partial and len(issues) < safe_limit:
        issues.append("部分本地状态暂时无法读取。")
    if not issues:
        return "\n".join((*lines, "当前没有需要处理的本地故障。"))
    lines.extend(f"• {issue}" for issue in issues[:safe_limit])
    return "\n".join(lines)


__all__ = [
    "MAX_VIEW_ITEMS",
    "render_fault_explanations",
    "render_push_records",
    "render_recent_signals",
    "render_unpublished_reasons",
]
