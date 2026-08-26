from __future__ import annotations

import copy
import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Any, Iterable

from config import Settings
from shared.binance_data import BinanceDataSource
from shared.storage import JsonStore


TEMPLATE_ID = "TG_CONSOLIDATION_BREAKOUT"
STATE_SCHEMA_VERSION = 1
CST = timezone(timedelta(hours=8))

ATR_PERIOD = 14
TOUCH_TOLERANCE_ATR = 0.20
ENDPOINT_DRIFT_ATR = 0.35
BREAKOUT_BUFFER_ATR = 0.10
REENTRY_BUFFER_ATR = 0.05
FAKEOUT_BARS = 3
RETEST_BARS = 12


@dataclass(frozen=True)
class HorizonSpec:
    name: str
    label: str
    length: int
    max_width_atr: float
    max_width_pct: float
    max_efficiency: float
    stability: int
    cooldown: int
    maximum_age: int
    rank: int


HORIZONS = (
    HorizonSpec("short", "短期", 24, 4.5, 8.0, 0.35, 3, 5, 120, 1),
    HorizonSpec("medium", "中期", 72, 9.0, 18.0, 0.30, 5, 8, 360, 2),
    HorizonSpec("long", "长期", 240, 18.0, 35.0, 0.25, 8, 12, 0, 3),
)

EVENT_PRIORITY = {
    "upper_sweep": 1,
    "lower_sweep": 1,
    "retest_up": 2,
    "retest_down": 2,
    "breakout_up": 3,
    "breakout_down": 3,
    "strong_breakout_up": 4,
    "strong_breakout_down": 4,
    "fake_breakout": 5,
    "fake_breakdown": 5,
}

EVENT_LABELS = {
    "breakout_up": ("🚀", "向上突破"),
    "breakout_down": ("📉", "向下跌破"),
    "strong_breakout_up": ("🔥", "放量向上突破"),
    "strong_breakout_down": ("🧊", "放量向下跌破"),
    "retest_up": ("✅", "突破后回踩确认"),
    "retest_down": ("✅", "跌破后反抽确认"),
    "fake_breakout": ("⚠️", "假突破"),
    "fake_breakdown": ("⚠️", "假跌破"),
    "upper_sweep": ("🧹", "上沿扫流动性"),
    "lower_sweep": ("🧹", "下沿扫流动性"),
}


@dataclass(frozen=True)
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int

    @classmethod
    def from_binance(cls, row: Any) -> Candle | None:
        if not isinstance(row, (list, tuple)) or len(row) < 7:
            return None
        try:
            candle = cls(
                open_time=int(row[0]),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=max(0.0, float(row[5])),
                close_time=int(row[6]),
            )
        except (TypeError, ValueError, OverflowError):
            return None
        values = (candle.open, candle.high, candle.low, candle.close, candle.volume)
        if not all(math.isfinite(value) for value in values):
            return None
        if candle.close_time <= 0 or candle.high < candle.low or candle.close <= 0:
            return None
        return candle


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


def _timeframe_ms(timeframe: str) -> int:
    value = str(timeframe or "").strip().lower()
    if len(value) < 2:
        return 0
    try:
        amount = int(value[:-1])
    except ValueError:
        return 0
    unit = value[-1]
    multiplier = {
        "m": 60_000,
        "h": 3_600_000,
        "d": 86_400_000,
        "w": 7 * 86_400_000,
    }.get(unit, 0)
    return max(0, amount) * multiplier


def _cluster_count(flags: Iterable[bool]) -> int:
    """Count separated touch clusters; adjacent touch bars count only once."""

    count = 0
    touching = False
    for flag in flags:
        active = bool(flag)
        if active and not touching:
            count += 1
        touching = active
    return count


def count_touch_clusters(
    candles: list[Candle],
    *,
    upper: float,
    lower: float,
    tolerance: float,
) -> tuple[int, int]:
    tolerance = max(0.0, tolerance)
    upper_count = _cluster_count(
        candle.high >= upper - tolerance for candle in candles
    )
    lower_count = _cluster_count(
        candle.low <= lower + tolerance for candle in candles
    )
    return upper_count, lower_count


