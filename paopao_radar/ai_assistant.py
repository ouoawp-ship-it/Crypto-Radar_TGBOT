from __future__ import annotations

import json
import html
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import quote as url_quote

import requests

from .ai_prompts import load_ai_prompts
from .config import ENV_FILE, Settings, normalize_ai_model
from .data_sources import HTTP_HEADERS
from .price_alerts import (
    ALERT_MARKET_EXCHANGES,
    ALERT_MARKET_TYPES,
    AlertMarketQuote,
    PriceAlert,
    PriceAlertStore,
    alert_to_dict,
    contract_pair_multiplier,
    discover_alert_markets,
    fetch_open_interest_value,
    fetch_alert_market_quote,
    fetch_price_change_snapshot,
    fetch_binance_prices,
    fetch_price_alert_prices,
    format_price,
    normalize_symbol,
    parse_price,
    triggered_alerts,
)
from .symbol_dossier import (
    build_symbol_dossier,
    format_symbol_dossier_ai_context,
    format_symbol_dossier_report,
    is_symbol_dossier_request,
)
from .data_sources import DataQuality, HttpClient
from .funding_alert import classify_funding_alert, funding_row_text
from .funding_sources import MultiExchangeFundingClient


HOME_TEXT = """泡泡 AI 助手 Bot

这是泡泡雷达的独立 AI 助手，和群里自动推送雷达信号的 Bot 分开。

现在只保留一个命令：/start。其它功能都直接发消息或点按钮完成。

1. 查价格
直接发送 BTC、ETH、PEPE，或发送“BTC 现在多少钱”。

2. 看行情
直接发送“BTC 怎么看”，或粘贴雷达信号、资金费率、市场数据。
交易问题会自动走专业分析师模式。

3. 价格提醒
点击“设置价格提醒”，选择目标价、价格急涨急跌、持仓量变化或资金费率变化。
创建前会让你确认，确认后才会保存。

4. 管理提醒
点击“我的提醒”，可以查看、暂停、恢复、删除。

注意：价格提醒必须走按钮确认，自然语言不会直接创建提醒。
"""

HELP_TEXT = """泡泡 AI 助手使用说明

这个 Bot 不是命令型机器人。
除了 /start 打开首页，其它功能都靠“直接发消息 + 按钮确认”。

1. 查价格
直接发送：BTC、ETH、PEPE
我会返回五大交易所现货/合约价格。

2. 看行情
直接发送：BTC 怎么看、SOL 可以做多吗
也可以粘贴启动雷达、资金流、资金费率或市场数据。

我会读取这个币的历史雷达信号、当前价格、OI、成交量、市值、流动性和资金费率，给出偏多 / 偏空 / 观望 / 高风险观望。

3. 价格提醒
点击首页“设置价格提醒”，按步骤选择：
币种 -> 现货/合约 -> 交易所 -> 条件 -> 确认。

4. 管理提醒
点击首页“我的提醒”，可以查看、暂停、恢复、删除。

注意：价格提醒不靠一句话创建，必须走按钮确认，避免设置错交易所或价格源。
"""

ALERT_SETUP_TEXT = """设置价格提醒

请选择要创建的监控类型：

1. 目标价提醒
价格高于或低于某个目标价时提醒。

2. 价格急涨急跌
5分钟 / 15分钟 / 60分钟 内，现货或合约价格波动超过 1%-5% 时提醒。

3. 持仓量变化
合约 OI 在所选窗口内变化超过 1%-5% 时提醒。

4. 资金费率变化
监控资金费率周期缩短，例如 8H->4H、8H->1H、4H->1H，以及极正/极负费率。
"""

ALERT_SYMBOL_TEXT = """请输入币种简称，例如：
BTC
ETH
DOGE

下一步会自动识别 Binance、Bybit、OKX、Bitget、Gate 里可用的现货或 USDT 合约。
"""


@dataclass(frozen=True)
class BotReply:
    text: str
    reply_markup: dict[str, Any] | None = None
    parse_mode: str | None = None


AI_SETTINGS_CACHE_TTL_SEC = 2.0
AI_CALLBACK_SLOW_LOG_SEC = 0.5
AI_MESSAGE_SLOW_LOG_SEC = 1.0
_AI_SETTINGS_CACHE_LOCK = threading.Lock()
_AI_SETTINGS_CACHE_SIG: tuple[int, int] | None = None
_AI_SETTINGS_CACHE_LOADED_AT = 0.0
_AI_SETTINGS_CACHE_VALUE: Settings | None = None


def _env_file_signature() -> tuple[int, int]:
    try:
        stat = ENV_FILE.stat()
    except FileNotFoundError:
        return (0, 0)
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _settings_loader_is_mocked() -> bool:
    return "unittest.mock" in type(Settings.load).__module__


def clear_ai_settings_cache() -> None:
    global _AI_SETTINGS_CACHE_SIG, _AI_SETTINGS_CACHE_LOADED_AT, _AI_SETTINGS_CACHE_VALUE
    with _AI_SETTINGS_CACHE_LOCK:
        _AI_SETTINGS_CACHE_SIG = None
        _AI_SETTINGS_CACHE_LOADED_AT = 0.0
        _AI_SETTINGS_CACHE_VALUE = None


def load_ai_settings_cached(force: bool = False) -> Settings:
    global _AI_SETTINGS_CACHE_SIG, _AI_SETTINGS_CACHE_LOADED_AT, _AI_SETTINGS_CACHE_VALUE
    if _settings_loader_is_mocked():
        return Settings.load()
    now = time.time()
    signature = _env_file_signature()
    with _AI_SETTINGS_CACHE_LOCK:
        if (
            not force
            and _AI_SETTINGS_CACHE_VALUE is not None
            and _AI_SETTINGS_CACHE_SIG == signature
            and now - _AI_SETTINGS_CACHE_LOADED_AT <= AI_SETTINGS_CACHE_TTL_SEC
        ):
            return _AI_SETTINGS_CACHE_VALUE
        settings = Settings.load()
        _AI_SETTINGS_CACHE_SIG = signature
        _AI_SETTINGS_CACHE_LOADED_AT = now
        _AI_SETTINGS_CACHE_VALUE = settings
        return settings


def log_ai_update_latency(kind: str, started_at: float, note: str = "") -> None:
    elapsed = time.perf_counter() - started_at
    threshold = AI_CALLBACK_SLOW_LOG_SEC if kind == "callback" else AI_MESSAGE_SLOW_LOG_SEC
    if elapsed < threshold:
        return
    suffix = f" {note}" if note else ""
    print(f"ai-assistant: slow_{kind} elapsed={elapsed:.3f}s{suffix}", flush=True)


def _is_timeout_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return isinstance(exc, requests.Timeout) or "timeout" in text or "timed out" in text


def _is_connection_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return isinstance(exc, requests.ConnectionError) or "connection" in text


def user_facing_error(action: str, exc: BaseException) -> str:
    raw = str(exc).strip()
    if raw.startswith(f"{action}失败：") or raw.startswith(f"{action}超时："):
        return raw
    if "AI 接口响应超时" in raw:
        return f"{action}失败：{raw}"
    if "AI 接口" in raw or "DeepSeek" in raw or "deepseek" in raw:
        return f"{action}失败：{raw}"
    if action.startswith("AI") and re.match(r"^[1-5][0-9]{2}\b", raw):
        return f"{action}失败：{raw}"
    if _is_timeout_error(exc):
        return f"{action}超时：网络或接口响应慢，系统已记录日志；请稍后再试。"
    if _is_connection_error(exc):
        return f"{action}失败：网络连接不稳定，系统会继续自动重试；请稍后再试。"
    return f"{action}失败：系统已记录错误，请稍后再试。"


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def main_menu_markup() -> dict[str, Any]:
    return inline_keyboard([
        [("设置价格提醒", "flow:alert_setup"), ("我的提醒", "menu:alerts")],
        [("查询价格", "menu:price_query"), ("使用说明", "menu:help")],
    ])


def back_home_markup() -> dict[str, Any]:
    return inline_keyboard([[("返回首页", "menu:home")]])


def alert_kind_markup() -> dict[str, Any]:
    return inline_keyboard([
        [("目标价提醒", "alert:kind:target_price"), ("价格急涨急跌", "alert:kind:price_change")],
        [("持仓量变化", "alert:kind:oi_change"), ("资金费率变化", "alert:kind:funding_change")],
        [("取消", "flow:cancel")],
    ])


def cancel_markup() -> dict[str, Any]:
    return inline_keyboard([[("取消", "flow:cancel")]])


def market_type_markup(quotes: list[AlertMarketQuote]) -> dict[str, Any]:
    has_spot = any(quote.market_type == "spot" for quote in quotes)
    has_futures = any(quote.market_type == "futures" for quote in quotes)
    rows: list[list[tuple[str, str]]] = []
    row: list[tuple[str, str]] = []
    if has_spot:
        row.append(("现货", "alert:market:spot"))
    if has_futures:
        row.append(("USDT 合约", "alert:market:futures"))
    if row:
        rows.append(row)
    rows.append([("取消", "flow:cancel")])
    return inline_keyboard(rows)


def exchange_markup(quotes: list[AlertMarketQuote]) -> dict[str, Any]:
    rows: list[list[tuple[str, str]]] = []
    for quote in quotes:
        label = f"{quote.exchange_label} · {format_price(quote.price)}"
        rows.append([(label, f"alert:exchange:{quote.key}")])
    rows.append([("重新输入币种", "flow:alert_setup"), ("取消", "flow:cancel")])
    return inline_keyboard(rows)


def pending_alert_markup() -> dict[str, Any]:
    return inline_keyboard([
        [("确认添加提醒", "alert:confirm_pending")],
        [("重新设置", "flow:alert_setup"), ("取消", "flow:cancel")],
    ])


def timeframe_markup() -> dict[str, Any]:
    return inline_keyboard([
        [("5分钟", "alert:timeframe:300"), ("15分钟", "alert:timeframe:900"), ("60分钟", "alert:timeframe:3600")],
        [("取消", "flow:cancel")],
    ])


def threshold_markup() -> dict[str, Any]:
    return inline_keyboard([
        [("1%", "alert:threshold:1"), ("2%", "alert:threshold:2"), ("3%", "alert:threshold:3")],
        [("4%", "alert:threshold:4"), ("5%", "alert:threshold:5")],
        [("取消", "flow:cancel")],
    ])


def change_direction_markup() -> dict[str, Any]:
    return inline_keyboard([
        [("上涨", "alert:direction:up"), ("下跌", "alert:direction:down"), ("双向", "alert:direction:both")],
        [("取消", "flow:cancel")],
    ])


def repeat_policy_markup() -> dict[str, Any]:
    return inline_keyboard([
        [("提醒一次", "alert:repeat:once"), ("重复提醒", "alert:repeat:repeat")],
        [("持续提醒 每5分钟", "alert:repeat:interval:300")],
        [("取消", "flow:cancel")],
    ])


def alerts_manage_markup(alerts: list[PriceAlert]) -> dict[str, Any]:
    rows: list[list[tuple[str, str]]] = []
    for display_no, alert in enumerate(alerts[:30], start=1):
        row: list[tuple[str, str]] = []
        if alert.status == "paused":
            row.append((f"恢复{display_no}", f"alert:resume:{alert.id}"))
        elif alert.status == "active":
            row.append((f"暂停{display_no}", f"alert:pause:{alert.id}"))
        row.append((f"删除{display_no}", f"alert:delete:{alert.id}"))
        rows.append(row)
    rows.append([("返回首页", "menu:home")])
    return inline_keyboard(rows)


def quote_to_dict(quote: AlertMarketQuote) -> dict[str, Any]:
    return {
        "exchange": quote.exchange,
        "market_type": quote.market_type,
        "symbol": quote.symbol,
        "pair": quote.pair,
        "price": quote.price,
    }


def quote_from_dict(data: dict[str, Any]) -> AlertMarketQuote:
    return AlertMarketQuote(
        exchange=str(data.get("exchange") or "binance"),
        market_type=str(data.get("market_type") or "futures"),
        symbol=str(data.get("symbol") or ""),
        pair=str(data.get("pair") or ""),
        price=float(data.get("price") or 0),
    )


def session_quotes(session: dict[str, Any], market_type: str | None = None) -> list[AlertMarketQuote]:
    raw = session.get("quotes")
    items = raw if isinstance(raw, list) else []
    quotes = [quote_from_dict(item) for item in items if isinstance(item, dict)]
    if market_type:
        quotes = [quote for quote in quotes if quote.market_type == market_type]
    return quotes


