from __future__ import annotations

"""
泡泡抓币：精简版加密监控工具。

核心功能：
- 公告风险：低频解析 Binance 官方上新/下架等事件，独立推送并供启动预警引用。
- 费率/OI 异动扫描：负费率、资金费率趋势、持仓变化、价格变化、成交量变化。
- 热度做多雷达：按涨幅、成交量、OI、资金费率筛选短线动量。
- 庄家收筹/埋伏池：低市值、横盘、OI 暗流、负费率燃料的综合评分。
- BN 行情启动预警：15m/1h 价格、OI、成交量、短周期突破分层提醒。
- OI/价格背离扫描：识别建仓背离、多头共振、极端背离等状态。

默认推送周期：
- 资金雷达汇总：6 小时一次，每天最多 4 次；收线后延迟抓上一完整窗口。
- 启动雷达提醒：3 分钟检查一次，按最近完整 15m 收线窗口判断。
- 公告风险：独立低频运行；作为启动辅助证据时不参与启动分数。
- 同币同阶段启动提醒：默认 6 小时冷却。
"""

import argparse
import json
import math
import re
import sqlite3
import sys
import time
from pathlib import Path
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from config import Settings
from .database_backup import backup_databases
from shared.binance_data import BinanceDataSource, UPSTREAM_SOURCE_METRICS
from radars.capital_flow.radar import FlowRadarEngine
from radars.announcement_risk.radar import AnnouncementRiskRadar
from .health import lightweight_freshness_checks, runtime_health_checks
from shared.market_cockpit import persist_flow_market_rows, persist_market_batch
from radars.funding_alert.radar import FundingAlertEngine
from .maintenance import cleanup_runtime_artifacts
from .cli_text import check_name_text, format_push_result_cn
from radars.market_summary.radar import MarketSummaryRadar
from radars.launch_warning.ai_interpreter import build_launch_ai_context
from radars.launch_warning.ai_on_demand import (
    LaunchAiOnDemandService,
    build_launch_ai_deep_link,
    positive_telegram_user_id,
)
from .radar_engine import RadarEngine
from .diagnostics import build_market_radar_runtime_status
from .signal_effectiveness import SignalOutcomeTracker
from shared.signal_store import SignalEventStore, signal_public_ref
from shared.storage import JsonStore
from shared.telegram import (
    AI_INTERPRET_URL_BUTTON_TEXT,
    PRODUCTION_TOPIC_TEMPLATE_IDS,
    TOPIC_TEMPLATE_NAMES,
    TelegramGateway,
    TelegramUrlButton,
)
from shared.time_windows import next_closed_window_epoch


PROJECT_ABOUT = """泡泡抓币：精简版加密监控工具

保留功能：
- 公告风险：独立提醒 Binance 官方上新/下架事件，同时只作启动预警辅助证据、不参与打分。
- 费率/OI 异动扫描：资金费率、持仓、价格、成交量、数据质量。
- 热度做多雷达：涨幅、成交量、OI、资金费率综合筛选短线动量。
- 庄家收筹/埋伏池：低市值、横盘、OI 暗流、负费率燃料综合评分。
- BN 行情启动预警：15m/1h 价格、OI、成交量、短周期突破分层提醒。
- OI/价格背离扫描：建仓背离、多头共振、极端背离、信号持续/增强/消失。

推送内容：
- 资金雷达汇总：负费率榜、综合榜、埋伏榜、动量池、新币池、值得关注、图例、数据质量。
- 启动雷达提醒：币种、发现分、方向证据分、行情阶段、执行状态和已收盘数据。
- 启动预警辅助证据：最近的 Binance 官方公告与吸筹质量，不改变发现分或方向证据分。
- Telegram 测试消息：只在手动执行 telegram-test --send --confirm-real-send 时发送。

默认周期：
- 资金雷达汇总：6 小时一次，每天最多 4 次；可用 --interval 或 RADAR_SUMMARY_MIN_INTERVAL_SEC 调整。
- 启动雷达扫描：3 分钟检查一次，按最近完整 15m 收线窗口判断；可用 --launch-interval 调整。
- 启动同币同阶段冷却：6 小时，可用 LAUNCH_STAGE_COOLDOWN_SEC 调整。
- 自动清理：1 小时检查一次，可用 CLEANUP_INTERVAL_SEC 调整。

安全规则：
- 默认 dry-run，不真实推送 Telegram。
- 真实推送必须同时提供 --send --confirm-real-send。
- live/真实 loop 会先经过 readiness 门禁。
"""

PLACEHOLDER_WORDS = ("your", "token", "chat_id", "bot_token", "填写", "填入", "请输入", "xxx", "example")


def launch_runtime_diagnostics(launch: dict[str, object]) -> dict[str, object]:
    diagnostics = launch.get("diagnostics")
    return dict(diagnostics) if isinstance(diagnostics, dict) else {}


def launch_ai_on_demand_attachment(
    settings: Settings,
    alert: Mapping[str, object],
    dedup_key: str,
) -> tuple[dict[str, Any] | None, TelegramUrlButton | None]:
    """Build one immutable AI snapshot and its private-chat button.

    The snapshot is stored with the signal.  The button contains only the
    public bot username and an opaque signal reference; it never embeds signal
    facts, credentials, chat identifiers, or provider configuration.
    """

    if not bool(getattr(settings, "launch_ai_interpreter_enable", False)):
        return None, None
    if not bool(alert.get("launch_message_package_v2")):
        return None, None
    try:
        snapshot = build_launch_ai_context(alert)
    except (TypeError, ValueError):
        return None, None
    rule_result = snapshot.get("rule_result")
    if (
        not isinstance(rule_result, Mapping)
        or not str(rule_result.get("direction") or "").strip()
        or not str(rule_result.get("stage") or "").strip()
        or rule_result.get("data_complete") is not True
    ):
        return None, None

    button: TelegramUrlButton | None = None
    ai_configured = bool(
        str(getattr(settings, "ai_api_key", "")).strip()
        and str(getattr(settings, "ai_base_url", "")).strip()
        and str(getattr(settings, "ai_model", "")).strip()
    )
    private_admin_user_id = positive_telegram_user_id(
        getattr(settings, "tg_private_control_admin_user_id", None)
    )
    private_control_ready = bool(
        getattr(settings, "tg_private_control_enable", False)
        and private_admin_user_id is not None
    )
    if ai_configured and private_control_ready:
        public_ref = signal_public_ref(
            dedup_key,
            str(alert.get("symbol") or ""),
        )
        deep_link = build_launch_ai_deep_link(
            str(getattr(settings, "tg_bot_username", "")),
            public_ref,
        )
        if deep_link:
            button = TelegramUrlButton(
                AI_INTERPRET_URL_BUTTON_TEXT,
                deep_link,
            )
    return snapshot, button


