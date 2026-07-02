from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import requests

from .ai_prompts import load_ai_prompts
from .config import Settings, normalize_ai_model
from .data_sources import HTTP_HEADERS
from .price_alerts import (
    PriceAlert,
    PriceAlertStore,
    alert_to_dict,
    fetch_binance_prices,
    format_price,
    normalize_symbol,
    parse_price,
    triggered_alerts,
)
from .symbol_dossier import (
    build_symbol_dossier,
    extract_symbol_from_query,
    format_symbol_dossier_ai_context,
    format_symbol_dossier_report,
    is_symbol_dossier_request,
)


HOME_TEXT = """泡泡 AI 助手 Bot

这是泡泡雷达的独立 AI 助手，和群里自动推送雷达信号的 Bot 分开。
它主要负责：查币雷达档案、解读雷达数据、设置价格提醒、查询价格、回答运行状态问题。

常用入口：
1. 查币雷达档案
查 BTC
GWEI 怎么看
SOL 可以做多吗

2. 分析你粘贴的数据
分析这段：粘贴启动雷达、结构雷达、资金流、资金费率或市场数据

3. 设置价格提醒
可以点击“设置价格提醒”，按提示输入币种和目标价。创建前会让你确认。

创建提醒必须明确说“提醒我 / 通知我 / 设置提醒”。只转发带价格的雷达信号，不会自动创建提醒。

群里使用规则：
只有 Web 后台允许的群/频道才能使用；在群里必须 @我，或者回复我的消息，我才会处理，普通聊天不会触发。
"""

HELP_TEXT = """泡泡 AI 助手 Bot 完整帮助

这个 Bot 和群里自动推送雷达信号的 Bot 是分开的。
它主要负责：查币、解读数据、设置价格提醒、回答泡泡雷达运行问题。

你可以直接这样问：

1. 查币雷达档案
查 BTC
GWEI 怎么看
SOL 可以做多吗

我会读取这个币的历史雷达信号、当前价格、OI、成交量、市值、流动性、结构状态和资金费率，给出偏多 / 偏空 / 观望 / 高风险观望。

2. 分析你粘贴的数据
分析这段：粘贴启动雷达、结构雷达、资金流、资金费率或市场数据
也可以直接粘贴一整段雷达信号，我会自动识别并分析。

3. 查询价格
BTC 现在多少钱
ETH 当前价格
/price BTC

4. 设置价格提醒
BTC 跌破 58000 提醒我
ETH 突破 4200 通知我

注意：创建提醒必须明确说“提醒我 / 通知我 / 设置提醒”。只转发带价格的雷达信号，不会自动创建提醒。

5. 管理提醒
我的提醒有哪些
暂停提醒 12
恢复提醒 12
删除提醒 12

6. 查询雷达状态
帮我解释最近雷达状态
主服务正常吗
结构雷达有没有报错

群里使用规则：
只有 Web 后台允许的群/频道才能使用；在群里必须 @我，或者回复我的消息，我才会处理，普通聊天不会触发。

备用命令：
/coin BTC
/analyze 粘贴雷达信号或市场数据
/alert BTC 高于 58000
/alerts
/pause 12
/resume 12
/delete 12
/ai 帮我解释最近雷达状态
"""

PRICE_HELP_TEXT = """价格提醒说明

当前泡泡 AI 助手的价格提醒使用 Binance USDT 合约价格。

按钮模式：
1. 点击“设置价格提醒”
2. 输入币种，例如 BTC、ETH、DOGE
3. 输入目标价格，例如 58000
4. 机器人读取当前价格并自动判断方向
5. 点击“确认添加”后才会创建提醒

方向判断：
目标价高于当前价：价格高于或等于目标价时提醒
目标价低于当前价：价格低于或等于目标价时提醒

命令模式：
/alert BTC 高于 58000
/alert ETH 跌破 3200

提醒触发后会自动标记为已触发，不会反复轰炸。
"""

