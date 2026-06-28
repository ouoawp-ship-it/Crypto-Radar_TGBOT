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
    "readiness": {"label": "检查 readiness", "argv": ["readiness"], "timeout": 45},
    "doctor": {"label": "环境诊断 doctor", "argv": ["doctor"], "timeout": 45},
    "runtime-status": {"label": "查看 runtime-status", "argv": ["runtime-status"], "timeout": 20},
    "announcements-test": {"label": "测试 Binance 公告抓取", "argv": ["announcements-test"], "timeout": 90},
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
    raise ValueError("必须是 true 或 false")


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
      --bg: #f5f7f8;
      --panel: #ffffff;
      --panel-2: #eef3f4;
      --text: #182024;
      --muted: #66757d;
      --line: #d7e0e3;
      --accent: #0f766e;
      --accent-2: #1d4ed8;
      --warn: #a16207;
      --bad: #b42318;
      --good: #087443;
      --shadow: 0 1px 2px rgba(15, 23, 42, .06);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }
    button, input, select { font: inherit; }
    .app { min-height: 100vh; display: grid; grid-template-columns: 220px 1fr; }
    aside {
      background: #172126;
      color: #dbe5e7;
      padding: 18px 14px;
      position: sticky;
      top: 0;
      height: 100vh;
    }
    .brand { font-weight: 700; font-size: 18px; margin: 2px 8px 18px; }
    nav { display: grid; gap: 4px; }
    nav button {
      width: 100%;
      border: 0;
      border-radius: 6px;
      background: transparent;
      color: inherit;
      text-align: left;
      padding: 10px 11px;
      cursor: pointer;
    }
    nav button.active, nav button:hover { background: #26343a; }
    main { padding: 18px 22px 32px; min-width: 0; }
    header {
      display: flex;
      justify-content: space-between;
      gap: 16px;
      align-items: center;
      margin-bottom: 16px;
    }
    h1 { margin: 0; font-size: 22px; }
    .muted { color: var(--muted); }
    .grid { display: grid; grid-template-columns: repeat(12, 1fr); gap: 12px; }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 6px;
      box-shadow: var(--shadow);
      padding: 14px;
      min-width: 0;
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
    }
    .status.ok { background: #dff7ea; color: var(--good); }
    .status.bad { background: #ffe4df; color: var(--bad); }
    .toolbar { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; margin-bottom: 12px; }
    .btn {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      border-radius: 6px;
      padding: 8px 11px;
      cursor: pointer;
      min-height: 36px;
    }
    .btn.primary { background: var(--accent); border-color: var(--accent); color: white; }
    .btn.blue { background: var(--accent-2); border-color: var(--accent-2); color: white; }
    .btn.warn { border-color: #d6a01d; color: var(--warn); }
    .btn.danger { border-color: #f2b8ad; color: var(--bad); }
    .btn:disabled { opacity: .55; cursor: not-allowed; }
    pre {
      margin: 0;
      background: #0f1720;
      color: #d7e3ea;
      border-radius: 6px;
      padding: 13px;
      min-height: 320px;
      max-height: 640px;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
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
      background: white;
      color: var(--text);
      min-height: 36px;
    }
    .secret-row { display: grid; grid-template-columns: 1fr auto; gap: 8px; }
    .section-title { margin: 2px 0 10px; font-size: 15px; }
    .output { margin-top: 12px; }
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
      background: white;
      border-radius: 6px;
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
    </main>
  </div>

  <script>
    const titles = {
      overview: "总览",
      logs: "日志",
      config: "配置",
      actions: "检查测试",
      services: "服务控制"
    };
    const actionList = [
      ["readiness", "检查 readiness", "读取真实推送准备度"],
      ["runtime-status", "查看 runtime-status", "读取主服务和结构服务状态文件"],
      ["doctor", "环境诊断 doctor", "输出配置和状态文件诊断"],
      ["telegram-test", "发送 Telegram 测试", "真实发送一条测试消息"],
      ["announcements-test", "测试 Binance 公告", "抓取并分类公告"],
      ["structure-review", "结构信号复盘", "生成 dry-run 复盘报告"],
      ["cleanup", "清理运行垃圾", "立即执行 cleanup"]
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
    function statusPill(text, ok) {
      return `<span class="status ${ok ? "ok" : "bad"}">${escapeHtml(text || "unknown")}</span>`;
    }
    function escapeHtml(value) {
      return String(value ?? "").replace(/[&<>"']/g, s => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[s]));
    }
    function metric(label, value, extra = "") {
      return `<div class="panel span-3 metric"><div class="label">${label}</div><div class="value">${value}</div>${extra}</div>`;
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
        metric("主服务", statusPill(main.active, main.active_ok), `<div class="muted">${escapeHtml(main.service)}</div>`),
        metric("结构雷达", statusPill(structure.active, structure.active_ok), `<div class="muted">${escapeHtml(structure.service)}</div>`),
        metric("Web 控制台", statusPill(web.active, web.active_ok), `<div class="muted">${escapeHtml(web.service)}</div>`),
        metric("版本", escapeHtml(git.version || "unknown"), `<div class="muted">${escapeHtml(git.branch)} ${escapeHtml(git.commit)}</div>`),
        metric("配置文件", cfg.env_file_exists ? statusPill("存在", true) : statusPill("缺失", false), ""),
        `<div class="panel span-6"><h3 class="section-title">运行状态</h3><pre>${escapeHtml(JSON.stringify(runtime, null, 2))}</pre></div>`,
        `<div class="panel span-6"><h3 class="section-title">关键配置</h3><pre>${escapeHtml(JSON.stringify(cfg, null, 2))}</pre></div>`
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
    function fieldHtml(field) {
      const key = escapeHtml(field.key);
      const label = escapeHtml(field.label);
      if (field.kind === "bool") {
        const selectedTrue = String(field.value).toLowerCase() === "true" ? "selected" : "";
        const selectedFalse = String(field.value).toLowerCase() === "false" ? "selected" : "";
        return `<div class="field"><label>${label}</label><select data-key="${key}"><option value="true" ${selectedTrue}>true</option><option value="false" ${selectedFalse}>false</option></select></div>`;
      }
      if (field.secret) {
        return `<div class="field"><label>${label} <span class="muted">${escapeHtml(field.masked || "未配置")}</span></label><div class="secret-row"><input data-key="${key}" type="password" placeholder="留空不修改"><button class="btn" type="button" onclick="clearSecret('${key}')">清空</button></div></div>`;
      }
      return `<div class="field"><label>${label}</label><input data-key="${key}" value="${escapeHtml(field.value || "")}"></div>`;
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
      document.getElementById("actionGrid").innerHTML = actionList.map(([id, label, desc]) => `
        <div class="panel span-4">
          <h3 class="section-title">${label}</h3>
          <p class="muted">${desc}</p>
          <button class="btn primary" onclick="runAction('${id}')">执行</button>
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
        if (currentView === "actions") { setSubtitle("白名单命令"); renderActions(); }
        if (currentView === "services") { setSubtitle("systemd 白名单动作"); renderServices(); }
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
        if parsed.path == "/":
            self.send_html(INDEX_HTML)
            return
        if not self.require_auth():
            return
        query = parse_qs(parsed.query)
        if parsed.path == "/api/summary":
            self.send_json(summary_payload())
            return
        if parsed.path == "/api/config":
            self.send_json(config_payload())
            return
        if parsed.path == "/api/logs":
            target = query.get("target", ["main"])[0]
            lines = int(query.get("lines", ["200"])[0] or 200)
            self.send_json(logs_payload(target, lines))
            return
        self.send_json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        parsed = urlparse(self.path)
        try:
            data = self.read_json()
            if parsed.path == "/api/config":
                updates = data.get("updates", {})
                clear = data.get("clear", [])
                if not isinstance(updates, dict) or not isinstance(clear, list):
                    raise ValueError("updates 必须是对象，clear 必须是数组")
                self.send_json(write_env_updates(updates, clear=[str(item) for item in clear]))
                return
            if parsed.path == "/api/action":
                self.send_json(run_cli_action(str(data.get("name", ""))))
                return
            if parsed.path == "/api/service":
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
