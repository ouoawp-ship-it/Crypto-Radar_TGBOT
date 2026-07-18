from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import os
import platform
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
from .auth import (
    append_auth_audit,
    auth_audit_payload,
    build_clear_cookie,
    build_session_cookie,
    check_auth_lockout,
    clear_auth_failures,
    cookie_value,
    create_session_value,
    record_auth_failure,
    verify_password,
    iso_timestamp,
    verify_session_value,
    verify_session_value_detailed,
)
from .atomic_json import locked_read_json, locked_update_json, locked_write_json
from .config import BASE_DIR, ENV_FILE, Settings, load_env_file, normalize_ai_model
from .runtime_cache import get_or_set as runtime_cache_get_or_set
from .runtime_cache import invalidate as invalidate_runtime_cache
from .storage import JsonStore
from .web_services.api_core import (
    api_error,
    filter_params,
    normalize_symbol_filter,
    pagination_params,
    sort_params,
    time_range_params,
)
from .web_services.jobs import (
    LONG_ACTION_JOB_TYPES,
    cancel_job_payload,
    cleanup_jobs_payload,
    create_job_payload,
    job_detail_payload,
    job_report_payload,
    jobs_payload,
    jobs_stats_payload,
    rerun_job_payload,
)
from .web_services.ops import update_check_status_payload
from .web_services.public import (
    public_agents_overview_payload,
    public_coin_context_payload,
    public_data_sources_payload,
    public_api_health_payload,
    public_funds_assets_payload,
    public_funds_sectors_payload,
    public_info_feed_payload,
    public_market_overview_payload,
    public_realtime_market_payload,
    public_realtime_intelligence_payload,
    public_workstation_funds_open_interest_payload,
    public_market_snapshot_payload,
    public_radar_boards_payload,
    public_radar_intelligence_payload,
    public_workstation_radar_momentum_payload,
    public_watchlist_market_payload,
    public_signal_context_payload,
    public_signal_detail_payload,
    public_signal_stats_payload,
    public_signals_payload,
    public_stream_batch,
)
from .web_services.signals import (
    enhance_signal_items,
    signal_detail_view,
    signal_stats_display,
)
from .web_observability import PUBLIC_API_LIMITER, PUBLIC_API_METRICS, PUBLIC_STREAM_METRICS, PUBLIC_TELEMETRY


