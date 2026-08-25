"""
信号复盘存储与回填（ReviewStore）
========================================

职责：
- 记录每次推送的信号（币种/模板/价格/消息ID）；
- 到期用币安已闭合 K 线回填后续涨幅（15分钟信号为 1h/4h，背离信号为 2h）；
- 生成复盘回复（回复原信号消息）与汇总统计。

数据文件：data/review_signals.json
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from config.settings import Settings  # noqa: E402
from shared.binance_data import BinanceDataSource  # noqa: E402
from shared.storage import JsonStore  # noqa: E402
from shared.time_windows import CST  # noqa: E402

REVIEW_FILE = "review_signals.json"
MAX_RECORDS = 5000
RETENTION_DAYS = 90

# 各模板的语义命中规则（pct 为后续窗口价格变化）
_HIT_RULES: dict[str, Any] = {
    "health_up": lambda p: p > 0,
    "false_strong": lambda p: p < 0,
    "short_covering": lambda p: p < 0,
    "health_down": lambda p: p < 0,
    "false_weak": lambda p: p > 0,
    "panic_dump": lambda p: p < 0,
    "build": lambda p: p > 0,
    "pressure": lambda p: p < 0,
    "breakout": lambda p: p > 0,
    "panic": lambda p: p < 0,
    "resonance": lambda p: p > 0,
    "extreme": lambda p: abs(p) >= 5.0,
}

TEMPLATE_LABELS: dict[str, str] = {
    "health_up": "健康上涨",
    "false_strong": "假强背离",
    "short_covering": "空头回补",
    "health_down": "健康下跌",
    "false_weak": "假弱承接",
    "panic_dump": "恐慌杀多",
    "build": "庄家建仓",
    "pressure": "回调压力",
    "breakout": "强势突破",
    "panic": "恐慌抛售",
    "resonance": "多头共振",
    "extreme": "极端背离",
}

# 每个雷达回填的窗口（秒）
WINDOWS = {"alert": (3600, 14400), "divergence": (7200,)}


def _path(settings: Settings) -> Path:
    return settings.data_dir / REVIEW_FILE


def load_records(settings: Settings) -> list[dict[str, Any]]:
    store = JsonStore(settings.data_dir)
    records = store.load(_path(settings), [])
    return records if isinstance(records, list) else []


def save_records(settings: Settings, records: list[dict[str, Any]]) -> None:
    cutoff = int(time.time()) - RETENTION_DAYS * 86400
    records = [r for r in records if int(r.get("ts") or 0) >= cutoff]
    store = JsonStore(settings.data_dir)
    store.save(_path(settings), records[-MAX_RECORDS:])


def record_signals(settings: Settings, items: Iterable[Mapping[str, Any]]) -> None:
    """把本次推送的信号追加进复盘库。items 需含 symbol/template/price/message_id/radar 等。"""
    new_records: list[dict[str, Any]] = []
    now = int(time.time())
    for item in items:
        symbol = str(item.get("symbol") or "").strip().upper()
        template = str(item.get("template") or "")
        if not symbol or not template:
            continue
        new_records.append({
            "id": f"{now}-{uuid.uuid4().hex[:8]}",
            "radar": str(item.get("radar") or "alert"),
            "template": template,
            "symbol": symbol,
            "price": float(item.get("price") or 0.0),
            "oi_pct": float(item.get("oi_pct") or 0.0),
            "price_pct": float(item.get("price_pct") or 0.0),
            "divergence": float(item.get("divergence") or 0.0),
            "ts": now,
            "message_id": int(item.get("message_id") or 0),
            "outcomes": {},
            "reply_sent": False,
        })
    if not new_records:
        return
    records = load_records(settings)
    records.extend(new_records)
    save_records(settings, records)


def template_hit(template: str, pct: float) -> bool | None:
    rule = _HIT_RULES.get(template)
    return rule(pct) if rule else None


def template_label(template: str) -> str:
    return TEMPLATE_LABELS.get(template, template)


def window_label(window: int) -> str:
    if window % 3600 == 0:
        return f"{window // 3600}h"
    if window % 60 == 0:
        return f"{window // 60}m"
    return f"{window}s"


def best_outcome(record: Mapping[str, Any]) -> dict[str, float] | None:
    """取该信号最成熟（最长）的结果窗口。"""
    outcomes = record.get("outcomes") or {}
    if not isinstance(outcomes, dict) or not outcomes:
        return None
    window = max(int(k) for k in outcomes)
    outcome = outcomes.get(str(window), outcomes.get(window))
    if not isinstance(outcome, dict):
        return None
    pct = float(outcome.get("pct") or 0.0)
    return {"window": window, "pct": pct, "price": float(outcome.get("price") or 0.0)}


def backfill_outcomes(settings: Settings, now_ts: int | None = None) -> list[dict[str, Any]]:
    """回填到期信号，返回「本次新完成回填」的记录。"""
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    records = load_records(settings)
    due: list[dict[str, Any]] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        radar = str(record.get("radar") or "alert")
        windows = WINDOWS.get(radar, (3600,))
        ts = int(record.get("ts") or 0)
        outcomes = record.get("outcomes") or {}
        if not isinstance(outcomes, dict):
            outcomes = {}
        missing = [
            window
            for window in windows
            if str(window) not in outcomes
            and window not in outcomes
            and ts + window <= now_ts
        ]
        if missing:
            record["outcomes"] = outcomes
            due.append(record)
    if not due:
        return []

    source = BinanceDataSource(settings)
    changed = False
    try:
        for record in due:
            symbol = str(record.get("symbol") or "")
            ts = int(record.get("ts") or 0)
            outcomes = record.get("outcomes") or {}
            windows = WINDOWS.get(str(record.get("radar") or "alert"), (3600,))
            for window in windows:
                if (
                    str(window) in outcomes
                    or window in outcomes
                    or ts + window > now_ts
                ):
                    continue
                target_ms = (ts + window) * 1000
                close = _close_at(source, symbol, target_ms)
                if close is None:
                    continue
                base = float(record.get("price") or 0.0)
                if base <= 0:
                    continue
                outcomes[str(window)] = {
                    "price": close,
                    "pct": (close / base - 1.0) * 100.0,
                }
                changed = True
            record["outcomes"] = outcomes
    finally:
        source.close()
    if changed:
        save_records(settings, records)
    return [r for r in due if r.get("outcomes")]


def _close_at(source: BinanceDataSource, symbol: str, target_ms: int) -> float | None:
    """取 target_ms 之前最近的闭合 5m K线收盘价（最多往前找 6 根）。"""
    for lookback in range(0, 7):
        end = target_ms - lookback * 300_000
        try:
            rows = source.klines(symbol, interval="5m", limit=1, end_time=end - 1)
        except Exception:
            rows = []
        if rows and isinstance(rows[0], (list, tuple)) and len(rows[0]) > 4:
            close = float(rows[0][4])
            if close > 0:
                return close
    return None


def _outcome_line(template: str, window: int, pct: float) -> str:
    hit = template_hit(template, pct)
    marker = "✅" if hit is True else ("❌" if hit is False else "➡️")
    return f"{window_label(window):>4} {pct:+.1f}% {marker}"


def format_review_reply(record: Mapping[str, Any]) -> str:
    symbol = str(record.get("symbol") or "")
    template = str(record.get("template") or "")
    outcomes = record.get("outcomes") or {}
    parts: list[str] = []
    any_hit: bool | None = None
    for window in sorted(int(k) for k in outcomes):
        outcome = outcomes.get(str(window), outcomes.get(window))
        pct = float(outcome.get("pct") or 0.0)
        hit = template_hit(template, pct)
        if hit is not None:
            any_hit = hit if any_hit is None else (any_hit and hit)
        parts.append(_outcome_line(template, window, pct))
    verdict = ""
    if any_hit is not None:
        verdict = "（方向命中 ✅）" if any_hit else "（方向未中 ❌）"
    lines = [
        f"🧾 信号复盘 - {symbol} {template_label(template)}",
        *parts,
        verdict,
    ]
    return "<pre>" + "\n".join(lines) + "</pre>"


def format_grouped_reply(records: list[Mapping[str, Any]]) -> str:
    lines = ["🧾 背离卡片复盘", f"信号数: {len(records)}", ""]
    for idx, record in enumerate(
        sorted(records, key=lambda r: str(r.get("symbol") or "")),
        start=1,
    ):
        symbol = str(record.get("symbol") or "")
        label = template_label(str(record.get("template") or ""))
        outcome = best_outcome(record)
        pct_text = f"{outcome['pct']:+.1f}%" if outcome else "--"
        marker = "➡️"
        if outcome:
            hit = template_hit(str(record.get("template") or ""), outcome["pct"])
            marker = "✅" if hit is True else ("❌" if hit is False else "➡️")
        lines.append(f"{idx:>2}. {symbol:<10} {label:<5} {pct_text:>8} {marker}")
    return "<pre>" + "\n".join(lines) + "</pre>"


def _review_complete(record: Mapping[str, Any]) -> bool:
    outcomes = record.get("outcomes") or {}
    if not isinstance(outcomes, dict):
        return False
    windows = WINDOWS.get(str(record.get("radar") or "alert"), (3600,))
    return all(
        str(window) in outcomes or window in outcomes
        for window in windows
    )


def send_review_replies(
    settings: Settings,
    gateway: Any,
    *,
    send: bool,
    confirm_real_send: bool,
) -> list[dict[str, Any]]:
    """给「已完成回填且未回复」的信号发送复盘回复（回复原信号消息）。

    - alert（15分钟单币卡）：每条信号一条回复；
    - divergence（多币整卡）：同一张卡合并成一条回复。
    仅在真实发送成功后才标记 reply_sent，dry-run 不会消耗回复机会。
    """
    records = load_records(settings)
    pending = [
        record
        for record in records
        if isinstance(record, dict)
        and not record.get("reply_sent")
        and _review_complete(record)
        and int(record.get("message_id") or 0) > 0
    ]
    if not pending:
        return []
    sent: list[dict[str, Any]] = []
    changed = False

    def deliver(text: str, group: list[dict[str, Any]], dedup: str) -> None:
        nonlocal changed
        message_id = int(group[0].get("message_id") or 0)
        result = gateway.send(
            text,
            "TG_LAUNCH_ALERT",
            dedup,
            send=send,
            confirm_real_send=confirm_real_send,
            cooldown_sec=0,
            daily_limit=None,
            reply_to_message_id=message_id,
            parse_mode="HTML",
        )
        if result.sent:
            for record in group:
                record["reply_status"] = result.status
                record["reply_sent"] = True
            changed = True
        elif getattr(result, "message_ids", None):
            gateway.delete_messages_detailed(
                list(result.message_ids),
                reason="pulse_review_partial_send_rollback",
            )
        sent.append({
            "message_id": message_id,
            "symbols": [str(r.get("symbol") or "") for r in group],
            "status": result.status,
            "reason": result.reason,
        })

    for record in pending:
        if str(record.get("radar") or "alert") != "divergence":
            deliver(
                format_review_reply(record),
                [record],
                f"review-reply:{record.get('id')}",
            )

    groups: dict[int, list[dict[str, Any]]] = {}
    for record in pending:
        if str(record.get("radar") or "") == "divergence":
            groups.setdefault(int(record.get("message_id") or 0), []).append(record)
    for message_id in sorted(groups):
        group = groups[message_id]
        deliver(
            format_grouped_reply(group),
            group,
            f"review-reply:divergence:{message_id}",
        )

    if changed:
        save_records(settings, records)
    return sent


def week_top_gainers(
    settings: Settings,
    now_ts: int | None = None,
    top: int = 5,
) -> list[dict[str, Any]]:
    """本周（周一 00:00 起）涨幅最好的信号币，按后续涨幅排序。"""
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    now_local = datetime.fromtimestamp(now_ts, tz=CST)
    week_start = int(
        (now_local - timedelta(days=now_local.weekday()))
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .timestamp()
    )
    by_symbol: dict[str, dict[str, Any]] = {}
    for record in load_records(settings):
        if not isinstance(record, dict):
            continue
        if int(record.get("ts") or 0) < week_start:
            continue
        outcome = best_outcome(record)
        if outcome is None:
            continue
        symbol = str(record.get("symbol") or "")
        pct = outcome["pct"]
        previous = by_symbol.get(symbol)
        if previous is None or pct > previous["pct"]:
            by_symbol[symbol] = {
                "symbol": symbol,
                "template": str(record.get("template") or ""),
                "radar": str(record.get("radar") or "alert"),
                "pct": pct,
                "window": outcome["window"],
            }
    ranked = sorted(by_symbol.values(), key=lambda row: row["pct"], reverse=True)
    return ranked[:top]


def build_review_report(
    settings: Settings,
    *,
    now_ts: int | None = None,
    days: int = 7,
    top: int = 5,
) -> dict[str, Any]:
    """Build a read-only report from outcomes that have already been backfilled."""

    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    days = min(365, max(1, int(days)))
    top = min(20, max(1, int(top)))
    start_ts = now_ts - days * 86400
    records = [
        record
        for record in load_records(settings)
        if isinstance(record, dict)
        and start_ts <= int(record.get("ts") or 0) <= now_ts
    ]
    window_stats: dict[tuple[str, int], dict[str, Any]] = {}
    template_stats: dict[tuple[str, str], dict[str, Any]] = {}
    evaluated_samples = 0
    hits = 0

    for record in records:
        radar = str(record.get("radar") or "alert")
        template = str(record.get("template") or "")
        outcomes = record.get("outcomes") or {}
        if not isinstance(outcomes, Mapping):
            continue
        for window in WINDOWS.get(radar, (3600,)):
            outcome = outcomes.get(str(window), outcomes.get(window))
            if not isinstance(outcome, Mapping):
                continue
            try:
                pct = float(outcome.get("pct"))
            except (TypeError, ValueError):
                continue
            hit = template_hit(template, pct)
            if hit is None:
                continue
            evaluated_samples += 1
            hits += int(hit)
            for stats, key in (
                (window_stats, (radar, window)),
                (template_stats, (radar, template)),
            ):
                row = stats.setdefault(
                    key,
                    {"samples": 0, "hits": 0, "pct_total": 0.0},
                )
                row["samples"] += 1
                row["hits"] += int(hit)
                row["pct_total"] += pct

    def finalized_rows(
        grouped: Mapping[tuple[str, Any], Mapping[str, Any]],
        *,
        key_name: str,
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for (radar, key), values in grouped.items():
            samples = int(values.get("samples") or 0)
            row = {
                "radar": radar,
                key_name: key,
                "samples": samples,
                "hits": int(values.get("hits") or 0),
                "hit_rate_pct": round(
                    int(values.get("hits") or 0) * 100.0 / samples,
                    1,
                ),
                "avg_pct": round(float(values.get("pct_total") or 0.0) / samples, 2),
            }
            if key_name == "template":
                row["label"] = template_label(str(key))
            rows.append(row)
        if key_name == "window":
            return sorted(
                rows,
                key=lambda row: (str(row["radar"]), int(row["window"])),
            )
        return sorted(
            rows,
            key=lambda row: (str(row["radar"]), str(row["template"])),
        )

    return {
        "status": "ok",
        "generated_at_ts": now_ts,
        "period_days": days,
        "period_start_ts": start_ts,
        "period_end_ts": now_ts,
        "records": len(records),
        "completed_records": sum(1 for record in records if _review_complete(record)),
        "pending_records": sum(1 for record in records if not _review_complete(record)),
        "evaluated_samples": evaluated_samples,
        "hits": hits,
        "hit_rate_pct": (
            round(hits * 100.0 / evaluated_samples, 1)
            if evaluated_samples
            else None
        ),
        "sample_status": "sufficient" if evaluated_samples >= 20 else "accumulating",
        "top_limit": top,
        "by_window": finalized_rows(window_stats, key_name="window"),
        "by_template": finalized_rows(template_stats, key_name="template"),
        "week_top_gainers": week_top_gainers(settings, now_ts=now_ts, top=top),
    }


def format_review_report(report: Mapping[str, Any]) -> str:
    """Format a manual terminal report without sending Telegram messages."""

    def timestamp_text(value: Any) -> str:
        return datetime.fromtimestamp(int(value or 0), tz=CST).strftime(
            "%Y-%m-%d %H:%M:%S"
        )

    samples = int(report.get("evaluated_samples") or 0)
    rate = report.get("hit_rate_pct")
    rate_text = f"{float(rate):.1f}%" if rate is not None else "暂无可判定样本"
    sample_note = "样本已达到基础展示门槛" if samples >= 20 else "样本积累中，暂不代表稳定胜率"
    lines = [
        "脉冲雷达复盘报告",
        (
            f"统计区间（北京时间）: {timestamp_text(report.get('period_start_ts'))}"
            f" 至 {timestamp_text(report.get('period_end_ts'))}"
        ),
        (
            f"信号记录: {int(report.get('records') or 0)}；"
            f"已完成: {int(report.get('completed_records') or 0)}；"
            f"待回填: {int(report.get('pending_records') or 0)}"
        ),
        (
            f"可判定样本: {samples}；命中: {int(report.get('hits') or 0)}；"
            f"方向命中率: {rate_text}（{sample_note}）"
        ),
        "说明: 样本按“信号 × 已成熟窗口”统计，未到期或缺失窗口不计入命中率。",
        "",
        "按复盘窗口:",
    ]
    radar_labels = {"alert": "15分钟异动", "divergence": "2小时背离"}
    window_rows = report.get("by_window") or []
    if not window_rows:
        lines.append("- 暂无已成熟样本")
    for row in window_rows if isinstance(window_rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {radar_labels.get(str(row.get('radar') or ''), str(row.get('radar') or ''))} "
            f"{window_label(int(row.get('window') or 0))}: "
            f"{int(row.get('hits') or 0)}/{int(row.get('samples') or 0)}，"
            f"命中率 {float(row.get('hit_rate_pct') or 0.0):.1f}%"
        )

    lines.extend(["", "按信号分类:"])
    template_rows = report.get("by_template") or []
    if not template_rows:
        lines.append("- 暂无已成熟样本")
    for row in template_rows if isinstance(template_rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"- {row.get('label') or row.get('template')}: "
            f"{int(row.get('hits') or 0)}/{int(row.get('samples') or 0)}，"
            f"命中率 {float(row.get('hit_rate_pct') or 0.0):.1f}%，"
            f"平均后续涨跌 {float(row.get('avg_pct') or 0.0):+.2f}%"
        )

    lines.extend([
        "",
        (
            f"本周后续涨幅 TOP {int(report.get('top_limit') or 5)}"
            "（北京时间周一 00:00 起）:"
        ),
    ])
    top_rows = report.get("week_top_gainers") or []
    if not top_rows:
        lines.append("- 暂无已成熟样本")
    for index, row in enumerate(top_rows if isinstance(top_rows, list) else [], start=1):
        if not isinstance(row, Mapping):
            continue
        lines.append(
            f"{index}. {row.get('symbol')} "
            f"{float(row.get('pct') or 0.0):+.2f}% "
            f"({window_label(int(row.get('window') or 0))})"
        )
    return "\n".join(lines)


__all__ = [
    "backfill_outcomes",
    "best_outcome",
    "build_review_report",
    "format_grouped_reply",
    "format_review_report",
    "format_review_reply",
    "load_records",
    "record_signals",
    "save_records",
    "send_review_replies",
    "template_hit",
    "template_label",
    "week_top_gainers",
    "window_label",
]
