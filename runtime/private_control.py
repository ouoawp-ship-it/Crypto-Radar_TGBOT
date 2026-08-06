from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any, Callable, Mapping

from shared.atomic_json import locked_read_json, locked_write_json


_RADARS = (
    ("launch_alert", "启动预警"),
    ("radar_summary", "资金摘要"),
    ("funding_alert", "资金费率警报"),
    ("flow_radar", "五因子资金流"),
    ("announcement_risk", "公告风险"),
)
_TOPICS = (
    ("launch_alert", "启动预警"),
    ("radar_summary", "资金摘要"),
    ("funding_alert", "资金费率警报"),
    ("flow_radar", "五因子资金流"),
    ("announcement_risk", "公告风险"),
)
_STATE_LABELS = {
    "running": "运行中",
    "ok": "正常",
    "healthy": "正常",
    "degraded": "降级运行",
    "stale": "数据过旧",
    "failed": "异常",
    "error": "异常",
    "not_running": "未运行",
    "not_initialized": "尚未初始化",
    "waiting_first_cycle": "等待首次运行",
    "disabled": "已关闭",
    "unknown": "状态未知",
}
_DELIVERY_LABELS = {
    "dry_run": "安全演练",
    "observe": "只观察",
    "real": "真实推送",
}
_MENU_PREFIXES = (
    "📡 ",
    "🩺 ",
    "📚 ",
    "📨 ",
    "📌 ",
    "⚙️ ",
    "🎛️ ",
    "🧩 ",
    "🧭 ",
    "🤖 ",
    "🔑 ",
    "🌐 ",
    "🧠 ",
    "📝 ",
    "♻️ ",
    "🚨 ",
    "🔕 ",
    "✅ ",
    "⏸️ ",
    "🏠 ",
)
_MENU_ALIASES = {
    "雷达状态": "五雷达状态",
    "系统健康": "健康摘要",
    "运行记录": "运行详情",
    "推送额度": "发送额度",
    "话题状态": "话题配置",
    "开关总览": "开关状态",
    "五雷达开关": "雷达开关",
    "开启方向": "开启方向雷达",
    "关闭方向": "关闭方向雷达",
    "开启AI": "开启AI解读",
    "关闭AI": "关闭AI解读",
    "AI配置": "AI设置",
    "开启提醒": "开启故障提醒",
    "关闭提醒": "关闭故障提醒",
    "刷新菜单": "菜单",
}
_REQUEST_ACTIONS = {
    "开启方向雷达": ("directional_on", "确认开启方向雷达"),
    "关闭方向雷达": ("directional_off", "确认关闭方向雷达"),
    "开启AI解读": ("ai_on", "确认开启AI解读"),
    "关闭AI解读": ("ai_off", "确认关闭AI解读"),
    "开启故障提醒": ("private_alert_on", "确认开启故障提醒"),
    "关闭故障提醒": ("private_alert_off", "确认关闭故障提醒"),
    "开启启动预警": ("launch_alert_on", "确认开启启动预警"),
    "关闭启动预警": ("launch_alert_off", "确认关闭启动预警"),
    "开启资金摘要": ("radar_summary_on", "确认开启资金摘要"),
    "关闭资金摘要": ("radar_summary_off", "确认关闭资金摘要"),
    "开启资金费率警报": ("funding_alert_on", "确认开启资金费率警报"),
    "关闭资金费率警报": ("funding_alert_off", "确认关闭资金费率警报"),
    "开启五因子资金流": ("flow_radar_on", "确认开启五因子资金流"),
    "关闭五因子资金流": ("flow_radar_off", "确认关闭五因子资金流"),
    "开启公告风险": ("announcement_risk_on", "确认开启公告风险"),
    "关闭公告风险": ("announcement_risk_off", "确认关闭公告风险"),
    "恢复默认提示词": ("ai_prompt_default", "确认恢复默认AI提示词"),
}
_CONFIRM_ACTIONS = {
    phrase: action for action, phrase in _REQUEST_ACTIONS.values()
}
_CONFIG_ACTIONS = {
    "directional_on": ("LAUNCH_DIRECTIONAL_ENABLE", "true", "方向雷达已开启。"),
    "directional_off": ("LAUNCH_DIRECTIONAL_ENABLE", "false", "方向雷达已关闭。"),
    "ai_on": ("LAUNCH_AI_INTERPRETER_ENABLE", "true", "AI 解读员已开启。"),
    "ai_off": ("LAUNCH_AI_INTERPRETER_ENABLE", "false", "AI 解读员已关闭。"),
    "private_alert_on": (
        "TG_PRIVATE_CONTROL_ALERT_ENABLE",
        "true",
        "主动故障提醒已开启。",
    ),
    "private_alert_off": (
        "TG_PRIVATE_CONTROL_ALERT_ENABLE",
        "false",
        "主动故障提醒已关闭。",
    ),
    "launch_alert_on": ("LAUNCH_ALERT_ENABLE", "true", "启动预警已开启。"),
    "launch_alert_off": ("LAUNCH_ALERT_ENABLE", "false", "启动预警已关闭。"),
    "radar_summary_on": ("RADAR_SUMMARY_ENABLE", "true", "资金摘要已开启。"),
    "radar_summary_off": ("RADAR_SUMMARY_ENABLE", "false", "资金摘要已关闭。"),
    "funding_alert_on": (
        "FUNDING_ALERT_ENABLE",
        "true",
        "资金费率警报已开启。",
    ),
    "funding_alert_off": (
        "FUNDING_ALERT_ENABLE",
        "false",
        "资金费率警报已关闭。",
    ),
    "flow_radar_on": ("FLOW_RADAR_ENABLE", "true", "五因子资金流已开启。"),
    "flow_radar_off": ("FLOW_RADAR_ENABLE", "false", "五因子资金流已关闭。"),
    "announcement_risk_on": (
        "ANNOUNCEMENT_RISK_ENABLE",
        "true",
        "公告风险已开启。",
    ),
    "announcement_risk_off": (
        "ANNOUNCEMENT_RISK_ENABLE",
        "false",
        "公告风险已关闭。",
    ),
}