def push_launch_messages(
    settings: Settings,
    engine: RadarEngine,
    gateway: TelegramGateway,
    launch: dict[str, object],
    args: argparse.Namespace,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    real_send = bool(args.send and args.confirm_real_send)
    cleanup_diagnostics: dict[str, object] = {
        "enabled": False,
        "mode": "retain_history_reply_chain",
        "retried_packages": 0,
        "deleted_messages": 0,
        "failed_deletions": 0,
        "topic_history_deleted": 0,
        "topic_history_delete_failures": 0,
        "topic_history_undeletable": 0,
        "charts_sent": 0,
        "chart_failures": 0,
        "protected_latest_messages": 0,
    }

    messages = list(launch.get("messages") or [])
    alerts = list(launch.get("alerts") or [])
    pushes: list[dict[str, object]] = []
    sent_alerts: list[dict[str, object]] = []
    topic_deleted_message_ids: list[int] = []
    for idx, message in enumerate(messages, start=1):
        alert = alerts[idx - 1]
        is_package = bool(alert.get("launch_message_package_v2"))
        chart_required = bool(is_package and settings.launch_chart_v2_enable)
        lifecycle = alert.get("launch_lifecycle")
        lifecycle = lifecycle if isinstance(lifecycle, dict) else {}
        dedup_key = (
            f"launch-package:{int(lifecycle.get('cycle_id') or 0)}:"
            f"{int(lifecycle.get('observation_id') or 0)}"
            if is_package
            else f"launch:{alert['symbol']}:{alert['stage']}"
        )
        push_record: dict[str, object] = {
            "symbol": str(alert.get("symbol", "")),
            "stage": str(alert.get("stage", "")),
            "reply_target_configured": bool(
                int(alert.get("reply_to_message_id", 0) or 0)
            ),
            "package_v2": is_package,
            "chart_v2": chart_required,
        }
        chart_bytes = alert.get("chart_png_bytes")
        if chart_required and not isinstance(chart_bytes, bytes):
            push_record["status"] = "skipped"
            push_record["reason"] = str(alert.get("chart_error") or "chart_unavailable")
            cleanup_diagnostics["chart_failures"] = int(
                cleanup_diagnostics["chart_failures"]
            ) + 1
            print(format_push_result_cn(
                "启动预警推送",
                "skipped",
                str(push_record["reason"]),
                index=idx,
                note="旧卡片已保留",
            ))
            pushes.append(push_record)
            continue

        signal_record = {
            key: value
            for key, value in alert.items()
            if key != "chart_png_bytes"
        }
        if is_package:
            signal_record.update({
                "evaluation_eligible": False,
                "launch_message_package_v2": True,
                "launch_cycle_id": int(lifecycle.get("cycle_id") or 0),
                "launch_cycle_no": int(lifecycle.get("cycle_no") or 0),
                "launch_observation_id": int(lifecycle.get("observation_id") or 0),
            })
        ai_snapshot, ai_button = launch_ai_on_demand_attachment(
            settings,
            alert,
            dedup_key,
        )
        if ai_snapshot is not None:
            signal_record["ai_context_snapshot"] = ai_snapshot
        push = gateway.send(
            str(message),
            "TG_LAUNCH_ALERT",
            dedup_key,
            send=args.send,
            confirm_real_send=args.confirm_real_send,
            cooldown_sec=0 if is_package else settings.launch_stage_cooldown_sec,
            parse_mode="HTML",
            reply_to_message_id=(
                int(alert.get("reply_to_message_id", 0) or 0) or None
            ),
            signal_records=[signal_record],
            photo=chart_bytes if chart_required else None,
            enrich_market_context=not is_package,
            url_button=ai_button,
        )
        print(format_push_result_cn(
            "启动预警推送",
            push.status,
            push.reason,
            index=idx,
        ))
        push_record["status"] = push.status
        push_record["reason"] = push.reason
        if push.status == "sent":
            new_message_ids = list(push.message_ids or [])
            if ai_button is not None and (
                push.signal_store_written is False
                or push.ai_snapshot_ready is False
            ):
                rollback = gateway.delete_messages_detailed(
                    new_message_ids,
                    reason="launch_ai_snapshot_persist_rollback",
                )
                push_record["status"] = "ai_snapshot_persist_failed"
                push_record["reason"] = "ai_snapshot_persist_failed"
                push_record["rollback_deleted"] = len(
                    rollback.get("deleted_ids") or []
                )
                push_record["rollback_failures"] = len(
                    rollback.get("failed_ids") or []
                )
                if chart_required:
                    alert.pop("chart_png_bytes", None)
                pushes.append(push_record)
                continue
            if chart_required:
                alert.pop("chart_png_bytes", None)
                chart_bytes = None
                push_record["photo_status"] = push.status
                push_record["photo_reason"] = push.reason
                cleanup_diagnostics["charts_sent"] = int(
                    cleanup_diagnostics["charts_sent"]
                ) + 1
            alert["message_ids"] = new_message_ids
            if is_package:
                try:
                    commit = engine.commit_launch_package(
                        alert,
                        new_message_ids,
                    )
                except (OSError, RuntimeError, ValueError, sqlite3.Error) as exc:
                    # Telegram has already accepted the new card. Remove only
                    # that card when local lifecycle persistence fails, so the
                    # previous reply chain remains authoritative.
                    rollback = gateway.delete_messages_detailed(
                        new_message_ids,
                        reason="launch_package_commit_exception_rollback",
                    )
                    push_record["status"] = "package_commit_failed"
                    push_record["package_commit"] = "local_error"
                    push_record["package_commit_error"] = type(exc).__name__
                    push_record["rollback_deleted"] = len(
                        rollback.get("deleted_ids") or []
                    )
                    push_record["rollback_failures"] = len(
                        rollback.get("failed_ids") or []
                    )
                    pushes.append(push_record)
                    continue
                push_record["package_commit"] = str(commit.get("status") or "")
                if commit.get("status") not in {"committed", "idempotent"}:
                    rollback = gateway.delete_messages_detailed(
                        new_message_ids,
                        reason="launch_package_commit_rollback",
                    )
                    push_record["status"] = "package_commit_failed"
                    push_record["rollback_deleted"] = len(
                        rollback.get("deleted_ids") or []
                    )
                    push_record["rollback_failures"] = len(
                        rollback.get("failed_ids") or []
                    )
                    pushes.append(push_record)
                    continue
                push_record["previous_messages_retained"] = True
            sent_alerts.append(alert)
        elif is_package and push.message_ids and real_send:
            rollback = gateway.delete_messages_detailed(
                list(push.message_ids),
                reason="launch_package_send_rollback",
            )
            push_record["rollback_deleted"] = len(
                rollback.get("deleted_ids") or []
            )
            push_record["rollback_failures"] = len(
                rollback.get("failed_ids") or []
            )
        if chart_required and push.status == "failed":
            cleanup_diagnostics["chart_failures"] = int(
                cleanup_diagnostics["chart_failures"]
            ) + 1
        if chart_required:
            alert.pop("chart_png_bytes", None)
        pushes.append(push_record)
    engine.mark_launch_pushed(sent_alerts)
    reconciliation = engine.reconcile_launch_topic_messages(
        deleted_ids=topic_deleted_message_ids,
    )
    cleanup_diagnostics["topic_state_reconciliation"] = reconciliation
    return pushes, cleanup_diagnostics


def _clean_config_value(value: str) -> str:
    return (value or "").strip().strip('"').strip("'")


def is_valid_telegram_bot_token(value: str) -> bool:
    token = _clean_config_value(value)
    lowered = token.lower()
    if not token or any(word in lowered for word in PLACEHOLDER_WORDS):
        return False
    return bool(re.fullmatch(r"\d{5,}:[A-Za-z0-9_-]{25,}", token))


def is_valid_telegram_chat_id(value: str) -> bool:
    chat_id = _clean_config_value(value)
    lowered = chat_id.lower()
    if not chat_id or any(word in lowered for word in PLACEHOLDER_WORDS):
        return False
    if re.fullmatch(r"-?\d{5,20}", chat_id):
        return True
    return bool(re.fullmatch(r"@[A-Za-z0-9_]{5,32}", chat_id))


def telegram_config_checks(settings: Settings) -> list[tuple[str, bool, str]]:
    token_ok = is_valid_telegram_bot_token(settings.tg_bot_token)
    chat_ok = is_valid_telegram_chat_id(settings.tg_chat_id)
    return [
        (
            "telegram_bot_token",
            token_ok,
            "TG_BOT_TOKEN 格式有效" if token_ok else "TG_BOT_TOKEN 缺失或格式无效，必须类似 123456:ABC...",
        ),
        (
            "telegram_chat_id",
            chat_ok,
            "TG_CHAT_ID 格式有效" if chat_ok else "TG_CHAT_ID 缺失或格式无效，通常是 -100... 或 @channel_username",
        ),
    ]


def telegram_topic_route_checks(
    settings: Settings,
    store: JsonStore,
) -> list[tuple[str, bool, str]]:
    gateway = TelegramGateway(settings, store)
    checks: list[tuple[str, bool, str]] = []
    for template_id in PRODUCTION_TOPIC_TEMPLATE_IDS:
        configured = gateway.topic_route_configured(template_id)
        topic_name = TOPIC_TEMPLATE_NAMES[template_id]
        checks.append((
            f"telegram_topic_{template_id.removeprefix('TG_').lower()}",
            configured,
            f"{topic_name}专属话题已配置"
            if configured
            else f"{topic_name}专属话题未配置",
        ))
    if (
        bool(getattr(settings, "altcoin_contract_anomaly_production_enable", False))
        and bool(
            getattr(
                settings,
                "altcoin_contract_anomaly_production_send_enable",
                False,
            )
        )
    ):
        template_id = "TG_ALTCOIN_CONTRACT_ANOMALY"
        configured = gateway.topic_route_configured(template_id)
        topic_name = TOPIC_TEMPLATE_NAMES.get(template_id, "山寨合约异动")
        checks.append((
            "telegram_topic_altcoin_contract_anomaly",
            configured,
            f"{topic_name}专属话题已配置"
            if configured
            else f"{topic_name}专属话题未配置",
        ))
    return checks


def configure_console_encoding() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="泡泡抓币：精简版加密监控工具")
    parser.add_argument(
        "command",
        nargs="?",
        default="status",
        choices=["about", "status", "doctor", "readiness", "stable-check", "database-backup", "signal-repair", "signal-effectiveness", "telegram-test", "telegram-topic-setup", "private-control", "announcement-risk", "flow-radar", "funding-alert", "altcoin-anomaly", "market-stream", "runtime-status", "radar-status", "cleanup", "watchlist", "launch-history", "launch-report", "once", "trial", "observe", "loop", "daemon", "live"],
        help="默认 status；doctor 检查环境；database-backup 创建并恢复验证 SQLite 备份；signal-effectiveness 回填信号结果",
    )
    parser.add_argument("--send", action="store_true", help="允许真实发送 Telegram；仍需要 --confirm-real-send")
    parser.add_argument("--confirm-real-send", action="store_true", help="确认真实发送 Telegram")
    parser.add_argument(
        "--topic-template",
        choices=sorted(TOPIC_TEMPLATE_NAMES),
        default=None,
        help="用于 telegram-topic-setup：选择要手工创建/修复的话题",
    )
    parser.add_argument("--apply", action="store_true", help="用于 signal-repair：应用修复（默认仅审计）")
    parser.add_argument("--force-cleanup", action="store_true", help="用于 cleanup：忽略清理间隔，立即执行")
    parser.add_argument("--top", type=int, default=12, help="用于 watchlist/报告：显示前 N 个候选")
    parser.add_argument("--records", type=int, default=100, help="用于 launch-report：统计最近 N 轮")
    parser.add_argument("--cycles", type=int, default=3, help="用于 trial：试跑轮数")
    parser.add_argument("--duration-minutes", type=int, default=360, help="用于 observe：观察总时长分钟数")
    parser.add_argument("--stream-duration-minutes", type=float, default=0, help="用于 market-stream 本地验收；0 表示常驻运行")
    parser.add_argument(
        "--altcoin-production",
        action="store_true",
        help="仅用于 market-stream：显式启用山寨合约异动生产控制器",
    )
    parser.add_argument("--interval", default=None, help="loop/daemon 的资金雷达摘要间隔秒数")
    parser.add_argument("--launch-interval", type=int, default=180, help="loop/daemon 的启动雷达间隔秒数")
    parser.add_argument("--radar-scan-limit", type=int, default=None, help="临时覆盖资金雷达扫描上限")
    parser.add_argument("--launch-scan-limit", type=int, default=None, help="临时覆盖启动雷达扫描上限")
    parser.add_argument("--flow-scan-limit", type=int, default=None, help="临时覆盖五因子资金流雷达扫描上限")
    parser.add_argument("--funding-scan-limit", type=int, default=None, help="临时覆盖资金费率警报扫描上限")
    parser.add_argument("--no-launch", action="store_true", help="本轮不运行启动雷达")
    parser.add_argument("--no-announcements", action="store_true", help="本轮不运行公告风险雷达")
    parser.add_argument("--no-flow", action="store_true", help="本轮不运行五因子资金流雷达")
    parser.add_argument("--no-funding-alert", action="store_true", help="本轮不运行资金费率警报")
    parser.add_argument("--json", action="store_true", help="为支持的命令输出完整 JSON")
    parser.add_argument("--no-save", action="store_true", help="用于 stable-check：只查看，不写入验收历史")
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="用于 altcoin-anomaly：仅使用本地缓存，不访问网络",
    )
    parser.add_argument(
        "--preview-telegram",
        action="store_true",
        help="用于 altcoin-anomaly：输出 Telegram 分页预览，不发送消息",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="用于 altcoin-anomaly：另存机器可读 JSON 到指定文件",
    )
    parser.add_argument(
        "--realtime-duration-sec",
        type=int,
        default=None,
        help="用于 altcoin-anomaly：运行30到3600秒的P2实时确认Dry-run",
    )
    return parser


def make_runtime() -> tuple[Settings, JsonStore, RadarEngine, TelegramGateway]:
    settings = Settings.load()
    store = JsonStore(settings.data_dir)
    engine = RadarEngine(settings, store)
    gateway = TelegramGateway(settings, store)
    return settings, store, engine, gateway


def apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    updates: dict[str, object] = {}
    radar_scan_limit = getattr(args, "radar_scan_limit", None)
    launch_scan_limit = getattr(args, "launch_scan_limit", None)
    flow_scan_limit = getattr(args, "flow_scan_limit", None)
    funding_scan_limit = getattr(args, "funding_scan_limit", None)
    if radar_scan_limit is not None:
        updates["radar_scan_limit"] = max(0, int(radar_scan_limit))
    if launch_scan_limit is not None:
        updates["launch_scan_limit"] = max(0, int(launch_scan_limit))
    if flow_scan_limit is not None:
        updates["flow_scan_limit"] = max(0, int(flow_scan_limit))
    if funding_scan_limit is not None:
        updates["funding_alert_scan_limit"] = max(0, int(funding_scan_limit))
    if not updates:
        return settings
    return replace(settings, **updates)


def effective_radar_switches(
    settings: Settings,
    args: argparse.Namespace,
) -> dict[str, bool]:
    """Return hot-reloadable automatic radar switches.

    Existing ``--no-*`` process flags remain the stronger override.  These
    switches govern only the long-running automatic scheduler; explicit
    one-shot maintenance commands keep their existing semantics.
    """

    return {
        "launch_alert": bool(
            settings.launch_alert_enable and not bool(args.no_launch)
        ),
        "radar_summary": bool(settings.radar_summary_enable),
        "funding_alert": bool(
            settings.funding_alert_enable
            and not bool(getattr(args, "no_funding_alert", False))
        ),
        "flow_radar": bool(
            settings.flow_radar_enable and not bool(args.no_flow)
        ),
        "announcement_risk": bool(
            settings.announcement_risk_enable
            and not bool(args.no_announcements)
        ),
    }


def radar_runtime_flags(switches: dict[str, bool]) -> dict[str, bool]:
    return {
        "no_launch": not switches["launch_alert"],
        "no_summary": not switches["radar_summary"],
        "no_funding_alert": not switches["funding_alert"],
        "no_flow": not switches["flow_radar"],
        "no_announcements": not switches["announcement_risk"],
    }


def reload_loop_settings(
    current: Settings,
    args: argparse.Namespace,
) -> tuple[Settings, str]:
    """Reload file-backed controls without terminating the shared process."""

    try:
        return apply_cli_overrides(Settings.load(), args), ""
    except (OSError, TypeError, ValueError):
        return current, "settings_reload_failed"


def last_known_settings_reader(
    initial: Settings,
) -> Callable[[], Settings]:
    """Return a loader that never falls back past its latest valid result."""

    last_known = initial

    def read() -> Settings:
        nonlocal last_known
        try:
            last_known = Settings.load()
        except (OSError, TypeError, ValueError):
            pass
        return last_known

    return read


