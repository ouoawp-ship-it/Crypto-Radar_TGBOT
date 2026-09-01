from __future__ import annotations

import copy
import math
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from html import escape
from typing import Any, Iterable, Mapping


STATE_SCHEMA_VERSION = 1
# Bump this when candidate-eligibility semantics tighten so frozen state can
# migrate once without changing the bounded audit-state schema.
CANDIDATE_GATE_VERSION = "strict_crypto_contract_v1"
TELEGRAM_TEXT_LIMIT = 4096
DEFAULT_DIGEST_TEXT_LIMIT = 3800
SNAPSHOT_ARCHIVE_LIMIT = 7
DELIVERY_RETRY_BASE_SEC = 5 * 60
DELIVERY_RETRY_MAX_SEC = 6 * 3600
CST = timezone(timedelta(hours=8))

_QUALITY_ORDER = {"strong": 0, "standard": 1, "observe": 2}
_QUALITY_LABELS = {"strong": "强", "standard": "标准", "observe": "观察"}
_LIFECYCLE_ORDER = {
    "new": 0,
    "continuing": 1,
    "boundary_changed": 2,
    "breakout_watch": 3,
    "invalidated": 4,
}
_HORIZON_ORDER = {"long": 0, "medium": 1, "short": 2}


def empty_daily_digest_state() -> dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA_VERSION,
        "active": None,
        "pending_digests": [],
        "recent_snapshots": [],
        "last_finalized_close_time": 0,
        "last_delivered_close_time": 0,
        "last_delivery": {},
    }


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) else default


def _normalize_symbol(value: Any) -> str:
    return str(value or "").strip().upper()[:40]


def _normalize_symbols(values: Iterable[Any]) -> list[str]:
    normalized = {_normalize_symbol(value) for value in values}
    normalized.discard("")
    return sorted(normalized)


def _normalize_quality(value: Any) -> str:
    quality = str(value or "").strip().lower()
    if quality == "normal":
        quality = "standard"
    return quality if quality in _QUALITY_ORDER else "observe"


