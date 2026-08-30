from __future__ import annotations

import copy
import hashlib
import math
import time
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable, Mapping

from config import Settings
from radars.consolidation_breakout.daily import DAILY_HORIZONS
from shared.binance_data import BinanceDataSource
from shared.storage import JsonStore


TEMPLATE_ID = "TG_CONSOLIDATION_BREAKOUT"
SCHEMA = "range_proximity.v1"
STATE_SCHEMA_VERSION = 1
CST = timezone(timedelta(hours=8))

MINUTE_MS = 60_000
FIFTEEN_MINUTE_MS = 15 * MINUTE_MS
HOUR_MS = 60 * MINUTE_MS

DISCOVERY_LIMIT_DEFAULT = 20
FAST_MONITOR_LIMIT_DEFAULT = 20
FIFTEEN_MINUTE_HISTORY = 84

TICKER_PREFILTER_ATR = 1.0
FIFTEEN_MINUTE_MAX_DISTANCE_ATR = 0.20
HOURLY_MAX_DISTANCE_ATR = 0.30
MAX_DISTANCE_PCT = 0.35
FIFTEEN_MINUTE_MIN_PROGRESS_ATR = 0.10
FIFTEEN_MINUTE_TIGHT_DISTANCE_ATR = 0.10
HOURLY_MIN_PROGRESS_ATR = 0.05
HOURLY_TIGHT_DISTANCE_ATR = 0.10
REARM_DISTANCE_ATR = 0.60
HIGHER_TIMEFRAME_DISTANCE_ATR = 0.35
HIGHER_TIMEFRAME_DISTANCE_PCT = 0.50
FLOAT_EPSILON = 1e-9

EVENT_NAMES = {
    "upper": "proximity_upper",
    "lower": "proximity_lower",
}
EDGE_LABELS = {
    "upper": "上沿",
    "lower": "下沿",
}
TIMEFRAME_ORDER = {"1w": 0, "1d": 1, "4h": 2}


def _core() -> Any:
    # Imported lazily so the main radar can import this module without a cycle.
    from radars.consolidation_breakout import radar as consolidation_core

    return consolidation_core


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _price(value: float) -> str:
    magnitude = abs(value)
    if magnitude >= 1_000:
        return f"{value:.2f}"
    if magnitude >= 1:
        return f"{value:.4f}"
    if magnitude >= 0.01:
        return f"{value:.5f}"
    return f"{value:.8f}"


def _closed_candles(rows: Any, cutoff_ms: int) -> list[Any]:
    core = _core()
    if not isinstance(rows, (list, tuple)):
        return []
    parsed = [core.Candle.from_binance(row) for row in rows]
    ordered = sorted(
        (
            candle
            for candle in parsed
            if candle is not None and candle.close_time <= cutoff_ms
        ),
        key=lambda candle: candle.close_time,
    )
    deduplicated: list[Any] = []
    for candle in ordered:
        if deduplicated and deduplicated[-1].close_time == candle.close_time:
            deduplicated[-1] = candle
        else:
            deduplicated.append(candle)
    return deduplicated


def _aggregate_closed_hours(candles: list[Any]) -> list[Any]:
    """Aggregate exact groups of four closed 15-minute candles into 1H bars."""

    core = _core()
    groups: dict[int, list[Any]] = {}
    for candle in candles:
        bucket_start = candle.open_time // HOUR_MS * HOUR_MS
        groups.setdefault(bucket_start, []).append(candle)

    result: list[Any] = []
    for bucket_start, raw_group in sorted(groups.items()):
        group = sorted(raw_group, key=lambda candle: candle.open_time)
        expected_opens = [
            bucket_start + offset * FIFTEEN_MINUTE_MS
            for offset in range(4)
        ]
        if len(group) != 4:
            continue
        if [candle.open_time for candle in group] != expected_opens:
            continue
        if group[-1].close_time != bucket_start + HOUR_MS - 1:
            continue
        result.append(core.Candle(
            open_time=bucket_start,
            open=group[0].open,
            high=max(candle.high for candle in group),
            low=min(candle.low for candle in group),
            close=group[-1].close,
            volume=sum(candle.volume for candle in group),
            close_time=bucket_start + HOUR_MS - 1,
        ))
    return result


def _box_id(symbol: str, horizon: str, box: Mapping[str, Any]) -> str:
    raw = "|".join([
        str(symbol).upper(),
        str(horizon),
        str(_to_int(box.get("formed_close_time"))),
        f"{_to_float(box.get('lower')):.12g}",
        f"{_to_float(box.get('upper')):.12g}",
    ])
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"1h:{horizon}:{digest}"


def _empty_edge_state() -> dict[str, Any]:
    return {
        "live_sent": False,
        "shadow_seen": False,
        "rearm_count": 0,
        "last_event_id": "",
        "last_event_close_time": 0,
        "last_trigger_timeframe": "",
        "last_rearm_close_time": 0,
    }


def _new_monitor(source_box_id: str) -> dict[str, Any]:
    return {
        "source_box_id": source_box_id,
        "last_15m_close_time": 0,
        "last_1h_close_time": 0,
        "edges": {
            "upper": _empty_edge_state(),
            "lower": _empty_edge_state(),
        },
    }


def _normalize_monitor(value: Any, source_box_id: str) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("source_box_id") != source_box_id:
        return _new_monitor(source_box_id)
    monitor = copy.deepcopy(value)
    monitor["source_box_id"] = source_box_id
    monitor["last_15m_close_time"] = max(
        0,
        _to_int(monitor.get("last_15m_close_time")),
    )
    monitor["last_1h_close_time"] = max(
        0,
        _to_int(monitor.get("last_1h_close_time")),
    )
    raw_edges = monitor.get("edges")
    edges = raw_edges if isinstance(raw_edges, dict) else {}
    normalized_edges: dict[str, dict[str, Any]] = {}
    for edge in ("upper", "lower"):
        base = _empty_edge_state()
        raw = edges.get(edge)
        if isinstance(raw, dict):
            base.update({
                "live_sent": bool(raw.get("live_sent")),
                "shadow_seen": bool(raw.get("shadow_seen")),
                "rearm_count": max(0, _to_int(raw.get("rearm_count"))),
                "last_event_id": str(raw.get("last_event_id") or ""),
                "last_event_close_time": max(
                    0,
                    _to_int(raw.get("last_event_close_time")),
                ),
                "last_trigger_timeframe": str(
                    raw.get("last_trigger_timeframe") or ""
                ),
                "last_rearm_close_time": max(
                    0,
                    _to_int(raw.get("last_rearm_close_time")),
                ),
            })
        normalized_edges[edge] = base
    monitor["edges"] = normalized_edges
    return monitor


def _edge_distance(candle: Any, edge: str, boundary: float) -> float:
    if edge == "upper":
        return boundary - candle.close
    return candle.close - boundary


def _edge_crossed(candle: Any, edge: str, boundary: float) -> bool:
    if edge == "upper":
        return candle.high > boundary or candle.close > boundary
    return candle.low < boundary or candle.close < boundary