GROUP_CHAT_TYPES = {"group", "supergroup"}
NON_PRIVATE_CHAT_TYPES = GROUP_CHAT_TYPES | {"channel"}
ALERT_CREATE_INTENT_RE = re.compile(
    r"(提醒我|提醒一下|提醒下|通知我|通知一下|通知下|叫我|帮我盯|盯一下|设置提醒|设个提醒|创建提醒|添加提醒|到价|到了叫我|达到.*提醒|涨到.*(提醒|通知|叫)|跌到.*(提醒|通知|叫)|alert)",
    re.IGNORECASE,
)
ANALYSIS_INTENT_RE = re.compile(
    r"^\s*(?:"
    r"分析这段|帮我分析|分析一下|分析下|解读一下|解读下|解读这个|看看这个信号|看下这个信号|"
    r"这段(?:数据|内容|信号)?帮我(?:分析|看看|看下|解读)|"
    r"帮我(?:分析|看看|看下|解读)(?:这段|这个)(?:数据|内容|信号)?|"
    r"(?:分析|解读)(?:这个|这段)(?:数据|内容|信号)"
    r")",
    re.IGNORECASE,
)
PRICE_QUERY_RE = re.compile(r"(价格|现价|报价|行情|多少钱|多少|查价|查一下|看一下|price)", re.IGNORECASE)
MARKET_DATA_KEYWORDS = (
    "启动雷达",
    "资金流",
    "雷达信号",
    "触发明细",
    "阶段:",
    "阶段：",
    "分数:",
    "分数：",
    "当前价格",
    "基础信息",
    "oi",
    "cvd",
    "持仓",
    "成交量",
    "成交量倍数",
    "资金费率",
    "市值",
    "流动性",
    "清算",
    "多空",
    "合约",
    "现货",
    "coinglass",
)
SYMBOL_ALIASES = {
    "比特币": "BTC",
    "大饼": "BTC",
    "以太坊": "ETH",
    "以太": "ETH",
    "币安币": "BNB",
    "索拉纳": "SOL",
    "狗狗币": "DOGE",
    "狗狗": "DOGE",
}
SYMBOL_STOP_WORDS = {
    "AI",
    "API",
    "USDT",
    "USD",
    "CVD",
    "OI",
    "TV",
    "K",
    "WEB",
    "BOT",
    "HELP",
    "ALERT",
    "PRICE",
}


def telegram_plain_text(text: str) -> str:
    cleaned = str(text or "")
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1（\2）", cleaned)
    cleaned = re.sub(r"\*\*(.*?)\*\*", r"\1", cleaned, flags=re.S)
    cleaned = re.sub(r"__(.*?)__", r"\1", cleaned, flags=re.S)
    cleaned = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", cleaned)
    cleaned = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"^#{1,6}\s*", "", cleaned, flags=re.M)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def infer_telegram_parse_mode(text: str) -> str | None:
    return "HTML" if re.search(r"</?(?:a|b|strong|pre|code)\b", str(text or ""), flags=re.IGNORECASE) else None


def split_telegram_text(text: str, limit: int = 3900) -> list[str]:
    remaining = str(text or "").strip()
    if not remaining:
        return []
    chunks: list[str] = []
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining.strip())
            break
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < 800:
            split_at = limit
        chunk = remaining[:split_at].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[split_at:].strip()
    return chunks


def _normalize_bot_username(bot_username: str) -> str:
    return str(bot_username or "").strip().lstrip("@").lower()


def text_mentions_bot(text: str, bot_username: str) -> bool:
    username = _normalize_bot_username(bot_username)
    if not username:
        return False
    return bool(re.search(rf"(?i)@{re.escape(username)}\b", str(text or "")))


def reply_targets_bot(message: dict[str, Any], bot_username: str = "", bot_user_id: str = "") -> bool:
    reply = message.get("reply_to_message")
    if not isinstance(reply, dict):
        return False
    user = reply.get("from")
    if not isinstance(user, dict):
        return False
    if bot_user_id and str(user.get("id") or "") == str(bot_user_id):
        return True
    username = _normalize_bot_username(bot_username)
    reply_username = _normalize_bot_username(str(user.get("username") or ""))
    return bool(username and reply_username == username)


def message_targets_bot(message: dict[str, Any], bot_username: str = "", bot_user_id: str = "") -> bool:
    chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
    chat_type = str(chat.get("type") or "")
    if chat_type not in NON_PRIVATE_CHAT_TYPES:
        return True
    text = str(message.get("text") or "")
    return text_mentions_bot(text, bot_username) or reply_targets_bot(message, bot_username, bot_user_id)