def _normalize_reasons(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    reasons: list[str] = []
    for item in value:
        reason = str(item or "").strip()
        if reason and reason not in reasons:
            reasons.append(reason[:80])
    return reasons[:8]


def _optional_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _stable_box_id(structure: Mapping[str, Any], symbol: str) -> str:
    supplied = str(structure.get("box_id") or "").strip()
    if supplied:
        return supplied[:160]
    source = "|".join([
        symbol,
        str(structure.get("horizon") or ""),
        str(_as_int(structure.get("formed_close_time"))),
        f"{_as_float(structure.get('box_lower')):.12g}",
        f"{_as_float(structure.get('box_upper')):.12g}",
    ])
    return "box_" + sha256(source.encode("utf-8", errors="ignore")).hexdigest()[:20]


def _normalize_structure(
    raw: Mapping[str, Any],
    *,
    symbol: str,
) -> dict[str, Any] | None:
    lower = _as_float(raw.get("box_lower", raw.get("lower")))
    upper = _as_float(raw.get("box_upper", raw.get("upper")))
    if lower <= 0 or upper <= lower:
        return None
    quality = _normalize_quality(
        raw.get("structure_quality", raw.get("quality"))
    )
    lifecycle = str(raw.get("lifecycle_state") or "continuing").strip().lower()
    if lifecycle not in _LIFECYCLE_ORDER:
        lifecycle = "continuing"
    horizon = str(raw.get("horizon") or "").strip().lower()[:20]
    normalized = {
        "box_id": "",
        "symbol": symbol,
        "structure_timeframe": "1d",
        "trigger_timeframe": "1d",
        "trigger_kind": "daily_close_digest",
        "horizon": horizon,
        "horizon_label": str(raw.get("horizon_label") or horizon).strip()[:24],
        "base_bars": max(0, _as_int(raw.get("base_bars"))),
        "box_age": max(0, _as_int(raw.get("box_age"))),
        "formed_close_time": max(0, _as_int(raw.get("formed_close_time"))),
        "box_upper": upper,
        "box_lower": lower,
        "width_pct": max(
            0.0,
            _as_float(raw.get("width_pct", raw.get("box_width_pct"))),
        ),
        "width_atr": max(
            0.0,
            _as_float(raw.get("width_atr", raw.get("box_width_atr"))),
        ),
        "upper_touches": max(0, _as_int(raw.get("upper_touches"))),
        "lower_touches": max(0, _as_int(raw.get("lower_touches"))),
        "efficiency": max(
            0.0,
            _as_float(raw.get("efficiency", raw.get("box_efficiency"))),
        ),
        "current_close": max(
            0.0,
            _as_float(raw.get("current_close", raw.get("close"))),
        ),
        "distance_upper_atr": _optional_float(raw.get("distance_upper_atr")),
        "distance_lower_atr": _optional_float(raw.get("distance_lower_atr")),
        "structure_quality": quality,
        "structure_quality_label": _QUALITY_LABELS[quality],
        "quality_reasons": _normalize_reasons(raw.get("quality_reasons")),
        "lifecycle_state": lifecycle,
    }
    normalized["box_id"] = _stable_box_id(raw, symbol)
    return normalized


def _normalize_observation(
    raw: Mapping[str, Any],
    *,
    target_close_time: int,
) -> dict[str, Any] | None:
    symbol = _normalize_symbol(raw.get("symbol"))
    observed_close_time = _as_int(
        raw.get("target_close_time", raw.get("close_time"))
    )
    if not symbol or observed_close_time != target_close_time:
        return None
    status = str(raw.get("status") or "success").strip().lower()
    if status not in {"success", "request_error", "empty", "stale"}:
        status = "request_error"
    structures: list[dict[str, Any]] = []
    if status == "success":
        raw_structures = raw.get("structures")
        if isinstance(raw_structures, (list, tuple)):
            for item in raw_structures:
                if not isinstance(item, Mapping):
                    continue
                normalized = _normalize_structure(item, symbol=symbol)
                if normalized is not None:
                    structures.append(normalized)
    structures.sort(key=_structure_sort_key)
    return {
        "symbol": symbol,
        "target_close_time": target_close_time,
        "status": status,
        "structures": structures,
        "gate_failures": _normalize_reasons(raw.get("gate_failures")),
    }


def _distance_for_sort(structure: Mapping[str, Any]) -> float:
    values = (
        abs(_as_float(structure.get("distance_upper_atr"), math.inf)),
        abs(_as_float(structure.get("distance_lower_atr"), math.inf)),
    )
    finite = [value for value in values if math.isfinite(value)]
    return min(finite) if finite else math.inf


def _structure_sort_key(structure: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        _QUALITY_ORDER.get(str(structure.get("structure_quality") or ""), 99),
        _LIFECYCLE_ORDER.get(str(structure.get("lifecycle_state") or ""), 99),
        _distance_for_sort(structure),
        -_as_int(structure.get("box_age")),
        str(structure.get("symbol") or ""),
        _HORIZON_ORDER.get(str(structure.get("horizon") or ""), 99),
        str(structure.get("box_id") or ""),
    )


def _price(value: Any) -> str:
    number = _as_float(value)
    magnitude = abs(number)
    if magnitude >= 1_000:
        return f"{number:,.2f}"
    if magnitude >= 1:
        return f"{number:.4f}".rstrip("0").rstrip(".")
    return f"{number:.8f}".rstrip("0").rstrip(".")


def _safe_short(value: Any, limit: int) -> str:
    return escape(str(value or "").strip()[:limit], quote=False)


def _bounded_text_limit(value: Any) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError, OverflowError):
        limit = DEFAULT_DIGEST_TEXT_LIMIT
    return max(1, min(TELEGRAM_TEXT_LIMIT, limit))


