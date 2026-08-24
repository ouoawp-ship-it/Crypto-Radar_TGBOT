from __future__ import annotations

from pathlib import Path
import time
from typing import Any, Callable, Mapping

from shared.atomic_json import locked_update_json


Reader = Callable[[], Mapping[str, Any]]
Sender = Callable[[str], bool]

_STATE_SCHEMA_VERSION = 1
_RUNTIME_EVENT = "runtime:main"
_RADAR_EVENTS = {
    "launch_alert": "radar:launch_alert",
    "radar_summary": "radar:radar_summary",
    "funding_alert": "radar:funding_alert",
    "flow_radar": "radar:flow_radar",
    "announcement_risk": "radar:announcement_risk",
}
_DATA_EVENTS = {
    "market_snapshots_freshness": "data:market_snapshots_freshness",
    "realtime_features_freshness": "data:realtime_features_freshness",
}
_QUOTA_EVENT = "quota:global_hourly"
_EVENT_LABELS = {
    _RUNTIME_EVENT: "主 BOT：未运行或心跳已过期",
    "radar:launch_alert": "脉冲雷达：运行异常或计划周期逾期",
    "radar:radar_summary": "资金摘要：运行异常或计划周期逾期",
    "radar:funding_alert": "资金费率警报：运行异常或计划周期逾期",
    "radar:flow_radar": "五因子资金流：运行异常或计划周期逾期",
    "radar:announcement_risk": "公告风险：运行异常或计划周期逾期",
    "data:market_snapshots_freshness": "市场快照：数据过期或不可用",
    "data:realtime_features_freshness": "实时行情：数据过期或不可用",
    _QUOTA_EVENT: "真实推送额度：最近一小时额度已用完",
}
_EVENT_ORDER = tuple(_EVENT_LABELS)
_RUNTIME_FAILURE_STATES = {"not_initialized", "not_running", "stale"}
_RUNTIME_ACTIVE_STATES = {"running", "degraded", "ok", "healthy"}
_RADAR_FAILURE_STATES = {"degraded", "stale", "failed", "error"}
_DATA_FAILURE_STATES = {"fail", "failed", "stale", "critical"}


def _empty_reader() -> Mapping[str, Any]:
    return {}


def _status(value: Any) -> str:
    return value.strip().lower() if isinstance(value, str) else ""


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _timestamp(value: Any) -> int:
    number = _nonnegative_integer(value)
    return number if number is not None else 0


def _normalized_state(value: Any) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    source_incidents = source.get("incidents")
    source_incidents = (
        source_incidents if isinstance(source_incidents, Mapping) else {}
    )
    incidents: dict[str, dict[str, Any]] = {}
    for key in _EVENT_ORDER:
        item = source_incidents.get(key)
        if not isinstance(item, Mapping):
            continue
        incidents[key] = {
            "active": item.get("active") is True,
            "last_attempt_at": _timestamp(item.get("last_attempt_at")),
            "last_sent_at": _timestamp(item.get("last_sent_at")),
            "next_attempt_at": _timestamp(item.get("next_attempt_at")),
        }
    return {
        "schema_version": _STATE_SCHEMA_VERSION,
        "incidents": incidents,
    }


def _quota_exhausted(
    radar_status: Mapping[str, Any],
    quota: Mapping[str, Any],
) -> bool:
    if radar_status.get("real_send") is not True:
        return False
    if _status(radar_status.get("status")) not in _RUNTIME_ACTIVE_STATES:
        return False
    limit = _nonnegative_integer(
        quota.get("limit", quota.get("hourly_limit"))
    )
    used = _nonnegative_integer(quota.get("used", quota.get("sent")))
    remaining = _nonnegative_integer(quota.get("remaining"))
    if remaining is not None:
        return remaining == 0 and (limit is not None or used is not None)
    return limit is not None and used is not None and used >= limit


def _private_alert_message(event_keys: list[str]) -> str:
    lines = ["🚨 泡泡雷达主动故障提醒", ""]
    lines.extend(f"• {_EVENT_LABELS[key]}" for key in event_keys)
    lines.extend([
        "",
        "请私聊发送“五雷达状态”或“健康摘要”查看当前状态。",
        "同类故障在冷却期间不会重复提醒。",
    ])
    return "\n".join(lines)[:3900]