_AI_INPUT_REQUESTS = {
    "设置AI密钥": ("api_key", "AI 密钥"),
    "设置AI接口": ("base_url", "AI 接口地址"),
    "设置AI模型": ("model", "AI 模型"),
    "设置AI提示词": ("prompt", "AI 补充提示词"),
}


@dataclass(frozen=True)
class ControlReply:
    text: str
    command: str
    keyboard: tuple[tuple[str, ...], ...] | None = None
    delete_source_message: bool = False


@dataclass(frozen=True)
class _PendingAction:
    action: str
    confirmation: str
    expires_at: float


@dataclass(frozen=True)
class _PendingInput:
    kind: str
    label: str
    expires_at: float


def _empty_reader() -> Mapping[str, Any]:
    return {}


def _empty_text_reader() -> str:
    return ""


def _configured(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {
        "configured",
        "ok",
        "true",
        "available",
        "running",
    }


def _safe_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _normalize_menu_command(value: str) -> str:
    command = value.strip()
    for prefix in _MENU_PREFIXES:
        if command.startswith(prefix):
            command = command.removeprefix(prefix).strip()
            break
    return _MENU_ALIASES.get(command, command)


class PrivateControlService:
    """Admin-only Telegram private control with no network by default."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        bot_token: str = "",
        admin_user_id: int | str | None = None,
        offset_path: str | Path,
        config_manager: Any,
        session: Any | None = None,
        radar_status_reader: Callable[[], Mapping[str, Any]] = _empty_reader,
        health_reader: Callable[[], Mapping[str, Any]] = _empty_reader,
        delivery_quota_reader: Callable[[], Mapping[str, Any]] = _empty_reader,
        topic_status_reader: Callable[[], Mapping[str, Any]] = _empty_reader,
        recent_signals_reader: Callable[[], str] = _empty_text_reader,
        push_records_reader: Callable[[], str] = _empty_text_reader,
        unpublished_reasons_reader: Callable[[], str] = _empty_text_reader,
        fault_explanations_reader: Callable[[], str] = _empty_text_reader,
        clock: Callable[[], float] = time.time,
        long_poll_sec: int = 25,
        http_timeout_sec: int = 35,
        confirmation_ttl_sec: int = 120,
        input_ttl_sec: int = 300,
    ) -> None:
        self.enabled = bool(enabled)
        self._bot_token = str(bot_token).strip()
        self._admin_user_id = self._parse_admin_id(admin_user_id)
        self._offset_path = Path(offset_path)
        self._config_manager = config_manager
        self._session = session
        self._radar_status_reader = radar_status_reader
        self._health_reader = health_reader
        self._delivery_quota_reader = delivery_quota_reader
        self._topic_status_reader = topic_status_reader
        self._recent_signals_reader = recent_signals_reader
        self._push_records_reader = push_records_reader
        self._unpublished_reasons_reader = unpublished_reasons_reader
        self._fault_explanations_reader = fault_explanations_reader
        self._clock = clock
        self._long_poll_sec = max(1, min(int(long_poll_sec), 50))
        self._http_timeout_sec = max(
            self._long_poll_sec + 1,
            min(int(http_timeout_sec), 60),
        )
        self._confirmation_ttl_sec = max(
            30, min(int(confirmation_ttl_sec), 300)
        )
        self._input_ttl_sec = max(60, min(int(input_ttl_sec), 600))
        self._pending: _PendingAction | None = None
        self._pending_input: _PendingInput | None = None

    @staticmethod
    def _parse_admin_id(value: int | str | None) -> int | None:
        if isinstance(value, bool) or value is None:
            return None
        text = str(value).strip()
        if not text.isdecimal():
            return None
        number = int(text)
        return number if number > 0 else None

    def poll_once(self) -> dict[str, Any]:
        result = {
            "status": "disabled",
            "network_activity": False,
            "telegram_http_calls": 0,
            "updates_received": 0,
            "accepted_updates": 0,
            "ignored_updates": 0,
            "replies_sent": 0,
            "sensitive_messages_delete_attempted": 0,
            "sensitive_messages_deleted": 0,
            "error": "",
        }
        if not self.enabled:
            return result
        readiness_error = self._readiness_error()
        if readiness_error:
            result.update(status="failed", error=readiness_error)
            return result

        next_offset = self._load_offset()
        first_start = next_offset is None
        updates, error = self._get_updates(next_offset, first_start)
        result["network_activity"] = True
        result["telegram_http_calls"] = 1
        if error:
            result.update(status="failed", error=error)
            return result
        result["updates_received"] = len(updates)

        if first_start:
            newest = max(
                (
                    item.get("update_id")
                    for item in updates
                    if type(item.get("update_id")) is int
                ),
                default=-1,
            )
            if not self._save_offset(newest + 1):
                result.update(status="failed", error="offset_write_failed")
                return result
            result.update(status="initialized", ignored_updates=len(updates))
            return result

        for update in sorted(updates, key=self._update_sort_key):
            update_id = update.get("update_id")
            if type(update_id) is not int or update_id < 0:
                result["ignored_updates"] += 1
                continue
            reply = self.handle_update(update)
            if reply is None:
                result["ignored_updates"] += 1
            else:
                result["accepted_updates"] += 1
                if reply.delete_source_message:
                    message = update.get("message")
                    message_id = (
                        message.get("message_id")
                        if isinstance(message, Mapping)
                        else None
                    )
                    if type(message_id) is int and message_id > 0:
                        result["sensitive_messages_delete_attempted"] += 1
                        _payload, delete_error = self._telegram_call(
                            "deleteMessage",
                            {
                                "chat_id": self._admin_user_id,
                                "message_id": message_id,
                            },
                        )
                        result["telegram_http_calls"] += 1
                        if not delete_error:
                            result["sensitive_messages_deleted"] += 1
                        else:
                            reply = ControlReply(
                                reply.text
                                + "\n⚠️ Telegram 未能自动删除刚才的输入，请你手动删除该条私聊消息。",
                                reply.command,
                                reply.keyboard,
                            )
                error = self._send_reply(reply)
                result["telegram_http_calls"] += 1
                if error:
                    self._save_offset(update_id + 1)
                    result.update(status="failed", error=error)
                    return result
                result["replies_sent"] += 1
            if not self._save_offset(update_id + 1):
                result.update(status="failed", error="offset_write_failed")
                return result

        result["status"] = "ok"
        return result

    def handle_update(self, update: Mapping[str, Any]) -> ControlReply | None:
        message = update.get("message")
        if not isinstance(message, Mapping) or not self._authorized(message):
            return None
        text = message.get("text")
        if not isinstance(text, str):
            return None
        command = _normalize_menu_command(text)

        if command in {"/start", "/menu", "菜单", "帮助", "返回主菜单"}:
            self._pending = None
            self._pending_input = None
            return ControlReply(
                self.menu_text(),
                "menu",
                self.menu_keyboard(),
            )
        if command == "取消":
            self._pending = None
            self._pending_input = None
            return ControlReply(
                "已取消，配置没有改变。",
                "input_cancelled",
                self.menu_keyboard(),
            )
        if self._pending_input is not None:
            return self._consume_ai_input(text)
        if not command:
            return None
        if command == "五雷达状态":
            return ControlReply(self._radar_status_text(), "radar_status")
        if command == "健康摘要":
            return ControlReply(self._health_text(), "health")
        if command == "发送额度":
            return ControlReply(self._delivery_quota_text(), "delivery_quota")
        if command == "话题配置":
            return ControlReply(self._topic_status_text(), "topic_status")
        if command == "开关状态":
            return ControlReply(self._switch_status_text(), "switch_status")
        if command == "运行详情":
            return ControlReply(
                "📚 运行详情\n请选择要查看的本地记录。",
                "runtime_details_menu",
                self.runtime_details_keyboard(),
            )
        if command == "最近信号":
            return ControlReply(
                self._safe_text_view(
                    self._recent_signals_reader,
                    "📡 最近信号\n暂时无法读取。",
                ),
                "recent_signals",
                self.runtime_details_keyboard(),
            )
        if command == "推送记录":
            return ControlReply(
                self._safe_text_view(
                    self._push_records_reader,
                    "📨 推送记录\n暂时无法读取。",
                ),
                "push_records",
                self.runtime_details_keyboard(),
            )
        if command == "未推送原因":
            return ControlReply(
                self._safe_text_view(
                    self._unpublished_reasons_reader,
                    "⏸️ 未推送原因\n暂时无法读取。",
                ),
                "unpublished_reasons",
                self.runtime_details_keyboard(),
            )
        if command == "故障说明":
            return ControlReply(
                self._safe_text_view(
                    self._fault_explanations_reader,
                    "🩺 中文故障说明\n暂时无法读取。",
                ),
                "fault_explanations",
                self.runtime_details_keyboard(),
            )
        if command == "雷达开关":
            return ControlReply(
                self._radar_switches_text(),
                "radar_switches_menu",
                self.radar_switches_keyboard(),
            )
        if command == "功能开关":
            return ControlReply(
                self._feature_switches_text(),
                "feature_switches_menu",
                self.feature_switches_keyboard(),
            )
        if command == "AI设置":
            return ControlReply(
                self._ai_settings_text(),
                "ai_settings_menu",
                self.ai_settings_keyboard(),
            )
        if command == "AI状态":
            return ControlReply(
                self._ai_settings_text(),
                "ai_settings_status",
                self.ai_settings_keyboard(),
            )
        if command in _AI_INPUT_REQUESTS:
            kind, label = _AI_INPUT_REQUESTS[command]
            self._pending = None
            self._pending_input = _PendingInput(
                kind=kind,
                label=label,
                expires_at=self._clock() + self._input_ttl_sec,
            )
            warning = (
                "\n⚠️ 密钥会先经过 Telegram 服务器；机器人保存后会尽力删除你的这条密钥消息。"
                "如需最高私密性，请仍在 FinalShell 设置。"
                if kind == "api_key"
                else ""
            )
            prompt = (
                "请在五分钟内发送补充提示词，可使用多行文字。它只调整白话表达，"
                "不能覆盖方向、评分和安全规则。请勿在提示词中填写密钥或其他秘密，"
                "因为该文字会随分析请求发送给 AI 接口。"
                if kind == "prompt"
                else f"请在五分钟内发送新的{label}。"
            )
            return ControlReply(
                f"{prompt}{warning}\n不想修改请发送“取消”。",
                "ai_input_required",
                (("取消",),),
            )
        if command in _REQUEST_ACTIONS:
            action, confirmation = _REQUEST_ACTIONS[command]
            blocked = self._action_block_reason(action)
            if blocked:
                self._pending = None
                return blocked
            self._pending = _PendingAction(
                action=action,
                confirmation=confirmation,
                expires_at=self._clock() + self._confirmation_ttl_sec,
            )
            return ControlReply(
                f"请在两分钟内再次发送：{confirmation}\n未确认不会修改配置。",
                "confirmation_required",
                ((confirmation, "取消"),),
            )
        if command in _CONFIRM_ACTIONS:
            return self._confirm_action(command)

        return ControlReply(
            "不支持这条指令，也不会保存其中的内容。\n\n" + self.menu_text(),
            "unsupported",
        )

    @staticmethod
    def menu_text() -> str:
        return (
            "🔐 泡泡雷达管理\n\n"
            "📊 查运行：雷达状态、系统健康\n"
            "📚 查记录：最近信号、推送结果、未推送原因\n"
            "🎛️ 改开关：五个雷达、方向分析、故障提醒\n"
            "🤖 AI 设置：密钥、接口、模型、提示词和解读开关\n\n"
            "👇 直接点击下方按钮\n"
            "🔒 真实推送只能在 FinalShell 设置；服务器操作也不在私聊执行"
        )

    @staticmethod
    def menu_keyboard() -> tuple[tuple[str, ...], ...]:
        return (
            ("📡 雷达状态", "🩺 系统健康"),
            ("📚 运行记录", "📨 推送额度"),
            ("📌 话题状态", "⚙️ 开关总览"),
            ("🎛️ 五雷达开关", "🧩 功能开关"),
            ("🤖 AI设置",),
            ("🏠 刷新菜单",),
        )

    @staticmethod
    def runtime_details_keyboard() -> tuple[tuple[str, ...], ...]:
        return (
            ("📡 最近信号", "📨 推送记录"),
            ("⏸️ 未推送原因", "🩺 故障说明"),
            ("🏠 返回主菜单",),
        )

    @staticmethod
    def radar_switches_keyboard() -> tuple[tuple[str, ...], ...]:
        return (
            ("✅ 开启启动预警", "⏸️ 关闭启动预警"),
            ("✅ 开启资金摘要", "⏸️ 关闭资金摘要"),
            ("✅ 开启资金费率警报", "⏸️ 关闭资金费率警报"),
            ("✅ 开启五因子资金流", "⏸️ 关闭五因子资金流"),
            ("✅ 开启公告风险", "⏸️ 关闭公告风险"),
            ("⚙️ 开关总览", "🏠 返回主菜单"),
        )

    @staticmethod
    def feature_switches_keyboard() -> tuple[tuple[str, ...], ...]:
        return (
            ("🧭 开启方向", "🧭 关闭方向"),
            ("🤖 开启AI", "🤖 关闭AI"),
            ("🚨 开启提醒", "🔕 关闭提醒"),
            ("⚙️ 开关总览", "🏠 返回主菜单"),
        )

    @staticmethod
    def ai_settings_keyboard() -> tuple[tuple[str, ...], ...]:
        return (
            ("🤖 AI状态",),
            ("🔑 设置AI密钥", "🌐 设置AI接口"),
            ("🧠 设置AI模型", "📝 设置AI提示词"),
            ("♻️ 恢复默认提示词",),
            ("✅ 开启AI解读", "⏸️ 关闭AI解读"),
            ("🏠 返回主菜单",),
        )

    def _authorized(self, message: Mapping[str, Any]) -> bool:
        if self._admin_user_id is None:
            return False
        chat = message.get("chat")
        sender = message.get("from")
        if not isinstance(chat, Mapping) or not isinstance(sender, Mapping):
            return False
        if chat.get("type") != "private" or sender.get("is_bot") is True:
            return False
        if any(
            key in message
            for key in (
                "forward_origin",
                "forward_from",
                "forward_from_chat",
                "forward_sender_name",
                "forward_date",
                "is_automatic_forward",
            )
        ):
            return False
        return (
            type(chat.get("id")) is int
            and type(sender.get("id")) is int
            and chat["id"] == self._admin_user_id
            and sender["id"] == self._admin_user_id
        )

    def _consume_ai_input(self, value: str) -> ControlReply:
        pending = self._pending_input
        self._pending_input = None
        if pending is None or self._clock() > pending.expires_at:
            return ControlReply(
                "输入已过期，配置没有改变。请重新进入 AI 设置。",
                "input_expired",
                self.ai_settings_keyboard(),
                delete_source_message=True,
            )
        normalized = value.strip()
        if not normalized:
            return ControlReply(
                f"{pending.label}不能为空，旧配置已保留。",
                "configuration_update_failed",
                self.ai_settings_keyboard(),
                delete_source_message=True,
            )
        try:
            if pending.kind == "api_key":
                result = self._config_manager.set("AI_API_KEY", normalized)
            elif pending.kind == "base_url":
                result = self._config_manager.set("AI_BASE_URL", normalized)
            elif pending.kind == "model":
                result = self._config_manager.set("AI_MODEL", normalized)
            elif pending.kind == "prompt":
                result = self._config_manager.set_ai_prompt(value)
            else:
                result = {"status": "failed"}
        except Exception:
            result = {"status": "failed"}
        if (
            not isinstance(result, Mapping)
            or result.get("status") != "ok"
            or result.get("value") != "configured"
        ):
            return ControlReply(
                f"{pending.label}保存失败，旧配置已保留。请检查格式后重试。",
                "configuration_update_failed",
                self.ai_settings_keyboard(),
                delete_source_message=True,
            )
        return ControlReply(
            f"{pending.label}：已配置。主 BOT 会在下一轮自动加载。",
            "ai_configuration_updated",
            self.ai_settings_keyboard(),
            delete_source_message=True,
        )

    def _confirm_action(self, confirmation: str) -> ControlReply:
        pending = self._pending
        self._pending = None
        if (
            pending is None
            or pending.confirmation != confirmation
            or pending.action != _CONFIRM_ACTIONS[confirmation]
            or self._clock() > pending.expires_at
        ):
            return ControlReply(
                "确认无效或已过期，配置没有改变。请重新发起开关操作。",
                "confirmation_invalid",
            )
        blocked = self._action_block_reason(pending.action)
        if blocked:
            return blocked
        if pending.action == "ai_prompt_default":
            try:
                result = self._config_manager.clear_ai_prompt()
            except Exception:
                result = {"status": "failed"}
            if not isinstance(result, Mapping) or result.get("status") != "ok":
                return ControlReply(
                    "恢复默认提示词失败，原提示词已保留。",
                    "configuration_update_failed",
                    self.ai_settings_keyboard(),
                )
            return ControlReply(
                "AI 补充提示词已恢复为系统默认。主 BOT 会在下一轮自动加载。",
                "configuration_updated",
                self.ai_settings_keyboard(),
            )
        key, value, success_text = _CONFIG_ACTIONS[pending.action]
        try:
            result = self._config_manager.set(key, value)
        except Exception:
            return ControlReply(
                "配置修改失败，旧配置已保留。",
                "configuration_update_failed",
            )
        if not isinstance(result, Mapping) or result.get("status") != "ok":
            return ControlReply(
                "配置修改失败，旧配置已保留。",
                "configuration_update_failed",
            )
        return ControlReply(
            success_text + "\n新设置将在主 BOT 下一轮加载时生效。",
            "configuration_updated",
            self.menu_keyboard(),
        )

    def _config_status(self) -> Mapping[str, Any]:
        try:
            status = self._config_manager.status()
        except Exception:
            return {}
        return status if isinstance(status, Mapping) else {}

    def _ai_configuration_ready(self) -> bool:
        status = self._config_status()
        return (
            _configured(status.get("AI_API_KEY"))
            and _configured(status.get("AI_BASE_URL"))
            and str(status.get("AI_MODEL", "")).strip()
            not in {"", "not_configured"}
        )

    def _action_block_reason(self, action: str) -> ControlReply | None:
        status = self._config_status()
        directional_enabled = bool(
            status.get("LAUNCH_DIRECTIONAL_ENABLE", False)
        )
        ai_enabled = bool(
            status.get("LAUNCH_AI_INTERPRETER_ENABLE", False)
        )
        if action == "directional_on" and not bool(
            status.get("LAUNCH_FUSION_ENABLE", False)
        ):
            return ControlReply(
                "方向雷达的基础功能尚未启用，已拒绝开启。请先在 FinalShell 完成基础配置。",
                "directional_prerequisite_not_ready",
                self.menu_keyboard(),
            )
        if action == "directional_off" and ai_enabled:
            return ControlReply(
                "请先关闭 AI 解读，再关闭方向雷达。配置没有改变。",
                "directional_ai_must_be_disabled_first",
                self.menu_keyboard(),
            )
        if action == "ai_on" and not directional_enabled:
            return ControlReply(
                "请先开启方向雷达，再开启 AI 解读。配置没有改变。",
                "directional_not_enabled",
                self.menu_keyboard(),
            )
        if action == "ai_on" and not self._ai_configuration_ready():
            return ControlReply(
                "AI 配置尚未完整，已拒绝开启。请先进入“AI设置”配置密钥、接口地址和模型。",
                "ai_not_ready",
                self.ai_settings_keyboard(),
            )
        return None

    @staticmethod
    def _safe_text_view(reader: Callable[[], str], fallback: str) -> str:
        try:
            text = reader()
        except Exception:
            return fallback
        if not isinstance(text, str) or not text.strip():
            return fallback
        return text.strip()[:3900]

    def _radar_switches_text(self) -> str:
        status = self._config_status()
        if not status:
            return "🎛️ 五雷达开关\n读取失败，配置没有改变。"
        switches = (
            ("LAUNCH_ALERT_ENABLE", "启动预警"),
            ("RADAR_SUMMARY_ENABLE", "资金摘要"),
            ("FUNDING_ALERT_ENABLE", "资金费率警报"),
            ("FLOW_RADAR_ENABLE", "五因子资金流"),
            ("ANNOUNCEMENT_RISK_ENABLE", "公告风险"),
        )
        lines = ["🎛️ 五雷达自动运行开关"]
        lines.extend(
            f"• {label}：{'已开启' if bool(status.get(key, True)) else '已关闭'}"
            for key, label in switches
        )
        lines.append("修改需二次确认；不重启服务，也不会改变真实推送模式。")
        return "\n".join(lines)

    def _feature_switches_text(self) -> str:
        status = self._config_status()
        if not status:
            return "🧩 辅助功能开关\n读取失败，配置没有改变。"
        return (
            "🧩 辅助功能开关\n"
            f"• 方向分析：{'已开启' if bool(status.get('LAUNCH_DIRECTIONAL_ENABLE', False)) else '已关闭'}\n"
            f"• AI 解读：{'已开启' if bool(status.get('LAUNCH_AI_INTERPRETER_ENABLE', False)) else '已关闭'}\n"
            f"• 故障提醒：{'已开启' if bool(status.get('TG_PRIVATE_CONTROL_ALERT_ENABLE', False)) else '已关闭'}\n\n"
            "点击下方按钮后，还要再次确认才会修改。"
        )

    def _ai_settings_text(self) -> str:
        status = self._config_status()
        if not status:
            return "🤖 AI 设置\n读取失败，配置没有改变。"
        prompt_status = str(status.get("AI_OPERATOR_PROMPT") or "default")
        prompt_text = {
            "configured": "自定义",
            "invalid": "异常，已回退系统默认",
        }.get(prompt_status, "系统默认")
        return (
            "🤖 AI 解读设置\n"
            f"• 密钥：{'已配置' if _configured(status.get('AI_API_KEY')) else '未配置'}\n"
            f"• 接口：{'已配置' if _configured(status.get('AI_BASE_URL')) else '未配置'}\n"
            f"• 模型：{'已配置' if _configured(status.get('AI_MODEL')) else '未配置'}\n"
            f"• 提示词：{prompt_text}\n"
            f"• AI 解读：{'已开启' if bool(status.get('LAUNCH_AI_INTERPRETER_ENABLE', False)) else '已关闭'}\n\n"
            "AI 只做白话解读，不能改变方向、分数、失效位或安全规则。\n"
            "开启和关闭仍需二次确认；这里不能切换真实推送模式。"
        )

    def _switch_status_text(self) -> str:
        status = self._config_status()
        if not status:
            return "🎛️ 开关状态\n读取失败，配置没有改变。"
        directional = bool(status.get("LAUNCH_DIRECTIONAL_ENABLE", False))
        interpreter = bool(status.get("LAUNCH_AI_INTERPRETER_ENABLE", False))
        fault_alerts = bool(
            status.get("TG_PRIVATE_CONTROL_ALERT_ENABLE", False)
        )
        return (
            "🎛️ 当前安全开关\n"
            f"• 方向雷达：{'已开启' if directional else '已关闭'}\n"
            f"• AI 解读员：{'已开启' if interpreter else '已关闭'}\n"
            f"• 主动故障提醒：{'已开启' if fault_alerts else '已关闭'}\n"
            f"• AI 配置：{'完整' if self._ai_configuration_ready() else '未完整'}\n"
            "• 真实推送：本控制菜单无权修改\n\n"
            + self._radar_switches_text()
        )

    def _radar_status_text(self) -> str:
        try:
            payload = self._radar_status_reader()
        except Exception:
            return "📊 五雷达状态\n读取失败（radar_status_unavailable）"
        radars = payload.get("radars", {}) if isinstance(payload, Mapping) else {}
        lines = ["📊 五雷达状态"]
        for key, label in _RADARS:
            item = radars.get(key, {}) if isinstance(radars, Mapping) else {}
            state = item.get("state", "unknown") if isinstance(item, Mapping) else "unknown"
            delivery = (
                item.get("delivery_mode", "") if isinstance(item, Mapping) else ""
            )
            state_text = _STATE_LABELS.get(str(state), "状态未知")
            delivery_text = _DELIVERY_LABELS.get(str(delivery), "")
            suffix = f" · {delivery_text}" if delivery_text else ""
            lines.append(f"• {label}：{state_text}{suffix}")
        return "\n".join(lines)

    def _health_text(self) -> str:
        try:
            payload = self._health_reader()
        except Exception:
            return "🩺 健康摘要\n读取失败（health_unavailable）"
        checks = payload.get("checks", []) if isinstance(payload, Mapping) else []
        counts = {"ok": 0, "warning": 0, "failed": 0, "unknown": 0}
        if isinstance(checks, list):
            for item in checks[:100]:
                state = item.get("status") if isinstance(item, Mapping) else None
                normalized = str(state).strip().lower()
                if normalized in {"ok", "healthy", "pass", "passed"}:
                    counts["ok"] += 1
                elif normalized in {"warning", "warn", "degraded", "stale"}:
                    counts["warning"] += 1
                elif normalized in {"failed", "fail", "error", "critical"}:
                    counts["failed"] += 1
                else:
                    counts["unknown"] += 1
        overall = payload.get("status", "unknown") if isinstance(payload, Mapping) else "unknown"
        return (
            "🩺 健康摘要\n"
            f"• 总体：{_STATE_LABELS.get(str(overall), '状态未知')}\n"
            f"• 正常：{counts['ok']}\n"
            f"• 提醒：{counts['warning']}\n"
            f"• 异常：{counts['failed']}\n"
            f"• 未知：{counts['unknown']}"
        )

    def _delivery_quota_text(self) -> str:
        try:
            payload = self._delivery_quota_reader()
        except Exception:
            return "📨 发送额度\n读取失败（delivery_quota_unavailable）"
        payload = payload if isinstance(payload, Mapping) else {}
        limit = _safe_integer(payload.get("daily_limit", payload.get("limit")))
        used = _safe_integer(payload.get("sent_today", payload.get("used")))
        remaining = _safe_integer(payload.get("remaining"))
        if remaining is None and limit is not None and used is not None:
            remaining = max(0, limit - used)
        return (
            "📨 最近一小时真实推送额度\n"
            f"• 上限：{limit if limit is not None else '未提供'}\n"
            f"• 已用：{used if used is not None else '未提供'}\n"
            f"• 剩余：{remaining if remaining is not None else '未提供'}"
        )

    def _topic_status_text(self) -> str:
        try:
            payload = self._topic_status_reader()
        except Exception:
            return "📌 话题配置\n读取失败（topic_status_unavailable）"
        payload = payload if isinstance(payload, Mapping) else {}
        topics = payload.get("topics", payload)
        topics = topics if isinstance(topics, Mapping) else {}
        lines = ["📌 Telegram 话题配置"]
        lines.append(
            f"• 机器人：{'已配置' if _configured(payload.get('bot')) else '未配置'}"
        )
        lines.append(
            f"• 目标群：{'已配置' if _configured(payload.get('chat')) else '未配置'}"
        )
        for key, label in _TOPICS:
            item = topics.get(key)
            if isinstance(item, Mapping):
                item = item.get("configured")
            lines.append(f"• {label}：{'已配置' if _configured(item) else '未配置'}")
        lines.append("不会显示群号、话题号或机器人密钥。")
        return "\n".join(lines)

    def _readiness_error(self) -> str:
        if not self._bot_token:
            return "private_control_bot_not_configured"
        if self._admin_user_id is None:
            return "private_control_admin_not_configured"
        if self._session is None:
            return "private_control_transport_not_configured"
        return ""

    def _load_offset(self) -> int | None:
        payload = locked_read_json(self._offset_path, None)
        if not isinstance(payload, Mapping):
            return None
        value = payload.get("next_offset")
        return value if type(value) is int and value >= 0 else None

    def _save_offset(self, next_offset: int) -> bool:
        try:
            self._offset_path.parent.mkdir(parents=True, exist_ok=True)
            self._offset_path.parent.chmod(0o700)
            locked_write_json(
                self._offset_path,
                {"schema_version": 1, "next_offset": max(0, int(next_offset))},
            )
            self._offset_path.chmod(0o600)
        except (OSError, TypeError, ValueError):
            return False
        return True

    @staticmethod
    def _update_sort_key(update: Mapping[str, Any]) -> tuple[int, int]:
        value = update.get("update_id")
        return (0, value) if type(value) is int else (1, 0)

    def _get_updates(
        self, next_offset: int | None, first_start: bool
    ) -> tuple[list[Mapping[str, Any]], str]:
        data: dict[str, Any] = {
            "timeout": 0 if first_start else self._long_poll_sec,
            "limit": 100,
            "allowed_updates": ["message"],
        }
        if first_start:
            # Telegram's negative offset returns the newest queued update and
            # forgets all older entries.  Nothing from that first response is
            # executed, so enabling the controller cannot replay old commands.
            data["offset"] = -1
        elif next_offset is not None:
            data["offset"] = next_offset
        payload, error = self._telegram_call("getUpdates", data)
        if error:
            return [], error
        updates = payload.get("result") if isinstance(payload, Mapping) else None
        if not isinstance(updates, list) or any(
            not isinstance(item, Mapping) for item in updates
        ):
            return [], "telegram_invalid_response"
        return list(updates), ""

    def _send_reply(self, reply: ControlReply) -> str:
        keyboard = reply.keyboard or self.menu_keyboard()
        payload = {
            "chat_id": self._admin_user_id,
            "text": reply.text[:3900],
            "disable_web_page_preview": True,
            "reply_markup": {
                "keyboard": [list(row) for row in keyboard],
                "resize_keyboard": True,
                "one_time_keyboard": False,
                "input_field_placeholder": "请选择管理功能",
            },
        }
        _, error = self._telegram_call("sendMessage", payload)
        return error

    def send_private_alert(self, text: str) -> bool:
        """Send a bounded alert only to the configured private administrator."""

        if self._readiness_error() or not isinstance(text, str) or not text.strip():
            return False
        return not self._send_reply(
            ControlReply(text.strip()[:3900], "proactive_fault_alert")
        )

    def _telegram_call(
        self, method: str, payload: Mapping[str, Any]
    ) -> tuple[Mapping[str, Any], str]:
        url = f"https://api.telegram.org/bot{self._bot_token}/{method}"
        try:
            response = self._session.post(
                url,
                json=dict(payload),
                timeout=self._http_timeout_sec,
            )
        except Exception as exc:
            return {}, self._network_error(exc)
        status = getattr(response, "status_code", None)
        if status != 200:
            return {}, self._http_error(status)
        try:
            body = response.json()
        except Exception:
            return {}, "telegram_invalid_response"
        if not isinstance(body, Mapping):
            return {}, "telegram_invalid_response"
        if body.get("ok") is not True:
            return {}, self._http_error(body.get("error_code"))
        return body, ""

    @staticmethod
    def _http_error(status: Any) -> str:
        if status == 401:
            return "telegram_auth_failed"
        if status == 403:
            return "telegram_forbidden"
        if status == 404:
            return "telegram_endpoint_not_found"
        if status == 409:
            return "telegram_polling_conflict"
        if status == 429:
            return "telegram_rate_limited"
        if type(status) is int and status >= 500:
            return "telegram_provider_unavailable"
        return "telegram_http_error"

    @staticmethod
    def _network_error(exc: Exception) -> str:
        name = exc.__class__.__name__.lower()
        if "timeout" in name:
            return "telegram_timeout"
        if "ssl" in name or "tls" in name:
            return "telegram_tls_failed"
        if "name" in name or "dns" in name:
            return "telegram_dns_failed"
        return "telegram_connection_failed"


__all__ = ["ControlReply", "PrivateControlService"]