def _format_daily_digest(
    payload: Mapping[str, Any],
    *,
    max_items: int,
    text_limit: int,
) -> tuple[str, list[str]]:
    text_limit = _bounded_text_limit(text_limit)
    target_close_time = _as_int(payload.get("target_close_time"))
    generated_at = _as_int(payload.get("generated_at"))
    coverage = payload.get("coverage")
    coverage = coverage if isinstance(coverage, Mapping) else {}
    structures = [
        dict(item)
        for item in payload.get("structures", [])
        if isinstance(item, Mapping)
    ]
    structures.sort(key=_structure_sort_key)
    quality_counts = {
        quality: sum(
            str(item.get("structure_quality") or "") == quality
            for item in structures
        )
        for quality in _QUALITY_ORDER
    }
    target_text = datetime.fromtimestamp(
        target_close_time / 1000,
        CST,
    ).strftime("%m-%d %H:%M CST")
    generated_text = datetime.fromtimestamp(generated_at, CST).strftime(
        "%m-%d %H:%M CST"
    )
    status_text = (
        "覆盖不完整"
        if bool(payload.get("degraded"))
        else "全市场覆盖完成"
    )
    lines = [
        "🗺 <b>盘整突破雷达 · 1D盘整地图</b>",
        "结构周期 1D｜触发周期 1D｜触发方式 日K收盘汇总",
        f"对应日K｜{target_text}｜生成 {generated_text}",
        "",
        (
            f"市场覆盖｜已扫描 {_as_int(coverage.get('attempted'))} ｜ "
            f"应扫描 {_as_int(coverage.get('expected'))}（{status_text}）"
        ),
        (
            f"数据结果｜成功 {_as_int(coverage.get('successful'))} ｜ "
            f"失败 {_as_int(coverage.get('failed'))}"
        ),
        f"有效结构｜{len(structures)}个",
        (
            f"结构质量｜强 {quality_counts['strong']} ｜ "
            f"标准 {quality_counts['standard']} ｜ 观察 {quality_counts['observe']}"
        ),
    ]
    if bool(payload.get("degraded")):
        lines.append(
            "降级原因｜" + _safe_short(payload.get("finalize_reason"), 80)
        )

    max_items = max(0, int(max_items))
    selected = structures[:max_items]
    shown = 0
    displayed_box_ids: list[str] = []

    def footer() -> str:
        return (
            f"展示｜当前 {shown} ｜ 完整 {len(structures)}；"
            "完整结构保留在最近7份有界日报快照。"
        )

    if structures:
        section_started = False
        for structure in selected:
            symbol = _safe_short(structure.get("symbol"), 40)
            horizon = _safe_short(
                structure.get("horizon_label") or structure.get("horizon"),
                24,
            )
            quality = _safe_short(
                structure.get("structure_quality_label"),
                8,
            )
            lifecycle = _safe_short(structure.get("lifecycle_state"), 24)
            age = max(0, _as_int(structure.get("box_age")))
            reasons = structure.get("quality_reasons")
            reasons = reasons if isinstance(reasons, list) else []
            reason_text = "、".join(
                _safe_short(reason, 30) for reason in reasons[:2]
            ) or "硬门槛通过"
            block = [
                f"{shown + 1}. <b>{symbol}</b>｜{horizon} {age}根｜{quality}｜{lifecycle}",
                (
                    f"   箱体 {_price(structure.get('box_lower'))} – "
                    f"{_price(structure.get('box_upper'))}｜依据 {reason_text}"
                ),
            ]
            candidate_lines = [*lines]
            if not section_started:
                candidate_lines.extend(["", "<b>重点结构</b>"])
            candidate_lines.extend(block)
            candidate_shown = shown + 1
            candidate_footer = (
                f"展示｜当前 {candidate_shown} ｜ 完整 {len(structures)}；"
                "完整结构保留在最近7份有界日报快照。"
            )
            candidate_text = "\n".join([
                *candidate_lines,
                "",
                candidate_footer,
            ])
            if len(candidate_text) > text_limit:
                break
            if not section_started:
                lines.extend(["", "<b>重点结构</b>"])
                section_started = True
            lines.extend(block)
            shown += 1
            box_id = str(structure.get("box_id") or "").strip()
            if box_id:
                displayed_box_ids.append(box_id)
        if not selected:
            lines.extend([
                "",
                "重点结构展示已关闭，完整结构仍保留在最近7份有界日报快照。",
            ])
        elif shown == 0:
            lines.extend(["", "重点结构因单条消息长度限制未展开。"])
    else:
        lines.extend(["", "本轮没有达到硬门槛的1D盘整结构。"])

    text = "\n".join([*lines, "", footer()])
    if len(text) > text_limit and shown == 0 and structures and selected:
        lines = lines[:-2]
        text = "\n".join([*lines, "", footer()])
    if len(text) > text_limit:
        raise ValueError("daily digest text exceeds configured single-message limit")
    return text, displayed_box_ids


