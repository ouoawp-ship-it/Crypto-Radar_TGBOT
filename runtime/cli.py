from __future__ import annotations

"""
泡泡抓币：精简版加密监控工具。

核心功能：
- 公告风险：低频解析 Binance 官方上新/下架等事件并独立推送。
- 费率/OI 异动扫描：负费率、资金费率趋势、持仓变化、价格变化、成交量变化。
- 热度做多雷达：按涨幅、成交量、OI、资金费率筛选短线动量。
- 庄家收筹/埋伏池：低市值、横盘、OI 暗流、负费率燃料的综合评分。
- 脉冲雷达：15分钟价格、OI、CVD 六分类异动提醒。
- 2小时背离：识别建仓、回调压力、突破、恐慌、共振和极端背离。

默认推送周期：
- 资金雷达汇总：6 小时一次，每天最多 4 次；收线后延迟抓上一完整窗口。
- 脉冲异动提醒：每个完整15分钟窗口运行一次；背离分析每2小时运行一次。
- 公告风险：独立低频运行，不参与脉冲雷达分类。
- 同币脉冲事件：2小时内只在升级或反转时更新，最多3次。
"""

import argparse
import json
import math
import re
import sqlite3
import sys
import time
from pathlib import Path
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import datetime
from typing import Any

from config import Settings
from .database_backup import backup_databases
from shared.binance_data import BinanceDataSource, UPSTREAM_SOURCE_METRICS
from radars.capital_flow.radar import FlowRadarEngine
from radars.consolidation_breakout.radar import ConsolidationBreakoutRadar
from radars.consolidation_breakout.hourly_proximity import (
    ConsolidationHourlyProximityRadar,
)
from radars.consolidation_breakout.daily_digest import (
    CANDIDATE_GATE_VERSION,
    ConsolidationDailyDigestAccumulator,
    empty_daily_digest_state,
    select_digest_signal_structures,
)
from radars.consolidation_breakout.chart import (
    PNG_SIGNATURE as CONSOLIDATION_CHART_PNG_SIGNATURE,
    render_consolidation_chart_png,
)
from radars.announcement_risk.radar import AnnouncementRiskRadar
from .health import lightweight_freshness_checks, runtime_health_checks
from shared.market_cockpit import persist_flow_market_rows, persist_market_batch
from radars.funding_alert.radar import FundingAlertEngine
from .maintenance import cleanup_runtime_artifacts
from .cli_text import check_name_text, format_push_result_cn
from radars.market_summary.radar import MarketSummaryRadar
from radars.pulse.divergence import DivergenceConfig
from radars.pulse.review_store import build_review_report, format_review_report
from radars.pulse.simple_alert import (
    PULSE_CHART_KLINE_RESERVE,
    SimpleAlertConfig,
)
from .radar_engine import RadarEngine
from .diagnostics import build_market_radar_runtime_status
from .signal_effectiveness import SignalOutcomeTracker
from shared.signal_store import SignalEventStore
from shared.storage import JsonStore
from shared.telegram import (
    PRODUCTION_TOPIC_TEMPLATE_IDS,
    TOPIC_TEMPLATE_NAMES,
    TelegramGateway,
    plain_fallback,
)
from shared.time_windows import next_closed_window_epoch


PROJECT_ABOUT = """泡泡抓币：精简版加密监控工具

保留功能：
- 公告风险：独立提醒 Binance 官方上新/下架事件。
- 费率/OI 异动扫描：资金费率、持仓、价格、成交量、数据质量。
- 热度做多雷达：涨幅、成交量、OI、资金费率综合筛选短线动量。
- 庄家收筹/埋伏池：低市值、横盘、OI 暗流、负费率燃料综合评分。
- 脉冲雷达：15分钟价格、OI、CVD 六分类异动提醒。
- 2小时背离：建仓、回调压力、强势突破、恐慌、多头共振和极端背离。

推送内容：
- 资金雷达汇总：负费率榜、综合榜、埋伏榜、动量池、新币池、值得关注、图例、数据质量。
- 脉冲提醒：币种、价格与OI多窗口变化、量能、CVD、多空比和组合结论。
- 背离汇总：每2小时按持仓变化、价格变化和背离度分类。
- Telegram 测试消息：只在手动执行 telegram-test --send --confirm-real-send 时发送。

默认周期：
- 资金雷达汇总：6 小时一次，每天最多 4 次；可用 --interval 或 RADAR_SUMMARY_MIN_INTERVAL_SEC 调整。
- 脉冲雷达：每15分钟完整收线后运行；2小时背离按完整窗口运行。
- 同币事件默认跟随2小时，只在升级或反转时更新，最多3次。
- 自动清理：1 小时检查一次，可用 CLEANUP_INTERVAL_SEC 调整。

安全规则：
- 默认 dry-run，不真实推送 Telegram。
- 真实推送必须同时提供 --send --confirm-real-send。
- live/真实 loop 会先经过 readiness 门禁。
"""

PLACEHOLDER_WORDS = ("your", "token", "chat_id", "bot_token", "填写", "填入", "请输入", "xxx", "example")