def _atr(candles: list[Candle], end_index: int, period: int = ATR_PERIOD) -> float:
    if end_index <= 0 or end_index >= len(candles):
        return 0.0
    start = max(1, end_index - max(1, period) + 1)
    values: list[float] = []
    for index in range(start, end_index + 1):
        candle = candles[index]
        previous_close = candles[index - 1].close
        values.append(max(
            candle.high - candle.low,
            abs(candle.high - previous_close),
            abs(candle.low - previous_close),
        ))
    return sum(values) / len(values) if values else 0.0


def _path_efficiency(candles: list[Candle]) -> float:
    if len(candles) < 2:
        return 1.0
    travelled = sum(
        abs(candles[index].close - candles[index - 1].close)
        for index in range(1, len(candles))
    )
    if travelled <= 0:
        return 0.0
    return abs(candles[-1].close - candles[0].close) / travelled


def _box_candidate(
    candles: list[Candle],
    current_index: int,
    spec: HorizonSpec,
) -> dict[str, Any] | None:
    """Build a confirmed range from bars strictly preceding ``current_index``."""

    previous_end = current_index - 1
    earliest_start = previous_end - spec.length + 1 - (spec.stability - 1)
    if earliest_start < 0 or previous_end <= 0:
        return None
    main_start = previous_end - spec.length + 1
    window = candles[main_start:previous_end + 1]
    if len(window) != spec.length:
        return None

    atr = _atr(candles, previous_end)
    if atr <= 0:
        return None
    upper = max(candle.high for candle in window)
    lower = min(candle.low for candle in window)
    width = upper - lower
    midpoint = (upper + lower) / 2.0
    if width <= 0 or midpoint <= 0:
        return None
    width_atr = width / atr
    width_pct = width / midpoint * 100.0
    efficiency = _path_efficiency(window)
    if (
        width_atr > spec.max_width_atr
        or width_pct > spec.max_width_pct
        or efficiency > spec.max_efficiency
    ):
        return None

    max_drift = ENDPOINT_DRIFT_ATR * atr
    for shift in range(1, spec.stability):
        shifted_end = previous_end - shift
        shifted_start = shifted_end - spec.length + 1
        shifted = candles[shifted_start:shifted_end + 1]
        if len(shifted) != spec.length:
            return None
        shifted_upper = max(candle.high for candle in shifted)
        shifted_lower = min(candle.low for candle in shifted)
        if abs(shifted_upper - upper) > max_drift:
            return None
        if abs(shifted_lower - lower) > max_drift:
            return None

    upper_touches, lower_touches = count_touch_clusters(
        window,
        upper=upper,
        lower=lower,
        tolerance=TOUCH_TOLERANCE_ATR * atr,
    )
    if upper_touches < 2 or lower_touches < 2:
        return None
    return {
        "upper": upper,
        "lower": lower,
        "atr": atr,
        "width_atr": width_atr,
        "width_pct": width_pct,
        "efficiency": efficiency,
        "upper_touches": upper_touches,
        "lower_touches": lower_touches,
        "formed_close_time": candles[current_index].close_time,
        "active_bars": 0,
        "base_bars": spec.length,
        "upper_sweep_sent": False,
        "lower_sweep_sent": False,
    }


def _volume_ratio(candles: list[Candle], current_index: int) -> float:
    start = max(0, current_index - 20)
    baseline = [candle.volume for candle in candles[start:current_index]]
    average = sum(baseline) / len(baseline) if baseline else 0.0
    if average <= 0:
        return 0.0
    return candles[current_index].volume / average


def _reset_after_observation(
    track: dict[str, Any],
    candle: Candle,
    timeframe_ms: int,
    spec: HorizonSpec,
) -> None:
    track["box"] = None
    track["breakout"] = None
    track["cooldown_until"] = candle.close_time + timeframe_ms * spec.cooldown