MAIN_SERVICE = os.getenv("SERVICE_NAME", "paopao-radar")
WEB_SERVICE = os.getenv("WEB_SERVICE_NAME", "paopao-web")
AI_SERVICE = os.getenv("AI_SERVICE_NAME", "paopao-ai")
DASHBOARD_SERVICE_CACHE_TTL_SEC = 5
DASHBOARD_GIT_CACHE_TTL_SEC = 30
STABILITY_FILE_CACHE_TTL_SEC = 10
WEB_CONFIG_KEYS = {
    "WEB_HOST",
    "WEB_PORT",
    "WEB_ADMIN_TOKEN",
    "WEB_AUTH_MODE",
    "WEB_ADMIN_USERNAME",
    "WEB_SESSION_TTL_SEC",
    "WEB_AUTH_COOKIE_NAME",
    "WEB_AUTH_MAX_FAILURES",
    "WEB_AUTH_LOCKOUT_SEC",
    "WEB_AUTH_FAILURE_WINDOW_SEC",
    "WEB_AUTH_AUDIT_LIMIT",
    "WEB_SESSION_REFRESH_THRESHOLD_RATIO",
    "PAOXX_PUBLIC_BASE_URL",
    "PUBLIC_API_RATE_LIMIT_PER_MINUTE",
    "PUBLIC_API_HEAVY_RATE_LIMIT_PER_MINUTE",
    "PUBLIC_API_TRUSTED_PROXY_IPS",
}
SIGNAL_EVENT_CONFIG_KEYS = {
    "SIGNAL_EVENTS_FILE",
    "SIGNAL_EVENTS_DB_FILE",
    "SIGNAL_EVENTS_LIMIT",
    "SIGNAL_EVENTS_RETENTION_DAYS",
}
AI_CONFIG_KEYS = {
    "AI_ASSISTANT_ENABLE",
    "AI_BOT_TOKEN",
    "AI_BOT_USERNAME",
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
PROBLEM_STATE_FILE = "problem_state.json"
PROBLEM_STATE_LIMIT = 500
RELEASE_CLOSURE_TARGET = "v1.50.0"
RELEASE_CLOSURE_RULE = "进入 v1.50.0 收口路线后，不新增大模块，只做稳定性、验收、文档和已有功能修复。"
RELEASE_MAINTENANCE_POLICY = "v1.50.0 发布后，v1 主线进入长期维护：只做 bug 修复、策略微调、文档和运维补丁；新增大模块进入 v2 规划。"
RELEASE_CLOSURE_STAGES: tuple[dict[str, str], ...] = (
    {"version": "v1.47.0", "label": "功能冻结和稳定性收口", "goal": "冻结新增大模块，集中处理现有诊断、Web、AI Bot、服务控制和测试缺口。"},
    {"version": "v1.48.0", "label": "服务器部署验收闭环", "goal": "把更新、端口、服务、配置、回滚和 stable-check 做成可验收闭环。"},
    {"version": "v1.49.0", "label": "文档说明和运维流程最终整理", "goal": "整理安装、更新、排错、Web 后台和 AI Bot 使用说明。"},
    {"version": "v1.50.0", "label": "v1 完整稳定版发布", "goal": "完成最终自检、历史验收和稳定版发布，后续只做小修和策略优化。"},
)
_CPU_SAMPLE_LOCK = threading.Lock()
_CPU_LAST_SAMPLE: tuple[int, int] | None = None


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
    ConfigField("AI_ASSISTANT_ENABLE", "启用 AI 助手 Bot", "AI 助手", kind="bool", help="开启后 paopao-ai 服务会使用独立 AI_BOT_TOKEN 处理私聊和价格提醒。"),
    ConfigField("AI_BOT_TOKEN", "AI 助手 Bot Token", "AI 助手", secret=True, help="建议用 BotFather 单独创建一个机器人，不要和群推送 Bot 共用。"),
    ConfigField("AI_BOT_USERNAME", "AI 助手 Bot 用户名", "AI 助手", help="不含 @，用于从 Web 详情一键打开 AI 分析和提醒流程。"),
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
    ConfigField("SIGNAL_EVENTS_DB_FILE", "信号推送数据库文件", "AI 助手", help="默认 signals.db，存放所有 Telegram 推送结果的结构化记录，供 Web 信号推送页查询。一般不需要修改。"),
    ConfigField("SIGNAL_EVENTS_LIMIT", "币种档案信号保留数量", "AI 助手", kind="int", minimum=100, maximum=50000, help="AI 查询币种时会读取最近的结构化信号事件。建议 5000。"),
    ConfigField("SIGNAL_EVENTS_RETENTION_DAYS", "币种档案信号保留天数", "AI 助手", kind="int", minimum=1, maximum=365, help="超过该天数的信号事件会在新事件写入时自动清理。建议 60。"),
    ConfigField("TG_TOPIC_INTRO_ENABLE", "发送话题说明", "模块开关", kind="bool"),
    ConfigField("TG_TOPIC_INTRO_PIN", "置顶话题说明", "模块开关", kind="bool"),
    ConfigField("CLEANUP_ENABLE", "自动清理", "模块开关", kind="bool"),
    ConfigField("WEB_HOST", "Web 监听地址", "Web 控制台"),
    ConfigField("WEB_PORT", "Web 端口", "Web 控制台", kind="int", minimum=1, maximum=65535),
    ConfigField("PAOXX_PUBLIC_BASE_URL", "公开前台地址", "Web 控制台", help="例如 https://paoxx.com，用于 Telegram 与 Web 的信号深链。"),
    ConfigField("PUBLIC_API_RATE_LIMIT_PER_MINUTE", "公开接口每分钟请求上限", "Web 控制台", kind="int", minimum=30, maximum=3000),
    ConfigField("PUBLIC_API_HEAVY_RATE_LIMIT_PER_MINUTE", "聚合接口每分钟请求上限", "Web 控制台", kind="int", minimum=5, maximum=600),
    ConfigField("PUBLIC_API_TRUSTED_PROXY_IPS", "可信反向代理 IP", "Web 控制台", help="默认 127.0.0.1,::1；只有来自这些代理的 X-Forwarded-For 才会被信任。"),
    ConfigField("WEB_AUTH_MODE", "后台认证模式", "Web 控制台", help="默认 password；token 仅用于旧模式紧急回滚。"),
    ConfigField("WEB_ADMIN_USERNAME", "后台用户名", "Web 控制台"),
    ConfigField("WEB_SESSION_TTL_SEC", "登录有效期秒数", "Web 控制台", kind="int", minimum=300, maximum=604800),
    ConfigField("WEB_AUTH_COOKIE_NAME", "登录 Cookie 名称", "Web 控制台"),
    ConfigField("WEB_AUTH_MAX_FAILURES", "登录失败锁定次数", "Web 控制台", kind="int", minimum=1, maximum=20),
    ConfigField("WEB_AUTH_LOCKOUT_SEC", "登录锁定秒数", "Web 控制台", kind="int", minimum=60, maximum=86400),
    ConfigField("WEB_AUTH_FAILURE_WINDOW_SEC", "失败计数窗口秒数", "Web 控制台", kind="int", minimum=60, maximum=86400),
    ConfigField("WEB_AUTH_AUDIT_LIMIT", "登录审计保留条数", "Web 控制台", kind="int", minimum=50, maximum=5000),
    ConfigField("WEB_SESSION_REFRESH_THRESHOLD_RATIO", "会话续期阈值比例", "Web 控制台", kind="float", minimum=0.1, maximum=0.9),
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
)
EDITABLE_CONFIG: dict[str, ConfigField] = {field.key: field for field in EDITABLE_CONFIG_FIELDS}

