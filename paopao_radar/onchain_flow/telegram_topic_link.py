from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, urlsplit


OFFICIAL_TELEGRAM_HOSTS = {"t.me", "telegram.me"}
PUBLIC_USERNAME_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
POSITIVE_INTEGER_RE = re.compile(r"[1-9][0-9]*")
BOT_START_PARAMETERS = {"start", "startapp", "startgroup"}


class TelegramTopicLinkError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class TelegramTopicLink:
    link_type: str
    topic_id: int
    message_id: int
    channel_id: str = ""
    username: str = ""

    def public_result(
        self,
        *,
        topic_configured: bool = False,
    ) -> dict[str, object]:
        return {
            "status": "ok",
            "link_type": self.link_type,
            "chat_match": True,
            "topic_valid": True,
            "topic_configured": bool(topic_configured),
        }


def _positive_integer(
    raw: str,
    *,
    code: str,
    minimum: int = 1,
) -> int:
    if not POSITIVE_INTEGER_RE.fullmatch(raw):
        raise TelegramTopicLinkError(code, "Telegram link number is invalid")
    value = int(raw)
    if value < minimum:
        raise TelegramTopicLinkError(code, "Telegram link number is invalid")
    return value


def parse_telegram_topic_link(raw_link: str) -> TelegramTopicLink:
    link = raw_link.strip()
    if (
        not link
        or "\x00" in link
        or len(link) > 4096
        or "\r" in link
        or "\n" in link
    ):
        raise TelegramTopicLinkError(
            "topic_link_invalid",
            "Telegram message link is invalid",
        )
    try:
        parsed = urlsplit(link)
        port = parsed.port
    except ValueError as exc:
        raise TelegramTopicLinkError(
            "topic_link_invalid",
            "Telegram message link is invalid",
        ) from exc
    if parsed.scheme.lower() != "https":
        raise TelegramTopicLinkError(
            "topic_link_invalid",
            "Telegram message link must use HTTPS",
        )
    if (
        (parsed.hostname or "").lower() not in OFFICIAL_TELEGRAM_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
    ):
        raise TelegramTopicLinkError(
            "topic_link_domain_invalid",
            "Telegram message link domain is invalid",
        )
    if parsed.fragment:
        raise TelegramTopicLinkError(
            "topic_link_invalid",
            "Telegram message link fragment is not allowed",
        )

    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if any(key.lower() in BOT_START_PARAMETERS for key, _ in query_pairs):
        raise TelegramTopicLinkError(
            "topic_link_not_forum_message",
            "Telegram bot start links are not forum messages",
        )
    thread_values = [value for key, value in query_pairs if key == "thread"]
    if len(thread_values) > 1:
        raise TelegramTopicLinkError(
            "topic_link_ambiguous",
            "Telegram message link has multiple thread values",
        )
    thread_topic = (
        _positive_integer(
            thread_values[0],
            code="topic_link_topic_invalid",
            minimum=2,
        )
        if thread_values
        else None
    )

    if not parsed.path.startswith("/") or parsed.path.endswith("/"):
        raise TelegramTopicLinkError(
            "topic_link_not_forum_message",
            "Telegram link is not a forum message",
        )
    segments = parsed.path[1:].split("/")
    if not segments or any(not segment for segment in segments):
        raise TelegramTopicLinkError(
            "topic_link_not_forum_message",
            "Telegram link is not a forum message",
        )

    if segments[0].lower() == "c":
        return _parse_private_link(segments, thread_topic)
    return _parse_public_link(segments, thread_topic)


def _parse_private_link(
    segments: list[str],
    thread_topic: int | None,
) -> TelegramTopicLink:
    if thread_topic is None and len(segments) != 4:
        raise TelegramTopicLinkError(
            "topic_link_not_forum_message",
            "Private Telegram link is not a forum message",
        )
    if thread_topic is not None and len(segments) not in {3, 4}:
        raise TelegramTopicLinkError(
            "topic_link_not_forum_message",
            "Private Telegram link is not a forum message",
        )
    channel_id = segments[1]
    _positive_integer(channel_id, code="topic_link_chat_mismatch")
    message_id = _positive_integer(
        segments[-1],
        code="topic_link_not_forum_message",
    )
    path_topic = (
        _positive_integer(
            segments[-2],
            code="topic_link_topic_invalid",
            minimum=2,
        )
        if len(segments) == 4
        else None
    )
    topic_id = _resolve_topic(path_topic, thread_topic)
    return TelegramTopicLink(
        link_type="private",
        channel_id=channel_id,
        topic_id=topic_id,
        message_id=message_id,
    )


def _parse_public_link(
    segments: list[str],
    thread_topic: int | None,
) -> TelegramTopicLink:
    if thread_topic is None and len(segments) != 3:
        raise TelegramTopicLinkError(
            "topic_link_not_forum_message",
            "Public Telegram link is not a forum message",
        )
    if thread_topic is not None and len(segments) not in {2, 3}:
        raise TelegramTopicLinkError(
            "topic_link_not_forum_message",
            "Public Telegram link is not a forum message",
        )
    username = segments[0]
    if not PUBLIC_USERNAME_RE.fullmatch(username):
        raise TelegramTopicLinkError(
            "topic_link_not_forum_message",
            "Public Telegram link username is invalid",
        )
    message_id = _positive_integer(
        segments[-1],
        code="topic_link_not_forum_message",
    )
    path_topic = (
        _positive_integer(
            segments[-2],
            code="topic_link_topic_invalid",
            minimum=2,
        )
        if len(segments) == 3
        else None
    )
    topic_id = _resolve_topic(path_topic, thread_topic)
    return TelegramTopicLink(
        link_type="public",
        username=username.lower(),
        topic_id=topic_id,
        message_id=message_id,
    )


def _resolve_topic(
    path_topic: int | None,
    thread_topic: int | None,
) -> int:
    if path_topic is not None and thread_topic is not None:
        if path_topic != thread_topic:
            raise TelegramTopicLinkError(
                "topic_link_ambiguous",
                "Telegram path and thread topic values do not match",
            )
        return path_topic
    topic_id = path_topic if path_topic is not None else thread_topic
    if topic_id is None:
        raise TelegramTopicLinkError(
            "topic_link_not_forum_message",
            "Telegram link does not identify a forum topic",
        )
    return topic_id


def validate_telegram_topic_link(
    raw_link: str,
    *,
    configured_chat_id: str,
    configured_public_username: str = "",
) -> TelegramTopicLink:
    parsed = parse_telegram_topic_link(raw_link)
    if parsed.link_type == "private":
        chat_id = configured_chat_id.strip()
        if (
            not re.fullmatch(r"-100[1-9][0-9]*", chat_id)
            or chat_id != f"-100{parsed.channel_id}"
        ):
            raise TelegramTopicLinkError(
                "topic_link_chat_mismatch",
                "Telegram link does not match the configured chat",
            )
        return parsed

    configured_username = configured_public_username.strip().lstrip("@").lower()
    if not configured_username:
        raise TelegramTopicLinkError(
            "topic_link_public_chat_unverified",
            "Public Telegram chat cannot be verified offline",
        )
    if configured_username != parsed.username:
        raise TelegramTopicLinkError(
            "topic_link_chat_mismatch",
            "Telegram link does not match the configured chat",
        )
    return parsed
