from __future__ import annotations

import copy
import hashlib
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
    public_signal_detail_payload,
    public_signal_stats_payload,
    public_signals_payload,
)
from .web_services.signals import (
    enhance_signal_items,
    signal_detail_view,
    signal_stats_display,
)


MAIN_SERVICE = os.getenv("SERVICE_NAME", "paopao-radar")
STRUCTURE_SERVICE = os.getenv("STRUCTURE_SERVICE_NAME", "paopao-structure")
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
    ConfigField("SIGNAL_EVENTS_DB_FILE", "信号推送数据库文件", "AI 助手", help="默认 signals.db，存放所有 Telegram 推送结果的结构化记录，供 Web 信号推送页查询。一般不需要修改。"),
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
    ConfigField("WEB_AUTH_MODE", "后台认证模式", "Web 控制台", help="默认 password；token 仅用于旧模式紧急回滚。"),
    ConfigField("WEB_ADMIN_USERNAME", "后台用户名", "Web 控制台"),
    ConfigField("WEB_SESSION_TTL_SEC", "登录有效期秒数", "Web 控制台", kind="int", minimum=300, maximum=604800),
    ConfigField("WEB_AUTH_COOKIE_NAME", "登录 Cookie 名称", "Web 控制台"),
    ConfigField("WEB_AUTH_MAX_FAILURES", "登录失败锁定次数", "Web 控制台", kind="int", minimum=1, maximum=20),
    ConfigField("WEB_AUTH_LOCKOUT_SEC", "登录锁定秒数", "Web 控制台", kind="int", minimum=60, maximum=86400),
    ConfigField("WEB_AUTH_FAILURE_WINDOW_SEC", "失败计数窗口秒数", "Web 控制台", kind="int", minimum=60, maximum=86400),
    ConfigField("WEB_AUTH_AUDIT_LIMIT", "登录审计保留条数", "Web 控制台", kind="int", minimum=50, maximum=5000),
    ConfigField("WEB_SESSION_REFRESH_THRESHOLD_RATIO", "会话续期阈值比例", "Web 控制台", kind="float", minimum=0.1, maximum=0.9),
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
    "WEB_ADMIN_TOKEN": "旧 token 认证模式访问令牌，仅用于 WEB_AUTH_MODE=token 紧急回滚。",
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
    invalidate_runtime_cache("dashboard:")
    invalidate_runtime_cache("ops:")
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
    if path == "/api/jobs":
        job_type = str(data.get("job_type", ""))
        job_id = data.get("id") or data.get("job_id")
        return {
            "action": "创建后台任务",
            "target": job_type,
            "details": {"job_type": job_type, "job_id": job_id},
        }
    if path == "/api/jobs/cancel":
        job_id = data.get("id") or data.get("job_id")
        return {
            "action": "取消后台任务",
            "target": str(job_id or ""),
            "details": {"job_id": job_id},
        }
    if path == "/api/jobs/rerun":
        job_id = data.get("id") or data.get("job_id")
        return {
            "action": "重跑后台任务",
            "target": str(job_id or ""),
            "details": {"job_id": job_id},
        }
    if path == "/api/jobs/cleanup":
        return {
            "action": "清理后台任务",
            "target": "jobs",
            "details": {
                "retention_days": data.get("retention_days"),
                "limit": data.get("limit"),
            },
        }
    if path == "/api/auth/login":
        return {"action": "后台登录", "target": str(data.get("username") or ""), "details": {"username": str(data.get("username") or "")}}
    if path == "/api/auth/logout":
        return {"action": "退出后台登录", "target": "admin", "details": {}}
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
    if path == "/api/problem-state":
        status = str(data.get("status") or "").strip()
        fingerprint = str(data.get("fingerprint") or "").strip()
        return {"action": "标记诊断问题状态", "target": fingerprint, "details": {"status": status, "fingerprint": fingerprint}}
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


def problem_state_path(data_dir: Path | None = None) -> Path:
    base_dir = data_dir or Settings.load().data_dir
    return base_dir / PROBLEM_STATE_FILE


def problem_state_status_label(status: str) -> str:
    value = str(status or "open")
    if value == "acknowledged":
        return "已确认"
    if value == "resolved":
        return "已解决观察中"
    return "未确认"