def _rearm_ready(
    hourly_candles: list[Any],
    *,
    edge: str,
    boundary: float,
    atr: float,
) -> bool:
    if len(hourly_candles) < 2 or atr <= 0:
        return False
    latest = hourly_candles[-2:]
    return all(
        not _edge_crossed(candle, edge, boundary)
        and _edge_distance(candle, edge, boundary) / atr >= REARM_DISTANCE_ATR
        for candle in latest
    )


def _fifteen_minute_proximity(
    candles: list[Any],
    *,
    edge: str,
    boundary: float,
    atr: float,
) -> tuple[bool, float, float, str]:
    if len(candles) < 4 or atr <= 0 or boundary <= 0:
        return False, 0.0, 0.0, "insufficient_15m_history"
    recent = candles[-4:]
    if any(_edge_crossed(candle, edge, boundary) for candle in recent):
        return False, 0.0, 0.0, "wick_or_close_crossed"
    distances = [
        _edge_distance(candle, edge, boundary) / atr
        for candle in recent
    ]
    current_atr = distances[-1]
    current_pct = (
        _edge_distance(recent[-1], edge, boundary) / boundary * 100.0
    )
    if current_atr < 0:
        return False, current_atr, current_pct, "outside_box"
    if (
        current_atr > FIFTEEN_MINUTE_MAX_DISTANCE_ATR + FLOAT_EPSILON
        or current_pct > MAX_DISTANCE_PCT + FLOAT_EPSILON
    ):
        return False, current_atr, current_pct, "outside_proximity_zone"

    closer_segments = sum(
        distances[index] < distances[index - 1]
        for index in range(1, len(distances))
    )
    progressive = (
        closer_segments >= 2
        and distances[0] - distances[-1]
        + FLOAT_EPSILON >= FIFTEEN_MINUTE_MIN_PROGRESS_ATR
    )
    tight_hold = (
        distances[-2]
        <= FIFTEEN_MINUTE_TIGHT_DISTANCE_ATR + FLOAT_EPSILON
        and distances[-1]
        <= FIFTEEN_MINUTE_TIGHT_DISTANCE_ATR + FLOAT_EPSILON
    )
    if progressive:
        return True, current_atr, current_pct, "four_bar_progress"
    if tight_hold:
        return True, current_atr, current_pct, "two_bar_tight_hold"
    return False, current_atr, current_pct, "momentum_gate"


def _hourly_proximity(
    candles: list[Any],
    *,
    edge: str,
    boundary: float,
    atr: float,
) -> tuple[bool, float, float, str]:
    if len(candles) < 2 or atr <= 0 or boundary <= 0:
        return False, 0.0, 0.0, "insufficient_1h_history"
    previous, current = candles[-2:]
    if _edge_crossed(current, edge, boundary):
        return False, 0.0, 0.0, "wick_or_close_crossed"
    previous_atr = _edge_distance(previous, edge, boundary) / atr
    current_atr = _edge_distance(current, edge, boundary) / atr
    current_pct = (
        _edge_distance(current, edge, boundary) / boundary * 100.0
    )
    if current_atr < 0:
        return False, current_atr, current_pct, "outside_box"
    if (
        current_atr > HOURLY_MAX_DISTANCE_ATR + FLOAT_EPSILON
        or current_pct > MAX_DISTANCE_PCT + FLOAT_EPSILON
    ):
        return False, current_atr, current_pct, "outside_proximity_zone"
    if (
        previous_atr - current_atr + FLOAT_EPSILON
        >= HOURLY_MIN_PROGRESS_ATR
    ):
        return True, current_atr, current_pct, "hourly_progress"
    if current_atr <= HOURLY_TIGHT_DISTANCE_ATR + FLOAT_EPSILON:
        return True, current_atr, current_pct, "hourly_tight"
    return False, current_atr, current_pct, "momentum_gate"


def _active_box(track: Any) -> Mapping[str, Any] | None:
    if not isinstance(track, dict) or isinstance(track.get("breakout"), dict):
        return None
    box = track.get("box")
    if not isinstance(box, dict):
        return None
    upper = _to_float(box.get("upper"))
    lower = _to_float(box.get("lower"))
    atr = _to_float(box.get("atr"))
    if not (upper > lower > 0 and atr > 0):
        return None
    return box


def _higher_timeframe_confluence(
    *,
    symbol: str,
    edge: str,
    hourly_boundary: float,
    hourly_atr: float,
    legacy_tracks: Mapping[str, Any],
    daily_tracks: Mapping[str, Any],
) -> list[dict[str, Any]]:
    core = _core()
    candidates: dict[str, list[dict[str, Any]]] = {
        "4h": [],
        "1d": [],
        "1w": [],
    }

    for timeframe in ("4h", "1d", "1w"):
        for spec in core.HORIZONS:
            track = legacy_tracks.get(f"{symbol}|{timeframe}|{spec.name}")
            box = _active_box(track)
            if box is None:
                continue
            candidates[timeframe].append({
                "timeframe": timeframe,
                "horizon": spec.name,
                "horizon_label": spec.label,
                "base_bars": max(1, _to_int(box.get("base_bars"), spec.length)),
                "box": box,
            })

    for spec in DAILY_HORIZONS:
        track = daily_tracks.get(f"{symbol}|1d|{spec.name}")
        box = _active_box(track)
        if box is None:
            continue
        candidates["1d"].append({
            "timeframe": "1d",
            "horizon": spec.name,
            "horizon_label": spec.label,
            "base_bars": max(
                1,
                _to_int(box.get("base_bars"), min(spec.anchors)),
            ),
            "box": box,
        })

    selected: list[dict[str, Any]] = []
    for timeframe in ("4h", "1d", "1w"):
        matching: list[dict[str, Any]] = []
        for candidate in candidates[timeframe]:
            box = candidate["box"]
            higher_atr = _to_float(box.get("atr"))
            higher_boundary = _to_float(box.get(edge))
            minimum_atr = min(hourly_atr, higher_atr)
            if higher_boundary <= 0 or minimum_atr <= 0:
                continue
            difference = abs(hourly_boundary - higher_boundary)
            difference_atr = difference / minimum_atr
            difference_pct = difference / hourly_boundary * 100.0
            if (
                difference_atr > HIGHER_TIMEFRAME_DISTANCE_ATR
                or difference_pct > HIGHER_TIMEFRAME_DISTANCE_PCT
            ):
                continue
            matching.append({
                "timeframe": timeframe,
                "horizon": candidate["horizon"],
                "horizon_label": candidate["horizon_label"],
                "base_bars": candidate["base_bars"],
                "edge": edge,
                "edge_price": higher_boundary,
                "edge_difference_atr": difference_atr,
                "edge_difference_pct": difference_pct,
            })
        if matching:
            selected.append(max(
                matching,
                key=lambda item: (
                    _to_int(item.get("base_bars")),
                    -_to_float(item.get("edge_difference_atr")),
                ),
            ))
    return sorted(
        selected,
        key=lambda item: TIMEFRAME_ORDER.get(str(item.get("timeframe")), 99),
    )


def _confluence_summary(items: list[dict[str, Any]]) -> str:
    return "；".join(
        (
            f"{str(item.get('timeframe') or '').upper()}"
            f"{str(item.get('horizon_label') or '')}{EDGE_LABELS[str(item['edge'])]}"
            f" {_price(_to_float(item.get('edge_price')))}"
            f"（差 {_to_float(item.get('edge_difference_atr')):.2f} ATR / "
            f"{_to_float(item.get('edge_difference_pct')):.2f}%）"
        )
        for item in items
    )