ANALYSIS_HELP_TEXT = """AI 分析说明

支持两类分析：

1. 查币雷达档案
发送：查 BTC、GWEI 怎么看、SOL 可以做多吗
我会读取这个币的历史雷达信号、当前价格、OI、成交量、市值、流动性、结构状态和资金费率。

2. 分析你粘贴的数据
发送：分析这段：粘贴雷达信号或市场数据
也可以直接粘贴启动雷达、结构雷达、资金流、资金费率等内容，我会自动识别。

AI 分析只做数据解读，不承诺涨跌，不是自动交易指令。
"""

ASSISTANT_HELP_TEXT = """AI 助手说明

可以直接问：
BTC 现在多少钱
查 BTC
GWEI 怎么看
我的提醒有哪些
帮我解释最近雷达状态

备用命令：
/price BTC
/coin BTC
/analyze 粘贴雷达信号
/alerts
/pause 12
/resume 12
/delete 12
/id

普通私聊会自动识别意图。群里必须 @机器人或回复机器人消息，并且群 ID 已经在 Web 后台白名单里。
"""

GROUP_RULES_TEXT = """群里使用规则

默认情况下，AI 助手不会读取群里每一句普通聊天。

群内调用需要同时满足：
1. Web 后台开启 AI_ALLOW_GROUP_CHAT
2. 当前群/频道 ID 填入 AI_ALLOWED_CHAT_IDS
3. 用户 @机器人，或回复机器人消息

这样做是为了避免误触发，也避免个人价格提醒设置泄露到不该使用的群里。
"""

ALERT_SETUP_TEXT = """设置价格提醒

请先发送币种简称，例如：
BTC
ETH
DOGE

当前版本使用 Binance USDT 合约价格。下一步会让你输入目标价，并在创建前确认。
"""


@dataclass(frozen=True)
class BotReply:
    text: str
    reply_markup: dict[str, Any] | None = None


def inline_keyboard(rows: list[list[tuple[str, str]]]) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": text, "callback_data": data} for text, data in row]
            for row in rows
        ]
    }


def main_menu_markup() -> dict[str, Any]:
    return inline_keyboard([
        [("查币雷达档案", "menu:dossier"), ("设置价格提醒", "flow:alert_setup")],
        [("我的提醒", "menu:alerts"), ("查询价格", "menu:price_query")],
        [("分析雷达数据", "menu:analysis"), ("AI 助手说明", "menu:assistant")],
        [("群里使用规则", "menu:group"), ("完整帮助", "menu:help")],
    ])


def back_home_markup() -> dict[str, Any]:
    return inline_keyboard([[("返回首页", "menu:home")]])


def alert_confirm_markup(symbol: str, direction: str, target_price: float) -> dict[str, Any]:
    return inline_keyboard([
        [("确认添加提醒", f"alert:confirm:{symbol}:{direction}:{target_price:g}")],
        [("重新设置", "flow:alert_setup"), ("取消", "flow:cancel")],
    ])


def cancel_markup() -> dict[str, Any]:
    return inline_keyboard([[("取消", "flow:cancel")]])


