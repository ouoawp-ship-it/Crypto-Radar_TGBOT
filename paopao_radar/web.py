from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import BASE_DIR, ENV_FILE, Settings, load_env_file
from .storage import JsonStore


MAIN_SERVICE = os.getenv("SERVICE_NAME", "paopao-radar")
STRUCTURE_SERVICE = os.getenv("STRUCTURE_SERVICE_NAME", "paopao-structure")
WEB_SERVICE = os.getenv("WEB_SERVICE_NAME", "paopao-web")
WEB_CONFIG_KEYS = {"WEB_HOST", "WEB_PORT", "WEB_ADMIN_TOKEN"}


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    section: str
    kind: str = "text"
    secret: bool = False
    minimum: float | None = None
    maximum: float | None = None
    help: str = ""


EDITABLE_CONFIG_FIELDS: tuple[ConfigField, ...] = (
    ConfigField("TG_BOT_TOKEN", "机器人 Token", "Telegram", secret=True),
    ConfigField("TG_CHAT_ID", "群 ID / 频道用户名", "Telegram"),
    ConfigField("TELEGRAM_USE_TOPIC", "启用 Telegram 话题", "Telegram", kind="bool"),
    ConfigField("TG_AUTO_CREATE_TOPICS", "自动创建话题", "Telegram", kind="bool"),
    ConfigField("TG_TOPIC_ID", "默认话题 ID", "Telegram"),
    ConfigField("TG_RADAR_SUMMARY_TOPIC_ID", "资金摘要话题 ID", "Telegram"),
    ConfigField("TG_LAUNCH_ALERT_TOPIC_ID", "启动预警话题 ID", "Telegram"),
    ConfigField("TG_ANNOUNCEMENT_ALERT_TOPIC_ID", "公告话题 ID", "Telegram"),
    ConfigField("TG_TEST_TOPIC_ID", "测试消息话题 ID", "Telegram"),
    ConfigField("TG_FLOW_RADAR_TOPIC_ID", "资金流话题 ID", "Telegram"),
    ConfigField("STRUCTURE_TOPIC_ID", "结构雷达话题 ID", "Telegram"),
    ConfigField("STRUCTURE_REVIEW_TOPIC_ID", "结构复盘话题 ID", "Telegram"),
    ConfigField("WEB_HOST", "Web 监听地址", "Web 控制台"),
    ConfigField("WEB_PORT", "Web 端口", "Web 控制台", kind="int", minimum=1, maximum=65535),
    ConfigField("WEB_ADMIN_TOKEN", "Web 访问令牌", "Web 控制台", secret=True),
    ConfigField("COINALYZE_ENABLE", "启用 Coinalyze", "Coinalyze", kind="bool"),
    ConfigField("COINALYZE_API_KEY", "Coinalyze API Key", "Coinalyze", secret=True),
    ConfigField("RADAR_SUMMARY_MIN_INTERVAL_SEC", "资金摘要间隔秒", "雷达参数", kind="int", minimum=300),
    ConfigField("FLOW_INTERVAL_SEC", "资金流窗口秒", "雷达参数", kind="int", minimum=300),
    ConfigField("FLOW_SCAN_LIMIT", "资金流扫描数量", "雷达参数", kind="int", minimum=1, maximum=300),
    ConfigField("STRUCTURE_TOP_SYMBOLS", "结构雷达扫描数量", "雷达参数", kind="int", minimum=1, maximum=300),
    ConfigField(
        "STRUCTURE_NEAR_EDGE_PCT",
        "结构临界距离 %",
        "雷达参数",
        kind="float",
        minimum=0.1,
        maximum=10,
        help="结构临近突破/跌破的边缘距离。降低会减少临界观察信号，提高会放宽临界信号。",
    ),
    ConfigField(
        "STRUCTURE_MIN_SCORE",
        "结构雷达最低分",
        "雷达参数",
        kind="int",
        minimum=0,
        maximum=100,
        help="对应复盘建议里的 STRUCTURE_MIN_SCORE。提高会减少低分结构信号和假突破，降低会增加信号数量。",
    ),
    ConfigField(
        "STRUCTURE_SEND_CHART_TOP_N",
        "结构图发送数量",
        "雷达参数",
        kind="int",
        minimum=0,
        maximum=20,
        help="对应复盘建议里的 STRUCTURE_SEND_CHART_TOP_N。每轮最多给前 N 个结构信号发送 K 线图；设为 0 表示不发结构图。",
    ),
    ConfigField(
        "STRUCTURE_COOLDOWN_SEC",
        "同币冷却秒数",
        "雷达参数",
        kind="int",
        minimum=0,
        maximum=86400,
        help="同一个币种结构信号的冷却时间。提高会减少同币重复推送。",
    ),
    ConfigField("LIQUIDITY_SCORE_MAX_DELTA", "外部确认修正上限", "雷达参数", kind="int", minimum=0, maximum=30),
    ConfigField("LIQUIDITY_MIN_DISTANCE_PCT", "盘口墙最小距离 %", "雷达参数", kind="float", minimum=0),
    ConfigField("LIQUIDITY_MAX_DISTANCE_PCT", "盘口墙最大距离 %", "雷达参数", kind="float", minimum=0.1),
    ConfigField("BINANCE_ORDERBOOK_DEPTH_LIMIT", "Binance 盘口档位", "雷达参数", kind="int", minimum=5, maximum=1000),
)
EDITABLE_CONFIG: dict[str, ConfigField] = {field.key: field for field in EDITABLE_CONFIG_FIELDS}

TOPIC_FIELD_ROUTES: dict[str, tuple[str, str]] = {
    "TG_RADAR_SUMMARY_TOPIC_ID": ("TG_RADAR_SUMMARY", "资金摘要"),
    "TG_LAUNCH_ALERT_TOPIC_ID": ("TG_LAUNCH_ALERT", "启动预警"),
    "TG_ANNOUNCEMENT_ALERT_TOPIC_ID": ("TG_ANNOUNCEMENT_ALERT", "公告风险"),
    "TG_TEST_TOPIC_ID": ("TG_TEST_MESSAGE", "测试消息"),
    "TG_FLOW_RADAR_TOPIC_ID": ("TG_FLOW_RADAR", "资金流雷达"),
    "STRUCTURE_TOPIC_ID": ("TG_STRUCTURE_RADAR", "结构突破"),
    "STRUCTURE_REVIEW_TOPIC_ID": ("TG_STRUCTURE_REVIEW", "结构复盘"),
}

TOPIC_SUMMARY_ROUTE_KEYS: dict[str, str] = {
    "TG_RADAR_SUMMARY": "radar_summary",
    "TG_LAUNCH_ALERT": "launch_alert",
    "TG_ANNOUNCEMENT_ALERT": "announcement_alert",
    "TG_TEST_MESSAGE": "test",
    "TG_FLOW_RADAR": "flow_radar",
    "TG_STRUCTURE_RADAR": "structure_radar",
    "TG_STRUCTURE_REVIEW": "structure_review",
}


CLI_ACTIONS: dict[str, dict[str, Any]] = {
    "telegram-test": {
        "label": "发送 Telegram 测试消息",
        "argv": ["telegram-test", "--send", "--confirm-real-send"],
        "timeout": 60,
        "danger": True,
    },
    "readiness": {"label": "检查真实推送准备度", "argv": ["readiness"], "timeout": 45},
    "doctor": {"label": "环境诊断", "argv": ["doctor"], "timeout": 45},
    "runtime-status": {"label": "查看运行状态", "argv": ["runtime-status"], "timeout": 20},
    "announcements-test": {"label": "测试 Binance 公告", "argv": ["announcements-test"], "timeout": 90},
    "structure-review": {"label": "结构信号复盘", "argv": ["structure-review"], "timeout": 120},
    "cleanup": {"label": "立即清理运行垃圾", "argv": ["cleanup", "--force-cleanup"], "timeout": 60},
}