TOPIC_FIELD_ROUTES: dict[str, tuple[str, str]] = {
    "TG_RADAR_SUMMARY_TOPIC_ID": ("TG_RADAR_SUMMARY", "资金摘要"),
    "TG_LAUNCH_ALERT_TOPIC_ID": ("TG_LAUNCH_ALERT", "启动预警"),
    "TG_ANNOUNCEMENT_ALERT_TOPIC_ID": ("TG_ANNOUNCEMENT_ALERT", "公告风险"),
    "TG_TEST_TOPIC_ID": ("TG_TEST_MESSAGE", "测试消息"),
    "TG_FLOW_RADAR_TOPIC_ID": ("TG_FLOW_RADAR", "资金流雷达"),
    "TG_FUNDING_ALERT_TOPIC_ID": ("TG_FUNDING_ALERT", "资金费率警报"),
}

TOPIC_SUMMARY_ROUTE_KEYS: dict[str, str] = {
    "TG_RADAR_SUMMARY": "radar_summary",
    "TG_LAUNCH_ALERT": "launch_alert",
    "TG_ANNOUNCEMENT_ALERT": "announcement_alert",
    "TG_TEST_MESSAGE": "test",
    "TG_FLOW_RADAR": "flow_radar",
    "TG_FUNDING_ALERT": "funding_alert",
}

