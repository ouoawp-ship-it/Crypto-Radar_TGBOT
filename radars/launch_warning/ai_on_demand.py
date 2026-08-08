"""Administrator-triggered AI interpretation for one immutable launch signal."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings
from shared.signal_store import SignalEventStore

from .ai_interpreter import OPERATOR_PROMPT, OpenAiCompatibleLaunchInterpreter


AI_ON_DEMAND_POLICY_VERSION = "launch-ai-on-demand-v1"
AI_ON_DEMAND_MAX_SIGNAL_AGE_SEC = 7 * 24 * 3600
_PUBLIC_REF_PATTERN = re.compile(r"sig_[0-9a-f]{20}")
_BOT_USERNAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9_]{4,31}")
_CST = timezone(timedelta(hours=8))
_TELEGRAM_USER_ID_MAX = 9_223_372_036_854_775_807


def positive_telegram_user_id(value: object) -> int | None:
    """Parse one Telegram user id without trusting its config value type."""

    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if (
        not text
        or len(text) > 19
        or not text.isascii()
        or not text.isdecimal()
        or text.startswith("0")
    ):
        return None
    parsed = int(text)
    return parsed if parsed <= _TELEGRAM_USER_ID_MAX else None


def telegram_bot_username_configured(value: object) -> bool:
    username = str(value or "").strip().lstrip("@")
    return bool(
        _BOT_USERNAME_PATTERN.fullmatch(username)
        and username.lower().endswith("bot")
    )


def build_launch_ai_deep_link(bot_username: str, public_ref: str) -> str:
    """Return a Telegram private-chat deep link without embedding signal data."""

    username = str(bot_username or "").strip().lstrip("@")
    reference = str(public_ref or "").strip()
    if (
        not telegram_bot_username_configured(username)
    ):
        return ""
    if not _PUBLIC_REF_PATTERN.fullmatch(reference):
        return ""
    return f"https://t.me/{username}?start=ai_{reference}"


def _prebuilt_snapshot_source(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Adapt a stored prompt-ready snapshot to the interpreter input contract.

    The normal interpreter deliberately rebuilds its own whitelist.  Recreate
    the original source shape here so that the whitelist is applied again
    without dropping the already-bounded market fact groups.
    """

    market_facts: dict[str, Any] = {}
    for key in ("price_open_interest", "active_flow", "funding_basis"):
        value = snapshot.get(key)
        if isinstance(value, Mapping):
            market_facts.update(value)
    source: dict[str, Any] = {
        "discovery_score": snapshot.get("discovery_score"),
        "rule_result": snapshot.get("rule_result"),
        "launch_phase": snapshot.get("launch_phase"),
        "smc_filter": snapshot.get("smc_filter"),
        "multi_timeframe": snapshot.get("multi_timeframe"),
        "structure": snapshot.get("structure"),
        "plan": snapshot.get("plan"),
        "completeness": snapshot.get("completeness"),
        "market_facts": market_facts,
    }
    return {key: value for key, value in source.items() if value is not None}


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _safe_items(value: object, *, limit: int = 2) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()
        for item in value[:limit]
        if isinstance(item, str) and item.strip()
    ]


