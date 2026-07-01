from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass
from typing import Any

import requests

from .config import Settings
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


HELP_TEXT = """泡泡 AI 助手 Bot

这个机器人和群里推送雷达信号的 Bot 分开，主要负责私聊交互和价格提醒。

常用命令：
/alert BTC 高于 58000
/alert ETH 跌破 3200
/price BTC
/alerts
/pause 12
/resume 12
/delete 12
/ai 帮我解释最近雷达状态

也可以直接说：
BTC 跌破 58000 提醒我
ETH 突破 4200 提醒我
"""


GROUP_CHAT_TYPES = {"group", "supergroup"}
NON_PRIVATE_CHAT_TYPES = GROUP_CHAT_TYPES | {"channel"}
ALERT_CREATE_INTENT_RE = re.compile(
    r"(提醒我|提醒一下|提醒|通知我|通知一下|通知|叫我|设置|设个|创建|添加|到价|到了|达到|涨到|跌到|alert)",
    re.IGNORECASE,
)


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
            "allowed_updates": json.dumps(["message"]),
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

    def send_message(self, chat_id: str | int, text: str) -> bool:
        safe_text = telegram_plain_text(text) or "（无内容）"
        response = requests.post(
            f"{self.base_url}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": safe_text,
                "disable_web_page_preview": True,
            },
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


def call_ai_provider(settings: Settings, user_text: str, store: PriceAlertStore, user_id: str) -> str:
    if not settings.ai_provider_enable or not settings.ai_api_key:
        return local_assistant_reply(settings, store, user_id)
    alerts = store.list_alerts(user_id=user_id, limit=20)
    alert_lines = [
        f"{item.id}. {item.symbol} {item.direction_label} {format_price(item.target_price)} {item.status}"
        for item in alerts
    ]
    system_prompt = (
        "你是泡泡雷达的 AI 助手。回答必须用中文，简洁直接。"
        "你可以解释运行状态、价格提醒和雷达信号，但不能声称自己能直接交易。"
        "涉及投资判断时强调风险，不给确定收益承诺。"
    )
    context = "\n".join(
        [
            "当前运行状态：",
            runtime_context(settings),
            "",
            "用户价格提醒：",
            "\n".join(alert_lines) if alert_lines else "暂无",
        ]
    )
    response = requests.post(
        f"{settings.ai_base_url.rstrip('/')}/chat/completions",
        headers={
            **HTTP_HEADERS,
            "Authorization": f"Bearer {settings.ai_api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": settings.ai_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": f"{context}\n\n用户问题：{user_text}"},
            ],
            "temperature": 0.2,
        },
        timeout=max(5, int(settings.ai_request_timeout_sec)),
    )
    response.raise_for_status()
    data = response.json()
    choices = data.get("choices") if isinstance(data, dict) else None
    if not choices:
        return "AI 接口返回为空。"
    content = choices[0].get("message", {}).get("content", "")
    return str(content).strip() or "AI 接口没有返回正文。"


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
        return HELP_TEXT

    lowered = text.lower()
    if lowered in {"/start", "/help", "help", "帮助"}:
        return HELP_TEXT

    if lowered.startswith("/price"):
        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            return "用法：/price BTC"
        try:
            return price_text(settings, parts[1])
        except Exception as exc:
            return f"价格查询失败：{type(exc).__name__}: {exc}"

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

    if lowered.startswith("/ai"):
        prompt = text.split(maxsplit=1)[1] if len(text.split(maxsplit=1)) > 1 else ""
        if not prompt:
            return "用法：/ai 你的问题"
        try:
            return call_ai_provider(settings, prompt, store, user_id)
        except Exception as exc:
            return f"AI 回答失败：{type(exc).__name__}: {exc}"

    if settings.ai_provider_enable and settings.ai_api_key:
        try:
            return call_ai_provider(settings, text, store, user_id)
        except Exception as exc:
            return f"AI 回答失败：{type(exc).__name__}: {exc}"
    return local_assistant_reply(settings, store, user_id)


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
                message = update.get("message")
                if not isinstance(message, dict):
                    continue
                try:
                    reply = handle_message(settings, store, message, bot_username=bot_username, bot_user_id=bot_user_id)
                    if reply:
                        chat_id = message.get("chat", {}).get("id")
                        bot.send_message(chat_id, reply)
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
