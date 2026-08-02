from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from html import escape
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

import requests

from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import (
    PushResult,
    TelegramGateway,
    classify_telegram_network_error,
    classify_telegram_response,
)

from .automation_store import AutomationStore, AutomationStoreError
from .config import OAR_TELEGRAM_QUERY_ACK, OnchainSettings
from .report import TokenReportService
from .report_formatter import format_token_report
from .token_activity import TokenActivityQuery, TokenActivityQueryError


TEMPLATE_ID = "TG_ONCHAIN_QUERY"
ALLOWED_WINDOWS = {"15m", "1h", "4h"}
EVM_ADDRESS_RE = re.compile(r"(?<![0-9A-Fa-f])0x[0-9A-Fa-f]{40}(?![0-9A-Fa-f])")
COMMAND_RE = re.compile(
    r"^/oar(?:@(?P<bot>[A-Za-z0-9_]{5,64}))?(?:\s+(?P<body>.*))?$",
    re.IGNORECASE,
)
MENTION_RE = re.compile(
    r"^@(?P<bot>[A-Za-z0-9_]{5,64})(?:[\s，,:：]+(?P<body>.*))?$",
    re.IGNORECASE,
)
IGNORED_WORDS = {
    "base",
    "查询",
    "查",
    "看看",
    "链上",
    "链上异动",
    "异动",
    "是否",
    "有",
    "有没有",
    "活动",
    "代币",
    "token",
}


