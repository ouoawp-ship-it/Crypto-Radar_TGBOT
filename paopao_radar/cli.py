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
- 资金雷达汇总：30 分钟一次。
- 启动雷达提醒：3 分钟扫描一次。
- 公告机会/风险：跟随主扫描。
- 同币同阶段启动提醒：默认 6 小时冷却。
"""

import argparse
import json
import re
import sys
import time
from pathlib import Path
from collections import Counter
from dataclasses import replace
from datetime import datetime

from .config import Settings
from .data_sources import BinanceDataSource, CoinglassDataSource
from .flow_radar import FlowRadarEngine
from .maintenance import cleanup_runtime_artifacts, legacy_state_report, migrate_legacy_state
from .radar import RadarEngine
from .storage import JsonStore
from .telegram import TelegramGateway


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
- 资金雷达汇总：30 分钟一次，可用 --interval 或 RADAR_SUMMARY_MIN_INTERVAL_SEC 调整。
- 启动雷达扫描：3 分钟一次，可用 --launch-interval 调整。
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
        choices=["about", "status", "doctor", "readiness", "telegram-test", "coinglass-test", "flow-radar", "runtime-status", "cleanup", "watchlist", "launch-history", "launch-report", "migrate-state", "once", "trial", "observe", "loop", "daemon", "live"],
        help="默认 status；about 查看功能说明；doctor 检查环境；cleanup 清理运行垃圾；readiness 检查真实推送准备度；coinglass-test 验证 CoinGlass；flow-radar 扫描五因子资金流；once 扫描一轮；observe dry-run 观察；loop/daemon 持续运行；live 通过门禁后真实推送",
    )
    parser.add_argument("--send", action="store_true", help="允许真实发送 Telegram；仍需要 --confirm-real-send")
    parser.add_argument("--confirm-real-send", action="store_true", help="确认真实发送 Telegram")
    parser.add_argument("--apply", action="store_true", help="用于 migrate-state：真正复制旧状态文件")
    parser.add_argument("--force-cleanup", action="store_true", help="用于 cleanup：忽略清理间隔，立即执行")
    parser.add_argument("--top", type=int, default=12, help="用于 watchlist/报告：显示前 N 个候选")
    parser.add_argument("--records", type=int, default=100, help="用于 launch-report：统计最近 N 轮")
    parser.add_argument("--cycles", type=int, default=3, help="用于 trial：试跑轮数")
    parser.add_argument("--duration-minutes", type=int, default=360, help="用于 observe：观察总时长分钟数")
    parser.add_argument("--interval", type=int, default=1800, help="loop/daemon 的资金雷达摘要间隔秒数")
    parser.add_argument("--launch-interval", type=int, default=180, help="loop/daemon 的启动雷达间隔秒数")
    parser.add_argument("--radar-scan-limit", type=int, default=None, help="临时覆盖资金雷达扫描上限")
    parser.add_argument("--launch-scan-limit", type=int, default=None, help="临时覆盖启动雷达扫描上限")
    parser.add_argument("--flow-scan-limit", type=int, default=None, help="临时覆盖五因子资金流雷达扫描上限")
    parser.add_argument("--no-launch", action="store_true", help="本轮不运行启动雷达")
    parser.add_argument("--no-announcements", action="store_true", help="本轮不扫描公告机会/风险")
    parser.add_argument("--no-flow", action="store_true", help="本轮不运行五因子资金流雷达")
    return parser


def make_runtime() -> tuple[Settings, JsonStore, RadarEngine, TelegramGateway]:
    settings = Settings.load()
    store = JsonStore(settings.data_dir)
    engine = RadarEngine(settings, store)
    gateway = TelegramGateway(settings, store)
    return settings, store, engine, gateway


def apply_cli_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    updates: dict[str, int] = {}
    radar_scan_limit = getattr(args, "radar_scan_limit", None)
    launch_scan_limit = getattr(args, "launch_scan_limit", None)
    flow_scan_limit = getattr(args, "flow_scan_limit", None)
    if radar_scan_limit is not None:
        updates["radar_scan_limit"] = max(0, int(radar_scan_limit))
    if launch_scan_limit is not None:
        updates["launch_scan_limit"] = max(0, int(launch_scan_limit))
    if flow_scan_limit is not None:
        updates["flow_scan_limit"] = max(0, int(flow_scan_limit))
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
        settings.runtime_status_path,
        settings.radar_state_path,
        settings.funding_snapshot_path,
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
    payload: dict[str, object] = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "mode": mode,
        "status": status,
    }
    payload.update(details)
    try:
        store.save(settings.runtime_status_path, payload)
    except Exception as exc:
        print(f"[runtime-status] write failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return payload


def print_runtime_status(settings: Settings, store: JsonStore) -> None:
    data = store.load(settings.runtime_status_path, {})
    if not isinstance(data, dict) or not data:
        data = {
            "status": "empty",
            "path": str(settings.runtime_status_path),
            "message": "runtime status has not been written yet",
        }
    print(json.dumps(data, ensure_ascii=False, indent=2))


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


def run_coinglass_test(_args: argparse.Namespace) -> int:
    settings, _store, _engine, _gateway = make_runtime_for_args(_args)
    if not settings.coinglass_enable:
        print("coinglass_test: blocked (COINGLASS_ENABLE=false)")
        return 2
    if not settings.coinglass_api_key:
        print("coinglass_test: blocked (missing COINGLASS_API_KEY)")
        return 2

    source = CoinglassDataSource(settings)
    data = source.open_interest_exchange_list("BTC")
    ok = data is not None
    print(f"coinglass_test: {'ok' if ok else 'failed'}")
    print(json.dumps(source.diagnostics(), ensure_ascii=False, indent=2))
    if isinstance(data, list):
        print(f"sample_items: {len(data)}")
    elif isinstance(data, dict):
        print(f"sample_keys: {', '.join(list(data.keys())[:8])}")
    return 0 if ok else 1


def run_flow_radar(args: argparse.Namespace) -> int:
    settings, _store, _engine, gateway = make_runtime_for_args(args)
    flow = FlowRadarEngine(settings).build(
        BinanceDataSource(settings),
        CoinglassDataSource(settings),
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
        CoinglassDataSource(settings),
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
            )
            print(f"launch_push[{idx}]: {push.status} ({push.reason})")
            launch_pushes.append({
                "symbol": str(alert.get("symbol", "")),
                "stage": str(alert.get("stage", "")),
                "status": push.status,
                "reason": push.reason,
            })
            if push.status == "sent":
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
    if (
        not args.no_flow
        and settings.coinglass_enable
        and bool(settings.coinglass_api_key)
    ):
        flow_push_status, flow_diag = push_flow_radar(settings, gateway, args)
        diagnostics["flow"] = flow_diag

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
        radar_scan_limit=settings.radar_scan_limit,
        launch_scan_limit=settings.launch_scan_limit,
        flow_scan_limit=settings.flow_scan_limit,
        launch_pushes=launch_pushes,
        announcement_pushes=announcement_pushes,
        diagnostics=diagnostics,
    )
    return 0


def run_loop(args: argparse.Namespace) -> int:
    settings, store, _engine, _gateway = make_runtime_for_args(args)
    mode = command_mode(args)
    next_summary = 0.0
    next_launch = 0.0
    next_flow = 0.0
    write_runtime_status(
        settings,
        store,
        mode,
        "running",
        task="loop",
        real_send=bool(args.send and args.confirm_real_send),
        interval_sec=max(60, args.interval),
        launch_interval_sec=max(60, args.launch_interval),
        flow_interval_sec=max(60, settings.flow_interval_sec),
        no_launch=bool(args.no_launch),
        no_flow=bool(args.no_flow),
        radar_scan_limit=settings.radar_scan_limit,
        launch_scan_limit=settings.launch_scan_limit,
        flow_scan_limit=settings.flow_scan_limit,
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
            next_summary = time.time() + max(60, args.interval)
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
            and settings.coinglass_enable
            and bool(settings.coinglass_api_key)
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
            next_flow = time.time() + max(60, settings.flow_interval_sec)
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
                    )
                    print(f"launch_push[{idx}]: {push.status} ({push.reason})")
                    launch_pushes.append({
                        "symbol": str(alert.get("symbol", "")),
                        "stage": str(alert.get("stage", "")),
                        "status": push.status,
                        "reason": push.reason,
                    })
                    if push.status == "sent":
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
            )
            print(f"launch_push[{idx}]: {push.status} ({push.reason})")
            launch_pushes.append({
                "symbol": str(alert.get("symbol", "")),
                "stage": str(alert.get("stage", "")),
                "status": push.status,
                "reason": push.reason,
            })
            if push.status == "sent":
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
    if args.command == "coinglass-test":
        return run_coinglass_test(args)
    if args.command == "flow-radar":
        if args.send and args.confirm_real_send:
            gate = require_real_send_gate(settings, store, args)
            if gate != 0:
                return gate
        return run_flow_radar(args)
    if args.command == "runtime-status":
        print_runtime_status(settings, store)
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