def run_pulse_cycle(
    engine: RadarEngine,
    gateway: TelegramGateway,
    args: argparse.Namespace,
    *,
    include_simple: bool = True,
    include_divergence: bool = True,
    maintain_reviews: bool = True,
) -> dict[str, object]:
    diagnostics: dict[str, object] = {}
    failures: list[str] = []
    scan_limit = getattr(args, "pulse_scan_limit", None)

    if include_simple:
        try:
            simple = engine.run_simple_pulse(
                gateway,
                send=args.send,
                confirm_real_send=args.confirm_real_send,
                scan_limit=scan_limit,
            )
            diagnostics["simple"] = simple
            if any(
                str(push.get("status") or "") in {"failed", "partial"}
                for push in simple.get("pushes", [])
                if isinstance(push, dict)
            ):
                failures.append("simple_delivery")
        except Exception as exc:
            failures.append("simple")
            diagnostics["simple"] = {
                "status": "failed",
                "error": type(exc).__name__,
            }

    if include_divergence:
        try:
            divergence = engine.run_divergence_pulse(
                gateway,
                send=args.send,
                confirm_real_send=args.confirm_real_send,
                scan_limit=scan_limit,
            )
            diagnostics["divergence"] = divergence
            push = divergence.get("push") or {}
            if isinstance(push, dict) and push.get("status") in {
                "failed",
                "partial",
            }:
                failures.append("divergence_delivery")
        except Exception as exc:
            failures.append("divergence")
            diagnostics["divergence"] = {
                "status": "failed",
                "error": type(exc).__name__,
            }

    if maintain_reviews:
        review = engine.maintain_pulse_reviews(
            gateway,
            send=args.send,
            confirm_real_send=args.confirm_real_send,
        )
        diagnostics["review"] = review
        if review.get("status") == "degraded":
            failures.append("review")

    diagnostics["status"] = "degraded" if failures else "ok"
    diagnostics["failed_components"] = failures
    return diagnostics




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
    if bool(getattr(settings, "consolidation_breakout_enable", False)):
        template_id = "TG_CONSOLIDATION_BREAKOUT"
        configured = gateway.topic_route_configured(template_id)
        topic_name = TOPIC_TEMPLATE_NAMES[template_id]
        checks.append((
            "telegram_topic_consolidation_breakout",
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
        choices=["about", "status", "doctor", "readiness", "stable-check", "database-backup", "signal-repair", "signal-effectiveness", "pulse-review-report", "telegram-test", "telegram-topic-setup", "telegram-topic-refresh", "private-control", "announcement-risk", "flow-radar", "funding-alert", "consolidation-breakout", "altcoin-anomaly", "pulse", "market-stream", "runtime-status", "radar-status", "cleanup", "once", "loop", "daemon", "live"],
        help="默认 status；doctor 检查环境；database-backup 创建并恢复验证 SQLite 备份；signal-effectiveness 回填信号结果",
    )
    parser.add_argument("--send", action="store_true", help="允许真实发送 Telegram；仍需要 --confirm-real-send")
    parser.add_argument("--confirm-real-send", action="store_true", help="确认真实发送 Telegram")
    parser.add_argument(
        "--topic-template",
        choices=sorted(TOPIC_TEMPLATE_NAMES),
        default=None,
        help="用于 telegram-topic-setup/refresh：选择要创建、修复或刷新的话题",
    )
    parser.add_argument("--apply", action="store_true", help="用于 signal-repair：应用修复（默认仅审计）")
    parser.add_argument("--force-cleanup", action="store_true", help="用于 cleanup：忽略清理间隔，立即执行")
    parser.add_argument("--stream-duration-minutes", type=float, default=0, help="用于 market-stream 本地验收；0 表示常驻运行")
    parser.add_argument(
        "--altcoin-production",
        action="store_true",
        help="仅用于 market-stream：显式启用山寨合约异动生产控制器",
    )
    parser.add_argument("--interval", default=None, help="loop/daemon 的资金雷达摘要间隔秒数")
    parser.add_argument("--radar-scan-limit", type=int, default=None, help="临时覆盖资金雷达扫描上限")
    parser.add_argument(
        "--pulse-scan-limit",
        dest="pulse_scan_limit",
        type=int,
        default=None,
        help="临时覆盖脉冲雷达扫描上限；0表示全部合格加密合约",
    )
    parser.add_argument("--review-days", type=int, default=7, help="用于 pulse-review-report：统计最近天数，默认 7 天")
    parser.add_argument("--review-top", type=int, default=5, help="用于 pulse-review-report：本周涨幅榜数量，默认 5")
    parser.add_argument("--flow-scan-limit", type=int, default=None, help="临时覆盖五因子资金流雷达扫描上限")
    parser.add_argument("--funding-scan-limit", type=int, default=None, help="临时覆盖资金费率警报扫描上限")
    parser.add_argument("--consolidation-scan-limit", type=int, default=None, help="临时覆盖盘整突破雷达每批扫描数量")
    parser.add_argument("--no-pulse", dest="no_launch", action="store_true", help="本轮不运行脉冲雷达")
    parser.add_argument("--no-announcements", action="store_true", help="本轮不运行公告风险雷达")
    parser.add_argument("--no-flow", action="store_true", help="本轮不运行五因子资金流雷达")
    parser.add_argument("--no-funding-alert", action="store_true", help="本轮不运行资金费率警报")
    parser.add_argument("--no-consolidation-breakout", action="store_true", help="本轮不运行盘整突破雷达")
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
    pulse_scan_limit = getattr(args, "pulse_scan_limit", None)
    flow_scan_limit = getattr(args, "flow_scan_limit", None)
    funding_scan_limit = getattr(args, "funding_scan_limit", None)
    consolidation_scan_limit = getattr(
        args,
        "consolidation_scan_limit",
        None,
    )
    if radar_scan_limit is not None:
        updates["radar_scan_limit"] = max(0, int(radar_scan_limit))
    if pulse_scan_limit is not None:
        updates["pulse_simple_scan_limit"] = max(0, int(pulse_scan_limit))
        updates["pulse_divergence_scan_limit"] = max(1, int(pulse_scan_limit))
    if flow_scan_limit is not None:
        updates["flow_scan_limit"] = max(0, int(flow_scan_limit))
    if funding_scan_limit is not None:
        updates["funding_alert_scan_limit"] = max(0, int(funding_scan_limit))
    if consolidation_scan_limit is not None:
        updates["consolidation_breakout_scan_limit"] = max(
            1,
            int(consolidation_scan_limit),
        )
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
            settings.pulse_radar_enable and not bool(args.no_launch)
        ),
        "radar_summary": bool(settings.radar_summary_enable),
        "funding_alert": bool(
            settings.funding_alert_enable
            and not bool(getattr(args, "no_funding_alert", False))
        ),
        "flow_radar": bool(
            settings.flow_radar_enable and not bool(args.no_flow)
        ),
        "consolidation_breakout": bool(
            settings.consolidation_breakout_enable
            and not bool(
                getattr(args, "no_consolidation_breakout", False)
            )
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
        "no_consolidation_breakout": not switches[
            "consolidation_breakout"
        ],
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
        settings.consolidation_breakout_state_path,
        settings.consolidation_hourly_proximity_state_path,
        settings.announcement_state_path,
        settings.data_dir / "simple_alert_state.json",
        settings.data_dir / "review_signals.json",
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
    pulse_status_update = bool(
        "pulse_cycle_status" in details
        or isinstance(incoming_diagnostics, dict)
        and "pulse" in incoming_diagnostics
    )
    if pulse_status_update:
        for legacy_key in (
            "launch_pushes",
            "launch_scan_limit",
            "launch_cycle_status",
            "launch_error_code",
            "launch_interval_sec",
        ):
            payload.pop(legacy_key, None)
    if merge_loop and isinstance(previous_diagnostics, dict) and isinstance(
        incoming_diagnostics, dict
    ):
        merged_diagnostics = dict(previous_diagnostics)
        merged_diagnostics.update(incoming_diagnostics)
        if pulse_status_update:
            merged_diagnostics.pop("launch", None)
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
                "consolidation_breakout": "TG_CONSOLIDATION_BREAKOUT",
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


def run_telegram_topic_refresh(args: argparse.Namespace) -> int:
    settings, store, _engine, _gateway = make_runtime_for_args(args)
    gateway = TelegramGateway(settings, store)
    result = gateway.refresh_topic_intro(
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
    real_send = bool(args.send and args.confirm_real_send)
    with BinanceDataSource(settings) as source:
        flow = FlowRadarEngine(settings).build(
            source,
            persist_candidate_state=real_send,
        )
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
    real_send = bool(args.send and args.confirm_real_send)
    with BinanceDataSource(settings) as source:
        flow = FlowRadarEngine(settings).build(
            source,
            persist_candidate_state=real_send,
        )
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


def run_consolidation_breakout(args: argparse.Namespace) -> int:
    settings, _store, _engine, _gateway = make_runtime_for_args(args)
    # An explicit one-shot command is an operator-requested scan.  The feature
    # switch controls only automatic scheduling, so dry-run validation remains
    # possible before enabling the daemon path.
    if not settings.consolidation_breakout_enable:
        settings = replace(settings, consolidation_breakout_enable=True)
    store = JsonStore(settings.data_dir)
    gateway = TelegramGateway(settings, store)
    push_status, diagnostics = push_consolidation_breakout(
        settings,
        store,
        gateway,
        args,
    )
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    hourly_error = _consolidation_hourly_proximity_error_code(diagnostics)
    return 1 if push_status in {"failed", "partial"} or hourly_error else 0


def _consolidation_chart_photo(
    event: Mapping[str, Any],
    chart_payload: object,
) -> tuple[bytes | None, str]:
    caption = str(event.get("text") or "")
    if len(plain_fallback(caption)) > 1024:
        return None, "caption_too_long"
    if not isinstance(chart_payload, Mapping):
        return None, "payload_unavailable"
    try:
        photo = render_consolidation_chart_png(
            event=event,
            chart_payload=chart_payload,
        )
    except Exception:  # Presentation failure must not stop market scanning.
        return None, "render_failed"
    if not isinstance(photo, bytes) or not photo.startswith(
        CONSOLIDATION_CHART_PNG_SIGNATURE
    ):
        return None, "invalid_png"
    if len(photo) > 10 * 1024 * 1024:
        return None, "photo_too_large"
    return photo, "ready"


def _process_consolidation_daily_digest(
    settings: Settings,
    store: JsonStore,
    gateway: TelegramGateway,
    args: argparse.Namespace,
    result: Mapping[str, Any],
) -> tuple[str | None, dict[str, object]]:
    enabled = bool(
        getattr(settings, "consolidation_daily_product_enable", False)
        and getattr(settings, "consolidation_daily_digest_enable", False)
    )
    shadow_mode = bool(
        getattr(settings, "consolidation_daily_shadow_mode", True)
    )
    diagnostics: dict[str, object] = {
        "status": "disabled" if not enabled else "idle",
        "enabled": enabled,
        "shadow_mode": shadow_mode,
        "batch_status": "unavailable",
        "pending_count": 0,
    }
    if not enabled:
        return None, diagnostics

    now_ts = int(time.time())
    state_path = Path(getattr(
        settings,
        "consolidation_daily_digest_state_path",
        settings.data_dir / "consolidation_daily_digest_state.json",
    ))
    try:
        state = store.load(state_path, empty_daily_digest_state())
        raw_pending = (
            state.get("pending_digests", [])
            if isinstance(state, Mapping)
            else []
        )
        legacy_pending_count = sum(
            1
            for item in raw_pending
            if isinstance(item, Mapping)
            and str(item.get("candidate_gate_version") or "")
            != CANDIDATE_GATE_VERSION
        ) if isinstance(raw_pending, list) else 0
        accumulator = ConsolidationDailyDigestAccumulator(
            state if isinstance(state, Mapping) else None,
            max_items=int(getattr(
                settings,
                "consolidation_daily_digest_max_items",
                20,
            )),
            max_retry_rounds=int(getattr(
                settings,
                "consolidation_daily_retry_rounds",
                2,
            )),
            max_wait_sec=int(getattr(
                settings,
                "consolidation_daily_max_wait_sec",
                3 * 3600,
            )),
            text_limit=max(1, min(
                4096,
                int(getattr(settings, "tg_push_split_limit", 3800)),
            )),
            migration_now_ts=now_ts,
        )
        diagnostics["candidate_gate_version"] = CANDIDATE_GATE_VERSION
        diagnostics["invalidated_pending_count"] = legacy_pending_count
        if legacy_pending_count:
            diagnostics["invalidation_reason"] = (
                "candidate_universe_tightened"
            )
            store.save(state_path, accumulator.snapshot())
        raw_batch = result.get("daily_digest_batch")
        batch = raw_batch if isinstance(raw_batch, Mapping) else {}
        target_close_time = int(batch.get("target_close_time") or 0)
        raw_expected = batch.get("expected_symbols")
        expected_symbols = (
            list(raw_expected)
            if isinstance(raw_expected, (list, tuple, set))
            else []
        )
        raw_observations = batch.get("observations")
        observations = (
            [item for item in raw_observations if isinstance(item, Mapping)]
            if isinstance(raw_observations, (list, tuple))
            else []
        )
        batch_ingested = False
        if target_close_time > 0 and expected_symbols:
            reconciliation = accumulator.reconcile_symbols(
                expected_symbols,
                now_ts=now_ts,
            )
            diagnostics["candidate_reconciliation"] = reconciliation
            # Make a legacy-active migration durable before ingestion or any
            # later Telegram request can fail the process.
            store.save(state_path, accumulator.snapshot())
            try:
                accumulator.ingest_batch(
                    target_close_time=target_close_time,
                    expected_symbols=expected_symbols,
                    observations=observations,
                    now_ts=now_ts,
                    round_completed=bool(batch.get("round_completed")),
                    round_token=str(
                        batch.get("round_token")
                        or batch.get("rotation_round")
                        or ""
                    ),
                )
            except (TypeError, ValueError):
                diagnostics["batch_status"] = "invalid"
            else:
                batch_ingested = True
                diagnostics["batch_status"] = "ingested"
                diagnostics["target_close_time"] = target_close_time

        pending = accumulator.pending_digest(now_ts=now_ts)
        if batch_ingested:
            # Persist the complete accumulator and any newly frozen pending
            # digest before a Telegram request can leave the process.
            store.save(state_path, accumulator.snapshot())
        current_snapshot = accumulator.snapshot()
        pending_items = current_snapshot.get("pending_digests", [])
        pending_items = pending_items if isinstance(pending_items, list) else []
        diagnostics["pending_count"] = len(pending_items)
        recent_snapshots = current_snapshot.get("recent_snapshots", [])
        diagnostics["snapshot_count"] = len(
            recent_snapshots if isinstance(recent_snapshots, list) else []
        )

        if (
            not batch_ingested
            and legacy_pending_count > 0
            and not pending_items
        ):
            diagnostics["status"] = "candidate_universe_tightened"
            return None, diagnostics

        if shadow_mode:
            diagnostics["status"] = (
                "shadow_accumulating" if batch_ingested else "shadow_idle"
            )
            return None, diagnostics
        if pending is None:
            if pending_items:
                delivery = pending_items[-1].get("delivery")
                delivery = delivery if isinstance(delivery, Mapping) else {}
                diagnostics["status"] = "retry_backoff"
                diagnostics["next_attempt_at"] = int(
                    delivery.get("next_attempt_at") or 0
                )
                return None, diagnostics
            diagnostics["status"] = (
                "accumulating" if batch_ingested else "batch_unavailable"
            )
            return None, diagnostics

        # A pending digest loaded from a previous run is already durable, but
        # save it once more so every delivery path has the same ordering rule.
        store.save(state_path, accumulator.snapshot())
        digest_id = str(pending.get("digest_id") or "")
        signal_records: list[dict[str, Any]] = []
        for item in select_digest_signal_structures(
            pending,
            max_items=int(getattr(
                settings,
                "consolidation_daily_digest_max_items",
                20,
            )),
        ):
            signal_records.append({
                **dict(item),
                "event": "daily_consolidation_digest",
                "event_time": int(pending.get("target_close_time") or 0),
                "timeframe": "1d",
            })
        push = gateway.send(
            str(pending.get("text") or ""),
            str(result.get("template_id") or "TG_CONSOLIDATION_BREAKOUT"),
            str(pending.get("dedup_key") or digest_id),
            send=args.send,
            confirm_real_send=args.confirm_real_send,
            cooldown_sec=7 * 86400,
            parse_mode="HTML",
            signal_records=signal_records or None,
            photo=None,
            enrich_market_context=False,
        )
        accepted = accumulator.mark_delivery(
            digest_id,
            status=push.status,
            reason=push.reason,
            now_ts=now_ts,
        )
        post_delivery_snapshot = accumulator.snapshot()
        store.save(state_path, post_delivery_snapshot)
        post_delivery_archives = post_delivery_snapshot.get(
            "recent_snapshots",
            [],
        )
        diagnostics.update({
            "status": "delivered" if accepted else "pending_retained",
            "pending_count": len(
                post_delivery_snapshot.get("pending_digests", [])
            ),
            "snapshot_count": len(
                post_delivery_archives
                if isinstance(post_delivery_archives, list)
                else []
            ),
            "delivery": {
                "status": push.status,
                "reason": push.reason,
                "accepted": accepted,
            },
        })
        return push.status, diagnostics
    except Exception as exc:
        diagnostics.update({
            "status": "failed",
            "error_code": type(exc).__name__,
        })
        return "failed", diagnostics


def _consolidation_event_key(
    event: Mapping[str, Any],
) -> tuple[str, int] | None:
    symbol = str(event.get("symbol") or "").strip().upper()
    try:
        close_time = int(
            event.get("close_time") or event.get("event_time") or 0
        )
    except (TypeError, ValueError, OverflowError):
        close_time = 0
    if not symbol or close_time <= 0:
        return None
    return symbol, close_time


def _consolidation_hourly_proximity_error_code(
    diagnostics: Mapping[str, object],
) -> str:
    raw = diagnostics.get("hourly_proximity")
    hourly = raw if isinstance(raw, Mapping) else {}
    status = str(hourly.get("status") or "").strip().lower()
    if status not in {
        "scan_failed",
        "shadow_commit_failed",
        "commit_failed",
    }:
        return ""
    return f"hourly_proximity_{status}"


def _process_consolidation_hourly_proximity(
    settings: Settings,
    store: JsonStore,
    gateway: TelegramGateway,
    args: argparse.Namespace,
    base_events: object,
    base_delivery_pending: bool,
    base_events_withheld: bool,
) -> tuple[str | None, dict[str, object]]:
    enabled = bool(getattr(
        settings,
        "consolidation_hourly_proximity_enable",
        False,
    ))
    shadow_mode = bool(getattr(
        settings,
        "consolidation_hourly_proximity_shadow_mode",
        True,
    ))
    diagnostics: dict[str, object] = {
        "status": "disabled" if not enabled else "idle",
        "enabled": enabled,
        "shadow_mode": shadow_mode,
        "events": 0,
        "accepted": 0,
        "suppressed_by_structure": 0,
        "pushes": [],
    }
    if not enabled:
        return None, diagnostics

    try:
        radar = ConsolidationHourlyProximityRadar(settings, store)
        with BinanceDataSource(
            settings,
            kline_budget=int(getattr(
                settings,
                "consolidation_hourly_proximity_kline_budget",
                60,
            )),
        ) as source:
            result = radar.build(source)
    except Exception as exc:
        diagnostics.update({
            "status": "scan_failed",
            "error_code": type(exc).__name__,
        })
        return None, diagnostics

    raw_scan_diagnostics = result.get("diagnostics")
    if isinstance(raw_scan_diagnostics, Mapping):
        diagnostics["scan"] = dict(raw_scan_diagnostics)
    raw_events = result.get("events")
    events = (
        [event for event in raw_events if isinstance(event, Mapping)]
        if isinstance(raw_events, (list, tuple))
        else []
    )
    diagnostics["events"] = len(events)

    if shadow_mode:
        accepted_event_ids = {
            str(event.get("event_id") or "")
            for event in events
            if str(event.get("event_id") or "")
        }
        try:
            committed = radar.commit(result, accepted_event_ids)
        except Exception as exc:
            diagnostics.update({
                "status": "shadow_commit_failed",
                "error_code": type(exc).__name__,
                "accepted": len(accepted_event_ids),
            })
            return "failed", diagnostics
        else:
            diagnostics.update({
                "status": (
                    "shadow_observed" if events else "shadow_idle"
                ),
                "accepted": len(accepted_event_ids),
                "state_updates_committed": committed,
            })
        return None, diagnostics

    base_event_list = [
        event
        for event in (
            base_events if isinstance(base_events, (list, tuple)) else []
        )
        if isinstance(event, Mapping)
    ]
    base_event_keys = {
        key
        for event in base_event_list
        for key in [_consolidation_event_key(event)]
        if key is not None
    }
    configured_base_max = max(0, int(getattr(
        settings,
        "consolidation_breakout_max_signals_per_scan",
        8,
    )))
    base_capacity_saturated = bool(base_events_withheld) or (
        configured_base_max > 0
        and len(base_event_list) >= configured_base_max
    )
    diagnostics["base_events_withheld"] = bool(base_events_withheld)
    diagnostics["base_capacity_saturated"] = base_capacity_saturated
    raw_chart_payloads = result.get("chart_payloads")
    chart_payloads = (
        raw_chart_payloads
        if isinstance(raw_chart_payloads, Mapping)
        else {}
    )
    accepted_event_ids: set[str] = set()
    push_results: list[dict[str, object]] = []
    for index, event in enumerate(events, start=1):
        event_id = str(event.get("event_id") or "")
        event_key = _consolidation_event_key(event)
        if base_delivery_pending:
            push_results.append({
                "event_id": event_id,
                "status": "deferred",
                "reason": "base_structure_delivery_pending",
                "chart_status": "not_rendered",
            })
            continue
        if base_capacity_saturated:
            push_results.append({
                "event_id": event_id,
                "status": "deferred",
                "reason": "base_structure_capacity_saturated",
                "chart_status": "not_rendered",
            })
            continue
        if event_key is not None and event_key in base_event_keys:
            if event_id:
                accepted_event_ids.add(event_id)
            push_results.append({
                "event_id": event_id,
                "status": "suppressed",
                "reason": "base_structure_same_close",
                "chart_status": "not_rendered",
            })
            continue

        photo, chart_status = _consolidation_chart_photo(
            event,
            chart_payloads.get(event_id),
        )
        try:
            push = gateway.send(
                str(event.get("text") or ""),
                str(
                    result.get("template_id")
                    or "TG_CONSOLIDATION_BREAKOUT"
                ),
                str(event.get("dedup_key") or event_id),
                send=args.send,
                confirm_real_send=args.confirm_real_send,
                cooldown_sec=7 * 86400,
                parse_mode="HTML",
                signal_records=[event],
                photo=photo,
                enrich_market_context=False,
            )
        except Exception as exc:
            push_results.append({
                "event_id": event_id,
                "status": "failed",
                "reason": type(exc).__name__,
                "chart_status": chart_status,
            })
            continue
        print(format_push_result_cn(
            "1H 箱体临界预警推送",
            push.status,
            push.reason,
            index=index,
        ))
        push_results.append({
            "event_id": event_id,
            "status": push.status,
            "reason": push.reason,
            "chart_status": chart_status,
        })
        if event_id and (
            push.status == "sent"
            or (
                push.status == "skipped"
                and push.reason in {"dedup_cooldown", "exact_duplicate"}
            )
        ):
            accepted_event_ids.add(event_id)

    commit_failed = False
    try:
        committed = radar.commit(result, accepted_event_ids)
    except Exception as exc:
        commit_failed = True
        diagnostics.update({
            "status": "commit_failed",
            "error_code": type(exc).__name__,
        })
    else:
        diagnostics["state_updates_committed"] = committed

    delivery_statuses = {
        str(item.get("status") or "")
        for item in push_results
        if item.get("status") != "suppressed"
    }
    if delivery_statuses & {"failed", "partial"}:
        delivery_status = "failed"
    elif "blocked" in delivery_statuses:
        delivery_status = "blocked"
    elif "sent" in delivery_statuses:
        delivery_status = "sent"
    elif "dry_run" in delivery_statuses:
        delivery_status = "dry_run"
    else:
        delivery_status = "skipped"
    diagnostics.update({
        "status": (
            diagnostics.get("status")
            if diagnostics.get("status") == "commit_failed"
            else "live"
        ),
        "delivery_status": delivery_status,
        "accepted": len(accepted_event_ids),
        "suppressed_by_structure": sum(
            item.get("status") == "suppressed" for item in push_results
        ),
        "deferred_by_structure_capacity": sum(
            item.get("reason") == "base_structure_capacity_saturated"
            for item in push_results
        ),
        "deferred_by_structure_delivery": sum(
            item.get("reason") == "base_structure_delivery_pending"
            for item in push_results
        ),
        "charts_ready": sum(
            item.get("chart_status") == "ready" for item in push_results
        ),
        "charts_delivered": sum(
            item.get("chart_status") == "ready"
            and item.get("status") == "sent"
            for item in push_results
        ),
        "charts_text_fallback": sum(
            item.get("status") not in {"suppressed", "deferred"}
            and item.get("chart_status") != "ready"
            for item in push_results
        ),
        "pushes": push_results,
    })
    return "failed" if commit_failed else delivery_status, diagnostics


def push_consolidation_breakout(
    settings: Settings,
    store: JsonStore,
    gateway: TelegramGateway,
    args: argparse.Namespace,
) -> tuple[str, dict[str, object]]:
    radar = ConsolidationBreakoutRadar(settings, store)
    with BinanceDataSource(settings) as source:
        result = radar.build(source)

    accepted_event_ids: set[str] = set()
    push_results: list[dict[str, object]] = []
    raw_chart_payloads = result.get("chart_payloads")
    chart_payloads = (
        raw_chart_payloads if isinstance(raw_chart_payloads, Mapping) else {}
    )
    for index, event in enumerate(result.get("events") or [], start=1):
        event_id = str(event.get("event_id") or "")
        photo, chart_status = _consolidation_chart_photo(
            event,
            chart_payloads.get(event_id),
        )
        push = gateway.send(
            str(event.get("text") or ""),
            str(result.get("template_id") or "TG_CONSOLIDATION_BREAKOUT"),
            str(event.get("dedup_key") or event_id),
            send=args.send,
            confirm_real_send=args.confirm_real_send,
            cooldown_sec=7 * 86400,
            parse_mode="HTML",
            signal_records=[event],
            photo=photo,
            enrich_market_context=False,
        )
        print(format_push_result_cn(
            "盘整突破雷达推送",
            push.status,
            push.reason,
            index=index,
        ))
        push_results.append({
            "event_id": event_id,
            "status": push.status,
            "reason": push.reason,
            "chart_status": chart_status,
        })
        if push.status == "sent" or (
            push.status == "skipped" and push.reason == "dedup_cooldown"
        ):
            accepted_event_ids.add(event_id)

    committed = radar.commit(result, accepted_event_ids)
    daily_digest_status, daily_digest_diagnostics = (
        _process_consolidation_daily_digest(
            settings,
            store,
            gateway,
            args,
            result,
        )
    )
    raw_base_diagnostics = result.get("diagnostics")
    raw_withheld_count = (
        raw_base_diagnostics.get("withheld_event_count", 0)
        if isinstance(raw_base_diagnostics, Mapping)
        else 0
    )
    try:
        base_events_withheld = int(raw_withheld_count) > 0
    except (TypeError, ValueError, OverflowError):
        base_events_withheld = False
    proximity_status, proximity_diagnostics = (
        _process_consolidation_hourly_proximity(
            settings,
            store,
            gateway,
            args,
            result.get("events") or [],
            len(accepted_event_ids) < len(result.get("events") or []),
            base_events_withheld,
        )
    )
    diagnostics = dict(result.get("diagnostics") or {})
    diagnostics["daily_digest"] = daily_digest_diagnostics
    diagnostics["hourly_proximity"] = proximity_diagnostics
    diagnostics["delivery"] = {
        "events": len(result.get("events") or []),
        "accepted": len(accepted_event_ids),
        "state_updates_committed": committed,
        "charts_ready": sum(
            item.get("chart_status") == "ready" for item in push_results
        ),
        "charts_delivered": sum(
            item.get("chart_status") == "ready"
            and item.get("status") == "sent"
            for item in push_results
        ),
        "charts_text_fallback": sum(
            item.get("chart_status") != "ready" for item in push_results
        ),
        "pushes": push_results,
    }
    statuses = {str(item.get("status") or "") for item in push_results}
    if daily_digest_status:
        statuses.add(daily_digest_status)
    if proximity_status:
        statuses.add(proximity_status)
    if statuses & {"failed", "partial"}:
        overall = "failed"
    elif "blocked" in statuses:
        overall = "blocked"
    elif "sent" in statuses:
        overall = "sent"
    elif "dry_run" in statuses:
        overall = "dry_run"
    else:
        overall = "skipped"
    return overall, diagnostics


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


def print_readiness(settings: Settings, store: JsonStore) -> int:
    runtime_health = [
        item for item in runtime_health_checks(settings, store)
        if item.get("name") != "runtime_status"
    ]
    health_failures = [
        item for item in runtime_health
        if item.get("status") == "fail"
    ]
    pulse_simple_scan_limit = int(settings.pulse_simple_scan_limit)
    pulse_scan_text = (
        "全部合格加密合约"
        if pulse_simple_scan_limit == 0
        else str(pulse_simple_scan_limit)
    )
    blocking_checks = [
        *telegram_config_checks(settings),
        *telegram_topic_route_checks(settings, store),
        (
            "runtime_health",
            not health_failures,
            "BOT 核心数据健康"
            if not health_failures
            else "；".join(
                str(item.get("detail") or "")
                for item in health_failures
            ),
        ),
        (
            "pulse_enabled",
            bool(settings.pulse_radar_enable),
            "脉冲雷达已启用"
            if settings.pulse_radar_enable
            else "脉冲雷达未启用",
        ),
        (
            "pulse_scan_limit",
            (
                int(settings.pulse_simple_scan_limit) >= 0
                and int(settings.pulse_divergence_scan_limit) > 0
            ),
            (
                f"脉冲扫描范围 15m={pulse_scan_text}，"
                f"2h={int(settings.pulse_divergence_scan_limit)}"
            ),
        ),
        (
            "pulse_kline_budget",
            bool(settings.binance_global_rate_limit_enable),
            (
                "细算预算按本轮合格合约动态分配；"
                f"触发图表额外预留 {PULSE_CHART_KLINE_RESERVE} 次；"
                "跨服务全局限流"
                f"{'已启用' if settings.binance_global_rate_limit_enable else '未启用'}"
            ),
        ),
    ]
    passed = sum(1 for _name, ok, _message in blocking_checks if ok)
    print(f"真实推送准备度: {passed}/{len(blocking_checks)}")
    for name, ok, message in blocking_checks:
        mark = "✅ 已通过" if ok else "⏳ 待处理"
        print(f"- {mark} {check_name_text(name)}：{message}")
    if passed == len(blocking_checks):
        print("")
        print("准备检查通过；真实发送仍需要 --send --confirm-real-send。")
        return 0
    print("")
    print("真实发送保持阻止，请先处理以上未通过项目。")
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
    consolidation_enabled = bool(
        settings.consolidation_breakout_enable
        and not bool(getattr(args, "no_consolidation_breakout", False))
    )
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
        no_consolidation_breakout=not consolidation_enabled,
        radar_scan_limit=settings.radar_scan_limit,
        pulse_simple_scan_limit=settings.pulse_simple_scan_limit,
        pulse_divergence_scan_limit=settings.pulse_divergence_scan_limit,
        flow_scan_limit=settings.flow_scan_limit,
        consolidation_breakout_scan_limit=(
            settings.consolidation_breakout_scan_limit
        ),
    )
    result = engine.run_once(
        include_announcements=not args.no_announcements,
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

    pulse_diagnostics: dict[str, object] = {"status": "skipped"}
    if not args.no_launch:
        pulse_diagnostics = run_pulse_cycle(
            engine,
            gateway,
            args,
        )
        result["diagnostics"]["pulse"] = pulse_diagnostics

    diagnostics = dict(result["diagnostics"])
    flow_push_status = "skipped"
    if not args.no_flow:
        flow_push_status, flow_diag = push_flow_radar(settings, gateway, args)
        diagnostics["flow"] = flow_diag
    funding_alert_push_status = "skipped"
    if not getattr(args, "no_funding_alert", False):
        funding_alert_push_status, funding_diag = push_funding_alert(settings, store, gateway, args)
        diagnostics["funding_alert"] = funding_diag
    consolidation_push_status = "skipped"
    consolidation_error_code = ""
    if consolidation_enabled:
        consolidation_push_status, consolidation_diag = (
            push_consolidation_breakout(settings, store, gateway, args)
        )
        diagnostics["consolidation_breakout"] = consolidation_diag
        consolidation_error_code = (
            _consolidation_hourly_proximity_error_code(
                consolidation_diag
            )
        )
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
        "pulse_simple_scan_limit": settings.pulse_simple_scan_limit,
        "pulse_divergence_scan_limit": settings.pulse_divergence_scan_limit,
        "flow_scan_limit": settings.flow_scan_limit,
        "funding_alert_scan_limit": settings.funding_alert_scan_limit,
        "consolidation_breakout_scan_limit": (
            settings.consolidation_breakout_scan_limit
        ),
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
        runtime_details["pulse_cycle_status"] = pulse_diagnostics.get("status")
        runtime_details["pulse_failed_components"] = pulse_diagnostics.get(
            "failed_components", []
        )
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
    if consolidation_enabled:
        runtime_details["consolidation_breakout_push"] = (
            consolidation_push_status
        )
        runtime_details["consolidation_breakout_cycle_status"] = (
            "failed" if consolidation_error_code else "ok"
        )
        runtime_details["consolidation_breakout_error_code"] = (
            consolidation_error_code
        )
    runtime_status = "running" if runtime_task == "loop" else "completed"
    if consolidation_error_code:
        runtime_status = "consolidation_breakout_failed"
    write_runtime_status(
        settings,
        store,
        mode,
        runtime_status,
        **runtime_details,
    )
    return 1 if consolidation_error_code else 0


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
    next_divergence = 0.0
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
    next_consolidation_breakout = next_closed_window_epoch(
        time.time(),
        interval_sec=settings.consolidation_breakout_interval_sec,
        delay_sec=settings.consolidation_breakout_close_delay_sec,
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
        pulse_interval_sec=15 * 60,
        flow_interval_sec=max(60, settings.flow_interval_sec),
        consolidation_breakout_interval_sec=max(
            60,
            settings.consolidation_breakout_interval_sec,
        ),
        funding_alert_interval_sec=max(60, settings.funding_alert_interval_sec),
        summary_close_delay_sec=settings.radar_summary_close_delay_sec,
        flow_close_delay_sec=settings.flow_close_delay_sec,
        next_summary_at=timestamp_from_epoch(next_summary),
        next_announcement_at=timestamp_from_epoch(next_announcement),
        next_flow_at=timestamp_from_epoch(next_flow),
        next_consolidation_breakout_at=timestamp_from_epoch(
            next_consolidation_breakout
        ),
        next_funding_alert_at=timestamp_from_epoch(next_funding_alert),
        next_launch_at="",
        next_market_snapshot_at="",
        **runtime_flags,
        radar_scan_limit=settings.radar_scan_limit,
        pulse_simple_scan_limit=settings.pulse_simple_scan_limit,
        pulse_divergence_scan_limit=settings.pulse_divergence_scan_limit,
        flow_scan_limit=settings.flow_scan_limit,
        consolidation_breakout_scan_limit=(
            settings.consolidation_breakout_scan_limit
        ),
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
        if (
            not switches["consolidation_breakout"]
            and now >= next_consolidation_breakout
        ):
            next_consolidation_breakout = next_closed_window_epoch(
                time.time(),
                interval_sec=settings.consolidation_breakout_interval_sec,
                delay_sec=settings.consolidation_breakout_close_delay_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                next_consolidation_breakout_at=timestamp_from_epoch(
                    next_consolidation_breakout
                ),
                settings_reload_error=settings_reload_error,
                **runtime_flags,
                last_error="",
            )
        if (
            switches["consolidation_breakout"]
            and now >= next_consolidation_breakout
        ):
            consolidation_ok = True
            consolidation_error_code = ""
            consolidation_diag: dict[str, object] = {}
            consolidation_push_status = "skipped"
            try:
                store, _engine, gateway = make_runtime_from_settings(settings)
                (
                    consolidation_push_status,
                    consolidation_diag,
                ) = push_consolidation_breakout(
                    settings,
                    store,
                    gateway,
                    args,
                )
                hourly_error = (
                    _consolidation_hourly_proximity_error_code(
                        consolidation_diag
                    )
                )
                if hourly_error:
                    consolidation_ok = False
                    consolidation_error_code = hourly_error
                print(json.dumps(
                    {"consolidation_breakout": consolidation_diag},
                    ensure_ascii=False,
                    indent=2,
                ))
            except Exception as exc:
                consolidation_ok = False
                consolidation_error_code = type(exc).__name__
                print(
                    "[loop] consolidation breakout failed: "
                    f"{consolidation_error_code}",
                    file=sys.stderr,
                )
            next_consolidation_breakout = next_closed_window_epoch(
                time.time(),
                interval_sec=settings.consolidation_breakout_interval_sec,
                delay_sec=settings.consolidation_breakout_close_delay_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                (
                    "running"
                    if consolidation_ok
                    else "consolidation_breakout_failed"
                ),
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                last_consolidation_breakout_at=timestamp_from_epoch(
                    time.time()
                ),
                next_consolidation_breakout_at=timestamp_from_epoch(
                    next_consolidation_breakout
                ),
                consolidation_breakout_push=consolidation_push_status,
                consolidation_breakout_cycle_status=(
                    "ok" if consolidation_ok else "failed"
                ),
                consolidation_breakout_error_code=(
                    consolidation_error_code
                ),
                diagnostics={
                    "consolidation_breakout": consolidation_diag
                },
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
            simple_cfg = SimpleAlertConfig.from_env(settings)
            next_launch = next_closed_window_epoch(
                time.time(),
                interval_sec=15 * 60,
                delay_sec=simple_cfg.close_delay_sec,
            )
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
            launch_diag: dict[str, object] = {}
            try:
                _local_store, engine, gateway = make_runtime_from_settings(
                    settings
                )
                include_divergence = now >= next_divergence
                launch_diag.update(run_pulse_cycle(
                    engine,
                    gateway,
                    args,
                    include_divergence=include_divergence,
                ))
                launch_ok = launch_diag.get("status") == "ok"
                if not launch_ok:
                    failed = launch_diag.get("failed_components") or []
                    launch_error_code = (
                        "pulse_components_failed:"
                        + ",".join(str(item) for item in failed)
                    )
                if include_divergence:
                    divergence_cfg = DivergenceConfig.from_env(settings)
                    next_divergence = next_closed_window_epoch(
                        time.time(),
                        interval_sec=divergence_cfg.interval_sec,
                        delay_sec=divergence_cfg.close_delay_sec,
                    )
                if snapshot_due:
                    launch_diag["market_snapshot"] = refresh_shared_market_snapshot(settings)
                    next_market_snapshot = time.time() + max(
                        60,
                        int(settings.market_snapshot_interval_sec),
                    )
                launch_diag["signal_effectiveness"] = dict(
                    signal_effectiveness_diag
                )
                print(json.dumps({"pulse": launch_diag}, ensure_ascii=False, indent=2))
            except Exception as exc:
                launch_ok = False
                launch_error_code = type(exc).__name__
                print(
                    f"[loop] pulse failed: {launch_error_code}",
                    file=sys.stderr,
                )
            simple_cfg = SimpleAlertConfig.from_env(settings)
            next_launch = next_closed_window_epoch(
                time.time(),
                interval_sec=15 * 60,
                delay_sec=simple_cfg.close_delay_sec,
            )
            write_runtime_status(
                settings,
                store,
                mode,
                "running" if launch_ok else "pulse_failed",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                last_launch_at=timestamp_from_epoch(time.time()),
                next_launch_at=timestamp_from_epoch(next_launch),
                next_market_snapshot_at=(
                    timestamp_from_epoch(next_market_snapshot)
                    if next_market_snapshot > 0
                    else ""
                ),
                pulse_cycle_status="ok" if launch_ok else "failed",
                pulse_error_code=launch_error_code,
                diagnostics={"pulse": launch_diag},
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
    if args.command == "telegram-topic-refresh":
        return run_telegram_topic_refresh(args)
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
    if args.command == "consolidation-breakout":
        if args.send and args.confirm_real_send:
            gate = require_real_send_gate(settings, store, args)
            if gate != 0:
                return gate
        return run_consolidation_breakout(args)
    if args.command == "runtime-status":
        print_runtime_status(settings, store)
        return 0
    if args.command == "radar-status":
        print_radar_status(settings, store)
        return 0
    if args.command == "signal-repair":
        report = SignalEventStore(settings.signal_events_db_path).repair_legacy_signals(apply=args.apply)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report.get("status") == "ok" or args.apply else 1
    if args.command == "signal-effectiveness":
        print_signal_effectiveness(settings)
        return 0
    if args.command == "pulse-review-report":
        report = build_review_report(
            settings,
            days=max(1, int(args.review_days)),
            top=max(1, int(args.review_top)),
        )
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print(format_review_report(report))
        return 0
    if args.command == "pulse":
        if args.send and args.confirm_real_send:
            gate = require_real_send_gate(settings, store, args)
            if gate != 0:
                return gate
        _pulse_store, pulse_engine, pulse_gateway = make_runtime_for_args(args)[1:]
        pulse = run_pulse_cycle(pulse_engine, pulse_gateway, args)
        print(json.dumps({"pulse": pulse}, ensure_ascii=False, indent=2))
        return 0 if pulse.get("status") == "ok" else 1
    if args.command == "once":
        if args.send and args.confirm_real_send:
            gate = require_real_send_gate(settings, store, args)
            if gate != 0:
                return gate
        return run_once(args)
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