def format_daily_digest(
    payload: Mapping[str, Any],
    *,
    max_items: int = 20,
    text_limit: int = DEFAULT_DIGEST_TEXT_LIMIT,
) -> str:
    text, _displayed_box_ids = _format_daily_digest(
        payload,
        max_items=max_items,
        text_limit=text_limit,
    )
    return text


def select_digest_signal_structures(
    payload: Mapping[str, Any],
    *,
    max_items: int = 20,
) -> list[dict[str, Any]]:
    """Return bounded, best-per-symbol structures already eligible for display."""

    limit = max(0, int(max_items))
    if limit <= 0:
        return []
    structures = [
        copy.deepcopy(dict(item))
        for item in payload.get("structures", [])
        if isinstance(item, Mapping)
    ]
    structures.sort(key=_structure_sort_key)
    raw_displayed = payload.get("displayed_box_ids")
    displayed_ids = (
        {
            str(item or "").strip()
            for item in raw_displayed
            if str(item or "").strip()
        }
        if isinstance(raw_displayed, list)
        else set()
    )
    candidates = (
        [
            item
            for item in structures
            if str(item.get("box_id") or "") in displayed_ids
        ]
        if displayed_ids
        else structures[:limit]
    )
    selected: list[dict[str, Any]] = []
    seen_symbols: set[str] = set()
    for item in candidates:
        symbol = _normalize_symbol(item.get("symbol"))
        if not symbol or symbol in seen_symbols:
            continue
        seen_symbols.add(symbol)
        selected.append(item)
        if len(selected) >= limit:
            break
    return selected


def _snapshot_sort_key(item: Mapping[str, Any]) -> tuple[int, str]:
    return (
        _as_int(item.get("target_close_time")),
        str(item.get("digest_id") or ""),
    )


def _append_recent_snapshot(
    state: dict[str, Any],
    payload: Mapping[str, Any],
    *,
    status: str,
    reason: str,
    archived_at: int,
    limit: int,
) -> None:
    snapshot = copy.deepcopy(dict(payload))
    snapshot["archive"] = {
        "status": str(status or "archived"),
        "reason": str(reason or ""),
        "archived_at": max(0, int(archived_at)),
    }
    digest_id = str(snapshot.get("digest_id") or "")
    existing = state.get("recent_snapshots")
    snapshots = [
        copy.deepcopy(dict(item))
        for item in (existing if isinstance(existing, list) else [])
        if isinstance(item, Mapping)
        and str(item.get("digest_id") or "") != digest_id
    ]
    snapshots.append(snapshot)
    snapshots.sort(key=_snapshot_sort_key)
    state["recent_snapshots"] = snapshots[-max(1, int(limit)):]


