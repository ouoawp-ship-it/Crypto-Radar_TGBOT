from __future__ import annotations

"""
泡泡抓币：精简版加密监控工具。

核心功能：
- Binance 公告机会/风险监听：Alpha、上新、HODLer、Launchpool、Airdrop、下架、停止交易等。
- 费率/OI 异动扫描：负费率、资金费率趋势、持仓变化、价格变化、成交量变化。
- 热度做多雷达：按涨幅、成交量、OI、资金费率筛选短线动量。
- 庄家收筹/埋伏池：低市值、横盘、OI 暗流、负费率燃料的综合评分。
- BN 行情启动预警：15m/1h 价格、OI、成交量、短周期突破分层提醒。
- OI/价格背离扫描：识别建仓背离、多头共振、极端背离等状态。

默认推送周期：
- 资金雷达汇总：6 小时一次，每天最多 4 次；收线后延迟抓上一完整窗口。
- 启动雷达提醒：3 分钟检查一次，按最近完整 15m 收线窗口判断。
- 公告机会/风险：跟随主扫描。
- 同币同阶段启动提醒：默认 6 小时冷却。
"""

import argparse
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path
from collections import Counter
from dataclasses import replace
from datetime import datetime

from .auth import append_auth_audit, generate_password_hash, generate_session_secret
from .config import ENV_FILE, Settings, load_env_file
from .data_sources import BinanceDataSource
from .flow_radar import FlowRadarEngine
from .funding_alert import FundingAlertEngine
from .liquidity_router import build_liquidity_enhancer
from .lifecycle_engine import lifecycle_report_text, lifecycle_status_payload, scan_lifecycles
from .maintenance import cleanup_runtime_artifacts, cleanup_structure_charts, legacy_state_report, migrate_legacy_state
from .outcome_tracker import scan_outcomes, scan_report_text
from .radar import RadarEngine, fmt_price
from .storage import JsonStore
from .structure_radar import (
    SIGNAL_CN,
    StructureRadarEngine,
    StructureSignal,
    next_structure_confirm_epoch,
    next_structure_pre_epoch,
)
from .structure_review import StructureReviewEngine
from .telegram import TelegramGateway
from .time_windows import next_closed_window_epoch