def make_runtime_for_args(args: argparse.Namespace) -> tuple[Settings, JsonStore, RadarEngine, TelegramGateway]:
    settings, store, engine, gateway = make_runtime()
    updated = apply_cli_overrides(settings, args)
    if updated == settings:
        return settings, store, engine, gateway
    store = JsonStore(updated.data_dir)
    engine = RadarEngine(updated, store)
    gateway = TelegramGateway(updated, store)
    return updated, store, engine, gateway


def make_runtime_from_settings(
    settings: Settings,
) -> tuple[JsonStore, RadarEngine, TelegramGateway]:
    store = JsonStore(settings.data_dir)
    return store, RadarEngine(settings, store), TelegramGateway(settings, store)


def state_paths(settings: Settings) -> list[Path]:
    return [
        settings.tg_push_history_path,
        settings.runtime_status_path,
        settings.radar_state_path,
        settings.funding_snapshot_path,
        settings.funding_alert_state_path,
        settings.announcement_state_path,
        settings.launch_state_path,
        settings.launch_watchlist_path,
        settings.launch_watch_history_path,
        settings.divergence_state_path,
        settings.divergence_cooldown_path,
        settings.cleanup_state_path,
    ]


def build_status(settings: Settings, store: JsonStore) -> dict[str, object]:
    status = settings.redacted_status()
    status["state_files"] = store.exists_summary(state_paths(settings))
    return status


def print_status(settings: Settings, store: JsonStore) -> None:
    status = build_status(settings, store)
    print(json.dumps(status, ensure_ascii=False, indent=2))


def command_mode(args: argparse.Namespace) -> str:
    return str(getattr(args, "command", "") or "unknown")