GROUP_CHAT_TYPES = {"group", "supergroup"}
NON_PRIVATE_CHAT_TYPES = GROUP_CHAT_TYPES | {"channel"}
ALERT_CREATE_INTENT_RE = re.compile(
    r"(提醒我|提醒一下|提醒下|通知我|通知一下|通知下|叫我|帮我盯|盯一下|设置提醒|设个提醒|创建提醒|添加提醒|到价|到了叫我|达到.*提醒|涨到.*(提醒|通知|叫)|跌到.*(提醒|通知|叫)|alert)",
    re.IGNORECASE,
)
ANALYSIS_INTENT_RE = re.compile(
    r"^\s*(/analyze(?:@\w+)?\b|/analysis(?:@\w+)?\b|分析这段|帮我分析|分析一下|分析下|解读一下|解读下|解读这个|看看这个信号|看下这个信号)",
    re.IGNORECASE,
)
PRICE_QUERY_RE = re.compile(r"(价格|现价|报价|行情|多少钱|多少|查价|查一下|看一下|price)", re.IGNORECASE)
ALERT_LIST_RE = re.compile(
    r"(/alerts\b|/list\b|我的提醒|提醒列表|警报列表|价格提醒列表|查看.*提醒|看看.*提醒|查.*提醒|提醒.*有哪些|提醒.*清单|alerts)",
    re.IGNORECASE,
)
ALERT_DELETE_RE = re.compile(r"(删除|删掉|移除|取消|delete|remove)", re.IGNORECASE)
ALERT_PAUSE_RE = re.compile(r"(暂停|停用|关闭|pause)", re.IGNORECASE)
ALERT_RESUME_RE = re.compile(r"(恢复|继续|启用|开启|resume)", re.IGNORECASE)
MARKET_DATA_KEYWORDS = (
    "启动雷达",
    "结构雷达",
    "资金流",
    "雷达信号",
    "触发明细",
    "阶段:",
    "阶段：",
    "分数:",
    "分数：",
    "oi",
    "cvd",
    "成交量",
    "资金费率",
    "市值",
    "流动性",
    "清算",
    "多空",
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


class TelegramBotClient:
    def __init__(self, token: str, timeout_sec: int = 15):
        self.token = token
        self.timeout_sec = max(3, int(timeout_sec))
        self.base_url = f"https://api.telegram.org/bot{token}"

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
    ) -> bool:
        safe_text = telegram_plain_text(text) or "（无内容）"
        chunks = split_telegram_text(safe_text) or ["（无内容）"]
        ok = True
        for index, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if reply_markup and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            response = requests.post(
                f"{self.base_url}/sendMessage",
                json=payload,
                headers=HTTP_HEADERS,
                timeout=self.timeout_sec,
            )
            response.raise_for_status()
            data = response.json()
            ok = ok and bool(data.get("ok"))
        return ok

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        payload: dict[str, Any] = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:180]
        response = requests.post(
            f"{self.base_url}/answerCallbackQuery",
            json=payload,
            headers=HTTP_HEADERS,
            timeout=self.timeout_sec,
        )
        response.raise_for_status()
        data = response.json()
        return bool(data.get("ok"))


def parse_alert_request(text: str) -> ParsedAlertRequest | None:
    clean = text.strip()
    if clean.startswith("/alert"):
        clean = clean.split(maxsplit=1)[1] if len(clean.split(maxsplit=1)) > 1 else ""
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
    clean = re.sub(r"^/(analyze|analysis)(?:@\w+)?\b", "", clean, flags=re.IGNORECASE).strip()
    clean = re.sub(r"^(分析这段|帮我分析|分析一下|分析下|解读一下|解读下|解读这个|看看这个信号|看下这个信号)", "", clean).strip()
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
    symbol = extract_symbol_text(clean)
    if not symbol:
        return False
    if PRICE_QUERY_RE.search(clean):
        return True
    return bool(re.fullmatch(r"(?i)[A-Z][A-Z0-9]{1,20}(?:USDT)?", clean))


def parse_alert_id(text: str) -> int | None:
    match = re.search(r"(?:提醒|警报|alert|编号|#|第)?\s*(\d{1,8})\s*(?:号|个)?", str(text or ""), flags=re.IGNORECASE)
    if not match:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def parse_alert_mutation(text: str) -> tuple[str, int] | None:
    clean = compact_text(text)
    alert_id = parse_alert_id(clean)
    if alert_id is None:
        return None
    if ALERT_DELETE_RE.search(clean):
        return "delete", alert_id
    if ALERT_PAUSE_RE.search(clean):
        return "pause", alert_id
    if ALERT_RESUME_RE.search(clean):
        return "resume", alert_id
    return None


def is_alert_list_request(text: str) -> bool:
    clean = compact_text(text)
    return bool(ALERT_LIST_RE.search(clean))


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