CONFIG_FIELD_PURPOSE: dict[str, str] = {
    "TG_BOT_TOKEN": "群推送机器人令牌，用来把雷达信号发送到 Telegram。",
    "TG_CHAT_ID": "群、频道或超级群 ID，决定群推送机器人把消息发到哪里。",
    "TELEGRAM_USE_TOPIC": "控制群推送是否使用 Telegram 话题。",
    "TG_AUTO_CREATE_TOPICS": "开启后系统会按资金摘要、启动预警、资金流等模板自动创建话题并记录路由。",
    "TG_TOPIC_ID": "没有单独话题 ID 时使用的默认话题。",
    "WEB_HOST": "Web 后台监听地址。普通部署保持 0.0.0.0 即可让服务器 IP 可以访问。",
    "WEB_PORT": "Web 后台访问端口。默认 8080。",
    "WEB_ADMIN_TOKEN": "旧 token 认证模式访问令牌，仅用于 WEB_AUTH_MODE=token 紧急回滚。",
    "TG_TOPIC_INTRO_ENABLE": "控制自动话题创建后是否发送话题说明。",
    "TG_TOPIC_INTRO_PIN": "控制话题说明是否置顶。",
    "CLEANUP_ENABLE": "控制后台是否按计划清理临时文件、过期图表和超限历史记录。",
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
    "cleanup": {"label": "立即清理运行垃圾", "argv": ["cleanup", "--force-cleanup"], "timeout": 60},
}