def _direction_text(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized.startswith("bull") or normalized in {"up", "long"}:
        return "偏多"
    if normalized.startswith("bear") or normalized in {"down", "short"}:
        return "偏空"
    return "方向待确认"


def format_on_demand_ai_result(
    *,
    symbol: str,
    signal_ts: int,
    result: Mapping[str, Any],
) -> str:
    """Render only validated AI fields and an explicit rule boundary."""

    clean_symbol = str(symbol or "启动信号").strip().upper()[:32]
    try:
        signal_time = datetime.fromtimestamp(int(signal_ts), _CST).strftime(
            "%m-%d %H:%M CST"
        )
    except (OSError, OverflowError, TypeError, ValueError):
        signal_time = "时间未知"
    direction = _direction_text(result.get("direction"))
    stage = str(result.get("stage") or "阶段待确认").strip()[:64]
    summary = str(result.get("summary") or "").strip()[:600]
    lines = [
        f"🤖 AI按需解读 · {clean_symbol}",
        f"🕒 基于：{signal_time} 发出的信号快照",
        f"🧭 原规则：{direction}｜{stage}",
        "",
        f"💬 AI观点：{summary}",
    ]
    sections = (
        ("✅ 支持", "supporting_evidence"),
        ("⚠️ 反向", "counter_evidence"),
        ("🛡️ 风险", "risk_notes"),
        ("⏳ 等待", "wait_for"),
        ("📌 限制", "limitations"),
    )
    for label, key in sections:
        items = _safe_items(result.get(key))
        if items:
            lines.append(f"{label}：{'；'.join(items)}")
    lines.extend([
        "",
        "AI只负责解释这条信号，不改变发现分、方向证据分、行情阶段、失效规则或原始结论。",
    ])
    return "\n".join(lines)[:3400]


class LaunchAiOnDemandService:
    """Load, deduplicate, interpret and cache one administrator-selected signal."""

    def __init__(
        self,
        *,
        settings_reader: Callable[[], Settings],
        signal_store: SignalEventStore,
        session: Any | None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings_reader = settings_reader
        self._signal_store = signal_store
        self._session = session
        self._clock = clock

    def request(self, public_ref: str) -> dict[str, Any]:
        settings = self._settings_reader()
        if not bool(getattr(settings, "launch_ai_interpreter_enable", False)):
            return {"status": "disabled"}
        if not (
            str(getattr(settings, "ai_api_key", "")).strip()
            and str(getattr(settings, "ai_base_url", "")).strip()
            and str(getattr(settings, "ai_model", "")).strip()
            and self._session is not None
        ):
            return {"status": "not_configured"}

        now = int(self._clock())
        loaded = self._signal_store.load_ai_context_snapshot(public_ref)
        loaded_status = str(loaded.get("status") or "")
        if loaded_status in {"signal_unavailable", "snapshot_missing"}:
            return {"status": "not_found"}
        if loaded_status != "ready":
            return {"status": "security_failed"}
        captured_at = int(loaded.get("captured_at") or loaded.get("signal_ts") or 0)
        if captured_at <= 0 or captured_at > now + 60:
            return {"status": "security_failed"}
        if now - captured_at > AI_ON_DEMAND_MAX_SIGNAL_AGE_SEC:
            return {"status": "expired"}

        endpoint_hash = _sha256(str(settings.ai_base_url).strip().rstrip("/"))
        prompt_hash = _sha256(
            OPERATOR_PROMPT + "\x1f" + str(settings.ai_operator_prompt or "")
        )
        try:
            reservation = self._signal_store.reserve_ai_interpretation(
                public_ref,
                model=str(settings.ai_model),
                endpoint_hash=endpoint_hash,
                prompt_hash=prompt_hash,
                policy_version=AI_ON_DEMAND_POLICY_VERSION,
                now_ts=now,
                in_flight_ttl_sec=min(300, int(settings.ai_timeout_sec) + 30),
                daily_limit=int(
                    getattr(settings, "ai_on_demand_daily_limit", 20)
                ),
            )
        except (OSError, RuntimeError, TypeError, ValueError):
            return {"status": "security_failed"}

        reservation_status = str(reservation.get("status") or "")
        if reservation_status == "available":
            return self._completed_reply(
                reservation.get("result"),
                cached=True,
                symbol=str(reservation.get("symbol") or loaded.get("symbol") or ""),
                signal_ts=int(
                    reservation.get("signal_ts")
                    or loaded.get("signal_ts")
                    or captured_at
                ),
                fallback_ts=captured_at,
            )
        if reservation_status == "in_flight":
            return {"status": "processing"}
        if reservation_status == "quota_exhausted":
            return {"status": "quota_exhausted"}
        if reservation_status == "cooldown":
            return {"status": self._failure_status(reservation.get("error_code"))}
        if reservation_status in {"signal_unavailable", "snapshot_missing"}:
            return {"status": "not_found"}
        if reservation_status != "reserved":
            return {"status": "security_failed"}

        snapshot = reservation.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return {"status": "security_failed"}
        try:
            interpreter = OpenAiCompatibleLaunchInterpreter(
                api_key=settings.ai_api_key,
                base_url=settings.ai_base_url,
                model=settings.ai_model,
                session=self._session,
                timeout_sec=settings.ai_timeout_sec,
                max_tokens=2048,
                max_retries=0,
                operator_prompt=settings.ai_operator_prompt,
            )
        except (TypeError, ValueError):
            self._cache_failure(
                reservation,
                "invalid_configuration",
                int(self._clock()),
            )
            return {"status": "not_configured"}

        result = interpreter.interpret(
            _prebuilt_snapshot_source(snapshot),
            enabled=True,
        )
        if result.get("status") == "available":
            finished_at = int(self._clock())
            stored = self._signal_store.cache_ai_success(
                str(reservation.get("cache_key") or ""),
                str(reservation.get("lease_id") or ""),
                result,
                now_ts=finished_at,
            )
            if stored.get("status") != "available" or stored.get("stored") is not True:
                return {"status": "security_failed"}
            return self._completed_reply(
                result,
                cached=False,
                symbol=str(reservation.get("symbol") or loaded.get("symbol") or ""),
                signal_ts=int(
                    reservation.get("signal_ts")
                    or loaded.get("signal_ts")
                    or captured_at
                ),
                fallback_ts=captured_at,
            )

        error_code = str(result.get("status") or "ai_request_failed")
        self._cache_failure(reservation, error_code, int(self._clock()))
        return {"status": self._failure_status(error_code)}

    def _cache_failure(
        self,
        reservation: Mapping[str, Any],
        error_code: str,
        now: int,
    ) -> None:
        cooldown = 60 if error_code == "ai_rate_limited" else 30
        self._signal_store.cache_ai_failure(
            str(reservation.get("cache_key") or ""),
            str(reservation.get("lease_id") or ""),
            error_code,
            cooldown_sec=cooldown,
            now_ts=now,
        )

    def _completed_reply(
        self,
        result: object,
        *,
        cached: bool,
        symbol: str,
        signal_ts: int,
        fallback_ts: int,
    ) -> dict[str, Any]:
        if not isinstance(result, Mapping) or result.get("status") != "available":
            return {"status": "security_failed"}
        text = format_on_demand_ai_result(
            symbol=str(symbol or "启动信号"),
            signal_ts=int(signal_ts or fallback_ts),
            result=result,
        )
        return {"status": "cached" if cached else "completed", "text": text}

    @staticmethod
    def _failure_status(error_code: object) -> str:
        code = str(error_code or "").strip().lower()
        if code == "ai_rate_limited":
            return "rate_limited"
        if code == "ai_timeout":
            return "timeout"
        if code in {"ai_auth_failed", "invalid_configuration"}:
            return "not_configured"
        return "security_failed"


__all__ = [
    "AI_ON_DEMAND_MAX_SIGNAL_AGE_SEC",
    "AI_ON_DEMAND_POLICY_VERSION",
    "LaunchAiOnDemandService",
    "build_launch_ai_deep_link",
    "format_on_demand_ai_result",
    "positive_telegram_user_id",
    "telegram_bot_username_configured",
]
