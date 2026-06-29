from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
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


@dataclass(frozen=True)
class ConfigField:
    key: str
    label: str
    section: str
    kind: str = "text"
    secret: bool = False
    minimum: float | None = None
    maximum: float | None = None


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
    ConfigField("STRUCTURE_MIN_SCORE", "结构雷达最低分", "雷达参数", kind="int", minimum=0, maximum=100),
    ConfigField("LIQUIDITY_SCORE_MAX_DELTA", "外部确认修正上限", "雷达参数", kind="int", minimum=0, maximum=30),
    ConfigField("LIQUIDITY_MIN_DISTANCE_PCT", "盘口墙最小距离 %", "雷达参数", kind="float", minimum=0),
    ConfigField("LIQUIDITY_MAX_DISTANCE_PCT", "盘口墙最大距离 %", "雷达参数", kind="float", minimum=0.1),
    ConfigField("BINANCE_ORDERBOOK_DEPTH_LIMIT", "Binance 盘口档位", "雷达参数", kind="int", minimum=5, maximum=1000),
)
EDITABLE_CONFIG: dict[str, ConfigField] = {field.key: field for field in EDITABLE_CONFIG_FIELDS}


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


def config_payload(path: Path | None = None) -> dict[str, Any]:
    values = read_env_values(path)
    sections: dict[str, list[dict[str, Any]]] = {}
    for field in EDITABLE_CONFIG_FIELDS:
        raw_value = values.get(field.key, "")
        item = {
            "key": field.key,
            "label": field.label,
            "kind": field.kind,
            "secret": field.secret,
            "configured": bool(raw_value),
            "value": "" if field.secret else raw_value,
            "display_value": mask_secret(raw_value) if field.secret else raw_value,
            "masked": mask_secret(raw_value) if field.secret else "",
            "minimum": field.minimum,
            "maximum": field.maximum,
        }
        sections.setdefault(field.section, []).append(item)
    return {"env_file": str(path or ENV_FILE), "sections": sections}


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
    shutil.copy2(path, backup)
    return backup


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
            "telegram": redacted.get("telegram"),
            "liquidity": redacted.get("liquidity"),
            "coinalyze": redacted.get("coinalyze"),
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
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 10px;
    }
    .field-current {
      display: inline-flex;
      align-items: center;
      max-width: 62%;
      border: 1px solid rgba(93, 106, 115, .24);
      border-radius: 999px;
      padding: 2px 8px;
      background: linear-gradient(135deg, rgba(255,255,255,.78), rgba(236,241,244,.72));
      color: #48565d;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
      overflow: hidden;
      text-overflow: ellipsis;
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
    .action-card { display: grid; gap: 10px; align-content: start; }
    .action-card ul {
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.58;
    }
    .action-card li { margin: 4px 0; }
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
      .form-grid { grid-template-columns: 1fr; }
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
          <button class="btn primary" onclick="saveConfig()">保存配置</button>
        </div>
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
          "适合确认服务是否真的在循环运行，而不是只看 systemd 是否显示运行中。",
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
          "不会删除 .env.oi、源码、systemd 服务文件和关键配置。",
          "适合磁盘空间变多、图表文件堆积、历史记录太长时手动执行。",
          "输出会列出删除、保留和裁剪了哪些文件。"
        ]
      }
    ];
    const serviceList = [
      ["restart-main", "重启主服务", "paopao-radar", "warn"],
      ["start-main", "启动主服务", "paopao-radar", ""],
      ["stop-main", "停止主服务", "paopao-radar", "danger"],
      ["restart-structure", "重启结构雷达", "paopao-structure", "warn"],
      ["start-structure", "启动结构雷达", "paopao-structure", ""],
      ["stop-structure", "停止结构雷达", "paopao-structure", "danger"],
      ["restart-web", "重启 Web 控制台", "paopao-web", "warn"],
      ["start-web", "启动 Web 控制台", "paopao-web", ""],
      ["stop-web", "停止 Web 控制台", "paopao-web", "danger"]
    ];
    let currentView = "overview";

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
      setSubtitle(data.env_file);
      const root = document.getElementById("configForms");
      root.innerHTML = "";
      Object.entries(data.sections || {}).forEach(([section, fields]) => {
        const panel = document.createElement("div");
        panel.className = "panel span-12";
        const body = fields.map(fieldHtml).join("");
        panel.innerHTML = `<h3 class="section-title">${escapeHtml(section)}</h3><div class="form-grid">${body}</div>`;
        root.appendChild(panel);
      });
    }
    function configCurrentText(field) {
      if (!field.configured && !field.value && !field.display_value) return "当前未配置";
      if (field.kind === "bool") return `当前使用：${zhBool(field.value)}`;
      const display = field.secret ? (field.masked || field.display_value || "已配置") : (field.display_value || field.value || "已配置");
      return `当前使用：${display}`;
    }
    function fieldHtml(field) {
      const key = escapeHtml(field.key);
      const label = escapeHtml(field.label);
      const current = escapeHtml(configCurrentText(field));
      if (field.kind === "bool") {
        const raw = String(field.value || "").trim().toLowerCase();
        const selectedTrue = ["true", "1", "yes", "on", "y"].includes(raw) ? "selected" : "";
        const selectedFalse = ["false", "0", "no", "off", "n"].includes(raw) ? "selected" : "";
        return `<div class="field"><div class="field-heading"><label>${label}</label><span class="field-current">${current}</span></div><select data-key="${key}"><option value="true" ${selectedTrue}>开启</option><option value="false" ${selectedFalse}>关闭</option></select></div>`;
      }
      if (field.secret) {
        return `<div class="field"><div class="field-heading"><label>${label}</label><span class="field-current">${current}</span></div><div class="secret-row"><input data-key="${key}" type="password" placeholder="输入新值才会替换当前值"><button class="btn" type="button" onclick="clearSecret('${key}')">清空</button></div><div class="field-help">输入新值才会替换当前值；安全起见只显示遮罩值，留空保存不会改动。</div></div>`;
      }
      return `<div class="field"><div class="field-heading"><label>${label}</label><span class="field-current">${current}</span></div><input data-key="${key}" value="${escapeHtml(field.value || "")}"></div>`;
    }
    const clearKeys = new Set();
    function clearSecret(key) {
      clearKeys.add(key);
      const input = document.querySelector(`[data-key="${key}"]`);
      if (input) input.value = "";
      document.getElementById("configOutput").textContent = `${key} 已标记为清空，保存后生效`;
    }
    async function saveConfig() {
      const updates = {};
      document.querySelectorAll("#configForms [data-key]").forEach(el => {
        if (el.type === "password" && !el.value && !clearKeys.has(el.dataset.key)) return;
        updates[el.dataset.key] = el.value;
      });
      const data = await api("/api/config", {
        method: "POST",
        body: JSON.stringify({ updates, clear: Array.from(clearKeys) })
      });
      document.getElementById("configOutput").textContent = JSON.stringify(data, null, 2);
      clearKeys.clear();
      await loadConfig();
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
      document.getElementById("serviceGrid").innerHTML = serviceList.map(([id, label, desc, level]) => `
        <div class="panel span-4">
          <h3 class="section-title">${label}</h3>
          <p class="muted">${desc}</p>
          <button class="btn ${level}" onclick="runService('${id}', '${label}')">执行</button>
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
        <div class="panel span-6">
          <h3 class="section-title">页面功能</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>总览</strong><span class="muted">查看主服务、结构雷达、Web 控制台、版本、runtime-status 和关键配置。</span></div>
            <div class="feature-item"><strong>日志</strong><span class="muted">读取主服务、结构雷达、Web 控制台最近日志，支持复制。</span></div>
            <div class="feature-item"><strong>配置</strong><span class="muted">修改 Telegram、话题、Coinalyze、雷达参数和 Web 访问配置；保存前自动备份 .env.oi。</span></div>
            <div class="feature-item"><strong>检查测试</strong><span class="muted">执行固定白名单动作；页面会说明每个按钮检查什么、什么时候用、是否会真实发送消息或清理文件。</span></div>
            <div class="feature-item"><strong>服务控制</strong><span class="muted">启动、停止、重启主服务、结构雷达和 Web 控制台；停止操作需要输入 STOP。</span></div>
          </div>
        </div>
        <div class="panel span-6">
          <h3 class="section-title">使用规则</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>访问地址</strong><span class="muted">默认使用 http://服务器IP:8080/。如果你改了 WEB_PORT，按配置里的端口访问。</span></div>
            <div class="feature-item"><strong>登录令牌</strong><span class="muted">输入 WEB_ADMIN_TOKEN。服务器输入 paopao 后选择 1 可查看。不要把令牌发到公开群。</span></div>
            <div class="feature-item"><strong>配置生效</strong><span class="muted">保存 .env.oi 后，后台运行中的服务通常需要重启才会读取新配置。</span></div>
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
        if (currentView === "services") { setSubtitle("systemd 白名单动作"); renderServices(); }
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
                self.send_json(write_env_updates(updates, clear=[str(item) for item in clear]))
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