class ConsolidationDailyDigestAccumulator:
    """Pure state machine for one-close-time-at-a-time 1D market digests."""

    def __init__(
        self,
        state: Mapping[str, Any] | None = None,
        *,
        max_items: int = 20,
        max_retry_rounds: int = 2,
        max_wait_sec: int = 3 * 3600,
        text_limit: int = DEFAULT_DIGEST_TEXT_LIMIT,
        snapshot_archive_limit: int = SNAPSHOT_ARCHIVE_LIMIT,
        delivery_retry_base_sec: int = DELIVERY_RETRY_BASE_SEC,
        delivery_retry_max_sec: int = DELIVERY_RETRY_MAX_SEC,
        migration_now_ts: int = 0,
    ) -> None:
        self.max_items = max(0, int(max_items))
        self.max_retry_rounds = max(0, int(max_retry_rounds))
        self.max_wait_sec = max(1, int(max_wait_sec))
        self.text_limit = _bounded_text_limit(text_limit)
        self.snapshot_archive_limit = max(1, int(snapshot_archive_limit))
        self.delivery_retry_base_sec = max(
            1,
            int(delivery_retry_base_sec),
        )
        self.delivery_retry_max_sec = max(
            self.delivery_retry_base_sec,
            int(delivery_retry_max_sec),
        )
        self._state = self._normalize_state(
            state,
            snapshot_archive_limit=self.snapshot_archive_limit,
            migration_now_ts=max(0, int(migration_now_ts)),
        )

    @staticmethod
    def _normalize_state(
        state: Mapping[str, Any] | None,
        *,
        snapshot_archive_limit: int,
        migration_now_ts: int,
    ) -> dict[str, Any]:
        if not isinstance(state, Mapping) or _as_int(
            state.get("schema_version")
        ) != STATE_SCHEMA_VERSION:
            return empty_daily_digest_state()
        normalized = empty_daily_digest_state()
        active = state.get("active")
        if isinstance(active, Mapping):
            normalized["active"] = copy.deepcopy(dict(active))
        snapshots = state.get("recent_snapshots")
        if isinstance(snapshots, list):
            normalized["recent_snapshots"] = [
                copy.deepcopy(dict(item))
                for item in snapshots
                if isinstance(item, Mapping)
            ]
            normalized["recent_snapshots"].sort(key=_snapshot_sort_key)
            normalized["recent_snapshots"] = normalized[
                "recent_snapshots"
            ][-snapshot_archive_limit:]
        pending = state.get("pending_digests")
        raw_pending_items = (
            [
                copy.deepcopy(dict(item))
                for item in pending
                if isinstance(item, Mapping)
            ]
            if isinstance(pending, list)
            else []
        )
        pending_items: list[dict[str, Any]] = []
        for item in raw_pending_items:
            if str(item.get("candidate_gate_version") or "") == (
                CANDIDATE_GATE_VERSION
            ):
                pending_items.append(item)
                continue
            _append_recent_snapshot(
                normalized,
                item,
                status="invalidated",
                reason="candidate_universe_tightened",
                archived_at=migration_now_ts,
                limit=snapshot_archive_limit,
            )
        pending_items.sort(key=_snapshot_sort_key)
        for older in pending_items[:-1]:
            _append_recent_snapshot(
                normalized,
                older,
                status="superseded",
                reason="newer_pending_digest",
                archived_at=_as_int(
                    older.get("delivery", {}).get("attempted_at")
                    if isinstance(older.get("delivery"), Mapping)
                    else 0
                ),
                limit=snapshot_archive_limit,
            )
        if pending_items:
            normalized["pending_digests"] = [pending_items[-1]]
        normalized["last_finalized_close_time"] = max(
            0,
            _as_int(state.get("last_finalized_close_time")),
        )
        normalized["last_delivered_close_time"] = max(
            0,
            _as_int(state.get("last_delivered_close_time")),
        )
        last_delivery = state.get("last_delivery")
        normalized["last_delivery"] = (
            copy.deepcopy(dict(last_delivery))
            if isinstance(last_delivery, Mapping)
            else {}
        )
        return normalized

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._state)

    def reconcile_symbols(
        self,
        current_eligible: Iterable[Any],
        *,
        now_ts: int,
    ) -> dict[str, Any]:
        """Fail closed when a persisted active batch predates the candidate gate."""

        eligible = set(_normalize_symbols(current_eligible))
        result: dict[str, Any] = {
            "candidate_gate_version": CANDIDATE_GATE_VERSION,
            "active_current": False,
            "active_reconciled": False,
            "active_reset": False,
            "removed_symbols": 0,
            "remaining_symbols": 0,
            "reconciled_at": max(0, int(now_ts)),
        }
        active = self._state.get("active")
        if not isinstance(active, Mapping):
            return result

        active_dict = active if isinstance(active, dict) else dict(active)
        previous_expected = set(_normalize_symbols(
            active_dict.get("expected_symbols", [])
        ))
        if str(active_dict.get("candidate_gate_version") or "") == (
            CANDIDATE_GATE_VERSION
        ):
            result["active_current"] = True
            result["remaining_symbols"] = len(previous_expected)
            return result

        retained = previous_expected & eligible
        result.update({
            "active_reconciled": True,
            "removed_symbols": len(previous_expected - retained),
            "remaining_symbols": len(retained),
        })
        if not retained:
            self._state["active"] = None
            result["active_reset"] = True
            return result

        raw_observations = active_dict.get("observations")
        observations = (
            raw_observations
            if isinstance(raw_observations, Mapping)
            else {}
        )
        active_dict["expected_symbols"] = sorted(retained)
        active_dict["observations"] = {
            symbol: copy.deepcopy(dict(observation))
            for raw_symbol, observation in observations.items()
            if (symbol := _normalize_symbol(raw_symbol)) in retained
            and isinstance(observation, Mapping)
        }
        active_dict["candidate_gate_version"] = CANDIDATE_GATE_VERSION
        self._state["active"] = active_dict
        return result

    def pending_digest(self, now_ts: int | None = None) -> dict[str, Any] | None:
        pending = self._state["pending_digests"]
        if not pending:
            return None
        pending.sort(
            key=lambda item: (
                _as_int(item.get("target_close_time")),
                str(item.get("digest_id") or ""),
            )
        )
        selected = pending[-1]
        if now_ts is not None:
            delivery = selected.get("delivery")
            next_attempt_at = _as_int(
                delivery.get("next_attempt_at")
                if isinstance(delivery, Mapping)
                else 0
            )
            if next_attempt_at > int(now_ts):
                return None
        return copy.deepcopy(selected)

    def _archive_pending_before(
        self,
        target_close_time: int,
        *,
        now_ts: int,
    ) -> None:
        retained: list[dict[str, Any]] = []
        for item in self._state["pending_digests"]:
            if _as_int(item.get("target_close_time")) >= target_close_time:
                retained.append(item)
                continue
            _append_recent_snapshot(
                self._state,
                item,
                status="superseded",
                reason="newer_daily_target",
                archived_at=now_ts,
                limit=self.snapshot_archive_limit,
            )
        retained.sort(key=_snapshot_sort_key)
        self._state["pending_digests"] = retained[-1:]

    def _set_latest_pending(
        self,
        payload: Mapping[str, Any],
        *,
        now_ts: int,
    ) -> None:
        digest_id = str(payload.get("digest_id") or "")
        for item in self._state["pending_digests"]:
            if str(item.get("digest_id") or "") == digest_id:
                return
            _append_recent_snapshot(
                self._state,
                item,
                status="superseded",
                reason="newer_pending_digest",
                archived_at=now_ts,
                limit=self.snapshot_archive_limit,
            )
        self._state["pending_digests"] = [copy.deepcopy(dict(payload))]

    def ingest_batch(
        self,
        *,
        target_close_time: int,
        expected_symbols: Iterable[Any],
        observations: Iterable[Mapping[str, Any]],
        now_ts: int,
        round_completed: bool = False,
        round_token: str = "",
    ) -> dict[str, Any] | None:
        target_close_time = int(target_close_time)
        now_ts = int(now_ts)
        if target_close_time <= 0:
            raise ValueError("target_close_time must be positive")
        expected = _normalize_symbols(expected_symbols)
        if not expected:
            raise ValueError("expected_symbols must not be empty")
        if round_completed and not str(round_token or "").strip():
            raise ValueError("round_token is required for a completed round")

        # Runtime persists this reconciliation before ingestion. Keeping the
        # state-machine guard here also makes direct callers fail closed.
        self.reconcile_symbols(expected, now_ts=now_ts)

        active = self._state.get("active")
        if isinstance(active, Mapping):
            active_target = _as_int(active.get("target_close_time"))
            if target_close_time < active_target:
                return self.pending_digest(now_ts=now_ts)
            self._archive_pending_before(
                target_close_time,
                now_ts=now_ts,
            )
            if target_close_time > active_target:
                self._freeze_active(
                    now_ts=now_ts,
                    degraded=True,
                    reason="superseded_by_new_target",
                )
                self._archive_pending_before(
                    target_close_time,
                    now_ts=now_ts,
                )
                active = None
        else:
            self._archive_pending_before(
                target_close_time,
                now_ts=now_ts,
            )

        if not isinstance(active, Mapping):
            if target_close_time <= _as_int(
                self._state.get("last_finalized_close_time")
            ):
                return self.pending_digest(now_ts=now_ts)
            active = {
                "target_close_time": target_close_time,
                "expected_symbols": expected,
                "candidate_gate_version": CANDIDATE_GATE_VERSION,
                "started_at": now_ts,
                "observations": {},
                "completed_round_tokens": [],
                "failed_rounds": 0,
            }
            self._state["active"] = active

        frozen_expected = set(_normalize_symbols(active.get("expected_symbols", [])))
        current_observations = active.get("observations")
        if not isinstance(current_observations, dict):
            current_observations = {}
            active["observations"] = current_observations
        for raw in observations:
            if not isinstance(raw, Mapping):
                continue
            observation = _normalize_observation(
                raw,
                target_close_time=target_close_time,
            )
            if observation is None:
                continue
            symbol = str(observation["symbol"])
            if symbol not in frozen_expected:
                continue
            previous = current_observations.get(symbol)
            if (
                isinstance(previous, Mapping)
                and previous.get("status") == "success"
                and observation.get("status") != "success"
            ):
                continue
            current_observations[symbol] = observation

        if round_completed:
            token = str(round_token).strip()
            completed_tokens = active.get("completed_round_tokens")
            if not isinstance(completed_tokens, list):
                completed_tokens = []
                active["completed_round_tokens"] = completed_tokens
            if token not in completed_tokens:
                completed_tokens.append(token)
                coverage = self._coverage(active)
                if (
                    coverage["attempted"] == coverage["expected"]
                    and coverage["failed"] > 0
                ):
                    active["failed_rounds"] = max(
                        0,
                        _as_int(active.get("failed_rounds")),
                    ) + 1

        coverage = self._coverage(active)
        if coverage["successful"] == coverage["expected"]:
            self._freeze_active(
                now_ts=now_ts,
                degraded=False,
                reason="full_market_coverage",
            )
        elif now_ts - _as_int(active.get("started_at"), now_ts) >= self.max_wait_sec:
            self._freeze_active(
                now_ts=now_ts,
                degraded=True,
                reason="coverage_timeout",
            )
        elif _as_int(active.get("failed_rounds")) > self.max_retry_rounds:
            self._freeze_active(
                now_ts=now_ts,
                degraded=True,
                reason="retry_rounds_exhausted",
            )
        return self.pending_digest(now_ts=now_ts)

    @staticmethod
    def _coverage(active: Mapping[str, Any]) -> dict[str, int]:
        expected = set(_normalize_symbols(active.get("expected_symbols", [])))
        raw_observations = active.get("observations")
        observations = (
            raw_observations if isinstance(raw_observations, Mapping) else {}
        )
        attempted = expected & set(observations)
        successful = {
            symbol
            for symbol in attempted
            if isinstance(observations.get(symbol), Mapping)
            and observations[symbol].get("status") == "success"
        }
        return {
            "expected": len(expected),
            "attempted": len(attempted),
            "successful": len(successful),
            "failed": len(attempted - successful),
            "missing": len(expected - attempted),
        }

    def _freeze_active(
        self,
        *,
        now_ts: int,
        degraded: bool,
        reason: str,
    ) -> None:
        active = self._state.get("active")
        if not isinstance(active, Mapping):
            return
        target_close_time = _as_int(active.get("target_close_time"))
        observations = active.get("observations")
        observations = observations if isinstance(observations, Mapping) else {}
        structures: list[dict[str, Any]] = []
        for symbol in sorted(observations):
            observation = observations[symbol]
            if not isinstance(observation, Mapping):
                continue
            for structure in observation.get("structures", []):
                if isinstance(structure, Mapping):
                    structures.append(copy.deepcopy(dict(structure)))
        structures.sort(key=_structure_sort_key)
        digest_id = f"range_daily_digest.v1:1d:{target_close_time}"
        coverage = self._coverage(active)
        payload: dict[str, Any] = {
            "schema": "range_daily_digest.v1",
            "digest_id": digest_id,
            "dedup_key": digest_id,
            "target_close_time": target_close_time,
            "generated_at": int(now_ts),
            "degraded": bool(degraded),
            "finalize_reason": str(reason),
            "coverage": coverage,
            "structures": structures,
            "structure_timeframe": "1d",
            "trigger_timeframe": "1d",
            "trigger_kind": "daily_close_digest",
            "candidate_gate_version": CANDIDATE_GATE_VERSION,
            "delivery": {
                "attempt_count": 0,
                "next_attempt_at": 0,
            },
        }
        payload["text"], payload["displayed_box_ids"] = _format_daily_digest(
            payload,
            max_items=self.max_items,
            text_limit=self.text_limit,
        )
        self._set_latest_pending(payload, now_ts=now_ts)
        self._state["last_finalized_close_time"] = max(
            _as_int(self._state.get("last_finalized_close_time")),
            target_close_time,
        )
        self._state["active"] = None

    def mark_delivery(
        self,
        digest_id: str,
        *,
        status: str,
        reason: str = "",
        now_ts: int = 0,
    ) -> bool:
        digest_id = str(digest_id or "").strip()
        status = str(status or "").strip().lower()
        reason = str(reason or "").strip()
        accepted = status == "sent" or (
            status == "skipped" and reason == "dedup_cooldown"
        )
        pending = self._state["pending_digests"]
        for index, item in enumerate(pending):
            if str(item.get("digest_id") or "") != digest_id:
                continue
            previous_delivery = item.get("delivery")
            previous_attempts = _as_int(
                previous_delivery.get("attempt_count")
                if isinstance(previous_delivery, Mapping)
                else 0
            )
            attempt_count = max(0, previous_attempts) + 1
            delivery = {
                "status": status,
                "reason": reason,
                "attempted_at": max(0, int(now_ts)),
                "attempt_count": attempt_count,
                "next_attempt_at": 0,
            }
            self._state["last_delivery"] = copy.deepcopy(delivery)
            if accepted:
                item["delivery"] = copy.deepcopy(delivery)
                delivered = pending.pop(index)
                _append_recent_snapshot(
                    self._state,
                    delivered,
                    status=(
                        "delivered"
                        if status == "sent"
                        else "already_delivered"
                    ),
                    reason=reason,
                    archived_at=max(0, int(now_ts)),
                    limit=self.snapshot_archive_limit,
                )
                self._state["last_delivered_close_time"] = max(
                    _as_int(self._state.get("last_delivered_close_time")),
                    _as_int(delivered.get("target_close_time")),
                )
            else:
                multiplier = 2 ** min(10, max(0, attempt_count - 1))
                retry_delay = min(
                    self.delivery_retry_max_sec,
                    self.delivery_retry_base_sec * multiplier,
                )
                delivery["next_attempt_at"] = (
                    max(0, int(now_ts)) + retry_delay
                )
                item["delivery"] = delivery
                self._state["last_delivery"] = copy.deepcopy(delivery)
            return accepted
        return False


__all__ = [
    "CANDIDATE_GATE_VERSION",
    "ConsolidationDailyDigestAccumulator",
    "DEFAULT_DIGEST_TEXT_LIMIT",
    "DELIVERY_RETRY_BASE_SEC",
    "DELIVERY_RETRY_MAX_SEC",
    "SNAPSHOT_ARCHIVE_LIMIT",
    "STATE_SCHEMA_VERSION",
    "TELEGRAM_TEXT_LIMIT",
    "empty_daily_digest_state",
    "format_daily_digest",
    "select_digest_signal_structures",
]