def timestamp_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def write_runtime_status(
    settings: Settings,
    store: JsonStore,
    mode: str,
    status: str,
    **details: object,
) -> dict[str, object]:
    status_path = settings.runtime_status_path
    base_payload: dict[str, object] = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "status": status,
        "upstream_sources": UPSTREAM_SOURCE_METRICS.snapshot(),
    }
    incoming_task = str(details.get("task") or "")
    existing = store.load(status_path, {})
    merge_loop = bool(
        isinstance(existing, dict)
        and str(existing.get("task") or "") == "loop"
        and incoming_task == "loop"
        and str(existing.get("mode") or "") in {"loop", "daemon", "live"}
        and mode in {"loop", "daemon", "live"}
    )
    payload = dict(existing) if merge_loop else {}
    payload.update(base_payload)
    previous_diagnostics = payload.get("diagnostics")
    incoming_diagnostics = details.get("diagnostics")
    payload.update(details)
    if merge_loop and isinstance(previous_diagnostics, dict) and isinstance(
        incoming_diagnostics, dict
    ):
        merged_diagnostics = dict(previous_diagnostics)
        merged_diagnostics.update(incoming_diagnostics)
        payload["diagnostics"] = merged_diagnostics
    try:
        store.save(status_path, payload)
    except Exception as exc:
        print(f"[runtime-status] write failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return payload


def _load_runtime_status_or_empty(store: JsonStore, path: Path, label: str) -> dict[str, object]:
    data = store.load(path, {})
    if isinstance(data, dict) and data:
        return data
    return {
        "status": "empty",
        "path": str(path),
        "message": f"{label} runtime status has not been written yet",
    }


def print_runtime_status(settings: Settings, store: JsonStore) -> None:
    main_status = _load_runtime_status_or_empty(store, settings.runtime_status_path, "main")
    print(json.dumps({"main": main_status}, ensure_ascii=False, indent=2))


def print_radar_status(settings: Settings, store: JsonStore) -> None:
    print(json.dumps(
        build_market_radar_runtime_status(settings, store),
        ensure_ascii=False,
        indent=2,
    ))


def run_private_control(settings: Settings, store: JsonStore) -> int:
    """Run the isolated, admin-only Telegram private control worker."""

    if not settings.tg_private_control_enable:
        print("private_control_disabled", file=sys.stderr)
        return 2

    try:
        import fcntl
        import requests
        from runtime.private_alerts import PrivateAlertEvaluator
        from runtime.private_control import PrivateControlService
        from runtime.private_control_views import (
            render_fault_explanations,
            render_push_records,
            render_recent_signals,
            render_unpublished_reasons,
        )
        from scripts.paopao_config import ConfigManager
    except ImportError:
        print("private_control_runtime_unavailable", file=sys.stderr)
        return 2

    lock_path = settings.data_dir / "telegram_private_control_worker.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_handle = lock_path.open("a+", encoding="utf-8")
    try:
        lock_path.chmod(0o600)
        try:
            fcntl.flock(
                lock_handle.fileno(),
                fcntl.LOCK_EX | fcntl.LOCK_NB,
            )
        except BlockingIOError:
            print(
                "private_control_worker_already_running",
                file=sys.stderr,
            )
            return 2

        current_settings = last_known_settings_reader(settings)
        ai_on_demand_service = LaunchAiOnDemandService(
            settings_reader=current_settings,
            signal_store=SignalEventStore(settings.signal_events_db_path),
            session=requests.Session(),
        )

        def health_reader() -> dict[str, object]:
            active_settings = current_settings()
            checks = runtime_health_checks(
                active_settings,
                JsonStore(active_settings.data_dir),
            )
            states = {
                str(item.get("status") or "unknown")
                for item in checks
                if isinstance(item, dict)
            }
            overall = (
                "failed"
                if "fail" in states
                else "degraded"
                if states & {"warn", "warning", "degraded", "stale"}
                else "ok"
            )
            return {"status": overall, "checks": checks}

        def delivery_quota_reader() -> dict[str, object]:
            active_settings = current_settings()
            active_store = JsonStore(active_settings.data_dir)
            records = active_store.load(
                active_settings.tg_push_history_path,
                [],
            )
            cutoff = int(time.time()) - 3600
            used = sum(
                1
                for item in records
                if isinstance(item, dict)
                and item.get("status") == "sent"
                and isinstance(item.get("ts"), (int, float))
                and int(item["ts"]) >= cutoff
            ) if isinstance(records, list) else 0
            limit = max(0, int(active_settings.tg_global_hourly_limit))
            return {
                "limit": limit,
                "used": used,
                "remaining": max(0, limit - used),
            }

        def topic_status_reader() -> dict[str, object]:
            active_settings = current_settings()
            active_store = JsonStore(active_settings.data_dir)
            gateway = TelegramGateway(active_settings, active_store)
            templates = {
                "launch_alert": "TG_LAUNCH_ALERT",
                "radar_summary": "TG_RADAR_SUMMARY",
                "funding_alert": "TG_FUNDING_ALERT",
                "flow_radar": "TG_FLOW_RADAR",
                "announcement_risk": "TG_ANNOUNCEMENT_ALERT",
            }
            return {
                "bot": bool(active_settings.tg_bot_token),
                "chat": bool(active_settings.tg_chat_id),
                "topics": {
                    key: gateway.topic_route_configured(template)
                    for key, template in templates.items()
                },
            }

        def radar_status_reader() -> dict[str, object]:
            active_settings = current_settings()
            return build_market_radar_runtime_status(
                active_settings,
                JsonStore(active_settings.data_dir),
            )

        def data_freshness_reader() -> dict[str, object]:
            return {
                "checks": lightweight_freshness_checks(current_settings())
            }

        def recent_signals_reader() -> str:
            return render_recent_signals(current_settings().signal_events_db_path)

        def push_records_reader() -> str:
            return render_push_records(current_settings().tg_push_history_path)

        def unpublished_reasons_reader() -> str:
            return render_unpublished_reasons(
                current_settings().tg_push_history_path
            )

        def fault_explanations_reader() -> str:
            return render_fault_explanations(
                health_reader(),
                radar_status_reader(),
            )

        service = PrivateControlService(
            enabled=settings.tg_private_control_enable,
            bot_token=settings.tg_bot_token,
            admin_user_id=settings.tg_private_control_admin_user_id,
            offset_path=settings.tg_private_control_state_path,
            config_manager=ConfigManager(settings.base_dir),
            session=requests.Session(),
            radar_status_reader=radar_status_reader,
            health_reader=health_reader,
            delivery_quota_reader=delivery_quota_reader,
            topic_status_reader=topic_status_reader,
            recent_signals_reader=recent_signals_reader,
            push_records_reader=push_records_reader,
            unpublished_reasons_reader=unpublished_reasons_reader,
            fault_explanations_reader=fault_explanations_reader,
            ai_on_demand_requester=ai_on_demand_service.request,
        )
        permanent_errors = {
            "private_control_bot_not_configured",
            "private_control_admin_not_configured",
            "private_control_transport_not_configured",
            "telegram_auth_failed",
            "telegram_forbidden",
            "telegram_endpoint_not_found",
            "telegram_polling_conflict",
        }
        next_private_alert_check = 0.0
        while True:
            result = service.poll_once()
            status = result.get("status")
            if status == "failed":
                error = str(result.get("error") or "private_control_failed")
                print(error, file=sys.stderr)
                return 2 if error in permanent_errors else 1
            if status == "disabled":
                return 2
            now = time.time()
            if now >= next_private_alert_check:
                active_settings = current_settings()
                alert_result = PrivateAlertEvaluator(
                    enabled=active_settings.tg_private_control_alert_enable,
                    state_path=(
                        active_settings.tg_private_control_alert_state_path
                    ),
                    sender=service.send_private_alert,
                    radar_status_reader=radar_status_reader,
                    data_freshness_reader=data_freshness_reader,
                    delivery_quota_reader=delivery_quota_reader,
                    cooldown_sec=(
                        active_settings.tg_private_control_alert_cooldown_sec
                    ),
                ).run_once()
                if alert_result.get("status") in {
                    "state_unavailable",
                    "send_failed",
                    "send_failed_state_unavailable",
                }:
                    print(
                        "private_fault_alert_failed",
                        file=sys.stderr,
                    )
                next_private_alert_check = now + 60
    finally:
        try:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        lock_handle.close()


def print_cleanup(settings: Settings, store: JsonStore, force: bool) -> None:
    result = cleanup_runtime_artifacts(settings, store, force=force)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def refresh_signal_effectiveness(
    settings: Settings,
    *,
    signal_limit: int = 1_000,
) -> dict[str, object]:
    return SignalOutcomeTracker(
        settings.signal_events_db_path,
        settings.market_snapshots_db_path,
    ).refresh(signal_limit=signal_limit)


SIGNAL_EFFECTIVENESS_BACKGROUND_INTERVAL_SEC = 15 * 60
SIGNAL_EFFECTIVENESS_CADENCE_SCHEMA_VERSION = 1
SIGNAL_EFFECTIVENESS_CADENCE_FILE = "signal_effectiveness_cadence.json"


def _signal_effectiveness_cadence_path(settings: Settings) -> Path:
    return settings.data_dir / SIGNAL_EFFECTIVENESS_CADENCE_FILE


def load_signal_effectiveness_next_run_at(
    settings: Settings,
    store: JsonStore,
    *,
    now: float,
) -> float:
    """Restore the background cadence without blocking on invalid local state."""

    try:
        state = store.load(_signal_effectiveness_cadence_path(settings), {})
    except Exception as exc:
        print(
            "signal_effectiveness_cadence warning="
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
        return 0.0
    if not isinstance(state, dict):
        return 0.0
    try:
        schema_version = int(state.get("schema_version") or 0)
    except (TypeError, ValueError):
        return 0.0
    if schema_version != SIGNAL_EFFECTIVENESS_CADENCE_SCHEMA_VERSION:
        return 0.0
    try:
        last_attempt_at = float(state.get("last_attempt_at") or 0.0)
    except (TypeError, ValueError):
        return 0.0
    age = now - last_attempt_at
    if (
        not math.isfinite(last_attempt_at)
        or last_attempt_at <= 0
        or age < 0
        or age >= SIGNAL_EFFECTIVENESS_BACKGROUND_INTERVAL_SEC
    ):
        return 0.0
    return last_attempt_at + SIGNAL_EFFECTIVENESS_BACKGROUND_INTERVAL_SEC


def _persist_signal_effectiveness_attempt(
    settings: Settings,
    store: JsonStore,
    *,
    attempted_at: float,
) -> None:
    store.update(
        _signal_effectiveness_cadence_path(settings),
        lambda _current: {
            "schema_version": SIGNAL_EFFECTIVENESS_CADENCE_SCHEMA_VERSION,
            "last_attempt_at": float(attempted_at),
        },
        {},
    )


def refresh_signal_effectiveness_if_due(
    settings: Settings,
    store: JsonStore,
    *,
    now: float,
    next_run_at: float,
) -> tuple[dict[str, object] | None, float]:
    """Run the background outcome refresh at most once per 15-minute period."""

    if now < next_run_at:
        return None, next_run_at
    following_run_at = now + SIGNAL_EFFECTIVENESS_BACKGROUND_INTERVAL_SEC
    try:
        _persist_signal_effectiveness_attempt(
            settings,
            store,
            attempted_at=now,
        )
    except Exception as exc:
        print(
            "signal_effectiveness_cadence warning="
            f"{type(exc).__name__}",
            file=sys.stderr,
        )
    try:
        result = refresh_signal_effectiveness(settings)
    except (OSError, sqlite3.Error, ValueError) as exc:
        result = {"status": "failed", "error": type(exc).__name__}
    return result, following_run_at


def print_signal_effectiveness(settings: Settings) -> None:
    print(json.dumps(
        refresh_signal_effectiveness(settings, signal_limit=5_000),
        ensure_ascii=False,
        indent=2,
    ))


def print_database_backup(settings: Settings) -> int:
    report = backup_databases(settings)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("status") == "ok" else 1


def print_doctor(settings: Settings, store: JsonStore) -> None:
    status = build_status(settings, store)
    status["runtime"] = {
        "safe_default": "dry_run",
        "real_send_requires": "--send --confirm-real-send",
        "auto_cleanup": "enabled" if settings.cleanup_enable else "disabled",
    }
    print(json.dumps(status, ensure_ascii=False, indent=2))


def _stable_check_status_label(status: str) -> str:
    return {
        "ready": "达到稳定版标准",
        "attention": "基本可运行，建议关注",
        "blocked": "未达稳定版标准",
        "ok": "通过",
        "warn": "关注",
        "fail": "未达标",
    }.get(str(status or ""), str(status or "未知"))


def print_stable_check(as_json: bool = False, save: bool = True) -> int:
    settings, store, _engine, _gateway = make_runtime()
    checks: list[dict[str, object]] = []
    for name, ok, detail in telegram_config_checks(settings):
        checks.append({"name": name, "status": "ok" if ok else "fail", "detail": detail})
    checks.extend(runtime_health_checks(settings, store))

    fail_count = sum(item["status"] == "fail" for item in checks)
    warn_count = sum(item["status"] == "warn" for item in checks)
    status = "blocked" if fail_count else "attention" if warn_count else "ready"
    snapshot: dict[str, object] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "scope": "telegram-bot-only",
        "version": (settings.base_dir / "VERSION").read_text(encoding="utf-8").strip() if (settings.base_dir / "VERSION").exists() else "unknown",
        "stability": {
            "status": status,
            "summary": f"BOT 核心检查：失败 {fail_count}，待预热 {warn_count}",
            "checks": checks,
        },
    }
    if save:
        latest_path = settings.data_dir / "stability_latest.json"
        history_path = settings.data_dir / "stability_history.json"
        store.save(latest_path, snapshot)

        def append_history(current: object) -> list[object]:
            history = list(current) if isinstance(current, list) else []
            history.append(snapshot)
            return history[-100:]

        history = store.update(history_path, append_history, [])
        snapshot["stability_saved"] = {"saved": True, "history_count": len(history)}

    stability = snapshot["stability"]
    if as_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        print("泡泡雷达 BOT-only 稳定性自检")
        print(f"生成时间: {snapshot.get('generated_at', '')}")
        print(f"版本: {snapshot.get('version', '')}")
        print(f"状态: {_stable_check_status_label(str(stability.get('status') or ''))}")
        print(f"摘要: {stability.get('summary') or ''}")
        saved = snapshot.get("stability_saved", {}) if isinstance(snapshot.get("stability_saved"), dict) else {}
        if saved.get("saved"):
            print(f"记录: 已保存 latest/history，历史 {saved.get('history_count', 0)} 条")
        elif not save:
            print("记录: 本次未保存（--no-save）")
        print("")
        print("检查项:")
        checks = stability.get("checks", []) if isinstance(stability.get("checks"), list) else []
        for item in checks:
            if not isinstance(item, dict):
                continue
            print(f"- {item.get('name', '')}: {_stable_check_status_label(str(item.get('status') or ''))} - {item.get('detail', '')}")
    status = str(stability.get("status") or "")
    if status == "blocked":
        return 2
    if status == "attention":
        return 1
    return 0


def run_telegram_test(args: argparse.Namespace) -> int:
    settings, _store, _engine, gateway = make_runtime()
    requested_template = str(args.topic_template or "").strip()
    if requested_template not in {"", "TG_ALTCOIN_CONTRACT_ANOMALY"}:
        print(format_push_result_cn(
            "Telegram 验收测试",
            "blocked",
            "unsupported_test_topic",
        ))
        return 2
    if args.send and args.confirm_real_send:
        checks = telegram_config_checks(settings)
        failed = [(name, message) for name, ok, message in checks if not ok]
        if failed:
            print(format_push_result_cn(
                "Telegram 测试",
                "blocked",
                "invalid_telegram_config",
            ))
            for name, message in failed:
                print(f"- 待处理 {check_name_text(name)}：{message}")
            return 2
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    altcoin_topic_test = requested_template == "TG_ALTCOIN_CONTRACT_ANOMALY"
    template_id = (
        "TG_ALTCOIN_CONTRACT_ANOMALY"
        if altcoin_topic_test
        else "TG_TEST_MESSAGE"
    )
    text = "\n".join(
        [
            "🧪【山寨合约异动｜验收测试】",
            f"时间：{now}",
            "用途：验证人工预建固定话题的路由与发送权限。",
            "说明：这不是交易信号，不代表任何市场判断。",
        ]
        if altcoin_topic_test
        else [
            "🧪 泡泡抓币 Telegram 测试",
            f"时间: {now}",
            "用途: 验证 bot token / chat id / topic 配置",
            "说明: 这不是交易信号",
        ]
    )
    result = gateway.send(
        text,
        template_id,
        (
            "altcoin-contract-anomaly:acceptance-test:"
            if altcoin_topic_test
            else "telegram-test:"
        ) + datetime.now().strftime("%Y%m%d%H%M"),
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=0,
        daily_limit=None,
        parse_mode="HTML" if altcoin_topic_test else "",
    )
    print(format_push_result_cn(
        "Telegram 测试",
        result.status,
        result.reason,
    ))
    if result.status == "blocked":
        return 2
    if result.status == "failed":
        return 1
    return 0


def run_telegram_topic_setup(args: argparse.Namespace) -> int:
    settings, store, _engine, _gateway = make_runtime_for_args(args)
    gateway = TelegramGateway(settings, store)
    result = gateway.setup_topic(
        str(args.topic_template or ""),
        send=bool(args.send),
        confirm_real_send=bool(args.confirm_real_send),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result.get("status") == "ok":
        return 0
    if result.get("status") == "blocked":
        return 2
    return 1


def deliver_announcement_risk(
    engine: AnnouncementRiskRadar,
    gateway: TelegramGateway,
    result: dict[str, Any],
    args: argparse.Namespace,
) -> tuple[str, list[dict[str, str]]]:
    if str(result.get("status") or "") != "ok":
        return "failed", []
    push_status = "no_new_alerts"
    pushes: list[dict[str, str]] = []
    sent_alerts: list[dict[str, Any]] = []
    for index, (message, alert) in enumerate(
        zip(result.get("messages", []), result.get("alerts", [])),
        start=1,
    ):
        push = gateway.send(
            str(message),
            "TG_ANNOUNCEMENT_ALERT",
            f"announcement:{alert.get('kind')}:{alert.get('code')}",
            send=args.send,
            confirm_real_send=args.confirm_real_send,
            cooldown_sec=0,
            daily_limit=8,
            parse_mode="HTML",
            signal_records=[dict(alert)],
        )
        print(format_push_result_cn(
            "公告风险推送",
            push.status,
            push.reason,
            index=index,
        ))
        push_status = push.status
        pushes.append({"status": push.status, "reason": push.reason})
        if push.status == "sent":
            alert["message_ids"] = list(push.message_ids or [])
            sent_alerts.append(alert)
    engine.mark_announcements_seen(sent_alerts)
    return push_status, pushes


def push_announcement_risk(
    settings: Settings,
    store: JsonStore,
    gateway: TelegramGateway,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any]]:
    engine = AnnouncementRiskRadar(settings, store)
    with BinanceDataSource(settings) as source:
        result = engine.build_announcement_alerts(source)
        diagnostics = {
            "status": result.get("status", "unknown"),
            "error": result.get("error", ""),
            "articles_scanned": int(result.get("articles_scanned") or 0),
            "alerts_classified": int(result.get("alerts_classified") or 0),
            "new_alerts": len(result.get("alerts") or []),
            "binance": source.diagnostics(),
        }
    push_status, pushes = deliver_announcement_risk(
        engine,
        gateway,
        result,
        args,
    )
    diagnostics["pushes"] = pushes
    return push_status, diagnostics


def run_announcement_risk(args: argparse.Namespace) -> int:
    settings, store, _engine, gateway = make_runtime_for_args(args)
    push_status, diagnostics = push_announcement_risk(
        settings,
        store,
        gateway,
        args,
    )
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    return 1 if push_status == "failed" or diagnostics.get("status") != "ok" else 0


def push_market_summary(
    settings: Settings,
    store: JsonStore,
    gateway: TelegramGateway,
    args: argparse.Namespace,
) -> tuple[str, dict[str, Any]]:
    engine = MarketSummaryRadar(settings, store)
    with BinanceDataSource(settings) as source:
        summary = engine.build_money_radar_summary(source)
        diagnostics = source.diagnostics()
    push = gateway.send(
        summary["text"],
        summary["template_id"],
        summary["dedup_key"],
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=settings.radar_summary_min_interval_sec,
        daily_limit=settings.radar_summary_max_daily_push,
        parse_mode="HTML",
        signal_records=list(summary.get("context_records") or []),
    )
    print(format_push_result_cn(
        "资金摘要推送",
        push.status,
        push.reason,
    ))
    return push.status, {
        "binance": diagnostics,
        "template_id": str(summary.get("template_id") or ""),
        "dedup_key": str(summary.get("dedup_key") or ""),
    }


def run_flow_radar(args: argparse.Namespace) -> int:
    settings, _store, _engine, gateway = make_runtime_for_args(args)
    with BinanceDataSource(settings) as source:
        flow = FlowRadarEngine(settings).build(source)
    try:
        saved = persist_flow_market_rows(settings, flow)
        flow["diagnostics"]["market_snapshot"] = {"status": "saved" if saved else "empty", "count": saved}
    except Exception as exc:
        flow["diagnostics"]["market_snapshot"] = {"status": "failed", "error": type(exc).__name__}
    push = gateway.send(
        flow["text"],
        flow["template_id"],
        flow["dedup_key"],
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=max(60, settings.flow_interval_sec),
        parse_mode="HTML",
        signal_records=list(flow.get("items") or []),
    )
    print(format_push_result_cn(
        "五因子资金流推送",
        push.status,
        push.reason,
    ))
    print(json.dumps(flow["diagnostics"], ensure_ascii=False, indent=2))
    return 0 if push.status != "failed" else 1


def push_flow_radar(settings: Settings, gateway: TelegramGateway, args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    with BinanceDataSource(settings) as source:
        flow = FlowRadarEngine(settings).build(source)
    try:
        saved = persist_flow_market_rows(settings, flow)
        flow["diagnostics"]["market_snapshot"] = {"status": "saved" if saved else "empty", "count": saved}
    except Exception as exc:
        flow["diagnostics"]["market_snapshot"] = {"status": "failed", "error": type(exc).__name__}
    push = gateway.send(
        flow["text"],
        flow["template_id"],
        flow["dedup_key"],
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=max(60, settings.flow_interval_sec),
        parse_mode="HTML",
        signal_records=list(flow.get("items") or []),
    )
    print(format_push_result_cn(
        "五因子资金流推送",
        push.status,
        push.reason,
    ))
    return push.status, flow["diagnostics"]


def run_funding_alert(args: argparse.Namespace) -> int:
    settings, store, _engine, gateway = make_runtime_for_args(args)
    push_status, diagnostics = push_funding_alert(settings, store, gateway, args)
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    return 0 if push_status != "failed" else 1


def push_funding_alert(
    settings: Settings,
    store: JsonStore,
    gateway: TelegramGateway,
    args: argparse.Namespace,
) -> tuple[str, dict[str, object]]:
    funding_engine = FundingAlertEngine(settings, store)
    with BinanceDataSource(settings) as source:
        result = funding_engine.build(source)
    push_status = "skipped"
    sent_alerts: list[dict[str, object]] = []
    for idx, message in enumerate(result["messages"], start=1):
        alert = result["alerts"][idx - 1]
        push = gateway.send(
            message,
            result["template_id"],
            str(alert.get("dedup_key") or f"funding-alert:{idx}"),
            send=args.send,
            confirm_real_send=args.confirm_real_send,
            cooldown_sec=max(60, settings.funding_alert_cooldown_sec),
            parse_mode="HTML",
            reply_to_message_id=int(alert.get("reply_to_message_id", 0) or 0) or None,
            signal_records=[alert],
        )
        print(format_push_result_cn(
            "资金费率警报推送",
            push.status,
            push.reason,
            index=idx,
        ))
        push_status = push.status
        if push.status == "sent":
            alert["message_ids"] = push.message_ids or []
            sent_alerts.append(alert)
    funding_engine.mark_pushed(sent_alerts)
    return push_status, result["diagnostics"]


def launch_alert_pressure_within_limit(total_alerts: int, records: int) -> bool:
    """Allow multiple symbol candidates per scan without treating them as sent messages."""

    return max(0, int(total_alerts)) <= max(1, max(0, int(records)) * 2)


def _launch_metric(
    record: Mapping[str, object],
    canonical_key: str,
    legacy_key: str,
) -> int:
    value = record.get(canonical_key) if canonical_key in record else None
    if canonical_key not in record:
        value = record.get(legacy_key)
    return int(value) if isinstance(value, (int, float)) else 0


def _optional_launch_metric(
    record: Mapping[str, object],
    canonical_key: str,
    legacy_key: str = "",
) -> int | None:
    if canonical_key in record:
        value = record.get(canonical_key)
    else:
        value = record.get(legacy_key) if legacy_key else None
    return int(value) if isinstance(value, (int, float)) else None


def current_launch_alert_candidate_count(
    settings: Settings,
    store: JsonStore,
) -> int | None:
    """Count distinct currently actionable launch symbols for readiness."""

    if not settings.launch_state_path.exists():
        return None
    state = store.load(settings.launch_state_path, None)
    if not isinstance(state, dict):
        return None
    actionable_stages = {"primed", "breakout", "launched"}

    return sum(
        1
        for record in state.values()
        if isinstance(record, dict)
        and str(record.get("stage") or "") in actionable_stages
        and _launch_metric(record, "evidence_score", "score")
        >= settings.launch_min_score_push
    )


def print_readiness(settings: Settings, store: JsonStore) -> int:
    records = store.load(settings.launch_watch_history_path, [])
    record_count = len(records) if isinstance(records, list) else 0
    report = build_launch_report(records[-100:] if isinstance(records, list) else [], settings)
    pressure_total = int(report.get("total_alerts", 0) or 0)
    pressure_records = int(report.get("records", 0) or 0)
    pressure_ok = launch_alert_pressure_within_limit(
        pressure_total,
        pressure_records,
    )
    pressure_message = (
        f"最近推送候选 {pressure_total} / {pressure_records} 轮"
        "（上限每轮 2 个候选；不等于实际推送）"
    )
    if settings.launch_message_package_v2_enable:
        current_candidates = current_launch_alert_candidate_count(settings, store)
        if current_candidates is not None:
            pressure_ok = launch_alert_pressure_within_limit(
                current_candidates,
                1,
            )
            pressure_message = (
                f"当前独立有效候选 {current_candidates} / 2；"
                f"历史候选 {pressure_total} / {pressure_records} 轮"
                "（dry-run 会重复记录尚未成功发布的同一事件）"
            )
    runtime_health = [
        item for item in runtime_health_checks(settings, store)
        if item.get("name") != "runtime_status"
    ]
    health_failures = [item for item in runtime_health if item.get("status") == "fail"]
    blocking_checks = [
        *telegram_config_checks(settings),
        *telegram_topic_route_checks(settings, store),
        (
            "runtime_health",
            not health_failures,
            "BOT 核心数据健康"
            if not health_failures
            else "；".join(str(item.get("detail") or "") for item in health_failures),
        ),
        ("observe_history", record_count >= 5, f"启动观察历史 {record_count} 轮"),
        ("history_file", settings.launch_watch_history_path.exists(), "启动观察历史文件存在" if settings.launch_watch_history_path.exists() else "启动观察历史文件不存在"),
    ]
    passed = sum(1 for _name, ok, _message in blocking_checks if ok)
    print(f"真实推送准备度: {passed}/{len(blocking_checks)}")
    for name, ok, message in blocking_checks:
        mark = "✅ 已通过" if ok else "⏳ 待处理"
        print(f"- {mark} {check_name_text(name)}：{message}")
    pressure_mark = "✅ 正常" if pressure_ok else "⚠️ 仅提醒"
    print(
        f"- {pressure_mark} {check_name_text('launch_alert_pressure')}："
        f"{pressure_message}；运行时仍受单轮与每小时发送额度限制"
    )
    print("")
    print(format_launch_report(settings, store, 100, 8, records=records))
    if passed == len(blocking_checks):
        print("")
        print("下一步：可以在中文菜单中执行一次真实 Telegram 测试。")
        return 0
    print("")
    print("下一步：继续使用安全演练模式观察，或补齐缺少的 Telegram 配置。")
    return 1


def require_real_send_gate(settings: Settings, store: JsonStore, args: argparse.Namespace) -> int:
    if not args.send or not args.confirm_real_send:
        print("真实推送已阻止：必须同时提供 --send --confirm-real-send。")
        return 2
    readiness = print_readiness(settings, store)
    if readiness != 0:
        print("真实推送已阻止：准备检查未通过。")
        return 2
    return 0


def bootstrap_live_market_snapshot(
    settings: Settings,
    store: JsonStore,
) -> dict[str, object]:
    """Refresh a stale snapshot without opening the Telegram send path."""

    market_check = next(
        (
            item
            for item in runtime_health_checks(settings, store)
            if item.get("name") == "market_snapshots_freshness"
        ),
        {},
    )
    if market_check.get("status") != "fail":
        return {"status": "not_needed"}
    source: BinanceDataSource | None = None
    try:
        source = BinanceDataSource(settings)
        result = persist_market_batch(
            settings,
            source=source,
            force=True,
        )
        return {
            "status": str(result.get("status") or "unknown"),
            "count": int(result.get("count") or 0),
            "telegram_calls": 0,
        }
    except Exception as exc:
        return {
            "status": "failed",
            "error": type(exc).__name__,
            "telegram_calls": 0,
        }
    finally:
        if source is not None:
            source.close()


def print_watchlist(settings: Settings, store: JsonStore, top_n: int) -> None:
    data = store.load(settings.launch_watchlist_path, {})
    if not isinstance(data, dict) or not data.get("items"):
        print("暂无启动候选记录。先运行：python main.py once")
        return
    items = data.get("items", [])
    if not isinstance(items, list):
        print("启动候选记录格式异常。")
        return
    print(f"启动候选观察表 | 更新时间: {data.get('updated_at', 'unknown')} | 数量: {data.get('count', len(items))}")
    for idx, item in enumerate(items[:max(1, top_n)], start=1):
        reasons = "；".join(item.get("reasons") or []) or "无触发项"
        discovery_score = _launch_metric(item, "discovery_score", "score")
        evidence_score = _optional_launch_metric(
            item,
            "evidence_score",
            "score",
        )
        evidence_text = str(evidence_score) if evidence_score is not None else "待确认"
        timing_stage = str(item.get("timing_stage") or "待确认")
        execution_status = str(item.get("execution_status") or "待确认")
        print(
            f"{idx:02d}. {item.get('symbol', ''):<12} "
            f"发现{discovery_score:>3} | 证据{evidence_text:>3} | "
            f"阶段{timing_stage} | 执行{execution_status} | "
            f"15m价{float(item.get('price_15m', 0)):+.2f}% | "
            f"1h价{float(item.get('price_1h', 0)):+.2f}% | "
            f"15m OI{float(item.get('oi_15m', 0)):+.2f}% | "
            f"1h OI{float(item.get('oi_1h', 0)):+.2f}% | "
            f"量{float(item.get('volume_ratio', 0)):.2f}x | {reasons}"
        )


def print_launch_history(settings: Settings, store: JsonStore, top_n: int) -> None:
    records = store.load(settings.launch_watch_history_path, [])
    if not isinstance(records, list) or not records:
        print("暂无启动观察历史。先运行：python main.py trial --cycles 1")
        return
    selected = records[-max(1, top_n):]
    print(f"启动观察历史 | 总记录: {len(records)} | 最近: {len(selected)}")
    for idx, record in enumerate(selected, start=1):
        if not isinstance(record, dict):
            continue
        buckets = record.get("buckets") if isinstance(record.get("buckets"), dict) else {}
        top_symbols = ", ".join(record.get("top_symbols", [])[:5]) if isinstance(record.get("top_symbols"), list) else ""
        if "top_discovery_score" in record or "top_evidence_score" in record:
            discovery = _optional_launch_metric(record, "top_discovery_score")
            evidence = _optional_launch_metric(record, "top_evidence_score")
            score_text = (
                f"最高发现分{discovery if discovery is not None else '待确认'} | "
                f"最高证据分{evidence if evidence is not None else '待确认'}"
            )
        else:
            score_text = f"旧版分数{int(record.get('top_score', 0) or 0)}"
        print(
            f"{idx:02d}. {record.get('updated_at', 'unknown')} | "
            f"扫描{int(record.get('scanned', 0))} | "
            f"{score_text} | "
            f"推送候选{int(record.get('alert_count', 0))} | "
            f"观察{int(buckets.get('watching', 0))}/预警{int(buckets.get('primed', 0))}/确认{int(buckets.get('breakout', 0))}/瞬间{int(buckets.get('launched', 0))} | "
            f"{top_symbols}"
        )


def build_launch_report(records: list[dict[str, object]], settings: Settings) -> dict[str, object]:
    valid = [record for record in records if isinstance(record, dict)]
    top_evidence_scores = [
        value
        for record in valid
        if (
            value := _optional_launch_metric(record, "top_evidence_score")
        ) is not None
    ]
    top_discovery_scores = [
        value
        for record in valid
        if (
            value := _optional_launch_metric(record, "top_discovery_score")
        ) is not None
    ]
    legacy_top_scores = [
        int(record.get("top_score", 0) or 0)
        for record in valid
        if "top_discovery_score" not in record
        and "top_evidence_score" not in record
    ]
    total_scanned = sum(int(record.get("scanned", 0) or 0) for record in valid)
    total_alerts = sum(int(record.get("alert_count", 0) or 0) for record in valid)
    bucket_totals: Counter[str] = Counter()
    symbol_counts: Counter[str] = Counter()
    for record in valid:
        buckets = record.get("buckets")
        if isinstance(buckets, dict):
            for key, value in buckets.items():
                bucket_totals[str(key)] += int(value or 0)
        top_symbols = record.get("top_symbols")
        if isinstance(top_symbols, list):
            symbol_counts.update(
                str(symbol) for symbol in top_symbols
                if symbol and not is_excluded_symbol(str(symbol), settings)
            )

    effective_top_scores = top_evidence_scores or legacy_top_scores
    max_top_score = max(effective_top_scores) if effective_top_scores else 0
    avg_top_score = (
        round(sum(effective_top_scores) / len(effective_top_scores), 2)
        if effective_top_scores
        else 0
    )
    max_top_discovery_score = (
        max(top_discovery_scores) if top_discovery_scores else None
    )
    avg_top_discovery_score = (
        round(sum(top_discovery_scores) / len(top_discovery_scores), 2)
        if top_discovery_scores
        else None
    )
    suggestion = "样本不足，先继续 dry-run。"
    if len(valid) >= 5:
        active_count = (
            bucket_totals.get("watching", 0)
            + bucket_totals.get("primed", 0)
            + bucket_totals.get("breakout", 0)
            + bucket_totals.get("launched", 0)
        )
        if total_alerts >= len(valid):
            suggestion = "推送候选偏多，先提高 LAUNCH_MIN_SCORE_PUSH 或 LAUNCH_PRIMED_SCORE。"
        elif max_top_score < settings.launch_watch_score:
            suggestion = "近期最高分低于观察线，市场暂时没有明显启动形态，阈值无需下调。"
        elif active_count > 0 and total_alerts == 0:
            suggestion = "已有观察级信号但未到推送线，适合继续 dry-run 观察，不急开真实推送。"
        else:
            suggestion = "当前阈值暂时可保持，继续积累样本。"

    return {
        "records": len(valid),
        "total_scanned": total_scanned,
        "total_alerts": total_alerts,
        "max_top_score": max_top_score,
        "avg_top_score": avg_top_score,
        "max_top_discovery_score": max_top_discovery_score,
        "avg_top_discovery_score": avg_top_discovery_score,
        "evidence_score_records": len(top_evidence_scores),
        "legacy_score_records": len(legacy_top_scores),
        "max_legacy_top_score": max(legacy_top_scores) if legacy_top_scores else None,
        "avg_legacy_top_score": (
            round(sum(legacy_top_scores) / len(legacy_top_scores), 2)
            if legacy_top_scores
            else None
        ),
        "buckets": dict(bucket_totals),
        "top_symbols": symbol_counts.most_common(10),
        "suggestion": suggestion,
    }


def is_excluded_symbol(symbol: str, settings: Settings) -> bool:
    coin = symbol.upper()
    if coin.endswith("USDT"):
        coin = coin[:-4]
    return coin in set(settings.excluded_base_assets)


def print_launch_report(settings: Settings, store: JsonStore, record_limit: int, top_n: int) -> None:
    print(format_launch_report(settings, store, record_limit, top_n))


def format_launch_report(
    settings: Settings,
    store: JsonStore,
    record_limit: int,
    top_n: int,
    *,
    records: list[Any] | None = None,
) -> str:
    records = (
        store.load(settings.launch_watch_history_path, [])
        if records is None
        else records
    )
    if not isinstance(records, list) or not records:
        return "暂无启动观察历史。先运行：python main.py trial --cycles 1"
    selected = records[-max(1, record_limit):]
    report = build_launch_report(selected, settings)
    buckets = report["buckets"] if isinstance(report["buckets"], dict) else {}
    lines = [
        f"启动历史分析 | 最近{report['records']}轮",
        f"扫描合计: {report['total_scanned']} | 推送候选: {report['total_alerts']}",
    ]
    if report.get("max_top_discovery_score") is not None:
        lines.append(
            f"最高发现分: {report['max_top_discovery_score']} | "
            f"平均最高发现分: {report['avg_top_discovery_score']}"
        )
    if int(report.get("evidence_score_records") or 0) > 0:
        lines.append(
            f"最高方向证据分: {report['max_top_score']} | "
            f"平均最高方向证据分: {report['avg_top_score']}"
        )
    if int(report.get("legacy_score_records") or 0) > 0:
        lines.append(
            f"旧版分数（语义无法回填）: 最高{report['max_legacy_top_score']} | "
            f"平均{report['avg_legacy_top_score']}"
        )
    lines.append(
        (
            "阶段合计: "
            f"观察{int(buckets.get('watching', 0))} / "
            f"预警{int(buckets.get('primed', 0))} / "
            f"确认{int(buckets.get('breakout', 0))} / "
            f"瞬间{int(buckets.get('launched', 0))}"
        )
    )
    symbols = report["top_symbols"] if isinstance(report["top_symbols"], list) else []
    if symbols:
        shown = "，".join(f"{symbol}({count})" for symbol, count in symbols[:max(1, top_n)])
        lines.append(f"高频候选: {shown}")
    lines.append(f"建议: {report['suggestion']}")
    return "\n".join(lines)


def format_observe_report(
    settings: Settings,
    store: JsonStore,
    record_limit: int,
    top_n: int,
    *,
    started_at: str,
    cycles: int,
    failures: int,
    status: str,
    last_error: str = "",
) -> str:
    lines = [
        "启动 dry-run 观察报告",
        f"状态: {status}",
        f"开始: {started_at}",
        f"更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"已跑轮数: {cycles} | 错误次数: {failures}",
        "模式: dry-run，不真实发送 Telegram",
    ]
    if last_error:
        lines.append(f"最近错误: {last_error}")
    lines.extend(["", format_launch_report(settings, store, record_limit, top_n)])
    return "\n".join(lines)


def save_observe_report(
    settings: Settings,
    store: JsonStore,
    record_limit: int,
    top_n: int,
    *,
    started_at: str,
    cycles: int,
    failures: int,
    status: str,
    last_error: str = "",
) -> Path:
    report_text = format_observe_report(
        settings,
        store,
        record_limit,
        top_n,
        started_at=started_at,
        cycles=cycles,
        failures=failures,
        status=status,
        last_error=last_error,
    )
    report_path = settings.data_dir / "launch_observe_report.txt"
    report_path.write_text(report_text + "\n", encoding="utf-8")
    return report_path


def refresh_shared_market_snapshot(
    settings: Settings,
    *,
    source: BinanceDataSource | None = None,
) -> dict[str, object]:
    """Refresh the shared market fact store independently of radar switches."""

    owned_source = source is None
    try:
        if source is None:
            source = BinanceDataSource(settings)
        result = persist_market_batch(settings, source=source)
        return dict(result) if isinstance(result, dict) else {"status": "ok"}
    except Exception:
        return {
            "status": "failed",
            "error": "market_snapshot_refresh_failed",
        }
    finally:
        if owned_source and source is not None:
            source.close()


def run_once(
    args: argparse.Namespace,
    *,
    refresh_effectiveness: bool = True,
) -> int:
    settings, store, engine, gateway = make_runtime_for_args(args)
    mode = command_mode(args)
    runtime_task = (
        "loop" if mode in {"loop", "daemon", "live"} else "once"
    )
    write_runtime_status(
        settings,
        store,
        mode,
        "running",
        task=runtime_task,
        real_send=bool(args.send and args.confirm_real_send),
        no_launch=bool(args.no_launch),
        no_announcements=bool(args.no_announcements),
        no_flow=bool(args.no_flow),
        no_funding_alert=bool(
            getattr(args, "no_funding_alert", False)
        ),
        radar_scan_limit=settings.radar_scan_limit,
        launch_scan_limit=settings.launch_scan_limit,
        flow_scan_limit=settings.flow_scan_limit,
    )
    result = engine.run_once(
        include_launch=not args.no_launch,
        include_announcements=not args.no_announcements,
    )
    if not args.no_launch:
        launch_delete_callback = (
            gateway.delete_messages_detailed
            if args.send and args.confirm_real_send
            else None
        )
        result["diagnostics"]["launch_lifecycle_cleanup"] = (
            engine.cleanup_failed_launch_messages(launch_delete_callback)
        )

    summary = result["summary"]
    push = gateway.send(
        summary["text"],
        summary["template_id"],
        summary["dedup_key"],
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=settings.radar_summary_min_interval_sec,
        daily_limit=settings.radar_summary_max_daily_push,
        parse_mode="HTML",
        signal_records=list(summary.get("context_records") or []),
    )
    print(format_push_result_cn(
        "资金摘要推送",
        push.status,
        push.reason,
    ))
    summary_push_status = push.status

    announcement_push_status = "skipped"
    announcement_pushes: list[dict[str, str]] = []
    if not args.no_announcements:
        announcement_push_status, announcement_pushes = deliver_announcement_risk(
            engine,
            gateway,
            result["announcements"],
            args,
        )

    launch_pushes: list[dict[str, str]] = []
    if not args.no_launch:
        launch_pushes, package_cleanup = push_launch_messages(
            settings,
            engine,
            gateway,
            result["launch"],
            args,
        )
        result["diagnostics"]["launch_package_cleanup"] = package_cleanup

    diagnostics = dict(result["diagnostics"])
    flow_push_status = "skipped"
    if not args.no_flow:
        flow_push_status, flow_diag = push_flow_radar(settings, gateway, args)
        diagnostics["flow"] = flow_diag
    funding_alert_push_status = "skipped"
    if not getattr(args, "no_funding_alert", False):
        funding_alert_push_status, funding_diag = push_funding_alert(settings, store, gateway, args)
        diagnostics["funding_alert"] = funding_diag
    if refresh_effectiveness:
        try:
            diagnostics["signal_effectiveness"] = refresh_signal_effectiveness(settings)
        except (OSError, sqlite3.Error, ValueError) as exc:
            diagnostics["signal_effectiveness"] = {"status": "failed", "error": type(exc).__name__}
    else:
        diagnostics["signal_effectiveness"] = {
            "status": "skipped",
            "reason": "background_interval_gate",
        }

    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    runtime_details: dict[str, object] = {
        "task": runtime_task,
        "real_send": bool(args.send and args.confirm_real_send),
        "summary_push": summary_push_status,
        "summary_cycle_status": "ok",
        "summary_error_code": "",
        "radar_scan_limit": settings.radar_scan_limit,
        "launch_scan_limit": settings.launch_scan_limit,
        "flow_scan_limit": settings.flow_scan_limit,
        "funding_alert_scan_limit": settings.funding_alert_scan_limit,
        "last_error": "",
        "announcement_evidence": result.get("announcement_evidence", {}),
        "announcement_risk_push": announcement_push_status,
        "announcement_risk_pushes": announcement_pushes,
        "announcement_risk_cycle_status": (
            "ok"
            if result.get("announcements", {}).get("status") == "ok"
            else "failed"
        ),
        "announcement_risk_error_code": str(
            result.get("announcements", {}).get("error") or ""
        ),
        "diagnostics": diagnostics,
    }
    if not args.no_launch:
        runtime_details["launch_pushes"] = launch_pushes
        runtime_details["launch_cycle_status"] = "ok"
        runtime_details["launch_error_code"] = ""
    if not args.no_flow:
        runtime_details["flow_push"] = flow_push_status
        runtime_details["flow_cycle_status"] = "ok"
        runtime_details["flow_error_code"] = ""
    if not getattr(args, "no_funding_alert", False):
        runtime_details["funding_alert_push"] = (
            funding_alert_push_status
        )
        runtime_details["funding_alert_cycle_status"] = "ok"
        runtime_details["funding_alert_error_code"] = ""
    write_runtime_status(
        settings,
        store,
        mode,
        "running" if runtime_task == "loop" else "completed",
        **runtime_details,
    )
    return 0


def run_loop(args: argparse.Namespace) -> int:
    settings, store, _engine, _gateway = make_runtime_for_args(args)
    switches = effective_radar_switches(settings, args)
    runtime_flags = radar_runtime_flags(switches)
    mode = command_mode(args)
    summary_interval = max(
        60,
        int(args.interval if args.interval is not None else settings.radar_summary_min_interval_sec),
    )
    next_summary = next_closed_window_epoch(
        time.time(),
        interval_sec=summary_interval,
        delay_sec=settings.radar_summary_close_delay_sec,
    )
    announcement_interval = summary_interval
    next_announcement = next_summary
    next_launch = 0.0
    next_market_snapshot = 0.0
    next_signal_effectiveness = load_signal_effectiveness_next_run_at(
        settings,
        store,
        now=time.time(),
    )
    signal_effectiveness_diag: dict[str, object] = {"status": "pending"}
    next_flow = next_closed_window_epoch(
        time.time(),
        interval_sec=settings.flow_interval_sec,
        delay_sec=settings.flow_close_delay_sec,
    )
    next_funding_alert = time.time()
    heartbeat_interval_sec = max(
        5,
        min(60, settings.health_runtime_max_age_sec // 3),
    )
    next_heartbeat = time.time() + heartbeat_interval_sec
    write_runtime_status(
        settings,
        store,
        mode,
        "running",
        task="loop",
        real_send=bool(args.send and args.confirm_real_send),
        interval_sec=summary_interval,
        launch_interval_sec=max(60, args.launch_interval),
        flow_interval_sec=max(60, settings.flow_interval_sec),
        funding_alert_interval_sec=max(60, settings.funding_alert_interval_sec),
        summary_close_delay_sec=settings.radar_summary_close_delay_sec,
        flow_close_delay_sec=settings.flow_close_delay_sec,
        next_summary_at=timestamp_from_epoch(next_summary),
        next_announcement_at=timestamp_from_epoch(next_announcement),
        next_flow_at=timestamp_from_epoch(next_flow),
        next_funding_alert_at=timestamp_from_epoch(next_funding_alert),
        next_launch_at="",
        next_market_snapshot_at="",
        **runtime_flags,
        radar_scan_limit=settings.radar_scan_limit,
        launch_scan_limit=settings.launch_scan_limit,
        flow_scan_limit=settings.flow_scan_limit,
        funding_alert_scan_limit=settings.funding_alert_scan_limit,
        last_error="",
    )
    while True:
        now = time.time()
        settings, settings_reload_error = reload_loop_settings(settings, args)
        switches = effective_radar_switches(settings, args)
        runtime_flags = radar_runtime_flags(switches)
        cleanup_runtime_artifacts(settings, store)
        effectiveness_result, next_signal_effectiveness = (
            refresh_signal_effectiveness_if_due(
                settings,
                store,
                now=now,
                next_run_at=next_signal_effectiveness,
            )
        )
        if effectiveness_result is not None:
            signal_effectiveness_diag = effectiveness_result
        snapshot_due = now >= next_market_snapshot
        if snapshot_due and (
            not switches["launch_alert"] or now < next_launch
        ):
            market_snapshot_diag = refresh_shared_market_snapshot(settings)
            next_market_snapshot = time.time() + max(
                60,
                int(settings.market_snapshot_interval_sec),
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                next_market_snapshot_at=timestamp_from_epoch(
                    next_market_snapshot
                ),
                diagnostics={"market_snapshot": market_snapshot_diag},
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if not switches["radar_summary"] and now >= next_summary:
            next_summary = next_closed_window_epoch(
                time.time(),
                interval_sec=summary_interval,
                delay_sec=settings.radar_summary_close_delay_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                next_summary_at=timestamp_from_epoch(next_summary),
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if switches["radar_summary"] and now >= next_summary:
            summary_ok = True
            summary_error_code = ""
            summary_diag: dict[str, Any] = {}
            summary_push_status = "skipped"
            try:
                store, _engine, gateway = make_runtime_from_settings(settings)
                summary_push_status, summary_diag = push_market_summary(
                    settings,
                    store,
                    gateway,
                    args,
                )
                print(json.dumps(
                    {"radar_summary": summary_diag},
                    ensure_ascii=False,
                    indent=2,
                ))
            except Exception as exc:
                summary_ok = False
                summary_error_code = type(exc).__name__
                print(
                    f"[loop] summary failed: {summary_error_code}",
                    file=sys.stderr,
                )
            next_summary = next_closed_window_epoch(
                time.time(),
                interval_sec=summary_interval,
                delay_sec=settings.radar_summary_close_delay_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running" if summary_ok else "summary_failed",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                last_summary_at=timestamp_from_epoch(time.time()),
                next_summary_at=timestamp_from_epoch(next_summary),
                summary_cycle_status="ok" if summary_ok else "failed",
                summary_error_code=summary_error_code,
                summary_push=summary_push_status,
                diagnostics={"radar_summary": summary_diag},
                last_error="",
                settings_reload_error=settings_reload_error,
                **runtime_flags,
            )
        if not switches["announcement_risk"] and now >= next_announcement:
            next_announcement = next_closed_window_epoch(
                time.time(),
                interval_sec=announcement_interval,
                delay_sec=settings.radar_summary_close_delay_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                next_announcement_at=timestamp_from_epoch(next_announcement),
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if switches["announcement_risk"] and now >= next_announcement:
            announcement_ok = True
            announcement_error_code = ""
            announcement_diag: dict[str, Any] = {}
            announcement_push_status = "skipped"
            try:
                store, _engine, gateway = make_runtime_from_settings(settings)
                announcement_push_status, announcement_diag = push_announcement_risk(
                    settings,
                    store,
                    gateway,
                    args,
                )
                if announcement_diag.get("status") != "ok":
                    announcement_ok = False
                    announcement_error_code = str(
                        announcement_diag.get("error")
                        or "announcement_source_degraded"
                    )
                print(json.dumps(
                    {"announcement_risk": announcement_diag},
                    ensure_ascii=False,
                    indent=2,
                ))
            except Exception as exc:
                announcement_ok = False
                announcement_error_code = type(exc).__name__
                print(
                    f"[loop] announcement risk failed: {announcement_error_code}",
                    file=sys.stderr,
                )
            next_announcement = next_closed_window_epoch(
                time.time(),
                interval_sec=announcement_interval,
                delay_sec=settings.radar_summary_close_delay_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running" if announcement_ok else "announcement_risk_failed",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                last_announcement_at=timestamp_from_epoch(time.time()),
                next_announcement_at=timestamp_from_epoch(next_announcement),
                announcement_risk_push=announcement_push_status,
                announcement_risk_candidate_count=int(
                    announcement_diag.get("new_alerts") or 0
                ),
                announcement_risk_scanned_count=int(
                    announcement_diag.get("articles_scanned") or 0
                ),
                announcement_risk_cycle_status=(
                    "ok" if announcement_ok else "failed"
                ),
                announcement_risk_error_code=announcement_error_code,
                diagnostics={"announcement_risk": announcement_diag},
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if not switches["flow_radar"] and now >= next_flow:
            next_flow = next_closed_window_epoch(
                time.time(),
                interval_sec=settings.flow_interval_sec,
                delay_sec=settings.flow_close_delay_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                next_flow_at=timestamp_from_epoch(next_flow),
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if switches["flow_radar"] and now >= next_flow:
            flow_ok = True
            flow_error_code = ""
            flow_diag: dict[str, object] = {}
            flow_push_status = "skipped"
            try:
                _local_store, _engine, gateway = make_runtime_from_settings(
                    settings
                )
                flow_push_status, flow_diag = push_flow_radar(settings, gateway, args)
                print(json.dumps({"flow": flow_diag}, ensure_ascii=False, indent=2))
            except Exception as exc:
                flow_ok = False
                flow_error_code = type(exc).__name__
                print(
                    f"[loop] flow failed: {flow_error_code}",
                    file=sys.stderr,
                )
            next_flow = next_closed_window_epoch(
                time.time(),
                interval_sec=settings.flow_interval_sec,
                delay_sec=settings.flow_close_delay_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running" if flow_ok else "flow_failed",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                last_flow_at=timestamp_from_epoch(time.time()),
                next_flow_at=timestamp_from_epoch(next_flow),
                flow_push=flow_push_status,
                flow_cycle_status="ok" if flow_ok else "failed",
                flow_error_code=flow_error_code,
                diagnostics={"flow": flow_diag},
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if not switches["funding_alert"] and now >= next_funding_alert:
            next_funding_alert = time.time() + max(
                60,
                settings.funding_alert_interval_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                next_funding_alert_at=timestamp_from_epoch(
                    next_funding_alert
                ),
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if switches["funding_alert"] and now >= next_funding_alert:
            funding_ok = True
            funding_error_code = ""
            funding_diag: dict[str, object] = {}
            funding_push_status = "skipped"
            try:
                store, _engine, gateway = make_runtime_from_settings(settings)
                funding_push_status, funding_diag = push_funding_alert(settings, store, gateway, args)
                print(json.dumps({"funding_alert": funding_diag}, ensure_ascii=False, indent=2))
            except Exception as exc:
                funding_ok = False
                funding_error_code = type(exc).__name__
                print(
                    "[loop] funding alert failed: "
                    f"{funding_error_code}",
                    file=sys.stderr,
                )
            next_funding_alert = time.time() + max(60, settings.funding_alert_interval_sec)
            write_runtime_status(
                settings,
                store,
                mode,
                "running" if funding_ok else "funding_alert_failed",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                last_funding_alert_at=timestamp_from_epoch(time.time()),
                next_funding_alert_at=timestamp_from_epoch(next_funding_alert),
                funding_alert_push=funding_push_status,
                funding_alert_cycle_status=(
                    "ok" if funding_ok else "failed"
                ),
                funding_alert_error_code=funding_error_code,
                diagnostics={"funding_alert": funding_diag},
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if not switches["launch_alert"] and now >= next_launch:
            next_launch = time.time() + max(60, args.launch_interval)
            write_runtime_status(
                settings,
                store,
                mode,
                "running",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                next_launch_at=timestamp_from_epoch(next_launch),
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if switches["launch_alert"] and now >= next_launch:
            launch_ok = True
            launch_error_code = ""
            launch_pushes: list[dict[str, str]] = []
            launch_diag: dict[str, object] = {}
            source: BinanceDataSource | None = None
            try:
                _local_store, engine, gateway = make_runtime_from_settings(
                    settings
                )
                source = BinanceDataSource(settings)
                launch = engine.build_launch_alerts(source)
                launch_diag.update(launch_runtime_diagnostics(launch))
                launch_delete_callback = (
                    gateway.delete_messages_detailed
                    if args.send and args.confirm_real_send
                    else None
                )
                launch_diag["lifecycle_cleanup"] = engine.cleanup_failed_launch_messages(
                    launch_delete_callback
                )
                if snapshot_due:
                    launch_diag["market_snapshot"] = refresh_shared_market_snapshot(
                        settings,
                        source=source,
                    )
                    next_market_snapshot = time.time() + max(
                        60,
                        int(settings.market_snapshot_interval_sec),
                    )
                launch_diag["signal_effectiveness"] = dict(
                    signal_effectiveness_diag
                )
                launch_pushes, package_cleanup = push_launch_messages(
                    settings,
                    engine,
                    gateway,
                    launch,
                    args,
                )
                launch_diag["package_cleanup"] = package_cleanup
                launch_diag["binance"] = source.diagnostics()
                print(json.dumps({"launch": launch_diag}, ensure_ascii=False, indent=2))
            except Exception as exc:
                launch_ok = False
                launch_error_code = type(exc).__name__
                print(
                    f"[loop] launch failed: {launch_error_code}",
                    file=sys.stderr,
                )
            finally:
                if source is not None:
                    source.close()
            next_launch = time.time() + max(60, args.launch_interval)
            write_runtime_status(
                settings,
                store,
                mode,
                "running" if launch_ok else "launch_failed",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                last_launch_at=timestamp_from_epoch(time.time()),
                next_launch_at=timestamp_from_epoch(next_launch),
                next_market_snapshot_at=(
                    timestamp_from_epoch(next_market_snapshot)
                    if next_market_snapshot > 0
                    else ""
                ),
                launch_pushes=launch_pushes,
                launch_cycle_status="ok" if launch_ok else "failed",
                launch_error_code=launch_error_code,
                diagnostics={"launch": launch_diag},
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if time.time() >= next_heartbeat:
            write_runtime_status(
                settings,
                store,
                mode,
                "running",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                heartbeat_interval_sec=heartbeat_interval_sec,
                last_error="",
            )
            next_heartbeat = time.time() + heartbeat_interval_sec
        time.sleep(3)


def run_trial(
    args: argparse.Namespace,
    *,
    refresh_effectiveness: bool = True,
) -> int:
    cycles = max(1, args.cycles)
    wait_sec = max(30, args.launch_interval)
    settings, store, _engine, _gateway = make_runtime_for_args(args)
    mode = command_mode(args)
    write_runtime_status(
        settings,
        store,
        mode,
        "running",
        task="trial",
        cycle=0,
        cycles=cycles,
        real_send=bool(args.send and args.confirm_real_send),
        launch_scan_limit=settings.launch_scan_limit,
    )
    for cycle in range(1, cycles + 1):
        print(f"[trial] launch cycle {cycle}/{cycles}")
        settings, store, engine, gateway = make_runtime_for_args(args)
        source = BinanceDataSource(settings)
        launch = engine.build_launch_alerts(source)
        launch_delete_callback = (
            gateway.delete_messages_detailed
            if args.send and args.confirm_real_send
            else None
        )
        launch_cleanup = engine.cleanup_failed_launch_messages(launch_delete_callback)
        try:
            market_snapshot = persist_market_batch(settings, source=source)
        except Exception as exc:
            market_snapshot = {"status": "failed", "error": type(exc).__name__}
        if refresh_effectiveness and cycle == 1:
            try:
                signal_effectiveness = refresh_signal_effectiveness(settings)
            except (OSError, sqlite3.Error, ValueError) as exc:
                signal_effectiveness = {
                    "status": "failed",
                    "error": type(exc).__name__,
                }
        else:
            signal_effectiveness = {
                "status": "skipped",
                "reason": "first_cycle_only",
            }
        launch_pushes, package_cleanup = push_launch_messages(
            settings,
            engine,
            gateway,
            launch,
            args,
        )
        diagnostics = {
            **launch_runtime_diagnostics(launch),
            "binance": source.diagnostics(),
            "lifecycle_cleanup": launch_cleanup,
            "package_cleanup": package_cleanup,
            "market_snapshot": market_snapshot,
            "signal_effectiveness": signal_effectiveness,
        }
        source.close()
        print(json.dumps({
            "watchlist_count": launch.get("watchlist_count", 0),
            "diagnostics": diagnostics,
        }, ensure_ascii=False, indent=2))
        write_runtime_status(
            settings,
            store,
            mode,
            "running" if cycle < cycles else "completed",
            task="trial",
            cycle=cycle,
            cycles=cycles,
            watchlist_count=launch.get("watchlist_count", 0),
            launch_pushes=launch_pushes,
            diagnostics=diagnostics,
            real_send=bool(args.send and args.confirm_real_send),
            launch_scan_limit=settings.launch_scan_limit,
        )
        if cycle < cycles:
            time.sleep(wait_sec)
    return 0


def run_observe(args: argparse.Namespace) -> int:
    settings, store, _engine, _gateway = make_runtime_for_args(args)
    duration_sec = max(0, args.duration_minutes) * 60
    wait_sec = max(60, args.launch_interval)
    deadline = time.time() + duration_sec
    cycle = 0
    failures = 0
    last_error = ""
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if args.send or args.confirm_real_send:
        print("[observe] 强制 dry-run：已忽略 --send / --confirm-real-send")
    print(
        f"[observe] dry-run 启动观察开始 | 时长{max(0, args.duration_minutes)}分钟 | "
        f"启动间隔{wait_sec}秒 | 扫描上限{settings.launch_scan_limit}"
    )
    status = "running"
    write_runtime_status(
        settings,
        store,
        command_mode(args),
        status,
        task="observe",
        started_at=started_at,
        cycles=cycle,
        failures=failures,
        real_send=False,
        duration_minutes=max(0, args.duration_minutes),
        launch_scan_limit=settings.launch_scan_limit,
    )
    try:
        while True:
            cycle += 1
            print(f"[observe] launch cycle {cycle}")
            cycle_args = argparse.Namespace(**{
                **vars(args),
                "send": False,
                "confirm_real_send": False,
                "cycles": 1,
                "launch_interval": wait_sec,
            })
            try:
                run_trial(
                    cycle_args,
                    refresh_effectiveness=cycle == 1,
                )
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                failures += 1
                last_error = f"{type(exc).__name__}: {exc}"
                print(f"[observe] cycle failed: {last_error}", file=sys.stderr)
            report_path = save_observe_report(
                settings,
                store,
                args.records,
                args.top,
                started_at=started_at,
                cycles=cycle,
                failures=failures,
                status=status,
                last_error=last_error,
            )
            print(f"[observe] 中间报告已保存: {report_path}")
            write_runtime_status(
                settings,
                store,
                command_mode(args),
                status,
                task="observe",
                started_at=started_at,
                cycles=cycle,
                failures=failures,
                last_error=last_error,
                report_path=str(report_path),
                real_send=False,
                launch_scan_limit=settings.launch_scan_limit,
            )
            if duration_sec <= 0 or time.time() >= deadline:
                break
            sleep_for = min(wait_sec, max(0, deadline - time.time()))
            if sleep_for > 0:
                time.sleep(sleep_for)
    except KeyboardInterrupt:
        status = "interrupted"
        report_path = save_observe_report(
            settings,
            store,
            args.records,
            args.top,
            started_at=started_at,
            cycles=cycle,
            failures=failures,
            status=status,
            last_error=last_error,
        )
        print(f"[observe] 已中断，报告已保存: {report_path}")
        write_runtime_status(
            settings,
            store,
            command_mode(args),
            status,
            task="observe",
            started_at=started_at,
            cycles=cycle,
            failures=failures,
            last_error=last_error,
            report_path=str(report_path),
            real_send=False,
            launch_scan_limit=settings.launch_scan_limit,
        )
        return 130

    status = "completed"
    report_path = save_observe_report(
        settings,
        store,
        args.records,
        args.top,
        started_at=started_at,
        cycles=cycle,
        failures=failures,
        status=status,
        last_error=last_error,
    )
    print(format_observe_report(
        settings,
        store,
        args.records,
        args.top,
        started_at=started_at,
        cycles=cycle,
        failures=failures,
        status=status,
        last_error=last_error,
    ))
    print(f"[observe] 报告已保存: {report_path}")
    write_runtime_status(
        settings,
        store,
        command_mode(args),
        status,
        task="observe",
        started_at=started_at,
        cycles=cycle,
        failures=failures,
        last_error=last_error,
        report_path=str(report_path),
        real_send=False,
        launch_scan_limit=settings.launch_scan_limit,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    configure_console_encoding()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.altcoin_production and args.command != "market-stream":
        print(json.dumps({
            "status": "blocked",
            "reason": "altcoin_production_requires_market_stream",
        }, ensure_ascii=False))
        return 2
    if args.command == "stable-check":
        return print_stable_check(as_json=args.json, save=not args.no_save)
    if args.command == "altcoin-anomaly":
        from radars.altcoin_contract_anomaly.cli import run_altcoin_anomaly_cli

        return run_altcoin_anomaly_cli(args, settings=Settings.load())
    if args.command == "market-stream":
        settings = Settings.load()
        if bool(args.send) != bool(args.confirm_real_send):
            print(json.dumps({
                "service": "binance_realtime_market",
                "failures": ["telegram_real_send_dual_gate_required"],
            }, ensure_ascii=False))
            return 2
        duration_sec = max(
            0.0,
            float(args.stream_duration_minutes or 0),
        ) * 60
        if args.altcoin_production:
            from radars.altcoin_contract_anomaly.production_runtime import (
                run_altcoin_production_service,
            )

            return run_altcoin_production_service(
                settings,
                duration_sec=duration_sec,
                real_send_requested=bool(args.send and args.confirm_real_send),
            )

        if args.send or args.confirm_real_send:
            print(json.dumps({
                "service": "binance_realtime_market",
                "failures": ["telegram_send_requires_altcoin_production"],
            }, ensure_ascii=False))
            return 2
        from shared.process_lock import ProcessFileLock
        from shared.realtime_market import run_realtime_market_service

        return run_realtime_market_service(
            settings,
            duration_sec=duration_sec,
            process_lock=ProcessFileLock(
                settings.altcoin_contract_anomaly_realtime_lock_path
            ),
        )
    settings, store, _engine, _gateway = make_runtime()

    if args.command == "about":
        print(PROJECT_ABOUT)
        return 0
    if args.command == "private-control":
        return run_private_control(settings, store)
    if args.command == "cleanup":
        print_cleanup(settings, store, force=args.force_cleanup)
        return 0
    cleanup_runtime_artifacts(settings, store)

    if args.command == "status":
        print_status(settings, store)
        return 0
    if args.command == "doctor":
        print_doctor(settings, store)
        return 0
    if args.command == "readiness":
        return print_readiness(settings, store)
    if args.command == "database-backup":
        return print_database_backup(settings)
    if args.command == "telegram-test":
        return run_telegram_test(args)
    if args.command == "telegram-topic-setup":
        return run_telegram_topic_setup(args)
    if args.command == "announcement-risk":
        if args.send and args.confirm_real_send:
            gate = require_real_send_gate(settings, store, args)
            if gate != 0:
                return gate
        return run_announcement_risk(args)
    if args.command == "flow-radar":
        if args.send and args.confirm_real_send:
            gate = require_real_send_gate(settings, store, args)
            if gate != 0:
                return gate
        return run_flow_radar(args)
    if args.command == "funding-alert":
        if args.send and args.confirm_real_send:
            gate = require_real_send_gate(settings, store, args)
            if gate != 0:
                return gate
        return run_funding_alert(args)
    if args.command == "runtime-status":
        print_runtime_status(settings, store)
        return 0
    if args.command == "radar-status":
        print_radar_status(settings, store)
        return 0
    if args.command == "watchlist":
        print_watchlist(settings, store, args.top)
        return 0
    if args.command == "launch-history":
        print_launch_history(settings, store, args.top)
        return 0
    if args.command == "launch-report":
        print_launch_report(settings, store, args.records, args.top)
        return 0
    if args.command == "signal-repair":
        report = SignalEventStore(settings.signal_events_db_path).repair_legacy_signals(apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("status") == "ok" or args.apply else 1
    if args.command == "signal-effectiveness":
        print_signal_effectiveness(settings)
        return 0
    if args.command == "once":
        if args.send and args.confirm_real_send:
            gate = require_real_send_gate(settings, store, args)
            if gate != 0:
                return gate
        return run_once(args)
    if args.command == "trial":
        return run_trial(args)
    if args.command == "observe":
        return run_observe(args)
    if args.command == "live":
        if args.send and args.confirm_real_send:
            bootstrap = bootstrap_live_market_snapshot(settings, store)
            print(json.dumps({"live_readiness_bootstrap": bootstrap}, ensure_ascii=False))
        gate = require_real_send_gate(settings, store, args)
        if gate != 0:
            return gate
        return run_loop(args)
    if args.command in {"loop", "daemon"}:
        if args.send and args.confirm_real_send:
            gate = require_real_send_gate(settings, store, args)
            if gate != 0:
                return gate
        return run_loop(args)
    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