class TelegramQueryError(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class ParsedTelegramQuery:
    invoked: bool
    target: str = ""
    window: str = "15m"
    help_requested: bool = False
    error: str = ""


def _private_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


def parse_telegram_query(text: str, bot_username: str) -> ParsedTelegramQuery:
    raw = str(text or "").strip()
    username = str(bot_username or "").lstrip("@").strip().lower()
    if not raw or not username:
        return ParsedTelegramQuery(False)
    match = COMMAND_RE.fullmatch(raw) or MENTION_RE.fullmatch(raw)
    if match is None:
        return ParsedTelegramQuery(False)
    target_bot = str(match.group("bot") or "").lower()
    if target_bot and target_bot != username:
        return ParsedTelegramQuery(False)
    body = str(match.group("body") or "").strip()
    if not body or body.lower() in {"help", "帮助", "使用方法"}:
        return ParsedTelegramQuery(True, help_requested=True)

    window = "15m"
    windows = [
        token.lower()
        for token in re.findall(r"(?<![A-Za-z0-9])(15m|1h|4h|24h)(?![A-Za-z0-9])", body, re.I)
    ]
    if len(set(windows)) > 1:
        return ParsedTelegramQuery(True, error="query_window_ambiguous")
    if windows:
        window = windows[0]
    if window not in ALLOWED_WINDOWS:
        return ParsedTelegramQuery(True, error="query_window_not_allowed")

    addresses = EVM_ADDRESS_RE.findall(body)
    if len(set(item.lower() for item in addresses)) > 1:
        return ParsedTelegramQuery(True, error="query_contract_ambiguous")
    if addresses:
        return ParsedTelegramQuery(True, addresses[0].lower(), window)

    candidates: list[str] = []
    for token in re.findall(r"[$A-Za-z0-9_\-]+", body):
        normalized = token.strip().lstrip("$").upper()
        if not normalized or normalized.lower() in IGNORED_WORDS:
            continue
        if normalized.lower() in ALLOWED_WINDOWS:
            continue
        if re.fullmatch(r"[A-Z0-9]{2,30}", normalized):
            candidates.append(normalized)
    unique = list(dict.fromkeys(candidates))
    if len(unique) != 1:
        return ParsedTelegramQuery(True, error="query_target_invalid")
    return ParsedTelegramQuery(True, unique[0], window)


class TelegramQueryHttpClient:
    def __init__(
        self,
        token: str,
        *,
        http_client: Any = requests,
        timeout_sec: int = 20,
    ):
        self.token = token
        self.http_client = http_client
        self.timeout_sec = max(1, min(60, int(timeout_sec)))
        self.http_calls = 0

    def call(
        self,
        method: str,
        payload: dict[str, object],
        *,
        timeout_sec: int | None = None,
    ) -> object:
        self.http_calls += 1
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        try:
            response = self.http_client.post(
                url,
                json=payload,
                timeout=(
                    self.timeout_sec
                    if timeout_sec is None
                    else max(1, int(timeout_sec))
                ),
            )
        except requests.exceptions.RequestException as exc:
            raise TelegramQueryError(
                classify_telegram_network_error(exc)
            ) from exc
        error_class, _error_code, _retry_after = classify_telegram_response(
            response
        )
        if int(getattr(response, "status_code", 0) or 0) == 409:
            raise TelegramQueryError("telegram_polling_conflict")
        if error_class != "telegram_ok":
            raise TelegramQueryError(error_class)
        try:
            body = response.json()
        except (TypeError, ValueError) as exc:
            raise TelegramQueryError("telegram_invalid_response") from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise TelegramQueryError("telegram_invalid_response")
        return body.get("result")


class TelegramQueryState:
    def __init__(
        self,
        path: Path,
        data_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.path = path
        self.store = JsonStore(data_dir)
        self.clock = clock

    def load(self) -> dict[str, object]:
        value = self.store.load(self.path, {})
        return value if isinstance(value, dict) else {}

    def save_offset(self, next_offset: int, *, initialized: bool = True) -> None:
        now = int(self.clock())

        def update(value: object) -> dict[str, object]:
            state = dict(value) if isinstance(value, dict) else {}
            state["schema_version"] = "oar-telegram-query-v1"
            state["initialized"] = bool(initialized)
            state["next_offset"] = max(0, int(next_offset))
            state["updated_at"] = now
            return state

        self.store.update(self.path, update, {})
        _private_mode(self.path.parent, 0o700)
        _private_mode(self.path, 0o600)

    def allow_query(
        self,
        user_id: int,
        *,
        cooldown_sec: int,
        max_per_hour: int,
    ) -> tuple[bool, str]:
        now = int(self.clock())
        user_key = sha256(
            f"oar-telegram-query:{int(user_id)}".encode("utf-8")
        ).hexdigest()
        result = {"allowed": False, "reason": "query_rate_limited"}

        def update(value: object) -> dict[str, object]:
            state = dict(value) if isinstance(value, dict) else {}
            recent = [
                int(item)
                for item in state.get("query_timestamps", [])
                if isinstance(item, int) and item > now - 3600
            ]
            per_user = state.get("user_last_query")
            per_user = dict(per_user) if isinstance(per_user, dict) else {}
            last = int(per_user.get(user_key) or 0)
            if last and now - last < int(cooldown_sec):
                result["reason"] = "query_user_cooldown"
            elif len(recent) >= int(max_per_hour):
                result["reason"] = "query_hourly_limit"
            else:
                result["allowed"] = True
                result["reason"] = ""
                recent.append(now)
                per_user[user_key] = now
            state["schema_version"] = "oar-telegram-query-v1"
            state["query_timestamps"] = recent
            state["user_last_query"] = {
                key: int(timestamp)
                for key, timestamp in per_user.items()
                if int(timestamp) > now - 3600
            }
            state["updated_at"] = now
            return state

        self.store.update(self.path, update, {})
        _private_mode(self.path.parent, 0o700)
        _private_mode(self.path, 0o600)
        return bool(result["allowed"]), str(result["reason"])


def build_query_gateway(settings: OnchainSettings) -> TelegramGateway:
    history_path = settings.data_dir / "telegram_query_history.json"
    outbox_path = settings.data_dir / "telegram_query_outbox.json"
    gateway_settings = Settings(
        base_dir=settings.base_dir,
        data_dir=settings.data_dir,
        tg_bot_token=settings.tg_bot_token,
        tg_chat_id=settings.tg_chat_id,
        tg_onchain_flow_topic_id=settings.tg_onchain_flow_topic_id,
        tg_use_topic=True,
        tg_auto_create_topics=False,
        tg_topic_routes_path=settings.tg_topic_routes_path,
        tg_push_history_path=history_path,
        tg_outbox_path=outbox_path,
        tg_global_hourly_limit=settings.oar_telegram_query_max_per_hour,
        tg_default_cooldown_sec=0,
        signal_events_path=settings.signal_events_path,
        signal_events_db_path=settings.signal_events_db_path,
        runtime_status_path=settings.runtime_status_path,
    )
    return TelegramGateway(gateway_settings, JsonStore(settings.data_dir))


class TelegramQueryService:
    def __init__(
        self,
        settings: OnchainSettings,
        *,
        api: TelegramQueryHttpClient | None = None,
        gateway: TelegramGateway | None = None,
        automation_store: AutomationStore | None = None,
        report_factory: Callable[[OnchainSettings, TokenActivityQuery], Any]
        | None = None,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.settings = settings
        self.api = api or TelegramQueryHttpClient(
            settings.tg_bot_token,
            timeout_sec=settings.oar_telegram_query_poll_timeout_sec + 10,
        )
        self.gateway = gateway
        self.automation_store = automation_store or AutomationStore.from_settings(
            settings
        )
        self.report_factory = report_factory or TokenReportService.from_settings
        self.clock = clock
        self.monotonic = monotonic
        self._state: TelegramQueryState | None = None
        self.bot_username = ""

    @property
    def state(self) -> TelegramQueryState:
        if self._state is None:
            self._state = TelegramQueryState(
                self.settings.oar_telegram_query_state_path,
                self.settings.data_dir,
                clock=self.clock,
            )
        return self._state

    def validate_gate(
        self,
        *,
        allow_network: bool,
        send: bool,
        confirm_real_send: bool,
    ) -> None:
        self.settings.validate()
        if not allow_network:
            raise TelegramQueryError("allow_network_required")
        if not (send and confirm_real_send):
            raise TelegramQueryError("telegram_query_send_gate_blocked")
        if not (
            self.settings.oar_telegram_query_enable
            and self.settings.oar_telegram_query_ack
            == OAR_TELEGRAM_QUERY_ACK
        ):
            raise TelegramQueryError("telegram_query_configuration_blocked")

    def _startup(self) -> None:
        webhook = self.api.call("getWebhookInfo", {})
        if not isinstance(webhook, dict):
            raise TelegramQueryError("telegram_invalid_response")
        if str(webhook.get("url") or ""):
            raise TelegramQueryError("telegram_webhook_conflict")
        bot = self.api.call("getMe", {})
        if not isinstance(bot, dict):
            raise TelegramQueryError("telegram_invalid_response")
        bot_id = bot.get("id")
        if not isinstance(bot_id, int) or bot_id <= 0:
            raise TelegramQueryError("telegram_invalid_response")
        username = str(bot.get("username") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{5,64}", username):
            raise TelegramQueryError("telegram_bot_username_missing")
        chat = self.api.call(
            "getChat",
            {"chat_id": self.settings.tg_chat_id},
        )
        if not isinstance(chat, dict):
            raise TelegramQueryError("telegram_invalid_response")
        if (
            str(chat.get("type") or "") != "supergroup"
            or chat.get("is_forum") is not True
        ):
            raise TelegramQueryError("telegram_query_forum_required")
        membership = self.api.call(
            "getChatMember",
            {
                "chat_id": self.settings.tg_chat_id,
                "user_id": bot_id,
            },
        )
        if not isinstance(membership, dict):
            raise TelegramQueryError("telegram_invalid_response")
        if str(membership.get("status") or "") != "administrator":
            raise TelegramQueryError("telegram_query_admin_required")
        self.bot_username = username

    def _poll(self, offset: int, *, timeout_sec: int) -> list[dict[str, object]]:
        result = self.api.call(
            "getUpdates",
            {
                "offset": max(0, int(offset)),
                "limit": 20,
                "timeout": max(0, int(timeout_sec)),
                "allowed_updates": ["message"],
            },
            timeout_sec=max(5, int(timeout_sec) + 10),
        )
        if not isinstance(result, list):
            raise TelegramQueryError("telegram_invalid_response")
        return [item for item in result if isinstance(item, dict)]

    def _initialize_offset(self) -> int:
        current = self.state.load()
        if current.get("initialized") is True:
            return max(0, int(current.get("next_offset") or 0))
        backlog = self._poll(0, timeout_sec=0)
        next_offset = max(
            (int(item.get("update_id") or -1) + 1 for item in backlog),
            default=0,
        )
        self.state.save_offset(next_offset)
        return next_offset

    def run_live(
        self,
        *,
        allow_network: bool,
        send: bool,
        confirm_real_send: bool,
        duration_minutes: float | None = None,
    ) -> dict[str, object]:
        self.validate_gate(
            allow_network=allow_network,
            send=send,
            confirm_real_send=confirm_real_send,
        )
        self._startup()
        next_offset = self._initialize_offset()
        deadline = (
            None
            if duration_minutes is None
            else self.monotonic() + max(0.0, float(duration_minutes)) * 60
        )
        processed = 0
        queries = 0
        while deadline is None or self.monotonic() < deadline:
            updates = self._poll(
                next_offset,
                timeout_sec=self.settings.oar_telegram_query_poll_timeout_sec,
            )
            for update in updates:
                update_id = int(update.get("update_id") or -1)
                if update_id < 0:
                    continue
                next_offset = max(next_offset, update_id + 1)
                try:
                    outcome = self.process_update(update)
                except Exception:
                    # A malformed or locally inconsistent update must not
                    # terminate the long-running polling worker.  The raw
                    # exception is deliberately not logged because it may
                    # contain local paths or provider details.
                    outcome = "query_update_failed"
                processed += 1
                queries += int(outcome == "query_completed")
                self.state.save_offset(next_offset)
                print(json.dumps({
                    "status": "ok",
                    "event": "telegram_query_update",
                    "outcome": outcome,
                    "telegram_http_calls": self.api.http_calls,
                }, ensure_ascii=False, sort_keys=True))
        return {
            "status": "ok",
            "updates_processed": processed,
            "queries_completed": queries,
            "telegram_http_calls": self.api.http_calls,
            "persistent_query_replies": queries,
        }

    def process_update(self, update: dict[str, object]) -> str:
        message = update.get("message")
        if not isinstance(message, dict):
            return "ignored_update_type"
        text = message.get("text")
        if not isinstance(text, str) or len(text) > 500:
            return "ignored_non_text"
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return "ignored_invalid_message"
        if str(chat.get("id") or "") != self.settings.tg_chat_id:
            return "ignored_other_chat"
        if str(message.get("message_thread_id") or "") != str(
            self.settings.tg_onchain_flow_topic_id
        ):
            return "ignored_other_topic"
        if sender.get("is_bot") is True:
            return "ignored_bot_sender"
        message_date = int(message.get("date") or 0)
        if message_date and message_date < int(self.clock()) - 120:
            return "ignored_stale_message"

        parsed = parse_telegram_query(text, self.bot_username)
        if not parsed.invoked:
            return "ignored_not_invoked"
        message_id = int(message.get("message_id") or 0)
        update_id = int(update.get("update_id") or 0)
        if message_id <= 0:
            return "ignored_invalid_message"
        user_id = sender.get("id")
        if not isinstance(user_id, int) or user_id <= 0:
            return "ignored_invalid_sender"
        allowed, reason = self.state.allow_query(
            user_id,
            cooldown_sec=self.settings.oar_telegram_query_cooldown_sec,
            max_per_hour=self.settings.oar_telegram_query_max_per_hour,
        )
        if not allowed:
            # Fail quietly after the first response so an abusive sender cannot
            # turn rate-limit notices into a Telegram message flood.
            return reason
        if parsed.help_requested:
            self._send_text(self._help_text(), message_id, update_id)
            return "help_sent"
        if parsed.error:
            self._send_text(self._error_text(parsed.error), message_id, update_id)
            return "query_rejected"

        chain, contract, resolution_error = self._resolve_target(parsed.target)
        if resolution_error:
            self._send_text(
                self._error_text(resolution_error), message_id, update_id
            )
            return "query_rejected"

        try:
            query = TokenActivityQuery.create(
                self.settings,
                chain=chain,
                contract=contract,
                window=parsed.window,
                max_events=self.settings.oar_telegram_query_max_events,
                max_rpc_requests=(
                    self.settings.oar_telegram_query_max_rpc_requests
                ),
                top_n=self.settings.oar_telegram_query_top_n,
                with_price=False,
                min_usd=None,
            )
            payload = self.report_factory(self.settings, query).execute(
                query,
                with_ai=False,
            )
            text_out = format_token_report(payload)
            text_out += (
                "\n\n<i>群内只读查询 · 未调用 AI · 不执行交易 · "
                "不构成投资建议</i>"
            )
        except TokenActivityQueryError as exc:
            text_out = self._error_text(exc.reason)
        except Exception:
            text_out = self._error_text("query_runtime_failed")
        result = self._send_text(text_out, message_id, update_id)
        return "query_completed" if result.sent else "query_reply_failed"

    def _resolve_target(self, target: str) -> tuple[str, str, str]:
        if EVM_ADDRESS_RE.fullmatch(target):
            return "base", target.lower(), ""
        normalized = target.upper()
        # The Registry accepts market symbols (for example CBDOGEUSDT), while
        # the public query syntax intentionally also accepts token symbols
        # (CBDOGE).  Never submit the short token symbol to the strict Registry
        # validator: normalize it deterministically first.
        symbols = [
            normalized if normalized.endswith("USDT") else f"{normalized}USDT"
        ]
        resolved: list[tuple[str, str]] = []
        blocked = ""
        for symbol in symbols:
            try:
                outcome = self.automation_store.resolve_registry(symbol)
            except AutomationStoreError as exc:
                if exc.code == "invalid_symbol":
                    continue
                return "", "", "registry_resolution_failed"
            if outcome.get("status") == "resolved":
                token = outcome.get("token")
                if isinstance(token, dict):
                    contract = str(token.get("contract_address") or "")
                    if EVM_ADDRESS_RE.fullmatch(contract):
                        resolved.append(
                            (
                                str(token.get("chain") or "base"),
                                contract.lower(),
                            )
                        )
            elif outcome.get("status") in {
                "registry_not_verified",
                "ambiguous_contract",
            }:
                blocked = str(outcome.get("status"))
        unique = list(dict.fromkeys(resolved))
        if len(unique) == 1:
            return unique[0][0], unique[0][1], ""
        if len(unique) > 1:
            return "", "", "ambiguous_contract"
        return "", "", blocked or "registry_symbol_not_found"

    def _send_text(
        self,
        text: str,
        reply_to_message_id: int,
        update_id: int,
    ) -> PushResult:
        if self.gateway is None:
            self.gateway = build_query_gateway(self.settings)
        return self.gateway.send(
            text,
            TEMPLATE_ID,
            f"oar-query:update:{int(update_id)}",
            send=True,
            confirm_real_send=True,
            cooldown_sec=7 * 86400,
            parse_mode="HTML",
            reply_to_message_id=reply_to_message_id,
            enrich_market_context=False,
        )

    def _help_text(self) -> str:
        username = escape(self.bot_username)
        return (
            "🔎 <b>链上异动查询</b>\n\n"
            f"<code>@{username} 查询 CBDOGE 15m</code>\n"
            f"<code>@{username} 查询 0x合约地址 1h</code>\n"
            f"<code>/oar@{username} CBDOGE 4h</code>\n\n"
            "支持窗口：15m、1h、4h。Symbol 只解析本地已验证 Registry，"
            "不会根据名称猜合约。"
        )

    @staticmethod
    def _error_text(reason: str) -> str:
        messages = {
            "query_window_ambiguous": "查询中只能指定一个时间窗口。",
            "query_window_not_allowed": "仅支持 15m、1h、4h 窗口。",
            "query_contract_ambiguous": "一次只能查询一个合约。",
            "query_target_invalid": "请提供一个已验证 Symbol 或完整 Base 合约地址。",
            "registry_symbol_not_found": "该 Symbol 尚未进入已验证 Registry，请改用完整合约地址。",
            "registry_not_verified": "该 Symbol 的合约尚未完成 Registry 验证。",
            "ambiguous_contract": "该 Symbol 对应多个合约，已拒绝猜测。",
            "registry_resolution_failed": "Registry 暂时无法完成解析，请稍后重试。",
            "query_user_cooldown": "查询过于频繁，请稍后再试。",
            "query_hourly_limit": "本小时群内链上查询额度已用完。",
            "invalid_contract": "Base 合约地址格式无效。",
            "query_runtime_failed": "链上查询暂时失败，未形成结论。",
            "telegram_query_admin_required": "机器人需要群管理员权限才能可靠接收 @Bot 查询。",
        }
        message = messages.get(reason, "链上查询未完成，请检查输入后重试。")
        return f"⚠️ <b>链上查询未完成</b>\n{escape(message)}"