def _step_track(
    original: dict[str, Any],
    candles: list[Candle],
    current_index: int,
    timeframe_ms: int,
    spec: HorizonSpec,
    strong_volume_ratio: float,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    track = copy.deepcopy(original) if isinstance(original, dict) else {}
    candle = candles[current_index]
    track.setdefault("box", None)
    track.setdefault("breakout", None)
    track.setdefault("cooldown_until", 0)

    box = track.get("box")
    if not isinstance(box, dict):
        track["box"] = None
        track["breakout"] = None
        if candle.close_time >= int(track.get("cooldown_until") or 0):
            candidate = _box_candidate(candles, current_index, spec)
            if candidate is not None:
                track["box"] = candidate
                box = candidate
    else:
        box["active_bars"] = max(0, int(box.get("active_bars") or 0)) + 1

    box = track.get("box")
    if not isinstance(box, dict):
        track["last_close_time"] = candle.close_time
        return track, None
    box.setdefault("upper_sweep_sent", False)
    box.setdefault("lower_sweep_sent", False)

    if (
        spec.maximum_age > 0
        and int(box.get("active_bars") or 0) > spec.maximum_age
        and not isinstance(track.get("breakout"), dict)
    ):
        _reset_after_observation(track, candle, timeframe_ms, spec)
        track["last_close_time"] = candle.close_time
        return track, None

    upper = _to_float(box.get("upper"))
    lower = _to_float(box.get("lower"))
    frozen_atr = _to_float(box.get("atr"))
    if upper <= lower or frozen_atr <= 0:
        _reset_after_observation(track, candle, timeframe_ms, spec)
        track["last_close_time"] = candle.close_time
        return track, None

    breakout_buffer = BREAKOUT_BUFFER_ATR * frozen_atr
    reentry_buffer = REENTRY_BUFFER_ATR * frozen_atr
    ratio = _volume_ratio(candles, current_index)
    breakout = track.get("breakout")
    event_name = ""
    direction = ""

    if isinstance(breakout, dict):
        breakout["bars_since"] = max(0, int(breakout.get("bars_since") or 0)) + 1
        bars_since = int(breakout["bars_since"])
        breakout_direction = str(breakout.get("direction") or "")
        if breakout_direction == "up":
            if candle.close < lower - breakout_buffer:
                event_name = (
                    "strong_breakout_down"
                    if ratio >= strong_volume_ratio
                    else "breakout_down"
                )
                direction = "down"
                track["breakout"] = {
                    "direction": "down",
                    "bars_since": 0,
                    "started_close_time": candle.close_time,
                    "retest_sent": False,
                }
            elif bars_since > RETEST_BARS:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif bars_since <= FAKEOUT_BARS and candle.close < upper - reentry_buffer:
                event_name = "fake_breakout"
                direction = "down"
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif bars_since > FAKEOUT_BARS and candle.close < upper - reentry_buffer:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif (
                bars_since <= RETEST_BARS
                and not bool(breakout.get("retest_sent"))
                and candle.low <= upper + reentry_buffer
                and candle.close > upper + reentry_buffer
            ):
                event_name = "retest_up"
                direction = "up"
                breakout["retest_sent"] = True
        elif breakout_direction == "down":
            if candle.close > upper + breakout_buffer:
                event_name = (
                    "strong_breakout_up"
                    if ratio >= strong_volume_ratio
                    else "breakout_up"
                )
                direction = "up"
                track["breakout"] = {
                    "direction": "up",
                    "bars_since": 0,
                    "started_close_time": candle.close_time,
                    "retest_sent": False,
                }
            elif bars_since > RETEST_BARS:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif bars_since <= FAKEOUT_BARS and candle.close > lower + reentry_buffer:
                event_name = "fake_breakdown"
                direction = "up"
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif bars_since > FAKEOUT_BARS and candle.close > lower + reentry_buffer:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif (
                bars_since <= RETEST_BARS
                and not bool(breakout.get("retest_sent"))
                and candle.high >= lower - reentry_buffer
                and candle.close < lower - reentry_buffer
            ):
                event_name = "retest_down"
                direction = "down"
                breakout["retest_sent"] = True
        else:
            track["breakout"] = None
    else:
        if candle.close > upper + breakout_buffer:
            event_name = (
                "strong_breakout_up"
                if ratio >= strong_volume_ratio
                else "breakout_up"
            )
            direction = "up"
            track["breakout"] = {
                "direction": "up",
                "bars_since": 0,
                "started_close_time": candle.close_time,
                "retest_sent": False,
            }
        elif candle.close < lower - breakout_buffer:
            event_name = (
                "strong_breakout_down"
                if ratio >= strong_volume_ratio
                else "breakout_down"
            )
            direction = "down"
            track["breakout"] = {
                "direction": "down",
                "bars_since": 0,
                "started_close_time": candle.close_time,
                "retest_sent": False,
            }
        else:
            swept_upper = candle.high > upper + breakout_buffer and candle.close <= upper
            swept_lower = candle.low < lower - breakout_buffer and candle.close >= lower
            if swept_upper and swept_lower:
                _reset_after_observation(track, candle, timeframe_ms, spec)
            elif swept_upper and not bool(box.get("upper_sweep_sent")):
                event_name = "upper_sweep"
                box["upper_sweep_sent"] = True
            elif swept_lower and not bool(box.get("lower_sweep_sent")):
                event_name = "lower_sweep"
                box["lower_sweep_sent"] = True
            if event_name:
                direction = "down" if event_name == "upper_sweep" else "up"

    track["last_close_time"] = candle.close_time
    if not event_name:
        return track, None
    event = {
        "event": event_name,
        "direction": direction,
        "close_time": candle.close_time,
        "close": candle.close,
        "box_upper": upper,
        "box_lower": lower,
        "box_age": spec.length + max(0, int(box.get("active_bars") or 0)),
        "box_width_atr": _to_float(box.get("width_atr")),
        "box_width_pct": _to_float(box.get("width_pct")),
        "width_pct": _to_float(box.get("width_pct")),
        "box_efficiency": _to_float(box.get("efficiency")),
        "upper_touches": max(0, int(box.get("upper_touches") or 0)),
        "lower_touches": max(0, int(box.get("lower_touches") or 0)),
        "volume_ratio": ratio,
        "bars_since_breakout": (
            0
            if event_name in {
                "breakout_up",
                "breakout_down",
                "strong_breakout_up",
                "strong_breakout_down",
            }
            else max(0, int(breakout.get("bars_since") or 0))
            if isinstance(breakout, dict)
            else 0
        ),
    }
    # Signed distance from the event's trigger edge: positive is beyond the
    # edge in breakout direction; negative has closed back inside the box.
    if event_name in {
        "breakout_up",
        "strong_breakout_up",
        "retest_up",
        "fake_breakout",
        "upper_sweep",
    }:
        event["breakout_distance_pct"] = (candle.close - upper) / upper * 100.0
    else:
        event["breakout_distance_pct"] = (lower - candle.close) / lower * 100.0
    event["breakout_distance_basis"] = "signed_directional_edge_pct"
    return track, event


def _price(value: float) -> str:
    amount = abs(value)
    if amount >= 1000:
        return f"{value:,.2f}"
    if amount >= 1:
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _event_score(event: dict[str, Any], spec: HorizonSpec, strong_ratio: float) -> int:
    width_atr = max(0.0, _to_float(event.get("box_width_atr")))
    tightness = max(0.0, 1.0 - width_atr / max(spec.max_width_atr, 0.01))
    touch_bonus = min(
        10.0,
        max(0, int(event.get("upper_touches") or 0) + int(event.get("lower_touches") or 0) - 4) * 2.0 + 4.0,
    )
    ratio = max(0.0, _to_float(event.get("volume_ratio")))
    volume_bonus = min(20.0, ratio / max(strong_ratio, 0.01) * 10.0)
    kind_bonus = {
        "fake_breakout": 12,
        "fake_breakdown": 12,
        "strong_breakout_up": 10,
        "strong_breakout_down": 10,
        "retest_up": 7,
        "retest_down": 7,
    }.get(str(event.get("event") or ""), 0)
    return min(100, max(1, round(45 + tightness * 15 + touch_bonus + volume_bonus + kind_bonus)))


def _observation_text(event_name: str) -> str:
    if event_name in {"breakout_up", "strong_breakout_up"}:
        return "未来3根K线若深度回到上沿内侧，升级为假突破；12根内关注回踩确认。"
    if event_name in {"breakout_down", "strong_breakout_down"}:
        return "未来3根K线若深度收回下沿上方，升级为假跌破；12根内关注反抽确认。"
    if event_name == "fake_breakout":
        return "突破后3根K线内深度重返箱体，原向上突破失效；等待冷却后重新识别箱体。"
    if event_name == "fake_breakdown":
        return "跌破后3根K线内深度收回箱体，原向下跌破失效；等待冷却后重新识别箱体。"
    if event_name == "retest_up":
        return "价格回踩原箱体上沿后重新收于其上，当前按突破确认观察。"
    if event_name == "retest_down":
        return "价格反抽原箱体下沿后重新收于其下，当前按跌破确认观察。"
    if event_name == "upper_sweep":
        return "影线越过上沿但收盘回到箱体，暂按扫流动性处理，不视为有效突破。"
    return "影线跌破下沿但收盘回到箱体，暂按扫流动性处理，不视为有效跌破。"


def _format_event(event: dict[str, Any]) -> str:
    event_name = str(event.get("event") or "")
    icon, label = EVENT_LABELS[event_name]
    symbol = escape(str(event.get("symbol") or ""), quote=False)
    timeframe = escape(str(event.get("timeframe") or "").upper(), quote=False)
    horizon = escape(str(event.get("horizon_label") or ""), quote=False)
    close_time = int(event.get("close_time") or 0)
    when = datetime.fromtimestamp(close_time / 1000, CST).strftime("%m-%d %H:%M CST")
    return "\n".join([
        f"{icon} <b>盘整突破雷达 · {escape(label, quote=False)}</b>",
        f"<b>{symbol}</b> ｜ 周期 {timeframe} ｜ {horizon}箱体（{int(event.get('box_age') or 0)}根）",
        f"⏰ {when}",
        "",
        f"收盘｜<b>{_price(_to_float(event.get('close')))}</b>",
        f"箱体｜上沿 {_price(_to_float(event.get('box_upper')))} ｜ 下沿 {_price(_to_float(event.get('box_lower')))}",
        (
            f"箱宽｜{_to_float(event.get('box_width_pct')):.2f}% ｜ "
            f"{_to_float(event.get('box_width_atr')):.2f} ATR"
        ),
        (
            f"触碰｜上沿 {int(event.get('upper_touches') or 0)} ｜ "
            f"下沿 {int(event.get('lower_touches') or 0)}（已去抖）"
        ),
        (
            f"量比｜{_to_float(event.get('volume_ratio')):.2f}x ｜ "
            f"评分 <b>{int(event.get('score') or 0)}/100</b>"
        ),
        "",
        f"🧭 观察：{escape(_observation_text(event_name), quote=False)}",
    ])


class ConsolidationBreakoutRadar:
    """Scan closed Binance USDT-perpetual candles for frozen-range events."""

    def __init__(self, settings: Settings, store: JsonStore | None = None):
        self.settings = settings
        self.store = store or JsonStore(settings.data_dir)

    @property
    def state_path(self) -> Path:
        value = getattr(
            self.settings,
            "consolidation_breakout_state_path",
            self.settings.data_dir / "consolidation_breakout_state.json",
        )
        return Path(value)

    def _empty_result(self, reason: str) -> dict[str, Any]:
        return {
            "template_id": TEMPLATE_ID,
            "events": [],
            "state_updates": [],
            "diagnostics": {
                "status": reason,
                "candidate_count": 0,
                "scanned_pairs": 0,
                "event_count": 0,
            },
        }

    def _load_state(self) -> dict[str, Any]:
        raw = self.store.load(self.state_path, {})
        if not isinstance(raw, dict) or raw.get("schema_version") != STATE_SCHEMA_VERSION:
            return {"schema_version": STATE_SCHEMA_VERSION, "tracks": {}}
        tracks = raw.get("tracks")
        if not isinstance(tracks, dict):
            return {"schema_version": STATE_SCHEMA_VERSION, "tracks": {}}
        return {"schema_version": STATE_SCHEMA_VERSION, "tracks": tracks}

    def _candidates(self, source: BinanceDataSource) -> list[str]:
        valid = {
            str(item.get("symbol") or "").upper()
            for item in source.usdt_perp_symbols()
            if isinstance(item, dict) and str(item.get("symbol") or "").upper().endswith("USDT")
        }
        excluded = {
            str(asset or "").upper()
            for asset in getattr(self.settings, "excluded_base_assets", ())
        }
        minimum = max(
            0.0,
            _to_float(getattr(self.settings, "consolidation_breakout_min_quote_volume", 5_000_000)),
        )
        rows: list[tuple[str, float]] = []
        for item in source.ticker_24h():
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            if symbol not in valid or not symbol.endswith("USDT"):
                continue
            if symbol[:-4] in excluded:
                continue
            quote_volume = _to_float(item.get("quoteVolume"))
            if quote_volume < minimum:
                continue
            rows.append((symbol, quote_volume))
        rows.sort(key=lambda item: (-item[1], item[0]))
        limit = max(0, int(getattr(self.settings, "consolidation_breakout_scan_limit", 24)))
        return [symbol for symbol, _volume in rows[:limit]]

    def build(
        self,
        source: BinanceDataSource,
        now_ms: int | None = None,
    ) -> dict[str, Any]:
        if not bool(getattr(self.settings, "consolidation_breakout_enable", False)):
            return self._empty_result("disabled")
        if int(getattr(self.settings, "consolidation_breakout_scan_limit", 24)) <= 0:
            return self._empty_result("scan_limit_zero")

        observed_now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
        close_delay_ms = max(
            0,
            int(getattr(self.settings, "consolidation_breakout_close_delay_sec", 90)),
        ) * 1000
        cutoff_ms = observed_now_ms - close_delay_ms
        timeframes = tuple(dict.fromkeys(
            str(value or "").strip().lower()
            for value in getattr(self.settings, "consolidation_breakout_timeframes", ("4h", "1d", "1w"))
            if _timeframe_ms(str(value or "")) > 0
        ))
        if not timeframes:
            return self._empty_result("no_valid_timeframes")

        try:
            symbols = self._candidates(source)
        except Exception as exc:
            result = self._empty_result("candidate_source_error")
            result["diagnostics"]["error"] = type(exc).__name__
            return result

        state = self._load_state()
        tracks = state.get("tracks", {})
        state_updates: list[dict[str, Any]] = []
        required_by_key: dict[str, list[str]] = {}
        events: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        scanned_pairs = 0
        closed_candles = 0
        suppressed_horizon_events = 0
        strong_ratio = max(
            0.01,
            _to_float(getattr(self.settings, "consolidation_breakout_strong_volume_ratio", 1.20), 1.20),
        )
        require_strong = bool(
            getattr(self.settings, "consolidation_breakout_require_strong_volume", False)
        )
        kline_limit = max(spec.length + spec.stability for spec in HORIZONS) + RETEST_BARS + 4

        for symbol in symbols:
            for timeframe in timeframes:
                interval_ms = _timeframe_ms(timeframe)
                try:
                    raw_klines = source.klines(symbol, interval=timeframe, limit=kline_limit)
                except Exception as exc:
                    errors.append({
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "error": type(exc).__name__,
                    })
                    continue
                parsed = [Candle.from_binance(row) for row in raw_klines]
                candles = sorted(
                    (candle for candle in parsed if candle is not None and candle.close_time <= cutoff_ms),
                    key=lambda candle: candle.close_time,
                )
                if not candles:
                    continue
                deduplicated: list[Candle] = []
                for candle in candles:
                    if deduplicated and deduplicated[-1].close_time == candle.close_time:
                        deduplicated[-1] = candle
                    else:
                        deduplicated.append(candle)
                candles = deduplicated
                scanned_pairs += 1
                closed_candles += len(candles)

                working: dict[str, dict[str, Any]] = {}
                pending_indices: set[int] = set()
                for spec in HORIZONS:
                    key = f"{symbol}|{timeframe}|{spec.name}"
                    existing = tracks.get(key, {}) if isinstance(tracks, dict) else {}
                    working[spec.name] = copy.deepcopy(existing) if isinstance(existing, dict) else {}
                    last_close = int(working[spec.name].get("last_close_time") or 0)
                    if last_close > 0:
                        pending_indices.update(
                            index
                            for index, candle in enumerate(candles)
                            if candle.close_time > last_close
                        )
                    else:
                        pending_indices.add(len(candles) - 1)

                for index in sorted(pending_indices):
                    candidates_on_bar: list[tuple[HorizonSpec, dict[str, Any]]] = []
                    processed_on_bar: list[tuple[HorizonSpec, str]] = []
                    for spec in HORIZONS:
                        track = working[spec.name]
                        last_close = int(track.get("last_close_time") or 0)
                        if candles[index].close_time <= last_close:
                            continue
                        key = f"{symbol}|{timeframe}|{spec.name}"
                        updated, raw_event = _step_track(
                            track,
                            candles,
                            index,
                            interval_ms,
                            spec,
                            strong_ratio,
                        )
                        working[spec.name] = updated
                        processed_on_bar.append((spec, key))
                        if raw_event is not None:
                            raw_event.update({
                                "schema": "range_breakout.v1",
                                "symbol": symbol,
                                "timeframe": timeframe,
                                "horizon": spec.name,
                                "horizon_label": spec.label,
                                "horizon_length": spec.length,
                                "event_time": raw_event["close_time"],
                            })
                            event_id = (
                                f"range_breakout.v1:{symbol}:{timeframe}:{spec.name}:"
                                f"{raw_event['event']}:{raw_event['close_time']}"
                            )
                            raw_event["event_id"] = event_id
                            raw_event["dedup_key"] = event_id
                            raw_event["score"] = _event_score(raw_event, spec, strong_ratio)
                            candidates_on_bar.append((spec, raw_event))

                    eligible = [
                        (spec, event)
                        for spec, event in candidates_on_bar
                        if not (
                            require_strong
                            and event["event"] in {"breakout_up", "breakout_down"}
                        )
                    ]
                    if eligible:
                        winner_spec, winner = max(
                            eligible,
                            key=lambda item: (
                                EVENT_PRIORITY.get(str(item[1].get("event") or ""), 0),
                                item[0].rank,
                            ),
                        )
                        winner["priority"] = EVENT_PRIORITY[str(winner["event"])]
                        winner["text"] = _format_event(winner)
                        events.append(winner)
                        key = f"{symbol}|{timeframe}|{winner_spec.name}"
                        required_by_key.setdefault(key, []).append(str(winner["event_id"]))
                        suppressed_horizon_events += max(0, len(candidates_on_bar) - 1)
                    else:
                        suppressed_horizon_events += len(candidates_on_bar)
                    for spec, key in processed_on_bar:
                        state_updates.append({
                            "key": key,
                            "state": copy.deepcopy(working[spec.name]),
                            "required_event_ids": list(required_by_key.get(key, [])),
                        })
        max_signals = max(
            0,
            int(getattr(self.settings, "consolidation_breakout_max_signals_per_scan", 8)),
        )
        outbound_events = events[:max_signals]
        withheld_event_ids = {
            str(event.get("event_id") or "") for event in events[max_signals:]
        }
        diagnostics: dict[str, Any] = {
            "status": "ok" if not errors else "degraded",
            "candidate_count": len(symbols),
            "timeframes": list(timeframes),
            "scanned_pairs": scanned_pairs,
            "closed_candles": closed_candles,
            "event_count": len(outbound_events),
            "withheld_event_count": len(withheld_event_ids),
            "suppressed_horizon_events": suppressed_horizon_events,
            "state_update_count": len(state_updates),
            "cutoff_ms": cutoff_ms,
        }
        source_diagnostics = getattr(source, "diagnostics", None)
        if callable(source_diagnostics):
            diagnostics["binance"] = source_diagnostics()
        if errors:
            diagnostics["errors"] = errors[:20]
        return {
            "template_id": TEMPLATE_ID,
            "events": outbound_events,
            "state_updates": state_updates,
            "diagnostics": diagnostics,
        }

    def commit(
        self,
        result: dict[str, Any],
        accepted_event_ids: Iterable[str] | None,
    ) -> dict[str, Any]:
        """Commit safe updates; unaccepted outbound events remain replayable."""

        accepted = {
            str(value) for value in (accepted_event_ids or ()) if str(value)
        }
        updates = [
            update
            for update in result.get("state_updates", [])
            if isinstance(update, dict)
            and str(update.get("key") or "")
            and isinstance(update.get("state"), dict)
        ]
        applicable: list[dict[str, Any]] = []
        deferred = 0
        for update in updates:
            required = {
                str(value)
                for value in update.get("required_event_ids", [])
                if str(value)
            }
            if required.issubset(accepted):
                applicable.append(update)
            else:
                deferred += 1
        if not applicable:
            return {
                "status": "deferred" if deferred else "no_changes",
                "applied": 0,
                "deferred": deferred,
            }

        def apply(current: Any) -> dict[str, Any]:
            if not isinstance(current, dict) or current.get("schema_version") != STATE_SCHEMA_VERSION:
                payload: dict[str, Any] = {
                    "schema_version": STATE_SCHEMA_VERSION,
                    "tracks": {},
                }
            else:
                payload = copy.deepcopy(current)
                if not isinstance(payload.get("tracks"), dict):
                    payload["tracks"] = {}
            for update in applicable:
                payload["tracks"][str(update["key"])] = copy.deepcopy(update["state"])
            payload["updated_at"] = int(time.time())
            return payload

        self.store.update(self.state_path, apply, {})
        return {
            "status": "ok",
            "applied": len(applicable),
            "deferred": deferred,
        }


__all__ = [
    "Candle",
    "ConsolidationBreakoutRadar",
    "HORIZONS",
    "TEMPLATE_ID",
    "count_touch_clusters",
]