class PrivateAlertEvaluator:
    """Evaluate local status and dispatch one bounded admin-private alert.

    The three readers must be local-only. ``sender`` owns the transport and must
    return exactly ``True`` only after the private message has been accepted.
    Call ``run_once`` from the private-control process on its own cadence.
    """

    def __init__(
        self,
        *,
        state_path: str | Path,
        sender: Sender,
        enabled: bool = False,
        radar_status_reader: Reader = _empty_reader,
        data_freshness_reader: Reader = _empty_reader,
        delivery_quota_reader: Reader = _empty_reader,
        clock: Callable[[], float] = time.time,
        cooldown_sec: int = 3600,
        failure_backoff_sec: int = 300,
    ) -> None:
        self.enabled = bool(enabled)
        self._state_path = Path(state_path)
        self._sender = sender
        self._radar_status_reader = radar_status_reader
        self._data_freshness_reader = data_freshness_reader
        self._delivery_quota_reader = delivery_quota_reader
        self._clock = clock
        self._cooldown_sec = max(1, int(cooldown_sec))
        self._failure_backoff_sec = max(
            1,
            min(self._cooldown_sec, int(failure_backoff_sec)),
        )

    @staticmethod
    def _result(
        status: str,
        *,
        active: int = 0,
        due: int = 0,
        sent: int = 0,
        sender_calls: int = 0,
        reader_failures: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        return {
            "status": status,
            "active_incidents": max(0, int(active)),
            "due_incidents": max(0, int(due)),
            "sent_incidents": max(0, int(sent)),
            "sender_calls": max(0, int(sender_calls)),
            "reader_failures": reader_failures,
        }

    @staticmethod
    def _read(
        reader: Reader,
        label: str,
        failures: list[str],
    ) -> Mapping[str, Any]:
        try:
            payload = reader()
        except Exception:
            failures.append(label)
            return {}
        if not isinstance(payload, Mapping):
            failures.append(label)
            return {}
        return payload

    def _events(
        self,
    ) -> tuple[set[str], set[str], tuple[str, ...]]:
        failures: list[str] = []
        radar_status = self._read(
            self._radar_status_reader,
            "radar_status",
            failures,
        )
        data_status = self._read(
            self._data_freshness_reader,
            "data_freshness",
            failures,
        )
        quota = self._read(
            self._delivery_quota_reader,
            "delivery_quota",
            failures,
        )

        active: set[str] = set()
        unobserved: set[str] = set()
        if "radar_status" in failures:
            unobserved.add(_RUNTIME_EVENT)
            unobserved.update(_RADAR_EVENTS.values())
        else:
            runtime_state = _status(radar_status.get("status"))
            runtime_failed = runtime_state in _RUNTIME_FAILURE_STATES
            if runtime_failed:
                active.add(_RUNTIME_EVENT)
            else:
                radars = radar_status.get("radars")
                radars = radars if isinstance(radars, Mapping) else {}
                for radar_key, event_key in _RADAR_EVENTS.items():
                    item = radars.get(radar_key)
                    item = item if isinstance(item, Mapping) else {}
                    if _status(item.get("state")) in _RADAR_FAILURE_STATES:
                        active.add(event_key)

        if "data_freshness" in failures:
            unobserved.update(_DATA_EVENTS.values())
        else:
            checks = data_status.get("checks")
            checks = checks if isinstance(checks, list) else []
            for item in checks[:100]:
                if not isinstance(item, Mapping):
                    continue
                name = item.get("name")
                if not isinstance(name, str) or name not in _DATA_EVENTS:
                    continue
                if _status(item.get("status")) in _DATA_FAILURE_STATES:
                    active.add(_DATA_EVENTS[name])

        if "delivery_quota" in failures or "radar_status" in failures:
            unobserved.add(_QUOTA_EVENT)
        elif _quota_exhausted(radar_status, quota):
            active.add(_QUOTA_EVENT)

        return active, unobserved, tuple(failures)

    def _write_state(
        self,
        update: Callable[[Any], dict[str, Any]],
    ) -> bool:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.parent.chmod(0o700)
            locked_update_json(self._state_path, update, {})
            self._state_path.chmod(0o600)
        except Exception:
            return False
        return True

    def _reserve(
        self,
        active: set[str],
        unobserved: set[str],
        now: int,
    ) -> tuple[bool, list[str]]:
        due: list[str] = []

        def update(value: Any) -> dict[str, Any]:
            state = _normalized_state(value)
            incidents = state["incidents"]
            for key in _EVENT_ORDER:
                if key in unobserved:
                    continue
                item = incidents.get(key)
                if item is None:
                    if key not in active:
                        continue
                    item = {
                        "active": False,
                        "last_attempt_at": 0,
                        "last_sent_at": 0,
                        "next_attempt_at": 0,
                    }
                    incidents[key] = item
                item["active"] = key in active
                if key not in active:
                    continue
                if now < _timestamp(item.get("next_attempt_at")):
                    continue
                due.append(key)
                item["last_attempt_at"] = now
                # A successful send followed by a state-write failure must not
                # cause a restart loop to resend the same notice.
                item["next_attempt_at"] = now + self._cooldown_sec
            return state

        return self._write_state(update), due

    def _finish_attempt(
        self,
        event_keys: list[str],
        now: int,
        *,
        sent: bool,
    ) -> bool:
        def update(value: Any) -> dict[str, Any]:
            state = _normalized_state(value)
            incidents = state["incidents"]
            for key in event_keys:
                item = incidents.get(key)
                if item is None:
                    continue
                if sent:
                    item["last_sent_at"] = now
                    item["next_attempt_at"] = now + self._cooldown_sec
                else:
                    item["next_attempt_at"] = (
                        now + self._failure_backoff_sec
                    )
            return state

        return self._write_state(update)

    def run_once(self) -> dict[str, Any]:
        if not self.enabled:
            return self._result("disabled")
        try:
            now = max(0, int(self._clock()))
        except Exception:
            return self._result("clock_failed")

        active, unobserved, reader_failures = self._events()
        state_ready, due = self._reserve(active, unobserved, now)
        if not state_ready:
            return self._result(
                "state_unavailable",
                active=len(active),
                reader_failures=reader_failures,
            )
        if not due:
            return self._result(
                "suppressed" if active else "idle",
                active=len(active),
                reader_failures=reader_failures,
            )

        message = _private_alert_message(due)
        try:
            sent = self._sender(message) is True
        except Exception:
            sent = False
        state_finished = self._finish_attempt(due, now, sent=sent)
        if sent:
            return self._result(
                "sent" if state_finished else "sent_state_unavailable",
                active=len(active),
                due=len(due),
                sent=len(due),
                sender_calls=1,
                reader_failures=reader_failures,
            )
        return self._result(
            "send_failed" if state_finished else "send_failed_state_unavailable",
            active=len(active),
            due=len(due),
            sender_calls=1,
            reader_failures=reader_failures,
        )


__all__ = ["PrivateAlertEvaluator"]