SERVICE_ACTIONS: dict[str, tuple[str, str]] = {
    "restart-main": (MAIN_SERVICE, "restart"),
    "start-main": (MAIN_SERVICE, "start"),
    "stop-main": (MAIN_SERVICE, "stop"),
    "restart-structure": (STRUCTURE_SERVICE, "restart"),
    "start-structure": (STRUCTURE_SERVICE, "start"),
    "stop-structure": (STRUCTURE_SERVICE, "stop"),
    "restart-web": (WEB_SERVICE, "restart"),
    "start-web": (WEB_SERVICE, "start"),
    "stop-web": (WEB_SERVICE, "stop"),
}


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def read_text_file(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8-sig", errors="ignore")


def read_env_values(path: Path | None = None) -> dict[str, str]:
    env_path = path or ENV_FILE
    values: dict[str, str] = {}
    for raw_line in read_text_file(env_path).splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "****"
    return f"{value[:4]}...{value[-4:]}"


def default_topic_routes_path(env_path: Path, values: dict[str, str]) -> Path:
    configured = values.get("TG_TOPIC_ROUTES_FILE", "").strip()
    if configured:
        route_path = Path(configured)
        if route_path.is_absolute():
            return route_path
        if route_path.parts and route_path.parts[0].lower() == "data":
            return BASE_DIR / route_path
        return env_path.parent / "data" / route_path
    if env_path.resolve() == ENV_FILE.resolve():
        return BASE_DIR / "data" / "tg_topic_routes.json"
    return env_path.parent / "data" / "tg_topic_routes.json"


def read_topic_routes(path: Path) -> dict[str, dict[str, str]]:
    data = load_json_or_empty(path)
    if not isinstance(data, dict):
        return {}
    routes = data.get("routes", {})
    if not isinstance(routes, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for template_id, record in routes.items():
        if not isinstance(record, dict):
            continue
        topic_id = str(record.get("topic_id") or "").strip()
        if not topic_id:
            continue
        result[str(template_id)] = {
            "topic_id": topic_id,
            "name": str(record.get("name") or "").strip(),
        }
    return result


def config_payload(path: Path | None = None, topic_routes_path: Path | None = None) -> dict[str, Any]:
    env_path = path or ENV_FILE
    values = read_env_values(env_path)
    routes_path = topic_routes_path or default_topic_routes_path(env_path, values)
    topic_routes = read_topic_routes(routes_path)
    sections: dict[str, list[dict[str, Any]]] = {}
    for field in EDITABLE_CONFIG_FIELDS:
        raw_value = values.get(field.key, "")
        display_value = raw_value
        source = "env" if raw_value else ""
        route_name = ""
        route = TOPIC_FIELD_ROUTES.get(field.key)
        if not raw_value and route is not None:
            template_id, default_name = route
            saved_route = topic_routes.get(template_id, {})
            saved_topic_id = saved_route.get("topic_id", "")
            if saved_topic_id:
                display_value = saved_topic_id
                source = "auto_route"
                route_name = saved_route.get("name") or default_name
        item = {
            "key": field.key,
            "label": field.label,
            "kind": field.kind,
            "secret": field.secret,
            "configured": bool(raw_value or display_value),
            "value": raw_value,
            "display_value": display_value,
            "masked": mask_secret(raw_value) if field.secret else "",
            "source": source,
            "route_name": route_name,
            "minimum": field.minimum,
            "maximum": field.maximum,
            "help": field.help,
        }
        sections.setdefault(field.section, []).append(item)
    return {
        "env_file": str(env_path),
        "topic_routes_file": str(routes_path),
        "topic_routes_found": bool(topic_routes),
        "sections": sections,
    }


def normalize_bool(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "y"}:
        return "true"
    if text in {"0", "false", "no", "off", "n"}:
        return "false"
    raise ValueError("必须选择开启或关闭")


def validate_config_value(field: ConfigField, value: Any) -> str:
    text = str(value).strip()
    if len(text) > 500:
        raise ValueError("值太长")
    if field.kind == "bool":
        return normalize_bool(value)
    if field.kind == "int":
        try:
            number = int(text)
        except ValueError as exc:
            raise ValueError("必须是整数") from exc
        if field.minimum is not None and number < field.minimum:
            raise ValueError(f"不能小于 {field.minimum:g}")
        if field.maximum is not None and number > field.maximum:
            raise ValueError(f"不能大于 {field.maximum:g}")
        return str(number)
    if field.kind == "float":
        try:
            number = float(text)
        except ValueError as exc:
            raise ValueError("必须是数字") from exc
        if field.minimum is not None and number < field.minimum:
            raise ValueError(f"不能小于 {field.minimum:g}")
        if field.maximum is not None and number > field.maximum:
            raise ValueError(f"不能大于 {field.maximum:g}")
        return f"{number:g}"
    if field.key.endswith("_TOPIC_ID") or field.key == "TG_TOPIC_ID":
        if text and not text.isdigit():
            raise ValueError("话题 ID 必须是数字")
    if field.key == "TG_CHAT_ID":
        if text and not (text.startswith("@") or text.lstrip("-").isdigit()):
            raise ValueError("群 ID 应该是 -100... 或 @channel_username")
    return text


def backup_env_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = path.with_name(f"{path.name}.bak.web.{time.strftime('%Y%m%d_%H%M%S')}")
    index = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak.web.{time.strftime('%Y%m%d_%H%M%S')}.{index}")
        index += 1
    shutil.copy2(path, backup)
    return backup


def env_backup_payload(path: Path | None = None, *, limit: int = 20) -> dict[str, Any]:
    env_path = path or ENV_FILE
    backups: list[dict[str, Any]] = []
    candidates: list[tuple[float, Path, os.stat_result]] = []
    for backup in env_path.parent.glob(f"{env_path.name}.bak.web.*"):
        try:
            stat = backup.stat()
        except OSError:
            continue
        candidates.append((stat.st_mtime, backup, stat))
    for _, backup, stat in sorted(candidates, key=lambda item: item[0], reverse=True):
        backups.append(
            {
                "name": backup.name,
                "path": str(backup),
                "size": stat.st_size,
                "modified_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
            }
        )
        if len(backups) >= limit:
            break
    return {"env_file": str(env_path), "backups": backups}


def restore_env_backup(name: str, *, path: Path | None = None) -> dict[str, Any]:
    env_path = path or ENV_FILE
    safe_name = Path(name).name
    backup = env_path.with_name(safe_name)
    if safe_name != name or backup.parent.resolve() != env_path.parent.resolve():
        return {"ok": False, "error": "备份文件名不合法"}
    if not safe_name.startswith(f"{env_path.name}.bak.web."):
        return {"ok": False, "error": "只能恢复 Web 自动创建的 .env.oi 备份"}
    if not backup.exists() or not backup.is_file():
        return {"ok": False, "error": "备份文件不存在"}

    before = read_env_values(env_path)
    current_backup = backup_env_file(env_path)
    shutil.copy2(backup, env_path)
    load_env_file(env_path)
    after = read_env_values(env_path)
    changed = sorted(key for key in set(before) | set(after) if before.get(key, "") != after.get(key, ""))
    return {
        "ok": True,
        "restored": backup.name,
        "backup": str(current_backup) if current_backup else "",
        "changed": changed,
        "message": "配置备份已恢复，正在自动应用",
    }


def structure_review_recommendations_payload(path: Path | None = None) -> dict[str, Any]:
    settings = Settings.load()
    stats_path = path or settings.structure_stats_path
    stats = load_json_or_empty(stats_path)
    if not isinstance(stats, dict):
        return {"ok": False, "stats_file": str(stats_path), "recommendations": [], "updates": {}, "message": "结构复盘统计文件不可读"}

    summary = stats.get("summary", {}) if isinstance(stats.get("summary"), dict) else {}
    total = int(summary.get("total", 0) or 0)
    reviewed = int(summary.get("reviewed", 0) or 0)
    min_sample = max(1, int(settings.structure_review_min_sample or 1))
    recommendations: list[dict[str, Any]] = []

    def add_recommendation(key: str, suggested: int | float, reason: str) -> None:
        current = {
            "STRUCTURE_MIN_SCORE": settings.structure_min_score,
            "STRUCTURE_SEND_CHART_TOP_N": settings.structure_send_chart_top_n,
            "STRUCTURE_NEAR_EDGE_PCT": settings.structure_near_edge_pct,
            "STRUCTURE_COOLDOWN_SEC": settings.structure_cooldown_sec,
        }.get(key)
        if current is None or str(current) == str(suggested):
            return
        recommendations.append(
            {
                "key": key,
                "label": EDITABLE_CONFIG.get(key, ConfigField(key, key, "雷达参数")).label,
                "current": current,
                "suggested": suggested,
                "reason": reason,
            }
        )

    if reviewed < min_sample:
        return {
            "ok": True,
            "stats_file": str(stats_path),
            "summary": summary,
            "recommendations": [],
            "updates": {},
            "message": f"已复盘样本 {reviewed} 条，少于最小样本 {min_sample} 条，暂不建议自动调整。",
        }

    by_level = stats.get("by_level", {}) if isinstance(stats.get("by_level"), dict) else {}
    b_bucket = by_level.get("B", {}) if isinstance(by_level.get("B"), dict) else {}
    if int(b_bucket.get("reviewed", 0) or 0) >= 3 and float(b_bucket.get("fake_rate", 0) or 0) >= 0.45:
        add_recommendation(
            "STRUCTURE_MIN_SCORE",
            max(int(settings.structure_min_score) + 5, 70),
            "B级假突破率偏高，提高最低分可以过滤低质量结构信号。",
        )

    by_type = stats.get("by_signal_type", {}) if isinstance(stats.get("by_signal_type"), dict) else {}
    pre_total = sum(
        int((by_type.get(key, {}) if isinstance(by_type.get(key), dict) else {}).get("total", 0) or 0)
        for key in ("PRE_BREAKOUT_NEAR", "PRE_BREAKDOWN_NEAR")
    )
    if total and pre_total / total >= 0.7 and float(summary.get("hit_rate", 0) or 0) < 0.35:
        add_recommendation(
            "STRUCTURE_NEAR_EDGE_PCT",
            round(max(0.5, float(settings.structure_near_edge_pct) - 0.3), 1),
            "临界信号占比偏高且命中率不足，收紧临界距离可以减少提前观察噪音。",
        )

    by_symbol = stats.get("by_symbol", {}) if isinstance(stats.get("by_symbol"), dict) else {}
    symbol_counts = [int(bucket.get("total", 0) or 0) for bucket in by_symbol.values() if isinstance(bucket, dict)]
    if symbol_counts and max(symbol_counts) >= max(4, total // 4):
        add_recommendation(
            "STRUCTURE_COOLDOWN_SEC",
            max(int(settings.structure_cooldown_sec) * 2, 7200),
            "同币重复信号较多，提高冷却时间可以减少同一个币连续刷屏。",
        )

    if total >= 30 and int(settings.structure_send_chart_top_n) > 2:
        add_recommendation(
            "STRUCTURE_SEND_CHART_TOP_N",
            2,
            "结构信号数量较多，降低每轮发图数量可以减少图片刷屏。",
        )

    updates = {item["key"]: str(item["suggested"]) for item in recommendations}
    message = "已生成可应用的结构复盘参数建议" if updates else "当前样本未显示明显参数问题，暂不建议调整。"
    return {
        "ok": True,
        "stats_file": str(stats_path),
        "summary": summary,
        "recommendations": recommendations,
        "updates": updates,
        "message": message,
    }


def write_env_updates(
    updates: dict[str, Any],
    *,
    clear: list[str] | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    env_path = path or ENV_FILE
    clear_set = set(clear or [])
    normalized: dict[str, str] = {}
    errors: dict[str, str] = {}
    for key, value in updates.items():
        field = EDITABLE_CONFIG.get(key)
        if field is None:
            errors[key] = "不允许修改这个配置项"
            continue
        if field.secret and str(value).strip() == "" and key not in clear_set:
            continue
        try:
            normalized[key] = "" if key in clear_set else validate_config_value(field, value)
        except ValueError as exc:
            errors[key] = str(exc)
    for key in clear_set:
        if key not in EDITABLE_CONFIG:
            errors[key] = "不允许清空这个配置项"
        elif key not in normalized:
            normalized[key] = ""
    if errors:
        return {"ok": False, "errors": errors}
    if not normalized:
        return {"ok": True, "changed": [], "backup": "", "message": "没有需要保存的修改"}

    env_path.parent.mkdir(parents=True, exist_ok=True)
    backup = backup_env_file(env_path)
    seen: set[str] = set()
    lines = read_text_file(env_path).splitlines()
    output: list[str] = []
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#") or "=" not in raw_line:
            output.append(raw_line)
            continue
        key = raw_line.split("=", 1)[0].strip()
        if key in normalized:
            output.append(f"{key}={normalized[key]}")
            seen.add(key)
        else:
            output.append(raw_line)
    missing = [key for key in normalized if key not in seen]
    if missing and output and output[-1].strip():
        output.append("")
    for key in missing:
        output.append(f"{key}={normalized[key]}")
    env_path.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    for key, value in normalized.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    load_env_file(env_path)
    return {
        "ok": True,
        "changed": sorted(normalized),
        "backup": str(backup) if backup else "",
        "message": "配置已保存，重启服务后后台进程会使用新配置",
    }


def run_subprocess(argv: list[str], *, timeout: int = 30, use_python: bool = False) -> dict[str, Any]:
    command = [sys.executable, "main.py", *argv] if use_python else argv
    try:
        completed = subprocess.run(
            command,
            cwd=BASE_DIR,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
        return {
            "ok": completed.returncode == 0,
            "returncode": completed.returncode,
            "stdout": completed.stdout[-12000:],
            "stderr": completed.stderr[-6000:],
            "command": " ".join(command),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "returncode": 124,
            "stdout": (exc.stdout or "")[-12000:] if isinstance(exc.stdout, str) else "",
            "stderr": f"命令超时：{timeout}s",
            "command": " ".join(command),
        }
    except OSError as exc:
        return {
            "ok": False,
            "returncode": 127,
            "stdout": "",
            "stderr": f"{type(exc).__name__}: {exc}",
            "command": " ".join(command),
        }


def run_cli_action(name: str) -> dict[str, Any]:
    action = CLI_ACTIONS.get(name)
    if action is None:
        return {"ok": False, "returncode": 2, "stderr": "未知动作", "stdout": ""}
    result = run_subprocess(action["argv"], timeout=int(action.get("timeout", 30)), use_python=True)
    result["label"] = action["label"]
    return result


def sudo_systemctl_command(service: str, action: str) -> list[str]:
    if os.name == "nt":
        return ["systemctl", action, service]
    if os.geteuid() == 0:
        return ["systemctl", action, service]
    return ["sudo", "-n", "systemctl", action, service]


def run_service_action(name: str) -> dict[str, Any]:
    item = SERVICE_ACTIONS.get(name)
    if item is None:
        return {"ok": False, "returncode": 2, "stderr": "未知服务动作", "stdout": ""}
    service, action = item
    if action == "stop" and not name.startswith("stop-"):
        return {"ok": False, "returncode": 2, "stderr": "停止服务需要明确动作", "stdout": ""}
    return run_subprocess(sudo_systemctl_command(service, action), timeout=30)


def schedule_service_action(name: str, *, delay_sec: float = 1.2) -> dict[str, Any]:
    item = SERVICE_ACTIONS.get(name)
    if item is None:
        return {"ok": False, "returncode": 2, "stderr": "未知服务动作", "stdout": ""}
    service, action = item

    def worker() -> None:
        time.sleep(delay_sec)
        result = run_subprocess(sudo_systemctl_command(service, action), timeout=30)
        sys.stderr.write(
            f"[web] delayed {action} {service}: ok={result.get('ok')} returncode={result.get('returncode')}\n"
        )

    thread = threading.Thread(target=worker, name=f"web-{action}-{service}", daemon=True)
    thread.start()
    return {
        "ok": True,
        "scheduled": True,
        "service": service,
        "action": action,
        "delay_sec": delay_sec,
        "message": "已安排延迟执行，当前请求会先返回结果",
    }


def auto_apply_config_changes(changed: list[str]) -> dict[str, Any]:
    changed_set = set(changed)
    if not changed_set:
        return {"ok": True, "mode": "none", "results": [], "message": "没有配置变更，不需要自动应用"}

    results: list[dict[str, Any]] = []
    if changed_set - WEB_CONFIG_KEYS:
        for action_name in ("restart-main", "restart-structure"):
            result = run_service_action(action_name)
            service, action = SERVICE_ACTIONS[action_name]
            result.update({"name": action_name, "service": service, "action": action})
            results.append(result)
    if changed_set & WEB_CONFIG_KEYS:
        results.append(schedule_service_action("restart-web"))

    ok = all(bool(item.get("ok")) for item in results)
    if not results:
        message = "没有需要自动重启的服务"
    elif ok:
        message = "配置已保存并自动应用；Web 控制台配置变更会在返回结果后短暂重启"
    else:
        message = "配置已保存，但部分服务自动应用失败；可到服务控制页手动重启"
    return {"ok": ok, "mode": "auto_restart", "results": results, "message": message}


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def service_status(service: str) -> dict[str, Any]:
    if not command_exists("systemctl"):
        return {"service": service, "available": False, "active": "unknown", "enabled": "unknown"}
    active = run_subprocess(["systemctl", "is-active", service], timeout=8)
    enabled = run_subprocess(["systemctl", "is-enabled", service], timeout=8)
    return {
        "service": service,
        "available": True,
        "active": active["stdout"].strip() or active["stderr"].strip() or "unknown",
        "enabled": enabled["stdout"].strip() or enabled["stderr"].strip() or "unknown",
        "active_ok": active["returncode"] == 0,
    }


def git_info() -> dict[str, str]:
    version_path = BASE_DIR / "VERSION"
    version = version_path.read_text(encoding="utf-8", errors="ignore").strip() if version_path.exists() else "unknown"
    commit = run_subprocess(["git", "rev-parse", "--short", "HEAD"], timeout=10)
    subject = run_subprocess(["git", "log", "-1", "--format=%s"], timeout=10)
    branch = run_subprocess(["git", "branch", "--show-current"], timeout=10)
    return {
        "version": version or "unknown",
        "commit": commit["stdout"].strip() or "unknown",
        "branch": branch["stdout"].strip() or "unknown",
        "subject": subject["stdout"].strip() or "",
    }


def load_json_or_empty(path: Path) -> Any:
    if not path.exists():
        return {"status": "empty", "path": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "invalid", "path": str(path), "error": f"{type(exc).__name__}: {exc}"}


def summary_payload() -> dict[str, Any]:
    settings = Settings.load()
    store = JsonStore(settings.data_dir)
    redacted = settings.redacted_status()
    telegram = dict(redacted.get("telegram") or {})
    topic_routes = read_topic_routes(settings.tg_topic_routes_path)
    configured_routes = dict(telegram.get("topic_routes_configured") or {})
    saved_routes: dict[str, str] = {}
    for template_id, route_key in TOPIC_SUMMARY_ROUTE_KEYS.items():
        topic_id = topic_routes.get(template_id, {}).get("topic_id", "")
        if topic_id:
            configured_routes[route_key] = True
            saved_routes[route_key] = topic_id
    telegram["topic_routes_configured"] = configured_routes
    telegram["topic_routes_saved"] = saved_routes
    telegram["topic_routes_file_exists"] = settings.tg_topic_routes_path.exists()
    return {
        "updated_at": now_text(),
        "git": git_info(),
        "services": {
            "main": service_status(MAIN_SERVICE),
            "structure": service_status(STRUCTURE_SERVICE),
            "web": service_status(WEB_SERVICE),
        },
        "runtime": {
            "main": load_json_or_empty(settings.runtime_status_path),
            "structure": load_json_or_empty(settings.structure_runtime_status_path),
        },
        "config": {
            "env_file_exists": redacted.get("env_file_exists"),
            "telegram": telegram,
            "liquidity": redacted.get("liquidity"),
            "coinalyze": redacted.get("coinalyze"),
            "structure_radar": redacted.get("structure_radar"),
        },
        "state_files": store.exists_summary([
            settings.runtime_status_path,
            settings.structure_runtime_status_path,
            settings.tg_push_history_path,
            settings.structure_history_path,
        ]),
    }


def tail_file(path: Path, lines: int) -> str:
    if not path.exists():
        return ""
    data = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(data[-max(1, lines):])


def logs_payload(target: str, lines: int) -> dict[str, Any]:
    settings = Settings.load()
    lines = max(20, min(2000, int(lines)))
    if target == "structure":
        service = STRUCTURE_SERVICE
        fallback_path = settings.data_dir / "structure.log"
    elif target == "web":
        service = WEB_SERVICE
        fallback_path = settings.data_dir / "web.log"
    else:
        service = MAIN_SERVICE
        fallback_path = settings.data_dir / "runtime.log"
    if command_exists("journalctl"):
        result = run_subprocess(["journalctl", "-u", service, "-n", str(lines), "--no-pager"], timeout=15)
        if result["stdout"].strip() or result["returncode"] == 0:
            return {"target": target, "source": f"journalctl:{service}", "text": result["stdout"], "ok": result["ok"]}
    text = tail_file(fallback_path, lines)
    return {"target": target, "source": str(fallback_path), "text": text, "ok": bool(text)}


def check_auth(handler: BaseHTTPRequestHandler) -> bool:
    token = getattr(handler.server, "admin_token", "")  # type: ignore[attr-defined]
    if not token:
        return True
    parsed = urlparse(handler.path)
    query_token = parse_qs(parsed.query).get("token", [""])[0]
    header_token = handler.headers.get("X-Admin-Token", "")
    return token in {query_token, header_token}


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>泡泡雷达控制台</title>
  <style>
    :root {
      --bg: #e7ebee;
      --bg-2: #f7f9fa;
      --panel: rgba(255, 255, 255, .84);
      --panel-2: #edf1f3;
      --text: #171c1f;
      --muted: #68747b;
      --line: rgba(118, 132, 141, .32);
      --line-strong: rgba(75, 86, 94, .42);
      --accent: #0f6f68;
      --accent-2: #34424a;
      --warn: #9a6508;
      --bad: #b3261e;
      --good: #087443;
      --shadow: 0 12px 28px rgba(30, 38, 43, .08), 0 1px 1px rgba(255, 255, 255, .7) inset;
      --metal: linear-gradient(135deg, rgba(255,255,255,.92), rgba(240,244,246,.78) 44%, rgba(255,255,255,.86));
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background:
        linear-gradient(115deg, rgba(255,255,255,.55), rgba(190,198,204,.36)),
        repeating-linear-gradient(90deg, rgba(255,255,255,.22) 0, rgba(255,255,255,.22) 1px, rgba(143,153,160,.08) 1px, rgba(143,153,160,.08) 4px),
        var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    button, input, select { font: inherit; }
    .app { min-height: 100vh; display: grid; grid-template-columns: 220px 1fr; }
    aside {
      background:
        linear-gradient(180deg, rgba(41, 49, 55, .98), rgba(19, 24, 28, .98)),
        repeating-linear-gradient(90deg, rgba(255,255,255,.05) 0, rgba(255,255,255,.05) 1px, transparent 1px, transparent 5px);
      color: #e8eef0;
      padding: 18px 14px;
      position: sticky;
      top: 0;
      height: 100vh;
      border-right: 1px solid rgba(255,255,255,.08);
      box-shadow: 6px 0 18px rgba(20, 27, 31, .12);
    }
    .brand {
      font-weight: 800;
      font-size: 18px;
      margin: 2px 8px 18px;
      letter-spacing: .02em;
      color: #f7fafb;
    }
    nav { display: grid; gap: 4px; }
    nav button {
      width: 100%;
      border: 1px solid transparent;
      border-radius: 6px;
      background: transparent;
      color: inherit;
      text-align: left;
      padding: 10px 11px;
      cursor: pointer;
    }
    nav button.active, nav button:hover {
      background: linear-gradient(135deg, rgba(255,255,255,.14), rgba(255,255,255,.06));
      border-color: rgba(255,255,255,.13);
      box-shadow: 0 1px 0 rgba(255,255,255,.08) inset;
    }
    main { padding: 22px 26px 36px; min-width: 0; }
    header {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      margin-bottom: 16px;
    }
    h1 { margin: 0; font-size: 23px; letter-spacing: .01em; }
    .muted { color: var(--muted); }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
    .panel {
      background: var(--metal);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
      min-width: 0;
      backdrop-filter: blur(8px);
    }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-6 { grid-column: span 6; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    .metric { display: grid; gap: 5px; min-height: 82px; }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { font-size: 20px; font-weight: 700; overflow-wrap: anywhere; }
    .status {
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      font-weight: 700;
      background: var(--panel-2);
      color: var(--muted);
      border: 1px solid rgba(101, 113, 121, .18);
    }
    .status.ok { background: linear-gradient(135deg, #dff7ea, #f2fbf6); color: var(--good); }
    .status.bad { background: linear-gradient(135deg, #ffe4df, #fff5f2); color: var(--bad); }
    .status.neutral { background: linear-gradient(135deg, #eef2f4, #fafbfc); color: var(--muted); }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
    .btn {
      border: 1px solid var(--line);
      background: linear-gradient(135deg, rgba(255,255,255,.9), rgba(235,240,243,.82));
      color: var(--text);
      border-radius: 6px;
      padding: 8px 11px;
      cursor: pointer;
      min-height: 36px;
      box-shadow: 0 1px 0 rgba(255,255,255,.75) inset, 0 6px 14px rgba(35, 43, 48, .06);
    }
    .btn.primary { background: linear-gradient(135deg, #0f766e, #0b5f59); border-color: #0b5f59; color: white; }
    .btn.blue { background: linear-gradient(135deg, #41515a, #2f3a41); border-color: #2f3a41; color: white; }
    .btn.warn { border-color: #d6a01d; color: var(--warn); }
    .btn.danger { border-color: #f2b8ad; color: var(--bad); }
    .btn:disabled { opacity: .55; cursor: not-allowed; }
    pre {
      margin: 0;
      background: #11171b;
      color: #dce7eb;
      border-radius: 6px;
      padding: 13px;
      min-height: 320px;
      max-height: 640px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      border: 1px solid rgba(255,255,255,.08);
    }
    .table { width: 100%; border-collapse: collapse; }
    .table th, .table td { border-bottom: 1px solid var(--line); text-align: left; padding: 9px 6px; vertical-align: top; }
    .table th { color: var(--muted); font-size: 12px; font-weight: 700; }
    .form-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
    .field { display: grid; gap: 6px; }
    label { font-weight: 700; font-size: 13px; }
    input, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 9px;
      background: linear-gradient(180deg, #ffffff, #f7f9fa);
      color: var(--text);
      min-height: 36px;
      box-shadow: 0 1px 0 rgba(255,255,255,.8) inset;
    }
    input:focus, select:focus {
      outline: 2px solid rgba(15, 118, 110, .18);
      border-color: rgba(15, 118, 110, .55);
    }
    .secret-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
    .field-heading {
      display: grid;
      grid-template-columns: minmax(120px, max-content) 1fr;
      align-items: flex-start;
      gap: 10px;
    }
    .field-current {
      display: inline-flex;
      align-items: center;
      justify-self: end;
      max-width: 100%;
      border: 1px solid rgba(93, 106, 115, .24);
      border-radius: 999px;
      padding: 2px 8px;
      background: linear-gradient(135deg, rgba(255,255,255,.78), rgba(236,241,244,.72));
      color: #48565d;
      font-size: 12px;
      font-weight: 700;
      white-space: normal;
      overflow-wrap: anywhere;
      word-break: break-word;
      text-align: right;
    }
    .field-help { color: var(--muted); font-size: 12px; }
    .section-title { margin: 2px 0 10px; font-size: 15px; }
    .output { margin-top: 12px; }
    .summary-card { display: grid; gap: 11px; align-content: start; }
    .summary-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
    .summary-title { margin: 0; font-size: 15px; }
    .summary-meta { color: var(--muted); font-size: 12px; word-break: break-all; }
    .readable-list { display: grid; gap: 8px; }
    .readable-row {
      display: grid;
      grid-template-columns: 136px 1fr;
      gap: 10px;
      align-items: start;
      border-top: 1px solid #edf1f3;
      padding-top: 8px;
    }
    .readable-row:first-child { border-top: 0; padding-top: 0; }
    .readable-label { color: var(--muted); font-weight: 700; }
    .readable-value { color: var(--text); word-break: break-word; }
    .hint { color: var(--muted); font-size: 13px; }
    .raw-details {
      grid-column: span 12;
      padding: 0;
      overflow: hidden;
    }
    .raw-details summary {
      cursor: pointer;
      padding: 13px 14px;
      font-weight: 800;
      border-bottom: 1px solid transparent;
    }
    .raw-details[open] summary { border-bottom-color: var(--line); }
    .raw-details .raw-body { padding: 14px; display: grid; gap: 12px; }
    .raw-details pre { min-height: 180px; max-height: 460px; }
    .notice {
      background: linear-gradient(135deg, rgba(236, 247, 246, .92), rgba(249, 252, 252, .76));
      border: 1px solid #b9ddda;
      color: #194844;
      border-radius: 8px;
      padding: 12px 14px;
    }
    .feature-list { display: grid; gap: 10px; }
    .feature-item {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 11px;
      background: linear-gradient(135deg, rgba(255,255,255,.74), rgba(239,244,246,.7));
    }
    .feature-item strong { display: block; margin-bottom: 4px; }
    .api-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 10px;
    }
    .api-card {
      border: 1px solid rgba(93, 106, 115, .22);
      border-radius: 8px;
      padding: 11px;
      background: linear-gradient(135deg, rgba(255,255,255,.76), rgba(238,243,245,.72));
      display: grid;
      gap: 7px;
      align-content: start;
    }
    .api-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }
    .api-title {
      display: inline-flex;
      align-items: center;
      gap: 9px;
      min-width: 0;
    }
    .api-logo {
      width: 30px;
      height: 30px;
      border-radius: 9px;
      display: inline-grid;
      place-items: center;
      flex: 0 0 auto;
      color: #34424a;
      font-size: 10px;
      font-weight: 900;
      letter-spacing: 0;
      background: linear-gradient(135deg, #ffffff, #edf2f4);
      border: 1px solid rgba(93, 106, 115, .24);
      box-shadow: 0 1px 0 rgba(255,255,255,.55) inset, 0 8px 18px rgba(33, 42, 48, .12);
      overflow: hidden;
    }
    .api-logo img {
      width: 22px;
      height: 22px;
      object-fit: contain;
      display: block;
    }
    .api-logo-fallback { display: none; }
    .api-card h4 { margin: 0; font-size: 14px; }
    .api-card p { margin: 0; color: var(--muted); line-height: 1.5; }
    .api-card ul { margin: 0; padding-left: 17px; color: var(--muted); line-height: 1.5; }
    .action-card { display: grid; gap: 10px; align-content: start; }
    .action-card ul {
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.58;
    }
    .action-card li { margin: 4px 0; }
    .service-guide {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
      margin-top: 10px;
    }
    .service-guide-item {
      border: 1px solid rgba(93, 106, 115, .22);
      border-radius: 8px;
      padding: 10px;
      background: linear-gradient(135deg, rgba(255,255,255,.72), rgba(236,241,244,.64));
    }
    .service-guide-item strong { display: block; margin-bottom: 4px; }
    .service-card { display: grid; gap: 12px; }
    .service-card-head {
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }
    .service-action {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: center;
      border-top: 1px solid rgba(105, 118, 126, .18);
      padding-top: 10px;
    }
    .service-action:first-of-type { border-top: 0; padding-top: 0; }
    .service-action-title { font-weight: 800; }
    .service-action-note { color: var(--muted); font-size: 13px; margin-top: 2px; }
    .action-badge {
      display: inline-flex;
      width: fit-content;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 2px 8px;
      font-size: 12px;
      color: var(--muted);
      background: linear-gradient(135deg, #f8fafb, #edf2f4);
    }
    .kv {
      display: grid;
      grid-template-columns: 140px 1fr;
      gap: 7px 12px;
      font-size: 13px;
    }
    .kv div:nth-child(odd) { color: var(--muted); font-weight: 700; }
    .hidden { display: none !important; }
    .auth {
      position: fixed;
      inset: 0;
      display: grid;
      place-items: center;
      background: rgba(15, 23, 42, .32);
      z-index: 10;
    }
    .auth-box {
      width: min(420px, calc(100vw - 32px));
      background: var(--metal);
      border-radius: 8px;
      border: 1px solid var(--line);
      padding: 18px;
      box-shadow: 0 12px 36px rgba(15, 23, 42, .2);
    }
    @media (max-width: 900px) {
      .app { grid-template-columns: 1fr; }
      aside { position: static; height: auto; }
      nav { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      main { padding: 14px; }
      .span-3, .span-4, .span-6, .span-8 { grid-column: span 12; }
      .service-guide { grid-template-columns: 1fr; }
      .service-action { grid-template-columns: 1fr; }
      .api-grid { grid-template-columns: 1fr; }
      .form-grid { grid-template-columns: 1fr; }
      .field-heading { grid-template-columns: 1fr; }
      .field-current { justify-self: start; text-align: left; }
      header { align-items: flex-start; flex-direction: column; }
    }
  </style>
</head>
<body>
  <div id="auth" class="auth hidden">
    <div class="auth-box">
      <h2>访问令牌</h2>
      <div class="field">
        <label for="tokenInput">WEB_ADMIN_TOKEN</label>
        <input id="tokenInput" type="password" autocomplete="current-password">
      </div>
      <div class="toolbar" style="margin:12px 0 0">
        <button class="btn primary" onclick="saveToken()">进入</button>
      </div>
    </div>
  </div>
  <div class="app">
    <aside>
      <div class="brand">泡泡雷达控制台</div>
      <nav>
        <button data-view="overview" class="active">总览</button>
        <button data-view="logs">日志</button>
        <button data-view="config">配置</button>
        <button data-view="actions">检查测试</button>
        <button data-view="services">服务控制</button>
        <button data-view="guide">功能说明</button>
      </nav>
    </aside>
    <main>
      <header>
        <div>
          <h1 id="pageTitle">总览</h1>
          <div id="subtitle" class="muted">正在读取状态</div>
        </div>
        <div class="toolbar">
          <button class="btn" onclick="refreshCurrent()">刷新</button>
        </div>
      </header>

      <section id="overview" class="view">
        <div class="grid" id="overviewGrid"></div>
      </section>

      <section id="logs" class="view hidden">
        <div class="toolbar">
      <select id="logTarget">
        <option value="main">主服务</option>
        <option value="structure">结构雷达</option>
        <option value="web">Web 控制台</option>
      </select>
          <select id="logLines">
            <option value="200">最近 200 行</option>
            <option value="500">最近 500 行</option>
            <option value="1000">最近 1000 行</option>
          </select>
          <button class="btn primary" onclick="loadLogs()">读取日志</button>
          <button class="btn" onclick="copyLogs()">复制</button>
        </div>
        <pre id="logOutput"></pre>
      </section>

      <section id="config" class="view hidden">
        <div id="configForms" class="grid"></div>
        <div class="toolbar" style="margin-top:12px">
          <button class="btn" onclick="previewConfig()">预览改动</button>
          <button class="btn blue" onclick="applyStructureRecommendations()">应用复盘建议</button>
          <button class="btn primary" onclick="saveConfig()">保存配置</button>
        </div>
        <div id="configPreview" class="panel hidden"></div>
        <pre id="configOutput" class="output"></pre>
      </section>

      <section id="actions" class="view hidden">
        <div class="grid" id="actionGrid"></div>
        <pre id="actionOutput" class="output"></pre>
      </section>

      <section id="services" class="view hidden">
        <div class="grid" id="serviceGrid"></div>
        <pre id="serviceOutput" class="output"></pre>
      </section>

      <section id="guide" class="view hidden">
        <div class="grid" id="guideGrid"></div>
      </section>
    </main>
  </div>

  <script>
    const titles = {
      overview: "总览",
      logs: "日志",
      config: "配置",
      actions: "检查测试",
      services: "服务控制",
      guide: "功能说明"
    };
    const actionList = [
      {
        id: "readiness",
        label: "检查真实推送准备度",
        badge: "只检查，不发送",
        desc: "readiness 是真实推送前的门禁检查，用来判断机器人现在是否具备安全推送条件。",
        details: [
          "检查 Telegram Token、群 ID、话题配置是否满足真实推送要求。",
          "检查启动观察历史是否足够，避免刚部署就直接推送不稳定信号。",
          "检查最近候选压力和历史文件状态；全部通过才适合开启真实推送。",
          "结果里 OK 表示通过，WAIT 表示还需要补配置或继续 dry-run 观察。"
        ]
      },
      {
        id: "runtime-status",
        label: "查看运行状态",
        badge: "读取状态文件",
        desc: "读取主服务和结构雷达写入的 runtime-status，快速看后台最近在做什么。",
        details: [
          "能看到当前任务、运行模式、最近扫描时间、下一次扫描时间和最后错误。",
          "适合确认服务是否真的在循环运行，而不是只看后台服务是否显示运行中。",
          "如果这里长时间不更新，再去日志页查看具体报错。"
        ]
      },
      {
        id: "doctor",
        label: "环境诊断",
        badge: "只读诊断",
        desc: "输出配置、状态文件、历史文件、清理设置等诊断信息，适合排查部署问题。",
        details: [
          "用于确认 .env.oi 是否存在、关键路径是否正常、状态文件是否能读取。",
          "不会发送 Telegram，也不会修改配置。",
          "如果功能异常但日志不明显，先跑这个看环境是否缺文件或配置。"
        ]
      },
      {
        id: "telegram-test",
        label: "发送 Telegram 测试",
        badge: "会真实发送",
        desc: "向配置的 Telegram 群或话题发送一条测试消息，用来验证 Token、群 ID、话题 ID 是否可用。",
        details: [
          "这个动作会真实发消息，不是 dry-run。",
          "适合改完机器人 Token、群 ID、话题 ID 后立即验证。",
          "失败时重点看输出里的 Telegram 错误，例如无权限、chat not found、topic not found。"
        ]
      },
      {
        id: "announcements-test",
        label: "测试 Binance 公告",
        badge: "抓取测试",
        desc: "抓取 Binance 公告并按机会/风险分类，验证公告监听是否能正常读取数据。",
        details: [
          "用于检查 Alpha、上新、活动、下架、停止交易等公告识别。",
          "只测试抓取和分类，不代表一定会推送新公告。",
          "如果失败，多半是网络、Binance 页面接口或限频问题。"
        ]
      },
      {
        id: "structure-review",
        label: "结构信号复盘",
        badge: "生成报告",
        desc: "对最近结构雷达信号做 dry-run 复盘，生成报告用于检查结构策略表现。",
        details: [
          "适合查看最近结构突破、临界预警、确认信号的后续表现。",
          "默认生成复盘报告；是否发送 Telegram 仍受命令参数和配置门禁限制。",
          "如果结构雷达刚部署不久，历史数据少，报告可能比较空。"
        ]
      },
      {
        id: "cleanup",
        label: "清理运行垃圾",
        badge: "会删除临时文件",
        desc: "立即执行 cleanup，清理运行中产生的临时文件、过期图表和超限历史记录。",
        details: [
          "不会删除 .env.oi、源码、后台服务文件和关键配置。",
          "适合磁盘空间变多、图表文件堆积、历史记录太长时手动执行。",
          "输出会列出删除、保留和裁剪了哪些文件。"
        ]
      }
    ];
    const apiSourceList = [
      {
        brand: "telegram",
        logoUrl: "https://www.google.com/s2/favicons?domain=telegram.org&sz=64",
        name: "Telegram Bot API",
        status: "必填",
        keyText: "需要填写 TG_BOT_TOKEN 和 TG_CHAT_ID",
        usage: "负责把雷达结果发送到 Telegram 群、频道或话题。",
        supports: ["真实推送", "测试消息", "话题路由", "回复链追踪"],
        note: "这是机器人能不能发消息的核心配置。"
      },
      {
        brand: "binance",
        logoUrl: "https://www.google.com/s2/favicons?domain=binance.com&sz=64",
        name: "Binance 免费公开数据",
        status: "已接入，无需 Key",
        keyText: "不用填写 API Key",
        usage: "项目的主数据源，负责行情、K线、OI、资金费率、成交额、盘口深度、公告抓取和优先市值数据。",
        supports: ["资金摘要", "启动雷达", "资金流雷达", "结构雷达", "公告监听", "盘口流动性"],
        note: "只用公开接口；如果网络或限频异常，日志和数据质量里会显示。"
      },
      {
        brand: "coinpaprika",
        logoUrl: "https://www.google.com/s2/favicons?domain=coinpaprika.com&sz=64",
        name: "CoinPaprika 免费市值数据",
        status: "已接入，无需 Key",
        keyText: "不用填写 API Key",
        usage: "当 Binance 没有返回币种市值时，给启动雷达补市值。",
        supports: ["启动雷达市值兜底", "市值高/中/低分档", "市值来源显示"],
        note: "它只补市值，不参与价格、OI、成交量和交易信号计算。"
      },
      {
        brand: "coinalyze",
        logoUrl: "https://www.google.com/s2/favicons?domain=coinalyze.net&sz=64",
        name: "Coinalyze API",
        status: "可选",
        keyText: "可填写 COINALYZE_API_KEY，并开启 COINALYZE_ENABLE",
        usage: "只用于结构雷达外部确认里的历史清算量方向辅助。",
        supports: ["历史清算量", "清算方向辅助", "结构雷达分数小幅修正"],
        note: "本项目没有用 Coinalyze 获取市值；它也不影响启动雷达市值。"
      },
      {
        brand: "coinmarketcap",
        logoUrl: "https://www.google.com/s2/favicons?domain=coinmarketcap.com&sz=64",
        name: "CoinMarketCap API",
        status: "预留，未接入",
        keyText: "现在不需要填写，Web 里也没有这个 Key 的配置项",
        usage: "可作为以后更高质量市值兜底的数据源。",
        supports: ["暂未支持"],
        note: "如果后续接入，会在配置页新增对应 API Key 输入框。"
      }
    ];
    const serviceGroups = [
      {
        name: "主服务",
        service: "paopao-radar",
        desc: "负责资金摘要、启动雷达、公告监听、资金流雷达等主要循环。它停了以后，主要 Telegram 推送会停止。",
        actions: [
          { id: "restart-main", label: "重启主服务", button: "重启", level: "warn", note: "改完 .env.oi、推送配置、扫描参数后通常点这个，让主服务重新读取配置。" },
          { id: "start-main", label: "启动主服务", button: "启动", level: "", note: "主服务被停止或服务器重启后没有自动起来时使用。" },
          { id: "stop-main", label: "停止主服务", button: "停止", level: "danger", note: "会暂停主要扫描和推送。只有维护、排错或避免继续发消息时使用。" }
        ]
      },
      {
        name: "结构雷达",
        service: "paopao-structure",
        desc: "负责结构突破雷达的独立循环，包括临界预警、收线确认和结构复盘相关状态。",
        actions: [
          { id: "restart-structure", label: "重启结构雷达", button: "重启", level: "warn", note: "改完结构雷达参数、话题 ID 或图表配置后使用。" },
          { id: "start-structure", label: "启动结构雷达", button: "启动", level: "", note: "结构雷达状态显示已停止、但你希望继续结构信号监控时使用。" },
          { id: "stop-structure", label: "停止结构雷达", button: "停止", level: "danger", note: "会暂停结构雷达预警和确认。主服务仍可继续运行。" }
        ]
      },
      {
        name: "Web 控制台",
        service: "paopao-web",
        desc: "就是当前这个网页后台服务。重启后页面会短暂断开，刷新浏览器即可。",
        actions: [
          { id: "restart-web", label: "重启 Web 控制台", button: "重启", level: "warn", note: "改完 Web 端口、Web 令牌或更新代码后使用。" },
          { id: "start-web", label: "启动 Web 控制台", button: "启动", level: "", note: "浏览器打不开控制台，且服务器服务状态显示 Web 已停止时使用。" },
          { id: "stop-web", label: "停止 Web 控制台", button: "停止", level: "danger", note: "会让网页控制台打不开。除非你明确要关闭 Web 入口，否则不建议点。" }
        ]
      }
    ];
    let currentView = "overview";
    let latestConfigData = null;

    function token() { return localStorage.getItem("paopaoAdminToken") || ""; }
    function headers() {
      const h = {"Content-Type": "application/json"};
      if (token()) h["X-Admin-Token"] = token();
      return h;
    }
    function showAuth() { document.getElementById("auth").classList.remove("hidden"); }
    function hideAuth() { document.getElementById("auth").classList.add("hidden"); }
    function saveToken() {
      localStorage.setItem("paopaoAdminToken", document.getElementById("tokenInput").value);
      hideAuth();
      refreshCurrent();
    }
    async function api(path, options = {}) {
      const res = await fetch(path, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
      if (res.status === 401) {
        showAuth();
        throw new Error("需要访问令牌");
      }
      const text = await res.text();
      try { return JSON.parse(text); } catch { return { ok: res.ok, text }; }
    }
    function setSubtitle(text) { document.getElementById("subtitle").textContent = text; }
    function zhStatus(value) {
      const key = String(value || "unknown").toLowerCase();
      const map = {
        active: "运行中",
        inactive: "已停止",
        failed: "异常",
        activating: "启动中",
        deactivating: "停止中",
        enabled: "开机自启",
        disabled: "未开机自启",
        static: "静态服务",
        masked: "已屏蔽",
        unknown: "未知",
        running: "运行中",
        completed: "已完成",
        empty: "暂无状态",
        invalid: "状态文件异常",
        live: "真实推送",
        observe: "观察模式",
        loop: "循环运行",
        daemon: "后台循环",
        "structure-loop": "结构雷达循环",
        "structure-review": "结构信号复盘",
        "flow-radar": "资金流雷达",
        "runtime-status": "查看运行状态",
        "telegram-test": "Telegram 测试",
        "announcements-test": "Binance 公告测试",
        true: "开启",
        false: "关闭"
      };
      return map[key] || String(value || "未知");
    }
    function zhTask(value) {
      const key = String(value || "").toLowerCase();
      const map = {
        loop: "主循环",
        live: "真实推送循环",
        daemon: "后台循环",
        "structure-loop": "结构雷达循环",
        "structure-review": "结构信号复盘",
        "flow-radar": "资金流雷达",
        cleanup: "清理运行垃圾"
      };
      return map[key] || (value ? String(value) : "暂无");
    }
    function zhBool(value, enabledText = "开启", disabledText = "关闭") {
      if (value === true || String(value).toLowerCase() === "true") return enabledText;
      if (value === false || String(value).toLowerCase() === "false") return disabledText;
      return "未知";
    }
    function configuredText(value) {
      return value ? "已配置" : "未配置";
    }
    function statusPill(text, ok) {
      return `<span class="status ${ok ? "ok" : "bad"}">${escapeHtml(zhStatus(text))}</span>`;
    }
    function neutralPill(text) {
      return `<span class="status neutral">${escapeHtml(text)}</span>`;
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, s => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[s]));
    }
    function metric(label, value, extra = "") {
      return `<div class="panel span-3 metric"><div class="label">${label}</div><div class="value">${value}</div>${extra}</div>`;
    }
    function row(label, value) {
      return `<div class="readable-row"><div class="readable-label">${escapeHtml(label)}</div><div class="readable-value">${value}</div></div>`;
    }
    function textValue(value, fallback = "暂无") {
      const text = value === undefined || value === null || value === "" ? fallback : String(value);
      return escapeHtml(text);
    }
    function serviceCard(title, service) {
      const active = service.active || "unknown";
      const enabled = service.enabled || "unknown";
      const ok = Boolean(service.active_ok);
      return `<div class="panel span-3 summary-card">
        <div class="summary-head">
          <h3 class="summary-title">${escapeHtml(title)}</h3>
          ${statusPill(active, ok)}
        </div>
        <div class="summary-meta">${escapeHtml(service.service || "未找到服务名")}</div>
        <div class="readable-list">
          ${row("运行状态", statusPill(active, ok))}
          ${row("开机启动", neutralPill(zhStatus(enabled)))}
        </div>
      </div>`;
    }
    function successSummary(runtimeItem) {
      const found = [];
      function walk(value) {
        if (!value || typeof value !== "object") return;
        if (value.successes && typeof value.successes === "object") {
          Object.entries(value.successes).forEach(([name, count]) => found.push(`${name} ${count}`));
        }
        Object.values(value).forEach(walk);
      }
      walk(runtimeItem && runtimeItem.diagnostics);
      return found.length ? found.slice(0, 6).join("，") : "暂无请求统计";
    }
    function runtimeCard(title, runtimeItem, kind) {
      if (!runtimeItem || runtimeItem.status === "empty") {
        return `<div class="panel span-6 summary-card">
          <div class="summary-head"><h3 class="summary-title">${escapeHtml(title)}</h3>${neutralPill("暂无状态")}</div>
          <div class="hint">还没有状态文件。服务刚启动、尚未完成第一轮扫描，或服务没有写入 runtime-status 时会这样显示。</div>
        </div>`;
      }
      if (runtimeItem.status === "invalid") {
        return `<div class="panel span-6 summary-card">
          <div class="summary-head"><h3 class="summary-title">${escapeHtml(title)}</h3>${statusPill("异常", false)}</div>
          <div class="hint">${textValue(runtimeItem.error || "状态文件无法解析")}</div>
        </div>`;
      }
      const statusKey = String(runtimeItem.status || "").toLowerCase();
      const ok = ["running", "completed"].includes(statusKey);
      const commonRows = [
        row("运行状态", statusPill(runtimeItem.status || "unknown", ok)),
        row("当前任务", textValue(zhTask(runtimeItem.task))),
        row("运行模式", textValue(zhStatus(runtimeItem.mode))),
        row("真实推送", neutralPill(zhBool(runtimeItem.real_send, "已开启", "未开启"))),
        row("最近更新", textValue(runtimeItem.updated_at)),
        row("最后错误", runtimeItem.last_error ? `<span class="status bad">${escapeHtml(runtimeItem.last_error)}</span>` : neutralPill("无"))
      ];
      const mainRows = [
        ...commonRows,
        row("最近启动扫描", textValue(runtimeItem.last_launch_at)),
        row("下次启动扫描", textValue(runtimeItem.next_launch_at)),
        row("数据请求", textValue(successSummary(runtimeItem)))
      ];
      const structureRows = [
        ...commonRows,
        row("K线周期", textValue(runtimeItem.structure_interval)),
        row("下次临界预警", textValue(runtimeItem.next_pre_at)),
        row("下次收线确认", textValue(runtimeItem.next_confirm_at))
      ];
      return `<div class="panel span-6 summary-card">
        <div class="summary-head"><h3 class="summary-title">${escapeHtml(title)}</h3>${statusPill(runtimeItem.status || "unknown", ok)}</div>
        <div class="readable-list">${(kind === "structure" ? structureRows : mainRows).join("")}</div>
      </div>`;
    }
    function configCard(title, rows, span = 4) {
      return `<div class="panel span-${span} summary-card">
        <h3 class="summary-title">${escapeHtml(title)}</h3>
        <div class="readable-list">${rows.join("")}</div>
      </div>`;
    }
    function configSummaryCards(cfg, stateFiles) {
      const telegram = cfg.telegram || {};
      const routes = telegram.topic_routes_configured || {};
      const routeValues = Object.values(routes);
      const routeReady = routeValues.filter(Boolean).length;
      const routeTotal = routeValues.length || 0;
      const liquidity = cfg.liquidity || {};
      const coinalyze = cfg.coinalyze || {};
      const structureRadar = cfg.structure_radar || {};
      const existingFiles = (stateFiles || []).filter(item => item.exists).length;
      return [
        configCard("Telegram 配置", [
          row("配置文件", cfg.env_file_exists ? statusPill("存在", true) : statusPill("缺失", false)),
          row("机器人 Token", neutralPill(configuredText(telegram.bot_token_configured))),
          row("群/频道 ID", neutralPill(configuredText(telegram.chat_id_configured))),
          row("话题模式", neutralPill(zhBool(telegram.use_topic))),
          row("已配置话题", neutralPill(routeTotal ? `${routeReady}/${routeTotal}` : "未使用话题")),
          row("自动建话题", neutralPill(zhBool(telegram.auto_create_topics)))
        ]),
        configCard("外部确认配置", [
          row("外部确认", neutralPill(zhBool(liquidity.fallback_enable))),
          row("Binance 盘口", neutralPill(zhBool(liquidity.binance_orderbook_enable))),
          row("分数修正上限", textValue(liquidity.score_max_delta)),
          row("盘口距离范围", textValue(`${liquidity.min_distance_pct ?? "?"}% - ${liquidity.max_distance_pct ?? "?"}%`)),
          row("盘口档位", textValue(liquidity.binance_orderbook_depth_limit))
        ]),
        configCard("Coinalyze 配置", [
          row("功能开关", neutralPill(zhBool(coinalyze.enable))),
          row("API Key", neutralPill(configuredText(coinalyze.api_key_configured))),
          row("请求预算", textValue(coinalyze.request_budget)),
          row("清算周期", textValue(coinalyze.liquidation_interval)),
          row("回看小时", textValue(coinalyze.liquidation_lookback_hours))
        ]),
        configCard("结构雷达参数", [
          row("最低推送分", textValue(structureRadar.min_score)),
          row("结构图数量", textValue(structureRadar.send_chart_top_n)),
          row("扫描数量", textValue(structureRadar.top_symbols)),
          row("复盘统计", neutralPill(zhBool(structureRadar.review_enable))),
          row("复盘回看", textValue(`${structureRadar.review_lookback_hours ?? "?"} 小时`))
        ]),
        configCard("状态文件", [
          row("文件数量", neutralPill(`${existingFiles}/${(stateFiles || []).length || 0} 存在`)),
          ...((stateFiles || []).slice(0, 4).map(item => {
            const name = String(item.path || "").split(/[\\/]/).pop() || "状态文件";
            const value = item.exists ? `存在，${item.size || 0} 字节` : "缺失";
            return row(name, item.exists ? neutralPill(value) : statusPill(value, false));
          }))
        ], 12)
      ].join("");
    }
    function rawDetails(title, data) {
      return `<details class="panel raw-details">
        <summary>${escapeHtml(title)}</summary>
        <div class="raw-body"><pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre></div>
      </details>`;
    }
    function apiLogo(brand, label, logoUrl) {
      const safeBrand = escapeHtml(String(brand || "generic"));
      const safeLabel = escapeHtml(label || "");
      const safeUrl = escapeHtml(logoUrl || "");
      const fallback = escapeHtml((label || "?").replace(/\s+/g, "").slice(0, 3).toUpperCase());
      return `<span class="api-logo ${safeBrand}" title="${safeLabel}"><img src="${safeUrl}" alt="${safeLabel} logo" loading="lazy" referrerpolicy="no-referrer" onerror="this.style.display='none';this.nextElementSibling.style.display='inline';"><span class="api-logo-fallback" aria-hidden="true">${fallback}</span></span>`;
    }
    function apiSourceCards() {
      return `<div class="api-grid">` + apiSourceList.map(source => `
        <div class="api-card">
          <div class="api-card-head">
            <div class="api-title">${apiLogo(source.brand, source.name, source.logoUrl)}<h4>${escapeHtml(source.name)}</h4></div>
            ${neutralPill(source.status)}
          </div>
          <p><strong>填写要求：</strong>${escapeHtml(source.keyText)}</p>
          <p><strong>项目用途：</strong>${escapeHtml(source.usage)}</p>
          <ul>${source.supports.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
          <p>${escapeHtml(source.note)}</p>
        </div>
      `).join("") + `</div>`;
    }
    function apiSourcePanel() {
      return `<div class="panel span-12">
        <h3 class="section-title">外部接口和 API Key 说明</h3>
        <div class="notice"><strong>这里说明每个外部接口在本项目里到底做什么。</strong> 现在必须填写的是 Telegram；Coinalyze 是可选清算辅助；CoinMarketCap 只是预留方案，当前源码没有接入。</div>
        ${apiSourceCards()}
      </div>`;
    }
    function structureRecommendationPanel() {
      return `<div class="panel span-12 notice">
        <strong>结构复盘推送里的参数建议，可以在本页“雷达参数”里直接改。</strong>
        对应复盘建议里的 STRUCTURE_MIN_SCORE 控制结构雷达最低推送分；假突破偏高时提高它。对应复盘建议里的 STRUCTURE_SEND_CHART_TOP_N 控制每轮最多发送几张结构 K 线图；图片刷屏时降低它。保存后会自动应用。
        <div id="structureRecommendationBox" class="feature-list" style="margin-top:10px"></div>
      </div>`;
    }
    function configBackupPanel() {
      return `<div class="panel span-12">
        <div class="summary-head">
          <h3 class="section-title">配置备份和恢复</h3>
          <button class="btn" type="button" onclick="loadConfigBackups()">刷新备份</button>
        </div>
        <div class="notice"><strong>每次保存配置前都会自动备份 .env.oi。</strong> 如果参数改错，可以在这里恢复最近一次 Web 备份；恢复后也会自动应用。</div>
        <div id="configBackupList" class="feature-list" style="margin-top:10px"></div>
      </div>`;
    }
    async function loadSummary() {
      const data = await api("/api/summary");
      setSubtitle(`更新时间 ${data.updated_at}`);
      const main = data.services.main || {};
      const structure = data.services.structure || {};
      const web = data.services.web || {};
      const git = data.git || {};
      const runtime = data.runtime || {};
      const cfg = data.config || {};
      document.getElementById("overviewGrid").innerHTML = [
        `<div class="panel span-12 notice"><strong>Web 控制台是当前版本的主要操作入口。</strong> 服务器只需要记住 paopao；地址、令牌、Web 服务状态、日志和更新入口都在中文菜单里。配置修改、测试、服务控制都在这里完成。</div>`,
        serviceCard("主服务", main),
        serviceCard("结构雷达", structure),
        serviceCard("Web 控制台", web),
        metric("版本", escapeHtml(git.version || "unknown"), `<div class="muted">${escapeHtml(git.branch)} ${escapeHtml(git.commit)}</div>`),
        runtimeCard("主服务运行摘要", runtime.main, "main"),
        runtimeCard("结构雷达运行摘要", runtime.structure, "structure"),
        configSummaryCards(cfg, data.state_files || []),
        rawDetails("高级排查：原始运行状态 JSON", runtime),
        rawDetails("高级排查：原始配置摘要 JSON", cfg)
      ].join("");
    }
    async function loadLogs() {
      const target = document.getElementById("logTarget").value;
      const lines = document.getElementById("logLines").value;
      const data = await api(`/api/logs?target=${encodeURIComponent(target)}&lines=${encodeURIComponent(lines)}`);
      setSubtitle(`日志来源 ${data.source || ""}`);
      document.getElementById("logOutput").textContent = data.text || "暂无日志";
    }
    function copyLogs() {
      navigator.clipboard.writeText(document.getElementById("logOutput").textContent || "");
    }
    async function loadConfig() {
      const data = await api("/api/config");
      latestConfigData = data;
      setSubtitle(data.env_file);
      const root = document.getElementById("configForms");
      root.innerHTML = apiSourcePanel() + structureRecommendationPanel() + configBackupPanel();
      Object.entries(data.sections || {}).forEach(([section, fields]) => {
        const panel = document.createElement("div");
        panel.className = "panel span-12";
        const body = fields.map(fieldHtml).join("");
        panel.innerHTML = `<h3 class="section-title">${escapeHtml(section)}</h3><div class="form-grid">${body}</div>`;
        root.appendChild(panel);
      });
      await loadStructureRecommendations();
      await loadConfigBackups();
    }
    function configCurrentText(field) {
      if (!field.configured && !field.value && !field.display_value) return "当前未配置";
      if (field.kind === "bool") return `当前使用：${zhBool(field.value)}`;
      const display = field.display_value || field.value || "已配置";
      if (field.source === "auto_route") return `当前使用：${display}（自动话题：${field.route_name || "已记录"}）`;
      return `当前使用：${display}`;
    }
    function fieldHtml(field) {
      const key = escapeHtml(field.key);
      const label = escapeHtml(field.label);
      const current = escapeHtml(configCurrentText(field));
      const helpParts = [];
      if (field.help) helpParts.push(escapeHtml(field.help));
      if (field.source === "auto_route") helpParts.push("当前 ID 来自自动创建的话题路由文件；输入新值并保存后会写入 .env.oi。");
      const help = helpParts.map(text => `<div class="field-help">${text}</div>`).join("");
      if (field.kind === "bool") {
        const raw = String(field.value || "").trim().toLowerCase();
        const selectedTrue = ["true", "1", "yes", "on", "y"].includes(raw) ? "selected" : "";
        const selectedFalse = ["false", "0", "no", "off", "n"].includes(raw) ? "selected" : "";
        return `<div class="field"><div class="field-heading"><label>${label}</label><span class="field-current">${current}</span></div><select data-key="${key}"><option value="true" ${selectedTrue}>开启</option><option value="false" ${selectedFalse}>关闭</option></select>${help}</div>`;
      }
      if (field.secret) {
        return `<div class="field"><div class="field-heading"><label>${label}</label><span class="field-current">${current}</span></div><div class="secret-row"><input data-key="${key}" type="password" placeholder="输入新值才会替换当前值"><button class="btn" type="button" onclick="clearSecret('${key}')">清空</button></div><div class="field-help">当前值会完整显示；输入新值才会替换当前值，留空保存不会改动。</div>${help}</div>`;
      }
      return `<div class="field"><div class="field-heading"><label>${label}</label><span class="field-current">${current}</span></div><input data-key="${key}" value="${escapeHtml(field.value || "")}">${help}</div>`;
    }
    const clearKeys = new Set();
    function clearSecret(key) {
      clearKeys.add(key);
      const input = document.querySelector(`[data-key="${key}"]`);
      if (input) input.value = "";
      document.getElementById("configOutput").textContent = `${key} 已标记为清空，保存后生效`;
    }
    function configFieldMap() {
      const map = {};
      Object.values((latestConfigData && latestConfigData.sections) || {}).flat().forEach(field => {
        map[field.key] = field;
      });
      return map;
    }
    function gatherConfigUpdates() {
      const updates = {};
      document.querySelectorAll("#configForms [data-key]").forEach(el => {
        if (el.type === "password" && !el.value && !clearKeys.has(el.dataset.key)) return;
        updates[el.dataset.key] = el.value;
      });
      return updates;
    }
    function buildConfigChanges(updates) {
      const fields = configFieldMap();
      return Object.entries(updates).flatMap(([key, value]) => {
        const field = fields[key] || { key, label: key, value: "" };
        const oldValue = String(field.value || "");
        const newValue = clearKeys.has(key) ? "" : String(value || "");
        if (oldValue === newValue) return [];
        const oldText = oldValue || (field.source === "auto_route" ? `${field.display_value}（自动话题）` : "空");
        const newText = newValue || "空";
        return [{ key, label: field.label || key, oldText, newText }];
      });
    }
    function renderConfigChanges(changes) {
      const target = document.getElementById("configPreview");
      if (!changes.length) {
        target.classList.remove("hidden");
        target.innerHTML = `<h3 class="section-title">配置改动预览</h3><div class="hint">没有检测到需要保存的改动。</div>`;
        return;
      }
      target.classList.remove("hidden");
      target.innerHTML = `<h3 class="section-title">配置改动预览</h3>
        <div class="readable-list">${changes.map(item => row(`${item.label} (${item.key})`, `<strong>${escapeHtml(item.oldText)}</strong> -> <strong>${escapeHtml(item.newText)}</strong>`)).join("")}</div>`;
    }
    function previewConfig() {
      const updates = gatherConfigUpdates();
      const changes = buildConfigChanges(updates);
      renderConfigChanges(changes);
      return changes;
    }
    function formatSaveResult(data, changes) {
      const lines = [];
      lines.push(data.ok ? "配置保存成功" : "配置保存失败");
      if (data.message) lines.push(`结果：${data.message}`);
      if (changes && changes.length) {
        lines.push("");
        lines.push("本次改动：");
        changes.forEach(item => lines.push(`- ${item.label} (${item.key}): ${item.oldText} -> ${item.newText}`));
      }
      if (data.backup) lines.push(`备份文件：${data.backup}`);
      const applyResults = (data.apply && data.apply.results) || [];
      if (applyResults.length) {
        lines.push("");
        lines.push("自动应用：");
        applyResults.forEach(item => lines.push(`- ${item.service || item.name || "服务"} ${item.action || ""}: ${item.ok ? "成功" : "失败"}`));
      }
      if (!data.ok && data.errors) {
        lines.push("");
        lines.push("错误：");
        Object.entries(data.errors).forEach(([key, value]) => lines.push(`- ${key}: ${value}`));
      }
      return lines.join("\n");
    }
    async function loadConfigBackups() {
      const box = document.getElementById("configBackupList");
      if (!box) return;
      const data = await api("/api/config-backups");
      const backups = data.backups || [];
      box.innerHTML = backups.length ? backups.map(item => `
        <div class="feature-item">
          <strong>${escapeHtml(item.name)}</strong>
          <span class="muted">${escapeHtml(item.modified_at || "")} · ${escapeHtml(String(item.size || 0))} 字节</span>
          <div class="toolbar" style="margin:8px 0 0">
            <button class="btn warn" type="button" onclick="restoreConfigBackup('${escapeHtml(item.name)}')">恢复这个备份</button>
          </div>
        </div>
      `).join("") : `<div class="hint">还没有 Web 保存产生的配置备份。</div>`;
    }
    async function restoreConfigBackup(name) {
      const confirmText = prompt(`恢复配置备份会覆盖当前 .env.oi，并自动应用。输入 RESTORE 确认：${name}`);
      if (confirmText !== "RESTORE") return;
      const data = await api("/api/config-restore", { method: "POST", body: JSON.stringify({ name }) });
      document.getElementById("configOutput").textContent = formatSaveResult(data, []);
      await loadConfig();
    }
    async function loadStructureRecommendations() {
      const box = document.getElementById("structureRecommendationBox");
      if (!box) return;
      const data = await api("/api/structure-recommendations");
      const items = data.recommendations || [];
      if (!items.length) {
        box.innerHTML = `<div class="hint">${escapeHtml(data.message || "暂无可应用建议。")}</div>`;
        return;
      }
      box.innerHTML = items.map(item => `
        <div class="feature-item">
          <strong>${escapeHtml(item.label || item.key)} (${escapeHtml(item.key)})</strong>
          <span class="muted">建议：${escapeHtml(String(item.current))} -> ${escapeHtml(String(item.suggested))}</span>
          <div class="hint">${escapeHtml(item.reason || "")}</div>
        </div>
      `).join("") + `<div class="toolbar" style="margin:8px 0 0"><button class="btn primary" type="button" onclick="applyStructureRecommendations()">应用这些建议并保存</button></div>`;
    }
    async function applyStructureRecommendations() {
      const data = await api("/api/structure-recommendations");
      const updates = data.updates || {};
      const keys = Object.keys(updates);
      if (!keys.length) {
        document.getElementById("configOutput").textContent = data.message || "暂无可应用建议";
        return;
      }
      if (!confirm(`将应用 ${keys.length} 条结构复盘建议并自动保存，是否继续？`)) return;
      Object.entries(updates).forEach(([key, value]) => {
        const input = document.querySelector(`#configForms [data-key="${key}"]`);
        if (input) input.value = value;
      });
      await saveConfig();
    }
    async function saveConfig() {
      const updates = gatherConfigUpdates();
      const changes = buildConfigChanges(updates);
      renderConfigChanges(changes);
      if (!changes.length) {
        document.getElementById("configOutput").textContent = "没有检测到需要保存的改动。";
        return;
      }
      if (!confirm(`即将保存 ${changes.length} 项配置改动，并自动应用。是否继续？`)) return;
      const data = await api("/api/config", {
        method: "POST",
        body: JSON.stringify({ updates, clear: Array.from(clearKeys) })
      });
      document.getElementById("configOutput").textContent = formatSaveResult(data, changes);
      if (data.ok && updates.WEB_ADMIN_TOKEN) localStorage.setItem("paopaoAdminToken", updates.WEB_ADMIN_TOKEN);
      if (data.ok && clearKeys.has("WEB_ADMIN_TOKEN")) localStorage.removeItem("paopaoAdminToken");
      clearKeys.clear();
      try {
        await loadConfig();
      } catch (err) {
        setSubtitle("配置已保存，后台服务正在自动应用；如果页面短暂断开，稍后刷新即可");
      }
    }
    function renderActions() {
      document.getElementById("actionGrid").innerHTML = `
        <div class="panel span-12 notice">
          <strong>这里的按钮都是固定白名单动作。</strong>
          不能输入任意命令；点击后只会执行页面写明的检查、测试或清理动作。会真实发送或删除文件的动作，卡片里会单独标明。
        </div>
      ` + actionList.map(action => `
        <div class="panel span-6 action-card">
          <div>
            <h3 class="section-title">${escapeHtml(action.label)}</h3>
            <span class="action-badge">${escapeHtml(action.badge)}</span>
          </div>
          <p class="muted">${escapeHtml(action.desc)}</p>
          <ul>${action.details.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
          <button class="btn primary" onclick="runAction('${escapeHtml(action.id)}')">执行</button>
        </div>
      `).join("");
    }
    async function runAction(name) {
      const data = await api("/api/action", { method: "POST", body: JSON.stringify({ name }) });
      document.getElementById("actionOutput").textContent = JSON.stringify(data, null, 2);
    }
    function renderServices() {
      document.getElementById("serviceGrid").innerHTML = `
        <div class="panel span-12 notice">
          <strong>这个页面是控制后台服务开关的，不是普通测试按钮。</strong>
          主服务、结构雷达、Web 控制台是三个不同的后台服务。建议优先使用“重启”，只有确认要暂停某类功能时才点“停止”。
          <div class="service-guide">
            <div class="service-guide-item"><strong>重启</strong><span class="muted">最常用。用于更新代码、修改配置后让服务重新读取设置。</span></div>
            <div class="service-guide-item"><strong>启动</strong><span class="muted">服务已经停止时使用。不会修改配置，只是把服务拉起来。</span></div>
            <div class="service-guide-item"><strong>停止</strong><span class="muted">会暂停对应功能。点击后需要输入 STOP 二次确认。</span></div>
          </div>
        </div>
      ` + serviceGroups.map(group => `
        <div class="panel span-4 service-card">
          <div class="service-card-head">
            <div>
              <h3 class="section-title">${escapeHtml(group.name)}</h3>
              <div class="summary-meta">${escapeHtml(group.service)}</div>
            </div>
            ${neutralPill("系统服务")}
          </div>
          <p class="muted">${escapeHtml(group.desc)}</p>
          ${group.actions.map(action => `
            <div class="service-action">
              <div>
                <div class="service-action-title">${escapeHtml(action.label)}</div>
                <div class="service-action-note">${escapeHtml(action.note)}</div>
              </div>
              <button class="btn ${escapeHtml(action.level)}" onclick="runService('${escapeHtml(action.id)}', '${escapeHtml(action.label)}')">${escapeHtml(action.button)}</button>
            </div>
          `).join("")}
        </div>
      `).join("");
    }
    async function loadGuide() {
      const data = await api("/api/summary");
      const git = data.git || {};
      document.getElementById("guideGrid").innerHTML = `
        <div class="panel span-12">
          <h3 class="section-title">版本信息</h3>
          <div class="kv">
            <div>版本</div><div>${escapeHtml(git.version || "unknown")}</div>
            <div>提交</div><div>${escapeHtml(git.commit || "unknown")}</div>
            <div>分支</div><div>${escapeHtml(git.branch || "unknown")}</div>
            <div>说明</div><div>${escapeHtml(git.subject || "")}</div>
          </div>
        </div>
        ${apiSourcePanel()}
        <div class="panel span-6">
          <h3 class="section-title">页面功能</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>总览</strong><span class="muted">查看主服务、结构雷达、Web 控制台、版本、runtime-status 和关键配置。</span></div>
            <div class="feature-item"><strong>日志</strong><span class="muted">读取主服务、结构雷达、Web 控制台最近日志，支持复制。</span></div>
            <div class="feature-item"><strong>配置</strong><span class="muted">修改 Telegram、话题、Coinalyze、雷达参数和 Web 访问配置；保存前自动备份 .env.oi。</span></div>
            <div class="feature-item"><strong>检查测试</strong><span class="muted">执行固定白名单动作；页面会说明每个按钮检查什么、什么时候用、是否会真实发送消息或清理文件。</span></div>
            <div class="feature-item"><strong>服务控制</strong><span class="muted">启动、停止、重启主服务、结构雷达和 Web 控制台；页面会说明每个服务负责什么，停止操作需要输入 STOP。</span></div>
          </div>
        </div>
        <div class="panel span-6">
          <h3 class="section-title">使用规则</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>访问地址</strong><span class="muted">默认使用 http://服务器IP:8080/。如果你改了 WEB_PORT，按配置里的端口访问。</span></div>
            <div class="feature-item"><strong>登录令牌</strong><span class="muted">输入 WEB_ADMIN_TOKEN。服务器输入 paopao 后选择 1 可查看。不要把令牌发到公开群。</span></div>
            <div class="feature-item"><strong>配置生效</strong><span class="muted">保存配置后会自动应用：主服务和结构雷达会自动重启，Web 端口或令牌变更会让 Web 控制台短暂重启。</span></div>
            <div class="feature-item"><strong>服务器入口</strong><span class="muted">服务器只需要记住 paopao。进入中文菜单后查看 Web 地址、令牌、状态、日志和更新入口。</span></div>
            <div class="feature-item"><strong>安全边界</strong><span class="muted">Web 后端只执行白名单动作，不提供任意 shell 命令入口。</span></div>
          </div>
        </div>
      `;
      setSubtitle(`版本 ${git.version || "unknown"} · ${git.commit || "unknown"}`);
    }
    async function runService(name, label) {
      if (name.startsWith("stop-")) {
        const confirmText = prompt(`输入 STOP 确认：${label}`);
        if (confirmText !== "STOP") return;
      }
      const data = await api("/api/service", { method: "POST", body: JSON.stringify({ name }) });
      document.getElementById("serviceOutput").textContent = JSON.stringify(data, null, 2);
      await loadSummary();
    }
    function switchView(view) {
      currentView = view;
      document.querySelectorAll(".view").forEach(el => el.classList.add("hidden"));
      document.getElementById(view).classList.remove("hidden");
      document.querySelectorAll("nav button").forEach(btn => btn.classList.toggle("active", btn.dataset.view === view));
      document.getElementById("pageTitle").textContent = titles[view];
      refreshCurrent();
    }
    async function refreshCurrent() {
      try {
        if (currentView === "overview") await loadSummary();
        if (currentView === "logs") await loadLogs();
        if (currentView === "config") await loadConfig();
        if (currentView === "actions") { setSubtitle("固定白名单动作，说明写在每张卡片里"); renderActions(); }
        if (currentView === "services") { setSubtitle("后台服务开关，停止前会二次确认"); renderServices(); }
        if (currentView === "guide") await loadGuide();
      } catch (err) {
        setSubtitle(err.message || String(err));
      }
    }
    document.querySelectorAll("nav button").forEach(btn => btn.addEventListener("click", () => switchView(btn.dataset.view)));
    refreshCurrent();
  </script>
</body>
</html>
"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "PaopaoRadarWeb/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[web] {self.address_string()} {fmt % args}\n")

    def send_json(self, data: Any, status: int = 200) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_html(self, html: str) -> None:
        payload = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_json(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0") or 0)
        if size > 128 * 1024:
            raise ValueError("请求体太大")
        raw = self.rfile.read(size).decode("utf-8") if size else "{}"
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def require_auth(self) -> bool:
        if check_auth(self):
            return True
        self.send_json({"ok": False, "error": "unauthorized"}, HTTPStatus.UNAUTHORIZED)
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            self.send_html(INDEX_HTML)
            return
        if not self.require_auth():
            return
        query = parse_qs(parsed.query)
        if path == "/api/summary":
            self.send_json(summary_payload())
            return
        if path == "/api/config":
            self.send_json(config_payload())
            return
        if path == "/api/config-backups":
            self.send_json(env_backup_payload())
            return
        if path == "/api/structure-recommendations":
            self.send_json(structure_review_recommendations_payload())
            return
        if path == "/api/logs":
            target = query.get("target", ["main"])[0]
            lines = int(query.get("lines", ["200"])[0] or 200)
            self.send_json(logs_payload(target, lines))
            return
        self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        path = urlparse(self.path).path
        try:
            data = self.read_json()
            if path == "/api/config":
                updates = data.get("updates", {})
                clear = data.get("clear", [])
                if not isinstance(updates, dict) or not isinstance(clear, list):
                    raise ValueError("updates 必须是对象，clear 必须是数组")
                result = write_env_updates(updates, clear=[str(item) for item in clear])
                if result.get("ok") and result.get("changed"):
                    apply_result = auto_apply_config_changes([str(item) for item in result.get("changed", [])])
                    result["apply"] = apply_result
                    result["message"] = apply_result.get("message", result.get("message"))
                self.send_json(result)
                return
            if path == "/api/config-restore":
                name = str(data.get("name", ""))
                result = restore_env_backup(name)
                if result.get("ok") and result.get("changed"):
                    apply_result = auto_apply_config_changes([str(item) for item in result.get("changed", [])])
                    result["apply"] = apply_result
                    result["message"] = apply_result.get("message", result.get("message"))
                self.send_json(result)
                return
            if path == "/api/action":
                self.send_json(run_cli_action(str(data.get("name", ""))))
                return
            if path == "/api/service":
                self.send_json(run_service_action(str(data.get("name", ""))))
                return
            self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:
            self.send_json({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, HTTPStatus.BAD_REQUEST)


def is_loopback_host(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def run_web_server(host: str = "", port: int = 0, admin_token: str = "") -> int:
    load_env_file()
    host = host or os.getenv("WEB_HOST", "127.0.0.1").strip() or "127.0.0.1"
    port = int(port or os.getenv("WEB_PORT", "8080") or 8080)
    token = admin_token or os.getenv("WEB_ADMIN_TOKEN", "")
    if not is_loopback_host(host) and not token:
        print("web: refused to bind non-loopback host without WEB_ADMIN_TOKEN", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((host, int(port)), WebHandler)
    server.admin_token = token  # type: ignore[attr-defined]
    auth_note = "enabled" if token else "disabled"
    print(f"web: listening on http://{host}:{port} (auth {auth_note})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nweb: stopped")
    finally:
        server.server_close()
    return 0