def alert_created_text(alert: PriceAlert) -> str:
    return "\n".join(
        [
            "已创建价格提醒",
            "",
            f"编号：{alert.id}",
            f"币种：{alert.symbol}",
            f"条件：价格 {alert.direction_label} {format_price(alert.target_price)}",
            "触发方式：触发一次后自动停止",
            "",
            f"查看：/alerts",
            f"暂停：/pause {alert.id}",
            f"删除：/delete {alert.id}",
        ]
    )


def alert_trigger_text(alert: PriceAlert, price: float) -> str:
    return "\n".join(
        [
            "价格提醒已触发",
            "",
            f"币种：{alert.symbol}",
            f"条件：价格 {alert.direction_label} {format_price(alert.target_price)}",
            f"当前价：{format_price(price)}",
            f"提醒编号：{alert.id}",
            "",
            "这条提醒已经标记为已触发，不会重复发送。",
        ]
    )


def list_alerts_text(alerts: list[PriceAlert]) -> str:
    if not alerts:
        return "当前没有价格提醒。可以发送：BTC 跌破 58000 提醒我"
    lines = ["你的价格提醒：", ""]
    status_map = {"active": "运行中", "paused": "已暂停", "triggered": "已触发"}
    for alert in alerts[:30]:
        lines.append(
            f"{alert.id}. {alert.symbol} {alert.direction_label} {format_price(alert.target_price)} "
            f"[{status_map.get(alert.status, alert.status)}]"
        )
    return "\n".join(lines)


def price_text(settings: Settings, symbol_text: str) -> str:
    symbol = normalize_symbol(symbol_text)
    prices = fetch_binance_prices(settings, [symbol])
    price = prices.get(symbol)
    if price is None:
        return f"没有从 Binance 合约行情里读到 {symbol} 的价格。"
    return f"{symbol} 当前 Binance 合约价格：{format_price(price)}"


def id_text(message: dict[str, Any]) -> str:
    chat = message.get("chat", {}) if isinstance(message.get("chat"), dict) else {}
    user_id, username = user_label(message)
    lines = [
        "当前 Telegram ID",
        "",
        f"你的用户 ID：{user_id or '未知'}",
        f"当前聊天 ID：{chat.get('id') or '未知'}",
        f"当前聊天类型：{chat.get('type') or '未知'}",
    ]
    if username:
        lines.append(f"用户名：{username}")
    return "\n".join(lines)


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


def current_price_for_symbol(settings: Settings, symbol: str) -> float | None:
    try:
        prices = fetch_binance_prices(settings, [symbol])
    except Exception:
        return None
    price = prices.get(symbol)
    return float(price) if isinstance(price, (int, float)) else None


def infer_alert_direction(target_price: float, current_price: float | None = None, fallback: str = "above") -> str:
    if fallback in {"above", "below"}:
        return fallback
    if current_price is not None and current_price > 0:
        return "above" if target_price >= current_price else "below"
    return "above"


def alert_confirmation_text(
    symbol: str,
    direction: str,
    target_price: float,
    current_price: float | None = None,
) -> str:
    direction_label = "高于或等于" if direction == "above" else "低于或等于"
    lines = [
        "请确认添加价格提醒",
        "",
        f"币种：{symbol}",
        "数据源：Binance USDT 合约",
    ]
    if current_price is not None:
        lines.append(f"当前价：{format_price(current_price)}")
    lines.extend([
        f"目标价：{format_price(target_price)}",
        f"触发条件：价格 {direction_label} {format_price(target_price)}",
        "",
        "确认后才会创建提醒；取消则不会保存。",
    ])
    return "\n".join(lines)


def alert_confirmation_reply(
    settings: Settings,
    parsed: ParsedAlertRequest,
    current_price: float | None = None,
) -> BotReply:
    direction = infer_alert_direction(parsed.target_price, current_price, parsed.direction)
    return BotReply(
        alert_confirmation_text(parsed.symbol, direction, parsed.target_price, current_price),
        alert_confirm_markup(parsed.symbol, direction, parsed.target_price),
    )


