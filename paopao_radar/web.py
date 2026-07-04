from __future__ import annotations

import json
import os
import re
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

from .ai_prompts import load_ai_prompts, reset_ai_prompts, save_ai_prompts
from .config import BASE_DIR, ENV_FILE, Settings, load_env_file, normalize_ai_model
from .storage import JsonStore


MAIN_SERVICE = os.getenv("SERVICE_NAME", "paopao-radar")
STRUCTURE_SERVICE = os.getenv("STRUCTURE_SERVICE_NAME", "paopao-structure")
WEB_SERVICE = os.getenv("WEB_SERVICE_NAME", "paopao-web")
AI_SERVICE = os.getenv("AI_SERVICE_NAME", "paopao-ai")
WEB_CONFIG_KEYS = {"WEB_HOST", "WEB_PORT", "WEB_ADMIN_TOKEN"}
SIGNAL_EVENT_CONFIG_KEYS = {
    "SIGNAL_EVENTS_FILE",
    "SIGNAL_EVENTS_LIMIT",
    "SIGNAL_EVENTS_RETENTION_DAYS",
}
AI_CONFIG_KEYS = {
    "AI_ASSISTANT_ENABLE",
    "AI_BOT_TOKEN",
    "AI_ADMIN_USER_IDS",
    "AI_ALLOW_GROUP_CHAT",
    "AI_ALLOWED_CHAT_IDS",
    "AI_PRICE_ALERTS_ENABLE",
    "AI_PRICE_ALERTS_DB_FILE",
    "AI_DEFAULT_CHAT_ID",
    "AI_ALERT_CHECK_INTERVAL_SEC",
    "AI_POLL_TIMEOUT_SEC",
    "AI_PROVIDER_ENABLE",
    "AI_API_KEY",
    "AI_BASE_URL",
    "AI_MODEL",
    "AI_REQUEST_TIMEOUT_SEC",
    "AI_PROMPTS_FILE",
} | SIGNAL_EVENT_CONFIG_KEYS
WEB_AUDIT_LOG_FILE = "web_audit_log.json"
WEB_AUDIT_LIMIT = 1000


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
    ConfigField("TG_FUNDING_ALERT_TOPIC_ID", "资金费率警报话题 ID", "Telegram"),
    ConfigField("STRUCTURE_TOPIC_ID", "结构雷达话题 ID", "Telegram"),
    ConfigField("STRUCTURE_REVIEW_TOPIC_ID", "结构复盘话题 ID", "Telegram"),
    ConfigField("AI_ASSISTANT_ENABLE", "启用 AI 助手 Bot", "AI 助手", kind="bool", help="开启后 paopao-ai 服务会使用独立 AI_BOT_TOKEN 处理私聊和价格提醒。"),
    ConfigField("AI_BOT_TOKEN", "AI 助手 Bot Token", "AI 助手", secret=True, help="建议用 BotFather 单独创建一个机器人，不要和群推送 Bot 共用。"),
    ConfigField("AI_ADMIN_USER_IDS", "允许使用的 Telegram 用户 ID", "AI 助手", help="多个 ID 用英文逗号分隔。留空表示不限制用户，不建议公开使用。"),
    ConfigField("AI_ALLOW_GROUP_CHAT", "允许群内调用 AI 助手", "AI 助手", kind="bool", help="默认关闭。开启后群里也必须 @机器人 或回复机器人消息才会处理，普通群聊不会触发。"),
    ConfigField("AI_ALLOWED_CHAT_IDS", "允许调用的群/频道 ID", "AI 助手", help="开启群内调用后必须填写。多个用英文逗号分隔，例如 -1001234567890,-1009876543210 或 @channel_username。"),
    ConfigField("AI_PRICE_ALERTS_ENABLE", "启用价格提醒", "AI 助手", kind="bool", help="Telegram 私聊里按按钮手动选择 Binance、Bybit、OKX、Bitget、Gate 的现货或 USDT 合约价格源。"),
    ConfigField("AI_DEFAULT_CHAT_ID", "Web 创建提醒默认接收 ID", "AI 助手", help="通常填你的 Telegram 用户 ID；Telegram 私聊创建提醒时会自动使用当前私聊。"),
    ConfigField("AI_ALERT_CHECK_INTERVAL_SEC", "价格提醒检查间隔秒数", "AI 助手", kind="int", minimum=3, maximum=3600, help="建议 5-10 秒。越小越实时，但请求更频繁。"),
    ConfigField("AI_PROVIDER_ENABLE", "启用 AI 问答接口", "AI 助手", kind="bool", help="关闭时仍可使用价格提醒和本地状态助手。"),
    ConfigField("AI_API_KEY", "AI API Key", "AI 助手", secret=True, help="兼容 OpenAI 格式的接口 Key，例如 DeepSeek/OpenAI 兼容服务。"),
    ConfigField("AI_BASE_URL", "AI 接口地址", "AI 助手", help="例如 https://api.deepseek.com 或其他 OpenAI-compatible 地址。"),
    ConfigField("AI_MODEL", "AI 模型名称", "AI 助手", help="例如 deepseek-v4-pro。"),
    ConfigField("AI_REQUEST_TIMEOUT_SEC", "AI 请求超时秒数", "AI 助手", kind="int", minimum=5, maximum=300, help="deepseek-v4-pro 思考模式建议 90-180 秒；如果经常超时就调大，或者改用 deepseek-v4-flash。"),
    ConfigField("AI_PROMPTS_FILE", "AI 提示词文件", "AI 助手", help="默认 ai_prompts.json，存放在 data 目录下。一般不需要修改。"),
    ConfigField("SIGNAL_EVENTS_FILE", "币种档案信号索引文件", "AI 助手", help="默认 signal_events.json，存放在 data 目录下。一般不需要修改。"),
    ConfigField("SIGNAL_EVENTS_LIMIT", "币种档案信号保留数量", "AI 助手", kind="int", minimum=100, maximum=50000, help="AI 查询币种时会读取最近的结构化信号事件。建议 5000。"),
    ConfigField("SIGNAL_EVENTS_RETENTION_DAYS", "币种档案信号保留天数", "AI 助手", kind="int", minimum=1, maximum=365, help="超过该天数的信号事件会在新事件写入时自动清理。建议 60。"),
    ConfigField("TG_TOPIC_INTRO_ENABLE", "发送话题说明", "模块开关", kind="bool"),
    ConfigField("TG_TOPIC_INTRO_PIN", "置顶话题说明", "模块开关", kind="bool"),
    ConfigField("CLEANUP_ENABLE", "自动清理", "模块开关", kind="bool"),
    ConfigField("LIQUIDITY_FALLBACK_ENABLE", "结构外部确认", "模块开关", kind="bool"),
    ConfigField("BINANCE_ORDERBOOK_LIQUIDITY_ENABLE", "Binance 盘口确认", "模块开关", kind="bool"),
    ConfigField("STRUCTURE_RADAR_ENABLE", "结构雷达", "模块开关", kind="bool"),
    ConfigField("STRUCTURE_REVIEW_ENABLE", "结构复盘", "模块开关", kind="bool"),
    ConfigField("WEB_HOST", "Web 监听地址", "Web 控制台"),
    ConfigField("WEB_PORT", "Web 端口", "Web 控制台", kind="int", minimum=1, maximum=65535),
    ConfigField("WEB_ADMIN_TOKEN", "Web 访问令牌", "Web 控制台", secret=True),
    ConfigField("COINALYZE_ENABLE", "启用 Coinalyze", "Coinalyze", kind="bool"),
    ConfigField("COINALYZE_API_KEY", "Coinalyze API Key", "Coinalyze", secret=True),
    ConfigField("RADAR_SUMMARY_MIN_INTERVAL_SEC", "资金摘要间隔秒", "雷达参数", kind="int", minimum=300, help="建议 21600 秒（6 小时）。越小推送越频繁，越大越安静。"),
    ConfigField("FLOW_INTERVAL_SEC", "资金流窗口秒", "雷达参数", kind="int", minimum=300, help="建议 3600 秒（1 小时）。资金流按完整闭合窗口统计。"),
    ConfigField("FLOW_SCAN_LIMIT", "资金流扫描数量", "雷达参数", kind="int", minimum=1, maximum=300, help="建议 8-30。越大覆盖越多币，但请求和计算更重。"),
    ConfigField("FUNDING_ALERT_ENABLE", "启用资金费率警报", "资金费率警报", kind="bool", help="独立扫描多交易所资金费率异常，并推送到资金费率警报话题。"),
    ConfigField("FUNDING_ALERT_INTERVAL_SEC", "资金费率扫描间隔秒", "资金费率警报", kind="int", minimum=60, maximum=86400, help="建议 180-300 秒。越小越及时，但请求更多。"),
    ConfigField("FUNDING_ALERT_SCAN_LIMIT", "资金费率扫描数量", "资金费率警报", kind="int", minimum=1, maximum=300, help="按 Binance 24h 成交额排序扫描前 N 个 USDT 合约。"),
    ConfigField("FUNDING_ALERT_EXCHANGES", "资金费率交易所", "资金费率警报", help="英文逗号分隔，默认 BINANCE,OKX,BYBIT,BITGET,GATE。"),
    ConfigField("FUNDING_ALERT_EXTREME_NEGATIVE_PCT", "极负阈值 %", "资金费率警报", kind="float", maximum=0, help="低于该值触发极负警报，默认 -0.5。"),
    ConfigField("FUNDING_ALERT_SUPER_NEGATIVE_PCT", "超极负阈值 %", "资金费率警报", kind="float", maximum=0, help="低于该值标为极端风险，默认 -1.0。"),
    ConfigField("FUNDING_ALERT_EXTREME_POSITIVE_PCT", "极正阈值 %", "资金费率警报", kind="float", minimum=0, help="高于该值触发多头过热警报，默认 0.5。"),
    ConfigField("FUNDING_ALERT_MIN_EXCHANGE_COUNT", "共振交易所数量", "资金费率警报", kind="int", minimum=1, maximum=5, help="达到多少家交易所同时异常时标为共振，默认 2。"),
    ConfigField("FUNDING_ALERT_DIVERGENCE_PCT", "交易所偏离阈值 %", "资金费率警报", kind="float", minimum=0, help="最高费率和最低费率差超过该值时提示单所偏离，默认 0.75。"),
    ConfigField("FUNDING_ALERT_COOLDOWN_SEC", "资金费率警报冷却秒", "资金费率警报", kind="int", minimum=60, help="同币同类警报冷却时间，默认 3600 秒。"),
    ConfigField("FUNDING_ALERT_REPLY_CHAIN_ENABLE", "同币回复上一条", "资金费率警报", kind="bool", help="开启后，同一个币第二次及后续资金费率警报会回复上一条同币消息，方便追踪。"),
    ConfigField("FUNDING_ALERT_DECAY_QUIET_SCANS", "热度衰减安静轮数", "资金费率警报", kind="int", minimum=1, maximum=20, help="连续多少轮不再达标后，回复提示热度衰减，默认 2。"),
    ConfigField("FUNDING_ALERT_END_QUIET_SCANS", "观察结束安静轮数", "资金费率警报", kind="int", minimum=1, maximum=50, help="连续多少轮不再达标后，状态标为观察结束，默认 5。"),
    ConfigField("LAUNCH_MULTI_EXCHANGE_FUNDING_ENABLE", "启动多交易所资金费率", "雷达参数", kind="bool", help="开启后，启动预警推送会显示 Binance、OKX、Bybit、Bitget、Gate 的实时资金费率和结算时间。"),
    ConfigField("LAUNCH_FUNDING_EXCHANGES", "启动资金费率交易所", "雷达参数", help="英文逗号分隔，默认 BINANCE,OKX,BYBIT,BITGET,GATE。可删掉你不想请求的交易所。"),
    ConfigField("LAUNCH_FUNDING_HISTORY_LIMIT", "资金费率历史条数", "雷达参数", kind="int", minimum=3, maximum=20, help="用于判断结算周期是否从 8H 缩短到 4H 或 1H。建议 4-6。"),
    ConfigField("STRUCTURE_TOP_SYMBOLS", "结构雷达扫描数量", "雷达参数", kind="int", minimum=1, maximum=300, help="建议 50-120。越大覆盖越多币，但结构扫描耗时更长。"),
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
    ConfigField("LIQUIDITY_SCORE_MAX_DELTA", "外部确认修正上限", "雷达参数", kind="int", minimum=0, maximum=30, help="建议 10-15。越高外部确认对结构分数影响越大。"),
    ConfigField("LIQUIDITY_MIN_DISTANCE_PCT", "盘口墙最小距离 %", "雷达参数", kind="float", minimum=0, help="建议 0.5。忽略距离现价太近的盘口墙，减少噪音。"),
    ConfigField("LIQUIDITY_MAX_DISTANCE_PCT", "盘口墙最大距离 %", "雷达参数", kind="float", minimum=0.1, help="建议 5-8。越大越容易找到远处盘口墙，但参考价值会下降。"),
    ConfigField("BINANCE_ORDERBOOK_DEPTH_LIMIT", "Binance 盘口档位", "雷达参数", kind="int", minimum=5, maximum=1000, help="建议 100。越大盘口更完整，但请求数据更重。"),
)
EDITABLE_CONFIG: dict[str, ConfigField] = {field.key: field for field in EDITABLE_CONFIG_FIELDS}

TOPIC_FIELD_ROUTES: dict[str, tuple[str, str]] = {
    "TG_RADAR_SUMMARY_TOPIC_ID": ("TG_RADAR_SUMMARY", "资金摘要"),
    "TG_LAUNCH_ALERT_TOPIC_ID": ("TG_LAUNCH_ALERT", "启动预警"),
    "TG_ANNOUNCEMENT_ALERT_TOPIC_ID": ("TG_ANNOUNCEMENT_ALERT", "公告风险"),
    "TG_TEST_TOPIC_ID": ("TG_TEST_MESSAGE", "测试消息"),
    "TG_FLOW_RADAR_TOPIC_ID": ("TG_FLOW_RADAR", "资金流雷达"),
    "TG_FUNDING_ALERT_TOPIC_ID": ("TG_FUNDING_ALERT", "资金费率警报"),
    "STRUCTURE_TOPIC_ID": ("TG_STRUCTURE_RADAR", "结构突破"),
    "STRUCTURE_REVIEW_TOPIC_ID": ("TG_STRUCTURE_REVIEW", "结构复盘"),
}

TOPIC_SUMMARY_ROUTE_KEYS: dict[str, str] = {
    "TG_RADAR_SUMMARY": "radar_summary",
    "TG_LAUNCH_ALERT": "launch_alert",
    "TG_ANNOUNCEMENT_ALERT": "announcement_alert",
    "TG_TEST_MESSAGE": "test",
    "TG_FLOW_RADAR": "flow_radar",
    "TG_FUNDING_ALERT": "funding_alert",
    "TG_STRUCTURE_RADAR": "structure_radar",
    "TG_STRUCTURE_REVIEW": "structure_review",
}