PROJECT_ABOUT = """泡泡抓币：精简版加密监控工具

保留功能：
- Binance 公告机会/风险监听：Alpha、上新、HODLer、Launchpool、Airdrop、下架、停止交易等。
- 费率/OI 异动扫描：资金费率、持仓、价格、成交量、数据质量。
- 热度做多雷达：涨幅、成交量、OI、资金费率综合筛选短线动量。
- 庄家收筹/埋伏池：低市值、横盘、OI 暗流、负费率燃料综合评分。
- BN 行情启动预警：15m/1h 价格、OI、成交量、短周期突破分层提醒。
- OI/价格背离扫描：建仓背离、多头共振、极端背离、信号持续/增强/消失。

推送内容：
- 资金雷达汇总：负费率榜、综合榜、埋伏榜、动量池、新币池、值得关注、图例、数据质量。
- 启动雷达提醒：币种、阶段、分数、价格变化、OI 变化、成交量放大、触发原因。
- 公告提醒：公告类型、关联币种、机会/风险说明。
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
        choices=["about", "status", "doctor", "readiness", "stable-check", "telegram-test", "announcements-test", "flow-radar", "funding-alert", "structure-radar", "structure-loop", "structure-review", "runtime-status", "cleanup", "outcome-scan", "lifecycle-backfill", "lifecycle-scan", "lifecycle-status", "lifecycle-intelligence", "lifecycle-replay", "lifecycle-replay-backfill", "lifecycle-analytics", "lifecycle-similar", "lifecycle-outcome-link", "lifecycle-outcome-backfill", "lifecycle-outcome-status", "lifecycle-outcome-reconcile", "watchlist", "launch-history", "launch-report", "migrate-state", "web", "ai-assistant", "price-alerts", "admin-password", "once", "trial", "observe", "loop", "daemon", "live"],
        help="默认 status；about 查看功能说明；doctor 检查环境；stable-check 稳定版验收；cleanup 清理运行垃圾；readiness 检查真实推送准备度；flow-radar 扫描五因子资金流；once 扫描一轮；observe dry-run 观察；loop/daemon 持续运行；live 通过门禁后真实推送",
    )
    parser.add_argument("admin_action", nargs="?", default="", help="用于 admin-password：set")
    parser.add_argument("--send", action="store_true", help="允许真实发送 Telegram；仍需要 --confirm-real-send")
    parser.add_argument("--confirm-real-send", action="store_true", help="确认真实发送 Telegram")
    parser.add_argument("--apply", action="store_true", help="用于 migrate-state：真正复制旧状态文件")
    parser.add_argument("--force-cleanup", action="store_true", help="用于 cleanup：忽略清理间隔，立即执行")
    parser.add_argument("--top", type=int, default=12, help="用于 watchlist/报告：显示前 N 个候选")
    parser.add_argument("--records", type=int, default=100, help="用于 launch-report：统计最近 N 轮")
    parser.add_argument("--limit", type=int, default=None, help="用于 outcome/lifecycle-outcome/replay：本次最多处理的有界批量")
    parser.add_argument("--limit-symbols", type=int, default=None, help="用于 lifecycle-backfill/lifecycle-scan：本次最多处理多少个币种")
    parser.add_argument("--cycles", type=int, default=3, help="用于 trial：试跑轮数")
    parser.add_argument("--duration-minutes", type=int, default=360, help="用于 observe：观察总时长分钟数")
    parser.add_argument("--interval", default=None, help="loop/daemon 的资金雷达摘要间隔秒数；structure-radar 使用 15m/1h 这类K线周期")
    parser.add_argument("--launch-interval", type=int, default=180, help="loop/daemon 的启动雷达间隔秒数")
    parser.add_argument("--radar-scan-limit", type=int, default=None, help="临时覆盖资金雷达扫描上限")
    parser.add_argument("--launch-scan-limit", type=int, default=None, help="临时覆盖启动雷达扫描上限")
    parser.add_argument("--flow-scan-limit", type=int, default=None, help="临时覆盖五因子资金流雷达扫描上限")
    parser.add_argument("--funding-scan-limit", type=int, default=None, help="临时覆盖资金费率警报扫描上限")
    parser.add_argument("--top-symbols", type=int, default=None, help="structure-radar 临时覆盖扫描币种数量")
    parser.add_argument("--min-score", type=float, default=None, help="structure-radar 临时覆盖最低推送分数")
    parser.add_argument("--save-charts", action="store_true", help="structure-radar 保存K线状态图")
    parser.add_argument("--mode", choices=["pre", "confirm"], default="pre", help="structure-radar 运行模式：pre 提前临界，confirm 收线确认")
    parser.add_argument("--lookback-hours", type=int, default=None, help="structure-review 或 lifecycle 命令的回看小时数")
    parser.add_argument("--horizon", default="", help="用于 outcome-scan/lifecycle-outcome：只处理 1h/4h/24h/72h 中的一个窗口")
    parser.add_argument("--symbol", default="", help="用于 outcome/lifecycle 命令：只处理某个币种，例如 BTC 或 BTCUSDT")
    parser.add_argument("--lifecycle-id", type=int, default=None, help="用于 lifecycle-replay/lifecycle-outcome：按生命周期 ID 精确处理")
    parser.add_argument("--all-active", action="store_true", help="用于 lifecycle-intelligence：处理全部活跃生命周期")
    parser.add_argument("--dry-run", action="store_true", help="用于 outcome/lifecycle 命令：只预览，不写数据库或发送 Telegram")
    parser.add_argument("--pretty", action="store_true", help="生命周期智能命令使用缩进 JSON 输出")
    parser.add_argument("--force-rebuild", action="store_true", help="忽略源事件签名缓存并强制重新生成智能评价或回放")
    parser.add_argument("--force-relink", action="store_true", help="用于 lifecycle outcome 命令：重新核对已有关联，不重算成功 Outcome")
    parser.add_argument("--force-outcome-rebuild", action="store_true", help="用于 lifecycle-outcome-backfill：明确重算已到期 Outcome")
    parser.add_argument("--repair", action="store_true", help="用于 lifecycle-outcome-reconcile：修复可安全修复的覆盖率数据")
    parser.add_argument("--backfill-days", type=int, default=None, help="用于 outcome-scan：回填最近 N 天已发送信号")
    parser.add_argument("--push", action="store_true", help="用于 lifecycle-scan：对重要生命周期事件尝试 Telegram 跟随推送；真实发送仍需 --send --confirm-real-send")
    parser.add_argument("--no-launch", action="store_true", help="本轮不运行启动雷达")
    parser.add_argument("--no-announcements", action="store_true", help="本轮不扫描公告机会/风险")
    parser.add_argument("--no-flow", action="store_true", help="本轮不运行五因子资金流雷达")
    parser.add_argument("--no-funding-alert", action="store_true", help="本轮不运行资金费率警报")
    parser.add_argument("--host", default="", help="web 控制台监听地址，默认读取 WEB_HOST")
    parser.add_argument("--port", type=int, default=0, help="web 控制台端口，默认读取 WEB_PORT")
    parser.add_argument("--web-token", default="", help="旧 token 认证模式访问令牌；也可用 WEB_ADMIN_TOKEN")
    parser.add_argument("--hidden", action="store_true", help="用于 admin-password set：隐藏输入密码")
    parser.add_argument("--json", action="store_true", help="用于 stable-check：输出完整 JSON 快照")
    parser.add_argument("--no-save", action="store_true", help="用于 stable-check：只查看，不写入验收历史")
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
    top_symbols = getattr(args, "top_symbols", None)
    min_score = getattr(args, "min_score", None)
    interval = getattr(args, "interval", None)
    save_charts = getattr(args, "save_charts", False)
    lookback_hours = getattr(args, "lookback_hours", None)
    if radar_scan_limit is not None:
        updates["radar_scan_limit"] = max(0, int(radar_scan_limit))
    if launch_scan_limit is not None:
        updates["launch_scan_limit"] = max(0, int(launch_scan_limit))
    if flow_scan_limit is not None:
        updates["flow_scan_limit"] = max(0, int(flow_scan_limit))
    if funding_scan_limit is not None:
        updates["funding_alert_scan_limit"] = max(0, int(funding_scan_limit))
    if top_symbols is not None:
        updates["structure_top_symbols"] = max(1, int(top_symbols))
    if min_score is not None:
        updates["structure_min_score"] = max(0, int(float(min_score)))
    if interval is not None and not str(interval).isdigit():
        updates["structure_interval"] = str(interval)
    if save_charts:
        updates["structure_save_charts"] = True
    if lookback_hours is not None:
        updates["structure_review_lookback_hours"] = max(1, int(lookback_hours))
    if not updates:
        return settings
    return replace(settings, **updates)


def make_runtime_for_args(args: argparse.Namespace) -> tuple[Settings, JsonStore, RadarEngine, TelegramGateway]:
    settings, store, engine, gateway = make_runtime()
    updated = apply_cli_overrides(settings, args)
    if updated == settings:
        return settings, store, engine, gateway
    store = JsonStore(updated.data_dir)
    engine = RadarEngine(updated, store)
    gateway = TelegramGateway(updated, store)
    return updated, store, engine, gateway


def state_paths(settings: Settings) -> list[Path]:
    return [
        settings.tg_push_history_path,
        settings.ai_price_alerts_db_path,
        settings.runtime_status_path,
        settings.structure_runtime_status_path,
        settings.radar_state_path,
        settings.funding_snapshot_path,
        settings.funding_alert_state_path,
        settings.launch_state_path,
        settings.launch_watchlist_path,
        settings.launch_watch_history_path,
        settings.structure_state_path,
        settings.structure_history_path,
        settings.structure_review_path,
        settings.structure_stats_path,
        settings.structure_review_report_path,
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


def update_env_values(path: Path, updates: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key = line.split("=", 1)[0].strip()
        positions.setdefault(key, index)
    for key, value in updates.items():
        if key in positions:
            lines[positions[key]] = f"{key}={value}"
        else:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"{key}={value}")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    for key, value in updates.items():
        os.environ[key] = value


def run_admin_password(args: argparse.Namespace) -> int:
    action = str(getattr(args, "admin_action", "") or "set")
    if action != "set":
        print("用法: python main.py admin-password set [--hidden]")
        return 2
    load_env_file(ENV_FILE)
    hidden = bool(getattr(args, "hidden", False))
    if not hidden:
        print("提示：当前密码输入会明文显示，请确认终端环境安全。")
    username = input("后台用户名 [admin]: ").strip() or "admin"
    if hidden:
        password = getpass.getpass("后台密码: ")
        confirm = getpass.getpass("再次输入后台密码: ")
    else:
        password = input("后台密码: ")
        confirm = input("再次输入后台密码: ")
    if not password:
        print("密码不能为空")
        return 2
    if password != confirm:
        print("两次输入的密码不一致，请重新执行设置命令。")
        return 2
    current_secret = os.getenv("WEB_SESSION_SECRET", "").strip()
    updates = {
        "WEB_AUTH_MODE": "password",
        "WEB_ADMIN_USERNAME": username,
        "WEB_ADMIN_PASSWORD_HASH": generate_password_hash(password),
        "WEB_SESSION_SECRET": current_secret or generate_session_secret(),
        "WEB_SESSION_TTL_SEC": os.getenv("WEB_SESSION_TTL_SEC", "86400").strip() or "86400",
        "WEB_AUTH_COOKIE_NAME": os.getenv("WEB_AUTH_COOKIE_NAME", "paopao_admin_session").strip() or "paopao_admin_session",
    }
    update_env_values(ENV_FILE, updates)
    try:
        append_auth_audit(
            Path(os.getenv("DATA_DIR", str(ENV_FILE.parent / "data"))),
            event="password_changed",
            username=username,
            ip="local-cli",
            user_agent="main.py admin-password set",
            result="success",
            reason="password_set",
            limit=max(1, int(os.getenv("WEB_AUTH_AUDIT_LIMIT", "500") or "500")),
            secret=updates["WEB_SESSION_SECRET"],
        )
    except Exception:
        pass
    print(f"后台用户名已设置：{username}")
    print("后台密码哈希已更新")
    if current_secret:
        print("会话密钥已保留")
    else:
        print("会话密钥已生成")
    print("请重启 paopao-web 服务生效：")
    print("sudo systemctl restart paopao-web")
    return 0


def run_outcome_scan(args: argparse.Namespace) -> int:
    settings = Settings.load()
    result = scan_outcomes(
        settings=settings,
        limit=getattr(args, "limit", None),
        horizon=str(getattr(args, "horizon", "") or ""),
        symbol=str(getattr(args, "symbol", "") or ""),
        dry_run=bool(getattr(args, "dry_run", False)),
        backfill_days=getattr(args, "backfill_days", None),
    )
    print(scan_report_text(result))
    return 0 if result.get("ok", True) else 1


def run_lifecycle_backfill(args: argparse.Namespace) -> int:
    settings = Settings.load()
    lookback_hours = int(getattr(args, "lookback_hours", None) or 168)
    limit_symbols = max(
        1,
        min(
            int(getattr(args, "limit_symbols", None) or settings.lifecycle_active_max_symbols or 80),
            500,
        ),
    )
    result = scan_lifecycles(
        settings=settings,
        lookback_hours=lookback_hours,
        limit_symbols=limit_symbols,
        dry_run=bool(getattr(args, "dry_run", False)),
        push=False,
    )
    print(lifecycle_report_text(result))
    return 0 if result.get("ok", True) else 1


def run_lifecycle_scan(args: argparse.Namespace) -> int:
    settings = Settings.load()
    result = scan_lifecycles(
        settings=settings,
        lookback_hours=int(getattr(args, "lookback_hours", None) or settings.lifecycle_lookback_hours or 24),
        limit_symbols=int(getattr(args, "limit_symbols", None) or getattr(args, "limit", None) or settings.lifecycle_active_max_symbols or 80),
        symbol=str(getattr(args, "symbol", "") or ""),
        dry_run=bool(getattr(args, "dry_run", False)),
        push=bool(getattr(args, "push", False)),
        send=bool(getattr(args, "send", False)),
        confirm_real_send=bool(getattr(args, "confirm_real_send", False)),
    )
    print(lifecycle_report_text(result))
    return 0 if result.get("ok", True) else 1


def run_lifecycle_status(args: argparse.Namespace) -> int:
    settings = Settings.load()
    payload = lifecycle_status_payload(settings=settings, symbol=str(getattr(args, "symbol", "") or ""))
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if payload.get("ok", True) else 1


def _print_lifecycle_intelligence_result(result: dict[str, object], args: argparse.Namespace) -> int:
    print(json.dumps(result, ensure_ascii=False, indent=2 if bool(getattr(args, "pretty", False)) else None))
    return 0 if bool(result.get("ok", True)) else 1


def run_lifecycle_intelligence(args: argparse.Namespace) -> int:
    from .lifecycle_intelligence import generate_intelligence

    result = generate_intelligence(
        settings=Settings.load(),
        symbol=str(getattr(args, "symbol", "") or ""),
        all_active=bool(getattr(args, "all_active", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
        force=bool(getattr(args, "force_rebuild", False)),
        limit=max(1, min(int(getattr(args, "limit", None) or 500), 5000)),
    )
    return _print_lifecycle_intelligence_result(result, args)


def run_lifecycle_replay(args: argparse.Namespace, *, backfill: bool = False) -> int:
    from .lifecycle_replay import get_replay_payload, rebuild_replays

    settings = Settings.load()
    symbol = "" if backfill else str(getattr(args, "symbol", "") or "")
    lifecycle_id = None if backfill else getattr(args, "lifecycle_id", None)
    if not backfill and not symbol.strip() and not lifecycle_id:
        return _print_lifecycle_intelligence_result(
            {
                "ok": False,
                "code": "missing_lifecycle_target",
                "error": "lifecycle-replay 需要 --symbol 或 --lifecycle-id。",
            },
            args,
        )
    result = rebuild_replays(
        settings=settings,
        symbol=symbol,
        lifecycle_id=lifecycle_id,
        limit=max(1, min(int(getattr(args, "limit", None) or (500 if backfill else 1)), 5000)),
        dry_run=bool(getattr(args, "dry_run", False)),
        force=bool(getattr(args, "force_rebuild", False)),
    )
    if not backfill and not bool(getattr(args, "dry_run", False)) and result.get("ok", True):
        replay = get_replay_payload(
            settings=settings,
            symbol=symbol,
            lifecycle_id=lifecycle_id,
            frame_limit=100,
            frame_offset=0,
        )
        result = {**result, "replay": replay.get("data", replay)}
    return _print_lifecycle_intelligence_result(result, args)


def run_lifecycle_analytics(args: argparse.Namespace) -> int:
    from .lifecycle_analytics import generate_lifecycle_analytics

    result = generate_lifecycle_analytics(
        settings=Settings.load(),
        dry_run=bool(getattr(args, "dry_run", False)),
        force=bool(getattr(args, "force_rebuild", False)),
    )
    return _print_lifecycle_intelligence_result(result, args)


def run_lifecycle_similar(args: argparse.Namespace) -> int:
    from .lifecycle_similarity import find_similar_for_symbol

    settings = Settings.load()
    symbol = str(getattr(args, "symbol", "") or "")
    result = find_similar_for_symbol(
        settings=settings,
        symbol=symbol,
        limit=max(1, min(int(getattr(args, "limit", None) or 10), 50)),
        min_samples=max(1, int(settings.lifecycle_similarity_min_samples or 5)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    if not isinstance(result, dict):
        result = {"ok": False, "error": "相似生命周期计算返回格式异常"}
    else:
        if result.get("status") == "not_found":
            result["status"] = "insufficient_mature_samples"
            result["message"] = "当前相似样本不足，暂不生成统计结论。"
            result["ok"] = True
        result.setdefault("ok", True)
    return _print_lifecycle_intelligence_result(result, args)


def _lifecycle_outcome_limit(args: argparse.Namespace, settings: Settings) -> int:
    configured = max(1, int(getattr(settings, "lifecycle_outcome_backfill_batch_size", 200) or 200))
    return max(1, min(int(getattr(args, "limit", None) or configured), 1000))


def run_lifecycle_outcome_link(args: argparse.Namespace) -> int:
    from .lifecycle_outcomes import link_lifecycle_outcomes

    settings = Settings.load()
    result = link_lifecycle_outcomes(
        settings=settings,
        symbol=str(getattr(args, "symbol", "") or ""),
        lifecycle_id=getattr(args, "lifecycle_id", None),
        limit=_lifecycle_outcome_limit(args, settings),
        horizon=str(getattr(args, "horizon", "") or ""),
        dry_run=bool(getattr(args, "dry_run", False)),
        force_relink=bool(getattr(args, "force_relink", False)),
    )
    return _print_lifecycle_intelligence_result(result, args)


def run_lifecycle_outcome_backfill(args: argparse.Namespace) -> int:
    from .lifecycle_outcomes import backfill_lifecycle_outcomes

    settings = Settings.load()
    result = backfill_lifecycle_outcomes(
        settings=settings,
        symbol=str(getattr(args, "symbol", "") or ""),
        lifecycle_id=getattr(args, "lifecycle_id", None),
        limit=_lifecycle_outcome_limit(args, settings),
        horizon=str(getattr(args, "horizon", "") or ""),
        dry_run=bool(getattr(args, "dry_run", False)),
        force_relink=bool(getattr(args, "force_relink", False)),
        force_outcome_rebuild=bool(getattr(args, "force_outcome_rebuild", False)),
    )
    return _print_lifecycle_intelligence_result(result, args)


def run_lifecycle_outcome_status(args: argparse.Namespace) -> int:
    from .lifecycle_outcomes import lifecycle_outcome_status

    result = lifecycle_outcome_status(
        settings=Settings.load(),
        symbol=str(getattr(args, "symbol", "") or ""),
        lifecycle_id=getattr(args, "lifecycle_id", None),
    )
    return _print_lifecycle_intelligence_result(result, args)


def run_lifecycle_outcome_reconcile(args: argparse.Namespace) -> int:
    from .lifecycle_outcomes import reconcile_lifecycle_outcomes

    settings = Settings.load()
    result = reconcile_lifecycle_outcomes(
        settings=settings,
        symbol=str(getattr(args, "symbol", "") or ""),
        lifecycle_id=getattr(args, "lifecycle_id", None),
        limit=_lifecycle_outcome_limit(args, settings),
        repair=bool(getattr(args, "repair", False)),
        dry_run=bool(getattr(args, "dry_run", False)),
    )
    return _print_lifecycle_intelligence_result(result, args)


def run_lifecycle_tracker_cycle(settings: Settings, args: argparse.Namespace) -> dict[str, object]:
    """Run one isolated lifecycle cycle without changing the main push flow."""
    if not bool(settings.lifecycle_tracker_enable):
        return {
            "ok": True,
            "skipped": True,
            "reason": "lifecycle_tracker_disabled",
            "counts": {},
        }
    real_send = bool(getattr(args, "send", False) and getattr(args, "confirm_real_send", False))
    try:
        return scan_lifecycles(
            settings=settings,
            lookback_hours=max(1, int(settings.lifecycle_lookback_hours or 24)),
            limit_symbols=max(1, min(int(settings.lifecycle_active_max_symbols or 80), 500)),
            dry_run=False,
            push=bool(settings.lifecycle_telegram_enable),
            send=real_send,
            confirm_real_send=real_send,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        print(f"[loop] lifecycle failed: {error}", file=sys.stderr)
        return {
            "ok": False,
            "error": error,
            "message": "生命周期扫描失败，主服务继续运行。",
            "counts": {},
        }


def command_mode(args: argparse.Namespace) -> str:
    return str(getattr(args, "command", "") or "unknown")


def timestamp_from_epoch(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def next_interval_epoch(value: float, interval_sec: int) -> float:
    interval = max(60, int(interval_sec))
    if interval % 3600 == 0:
        return float((int(value) // interval + 1) * interval)
    return value + interval


def write_runtime_status(
    settings: Settings,
    store: JsonStore,
    mode: str,
    status: str,
    **details: object,
) -> dict[str, object]:
    task = str(details.get("task", ""))
    status_path = (
        settings.structure_runtime_status_path
        if task.startswith("structure") or mode.startswith("structure")
        else settings.runtime_status_path
    )
    payload: dict[str, object] = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "status": status,
    }
    payload.update(details)
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
    structure_status = _load_runtime_status_or_empty(
        store,
        settings.structure_runtime_status_path,
        "structure",
    )
    print(json.dumps({
        "main": main_status,
        "structure": structure_status,
    }, ensure_ascii=False, indent=2))


def print_cleanup(settings: Settings, store: JsonStore, force: bool) -> None:
    result = cleanup_runtime_artifacts(settings, store, force=force)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def print_doctor(settings: Settings, store: JsonStore) -> None:
    status = build_status(settings, store)
    status["legacy_state"] = legacy_state_report(settings)
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


def _release_readiness_status_label(status: str) -> str:
    return {
        "complete_candidate": "完整稳定版候选",
        "candidate": "准稳定候选",
        "blocked": "需要处理",
        "ok": "通过",
        "warn": "关注",
        "fail": "未达标",
    }.get(str(status or ""), str(status or "未知"))


def _trend_value(value: object) -> str:
    return "未记录" if value is None else str(value)


def print_stable_check(as_json: bool = False, save: bool = True) -> int:
    from .web import build_deployment_acceptance, build_release_readiness, build_release_trend, ops_snapshot_payload, save_stability_snapshot, stability_history_payload

    snapshot = ops_snapshot_payload()
    if save:
        snapshot["stability_saved"] = save_stability_snapshot(snapshot)
        snapshot["stability_history"] = stability_history_payload(limit=8)
        snapshot["release_readiness"] = build_release_readiness(snapshot)
        snapshot["release_trend"] = build_release_trend(snapshot["stability_history"])
        snapshot["deployment_acceptance"] = build_deployment_acceptance(snapshot)
    stability = snapshot.get("stability", {}) if isinstance(snapshot.get("stability"), dict) else {}
    release_readiness = snapshot.get("release_readiness", {}) if isinstance(snapshot.get("release_readiness"), dict) else {}
    release_trend = snapshot.get("release_trend", {}) if isinstance(snapshot.get("release_trend"), dict) else {}
    deployment = snapshot.get("deployment_acceptance", {}) if isinstance(snapshot.get("deployment_acceptance"), dict) else {}
    if as_json:
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    else:
        git = snapshot.get("git", {}) if isinstance(snapshot.get("git"), dict) else {}
        print("泡泡雷达稳定版自检")
        print(f"生成时间: {snapshot.get('generated_at', '')}")
        print(f"版本: {git.get('version', '')} {git.get('branch', '')} {git.get('commit', '')}".strip())
        print(f"状态: {_stable_check_status_label(str(stability.get('status') or ''))}")
        print(f"摘要: {stability.get('summary') or ''}")
        saved = snapshot.get("stability_saved", {}) if isinstance(snapshot.get("stability_saved"), dict) else {}
        if saved.get("saved"):
            print(f"记录: 已保存 latest/history，历史 {saved.get('history_count', 0)} 条")
        elif not save:
            print("记录: 本次未保存（--no-save）")
        print("")
        print("长期运行就绪度:")
        print(f"状态: {_release_readiness_status_label(str(release_readiness.get('status') or ''))}")
        print(f"评分: {release_readiness.get('score', '')}/100")
        print(f"摘要: {release_readiness.get('summary') or ''}")
        print(f"下一目标: {release_readiness.get('next_version_goal') or ''}")
        print(
            "计数: "
            f"通过 {int(release_readiness.get('ok_count', 0) or 0)} | "
            f"警告 {int(release_readiness.get('warn_count', 0) or 0)} | "
            f"阻断 {int(release_readiness.get('fail_count', 0) or 0)}"
        )
        readiness_checks = release_readiness.get("checks", []) if isinstance(release_readiness.get("checks"), list) else []
        if readiness_checks:
            print("就绪度检查:")
        for item in readiness_checks:
            if not isinstance(item, dict):
                continue
            status_label = _release_readiness_status_label(str(item.get("status") or ""))
            line = f"- {item.get('label', '')}: {status_label} - {item.get('detail', '')}"
            action = str(item.get("action") or "")
            if action:
                line += f" | 建议: {action}"
            print(line)
        print("")
        print("服务器部署验收:")
        print(f"状态: {deployment.get('label') or _stable_check_status_label(str(deployment.get('status') or ''))}")
        print(f"摘要: {deployment.get('summary') or ''}")
        print(f"下一步: {deployment.get('next_action') or ''}")
        print(
            "计数: "
            f"通过 {int(deployment.get('ok_count', 0) or 0)} | "
            f"警告 {int(deployment.get('warn_count', 0) or 0)} | "
            f"阻断 {int(deployment.get('fail_count', 0) or 0)}"
        )
        deployment_checks = deployment.get("checks", []) if isinstance(deployment.get("checks"), list) else []
        if deployment_checks:
            print("部署检查:")
        for item in deployment_checks:
            if not isinstance(item, dict):
                continue
            line = f"- {item.get('label', '')}: {_stable_check_status_label(str(item.get('status') or ''))} - {item.get('detail', '')}"
            action = str(item.get("action") or "")
            if action:
                line += f" | 建议: {action}"
            print(line)
        print("")
        print("趋势变化:")
        print(f"状态: {release_trend.get('label') or '暂无趋势'}")
        print(
            "分数: "
            f"当前 {_trend_value(release_trend.get('current_score'))} | "
            f"上次 {_trend_value(release_trend.get('previous_score'))} | "
            f"变化 {_trend_value(release_trend.get('score_delta'))}"
        )
        print(f"摘要: {release_trend.get('summary') or ''}")
        print(f"建议: {release_trend.get('action') or ''}")
        print("")
        print("检查项:")
        checks = stability.get("checks", []) if isinstance(stability.get("checks"), list) else []
        if not checks:
            print("- 暂无自检结果")
        for item in checks:
            if not isinstance(item, dict):
                continue
            line = f"- {item.get('label', '')}: {_stable_check_status_label(str(item.get('status') or ''))} - {item.get('detail', '')}"
            action = str(item.get("action") or "")
            if action:
                line += f" | 建议: {action}"
            print(line)
        print("")
        print("建议动作:")
        recommendations = snapshot.get("recommendations", []) if isinstance(snapshot.get("recommendations"), list) else []
        if not recommendations:
            print("- 暂无")
        for item in recommendations:
            print(f"- {item}")
    status = str(stability.get("status") or "")
    if status == "blocked":
        return 2
    if status == "attention":
        return 1
    return 0


def run_telegram_test(args: argparse.Namespace) -> int:
    settings, _store, _engine, gateway = make_runtime()
    if args.send and args.confirm_real_send:
        checks = telegram_config_checks(settings)
        failed = [(name, message) for name, ok, message in checks if not ok]
        if failed:
            print("telegram_test: blocked (invalid Telegram config)")
            for name, message in failed:
                print(f"- WAIT {name}: {message}")
            return 2
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    text = "\n".join([
        "🧪 泡泡抓币 Telegram 测试",
        f"时间: {now}",
        "用途: 验证 bot token / chat id / topic 配置",
        "说明: 这不是交易信号",
    ])
    result = gateway.send(
        text,
        "TG_TEST_MESSAGE",
        f"telegram-test:{datetime.now().strftime('%Y%m%d%H%M')}",
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=0,
        daily_limit=None,
        parse_mode="",
    )
    print(f"telegram_test: {result.status} ({result.reason})")
    if result.status == "blocked":
        return 2
    if result.status == "failed":
        return 1
    return 0

def run_announcements_test(args: argparse.Namespace) -> int:
    settings, store, engine, _gateway = make_runtime_for_args(args)
    source = BinanceDataSource(settings)
    result = engine.build_announcement_alerts(source, include_seen=True)
    alert_summaries = []
    for alert in result.get("alerts", []):
        if not isinstance(alert, dict):
            continue
        alert_summaries.append({
            "kind": alert.get("kind", ""),
            "code": alert.get("code", ""),
            "title": alert.get("title", ""),
            "symbols": alert.get("symbols", []),
            "contract_symbols": alert.get("contract_symbols", []),
            "non_contract_symbols": alert.get("non_contract_symbols", []),
            "release_ts": alert.get("release_ts", 0),
            "expires_at": alert.get("expires_at", 0),
        })
    print("announcements_test: ok")
    print(json.dumps({
        "page_size": settings.announcement_page_size,
        "articles_scanned": result.get("articles_scanned", 0),
        "alerts_classified": result.get("alerts_classified", 0),
        "messages_ready": len(result.get("messages", [])),
        "alerts": alert_summaries,
        "diagnostics": source.diagnostics(),
    }, ensure_ascii=False, indent=2))
    return 0


def run_flow_radar(args: argparse.Namespace) -> int:
    settings, _store, _engine, gateway = make_runtime_for_args(args)
    flow = FlowRadarEngine(settings).build(
        BinanceDataSource(settings),
    )
    push = gateway.send(
        flow["text"],
        flow["template_id"],
        flow["dedup_key"],
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=max(60, settings.flow_interval_sec),
        parse_mode="HTML",
    )
    print(f"flow_push: {push.status} ({push.reason})")
    print(json.dumps(flow["diagnostics"], ensure_ascii=False, indent=2))
    return 0 if push.status != "failed" else 1


def push_flow_radar(settings: Settings, gateway: TelegramGateway, args: argparse.Namespace) -> tuple[str, dict[str, object]]:
    flow = FlowRadarEngine(settings).build(
        BinanceDataSource(settings),
    )
    push = gateway.send(
        flow["text"],
        flow["template_id"],
        flow["dedup_key"],
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=max(60, settings.flow_interval_sec),
        parse_mode="HTML",
    )
    print(f"flow_push: {push.status} ({push.reason})")
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
    result = funding_engine.build(BinanceDataSource(settings))
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
        )
        print(f"funding_alert_push[{idx}]: {push.status} ({push.reason})")
        push_status = push.status
        if push.status == "sent":
            alert["message_ids"] = push.message_ids or []
            sent_alerts.append(alert)
    funding_engine.mark_pushed(sent_alerts)
    return push_status, result["diagnostics"]


def structure_photo_caption(signal: StructureSignal) -> str:
    signal_name = SIGNAL_CN.get(signal.signal_type, signal.signal_type)
    return (
        f"🧱 <b>结构图</b> {signal.symbol}\n"
        f"{signal_name} | {signal.level}级 {signal.score:.0f}分 | {signal.interval}\n"
        f"上沿 {fmt_price(signal.box_high)} | 下沿 {fmt_price(signal.box_low)} | 现价 {fmt_price(signal.price)}"
    )


def delete_chart_after_success(settings: Settings, photo_result: object, chart_path: str | None) -> dict[str, object]:
    if not settings.structure_delete_chart_after_send:
        return {"deleted": False, "reason": "disabled"}
    if not chart_path:
        return {"deleted": False, "reason": "missing_path"}
    if getattr(photo_result, "status", "") != "sent" or not bool(getattr(photo_result, "sent", False)):
        return {"deleted": False, "reason": "not_sent"}
    path = Path(chart_path)
    try:
        path.unlink(missing_ok=True)
        return {"deleted": True, "path": str(path)}
    except OSError as exc:
        return {"deleted": False, "reason": f"{type(exc).__name__}: {exc}", "path": str(path)}


def structure_reply_to_message_id(settings: Settings, store: JsonStore, signals: list[StructureSignal]) -> int | None:
    if not settings.structure_reply_chain_enable or len(signals) != 1:
        return None
    signal = signals[0]
    state = store.load(settings.structure_state_path, {})
    if not isinstance(state, dict):
        return None
    record = state.get(signal.symbol, {})
    if not isinstance(record, dict):
        symbols = state.get("symbols", {})
        record = symbols.get(signal.symbol, {}) if isinstance(symbols, dict) else {}
    if not isinstance(record, dict):
        return None
    try:
        message_id = int(record.get("last_message_id", 0) or 0)
    except (TypeError, ValueError):
        return None
    return message_id if message_id > 0 else None


def run_structure_review(args: argparse.Namespace) -> int:
    settings, store, _engine, gateway = make_runtime_for_args(args)
    if not settings.structure_review_enable:
        print("structure_review: disabled (STRUCTURE_REVIEW_ENABLE=false)")
        return 2
    review = StructureReviewEngine(settings, store)
    result = review.update(
        BinanceDataSource(settings),
        lookback_hours=args.lookback_hours or settings.structure_review_lookback_hours,
    )
    push = gateway.send(
        result["text"],
        result["template_id"],
        result["dedup_key"],
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=settings.structure_review_max_report_interval_sec,
        parse_mode="HTML",
    )
    print(f"structure_review_push: {push.status} ({push.reason})")
    print(json.dumps({
        "report_path": result["report_path"],
        "summary": result["stats"].get("summary", {}),
    }, ensure_ascii=False, indent=2))
    write_runtime_status(
        settings,
        store,
        command_mode(args),
        "completed",
        task="structure-review",
        real_send=bool(args.send and args.confirm_real_send),
        structure_review_push=push.status,
        report_path=result["report_path"],
        stats_summary=result["stats"].get("summary", {}),
    )
    return 0 if push.status != "failed" else 1


def run_structure_radar(args: argparse.Namespace) -> int:
    settings, store, _engine, gateway = make_runtime_for_args(args)
    if not settings.structure_radar_enable:
        print("structure_radar: disabled (STRUCTURE_RADAR_ENABLE=false)")
        return 2
    source = BinanceDataSource(settings)
    radar = StructureRadarEngine(settings, store)
    liquidity_enhancer = build_liquidity_enhancer(settings, source)
    result = radar.build(
        source,
        mode=args.mode,
        top_symbols=args.top_symbols,
        min_score=args.min_score,
        interval=str(args.interval) if args.interval and not str(args.interval).isdigit() else settings.structure_interval,
        save_charts=True if args.save_charts else settings.structure_save_charts,
        liquidity_enhancer=liquidity_enhancer,
    )
    report_path = settings.data_dir / "structure_report.txt"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(result["text"], encoding="utf-8")
    signal_objects = result.get("signal_objects") or []
    reply_to_message_id = structure_reply_to_message_id(settings, store, signal_objects)
    push = gateway.send(
        result["text"],
        result["template_id"],
        result["dedup_key"],
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        cooldown_sec=settings.structure_cooldown_sec,
        parse_mode="HTML",
        reply_to_message_id=reply_to_message_id,
    )
    print(f"structure_push: {push.status} ({push.reason})")

    sent_signals: list[StructureSignal] = []
    if push.status == "sent":
        sent_signals.extend(signal_objects)
    review_recorded = 0
    if settings.structure_review_enable:
        review_recorded = StructureReviewEngine(settings, store).record_signals(
            signal_objects,
            mode=str(result.get("mode") or args.mode),
            window=result.get("window") if isinstance(result.get("window"), dict) else {},
            push_status=push.status,
        )
    photo_count = 0
    chart_delete_results: list[dict[str, object]] = []
    for idx, signal in enumerate((result.get("signal_objects") or [])[: settings.structure_send_chart_top_n], start=1):
        if not signal.chart_path:
            continue
        photo = gateway.send_photo(
            signal.chart_path,
            structure_photo_caption(signal),
            result["template_id"],
            f"structure-chart:{signal.symbol}:{signal.signal_type}:{Path(signal.chart_path).stem}",
            send=args.send,
            confirm_real_send=args.confirm_real_send,
            cooldown_sec=settings.structure_cooldown_sec,
            parse_mode="HTML",
        )
        print(f"structure_photo[{idx}]: {photo.status} ({photo.reason})")
        if photo.status in {"sent", "dry_run"}:
            photo_count += 1
        delete_result = delete_chart_after_success(settings, photo, signal.chart_path)
        if delete_result.get("deleted"):
            print(f"structure_chart_delete[{idx}]: deleted")
        elif delete_result.get("reason") not in {"disabled", "not_sent"}:
            print(f"structure_chart_delete[{idx}]: skipped ({delete_result.get('reason')})")
        chart_delete_results.append(delete_result)
    if sent_signals:
        radar.mark_pushed(sent_signals, push.message_ids or [])
    chart_cleanup = cleanup_structure_charts(
        settings.structure_chart_dir,
        settings.structure_chart_retention_hours,
        settings.structure_max_chart_files,
    )
    print(json.dumps({
        "report_path": str(report_path),
        "chart_paths": result.get("chart_paths", []),
        "photo_count": photo_count,
        "reply_to_message_id": reply_to_message_id or 0,
        "review_recorded": review_recorded,
        "chart_delete_results": chart_delete_results,
        "chart_cleanup": chart_cleanup,
        "diagnostics": result.get("diagnostics", {}),
    }, ensure_ascii=False, indent=2))
    write_runtime_status(
        settings,
        store,
        command_mode(args),
        "completed",
        task="structure-radar",
        real_send=bool(args.send and args.confirm_real_send),
        structure_mode=args.mode,
        structure_interval=settings.structure_interval,
        structure_push=push.status,
        structure_reply_to_message_id=reply_to_message_id or 0,
        structure_signals=len(result.get("signals", [])),
        structure_review_recorded=review_recorded,
        report_path=str(report_path),
        chart_paths=result.get("chart_paths", []),
        chart_cleanup=chart_cleanup,
        diagnostics={"structure": result.get("diagnostics", {})},
    )
    return 0


def run_structure_loop(args: argparse.Namespace) -> int:
    settings, store, _engine, _gateway = make_runtime_for_args(args)
    mode = command_mode(args)
    next_pre = next_structure_pre_epoch(time.time(), settings.structure_pre_scan_minute)
    next_confirm = next_structure_confirm_epoch(time.time(), settings.structure_confirm_delay_sec)
    write_runtime_status(
        settings,
        store,
        mode,
        "running",
        task="structure-loop",
        real_send=bool(args.send and args.confirm_real_send),
        next_pre_at=timestamp_from_epoch(next_pre),
        next_confirm_at=timestamp_from_epoch(next_confirm),
        structure_interval=settings.structure_interval,
    )
    while True:
        now = time.time()
        print(
            "[structure-loop] next pre="
            f"{timestamp_from_epoch(next_pre)} | next confirm={timestamp_from_epoch(next_confirm)}",
            flush=True,
        )
        if now >= next_pre:
            try:
                run_structure_radar(argparse.Namespace(**{**vars(args), "mode": "pre"}))
            except Exception as exc:
                print(f"[structure-loop] pre failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            next_pre = next_structure_pre_epoch(time.time(), settings.structure_pre_scan_minute)
        if now >= next_confirm:
            try:
                run_structure_radar(argparse.Namespace(**{**vars(args), "mode": "confirm"}))
                if settings.structure_review_enable:
                    run_structure_review(argparse.Namespace(**{
                        **vars(args),
                        "lookback_hours": settings.structure_review_lookback_hours,
                    }))
            except Exception as exc:
                print(f"[structure-loop] confirm failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            next_confirm = next_structure_confirm_epoch(time.time(), settings.structure_confirm_delay_sec)
        write_runtime_status(
            settings,
            store,
            mode,
            "running",
            task="structure-loop",
            real_send=bool(args.send and args.confirm_real_send),
            next_pre_at=timestamp_from_epoch(next_pre),
            next_confirm_at=timestamp_from_epoch(next_confirm),
            structure_interval=settings.structure_interval,
        )
        time.sleep(15)


def print_readiness(settings: Settings, store: JsonStore) -> int:
    records = store.load(settings.launch_watch_history_path, [])
    record_count = len(records) if isinstance(records, list) else 0
    report = build_launch_report(records[-100:] if isinstance(records, list) else [], settings)
    checks = [
        *telegram_config_checks(settings),
        ("observe_history", record_count >= 5, f"启动观察历史 {record_count} 轮"),
        ("launch_alert_pressure", int(report.get("total_alerts", 0) or 0) <= max(1, int(report.get("records", 0) or 0)), f"最近推送候选 {report.get('total_alerts', 0)} / {report.get('records', 0)} 轮"),
        ("history_file", settings.launch_watch_history_path.exists(), "启动观察历史文件存在" if settings.launch_watch_history_path.exists() else "启动观察历史文件不存在"),
    ]
    passed = sum(1 for _name, ok, _message in checks if ok)
    print(f"真实推送准备度: {passed}/{len(checks)}")
    for name, ok, message in checks:
        mark = "OK" if ok else "WAIT"
        print(f"- {mark} {name}: {message}")
    print("")
    print(format_launch_report(settings, store, 100, 8))
    if passed == len(checks):
        print("")
        print("下一步: 可以先运行 python main.py telegram-test --send --confirm-real-send 验证 Telegram。")
        return 0
    print("")
    print("下一步: 先继续 dry-run observe，或补齐 Telegram 配置。")
    return 1


def require_real_send_gate(settings: Settings, store: JsonStore, args: argparse.Namespace) -> int:
    if not args.send or not args.confirm_real_send:
        print("真实推送已阻止：必须同时提供 --send --confirm-real-send。")
        return 2
    readiness = print_readiness(settings, store)
    if readiness != 0:
        print("真实推送已阻止：readiness 未通过。")
        return 2
    return 0


def print_watchlist(settings: Settings, store: JsonStore, top_n: int) -> None:
    data = store.load(settings.launch_watchlist_path, {})
    if not isinstance(data, dict) or not data.get("items"):
        print("暂无启动候选记录。先运行：python main.py once --no-announcements")
        return
    items = data.get("items", [])
    if not isinstance(items, list):
        print("启动候选记录格式异常。")
        return
    print(f"启动候选观察表 | 更新时间: {data.get('updated_at', 'unknown')} | 数量: {data.get('count', len(items))}")
    for idx, item in enumerate(items[:max(1, top_n)], start=1):
        reasons = "；".join(item.get("reasons") or []) or "无触发项"
        print(
            f"{idx:02d}. {item.get('symbol', ''):<12} "
            f"{int(item.get('score', 0)):>3}分 | "
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
        print(
            f"{idx:02d}. {record.get('updated_at', 'unknown')} | "
            f"扫描{int(record.get('scanned', 0))} | "
            f"最高{int(record.get('top_score', 0))}分 | "
            f"推送候选{int(record.get('alert_count', 0))} | "
            f"观察{int(buckets.get('watching', 0))}/预警{int(buckets.get('primed', 0))}/确认{int(buckets.get('breakout', 0))}/瞬间{int(buckets.get('launched', 0))} | "
            f"{top_symbols}"
        )


def build_launch_report(records: list[dict[str, object]], settings: Settings) -> dict[str, object]:
    valid = [record for record in records if isinstance(record, dict)]
    top_scores = [int(record.get("top_score", 0) or 0) for record in valid]
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

    max_top_score = max(top_scores) if top_scores else 0
    avg_top_score = round(sum(top_scores) / len(top_scores), 2) if top_scores else 0
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


def format_launch_report(settings: Settings, store: JsonStore, record_limit: int, top_n: int) -> str:
    records = store.load(settings.launch_watch_history_path, [])
    if not isinstance(records, list) or not records:
        return "暂无启动观察历史。先运行：python main.py trial --cycles 1"
    selected = records[-max(1, record_limit):]
    report = build_launch_report(selected, settings)
    buckets = report["buckets"] if isinstance(report["buckets"], dict) else {}
    lines = [
        f"启动历史分析 | 最近{report['records']}轮",
        f"扫描合计: {report['total_scanned']} | 推送候选: {report['total_alerts']}",
        f"最高分: {report['max_top_score']} | 平均最高分: {report['avg_top_score']}",
        (
            "阶段合计: "
            f"观察{int(buckets.get('watching', 0))} / "
            f"预警{int(buckets.get('primed', 0))} / "
            f"确认{int(buckets.get('breakout', 0))} / "
            f"瞬间{int(buckets.get('launched', 0))}"
        ),
    ]
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


def run_once(args: argparse.Namespace) -> int:
    settings, store, engine, gateway = make_runtime_for_args(args)
    mode = command_mode(args)
    write_runtime_status(
        settings,
        store,
        mode,
        "running",
        task="once",
        real_send=bool(args.send and args.confirm_real_send),
        no_launch=bool(args.no_launch),
        no_announcements=bool(args.no_announcements),
        no_flow=bool(args.no_flow),
        radar_scan_limit=settings.radar_scan_limit,
        launch_scan_limit=settings.launch_scan_limit,
        flow_scan_limit=settings.flow_scan_limit,
    )
    if not args.no_announcements:
        delete_callback = gateway.delete_messages if args.send and args.confirm_real_send else None
        cleanup_result = engine.cleanup_expired_announcements(delete_callback)
        if cleanup_result.get("expired"):
            print(
                "announcement_cleanup: "
                f"expired={cleanup_result.get('expired', 0)} "
                f"deleted_messages={cleanup_result.get('deleted_messages', 0)}"
            )
    result = engine.run_once(
        include_launch=not args.no_launch,
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
    )
    print(f"summary_push: {push.status} ({push.reason})")
    summary_push_status = push.status

    launch_pushes: list[dict[str, str]] = []
    if not args.no_launch:
        sent_launch_alerts = []
        for idx, message in enumerate(result["launch"]["messages"], start=1):
            alert = result["launch"]["alerts"][idx - 1]
            push = gateway.send(
                message,
                "TG_LAUNCH_ALERT",
                f"launch:{alert['symbol']}:{alert['stage']}",
                send=args.send,
                confirm_real_send=args.confirm_real_send,
                cooldown_sec=settings.launch_stage_cooldown_sec,
                parse_mode="HTML",
                reply_to_message_id=int(alert.get("reply_to_message_id", 0) or 0) or None,
            )
            print(f"launch_push[{idx}]: {push.status} ({push.reason})")
            launch_pushes.append({
                "symbol": str(alert.get("symbol", "")),
                "stage": str(alert.get("stage", "")),
                "reply_to": str(alert.get("reply_to_message_id") or ""),
                "status": push.status,
                "reason": push.reason,
            })
            if push.status == "sent":
                alert["message_ids"] = push.message_ids or []
                sent_launch_alerts.append(alert)
        engine.mark_launch_pushed(sent_launch_alerts)

    announcement_pushes: list[dict[str, str]] = []
    if not args.no_announcements:
        sent_announcements = []
        for idx, message in enumerate(result["announcements"]["messages"], start=1):
            alert = result["announcements"]["alerts"][idx - 1]
            cooldown = 24 * 3600 if alert["kind"] == "opportunity" else 6 * 3600
            push = gateway.send(
                message,
                "TG_ANNOUNCEMENT_ALERT",
                f"announcement:{alert['kind']}:{alert['code']}",
                send=args.send,
                confirm_real_send=args.confirm_real_send,
                cooldown_sec=cooldown,
                parse_mode="HTML",
            )
            print(f"announcement_push[{idx}]: {push.status} ({push.reason})")
            announcement_pushes.append({
                "kind": str(alert.get("kind", "")),
                "code": str(alert.get("code", "")),
                "status": push.status,
                "reason": push.reason,
            })
            if push.status == "sent":
                alert["message_ids"] = push.message_ids or []
                sent_announcements.append(alert)
        engine.mark_announcements_seen(sent_announcements)

    diagnostics = dict(result["diagnostics"])
    flow_push_status = "skipped"
    if not args.no_flow:
        flow_push_status, flow_diag = push_flow_radar(settings, gateway, args)
        diagnostics["flow"] = flow_diag
    funding_alert_push_status = "skipped"
    if not getattr(args, "no_funding_alert", False):
        funding_alert_push_status, funding_diag = push_funding_alert(settings, store, gateway, args)
        diagnostics["funding_alert"] = funding_diag

    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    write_runtime_status(
        settings,
        store,
        mode,
        "completed",
        task="once",
        real_send=bool(args.send and args.confirm_real_send),
        summary_push=summary_push_status,
        flow_push=flow_push_status,
        funding_alert_push=funding_alert_push_status,
        radar_scan_limit=settings.radar_scan_limit,
        launch_scan_limit=settings.launch_scan_limit,
        flow_scan_limit=settings.flow_scan_limit,
        funding_alert_scan_limit=settings.funding_alert_scan_limit,
        launch_pushes=launch_pushes,
        announcement_pushes=announcement_pushes,
        diagnostics=diagnostics,
    )
    return 0


def run_loop(args: argparse.Namespace) -> int:
    settings, store, _engine, _gateway = make_runtime_for_args(args)
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
    next_launch = 0.0
    next_flow = next_closed_window_epoch(
        time.time(),
        interval_sec=settings.flow_interval_sec,
        delay_sec=settings.flow_close_delay_sec,
    )
    next_funding_alert = time.time()
    lifecycle_interval = max(60, int(settings.lifecycle_scan_interval_sec or 900))
    next_lifecycle = time.time()
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
        lifecycle_tracker_enable=bool(settings.lifecycle_tracker_enable),
        lifecycle_scan_interval_sec=lifecycle_interval,
        summary_close_delay_sec=settings.radar_summary_close_delay_sec,
        flow_close_delay_sec=settings.flow_close_delay_sec,
        next_summary_at=timestamp_from_epoch(next_summary),
        next_flow_at=timestamp_from_epoch(next_flow),
        next_funding_alert_at=timestamp_from_epoch(next_funding_alert),
        next_lifecycle_at=timestamp_from_epoch(next_lifecycle) if settings.lifecycle_tracker_enable else "",
        no_launch=bool(args.no_launch),
        no_flow=bool(args.no_flow),
        no_funding_alert=bool(getattr(args, "no_funding_alert", False)),
        radar_scan_limit=settings.radar_scan_limit,
        launch_scan_limit=settings.launch_scan_limit,
        flow_scan_limit=settings.flow_scan_limit,
        funding_alert_scan_limit=settings.funding_alert_scan_limit,
    )
    while True:
        now = time.time()
        cleanup_runtime_artifacts(settings, store)
        if now >= next_summary:
            summary_ok = True
            summary_error = ""
            try:
                run_once(argparse.Namespace(**{**vars(args), "no_launch": True, "no_flow": True}))
            except Exception as exc:
                summary_ok = False
                summary_error = f"{type(exc).__name__}: {exc}"
                print(f"[loop] summary failed: {type(exc).__name__}: {exc}", file=sys.stderr)
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
                last_error=summary_error,
                no_launch=bool(args.no_launch),
                no_flow=bool(args.no_flow),
            )
        if (
            not args.no_flow
            and now >= next_flow
        ):
            flow_ok = True
            flow_error = ""
            flow_diag: dict[str, object] = {}
            flow_push_status = "skipped"
            try:
                settings, _store, _engine, gateway = make_runtime_for_args(args)
                flow_push_status, flow_diag = push_flow_radar(settings, gateway, args)
                print(json.dumps({"flow": flow_diag}, ensure_ascii=False, indent=2))
            except Exception as exc:
                flow_ok = False
                flow_error = f"{type(exc).__name__}: {exc}"
                print(f"[loop] flow failed: {type(exc).__name__}: {exc}", file=sys.stderr)
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
                diagnostics={"flow": flow_diag},
                last_error=flow_error,
            )
        if not getattr(args, "no_funding_alert", False) and now >= next_funding_alert:
            funding_ok = True
            funding_error = ""
            funding_diag: dict[str, object] = {}
            funding_push_status = "skipped"
            try:
                settings, store, _engine, gateway = make_runtime_for_args(args)
                funding_push_status, funding_diag = push_funding_alert(settings, store, gateway, args)
                print(json.dumps({"funding_alert": funding_diag}, ensure_ascii=False, indent=2))
            except Exception as exc:
                funding_ok = False
                funding_error = f"{type(exc).__name__}: {exc}"
                print(f"[loop] funding alert failed: {type(exc).__name__}: {exc}", file=sys.stderr)
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
                diagnostics={"funding_alert": funding_diag},
                last_error=funding_error,
            )
        if not args.no_launch and now >= next_launch:
            launch_ok = True
            launch_error = ""
            launch_pushes: list[dict[str, str]] = []
            launch_diag: dict[str, object] = {}
            try:
                settings, _store, engine, gateway = make_runtime_for_args(args)
                source = BinanceDataSource(settings)
                launch = engine.build_launch_alerts(source)
                sent_launch_alerts = []
                for idx, message in enumerate(launch["messages"], start=1):
                    alert = launch["alerts"][idx - 1]
                    push = gateway.send(
                        message,
                        "TG_LAUNCH_ALERT",
                        f"launch:{alert['symbol']}:{alert['stage']}",
                        send=args.send,
                        confirm_real_send=args.confirm_real_send,
                        cooldown_sec=settings.launch_stage_cooldown_sec,
                        parse_mode="HTML",
                        reply_to_message_id=int(alert.get("reply_to_message_id", 0) or 0) or None,
                    )
                    print(f"launch_push[{idx}]: {push.status} ({push.reason})")
                    launch_pushes.append({
                        "symbol": str(alert.get("symbol", "")),
                        "stage": str(alert.get("stage", "")),
                        "reply_to": str(alert.get("reply_to_message_id") or ""),
                        "status": push.status,
                        "reason": push.reason,
                    })
                    if push.status == "sent":
                        alert["message_ids"] = push.message_ids or []
                        sent_launch_alerts.append(alert)
                engine.mark_launch_pushed(sent_launch_alerts)
                launch_diag = source.diagnostics()
                print(json.dumps({"launch": launch_diag}, ensure_ascii=False, indent=2))
            except Exception as exc:
                launch_ok = False
                launch_error = f"{type(exc).__name__}: {exc}"
                print(f"[loop] launch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
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
                launch_pushes=launch_pushes,
                diagnostics={"launch": launch_diag},
                last_error=launch_error,
            )
        if bool(settings.lifecycle_tracker_enable) and now >= next_lifecycle:
            lifecycle_result = run_lifecycle_tracker_cycle(settings, args)
            lifecycle_ok = bool(lifecycle_result.get("ok", False))
            lifecycle_error = str(lifecycle_result.get("error") or "")
            lifecycle_counts = lifecycle_result.get("counts") if isinstance(lifecycle_result.get("counts"), dict) else {}
            print(json.dumps({"lifecycle": lifecycle_counts}, ensure_ascii=False, indent=2))
            lifecycle_interval = max(60, int(settings.lifecycle_scan_interval_sec or 900))
            next_lifecycle = time.time() + lifecycle_interval
            write_runtime_status(
                settings,
                store,
                mode,
                "running" if lifecycle_ok else "lifecycle_failed",
                task="loop",
                real_send=bool(args.send and args.confirm_real_send),
                last_lifecycle_at=timestamp_from_epoch(time.time()),
                next_lifecycle_at=timestamp_from_epoch(next_lifecycle),
                lifecycle_counts=lifecycle_counts,
                last_error=lifecycle_error,
            )
        time.sleep(3)


def run_trial(args: argparse.Namespace) -> int:
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
        sent_launch_alerts = []
        launch_pushes: list[dict[str, str]] = []
        for idx, message in enumerate(launch["messages"], start=1):
            alert = launch["alerts"][idx - 1]
            push = gateway.send(
                message,
                "TG_LAUNCH_ALERT",
                f"launch:{alert['symbol']}:{alert['stage']}",
                send=args.send,
                confirm_real_send=args.confirm_real_send,
                cooldown_sec=settings.launch_stage_cooldown_sec,
                parse_mode="HTML",
                reply_to_message_id=int(alert.get("reply_to_message_id", 0) or 0) or None,
            )
            print(f"launch_push[{idx}]: {push.status} ({push.reason})")
            launch_pushes.append({
                "symbol": str(alert.get("symbol", "")),
                "stage": str(alert.get("stage", "")),
                "reply_to": str(alert.get("reply_to_message_id") or ""),
                "status": push.status,
                "reason": push.reason,
            })
            if push.status == "sent":
                alert["message_ids"] = push.message_ids or []
                sent_launch_alerts.append(alert)
        engine.mark_launch_pushed(sent_launch_alerts)
        diagnostics = source.diagnostics()
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
                run_trial(cycle_args)
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
    if args.command == "web":
        from .web import run_web_server

        return run_web_server(args.host, args.port, args.web_token)
    if args.command == "ai-assistant":
        from .ai_assistant import run_ai_assistant_service

        return run_ai_assistant_service()
    if args.command == "stable-check":
        return print_stable_check(as_json=args.json, save=not args.no_save)
    if args.command == "admin-password":
        return run_admin_password(args)
    settings, store, _engine, _gateway = make_runtime()

    if args.command == "about":
        print(PROJECT_ABOUT)
        return 0
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
    if args.command == "telegram-test":
        return run_telegram_test(args)
    if args.command == "announcements-test":
        return run_announcements_test(args)
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
    if args.command == "structure-radar":
        return run_structure_radar(args)
    if args.command == "structure-loop":
        return run_structure_loop(args)
    if args.command == "structure-review":
        return run_structure_review(args)
    if args.command == "runtime-status":
        print_runtime_status(settings, store)
        return 0
    if args.command == "outcome-scan":
        return run_outcome_scan(args)
    if args.command == "lifecycle-backfill":
        return run_lifecycle_backfill(args)
    if args.command == "lifecycle-scan":
        return run_lifecycle_scan(args)
    if args.command == "lifecycle-status":
        return run_lifecycle_status(args)
    if args.command == "lifecycle-intelligence":
        return run_lifecycle_intelligence(args)
    if args.command == "lifecycle-replay":
        return run_lifecycle_replay(args)
    if args.command == "lifecycle-replay-backfill":
        return run_lifecycle_replay(args, backfill=True)
    if args.command == "lifecycle-analytics":
        return run_lifecycle_analytics(args)
    if args.command == "lifecycle-similar":
        return run_lifecycle_similar(args)
    if args.command == "lifecycle-outcome-link":
        return run_lifecycle_outcome_link(args)
    if args.command == "lifecycle-outcome-backfill":
        return run_lifecycle_outcome_backfill(args)
    if args.command == "lifecycle-outcome-status":
        return run_lifecycle_outcome_status(args)
    if args.command == "lifecycle-outcome-reconcile":
        return run_lifecycle_outcome_reconcile(args)
    if args.command == "price-alerts":
        from .ai_assistant import price_alerts_payload

        print(json.dumps(price_alerts_payload(settings), ensure_ascii=False, indent=2))
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
    if args.command == "migrate-state":
        print(json.dumps(migrate_legacy_state(settings, apply=args.apply), ensure_ascii=False, indent=2))
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