def problem_fingerprint(*parts: Any) -> str:
    text = "|".join(str(part or "").strip().lower() for part in parts)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def load_problem_state(data_dir: Path | None = None) -> dict[str, Any]:
    store = JsonStore((data_dir or Settings.load().data_dir))
    path = problem_state_path(data_dir)
    payload = store.load(path, {"records": []})
    records = payload.get("records", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    clean_records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in records if isinstance(records, list) else []:
        if not isinstance(item, dict):
            continue
        fingerprint = str(item.get("fingerprint") or "").strip()
        if not fingerprint or fingerprint in seen:
            continue
        status = str(item.get("status") or "open")
        if status not in {"acknowledged", "resolved"}:
            continue
        seen.add(fingerprint)
        clean_records.append(
            {
                "fingerprint": fingerprint,
                "status": status,
                "label": problem_state_status_label(status),
                "title": str(item.get("title") or "")[:160],
                "key": str(item.get("key") or "")[:80],
                "target": str(item.get("target") or "")[:80],
                "note": str(item.get("note") or "")[:240],
                "updated_at": str(item.get("updated_at") or ""),
            }
        )
    return {
        "ok": True,
        "path": str(path),
        "records": clean_records[:PROBLEM_STATE_LIMIT],
        "total": len(clean_records),
    }


def problem_state_payload(*, data_dir: Path | None = None, limit: int = 100) -> dict[str, Any]:
    payload = load_problem_state(data_dir)
    records = payload.get("records", []) if isinstance(payload.get("records"), list) else []
    return {
        **payload,
        "records": records[: max(1, min(int(limit or 100), PROBLEM_STATE_LIMIT))],
        "message": "已读取问题处理状态",
    }


def save_problem_state_records(records: list[dict[str, Any]], data_dir: Path | None = None) -> None:
    store = JsonStore((data_dir or Settings.load().data_dir))
    path = problem_state_path(data_dir)
    store.save(
        path,
        {
            "updated_at": now_text(),
            "records": records[:PROBLEM_STATE_LIMIT],
        },
    )


def update_problem_state_payload(data: dict[str, Any], *, data_dir: Path | None = None) -> dict[str, Any]:
    fingerprint = str(data.get("fingerprint") or "").strip()
    if not fingerprint:
        return {"ok": False, "error": "缺少问题编号", "message": "缺少问题编号"}
    status = str(data.get("status") or "acknowledged").strip().lower()
    if status not in {"acknowledged", "resolved", "open", "clear"}:
        return {"ok": False, "error": "不支持的问题状态", "message": "不支持的问题状态"}
    state = load_problem_state(data_dir)
    records = state.get("records", []) if isinstance(state.get("records"), list) else []
    kept = [item for item in records if isinstance(item, dict) and item.get("fingerprint") != fingerprint]
    if status in {"open", "clear"}:
        save_problem_state_records(kept, data_dir)
        return {
            "ok": True,
            "fingerprint": fingerprint,
            "status": "open",
            "label": problem_state_status_label("open"),
            "message": "已清除这个问题的处理标记",
        }
    record = {
        "fingerprint": fingerprint,
        "status": status,
        "label": problem_state_status_label(status),
        "title": redact_sensitive_text(str(data.get("title") or ""))[:160],
        "key": str(data.get("key") or "")[:80],
        "target": str(data.get("target") or "")[:80],
        "note": redact_sensitive_text(str(data.get("note") or ""))[:240],
        "updated_at": now_text(),
    }
    save_problem_state_records([record, *kept], data_dir)
    return {
        "ok": True,
        **record,
        "message": f"已标记为：{record['label']}",
    }


def enrich_problem_action_plan(action_plan: list[dict[str, Any]], problem_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    records = problem_state.get("records", []) if isinstance(problem_state, dict) and isinstance(problem_state.get("records"), list) else []
    state_by_fingerprint = {
        str(item.get("fingerprint")): item
        for item in records
        if isinstance(item, dict) and item.get("fingerprint")
    }
    enriched: list[dict[str, Any]] = []
    for action in action_plan:
        item = dict(action)
        fingerprint = problem_fingerprint(
            item.get("key"),
            item.get("severity"),
            item.get("target"),
            item.get("log_target"),
            item.get("action_id"),
            item.get("title"),
        )
        state = state_by_fingerprint.get(fingerprint, {})
        state_status = str(state.get("status") or "open") if isinstance(state, dict) else "open"
        item.update(
            {
                "fingerprint": fingerprint,
                "state_status": state_status,
                "state_label": problem_state_status_label(state_status),
                "state_updated_at": str(state.get("updated_at") or "") if isinstance(state, dict) else "",
                "state_note": str(state.get("note") or "") if isinstance(state, dict) else "",
            }
        )
        enriched.append(item)
    return enriched


def review_problem_state(action_plan: list[dict[str, Any]], problem_state: dict[str, Any] | None = None) -> dict[str, Any]:
    records = problem_state.get("records", []) if isinstance(problem_state, dict) and isinstance(problem_state.get("records"), list) else []
    active_fingerprints = {
        str(item.get("fingerprint") or "")
        for item in action_plan
        if isinstance(item, dict) and item.get("key") != "observe" and item.get("fingerprint")
    }
    reviewed_records: list[dict[str, Any]] = []
    counts = {
        "tracked_total": 0,
        "tracked_active": 0,
        "tracked_missing": 0,
        "resolved_active": 0,
        "resolved_missing": 0,
        "acknowledged_active": 0,
        "acknowledged_missing": 0,
    }
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        fingerprint = str(record.get("fingerprint") or "")
        if not fingerprint:
            continue
        status = str(record.get("status") or "open")
        active = fingerprint in active_fingerprints
        counts["tracked_total"] += 1
        counts["tracked_active" if active else "tracked_missing"] += 1
        if status == "resolved" and active:
            review_status = "still_active"
            review_label = "仍然存在"
            counts["resolved_active"] += 1
        elif status == "resolved":
            review_status = "missing_after_resolved"
            review_label = "已消失待复查"
            counts["resolved_missing"] += 1
        elif status == "acknowledged" and active:
            review_status = "acknowledged_active"
            review_label = "仍需处理"
            counts["acknowledged_active"] += 1
        elif status == "acknowledged":
            review_status = "missing_after_acknowledged"
            review_label = "已消失待确认"
            counts["acknowledged_missing"] += 1
        else:
            review_status = "unknown"
            review_label = "状态未知"
        reviewed = dict(record)
        reviewed.update(
            {
                "active": active,
                "review_status": review_status,
                "review_label": review_label,
            }
        )
        reviewed_records.append(reviewed)

    if counts["resolved_active"]:
        summary = f"{counts['resolved_active']} 个已标记解决的问题仍然存在，建议继续处理后再 stable-check。"
        status = "attention"
    elif counts["resolved_missing"]:
        summary = f"{counts['resolved_missing']} 个已标记解决的问题当前已消失，建议执行 stable-check 复查。"
        status = "review"
    elif counts["acknowledged_active"]:
        summary = f"{counts['acknowledged_active']} 个已确认问题仍在处理清单里。"
        status = "tracking"
    elif counts["tracked_total"]:
        summary = "历史标记的问题当前没有出现在处理清单里，可继续观察。"
        status = "quiet"
    else:
        summary = "暂无历史问题处理状态。"
        status = "empty"

    return {
        "status": status,
        "summary": summary,
        "counts": counts,
        "records": reviewed_records[:PROBLEM_STATE_LIMIT],
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
    invalidate_runtime_cache("dashboard:")
    invalidate_runtime_cache("ops:")
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
    invalidate_runtime_cache("dashboard:")
    if name in {"stable-check", "doctor", "readiness", "cleanup"}:
        invalidate_runtime_cache("stable:")
    if name in LONG_ACTION_JOB_TYPES:
        result = create_job_payload(name, {"source": "api/action", "name": name})
        result["label"] = action["label"]
        result["job_created"] = bool(result.get("ok"))
        return result
    result = run_subprocess(action["argv"], timeout=int(action.get("timeout", 30)), use_python=True)
    ok_returncodes = action.get("ok_returncodes")
    if isinstance(ok_returncodes, list):
        result["ok"] = int(result.get("returncode", 0) or 0) in {int(code) for code in ok_returncodes}
    result["label"] = action["label"]
    invalidate_runtime_cache("dashboard:")
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
    invalidate_runtime_cache("dashboard:")
    try:
        return run_subprocess(sudo_systemctl_command(service, action), timeout=30)
    finally:
        invalidate_runtime_cache("dashboard:")


def schedule_service_action(name: str, *, delay_sec: float = 1.2) -> dict[str, Any]:
    item = SERVICE_ACTIONS.get(name)
    if item is None:
        return {"ok": False, "returncode": 2, "stderr": "未知服务动作", "stdout": ""}
    service, action = item
    invalidate_runtime_cache("dashboard:")

    def worker() -> None:
        time.sleep(delay_sec)
        try:
            result = run_subprocess(sudo_systemctl_command(service, action), timeout=30)
            sys.stderr.write(
                f"[web] delayed {action} {service}: ok={result.get('ok')} returncode={result.get('returncode')}\n"
            )
        finally:
            invalidate_runtime_cache("dashboard:")

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
        add_service("restart-web", "Web 控制台地址、端口或认证配置变更后需要重启 Web 服务。", scheduled=True)

    warnings: list[str] = []
    if sensitive_keys:
        labels = "、".join(EDITABLE_CONFIG.get(key, ConfigField(key, key, "")).label for key in sensitive_keys)
        warnings.append(f"包含敏感配置：{labels}。审计和诊断只记录字段名，不记录具体值。")
    if changed_set & WEB_CONFIG_KEYS:
        warnings.append("Web 入口或认证配置会在保存返回后短暂重启；如果页面断开，稍后重新打开后台登录。")
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


def _load_service_status(service: str) -> dict[str, Any]:
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


def service_status(service: str) -> dict[str, Any]:
    cached = runtime_cache_get_or_set(
        f"dashboard:service:{service}",
        DASHBOARD_SERVICE_CACHE_TTL_SEC,
        lambda: _load_service_status(service),
    )
    return dict(cached)


def _load_git_info() -> dict[str, str]:
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


def git_info() -> dict[str, str]:
    cached = runtime_cache_get_or_set("dashboard:git", DASHBOARD_GIT_CACHE_TTL_SEC, _load_git_info)
    return dict(cached)


def percent_value(used: float | int | None, total: float | int | None) -> float | None:
    try:
        used_float = float(used if used is not None else 0)
        total_float = float(total if total is not None else 0)
    except (TypeError, ValueError):
        return None
    if total_float <= 0:
        return None
    return round(max(0.0, min(100.0, used_float / total_float * 100.0)), 1)


def read_proc_cpu_totals(path: Path = Path("/proc/stat")) -> tuple[int, int] | None:
    try:
        first = path.read_text(encoding="utf-8", errors="ignore").splitlines()[0].split()
    except Exception:
        return None
    if not first or first[0] != "cpu":
        return None
    try:
        values = [int(float(item)) for item in first[1:]]
    except ValueError:
        return None
    if len(values) < 4:
        return None
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    total = sum(values)
    return total, idle


def cpu_usage_percent() -> float | None:
    global _CPU_LAST_SAMPLE
    sample = read_proc_cpu_totals()
    if sample is None:
        return None
    with _CPU_SAMPLE_LOCK:
        previous = _CPU_LAST_SAMPLE
        _CPU_LAST_SAMPLE = sample
    if previous is None:
        return None
    total_delta = sample[0] - previous[0]
    idle_delta = sample[1] - previous[1]
    if total_delta <= 0:
        return None
    return round(max(0.0, min(100.0, (1.0 - idle_delta / total_delta) * 100.0)), 1)


def load_average_payload(cpu_count: int) -> dict[str, Any]:
    try:
        load1, load5, load15 = os.getloadavg()
    except (AttributeError, OSError):
        return {"available": False, "load1": None, "load5": None, "load15": None, "load1_pct": None}
    load1_pct = round(max(0.0, load1 / max(1, cpu_count) * 100.0), 1)
    return {
        "available": True,
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "load1_pct": load1_pct,
    }


def read_meminfo(path: Path = Path("/proc/meminfo")) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return result
    for line in lines:
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if not parts:
            continue
        try:
            result[key] = int(parts[0]) * 1024
        except ValueError:
            continue
    return result


def memory_payload() -> dict[str, Any]:
    meminfo = read_meminfo()
    total = meminfo.get("MemTotal")
    available = meminfo.get("MemAvailable")
    free = meminfo.get("MemFree")
    if total is None:
        return {"available": False, "total": None, "used": None, "free": None, "percent": None, "swap": {}}
    free_value = available if available is not None else free
    used = max(0, total - int(free_value or 0))
    swap_total = meminfo.get("SwapTotal") or 0
    swap_free = meminfo.get("SwapFree") or 0
    swap_used = max(0, swap_total - swap_free)
    return {
        "available": True,
        "total": total,
        "used": used,
        "free": int(free_value or 0),
        "percent": percent_value(used, total),
        "swap": {
            "total": swap_total,
            "used": swap_used,
            "free": swap_free,
            "percent": percent_value(swap_used, swap_total),
        },
    }


def disk_item(path: Path, label: str) -> dict[str, Any]:
    try:
        usage = shutil.disk_usage(path)
        used = usage.total - usage.free
        return {
            "label": label,
            "path": str(path),
            "available": True,
            "total": usage.total,
            "used": used,
            "free": usage.free,
            "percent": percent_value(used, usage.total),
        }
    except Exception as exc:
        return {"label": label, "path": str(path), "available": False, "error": f"{type(exc).__name__}: {exc}"}


def uptime_seconds(path: Path = Path("/proc/uptime")) -> float | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore").split()[0]
        return round(float(raw), 1)
    except Exception:
        return None


def server_status_payload() -> dict[str, Any]:
    cpu_count = os.cpu_count() or 1
    load = load_average_payload(cpu_count)
    cpu_percent = cpu_usage_percent()
    if cpu_percent is None and load.get("load1_pct") is not None:
        cpu_percent = float(load["load1_pct"])
    root_path = Path(os.path.abspath(os.sep))
    disks = [disk_item(BASE_DIR, "项目目录")]
    if root_path != BASE_DIR:
        disks.append(disk_item(root_path, "系统根目录"))
    return {
        "updated_at": now_text(),
        "host": {
            "name": platform.node() or "unknown",
            "system": platform.system() or "unknown",
            "release": platform.release() or "",
            "platform": platform.platform() or "",
            "python": platform.python_version(),
            "base_dir": str(BASE_DIR),
            "uptime_sec": uptime_seconds(),
        },
        "cpu": {
            "cores": cpu_count,
            "percent": cpu_percent,
            "load": load,
        },
        "memory": memory_payload(),
        "disks": disks,
    }


def update_check_payload() -> dict[str, Any]:
    return update_check_status_payload()


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


def clamp_query_int(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value if value not in {None, ""} else default)
    except (TypeError, ValueError):
        number = default
    return max(1, min(int(maximum), number))


def query_int_or(value: Any, default: int = 0) -> int:
    try:
        return int(value if value not in {None, ""} else default)
    except (TypeError, ValueError):
        return default


def normalize_signal_symbol(value: str) -> str:
    return normalize_symbol_filter(value).get("symbol", "")


def signal_store_for_settings(settings: Settings | None = None):
    from .signal_store import SignalEventStore

    loaded = settings or Settings.load()
    return SignalEventStore(loaded.signal_events_db_path)


def signals_payload(
    *,
    limit: int = 50,
    cursor: int | None = None,
    module: str = "",
    symbol: str = "",
    status: str = "",
    severity: str = "",
    q: str = "",
    sort_field: str = "id",
    sort_direction: str = "desc",
    start_ts: int | None = None,
    end_ts: int | None = None,
    pagination: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    sort: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    store = signal_store_for_settings(loaded)
    normalized_symbol = normalize_signal_symbol(symbol)
    result = store.list_signals(
        limit=clamp_query_int(limit, 50, 200),
        cursor=cursor,
        module=str(module or "").strip().lower(),
        symbol=normalized_symbol,
        status=str(status or "").strip().lower(),
        severity=str(severity or "").strip().lower(),
        sort_field=sort_field,
        sort_direction=sort_direction,
        start_ts=start_ts,
        end_ts=end_ts,
        q=q,
        compact=True,
    )
    items = enhance_signal_items(result["items"])
    default_filters = {
        "module": str(module or "").strip().lower(),
        "symbol": normalized_symbol,
        "status": str(status or "").strip().lower(),
        "severity": str(severity or "").strip().lower(),
        "q": str(q or "").strip()[:80],
    }
    return {
        "ok": True,
        "data": {"items": items},
        "items": items,
        "next_cursor": result["next_cursor"],
        "count": result["count"],
        "pagination": pagination,
        "filters": filters if filters is not None else default_filters,
        "sort": sort if sort is not None else {
            "field": sort_field,
            "direction": sort_direction,
            "raw": f"{'-' if sort_direction == 'desc' else ''}{sort_field}",
        },
        "db_file": str(loaded.signal_events_db_path),
        "legacy_signal_events_file": str(loaded.signal_events_path),
        "legacy_signal_events_exists": loaded.signal_events_path.exists(),
        "message": "已读取信号推送记录",
    }


def signals_latest_payload(
    *,
    after_id: int = 0,
    limit: int = 100,
    settings: Settings | None = None,
) -> dict[str, Any]:
    store = signal_store_for_settings(settings)
    items = enhance_signal_items(
        store.latest_after(
            after_id=max(0, int(after_id or 0)),
            limit=clamp_query_int(limit, 100, 300),
            compact=True,
        )
    )
    return {
        "ok": True,
        "items": items,
        "count": len(items),
        "message": "已读取最新信号推送记录",
    }


def signals_stats_payload(*, window_sec: int = 86400, settings: Settings | None = None) -> dict[str, Any]:
    store = signal_store_for_settings(settings)
    stats = store.stats_with_latest(window_sec=max(1, int(window_sec or 86400)))
    latest_sent = enhance_signal_items(stats.pop("latest_sent", []))
    latest_failed = enhance_signal_items(stats.pop("latest_failed", []))
    stats.pop("latest", None)
    latest_by_module = {
        str(module): enhance_signal_items(items)
        for module, items in stats.pop("latest_by_module", {}).items()
    }
    return {
        "ok": True,
        **stats,
        **signal_stats_display(stats),
        "latest_sent": latest_sent,
        "latest_failed": latest_failed,
        "latest_by_module": latest_by_module,
        "message": "已读取信号推送统计",
    }


def signal_detail_payload(signal_id: int, *, settings: Settings | None = None) -> dict[str, Any]:
    store = signal_store_for_settings(settings)
    with store.connect() as conn:
        item = store.signal_detail(int(signal_id or 0), conn=conn)
        if not item:
            return {"ok": False, "item": None, "detail": None, "code": "not_found", "message": "信号记录不存在"}
        related = []
        if item.get("symbol"):
            related = [
                related_item
                for related_item in store.symbol_timeline(
                    str(item.get("symbol") or ""),
                    limit=11,
                    compact=True,
                    conn=conn,
                )
                if int(related_item.get("id") or 0) != int(item.get("id") or 0)
            ][:10]
    enhanced = enhance_signal_items([item])[0]
    return {
        "ok": True,
        "item": enhanced,
        "detail": signal_detail_view(item, related),
        "message": "已读取信号详情",
    }


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
        "web": redacted.get("web"),
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
        "jobs": jobs_stats_payload(),
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
TRANSIENT_LOG_RE = re.compile(
    r"(getUpdates failed ReadTimeout|Read timed out|read timeout=\d+|\b[A-Za-z][A-Za-z0-9_.:-]{2,}\s*:\s*(?:ReadTimeout|ConnectTimeout|Timeout)\b)",
    re.I,
)
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
    jobs = snapshot.get("jobs", {}) if isinstance(snapshot.get("jobs"), dict) else {}
    recent_failed_jobs = jobs.get("recent_failed", []) if isinstance(jobs.get("recent_failed"), list) else []
    release_trend = snapshot.get("release_trend", {}) if isinstance(snapshot.get("release_trend"), dict) else {}
    release_trend_status = str(release_trend.get("status") or "")
    problem_center = snapshot.get("problem_center", {}) if isinstance(snapshot.get("problem_center"), dict) else {}
    problem_counts = problem_center.get("counts", {}) if isinstance(problem_center.get("counts"), dict) else {}
    deployment = snapshot.get("deployment_acceptance", {}) if isinstance(snapshot.get("deployment_acceptance"), dict) else {}
    deployment_status = str(deployment.get("status") or "")
    jobs = snapshot.get("jobs", {}) if isinstance(snapshot.get("jobs"), dict) else {}
    recent_failed_jobs = jobs.get("recent_failed", []) if isinstance(jobs.get("recent_failed"), list) else []
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
    if recent_failed_jobs:
        recommendations.append("后台任务存在失败或超时记录：先打开任务中心查看错误摘要和 stderr_tail，再决定是否重跑任务。")
        if any(str(item.get("job_type") or "") == "stable-check" for item in recent_failed_jobs if isinstance(item, dict)):
            recommendations.append("stable-check 后台任务失败时，先复制任务报告，再对照诊断报告处理清单排查。")
        if any(str(item.get("job_type") or "") == "update-check" for item in recent_failed_jobs if isinstance(item, dict)):
            recommendations.append("update-check 失败通常和网络或 GitHub 访问有关；真实更新仍建议在服务器执行 paopao update --yes。")
    if transient_total >= 10 and not log_error_total:
        recommendations.append(f"近期 Telegram 网络超时 {transient_total} 次。服务会自动重试；如果 AI Bot 明显不回复，再检查服务器到 api.telegram.org 的网络。")
    if release_trend_status == "regressed":
        recommendations.append("长期运行趋势发生回退：这次从候选状态掉到需要处理。优先打开诊断报告的问题中心和日志中心，按阻断项处理后重新 stable-check。")
    elif release_trend_status == "worse":
        recommendations.append("长期运行趋势变差：分数或候选状态低于上次。先看趋势卡片的分数变化，再处理本次新增的警告或阻断项。")
    resolved_active = int(problem_counts.get("state_resolved_active", 0) or 0)
    resolved_missing = int(problem_counts.get("state_resolved_missing", 0) or 0)
    if resolved_active:
        recommendations.append(f"有 {resolved_active} 个已标记解决的问题仍然存在：继续按处理清单排查，处理后再执行 stable-check。")
    elif resolved_missing:
        recommendations.append(f"有 {resolved_missing} 个已标记解决的问题当前已消失：建议执行 stable-check 复查并保存新的验收记录。")
    if deployment_status == "blocked":
        recommendations.append("服务器部署验收未通过：先处理部署验收里的阻断项，再重新执行 stable-check。")
    elif deployment_status == "attention":
        recommendations.append("服务器部署验收存在观察项：确认 Web 入口、服务、配置、日志和审计是否影响真实运行。")
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
    jobs = snapshot.get("jobs", {}) if isinstance(snapshot.get("jobs"), dict) else {}
    recent_failed_jobs = jobs.get("recent_failed", []) if isinstance(jobs.get("recent_failed"), list) else []
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

    if recent_failed_jobs:
        timeout_count = sum(1 for item in recent_failed_jobs if isinstance(item, dict) and str(item.get("status") or "") == "timeout")
        for item in recent_failed_jobs[:6]:
            if not isinstance(item, dict):
                continue
            job_type = str(item.get("job_type") or "unknown")
            status = str(item.get("status") or "")
            severity = "warning"
            if job_type == "stable-check" or (status == "timeout" and timeout_count >= 3):
                severity = "critical"
            detail = str(item.get("error_summary") or item.get("error") or "任务失败但没有可读错误摘要。")
            add(
                severity=severity,
                module="后台任务",
                title=f"{job_type} 任务{status or '失败'}",
                detail=detail,
                count=1,
                target="jobs",
                action="打开任务中心查看任务详情、stdout_tail 和 stderr_tail；确认后可重跑同类任务。",
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


def build_problem_center(snapshot: dict[str, Any], problem_state: dict[str, Any] | None = None) -> dict[str, Any]:
    issues = snapshot.get("issues", []) if isinstance(snapshot.get("issues"), list) else []
    stability = snapshot.get("stability", {}) if isinstance(snapshot.get("stability"), dict) else {}
    health = snapshot.get("health", []) if isinstance(snapshot.get("health"), list) else []
    recent_errors = snapshot.get("recent_errors", []) if isinstance(snapshot.get("recent_errors"), list) else []
    audit = snapshot.get("audit", {}) if isinstance(snapshot.get("audit"), dict) else {}
    failed_audit = audit.get("failed_recent", []) if isinstance(audit.get("failed_recent"), list) else []
    logs = snapshot.get("log_errors", {}) if isinstance(snapshot.get("log_errors"), dict) else {}
    jobs = snapshot.get("jobs", {}) if isinstance(snapshot.get("jobs"), dict) else {}
    recent_failed_jobs = jobs.get("recent_failed", []) if isinstance(jobs.get("recent_failed"), list) else []
    release_trend = snapshot.get("release_trend", {}) if isinstance(snapshot.get("release_trend"), dict) else {}
    release_trend_status = str(release_trend.get("status") or "")
    trend_regressed = release_trend_status == "regressed"
    trend_worse = release_trend_status == "worse"

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
    current_blocking = bool(critical_count or bad_health_count or stability_status == "blocked")
    current_attention = bool(warning_count or warn_health_count or log_error_total or failed_audit or recent_failed_jobs or stability_status == "attention")
    actionable_trend_worse = bool(trend_worse and current_attention)
    pure_trend_worse = bool(trend_worse and not current_attention and not current_blocking)

    if current_blocking or trend_regressed:
        status = "blocked"
        label = "需要优先处理"
        summary = "存在阻断项或严重问题，建议先处理后再继续观察。"
        primary_action = "先处理问题中心里的严重项；服务异常优先重启对应服务，配置缺失优先补齐配置。"
        if trend_regressed and not current_blocking:
            primary_action = "长期运行趋势发生回退，先查看趋势卡片和验收历史，再按本次新增阻断项重新验收。"
    elif current_attention or actionable_trend_worse:
        status = "attention"
        label = "需要关注"
        summary = "系统可运行，但存在警告、错误日志或失败操作，建议确认是否影响实际推送。"
        primary_action = "先看建议动作和相关日志；如果功能正常，可以继续观察并等待下一次 stable-check。"
        if trend_worse and not (warning_count or warn_health_count or log_error_total or failed_audit or stability_status == "attention"):
            primary_action = "长期运行趋势变差，先查看趋势卡片的分数变化和验收历史，确认是否需要处理。"
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
    if recent_failed_jobs:
        next_steps.append("打开任务中心，查看失败/超时任务的错误摘要和 stderr_tail；需要时重跑同类任务。")
    if transient_total >= 10 and not log_error_total:
        next_steps.append("Telegram/API 网络超时偏多但可自动重试；如果 Bot 明显不回复，再检查服务器网络。")
    if trend_regressed:
        next_steps.append("长期运行趋势发生回退：优先查看趋势卡片、验收历史和本次阻断项，处理后重新执行 stable-check。")
    elif actionable_trend_worse:
        next_steps.append("长期运行趋势变差：对比本次和上次分数变化，确认新增警告或阻断项是否影响真实推送。")
    if pure_trend_worse:
        next_steps.append("长期趋势只是历史分数对比变差；当前没有真实错误或失败操作，可以再执行一次 stable-check 保存健康记录。")
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

    if recent_failed_jobs:
        first_job = recent_failed_jobs[0] if isinstance(recent_failed_jobs[0], dict) else {}
        add_action(
            "failed-jobs",
            severity="critical" if str(first_job.get("job_type") or "") == "stable-check" else "warning",
            title="查看失败或超时任务",
            detail=str(first_job.get("error_summary") or "后台任务出现失败或超时。先看任务详情里的错误摘要、stdout_tail 和 stderr_tail。"),
            target="jobs",
            button="打开任务中心",
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

    if trend_regressed or actionable_trend_worse:
        add_action(
            "release-trend",
            severity="critical" if trend_regressed else "warning",
            title="处理长期运行趋势回退" if trend_regressed else "确认长期运行趋势变差",
            detail=str(release_trend.get("action") or release_trend.get("summary") or "查看趋势卡片和验收历史，确认分数下降原因。"),
            target="report",
            button="查看趋势详情",
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

    action_plan = enrich_problem_action_plan(action_plan, problem_state)
    actionable_items = [item for item in action_plan if item.get("key") != "observe"]
    acknowledged_count = sum(1 for item in actionable_items if item.get("state_status") == "acknowledged")
    resolved_count = sum(1 for item in actionable_items if item.get("state_status") == "resolved")
    open_count = sum(1 for item in actionable_items if item.get("state_status") == "open")
    state_review = review_problem_state(action_plan, problem_state)
    state_counts = state_review.get("counts", {}) if isinstance(state_review.get("counts"), dict) else {}
    state_records = state_review.get("records", []) if isinstance(state_review.get("records"), list) else []

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
            "failed_jobs": len(recent_failed_jobs),
            "log_errors": log_error_total,
            "transient_timeouts": transient_total,
            "stability_fail": int(stability.get("fail_count", 0) or 0),
            "stability_warn": int(stability.get("warn_count", 0) or 0),
            "release_trend_regressed": 1 if trend_regressed else 0,
            "release_trend_worse": 1 if trend_worse else 0,
            "action_open": open_count,
            "action_acknowledged": acknowledged_count,
            "action_resolved": resolved_count,
            "state_tracked_active": int(state_counts.get("tracked_active", 0) or 0),
            "state_tracked_missing": int(state_counts.get("tracked_missing", 0) or 0),
            "state_resolved_active": int(state_counts.get("resolved_active", 0) or 0),
            "state_resolved_missing": int(state_counts.get("resolved_missing", 0) or 0),
        },
        "modules": modules,
        "next_steps": next_steps,
        "action_plan": action_plan,
        "problem_state": {
            "records": state_records[:8],
            "total": int(problem_state.get("total", len(state_records)) if isinstance(problem_state, dict) else len(state_records)),
            "review": state_review,
        },
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


def version_tuple(value: str) -> tuple[int, int, int]:
    text = str(value or "").strip()
    match = SEMVER_VERSION_RE.match(text)
    if not match:
        return (0, 0, 0)
    parts = re.findall(r"\d+", text, flags=0)[:3]
    while len(parts) < 3:
        parts.append("0")
    return tuple(int(part) for part in parts)


def release_closure_stage_index(version: str) -> int:
    current = version_tuple(version)
    selected = 0
    for idx, item in enumerate(RELEASE_CLOSURE_STAGES):
        if current >= version_tuple(item["version"]):
            selected = idx
    return selected


def build_release_closure_plan(
    *,
    version: str,
    readiness_status: str,
    score: int,
    fail_count: int,
    warn_count: int,
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    current_index = release_closure_stage_index(version)
    final_stage = current_index >= len(RELEASE_CLOSURE_STAGES) - 1
    blocking = [item for item in checks if item.get("status") == "fail"]
    warnings = [item for item in checks if item.get("status") == "warn"]

    if readiness_status == "blocked":
        current_stage_status = "blocked"
        summary = "v1.50.0 发布门禁仍有阻断项，先处理后重新 stable-check。" if final_stage else "当前仍有阻断项，先完成 v1.47.0 稳定性收口。"
    elif readiness_status == "candidate":
        current_stage_status = "active"
        summary = "v1.50.0 发布前仍有观察项，确认不影响真实推送后再保存验收历史。" if final_stage else "当前可运行但仍有观察项，继续按处理清单收口。"
    elif final_stage:
        current_stage_status = "complete"
        summary = "当前已经进入 v1.50.0 完整稳定版发布阶段。"
    else:
        current_stage_status = "ready_to_advance"
        summary = "当前阶段门禁已满足，可以推进到下一阶段。"

    stage_items: list[dict[str, Any]] = []
    for idx, item in enumerate(RELEASE_CLOSURE_STAGES):
        if idx < current_index:
            stage_status = "done"
        elif idx == current_index:
            stage_status = current_stage_status
        else:
            stage_status = "pending"
        stage_items.append({**item, "status": stage_status})

    current_stage = stage_items[current_index] if stage_items else {}
    next_stage = stage_items[current_index + 1] if current_index + 1 < len(stage_items) else None
    return {
        "target_version": RELEASE_CLOSURE_TARGET,
        "mode": "v1 完整稳定版发布" if final_stage else "功能冻结收口",
        "rule": RELEASE_CLOSURE_RULE,
        "maintenance_policy": RELEASE_MAINTENANCE_POLICY,
        "current_stage": current_stage,
        "next_stage": next_stage,
        "stages": stage_items,
        "status": current_stage_status,
        "summary": summary,
        "score": score,
        "fail_count": fail_count,
        "warn_count": warn_count,
        "blocking": [{"key": item.get("key", ""), "label": item.get("label", ""), "detail": item.get("detail", "")} for item in blocking],
        "warnings": [{"key": item.get("key", ""), "label": item.get("label", ""), "detail": item.get("detail", "")} for item in warnings],
        "no_new_major_features": True,
        "final_release": final_stage and current_stage_status == "complete",
    }


def build_release_readiness(snapshot: dict[str, Any]) -> dict[str, Any]:
    stability = snapshot.get("stability", {}) if isinstance(snapshot.get("stability"), dict) else {}
    problem_center = snapshot.get("problem_center", {}) if isinstance(snapshot.get("problem_center"), dict) else {}
    history = snapshot.get("stability_history", {}) if isinstance(snapshot.get("stability_history"), dict) else {}
    git = snapshot.get("git", {}) if isinstance(snapshot.get("git"), dict) else {}
    counts = problem_center.get("counts", {}) if isinstance(problem_center.get("counts"), dict) else {}
    problem_state = problem_center.get("problem_state", {}) if isinstance(problem_center.get("problem_state"), dict) else {}
    problem_review = problem_state.get("review", {}) if isinstance(problem_state.get("review"), dict) else {}
    problem_review_counts = problem_review.get("counts", {}) if isinstance(problem_review.get("counts"), dict) else {}
    release_trend = snapshot.get("release_trend", {}) if isinstance(snapshot.get("release_trend"), dict) else {}
    records = history.get("records", []) if isinstance(history.get("records"), list) else []
    latest = history.get("latest", {}) if isinstance(history.get("latest"), dict) else {}
    git_version = str(git.get("version") or "")
    closure_index = release_closure_stage_index(git_version)
    final_stage = closure_index >= len(RELEASE_CLOSURE_STAGES) - 1
    current_stage_info = RELEASE_CLOSURE_STAGES[closure_index] if RELEASE_CLOSURE_STAGES else {}
    next_stage_info = RELEASE_CLOSURE_STAGES[closure_index + 1] if closure_index + 1 < len(RELEASE_CLOSURE_STAGES) else None

    checks: list[dict[str, Any]] = []

    def add(key: str, label: str, status: str, detail: str, action: str = "") -> None:
        if key == "release_trend" and status == "warn" and not any(
            item.get("status") in {"fail", "warn"} for item in checks
        ):
            status = "ok"
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

    add(
        "feature_freeze",
        "功能冻结边界",
        "ok",
        "当前路线已固定为 v1.47.0-v1.50.0 收口，不新增大模块。",
        "后续只处理已有功能修复、稳定性、部署验收和文档收口。",
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

    resolved_active = int(problem_review_counts.get("resolved_active", 0) or 0)
    resolved_missing = int(problem_review_counts.get("resolved_missing", 0) or 0)
    if resolved_active:
        add(
            "problem_state_review",
            "问题状态复查",
            "fail",
            f"{resolved_active} 个已标记解决的问题仍然存在。",
            "继续按处理清单排查，处理后再执行 stable-check。",
        )
    elif resolved_missing:
        add(
            "problem_state_review",
            "问题状态复查",
            "warn",
            f"{resolved_missing} 个已标记解决的问题当前已消失，但还需要 stable-check 复查确认。",
            "执行 stable-check 保存新的验收记录；确认稳定后可清除对应标记。",
        )
    else:
        add("problem_state_review", "问题状态复查", "ok", "没有已解决但仍存在的问题。")

    trend_status = str(release_trend.get("status") or "")
    if trend_status == "regressed":
        add(
            "release_trend",
            "长期运行趋势",
            "fail",
            str(release_trend.get("summary") or "长期运行就绪度发生回退。"),
            "优先处理趋势回退原因，再推进完整稳定版。",
        )
    elif trend_status == "worse":
        add(
            "release_trend",
            "长期运行趋势",
            "warn",
            str(release_trend.get("summary") or "长期运行就绪度变差。"),
            "查看本次新增警告或阻断项，确认是否影响长期运行。",
        )
    elif trend_status:
        add("release_trend", "长期运行趋势", "ok", str(release_trend.get("summary") or "长期运行趋势正常。"))

    fail_count = sum(1 for item in checks if item.get("status") == "fail")
    warn_count = sum(1 for item in checks if item.get("status") == "warn")
    ok_count = sum(1 for item in checks if item.get("status") == "ok")
    score = max(0, min(100, 100 - fail_count * 25 - warn_count * 8))

    if fail_count:
        status = "blocked"
        label = "还不能作为完整稳定版"
        summary = "存在阻断项，先处理服务、配置、日志、趋势或问题复查，再继续推进下一阶段。"
        next_version_goal = "v1.50.0：先处理阻断项，重新执行 stable-check 后再确认完整稳定版。" if final_stage else f"{current_stage_info.get('version', 'v1.47.0')}：先把诊断报告清到无阻断，再重新执行 stable-check。"
    elif warn_count:
        status = "candidate"
        label = "准稳定候选"
        summary = "核心服务可运行，但仍有观察项；适合继续跑一段时间并保存新的验收记录。"
        next_version_goal = "v1.50.0：确认警告不影响真实推送，再保存新的 stable-check 验收历史。" if final_stage else f"{current_stage_info.get('version', 'v1.47.0')}：清完观察项后，再进入下一收口阶段。"
    else:
        status = "complete_candidate"
        label = "完整稳定版候选"
        summary = "当前快照、问题中心、日志、审计和验收历史都达到长期运行候选标准。"
        if final_stage:
            next_version_goal = "v1.50.0：已经达到 v1 完整稳定版发布门槛，后续进入长期维护。"
        elif next_stage_info:
            next_version_goal = f"{next_stage_info.get('version')}：可以进入{next_stage_info.get('label')}。"
        else:
            next_version_goal = "v1.50.0：继续保持长期运行验收记录。"

    requirements = [
        "后台核心服务运行正常",
        "当前 stable-check 达标",
        "问题中心没有严重项和警告项",
        "近期没有真实日志错误和失败审计",
        "至少保留两次稳定版达标历史",
        "没有仍存在的已解决问题，长期运行趋势没有回退",
    ]
    closure_plan = build_release_closure_plan(
        version=git_version,
        readiness_status=status,
        score=score,
        fail_count=fail_count,
        warn_count=warn_count,
        checks=checks,
    )
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
        "closure_plan": closure_plan,
    }


def build_deployment_acceptance(snapshot: dict[str, Any]) -> dict[str, Any]:
    services = snapshot.get("services", {}) if isinstance(snapshot.get("services"), dict) else {}
    config = snapshot.get("config", {}) if isinstance(snapshot.get("config"), dict) else {}
    git = snapshot.get("git", {}) if isinstance(snapshot.get("git"), dict) else {}
    stability = snapshot.get("stability", {}) if isinstance(snapshot.get("stability"), dict) else {}
    release = snapshot.get("release_readiness", {}) if isinstance(snapshot.get("release_readiness"), dict) else {}
    logs = snapshot.get("log_errors", {}) if isinstance(snapshot.get("log_errors"), dict) else {}
    audit = snapshot.get("audit", {}) if isinstance(snapshot.get("audit"), dict) else {}

    checks: list[dict[str, Any]] = []

    def add(key: str, label: str, status: str, detail: str, action: str = "") -> None:
        checks.append({"key": key, "label": label, "status": status, "detail": detail, "action": action})

    version = str(git.get("version") or "")
    commit = str(git.get("commit") or "")
    add(
        "version",
        "代码版本",
        "ok" if SEMVER_VERSION_RE.match(version) and commit and commit != "unknown" else "fail",
        f"{version or 'unknown'} {commit or 'unknown'}".strip(),
        "确认服务器目录是 Git 仓库，且 VERSION 文件和提交号能正常读取。",
    )

    required_services = {
        "main": "主服务",
        "structure": "结构雷达",
        "web": "Web 控制台",
    }
    ai_config = config.get("ai_assistant", {}) if isinstance(config.get("ai_assistant"), dict) else {}
    if bool(ai_config.get("enable")):
        required_services["ai"] = "AI 助手"
    down = []
    for key, label in required_services.items():
        item = services.get(key, {}) if isinstance(services.get(key), dict) else {}
        if not bool(item.get("active_ok")):
            down.append(label)
    add(
        "services",
        "后台服务",
        "ok" if not down else "fail",
        "主服务、结构雷达、Web 控制台和已启用的 AI 助手均在运行。" if not down else "未运行：" + "、".join(down),
        "进入 Web「雷达服务」页重启对应服务；仍失败时查看日志中心。",
    )

    web_config = config.get("web", {}) if isinstance(config.get("web"), dict) else {}
    web_host = str(web_config.get("host") or "")
    web_port = int(web_config.get("port", 0) or 0)
    web_auth_mode = str(web_config.get("auth_mode") or "password").lower()
    web_auth_ok = (
        bool(web_config.get("admin_password_hash_configured") and web_config.get("session_secret_configured"))
        if web_auth_mode != "token"
        else bool(web_config.get("admin_token_configured"))
    )
    if not web_auth_ok:
        web_status = "fail"
        web_detail = "后台账号密码或会话密钥未配置，后台无法安全登录。"
        web_action = "服务器执行 .venv/bin/python main.py admin-password set 后重启 paopao-web。"
    elif web_host in {"0.0.0.0", "::"} and 1 <= web_port <= 65535:
        web_status = "ok"
        web_detail = f"Web 入口监听 {web_host}:{web_port}，可通过服务器 IP 访问。"
        web_action = ""
    else:
        web_status = "warn"
        web_detail = f"Web 入口监听 {web_host or 'unknown'}:{web_port or 'unknown'}，可能只适合本机或自定义代理访问。"
        web_action = "如果希望直接用服务器 IP 打开，保持 WEB_HOST=0.0.0.0，WEB_PORT=8080。"
    add("web_entry", "Web 入口", web_status, web_detail, web_action)

    telegram = config.get("telegram", {}) if isinstance(config.get("telegram"), dict) else {}
    telegram_ok = bool(telegram.get("bot_token_configured") and telegram.get("chat_id_configured"))
    add(
        "telegram",
        "Telegram 推送配置",
        "ok" if telegram_ok else "fail",
        "Token 和群/频道 ID 已配置。" if telegram_ok else "缺少 Telegram Token 或群/频道 ID。",
        "到配置中心补齐 Telegram 配置后，执行 Telegram 测试消息和 readiness。",
    )

    if bool(ai_config.get("enable")):
        ai_ok = bool(ai_config.get("bot_token_configured"))
        add(
            "ai_bot",
            "AI 助手配置",
            "ok" if ai_ok else "warn",
            "AI 助手已启用且 Bot Token 已配置。" if ai_ok else "AI 助手已启用但缺 AI_BOT_TOKEN。",
            "到配置中心补齐 AI Bot Token；如果暂时不用 AI 助手，可以关闭 AI_ASSISTANT_ENABLE。",
        )
    else:
        add("ai_bot", "AI 助手配置", "warn", "AI 助手未启用；不影响群推送，但价格提醒和 AI 分析不可用。")

    stability_status = str(stability.get("status") or "")
    release_status = str(release.get("status") or "")
    if stability_status == "ready" and release_status in {"complete_candidate", "candidate"}:
        deploy_status = "ok"
        deploy_detail = f"stable-check={stability_status}，长期就绪度={release_status}。"
        deploy_action = ""
    elif stability_status == "blocked" or release_status == "blocked":
        deploy_status = "fail"
        deploy_detail = f"stable-check={stability_status or 'unknown'}，长期就绪度={release_status or 'unknown'}。"
        deploy_action = "先按诊断报告处理阻断项，再重新执行 stable-check。"
    else:
        deploy_status = "warn"
        deploy_detail = f"stable-check={stability_status or 'unknown'}，长期就绪度={release_status or 'unknown'}。"
        deploy_action = "如果实际推送正常，可以观察；建议再执行一次 stable-check 保存历史。"
    add("stable_check", "稳定版验收", deploy_status, deploy_detail, deploy_action)

    log_error_total = sum(int(item.get("error_count", 0) or 0) for item in logs.values() if isinstance(item, dict))
    failed_audit = audit.get("failed_recent", []) if isinstance(audit.get("failed_recent"), list) else []
    add(
        "logs",
        "日志稳定性",
        "ok" if log_error_total == 0 else ("warn" if log_error_total < 10 else "fail"),
        "近期没有真实日志错误。" if log_error_total == 0 else f"近期真实日志错误 {log_error_total} 条。",
        "打开日志中心勾选“只看错误”，按最早错误时间排查。",
    )
    add(
        "audit",
        "后台操作审计",
        "ok" if not failed_audit else "warn",
        "最近没有失败的后台操作。" if not failed_audit else f"最近失败操作 {len(failed_audit)} 条。",
        "打开审计记录按失败筛选，确认是否已经重试成功。",
    )

    script_status = "ok" if (BASE_DIR / "scripts" / "update_server.sh").exists() and (BASE_DIR / "scripts" / "install_server.sh").exists() else "fail"
    add(
        "scripts",
        "部署脚本",
        script_status,
        "更新脚本和安装脚本存在。" if script_status == "ok" else "缺少更新脚本或安装脚本。",
        "确认 scripts/update_server.sh 和 scripts/install_server.sh 没有被删除。",
    )

    fail_count = sum(1 for item in checks if item.get("status") == "fail")
    warn_count = sum(1 for item in checks if item.get("status") == "warn")
    ok_count = sum(1 for item in checks if item.get("status") == "ok")
    if fail_count:
        status = "blocked"
        label = "部署验收未通过"
        summary = f"{fail_count} 个阻断项会影响服务器长期运行。"
        next_action = "先处理阻断项，再执行 paopao update --yes 或 python main.py stable-check。"
    elif warn_count:
        status = "attention"
        label = "部署基本可用，建议关注"
        summary = f"{warn_count} 个观察项不一定阻断运行，但建议确认。"
        next_action = "确认警告不影响真实推送后，再保存一次 stable-check 历史。"
    else:
        status = "ready"
        label = "部署验收通过"
        summary = "服务器部署、服务、入口、配置、日志和审计均达到当前收口标准。"
        next_action = "可以进入下一阶段收口。"
    return {
        "status": status,
        "label": label,
        "summary": summary,
        "ok_count": ok_count,
        "warn_count": warn_count,
        "fail_count": fail_count,
        "checks": checks,
        "next_action": next_action,
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
    closure = release.get("closure_plan", {}) if isinstance(release.get("closure_plan"), dict) else {}
    closure_stage = closure.get("current_stage", {}) if isinstance(closure.get("current_stage"), dict) else {}
    deployment = snapshot.get("deployment_acceptance", {}) if isinstance(snapshot.get("deployment_acceptance"), dict) else {}
    problem_center = snapshot.get("problem_center", {}) if isinstance(snapshot.get("problem_center"), dict) else {}
    problem_state = problem_center.get("problem_state", {}) if isinstance(problem_center.get("problem_state"), dict) else {}
    problem_review = problem_state.get("review", {}) if isinstance(problem_state.get("review"), dict) else {}
    problem_review_counts = problem_review.get("counts", {}) if isinstance(problem_review.get("counts"), dict) else {}
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
        "closure_target_version": closure.get("target_version", RELEASE_CLOSURE_TARGET),
        "closure_mode": closure.get("mode", "功能冻结收口"),
        "closure_current_stage": closure_stage.get("version", ""),
        "closure_current_stage_label": closure_stage.get("label", ""),
        "closure_stage_status": closure_stage.get("status", ""),
        "deployment_status": deployment.get("status", "unknown"),
        "deployment_label": deployment.get("label", "未知"),
        "deployment_summary": deployment.get("summary", ""),
        "deployment_warn_count": int(deployment.get("warn_count", 0) or 0),
        "deployment_fail_count": int(deployment.get("fail_count", 0) or 0),
        "issue_count": len(issues),
        "log_error_count": log_error_total,
        "transient_count": transient_total,
        "problem_review_status": problem_review.get("status", "empty"),
        "problem_review_summary": problem_review.get("summary", ""),
        "problem_resolved_active": int(problem_review_counts.get("resolved_active", 0) or 0),
        "problem_resolved_missing": int(problem_review_counts.get("resolved_missing", 0) or 0),
        "problem_tracked_active": int(problem_review_counts.get("tracked_active", 0) or 0),
        "problem_tracked_missing": int(problem_review_counts.get("tracked_missing", 0) or 0),
    }


def load_stability_history(data_dir: Path | None = None, limit: int = STABILITY_HISTORY_LIMIT) -> list[dict[str, Any]]:
    path = stability_history_path(data_dir)
    payload = locked_read_json(path, [], quarantine_corrupt=True)
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
    max_records = max(1, min(STABILITY_HISTORY_LIMIT, int(limit or STABILITY_HISTORY_LIMIT)))
    problem_state = load_problem_state(base_dir)
    saved_record: dict[str, Any] = {}
    saved_history: list[dict[str, Any]] = []

    def save_under_history_lock(current: Any) -> list[dict[str, Any]]:
        nonlocal saved_record, saved_history
        rows = current if isinstance(current, list) else current.get("records", []) if isinstance(current, dict) else []
        previous_records = [item for item in rows if isinstance(item, dict)][:max_records]

        provisional_record = stability_record_from_snapshot(snapshot)
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
        trimmed = [record, *previous_records][:max_records]
        snapshot["stability_history"] = {
            "latest_path": str(latest_path),
            "history_path": str(history_path),
            "latest": record,
            "records": trimmed,
            "count": len(trimmed),
        }
        snapshot["release_trend"] = build_release_trend(snapshot["stability_history"])
        snapshot["problem_center"] = build_problem_center(snapshot, problem_state)
        snapshot["release_readiness"] = build_release_readiness(snapshot)
        snapshot["deployment_acceptance"] = build_deployment_acceptance(snapshot)
        record = stability_record_from_snapshot(snapshot)
        trimmed = [record, *previous_records][:max_records]
        snapshot["stability_history"] = {
            "latest_path": str(latest_path),
            "history_path": str(history_path),
            "latest": record,
            "records": trimmed,
            "count": len(trimmed),
        }
        snapshot["release_trend"] = build_release_trend(snapshot["stability_history"])
        snapshot["recommendations"] = build_ops_recommendations(snapshot)

        # Serialize latest and history through the history lock so concurrent
        # stable-check jobs cannot leave the two files pointing at different
        # snapshots. Each individual file still uses its own atomic replace.
        locked_write_json(latest_path, snapshot)
        saved_record = record
        saved_history = trimmed
        return trimmed

    locked_update_json(history_path, save_under_history_lock, [])
    invalidate_runtime_cache("stable:")
    return {
        "saved": True,
        "latest_path": str(latest_path),
        "history_path": str(history_path),
        "record": saved_record,
        "history_count": len(saved_history),
    }


def _load_stability_history_payload(base_dir: Path, limit: int) -> dict[str, Any]:
    latest_path = stability_latest_path(base_dir)
    latest_record: dict[str, Any] | None = None
    latest_snapshot = locked_read_json(latest_path, {}, quarantine_corrupt=True)
    if isinstance(latest_snapshot, dict) and latest_snapshot:
        latest_record = stability_record_from_snapshot(latest_snapshot)
    records = load_stability_history(base_dir, limit=limit)
    return {
        "latest_path": str(latest_path),
        "history_path": str(stability_history_path(base_dir)),
        "latest": latest_record,
        "records": records,
        "count": len(records),
    }


def stability_history_payload(data_dir: Path | None = None, limit: int = 8) -> dict[str, Any]:
    base_dir = data_dir or Settings.load().data_dir
    safe_limit = max(1, min(STABILITY_HISTORY_LIMIT, int(limit or 8)))
    cache_key = f"stable:history:{base_dir.resolve()}:{safe_limit}"
    cached = runtime_cache_get_or_set(
        cache_key,
        STABILITY_FILE_CACHE_TTL_SEC,
        lambda: _load_stability_history_payload(base_dir, safe_limit),
    )
    return copy.deepcopy(cached)


def ops_snapshot_payload() -> dict[str, Any]:
    summary = summary_payload()
    settings = Settings.load()
    audit_all = web_audit_payload(limit=10, result="all")
    audit_failed = web_audit_payload(limit=10, result="failed")
    problem_state = load_problem_state(settings.data_dir)
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
    snapshot["jobs"] = jobs_stats_payload()
    snapshot["stability_history"] = stability_history_payload(settings.data_dir, limit=8)
    snapshot["release_trend"] = build_release_trend(snapshot["stability_history"])
    snapshot["issues"] = build_ops_issues(snapshot)
    snapshot["stability"] = build_stability_checks(snapshot)
    snapshot["problem_center"] = build_problem_center(snapshot, problem_state)
    snapshot["release_readiness"] = build_release_readiness(snapshot)
    snapshot["deployment_acceptance"] = build_deployment_acceptance(snapshot)
    snapshot["recommendations"] = build_ops_recommendations(snapshot)
    snapshot["message"] = "已生成安全运维快照，可复制给排查人员；不包含 Token、API Key 或提示词正文。"
    return snapshot


def handler_settings(handler: BaseHTTPRequestHandler) -> Settings:
    settings = getattr(handler.server, "settings", None)  # type: ignore[attr-defined]
    return settings if isinstance(settings, Settings) else Settings.load()


def request_is_https(handler: BaseHTTPRequestHandler) -> bool:
    forwarded_proto = handler.headers.get("X-Forwarded-Proto", "")
    if forwarded_proto.lower().split(",", 1)[0].strip() == "https":
        return True
    request = getattr(handler, "request", None)
    return getattr(request, "type", "") == "https"


def auth_mode(settings: Settings) -> str:
    mode = str(getattr(settings, "web_auth_mode", "password") or "password").strip().lower()
    return "token" if mode == "token" else "password"


def request_client_ip(handler: BaseHTTPRequestHandler) -> str:
    remote = ""
    try:
        remote = str((getattr(handler, "client_address", ("", 0)) or ("", 0))[0] or "")
    except Exception:
        remote = ""
    local_remote = remote in {"", "127.0.0.1", "::1", "localhost"}
    if local_remote:
        forwarded_for = handler.headers.get("X-Forwarded-For", "")
        if forwarded_for:
            candidate = forwarded_for.split(",", 1)[0].strip()
            if candidate:
                return candidate[:128]
        real_ip = handler.headers.get("X-Real-IP", "").strip()
        if real_ip:
            return real_ip[:128]
    return (remote or "unknown")[:128]


def audit_auth_event(
    handler: BaseHTTPRequestHandler,
    event: str,
    *,
    username: str = "",
    result: str = "",
    reason: str = "",
) -> None:
    settings = handler_settings(handler)
    try:
        append_auth_audit(
            settings.data_dir,
            event=event,
            username=username,
            ip=request_client_ip(handler),
            user_agent=handler.headers.get("User-Agent", ""),
            result=result,
            reason=reason,
            limit=settings.web_auth_audit_limit,
            secret=settings.web_session_secret,
        )
    except Exception as exc:
        sys.stderr.write(f"[web] auth audit write failed: {type(exc).__name__}: {exc}\n")


def session_payload(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    cached = getattr(handler, "auth_session", None)
    if isinstance(cached, dict):
        return cached
    settings = handler_settings(handler)
    if auth_mode(settings) != "password":
        return None
    cookie_name = settings.web_auth_cookie_name or "paopao_admin_session"
    raw_cookie = cookie_value(handler.headers.get("Cookie", ""), cookie_name)
    payload, reason = verify_session_value_detailed(
        raw_cookie,
        settings.web_session_secret,
        expected_username=settings.web_admin_username,
    )
    if payload:
        setattr(handler, "auth_session", payload)
    elif raw_cookie and not getattr(handler, "auth_session_invalid_audited", False):
        setattr(handler, "auth_session_invalid_audited", True)
        audit_auth_event(
            handler,
            "session_expired" if reason == "expired" else "session_invalid",
            username=settings.web_admin_username,
            result="failed",
            reason=reason,
        )
    return payload


def maybe_refresh_session(handler: BaseHTTPRequestHandler) -> dict[str, Any] | None:
    settings = handler_settings(handler)
    if auth_mode(settings) != "password":
        return None
    payload = session_payload(handler)
    if not payload:
        return None
    now = int(time.time())
    expires_at = int(payload.get("exp", 0) or 0)
    ttl_sec = max(60, int(settings.web_session_ttl_sec or 86400))
    remaining = expires_at - now
    threshold_ratio = min(0.9, max(0.1, float(settings.web_session_refresh_threshold_ratio or 0.5)))
    if remaining <= 0 or remaining >= int(ttl_sec * threshold_ratio):
        return payload
    csrf = str(payload.get("csrf", ""))
    value, refreshed_csrf = create_session_value(
        settings.web_admin_username,
        settings.web_session_secret,
        ttl_sec=ttl_sec,
        csrf=csrf,
    )
    refreshed, _reason = verify_session_value_detailed(
        value,
        settings.web_session_secret,
        expected_username=settings.web_admin_username,
    )
    if refreshed:
        setattr(handler, "auth_session", refreshed)
        setattr(handler, "auth_refresh_cookie", build_session_cookie(
            settings.web_auth_cookie_name,
            value,
            max_age=ttl_sec,
            secure=request_is_https(handler),
        ))
        refreshed["csrf"] = refreshed_csrf
        return refreshed
    return payload


def check_auth(handler: BaseHTTPRequestHandler) -> bool:
    settings = handler_settings(handler)
    if auth_mode(settings) == "token":
        token = getattr(handler.server, "admin_token", "") or os.getenv("WEB_ADMIN_TOKEN", "")  # type: ignore[attr-defined]
        if not token:
            return False
        parsed = urlparse(handler.path)
        query_token = parse_qs(parsed.query).get("token", [""])[0]
        header_token = handler.headers.get("X-Admin-Token", "")
        return token in {query_token, header_token}
    return session_payload(handler) is not None


def check_csrf(handler: BaseHTTPRequestHandler) -> bool:
    settings = handler_settings(handler)
    if auth_mode(settings) == "token":
        return True
    payload = session_payload(handler) or {}
    expected = str(payload.get("csrf", ""))
    supplied = handler.headers.get("X-CSRF-Token", "")
    return bool(expected and supplied and hmac_compare(expected, supplied))


def hmac_compare(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(str(left or ""), str(right or ""))


PUBLIC_INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>泡泡抓币 Crypto Radar</title><style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f7fa;color:#172033;font-family:Inter,"Noto Sans SC","Microsoft YaHei",system-ui,sans-serif}.card{width:min(560px,calc(100% - 40px));padding:32px;background:#fff;border:1px solid #e2e7ee;border-radius:16px;box-shadow:0 18px 50px #1e293b12}h1{margin:0 0 10px;font-size:26px}p{color:#667085;line-height:1.7}.row{display:flex;gap:10px;flex-wrap:wrap;margin-top:22px}a{padding:10px 14px;border-radius:9px;text-decoration:none;font-weight:650;background:#2563eb;color:#fff}a.secondary{background:#fff;color:#2563eb;border:1px solid #dbe3ef}</style></head><body><main class="card"><h1>泡泡抓币 Crypto Radar</h1><p>公开前台由 Next.js 提供。这里是 Python 后端入口，仅保留信号 API 与运维控制台。</p><div class="row"><a href="/admin">打开后台控制台</a><a class="secondary" href="/public-api/signals?limit=20">查看公开信号 API</a></div></main></body></html>"""


INDEX_HTML = read_text_file(BASE_DIR / "paopao_radar" / "admin.html")


class WebHandler(BaseHTTPRequestHandler):
    server_version = "PaopaoRadarWeb/1.0"
    CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionResetError, ConnectionAbortedError)

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

    def send_json(self, data: Any, status: int = 200, *, extra_headers: dict[str, str] | None = None) -> None:
        payload_obj = data
        if isinstance(data, dict):
            payload_obj = dict(data)
            existing_meta = payload_obj.get("_meta")
            meta = existing_meta if isinstance(existing_meta, dict) else {}
            payload_obj["_meta"] = {**meta, **self.api_meta(int(status))}
        payload = json.dumps(payload_obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_payload(payload, status, "application/json; charset=utf-8", extra_headers=extra_headers)

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
        self.send_payload(payload, 200, "text/html; charset=utf-8")

    def send_payload(
        self,
        payload: bytes,
        status: int,
        content_type: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            headers = dict(extra_headers or {})
            refresh_cookie = getattr(self, "auth_refresh_cookie", "")
            if refresh_cookie and "Set-Cookie" not in headers:
                headers["Set-Cookie"] = str(refresh_cookie)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        except self.CLIENT_DISCONNECT_ERRORS:
            sys.stderr.write("[web] client disconnected during response\n")

    def read_json(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length", "0") or 0)
        if size > 128 * 1024:
            raise ValueError("请求体太大")
        raw = self.rfile.read(size).decode("utf-8") if size else "{}"
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def auth_status_payload(self) -> dict[str, Any]:
        settings = handler_settings(self)
        mode = auth_mode(settings)
        payload = maybe_refresh_session(self) if mode == "password" else None
        logged_in = bool(payload) if mode == "password" else check_auth(self)
        issued_at = int((payload or {}).get("iat", 0) or 0)
        expires_at = int((payload or {}).get("exp", 0) or 0)
        now = int(time.time())
        return {
            "ok": True,
            "logged_in": logged_in,
            "auth_mode": mode,
            "username": settings.web_admin_username if logged_in else "",
            "issued_at": iso_timestamp(issued_at) if logged_in and issued_at else "",
            "expires_at": iso_timestamp(expires_at) if logged_in and expires_at else "",
            "ttl_sec": int(settings.web_session_ttl_sec or 86400),
            "remaining_sec": max(0, expires_at - now) if logged_in and expires_at else 0,
            "csrf_token": str((payload or {}).get("csrf", "")) if logged_in else "",
            "password_configured": bool(settings.web_admin_password_hash),
            "session_configured": bool(settings.web_session_secret),
            "lockout_config": {
                "max_failures": settings.web_auth_max_failures,
                "lockout_sec": settings.web_auth_lockout_sec,
                "failure_window_sec": settings.web_auth_failure_window_sec,
            },
            "message": "已登录" if logged_in else "未登录",
        }

    def send_unauthorized(self, message: str = "请先登录后台") -> None:
        self.send_json(
            {
                "ok": False,
                "error": {"code": "unauthorized", "message": message},
                "message": message,
                "code": "unauthorized",
            },
            HTTPStatus.UNAUTHORIZED,
        )

    def send_forbidden(self, message: str = "请求校验失败，请刷新页面后重试") -> None:
        self.send_json(
            {
                "ok": False,
                "error": {"code": "forbidden", "message": message},
                "message": message,
                "code": "forbidden",
            },
            HTTPStatus.FORBIDDEN,
        )

    def handle_auth_login(self) -> None:
        started_at = time.time()
        settings = handler_settings(self)
        if auth_mode(settings) == "token":
            result = {"ok": False, "error": {"code": "unsupported_auth_mode", "message": "当前为旧令牌模式"}, "message": "当前为旧令牌模式", "code": "unsupported_auth_mode"}
            try:
                append_web_audit("/api/auth/login", {}, result, status=HTTPStatus.BAD_REQUEST, started_at=started_at, data_dir=settings.data_dir)
            except Exception as exc:
                sys.stderr.write(f"[web] audit write failed: {type(exc).__name__}: {exc}\n")
            self.send_json(result, HTTPStatus.BAD_REQUEST)
            return
        if not settings.web_admin_password_hash or not settings.web_session_secret:
            result = {
                "ok": False,
                "error": {"code": "auth_not_configured", "message": "后台账号密码尚未配置，请在服务器执行设置命令。"},
                "message": "后台账号密码尚未配置，请在服务器执行设置命令。",
                "code": "auth_not_configured",
            }
            try:
                append_web_audit("/api/auth/login", {}, result, status=HTTPStatus.BAD_REQUEST, started_at=started_at, data_dir=settings.data_dir)
            except Exception as exc:
                sys.stderr.write(f"[web] audit write failed: {type(exc).__name__}: {exc}\n")
            self.send_json(result, HTTPStatus.BAD_REQUEST)
            return
        try:
            data = self.read_json()
        except ValueError as exc:
            self.send_error_json(str(exc), HTTPStatus.BAD_REQUEST, "bad_request")
            return
        username = str(data.get("username", "")).strip()
        password = str(data.get("password", ""))
        client_ip = request_client_ip(self)
        lockout = check_auth_lockout(
            settings.data_dir,
            settings.web_session_secret,
            username or settings.web_admin_username,
            client_ip,
            window_sec=settings.web_auth_failure_window_sec,
        )
        if lockout.get("locked"):
            retry_after = int(lockout.get("retry_after_sec", settings.web_auth_lockout_sec) or settings.web_auth_lockout_sec)
            result = {
                "ok": False,
                "error": {"code": "locked", "message": "登录失败次数过多，请稍后再试。"},
                "message": "登录失败次数过多，请稍后再试。",
                "code": "locked",
                "retry_after_sec": retry_after,
            }
            audit_auth_event(self, "login_locked", username=username, result="locked", reason="lockout_active")
            try:
                append_web_audit("/api/auth/login", {"username": username}, result, status=HTTPStatus.TOO_MANY_REQUESTS, started_at=started_at, data_dir=settings.data_dir)
            except Exception as exc:
                sys.stderr.write(f"[web] audit write failed: {type(exc).__name__}: {exc}\n")
            self.send_json(result, HTTPStatus.TOO_MANY_REQUESTS)
            return
        if username != settings.web_admin_username or not verify_password(password, settings.web_admin_password_hash):
            failure = record_auth_failure(
                settings.data_dir,
                settings.web_session_secret,
                username or settings.web_admin_username,
                client_ip,
                max_failures=settings.web_auth_max_failures,
                lockout_sec=settings.web_auth_lockout_sec,
                window_sec=settings.web_auth_failure_window_sec,
            )
            if failure.get("locked"):
                retry_after = int(failure.get("retry_after_sec", settings.web_auth_lockout_sec) or settings.web_auth_lockout_sec)
                result = {
                    "ok": False,
                    "error": {"code": "locked", "message": "登录失败次数过多，请稍后再试。"},
                    "message": "登录失败次数过多，请稍后再试。",
                    "code": "locked",
                    "retry_after_sec": retry_after,
                }
                audit_auth_event(self, "login_locked", username=username, result="locked", reason="max_failures")
                try:
                    append_web_audit("/api/auth/login", {"username": username}, result, status=HTTPStatus.TOO_MANY_REQUESTS, started_at=started_at, data_dir=settings.data_dir)
                except Exception as exc:
                    sys.stderr.write(f"[web] audit write failed: {type(exc).__name__}: {exc}\n")
                self.send_json(result, HTTPStatus.TOO_MANY_REQUESTS)
                return
            result = {
                "ok": False,
                "error": {"code": "unauthorized", "message": "用户名或密码错误"},
                "message": "用户名或密码错误",
                "code": "unauthorized",
            }
            audit_auth_event(self, "login_failed", username=username, result="failed", reason="bad_credentials")
            try:
                append_web_audit("/api/auth/login", {"username": username}, result, status=HTTPStatus.UNAUTHORIZED, started_at=started_at, data_dir=settings.data_dir)
            except Exception as exc:
                sys.stderr.write(f"[web] audit write failed: {type(exc).__name__}: {exc}\n")
            self.send_json(result, HTTPStatus.UNAUTHORIZED)
            return
        clear_auth_failures(settings.data_dir, settings.web_session_secret, settings.web_admin_username, client_ip)
        value, csrf = create_session_value(
            settings.web_admin_username,
            settings.web_session_secret,
            ttl_sec=settings.web_session_ttl_sec,
        )
        cookie_header = build_session_cookie(
            settings.web_auth_cookie_name,
            value,
            max_age=settings.web_session_ttl_sec,
            secure=request_is_https(self),
        )
        result = {
            "ok": True,
            "message": "登录成功",
            "logged_in": True,
            "username": settings.web_admin_username,
            "csrf_token": csrf,
        }
        audit_auth_event(self, "login_success", username=settings.web_admin_username, result="success", reason="")
        try:
            append_web_audit("/api/auth/login", {"username": username}, result, status=200, started_at=started_at, data_dir=settings.data_dir)
        except Exception as exc:
            sys.stderr.write(f"[web] audit write failed: {type(exc).__name__}: {exc}\n")
        self.send_json(result, extra_headers={"Set-Cookie": cookie_header})

    def handle_auth_logout(self) -> None:
        started_at = time.time()
        settings = handler_settings(self)
        cookie_header = build_clear_cookie(
            settings.web_auth_cookie_name or "paopao_admin_session",
            secure=request_is_https(self),
        )
        result = {"ok": True, "message": "已退出登录", "logged_in": False}
        payload = session_payload(self)
        audit_auth_event(self, "logout", username=str((payload or {}).get("username", "")), result="success", reason="")
        try:
            append_web_audit("/api/auth/logout", {}, result, status=200, started_at=started_at, data_dir=settings.data_dir)
        except Exception as exc:
            sys.stderr.write(f"[web] audit write failed: {type(exc).__name__}: {exc}\n")
        self.send_json(result, extra_headers={"Set-Cookie": cookie_header})

    def require_auth(self, *, write: bool = False) -> bool:
        if check_auth(self):
            maybe_refresh_session(self)
            if write and not check_csrf(self):
                self.send_forbidden()
                return False
            return True
        self.send_unauthorized()
        return False

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == "/":
            self.send_html(PUBLIC_INDEX_HTML)
            return
        if path == "/admin" or path.startswith("/admin/"):
            self.send_html(INDEX_HTML)
            return
        if path == "/api/auth/status":
            self.send_json(self.auth_status_payload())
            return
        if path == "/api/auth/audit":
            if not self.require_auth():
                return
            self.send_json(auth_audit_payload(handler_settings(self).data_dir, limit=clamp_query_int(query.get("limit", ["50"])[0], 50, 200)))
            return
        if path == "/public-api/signals":
            self.send_json(public_signals_payload(
                limit=clamp_query_int(query.get("limit", ["50"])[0], 50, 200),
                cursor=query_int_or(query.get("cursor", ["0"])[0], 0) or None,
                module=query.get("module", [""])[0],
                symbol=query.get("symbol", [""])[0],
                status=query.get("status", [""])[0],
                q=query.get("q", [""])[0],
                window_sec=min(2592000, max(1, query_int_or(query.get("window_sec", ["86400"])[0], 86400))),
            ))
            return
        if path == "/public-api/signals/detail":
            self.send_json(public_signal_detail_payload(query_int_or(query.get("id", ["0"])[0], 0)))
            return
        if path == "/public-api/signals/stats":
            self.send_json(public_signal_stats_payload(
                window_sec=min(2592000, max(1, query_int_or(query.get("window_sec", ["86400"])[0], 86400))),
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
        if path == "/api/structure-recommendations":
            self.send_json(structure_review_recommendations_payload())
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
        path = urlparse(self.path).path
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