def start_alert_setup_session(sessions: dict[str, dict[str, Any]], key: str) -> BotReply:
    sessions[key] = {"state": "alert_symbol", "created_at": int(time.time())}
    return BotReply(ALERT_SETUP_TEXT, cancel_markup())


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
    if lowered in {"/cancel", "取消"}:
        sessions.pop(key, None)
        return BotReply("已取消价格提醒设置。", main_menu_markup())

    state = str(session.get("state") or "")
    if state == "alert_symbol":
        symbol_text = extract_symbol_text(text) or text
        try:
            symbol = normalize_symbol(symbol_text)
        except Exception as exc:
            return BotReply(f"没有识别出币种：{exc}\n请只输入币种简称，例如 BTC、ETH、DOGE。", cancel_markup())
        session.update({"state": "alert_price", "symbol": symbol})
        return BotReply(
            "\n".join([
                f"已识别币种：{symbol}",
                "",
                "请发送目标价格，例如：58000",
                "目标价高于当前价会按上涨提醒；低于当前价会按下跌提醒。",
            ]),
            cancel_markup(),
        )

    if state == "alert_price":
        symbol = str(session.get("symbol") or "")
        if not symbol:
            sessions.pop(key, None)
            return BotReply("会话已失效，请重新设置价格提醒。", main_menu_markup())
        try:
            target_price = parse_price(text)
        except Exception as exc:
            return BotReply(f"价格格式不正确：{exc}\n请发送数字，例如 58000 或 0.35。", cancel_markup())
        current_price = current_price_for_symbol(settings, symbol)
        direction = infer_alert_direction(target_price, current_price, fallback="")
        sessions.pop(key, None)
        return BotReply(
            alert_confirmation_text(symbol, direction, target_price, current_price),
            alert_confirm_markup(symbol, direction, target_price),
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
) -> PriceAlert:
    return store.create_alert(
        user_id=user_id,
        chat_id=chat_id,
        username=username,
        symbol=symbol,
        direction=direction,
        target_price=target_price,
        source=source,
        note=note,
    )