CONFIG_FIELD_PURPOSE: dict[str, str] = {
    "TG_BOT_TOKEN": "群推送机器人令牌，用来把雷达信号发送到 Telegram。",
    "TG_CHAT_ID": "群、频道或超级群 ID，决定群推送机器人把消息发到哪里。",
    "TELEGRAM_USE_TOPIC": "控制群推送是否使用 Telegram 话题。",
    "TG_AUTO_CREATE_TOPICS": "开启后系统会按资金摘要、启动预警、资金流等模板自动创建话题并记录路由。",
    "TG_TOPIC_ID": "没有单独话题 ID 时使用的默认话题。",
    "WEB_HOST": "Web 后台监听地址。普通部署保持 0.0.0.0 即可让服务器 IP 可以访问。",
    "WEB_PORT": "Web 后台访问端口。默认 8080。",
    "WEB_ADMIN_TOKEN": "Web 后台访问令牌，控制谁能进入管理后台。",
    "TG_TOPIC_INTRO_ENABLE": "控制自动话题创建后是否发送话题说明。",
    "TG_TOPIC_INTRO_PIN": "控制话题说明是否置顶。",
    "CLEANUP_ENABLE": "控制后台是否按计划清理临时文件、过期图表和超限历史记录。",
    "LIQUIDITY_FALLBACK_ENABLE": "控制结构雷达是否使用外部盘口/清算数据做辅助确认。",
    "BINANCE_ORDERBOOK_LIQUIDITY_ENABLE": "控制结构雷达是否读取 Binance 盘口深度做流动性墙确认。",
    "STRUCTURE_RADAR_ENABLE": "结构雷达总开关，关闭后不再发送结构突破类信号。",
    "STRUCTURE_REVIEW_ENABLE": "结构复盘总开关，关闭后不再发送结构信号复盘统计。",
    "COINALYZE_ENABLE": "控制是否启用 Coinalyze 作为结构雷达外部确认数据源。",
    "COINALYZE_API_KEY": "Coinalyze 接口令牌，只用于可选外部确认，不用于市值数据。",
    "RADAR_SUMMARY_MIN_INTERVAL_SEC": "资金摘要最小推送间隔，控制摘要消息安静程度。",
    "FLOW_INTERVAL_SEC": "资金流雷达统计窗口长度，按完整闭合窗口统计资金流。",
    "FLOW_SCAN_LIMIT": "资金流雷达每轮扫描多少个币。",
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
    "stable-check": {"label": "执行稳定版验收", "argv": ["stable-check"], "timeout": 60, "ok_returncodes": [0, 1, 2]},
    "announcements-test": {"label": "测试 Binance 公告", "argv": ["announcements-test"], "timeout": 90},
    "funding-alert": {"label": "扫描资金费率警报", "argv": ["funding-alert"], "timeout": 180},
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
    "restart-ai": (AI_SERVICE, "restart"),
    "start-ai": (AI_SERVICE, "start"),
    "stop-ai": (AI_SERVICE, "stop"),
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


def config_field_explain(field: ConfigField) -> dict[str, str]:
    key = field.key
    if key in CONFIG_FIELD_PURPOSE:
        purpose = CONFIG_FIELD_PURPOSE[key]
    elif key.endswith("_TOPIC_ID"):
        purpose = f"指定“{field.label.replace('话题 ID', '')}”推送使用哪个 Telegram 话题。"
    elif key.startswith("FUNDING_ALERT_"):
        purpose = field.help or f"调整资金费率警报里的“{field.label}”。"
    elif key.startswith("STRUCTURE_"):
        purpose = field.help or f"调整结构雷达里的“{field.label}”。"
    elif key.startswith("LAUNCH_FUNDING_") or key == "LAUNCH_MULTI_EXCHANGE_FUNDING_ENABLE":
        purpose = field.help or f"调整启动预警推送里的“{field.label}”。"
    elif key.startswith("AI_"):
        purpose = field.help or f"调整 AI 助手 Bot 里的“{field.label}”。"
    elif key.startswith("LIQUIDITY_") or key.startswith("BINANCE_ORDERBOOK_"):
        purpose = field.help or f"调整结构雷达外部确认里的“{field.label}”。"
    else:
        purpose = field.help or f"配置“{field.label}”。"

    if key.startswith("TG_") or key == "TELEGRAM_USE_TOPIC" or key.endswith("_TOPIC_ID"):
        affects = "Telegram 真实推送、话题路由、测试消息和 readiness 检查。"
    elif key in WEB_CONFIG_KEYS:
        affects = "Web 后台访问地址、端口或登录令牌。"
    elif key in {"AI_PRICE_ALERTS_ENABLE", "AI_DEFAULT_CHAT_ID", "AI_ALERT_CHECK_INTERVAL_SEC"}:
        affects = "AI 助手 Bot 的个人价格提醒、监控频率和 Web 创建提醒默认接收人。"
    elif key in AI_CONFIG_KEYS:
        affects = "AI 助手 Bot、AI 行情分析、允许用户/群组和币种档案读取。"
    elif key.startswith("FUNDING_ALERT_"):
        affects = "独立资金费率警报扫描、异常阈值、冷却、回复链和资金费率话题推送。"
    elif key.startswith("STRUCTURE_"):
        affects = "结构雷达信号数量、结构复盘、结构图数量和同币冷却。"
    elif key.startswith("LAUNCH_FUNDING_") or key == "LAUNCH_MULTI_EXCHANGE_FUNDING_ENABLE":
        affects = "启动预警推送里的多交易所资金费率展示和结算周期识别。"
    elif key in {"COINALYZE_ENABLE", "COINALYZE_API_KEY"}:
        affects = "结构雷达外部确认；不影响市值数据。"
    elif key.startswith("LIQUIDITY_") or key.startswith("BINANCE_ORDERBOOK_"):
        affects = "结构雷达盘口墙、流动性确认和外部确认分数修正。"
    elif key.startswith("FLOW_"):
        affects = "资金流雷达的统计窗口、扫描范围和请求压力。"
    elif key.startswith("RADAR_SUMMARY_"):
        affects = "资金摘要推送频率。"
    elif key in {"TG_TOPIC_INTRO_ENABLE", "TG_TOPIC_INTRO_PIN", "CLEANUP_ENABLE"}:
        affects = "话题说明或运行垃圾清理行为。"
    else:
        affects = f"{field.section} 模块。"

    if key in WEB_CONFIG_KEYS:
        apply = "保存后自动延迟重启 Web 控制台；页面可能短暂断开。"
    elif key in AI_CONFIG_KEYS:
        apply = "保存后自动重启 AI 助手服务。"
    else:
        apply = "保存后自动重启主服务和结构雷达。"
    return {"purpose": purpose, "affects": affects, "apply": apply}


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
            **config_field_explain(field),
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
    if field.key == "AI_ALLOWED_CHAT_IDS":
        if not text:
            return ""
        parts = [part.strip() for part in re.split(r"[,，\s]+", text) if part.strip()]
        for part in parts:
            if not (part.startswith("@") or part.lstrip("-").isdigit()):
                raise ValueError("群/频道 ID 应该是 -100... 或 @channel_username，多个用逗号分隔")
        return ",".join(parts)
    if field.key == "AI_MODEL":
        return normalize_ai_model(text)
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


def delete_env_backup(name: str, *, path: Path | None = None) -> dict[str, Any]:
    env_path = path or ENV_FILE
    safe_name = Path(name).name
    backup = env_path.with_name(safe_name)
    if safe_name != name or backup.parent.resolve() != env_path.parent.resolve():
        return {"ok": False, "error": "备份文件名不合法"}
    if not safe_name.startswith(f"{env_path.name}.bak.web."):
        return {"ok": False, "error": "只能删除 Web 自动创建的 .env.oi 备份"}
    if not backup.exists() or not backup.is_file():
        return {"ok": False, "error": "备份文件不存在"}

    try:
        size = backup.stat().st_size
        backup.unlink()
    except OSError as exc:
        return {"ok": False, "error": f"删除备份失败：{type(exc).__name__}: {exc}"}

    return {
        "ok": True,
        "deleted": backup.name,
        "size": size,
        "message": f"已删除备份 {backup.name}",
    }


def web_audit_log_path(data_dir: Path | None = None) -> Path:
    base_dir = data_dir or Settings.load().data_dir
    return base_dir / WEB_AUDIT_LOG_FILE


def first_text_line(text: Any) -> str:
    lines = str(text or "").splitlines()
    return lines[0].strip() if lines else ""


def result_error_summary(result: dict[str, Any]) -> str:
    if result.get("error"):
        return str(result.get("error"))
    if result.get("stderr"):
        return first_text_line(result.get("stderr"))
    errors = result.get("errors")
    if isinstance(errors, dict):
        return "; ".join(f"{key}: {value}" for key, value in list(errors.items())[:5])
    return ""


def audit_request_summary(path: str, data: dict[str, Any]) -> dict[str, Any]:
    if path == "/api/config":
        updates = data.get("updates", {})
        clear = data.get("clear", [])
        update_keys = sorted(str(key) for key in updates) if isinstance(updates, dict) else []
        clear_keys = sorted(str(item) for item in clear) if isinstance(clear, list) else []
        return {"action": "保存配置", "target": ",".join(update_keys + clear_keys), "details": {"keys": update_keys, "clear": clear_keys}}
    if path == "/api/config-restore":
        name = str(data.get("name", ""))
        return {"action": "恢复配置备份", "target": name, "details": {"backup": name}}
    if path == "/api/config-backup-delete":
        name = str(data.get("name", ""))
        return {"action": "删除配置备份", "target": name, "details": {"backup": name}}
    if path == "/api/action":
        name = str(data.get("name", ""))
        return {"action": "执行检查测试", "target": name, "details": {"name": name}}
    if path == "/api/service":
        name = str(data.get("name", ""))
        service, action = SERVICE_ACTIONS.get(name, ("", ""))
        return {"action": "控制后台服务", "target": name, "details": {"name": name, "service": service, "service_action": action}}
    if path == "/api/price-alerts":
        action = str(data.get("action") or "create").strip().lower()
        symbol = str(data.get("symbol") or data.get("id") or "")
        return {"action": "管理价格提醒", "target": symbol or action, "details": {"action": action, "symbol": str(data.get("symbol") or ""), "id": data.get("id")}}
    if path == "/api/ai-prompts":
        action = str(data.get("action") or "save").strip().lower()
        mode = str(data.get("mode") or "")
        return {"action": "管理 AI 提示词", "target": mode or action, "details": {"action": action, "mode": mode}}
    return {"action": "Web 操作", "target": path, "details": {}}


def append_web_audit(
    path: str,
    data: dict[str, Any],
    result: dict[str, Any],
    *,
    status: int,
    started_at: float,
    data_dir: Path | None = None,
) -> dict[str, Any]:
    request = audit_request_summary(path, data)
    ok = bool(result.get("ok")) and int(status) < 400
    error = result_error_summary(result)
    message = str(result.get("message") or error or "")
    record = {
        "ts": now_text(),
        "path": path,
        "action": request["action"],
        "target": request["target"],
        "ok": ok,
        "status": int(status),
        "duration_ms": int((time.time() - started_at) * 1000),
        "message": message[:500],
        "error": str(error)[:500],
        "details": request["details"],
    }
    store = JsonStore((data_dir or Settings.load().data_dir))
    store.append_record(web_audit_log_path(data_dir), record, limit=WEB_AUDIT_LIMIT)
    return record


def web_audit_payload(
    *,
    data_dir: Path | None = None,
    limit: int = 200,
    result: str = "all",
    search: str = "",
) -> dict[str, Any]:
    store = JsonStore((data_dir or Settings.load().data_dir))
    path = web_audit_log_path(data_dir)
    records = store.load(path, [])
    if not isinstance(records, list):
        records = []
    result_filter = str(result or "all").lower()
    query = str(search or "").strip().lower()

    def keep(record: Any) -> bool:
        if not isinstance(record, dict):
            return False
        ok = bool(record.get("ok"))
        if result_filter == "ok" and not ok:
            return False
        if result_filter == "failed" and ok:
            return False
        if query:
            haystack = " ".join(
                str(record.get(key, ""))
                for key in ("ts", "path", "action", "target", "message", "error", "status")
            ).lower()
            if query not in haystack:
                return False
        return True

    filtered = [record for record in reversed(records) if keep(record)]
    limited = filtered[: max(1, min(int(limit or 200), 1000))]
    return {
        "ok": True,
        "path": str(path),
        "total": len(records),
        "matched": len(filtered),
        "records": limited,
        "message": "已读取 Web 操作审计记录",
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
    ok_returncodes = action.get("ok_returncodes")
    if isinstance(ok_returncodes, list):
        result["ok"] = int(result.get("returncode", 0) or 0) in {int(code) for code in ok_returncodes}
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


def config_field_info(key: str) -> dict[str, Any]:
    field = EDITABLE_CONFIG.get(key)
    if field is None:
        return {"key": key, "label": key, "section": "未知", "secret": False, "kind": "text"}
    return {"key": key, "label": field.label, "section": field.section, "secret": field.secret, "kind": field.kind}


def config_change_impact(changed: list[str]) -> dict[str, Any]:
    changed_keys = sorted({str(key) for key in changed if str(key)})
    changed_set = set(changed_keys)
    changed_fields = [config_field_info(key) for key in changed_keys]
    modules = sorted({str(item.get("section") or "未知") for item in changed_fields})
    sensitive_keys = [item["key"] for item in changed_fields if item.get("secret")]
    service_actions: list[dict[str, Any]] = []

    def add_service(action_name: str, reason: str, *, scheduled: bool = False) -> None:
        service, action = SERVICE_ACTIONS[action_name]
        service_actions.append(
            {
                "name": action_name,
                "service": service,
                "action": action,
                "scheduled": scheduled,
                "reason": reason,
            }
        )

    standard_restart_keys = changed_set - WEB_CONFIG_KEYS - AI_CONFIG_KEYS
    if standard_restart_keys or (changed_set & SIGNAL_EVENT_CONFIG_KEYS):
        add_service("restart-main", "主服务需要重新读取 Telegram、雷达扫描、资金费率或信号索引相关配置。")
        add_service("restart-structure", "结构雷达需要重新读取结构参数、话题、外部确认或信号索引相关配置。")
    if changed_set & AI_CONFIG_KEYS:
        add_service("restart-ai", "AI 助手需要重新读取 Bot Token、AI 接口、允许群组、价格提醒或币种档案配置。")
    if changed_set & WEB_CONFIG_KEYS:
        add_service("restart-web", "Web 控制台地址、端口或访问令牌变更后需要重启 Web 服务。", scheduled=True)

    warnings: list[str] = []
    if sensitive_keys:
        labels = "、".join(EDITABLE_CONFIG.get(key, ConfigField(key, key, "")).label for key in sensitive_keys)
        warnings.append(f"包含敏感配置：{labels}。审计和诊断只记录字段名，不记录具体值。")
    if changed_set & {"WEB_HOST", "WEB_PORT", "WEB_ADMIN_TOKEN"}:
        warnings.append("Web 入口配置会在保存返回后短暂重启；如果页面断开，稍后按新的地址或令牌重新打开。")
    if changed_set & {"TG_BOT_TOKEN", "TG_CHAT_ID", "TELEGRAM_USE_TOPIC", "TG_AUTO_CREATE_TOPICS"} or any(key.endswith("_TOPIC_ID") for key in changed_set):
        warnings.append("Telegram 推送配置会影响真实消息发送；保存后建议先执行 Telegram 测试消息或 readiness。")
    if changed_set & {"AI_BOT_TOKEN", "AI_API_KEY", "AI_BASE_URL", "AI_MODEL", "AI_ALLOWED_CHAT_IDS", "AI_PROVIDER_ENABLE"}:
        warnings.append("AI 助手配置会影响私聊、行情分析和价格提醒；保存后建议到 AI 助手页确认服务状态。")
    if changed_set & {"STRUCTURE_MIN_SCORE", "STRUCTURE_SEND_CHART_TOP_N", "STRUCTURE_NEAR_EDGE_PCT", "STRUCTURE_COOLDOWN_SEC"}:
        warnings.append("结构雷达参数会改变信号数量、图片数量或同币冷却；保存后建议观察结构复盘和下一轮推送。")
    if any(key.startswith("FUNDING_ALERT_") for key in changed_set):
        warnings.append("资金费率警报参数会改变扫描频率、阈值或冷却；保存后建议手动扫描一次资金费率警报。")

    if not changed_keys:
        message = "没有检测到配置变更，不会重启服务。"
    elif service_actions:
        names = "、".join(item["service"] for item in service_actions)
        message = f"保存后会自动应用，并影响这些服务：{names}。"
    else:
        message = "保存后不需要自动重启后台服务。"
    return {
        "changed": changed_keys,
        "changed_fields": changed_fields,
        "modules": modules,
        "service_actions": service_actions,
        "warnings": warnings,
        "rollback": "保存前会自动生成 .env.oi Web 备份；如果改错，可到配置中心的备份恢复里恢复最近一次备份。",
        "message": message,
    }


def config_impact_payload(data: dict[str, Any], *, path: Path | None = None) -> dict[str, Any]:
    updates = data.get("updates", {})
    clear = data.get("clear", [])
    if not isinstance(updates, dict) or not isinstance(clear, list):
        return {"ok": False, "errors": {"request": "updates 必须是对象，clear 必须是数组"}, "impact": config_change_impact([])}
    clear_set = {str(item) for item in clear}
    normalized: dict[str, str] = {}
    errors: dict[str, str] = {}
    for key, value in updates.items():
        field = EDITABLE_CONFIG.get(str(key))
        if field is None:
            errors[str(key)] = "不允许修改这个配置项"
            continue
        if field.secret and str(value).strip() == "" and str(key) not in clear_set:
            continue
        try:
            normalized[str(key)] = "" if str(key) in clear_set else validate_config_value(field, value)
        except ValueError as exc:
            errors[str(key)] = str(exc)
    for key in clear_set:
        if key not in EDITABLE_CONFIG:
            errors[key] = "不允许清空这个配置项"
        elif key not in normalized:
            normalized[key] = ""
    current = read_env_values(path or ENV_FILE)
    changed = sorted(key for key, value in normalized.items() if current.get(key, "") != value)
    impact = config_change_impact(changed)
    return {
        "ok": not errors,
        "changed": changed,
        "errors": errors,
        "impact": impact,
        "message": "配置影响预检完成" if not errors else "配置影响预检发现错误，保存前需要修正",
    }


def auto_apply_config_changes(changed: list[str]) -> dict[str, Any]:
    changed_set = set(changed)
    if not changed_set:
        return {"ok": True, "mode": "none", "results": [], "impact": config_change_impact(changed), "message": "没有配置变更，不需要自动应用"}

    results: list[dict[str, Any]] = []
    standard_restart_keys = changed_set - WEB_CONFIG_KEYS - AI_CONFIG_KEYS
    if standard_restart_keys or (changed_set & SIGNAL_EVENT_CONFIG_KEYS):
        for action_name in ("restart-main", "restart-structure"):
            result = run_service_action(action_name)
            service, action = SERVICE_ACTIONS[action_name]
            result.update({"name": action_name, "service": service, "action": action})
            results.append(result)
    if changed_set & AI_CONFIG_KEYS:
        result = run_service_action("restart-ai")
        service, action = SERVICE_ACTIONS["restart-ai"]
        result.update({"name": "restart-ai", "service": service, "action": action})
        results.append(result)
    if changed_set & WEB_CONFIG_KEYS:
        results.append(schedule_service_action("restart-web"))

    ok = all(bool(item.get("ok")) for item in results)
    if not results:
        message = "没有需要自动重启的服务"
    elif ok:
        message = "配置已保存并自动应用；Web 控制台配置变更会在返回结果后短暂重启"
    else:
        message = "配置已保存，但部分服务自动应用失败；可到雷达服务页手动重启"
    return {"ok": ok, "mode": "auto_restart", "results": results, "impact": config_change_impact(changed), "message": message}


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


def update_check_payload() -> dict[str, Any]:
    script = BASE_DIR / "scripts" / "update_server.sh"
    if not script.exists():
        return {"ok": False, "message": "未找到更新脚本 scripts/update_server.sh", "stdout": "", "stderr": ""}
    if not command_exists("bash"):
        return {"ok": False, "message": "当前环境没有 bash，服务器上可以使用 paopao update --yes 更新", "stdout": "", "stderr": ""}
    result = run_subprocess(["bash", str(script), "--check"], timeout=120)
    return {
        "ok": bool(result.get("ok")),
        "message": "更新检查完成" if result.get("ok") else "更新检查失败",
        "stdout": result.get("stdout", ""),
        "stderr": result.get("stderr", ""),
        "command": result.get("command", ""),
    }


def load_json_or_empty(path: Path) -> Any:
    if not path.exists():
        return {"status": "empty", "path": str(path)}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"status": "invalid", "path": str(path), "error": f"{type(exc).__name__}: {exc}"}


def collect_nested_keys(value: Any, key_name: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        if key_name in value:
            found.append(value[key_name])
        for child in value.values():
            found.extend(collect_nested_keys(child, key_name))
    elif isinstance(value, list):
        for child in value:
            found.extend(collect_nested_keys(child, key_name))
    return found


def format_failure_key(key: str) -> str:
    text = str(key or "").strip()
    if text.startswith("funding:"):
        exchange = text.split(":", 1)[1] or "未知交易所"
        return f"{exchange} 资金费率接口"
    names = {
        "exchangeInfo": "Binance 合约信息接口",
        "ticker24hr": "Binance 24小时成交额接口",
        "premiumIndex": "Binance 资金费率接口",
        "openInterestHist": "Binance OI 历史接口",
        "klines": "Binance 合约K线接口",
        "spotKlines": "Binance 现货K线接口",
        "fundingRate": "Binance 历史资金费率接口",
        "depth": "Binance 盘口接口",
        "marketCaps": "Binance 市值接口",
        "coinpaprikaMarketCaps": "CoinPaprika 市值接口",
        "announcements": "Binance 公告接口",
    }
    return names.get(text, text)


def format_runtime_failure(failure: dict[str, Any]) -> str:
    items: list[str] = []
    for key, count in failure.items():
        try:
            value = int(count)
        except (TypeError, ValueError):
            value = 1
        items.append(f"{format_failure_key(str(key))}失败 {value} 次")
    if not items:
        return "检测到接口失败计数"
    suffix = "主服务仍在运行；本轮只会少用对应数据源，后续扫描会自动重试。"
    return "；".join(items[:6]) + "。" + suffix


def is_low_priority_funding_failure(failure: dict[str, Any]) -> bool:
    if not failure:
        return False
    total = 0
    for key, count in failure.items():
        if not str(key).startswith("funding:"):
            return False
        try:
            total += int(count)
        except (TypeError, ValueError):
            total += 1
    return total <= 3


def runtime_last_error(name: str, runtime: Any) -> dict[str, str] | None:
    if not isinstance(runtime, dict):
        return None
    status = str(runtime.get("status") or "")
    if status == "invalid":
        return {"source": name, "level": "异常", "message": str(runtime.get("error") or "状态文件无法解析")}
    last_error = str(runtime.get("last_error") or "").strip()
    if last_error:
        return {"source": name, "level": "异常", "message": last_error}
    failures = collect_nested_keys(runtime, "failures")
    for failure in failures:
        if isinstance(failure, dict) and failure:
            if is_low_priority_funding_failure(failure):
                continue
            return {"source": name, "level": "警告", "message": format_runtime_failure(failure)}
    return None


def build_health_items(services: dict[str, Any], runtime: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    telegram = config.get("telegram", {}) if isinstance(config.get("telegram"), dict) else {}
    ai = config.get("ai_assistant", {}) if isinstance(config.get("ai_assistant"), dict) else {}
    liquidity = config.get("liquidity", {}) if isinstance(config.get("liquidity"), dict) else {}
    structure = config.get("structure_radar", {}) if isinstance(config.get("structure_radar"), dict) else {}

    def service_item(label: str, key: str) -> dict[str, Any]:
        service = services.get(key, {}) if isinstance(services.get(key), dict) else {}
        ok = bool(service.get("active_ok"))
        return {
            "label": label,
            "status": "ok" if ok else "bad",
            "value": "运行中" if ok else str(service.get("active") or "未知"),
            "detail": str(service.get("service") or ""),
        }

    items = [
        service_item("主服务", "main"),
        service_item("结构雷达", "structure"),
        service_item("Web 控制台", "web"),
        service_item("AI 助手", "ai"),
        {
            "label": "Telegram 推送",
            "status": "ok" if telegram.get("bot_token_configured") and telegram.get("chat_id_configured") else "bad",
            "value": "已配置" if telegram.get("bot_token_configured") and telegram.get("chat_id_configured") else "缺 Token 或群 ID",
            "detail": "真实推送依赖 Telegram Token 和群 ID",
        },
        {
            "label": "AI 助手 Bot",
            "status": "ok" if ai.get("enable") and ai.get("bot_token_configured") else ("warn" if ai.get("enable") else "warn"),
            "value": "已启用" if ai.get("enable") and ai.get("bot_token_configured") else ("缺 AI_BOT_TOKEN" if ai.get("enable") else "未启用"),
            "detail": "AI 助手和价格提醒使用独立 Telegram Bot，不影响群推送 Bot",
        },
        {
            "label": "话题路由",
            "status": "ok" if (not telegram.get("use_topic") or telegram.get("topic_routes_file_exists") or any((telegram.get("topic_routes_configured") or {}).values())) else "warn",
            "value": "正常" if (not telegram.get("use_topic") or telegram.get("topic_routes_file_exists") or any((telegram.get("topic_routes_configured") or {}).values())) else "未检测到话题 ID",
            "detail": "自动话题会读取 data/tg_topic_routes.json",
        },
        {
            "label": "结构雷达开关",
            "status": "ok" if structure.get("enable") else "warn",
            "value": "开启" if structure.get("enable") else "关闭",
            "detail": "关闭后结构雷达不会发送结构信号",
        },
        {
            "label": "外部确认",
            "status": "ok" if liquidity.get("fallback_enable") else "warn",
            "value": "开启" if liquidity.get("fallback_enable") else "关闭",
            "detail": "用于结构雷达盘口和清算辅助确认",
        },
    ]
    for label, key in (("主服务状态文件", "main"), ("结构状态文件", "structure")):
        item = runtime.get(key, {}) if isinstance(runtime.get(key), dict) else {}
        status = str(item.get("status") or "")
        last_error = str(item.get("last_error") or "")
        items.append(
            {
                "label": label,
                "status": "bad" if status == "invalid" or last_error else ("warn" if status == "empty" else "ok"),
                "value": "正常" if status and status not in {"empty", "invalid"} and not last_error else ("暂无" if status == "empty" else "异常"),
                "detail": last_error or str(item.get("updated_at") or item.get("error") or ""),
            }
        )
    return items


def recent_errors_payload(runtime: dict[str, Any]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    for name, item in (("主服务", runtime.get("main")), ("结构雷达", runtime.get("structure"))):
        error = runtime_last_error(name, item)
        if error:
            errors.append(error)
    return errors


def push_preview_payload() -> dict[str, Any]:
    settings = Settings.load()
    previews = [
        {
            "title": "启动雷达样例",
            "text": (
                "🚀 启动雷达 [BTC](https://www.coinglass.com/tv/zh/Binance_BTCUSDT)\n"
                "阶段: 提前预警\n"
                f"分数: {settings.launch_primed_score}\n"
                "市场概况\n市值: $1.2T（高市值）\n流动性: $2.5B/24h（高流动性）\n"
                "说明: 这是静态预览，不会真实发送。"
            ),
        },
        {
            "title": "资金流雷达样例",
            "text": (
                "📌 资金流雷达｜统计窗口 1h\n"
                "真启动候选: BTCUSDT\n"
                f"扫描数量: {settings.flow_scan_limit}\n"
                "说明: CVD、OI、费率字段会按真实数据填充；这里仅展示版式。"
            ),
        },
        {
            "title": "结构雷达样例",
            "text": (
                "📐 结构雷达 BTCUSDT\n"
                f"最低推送分: {settings.structure_min_score}\n"
                f"每轮结构图数量: {settings.structure_send_chart_top_n}\n"
                "外部确认: Binance 盘口 / 可选 Coinalyze 清算辅助\n"
                "说明: 这是静态预览，不会真实发送。"
            ),
        },
    ]
    return {"ok": True, "previews": previews, "message": "推送预览只展示格式，不会调用 Telegram，也不会真实发送"}


def ai_prompts_test_payload(data: dict[str, Any]) -> dict[str, Any]:
    settings = Settings.load()
    if not settings.ai_provider_enable or not settings.ai_api_key:
        return {"ok": False, "error": "AI 问答接口未启用，请先配置 AI_PROVIDER_ENABLE 和 AI_API_KEY"}
    mode = str(data.get("mode") or "analyst").strip().lower()
    if mode not in {"assistant", "analyst"}:
        mode = "analyst"
    text = str(data.get("text") or "").strip()
    if not text:
        return {"ok": False, "error": "测试内容不能为空"}
    prompt = str(data.get("analyst_prompt" if mode == "analyst" else "assistant_prompt") or "").strip()
    if not prompt:
        return {"ok": False, "error": "测试提示词不能为空"}
    from .ai_assistant import build_chat_completion_payload, complete_ai_text

    payload = build_chat_completion_payload(settings, prompt, text)
    reply = complete_ai_text(settings, payload)
    return {
        "ok": True,
        "mode": mode,
        "model": settings.ai_model,
        "reply": reply,
        "message": "测试完成；测试不会保存提示词，确认后请点击保存提示词",
    }


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
    services = {
        "main": service_status(MAIN_SERVICE),
        "structure": service_status(STRUCTURE_SERVICE),
        "web": service_status(WEB_SERVICE),
        "ai": service_status(AI_SERVICE),
    }
    runtime = {
        "main": load_json_or_empty(settings.runtime_status_path),
        "structure": load_json_or_empty(settings.structure_runtime_status_path),
    }
    config = {
        "env_file_exists": redacted.get("env_file_exists"),
        "telegram": telegram,
        "runtime": redacted.get("runtime"),
        "liquidity": redacted.get("liquidity"),
        "coinalyze": redacted.get("coinalyze"),
        "ai_assistant": redacted.get("ai_assistant"),
        "structure_radar": redacted.get("structure_radar"),
    }
    return {
        "updated_at": now_text(),
        "git": git_info(),
        "services": services,
        "runtime": runtime,
        "config": config,
        "health": build_health_items(services, runtime, config),
        "recent_errors": recent_errors_payload(runtime),
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
    elif target == "ai":
        service = AI_SERVICE
        fallback_path = settings.data_dir / "ai-assistant.log"
    else:
        service = MAIN_SERVICE
        fallback_path = settings.data_dir / "runtime.log"
    if command_exists("journalctl"):
        result = run_subprocess(["journalctl", "-u", service, "-n", str(lines), "--no-pager"], timeout=15)
        if result["stdout"].strip() or result["returncode"] == 0:
            return {"target": target, "source": f"journalctl:{service}", "text": result["stdout"], "ok": result["ok"]}
    text = tail_file(fallback_path, lines)
    return {"target": target, "source": str(fallback_path), "text": text, "ok": bool(text)}


ERROR_LINE_RE = re.compile(
    r"(\b[a-z0-9_]*error\b|traceback|exception|failed|fatal|\breadtimeout\b|(?<![_a-z0-9])timeout\b|timed out|denied|forbidden|失败|异常|错误|超时|拒绝)",
    re.I,
)
WEB_CLIENT_DISCONNECT_RE = re.compile(
    r"(ConnectionResetError|ConnectionAbortedError|BrokenPipeError|\[Errno\s+104\]\s+Connection reset by peer|\[Errno\s+32\]\s+Broken pipe)",
    re.I,
)
WEB_CLIENT_DISCONNECT_CONTEXT_RE = re.compile(
    r"(Exception occurred during processing of request from|Traceback \(most recent call last\):)",
    re.I,
)
EMPTY_ERROR_FIELD_RE = re.compile(
    r"""(?ix)
    (?:["']?[a-z0-9_]*errors?["']?)\s*[:=]\s*
    (?:
        \[\s*\]
        | "" | ''
        | null\b
        | none\b
        | false\b
    )
    """
)
SENSITIVE_LINE_RE = re.compile(r"(?i)\b(token|api[_-]?key|secret|password)\b\s*[:=]\s*['\"]?[^'\"\s,;]+")
TELEGRAM_TOKEN_RE = re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b")
API_KEY_RE = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9_-]{10,}\b")
TRANSIENT_LOG_RE = re.compile(r"(getUpdates failed ReadTimeout|Read timed out|read timeout=\d+)", re.I)
SEMVER_VERSION_RE = re.compile(r"^v\d+\.\d+\.\d+(?:[-+][A-Za-z0-9_.-]+)?$")
STABILITY_LATEST_FILE = "stable_check_latest.json"
STABILITY_HISTORY_FILE = "stable_check_history.json"
STABILITY_HISTORY_LIMIT = 30


def redact_sensitive_text(text: str) -> str:
    redacted = TELEGRAM_TOKEN_RE.sub("<redacted-telegram-token>", str(text or ""))
    redacted = API_KEY_RE.sub("<redacted-api-key>", redacted)
    redacted = SENSITIVE_LINE_RE.sub(lambda match: f"{match.group(1)}=<redacted>", redacted)
    return redacted


def is_error_log_line(line: str) -> bool:
    cleaned = EMPTY_ERROR_FIELD_RE.sub("", str(line or ""))
    return bool(ERROR_LINE_RE.search(cleaned))


def is_transient_log_line(line: str) -> bool:
    return bool(TRANSIENT_LOG_RE.search(str(line or "")))


def is_ignorable_log_line(target: str, line: str) -> bool:
    if str(target or "").lower() != "web":
        return False
    text = str(line or "")
    return bool(WEB_CLIENT_DISCONNECT_RE.search(text) or WEB_CLIENT_DISCONNECT_CONTEXT_RE.search(text))


def log_error_excerpt(target: str, *, lines: int = 300, limit: int = 20) -> dict[str, Any]:
    payload = logs_payload(target, lines)
    raw_lines = str(payload.get("text") or "").splitlines()
    transient_lines = [line for line in raw_lines if is_transient_log_line(line) or is_ignorable_log_line(target, line)]
    error_lines = [
        line for line in raw_lines
        if is_error_log_line(line) and not is_transient_log_line(line) and not is_ignorable_log_line(target, line)
    ]
    selected = error_lines[-limit:]
    selected_transient = transient_lines[-limit:]
    return {
        "target": target,
        "source": payload.get("source", ""),
        "ok": bool(payload.get("ok")),
        "total_lines": len(raw_lines),
        "error_count": len(error_lines),
        "transient_count": len(transient_lines),
        "lines": [redact_sensitive_text(line)[-600:] for line in selected],
        "transient_lines": [redact_sensitive_text(line)[-600:] for line in selected_transient],
    }


def build_ops_recommendations(snapshot: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    health = snapshot.get("health", []) if isinstance(snapshot.get("health"), list) else []
    bad_health = [item for item in health if isinstance(item, dict) and item.get("status") == "bad"]
    warn_health = [item for item in health if isinstance(item, dict) and item.get("status") == "warn"]
    recent_errors = snapshot.get("recent_errors", []) if isinstance(snapshot.get("recent_errors"), list) else []
    audit = snapshot.get("audit", {}) if isinstance(snapshot.get("audit"), dict) else {}
    failed_audit = audit.get("failed_recent", []) if isinstance(audit.get("failed_recent"), list) else []
    logs = snapshot.get("log_errors", {}) if isinstance(snapshot.get("log_errors"), dict) else {}
    log_error_total = sum(
        int(item.get("error_count", 0) or 0)
        for item in logs.values()
        if isinstance(item, dict)
    )
    transient_total = sum(
        int(item.get("transient_count", 0) or 0)
        for item in logs.values()
        if isinstance(item, dict)
    )
    if bad_health:
        labels = "、".join(str(item.get("label") or "") for item in bad_health[:4])
        recommendations.append(f"优先处理异常健康项：{labels}。先到“雷达服务”页重启对应服务，再看日志中心错误。")
    if recent_errors:
        recommendations.append("总览已经检测到最近错误，先按错误来源跳转日志中心查看同一时间段日志。")
    if failed_audit:
        recommendations.append("最近存在失败的 Web 后台操作，先到“审计记录”按失败筛选，确认是不是配置保存、服务控制或测试动作失败。")
    if log_error_total:
        recommendations.append(f"近期日志中检测到 {log_error_total} 条错误/异常关键字，优先查看诊断报告里的日志片段和日志中心原文。")
    if transient_total >= 10 and not log_error_total:
        recommendations.append(f"近期 Telegram 网络超时 {transient_total} 次。服务会自动重试；如果 AI Bot 明显不回复，再检查服务器到 api.telegram.org 的网络。")
    if warn_health and not bad_health:
        labels = "、".join(str(item.get("label") or "") for item in warn_health[:4])
        recommendations.append(f"存在需要关注的警告项：{labels}。如果功能正常，可以观察；如果推送异常，再进入对应配置页检查。")
    if not recommendations:
        recommendations.append("当前快照没有发现明显异常。若仍然感觉不对，复制本页报告并补充你看到的具体现象。")
    return recommendations


def issue_target_from_source(source: str) -> str:
    text = str(source or "").lower()
    if "结构" in source or "structure" in text:
        return "structure"
    if "web" in text:
        return "web"
    if "ai" in text or "助手" in source:
        return "ai"
    return "main"


def build_ops_issues(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []

    def add(
        *,
        severity: str,
        module: str,
        title: str,
        detail: str,
        count: int = 1,
        target: str = "",
        action: str = "",
    ) -> None:
        issues.append(
            {
                "severity": severity,
                "module": module,
                "title": title,
                "detail": detail,
                "count": max(1, int(count or 1)),
                "target": target,
                "action": action,
            }
        )

    health = snapshot.get("health", []) if isinstance(snapshot.get("health"), list) else []
    for item in health:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "")
        if status not in {"bad", "warn"}:
            continue
        label = str(item.get("label") or "健康检查")
        value = str(item.get("value") or "")
        detail = str(item.get("detail") or "")
        add(
            severity="critical" if status == "bad" else "warning",
            module=label,
            title=f"{label}{'异常' if status == 'bad' else '需要关注'}",
            detail="；".join(part for part in (value, detail) if part),
            target=issue_target_from_source(label),
            action="先确认服务状态；如果是运行异常，进入雷达服务页重启对应服务，再看相关日志。",
        )

    recent_errors = snapshot.get("recent_errors", []) if isinstance(snapshot.get("recent_errors"), list) else []
    for item in recent_errors:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "运行状态")
        level = str(item.get("level") or "异常")
        message = str(item.get("message") or "")
        add(
            severity="critical" if "异常" in level else "warning",
            module=source,
            title=f"{source} · {level}",
            detail=message,
            target=issue_target_from_source(source),
            action="打开相关日志，对照 runtime-status 的时间点查看同一轮扫描发生了什么。",
        )

    audit = snapshot.get("audit", {}) if isinstance(snapshot.get("audit"), dict) else {}
    failed_audit = audit.get("failed_recent", []) if isinstance(audit.get("failed_recent"), list) else []
    if failed_audit:
        first = failed_audit[0] if isinstance(failed_audit[0], dict) else {}
        add(
            severity="warning",
            module="Web 后台操作",
            title="存在失败的 Web 后台操作",
            detail=str(first.get("error") or first.get("message") or "配置保存、服务控制或检查测试曾失败。"),
            count=len(failed_audit),
            target="audit",
            action="进入审计记录页选择“只看失败”，确认失败动作、接口和错误摘要。",
        )

    target_labels = {"main": "主服务", "structure": "结构雷达", "web": "Web 控制台", "ai": "AI 助手"}
    logs = snapshot.get("log_errors", {}) if isinstance(snapshot.get("log_errors"), dict) else {}
    for target, item in logs.items():
        if not isinstance(item, dict):
            continue
        error_count = int(item.get("error_count", 0) or 0)
        transient_count = int(item.get("transient_count", 0) or 0)
        label = target_labels.get(str(target), str(target))
        if error_count:
            first_line = ""
            lines = item.get("lines", [])
            if isinstance(lines, list) and lines:
                first_line = str(lines[-1])
            add(
                severity="critical" if error_count >= 10 else "warning",
                module=label,
                title=f"{label}日志出现错误关键字",
                detail=first_line or f"近 300 行日志检测到 {error_count} 条错误/异常关键字。",
                count=error_count,
                target=str(target),
                action="打开对应日志中心，筛选“只看错误”，按第一条错误的时间点继续排查。",
            )
        if transient_count >= 10:
            add(
                severity="notice",
                module=label,
                title=f"{label}网络超时较多",
                detail=f"近 300 行日志检测到 {transient_count} 条 Telegram/API 可重试超时。低频无需处理，持续增多时检查服务器网络。",
                count=transient_count,
                target=str(target),
                action="先观察是否自动恢复；如果机器人明显不回复，再检查服务器到 Telegram/API 的网络。",
            )

    severity_order = {"critical": 0, "warning": 1, "notice": 2}
    return sorted(issues, key=lambda item: (severity_order.get(str(item.get("severity")), 9), -int(item.get("count", 1) or 1), str(item.get("module") or "")))[:20]


def build_problem_center(snapshot: dict[str, Any]) -> dict[str, Any]:
    issues = snapshot.get("issues", []) if isinstance(snapshot.get("issues"), list) else []
    stability = snapshot.get("stability", {}) if isinstance(snapshot.get("stability"), dict) else {}
    health = snapshot.get("health", []) if isinstance(snapshot.get("health"), list) else []
    recent_errors = snapshot.get("recent_errors", []) if isinstance(snapshot.get("recent_errors"), list) else []
    audit = snapshot.get("audit", {}) if isinstance(snapshot.get("audit"), dict) else {}
    failed_audit = audit.get("failed_recent", []) if isinstance(audit.get("failed_recent"), list) else []
    logs = snapshot.get("log_errors", {}) if isinstance(snapshot.get("log_errors"), dict) else {}

    critical_count = sum(1 for item in issues if isinstance(item, dict) and item.get("severity") == "critical")
    warning_count = sum(1 for item in issues if isinstance(item, dict) and item.get("severity") == "warning")
    notice_count = sum(1 for item in issues if isinstance(item, dict) and item.get("severity") == "notice")
    bad_health_count = sum(1 for item in health if isinstance(item, dict) and item.get("status") == "bad")
    warn_health_count = sum(1 for item in health if isinstance(item, dict) and item.get("status") == "warn")
    log_error_total = sum(
        int(item.get("error_count", 0) or 0)
        for item in logs.values()
        if isinstance(item, dict)
    )
    transient_total = sum(
        int(item.get("transient_count", 0) or 0)
        for item in logs.values()
        if isinstance(item, dict)
    )
    stability_status = str(stability.get("status") or "")

    if critical_count or bad_health_count or stability_status == "blocked":
        status = "blocked"
        label = "需要优先处理"
        summary = "存在阻断项或严重问题，建议先处理后再继续观察。"
        primary_action = "先处理问题中心里的严重项；服务异常优先重启对应服务，配置缺失优先补齐配置。"
    elif warning_count or warn_health_count or log_error_total or failed_audit or stability_status == "attention":
        status = "attention"
        label = "需要关注"
        summary = "系统可运行，但存在警告、错误日志或失败操作，建议确认是否影响实际推送。"
        primary_action = "先看建议动作和相关日志；如果功能正常，可以继续观察并等待下一次 stable-check。"
    else:
        status = "ok"
        label = "当前健康"
        summary = "核心服务、配置、日志、审计和稳定版验收未发现需要优先处理的问题。"
        primary_action = "继续正常运行；更新后可再次执行 stable-check 保存一条验收记录。"

    module_counts: dict[str, dict[str, Any]] = {}
    severity_rank = {"critical": 0, "warning": 1, "notice": 2}
    for item in issues:
        if not isinstance(item, dict):
            continue
        module = str(item.get("module") or "未知模块")
        severity = str(item.get("severity") or "notice")
        count = int(item.get("count", 1) or 1)
        existing = module_counts.setdefault(
            module,
            {
                "module": module,
                "severity": severity,
                "count": 0,
                "target": str(item.get("target") or ""),
                "reason": str(item.get("title") or ""),
            },
        )
        existing["count"] = int(existing.get("count", 0) or 0) + max(1, count)
        if severity_rank.get(severity, 9) < severity_rank.get(str(existing.get("severity") or ""), 9):
            existing["severity"] = severity
            existing["target"] = str(item.get("target") or "")
            existing["reason"] = str(item.get("title") or "")

    next_steps: list[str] = []
    if critical_count:
        next_steps.append("先处理严重问题，严重项通常意味着服务异常、关键配置缺失或日志错误过多。")
    if log_error_total:
        next_steps.append("打开日志中心，选择对应模块并勾选“只看错误”，按最早错误时间排查。")
    if failed_audit:
        next_steps.append("打开审计记录，筛选失败操作，确认最近的配置保存或服务控制是否已经重试成功。")
    if transient_total >= 10 and not log_error_total:
        next_steps.append("Telegram/API 网络超时偏多但可自动重试；如果 Bot 明显不回复，再检查服务器网络。")
    if not next_steps:
        next_steps.append("暂无需要立即处理的动作。")

    action_plan: list[dict[str, Any]] = []
    action_keys: set[str] = set()

    def add_action(
        key: str,
        *,
        severity: str,
        title: str,
        detail: str,
        target: str,
        button: str,
        log_target: str = "",
        action_id: str = "",
    ) -> None:
        if key in action_keys:
            return
        action_keys.add(key)
        action_plan.append(
            {
                "key": key,
                "severity": severity,
                "title": title,
                "detail": detail,
                "target": target,
                "button": button,
                "log_target": log_target,
                "action_id": action_id,
            }
        )

    if bad_health_count or critical_count:
        add_action(
            "service-health",
            severity="critical",
            title="先处理服务或健康异常",
            detail="如果主服务、结构雷达、Web 控制台或 AI 助手显示异常，先到雷达服务页重启对应服务，再回来看诊断报告。",
            target="services",
            button="打开雷达服务",
        )

    config_problem = any(
        isinstance(item, dict) and str(item.get("key") or "") == "config" and str(item.get("status") or "") in {"fail", "warn"}
        for item in (stability.get("checks", []) if isinstance(stability.get("checks"), list) else [])
    )
    if config_problem:
        add_action(
            "config-check",
            severity="critical" if stability_status == "blocked" else "warning",
            title="补齐关键配置",
            detail="关键配置异常通常是 Telegram Token、群 ID 或 AI Bot Token 缺失。进入配置中心按模块补齐后保存，后台会自动应用。",
            target="config",
            button="打开配置中心",
        )

    if log_error_total:
        worst_log_target = ""
        worst_log_count = -1
        for target, item in logs.items():
            if not isinstance(item, dict):
                continue
            count = int(item.get("error_count", 0) or 0)
            if count > worst_log_count:
                worst_log_count = count
                worst_log_target = str(target)
        add_action(
            "log-errors",
            severity="critical" if log_error_total >= 20 else "warning",
            title="查看错误日志原文",
            detail=f"最近日志检测到 {log_error_total} 条错误/异常关键字。先看错误最多的模块，再按时间点回查上下文。",
            target="logs",
            button="打开相关日志",
            log_target=worst_log_target,
        )

    if failed_audit:
        add_action(
            "failed-audit",
            severity="warning",
            title="检查失败的后台操作",
            detail="配置保存、服务控制或检查测试失败时会进入审计记录。先确认失败动作是否已经重新执行成功。",
            target="audit",
            button="查看失败审计",
        )

    if transient_total >= 10 and not log_error_total:
        worst_transient_target = ""
        worst_transient_count = -1
        for target, item in logs.items():
            if not isinstance(item, dict):
                continue
            count = int(item.get("transient_count", 0) or 0)
            if count > worst_transient_count:
                worst_transient_count = count
                worst_transient_target = str(target)
        add_action(
            "transient-timeouts",
            severity="notice",
            title="观察网络超时是否恢复",
            detail=f"最近检测到 {transient_total} 条可自动重试超时。低频通常不用处理；如果 Bot 明显不回复，再检查服务器到 Telegram/API 的网络。",
            target="logs",
            button="查看超时日志",
            log_target=worst_transient_target,
        )

    if stability_status in {"blocked", "attention"}:
        add_action(
            "stable-check",
            severity="critical" if stability_status == "blocked" else "warning",
            title="处理后重新验收",
            detail="处理上面的服务、配置或日志问题后，再执行稳定版验收，把新的结果保存到验收历史。",
            target="actions",
            button="打开检查测试",
            action_id="stable-check",
        )

    if not action_plan:
        add_action(
            "observe",
            severity="notice",
            title="继续观察",
            detail="当前没有需要立即处理的动作。更新后可以执行一次稳定版验收，保存健康记录。",
            target="actions",
            button="打开检查测试",
            action_id="stable-check",
        )

    modules = sorted(
        module_counts.values(),
        key=lambda item: (severity_rank.get(str(item.get("severity") or ""), 9), -int(item.get("count", 0) or 0), str(item.get("module") or "")),
    )[:8]
    return {
        "status": status,
        "label": label,
        "summary": summary,
        "primary_action": primary_action,
        "counts": {
            "critical": critical_count,
            "warning": warning_count,
            "notice": notice_count,
            "bad_health": bad_health_count,
            "warn_health": warn_health_count,
            "recent_errors": len(recent_errors),
            "failed_audit": len(failed_audit),
            "log_errors": log_error_total,
            "transient_timeouts": transient_total,
            "stability_fail": int(stability.get("fail_count", 0) or 0),
            "stability_warn": int(stability.get("warn_count", 0) or 0),
        },
        "modules": modules,
        "next_steps": next_steps,
        "action_plan": action_plan,
    }


def build_stability_checks(snapshot: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    def add(
        key: str,
        label: str,
        status: str,
        detail: str,
        action: str = "",
    ) -> None:
        checks.append(
            {
                "key": key,
                "label": label,
                "status": status,
                "detail": detail,
                "action": action,
            }
        )

    git = snapshot.get("git", {}) if isinstance(snapshot.get("git"), dict) else {}
    version = str(git.get("version") or "").strip()
    commit = str(git.get("commit") or "").strip()
    version_ok = bool(version and version != "unknown" and SEMVER_VERSION_RE.match(version))
    add(
        "version",
        "版本信息",
        "ok" if version_ok and commit and commit != "unknown" else "fail",
        f"{version or 'unknown'} {commit or 'unknown'}".strip(),
        "确认 VERSION 文件存在，并且当前目录是 Git 仓库。" if not version_ok or not commit or commit == "unknown" else "",
    )

    services = snapshot.get("services", {}) if isinstance(snapshot.get("services"), dict) else {}
    service_labels = {"main": "主服务", "structure": "结构雷达", "web": "Web 控制台", "ai": "AI 助手"}
    down_services = [
        service_labels.get(key, key)
        for key, item in services.items()
        if isinstance(item, dict) and not bool(item.get("active_ok"))
    ]
    add(
        "services",
        "后台服务",
        "ok" if not down_services else "fail",
        "主服务、结构雷达、Web 控制台和 AI 助手均运行中" if not down_services else f"未运行：{'、'.join(down_services)}",
        "进入“雷达服务”页重启对应服务；如果仍失败，再看日志中心。" if down_services else "",
    )

    health = snapshot.get("health", []) if isinstance(snapshot.get("health"), list) else []
    bad_health = [item for item in health if isinstance(item, dict) and item.get("status") == "bad"]
    warn_health = [item for item in health if isinstance(item, dict) and item.get("status") == "warn"]
    health_status = "fail" if bad_health else ("warn" if warn_health else "ok")
    health_detail = "全部健康项通过"
    if bad_health:
        health_detail = "异常：" + "、".join(str(item.get("label") or "") for item in bad_health[:5])
    elif warn_health:
        health_detail = "警告：" + "、".join(str(item.get("label") or "") for item in warn_health[:5])
    add(
        "health",
        "健康门禁",
        health_status,
        health_detail,
        "先处理异常健康项；警告项可根据实际是否使用对应功能判断。" if health_status != "ok" else "",
    )

    issues = snapshot.get("issues", []) if isinstance(snapshot.get("issues"), list) else []
    critical_count = sum(1 for item in issues if isinstance(item, dict) and item.get("severity") == "critical")
    warning_count = sum(1 for item in issues if isinstance(item, dict) and item.get("severity") == "warning")
    issue_status = "fail" if critical_count else ("warn" if warning_count else "ok")
    add(
        "issues",
        "问题中心",
        issue_status,
        "无严重问题" if not critical_count and not warning_count else f"严重 {critical_count} 个，警告 {warning_count} 个",
        "按问题中心从上到下处理，优先处理严重问题。" if issue_status != "ok" else "",
    )

    logs = snapshot.get("log_errors", {}) if isinstance(snapshot.get("log_errors"), dict) else {}
    log_error_total = sum(
        int(item.get("error_count", 0) or 0)
        for item in logs.values()
        if isinstance(item, dict)
    )
    transient_total = sum(
        int(item.get("transient_count", 0) or 0)
        for item in logs.values()
        if isinstance(item, dict)
    )
    log_status = "fail" if log_error_total >= 20 else ("warn" if log_error_total or transient_total >= 10 else "ok")
    log_detail = "近期日志无错误"
    if log_error_total:
        log_detail = f"错误/异常关键字 {log_error_total} 条"
    elif transient_total:
        log_detail = f"可自动重试网络超时 {transient_total} 条"
    add(
        "logs",
        "日志稳定性",
        log_status,
        log_detail,
        "打开日志中心按“只看错误”筛选；网络超时持续增多时检查服务器网络。" if log_status != "ok" else "",
    )

    audit = snapshot.get("audit", {}) if isinstance(snapshot.get("audit"), dict) else {}
    failed_audit = audit.get("failed_recent", []) if isinstance(audit.get("failed_recent"), list) else []
    add(
        "audit",
        "后台操作审计",
        "warn" if failed_audit else "ok",
        f"最近失败操作 {len(failed_audit)} 条" if failed_audit else "最近没有失败操作",
        "进入审计记录页筛选失败，确认失败动作是否已重试成功。" if failed_audit else "",
    )

    config = snapshot.get("config", {}) if isinstance(snapshot.get("config"), dict) else {}
    telegram = config.get("telegram", {}) if isinstance(config.get("telegram"), dict) else {}
    ai = config.get("ai_assistant", {}) if isinstance(config.get("ai_assistant"), dict) else {}
    telegram_ok = bool(telegram.get("bot_token_configured") and telegram.get("chat_id_configured"))
    ai_enabled = bool(ai.get("enable"))
    ai_ok = bool(ai_enabled and ai.get("bot_token_configured"))
    config_status = "fail" if not telegram_ok else ("warn" if ai_enabled and not ai_ok else "ok")
    config_detail = "Telegram 推送和 AI Bot 配置可用"
    if not telegram_ok:
        config_detail = "Telegram 群推送缺 Token 或群 ID"
    elif ai_enabled and not ai_ok:
        config_detail = "AI 助手已开启但缺 AI_BOT_TOKEN"
    elif not ai_enabled:
        config_detail = "Telegram 群推送可用；AI 助手未启用"
    add(
        "config",
        "关键配置",
        config_status,
        config_detail,
        "进入配置中心补齐 Telegram 或 AI Bot 配置，保存后让后台自动应用。" if config_status != "ok" else "",
    )

    fail_count = sum(1 for item in checks if item.get("status") == "fail")
    warn_count = sum(1 for item in checks if item.get("status") == "warn")
    ok_count = sum(1 for item in checks if item.get("status") == "ok")
    if fail_count:
        status = "blocked"
        label = "未达稳定版标准"
        summary = f"{fail_count} 个阻断项，先处理后再长期运行。"
    elif warn_count:
        status = "attention"
        label = "基本可运行，建议关注"
        summary = f"{warn_count} 个警告项，不一定阻断运行，但建议确认。"
    else:
        status = "ready"
        label = "达到稳定版标准"
        summary = "核心服务、配置、日志和诊断均未发现阻断项。"
    return {
        "status": status,
        "label": label,
        "summary": summary,
        "ok_count": ok_count,
        "warn_count": warn_count,
        "fail_count": fail_count,
        "checks": checks,
    }


def build_release_readiness(snapshot: dict[str, Any]) -> dict[str, Any]:
    stability = snapshot.get("stability", {}) if isinstance(snapshot.get("stability"), dict) else {}
    problem_center = snapshot.get("problem_center", {}) if isinstance(snapshot.get("problem_center"), dict) else {}
    history = snapshot.get("stability_history", {}) if isinstance(snapshot.get("stability_history"), dict) else {}
    counts = problem_center.get("counts", {}) if isinstance(problem_center.get("counts"), dict) else {}
    records = history.get("records", []) if isinstance(history.get("records"), list) else []
    latest = history.get("latest", {}) if isinstance(history.get("latest"), dict) else {}

    checks: list[dict[str, Any]] = []

    def add(key: str, label: str, status: str, detail: str, action: str = "") -> None:
        checks.append(
            {
                "key": key,
                "label": label,
                "status": status,
                "detail": detail,
                "action": action,
            }
        )

    stability_status = str(stability.get("status") or "")
    if stability_status == "ready":
        add("current_stability", "当前稳定版验收", "ok", str(stability.get("summary") or "当前 stable-check 已通过"))
    elif stability_status == "attention":
        add(
            "current_stability",
            "当前稳定版验收",
            "warn",
            str(stability.get("summary") or "当前 stable-check 有警告"),
            "按稳定版自检里的警告项确认是否影响真实推送。",
        )
    else:
        add(
            "current_stability",
            "当前稳定版验收",
            "fail",
            str(stability.get("summary") or "当前 stable-check 未达标"),
            "先处理阻断项，再重新执行稳定版验收。",
        )

    problem_status = str(problem_center.get("status") or "")
    if problem_status == "ok":
        add("problem_center", "问题中心", "ok", str(problem_center.get("summary") or "问题中心当前健康"))
    elif problem_status == "attention":
        add(
            "problem_center",
            "问题中心",
            "warn",
            str(problem_center.get("summary") or "问题中心存在需要关注项"),
            "按处理清单确认警告是否影响主服务、结构雷达或 AI 助手。",
        )
    else:
        add(
            "problem_center",
            "问题中心",
            "fail",
            str(problem_center.get("summary") or "问题中心存在优先处理项"),
            "优先处理问题中心里的严重项，再评估长期运行。",
        )

    ready_records = [item for item in records if isinstance(item, dict) and item.get("status") == "ready"]
    latest_status = str(latest.get("status") or "")
    if not records:
        add(
            "stability_history",
            "验收历史",
            "warn",
            "还没有保存过稳定版验收历史，无法判断是否连续稳定。",
            "执行一次稳定版验收；更新后建议至少保留两次通过记录。",
        )
    elif latest_status == "blocked":
        add(
            "stability_history",
            "验收历史",
            "fail",
            "最近一次保存的稳定版验收未达标。",
            "处理问题后重新执行稳定版验收，覆盖最新历史状态。",
        )
    elif latest_status == "ready" and len(ready_records) >= 2:
        add(
            "stability_history",
            "验收历史",
            "ok",
            f"最近历史里已有 {len(ready_records)} 次达标记录，可作为长期运行参考。",
        )
    elif latest_status == "ready":
        add(
            "stability_history",
            "验收历史",
            "warn",
            "最近一次验收已达标，但达标历史少于 2 次。",
            "继续观察并再执行一次稳定版验收，确认不是偶然通过。",
        )
    else:
        add(
            "stability_history",
            "验收历史",
            "warn",
            "最近验收历史不是完全达标状态。",
            "按最新诊断处理警告后，再保存一条新的验收记录。",
        )

    log_errors = int(counts.get("log_errors", 0) or 0)
    failed_audit = int(counts.get("failed_audit", 0) or 0)
    transient_timeouts = int(counts.get("transient_timeouts", 0) or 0)
    if log_errors >= 10:
        add(
            "log_errors",
            "日志错误",
            "fail",
            f"近期日志错误/异常关键字 {log_errors} 条，数量偏高。",
            "打开日志中心筛选错误，先处理最早和重复次数最多的错误。",
        )
    elif log_errors:
        add(
            "log_errors",
            "日志错误",
            "warn",
            f"近期日志错误/异常关键字 {log_errors} 条。",
            "确认是否只是偶发错误；真实异常应先处理再作为完整稳定版候选。",
        )
    else:
        add("log_errors", "日志错误", "ok", "近期没有真实日志错误。")

    if failed_audit:
        add(
            "failed_audit",
            "后台操作审计",
            "warn",
            f"最近有 {failed_audit} 条失败的后台操作。",
            "打开审计记录确认失败操作是否已经重试成功。",
        )
    else:
        add("failed_audit", "后台操作审计", "ok", "最近没有失败的后台操作。")

    if transient_timeouts >= 10:
        add(
            "transient_timeouts",
            "网络重试噪声",
            "warn",
            f"近期可自动重试的网络超时 {transient_timeouts} 条。",
            "如果 Bot 回复和推送正常，可以继续观察；持续增多时检查服务器到 Telegram/API 的网络。",
        )
    else:
        add("transient_timeouts", "网络重试噪声", "ok", "网络超时在可接受范围内。")

    fail_count = sum(1 for item in checks if item.get("status") == "fail")
    warn_count = sum(1 for item in checks if item.get("status") == "warn")
    ok_count = sum(1 for item in checks if item.get("status") == "ok")
    score = max(0, min(100, 100 - fail_count * 25 - warn_count * 8))

    if fail_count:
        status = "blocked"
        label = "还不能作为完整稳定版"
        summary = "存在阻断项，先处理服务、配置、日志或验收问题，再继续推进下一阶段。"
        next_version_goal = "先把诊断报告清到无阻断，再重新执行 stable-check。"
    elif warn_count:
        status = "candidate"
        label = "准稳定候选"
        summary = "核心服务可运行，但仍有观察项；适合继续跑一段时间并保存新的验收记录。"
        next_version_goal = "连续两次验收达标且问题中心无警告后，再进入完整候选。"
    else:
        status = "complete_candidate"
        label = "完整稳定版候选"
        summary = "当前快照、问题中心、日志、审计和验收历史都达到长期运行候选标准。"
        next_version_goal = "可以进入 v1 完整稳定收口或 v2.0 功能规划。"

    requirements = [
        "后台核心服务运行正常",
        "当前 stable-check 达标",
        "问题中心没有严重项和警告项",
        "近期没有真实日志错误和失败审计",
        "至少保留两次稳定版达标历史",
    ]
    return {
        "status": status,
        "label": label,
        "summary": summary,
        "score": score,
        "ok_count": ok_count,
        "warn_count": warn_count,
        "fail_count": fail_count,
        "checks": checks,
        "requirements": requirements,
        "next_version_goal": next_version_goal,
    }


def _release_status_rank(status: str) -> int:
    return {
        "blocked": 0,
        "candidate": 1,
        "complete_candidate": 2,
    }.get(str(status or ""), -1)


def build_release_trend(history: dict[str, Any]) -> dict[str, Any]:
    records = history.get("records", []) if isinstance(history.get("records"), list) else []
    clean_records = [item for item in records if isinstance(item, dict)]
    if not clean_records:
        return {
            "status": "empty",
            "label": "暂无趋势",
            "summary": "还没有长期运行就绪度历史记录。",
            "current_score": None,
            "previous_score": None,
            "score_delta": None,
            "current_status": "",
            "previous_status": "",
            "action": "执行一次 stable-check 后会开始形成趋势。",
        }

    current = clean_records[0]
    previous = clean_records[1] if len(clean_records) > 1 else {}
    current_status = str(current.get("release_status") or "")
    previous_status = str(previous.get("release_status") or "")
    current_score_raw = current.get("release_score")
    previous_score_raw = previous.get("release_score")
    current_score = int(current_score_raw) if isinstance(current_score_raw, (int, float)) else None
    previous_score = int(previous_score_raw) if isinstance(previous_score_raw, (int, float)) else None
    score_delta = current_score - previous_score if current_score is not None and previous_score is not None else None
    status_delta = _release_status_rank(current_status) - _release_status_rank(previous_status)

    if not previous:
        status = "single"
        label = "等待下一次对比"
        summary = "当前只有一条长期运行就绪度历史，下一次 stable-check 后才能判断趋势。"
        action = "继续运行并在下次更新或排障后再执行 stable-check。"
    elif current_status == "blocked" and previous_status and previous_status != "blocked":
        status = "regressed"
        label = "发生回退"
        summary = "长期运行就绪度从候选状态回退到需要处理。"
        action = "优先打开问题中心和日志中心，按最新阻断项处理后重新验收。"
    elif status_delta > 0 or (score_delta is not None and score_delta >= 8):
        status = "improved"
        label = "趋势变好"
        summary = "长期运行就绪度比上一次验收更好。"
        action = "继续观察；如果连续达标，可以进入下一阶段规划。"
    elif status_delta < 0 or (score_delta is not None and score_delta <= -8):
        status = "worse"
        label = "趋势变差"
        summary = "长期运行就绪度比上一次验收变差。"
        action = "查看本次新增的警告或阻断项，先处理分数下降原因。"
    else:
        status = "stable"
        label = "趋势持平"
        summary = "长期运行就绪度和上一次基本一致。"
        action = "如果当前仍有警告，继续按处理清单收口；如果已达标，可继续观察。"

    return {
        "status": status,
        "label": label,
        "summary": summary,
        "current_score": current_score,
        "previous_score": previous_score,
        "score_delta": score_delta,
        "current_status": current_status,
        "previous_status": previous_status,
        "current_label": str(current.get("release_label") or ""),
        "previous_label": str(previous.get("release_label") or ""),
        "current_ts": str(current.get("ts") or current.get("generated_at") or ""),
        "previous_ts": str(previous.get("ts") or previous.get("generated_at") or ""),
        "action": action,
    }


def stability_latest_path(data_dir: Path | None = None) -> Path:
    return (data_dir or Settings.load().data_dir) / STABILITY_LATEST_FILE


def stability_history_path(data_dir: Path | None = None) -> Path:
    return (data_dir or Settings.load().data_dir) / STABILITY_HISTORY_FILE


def stability_record_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    git = snapshot.get("git", {}) if isinstance(snapshot.get("git"), dict) else {}
    stability = snapshot.get("stability", {}) if isinstance(snapshot.get("stability"), dict) else {}
    release = snapshot.get("release_readiness", {}) if isinstance(snapshot.get("release_readiness"), dict) else {}
    issues = snapshot.get("issues", []) if isinstance(snapshot.get("issues"), list) else []
    logs = snapshot.get("log_errors", {}) if isinstance(snapshot.get("log_errors"), dict) else {}
    log_error_total = sum(
        int(item.get("error_count", 0) or 0)
        for item in logs.values()
        if isinstance(item, dict)
    )
    transient_total = sum(
        int(item.get("transient_count", 0) or 0)
        for item in logs.values()
        if isinstance(item, dict)
    )
    return {
        "ts": now_text(),
        "generated_at": snapshot.get("generated_at", ""),
        "status": stability.get("status", "unknown"),
        "label": stability.get("label", "未知"),
        "summary": stability.get("summary", ""),
        "version": git.get("version", "unknown"),
        "branch": git.get("branch", "unknown"),
        "commit": git.get("commit", "unknown"),
        "ok_count": int(stability.get("ok_count", 0) or 0),
        "warn_count": int(stability.get("warn_count", 0) or 0),
        "fail_count": int(stability.get("fail_count", 0) or 0),
        "release_status": release.get("status", "unknown"),
        "release_label": release.get("label", "未知"),
        "release_score": release.get("score", None),
        "release_summary": release.get("summary", ""),
        "release_next_version_goal": release.get("next_version_goal", ""),
        "release_ok_count": int(release.get("ok_count", 0) or 0),
        "release_warn_count": int(release.get("warn_count", 0) or 0),
        "release_fail_count": int(release.get("fail_count", 0) or 0),
        "issue_count": len(issues),
        "log_error_count": log_error_total,
        "transient_count": transient_total,
    }


def load_stability_history(data_dir: Path | None = None, limit: int = STABILITY_HISTORY_LIMIT) -> list[dict[str, Any]]:
    path = stability_history_path(data_dir)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    rows = payload if isinstance(payload, list) else payload.get("records", []) if isinstance(payload, dict) else []
    records = [row for row in rows if isinstance(row, dict)]
    return records[: max(1, min(STABILITY_HISTORY_LIMIT, int(limit or STABILITY_HISTORY_LIMIT)))]


def save_stability_snapshot(
    snapshot: dict[str, Any],
    data_dir: Path | None = None,
    limit: int = STABILITY_HISTORY_LIMIT,
) -> dict[str, Any]:
    base_dir = data_dir or Settings.load().data_dir
    base_dir.mkdir(parents=True, exist_ok=True)
    latest_path = stability_latest_path(base_dir)
    history_path = stability_history_path(base_dir)
    previous_records = load_stability_history(base_dir, limit=limit)
    provisional_record = stability_record_from_snapshot(snapshot)
    max_records = max(1, min(STABILITY_HISTORY_LIMIT, int(limit or STABILITY_HISTORY_LIMIT)))
    provisional_records = [provisional_record, *previous_records][:max_records]
    snapshot["stability_history"] = {
        "latest_path": str(latest_path),
        "history_path": str(history_path),
        "latest": provisional_record,
        "records": provisional_records,
        "count": len(provisional_records),
    }
    snapshot["release_readiness"] = build_release_readiness(snapshot)
    record = stability_record_from_snapshot(snapshot)
    records = [record, *previous_records]
    trimmed = records[:max_records]
    snapshot["stability_history"] = {
        "latest_path": str(latest_path),
        "history_path": str(history_path),
        "latest": record,
        "records": trimmed,
        "count": len(trimmed),
    }
    snapshot["release_trend"] = build_release_trend(snapshot["stability_history"])
    latest_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    history_path.write_text(json.dumps(trimmed, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "saved": True,
        "latest_path": str(latest_path),
        "history_path": str(history_path),
        "record": record,
        "history_count": len(trimmed),
    }


def stability_history_payload(data_dir: Path | None = None, limit: int = 8) -> dict[str, Any]:
    base_dir = data_dir or Settings.load().data_dir
    latest_path = stability_latest_path(base_dir)
    latest_record: dict[str, Any] | None = None
    if latest_path.exists():
        try:
            latest_snapshot = json.loads(latest_path.read_text(encoding="utf-8"))
            if isinstance(latest_snapshot, dict):
                latest_record = stability_record_from_snapshot(latest_snapshot)
        except Exception:
            latest_record = None
    records = load_stability_history(base_dir, limit=limit)
    return {
        "latest_path": str(latest_path),
        "history_path": str(stability_history_path(base_dir)),
        "latest": latest_record,
        "records": records,
        "count": len(records),
    }


def ops_snapshot_payload() -> dict[str, Any]:
    summary = summary_payload()
    settings = Settings.load()
    audit_all = web_audit_payload(limit=10, result="all")
    audit_failed = web_audit_payload(limit=10, result="failed")
    log_errors = {
        target: log_error_excerpt(target, lines=300, limit=20)
        for target in ("main", "structure", "web", "ai")
    }
    snapshot = {
        "ok": True,
        "generated_at": now_text(),
        "git": summary.get("git", {}),
        "services": summary.get("services", {}),
        "health": summary.get("health", []),
        "recent_errors": summary.get("recent_errors", []),
        "runtime": summary.get("runtime", {}),
        "config": summary.get("config", {}),
        "state_files": summary.get("state_files", []),
        "audit": {
            "recent": audit_all.get("records", []),
            "failed_recent": audit_failed.get("records", []),
            "total": audit_all.get("total", 0),
            "failed_matched": audit_failed.get("matched", 0),
        },
        "log_errors": log_errors,
    }
    snapshot["issues"] = build_ops_issues(snapshot)
    snapshot["stability"] = build_stability_checks(snapshot)
    snapshot["stability_history"] = stability_history_payload(settings.data_dir, limit=8)
    snapshot["problem_center"] = build_problem_center(snapshot)
    snapshot["release_readiness"] = build_release_readiness(snapshot)
    snapshot["release_trend"] = build_release_trend(snapshot["stability_history"])
    snapshot["recommendations"] = build_ops_recommendations(snapshot)
    snapshot["message"] = "已生成安全运维快照，可复制给排查人员；不包含 Token、API Key 或提示词正文。"
    return snapshot


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
    button, input, select, textarea { font: inherit; }
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
    .page-intro {
      grid-column: span 12;
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 14px;
      align-items: start;
      background:
        linear-gradient(135deg, rgba(255,255,255,.94), rgba(236,241,244,.82)),
        linear-gradient(90deg, rgba(15,111,104,.12), transparent 38%);
      border-color: rgba(75, 86, 94, .24);
    }
    .page-kicker {
      color: var(--accent);
      font-size: 12px;
      font-weight: 800;
      letter-spacing: 0;
      margin-bottom: 4px;
    }
    .page-intro h2 {
      margin: 0;
      font-size: 18px;
      line-height: 1.25;
      letter-spacing: 0;
    }
    .page-intro p {
      margin: 7px 0 0;
      color: var(--muted);
      max-width: 980px;
    }
    .intro-tags {
      display: flex;
      gap: 6px;
      flex-wrap: wrap;
      justify-content: flex-end;
      max-width: 520px;
    }
    .empty-state {
      display: grid;
      gap: 6px;
      justify-items: start;
      border: 1px dashed rgba(93, 106, 115, .32);
      border-radius: 8px;
      padding: 14px;
      color: var(--muted);
      background: linear-gradient(135deg, rgba(255,255,255,.62), rgba(237,242,244,.54));
    }
    .empty-state strong { color: var(--text); }
    .span-3 { grid-column: span 3; }
    .span-4 { grid-column: span 4; }
    .span-6 { grid-column: span 6; }
    .span-8 { grid-column: span 8; }
    .span-12 { grid-column: span 12; }
    .metric { display: grid; gap: 5px; min-height: 82px; }
    .metric .label { color: var(--muted); font-size: 12px; }
    .metric .value { font-size: 20px; font-weight: 700; overflow-wrap: anywhere; }
    .mini-metrics { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 8px; margin-top: 10px; }
    .mini-metric {
      display: grid;
      gap: 4px;
      padding: 10px;
      border: 1px solid rgba(101, 113, 121, .16);
      border-radius: 8px;
      background: linear-gradient(135deg, rgba(255,255,255,.55), rgba(239,244,247,.46));
    }
    .mini-metric .label { color: var(--muted); font-size: 12px; font-weight: 700; }
    .mini-metric .value { font-size: 15px; font-weight: 800; }
    .mini-metric .muted { font-size: 12px; }
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
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 9px;
      background: linear-gradient(180deg, #ffffff, #f7f9fa);
      color: var(--text);
      min-height: 36px;
      box-shadow: 0 1px 0 rgba(255,255,255,.8) inset;
    }
    textarea {
      min-height: 320px;
      resize: vertical;
      line-height: 1.55;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      font-size: 13px;
    }
    input:focus, select:focus, textarea:focus {
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
    .field-explain {
      display: grid;
      gap: 6px;
      margin-top: 2px;
      border: 1px solid rgba(93, 106, 115, .18);
      border-radius: 6px;
      padding: 8px;
      background: linear-gradient(135deg, rgba(255,255,255,.58), rgba(238,243,245,.48));
    }
    .field-explain-row {
      display: grid;
      grid-template-columns: 92px 1fr;
      gap: 8px;
      align-items: start;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .field-explain-label {
      color: #34424a;
      font-weight: 800;
    }
    .section-title { margin: 2px 0 10px; font-size: 15px; }
    .output { margin-top: 12px; white-space: pre-wrap; }
    .result-panel {
      background: var(--metal);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 14px;
      display: grid;
      gap: 12px;
      white-space: normal;
    }
    .error-card {
      border-color: rgba(179, 38, 30, .32);
      background: linear-gradient(135deg, rgba(255,255,255,.92), rgba(255,242,239,.78));
    }
    .error-card .raw-details {
      box-shadow: none;
      background: rgba(255,255,255,.58);
    }
    .result-title {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 12px;
      font-size: 15px;
      font-weight: 800;
    }
    .result-list {
      margin: 0;
      padding-left: 18px;
      color: var(--muted);
      line-height: 1.6;
    }
    .result-list li { margin: 3px 0; }
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
    .issue-list { display: grid; gap: 10px; }
    .issue-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 12px;
      background: linear-gradient(135deg, rgba(255,255,255,.82), rgba(239,244,246,.72));
      display: grid;
      gap: 9px;
    }
    .issue-card.critical { border-color: rgba(179, 38, 30, .34); background: linear-gradient(135deg, rgba(255,255,255,.92), rgba(255,240,236,.78)); }
    .issue-card.warning { border-color: rgba(154, 101, 8, .34); background: linear-gradient(135deg, rgba(255,255,255,.92), rgba(255,248,231,.78)); }
    .issue-card.notice { border-color: rgba(15, 111, 104, .26); }
    .issue-head {
      display: flex;
      gap: 8px;
      align-items: flex-start;
      justify-content: space-between;
    }
    .issue-title { font-weight: 800; }
    .issue-meta { display: flex; gap: 6px; flex-wrap: wrap; }
    .issue-detail { color: var(--muted); }
    .issue-action {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 10px;
      align-items: center;
      border-top: 1px solid rgba(105, 118, 126, .18);
      padding-top: 9px;
      color: var(--muted);
    }
    .config-category-bar {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-bottom: 12px;
    }
    .config-category-bar .btn.active {
      background: linear-gradient(135deg, #34424a, #202a30);
      border-color: #202a30;
      color: #fff;
    }
    .config-module-grid {
      grid-column: span 12;
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 12px;
    }
    .config-module-card {
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 14px;
      background: var(--metal);
      box-shadow: var(--shadow);
      text-align: left;
      cursor: pointer;
      color: var(--text);
      min-height: 126px;
      display: grid;
      gap: 8px;
      align-content: start;
    }
    .config-module-meta {
      display: grid;
      gap: 5px;
      color: var(--muted);
      font-size: 12px;
      line-height: 1.45;
    }
    .config-module-meta strong { color: #34424a; }
    .config-module-card:hover {
      border-color: rgba(15, 118, 110, .42);
      box-shadow: 0 14px 30px rgba(20, 31, 36, .12), 0 1px 1px rgba(255,255,255,.75) inset;
    }
    .config-module-title {
      display: flex;
      justify-content: space-between;
      gap: 10px;
      align-items: flex-start;
      font-weight: 800;
      font-size: 15px;
    }
    .config-category-summary {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      margin-top: 10px;
    }
    .config-category-summary > div {
      border: 1px solid rgba(93, 106, 115, .18);
      border-radius: 8px;
      padding: 9px;
      background: linear-gradient(135deg, rgba(255,255,255,.58), rgba(238,243,245,.46));
      color: var(--muted);
      font-size: 13px;
    }
    .config-category-summary strong { display: block; color: #34424a; margin-bottom: 3px; }
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
      .config-category-summary { grid-template-columns: 1fr; }
      .config-module-grid { grid-template-columns: 1fr; }
      .form-grid { grid-template-columns: 1fr; }
      .mini-metrics { grid-template-columns: 1fr; }
      .field-heading { grid-template-columns: 1fr; }
      .field-current { justify-self: start; text-align: left; }
      .page-intro { grid-template-columns: 1fr; }
      .intro-tags { justify-content: flex-start; }
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
        <button data-view="ai">AI 助手</button>
        <button data-view="price">价格提醒</button>
        <button data-view="services">雷达服务</button>
        <button data-view="config">配置中心</button>
        <button data-view="logs">日志中心</button>
        <button data-view="audit">审计记录</button>
        <button data-view="report">诊断报告</button>
        <button data-view="actions">检查测试</button>
        <button data-view="preview">更新备份</button>
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
          <button id="autoRefreshButton" class="btn" onclick="toggleAutoRefresh()">自动刷新：关闭</button>
        </div>
      </header>

      <section id="overview" class="view">
        <div class="grid" id="overviewGrid"></div>
      </section>

      <section id="logs" class="view hidden">
        <div class="grid" id="logsIntro"></div>
        <div class="toolbar">
      <select id="logTarget">
        <option value="main">主服务</option>
        <option value="structure">结构雷达</option>
        <option value="web">Web 控制台</option>
        <option value="ai">AI 助手</option>
      </select>
          <select id="logLines">
            <option value="200">最近 200 行</option>
            <option value="500">最近 500 行</option>
            <option value="1000">最近 1000 行</option>
          </select>
          <select id="logFilter" onchange="renderFilteredLogs()">
            <option value="all">全部日志</option>
            <option value="error">只看错误</option>
            <option value="telegram">Telegram</option>
            <option value="binance">Binance</option>
            <option value="structure">结构雷达</option>
            <option value="ai">AI 助手</option>
            <option value="funding">资金费率</option>
          </select>
          <input id="logSearch" placeholder="搜索币种、错误或关键词" oninput="renderFilteredLogs()">
          <button class="btn primary" onclick="loadLogs()">读取日志</button>
          <button class="btn" onclick="copyLogs()">复制</button>
        </div>
        <div id="logInsight" class="panel"></div>
        <pre id="logOutput"></pre>
      </section>

      <section id="audit" class="view hidden">
        <div class="grid" id="auditGrid"></div>
      </section>

      <section id="report" class="view hidden">
        <div class="grid" id="reportGrid"></div>
      </section>

      <section id="config" class="view hidden">
        <div id="configCategoryBar" class="config-category-bar"></div>
        <div id="configForms" class="grid"></div>
        <div id="configSaveToolbar" class="toolbar hidden" style="margin-top:12px">
          <button class="btn" onclick="previewConfig()">预览改动</button>
          <button id="applyStructureButton" class="btn blue hidden" onclick="applyStructureRecommendations()">应用复盘建议</button>
          <button class="btn primary" onclick="saveConfig()">保存配置</button>
        </div>
        <div id="configPreview" class="panel hidden"></div>
        <div id="configOutput" class="output"></div>
      </section>

      <section id="ai" class="view hidden">
        <div class="grid" id="aiGrid"></div>
        <pre id="aiOutput" class="output"></pre>
      </section>

      <section id="price" class="view hidden">
        <div class="grid" id="priceGrid"></div>
        <div id="priceOutput" class="output"></div>
      </section>

      <section id="prompts" class="view hidden">
        <div class="grid" id="promptGrid"></div>
        <pre id="promptOutput" class="output"></pre>
      </section>

      <section id="actions" class="view hidden">
        <div class="grid" id="actionGrid"></div>
        <div id="actionOutput" class="output"></div>
      </section>

      <section id="services" class="view hidden">
        <div class="grid" id="serviceGrid"></div>
        <div id="serviceOutput" class="output"></div>
      </section>

      <section id="preview" class="view hidden">
        <div class="grid" id="previewGrid"></div>
        <pre id="updateOutput" class="output"></pre>
      </section>

      <section id="guide" class="view hidden">
        <div class="grid" id="guideGrid"></div>
      </section>
    </main>
  </div>

  <script>
    const titles = {
      overview: "总览",
      ai: "AI 助手",
      price: "价格提醒",
      prompts: "AI 提示词",
      services: "雷达服务",
      config: "配置中心",
      logs: "日志中心",
      audit: "审计记录",
      report: "诊断报告",
      actions: "检查测试",
      preview: "更新备份",
      guide: "功能说明"
    };
    const viewMeta = {
      overview: {
        kicker: "运维总览",
        title: "单人管理员入口",
        desc: "服务器只需要记住 paopao；Web 后台负责状态查看、日志定位、配置修改、服务控制和更新入口。所有运维权限都集中给你本人使用，不做多用户角色区分。",
        tags: ["服务状态", "健康检查", "关键配置"]
      },
      ai: {
        kicker: "AI Bot",
        title: "AI 助手运行中心",
        desc: "查看独立 AI Bot、问答接口、提示词入口和价格提醒统计。这里说明 AI Bot 能做什么，但日常对话仍在 Telegram 私聊里完成。",
        tags: ["独立 Bot", "提示词", "意图分流"]
      },
      price: {
        kicker: "个人提醒",
        title: "价格提醒管理",
        desc: "查看、筛选、暂停、恢复和删除提醒。Web 适合管理员快速排查；普通提醒创建仍推荐 Telegram 私聊按钮流程。",
        tags: ["目标价", "筛选", "提醒状态"]
      },
      config: {
        kicker: "配置中心",
        title: "按功能模块管理配置",
        desc: "配置现在按功能模块分开管理。先选择 Telegram、AI、雷达参数、资金费率、外部接口、Web 控制台或备份恢复，再进入对应设置。保存前会预检影响模块和自动应用动作。",
        tags: ["分类配置", "保存预检", "自动应用"]
      },
      logs: {
        kicker: "排查日志",
        title: "日志中心",
        desc: "读取主服务、结构雷达、Web 控制台和 AI 助手日志，按错误、Telegram、Binance、结构、AI、资金费率筛选。先看摘要，再看原文。",
        tags: ["筛选", "复制", "自动刷新"]
      },
      audit: {
        kicker: "操作账本",
        title: "审计记录",
        desc: "审计记录是 Web 后台的操作账本。记录关键操作的动作、对象、结果、耗时和错误摘要，不保存 Token、API Key 或提示词正文。",
        tags: ["成功/失败", "搜索", "安全脱敏"]
      },
      report: {
        kicker: "问题快照",
        title: "诊断报告",
        desc: "一键诊断报告用于排查问题。问题中心会按严重程度汇总服务健康、最近错误、失败审计、日志错误和可自动重试网络超时，并给出建议动作和相关日志入口。",
        tags: ["健康检查", "日志片段", "复制报告"]
      },
      actions: {
        kicker: "白名单动作",
        title: "检查测试",
        desc: "这里的按钮都是固定白名单动作，只执行页面写明的检查、测试或清理动作，不能输入任意命令。会真实发送或删除文件的动作需要确认词。",
        tags: ["只读检查", "测试发送", "安全确认"]
      },
      services: {
        kicker: "后台服务",
        title: "雷达服务控制",
        desc: "控制主服务、结构雷达、Web 控制台和 AI 助手。优先使用重启；停止会暂停对应功能，并需要二次确认。",
        tags: ["重启", "启动", "停止"]
      },
      preview: {
        kicker: "更新与备份",
        title: "更新备份",
        desc: "推送预览只展示格式，不会调用 Telegram；更新检查只看 GitHub 是否有新版本，不会自动更新服务器代码；配置备份仍在配置中心里恢复或删除。",
        tags: ["推送预览", "更新检查", "备份入口"]
      },
      prompts: {
        kicker: "AI 提示词",
        title: "提示词管理",
        desc: "编辑日常 AI 助手和专业分析师提示词。保存或恢复默认后会自动重启 AI 助手服务。",
        tags: ["日常助手", "专业分析", "自动重启"]
      },
      guide: {
        kicker: "使用说明",
        title: "功能说明",
        desc: "查看当前版本、外部接口用途、页面功能和安全规则。这里是后台的内置说明书。",
        tags: ["版本", "接口用途", "安全规则"]
      }
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
        id: "stable-check",
        label: "执行稳定版验收",
        badge: "保存验收历史",
        desc: "把当前服务、配置、日志、审计和问题中心合成稳定版验收结果，并保存到验收历史。",
        details: [
          "更新后或处理完异常后执行，用来确认当前部署是否适合长期运行。",
          "结果会写入 data/stable_check_latest.json 和 data/stable_check_history.json。",
          "如果显示未达标，优先回到诊断报告查看问题中心总览和处理清单。"
        ]
      },
      {
        id: "web-self-check",
        label: "Web API 自诊断",
        badge: "前端自检",
        clientOnly: true,
        desc: "从浏览器连续读取总览、配置和 Web 日志接口，用来判断后台页面是不是接口慢、鉴权异常或日志读取异常。",
        details: [
          "这个动作不会执行服务器命令，也不会修改配置。",
          "会分别显示每个接口是否正常、HTTP 状态、服务端返回时间和浏览器实测耗时。",
          "适合页面卡顿、按钮没反应、保存失败时先做一次自检。"
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
        danger: true,
        confirmWord: "SEND",
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
        id: "funding-alert",
        label: "扫描资金费率警报",
        badge: "多交易所扫描",
        desc: "立即执行一轮资金费率警报扫描，检查 Binance、OKX、Bybit、Bitget、Gate 是否出现极端费率、周期缩短或交易所偏离。",
        details: [
          "默认只在达到警报阈值时生成推送内容；没有异常时输出会显示扫描数量和 alerts=0。",
          "真实发送仍受 Telegram 配置、话题 ID、冷却时间和 readiness 门禁影响。",
          "适合修改资金费率阈值、交易所列表或话题 ID 后手动验证。"
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
        danger: true,
        confirmWord: "CLEANUP",
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
        brand: "ai",
        logoUrl: "https://www.google.com/s2/favicons?domain=deepseek.com&sz=64",
        name: "AI 问答接口",
        status: "可选",
        keyText: "可填写 AI_API_KEY、AI_BASE_URL、AI_MODEL，并开启 AI_PROVIDER_ENABLE",
        usage: "负责 AI 助手 Bot 的自然语言问答、运行状态解释和提醒说明；价格提醒本身不依赖 AI Key。",
        supports: ["AI 私聊问答", "运行状态解释", "价格提醒说明", "后续策略助手扩展"],
        note: "AI_BOT_TOKEN 是 Telegram 机器人令牌；AI_API_KEY 是模型接口令牌，二者不是同一个配置。"
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
      },
      {
        name: "AI 助手 Bot",
        service: "paopao-ai",
        desc: "负责独立 Telegram 私聊、手动价格提醒和 AI 问答。它停了以后，群里的雷达推送不会受影响。",
        actions: [
          { id: "restart-ai", label: "重启 AI 助手", button: "重启", level: "warn", note: "修改 AI_BOT_TOKEN、AI API Key、允许用户 ID 或提醒间隔后使用。" },
          { id: "start-ai", label: "启动 AI 助手", button: "启动", level: "", note: "AI 助手服务停止后，想恢复私聊和价格提醒时使用。" },
          { id: "stop-ai", label: "停止 AI 助手", button: "停止", level: "danger", note: "会暂停 AI 私聊和价格提醒检查，但不影响主雷达推送。" }
        ]
      }
    ];
    let currentView = "overview";
    let currentConfigCategory = "home";
    let latestConfigData = null;
    let latestLogData = null;
    let latestAuditData = null;
    let latestReportData = null;
    let latestPriceAlertsData = null;
    let autoRefreshTimer = null;
    let autoRefreshEnabled = false;
    const autoRefreshIntervalMs = 15000;
    const configCategories = [
      {
        id: "home",
        label: "配置首页",
        desc: "按功能模块进入配置，不再把所有项目设置堆在同一页。",
        sections: []
      },
      {
        id: "telegram",
        label: "Telegram 推送",
        desc: "群推送机器人 Token、群 ID、话题 ID、自动创建话题等消息入口配置。",
        impact: "影响群推送、话题路由、测试消息和 readiness。",
        apply: "保存后自动重启主服务和结构雷达。",
        sections: ["Telegram"]
      },
      {
        id: "ai",
        label: "AI Bot",
        desc: "独立 AI Bot、允许用户/群组、AI 接口地址、模型、提示词文件和币种档案索引。",
        impact: "影响私聊 AI、行情分析、群内 @ 调用和币种档案读取。",
        apply: "保存后自动重启 AI 助手服务。",
        sections: ["AI 助手"],
        excludeKeys: ["AI_PRICE_ALERTS_ENABLE", "AI_DEFAULT_CHAT_ID", "AI_ALERT_CHECK_INTERVAL_SEC"]
      },
      {
        id: "price-alerts",
        label: "价格提醒",
        desc: "AI Bot 私聊里的手动价格提醒开关、默认接收人和提醒扫描间隔。",
        impact: "影响目标价、急涨急跌、OI、资金费率等个人提醒的检测和推送。",
        apply: "保存后自动重启 AI 助手服务。",
        sections: ["AI 助手"],
        keys: ["AI_PRICE_ALERTS_ENABLE", "AI_DEFAULT_CHAT_ID", "AI_ALERT_CHECK_INTERVAL_SEC"]
      },
      {
        id: "radar",
        label: "主雷达参数",
        desc: "资金摘要和资金流雷达的推送间隔、统计窗口和扫描范围。",
        impact: "影响主服务里的资金摘要和资金流雷达，不直接影响 AI Bot。",
        apply: "保存后自动重启主服务和结构雷达。",
        sections: ["雷达参数"],
        keys: ["RADAR_SUMMARY_MIN_INTERVAL_SEC", "FLOW_INTERVAL_SEC", "FLOW_SCAN_LIMIT"]
      },
      {
        id: "funding",
        label: "资金费率警报",
        desc: "独立资金费率警报，以及启动预警里多交易所资金费率展示相关参数。",
        impact: "影响资金费率警报话题、启动信号里的资金费率备注、阈值、冷却和回复链。",
        apply: "保存后自动重启主服务和结构雷达。",
        sections: ["资金费率警报", "雷达参数"],
        keys: [
          "FUNDING_ALERT_ENABLE",
          "FUNDING_ALERT_INTERVAL_SEC",
          "FUNDING_ALERT_SCAN_LIMIT",
          "FUNDING_ALERT_EXCHANGES",
          "FUNDING_ALERT_EXTREME_NEGATIVE_PCT",
          "FUNDING_ALERT_SUPER_NEGATIVE_PCT",
          "FUNDING_ALERT_EXTREME_POSITIVE_PCT",
          "FUNDING_ALERT_MIN_EXCHANGE_COUNT",
          "FUNDING_ALERT_DIVERGENCE_PCT",
          "FUNDING_ALERT_COOLDOWN_SEC",
          "FUNDING_ALERT_REPLY_CHAIN_ENABLE",
          "FUNDING_ALERT_DECAY_QUIET_SCANS",
          "FUNDING_ALERT_END_QUIET_SCANS",
          "LAUNCH_MULTI_EXCHANGE_FUNDING_ENABLE",
          "LAUNCH_FUNDING_EXCHANGES",
          "LAUNCH_FUNDING_HISTORY_LIMIT"
        ]
      },
      {
        id: "structure",
        label: "结构雷达",
        desc: "结构雷达开关、扫描数量、临界距离、最低分、结构图数量、同币冷却和复盘建议。",
        impact: "影响结构突破信号数量、图片刷屏程度、假突破过滤和结构复盘统计。",
        apply: "保存后自动重启主服务和结构雷达。",
        sections: ["模块开关", "雷达参数"],
        keys: [
          "STRUCTURE_RADAR_ENABLE",
          "STRUCTURE_REVIEW_ENABLE",
          "STRUCTURE_TOP_SYMBOLS",
          "STRUCTURE_NEAR_EDGE_PCT",
          "STRUCTURE_MIN_SCORE",
          "STRUCTURE_SEND_CHART_TOP_N",
          "STRUCTURE_COOLDOWN_SEC"
        ],
        special: "structure"
      },
      {
        id: "market",
        label: "行情源 / 外部接口",
        desc: "Binance、CoinPaprika、Coinalyze、盘口深度和外部确认相关配置说明。",
        impact: "影响结构雷达外部确认、盘口墙、流动性辅助和可选 Coinalyze 数据。",
        apply: "保存后自动重启主服务和结构雷达。",
        sections: ["Coinalyze", "模块开关", "雷达参数"],
        keys: [
          "COINALYZE_ENABLE",
          "COINALYZE_API_KEY",
          "LIQUIDITY_FALLBACK_ENABLE",
          "BINANCE_ORDERBOOK_LIQUIDITY_ENABLE",
          "LIQUIDITY_SCORE_MAX_DELTA",
          "LIQUIDITY_MIN_DISTANCE_PCT",
          "LIQUIDITY_MAX_DISTANCE_PCT",
          "BINANCE_ORDERBOOK_DEPTH_LIMIT"
        ],
        special: "api"
      },
      {
        id: "switches",
        label: "模块开关",
        desc: "话题说明置顶和运行垃圾清理这类通用开关。",
        impact: "影响话题说明展示、置顶和 cleanup 行为。",
        apply: "保存后自动重启主服务和结构雷达。",
        sections: ["模块开关"],
        keys: ["TG_TOPIC_INTRO_ENABLE", "TG_TOPIC_INTRO_PIN", "CLEANUP_ENABLE"]
      },
      {
        id: "web",
        label: "Web 控制台",
        desc: "Web 后台监听地址、访问端口和访问令牌配置。",
        impact: "影响浏览器打开后台的地址、端口和登录令牌。",
        apply: "保存后自动延迟重启 Web 控制台。",
        sections: ["Web 控制台"]
      },
      {
        id: "backup",
        label: "备份恢复",
        desc: "查看、恢复或删除 Web 自动生成的 .env.oi 配置备份。",
        impact: "影响 .env.oi 配置文件回滚，不直接改变源码。",
        apply: "恢复备份后会自动应用新配置；删除备份不会重启服务。",
        sections: [],
        special: "backup"
      }
    ];

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
      const started = performance.now();
      const res = await fetch(path, { ...options, headers: { ...headers(), ...(options.headers || {}) } });
      const elapsedMs = Math.round(performance.now() - started);
      if (res.status === 401) {
        showAuth();
        throw new Error("需要访问令牌");
      }
      const text = await res.text();
      let data;
      try { data = JSON.parse(text); } catch { data = { ok: res.ok, text }; }
      if (data && typeof data === "object") data._client_elapsed_ms = elapsedMs;
      if (!res.ok) {
        const error = new Error(apiErrorMessage(data, res, elapsedMs));
        error.payload = data;
        error.status = res.status;
        throw error;
      }
      return data;
    }
    function apiErrorMessage(data, res, elapsedMs) {
      const payload = data && typeof data === "object" ? data : {};
      const message = payload.message || payload.error || payload.text || `HTTP ${res.status}`;
      const code = payload.code ? `，错误码：${payload.code}` : "";
      return `${message}（HTTP ${res.status}，耗时 ${elapsedMs}ms${code}）`;
    }
    function apiMetaLine(data) {
      if (!data || typeof data !== "object") return "";
      const meta = data._meta || {};
      const parts = [];
      if (meta.path) parts.push(`接口：${meta.path}`);
      if (meta.status) parts.push(`HTTP：${meta.status}`);
      if (meta.served_at) parts.push(`服务端时间：${meta.served_at}`);
      if (data._client_elapsed_ms !== undefined) parts.push(`浏览器耗时：${data._client_elapsed_ms}ms`);
      return parts.join("，");
    }
    function apiOrError(path, label, options = {}) {
      return api(path, options)
        .then(data => ({ ok: true, data, label, path }))
        .catch(error => ({ ok: false, error, label, path }));
    }
    function errorDetails(err) {
      const payload = err && err.payload && typeof err.payload === "object" ? err.payload : {};
      const meta = payload._meta || {};
      return {
        message: (err && err.message) || String(err || "未知错误"),
        status: (err && err.status) || meta.status || "",
        path: meta.path || "",
        code: payload.code || "",
        meta,
        payload
      };
    }
    function renderErrorPanel(title, err, options = {}) {
      const details = errorDetails(err);
      const compact = Boolean(options.compact);
      const metaLine = apiMetaLine(details.payload);
      const pathText = options.path || details.path || "未知接口";
      return `<div class="panel span-12 error-card">
        <div class="result-title">
          <span>${escapeHtml(title || "页面加载失败")}</span>
          ${statusPill("异常", false)}
        </div>
        <div class="readable-list">
          ${row("原因", textValue(details.message))}
          ${row("接口", `<code>${escapeHtml(pathText)}</code>`)}
          ${details.status ? row("HTTP", neutralPill(String(details.status))) : ""}
          ${details.code ? row("错误码", `<code>${escapeHtml(details.code)}</code>`) : ""}
          ${metaLine ? row("耗时/时间", textValue(metaLine)) : ""}
          ${compact ? "" : row("下一步", textValue("先点“重试”；如果仍失败，打开诊断报告，再去日志中心按“只看错误”筛选。"))}
        </div>
        <div class="toolbar" style="margin:4px 0 0">
          <button class="btn primary" type="button" onclick="refreshCurrent()">重试</button>
          <button class="btn" type="button" onclick="switchView('report')">打开诊断报告</button>
          <button class="btn" type="button" onclick="switchView('logs')">打开日志中心</button>
        </div>
        ${rawDetails("高级排查：接口错误 JSON", details.payload && Object.keys(details.payload).length ? details.payload : details)}
      </div>`;
    }
    function partialErrorPanels(results) {
      return (results || [])
        .filter(result => result && !result.ok)
        .map(result => renderErrorPanel(`${result.label || "接口"} 读取失败`, result.error, { compact: true, path: result.path }))
        .join("");
    }
    function viewTargetId(view) {
      return {
        overview: "overviewGrid",
        logs: "logInsight",
        audit: "auditGrid",
        report: "reportGrid",
        config: "configForms",
        ai: "aiGrid",
        price: "priceGrid",
        prompts: "promptGrid",
        actions: "actionGrid",
        services: "serviceGrid",
        preview: "previewGrid",
        guide: "guideGrid"
      }[view] || "";
    }
    function renderViewError(view, err, isAuto = false) {
      const targetId = viewTargetId(view);
      const target = targetId ? document.getElementById(targetId) : null;
      if (target) target.innerHTML = renderErrorPanel(isAuto ? "自动刷新失败" : "页面加载失败", err);
      if (view === "logs") {
        const output = document.getElementById("logOutput");
        if (output) output.textContent = "";
      }
    }
    function setSubtitle(text) { document.getElementById("subtitle").textContent = text; }
    function autoRefreshSupported(view = currentView) {
      return ["overview", "logs", "audit"].includes(view);
    }
    function updateAutoRefreshButton() {
      const btn = document.getElementById("autoRefreshButton");
      if (!btn) return;
      const supported = autoRefreshSupported();
      btn.disabled = !supported;
      btn.textContent = supported
        ? `自动刷新：${autoRefreshEnabled ? "开启" : "关闭"}`
        : "自动刷新：当前页不可用";
      btn.classList.toggle("primary", supported && autoRefreshEnabled);
    }
    function stopAutoRefresh() {
      if (autoRefreshTimer) clearInterval(autoRefreshTimer);
      autoRefreshTimer = null;
    }
    function startAutoRefresh() {
      stopAutoRefresh();
      if (!autoRefreshEnabled || !autoRefreshSupported()) {
        updateAutoRefreshButton();
        return;
      }
      autoRefreshTimer = setInterval(() => {
        if (autoRefreshSupported()) refreshCurrent(true);
      }, autoRefreshIntervalMs);
      updateAutoRefreshButton();
    }
    function toggleAutoRefresh() {
      if (!autoRefreshSupported()) return;
      autoRefreshEnabled = !autoRefreshEnabled;
      startAutoRefresh();
      if (autoRefreshEnabled) refreshCurrent(true);
    }
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
        "funding-alert": "资金费率警报",
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
        "funding-alert": "资金费率警报",
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
    function renderPageIntro(view, extraTags = []) {
      const meta = viewMeta[view] || { kicker: "页面", title: titles[view] || view, desc: "", tags: [] };
      const tags = [...(meta.tags || []), ...(extraTags || [])].filter(Boolean).slice(0, 6);
      return `<div class="panel page-intro">
        <div>
          <div class="page-kicker">${escapeHtml(meta.kicker || "")}</div>
          <h2>${escapeHtml(meta.title || titles[view] || view)}</h2>
          <p>${escapeHtml(meta.desc || "")}</p>
        </div>
        <div class="intro-tags">${tags.map(tag => neutralPill(tag)).join("")}</div>
      </div>`;
    }
    function emptyState(title, desc, actionHtml = "") {
      return `<div class="empty-state"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(desc || "")}</span>${actionHtml || ""}</div>`;
    }
    function tableEmpty(colspan, title, desc) {
      return `<tr><td colspan="${Number(colspan) || 1}">${emptyState(title, desc)}</td></tr>`;
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
        row("最近费率警报", textValue(runtimeItem.last_funding_alert_at)),
        row("下次费率警报", textValue(runtimeItem.next_funding_alert_at)),
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
      const ai = cfg.ai_assistant || {};
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
        configCard("AI 助手配置", [
          row("AI 助手 Bot", neutralPill(zhBool(ai.enable))),
          row("AI_BOT_TOKEN", neutralPill(configuredText(ai.bot_token_configured))),
          row("允许用户", neutralPill(ai.admin_user_ids_configured ? `${ai.admin_user_count || 0} 个` : "未限制")),
          row("允许群/频道", neutralPill(ai.allowed_chat_ids_configured ? `${ai.allowed_chat_count || 0} 个` : "未配置")),
          row("价格提醒", neutralPill(zhBool(ai.price_alerts_enable))),
          row("AI 问答接口", neutralPill(zhBool(ai.provider_enable))),
          row("AI API Key", neutralPill(configuredText(ai.api_key_configured)))
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
    function healthPanel(items) {
      const list = (items || []).map(item => {
        const ok = item.status === "ok";
        const warn = item.status === "warn";
        const pill = warn ? `<span class="status">${escapeHtml(item.value || "需关注")}</span>` : statusPill(item.value || item.status, ok);
        return `<div class="feature-item">
          <strong>${escapeHtml(item.label || "检查项")}</strong>
          ${pill}
          <div class="hint">${escapeHtml(item.detail || "")}</div>
        </div>`;
      }).join("");
      return `<div class="panel span-12">
        <h3 class="section-title">运行健康度</h3>
        <div class="api-grid">${list || emptyState("暂无健康度数据", "后台还没有生成健康检查结果。可以稍后刷新，或到检查测试页执行 Web API 自诊断。")}</div>
      </div>`;
    }
    function logTargetForSource(source) {
      const text = String(source || "").toLowerCase();
      if (text.includes("结构")) return "structure";
      if (text.includes("web")) return "web";
      if (text.includes("ai")) return "ai";
      return "main";
    }
    function recentErrorsPanel(errors) {
      const list = (errors || []).map(item => `<div class="feature-item">
        <strong>${escapeHtml(item.source || "来源未知")} · ${escapeHtml(item.level || "错误")}</strong>
        <span class="muted">${escapeHtml(item.message || "")}</span>
        <div class="toolbar" style="margin:8px 0 0">
          <button class="btn" type="button" onclick="openLogsForError('${escapeHtml(logTargetForSource(item.source))}')">查看相关日志</button>
        </div>
      </div>`).join("");
      return `<div class="panel span-12">
        <h3 class="section-title">最近错误 / 警告</h3>
        ${list ? `<div class="feature-list">${list}</div>` : emptyState("当前没有检测到 runtime-status 里的错误或接口警告。", "如果仍怀疑异常，可以去日志中心按“只看错误”筛选，或打开诊断报告复制快照。")}
      </div>`;
    }
    function openLogsForError(target) {
      switchView("logs");
      setTimeout(() => {
        const targetEl = document.getElementById("logTarget");
        const filterEl = document.getElementById("logFilter");
        if (targetEl) targetEl.value = target || "main";
        if (filterEl) filterEl.value = "error";
        loadLogs();
      }, 0);
    }
    function rawDetails(title, data) {
      return `<details class="panel raw-details">
        <summary>${escapeHtml(title)}</summary>
        <div class="raw-body"><pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre></div>
      </details>`;
    }
    function serviceActionLabel(action) {
      const map = { restart: "重启", start: "启动", stop: "停止" };
      return map[action] || zhStatus(action || "执行");
    }
    function firstUsefulLine(text) {
      return String(text || "").split(/\r?\n/).map(line => line.trim()).filter(Boolean)[0] || "";
    }
    function commandResultSummary(data, title, kind = "action") {
      const ok = Boolean(data && data.ok);
      const lines = [];
      const label = title || data.label || data.service || "操作";
      lines.push(ok ? `${label} 已完成。` : `${label} 没有成功完成。`);
      if (data.message) lines.push(data.message);
      if (kind === "service") {
        const action = serviceActionLabel(data.action || "");
        const service = data.service || "后台服务";
        lines.push(`目标服务：${service}，动作：${action}。`);
      }
      if (data.command) lines.push(`执行命令：${data.command}`);
      if (data.returncode !== undefined) lines.push(`返回码：${data.returncode}`);
      const stdoutLine = firstUsefulLine(data.stdout);
      const stderrLine = firstUsefulLine(data.stderr);
      const metaLine = apiMetaLine(data);
      if (metaLine) lines.push(metaLine);
      if (Array.isArray(data.checks)) {
        data.checks.forEach(check => {
          const state = check.ok ? "正常" : "异常";
          const elapsed = check.elapsed_ms !== undefined ? `，耗时 ${check.elapsed_ms}ms` : "";
          const status = check.status ? `，HTTP ${check.status}` : "";
          lines.push(`${check.name}：${state}${status}${elapsed}`);
        });
      }
      if (stdoutLine) lines.push(`输出摘要：${stdoutLine}`);
      if (stderrLine) lines.push(`错误摘要：${stderrLine}`);
      if (!ok) lines.push("建议下一步：去日志中心按“只看错误”筛选，或在雷达服务页重启对应服务后再看总览。");
      if (ok && kind === "service") lines.push("建议下一步：回到总览确认服务状态和 runtime-status 是否继续更新。");
      if (ok && kind === "action") lines.push("建议下一步：如果这是测试或诊断动作，可以展开原始详情查看完整输出。");
      if (ok && kind === "price") lines.push("建议下一步：查看提醒列表里的状态和条件，确认是否符合预期。");
      return lines;
    }
    function renderOperationResult(targetId, data, title, kind = "action") {
      const target = document.getElementById(targetId);
      if (!target) return;
      const ok = Boolean(data && data.ok);
      const summary = commandResultSummary(data || {}, title, kind);
      target.innerHTML = `<div class="result-panel">
        <div class="result-title">
          <span>${escapeHtml(title || "操作结果")}</span>
          ${statusPill(ok ? "已完成" : "异常", ok)}
        </div>
        <ul class="result-list">${summary.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        ${rawDetails("高级详情：原始执行结果 JSON", data || {})}
      </div>`;
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
      setSubtitle(`更新时间 ${data.updated_at}${autoRefreshEnabled && currentView === "overview" ? " · 自动刷新中" : ""}`);
      const main = data.services.main || {};
      const structure = data.services.structure || {};
      const web = data.services.web || {};
      const ai = data.services.ai || {};
      const git = data.git || {};
      const runtime = data.runtime || {};
      const cfg = data.config || {};
      document.getElementById("overviewGrid").innerHTML = [
        renderPageIntro("overview", [data.updated_at ? `更新 ${data.updated_at}` : "", git.version || "unknown"]),
        healthPanel(data.health || []),
        recentErrorsPanel(data.recent_errors || []),
        serviceCard("主服务", main),
        serviceCard("结构雷达", structure),
        serviceCard("Web 控制台", web),
        serviceCard("AI 助手", ai),
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
      setSubtitle(`日志来源 ${data.source || ""}${autoRefreshEnabled && currentView === "logs" ? " · 自动刷新中" : ""}`);
      latestLogData = data;
      const intro = document.getElementById("logsIntro");
      if (intro) intro.innerHTML = renderPageIntro("logs", [data.source || "", `${lines} 行`]);
      renderFilteredLogs();
    }
    function renderFilteredLogs() {
      const raw = (latestLogData && latestLogData.text) || "";
      const search = String(document.getElementById("logSearch")?.value || "").trim().toLowerCase();
      const filter = String(document.getElementById("logFilter")?.value || "all").toLowerCase();
      const patterns = {
        error: /(error|traceback|exception|failed|失败|异常|错误)/i,
        telegram: /(telegram|bot|chat|topic|message_thread_id)/i,
        binance: /(binance|fapi|api\.binance|ticker|klines|openinterest)/i,
        structure: /(structure|结构|breakout|breakdown|squeeze)/i,
        ai: /(ai|assistant|deepseek|price_alert|alert|提醒|意图|intent|callback)/i,
        funding: /(funding|fundingrate|资金费率|费率|premium|gate|bybit|okx|bitget)/i
      };
      const lines = raw.split(/\r?\n/).filter(line => {
        const lower = line.toLowerCase();
        if (filter !== "all" && patterns[filter] && !patterns[filter].test(line)) return false;
        if (search && !lower.includes(search)) return false;
        return true;
      });
      const header = latestLogData ? `来源: ${latestLogData.source || ""}\n筛选后: ${lines.length} 行\n\n` : "";
      document.getElementById("logOutput").textContent = header + (lines.join("\n") || "没有匹配的日志");
      renderLogInsight(lines, raw, filter, search, patterns);
    }
    function renderLogInsight(lines, raw, filter, search, patterns) {
      const box = document.getElementById("logInsight");
      if (!box) return;
      const rawLines = raw ? raw.split(/\r?\n/).filter(Boolean) : [];
      const errorPattern = patterns.error;
      const errorLines = lines.filter(line => errorPattern.test(line));
      const firstError = errorLines[0] || "";
      const filterText = {
        all: "全部日志",
        error: "只看错误",
        telegram: "Telegram",
        binance: "Binance",
        structure: "结构雷达",
        ai: "AI 助手",
        funding: "资金费率"
      }[filter] || filter;
      box.innerHTML = `<div class="summary-head">
          <h3 class="section-title">日志筛选摘要</h3>
          ${neutralPill(`${lines.length}/${rawLines.length} 行`)}
        </div>
        <div class="readable-list">
          ${row("当前筛选", textValue(search ? `${filterText} + 关键词：${search}` : filterText))}
          ${row("错误命中", errorLines.length ? statusPill(`${errorLines.length} 条`, false) : neutralPill("未发现"))}
          ${row("第一条错误", firstError ? `<code>${escapeHtml(firstError.slice(0, 260))}</code>` : neutralPill("暂无"))}
        </div>`;
    }
    function copyLogs() {
      copyTextToClipboard(document.getElementById("logOutput").textContent || "").then(ok => {
        setSubtitle(ok ? "日志已复制" : "浏览器拒绝自动复制，已选中日志文本，请按 Ctrl+C");
        if (!ok) selectElementText("logOutput");
      });
    }
    function auditResultText(ok) {
      return ok ? "成功" : "失败";
    }
    function renderAuditRows(records) {
      if (!records.length) return tableEmpty(8, "没有匹配的审计记录", "可以调整成功/失败筛选、关键词或记录数量；如果刚部署，后台还没有产生可审计操作。");
      return records.map(item => `
        <tr>
          <td>${escapeHtml(item.ts || "")}</td>
          <td>${escapeHtml(item.action || "")}</td>
          <td>${escapeHtml(item.target || "")}</td>
          <td>${statusPill(auditResultText(Boolean(item.ok)), Boolean(item.ok))}</td>
          <td>${escapeHtml(String(item.status || ""))}</td>
          <td>${escapeHtml(String(item.duration_ms ?? ""))}ms</td>
          <td>${escapeHtml(item.message || item.error || "")}</td>
          <td>${escapeHtml(item.path || "")}</td>
        </tr>
      `).join("");
    }
    async function loadAudit() {
      const result = document.getElementById("auditResult")?.value || "all";
      const limit = document.getElementById("auditLimit")?.value || "200";
      const search = document.getElementById("auditSearch")?.value || "";
      const data = await api(`/api/audit?result=${encodeURIComponent(result)}&limit=${encodeURIComponent(limit)}&search=${encodeURIComponent(search)}`);
      latestAuditData = data;
      setSubtitle(`审计记录 ${data.matched || 0}/${data.total || 0} 条`);
      document.getElementById("auditGrid").innerHTML = `
        ${renderPageIntro("audit", [`匹配 ${data.matched || 0}/${data.total || 0}`, result === "failed" ? "只看失败" : "全部操作"])}
        <div class="panel span-12">
          <div class="summary-head">
            <h3 class="section-title">操作审计</h3>
            ${neutralPill(`${data.matched || 0}/${data.total || 0} 条`)}
          </div>
          <div class="toolbar">
            <select id="auditResult" onchange="loadAudit()">
              <option value="all" ${result === "all" ? "selected" : ""}>全部结果</option>
              <option value="ok" ${result === "ok" ? "selected" : ""}>只看成功</option>
              <option value="failed" ${result === "failed" ? "selected" : ""}>只看失败</option>
            </select>
            <select id="auditLimit" onchange="loadAudit()">
              <option value="100" ${limit === "100" ? "selected" : ""}>最近 100 条</option>
              <option value="200" ${limit === "200" ? "selected" : ""}>最近 200 条</option>
              <option value="500" ${limit === "500" ? "selected" : ""}>最近 500 条</option>
              <option value="1000" ${limit === "1000" ? "selected" : ""}>最近 1000 条</option>
            </select>
            <input id="auditSearch" value="${escapeHtml(search)}" placeholder="搜索动作、对象、错误、接口" onkeydown="if(event.key==='Enter') loadAudit()">
            <button class="btn primary" onclick="loadAudit()">刷新审计</button>
            <button class="btn" onclick="clearAuditFilters()">清空筛选</button>
          </div>
          <div class="hint" style="margin-bottom:8px">失败记录优先看“消息/错误摘要”和“接口”，再去日志中心按同一时间点排查。</div>
          <table class="table">
            <thead><tr><th>时间</th><th>动作</th><th>对象</th><th>结果</th><th>HTTP</th><th>耗时</th><th>消息 / 错误摘要</th><th>接口</th></tr></thead>
            <tbody>${renderAuditRows(data.records || [])}</tbody>
          </table>
          ${rawDetails("高级排查：原始审计 JSON", data)}
        </div>
      `;
    }
    function clearAuditFilters() {
      const result = document.getElementById("auditResult");
      const limit = document.getElementById("auditLimit");
      const search = document.getElementById("auditSearch");
      if (result) result.value = "all";
      if (limit) limit.value = "200";
      if (search) search.value = "";
      loadAudit();
    }
    function countLogErrors(logErrors) {
      return Object.values(logErrors || {}).reduce((total, item) => total + Number((item && item.error_count) || 0), 0);
    }
    function countTransientLogs(logErrors) {
      return Object.values(logErrors || {}).reduce((total, item) => total + Number((item && item.transient_count) || 0), 0);
    }
    function reportText(data) {
      const lines = [];
      const git = data.git || {};
      lines.push(`泡泡雷达诊断报告`);
      lines.push(`生成时间: ${data.generated_at || ""}`);
      lines.push(`版本: ${git.version || ""} ${git.branch || ""} ${git.commit || ""}`);
      lines.push("");
      const releaseReadiness = data.release_readiness || {};
      const releaseChecks = releaseReadiness.checks || [];
      const releaseTrend = data.release_trend || {};
      lines.push("长期运行就绪度:");
      lines.push(`- 状态: ${releaseReadiness.label || ""} ${releaseReadiness.summary || ""}`);
      lines.push(`- 评分: ${releaseReadiness.score ?? ""}`);
      lines.push(`- 计数: 通过 ${releaseReadiness.ok_count || 0} | 警告 ${releaseReadiness.warn_count || 0} | 阻断 ${releaseReadiness.fail_count || 0}`);
      lines.push(`- 下一目标: ${releaseReadiness.next_version_goal || ""}`);
      releaseChecks.forEach(item => lines.push(`- ${item.label || ""}: ${item.status || ""} ${item.detail || ""}${item.action ? ` | 建议: ${item.action}` : ""}`));
      lines.push("");
      lines.push("长期运行趋势:");
      lines.push(`- 状态: ${releaseTrend.label || ""} ${releaseTrend.summary || ""}`);
      lines.push(`- 分数: 当前 ${releaseTrend.current_score ?? "未记录"} | 上次 ${releaseTrend.previous_score ?? "未记录"} | 变化 ${releaseTrend.score_delta ?? "未记录"}`);
      lines.push(`- 建议: ${releaseTrend.action || ""}`);
      lines.push("");
      const problemCenter = data.problem_center || {};
      const problemCounts = problemCenter.counts || {};
      lines.push("问题中心总览:");
      lines.push(`- 状态: ${problemCenter.label || ""} ${problemCenter.summary || ""}`);
      lines.push(`- 主要动作: ${problemCenter.primary_action || ""}`);
      lines.push(`- 统计: 严重 ${problemCounts.critical || 0} | 警告 ${problemCounts.warning || 0} | 日志错误 ${problemCounts.log_errors || 0} | 网络超时 ${problemCounts.transient_timeouts || 0} | 失败审计 ${problemCounts.failed_audit || 0}`);
      ((problemCenter && problemCenter.next_steps) || []).forEach(item => lines.push(`- 下一步: ${item}`));
      const actionPlan = problemCenter.action_plan || [];
      if (actionPlan.length) {
        lines.push("处理清单:");
        actionPlan.forEach((item, idx) => lines.push(`- ${idx + 1}. ${item.title || ""}: ${item.detail || ""} | 入口: ${item.button || ""}`));
      }
      lines.push("");
      const stability = data.stability || {};
      lines.push("稳定版自检:");
      lines.push(`- 状态: ${stability.label || ""} ${stability.summary || ""}`);
      ((stability && stability.checks) || []).forEach(item => lines.push(`- ${item.label || ""}: ${item.status || ""} ${item.detail || ""}${item.action ? ` | 建议: ${item.action}` : ""}`));
      const stabilityHistory = data.stability_history || {};
      const latestStable = stabilityHistory.latest || null;
      if (latestStable) lines.push(`- 最近保存: ${latestStable.ts || ""} ${latestStable.label || ""} ${latestStable.version || ""} ${latestStable.commit || ""}`);
      ((stabilityHistory && stabilityHistory.records) || []).slice(0, 5).forEach(item => lines.push(`- 历史: ${item.ts || ""} ${item.label || ""} ${item.version || ""} ${item.commit || ""}`));
      lines.push("");
      lines.push("问题中心:");
      const issues = data.issues || [];
      if (!issues.length) lines.push("- 暂无明确问题");
      issues.forEach(item => lines.push(`- [${item.severity || ""}] ${item.module || ""} · ${item.title || ""} x${item.count || 1}: ${item.detail || ""} | 建议: ${item.action || ""}`));
      lines.push("");
      lines.push("建议动作:");
      (data.recommendations || []).forEach(item => lines.push(`- ${item}`));
      lines.push("");
      lines.push("健康检查:");
      (data.health || []).forEach(item => lines.push(`- ${item.label}: ${item.value} (${item.status}) ${item.detail || ""}`));
      lines.push("");
      lines.push("最近错误:");
      const recentErrors = data.recent_errors || [];
      if (!recentErrors.length) lines.push("- 暂无");
      recentErrors.forEach(item => lines.push(`- ${item.source} · ${item.level}: ${item.message}`));
      lines.push("");
      lines.push("失败审计:");
      const failed = ((data.audit || {}).failed_recent || []);
      if (!failed.length) lines.push("- 暂无");
      failed.forEach(item => lines.push(`- ${item.ts} ${item.action} ${item.target} ${item.error || item.message || ""}`));
      lines.push("");
      lines.push("日志错误片段:");
      Object.entries(data.log_errors || {}).forEach(([target, item]) => {
        lines.push(`- ${target}: ${(item && item.error_count) || 0} 条`);
        ((item && item.lines) || []).slice(-5).forEach(line => lines.push(`  ${line}`));
      });
      lines.push("");
      lines.push("网络超时/可自动重试:");
      Object.entries(data.log_errors || {}).forEach(([target, item]) => {
        lines.push(`- ${target}: ${(item && item.transient_count) || 0} 条`);
        ((item && item.transient_lines) || []).slice(-5).forEach(line => lines.push(`  ${line}`));
      });
      return lines.join("\n");
    }
    function selectElementText(id) {
      const element = document.getElementById(id);
      if (!element) return;
      const range = document.createRange();
      range.selectNodeContents(element);
      const selection = window.getSelection();
      if (!selection) return;
      selection.removeAllRanges();
      selection.addRange(range);
    }
    async function copyTextToClipboard(text) {
      const value = String(text || "");
      if (navigator.clipboard && window.isSecureContext) {
        try {
          await navigator.clipboard.writeText(value);
          return true;
        } catch (err) {
          console.warn("clipboard api failed", err);
        }
      }
      const textarea = document.createElement("textarea");
      textarea.value = value;
      textarea.setAttribute("readonly", "readonly");
      textarea.style.position = "fixed";
      textarea.style.left = "-9999px";
      textarea.style.top = "0";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (err) {
        console.warn("fallback copy failed", err);
      }
      document.body.removeChild(textarea);
      return ok;
    }
    function logErrorPanels(logErrors) {
      return Object.entries(logErrors || {}).map(([target, item]) => `
        <div class="panel span-6">
          <div class="summary-head">
            <h3 class="section-title">${escapeHtml(target)} 日志错误</h3>
            ${Number((item && item.error_count) || 0) ? statusPill(`${item.error_count} 条`, false) : neutralPill("未发现")}
          </div>
          <div class="summary-meta">${escapeHtml((item && item.source) || "")}</div>
          <pre class="output" style="max-height:220px">${escapeHtml(((item && item.lines) || []).join("\n") || "没有匹配的错误片段")}</pre>
          ${Number((item && item.transient_count) || 0) ? `<div class="hint" style="margin-top:8px">另有 ${escapeHtml(String(item.transient_count))} 条 Telegram 网络超时，通常会自动重试。</div>` : ""}
        </div>
      `).join("");
    }
    function issueSeverityPill(severity) {
      const value = String(severity || "");
      if (value === "critical") return statusPill("严重", false);
      if (value === "warning") return `<span class="status" style="color:var(--warn);background:linear-gradient(135deg,#fff3d7,#fffaf0)">警告</span>`;
      return neutralPill("提示");
    }
    function issueActionButton(issue) {
      const target = String(issue.target || "");
      if (target === "audit") return `<button class="btn" type="button" onclick="switchView('audit')">查看失败审计</button>`;
      if (["main", "structure", "web", "ai"].includes(target)) return `<button class="btn" type="button" onclick="openLogsForError('${escapeHtml(target)}')">查看相关日志</button>`;
      return `<button class="btn" type="button" onclick="switchView('logs')">打开日志中心</button>`;
    }
    function stabilityStatusPill(status) {
      const value = String(status || "");
      if (value === "ready" || value === "ok") return statusPill("达标", true);
      if (value === "attention" || value === "warn") return `<span class="status" style="color:var(--warn);background:linear-gradient(135deg,#fff3d7,#fffaf0)">关注</span>`;
      if (value === "blocked" || value === "fail") return statusPill("未达标", false);
      return neutralPill(value || "未知");
    }
    function releaseReadinessStatusPill(status) {
      const value = String(status || "");
      if (value === "complete_candidate") return statusPill("完整稳定版候选", true);
      if (value === "candidate") return `<span class="status" style="color:var(--warn);background:linear-gradient(135deg,#fff3d7,#fffaf0)">准稳定候选</span>`;
      if (value === "blocked") return statusPill("需要处理", false);
      return neutralPill(value || "未知");
    }
    function releaseTrendStatusPill(status) {
      const value = String(status || "");
      if (value === "improved") return statusPill("趋势变好", true);
      if (value === "stable") return neutralPill("趋势持平");
      if (value === "worse") return `<span class="status" style="color:var(--warn);background:linear-gradient(135deg,#fff3d7,#fffaf0)">趋势变差</span>`;
      if (value === "regressed") return statusPill("发生回退", false);
      if (value === "single") return neutralPill("等待对比");
      if (value === "empty") return neutralPill("暂无趋势");
      return neutralPill(value || "未知");
    }
    function scoreText(value) {
      return value === null || value === undefined ? "未记录" : String(value);
    }
    function releaseTrendPanel(trend) {
      const data = trend || {};
      return `
        <div class="panel span-12">
          <div class="summary-head">
            <div>
              <h3 class="section-title">长期运行趋势</h3>
              <div class="summary-meta">${escapeHtml(data.summary || "对比最近两次长期运行就绪度历史，判断分数和候选状态有没有回退。")}</div>
            </div>
            <div class="issue-meta">${releaseTrendStatusPill(data.status)}</div>
          </div>
          <div class="mini-metrics">
            <div class="mini-metric"><div class="label">当前分数</div><div class="value">${neutralPill(scoreText(data.current_score))}</div><div class="muted">${escapeHtml(data.current_label || data.current_status || "最新记录")}</div></div>
            <div class="mini-metric"><div class="label">上次分数</div><div class="value">${neutralPill(scoreText(data.previous_score))}</div><div class="muted">${escapeHtml(data.previous_label || data.previous_status || "上一条记录")}</div></div>
            <div class="mini-metric"><div class="label">分数变化</div><div class="value">${Number(data.score_delta || 0) < 0 ? statusPill(String(data.score_delta), false) : neutralPill(scoreText(data.score_delta))}</div><div class="muted">当前分数减上次分数</div></div>
            <div class="mini-metric"><div class="label">趋势状态</div><div class="value">${releaseTrendStatusPill(data.status)}</div><div class="muted">${escapeHtml(data.current_ts || "")}</div></div>
          </div>
          <div class="readable-list" style="margin-top:10px">
            ${row("建议动作", textValue(data.action || "暂无"))}
            ${row("对比时间", textValue(`${data.current_ts || "当前未记录"} / ${data.previous_ts || "上次未记录"}`))}
          </div>
        </div>
      `;
    }
    function releaseReadinessPanel(readiness) {
      const data = readiness || {};
      const checks = data.checks || [];
      const requirements = data.requirements || [];
      const score = Number(data.score || 0);
      const checkRows = checks.length ? checks.map(item => `
        <tr>
          <td>${escapeHtml(item.label || "")}</td>
          <td>${stabilityStatusPill(item.status)}</td>
          <td>${escapeHtml(item.detail || "")}</td>
          <td>${escapeHtml(item.action || "无需处理")}</td>
        </tr>
      `).join("") : tableEmpty(4, "暂无长期运行就绪度", "刷新诊断报告后会生成完整候选评估。");
      return `
        <div class="panel span-12">
          <div class="summary-head">
            <div>
              <h3 class="section-title">长期运行就绪度</h3>
              <div class="summary-meta">${escapeHtml(data.summary || "把稳定版验收、问题中心、日志、审计和验收历史合成一个长期运行结论。")}</div>
            </div>
            <div class="issue-meta">
              ${releaseReadinessStatusPill(data.status)}
              ${neutralPill(`评分 ${score}/100`)}
              ${Number(data.fail_count || 0) ? statusPill(`${data.fail_count} 阻断`, false) : ""}
              ${Number(data.warn_count || 0) ? neutralPill(`${data.warn_count} 警告`) : ""}
            </div>
          </div>
          <div class="mini-metrics">
            <div class="mini-metric"><div class="label">就绪评分</div><div class="value">${neutralPill(`${score}/100`)}</div><div class="muted">阻断扣 25，警告扣 8</div></div>
            <div class="mini-metric"><div class="label">通过项</div><div class="value">${neutralPill(String(data.ok_count || 0))}</div><div class="muted">当前满足的门槛</div></div>
            <div class="mini-metric"><div class="label">警告项</div><div class="value">${Number(data.warn_count || 0) ? neutralPill(String(data.warn_count)) : neutralPill("0")}</div><div class="muted">不一定阻断，但建议确认</div></div>
            <div class="mini-metric"><div class="label">阻断项</div><div class="value">${Number(data.fail_count || 0) ? statusPill(String(data.fail_count), false) : neutralPill("0")}</div><div class="muted">需要先处理</div></div>
          </div>
          <div class="readable-list" style="margin-top:10px">
            ${row("下一版本目标", textValue(data.next_version_goal || "暂无"))}
            ${row("完整候选门槛", textValue(requirements.join("；") || "暂无"))}
          </div>
          <table class="table" style="margin-top:10px">
            <thead><tr><th>检查项</th><th>状态</th><th>当前情况</th><th>建议动作</th></tr></thead>
            <tbody>${checkRows}</tbody>
          </table>
        </div>
      `;
    }
    function stabilityCards(stability) {
      const checks = (stability && stability.checks) || [];
      if (!checks.length) return emptyState("暂无稳定版自检结果", "刷新诊断报告后会生成服务、配置、日志和问题中心自检。");
      return `<div class="issue-list">${checks.map(item => {
        const severity = item.status === "fail" ? "critical" : (item.status === "warn" ? "warning" : "notice");
        return `
          <div class="issue-card ${severity}">
            <div class="issue-head">
              <div>
                <div class="issue-title">${escapeHtml(item.label || "检查项")}</div>
                <div class="issue-detail">${escapeHtml(item.detail || "")}</div>
              </div>
              <div class="issue-meta">${stabilityStatusPill(item.status)}</div>
            </div>
            ${item.action ? `<div class="issue-action"><span>${escapeHtml(item.action)}</span></div>` : ""}
          </div>
        `;
      }).join("")}</div>`;
    }
    function stabilityHistoryPanel(history) {
      const records = (history && history.records) || [];
      const latest = (history && history.latest) || null;
      const rows = records.length ? records.map(item => `
        <tr>
          <td>${escapeHtml(item.ts || item.generated_at || "")}</td>
          <td>${stabilityStatusPill(item.status)}</td>
          <td>${item.release_status && item.release_status !== "unknown" ? releaseReadinessStatusPill(item.release_status) : neutralPill("未记录")}</td>
          <td>${item.release_score === null || item.release_score === undefined ? escapeHtml("未记录") : escapeHtml(`${item.release_score}/100`)}</td>
          <td>${escapeHtml(item.version || "")} ${escapeHtml(item.commit || "")}</td>
          <td>${escapeHtml(item.summary || "")}</td>
        </tr>
      `).join("") : tableEmpty(6, "暂无历史验收记录", "执行 paopao update --yes 或 python main.py stable-check 后会自动保存。");
      return `
        <div class="panel span-12">
          <div class="summary-head">
            <div>
              <h3 class="section-title">验收历史</h3>
              <div class="summary-meta">${latest ? `最近保存：${escapeHtml(latest.ts || latest.generated_at || "")} · ${escapeHtml(latest.label || "")} · ${escapeHtml(latest.release_label || "长期就绪度未记录")}${latest.release_score === null || latest.release_score === undefined ? "" : ` ${escapeHtml(String(latest.release_score))}/100`}` : "还没有保存过稳定版验收结果"}</div>
            </div>
            ${neutralPill(`${Number((history && history.count) || 0)} 条`)}
          </div>
          <table class="table">
            <thead><tr><th>时间</th><th>稳定版状态</th><th>长期就绪度</th><th>评分</th><th>版本</th><th>摘要</th></tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>
      `;
    }
    function problemCenterStatusPill(status) {
      const value = String(status || "");
      if (value === "ok") return statusPill("当前健康", true);
      if (value === "attention") return `<span class="status" style="color:var(--warn);background:linear-gradient(135deg,#fff3d7,#fffaf0)">需要关注</span>`;
      if (value === "blocked") return statusPill("优先处理", false);
      return neutralPill(value || "未知");
    }
    function problemModuleRows(modules) {
      const rows = (modules || []).map(item => `
        <tr>
          <td>${escapeHtml(item.module || "")}</td>
          <td>${issueSeverityPill(item.severity)}</td>
          <td>${escapeHtml(String(item.count || 0))}</td>
          <td>${escapeHtml(item.reason || "")}</td>
          <td>${item.target ? issueActionButton(item) : `<button class="btn" type="button" onclick="switchView('logs')">打开日志中心</button>`}</td>
        </tr>
      `).join("");
      return rows || tableEmpty(5, "暂无异常模块", "当前没有需要单独处理的模块。");
    }
    function actionPlanButton(action) {
      const target = String((action && action.target) || "");
      const logTarget = String((action && action.log_target) || "");
      const allowedViews = ["overview", "ai", "price", "services", "config", "logs", "audit", "report", "actions", "preview", "guide"];
      if (target === "logs" && ["main", "structure", "web", "ai", "funding"].includes(logTarget)) {
        return `<button class="btn primary" type="button" onclick="openLogsForError('${escapeHtml(logTarget)}')">${escapeHtml(action.button || "查看日志")}</button>`;
      }
      if (allowedViews.includes(target)) {
        return `<button class="btn primary" type="button" onclick="switchView('${escapeHtml(target)}')">${escapeHtml(action.button || "打开页面")}</button>`;
      }
      return `<button class="btn" type="button" onclick="switchView('report')">留在诊断报告</button>`;
    }
    function actionPlanCards(actions) {
      const items = actions || [];
      if (!items.length) {
        return emptyState("暂无处理清单", "当前没有需要立即处理的动作。");
      }
      return `<div class="issue-list">${items.map((item, idx) => `
        <div class="issue-card ${escapeHtml(item.severity || "notice")}">
          <div class="issue-head">
            <div>
              <div class="issue-title">${idx + 1}. ${escapeHtml(item.title || "处理动作")}</div>
              <div class="issue-detail">${escapeHtml(item.detail || "")}</div>
            </div>
            <div class="issue-meta">${issueSeverityPill(item.severity)}</div>
          </div>
          <div class="issue-action">
            <span>${escapeHtml(item.button || "打开相关页面")}</span>
            ${actionPlanButton(item)}
          </div>
        </div>
      `).join("")}</div>`;
    }
    function problemCenterPanel(problemCenter) {
      const data = problemCenter || {};
      const counts = data.counts || {};
      const modules = data.modules || [];
      const nextSteps = data.next_steps || [];
      const actionPlan = data.action_plan || [];
      return `
        <div class="panel span-12">
          <div class="summary-head">
            <div>
              <h3 class="section-title">问题中心总览</h3>
              <div class="summary-meta">${escapeHtml(data.summary || "聚合稳定版验收、健康检查、日志错误、网络超时和失败审计。")}</div>
            </div>
            <div class="issue-meta">
              ${problemCenterStatusPill(data.status)}
              ${Number(counts.critical || 0) ? statusPill(`${counts.critical} 严重`, false) : neutralPill("无严重")}
              ${Number(counts.warning || 0) ? neutralPill(`${counts.warning} 警告`) : ""}
              ${Number(counts.log_errors || 0) ? statusPill(`${counts.log_errors} 日志错误`, false) : ""}
              ${Number(counts.transient_timeouts || 0) ? neutralPill(`${counts.transient_timeouts} 网络超时`) : ""}
            </div>
          </div>
          <div class="hint" style="margin:10px 0">${escapeHtml(data.primary_action || "暂无需要立即处理的动作。")}</div>
          <div class="mini-metrics">
            <div class="mini-metric"><div class="label">健康异常</div><div class="value">${Number(counts.bad_health || 0) ? statusPill(`${counts.bad_health} 项`, false) : neutralPill("暂无")}</div><div class="muted">健康检查 bad</div></div>
            <div class="mini-metric"><div class="label">最近错误</div><div class="value">${Number(counts.recent_errors || 0) ? statusPill(`${counts.recent_errors} 条`, false) : neutralPill("暂无")}</div><div class="muted">runtime 记录</div></div>
            <div class="mini-metric"><div class="label">失败审计</div><div class="value">${Number(counts.failed_audit || 0) ? statusPill(`${counts.failed_audit} 条`, false) : neutralPill("暂无")}</div><div class="muted">Web 操作失败</div></div>
            <div class="mini-metric"><div class="label">验收门禁</div><div class="value">${Number(counts.stability_fail || 0) ? statusPill(`${counts.stability_fail} 阻断`, false) : (Number(counts.stability_warn || 0) ? neutralPill(`${counts.stability_warn} 警告`) : neutralPill("通过"))}</div><div class="muted">stable-check</div></div>
          </div>
          <div class="readable-list" style="margin-top:10px">
            ${nextSteps.length ? nextSteps.map((item, idx) => row(`下一步 ${idx + 1}`, textValue(item))).join("") : row("下一步", neutralPill("暂无"))}
          </div>
          <div style="margin-top:10px">
            <h3 class="section-title">处理清单</h3>
            ${actionPlanCards(actionPlan)}
          </div>
          <table class="table" style="margin-top:10px">
            <thead><tr><th>模块</th><th>级别</th><th>次数</th><th>原因</th><th>入口</th></tr></thead>
            <tbody>${problemModuleRows(modules)}</tbody>
          </table>
        </div>
      `;
    }
    function issueCards(issues) {
      if (!issues.length) {
        return emptyState("当前没有明确问题", "健康检查、失败审计、日志错误和网络超时都没有达到需要优先处理的程度。");
      }
      return `<div class="issue-list">${issues.map(issue => `
        <div class="issue-card ${escapeHtml(issue.severity || "notice")}">
          <div class="issue-head">
            <div>
              <div class="issue-title">${escapeHtml(issue.title || "未命名问题")}</div>
              <div class="issue-detail">${escapeHtml(issue.detail || "")}</div>
            </div>
            <div class="issue-meta">${issueSeverityPill(issue.severity)}${neutralPill(issue.module || "未知模块")}${neutralPill(`${issue.count || 1} 次`)}</div>
          </div>
          <div class="issue-action">
            <span>${escapeHtml(issue.action || "查看相关日志和诊断详情。")}</span>
            ${issueActionButton(issue)}
          </div>
        </div>
      `).join("")}</div>`;
    }
    function reportAuditRows(records) {
      if (!records.length) return tableEmpty(5, "暂无失败审计", "最近没有失败的 Web 后台操作。配置保存、服务控制和检查测试如果失败，会出现在这里。");
      return records.map(item => `
        <tr>
          <td>${escapeHtml(item.ts || "")}</td>
          <td>${escapeHtml(item.action || "")}</td>
          <td>${escapeHtml(item.target || "")}</td>
          <td>${escapeHtml(String(item.duration_ms ?? ""))}ms</td>
          <td>${escapeHtml(item.error || item.message || "")}</td>
        </tr>
      `).join("");
    }
    async function loadReport() {
      const data = await api("/api/ops-snapshot");
      latestReportData = data;
      const health = data.health || [];
      const recentErrors = data.recent_errors || [];
      const audit = data.audit || {};
      const failedAudit = audit.failed_recent || [];
      const issues = data.issues || [];
      const stability = data.stability || {};
      const stabilityHistory = data.stability_history || {};
      const problemCenter = data.problem_center || {};
      const releaseReadiness = data.release_readiness || {};
      const releaseTrend = data.release_trend || {};
      const logErrorTotal = countLogErrors(data.log_errors || {});
      const transientTotal = countTransientLogs(data.log_errors || {});
      setSubtitle(`诊断报告 ${data.generated_at || ""}`);
      document.getElementById("reportGrid").innerHTML = `
        ${renderPageIntro("report", [data.generated_at || "", releaseReadiness.label || problemCenter.label || stability.label || "稳定版自检", releaseReadiness.score !== undefined ? `就绪度 ${releaseReadiness.score}/100` : "就绪度未生成", releaseTrend.label || "趋势未生成", issues.length ? `${issues.length} 个问题` : "无明确问题", logErrorTotal ? `${logErrorTotal} 条日志错误` : "无日志错误", transientTotal ? `${transientTotal} 条网络超时` : "无网络超时"])}
        ${releaseReadinessPanel(releaseReadiness)}
        ${releaseTrendPanel(releaseTrend)}
        ${problemCenterPanel(problemCenter)}
        <div class="panel span-12">
          <div class="summary-head">
            <div>
              <h3 class="section-title">稳定版自检</h3>
              <div class="summary-meta">${escapeHtml(stability.summary || "检查核心服务、配置、问题中心、日志和后台操作。")}</div>
            </div>
            <div class="issue-meta">${stabilityStatusPill(stability.status)}${neutralPill(`${Number(stability.ok_count || 0)} 通过`)}${Number(stability.warn_count || 0) ? neutralPill(`${stability.warn_count} 警告`) : ""}${Number(stability.fail_count || 0) ? statusPill(`${stability.fail_count} 阻断`, false) : ""}</div>
          </div>
          ${stabilityCards(stability)}
        </div>
        ${stabilityHistoryPanel(stabilityHistory)}
        <div class="panel span-3 metric"><div class="label">健康项</div><div class="value">${neutralPill(String(health.length))}</div><div class="muted">含服务和配置门禁</div></div>
        <div class="panel span-3 metric"><div class="label">问题中心</div><div class="value">${issues.length ? statusPill(`${issues.length} 个`, !issues.some(item => item.severity !== "notice")) : neutralPill("暂无")}</div><div class="muted">按严重程度排序</div></div>
        <div class="panel span-3 metric"><div class="label">最近错误</div><div class="value">${recentErrors.length ? statusPill(`${recentErrors.length} 条`, false) : neutralPill("暂无")}</div><div class="muted">runtime 检测</div></div>
        <div class="panel span-3 metric"><div class="label">失败审计</div><div class="value">${failedAudit.length ? statusPill(`${failedAudit.length} 条`, false) : neutralPill("暂无")}</div><div class="muted">Web 后台操作</div></div>
        <div class="panel span-3 metric"><div class="label">日志错误</div><div class="value">${logErrorTotal ? statusPill(`${logErrorTotal} 条`, false) : neutralPill("暂无")}</div><div class="muted">main/structure/web/ai</div></div>
        <div class="panel span-3 metric"><div class="label">网络超时</div><div class="value">${transientTotal ? neutralPill(`${transientTotal} 条`) : neutralPill("暂无")}</div><div class="muted">Telegram 自动重试类</div></div>
        <div class="panel span-12">
          <div class="summary-head">
            <h3 class="section-title">问题中心</h3>
            ${neutralPill(`${issues.length} 个问题`)}
          </div>
          ${issueCards(issues)}
        </div>
        <div class="panel span-12">
          <div class="summary-head">
            <h3 class="section-title">建议动作</h3>
            <div class="toolbar" style="margin:0">
              <button class="btn primary" onclick="copyReport()">复制报告</button>
              <span id="reportCopyStatus" class="hint"></span>
            </div>
          </div>
          <ul>${(data.recommendations || []).map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
        </div>
        <div class="panel span-12">
          <h3 class="section-title">最近错误</h3>
          <div class="readable-list">
            ${recentErrors.length ? recentErrors.map(item => row(`${item.source} · ${item.level}`, textValue(item.message))).join("") : row("最近错误", neutralPill("暂无"))}
          </div>
        </div>
        <div class="panel span-12">
          <h3 class="section-title">失败审计</h3>
          <table class="table">
            <thead><tr><th>时间</th><th>动作</th><th>对象</th><th>耗时</th><th>错误摘要</th></tr></thead>
            <tbody>${reportAuditRows(failedAudit)}</tbody>
          </table>
        </div>
        ${logErrorPanels(data.log_errors || {})}
        <div class="panel span-12">
          <h3 class="section-title">复制用文本</h3>
          <pre id="reportTextOutput" class="output">${escapeHtml(reportText(data))}</pre>
        </div>
        ${rawDetails("高级排查：原始诊断 JSON", data)}
      `;
    }
    async function copyReport() {
      const text = reportText(latestReportData || {});
      const ok = await copyTextToClipboard(text);
      const status = document.getElementById("reportCopyStatus");
      if (ok) {
        setSubtitle("诊断报告已复制");
        if (status) status.textContent = "已复制到剪贴板";
        return;
      }
      selectElementText("reportTextOutput");
      setSubtitle("浏览器拒绝自动复制，已选中报告文本，请按 Ctrl+C");
      if (status) status.textContent = "浏览器拒绝自动复制，已选中文本，请按 Ctrl+C";
    }
    async function loadConfig() {
      const data = await api("/api/config");
      latestConfigData = data;
      setSubtitle(`配置中心：${data.env_file}`);
      await renderConfigPage();
    }
    async function selectConfigCategory(id) {
      currentConfigCategory = id;
      clearKeys.clear();
      document.getElementById("configOutput").textContent = "";
      await renderConfigPage();
    }
    function configCategoryById(id) {
      return configCategories.find(item => item.id === id) || configCategories[0];
    }
    function configFieldAllowed(category, field) {
      if (!field || !field.key) return false;
      if (category.keys && category.keys.length) return category.keys.includes(field.key);
      if (category.excludeKeys && category.excludeKeys.includes(field.key)) return false;
      return true;
    }
    function configFieldsForCategory(category) {
      const sections = (latestConfigData && latestConfigData.sections) || {};
      return category.sections.flatMap(section => sections[section] || []).filter(field => configFieldAllowed(category, field));
    }
    function renderConfigCategoryBar() {
      const bar = document.getElementById("configCategoryBar");
      if (!bar) return;
      bar.innerHTML = configCategories.map(item => `
        <button class="btn ${item.id === currentConfigCategory ? "active" : ""}" type="button" onclick="selectConfigCategory('${escapeHtml(item.id)}')">${escapeHtml(item.label)}</button>
      `).join("");
    }
    function renderConfigHome() {
      const modules = configCategories.filter(item => item.id !== "home");
      return `
        ${renderPageIntro("config", [`${modules.length} 个模块`, "保存前预检"])}
        <div class="config-module-grid">
          ${modules.map(item => {
            const count = configFieldsForCategory(item).length;
            const countText = count ? `${count} 项设置` : "工具页面";
            return `<button class="config-module-card" type="button" onclick="selectConfigCategory('${escapeHtml(item.id)}')">
              <span class="config-module-title"><span>${escapeHtml(item.label)}</span>${neutralPill(countText)}</span>
              <span class="muted">${escapeHtml(item.desc)}</span>
              <span class="config-module-meta">
                <span><strong>影响：</strong>${escapeHtml(item.impact || "对应功能模块")}</span>
                <span><strong>保存后：</strong>${escapeHtml(item.apply || "按保存预检自动应用")}</span>
              </span>
            </button>`;
          }).join("")}
        </div>
      `;
    }
    function renderConfigSection(section, fields) {
      return `<div class="panel span-12">
        <h3 class="section-title">${escapeHtml(section)}</h3>
        <div class="form-grid">${fields.map(fieldHtml).join("")}</div>
      </div>`;
    }
    async function renderConfigPage() {
      const root = document.getElementById("configForms");
      const toolbar = document.getElementById("configSaveToolbar");
      const structureButton = document.getElementById("applyStructureButton");
      const category = configCategoryById(currentConfigCategory);
      const hasEditableFields = configFieldsForCategory(category).length > 0;
      renderConfigCategoryBar();
      if (toolbar) toolbar.classList.toggle("hidden", !hasEditableFields);
      if (structureButton) structureButton.classList.toggle("hidden", category.special !== "structure");
      document.getElementById("configPreview").classList.add("hidden");

      if (category.id === "home") {
        root.innerHTML = renderConfigHome();
        setSubtitle("配置中心：按功能模块选择要修改的设置");
        return;
      }

      const sections = (latestConfigData && latestConfigData.sections) || {};
      const parts = [
        `<div class="panel span-12">
          <div class="summary-head">
            <h3 class="section-title">${escapeHtml(category.label)}</h3>
            <button class="btn" type="button" onclick="selectConfigCategory('home')">返回配置首页</button>
          </div>
          <div class="hint">${escapeHtml(category.desc)}</div>
          <div class="config-category-summary">
            <div><strong>影响什么</strong>${escapeHtml(category.impact || "对应功能模块。")}</div>
            <div><strong>保存后怎么生效</strong>${escapeHtml(category.apply || "按保存预检自动应用。")}</div>
          </div>
        </div>`
      ];
      if (category.special === "api") parts.push(apiSourcePanel());
      if (category.special === "structure") parts.push(structureRecommendationPanel());
      category.sections.forEach(section => {
        const fields = sections[section] || [];
        if (fields.length) parts.push(renderConfigSection(section, fields));
      });
      if (category.special === "backup") parts.push(configBackupPanel());
      root.innerHTML = parts.join("");
      setSubtitle(`${category.label} · ${latestConfigData ? latestConfigData.env_file : ""}`);
      if (category.special === "structure") await loadStructureRecommendations();
      if (category.special === "backup") await loadConfigBackups();
    }
    function configCurrentText(field) {
      if (!field.configured && !field.value && !field.display_value) return "当前未配置";
      if (field.kind === "bool") return `当前使用：${zhBool(field.value)}`;
      const display = field.display_value || field.value || "已配置";
      if (field.source === "auto_route") return `当前使用：${display}（自动话题：${field.route_name || "已记录"}）`;
      return `当前使用：${display}`;
    }
    function fieldExplainHtml(field) {
      const rows = [
        ["做什么", field.purpose || field.help || `配置 ${field.label}`],
        ["影响什么", field.affects || "影响对应功能模块。"],
        ["改完是否自动重启", field.apply || "保存后按预检结果自动应用。"]
      ];
      return `<div class="field-explain">${rows.map(([label, value]) => `
        <div class="field-explain-row"><span class="field-explain-label">${escapeHtml(label)}</span><span>${escapeHtml(value)}</span></div>
      `).join("")}</div>`;
    }
    function fieldHtml(field) {
      const key = escapeHtml(field.key);
      const label = escapeHtml(field.label);
      const current = escapeHtml(configCurrentText(field));
      const helpParts = [];
      if (field.help) helpParts.push(escapeHtml(field.help));
      if (field.source === "auto_route") helpParts.push("当前 ID 来自自动创建的话题路由文件；输入新值并保存后会写入 .env.oi。");
      const help = helpParts.map(text => `<div class="field-help">${text}</div>`).join("");
      const explain = fieldExplainHtml(field);
      if (field.kind === "bool") {
        const raw = String(field.value || "").trim().toLowerCase();
        const selectedTrue = ["true", "1", "yes", "on", "y"].includes(raw) ? "selected" : "";
        const selectedFalse = ["false", "0", "no", "off", "n"].includes(raw) ? "selected" : "";
        return `<div class="field"><div class="field-heading"><label>${label}</label><span class="field-current">${current}</span></div><select data-key="${key}"><option value="true" ${selectedTrue}>开启</option><option value="false" ${selectedFalse}>关闭</option></select>${help}${explain}</div>`;
      }
      if (field.secret) {
        return `<div class="field"><div class="field-heading"><label>${label}</label><span class="field-current">${current}</span></div><div class="secret-row"><input data-key="${key}" type="password" placeholder="输入新值才会替换当前值"><button class="btn" type="button" onclick="clearSecret('${key}')">清空</button></div><div class="field-help">当前值会完整显示；输入新值才会替换当前值，留空保存不会改动。</div>${help}${explain}</div>`;
      }
      return `<div class="field"><div class="field-heading"><label>${label}</label><span class="field-current">${current}</span></div><input data-key="${key}" value="${escapeHtml(field.value || "")}">${help}${explain}</div>`;
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
    function configImpactHtml(impactData) {
      const impact = (impactData && impactData.impact) || impactData || {};
      if (!impact || !impact.message) return "";
      const services = impact.service_actions || [];
      const warnings = impact.warnings || [];
      const modules = impact.modules || [];
      return `<div class="config-impact">
        <h3 class="section-title">保存影响预检</h3>
        <div class="readable-list">
          ${row("影响模块", modules.length ? textValue(modules.join("、")) : neutralPill("无"))}
          ${row("自动应用", services.length ? textValue(services.map(item => `${serviceActionLabel(item.action)} ${item.service}${item.scheduled ? "（延迟）" : ""}`).join("、")) : neutralPill("不需要重启"))}
          ${row("结果说明", textValue(impact.message || ""))}
          ${row("回滚方式", textValue(impact.rollback || "保存前会自动备份 .env.oi"))}
        </div>
        ${warnings.length ? `<div class="notice warn"><strong>注意：</strong><ul>${warnings.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul></div>` : ""}
        ${impactData && impactData.errors && Object.keys(impactData.errors).length ? `<div class="notice danger"><strong>预检错误：</strong><ul>${Object.entries(impactData.errors).map(([key, value]) => `<li>${escapeHtml(key)}: ${escapeHtml(value)}</li>`).join("")}</ul></div>` : ""}
      </div>`;
    }
    function configImpactConfirmText(impactData) {
      const impact = (impactData && impactData.impact) || {};
      const services = (impact.service_actions || []).map(item => item.service).join("、") || "无服务重启";
      const modules = (impact.modules || []).join("、") || "无";
      return `影响模块：${modules}\n自动应用：${services}`;
    }
    async function fetchConfigImpact(updates, clear) {
      return await api("/api/config-impact", {
        method: "POST",
        body: JSON.stringify({ updates, clear })
      });
    }
    function renderConfigChanges(changes, impactData = null) {
      const target = document.getElementById("configPreview");
      if (!changes.length) {
        target.classList.remove("hidden");
        target.innerHTML = `<h3 class="section-title">配置改动预览</h3><div class="hint">没有检测到需要保存的改动。</div>${configImpactHtml(impactData)}`;
        return;
      }
      target.classList.remove("hidden");
      target.innerHTML = `<h3 class="section-title">配置改动预览</h3>
        <div class="readable-list">${changes.map(item => row(`${item.label} (${item.key})`, `<strong>${escapeHtml(item.oldText)}</strong> -> <strong>${escapeHtml(item.newText)}</strong>`)).join("")}</div>
        ${configImpactHtml(impactData)}`;
    }
    async function previewConfig() {
      const updates = gatherConfigUpdates();
      const visibleKeys = new Set(Object.keys(updates));
      const clear = Array.from(clearKeys).filter(key => visibleKeys.has(key));
      const changes = buildConfigChanges(updates);
      const impact = await fetchConfigImpact(updates, clear);
      renderConfigChanges(changes, impact);
      return { changes, impact };
    }
    function formatSaveResult(data, changes) {
      const lines = [];
      lines.push(data.ok ? "配置保存成功" : "配置保存失败");
      if (data.message) lines.push(`结果：${data.message}`);
      if (data.impact) {
        lines.push("");
        lines.push("影响预检：");
        lines.push(`- ${data.impact.message || ""}`);
        if ((data.impact.modules || []).length) lines.push(`- 影响模块：${data.impact.modules.join("、")}`);
        if ((data.impact.service_actions || []).length) {
          lines.push(`- 自动应用：${data.impact.service_actions.map(item => `${serviceActionLabel(item.action)} ${item.service}${item.scheduled ? "（延迟）" : ""}`).join("、")}`);
        } else {
          lines.push("- 自动应用：不需要重启服务");
        }
        (data.impact.warnings || []).forEach(item => lines.push(`- 注意：${item}`));
        if (data.impact.rollback) lines.push(`- 回滚：${data.impact.rollback}`);
      }
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
        applyResults.forEach(item => lines.push(`- ${item.service || item.name || "服务"} ${serviceActionLabel(item.action || "")}: ${item.ok ? "成功" : "失败"}`));
        if (applyResults.some(item => !item.ok)) {
          lines.push("建议下一步：去雷达服务页手动重启失败的服务，然后回总览确认状态。");
        } else {
          lines.push("建议下一步：回总览确认 runtime-status 的最近更新时间是否继续变化。");
        }
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
      box.innerHTML = backups.length ? backups.map(item => {
        const name = item.name || "";
        const nameArg = escapeHtml(JSON.stringify(name));
        return `
          <div class="feature-item">
            <strong>${escapeHtml(name)}</strong>
            <span class="muted">${escapeHtml(item.modified_at || "")} · ${escapeHtml(String(item.size || 0))} 字节</span>
            <div class="toolbar" style="margin:8px 0 0">
              <button class="btn warn" type="button" onclick="restoreConfigBackup(${nameArg})">恢复这个备份</button>
              <button class="btn danger" type="button" onclick="deleteConfigBackup(${nameArg})">删除备份</button>
            </div>
          </div>
        `;
      }).join("") : `<div class="hint">还没有 Web 保存产生的配置备份。</div>`;
    }
    async function restoreConfigBackup(name) {
      const confirmText = prompt(`恢复配置备份会覆盖当前 .env.oi，并自动应用。输入 RESTORE 确认：${name}`);
      if (confirmText !== "RESTORE") return;
      const data = await api("/api/config-restore", { method: "POST", body: JSON.stringify({ name }) });
      document.getElementById("configOutput").textContent = formatSaveResult(data, []);
      await loadConfig();
    }
    async function deleteConfigBackup(name) {
      const confirmText = prompt(`删除配置备份不可恢复。输入 DELETE 确认：${name}`);
      if (confirmText !== "DELETE") return;
      const data = await api("/api/config-backup-delete", { method: "POST", body: JSON.stringify({ name }) });
      document.getElementById("configOutput").textContent = data.message || data.error || "删除请求已处理";
      await loadConfigBackups();
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
      const visibleKeys = new Set(Object.keys(updates));
      const clear = Array.from(clearKeys).filter(key => visibleKeys.has(key));
      const changes = buildConfigChanges(updates);
      const impact = await fetchConfigImpact(updates, clear);
      renderConfigChanges(changes, impact);
      if (!changes.length) {
        document.getElementById("configOutput").textContent = "没有检测到需要保存的改动。";
        return;
      }
      if (!impact.ok) {
        document.getElementById("configOutput").textContent = formatSaveResult(impact, changes);
        return;
      }
      if (!confirm(`即将保存 ${changes.length} 项配置改动，并自动应用。\n${configImpactConfirmText(impact)}\n是否继续？`)) return;
      const data = await api("/api/config", {
        method: "POST",
        body: JSON.stringify({ updates, clear })
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
        ${renderPageIntro("actions", [`${actionList.length} 个动作`, "禁止任意命令"])}
      ` + actionList.map(action => `
        <div class="panel span-6 action-card">
          <div>
            <h3 class="section-title">${escapeHtml(action.label)}</h3>
            <span class="action-badge">${escapeHtml(action.badge)}</span>
          </div>
          <p class="muted">${escapeHtml(action.desc)}</p>
          <ul>${action.details.map(item => `<li>${escapeHtml(item)}</li>`).join("")}</ul>
          <button class="btn ${action.danger ? "danger" : "primary"}" onclick="runAction('${escapeHtml(action.id)}')">执行</button>
        </div>
      `).join("");
    }
    async function runAction(name) {
      const action = actionList.find(item => item.id === name);
      if (action && action.clientOnly) {
        await runWebSelfCheck(action);
        return;
      }
      if (action && action.confirmWord) {
        const confirmText = prompt(`这个动作是：${action.label}。输入 ${action.confirmWord} 确认执行：`);
        if (confirmText !== action.confirmWord) return;
      }
      const data = await api("/api/action", { method: "POST", body: JSON.stringify({ name }) });
      renderOperationResult("actionOutput", data, (action && action.label) || "检查测试", "action");
    }
    async function runWebSelfCheck(action) {
      const started = performance.now();
      const checks = [];
      const targets = [
        ["总览摘要", "/api/summary"],
        ["配置读取", "/api/config"],
        ["Web 日志", "/api/logs?target=web&lines=80"]
      ];
      try {
        for (const [name, path] of targets) {
          const data = await api(path);
          checks.push({
            name,
            path,
            ok: data.ok !== false,
            status: data._meta && data._meta.status,
            served_at: data._meta && data._meta.served_at,
            elapsed_ms: data._client_elapsed_ms
          });
        }
        const slow = checks.filter(item => Number(item.elapsed_ms || 0) > 3000);
        renderOperationResult("actionOutput", {
          ok: slow.length === 0,
          message: slow.length ? `有 ${slow.length} 个接口响应超过 3 秒。` : "Web API 自诊断通过。",
          checks,
          total_elapsed_ms: Math.round(performance.now() - started)
        }, action.label, "action");
      } catch (err) {
        const payload = err.payload || {};
        renderOperationResult("actionOutput", {
          ok: false,
          message: err.message || String(err),
          checks,
          error: payload.error || payload.message || String(err),
          _meta: payload._meta,
          _client_elapsed_ms: payload._client_elapsed_ms || Math.round(performance.now() - started)
        }, action.label, "action");
      }
    }
    function renderServices() {
      document.getElementById("serviceGrid").innerHTML = `
        ${renderPageIntro("services", [`${serviceGroups.length} 个服务`, "STOP 二次确认"])}
        <div class="panel span-12 notice">
          <strong>这个页面是控制后台服务开关的，不是普通测试按钮。</strong>
          建议优先使用“重启”。
          主服务、结构雷达、Web 控制台、AI 助手是四个不同的后台服务；只有确认要暂停某类功能时才点“停止”。
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
    async function loadPreviewPanel() {
      const data = await api("/api/push-preview");
      const previews = data.previews || [];
      document.getElementById("previewGrid").innerHTML = `
        ${renderPageIntro("preview", [`${previews.length} 个推送样例`, "不真实发送"])}
        ${previews.map(item => `
          <div class="panel span-4">
            <h3 class="section-title">${escapeHtml(item.title || "预览")}</h3>
            <pre style="min-height:220px;max-height:360px">${escapeHtml(item.text || "")}</pre>
          </div>
        `).join("")}
        <div class="panel span-8">
          <h3 class="section-title">版本更新检查</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>只检查</strong><span class="muted">读取当前版本和 GitHub 版本，不会拉代码。</span></div>
            <div class="feature-item"><strong>真正更新</strong><span class="muted">等我通知你完整版本完成后，再在服务器执行 paopao update --yes。</span></div>
          </div>
          <div class="toolbar" style="margin-top:10px">
            <button class="btn primary" onclick="checkUpdate()">检查 GitHub 更新</button>
          </div>
        </div>
        <div class="panel span-4">
          <h3 class="section-title">配置备份入口</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>自动备份</strong><span class="muted">每次保存配置前都会生成 .env.oi Web 备份。</span></div>
            <div class="feature-item"><strong>恢复 / 删除</strong><span class="muted">恢复和删除都需要输入确认词，避免误操作。</span></div>
          </div>
          <div class="toolbar" style="margin-top:10px"><button class="btn blue" onclick="currentConfigCategory='backup'; switchView('config')">打开备份恢复</button></div>
        </div>
      `;
      setSubtitle(data.message || "更新检查、推送预览和配置备份入口");
    }
    async function checkUpdate() {
      document.getElementById("updateOutput").textContent = "正在检查 GitHub 更新...";
      const data = await api("/api/update-check");
      document.getElementById("updateOutput").textContent = [
        data.message || "更新检查完成",
        "",
        data.stdout || "",
        data.stderr ? `\n错误输出:\n${data.stderr}` : ""
      ].join("\n").trim();
    }
    async function loadGuide() {
      const data = await api("/api/summary");
      const git = data.git || {};
      document.getElementById("guideGrid").innerHTML = `
        ${renderPageIntro("guide", [git.version || "unknown", git.commit || "unknown"])}
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
            <div class="feature-item"><strong>总览</strong><span class="muted">查看运行健康度、最近错误、主服务、结构雷达、Web 控制台、版本、runtime-status 和关键配置。</span></div>
            <div class="feature-item"><strong>AI 助手</strong><span class="muted">查看 AI 服务状态、意图分流规则、提示词入口和独立 AI Bot 使用方式；Telegram 私聊里可用“查 BTC”“GWEI 怎么看”查询币种雷达档案。</span></div>
            <div class="feature-item"><strong>价格提醒</strong><span class="muted">查看价格提醒统计，新增目标价提醒，按状态/类型/关键词筛选提醒，暂停、恢复和删除已有提醒；五大交易所手动选择流程仍建议在 Telegram 私聊按钮里完成。</span></div>
            <div class="feature-item"><strong>雷达服务</strong><span class="muted">启动、停止、重启主服务、结构雷达、Web 控制台和 AI 助手；页面会说明每个服务负责什么，停止操作需要输入 STOP。</span></div>
            <div class="feature-item"><strong>配置中心</strong><span class="muted">按 Telegram、AI、雷达参数、资金费率、模块开关、外部接口、Web 控制台和备份恢复分类修改设置；保存前预览，保存前自动备份 .env.oi。</span></div>
            <div class="feature-item"><strong>日志中心</strong><span class="muted">读取主服务、结构雷达、Web 控制台、AI 助手最近日志，支持自动刷新、搜索、错误/Telegram/Binance/结构/AI/资金费率筛选、摘要提取和复制。</span></div>
            <div class="feature-item"><strong>检查测试</strong><span class="muted">执行固定白名单动作；页面会说明每个按钮检查什么、什么时候用、是否会真实发送消息或清理文件。</span></div>
            <div class="feature-item"><strong>更新备份</strong><span class="muted">查看静态推送样例、GitHub 更新检查和配置备份入口；不会真实发送 Telegram，也不会自动更新代码。</span></div>
          </div>
        </div>
        <div class="panel span-6">
          <h3 class="section-title">使用规则</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>访问地址</strong><span class="muted">默认使用 http://服务器IP:8080/。如果你改了 WEB_PORT，按配置里的端口访问。</span></div>
            <div class="feature-item"><strong>登录令牌</strong><span class="muted">输入 WEB_ADMIN_TOKEN。服务器输入 paopao 后选择 1 可查看。不要把令牌发到公开群。</span></div>
            <div class="feature-item"><strong>单人管理员</strong><span class="muted">这个后台按你自己使用设计，登录后默认拥有全部操作权限，不引入多用户登录和复杂权限角色。</span></div>
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
      renderOperationResult("serviceOutput", data, label || "服务操作", "service");
      await refreshCurrent();
    }
    function zhDirection(value) {
      return value === "below" ? "低于或等于" : "高于或等于";
    }
    function zhAlertStatus(value) {
      const map = { active: "运行中", paused: "已暂停", triggered: "已触发" };
      return map[value] || value || "未知";
    }
    function alertTypeText(item) {
      return item.alert_type_label || item.alert_type || "目标价提醒";
    }
    function alertSearchText(item) {
      return [
        item.id,
        item.symbol,
        item.pair,
        item.venue_label,
        item.exchange,
        item.market_type,
        alertTypeText(item),
        item.condition_text,
        item.repeat_policy_label,
        item.status,
        item.last_price_text
      ].map(value => String(value || "").toLowerCase()).join(" ");
    }
    function renderAlertRows(alerts) {
      if (!alerts.length) {
        return tableEmpty(7, "还没有匹配的监控提醒", "可以在 Telegram 私聊 AI 助手 Bot 点击“设置价格提醒”，选择目标价、急涨急跌、OI 或资金费率监控。");
      }
      return alerts.map(item => `
        <tr>
          <td>${escapeHtml(String(item.id))}</td>
          <td><strong>${escapeHtml(item.pair || item.symbol)}</strong><div class="muted">${escapeHtml(item.venue_label || "Binance USDT 合约")}</div></td>
          <td>${escapeHtml(item.alert_type_label || "目标价提醒")}</td>
          <td>${escapeHtml(item.condition_text || `${zhDirection(item.direction)} ${item.target_price_text || item.target_price}`)}<div class="muted">${escapeHtml(item.repeat_policy_label || "提醒一次")}</div></td>
          <td>${neutralPill(zhAlertStatus(item.status))}</td>
          <td>${escapeHtml(item.last_price_text || "暂无")}</td>
          <td>
            <button class="btn" onclick="mutateAlert(${item.id}, '${item.status === "paused" ? "resume" : "pause"}')">${item.status === "paused" ? "恢复" : "暂停"}</button>
            <button class="btn danger" onclick="mutateAlert(${item.id}, 'delete')">删除</button>
          </td>
        </tr>
      `).join("");
    }
    function priceAlertFilterValues() {
      return {
        status: String(document.getElementById("priceStatusFilter")?.value || "all"),
        type: String(document.getElementById("priceTypeFilter")?.value || "all"),
        search: String(document.getElementById("priceSearch")?.value || "").trim().toLowerCase()
      };
    }
    function filteredPriceAlerts() {
      const alerts = (latestPriceAlertsData && latestPriceAlertsData.alerts) || [];
      const filters = priceAlertFilterValues();
      return alerts.filter(item => {
        if (filters.status !== "all" && String(item.status || "") !== filters.status) return false;
        if (filters.type !== "all" && alertTypeText(item) !== filters.type) return false;
        if (filters.search && !alertSearchText(item).includes(filters.search)) return false;
        return true;
      });
    }
    function renderPriceAlertTable() {
      const tbody = document.getElementById("priceAlertRows");
      if (!tbody) return;
      const alerts = (latestPriceAlertsData && latestPriceAlertsData.alerts) || [];
      const filtered = filteredPriceAlerts();
      tbody.innerHTML = renderAlertRows(filtered);
      const summary = document.getElementById("priceFilterSummary");
      if (summary) summary.innerHTML = `当前显示 ${filtered.length}/${alerts.length} 条提醒。可按状态、类型、币种、交易所、交易对或条件搜索。`;
    }
    function clearPriceAlertFilters() {
      const status = document.getElementById("priceStatusFilter");
      const type = document.getElementById("priceTypeFilter");
      const search = document.getElementById("priceSearch");
      if (status) status.value = "all";
      if (type) type.value = "all";
      if (search) search.value = "";
      renderPriceAlertTable();
    }
    async function loadAiAssistant() {
      const [summaryResult, alertsResult] = await Promise.all([
        apiOrError("/api/summary", "总览摘要"),
        apiOrError("/api/price-alerts", "价格提醒")
      ]);
      const summary = summaryResult.ok ? summaryResult.data : {};
      const alertsData = alertsResult.ok ? alertsResult.data : {};
      const failureHtml = partialErrorPanels([summaryResult, alertsResult]);
      const ai = ((summary.config || {}).ai_assistant || {});
      const service = ((summary.services || {}).ai || {});
      const stats = alertsData.stats || {};
      document.getElementById("aiGrid").innerHTML = `
        ${failureHtml}
        ${renderPageIntro("ai", [service.active || "unknown", ai.model || "deepseek-v4-pro"])}
        <div class="panel span-12 notice">
          <strong>AI 助手 Bot 和雷达推送 Bot 是分开的。</strong>
          群里的启动雷达、资金流雷达、结构雷达继续走 TG_BOT_TOKEN；私聊 AI、手动价格提醒和个人提醒走 AI_BOT_TOKEN。
          开启群内调用时，还必须在配置里填写允许调用的群/频道 ID，并且用户需要 @机器人或回复机器人消息。
        </div>
        <div class="panel span-3 metric"><div class="label">AI 服务</div><div class="value">${statusPill(service.active || "unknown", Boolean(service.active_ok))}</div><div class="muted">${escapeHtml(service.service || "paopao-ai")}</div></div>
        <div class="panel span-3 metric"><div class="label">AI Bot Token</div><div class="value">${neutralPill(configuredText(ai.bot_token_configured))}</div></div>
        <div class="panel span-3 metric"><div class="label">AI 问答接口</div><div class="value">${neutralPill(zhBool(ai.provider_enable))}</div><div class="muted">${escapeHtml(ai.model || "deepseek-v4-pro")}</div></div>
        <div class="panel span-3 metric"><div class="label">价格提醒</div><div class="value">${neutralPill(`${stats.active || 0} 运行中`)}</div><div class="muted">在“价格提醒”页管理</div></div>
        <div class="panel span-12">
          <h3 class="section-title">怎么用</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>首页按钮</strong><span class="muted">打开 AI 助手 Bot 私聊，发送 /start 会出现中文按钮首页；首页只保留设置价格提醒、我的提醒、查询价格和使用说明。AI 对话不需要按钮，直接发消息即可自动分流。</span></div>
            <div class="feature-item"><strong>自然语言</strong><span class="muted">可以直接说：BTC、BTC 现在多少钱、查 BTC、GWEI 怎么看，也可以问生活和功能问题。自然语言不会直接创建价格提醒。</span></div>
            <div class="feature-item"><strong>群内调用</strong><span class="muted">开启 AI_ALLOW_GROUP_CHAT 后，还要填写 AI_ALLOWED_CHAT_IDS；群里只有 @机器人或回复机器人消息才会触发。</span></div>
            <div class="feature-item"><strong>手动提醒</strong><span class="muted">价格提醒不再靠自然语言猜，私聊里点“设置价格提醒”，或在 Web 的“价格提醒”页创建和管理。</span></div>
            <div class="feature-item"><strong>自动分析</strong><span class="muted">直接粘贴启动雷达、结构雷达、资金流、OI、CVD、成交量等数据，会自动走专业分析师提示词。</span></div>
            <div class="feature-item"><strong>去命令化</strong><span class="muted">AI Bot 只保留 /start 打开首页；查价格、看行情、AI 分析都直接发消息，提醒管理在“我的提醒”按钮里完成。</span></div>
          </div>
        </div>
        <div class="panel span-6">
          <h3 class="section-title">提示词管理</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>泡泡 AI 助手提示词</strong><span class="muted">控制日常问答风格、功能说明和轻松对话。</span></div>
            <div class="feature-item"><strong>专业分析师提示词</strong><span class="muted">控制雷达数据、行情数据、资金费率、OI 和结构信号的分析方式。</span></div>
          </div>
          <div class="toolbar" style="margin-top:12px"><button class="btn primary" onclick="switchView('prompts')">编辑 AI 提示词</button></div>
        </div>
        <div class="panel span-6">
          <h3 class="section-title">提醒管理入口</h3>
          <div class="feature-list">
            <div class="feature-item"><strong>当前提醒</strong><span class="muted">运行中 ${stats.active || 0}，暂停 ${stats.paused || 0}，已触发 ${stats.triggered || 0}。</span></div>
            <div class="feature-item"><strong>创建方式</strong><span class="muted">Web 创建需要填写 Telegram 用户 ID，或先在配置中心填写 AI_DEFAULT_CHAT_ID。</span></div>
          </div>
          <div class="toolbar" style="margin-top:12px"><button class="btn primary" onclick="switchView('price')">打开价格提醒</button></div>
        </div>
      `;
      setSubtitle(failureHtml ? "AI 助手：部分信息读取失败，其余可用信息已显示" : "AI 助手：问答、意图分流、提示词和服务状态");
    }
    async function loadPriceAlerts() {
      const [summaryResult, alertsResult] = await Promise.all([
        apiOrError("/api/summary", "总览摘要"),
        apiOrError("/api/price-alerts", "价格提醒")
      ]);
      const summary = summaryResult.ok ? summaryResult.data : {};
      const alertsData = alertsResult.ok ? alertsResult.data : { stats: {}, alerts: [] };
      const failureHtml = partialErrorPanels([summaryResult, alertsResult]);
      latestPriceAlertsData = alertsData;
      const ai = ((summary.config || {}).ai_assistant || {});
      const service = ((summary.services || {}).ai || {});
      const stats = alertsData.stats || {};
      const alerts = alertsData.alerts || [];
      const alertTypes = Array.from(new Set(alerts.map(alertTypeText).filter(Boolean))).sort();
      document.getElementById("priceGrid").innerHTML = `
        ${failureHtml}
        ${renderPageIntro("price", [`${stats.active || 0} 运行中`, `${alerts.length} 条提醒`])}
        <div class="panel span-12 notice">
          <strong>价格提醒是独立的个人监控中心。</strong>
          普通用户在 Telegram 私聊里按按钮手动选择；这里是管理员 Web 入口，用来快速查看、创建、暂停、恢复和删除提醒。
        </div>
        <div class="panel span-3 metric"><div class="label">AI 服务</div><div class="value">${statusPill(service.active || "unknown", Boolean(service.active_ok))}</div><div class="muted">${escapeHtml(service.service || "paopao-ai")}</div></div>
        <div class="panel span-3 metric"><div class="label">提醒功能</div><div class="value">${neutralPill(zhBool(ai.price_alerts_enable))}</div><div class="muted">AI_PRICE_ALERTS_ENABLE</div></div>
        <div class="panel span-3 metric"><div class="label">运行中</div><div class="value">${neutralPill(String(stats.active || 0))}</div><div class="muted">触发前持续检查</div></div>
        <div class="panel span-3 metric"><div class="label">已触发 / 暂停</div><div class="value">${neutralPill(`${stats.triggered || 0} / ${stats.paused || 0}`)}</div><div class="muted">可在列表里恢复或删除</div></div>
        <div class="panel span-12">
          <h3 class="section-title">新增目标价提醒</h3>
          <div class="form-grid">
            <div class="field"><label>币种</label><input id="newAlertSymbol" placeholder="BTC 或 BTCUSDT"></div>
            <div class="field"><label>方向</label><select id="newAlertDirection"><option value="above">高于或等于</option><option value="below">低于或等于</option></select></div>
            <div class="field"><label>目标价格</label><input id="newAlertPrice" placeholder="58000"></div>
            <div class="field"><label>接收提醒的 Telegram 用户 ID</label><input id="newAlertChatId" placeholder="留空则使用 AI_DEFAULT_CHAT_ID"></div>
          </div>
          <div class="hint" style="margin-top:8px">Web 创建的是基础目标价提醒；五大交易所手动选择、急涨急跌、OI 和资金费率提醒仍建议在 Telegram 私聊按钮流程里创建。</div>
          <div class="toolbar" style="margin-top:12px"><button class="btn primary" onclick="createWebAlert()">创建提醒</button></div>
        </div>
        <div class="panel span-12">
          <h3 class="section-title">监控提醒列表</h3>
          <div class="toolbar">
            <select id="priceStatusFilter" onchange="renderPriceAlertTable()">
              <option value="all">全部状态</option>
              <option value="active">运行中</option>
              <option value="paused">已暂停</option>
              <option value="triggered">已触发</option>
            </select>
            <select id="priceTypeFilter" onchange="renderPriceAlertTable()">
              <option value="all">全部类型</option>
              ${alertTypes.map(type => `<option value="${escapeHtml(type)}">${escapeHtml(type)}</option>`).join("")}
            </select>
            <input id="priceSearch" placeholder="搜索币种、交易所、交易对、条件" oninput="renderPriceAlertTable()">
            <button class="btn" type="button" onclick="clearPriceAlertFilters()">清空筛选</button>
          </div>
          <div id="priceFilterSummary" class="hint" style="margin-bottom:8px"></div>
          <table class="table">
            <thead><tr><th>ID</th><th>币种</th><th>类型</th><th>条件</th><th>状态</th><th>最后价格</th><th>操作</th></tr></thead>
            <tbody id="priceAlertRows">${renderAlertRows(alerts)}</tbody>
          </table>
        </div>
      `;
      setSubtitle(failureHtml ? "价格提醒：部分信息读取失败，其余可用信息已显示" : "价格提醒：创建、暂停、恢复和删除");
      renderPriceAlertTable();
    }
    async function loadAiPrompts() {
      const data = await api("/api/ai-prompts");
      const prompts = data.prompts || {};
      setSubtitle(data.path || "AI 提示词文件");
      document.getElementById("promptGrid").innerHTML = `
        ${renderPageIntro("prompts", [data.path || "ai_prompts.json", "保存后自动重启"])}
        <div class="panel span-6">
          <h3 class="section-title">泡泡 AI 助手提示词</h3>
          <div class="hint">用于日常问答、生活问题、运行状态解释、价格提醒说明。默认风格更轻松，可以有一点皮，但交易问题仍会自动走专业分析。</div>
          <textarea id="assistantPrompt">${escapeHtml(prompts.assistant_prompt || "")}</textarea>
        </div>
        <div class="panel span-6">
          <h3 class="section-title">专业分析师提示词</h3>
          <div class="hint">用于“分析这段”“帮我分析”，以及自动识别出的雷达信号、资金流、OI、市值、流动性和链上/交易所数据。</div>
          <textarea id="analystPrompt">${escapeHtml(prompts.analyst_prompt || "")}</textarea>
        </div>
        <div class="panel span-12">
          <h3 class="section-title">测试提示词</h3>
          <div class="form-grid">
            <div class="field">
              <label>测试模式</label>
              <select id="promptTestMode"><option value="analyst">专业分析师</option><option value="assistant">泡泡 AI 助手</option></select>
            </div>
            <div class="field">
              <label>测试内容</label>
              <input id="promptTestInput" value="启动雷达 BTCUSDT：15m价格 +4.2%，1h OI +8.1%，成交量 3.4x，资金费率偏高。">
            </div>
          </div>
          <div class="toolbar" style="margin-top:12px">
            <button class="btn primary" onclick="saveAiPrompts()">保存提示词</button>
            <button class="btn" onclick="resetAiPrompts()">恢复默认</button>
            <button class="btn blue" onclick="testAiPrompt()">测试当前提示词</button>
          </div>
        </div>
      `;
    }
    async function saveAiPrompts() {
      const body = {
        action: "save",
        assistant_prompt: document.getElementById("assistantPrompt").value,
        analyst_prompt: document.getElementById("analystPrompt").value
      };
      const data = await api("/api/ai-prompts", { method: "POST", body: JSON.stringify(body) });
      document.getElementById("promptOutput").textContent = JSON.stringify(data, null, 2);
      await loadAiPrompts();
    }
    async function resetAiPrompts() {
      if (!confirm("确认恢复默认 AI 提示词？当前自定义内容会被覆盖。")) return;
      const data = await api("/api/ai-prompts", { method: "POST", body: JSON.stringify({ action: "reset" }) });
      document.getElementById("promptOutput").textContent = JSON.stringify(data, null, 2);
      await loadAiPrompts();
    }
    async function testAiPrompt() {
      const body = {
        action: "test",
        mode: document.getElementById("promptTestMode").value,
        text: document.getElementById("promptTestInput").value,
        assistant_prompt: document.getElementById("assistantPrompt").value,
        analyst_prompt: document.getElementById("analystPrompt").value
      };
      const data = await api("/api/ai-prompts", { method: "POST", body: JSON.stringify(body) });
      document.getElementById("promptOutput").textContent = JSON.stringify(data, null, 2);
    }
    async function createWebAlert() {
      const body = {
        action: "create",
        symbol: document.getElementById("newAlertSymbol").value,
        direction: document.getElementById("newAlertDirection").value,
        target_price: document.getElementById("newAlertPrice").value,
        chat_id: document.getElementById("newAlertChatId").value
      };
      const data = await api("/api/price-alerts", { method: "POST", body: JSON.stringify(body) });
      renderOperationResult("priceOutput", data, "创建价格提醒", "price");
      await loadPriceAlerts();
    }
    async function mutateAlert(id, action) {
      if (action === "delete" && !confirm(`确认删除提醒 ${id}？`)) return;
      const data = await api("/api/price-alerts", { method: "POST", body: JSON.stringify({ id, action }) });
      const labels = { pause: "暂停价格提醒", resume: "恢复价格提醒", delete: "删除价格提醒" };
      renderOperationResult("priceOutput", data, labels[action] || "修改价格提醒", "price");
      await loadPriceAlerts();
    }
    function switchView(view) {
      currentView = view;
      document.querySelectorAll(".view").forEach(el => el.classList.add("hidden"));
      document.getElementById(view).classList.remove("hidden");
      document.querySelectorAll("nav button").forEach(btn => btn.classList.toggle("active", btn.dataset.view === view));
      document.getElementById("pageTitle").textContent = titles[view];
      startAutoRefresh();
      refreshCurrent();
    }
    async function refreshCurrent(isAuto = false) {
      try {
        updateAutoRefreshButton();
        if (currentView === "overview") await loadSummary();
        if (currentView === "logs") await loadLogs();
        if (currentView === "audit") await loadAudit();
        if (currentView === "report") await loadReport();
        if (currentView === "config") await loadConfig();
        if (currentView === "ai") await loadAiAssistant();
        if (currentView === "price") await loadPriceAlerts();
        if (currentView === "prompts") await loadAiPrompts();
        if (currentView === "actions") { setSubtitle("固定白名单动作，说明写在每张卡片里"); renderActions(); }
        if (currentView === "services") { setSubtitle("雷达后台服务开关，停止前会二次确认"); renderServices(); }
        if (currentView === "preview") await loadPreviewPanel();
        if (currentView === "guide") await loadGuide();
      } catch (err) {
        setSubtitle(`${isAuto ? "自动刷新失败：" : ""}${err.message || String(err)}`);
        renderViewError(currentView, err, isAuto);
      }
    }
    document.querySelectorAll("nav button").forEach(btn => btn.addEventListener("click", () => switchView(btn.dataset.view)));
    updateAutoRefreshButton();
    refreshCurrent();
  </script>
</body>
</html>
"""


class WebHandler(BaseHTTPRequestHandler):
    server_version = "PaopaoRadarWeb/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write(f"[web] {self.address_string()} {fmt % args}\n")

    def api_meta(self, status: int) -> dict[str, Any]:
        parsed = urlparse(self.path)
        return {
            "served_at": now_text(),
            "path": parsed.path,
            "status": int(status),
            "request_id": f"{int(time.time() * 1000)}-{threading.get_ident()}",
        }

    def send_json(self, data: Any, status: int = 200) -> None:
        payload_obj = data
        if isinstance(data, dict):
            payload_obj = dict(data)
            existing_meta = payload_obj.get("_meta")
            meta = existing_meta if isinstance(existing_meta, dict) else {}
            payload_obj["_meta"] = {**meta, **self.api_meta(int(status))}
        payload = json.dumps(payload_obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, message: str, status: int = 400, code: str = "bad_request") -> None:
        self.send_json({"ok": False, "error": message, "message": message, "code": code}, status)

    def send_audited_json(
        self,
        path: str,
        request_data: dict[str, Any],
        result: dict[str, Any],
        *,
        status: int = 200,
        started_at: float,
    ) -> None:
        try:
            append_web_audit(path, request_data, result, status=int(status), started_at=started_at)
        except Exception as exc:
            sys.stderr.write(f"[web] audit write failed: {type(exc).__name__}: {exc}\n")
        self.send_json(result, status)

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
        self.send_error_json("需要访问令牌", HTTPStatus.UNAUTHORIZED, "unauthorized")
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
        if path == "/api/push-preview":
            self.send_json(push_preview_payload())
            return
        if path == "/api/update-check":
            self.send_json(update_check_payload())
            return
        if path == "/api/price-alerts":
            from .ai_assistant import price_alerts_payload

            self.send_json(price_alerts_payload())
            return
        if path == "/api/ai-prompts":
            self.send_json(load_ai_prompts(Settings.load()))
            return
        if path == "/api/logs":
            target = query.get("target", ["main"])[0]
            lines = int(query.get("lines", ["200"])[0] or 200)
            self.send_json(logs_payload(target, lines))
            return
        if path == "/api/audit":
            limit = int(query.get("limit", ["200"])[0] or 200)
            result = query.get("result", ["all"])[0]
            search = query.get("search", [""])[0]
            self.send_json(web_audit_payload(limit=limit, result=result, search=search))
            return
        if path == "/api/ops-snapshot":
            self.send_json(ops_snapshot_payload())
            return
        self.send_error_json("接口不存在", HTTPStatus.NOT_FOUND, "not_found")

    def do_POST(self) -> None:
        if not self.require_auth():
            return
        path = urlparse(self.path).path
        started_at = time.time()
        data: dict[str, Any] = {}
        try:
            data = self.read_json()
            if path == "/api/config-impact":
                self.send_json(config_impact_payload(data))
                return
            if path == "/api/config":
                updates = data.get("updates", {})
                clear = data.get("clear", [])
                if not isinstance(updates, dict) or not isinstance(clear, list):
                    raise ValueError("updates 必须是对象，clear 必须是数组")
                result = write_env_updates(updates, clear=[str(item) for item in clear])
                result["impact"] = config_change_impact([str(item) for item in result.get("changed", [])])
                if result.get("ok") and result.get("changed"):
                    apply_result = auto_apply_config_changes([str(item) for item in result.get("changed", [])])
                    result["apply"] = apply_result
                    result["impact"] = apply_result.get("impact", result["impact"])
                    result["message"] = apply_result.get("message", result.get("message"))
                self.send_audited_json(path, data, result, started_at=started_at)
                return
            if path == "/api/config-restore":
                name = str(data.get("name", ""))
                result = restore_env_backup(name)
                result["impact"] = config_change_impact([str(item) for item in result.get("changed", [])])
                if result.get("ok") and result.get("changed"):
                    apply_result = auto_apply_config_changes([str(item) for item in result.get("changed", [])])
                    result["apply"] = apply_result
                    result["impact"] = apply_result.get("impact", result["impact"])
                    result["message"] = apply_result.get("message", result.get("message"))
                self.send_audited_json(path, data, result, started_at=started_at)
                return
            if path == "/api/config-backup-delete":
                name = str(data.get("name", ""))
                self.send_audited_json(path, data, delete_env_backup(name), started_at=started_at)
                return
            if path == "/api/action":
                self.send_audited_json(path, data, run_cli_action(str(data.get("name", ""))), started_at=started_at)
                return
            if path == "/api/service":
                name = str(data.get("name", ""))
                result = run_service_action(name)
                if name in SERVICE_ACTIONS:
                    service, action = SERVICE_ACTIONS[name]
                    result.update({"name": name, "service": service, "action": action})
                self.send_audited_json(path, data, result, started_at=started_at)
                return
            if path == "/api/price-alerts":
                from .ai_assistant import create_price_alert_from_payload, mutate_price_alert_from_payload

                action = str(data.get("action") or "create").strip().lower()
                if action == "create":
                    result = create_price_alert_from_payload(data)
                else:
                    result = mutate_price_alert_from_payload(data)
                self.send_audited_json(path, data, result, started_at=started_at)
                return
            if path == "/api/ai-prompts":
                action = str(data.get("action") or "save").strip().lower()
                settings = Settings.load()
                if action == "reset":
                    result = reset_ai_prompts(settings)
                elif action == "test":
                    self.send_audited_json(path, data, ai_prompts_test_payload(data), started_at=started_at)
                    return
                else:
                    result = save_ai_prompts(
                        {
                            "assistant_prompt": data.get("assistant_prompt", ""),
                            "analyst_prompt": data.get("analyst_prompt", ""),
                        },
                        settings,
                    )
                if result.get("ok"):
                    apply_result = auto_apply_config_changes(["AI_PROMPTS_FILE"])
                    result["apply"] = apply_result
                    result["message"] = apply_result.get("message", result.get("message"))
                self.send_audited_json(path, data, result, started_at=started_at)
                return
            result = {"ok": False, "error": "接口不存在", "message": "接口不存在", "code": "not_found"}
            self.send_audited_json(path, data, result, status=HTTPStatus.NOT_FOUND, started_at=started_at)
        except Exception as exc:
            result = {"ok": False, "error": f"{type(exc).__name__}: {exc}", "message": f"{type(exc).__name__}: {exc}", "code": "bad_request"}
            self.send_audited_json(path, data, result, status=HTTPStatus.BAD_REQUEST, started_at=started_at)


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
