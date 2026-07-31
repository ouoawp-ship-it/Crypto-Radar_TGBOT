from __future__ import annotations

from pathlib import Path
from typing import Any

import requests

from paopao_radar.storage import JsonStore
from paopao_radar.telegram import (
    TOPIC_TEMPLATE_NAMES,
    classify_telegram_network_error,
    classify_telegram_response,
)

from .config import OnchainSettings


ROUTE_CHECK_FILENAME = "telegram_route_check.json"


def _private_mode(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass


class TelegramRouteChecker:
    """Validate one configured forum route without creating a message."""

    def __init__(
        self,
        settings: OnchainSettings,
        *,
        http_client: Any = requests,
        timeout_sec: int = 10,
    ):
        self.settings = settings
        self.http_client = http_client
        self.timeout_sec = max(1, min(30, int(timeout_sec)))
        self.http_calls = 0

    @staticmethod
    def _empty_result() -> dict[str, object]:
        return {
            "status": "failed",
            "token_auth": "failed",
            "chat_access": "failed",
            "chat_type": "unknown",
            "forum_enabled": False,
            "bot_membership": "unknown",
            "can_send_text": False,
            "can_manage_topics": False,
            "can_pin_messages": False,
            "topic_route": "failed",
            "error": "",
            "telegram_http_calls": 0,
            "persistent_messages": 0,
        }

    def _request(
        self,
        operation: str,
        payload: dict[str, object],
    ) -> tuple[dict[str, Any] | None, str]:
        self.http_calls += 1
        url = (
            "https://api.telegram.org/bot"
            f"{self.settings.tg_bot_token}/{operation}"
        )
        try:
            response = self.http_client.post(
                url,
                json=payload,
                timeout=self.timeout_sec,
            )
        except requests.exceptions.RequestException as exc:
            return None, classify_telegram_network_error(exc)
        error_class, _error_code, _retry_after = classify_telegram_response(
            response
        )
        if error_class != "telegram_ok":
            return None, error_class
        try:
            body = response.json()
        except (TypeError, ValueError):
            return None, "telegram_invalid_response"
        if not isinstance(body, dict) or body.get("ok") is not True:
            return None, "telegram_invalid_response"
        result = body.get("result")
        if operation == "sendChatAction" and result is True:
            return {"accepted": True}, ""
        if not isinstance(result, dict):
            return None, "telegram_invalid_response"
        return result, ""

    def _check_shared_chat(
        self,
        result: dict[str, object],
    ) -> int | None:
        bot, error = self._request("getMe", {})
        if bot is None:
            result["error"] = error
            return None
        bot_id = bot.get("id")
        if not isinstance(bot_id, int) or bot_id <= 0:
            result["error"] = "telegram_invalid_response"
            return None
        result["token_auth"] = "ok"

        chat, error = self._request(
            "getChat",
            {"chat_id": self.settings.tg_chat_id},
        )
        if chat is None:
            result["error"] = error
            return None
        result["chat_access"] = "ok"
        result["chat_type"] = (
            "supergroup" if chat.get("type") == "supergroup" else "other"
        )
        result["forum_enabled"] = chat.get("is_forum") is True

        membership, error = self._request(
            "getChatMember",
            {
                "chat_id": self.settings.tg_chat_id,
                "user_id": bot_id,
            },
        )
        if membership is None:
            result["error"] = error
            return None
        status = str(membership.get("status") or "unknown")
        if status not in {
            "administrator",
            "member",
            "restricted",
            "left",
            "kicked",
        }:
            status = "unknown"
        result["bot_membership"] = status
        if status in {"left", "kicked", "unknown"}:
            result["error"] = "telegram_bot_not_member"
            return None
        can_send_text = status in {"administrator", "member"}
        if status == "restricted":
            can_send_text = membership.get("can_send_messages") is True
        result["can_send_text"] = can_send_text
        result["can_manage_topics"] = (
            membership.get("can_manage_topics") is True
        )
        result["can_pin_messages"] = (
            membership.get("can_pin_messages") is True
        )
        if not can_send_text:
            result["error"] = "telegram_send_permission_denied"
            return None
        if not bool(result["forum_enabled"]):
            result["error"] = "telegram_forum_required"
            return None
        return bot_id

    def _saved_topic_id(self) -> str:
        data = JsonStore(self.settings.data_dir).load(
            self.settings.tg_topic_routes_path,
            {},
        )
        if not isinstance(data, dict):
            return ""
        routes = data.get("routes")
        if not isinstance(routes, dict):
            return ""
        record = routes.get("TG_ONCHAIN_FLOW_ALERT")
        if not isinstance(record, dict):
            return ""
        return str(record.get("topic_id") or "")

    @staticmethod
    def _valid_topic_id(value: object) -> str:
        try:
            topic_id = int(str(value))
        except (TypeError, ValueError):
            return ""
        return str(topic_id) if topic_id > 0 else ""

    def _persist_topic_id(self, topic_id: str) -> bool:
        from scripts.paopao_config import ConfigManager, ConfigManagerError

        try:
            ConfigManager(self.settings.base_dir).set(
                "TG_ONCHAIN_FLOW_TOPIC_ID",
                topic_id,
            )
        except (ConfigManagerError, OSError, UnicodeError):
            return False
        return True

    def check(self) -> dict[str, object]:
        result = self._empty_result()
        if not (
            self.settings.tg_bot_token
            and self.settings.tg_chat_id
            and self.settings.tg_onchain_flow_topic_id
        ):
            result["error"] = "telegram_not_configured"
            return self._finalize(result)
        try:
            topic_id = int(self.settings.tg_onchain_flow_topic_id)
        except (TypeError, ValueError):
            result["error"] = "telegram_topic_not_found"
            return self._finalize(result)
        if topic_id <= 0:
            result["error"] = "telegram_topic_not_found"
            return self._finalize(result)

        if self._check_shared_chat(result) is None:
            return self._finalize(result)

        action, error = self._request(
            "sendChatAction",
            {
                "chat_id": self.settings.tg_chat_id,
                "message_thread_id": topic_id,
                "action": "typing",
            },
        )
        if action is None:
            result["error"] = error
            return self._finalize(result)
        result["topic_route"] = "ok"
        result["status"] = "ok"
        return self._finalize(result)

    def bootstrap_topic(self) -> dict[str, object]:
        result = self._empty_result()
        result.update({
            "shared_telegram_config": True,
            "topic_action": "none",
            "topic_configured": False,
            "topics_created": 0,
        })
        if not (self.settings.tg_bot_token and self.settings.tg_chat_id):
            result["error"] = "telegram_shared_config_missing"
            return self._finalize(result)
        if self._check_shared_chat(result) is None:
            return self._finalize(result)

        configured_topic = self._valid_topic_id(
            self.settings.tg_onchain_flow_topic_id
        )
        topic_id = configured_topic or self._valid_topic_id(
            self._saved_topic_id()
        )
        if topic_id:
            action, error = self._request(
                "sendChatAction",
                {
                    "chat_id": self.settings.tg_chat_id,
                    "message_thread_id": int(topic_id),
                    "action": "typing",
                },
            )
            if action is not None:
                if not configured_topic and not self._persist_topic_id(topic_id):
                    result["error"] = "telegram_topic_configuration_failed"
                    return self._finalize(result)
                result["topic_route"] = "ok"
                result["topic_action"] = "reused"
                result["topic_configured"] = True
                result["status"] = "ok"
                return self._finalize(result)
            if error not in {
                "telegram_topic_not_found",
                "telegram_topic_closed",
            }:
                result["error"] = error
                return self._finalize(result)

        if result["can_manage_topics"] is not True:
            result["error"] = "telegram_manage_topics_permission_required"
            return self._finalize(result)
        created, error = self._request(
            "createForumTopic",
            {
                "chat_id": self.settings.tg_chat_id,
                "name": TOPIC_TEMPLATE_NAMES["TG_ONCHAIN_FLOW_ALERT"],
            },
        )
        if created is None:
            result["error"] = error
            return self._finalize(result)
        topic_id = self._valid_topic_id(created.get("message_thread_id"))
        if not topic_id:
            result["error"] = "telegram_invalid_response"
            return self._finalize(result)
        result["topics_created"] = 1
        if not self._persist_topic_id(topic_id):
            result["error"] = "telegram_topic_configuration_failed"
            return self._finalize(result)
        result["topic_route"] = "ok"
        result["topic_action"] = "created"
        result["topic_configured"] = True
        result["status"] = "ok"
        return self._finalize(result)

    def _finalize(self, result: dict[str, object]) -> dict[str, object]:
        result["telegram_http_calls"] = self.http_calls
        result["persistent_messages"] = 0
        return result


def save_route_check(
    settings: OnchainSettings,
    result: dict[str, object],
) -> Path:
    settings.assert_safe_paths()
    path = settings.data_dir / ROUTE_CHECK_FILENAME
    store = JsonStore(settings.data_dir)
    store.save(path, result)
    _private_mode(settings.data_dir, 0o700)
    _private_mode(path, 0o600)
    return path