def runtime_context(settings: Settings) -> str:
    parts: list[str] = []
    for label, path in (
        ("主服务", settings.runtime_status_path),
        ("结构雷达", settings.structure_runtime_status_path),
    ):
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
            "- 发送：BTC 跌破 58000 提醒我",
            "- 发送：/alerts 查看提醒",
            "- 发送：/price BTC 查看价格",
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

    lowered = text.lower()
    if lowered in {"/start", "/paopao"}:
        return HOME_TEXT
    if lowered in {"/help", "help", "帮助"}:
        return HELP_TEXT
    if lowered == "/id":
        return id_text(message)
    if lowered.startswith("/setup"):
        return ALERT_SETUP_TEXT

    if lowered.startswith("/price"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return "用法：/price BTC"
        try:
            return price_text(settings, parts[1])
        except Exception as exc:
            return f"价格查询失败：{type(exc).__name__}: {exc}"

    if lowered.startswith("/coin") or lowered.startswith("/dossier"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2 or not extract_symbol_from_query(parts[1]):
            return "用法：/coin BTC，或直接发送：GWEI 怎么看"
        try:
            return build_symbol_dossier_reply(settings, store, user_id, parts[1])
        except Exception as exc:
            return f"币种档案查询失败：{type(exc).__name__}: {exc}"

    if lowered in {"/alerts", "/list", "我的提醒", "提醒列表"}:
        return list_alerts_text(store.list_alerts(user_id=user_id, limit=50))

    for command, status, label in (
        ("/pause", "paused", "已暂停"),
        ("/resume", "active", "已恢复"),
    ):
        if lowered.startswith(command):
            parts = text.split()
            if len(parts) < 2 or not parts[1].isdigit():
                return f"用法：{command} 提醒编号"
            ok = store.set_status(int(parts[1]), status, user_id=user_id)
            return f"提醒 {parts[1]} {label}。" if ok else "没有找到这条提醒。"

    if lowered.startswith("/delete") or lowered.startswith("/remove"):
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return "用法：/delete 提醒编号"
        ok = store.delete_alert(int(parts[1]), user_id=user_id)
        return f"提醒 {parts[1]} 已删除。" if ok else "没有找到这条提醒。"

    if lowered.startswith("/ai"):
        prompt = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        if not prompt:
            return "用法：/ai 你的问题"
        try:
            return call_ai_provider(settings, prompt, store, user_id)
        except Exception as exc:
            return f"AI 回答失败：{type(exc).__name__}: {exc}"

    if is_alert_list_request(text):
        return list_alerts_text(store.list_alerts(user_id=user_id, limit=50))

    mutation = parse_alert_mutation(text)
    if mutation:
        action, alert_id = mutation
        if action == "delete":
            ok = store.delete_alert(alert_id, user_id=user_id)
            return f"提醒 {alert_id} 已删除。" if ok else "没有找到这条提醒。"
        status, label = ("paused", "已暂停") if action == "pause" else ("active", "已恢复")
        ok = store.set_status(alert_id, status, user_id=user_id)
        return f"提醒 {alert_id} {label}。" if ok else "没有找到这条提醒。"

    if is_symbol_dossier_request(text) and not is_price_query(text) and not is_market_data_intent(text):
        try:
            return build_symbol_dossier_reply(settings, store, user_id, text)
        except Exception as exc:
            return f"币种档案查询失败：{type(exc).__name__}: {exc}"

    if is_analysis_intent(text):
        prompt = strip_analysis_request(text)
        if not prompt:
            return "用法：/analyze 粘贴雷达信号或市场数据"
        try:
            return call_ai_provider(settings, prompt, store, user_id, mode="analyst")
        except Exception as exc:
            return f"AI 分析失败：{type(exc).__name__}: {exc}"

    if lowered.startswith("/alert") or is_alert_intent(text):
        parsed = parse_alert_request(text)
        if not parsed:
            return "我没识别出提醒条件。示例：BTC 跌破 58000 提醒我，或 /alert ETH 高于 4200"
        alert = store.create_alert(
            user_id=user_id,
            chat_id=chat_id,
            username=username,
            symbol=parsed.symbol,
            direction=parsed.direction,
            target_price=parsed.target_price,
            source="telegram",
            note=text,
        )
        return alert_created_text(alert)

    if is_market_data_intent(text):
        try:
            return call_ai_provider(settings, text, store, user_id, mode="analyst")
        except Exception as exc:
            return f"AI 分析失败：{type(exc).__name__}: {exc}"

    if is_price_query(text):
        symbol = extract_symbol_text(text)
        try:
            return price_text(settings, symbol)
        except Exception as exc:
            return f"价格查询失败：{type(exc).__name__}: {exc}"

    ambiguous = ambiguous_alert_text(text)
    if ambiguous:
        direction_label = "高于或等于" if ambiguous.direction == "above" else "低于或等于"
        return (
            f"你是想设置 {ambiguous.symbol} 价格 {direction_label} "
            f"{format_price(ambiguous.target_price)} 的提醒吗？"
            f"如果是，请发送：{ambiguous.symbol.replace('USDT', '')} "
            f"{direction_label} {format_price(ambiguous.target_price)} 提醒我"
        )

    if settings.ai_provider_enable and settings.ai_api_key:
        try:
            return call_ai_provider(settings, text, store, user_id)
        except Exception as exc:
            return f"AI 回答失败：{type(exc).__name__}: {exc}"
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
    lowered = text.lower()

    if lowered in {"/cancel", "取消"}:
        active_sessions.pop(key, None)
        return BotReply("已取消。", main_menu_markup())
    if lowered in {"/start", "/paopao"} or not text:
        active_sessions.pop(key, None)
        return BotReply(HOME_TEXT, main_menu_markup())
    if lowered in {"/help", "help", "帮助"}:
        return BotReply(HELP_TEXT, main_menu_markup())
    if lowered == "/id":
        return BotReply(id_text(message))
    if lowered.startswith("/setup"):
        return start_alert_setup_session(active_sessions, key)

    session_reply = handle_alert_setup_session(settings, active_sessions, message, text)
    if session_reply:
        return session_reply

    if not lowered.startswith("/alert") and is_alert_intent(text):
        parsed = parse_alert_request(text)
        if parsed:
            return alert_confirmation_reply(settings, parsed)

    reply = handle_message(settings, store, message, bot_username=bot_username, bot_user_id=bot_user_id)
    if not reply:
        return None
    if reply == HOME_TEXT:
        return BotReply(reply, main_menu_markup())
    if reply == HELP_TEXT:
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
    if data == "menu:dossier":
        return BotReply(
            "\n".join([
                "查币雷达档案",
                "",
                "直接发送：查 BTC、GWEI 怎么看、SOL 可以做多吗。",
                "我会汇总这个币最近的雷达信号、价格、OI、成交量、市值、流动性、结构状态和资金费率，再给出偏多、偏空、观望或高风险观望结论。",
            ]),
            back_home_markup(),
        )
    if data == "menu:alerts":
        return BotReply(list_alerts_text(store.list_alerts(user_id=user_id, limit=50)), back_home_markup())
    if data == "menu:price_query":
        return BotReply(
            "\n".join([
                "查询价格",
                "",
                "可以直接发送：BTC 现在多少钱、ETH 当前价格。",
                "备用命令：/price BTC",
                "",
                "当前读取 Binance USDT 合约价格。",
            ]),
            back_home_markup(),
        )
    if data == "menu:analysis":
        return BotReply(ANALYSIS_HELP_TEXT, back_home_markup())
    if data == "menu:assistant":
        return BotReply(ASSISTANT_HELP_TEXT, back_home_markup())
    if data == "menu:group":
        return BotReply(GROUP_RULES_TEXT, back_home_markup())
    if data == "flow:alert_setup":
        return start_alert_setup_session(active_sessions, key)
    if data == "flow:cancel":
        active_sessions.pop(key, None)
        return BotReply("已取消。", main_menu_markup())
    if data.startswith("alert:confirm:"):
        parts = data.split(":", 4)
        if len(parts) != 5:
            return BotReply("这个确认按钮已失效，请重新设置。", main_menu_markup())
        _, _, symbol, direction, target_text = parts
        try:
            target_price = parse_price(target_text)
            alert = create_alert_from_context(
                store,
                user_id=user_id,
                chat_id=chat_id,
                username=username,
                symbol=symbol,
                direction=direction,
                target_price=target_price,
                source="telegram-button",
                note="button-confirm",
            )
        except Exception as exc:
            return BotReply(f"创建提醒失败：{type(exc).__name__}: {exc}", main_menu_markup())
        active_sessions.pop(key, None)
        return BotReply(alert_created_text(alert), main_menu_markup())

    return BotReply("这个按钮暂时无法识别，请返回首页重新选择。", main_menu_markup())


def check_and_send_price_alerts(settings: Settings, store: PriceAlertStore, bot: TelegramBotClient) -> dict[str, Any]:
    if not settings.ai_price_alerts_enable:
        return {"ok": True, "enabled": False, "checked": 0, "triggered": 0}
    active = store.list_alerts(status="active", limit=1000)
    if not active:
        return {"ok": True, "enabled": True, "checked": 0, "triggered": 0}
    symbols = sorted({alert.symbol for alert in active})
    prices = fetch_binance_prices(settings, symbols)
    for symbol, price in prices.items():
        store.update_last_price(symbol, price)
    sent = 0
    errors: list[str] = []
    for alert, price in triggered_alerts(active, prices):
        if not store.mark_triggered(alert.id, price):
            continue
        try:
            bot.send_message(alert.chat_id, alert_trigger_text(alert, price))
            sent += 1
        except Exception as exc:
            errors.append(f"{alert.id}: {type(exc).__name__}: {exc}")
    return {"ok": not errors, "enabled": True, "checked": len(active), "triggered": sent, "errors": errors}


def idle_until_enabled() -> None:
    print("ai-assistant: disabled or missing AI_BOT_TOKEN; idle and reload config every 60s", flush=True)
    while True:
        time.sleep(60)
        settings = Settings.load()
        if settings.ai_assistant_enable and settings.ai_bot_token:
            return


def run_ai_assistant_service() -> int:
    offset: int | None = None
    while True:
        settings = Settings.load()
        if not settings.ai_assistant_enable or not settings.ai_bot_token:
            idle_until_enabled()
            continue
        store = PriceAlertStore(settings.ai_price_alerts_db_path)
        bot = TelegramBotClient(settings.ai_bot_token, timeout_sec=settings.tg_push_timeout_sec)
        bot_username = ""
        bot_user_id = ""
        try:
            bot_info = bot.get_me()
            bot_username = str(bot_info.get("username") or "")
            bot_user_id = str(bot_info.get("id") or "")
        except Exception as exc:
            print(f"ai-assistant: getMe failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        poll_timeout = max(1, int(settings.ai_poll_timeout_sec))
        alert_interval = max(5, int(settings.ai_alert_check_interval_sec))
        next_alert_check = 0.0
        sessions: dict[str, dict[str, Any]] = {}
        print(
            f"ai-assistant: running username={bot_username or '-'} poll_timeout={poll_timeout}s "
            f"alert_interval={alert_interval}s db={settings.ai_price_alerts_db_path}",
            flush=True,
        )
        while True:
            now = time.time()
            if now >= next_alert_check:
                try:
                    result = check_and_send_price_alerts(settings, store, bot)
                    print(f"ai-assistant: alert_check {json.dumps(result, ensure_ascii=False)}", flush=True)
                except Exception as exc:
                    print(f"ai-assistant: alert_check failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                next_alert_check = now + alert_interval
            try:
                updates = bot.get_updates(offset, timeout=poll_timeout)
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                print(f"ai-assistant: getUpdates failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                time.sleep(5)
                settings = Settings.load()
                if not settings.ai_assistant_enable or not settings.ai_bot_token:
                    break
                continue
            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                callback_query = update.get("callback_query")
                if isinstance(callback_query, dict):
                    try:
                        callback_id = str(callback_query.get("id") or "")
                        if callback_id:
                            try:
                                bot.answer_callback_query(callback_id)
                            except Exception:
                                pass
                        reply = handle_callback_query(settings, store, callback_query, sessions=sessions)
                        if reply:
                            callback_message = callback_query.get("message", {})
                            chat_id = callback_message.get("chat", {}).get("id") if isinstance(callback_message, dict) else None
                            if chat_id is not None:
                                bot.send_message(chat_id, reply.text, reply_markup=reply.reply_markup)
                    except Exception as exc:
                        print(f"ai-assistant: callback failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                        try:
                            callback_message = callback_query.get("message", {})
                            chat_id = callback_message.get("chat", {}).get("id") if isinstance(callback_message, dict) else None
                            if chat_id is not None:
                                bot.send_message(chat_id, f"按钮处理失败：{type(exc).__name__}: {exc}")
                        except Exception:
                            pass
                    continue
                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                try:
                    reply = handle_message_reply(
                        settings,
                        store,
                        message,
                        bot_username=bot_username,
                        bot_user_id=bot_user_id,
                        sessions=sessions,
                    )
                    if reply:
                        chat_id = message.get("chat", {}).get("id")
                        bot.send_message(chat_id, reply.text, reply_markup=reply.reply_markup)
                except Exception as exc:
                    print(f"ai-assistant: message failed {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
                    try:
                        chat_id = message.get("chat", {}).get("id")
                        bot.send_message(chat_id, f"处理失败：{type(exc).__name__}: {exc}")
                    except Exception:
                        pass


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
        direction=str(data.get("direction") or ""),
        target_price=parse_price(data.get("target_price") or ""),
        source="web",
        note=str(data.get("note") or ""),
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