def _format_event(event: Mapping[str, Any]) -> str:
    edge = str(event.get("proximity_edge") or "")
    edge_label = EDGE_LABELS.get(edge, "边界")
    trigger = str(event.get("trigger_timeframe") or "").upper()
    close_time = _to_int(event.get("close_time"))
    when = datetime.fromtimestamp(close_time / 1000, CST).strftime(
        "%m-%d %H:%M CST"
    )
    confluence = str(event.get("higher_tf_confluence_summary") or "")
    raw_quality_reasons = event.get("quality_reasons")
    quality_reasons = [
        str(reason)
        for reason in (
            raw_quality_reasons
            if isinstance(raw_quality_reasons, (list, tuple))
            else []
        )
        if str(reason)
    ]
    rule = str(event.get("proximity_rule") or "")
    rule_text = {
        "four_bar_progress": "最近4根15m中至少2段继续靠近，累计推进达到0.10 ATR",
        "two_bar_tight_hold": "连续2根15m收盘保持在0.10 ATR临界区",
        "hourly_progress": "1H收盘较前一根继续靠近至少0.05 ATR",
        "hourly_tight": "1H收盘进入0.10 ATR紧贴区",
    }.get(rule, "闭合K线满足临界接近条件")
    direction_text = "向上试探前置观察" if edge == "upper" else "向下试探前置观察"
    confirmation_text = (
        "只有后续1H收盘越过上沿并达到突破缓冲，才升级为有效向上突破。"
        if edge == "upper"
        else "只有后续1H收盘跌破下沿并达到突破缓冲，才升级为有效向下跌破。"
    )
    lines = [
        "🟡 <b>盘整突破雷达 · 1H箱体临界预警</b>",
        (
            f"<b>{escape(str(event.get('symbol') or ''), quote=False)}</b> ｜ "
            f"结构周期 1H ｜ 触发周期 {escape(trigger, quote=False)}"
        ),
        (
            f"{escape(str(event.get('horizon_label') or ''), quote=False)}箱体｜"
            f"{_to_int(event.get('box_age'))}根"
        ),
        f"⏰ {when}",
        "",
        f"状态｜<b>{edge_label}临近</b>（{direction_text}，尚未突破）",
        f"收盘｜<b>{_price(_to_float(event.get('close')))}</b>",
        (
            f"箱体｜上沿 {_price(_to_float(event.get('box_upper')))} ｜ "
            f"下沿 {_price(_to_float(event.get('box_lower')))}"
        ),
        (
            f"距{edge_label}｜{_to_float(event.get('proximity_distance_atr')):.2f} ATR ｜ "
            f"{_to_float(event.get('proximity_distance_pct')):.2f}%"
        ),
        (
            f"量能｜{_to_float(event.get('volume_ratio')):.2f}x"
            "（仅展示，尚未构成突破确认）"
        ),
        (
            "结构质量｜<b>"
            f"{escape(str(event.get('structure_quality_label') or '标准'), quote=False)}"
            "</b>"
        ),
        (
            "结构依据｜"
            + escape(
                "；".join(quality_reasons)
                if quality_reasons
                else "冻结箱体结构有效",
                quote=False,
            )
        ),
        f"临界依据｜{escape(rule_text, quote=False)}",
        (
            "4H以上同侧边界共振｜"
            + (escape(confluence, quote=False) if confluence else "无（仅1H结构）")
        ),
        "",
        (
            "🧭 观察：这是临界预警，不是突破信号；"
            + confirmation_text
            + " 影线扫过或收盘越界的当前K线不会按临近信号推送。"
        ),
    ]
    return "\n".join(lines)


