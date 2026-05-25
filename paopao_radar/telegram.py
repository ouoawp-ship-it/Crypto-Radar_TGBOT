from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Any

import requests

from .config import Settings
from .storage import JsonStore


@dataclass
class PushResult:
    status: str
    reason: str
    sent: bool = False


def utc_ts() -> int:
    return int(time.time())


def chunk_text(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        extra = len(line) + (1 if current else 0)
        if current and len(current) + extra > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [text]


def plain_fallback(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[*_`]", "", unescape(without_tags))


TOPIC_TEMPLATE_NAMES = {
    "TG_RADAR_SUMMARY": "资金摘要",
    "TG_LAUNCH_ALERT": "启动预警",
    "TG_ANNOUNCEMENT_ALERT": "公告风险",
    "TG_TEST_MESSAGE": "测试消息",
}


class TelegramGateway:
    def __init__(self, settings: Settings, store: JsonStore):
        self.settings = settings
        self.store = store

    def send(
        self,
        text: str,
        template_id: str,
        dedup_key: str,
        *,
        send: bool,
        confirm_real_send: bool,
        cooldown_sec: int | None = None,
        daily_limit: int | None = None,
        parse_mode: str = "Markdown",
    ) -> PushResult:
        now = utc_ts()
        cooldown = self.settings.tg_default_cooldown_sec if cooldown_sec is None else cooldown_sec
        history = self._load_history()
        topic_id = self._topic_id_for_template(template_id)

        duplicate = self._recent_match(history, dedup_key, cooldown)
        if duplicate:
            result = PushResult("skipped", "dedup_cooldown", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id)
            return result

        if daily_limit is not None and daily_limit >= 0 and self._daily_sent_count(history, template_id, now) >= daily_limit:
            result = PushResult("skipped", "template_daily_limit", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id)
            return result

        if self._hourly_sent_count(history, now) >= self.settings.tg_global_hourly_limit:
            result = PushResult("skipped", "global_hourly_limit", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id)
            return result

        if not send:
            print("\n========== TELEGRAM DRY-RUN ==========")
            print(f"template_id: {template_id}")
            print(f"dedup_key: {dedup_key}")
            if topic_id:
                print(f"topic_id: {topic_id}")
            print(text)
            print("========== END DRY-RUN ==============\n")
            result = PushResult("dry_run", "send_flag_not_set", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id)
            return result

        if not confirm_real_send:
            result = PushResult("blocked", "missing_confirm_real_send", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id)
            return result

        if not self.settings.tg_bot_token or not self.settings.tg_chat_id:
            result = PushResult("blocked", "telegram_not_configured", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id)
            return result

        topic_id = self._ensure_topic_id_for_template(template_id)
        ok = self._send_real(text, parse_mode=parse_mode, topic_id=topic_id)
        result = PushResult("sent" if ok else "failed", "telegram_api" if ok else "telegram_api_failed", ok)
        self._record(history, template_id, dedup_key, result, text, topic_id=topic_id)
        return result

    def _send_real(self, text: str, parse_mode: str, topic_id: str = "") -> bool:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/sendMessage"
        ok = True
        for chunk in chunk_text(text, self.settings.tg_push_split_limit):
            payload: dict[str, Any] = {
                "chat_id": self.settings.tg_chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if topic_id and (
                self.settings.tg_use_topic or str(self.settings.tg_chat_id).startswith("-100")
            ):
                try:
                    payload["message_thread_id"] = int(topic_id)
                except ValueError:
                    pass
            sent = False
            for attempt in range(1, self.settings.tg_push_retry + 1):
                try:
                    response = requests.post(url, json=payload, timeout=self.settings.tg_push_timeout_sec)
                    if response.status_code == 200:
                        sent = True
                        break
                    if response.status_code == 400 and parse_mode:
                        fallback = dict(payload)
                        fallback.pop("parse_mode", None)
                        fallback["text"] = plain_fallback(chunk)
                        response = requests.post(url, json=fallback, timeout=self.settings.tg_push_timeout_sec)
                        sent = response.status_code == 200
                        break
                    if response.status_code in {429, 500, 502, 503, 504}:
                        time.sleep(min(5, attempt))
                        continue
                    break
                except Exception:
                    if attempt < self.settings.tg_push_retry:
                        time.sleep(min(5, attempt))
            ok = ok and sent
            time.sleep(0.25)
        return ok

    def _topic_id_for_template(self, template_id: str) -> str:
        return (
            self._configured_topic_id_for_template(template_id)
            or self._saved_topic_id_for_template(template_id)
            or self.settings.tg_topic_id
        )

    def _configured_topic_id_for_template(self, template_id: str) -> str:
        topic_routes = {
            "TG_RADAR_SUMMARY": self.settings.tg_radar_summary_topic_id,
            "TG_LAUNCH_ALERT": self.settings.tg_launch_alert_topic_id,
            "TG_ANNOUNCEMENT_ALERT": self.settings.tg_announcement_alert_topic_id,
            "TG_TEST_MESSAGE": self.settings.tg_test_topic_id,
        }
        return topic_routes.get(template_id, "")

    def _ensure_topic_id_for_template(self, template_id: str) -> str:
        topic_id = self._configured_topic_id_for_template(template_id)
        if topic_id:
            return topic_id
        topic_id = self._saved_topic_id_for_template(template_id)
        if topic_id:
            return topic_id
        if not self._should_auto_create_topic(template_id):
            return self.settings.tg_topic_id
        return self._create_and_save_topic(template_id) or self.settings.tg_topic_id

    def _should_auto_create_topic(self, template_id: str) -> bool:
        if template_id not in TOPIC_TEMPLATE_NAMES:
            return False
        if not self.settings.tg_auto_create_topics:
            return False
        chat_id = str(self.settings.tg_chat_id)
        return self.settings.tg_use_topic or chat_id.startswith("-100")

    def _saved_topic_id_for_template(self, template_id: str) -> str:
        data = self.store.load(self.settings.tg_topic_routes_path, {})
        if not isinstance(data, dict):
            return ""
        routes = data.get("routes", {})
        if not isinstance(routes, dict):
            return ""
        record = routes.get(template_id, {})
        if not isinstance(record, dict):
            return ""
        return str(record.get("topic_id") or "")

    def _create_and_save_topic(self, template_id: str) -> str:
        name = TOPIC_TEMPLATE_NAMES.get(template_id)
        if not name:
            return ""
        topic_id = self._create_forum_topic(name)
        if not topic_id:
            return ""
        data = self.store.load(self.settings.tg_topic_routes_path, {})
        if not isinstance(data, dict):
            data = {}
        routes = data.get("routes", {})
        if not isinstance(routes, dict):
            routes = {}
        routes[template_id] = {
            "name": name,
            "topic_id": topic_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data["routes"] = routes
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.save(self.settings.tg_topic_routes_path, data)
        return topic_id

    def _create_forum_topic(self, name: str) -> str:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/createForumTopic"
        payload: dict[str, Any] = {
            "chat_id": self.settings.tg_chat_id,
            "name": name,
        }
        try:
            response = requests.post(url, json=payload, timeout=self.settings.tg_push_timeout_sec)
        except Exception:
            return ""
        if response.status_code != 200:
            print(f"[telegram] createForumTopic failed {response.status_code}: {response.text[:300]}", file=sys.stderr)
            return ""
        try:
            data = response.json()
        except ValueError:
            return ""
        result = data.get("result", {}) if isinstance(data, dict) else {}
        if not isinstance(result, dict):
            return ""
        topic_id = result.get("message_thread_id")
        return str(topic_id or "")

    def _load_history(self) -> list[dict[str, Any]]:
        data = self.store.load(self.settings.tg_push_history_path, [])
        return data if isinstance(data, list) else []

    def _save_history(self, history: list[dict[str, Any]]) -> None:
        now = int(time.time())
        retention_days = max(1, int(self.settings.tg_push_history_retention_days))
        cutoff = now - retention_days * 86400
        retained = [
            record for record in history
            if int(record.get("ts", now)) >= cutoff
        ]
        limit = max(100, int(self.settings.tg_push_history_limit))
        self.store.save(self.settings.tg_push_history_path, retained[-limit:])

    def _record(
        self,
        history: list[dict[str, Any]],
        template_id: str,
        dedup_key: str,
        result: PushResult,
        text: str,
        topic_id: str = "",
    ) -> None:
        history.append({
            "ts": utc_ts(),
            "time": datetime.now(timezone.utc).isoformat(),
            "template_id": template_id,
            "dedup_key": dedup_key,
            "topic_id": topic_id,
            "status": result.status,
            "reason": result.reason,
            "sent": result.sent,
            "preview": text[:240],
        })
        self._save_history(history)

    @staticmethod
    def _recent_match(history: list[dict[str, Any]], dedup_key: str, cooldown_sec: int) -> bool:
        if cooldown_sec <= 0:
            return False
        cutoff = utc_ts() - cooldown_sec
        for record in reversed(history):
            if record.get("dedup_key") != dedup_key:
                continue
            if int(record.get("ts", 0)) < cutoff:
                return False
            if record.get("status") == "sent":
                return True
        return False

    @staticmethod
    def _hourly_sent_count(history: list[dict[str, Any]], now: int) -> int:
        cutoff = now - 3600
        return sum(1 for record in history if int(record.get("ts", 0)) >= cutoff and record.get("status") == "sent")

    @staticmethod
    def _daily_sent_count(history: list[dict[str, Any]], template_id: str, now: int) -> int:
        start_of_day = int(datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
        return sum(
            1 for record in history
            if record.get("template_id") == template_id
            and int(record.get("ts", 0)) >= start_of_day
            and record.get("status") == "sent"
        )