def strip_bot_addressing(text: str, bot_username: str) -> str:
    username = _normalize_bot_username(bot_username)
    cleaned = str(text or "").strip()
    if not username:
        return cleaned
    cleaned = re.sub(rf"(?i)^/([A-Za-z0-9_]+)@{re.escape(username)}\b", r"/\1", cleaned)
    cleaned = re.sub(rf"(?i)(^|\s)@{re.escape(username)}\b", " ", cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    return cleaned.strip()


def _normalize_chat_ref(value: str) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("@"):
        return "@" + text.lstrip("@")
    return text


def chat_identifier_candidates(chat: dict[str, Any]) -> set[str]:
    chat_id = str(chat.get("id") or "").strip()
    username = str(chat.get("username") or "").strip()
    candidates = {_normalize_chat_ref(chat_id)}
    normalized_username = _normalize_chat_ref(username)
    if normalized_username:
        candidates.add(normalized_username)
        candidates.add(_normalize_chat_ref(f"@{normalized_username.lstrip('@')}"))
    return {item for item in candidates if item}


def chat_is_allowed(settings: Settings, chat: dict[str, Any]) -> bool:
    allowed = {_normalize_chat_ref(item) for item in settings.ai_allowed_chat_ids if _normalize_chat_ref(item)}
    if not allowed:
        return False
    return bool(chat_identifier_candidates(chat) & allowed)


@dataclass(frozen=True)
class ParsedAlertRequest:
    symbol: str
    direction: str
    target_price: float


@dataclass(frozen=True)
class UserIntent:
    kind: str
    symbol: str = ""
    prompt: str = ""


class TelegramBotClient:
    def __init__(
        self,
        token: str,
        timeout_sec: int = 15,
        send_timeout_sec: int | None = None,
        retry_count: int = 1,
        retry_delay_sec: float = 0.8,
    ):
        self.token = token
        self.timeout_sec = max(3, int(timeout_sec))
        self.send_timeout_sec = max(self.timeout_sec, int(send_timeout_sec or self.timeout_sec))
        self.retry_count = max(1, int(retry_count))
        self.retry_delay_sec = max(0.0, float(retry_delay_sec))
        self.base_url = f"https://api.telegram.org/bot{token}"

    def _post_json(
        self,
        method: str,
        payload: dict[str, Any],
        timeout_sec: int | None = None,
        retry_count: int | None = None,
    ) -> dict[str, Any]:
        request_timeout = max(3, int(timeout_sec or self.timeout_sec))
        attempts = max(1, int(self.retry_count if retry_count is None else retry_count))
        for attempt in range(1, attempts + 1):
            try:
                response = requests.post(
                    f"{self.base_url}/{method}",
                    json=payload,
                    headers=HTTP_HEADERS,
                    timeout=request_timeout,
                )
                response.raise_for_status()
                data = response.json()
                return data if isinstance(data, dict) else {}
            except requests.RequestException:
                if attempt >= attempts:
                    raise
                if self.retry_delay_sec:
                    time.sleep(self.retry_delay_sec * attempt)
        raise RuntimeError(f"Telegram {method} request failed")

    def get_me(self) -> dict[str, Any]:
        response = requests.get(
            f"{self.base_url}/getMe",
            headers=HTTP_HEADERS,
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(str(data))
        result = data.get("result", {})
        return result if isinstance(result, dict) else {}

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "timeout": max(1, int(timeout)),
            "allowed_updates": json.dumps(["message", "callback_query"]),
        }
        if offset is not None:
            params["offset"] = int(offset)
        response = requests.get(
            f"{self.base_url}/getUpdates",
            params=params,
            headers=HTTP_HEADERS,
            timeout=max(self.timeout_sec, int(timeout) + 5),
        )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(str(data))
        result = data.get("result", [])
        return result if isinstance(result, list) else []

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        ok, _ = self._send_message_chunks(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        return ok

    def send_message_with_ids(
        self,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> list[int]:
        _, message_ids = self._send_message_chunks(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
        return message_ids

    def _send_message_chunks(
        self,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> tuple[bool, list[int]]:
        mode = parse_mode or infer_telegram_parse_mode(text)
        safe_text = (str(text or "").strip() if mode == "HTML" else telegram_plain_text(text)) or "（无内容）"
        chunks = split_telegram_text(safe_text) or ["（无内容）"]
        ok = True
        message_ids: list[int] = []
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if mode:
                payload["parse_mode"] = mode
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            data = self._post_json("sendMessage", payload, timeout_sec=self.send_timeout_sec)
            ok = ok and bool(data.get("ok"))
            result = data.get("result", {}) if isinstance(data.get("result"), dict) else {}
            try:
                if result.get("message_id") is not None:
                    message_ids.append(int(result["message_id"]))
            except (TypeError, ValueError):
                pass
        return ok, message_ids

    def delete_message(self, chat_id: str | int, message_id: int) -> bool:
        data = self._post_json(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": int(message_id)},
            timeout_sec=min(3, self.timeout_sec),
            retry_count=1,
        )
        return bool(data.get("ok"))

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:180]
        data = self._post_json("answerCallbackQuery", payload, timeout_sec=min(3, self.timeout_sec), retry_count=1)
        return bool(data.get("ok"))


def send_bot_message_safely(
    bot: TelegramBotClient,
    chat_id: str | int | None,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    *,
    context: str,
    parse_mode: str | None = None,
    delete_after_send: tuple[tuple[str | int, int], ...] = (),
) -> bool:
    if chat_id is None:
        return False
    try:
        try:
            return bot.send_message(
                chat_id,
                text,
                reply_markup=reply_markup,
                context=context,
                parse_mode=parse_mode,
                delete_after_send=delete_after_send,
            )  # type: ignore[call-arg]
        except TypeError:
            try:
                return bot.send_message(
                    chat_id,
                    text,
                    reply_markup=reply_markup,
                    context=context,
                    parse_mode=parse_mode,
                )  # type: ignore[call-arg]
            except TypeError:
                try:
                    return bot.send_message(chat_id, text, reply_markup=reply_markup, parse_mode=parse_mode)
                except TypeError:
                    try:
                        return bot.send_message(chat_id, text, reply_markup=reply_markup, context=context)  # type: ignore[call-arg]
                    except TypeError:
                        return bot.send_message(chat_id, text, reply_markup=reply_markup)
    except Exception as exc:
        print(f"ai-assistant: {context} send failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return False


@dataclass(frozen=True)
class QueuedTelegramMessage:
    chat_id: str | int
    text: str
    reply_markup: dict[str, Any] | None = None
    context: str = "queued"
    parse_mode: str | None = None
    delete_after_send: tuple[tuple[str | int, int], ...] = ()
    attempts: int = 0


class QueuedTelegramSender:
    def __init__(
        self,
        bot: TelegramBotClient,
        max_queue_size: int = 1000,
        max_send_attempts: int = 3,
        retry_delay_sec: float = 1.0,
    ):
        self.bot = bot
        self._queue: queue.Queue[QueuedTelegramMessage | None] = queue.Queue(maxsize=max(10, int(max_queue_size)))
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="paopao-ai-sender", daemon=True)
        self.max_send_attempts = max(1, int(max_send_attempts))
        self.retry_delay_sec = max(0.0, float(retry_delay_sec))

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        self._thread.join(timeout=timeout)

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        reply_markup: dict[str, Any] | None = None,
        *,
        context: str = "queued",
        parse_mode: str | None = None,
        delete_after_send: tuple[tuple[str | int, int], ...] = (),
    ) -> bool:
        try:
            self._queue.put_nowait(
                QueuedTelegramMessage(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=reply_markup,
                    context=context,
                    parse_mode=parse_mode,
                    delete_after_send=delete_after_send,
                )
            )
            return True
        except queue.Full:
            print(f"ai-assistant: send queue full context={context}", file=sys.stderr, flush=True)
            return False

    def pending_count(self) -> int:
        return self._queue.qsize()

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                continue
            started = time.time()
            try:
                sent = self.bot.send_message(item.chat_id, item.text, reply_markup=item.reply_markup, parse_mode=item.parse_mode)
                if not sent:
                    raise RuntimeError("sendMessage returned false")
                self._delete_messages(item.delete_after_send)
                elapsed = time.time() - started
                if elapsed >= 3:
                    print(f"ai-assistant: slow send context={item.context} elapsed={elapsed:.2f}s", flush=True)
            except Exception as exc:
                next_attempt = item.attempts + 1
                if next_attempt < self.max_send_attempts and not self._stop.is_set():
                    print(
                        f"ai-assistant: queued send retry context={item.context} "
                        f"attempt={next_attempt + 1}/{self.max_send_attempts} {type(exc).__name__}: {exc}",
                        file=sys.stderr,
                        flush=True,
                    )
                    if self.retry_delay_sec:
                        time.sleep(self.retry_delay_sec * next_attempt)
                    requeued = False
                    try:
                        self._queue.put_nowait(replace(item, attempts=next_attempt))
                        requeued = True
                    except queue.Full:
                        print(
                            f"ai-assistant: queued send retry dropped context={item.context} queue_full=1",
                            file=sys.stderr,
                            flush=True,
                        )
                    if requeued:
                        continue
                print(
                    f"ai-assistant: queued send failed context={item.context} {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            finally:
                self._queue.task_done()

    def _delete_messages(self, targets: tuple[tuple[str | int, int], ...]) -> None:
        for chat_id, message_id in targets:
            try:
                self.bot.delete_message(chat_id, message_id)
            except Exception as exc:
                print(
                    f"ai-assistant: delete temporary notice failed {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )


class SessionLockRegistry:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[str, threading.RLock] = {}

    def lock_for(self, key: str) -> threading.RLock:
        with self._guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock


def parse_alert_request(text: str) -> ParsedAlertRequest | None:
    clean = text.strip()
    if not clean:
        return None

    direction = ""
    if re.search(r"(跌破|跌到|低于|小于|below|down|<=|<)", clean, flags=re.IGNORECASE):
        direction = "below"
    elif re.search(r"(涨到|高于|突破|大于|above|up|>=|>)", clean, flags=re.IGNORECASE):
        direction = "above"
    if not direction:
        parts = clean.split()
        if len(parts) >= 3 and parts[1].lower() in {"above", "below", ">=", "<=", ">", "<"}:
            direction = "above" if parts[1].lower() in {"above", ">=", ">"} else "below"

    price_match = re.search(r"(?<![A-Za-z])([0-9]+(?:\.[0-9]+)?[kKmM]?)(?![A-Za-z])", clean)
    symbol_match = re.search(r"\b([A-Za-z][A-Za-z0-9]{1,20})(?:USDT)?\b", clean)
    if not direction or not price_match or not symbol_match:
        return None

    symbol = normalize_symbol(symbol_match.group(1))
    price = parse_price(price_match.group(1))
    return ParsedAlertRequest(symbol=symbol, direction=direction, target_price=price)


def is_alert_intent(text: str) -> bool:
    return bool(ALERT_CREATE_INTENT_RE.search(text))


def is_analysis_intent(text: str) -> bool:
    return bool(ANALYSIS_INTENT_RE.search(text))


def strip_analysis_request(text: str) -> str:
    clean = str(text or "").strip()
    clean = ANALYSIS_INTENT_RE.sub("", clean, count=1).strip()
    clean = clean.lstrip("：:，, \n\t")
    return clean


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").strip())


def extract_symbol_text(text: str) -> str:
    clean = str(text or "").strip()
    for alias, symbol in SYMBOL_ALIASES.items():
        if alias in clean:
            return symbol
    for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9]{1,20})(?:USDT)?\b", clean):
        token = match.group(1).upper()
        if token in SYMBOL_STOP_WORDS:
            continue
        return token
    return ""


def is_price_query(text: str) -> bool:
    clean = compact_text(text)
    if not clean:
        return False
    if is_analysis_intent(clean) or is_market_data_intent(text):
        return False
    if "\n" in str(text or ""):
        return False
    if len(clean) > 48:
        return False
    symbol = extract_symbol_text(clean)
    if not symbol:
        return False
    if PRICE_QUERY_RE.search(clean):
        return True
    return bool(re.fullmatch(r"(?i)[A-Z][A-Z0-9]{1,20}(?:USDT)?", clean))


def is_market_data_intent(text: str) -> bool:
    clean = str(text or "").strip()
    if not clean:
        return False
    lowered = clean.lower()
    keyword_hits = sum(1 for item in MARKET_DATA_KEYWORDS if item.lower() in lowered)
    if keyword_hits >= 2:
        return True
    if "\n" in clean and keyword_hits >= 1:
        return True
    if re.search(r"\b(?:15m|30m|1h|4h|24h)\b", clean, flags=re.IGNORECASE) and "%" in clean:
        return True
    metric_hits = len(re.findall(r"[-+]?\d+(?:\.\d+)?\s*%", clean))
    if metric_hits >= 2 and re.search(r"(价格|成交|OI|CVD|费率|市值|流动性)", clean, flags=re.IGNORECASE):
        return True
    return False


def classify_user_intent(text: str) -> UserIntent:
    clean = str(text or "").strip()
    if not clean:
        return UserIntent("home")
    lowered = clean.lower()
    start_match = re.fullmatch(
        r"/start(?:@[A-Za-z0-9_]+)?(?:\s+(?:(analyze|alert)_([A-Za-z0-9]{2,20})))?",
        clean,
        flags=re.IGNORECASE,
    )
    if start_match:
        action = str(start_match.group(1) or "").lower()
        raw_symbol = extract_symbol_text(start_match.group(2) or "")
        symbol = normalize_symbol(raw_symbol) if raw_symbol else ""
        if action == "analyze" and symbol:
            return UserIntent("dossier", symbol=symbol, prompt=f"{symbol} 怎么看")
        if action == "alert" and symbol:
            return UserIntent("alert_deep", symbol=symbol)
        return UserIntent("home")
    if clean.startswith("/"):
        return UserIntent("command")
    if is_analysis_intent(clean):
        prompt = strip_analysis_request(clean)
        return UserIntent("analysis", prompt=prompt)
    if is_market_data_intent(clean):
        return UserIntent("analysis", prompt=clean)
    if is_alert_intent(clean):
        return UserIntent("alert_setup")
    if is_symbol_dossier_request(clean) and not is_price_query(clean):
        return UserIntent("dossier", prompt=clean)
    if is_price_query(clean):
        return UserIntent("price", symbol=extract_symbol_text(clean))
    if ambiguous_alert_text(clean):
        return UserIntent("ambiguous_alert")
    return UserIntent("assistant", prompt=clean)


def ambiguous_alert_text(text: str) -> ParsedAlertRequest | None:
    if is_alert_intent(text):
        return None
    return parse_alert_request(text)


def is_authorized(settings: Settings, user_id: str) -> bool:
    allowed = set(settings.ai_admin_user_ids)
    if allowed and user_id not in allowed:
        return False
    return True


def user_label(message: dict[str, Any]) -> tuple[str, str]:
    user = message.get("from", {}) if isinstance(message.get("from"), dict) else {}
    user_id = str(user.get("id") or "")
    username = str(user.get("username") or user.get("first_name") or "")
    return user_id, username


def alert_display_number(alerts: list[PriceAlert], alert_id: int) -> int | None:
    for display_no, alert in enumerate(alerts[:30], start=1):
        if alert.id == alert_id:
            return display_no
    return None


def alert_market_quote(alert: PriceAlert) -> AlertMarketQuote:
    return AlertMarketQuote(
        exchange=alert.exchange,
        market_type=alert.market_type,
        symbol=alert.symbol,
        pair=alert.pair or alert.symbol,
        price=alert.last_price or alert.target_price or 0,
    )


def alert_exchange_link(alert: PriceAlert) -> str:
    quote = alert_market_quote(alert)
    url = html.escape(coinglass_quote_url(quote), quote=True)
    label = html.escape(alert.exchange_label)
    return f'<a href="{url}"><b>{label}</b></a>'


def alert_pair_code(alert: PriceAlert) -> str:
    return telegram_code(alert.pair or alert.symbol)


def telegram_code(value: Any) -> str:
    return f"<code>{html.escape(str(value or ''))}</code>"


def alert_condition_short(alert: PriceAlert) -> str:
    if alert.alert_type == "target_price":
        return f"价格 {alert.direction_label} {format_price(alert.target_price)}"
    if alert.alert_type in {"price_change", "oi_change"}:
        return f"{alert.timeframe_label} {alert.direction_label}超过 {alert.threshold_pct:g}%"
    return "资金费率周期缩短，或出现极正/极负资金费率"


def alert_created_text(alert: PriceAlert, display_no: int | None = None) -> str:
    detail = [f"类型：{alert.alert_type_label}"]
    if alert.alert_type == "target_price":
        detail.append(f"条件：价格 {alert.direction_label} {format_price(alert.target_price)}")
    elif alert.alert_type in {"price_change", "oi_change"}:
        detail.append(f"条件：{alert.timeframe_label} {alert.direction_label}超过 {alert.threshold_pct:g}%")
    else:
        detail.append("条件：资金费率周期缩短，或出现极正/极负资金费率")
    detail.append(f"触发方式：{alert.repeat_policy_label}")
    display_label = str(display_no) if display_no is not None else "已创建"
    return "\n".join(
        [
            "已创建监控提醒",
            "",
            f"编号：{display_label}",
            f"币种：{html.escape(alert.symbol)}",
            f"交易所：{alert_exchange_link(alert)}",
            f"市场：{html.escape(alert.market_type_label)}",
            f"交易对：{alert_pair_code(alert)}",
            *(html.escape(item) for item in detail),
            "",
            "管理：点击首页“我的提醒”，可以暂停、恢复或删除。",
        ]
    )


def alert_trigger_text(alert: PriceAlert, price: float, detail: str = "", display_no: int | None = None) -> str:
    ending = "这条提醒已经标记为已触发，不会重复发送。" if alert.repeat_policy == "once" else f"这条提醒会继续运行：{alert.repeat_policy_label}。"
    lines = [
        f"{alert.alert_type_label}已触发",
        "",
        f"币种：{html.escape(alert.symbol)}",
        f"交易所：{alert_exchange_link(alert)}",
        f"市场：{html.escape(alert.market_type_label)}",
        f"交易对：{alert_pair_code(alert)}",
        f"条件：{html.escape(alert_condition_short(alert))}",
        f"当前价：{format_price(price)}",
    ]
    if detail.strip():
        lines.append(html.escape(detail.strip()))
    if display_no is not None:
        lines.append(f"提醒编号：{display_no}")
    lines.extend(["", html.escape(ending)])
    return "\n".join(lines)


def list_alerts_text(alerts: list[PriceAlert]) -> str:
    if not alerts:
        return "当前没有价格提醒。点击首页“设置价格提醒”即可创建。"
    lines = ["你的价格提醒：", ""]
    status_map = {"active": "运行中", "paused": "已暂停", "triggered": "已触发"}
    for display_no, alert in enumerate(alerts[:30], start=1):
        lines.append(
            f"{display_no}. {html.escape(alert.alert_type_label)}｜{alert_exchange_link(alert)} {alert_pair_code(alert)}｜"
            f"{html.escape(alert_condition_short(alert))}｜{html.escape(alert.repeat_policy_label)} "
            f"[{html.escape(status_map.get(alert.status, alert.status))}]"
        )
    return "\n".join(lines)


COINGLASS_EXCHANGE_SLUGS = {
    "binance": "Binance",
    "bybit": "Bybit",
    "okx": "OKX",
    "bitget": "Bitget",
    "gate": "Gate",
}
PRICE_TABLE_PRICE_HEADER = "价格"


def coinglass_quote_url(quote: AlertMarketQuote) -> str:
    exchange = COINGLASS_EXCHANGE_SLUGS.get(quote.exchange, quote.exchange_label)
    pair = quote.pair or quote.symbol
    path = f"{exchange}_{pair}"
    if quote.market_type == "spot":
        path = f"SPOT_{path}"
    return f"https://www.coinglass.com/tv/zh/{url_quote(path, safe='-_')}"


def text_display_width(text: str) -> int:
    return sum(2 if ord(char) > 127 else 1 for char in str(text or ""))


def pad_display_right(text: str, width: int) -> str:
    value = str(text or "")
    return value + " " * max(0, width - text_display_width(value))


def pad_display_left(text: str, width: int) -> str:
    value = str(text or "")
    return " " * max(0, width - text_display_width(value)) + value


def price_quote_multiplier(quote: AlertMarketQuote) -> int:
    if quote.market_type != "futures":
        return 1
    return contract_pair_multiplier(quote.pair, quote.symbol)


def price_quote_display_price(quote: AlertMarketQuote) -> float:
    multiplier = price_quote_multiplier(quote)
    if multiplier <= 1:
        return quote.price
    return quote.price / multiplier


def sort_price_quotes(quotes: list[AlertMarketQuote]) -> list[AlertMarketQuote]:
    exchange_order = {exchange: index for index, exchange in enumerate(ALERT_MARKET_EXCHANGES)}
    market_order = {market_type: index for index, market_type in enumerate(ALERT_MARKET_TYPES)}
    return sorted(
        quotes,
        key=lambda quote: (
            market_order.get(quote.market_type, 99),
            exchange_order.get(quote.exchange, 99),
            quote.pair,
        ),
    )


def price_quote_widths(quotes: list[AlertMarketQuote]) -> tuple[int, int, int]:
    exchange_width = max([text_display_width("交易所"), *(text_display_width(quote.exchange_label) for quote in quotes)], default=text_display_width("交易所"))
    pair_width = max([text_display_width("交易对"), *(text_display_width(quote.pair) for quote in quotes)], default=text_display_width("交易对"))
    price_width = max([text_display_width(PRICE_TABLE_PRICE_HEADER), *(text_display_width(format_price(price_quote_display_price(quote))) for quote in quotes)], default=text_display_width(PRICE_TABLE_PRICE_HEADER))
    return exchange_width, pair_width, price_width


def price_quote_exchange_link(quote: AlertMarketQuote, *, bold: bool = False) -> str:
    url = html.escape(coinglass_quote_url(quote), quote=True)
    label = html.escape(quote.exchange_label)
    if bold:
        label = f"<b>{label}</b>"
    return f'<a href="{url}">{label}</a>'


def price_quote_table_block(quotes: list[AlertMarketQuote], widths: tuple[int, int, int] | None = None) -> str:
    sorted_quotes = sort_price_quotes(quotes)
    exchange_width, pair_width, price_width = widths or price_quote_widths(sorted_quotes)
    rows = [
        f"{pad_display_right('交易所', exchange_width)}  "
        f"{pad_display_right('交易对', pair_width)}  "
        f"{pad_display_left(PRICE_TABLE_PRICE_HEADER, price_width)}"
    ]
    for quote in sorted_quotes:
        rows.append(
            f"{pad_display_right(quote.exchange_label, exchange_width)}  "
            f"{pad_display_right(quote.pair, pair_width)}  "
            f"{pad_display_left(format_price(price_quote_display_price(quote)), price_width)}"
        )
    return f"<pre>{html.escape(chr(10).join(rows))}</pre>"


def price_quote_links_line(quotes: list[AlertMarketQuote]) -> str:
    links = [price_quote_exchange_link(quote, bold=True) for quote in sort_price_quotes(quotes)]
    return f"K线：{' / '.join(links)}" if links else ""


def price_text_from_quotes(symbol: str, quotes: list[AlertMarketQuote]) -> str:
    if not quotes:
        return f"没有从 Binance、Bybit、OKX、Bitget、Gate 里读到 {symbol} 的现货或合约价格。"
    sorted_quotes = sort_price_quotes(quotes)
    widths = price_quote_widths(sorted_quotes)
    futures = [quote for quote in sorted_quotes if quote.market_type == "futures"]
    spot = [quote for quote in sorted_quotes if quote.market_type == "spot"]
    lines = [f"{symbol} 多交易所价格", ""]
    if futures:
        lines.append("合约：")
        lines.append(price_quote_table_block(futures, widths))
        links = price_quote_links_line(futures)
        if links:
            lines.append(links)
        lines.append("")
    if spot:
        lines.append("现货：")
        lines.append(price_quote_table_block(spot, widths))
        links = price_quote_links_line(spot)
        if links:
            lines.append(links)
        lines.append("")
    if any(price_quote_multiplier(quote) > 1 for quote in sorted_quotes):
        lines.append("说明：价格列已折算为单币价格；1000/10000/1000000 合约交易对仍保留交易所原始名称。")
        lines.append("")
    lines.extend([
        "可继续发送：",
        f"{symbol.replace('USDT', '')} 怎么看",
        "或点击首页“设置价格提醒”。",
    ])
    return "\n".join(lines).strip()


def price_text(settings: Settings, symbol_text: str) -> str:
    symbol = normalize_symbol(symbol_text)
    return price_text_from_quotes(symbol, discover_alert_markets(settings, symbol))


def price_reply(settings: Settings, symbol_text: str) -> BotReply:
    symbol = normalize_symbol(symbol_text)
    quotes = discover_alert_markets(settings, symbol)
    return BotReply(price_text_from_quotes(symbol, quotes), parse_mode="HTML")


def session_key(chat_id: str | int, user_id: str | int) -> str:
    return f"{chat_id}:{user_id}"


def message_session_key(message: dict[str, Any]) -> str:
    chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
    user_id, _ = user_label(message)
    return session_key(str(chat.get("id") or ""), user_id)


def callback_session_key(query: dict[str, Any]) -> str:
    message = query.get("message", {}) if isinstance(query.get("message"), dict) else {}
    chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
    user = query.get("from", {}) if isinstance(query.get("from"), dict) else {}
    return session_key(str(chat.get("id") or ""), str(user.get("id") or ""))


def current_price_for_alert_market(settings: Settings, quote: AlertMarketQuote) -> AlertMarketQuote | None:
    return fetch_alert_market_quote(settings, quote.symbol, quote.exchange, quote.market_type, quote.pair)


def infer_alert_direction(target_price: float, current_price: float | None = None, fallback: str = "above") -> str:
    if fallback in {"above", "below"}:
        return fallback
    if current_price is not None and current_price > 0:
        return "above" if target_price >= current_price else "below"
    return "above"


def monitor_confirmation_text(pending: dict[str, Any]) -> str:
    alert_type = str(pending.get("alert_type") or "target_price")
    symbol = str(pending.get("symbol") or "")
    exchange = str(pending.get("exchange") or "binance")
    market = str(pending.get("market_type_label") or pending.get("market_type") or "")
    pair = str(pending.get("pair") or symbol)
    quote = AlertMarketQuote(
        exchange=exchange,
        market_type=str(pending.get("market_type") or "futures"),
        symbol=symbol,
        pair=pair,
        price=float(pending.get("current_price") or pending.get("target_price") or 0),
    )
    direction = str(pending.get("direction") or "both")
    direction_label = {"up": "上涨", "down": "下跌", "both": "双向", "above": "高于或等于", "below": "低于或等于"}.get(direction, direction)
    repeat_label = str(pending.get("repeat_label") or "提醒一次")
    lines = [
        "请确认添加监控提醒",
        "",
        f"类型：{ {'target_price': '目标价提醒', 'price_change': '价格急涨急跌', 'oi_change': '持仓量变化', 'funding_change': '资金费率变化'}.get(alert_type, alert_type) }",
        f"币种：{html.escape(symbol)}",
        f"交易所：{price_quote_exchange_link(quote, bold=True)}",
        f"市场：{html.escape(market)}",
        f"交易对：{telegram_code(pair)}",
    ]
    if alert_type == "target_price":
        lines.extend([
            f"目标价：{format_price(float(pending.get('target_price') or 0))}",
            f"触发条件：价格 {direction_label} {format_price(float(pending.get('target_price') or 0))}",
        ])
    elif alert_type in {"price_change", "oi_change"}:
        lines.extend([
            f"时间窗口：{pending.get('timeframe_label')}",
            f"波动阈值：{pending.get('threshold_pct')}%",
            f"方向：{direction_label}",
        ])
    else:
        lines.extend([
            "监控内容：结算周期缩短、极正/极负资金费率",
            "说明：会读取多交易所资金费率快照；当前选择的交易所用于这条个人提醒的价格源和备注。",
        ])
    lines.extend([
        f"提醒方式：{repeat_label}",
        "",
        "确认后才会创建提醒；取消则不会保存。",
    ])
    return "\n".join(lines)


def start_alert_setup_session(sessions: dict[str, dict[str, Any]], key: str) -> BotReply:
    sessions[key] = {"state": "alert_kind", "created_at": int(time.time())}
    return BotReply(ALERT_SETUP_TEXT, alert_kind_markup())


def handle_alert_setup_session(
    settings: Settings,
    sessions: dict[str, dict[str, Any]],
    message: dict[str, Any],
    text: str,
) -> BotReply | None:
    key = message_session_key(message)
    session = sessions.get(key)
    if not session:
        return None
    lowered = text.strip().lower()
    if lowered == "取消":
        sessions.pop(key, None)
        return BotReply("已取消价格提醒设置。", main_menu_markup())

    state = str(session.get("state") or "")
    if state == "alert_symbol":
        alert_type = str(session.get("alert_type") or "target_price")
        symbol_text = extract_symbol_text(text) or text
        try:
            symbol = normalize_symbol(symbol_text)
        except Exception as exc:
            return BotReply(f"没有识别出币种：{exc}\n请只输入币种简称，例如 BTC、ETH、DOGE。", cancel_markup())
        quotes = discover_alert_markets(settings, symbol)
        if alert_type in {"oi_change", "funding_change"}:
            quotes = [quote for quote in quotes if quote.market_type == "futures"]
        if not quotes:
            return BotReply(
                f"没有在 Binance、Bybit、OKX、Bitget、Gate 里识别到 {symbol} 的可用价格源。\n"
                "可能是这个币没有 USDT 交易对，也可能是交易所接口临时失败。你可以换个币种再试。",
                cancel_markup(),
            )
        session.update({
            "state": "alert_market",
            "symbol": symbol,
            "quotes": [quote_to_dict(quote) for quote in quotes],
        })
        market_types = sorted({quote.market_type for quote in quotes})
        if alert_type in {"oi_change", "funding_change"} or len(market_types) == 1:
            selected_market = market_types[0]
            filtered = [quote for quote in quotes if quote.market_type == selected_market]
            session.update({"state": "alert_exchange", "market_type": selected_market})
            market_label = filtered[0].market_type_label if filtered else selected_market
            return BotReply(
                "\n".join([
                    f"已识别币种：{symbol}",
                    f"可用市场：{market_label}",
                    "",
                    "请选择交易所：",
                ]),
                exchange_markup(filtered),
            )
        return BotReply(
            "\n".join([
                f"已识别币种：{symbol}",
                "",
                "请选择市场类型：",
            ]),
            market_type_markup(quotes),
        )

    if state == "alert_price":
        symbol = str(session.get("symbol") or "")
        selected = session.get("selected_quote")
        quote = quote_from_dict(selected) if isinstance(selected, dict) else None
        if not symbol or not quote:
            sessions.pop(key, None)
            return BotReply("会话已失效，请重新设置价格提醒。", main_menu_markup())
        try:
            target_price = parse_price(text)
        except Exception as exc:
            return BotReply(f"价格格式不正确：{exc}\n请发送数字，例如 58000 或 0.35。", cancel_markup())
        fresh_quote = current_price_for_alert_market(settings, quote) or quote
        current_price = fresh_quote.price
        direction = infer_alert_direction(target_price, current_price, fallback="")
        session.update({
            "state": "alert_repeat",
            "pending_alert": {
                "alert_type": "target_price",
                "symbol": symbol,
                "exchange": fresh_quote.exchange,
                "exchange_label": fresh_quote.exchange_label,
                "market_type": fresh_quote.market_type,
                "market_type_label": fresh_quote.market_type_label,
                "pair": fresh_quote.pair,
                "direction": direction,
                "target_price": target_price,
                "current_price": current_price,
                "repeat_policy": "once",
                "repeat_interval_sec": 0,
                "repeat_label": "提醒一次",
            },
        })
        return BotReply(
            "请选择触发后的提醒方式：\n\n提醒一次：触发后自动停止。\n重复提醒：价格重新穿越条件时再次提醒。\n持续提醒：条件持续满足时每5分钟提醒一次。",
            repeat_policy_markup(),
        )

    sessions.pop(key, None)
    return BotReply("会话已失效，请重新设置价格提醒。", main_menu_markup())


def create_alert_from_context(
    store: PriceAlertStore,
    user_id: str,
    chat_id: str,
    username: str,
    symbol: str,
    direction: str,
    target_price: float,
    source: str,
    note: str,
    exchange: str = "binance",
    market_type: str = "futures",
    pair: str | None = None,
    alert_type: str = "target_price",
    timeframe_sec: int = 0,
    threshold_pct: float = 0.0,
    repeat_policy: str = "once",
    repeat_interval_sec: int = 0,
) -> PriceAlert:
    return store.create_alert(
        user_id=user_id,
        chat_id=chat_id,
        username=username,
        symbol=symbol,
        exchange=exchange,
        market_type=market_type,
        pair=pair,
        direction=direction,
        target_price=target_price,
        source=source,
        note=note,
        alert_type=alert_type,
        timeframe_sec=timeframe_sec,
        threshold_pct=threshold_pct,
        repeat_policy=repeat_policy,
        repeat_interval_sec=repeat_interval_sec,
    )


def runtime_context(settings: Settings) -> str:
    parts: list[str] = []
    for label, path in (("主服务", settings.runtime_status_path),):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        parts.append(
            f"{label}: status={data.get('status')}, task={data.get('task')}, "
            f"updated_at={data.get('updated_at')}, last_error={data.get('last_error') or '无'}"
        )
    return "\n".join(parts) or "暂时没有 runtime-status 数据。"


def build_chat_completion_payload(settings: Settings, system_prompt: str, user_content: str) -> dict[str, Any]:
    model = normalize_ai_model(settings.ai_model)
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
        "stream": False,
    }
    if model.startswith("deepseek-v4"):
        payload["thinking"] = {"type": "enabled"}
        payload["reasoning_effort"] = "high"
    return payload


def raise_for_ai_response(response: requests.Response) -> None:
    try:
        response.raise_for_status()
    except requests.HTTPError as exc:
        body = str(getattr(response, "text", "") or "").strip()
        if len(body) > 800:
            body = body[:800] + "..."
        status = getattr(response, "status_code", "")
        reason = str(getattr(response, "reason", "") or "").strip()
        prefix = f"{status} {reason}".strip()
        detail = body or str(exc)
        raise RuntimeError(f"{prefix}: {detail}" if prefix else detail) from exc


def ai_request_timeout_sec(settings: Settings) -> int:
    return max(5, int(settings.ai_request_timeout_sec))


def raise_for_ai_request_exception(exc: requests.RequestException, timeout_sec: int) -> None:
    message = str(exc)
    lower = message.lower()
    if isinstance(exc, requests.Timeout) or "timed out" in lower or "read timeout" in lower:
        raise RuntimeError(
            f"AI 接口响应超时（已等待 {timeout_sec} 秒）。"
            "deepseek-v4-pro 思考模式可能需要更久；"
            "请在 Web 后台把“AI 请求超时秒数”设为 90-180，或临时改用 deepseek-v4-flash。"
        ) from exc
    raise RuntimeError(f"AI 接口连接失败：{message}") from exc


def post_chat_completion(settings: Settings, payload: dict[str, Any]) -> dict[str, Any]:
    timeout_sec = ai_request_timeout_sec(settings)
    try:
        response = requests.post(
            f"{settings.ai_base_url.rstrip('/')}/chat/completions",
            headers={
                **HTTP_HEADERS,
                "Authorization": f"Bearer {settings.ai_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=timeout_sec,
        )
    except requests.RequestException as exc:
        raise_for_ai_request_exception(exc, timeout_sec)
    raise_for_ai_response(response)
    data = response.json()
    return data if isinstance(data, dict) else {}


def content_to_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(part.strip() for part in parts if part and part.strip()).strip()
    return ""


def extract_ai_reply_text(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
    if isinstance(choices, list) and choices:
        choice = choices[0] if isinstance(choices[0], dict) else {}
        message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
        for value in (
            message.get("content"),
            choice.get("text"),
            message.get("output_text"),
        ):
            text = content_to_text(value)
            if text:
                return text
    for value in (response_payload.get("output_text"), response_payload.get("text")):
        text = content_to_text(value)
        if text:
            return text
    return ""


def ai_response_has_reasoning_only(response_payload: dict[str, Any]) -> bool:
    choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
    if not isinstance(choices, list) or not choices:
        return False
    choice = choices[0] if isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    return bool(str(message.get("reasoning_content") or message.get("reasoning") or "").strip())


def empty_ai_response_message(response_payload: dict[str, Any]) -> str:
    choices = response_payload.get("choices") if isinstance(response_payload, dict) else None
    finish_reason = ""
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish_reason = str(choices[0].get("finish_reason") or "").strip()
    if ai_response_has_reasoning_only(response_payload):
        return (
            "AI 接口只返回了思考过程，没有返回正式正文。"
            "程序已经自动重试一次仍未拿到正文；可以在 Web 后台把模型临时改成 deepseek-v4-flash，"
            "或稍后重新发送分析。"
        )
    if finish_reason:
        return f"AI 接口没有返回正文（finish_reason={finish_reason}）。请稍后重试，或切换模型。"
    return "AI 接口没有返回正文。请稍后重试，或切换模型。"


def retry_payload_without_thinking(payload: dict[str, Any]) -> dict[str, Any]:
    retry_payload = dict(payload)
    retry_payload["messages"] = [
        dict(message)
        for message in payload.get("messages", [])
        if isinstance(message, dict)
    ]
    retry_payload["thinking"] = {"type": "disabled"}
    retry_payload.pop("reasoning_effort", None)
    retry_payload["messages"].append({
        "role": "user",
        "content": "上一次接口没有返回正式正文。请直接输出最终回复正文，不要只返回思考过程。",
    })
    return retry_payload


def complete_ai_text(settings: Settings, payload: dict[str, Any]) -> str:
    data = post_chat_completion(settings, payload)
    reply = extract_ai_reply_text(data)
    if reply:
        return reply
    model = str(payload.get("model") or "")
    if payload.get("thinking") or model.startswith("deepseek-v4"):
        retry_data = post_chat_completion(settings, retry_payload_without_thinking(payload))
        retry_reply = extract_ai_reply_text(retry_data)
        if retry_reply:
            return retry_reply
        return empty_ai_response_message(retry_data)
    return empty_ai_response_message(data)


def call_ai_provider(
    settings: Settings,
    user_text: str,
    store: PriceAlertStore,
    user_id: str,
    *,
    mode: str = "assistant",
) -> str:
    if not settings.ai_provider_enable or not settings.ai_api_key:
        if mode == "analyst":
            return "AI 分析接口还没有启用。请在 Web 后台配置 AI_API_KEY、AI_BASE_URL、AI_MODEL，并开启 AI_PROVIDER_ENABLE。"
        return local_assistant_reply(settings, store, user_id)
    alerts = store.list_alerts(user_id=user_id, limit=20)
    alert_lines = [
        f"{item.id}. {item.symbol} {item.direction_label} {format_price(item.target_price)} {item.status}"
        for item in alerts
    ]
    prompts = load_ai_prompts(settings)
    prompt_map = prompts.get("prompts", {}) if isinstance(prompts.get("prompts"), dict) else {}
    if mode == "analyst":
        system_prompt = str(prompt_map.get("analyst_prompt") or "").strip()
        context = "用户提供的数据："
    else:
        system_prompt = str(prompt_map.get("assistant_prompt") or "").strip()
        context = "\n".join(
            [
                "当前运行状态：",
                runtime_context(settings),
                "",
                "用户价格提醒：",
                "\n".join(alert_lines) if alert_lines else "暂无",
            ]
        )
    payload = build_chat_completion_payload(settings, system_prompt, f"{context}\n\n用户问题：{user_text}")
    return complete_ai_text(settings, payload)


def build_symbol_dossier_reply(settings: Settings, store: PriceAlertStore, user_id: str, user_text: str) -> str:
    dossier = build_symbol_dossier(settings, user_text)
    local_report = format_symbol_dossier_report(dossier)
    if not settings.ai_provider_enable or not settings.ai_api_key:
        return local_report
    context = format_symbol_dossier_ai_context(dossier, user_text)
    try:
        return call_ai_provider(settings, context, store, user_id, mode="analyst")
    except Exception as exc:
        return "\n".join([
            local_report,
            "",
            f"AI 增强分析失败：{type(exc).__name__}: {exc}",
        ])


def local_assistant_reply(settings: Settings, store: PriceAlertStore, user_id: str) -> str:
    stats = store.stats()
    return "\n".join(
        [
            "AI 对话接口还没有启用。",
            "",
            "现在已经可以使用价格提醒和本地状态助手：",
            "- 点击首页“设置价格提醒”创建提醒",
            "- 点击首页“我的提醒”管理提醒",
            "- 直接发送 BTC 查询五大交易所价格",
            "",
            f"提醒统计：运行中 {stats.get('active', 0)}，暂停 {stats.get('paused', 0)}，已触发 {stats.get('triggered', 0)}。",
            "",
            "如果要启用真正 AI 问答，请在 Web 后台配置 AI_API_KEY、AI_BASE_URL、AI_MODEL，并开启 AI_PROVIDER_ENABLE。",
        ]
    )


def handle_message(
    settings: Settings,
    store: PriceAlertStore,
    message: dict[str, Any],
    bot_username: str = "",
    bot_user_id: str = "",
) -> str | None:
    raw_text = str(message.get("text") or "").strip()
    if not raw_text:
        return None
    chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "")
    chat_type = str(chat.get("type") or "")
    user_id, username = user_label(message)
    if not user_id or not chat_id:
        return None
    if chat_type in NON_PRIVATE_CHAT_TYPES and not message_targets_bot(message, bot_username, bot_user_id):
        return None
    if chat_type in NON_PRIVATE_CHAT_TYPES:
        if not settings.ai_allow_group_chat:
            return "AI 助手还没有开启群内调用。请在 Web 后台开启 AI_ALLOW_GROUP_CHAT。"
        if not chat_is_allowed(settings, chat):
            return "这个群没有开通 AI 助手。请在 Web 后台的 AI_ALLOWED_CHAT_IDS 里加入当前群 ID 或频道用户名。"
    if not is_authorized(settings, user_id):
        return "你没有使用这个 AI 助手 Bot 的权限。请在 Web 后台配置 AI_ADMIN_USER_IDS。"

    text = strip_bot_addressing(raw_text, bot_username)
    if not text:
        return HOME_TEXT

    intent = classify_user_intent(text)
    if intent.kind == "home":
        return HOME_TEXT
    if intent.kind == "command":
        return "现在只保留 /start。查价格直接发送 BTC；看行情直接发送 BTC 怎么看；提醒请点击首页“设置价格提醒”。"

    if intent.kind == "dossier":
        try:
            return build_symbol_dossier_reply(settings, store, user_id, intent.prompt or text)
        except Exception as exc:
            return user_facing_error("币种档案查询", exc)

    if intent.kind == "analysis":
        prompt = intent.prompt
        if not prompt:
            return "请把要分析的雷达信号、资金费率或市场数据一起发过来。"
        try:
            return call_ai_provider(settings, prompt, store, user_id, mode="analyst")
        except Exception as exc:
            return user_facing_error("AI 分析", exc)

    if intent.kind == "alert_setup":
        return "价格提醒需要手动选择交易所和价格源。请点击首页“设置价格提醒”，按步骤确认后才会创建。"

    if intent.kind == "alert_deep":
        return f"已识别 {intent.symbol}。请使用按钮式会话继续选择提醒类型、市场与交易所，确认前不会创建提醒。"

    if intent.kind == "price":
        try:
            return price_text(settings, intent.symbol)
        except Exception as exc:
            return user_facing_error("价格查询", exc)

    if intent.kind == "ambiguous_alert":
        return "如果这是价格提醒，请点击首页“设置价格提醒”。如果你想让我分析这句话，可以直接说“帮我分析这段：...”再粘贴内容。"

    if settings.ai_provider_enable and settings.ai_api_key:
        try:
            return call_ai_provider(settings, text, store, user_id)
        except Exception as exc:
            return user_facing_error("AI 回答", exc)
    return local_assistant_reply(settings, store, user_id)


def handle_message_reply(
    settings: Settings,
    store: PriceAlertStore,
    message: dict[str, Any],
    bot_username: str = "",
    bot_user_id: str = "",
    sessions: dict[str, dict[str, Any]] | None = None,
) -> BotReply | None:
    raw_text = str(message.get("text") or "").strip()
    if not raw_text:
        return None
    chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "")
    chat_type = str(chat.get("type") or "")
    user_id, username = user_label(message)
    if not user_id or not chat_id:
        return None
    if chat_type in NON_PRIVATE_CHAT_TYPES and not message_targets_bot(message, bot_username, bot_user_id):
        return None
    if chat_type in NON_PRIVATE_CHAT_TYPES:
        if not settings.ai_allow_group_chat:
            return BotReply("AI 助手还没有开启群内调用。请在 Web 后台开启 AI_ALLOW_GROUP_CHAT。")
        if not chat_is_allowed(settings, chat):
            return BotReply("这个群没有开通 AI 助手。请在 Web 后台的 AI_ALLOWED_CHAT_IDS 里加入当前群 ID 或频道用户名。")
    if not is_authorized(settings, user_id):
        return BotReply("你没有使用这个 AI 助手 Bot 的权限。请在 Web 后台配置 AI_ADMIN_USER_IDS。")

    text = strip_bot_addressing(raw_text, bot_username)
    key = session_key(chat_id, user_id)
    active_sessions = sessions if sessions is not None else {}
    intent = classify_user_intent(text)

    if text.lower() == "取消":
        active_sessions.pop(key, None)
        return BotReply("已取消。", main_menu_markup())
    if intent.kind == "home":
        active_sessions.pop(key, None)
        return BotReply(HOME_TEXT, main_menu_markup())
    if intent.kind == "command":
        return BotReply(
            "现在只保留 /start。查价格直接发送 BTC；看行情直接发送 BTC 怎么看；提醒请点击首页“设置价格提醒”。",
            main_menu_markup(),
        )

    session_reply = handle_alert_setup_session(settings, active_sessions, message, text)
    if session_reply:
        return session_reply

    if intent.kind == "alert_setup":
        active_sessions.pop(key, None)
        return BotReply(
            "价格提醒需要手动选择交易所和价格源。\n\n请点击首页里的「设置价格提醒」，然后按流程选择现货/合约、交易所和目标价。",
            main_menu_markup(),
        )

    if intent.kind == "alert_deep":
        active_sessions[key] = {
            "state": "alert_kind",
            "created_at": int(time.time()),
            "prefill_symbol": intent.symbol,
        }
        return BotReply(
            f"已从信号详情带入币种：{intent.symbol}\n\n请选择要创建的提醒类型。确认前不会保存任何提醒。",
            alert_kind_markup(),
        )

    if intent.kind == "analysis":
        prompt = intent.prompt
        if not prompt:
            return BotReply("请把要分析的雷达信号、资金费率或市场数据一起发过来。", main_menu_markup())
        try:
            return BotReply(call_ai_provider(settings, prompt, store, user_id, mode="analyst"))
        except Exception as exc:
            return BotReply(user_facing_error("AI 分析", exc))

    if intent.kind == "dossier":
        try:
            return BotReply(build_symbol_dossier_reply(settings, store, user_id, intent.prompt or text))
        except Exception as exc:
            return BotReply(user_facing_error("币种档案查询", exc))

    if intent.kind == "price":
        try:
            return price_reply(settings, intent.symbol)
        except Exception as exc:
            return BotReply(user_facing_error("价格查询", exc))

    if intent.kind == "ambiguous_alert":
        return BotReply(
            "如果这是价格提醒，请点击首页“设置价格提醒”。如果你想让我分析这句话，可以直接说“帮我分析这段：...”再粘贴内容。",
            main_menu_markup(),
        )

    reply = handle_message(settings, store, message, bot_username=bot_username, bot_user_id=bot_user_id)
    if not reply:
        return None
    if reply == HOME_TEXT:
        return BotReply(reply, main_menu_markup())
    return BotReply(reply)


def handle_callback_query(
    settings: Settings,
    store: PriceAlertStore,
    query: dict[str, Any],
    sessions: dict[str, dict[str, Any]] | None = None,
) -> BotReply | None:
    data = str(query.get("data") or "").strip()
    message = query.get("message", {}) if isinstance(query.get("message"), dict) else {}
    chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "")
    chat_type = str(chat.get("type") or "")
    user = query.get("from", {}) if isinstance(query.get("from"), dict) else {}
    user_id = str(user.get("id") or "")
    username = str(user.get("username") or user.get("first_name") or "")
    if not data or not chat_id or not user_id:
        return None
    if chat_type in NON_PRIVATE_CHAT_TYPES:
        if not settings.ai_allow_group_chat:
            return BotReply("AI 助手还没有开启群内调用。请在 Web 后台开启 AI_ALLOW_GROUP_CHAT。")
        if not chat_is_allowed(settings, chat):
            return BotReply("这个群没有开通 AI 助手。请在 Web 后台的 AI_ALLOWED_CHAT_IDS 里加入当前群 ID 或频道用户名。")
    if not is_authorized(settings, user_id):
        return BotReply("你没有使用这个 AI 助手 Bot 的权限。请在 Web 后台配置 AI_ADMIN_USER_IDS。")

    active_sessions = sessions if sessions is not None else {}
    key = callback_session_key(query)

    if data == "menu:home":
        return BotReply(HOME_TEXT, main_menu_markup())
    if data == "menu:help":
        return BotReply(HELP_TEXT, main_menu_markup())
    if data == "menu:alerts":
        alerts = store.list_alerts(user_id=user_id, limit=50)
        return BotReply(list_alerts_text(alerts), alerts_manage_markup(alerts))
    if data == "menu:price_query":
        return BotReply(
            "\n".join([
                "查询价格",
                "",
                "可以直接发送：BTC、ETH、BTC 现在多少钱、ETH 当前价格。",
                "",
                "当前会读取 Binance、Bybit、OKX、Bitget、Gate 的现货和 USDT 合约价格。",
            ]),
            back_home_markup(),
        )
    if data == "flow:alert_setup":
        return start_alert_setup_session(active_sessions, key)
    if data == "flow:cancel":
        active_sessions.pop(key, None)
        return BotReply("已取消。", main_menu_markup())
    if data.startswith("alert:delete:"):
        alert_id_text = data.rsplit(":", 1)[-1]
        if not alert_id_text.isdigit():
            return BotReply("删除按钮已失效，请重新打开“我的提醒”。", main_menu_markup())
        alert_id = int(alert_id_text)
        before_alerts = store.list_alerts(user_id=user_id, limit=50)
        display_no = alert_display_number(before_alerts, alert_id)
        ok = store.delete_alert(alert_id, user_id=user_id)
        alerts = store.list_alerts(user_id=user_id, limit=50)
        prefix = f"已删除提醒 {display_no}。" if ok and display_no is not None else "没有找到这条提醒，可能已经删除。"
        return BotReply(f"{prefix}\n\n{list_alerts_text(alerts)}", alerts_manage_markup(alerts))
    if data.startswith("alert:pause:") or data.startswith("alert:resume:"):
        action, alert_id_text = data.split(":", 2)[1:]
        if not alert_id_text.isdigit():
            return BotReply("这个按钮已失效，请重新打开“我的提醒”。", main_menu_markup())
        alert_id = int(alert_id_text)
        before_alerts = store.list_alerts(user_id=user_id, limit=50)
        display_no = alert_display_number(before_alerts, alert_id)
        status, label = ("paused", "已暂停") if action == "pause" else ("active", "已恢复")
        ok = store.set_status(alert_id, status, user_id=user_id)
        alerts = store.list_alerts(user_id=user_id, limit=50)
        prefix = f"提醒 {display_no} {label}。" if ok and display_no is not None else "没有找到这条提醒，可能已经删除。"
        return BotReply(f"{prefix}\n\n{list_alerts_text(alerts)}", alerts_manage_markup(alerts))
    if data.startswith("alert:kind:"):
        session = active_sessions.get(key)
        if session is None:
            return BotReply("会话已失效，请重新设置。", main_menu_markup())
        alert_type = data.rsplit(":", 1)[-1]
        labels = {
            "target_price": "目标价提醒",
            "price_change": "价格急涨急跌",
            "oi_change": "持仓量变化",
            "funding_change": "资金费率变化",
        }
        if alert_type not in labels:
            return BotReply("这个提醒类型暂时无法识别，请重新选择。", alert_kind_markup())
        prefill_symbol = str(session.pop("prefill_symbol", "") or "").strip()
        session.update({"state": "alert_symbol", "alert_type": alert_type})
        if prefill_symbol:
            synthetic_message = {
                "chat": {"id": chat_id, "type": chat_type},
                "from": {"id": user_id, "username": username},
            }
            advanced = handle_alert_setup_session(settings, active_sessions, synthetic_message, prefill_symbol)
            if advanced:
                return advanced
        return BotReply(f"已选择：{labels[alert_type]}\n\n{ALERT_SYMBOL_TEXT}", cancel_markup())
    if data.startswith("alert:market:"):
        session = active_sessions.get(key)
        if not session:
            return BotReply("会话已失效，请重新设置价格提醒。", main_menu_markup())
        market_type = data.rsplit(":", 1)[-1]
        quotes = session_quotes(session, market_type)
        if not quotes:
            return BotReply("这个市场暂时没有可选交易所，请重新输入币种。", main_menu_markup())
        session.update({"state": "alert_exchange", "market_type": market_type})
        return BotReply(
            "\n".join([
                f"已选择市场：{quotes[0].market_type_label}",
                "",
                "请选择交易所：",
            ]),
            exchange_markup(quotes),
        )
    if data.startswith("alert:exchange:"):
        session = active_sessions.get(key)
        if not session:
            return BotReply("会话已失效，请重新设置价格提醒。", main_menu_markup())
        selected_key = data.split(":", 2)[-1]
        quotes = session_quotes(session, str(session.get("market_type") or ""))
        selected = next((quote for quote in quotes if quote.key == selected_key), None)
        if selected is None:
            return BotReply("这个交易所选项已失效，请重新输入币种。", main_menu_markup())
        alert_type = str(session.get("alert_type") or "target_price")
        session.update({"selected_quote": quote_to_dict(selected)})
        if alert_type == "target_price":
            session.update({"state": "alert_price"})
            return BotReply(
                "\n".join([
                    "已选择价格源",
                    "",
                    f"交易所：{price_quote_exchange_link(selected, bold=True)}",
                    f"市场：{html.escape(selected.market_type_label)}",
                    f"交易对：{telegram_code(selected.pair)}",
                    f"当前价：{format_price(selected.price)}",
                    "",
                    "请发送目标价格，例如：58000",
                    "目标价高于当前价会按上涨提醒；低于当前价会按下跌提醒。",
                ]),
                cancel_markup(),
            )
        if alert_type in {"price_change", "oi_change"}:
            session.update({"state": "alert_timeframe"})
            return BotReply(
                "\n".join([
                    "已选择监控源",
                    "",
                    f"交易所：{price_quote_exchange_link(selected, bold=True)}",
                    f"市场：{html.escape(selected.market_type_label)}",
                    f"交易对：{telegram_code(selected.pair)}",
                    "",
                    "请选择监控时间窗口：",
                ]),
                timeframe_markup(),
            )
        session.update({
            "state": "alert_repeat",
            "pending_alert": {
                "alert_type": "funding_change",
                "symbol": selected.symbol,
                "exchange": selected.exchange,
                "exchange_label": selected.exchange_label,
                "market_type": selected.market_type,
                "market_type_label": selected.market_type_label,
                "pair": selected.pair,
                "direction": "both",
                "target_price": 0,
                "timeframe_sec": 0,
                "timeframe_label": "-",
                "threshold_pct": 0,
                "repeat_policy": "once",
                "repeat_interval_sec": 0,
                "repeat_label": "提醒一次",
            },
        })
        return BotReply(
            "\n".join([
                "已选择资金费率监控源",
                "",
                f"交易所：{price_quote_exchange_link(selected, bold=True)}",
                "市场：USDT 合约",
                f"交易对：{telegram_code(selected.pair)}",
                "",
                "请选择触发后的提醒方式：",
            ]),
            repeat_policy_markup(),
        )
    if data.startswith("alert:timeframe:"):
        session = active_sessions.get(key)
        if not session:
            return BotReply("会话已失效，请重新设置。", main_menu_markup())
        timeframe = int(data.rsplit(":", 1)[-1])
        session.update({"state": "alert_threshold", "timeframe_sec": timeframe})
        return BotReply("请选择波动阈值：", threshold_markup())
    if data.startswith("alert:threshold:"):
        session = active_sessions.get(key)
        if not session:
            return BotReply("会话已失效，请重新设置。", main_menu_markup())
        threshold = float(data.rsplit(":", 1)[-1])
        session.update({"state": "alert_direction", "threshold_pct": threshold})
        return BotReply("请选择触发方向：", change_direction_markup())
    if data.startswith("alert:direction:"):
        session = active_sessions.get(key)
        if not session:
            return BotReply("会话已失效，请重新设置。", main_menu_markup())
        direction = data.rsplit(":", 1)[-1]
        selected = session.get("selected_quote")
        quote = quote_from_dict(selected) if isinstance(selected, dict) else None
        if not quote:
            return BotReply("会话已失效，请重新设置。", main_menu_markup())
        timeframe = int(session.get("timeframe_sec") or 300)
        threshold = float(session.get("threshold_pct") or 1)
        alert_type = str(session.get("alert_type") or "price_change")
        session.update({
            "state": "alert_repeat",
            "pending_alert": {
                "alert_type": alert_type,
                "symbol": quote.symbol,
                "exchange": quote.exchange,
                "exchange_label": quote.exchange_label,
                "market_type": quote.market_type,
                "market_type_label": quote.market_type_label,
                "pair": quote.pair,
                "direction": direction,
                "target_price": 0,
                "timeframe_sec": timeframe,
                "timeframe_label": {300: "5分钟", 900: "15分钟", 3600: "60分钟"}.get(timeframe, f"{timeframe}秒"),
                "threshold_pct": threshold,
                "repeat_policy": "once",
                "repeat_interval_sec": 0,
                "repeat_label": "提醒一次",
            },
        })
        return BotReply("请选择触发后的提醒方式：", repeat_policy_markup())
    if data.startswith("alert:repeat:"):
        session = active_sessions.get(key)
        if not session:
            return BotReply("会话已失效，请重新设置。", main_menu_markup())
        pending = session.get("pending_alert")
        if not isinstance(pending, dict):
            return BotReply("还没有待确认的提醒，请重新设置。", main_menu_markup())
        parts = data.split(":")
        policy = parts[2] if len(parts) >= 3 else "once"
        interval = int(parts[3]) if len(parts) >= 4 and str(parts[3]).isdigit() else 0
        if policy == "interval" and interval <= 0:
            interval = 300
        repeat_label = "提醒一次" if policy == "once" else ("重复提醒" if policy == "repeat" else f"持续提醒，每{max(1, interval // 60)}分钟一次")
        pending.update({"repeat_policy": policy, "repeat_interval_sec": interval, "repeat_label": repeat_label})
        session.update({"state": "alert_confirm", "pending_alert": pending})
        return BotReply(monitor_confirmation_text(pending), pending_alert_markup())
    if data == "alert:confirm_pending":
        session = active_sessions.get(key)
        pending = session.get("pending_alert") if isinstance(session, dict) else None
        if not isinstance(pending, dict):
            return BotReply("这个确认按钮已失效，请重新设置。", main_menu_markup())
        try:
            alert = create_alert_from_context(
                store,
                user_id=user_id,
                chat_id=chat_id,
                username=username,
                symbol=str(pending.get("symbol") or ""),
                exchange=str(pending.get("exchange") or "binance"),
                market_type=str(pending.get("market_type") or "futures"),
                pair=str(pending.get("pair") or ""),
                direction=str(pending.get("direction") or ""),
                target_price=parse_price(pending.get("target_price") or "") if str(pending.get("alert_type") or "target_price") == "target_price" else 0,
                source="telegram-button",
                note="button-confirm",
                alert_type=str(pending.get("alert_type") or "target_price"),
                timeframe_sec=int(pending.get("timeframe_sec") or 0),
                threshold_pct=float(pending.get("threshold_pct") or 0),
                repeat_policy=str(pending.get("repeat_policy") or "once"),
                repeat_interval_sec=int(pending.get("repeat_interval_sec") or 0),
            )
        except Exception as exc:
            return BotReply(f"创建提醒失败：{type(exc).__name__}: {exc}", main_menu_markup())
        active_sessions.pop(key, None)
        alerts = store.list_alerts(user_id=user_id, limit=50)
        return BotReply(alert_created_text(alert, alert_display_number(alerts, alert.id)), main_menu_markup())
    return BotReply("这个按钮暂时无法识别，请返回首页重新选择。", main_menu_markup())


def _change_direction_hit(direction: str, change_pct: float, threshold_pct: float) -> bool:
    if direction == "up":
        return change_pct >= threshold_pct
    if direction == "down":
        return change_pct <= -threshold_pct
    return abs(change_pct) >= threshold_pct


def _monitor_can_send(alert: PriceAlert, current_value: float, change_pct: float | None = None) -> bool:
    now = int(time.time())
    if alert.repeat_policy == "once":
        return alert.trigger_count <= 0
    if alert.repeat_policy == "interval":
        return not alert.last_triggered_at or now - int(alert.last_triggered_at) >= max(60, int(alert.repeat_interval_sec or 300))
    if alert.last_value is None:
        return alert.trigger_count <= 0
    if change_pct is not None:
        previous_change = alert.last_baseline if alert.last_baseline is not None else 0
        if alert.direction == "up":
            return previous_change < alert.threshold_pct <= change_pct
        if alert.direction == "down":
            return previous_change > -alert.threshold_pct >= change_pct
        return abs(previous_change) < alert.threshold_pct <= abs(change_pct)
    return current_value != alert.last_value


def evaluate_price_change_alert(settings: Settings, store: PriceAlertStore, alert: PriceAlert) -> dict[str, Any] | None:
    snapshot = fetch_price_change_snapshot(settings, alert)
    if not snapshot:
        return None
    current = float(snapshot["current"])
    baseline = float(snapshot["baseline"])
    change_pct = float(snapshot["change_pct"])
    should_send = _change_direction_hit(alert.direction, change_pct, alert.threshold_pct) and _monitor_can_send(alert, current, change_pct)
    store.update_monitor_state(alert.id, last_value=current, last_baseline=change_pct, last_price=current)
    if not should_send:
        return None
    detail = "\n".join([
        f"时间窗口：{alert.timeframe_label}",
        f"窗口起点：{format_price(baseline)}",
        f"当前价格：{format_price(current)}",
        f"价格波动：{change_pct:+.2f}%（阈值 {alert.threshold_pct:g}%）",
    ])
    return {"price": current, "detail": detail, "message": f"price_change {change_pct:+.2f}%"}


def evaluate_oi_change_alert(settings: Settings, store: PriceAlertStore, alert: PriceAlert) -> dict[str, Any] | None:
    current = fetch_open_interest_value(settings, alert)
    if current is None or current <= 0:
        return None
    baseline = alert.last_value
    change_pct = ((current - baseline) / baseline * 100) if baseline and baseline > 0 else None
    should_send = (
        change_pct is not None
        and _change_direction_hit(alert.direction, change_pct, alert.threshold_pct)
        and _monitor_can_send(alert, current, change_pct)
    )
    store.update_monitor_state(alert.id, last_value=current, last_baseline=change_pct if change_pct is not None else None)
    if not should_send or change_pct is None or baseline is None:
        return None
    quote = fetch_alert_market_quote(settings, alert.symbol, alert.exchange, alert.market_type, alert.pair)
    current_price = quote.price if quote else (alert.last_price or 0)
    detail = "\n".join([
        f"时间窗口：{alert.timeframe_label}（按服务采样基准计算）",
        f"上次 OI：{baseline:,.4f}",
        f"当前 OI：{current:,.4f}",
        f"OI 变化：{change_pct:+.2f}%（阈值 {alert.threshold_pct:g}%）",
    ])
    return {"price": float(current_price or 0), "detail": detail, "message": f"oi_change {change_pct:+.2f}%"}


def evaluate_funding_change_alert(settings: Settings, store: PriceAlertStore, alert: PriceAlert) -> dict[str, Any] | None:
    quality = DataQuality()
    http = HttpClient(settings, quality)
    client = MultiExchangeFundingClient(settings, http)
    rows = client.snapshot(alert.symbol, include_history=True)
    if not rows:
        return None
    classification = classify_funding_alert(rows, settings)
    max_abs = max((abs(float(row.get("funding_pct") or 0)) for row in rows if isinstance(row, dict)), default=0.0)
    should_send = bool(classification) and _monitor_can_send(alert, max_abs)
    store.update_monitor_state(alert.id, last_value=max_abs)
    if not should_send:
        return None
    quote = fetch_alert_market_quote(settings, alert.symbol, alert.exchange, alert.market_type, alert.pair)
    current_price = quote.price if quote else (alert.last_price or 0)
    row_lines = [funding_row_text(row, settings) for row in rows[:5]]
    detail = "\n".join([
        f"风险类型：{'、'.join(classification.get('types') or [])}",
        f"风险等级：{classification.get('risk') or '观察'}",
        "资金费率：",
        *row_lines,
    ])
    return {"price": float(current_price or 0), "detail": detail, "message": f"funding_change {classification.get('primary_kind') or ''}"}


def evaluate_monitor_alert(settings: Settings, store: PriceAlertStore, alert: PriceAlert) -> dict[str, Any] | None:
    if alert.alert_type == "price_change":
        return evaluate_price_change_alert(settings, store, alert)
    if alert.alert_type == "oi_change":
        return evaluate_oi_change_alert(settings, store, alert)
    if alert.alert_type == "funding_change":
        return evaluate_funding_change_alert(settings, store, alert)
    return None


def processing_notice_for_message(
    settings: Settings,
    message: dict[str, Any],
    bot_username: str = "",
    bot_user_id: str = "",
    sessions: dict[str, dict[str, Any]] | None = None,
) -> str:
    raw_text = str(message.get("text") or "").strip()
    if not raw_text:
        return ""
    chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
    chat_id = str(chat.get("id") or "")
    chat_type = str(chat.get("type") or "")
    user_id, _ = user_label(message)
    if not user_id or not chat_id:
        return ""
    if chat_type in NON_PRIVATE_CHAT_TYPES and not message_targets_bot(message, bot_username, bot_user_id):
        return ""
    if chat_type in NON_PRIVATE_CHAT_TYPES:
        if not settings.ai_allow_group_chat or not chat_is_allowed(settings, chat):
            return ""
    if not is_authorized(settings, user_id):
        return ""

    text = strip_bot_addressing(raw_text, bot_username)
    lowered = text.lower()
    if lowered == "/start" or text.startswith("/"):
        return ""
    session = (sessions or {}).get(session_key(chat_id, user_id)) or {}
    state = str(session.get("state") or "")
    if state == "alert_symbol":
        return "已收到币种，正在并发识别五大交易所的现货和合约价格源..."
    if state == "alert_price":
        return "已收到目标价，正在读取所选交易所的最新价格..."
    intent = classify_user_intent(text)
    if intent.kind == "analysis":
        return "已收到，正在调用 AI 分析；结果出来后会单独发给你。"
    if intent.kind == "dossier":
        return "已收到，正在读取历史信号和当前行情，生成币种档案..."
    if intent.kind == "price":
        return "已收到，正在并发查询五大交易所价格..."
    if settings.ai_provider_enable and settings.ai_api_key and not lowered.startswith("/"):
        return "已收到，正在思考；如果问题较复杂会稍慢一点。"
    return ""


def _chat_id_from_message(message: dict[str, Any]) -> str | int | None:
    chat = message.get("chat", {})
    return chat.get("id") if isinstance(chat, dict) else None


def _chat_id_from_callback(callback_query: dict[str, Any]) -> str | int | None:
    message = callback_query.get("message", {})
    chat = message.get("chat", {}) if isinstance(message, dict) else {}
    return chat.get("id") if isinstance(chat, dict) else None


def send_temporary_processing_notice(
    bot: TelegramBotClient,
    sender: QueuedTelegramSender,
    chat_id: str | int | None,
    text: str,
) -> tuple[tuple[str | int, int], ...]:
    if chat_id is None:
        return ()
    try:
        message_ids = bot.send_message_with_ids(chat_id, text)
        return tuple((chat_id, message_id) for message_id in message_ids)
    except AttributeError:
        send_bot_message_safely(sender, chat_id, text, context="message_processing_notice")
    except Exception as exc:
        print(f"ai-assistant: temporary notice send failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    return ()


def delete_temporary_messages(
    bot: TelegramBotClient,
    targets: tuple[tuple[str | int, int], ...],
) -> None:
    for chat_id, message_id in targets:
        try:
            bot.delete_message(chat_id, message_id)
        except Exception as exc:
            print(
                f"ai-assistant: delete temporary notice failed {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )


def acknowledge_callback_query(
    bot: TelegramBotClient,
    callback_query: dict[str, Any],
    text: str = "",
) -> None:
    callback_id = str(callback_query.get("id") or "")
    if not callback_id:
        return
    try:
        bot.answer_callback_query(callback_id, text)
    except Exception as exc:
        print(f"ai-assistant: callback ack failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)


def process_ai_update(
    update: dict[str, Any],
    bot: TelegramBotClient,
    sender: QueuedTelegramSender,
    bot_username: str,
    bot_user_id: str,
    sessions: dict[str, dict[str, Any]],
    session_locks: SessionLockRegistry,
) -> None:
    started_at = time.perf_counter()
    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict):
        acknowledge_callback_query(bot, callback_query)
        settings = load_ai_settings_cached()
        store = PriceAlertStore(settings.ai_price_alerts_db_path)
        chat_id = _chat_id_from_callback(callback_query)
        session_lock = session_locks.lock_for(callback_session_key(callback_query))
        try:
            with session_lock:
                reply = handle_callback_query(settings, store, callback_query, sessions=sessions)
        except Exception as exc:
            print(f"ai-assistant: callback failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
            send_bot_message_safely(sender, chat_id, user_facing_error("按钮处理", exc), context="callback_error")
            log_ai_update_latency("callback", started_at, "error=1")
            return
        if reply:
            send_bot_message_safely(
                sender,
                chat_id,
                reply.text,
                reply_markup=reply.reply_markup,
                context="callback_reply",
                parse_mode=reply.parse_mode,
            )
        log_ai_update_latency("callback", started_at)
        return

    message = update.get("message")
    if not isinstance(message, dict):
        return
    settings = load_ai_settings_cached()
    store = PriceAlertStore(settings.ai_price_alerts_db_path)
    chat_id = _chat_id_from_message(message)
    lock_key = message_session_key(message)
    session_lock = session_locks.lock_for(lock_key)
    notice = processing_notice_for_message(settings, message, bot_username=bot_username, bot_user_id=bot_user_id, sessions=sessions)
    delete_after_send: tuple[tuple[str | int, int], ...] = ()
    if notice:
        delete_after_send = send_temporary_processing_notice(bot, sender, chat_id, notice)
    try:
        with session_lock:
            reply = handle_message_reply(
                settings,
                store,
                message,
                bot_username=bot_username,
                bot_user_id=bot_user_id,
                sessions=sessions,
            )
    except Exception as exc:
        print(f"ai-assistant: message failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        sent = send_bot_message_safely(
            sender,
            chat_id,
            user_facing_error("消息处理", exc),
            context="message_error",
            delete_after_send=delete_after_send,
        )
        if not sent:
            delete_temporary_messages(bot, delete_after_send)
        log_ai_update_latency("message", started_at, "error=1")
        return
    if reply:
        sent = send_bot_message_safely(
            sender,
            chat_id,
            reply.text,
            reply_markup=reply.reply_markup,
            context="message_reply",
            parse_mode=reply.parse_mode,
            delete_after_send=delete_after_send,
        )
        if not sent:
            delete_temporary_messages(bot, delete_after_send)
    elif delete_after_send:
        delete_temporary_messages(bot, delete_after_send)
    log_ai_update_latency("message", started_at)


def check_and_send_price_alerts(settings: Settings, store: PriceAlertStore, bot: TelegramBotClient) -> dict[str, Any]:
    if not settings.ai_price_alerts_enable:
        return {"ok": True, "enabled": False, "checked": 0, "triggered": 0}
    active = store.list_alerts(status="active", limit=1000)
    if not active:
        return {"ok": True, "enabled": True, "checked": 0, "triggered": 0}
    target_alerts = [alert for alert in active if alert.alert_type == "target_price"]
    monitor_alerts = [alert for alert in active if alert.alert_type != "target_price"]
    prices = fetch_price_alert_prices(settings, target_alerts)
    sent = 0
    errors: list[str] = []
    price_misses = 0
    for alert, price in triggered_alerts(target_alerts, prices):
        try:
            user_alerts = store.list_alerts(user_id=alert.user_id, limit=50)
            queued = send_bot_message_safely(
                bot,
                alert.chat_id,
                alert_trigger_text(alert, price, display_no=alert_display_number(user_alerts, alert.id)),
                context="alert_trigger",
            )
        except Exception as exc:
            errors.append(f"{alert.id}: {type(exc).__name__}: {exc}")
            continue
        if not queued:
            errors.append(f"{alert.id}: send_failed")
            continue
        if not store.mark_triggered(alert, price):
            errors.append(f"{alert.id}: state_not_marked")
            continue
        sent += 1
    for alert in target_alerts:
        price = prices.get(alert.price_key)
        if price is None:
            price_misses += 1
        if price is not None:
            store.update_last_price(
                alert.symbol,
                price,
                exchange=alert.exchange,
                market_type=alert.market_type,
                pair=alert.pair,
            )
    for alert in monitor_alerts:
        try:
            trigger = evaluate_monitor_alert(settings, store, alert)
        except Exception as exc:
            errors.append(f"{alert.id}: {type(exc).__name__}: {exc}")
            continue
        if not trigger:
            continue
        try:
            user_alerts = store.list_alerts(user_id=alert.user_id, limit=50)
            queued = send_bot_message_safely(
                bot,
                alert.chat_id,
                alert_trigger_text(
                    alert,
                    trigger["price"],
                    str(trigger.get("detail") or ""),
                    display_no=alert_display_number(user_alerts, alert.id),
                ),
                context="alert_trigger",
            )
        except Exception as exc:
            errors.append(f"{alert.id}: {type(exc).__name__}: {exc}")
            continue
        if not queued:
            errors.append(f"{alert.id}: send_failed")
            continue
        if not store.mark_triggered(alert, trigger["price"], str(trigger.get("message") or "")):
            errors.append(f"{alert.id}: state_not_marked")
            continue
        sent += 1
    return {
        "ok": not errors,
        "enabled": True,
        "checked": len(active),
        "triggered": sent,
        "price_misses": price_misses,
        "errors": errors,
    }


def run_price_alert_scanner(stop_event: threading.Event, sender: QueuedTelegramSender) -> None:
    while not stop_event.is_set():
        settings = load_ai_settings_cached()
        if not settings.ai_assistant_enable or not settings.ai_bot_token:
            break
        interval = max(3, int(settings.ai_alert_check_interval_sec))
        started = time.time()
        try:
            store = PriceAlertStore(settings.ai_price_alerts_db_path)
            result = check_and_send_price_alerts(settings, store, sender)  # type: ignore[arg-type]
            elapsed = time.time() - started
            print(
                f"ai-assistant: alert_check elapsed={elapsed:.2f}s "
                f"queue={sender.pending_count()} {json.dumps(result, ensure_ascii=False)}",
                flush=True,
            )
        except Exception as exc:
            print(f"ai-assistant: alert_check failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        waited = time.time() - started
        stop_event.wait(max(0.1, interval - waited))


def idle_until_enabled() -> None:
    print("ai-assistant: disabled or missing AI_BOT_TOKEN; idle and reload config every 60s", flush=True)
    while True:
        time.sleep(60)
        settings = load_ai_settings_cached(force=True)
        if settings.ai_assistant_enable and settings.ai_bot_token:
            return


def run_ai_assistant_service() -> int:
    offset: int | None = None
    while True:
        settings = load_ai_settings_cached(force=True)
        if not settings.ai_assistant_enable or not settings.ai_bot_token:
            idle_until_enabled()
            continue
        bot = TelegramBotClient(
            settings.ai_bot_token,
            timeout_sec=settings.tg_push_timeout_sec,
            send_timeout_sec=max(20, int(settings.tg_push_timeout_sec)),
            retry_count=2,
            retry_delay_sec=1.0,
        )
        bot_username = ""
        bot_user_id = ""
        try:
            bot_info = bot.get_me()
            bot_username = str(bot_info.get("username") or "")
            bot_user_id = str(bot_info.get("id") or "")
        except Exception as exc:
            print(f"ai-assistant: getMe failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        poll_timeout = max(1, min(5, int(settings.ai_poll_timeout_sec)))
        alert_interval = max(3, int(settings.ai_alert_check_interval_sec))
        sessions: dict[str, dict[str, Any]] = {}
        session_locks = SessionLockRegistry()
        sender = QueuedTelegramSender(bot)
        sender.start()
        stop_event = threading.Event()
        alert_thread = threading.Thread(target=run_price_alert_scanner, args=(stop_event, sender), name="paopao-ai-alerts", daemon=True)
        alert_thread.start()
        worker_count = 8
        update_executor = ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="paopao-ai-update")
        print(
            f"ai-assistant: running username={bot_username or '-'} poll_timeout={poll_timeout}s "
            f"alert_interval={alert_interval}s workers={worker_count} db={settings.ai_price_alerts_db_path}",
            flush=True,
        )
        try:
            while True:
                current_settings = load_ai_settings_cached()
                if not current_settings.ai_assistant_enable or current_settings.ai_bot_token != settings.ai_bot_token:
                    break
                try:
                    updates = bot.get_updates(offset, timeout=poll_timeout)
                except KeyboardInterrupt:
                    raise
                except Exception as exc:
                    print(f"ai-assistant: getUpdates failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                    time.sleep(5)
                    current_settings = load_ai_settings_cached(force=True)
                    if not current_settings.ai_assistant_enable or not current_settings.ai_bot_token:
                        continue
                    continue
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        offset = update_id + 1
                    update_executor.submit(
                        process_ai_update,
                        update,
                        bot,
                        sender,
                        bot_username=bot_username,
                        bot_user_id=bot_user_id,
                        sessions=sessions,
                        session_locks=session_locks,
                    )
        finally:
            stop_event.set()
            alert_thread.join(timeout=5)
            update_executor.shutdown(wait=False, cancel_futures=True)
            sender.stop()


def price_alerts_payload(settings: Settings | None = None) -> dict[str, Any]:
    loaded = settings or Settings.load()
    store = PriceAlertStore(loaded.ai_price_alerts_db_path)
    alerts = store.list_alerts(limit=500)
    return {
        "ok": True,
        "db_path": str(loaded.ai_price_alerts_db_path),
        "enabled": loaded.ai_price_alerts_enable,
        "stats": store.stats(),
        "alerts": [alert_to_dict(alert) for alert in alerts],
    }


def create_price_alert_from_payload(data: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    loaded = settings or Settings.load()
    store = PriceAlertStore(loaded.ai_price_alerts_db_path)
    chat_id = str(data.get("chat_id") or loaded.ai_default_chat_id or "").strip()
    if not chat_id:
        raise ValueError("缺少接收提醒的 Telegram 用户 chat_id；可以在 Web 后台配置 AI_DEFAULT_CHAT_ID")
    user_id = str(data.get("user_id") or chat_id).strip()
    alert = store.create_alert(
        user_id=user_id,
        chat_id=chat_id,
        username=str(data.get("username") or "web"),
        symbol=str(data.get("symbol") or ""),
        exchange=str(data.get("exchange") or "binance"),
        market_type=str(data.get("market_type") or "futures"),
        pair=str(data.get("pair") or ""),
        direction=str(data.get("direction") or ""),
        target_price=parse_price(data.get("target_price") or "") if str(data.get("alert_type") or "target_price") == "target_price" else 0,
        source="web",
        note=str(data.get("note") or ""),
        alert_type=str(data.get("alert_type") or "target_price"),
        timeframe_sec=int(data.get("timeframe_sec") or 0),
        threshold_pct=float(data.get("threshold_pct") or 0),
        repeat_policy=str(data.get("repeat_policy") or "once"),
        repeat_interval_sec=int(data.get("repeat_interval_sec") or 0),
    )
    return {"ok": True, "alert": alert_to_dict(alert), "message": "价格提醒已创建"}


def mutate_price_alert_from_payload(data: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    loaded = settings or Settings.load()
    store = PriceAlertStore(loaded.ai_price_alerts_db_path)
    alert_id = int(data.get("id") or 0)
    action = str(data.get("action") or "").strip().lower()
    if alert_id <= 0:
        raise ValueError("缺少提醒编号")
    if action == "delete":
        ok = store.delete_alert(alert_id)
        return {"ok": ok, "message": "已删除" if ok else "没有找到这条提醒"}
    if action == "pause":
        ok = store.set_status(alert_id, "paused")
        return {"ok": ok, "message": "已暂停" if ok else "没有找到这条提醒"}
    if action == "resume":
        ok = store.set_status(alert_id, "active")
        return {"ok": ok, "message": "已恢复" if ok else "没有找到这条提醒"}
    raise ValueError("未知提醒操作")