class ConsolidationHourlyProximityRadar:
    """Discover frozen 1H boxes and monitor their edges with closed 15m bars."""

    def __init__(self, settings: Settings, store: JsonStore | None = None):
        self.settings = settings
        self.store = store or JsonStore(settings.data_dir)

    @property
    def state_path(self) -> Path:
        return Path(getattr(
            self.settings,
            "consolidation_hourly_proximity_state_path",
            self.settings.data_dir / "consolidation_hourly_proximity_state.json",
        ))

    def _empty_result(self, reason: str) -> dict[str, Any]:
        return {
            "template_id": TEMPLATE_ID,
            "events": [],
            "chart_payloads": {},
            "state_updates": [],
            "rotation_update": None,
            "diagnostics": {
                "status": reason,
                "schema": SCHEMA,
                "event_count": 0,
            },
        }

    def _load_state(self) -> dict[str, Any]:
        raw = self.store.load(self.state_path, {})
        if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
            return {
                "schema_version": STATE_SCHEMA_VERSION,
                "tracks": {},
                "rotation": {"after_symbol": "", "round": 1},
            }
        tracks = raw.get("tracks")
        rotation = raw.get("rotation")
        rotation = rotation if isinstance(rotation, dict) else {}
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "tracks": tracks if isinstance(tracks, dict) else {},
            "rotation": {
                "after_symbol": str(
                    rotation.get("after_symbol") or ""
                ).strip().upper(),
                "round": max(1, _to_int(rotation.get("round"), 1)),
            },
        }

    def _universe(self, source: BinanceDataSource) -> list[str]:
        excluded = {
            str(asset or "").upper()
            for asset in getattr(self.settings, "excluded_base_assets", ())
        }
        return sorted({
            str(item.get("symbol") or "").upper()
            for item in source.usdt_perp_symbols()
            if isinstance(item, dict)
            and str(item.get("symbol") or "").upper().endswith("USDT")
            and str(item.get("symbol") or "").upper()[:-4] not in excluded
        })

    @staticmethod
    def _rotation_batch(
        universe: list[str],
        state: Mapping[str, Any],
        limit: int,
    ) -> tuple[list[str], dict[str, Any] | None, dict[str, Any]]:
        if not universe:
            return [], None, {
                "rotation_round": 0,
                "remaining_in_round": 0,
                "round_completed": False,
            }
        rotation = state.get("rotation")
        rotation = rotation if isinstance(rotation, Mapping) else {}
        after_symbol = str(rotation.get("after_symbol") or "").upper()
        round_number = max(1, _to_int(rotation.get("round"), 1))
        start = bisect_right(universe, after_symbol) if after_symbol else 0
        if after_symbol and start >= len(universe):
            start = 0
            round_number += 1
        end = min(len(universe), start + max(1, limit))
        batch = universe[start:end]
        completed = bool(batch) and end >= len(universe)
        update = {
            "after_symbol": "" if completed else batch[-1],
            "round": round_number + 1 if completed else round_number,
        }
        return batch, update, {
            "rotation_round": round_number,
            "rotation_start_symbol": batch[0],
            "rotation_end_symbol": batch[-1],
            "remaining_in_round": max(0, len(universe) - end),
            "round_completed": completed,
        }

    def _external_tracks(self) -> tuple[dict[str, Any], dict[str, Any]]:
        legacy_path = Path(getattr(
            self.settings,
            "consolidation_breakout_state_path",
            self.settings.data_dir / "consolidation_breakout_state.json",
        ))
        daily_path = Path(getattr(
            self.settings,
            "consolidation_daily_state_path",
            self.settings.data_dir / "consolidation_daily_product_state.json",
        ))
        legacy = self.store.load(legacy_path, {})
        daily = self.store.load(daily_path, {})
        legacy_tracks = legacy.get("tracks") if isinstance(legacy, dict) else {}
        daily_tracks = daily.get("tracks") if isinstance(daily, dict) else {}
        return (
            legacy_tracks if isinstance(legacy_tracks, dict) else {},
            daily_tracks if isinstance(daily_tracks, dict) else {},
        )

    def _make_event(
        self,
        *,
        symbol: str,
        spec: Any,
        box: Mapping[str, Any],
        source_box_id: str,
        edge: str,
        trigger_timeframe: str,
        candle: Any,
        distance_atr: float,
        distance_pct: float,
        rule: str,
        volume_ratio: float,
        edge_state: Mapping[str, Any],
        shadow_mode: bool,
        legacy_tracks: Mapping[str, Any],
        daily_tracks: Mapping[str, Any],
    ) -> dict[str, Any]:
        core = _core()
        upper = _to_float(box.get("upper"))
        lower = _to_float(box.get("lower"))
        atr = _to_float(box.get("atr"))
        boundary = upper if edge == "upper" else lower
        rearm_count = max(0, _to_int(edge_state.get("rearm_count")))
        event_name = EVENT_NAMES[edge]
        event_id = (
            f"{SCHEMA}:{symbol}:1h:{spec.name}:{edge}:"
            f"{source_box_id}:r{rearm_count}"
        )
        confluence = _higher_timeframe_confluence(
            symbol=symbol,
            edge=edge,
            hourly_boundary=boundary,
            hourly_atr=atr,
            legacy_tracks=legacy_tracks,
            daily_tracks=daily_tracks,
        )
        base_bars = max(1, _to_int(box.get("base_bars"), spec.length))
        active_bars = max(0, _to_int(box.get("active_bars")))
        event: dict[str, Any] = {
            "schema": SCHEMA,
            "event": event_name,
            "event_id": event_id,
            "dedup_key": event_id,
            "symbol": symbol,
            "timeframe": trigger_timeframe,
            "structure_timeframe": "1h",
            "trigger_timeframe": trigger_timeframe,
            "trigger_kind": "closed_candle_proximity",
            "direction": "up" if edge == "upper" else "down",
            "forecast_only": True,
            "shadow_mode": shadow_mode,
            "horizon": spec.name,
            "horizon_label": spec.label,
            "horizon_length": base_bars,
            "box_id": source_box_id,
            "box_upper": upper,
            "box_lower": lower,
            "box_age": base_bars + active_bars,
            "box_base_bars": base_bars,
            "box_width_atr": _to_float(box.get("width_atr")),
            "box_width_pct": _to_float(box.get("width_pct")),
            "box_efficiency": _to_float(box.get("efficiency")),
            "upper_touches": max(0, _to_int(box.get("upper_touches"))),
            "lower_touches": max(0, _to_int(box.get("lower_touches"))),
            "box_formed_close_time": _to_int(box.get("formed_close_time")),
            "box_start_close_time": _to_int(
                box.get("window_start_close_time")
            ),
            "atr": atr,
            "close": candle.close,
            "close_time": candle.close_time,
            "event_time": candle.close_time,
            "proximity_edge": edge,
            "proximity_edge_price": boundary,
            "proximity_distance_atr": max(0.0, distance_atr),
            "proximity_distance_pct": max(0.0, distance_pct),
            "proximity_rule": rule,
            "volume_ratio": max(0.0, volume_ratio),
            "rearm_count": rearm_count,
            "rearm_epoch": rearm_count,
            "higher_tf_confluence": confluence,
            "higher_tf_confluence_pass": bool(confluence),
            "higher_tf_confluence_count": len(confluence),
            "higher_tf_confluence_timeframes": ",".join(
                str(item.get("timeframe") or "") for item in confluence
            ),
            "higher_tf_confluence_summary": _confluence_summary(confluence),
            "priority": 0,
        }
        event.update(core._range_quality(event, spec))
        event["text"] = _format_event(event)
        return event

    def build(
        self,
        source: BinanceDataSource,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if not bool(getattr(
            self.settings,
            "consolidation_hourly_proximity_enable",
            False,
        )):
            return self._empty_result("disabled")

        observed_now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        close_delay_ms = max(0, _to_int(getattr(
            self.settings,
            "consolidation_breakout_close_delay_sec",
            90,
        ))) * 1000
        cutoff_ms = observed_now_ms - close_delay_ms
        shadow_mode = bool(getattr(
            self.settings,
            "consolidation_hourly_proximity_shadow_mode",
            True,
        ))
        discovery_limit = max(1, _to_int(getattr(
            self.settings,
            "consolidation_hourly_proximity_discovery_limit",
            DISCOVERY_LIMIT_DEFAULT,
        ), DISCOVERY_LIMIT_DEFAULT))
        monitor_limit = max(1, _to_int(getattr(
            self.settings,
            "consolidation_hourly_proximity_monitor_limit",
            FAST_MONITOR_LIMIT_DEFAULT,
        ), FAST_MONITOR_LIMIT_DEFAULT))
        max_signals = max(0, _to_int(getattr(
            self.settings,
            "consolidation_hourly_proximity_max_signals_per_scan",
            getattr(self.settings, "consolidation_breakout_max_signals_per_scan", 8),
        ), 8))
        kline_budget = max(0, _to_int(getattr(
            self.settings,
            "consolidation_hourly_proximity_kline_budget",
            60,
        ), 60))

        state = self._load_state()
        tracks = state.get("tracks")
        tracks = tracks if isinstance(tracks, dict) else {}
        try:
            universe = self._universe(source)
        except Exception as exc:
            result = self._empty_result("candidate_source_error")
            result["diagnostics"]["error"] = type(exc).__name__
            return result
        batch, rotation_update, rotation_diag = self._rotation_batch(
            universe,
            state,
            discovery_limit,
        )

        core = _core()
        strong_ratio = max(0.01, _to_float(getattr(
            self.settings,
            "consolidation_breakout_strong_volume_ratio",
            1.20,
        ), 1.20))
        state_updates: list[dict[str, Any]] = []
        merged_tracks = copy.deepcopy(tracks)
        discovery_contexts: dict[str, list[Any]] = {}
        errors: list[dict[str, str]] = []
        discovery_transition_count = 0
        discovery_history_gap_reset_count = 0
        discovered_active_boxes = 0
        rearmed_edges = 0
        formal_transition_keys: set[str] = set()

        for symbol in batch:
            try:
                rows = source.klines(
                    symbol,
                    interval="1h",
                    limit=core.CHART_HISTORY_LIMIT,
                )
            except Exception as exc:
                errors.append({
                    "symbol": symbol,
                    "timeframe": "1h",
                    "stage": "discovery",
                    "error": type(exc).__name__,
                })
                continue
            candles = _closed_candles(rows, cutoff_ms)
            if not candles:
                errors.append({
                    "symbol": symbol,
                    "timeframe": "1h",
                    "stage": "discovery",
                    "error": "no_closed_candles",
                })
                continue
            discovery_contexts[symbol] = candles
            for spec in core.HORIZONS:
                key = f"{symbol}|1h|{spec.name}"
                existing = merged_tracks.get(key, {})
                working = copy.deepcopy(existing) if isinstance(existing, dict) else {}
                last_close = _to_int(working.get("last_close_time"))
                history_gap = (
                    last_close > 0
                    and candles[0].close_time > last_close + HOUR_MS
                )
                if history_gap:
                    # The missing interval may contain a breakout or expiry.
                    # Never bridge it with the old frozen box. Re-detect only
                    # from the complete history window returned now.
                    working = {}
                    pending = [len(candles) - 1]
                    discovery_history_gap_reset_count += 1
                else:
                    pending = [
                        index
                        for index, candle in enumerate(candles)
                        if candle.close_time > last_close
                    ] if last_close > 0 else [len(candles) - 1]
                for index in pending:
                    working, raw_event = core._step_track(
                        working,
                        candles,
                        index,
                        HOUR_MS,
                        spec,
                        strong_ratio,
                    )
                    if raw_event is not None:
                        discovery_transition_count += 1
                        formal_transition_keys.add(key)
                merged_tracks[key] = copy.deepcopy(working)
                state_updates.append({
                    "key": key,
                    "state": copy.deepcopy(working),
                    "required_event_ids": [],
                })
                active_box = _active_box(working)
                if active_box is not None:
                    discovered_active_boxes += 1
                    source_box_id = _box_id(symbol, spec.name, active_box)
                    monitor_key = f"{symbol}|1h|{spec.name}|proximity"
                    monitor = _normalize_monitor(
                        merged_tracks.get(monitor_key),
                        source_box_id,
                    )
                    before_monitor = copy.deepcopy(monitor)
                    atr = _to_float(active_box.get("atr"))
                    for edge in ("upper", "lower"):
                        edge_state = monitor["edges"][edge]
                        boundary = _to_float(active_box.get(edge))
                        if (
                            (bool(edge_state.get("live_sent"))
                             or bool(edge_state.get("shadow_seen")))
                            and _rearm_ready(
                                candles,
                                edge=edge,
                                boundary=boundary,
                                atr=atr,
                            )
                            and candles[-1].close_time
                            > _to_int(edge_state.get("last_rearm_close_time"))
                        ):
                            edge_state["rearm_count"] = (
                                max(0, _to_int(edge_state.get("rearm_count"))) + 1
                            )
                            edge_state["live_sent"] = False
                            edge_state["shadow_seen"] = False
                            edge_state["last_event_id"] = ""
                            edge_state["last_trigger_timeframe"] = ""
                            edge_state["last_rearm_close_time"] = (
                                candles[-1].close_time
                            )
                            rearmed_edges += 1
                    if monitor != before_monitor:
                        merged_tracks[monitor_key] = copy.deepcopy(monitor)
                        state_updates.append({
                            "key": monitor_key,
                            "state": copy.deepcopy(monitor),
                            "required_event_ids": [],
                        })

        ticker_prices: dict[str, float] = {}
        try:
            ticker_rows = source.ticker_24h()
        except Exception as exc:
            ticker_rows = []
            errors.append({
                "symbol": "*",
                "timeframe": "ticker",
                "stage": "prefilter",
                "error": type(exc).__name__,
            })
        for item in ticker_rows:
            if not isinstance(item, Mapping):
                continue
            symbol = str(item.get("symbol") or "").upper()
            price = _to_float(item.get("lastPrice"))
            if symbol in universe and price > 0:
                ticker_prices[symbol] = price
        active_by_symbol: dict[str, list[tuple[Any, Mapping[str, Any], str]]] = {}
        for symbol in universe:
            candidates: list[tuple[Any, Mapping[str, Any], str]] = []
            for spec in core.HORIZONS:
                key = f"{symbol}|1h|{spec.name}"
                track = merged_tracks.get(key)
                box = _active_box(track)
                if box is None:
                    continue
                candidates.append((
                    spec,
                    box,
                    _box_id(symbol, spec.name, box),
                ))
            if candidates:
                active_by_symbol[symbol] = [max(
                    candidates,
                    key=lambda item: (
                        max(1, _to_int(item[1].get("base_bars"), item[0].length)),
                        item[0].length,
                    ),
                )]
        if active_by_symbol and not ticker_prices:
            errors.append({
                "symbol": "*",
                "timeframe": "ticker",
                "stage": "prefilter",
                "error": "empty_ticker",
            })

        prefilter: list[tuple[int, int, float, str]] = []
        for symbol, boxes in active_by_symbol.items():
            ticker = ticker_prices.get(symbol, 0.0)
            if ticker <= 0:
                continue
            best_distance = float("inf")
            actionable = False
            rearm_candidate = False
            last_checked_15m = 0
            for spec, box, source_box_id in boxes:
                atr = _to_float(box.get("atr"))
                if atr <= 0:
                    continue
                monitor_key = f"{symbol}|1h|{spec.name}|proximity"
                monitor = _normalize_monitor(
                    merged_tracks.get(monitor_key),
                    source_box_id,
                )
                last_checked_15m = _to_int(
                    monitor.get("last_15m_close_time")
                )
                for edge in ("upper", "lower"):
                    boundary = _to_float(box.get(edge))
                    distance = abs(ticker - boundary) / atr
                    best_distance = min(best_distance, distance)
                    if distance > TICKER_PREFILTER_ATR:
                        continue
                    edge_state = monitor["edges"][edge]
                    already_seen = (
                        bool(edge_state.get("shadow_seen"))
                        if shadow_mode
                        else bool(edge_state.get("live_sent"))
                    )
                    if not already_seen:
                        actionable = True
                    elif distance >= REARM_DISTANCE_ATR:
                        rearm_candidate = True
            if best_distance <= TICKER_PREFILTER_ATR and (
                actionable or rearm_candidate
            ):
                priority_tier = (
                    0
                    if actionable
                    and best_distance <= HOURLY_MAX_DISTANCE_ATR
                    else 1 if actionable else 2
                )
                prefilter.append((
                    priority_tier,
                    last_checked_15m,
                    best_distance,
                    symbol,
                ))
        prefilter.sort(
            key=lambda item: (item[0], item[1], item[2], item[3])
        )
        fast_symbols = [item[3] for item in prefilter[:monitor_limit]]

        legacy_tracks, daily_tracks = self._external_tracks()
        candidate_events: list[dict[str, Any]] = []
        suppression_counts: dict[str, int] = {}
        monitor_latency_seconds: list[float] = []
        skipped_closed_15m_bars = 0
        hot_structure_transition_count = 0
        hot_inactive_box_count = 0
        direct_hourly_fallback_attempt_count = 0
        direct_hourly_fallback_success_count = 0
        direct_hourly_fallback_budget_skipped_count = 0
        structure_history_gap_detected_count = 0
        structure_history_gap_unresolved_count = 0
        fast_hourly_contexts: dict[str, list[Any]] = {}
        direct_hourly_fallback_limit = max(
            0,
            kline_budget - len(batch) - len(fast_symbols) - max_signals,
        )

        for symbol in fast_symbols:
            candles_15m: list[Any] = []
            candles_1h: list[Any] = []
            fifteen_minute_failed = False
            try:
                rows = source.klines(
                    symbol,
                    interval="15m",
                    limit=FIFTEEN_MINUTE_HISTORY,
                )
            except Exception as exc:
                errors.append({
                    "symbol": symbol,
                    "timeframe": "15m",
                    "stage": "fast_monitor",
                    "error": type(exc).__name__,
                })
                fifteen_minute_failed = True
            else:
                candles_15m = _closed_candles(rows, cutoff_ms)
            if len(candles_15m) < 4:
                if not fifteen_minute_failed:
                    errors.append({
                        "symbol": symbol,
                        "timeframe": "15m",
                        "stage": "fast_monitor",
                        "error": "insufficient_closed_candles",
                    })
            else:
                candles_1h = _aggregate_closed_hours(candles_15m)

            def has_structure_history_gap(hourly_candles: list[Any]) -> bool:
                if not hourly_candles:
                    return False
                first_hour_close = hourly_candles[0].close_time
                for selected_spec, _box, _box_id_value in (
                    active_by_symbol.get(symbol, [])
                ):
                    selected_track = merged_tracks.get(
                        f"{symbol}|1h|{selected_spec.name}"
                    )
                    last_close = _to_int(
                        selected_track.get("last_close_time")
                        if isinstance(selected_track, Mapping)
                        else 0
                    )
                    if (
                        last_close > 0
                        and first_hour_close > last_close + HOUR_MS
                    ):
                        return True
                return False

            structure_history_gap = has_structure_history_gap(candles_1h)
            if structure_history_gap:
                structure_history_gap_detected_count += 1

            # A direct closed-1H request keeps the slower fallback and the
            # structural state machine alive when 15m data is unavailable or
            # cannot form two complete hours. It is also mandatory when the
            # 15m aggregate no longer reaches the track's last processed hour;
            # skipping that gap could miss a breakout and revive a stale box.
            # Reserve room for chart requests and let the data source enforce
            # the hard request budget as well.
            if len(candles_1h) < 2 or structure_history_gap:
                if (
                    direct_hourly_fallback_attempt_count
                    >= direct_hourly_fallback_limit
                ):
                    direct_hourly_fallback_budget_skipped_count += 1
                    errors.append({
                        "symbol": symbol,
                        "timeframe": "1h",
                        "stage": "fast_monitor_fallback",
                        "error": "fallback_budget_reserved",
                    })
                else:
                    direct_hourly_fallback_attempt_count += 1
                    try:
                        hourly_rows = source.klines(
                            symbol,
                            interval="1h",
                            limit=core.CHART_HISTORY_LIMIT,
                        )
                    except Exception as exc:
                        errors.append({
                            "symbol": symbol,
                            "timeframe": "1h",
                            "stage": "fast_monitor_fallback",
                            "error": type(exc).__name__,
                        })
                    else:
                        direct_hourly = _closed_candles(
                            hourly_rows,
                            cutoff_ms,
                        )
                        if len(direct_hourly) < 2:
                            errors.append({
                                "symbol": symbol,
                                "timeframe": "1h",
                                "stage": "fast_monitor_fallback",
                                "error": "insufficient_closed_candles",
                            })
                        else:
                            candles_1h = direct_hourly
                            fast_hourly_contexts[symbol] = direct_hourly
                            direct_hourly_fallback_success_count += 1
                            structure_history_gap = (
                                has_structure_history_gap(candles_1h)
                            )
            if len(candles_1h) < 2 or structure_history_gap:
                if structure_history_gap:
                    structure_history_gap_unresolved_count += 1
                    errors.append({
                        "symbol": symbol,
                        "timeframe": "1h",
                        "stage": "fast_monitor_fallback",
                        "error": "structure_history_gap",
                    })
                continue

            for spec, box, source_box_id in active_by_symbol.get(symbol, []):
                track_key = f"{symbol}|1h|{spec.name}"
                current_track = merged_tracks.get(track_key, {})
                working_track = (
                    copy.deepcopy(current_track)
                    if isinstance(current_track, dict)
                    else {}
                )
                last_structure_close = _to_int(
                    working_track.get("last_close_time")
                )
                pending_hours = [
                    index
                    for index, candle in enumerate(candles_1h)
                    if candle.close_time > last_structure_close
                ]
                formal_transition_seen = track_key in formal_transition_keys
                for index in pending_hours:
                    working_track, raw_event = core._step_track(
                        working_track,
                        candles_1h,
                        index,
                        HOUR_MS,
                        spec,
                        strong_ratio,
                    )
                    if raw_event is not None:
                        formal_transition_seen = True
                        hot_structure_transition_count += 1
                if pending_hours:
                    merged_tracks[track_key] = copy.deepcopy(working_track)
                    state_updates.append({
                        "key": track_key,
                        "state": copy.deepcopy(working_track),
                        "required_event_ids": [],
                    })

                refreshed_box = _active_box(working_track)
                if refreshed_box is None:
                    hot_inactive_box_count += 1
                    suppression_counts["source_box_inactive"] = (
                        suppression_counts.get("source_box_inactive", 0) + 1
                    )
                    continue
                refreshed_box_id = _box_id(
                    symbol,
                    spec.name,
                    refreshed_box,
                )
                if refreshed_box_id != source_box_id:
                    hot_inactive_box_count += 1
                    suppression_counts["source_box_changed"] = (
                        suppression_counts.get("source_box_changed", 0) + 1
                    )
                    continue
                if formal_transition_seen:
                    # The structural transition owns this closed-data window.
                    # Advance the proximity watermark without consuming a
                    # future re-armed alert, so replaying the same candles
                    # cannot emit a stale "near edge" message.
                    monitor_key = f"{symbol}|1h|{spec.name}|proximity"
                    transition_monitor = _normalize_monitor(
                        merged_tracks.get(monitor_key),
                        source_box_id,
                    )
                    before_transition_monitor = copy.deepcopy(
                        transition_monitor
                    )
                    if len(candles_15m) >= 4:
                        transition_monitor["last_15m_close_time"] = (
                            candles_15m[-1].close_time
                        )
                    transition_monitor["last_1h_close_time"] = (
                        candles_1h[-1].close_time
                    )
                    if transition_monitor != before_transition_monitor:
                        merged_tracks[monitor_key] = copy.deepcopy(
                            transition_monitor
                        )
                        state_updates.append({
                            "key": monitor_key,
                            "state": copy.deepcopy(transition_monitor),
                            "required_event_ids": [],
                        })
                    suppression_counts["formal_structure_transition"] = (
                        suppression_counts.get(
                            "formal_structure_transition",
                            0,
                        ) + 1
                    )
                    continue
                box = refreshed_box

                monitor_key = f"{symbol}|1h|{spec.name}|proximity"
                monitor = _normalize_monitor(
                    merged_tracks.get(monitor_key),
                    source_box_id,
                )
                before = copy.deepcopy(monitor)
                previous_15m_close = _to_int(
                    monitor.get("last_15m_close_time")
                )
                latest_monitor_close = (
                    candles_15m[-1].close_time
                    if len(candles_15m) >= 4
                    else candles_1h[-1].close_time
                )
                monitor_latency_seconds.append(max(
                    0.0,
                    (observed_now_ms - latest_monitor_close) / 1000.0,
                ))
                if previous_15m_close > 0 and len(candles_15m) >= 4:
                    skipped_closed_15m_bars += max(
                        0,
                        (candles_15m[-1].close_time - previous_15m_close)
                        // FIFTEEN_MINUTE_MS
                        - 1,
                    )
                atr = _to_float(box.get("atr"))
                event_for_monitor: dict[str, Any] | None = None
                hard_veto_edges: set[str] = set()

                for edge in ("upper", "lower"):
                    edge_state = monitor["edges"][edge]
                    boundary = _to_float(box.get(edge))
                    if (
                        (bool(edge_state.get("live_sent"))
                         or bool(edge_state.get("shadow_seen")))
                        and _rearm_ready(
                            candles_1h,
                            edge=edge,
                            boundary=boundary,
                            atr=atr,
                        )
                        and candles_1h[-1].close_time
                        > _to_int(edge_state.get("last_rearm_close_time"))
                    ):
                        edge_state["rearm_count"] = (
                            max(0, _to_int(edge_state.get("rearm_count"))) + 1
                        )
                        edge_state["live_sent"] = False
                        edge_state["shadow_seen"] = False
                        edge_state["last_event_id"] = ""
                        edge_state["last_trigger_timeframe"] = ""
                        edge_state["last_rearm_close_time"] = (
                            candles_1h[-1].close_time
                        )
                        rearmed_edges += 1

                # The fast 15m preview owns the first alert opportunity when
                # available. Closed 1H remains an independent fallback.
                trigger_timeframes = (
                    ("15m", "1h")
                    if len(candles_15m) >= 4
                    else ("1h",)
                )
                for trigger_timeframe in trigger_timeframes:
                    if event_for_monitor is not None:
                        break
                    if trigger_timeframe == "1h":
                        if len(candles_1h) < 2:
                            continue
                        if (
                            len(candles_15m) >= 4
                            and candles_15m[-1].close_time
                            > candles_1h[-1].close_time
                        ):
                            suppression_counts["newer_15m_than_1h"] = (
                                suppression_counts.get(
                                    "newer_15m_than_1h",
                                    0,
                                ) + 1
                            )
                            continue
                        trigger_candle = candles_1h[-1]
                        last_processed = _to_int(
                            monitor.get("last_1h_close_time")
                        )
                    else:
                        trigger_candle = candles_15m[-1]
                        last_processed = _to_int(
                            monitor.get("last_15m_close_time")
                        )

                    edge_candidates: list[
                        tuple[float, str, float, str]
                    ] = []
                    for edge in ("upper", "lower"):
                        if (
                            trigger_timeframe == "1h"
                            and edge in hard_veto_edges
                        ):
                            continue
                        edge_state = monitor["edges"][edge]
                        already_seen = (
                            bool(edge_state.get("shadow_seen"))
                            if shadow_mode
                            else bool(edge_state.get("live_sent"))
                        )
                        live_after_shadow = (
                            not shadow_mode
                            and bool(edge_state.get("shadow_seen"))
                            and not bool(edge_state.get("live_sent"))
                        )
                        if already_seen and not live_after_shadow:
                            continue
                        if (
                            trigger_candle.close_time <= last_processed
                            and not live_after_shadow
                        ):
                            continue
                        boundary = _to_float(box.get(edge))
                        if trigger_timeframe == "1h":
                            passed, distance_atr, distance_pct, rule = (
                                _hourly_proximity(
                                    candles_1h,
                                    edge=edge,
                                    boundary=boundary,
                                    atr=atr,
                                )
                            )
                        else:
                            passed, distance_atr, distance_pct, rule = (
                                _fifteen_minute_proximity(
                                    candles_15m,
                                    edge=edge,
                                    boundary=boundary,
                                    atr=atr,
                                )
                            )
                        if not passed:
                            if (
                                trigger_timeframe == "15m"
                                and rule in {
                                    "wick_or_close_crossed",
                                    "outside_box",
                                }
                            ):
                                hard_veto_edges.add(edge)
                            suppression_counts[rule] = (
                                suppression_counts.get(rule, 0) + 1
                            )
                            continue
                        edge_candidates.append((
                            distance_atr,
                            edge,
                            distance_pct,
                            rule,
                        ))

                    if edge_candidates:
                        distance_atr, edge, distance_pct, rule = min(
                            edge_candidates,
                            key=lambda item: (item[0], item[1]),
                        )
                        edge_state = monitor["edges"][edge]
                        trigger_candles = (
                            candles_15m
                            if trigger_timeframe == "15m"
                            else candles_1h
                        )
                        event_for_monitor = self._make_event(
                            symbol=symbol,
                            spec=spec,
                            box=box,
                            source_box_id=source_box_id,
                            edge=edge,
                            trigger_timeframe=trigger_timeframe,
                            candle=trigger_candle,
                            distance_atr=distance_atr,
                            distance_pct=distance_pct,
                            rule=rule,
                            volume_ratio=core._volume_ratio(
                                trigger_candles,
                                len(trigger_candles) - 1,
                            ),
                            edge_state=edge_state,
                            shadow_mode=shadow_mode,
                            legacy_tracks=legacy_tracks,
                            daily_tracks=daily_tracks,
                        )
                        if shadow_mode:
                            edge_state["shadow_seen"] = True
                        else:
                            edge_state["live_sent"] = True
                        edge_state["last_event_id"] = event_for_monitor[
                            "event_id"
                        ]
                        edge_state["last_event_close_time"] = (
                            trigger_candle.close_time
                        )
                        edge_state["last_trigger_timeframe"] = (
                            trigger_timeframe
                        )

                if len(candles_15m) >= 4:
                    monitor["last_15m_close_time"] = (
                        candles_15m[-1].close_time
                    )
                if candles_1h:
                    monitor["last_1h_close_time"] = candles_1h[-1].close_time
                required_ids: list[str] = []
                if event_for_monitor is not None:
                    candidate_events.append(event_for_monitor)
                    required_ids.append(str(event_for_monitor["event_id"]))
                if monitor != before:
                    merged_tracks[monitor_key] = copy.deepcopy(monitor)
                    state_updates.append({
                        "key": monitor_key,
                        "state": copy.deepcopy(monitor),
                        "required_event_ids": required_ids,
                    })

        candidate_events.sort(key=lambda event: (
            -_to_int(event.get("higher_tf_confluence_count")),
            0 if event.get("trigger_timeframe") == "15m" else 1,
            -_to_int(event.get("quality_rank")),
            _to_float(event.get("proximity_distance_atr")),
            str(event.get("symbol") or ""),
            str(event.get("horizon") or ""),
        ))
        events = candidate_events[:max_signals]
        withheld_ids = {
            str(event.get("event_id") or "")
            for event in candidate_events[max_signals:]
        }

        chart_payloads: dict[str, dict[str, Any]] = {}
        chart_cache: dict[str, list[Any]] = dict(fast_hourly_contexts)
        chart_cache.update(discovery_contexts)
        for event in events:
            symbol = str(event.get("symbol") or "")
            event_id = str(event.get("event_id") or "")
            chart_candles = chart_cache.get(symbol, [])
            event_close_time = _to_int(event.get("close_time"))
            exact_hour = any(
                candle.close_time == event_close_time
                for candle in chart_candles
            )
            if not chart_candles or (
                event.get("trigger_timeframe") == "1h" and not exact_hour
            ):
                try:
                    rows = source.klines(
                        symbol,
                        interval="1h",
                        limit=core.CHART_HISTORY_LIMIT,
                    )
                except Exception as exc:
                    errors.append({
                        "symbol": symbol,
                        "timeframe": "1h",
                        "stage": "chart",
                        "error": type(exc).__name__,
                    })
                    continue
                chart_candles = _closed_candles(rows, cutoff_ms)
                chart_cache[symbol] = chart_candles
            eligible = [
                candle
                for candle in chart_candles
                if candle.close_time <= event_close_time
            ]
            if not eligible:
                continue
            if (
                event.get("trigger_timeframe") == "1h"
                and eligible[-1].close_time != event_close_time
            ):
                continue
            payload = core._chart_payload(
                eligible,
                len(eligible) - 1,
                event,
            )
            payload.update({
                "structure_timeframe": "1h",
                "trigger_timeframe": str(event.get("trigger_timeframe") or ""),
            })
            if event.get("trigger_timeframe") == "15m":
                payload["trigger_marker"] = {
                    "close_time": event_close_time,
                    "price": _to_float(event.get("close")),
                }
            chart_payloads[event_id] = payload

        diagnostics: dict[str, Any] = {
            "status": "ok" if not errors else "degraded",
            "schema": SCHEMA,
            "shadow_mode": shadow_mode,
            "candidate_count": len(universe),
            "discovery_batch_count": len(batch),
            "discovery_limit": discovery_limit,
            "discovery_active_box_count": discovered_active_boxes,
            "discovery_transition_count": discovery_transition_count,
            "discovery_history_gap_reset_count": (
                discovery_history_gap_reset_count
            ),
            "active_box_symbol_count": len(active_by_symbol),
            "ticker_prefilter_count": len(prefilter),
            "fast_monitor_count": len(fast_symbols),
            "monitor_limit": monitor_limit,
            "kline_budget": kline_budget,
            "monitor_backlog_count": max(
                0,
                len(prefilter) - len(fast_symbols),
            ),
            "event_candidate_count": len(candidate_events),
            "event_count": len(events),
            "confluence_event_count": sum(
                bool(event.get("higher_tf_confluence_pass"))
                for event in events
            ),
            "shadow_event_count": len(events) if shadow_mode else 0,
            "withheld_event_count": len(withheld_ids),
            "rearmed_edge_count": rearmed_edges,
            "hot_structure_transition_count": (
                hot_structure_transition_count
            ),
            "hot_inactive_box_count": hot_inactive_box_count,
            "direct_hourly_fallback_limit": direct_hourly_fallback_limit,
            "direct_hourly_fallback_attempt_count": (
                direct_hourly_fallback_attempt_count
            ),
            "direct_hourly_fallback_success_count": (
                direct_hourly_fallback_success_count
            ),
            "direct_hourly_fallback_budget_skipped_count": (
                direct_hourly_fallback_budget_skipped_count
            ),
            "structure_history_gap_detected_count": (
                structure_history_gap_detected_count
            ),
            "structure_history_gap_unresolved_count": (
                structure_history_gap_unresolved_count
            ),
            "suppression_counts": suppression_counts,
            "chart_payload_count": len(chart_payloads),
            "skipped_closed_15m_bar_count": skipped_closed_15m_bars,
            "p95_decision_latency_sec": (
                sorted(monitor_latency_seconds)[
                    max(
                        0,
                        math.ceil(len(monitor_latency_seconds) * 0.95) - 1,
                    )
                ]
                if monitor_latency_seconds
                else 0.0
            ),
            "max_decision_latency_sec": (
                max(monitor_latency_seconds)
                if monitor_latency_seconds
                else 0.0
            ),
            "cutoff_ms": cutoff_ms,
        }
        diagnostics.update(rotation_diag)
        source_diagnostics = getattr(source, "diagnostics", None)
        if callable(source_diagnostics):
            diagnostics["binance"] = source_diagnostics()
        if errors:
            diagnostics["errors"] = errors[:20]
        return {
            "template_id": TEMPLATE_ID,
            "events": events,
            "chart_payloads": chart_payloads,
            "state_updates": state_updates,
            "rotation_update": rotation_update,
            "diagnostics": diagnostics,
        }

    def commit(
        self,
        result: Mapping[str, Any],
        accepted_event_ids: Iterable[str] | None,
    ) -> dict[str, Any]:
        accepted = {
            str(value)
            for value in (accepted_event_ids or ())
            if str(value)
        }
        raw_updates = result.get("state_updates")
        updates = raw_updates if isinstance(raw_updates, list) else []
        applicable: list[Mapping[str, Any]] = []
        deferred = 0
        for update in updates:
            if not isinstance(update, Mapping):
                continue
            key = str(update.get("key") or "")
            state = update.get("state")
            if not key or not isinstance(state, dict):
                continue
            raw_required = update.get("required_event_ids")
            required = {
                str(value)
                for value in (
                    raw_required
                    if isinstance(raw_required, (list, tuple, set))
                    else ()
                )
                if str(value)
            }
            if required.issubset(accepted):
                applicable.append(update)
            else:
                deferred += 1

        raw_rotation = result.get("rotation_update")
        rotation = raw_rotation if isinstance(raw_rotation, Mapping) else None
        rotation_update = None
        if rotation is not None:
            rotation_update = {
                "after_symbol": str(
                    rotation.get("after_symbol") or ""
                ).strip().upper(),
                "round": max(1, _to_int(rotation.get("round"), 1)),
            }
        if not applicable and rotation_update is None:
            return {
                "status": "deferred" if deferred else "no_changes",
                "applied": 0,
                "deferred": deferred,
                "rotation_advanced": False,
            }

        def apply(current: Any) -> dict[str, Any]:
            if (
                not isinstance(current, dict)
                or current.get("schema_version") != STATE_SCHEMA_VERSION
            ):
                payload: dict[str, Any] = {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "tracks": {},
                    "rotation": {"after_symbol": "", "round": 1},
                }
            else:
                payload = copy.deepcopy(current)
                if not isinstance(payload.get("tracks"), dict):
                    payload["tracks"] = {}
            for update in applicable:
                payload["tracks"][str(update["key"])] = copy.deepcopy(
                    update["state"]
                )
            if rotation_update is not None:
                payload["rotation"] = copy.deepcopy(rotation_update)
            payload["updated_at"] = int(time.time())
            return payload

        self.store.update(self.state_path, apply, {})
        return {
            "status": "ok",
            "applied": len(applicable),
            "deferred": deferred,
            "rotation_advanced": rotation_update is not None,
        }


__all__ = [
    "ConsolidationHourlyProximityRadar",
    "SCHEMA",
    "TEMPLATE_ID",
]