SERVICE_ACTIONS: dict[str, tuple[str, str]] = {
    "restart-main": (MAIN_SERVICE, "restart"),
    "start-main": (MAIN_SERVICE, "start"),
    "stop-main": (MAIN_SERVICE, "stop"),
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
        if not line or line.startswith("#") or "="…44132 tokens truncated…symbol", [""])[0],
                status=query.get("status", [""])[0],
                q=query.get("q", [""])[0],
                window_sec=min(2592000, max(1, query_int_or(query.get("window_sec", ["86400"])[0], 86400))),
            ))
            return
        if path == "/public-api/signals/detail":
            self.send_json(public_signal_detail_payload(query.get("id", [""])[0]))
            return
        if path == "/public-api/signals/context":
            self.send_json(public_signal_context_payload(query.get("id", [""])[0]))
            return
        if path == "/public-api/signals/stats":
            self.send_json(public_signal_stats_payload(
                window_sec=min(2592000, max(1, query_int_or(query.get("window_sec", ["86400"])[0], 86400))),
            ))
            return
        if path == "/public-api/market/snapshot":
            self.send_json(public_market_snapshot_payload(query.get("symbol", [""])[0]))
            return
        if path == "/public-api/market/overview":
            self.send_json(public_market_overview_payload(
                window_sec=query_int_or(query.get("window_sec", ["3600"])[0], 3600),
            ))
            return
        if path == "/public-api/market/realtime":
            self.send_json(public_realtime_market_payload(
                symbol=query.get("symbol", [""])[0],
                limit=clamp_query_int(query.get("limit", ["80"])[0], 80, 200),
                max_age_sec=min(900, max(30, query_int_or(query.get("max_age_sec", ["180"])[0], 180))),
            ))
            return
        if path == "/public-api/radar/realtime-intelligence":
            self.send_json(public_realtime_intelligence_payload(
                limit=clamp_query_int(query.get("limit", ["10"])[0], 10, 30),
                include_backtest=str(query.get("backtest", [""])[0]).strip().lower()
                in {"1", "true", "yes"},
            ))
            return
        if path == "/public-api/coin/context":
            self.send_json(public_coin_context_payload(
                query.get("symbol", [""])[0],
                market_type=query.get("market_type", ["futures"])[0],
                interval=query.get("interval", ["15m"])[0],
                bars=clamp_query_int(query.get("bars", ["96"])[0], 96, 240),
            ))
            return
        if path == "/public-api/market/watchlist":
            self.send_json(public_watchlist_market_payload(query.get("symbols", [""])[0]))
            return
        if path == "/public-api/radar/intelligence":
            self.send_json(public_radar_intelligence_payload(
                window_sec=min(2_592_000, max(3600, query_int_or(query.get("window_sec", ["86400"])[0], 86400))),
                board_limit=clamp_query_int(query.get("limit", ["5"])[0], 5, 12),
                signal_refs=query.get("refs", [""])[0],
            ))
            return
        if path == "/public-api/radar/boards":
            self.send_json(public_radar_boards_payload(
                window_sec=query_int_or(query.get("window_sec", ["3600"])[0], 3600),
                board_limit=clamp_query_int(query.get("limit", ["8"])[0], 8, 20),
            ))
            return
        if path == "/public-api/workstation/radar/momentum":
            self.send_json(public_workstation_radar_momentum_payload(
                window=query.get("window", ["1h"])[0],
                board_limit=clamp_query_int(query.get("limit", ["8"])[0], 8, 20),
            ))
            return
        if path == "/public-api/workstation/funds/open-interest":
            self.send_json(public_workstation_funds_open_interest_payload(
                query.get("symbol", [""])[0],
            ))
            return
        if path == "/public-api/funds/sectors":
            self.send_json(public_funds_sectors_payload(
                window_sec=query_int_or(query.get("window_sec", ["3600"])[0], 3600),
                market_type=query.get("market_type", ["spot"])[0],
            ))
            return
        if path == "/public-api/funds/assets":
            self.send_json(public_funds_assets_payload(
                window_sec=query_int_or(query.get("window_sec", ["3600"])[0], 3600),
                market_type=query.get("market_type", ["spot"])[0],
                search=query.get("q", [""])[0],
                sector=query.get("sector", [""])[0],
                data_status=query.get("data_status", [""])[0],
                sort_key=query.get("sort", ["net_flow_usd"])[0],
                direction=query.get("direction", ["desc"])[0],
                page=clamp_query_int(query.get("page", ["1"])[0], 1, 10000),
                page_size=clamp_query_int(query.get("page_size", ["50"])[0], 50, 100),
            ))
            return
        if path == "/public-api/info/feed":
            self.send_json(public_info_feed_payload(
                source_type=query.get("source_type", [""])[0],
                language=query.get("language", [""])[0],
                importance=query.get("importance", [""])[0],
                symbol=query.get("symbol", [""])[0],
                search=query.get("q", [""])[0],
                page=clamp_query_int(query.get("page", ["1"])[0], 1, 10000),
                page_size=clamp_query_int(query.get("page_size", ["30"])[0], 30, 100),
                window_sec=min(2_592_000, max(3600, query_int_or(query.get("window_sec", ["604800"])[0], 604800))),
            ))
            return
        if path == "/public-api/agents/overview":
            self.send_json(public_agents_overview_payload(
                window_sec=query_int_or(query.get("window_sec", ["14400"])[0], 14400),
            ))
            return
        if path.startswith("/public-api/"):
            self.send_json(api_error("公开接口不存在", code="not_found"), HTTPStatus.NOT_FOUND)
            return
        if not self.require_auth():
            return
        if path == "/api/summary":
            self.send_json(summary_payload())
            return
        if path == "/api/version":
            self.send_json(git_info())
            return
        if path == "/api/server-status":
            self.send_json(server_status_payload())
            return
        if path == "/api/config":
            self.send_json(config_payload())
            return
        if path == "/api/config-backups":
            self.send_json(env_backup_payload())
            return
        if path == "/api/push-preview":
            self.send_json(push_preview_payload())
            return
        if path == "/api/update-check":
            self.send_json(update_check_payload())
            return
        if path == "/api/update-status":
            self.send_json(update_check_status_payload())
            return
        if path == "/api/jobs":
            page = pagination_params(query, default_limit=50, max_limit=200)
            filters = filter_params(query, ("status", "job_type"))
            sort = sort_params(query, ("id", "created_at", "updated_at", "status", "job_type", "returncode"), default="-id")
            time_range = time_range_params(query)
            self.send_json(jobs_payload(
                limit=page["limit"], cursor=page["cursor"], offset=page["offset"],
                status=filters.get("status", ""), job_type=filters.get("job_type", ""),
                sort_field=sort["field"], sort_direction=sort["direction"],
                start_ts=time_range["start_ts"] if time_range.get("applied") else None,
                end_ts=time_range["end_ts"] if time_range.get("applied") else None,
                pagination=page, filters={**filters, "time_range": time_range}, sort=sort,
            ))
            return
        if path == "/api/jobs/stats":
            self.send_json(jobs_stats_payload())
            return
        if path == "/api/jobs/detail":
            self.send_json(job_detail_payload(query_int_or(query.get("id", ["0"])[0], 0)))
            return
        if path == "/api/jobs/report":
            self.send_json(job_report_payload(query_int_or(query.get("id", ["0"])[0], 0)))
            return
        if path == "/api/price-alerts":
            from .ai_assistant import price_alerts_payload
            self.send_json(price_alerts_payload())
            return
        if path == "/api/ai-prompts":
            self.send_json(load_ai_prompts(Settings.load()))
            return
        if path == "/api/signals":
            page = pagination_params(query, default_limit=50, max_limit=200)
            filters = filter_params(query, ("module", "symbol", "status", "severity", "q"))
            if "symbol" in filters:
                symbol_info = normalize_symbol_filter(filters["symbol"])
                filters["symbol"] = symbol_info["symbol"]
                filters["coin"] = symbol_info["coin"]
            sort = sort_params(query, ("id", "ts", "module", "symbol", "status", "severity", "score"), default="-id")
            time_range = time_range_params(query)
            self.send_json(signals_payload(
                limit=page["limit"], cursor=page["cursor"], module=filters.get("module", ""),
                symbol=filters.get("symbol", ""), status=filters.get("status", ""),
                severity=filters.get("severity", ""), q=filters.get("q", ""),
                sort_field=sort["field"], sort_direction=sort["direction"],
                start_ts=time_range["start_ts"] if time_range.get("applied") else None,
                end_ts=time_range["end_ts"] if time_range.get("applied") else None,
                pagination=page, filters={**filters, "time_range": time_range}, sort=sort,
            ))
            return
        if path == "/api/signals/latest":
            self.send_json(signals_latest_payload(
                after_id=max(0, query_int_or(query.get("after_id", ["0"])[0], 0)),
                limit=clamp_query_int(query.get("limit", ["100"])[0], 100, 300),
            ))
            return
        if path == "/api/signals/stats":
            self.send_json(signals_stats_payload(
                window_sec=max(1, query_int_or(query.get("window_sec", ["86400"])[0], 86400)),
            ))
            return
        if path == "/api/signals/detail":
            self.send_json(signal_detail_payload(query_int_or(query.get("id", ["0"])[0], 0)))
            return
        if path == "/api/logs":
            self.send_json(logs_payload(query.get("target", ["main"])[0], int(query.get("lines", ["200"])[0] or 200)))
            return
        if path == "/api/audit":
            self.send_json(web_audit_payload(
                limit=int(query.get("limit", ["200"])[0] or 200),
                result=query.get("result", ["all"])[0],
                search=query.get("search", [""])[0],
            ))
            return
        if path == "/api/problem-state":
            self.send_json(problem_state_payload(limit=int(query.get("limit", ["100"])[0] or 100)))
            return
        if path == "/api/ops-snapshot":
            self.send_json(ops_snapshot_payload())
            return
        self.send_error_json("接口不存在", HTTPStatus.NOT_FOUND, "not_found")

    def do_POST(self) -> None:
        self.request_started_at = time.perf_counter()
        path = urlparse(self.path).path
        if path == "/public-api/telemetry":
            if not self.require_public_rate_limit(path):
                return
            try:
                size = parse_content_length(self.headers.get("Content-Length", "0"))
                if size > 2048:
                    self.send_error_json("请求体太大", HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "payload_too_large")
                    return
                data = self.read_json()
                if not PUBLIC_TELEMETRY.record(str(data.get("event") or "")):
                    self.send_error_json("不支持的遥测事件", HTTPStatus.BAD_REQUEST, "invalid_event")
                    return
                self.send_json({"ok": True, "message": "已记录匿名计数"}, HTTPStatus.ACCEPTED)
            except (ValueError, json.JSONDecodeError):
                self.send_error_json("请求体必须是有效 JSON", HTTPStatus.BAD_REQUEST, "bad_request")
            return
        if path == "/api/auth/login":
            self.handle_auth_login()
            return
        if path == "/api/auth/logout":
            self.handle_auth_logout()
            return
        if not self.require_auth(write=True):
            return
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
                result = restore_env_backup(str(data.get("name", "")))
                result["impact"] = config_change_impact([str(item) for item in result.get("changed", [])])
                if result.get("ok") and result.get("changed"):
                    apply_result = auto_apply_config_changes([str(item) for item in result.get("changed", [])])
                    result["apply"] = apply_result
                    result["impact"] = apply_result.get("impact", result["impact"])
                    result["message"] = apply_result.get("message", result.get("message"))
                self.send_audited_json(path, data, result, started_at=started_at)
                return
            if path == "/api/config-backup-delete":
                self.send_audited_json(path, data, delete_env_backup(str(data.get("name", ""))), started_at=started_at)
                return
            if path == "/api/jobs":
                result = create_job_payload(str(data.get("job_type", "")), {"source": "api/jobs"})
                self.send_audited_json(path, data, result, status=200 if result.get("ok") else HTTPStatus.BAD_REQUEST, started_at=started_at)
                return
            if path == "/api/jobs/cancel":
                result = cancel_job_payload(query_int_or(str(data.get("id") or data.get("job_id") or "0"), 0))
                self.send_audited_json(path, data, result, status=200 if result.get("ok") else HTTPStatus.BAD_REQUEST, started_at=started_at)
                return
            if path == "/api/jobs/rerun":
                result = rerun_job_payload(query_int_or(str(data.get("id") or data.get("job_id") or "0"), 0))
                self.send_audited_json(path, data, result, status=200 if result.get("ok") else HTTPStatus.BAD_REQUEST, started_at=started_at)
                return
            if path == "/api/jobs/cleanup":
                result = cleanup_jobs_payload(retention_days=data.get("retention_days"), limit=data.get("limit"))
                self.send_audited_json(path, data, result, status=200 if result.get("ok") else HTTPStatus.BAD_REQUEST, started_at=started_at)
                return
            if path == "/api/action":
                self.send_audited_json(path, data, run_cli_action(str(data.get("name", ""))), started_at=started_at)
                return
            if path == "/api/problem-state":
                self.send_audited_json(path, data, update_problem_state_payload(data), started_at=started_at)
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
                result = create_price_alert_from_payload(data) if action == "create" else mutate_price_alert_from_payload(data)
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
                    result = save_ai_prompts({
                        "assistant_prompt": data.get("assistant_prompt", ""),
                        "analyst_prompt": data.get("analyst_prompt", ""),
                    }, settings)
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
    settings = Settings.load()
    mode = auth_mode(settings)
    token = admin_token or os.getenv("WEB_ADMIN_TOKEN", "")
    if mode == "token" and not is_loopback_host(host) and not token:
        print("web: refused to bind non-loopback host in token mode without WEB_ADMIN_TOKEN", file=sys.stderr)
        return 2
    server = ThreadingHTTPServer((host, int(port)), WebHandler)
    server.settings = settings  # type: ignore[attr-defined]
    server.admin_token = token  # type: ignore[attr-defined]
    if mode == "password":
        auth_note = "password configured" if settings.web_admin_password_hash and settings.web_session_secret else "password not configured"
    else:
        auth_note = "token enabled" if token else "token disabled"
    print(f"web: listening on http://{host}:{port} (auth {auth_note})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nweb: stopped")
    finally:
        server.server_close()
    return 0
