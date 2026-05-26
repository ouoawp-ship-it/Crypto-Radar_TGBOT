from __future__ import annotations

import math
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import Settings
from .data_sources import BinanceDataSource
from .flow_radar import coin_link, tg_escape, tg_quote
from .radar import fmt_price, pct_cell, to_float
from .storage import JsonStore
from .time_windows import CST, ClosedWindow, closed_window


SIGNAL_PRE_BREAKOUT_NEAR = "PRE_BREAKOUT_NEAR"
SIGNAL_PRE_BREAKDOWN_NEAR = "PRE_BREAKDOWN_NEAR"
SIGNAL_SQUEEZE_WATCH = "SQUEEZE_WATCH"
SIGNAL_BREAKOUT_CONFIRMED = "BREAKOUT_CONFIRMED"
SIGNAL_BREAKDOWN_CONFIRMED = "BREAKDOWN_CONFIRMED"
SIGNAL_FAKE_BREAKOUT = "FAKE_BREAKOUT"
SIGNAL_FAKE_BREAKDOWN = "FAKE_BREAKDOWN"
SIGNAL_BACK_INSIDE_BOX = "BACK_INSIDE_BOX"

PRE_SIGNAL_TYPES = {
    SIGNAL_PRE_BREAKOUT_NEAR,
    SIGNAL_PRE_BREAKDOWN_NEAR,
    SIGNAL_SQUEEZE_WATCH,
}

CONFIRM_SIGNAL_TYPES = {
    SIGNAL_BREAKOUT_CONFIRMED,
    SIGNAL_BREAKDOWN_CONFIRMED,
    SIGNAL_FAKE_BREAKOUT,
    SIGNAL_FAKE_BREAKDOWN,
    SIGNAL_BACK_INSIDE_BOX,
}

UP_SIGNAL_TYPES = {SIGNAL_PRE_BREAKOUT_NEAR, SIGNAL_BREAKOUT_CONFIRMED, SIGNAL_FAKE_BREAKDOWN}
DOWN_SIGNAL_TYPES = {SIGNAL_PRE_BREAKDOWN_NEAR, SIGNAL_BREAKDOWN_CONFIRMED, SIGNAL_FAKE_BREAKOUT}

SIGNAL_CN = {
    SIGNAL_PRE_BREAKOUT_NEAR: "临近上沿",
    SIGNAL_PRE_BREAKDOWN_NEAR: "临近下沿",
    SIGNAL_SQUEEZE_WATCH: "压缩观察",
    SIGNAL_BREAKOUT_CONFIRMED: "突破确认",
    SIGNAL_BREAKDOWN_CONFIRMED: "跌破确认",
    SIGNAL_FAKE_BREAKOUT: "假突破",
    SIGNAL_FAKE_BREAKDOWN: "假跌破",
    SIGNAL_BACK_INSIDE_BOX: "回到箱体",
}


@dataclass
class StructureSignal:
    symbol: str
    interval: str
    signal_type: str
    level: str
    score: float
    price: float
    box_high: float
    box_low: float
    box_width_pct: float
    position_in_box: float
    distance_to_high_pct: float
    distance_to_low_pct: float
    touch_high_count: int
    touch_low_count: int
    atr_pct: float | None
    atr_compressed: bool
    bb_width_pct: float | None
    bb_compressed: bool
    volume_ratio: float | None
    oi_change_pct_1h: float | None
    oi_change_pct_4h: float | None
    taker_buy_ratio: float | None
    reason_lines: list[str]
    chart_path: str | None = None


@dataclass
class BoxMetrics:
    box_high: float
    box_low: float
    box_mid: float
    box_width_pct: float
    position_in_box: float
    distance_to_high_pct: float
    distance_to_low_pct: float
    touch_high_count: int
    touch_low_count: int


@dataclass
class Candle:
    open_time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    close_time: int
    quote_volume: float
    taker_buy_base_volume: float | None
    taker_buy_quote_volume: float | None


def parse_interval_seconds(interval: str) -> int:
    value = str(interval or "").strip().lower()
    if not value:
        return 900
    unit = value[-1]
    try:
        amount = int(value[:-1])
    except ValueError:
        return 900
    if unit == "m":
        return max(60, amount * 60)
    if unit == "h":
        return max(3600, amount * 3600)
    if unit == "d":
        return max(86400, amount * 86400)
    return max(60, amount)


def next_structure_pre_epoch(now: float, minute: int = 55) -> float:
    local = datetime.fromtimestamp(now, CST)
    target = local.replace(minute=max(0, min(59, int(minute))), second=0, microsecond=0)
    if local >= target:
        target = target + timedelta(hours=1)
    return target.timestamp()


def next_structure_confirm_epoch(now: float, delay_sec: int = 300) -> float:
    local = datetime.fromtimestamp(now, CST)
    hour_start = local.replace(minute=0, second=0, microsecond=0)
    target = hour_start + timedelta(seconds=max(0, int(delay_sec)))
    if local >= target:
        target = hour_start + timedelta(hours=1, seconds=max(0, int(delay_sec)))
    return target.timestamp()


def candle_from_kline(raw: Any) -> Candle | None:
    if not isinstance(raw, (list, tuple)) or len(raw) < 8:
        return None
    try:
        return Candle(
            open_time=int(float(raw[0])),
            open=float(raw[1]),
            high=float(raw[2]),
            low=float(raw[3]),
            close=float(raw[4]),
            volume=float(raw[5]),
            close_time=int(float(raw[6])),
            quote_volume=float(raw[7]),
            taker_buy_base_volume=float(raw[9]) if len(raw) > 9 and raw[9] not in (None, "") else None,
            taker_buy_quote_volume=float(raw[10]) if len(raw) > 10 and raw[10] not in (None, "") else None,
        )
    except (TypeError, ValueError):
        return None


def normalize_candles(klines: list[Any]) -> list[Candle]:
    candles: list[Candle] = []
    for raw in klines:
        candle = candle_from_kline(raw)
        if candle and candle.close > 0 and candle.high >= candle.low > 0:
            candles.append(candle)
    candles.sort(key=lambda item: item.open_time)
    return candles


def pct_change(new: float, old: float) -> float | None:
    if old == 0:
        return None
    return (new - old) / old * 100


def calculate_box(
    candles: list[Candle],
    price: float | None = None,
    tolerance_pct: float = 1.0,
) -> BoxMetrics | None:
    if len(candles) < 6:
        return None
    highs = [c.high for c in candles if c.high > 0]
    lows = [c.low for c in candles if c.low > 0]
    if not highs or not lows:
        return None
    box_high = max(highs)
    box_low = min(lows)
    if box_high <= box_low:
        return None
    box_mid = (box_high + box_low) / 2
    if box_mid <= 0:
        return None
    current = price if price and price > 0 else candles[-1].close
    box_width_pct = (box_high - box_low) / box_mid * 100
    position = (current - box_low) / (box_high - box_low) * 100
    position = max(0.0, min(100.0, position))
    distance_to_high_pct = (box_high - current) / current * 100 if current > 0 else 0.0
    distance_to_low_pct = (current - box_low) / current * 100 if current > 0 else 0.0
    tolerance = max(0.05, float(tolerance_pct)) / 100
    touch_high_count = sum(1 for c in candles if abs(c.high - box_high) / box_high <= tolerance)
    touch_low_count = sum(1 for c in candles if abs(c.low - box_low) / box_low <= tolerance)
    return BoxMetrics(
        box_high=box_high,
        box_low=box_low,
        box_mid=box_mid,
        box_width_pct=box_width_pct,
        position_in_box=position,
        distance_to_high_pct=distance_to_high_pct,
        distance_to_low_pct=distance_to_low_pct,
        touch_high_count=touch_high_count,
        touch_low_count=touch_low_count,
    )


def true_ranges(candles: list[Candle]) -> list[float]:
    ranges: list[float] = []
    previous_close: float | None = None
    for candle in candles:
        if previous_close is None:
            ranges.append(candle.high - candle.low)
        else:
            ranges.append(max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            ))
        previous_close = candle.close
    return ranges


def calculate_atr_pct(candles: list[Candle], period: int = 14) -> tuple[float | None, bool]:
    if len(candles) < max(3, period + 1):
        return None, False
    ranges = true_ranges(candles)
    recent = ranges[-period:]
    if not recent:
        return None, False
    atr = sum(recent) / len(recent)
    close = candles[-1].close
    if close <= 0:
        return None, False
    atr_pct = atr / close * 100
    baseline = sum(ranges) / len(ranges) if ranges else atr
    compressed = baseline > 0 and (atr <= baseline * 0.78 or atr_pct <= 1.5)
    return atr_pct, compressed


def calculate_bb_width_pct(candles: list[Candle], period: int = 20, stdev_mult: float = 2.0) -> tuple[float | None, bool]:
    if len(candles) < max(5, period):
        return None, False
    closes = [c.close for c in candles]
    window = closes[-period:]
    sma = sum(window) / len(window)
    if sma <= 0:
        return None, False
    variance = sum((value - sma) ** 2 for value in window) / len(window)
    stdev = math.sqrt(variance)
    width_pct = (stdev_mult * stdev * 2) / sma * 100

    widths: list[float] = []
    for idx in range(period, len(closes) + 1):
        item = closes[idx - period:idx]
        avg = sum(item) / len(item)
        if avg <= 0:
            continue
        var = sum((value - avg) ** 2 for value in item) / len(item)
        widths.append((stdev_mult * math.sqrt(var) * 2) / avg * 100)
    baseline = sum(widths) / len(widths) if widths else width_pct
    compressed = baseline > 0 and (width_pct <= baseline * 0.8 or width_pct <= 5.0)
    return width_pct, compressed


def calculate_volume_ratio(candles: list[Candle], period: int = 20) -> float | None:
    if len(candles) < 3:
        return None
    current = candles[-1].quote_volume or candles[-1].volume
    history = [
        c.quote_volume or c.volume
        for c in candles[-(period + 1):-1]
        if (c.quote_volume or c.volume) > 0
    ]
    if not history:
        return None
    avg = sum(history) / len(history)
    if avg <= 0:
        return None
    return current / avg


def calculate_taker_buy_ratio(candle: Candle) -> float | None:
    if candle.taker_buy_quote_volume is not None and candle.quote_volume > 0:
        return max(0.0, min(1.0, candle.taker_buy_quote_volume / candle.quote_volume))
    if candle.taker_buy_base_volume is not None and candle.volume > 0:
        return max(0.0, min(1.0, candle.taker_buy_base_volume / candle.volume))
    return None


def calculate_oi_changes(oi_rows: list[dict[str, Any]], interval: str) -> tuple[float | None, float | None]:
    if len(oi_rows) < 2:
        return None, None
    rows = sorted(oi_rows, key=lambda item: int(to_float(item.get("timestamp"))))
    values = [
        to_float(row.get("sumOpenInterestValue") or row.get("sumOpenInterest"))
        for row in rows
    ]
    values = [value for value in values if value > 0]
    if len(values) < 2:
        return None, None
    interval_sec = parse_interval_seconds(interval)
    one_h_steps = max(1, int(round(3600 / interval_sec)))
    four_h_steps = max(one_h_steps, int(round(4 * 3600 / interval_sec)))

    def change(steps: int) -> float | None:
        if len(values) <= steps:
            return pct_change(values[-1], values[0])
        return pct_change(values[-1], values[-1 - steps])

    return change(one_h_steps), change(four_h_steps)


def score_level(score: float) -> str:
    if score >= 85:
        return "S"
    if score >= 70:
        return "A"
    if score >= 60:
        return "B"
    if score >= 50:
        return "C"
    return "D"


def structure_window(settings: Settings, interval: str, mode: str) -> ClosedWindow:
    delay = settings.structure_confirm_delay_sec if mode == "confirm" else 0
    return closed_window(interval_sec=parse_interval_seconds(interval), delay_sec=delay)


def funding_map(source: BinanceDataSource) -> dict[str, float]:
    result: dict[str, float] = {}
    for item in source.premium_index():
        symbol = str(item.get("symbol") or "").upper()
        if not symbol:
            continue
        result[symbol] = to_float(item.get("lastFundingRate")) * 100
    return result


class StructureRadarEngine:
    def __init__(self, settings: Settings, store: JsonStore):
        self.settings = settings
        self.store = store

    def build(
        self,
        source: BinanceDataSource,
        *,
        mode: str = "pre",
        top_symbols: int | None = None,
        min_score: float | None = None,
        interval: str | None = None,
        save_charts: bool | None = None,
    ) -> dict[str, Any]:
        mode = "confirm" if mode == "confirm" else "pre"
        interval = interval or self.settings.structure_interval
        top_symbols = self.settings.structure_top_symbols if top_symbols is None else max(1, int(top_symbols))
        min_score = self.settings.structure_min_score if min_score is None else float(min_score)
        save_charts = self.settings.structure_save_charts if save_charts is None else bool(save_charts)

        window = structure_window(self.settings, interval, mode)
        state = self._load_state()
        fundings = funding_map(source)
        candidates = self._candidates(source, top_symbols)
        signals: list[StructureSignal] = []
        all_signal_records: list[StructureSignal] = []
        candle_map: dict[str, list[Candle]] = {}

        for candidate in candidates:
            symbol = str(candidate.get("symbol") or "").upper()
            analyzed = self._analyze_symbol(source, symbol, interval, mode, window, state, fundings)
            if not analyzed:
                continue
            signal, candles = analyzed
            all_signal_records.append(signal)
            if signal.score >= min_score and self._cooldown_ok(state, signal):
                signals.append(signal)
                candle_map[signal.symbol] = candles

        signals.sort(key=lambda item: item.score, reverse=True)
        if save_charts and signals:
            self._generate_charts(signals, candle_map)

        self._save_state(state, all_signal_records)
        self._append_history(mode, interval, window, candidates, signals)
        text = self._format(signals, len(candidates), interval, mode, window, source.diagnostics())
        return {
            "template_id": "TG_STRUCTURE_RADAR",
            "dedup_key": f"structure:{mode}:{interval}:{int(window.end_ms / 1000)}",
            "text": text,
            "signals": [asdict(signal) for signal in signals],
            "signal_objects": signals,
            "chart_paths": [signal.chart_path for signal in signals if signal.chart_path],
            "diagnostics": source.diagnostics(),
            "mode": mode,
            "window": {
                "start_ms": window.start_ms,
                "end_ms": window.end_ms,
                "label": window.label(),
            },
        }

    def mark_pushed(self, signals: list[StructureSignal], message_ids: list[int] | None = None) -> None:
        if not signals:
            return
        state = self._load_state()
        now = int(time.time())
        first_message_id = int(message_ids[0]) if message_ids else 0
        pushed_at = datetime.now(CST).isoformat()
        for signal in signals:
            record = state.get(signal.symbol, {})
            if not isinstance(record, dict):
                record = {}
            record["last_pushed"] = now
            record["last_pushed_signal"] = signal.signal_type
            record["last_pushed_score"] = round(signal.score, 2)
            record["last_pushed_at"] = pushed_at
            if first_message_id > 0:
                record["last_message_id"] = first_message_id
                record["last_message_ids"] = message_ids or []
                record["last_message_signal_type"] = signal.signal_type
                record["last_message_score"] = round(signal.score, 2)
                record["last_message_price"] = signal.price
                record["last_message_box_high"] = signal.box_high
                record["last_message_box_low"] = signal.box_low
            state[signal.symbol] = record
        self.store.save(self.settings.structure_state_path, state)

    def _candidates(self, source: BinanceDataSource, limit: int) -> list[dict[str, Any]]:
        valid_symbols = {
            str(item.get("symbol") or "").upper()
            for item in source.usdt_perp_symbols()
            if item.get("symbol")
        }
        rows: list[dict[str, Any]] = []
        for ticker in source.ticker_24h():
            symbol = str(ticker.get("symbol") or "").upper()
            if not symbol.endswith("USDT"):
                continue
            if valid_symbols and symbol not in valid_symbols:
                continue
            coin = symbol[:-4]
            if coin in set(self.settings.excluded_base_assets):
                continue
            quote_volume = to_float(ticker.get("quoteVolume"))
            if quote_volume < self.settings.radar_min_quote_volume:
                continue
            rows.append({
                "symbol": symbol,
                "quote_volume": quote_volume,
                "price_24h": to_float(ticker.get("priceChangePercent")),
                "last_price": to_float(ticker.get("lastPrice")),
            })
        rows.sort(key=lambda item: item["quote_volume"], reverse=True)
        return rows[:limit]

    def _analyze_symbol(
        self,
        source: BinanceDataSource,
        symbol: str,
        interval: str,
        mode: str,
        window: ClosedWindow,
        state: dict[str, Any],
        fundings: dict[str, float],
    ) -> tuple[StructureSignal, list[Candle]] | None:
        limit = max(self.settings.structure_box_lookback + 24, 64)
        raw_klines = source.klines(symbol, interval=interval, limit=limit, end_time=window.end_ms - 1)
        candles = normalize_candles(raw_klines)
        if len(candles) < self.settings.structure_box_lookback + 2:
            return None
        latest = candles[-1]
        box_source = candles[-(self.settings.structure_box_lookback + 1):-1]
        box = calculate_box(box_source, latest.close, self.settings.structure_near_edge_pct)
        if not box:
            return None

        atr_pct, atr_compressed = calculate_atr_pct(candles)
        bb_width_pct, bb_compressed = calculate_bb_width_pct(candles)
        volume_ratio = calculate_volume_ratio(candles)
        taker_buy_ratio = calculate_taker_buy_ratio(latest)
        oi_rows = source.open_interest_hist(
            symbol,
            period=interval,
            limit=max(20, self.settings.structure_box_lookback),
            end_time=window.end_ms - 1,
        )
        oi_change_pct_1h, oi_change_pct_4h = calculate_oi_changes(oi_rows, interval)
        funding_pct = fundings.get(symbol)
        higher_bias = self._higher_timeframe_bias(source, symbol)
        signal_type = self._classify_signal(
            mode,
            latest,
            box,
            atr_compressed,
            bb_compressed,
            volume_ratio,
            oi_change_pct_1h,
            taker_buy_ratio,
            state.get(symbol, {}) if isinstance(state.get(symbol), dict) else {},
        )
        if not signal_type:
            return None
        score, reasons = self._score(
            signal_type,
            box,
            atr_compressed,
            bb_compressed,
            volume_ratio,
            oi_change_pct_1h,
            taker_buy_ratio,
            higher_bias,
            funding_pct,
        )
        if score <= 0:
            return None
        signal = StructureSignal(
            symbol=symbol,
            interval=interval,
            signal_type=signal_type,
            level=score_level(score),
            score=round(score, 2),
            price=latest.close,
            box_high=box.box_high,
            box_low=box.box_low,
            box_width_pct=box.box_width_pct,
            position_in_box=box.position_in_box,
            distance_to_high_pct=box.distance_to_high_pct,
            distance_to_low_pct=box.distance_to_low_pct,
            touch_high_count=box.touch_high_count,
            touch_low_count=box.touch_low_count,
            atr_pct=atr_pct,
            atr_compressed=atr_compressed,
            bb_width_pct=bb_width_pct,
            bb_compressed=bb_compressed,
            volume_ratio=volume_ratio,
            oi_change_pct_1h=oi_change_pct_1h,
            oi_change_pct_4h=oi_change_pct_4h,
            taker_buy_ratio=taker_buy_ratio,
            reason_lines=reasons,
        )
        return signal, candles

    def _higher_timeframe_bias(self, source: BinanceDataSource, symbol: str) -> int:
        if source.budget.used.get("klines", 0) >= source.budget.limits.get("klines", 0):
            return 0
        rows = source.klines(symbol, interval=self.settings.structure_higher_interval, limit=6)
        candles = normalize_candles(rows)
        if len(candles) < 3:
            return 0
        first = candles[0].close
        last = candles[-1].close
        if first <= 0:
            return 0
        change = (last - first) / first * 100
        if change > 1.5:
            return 1
        if change < -1.5:
            return -1
        return 0

    def _classify_signal(
        self,
        mode: str,
        latest: Candle,
        box: BoxMetrics,
        atr_compressed: bool,
        bb_compressed: bool,
        volume_ratio: float | None,
        oi_change_pct_1h: float | None,
        taker_buy_ratio: float | None,
        previous: dict[str, Any],
    ) -> str | None:
        near_edge = max(0.1, self.settings.structure_near_edge_pct)
        compression = atr_compressed or bb_compressed
        activity = (volume_ratio is not None and volume_ratio >= 1.15) or (oi_change_pct_1h is not None and oi_change_pct_1h >= 2.0)
        buy_ok = taker_buy_ratio is None or taker_buy_ratio >= 0.48
        sell_ok = taker_buy_ratio is None or taker_buy_ratio <= 0.52
        inside_box = box.box_low <= latest.close <= box.box_high

        if mode == "pre":
            if (
                box.distance_to_high_pct <= near_edge
                and box.position_in_box >= 55
                and compression
                and activity
                and buy_ok
            ):
                return SIGNAL_PRE_BREAKOUT_NEAR
            if (
                box.distance_to_low_pct <= near_edge
                and box.position_in_box <= 45
                and compression
                and activity
                and sell_ok
            ):
                return SIGNAL_PRE_BREAKDOWN_NEAR
            if inside_box and compression and box.box_width_pct <= 14 and activity:
                return SIGNAL_SQUEEZE_WATCH
            return None

        previous_type = str(previous.get("last_signal_type") or previous.get("last_pushed_signal") or "")
        broke_up = latest.close > box.box_high
        broke_down = latest.close < box.box_low
        if broke_up and (volume_ratio is None or volume_ratio >= 0.9) and buy_ok:
            return SIGNAL_BREAKOUT_CONFIRMED
        if broke_down and (volume_ratio is None or volume_ratio >= 0.9) and sell_ok:
            return SIGNAL_BREAKDOWN_CONFIRMED
        if previous_type in {SIGNAL_PRE_BREAKOUT_NEAR, SIGNAL_BREAKOUT_CONFIRMED} and inside_box:
            return SIGNAL_FAKE_BREAKOUT
        if previous_type in {SIGNAL_PRE_BREAKDOWN_NEAR, SIGNAL_BREAKDOWN_CONFIRMED} and inside_box:
            return SIGNAL_FAKE_BREAKDOWN
        if previous_type in {SIGNAL_BREAKOUT_CONFIRMED, SIGNAL_BREAKDOWN_CONFIRMED} and inside_box:
            return SIGNAL_BACK_INSIDE_BOX
        return None

    def _score(
        self,
        signal_type: str,
        box: BoxMetrics,
        atr_compressed: bool,
        bb_compressed: bool,
        volume_ratio: float | None,
        oi_change_pct_1h: float | None,
        taker_buy_ratio: float | None,
        higher_bias: int,
        funding_pct: float | None,
    ) -> tuple[float, list[str]]:
        reasons: list[str] = []
        score = 0.0

        if signal_type in UP_SIGNAL_TYPES:
            edge_distance = box.distance_to_high_pct
            edge_name = "上沿"
        elif signal_type in DOWN_SIGNAL_TYPES:
            edge_distance = box.distance_to_low_pct
            edge_name = "下沿"
        else:
            edge_distance = min(box.distance_to_high_pct, box.distance_to_low_pct)
            edge_name = "箱体边缘"
        if edge_distance <= self.settings.structure_near_edge_pct:
            score += 20
            reasons.append(f"价格距离{edge_name}{edge_distance:.2f}%")
        elif signal_type in CONFIRM_SIGNAL_TYPES and edge_distance < 0:
            score += 20
            reasons.append(f"收盘已越过{edge_name}{abs(edge_distance):.2f}%")
        elif edge_distance <= self.settings.structure_near_edge_pct * 1.8:
            score += 12
            reasons.append(f"接近{edge_name}{edge_distance:.2f}%")

        if box.box_width_pct <= 8:
            score += 15
            reasons.append(f"箱体较窄{box.box_width_pct:.2f}%")
        elif box.box_width_pct <= 14:
            score += 10
            reasons.append(f"箱体宽度{box.box_width_pct:.2f}%")
        elif box.box_width_pct <= 22:
            score += 4
            reasons.append(f"箱体偏宽{box.box_width_pct:.2f}%")

        touch_count = box.touch_high_count + box.touch_low_count
        if touch_count >= 4:
            score += 10
            reasons.append(f"上下沿触碰{touch_count}次")
        elif touch_count >= 2:
            score += 6
            reasons.append(f"箱体触碰{touch_count}次")

        if atr_compressed and bb_compressed:
            score += 15
            reasons.append("ATR与BB同时压缩")
        elif atr_compressed or bb_compressed:
            score += 10
            reasons.append("ATR或BB压缩")

        if volume_ratio is not None:
            if volume_ratio >= 1.8:
                score += 10
                reasons.append(f"量能{volume_ratio:.2f}x")
            elif volume_ratio >= 1.15:
                score += 6
                reasons.append(f"量能{volume_ratio:.2f}x")

        if oi_change_pct_1h is not None:
            oi_abs = abs(oi_change_pct_1h)
            if oi_abs >= 8:
                score += 10
                reasons.append(f"1h OI{oi_change_pct_1h:+.1f}%")
            elif oi_abs >= 2:
                score += 6
                reasons.append(f"1h OI{oi_change_pct_1h:+.1f}%")

        if taker_buy_ratio is not None:
            if signal_type in UP_SIGNAL_TYPES and taker_buy_ratio >= 0.55:
                score += 10
                reasons.append(f"主动买入{taker_buy_ratio:.0%}")
            elif signal_type in DOWN_SIGNAL_TYPES and taker_buy_ratio <= 0.45:
                score += 10
                reasons.append(f"主动卖出{1 - taker_buy_ratio:.0%}")
            elif 0.48 <= taker_buy_ratio <= 0.52:
                score += 4
                reasons.append("主动买卖中性")
        else:
            reasons.append("主动买卖缺失")

        if signal_type in UP_SIGNAL_TYPES and higher_bias > 0:
            score += 5
            reasons.append("高周期偏多")
        elif signal_type in DOWN_SIGNAL_TYPES and higher_bias < 0:
            score += 5
            reasons.append("高周期偏空")
        elif higher_bias == 0:
            score += 2

        if funding_pct is None:
            score += 2
        elif abs(funding_pct) <= 0.08:
            score += 5
            reasons.append("费率未极端")
        elif abs(funding_pct) <= 0.20:
            score += 3
            reasons.append("费率可接受")
        else:
            reasons.append(f"费率偏热{funding_pct:+.3f}%")

        if signal_type in {SIGNAL_BREAKOUT_CONFIRMED, SIGNAL_BREAKDOWN_CONFIRMED}:
            score += 5
        if signal_type in {SIGNAL_FAKE_BREAKOUT, SIGNAL_FAKE_BREAKDOWN}:
            score += 8

        return min(100.0, score), reasons[:8]

    def _cooldown_ok(self, state: dict[str, Any], signal: StructureSignal) -> bool:
        record = state.get(signal.symbol)
        if not isinstance(record, dict):
            return True
        last_pushed = int(record.get("last_pushed", 0) or 0)
        last_type = str(record.get("last_pushed_signal") or "")
        if not last_pushed or last_type != signal.signal_type:
            return True
        return time.time() - last_pushed >= self.settings.structure_cooldown_sec

    def _load_state(self) -> dict[str, Any]:
        data = self.store.load(self.settings.structure_state_path, {})
        return data if isinstance(data, dict) else {}

    def _save_state(self, state: dict[str, Any], signals: list[StructureSignal]) -> None:
        now = int(time.time())
        for signal in signals:
            previous = state.get(signal.symbol, {})
            if not isinstance(previous, dict):
                previous = {}
            previous.update({
                "symbol": signal.symbol,
                "last_signal_type": signal.signal_type,
                "last_seen": now,
                "last_score": signal.score,
                "last_price": signal.price,
                "box_high": signal.box_high,
                "box_low": signal.box_low,
                "level": signal.level,
            })
            state[signal.symbol] = previous
        self.store.save(self.settings.structure_state_path, state)

    def _append_history(
        self,
        mode: str,
        interval: str,
        window: ClosedWindow,
        candidates: list[dict[str, Any]],
        signals: list[StructureSignal],
    ) -> None:
        self.store.append_record(
            self.settings.structure_history_path,
            {
                "ts": int(time.time()),
                "time": datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S CST"),
                "mode": mode,
                "interval": interval,
                "window": window.label(),
                "candidates": len(candidates),
                "signals": len(signals),
                "top_symbols": [signal.symbol for signal in signals[:10]],
                "top_score": signals[0].score if signals else 0,
            },
            limit=2000,
        )

    def _generate_charts(self, signals: list[StructureSignal], candle_map: dict[str, list[Candle]]) -> None:
        try:
            from .charts import generate_structure_chart
        except Exception:
            return
        chart_dir = self.settings.structure_chart_dir
        for signal in signals[: max(0, self.settings.structure_send_chart_top_n)]:
            candles = candle_map.get(signal.symbol)
            if not candles:
                continue
            try:
                signal.chart_path = generate_structure_chart(signal, candles, chart_dir)
            except Exception:
                signal.chart_path = None

    def _format(
        self,
        signals: list[StructureSignal],
        candidates_count: int,
        interval: str,
        mode: str,
        window: ClosedWindow,
        diagnostics: dict[str, Any],
    ) -> str:
        title = "结构突破雷达"
        mode_text = "提前临界" if mode == "pre" else "收线确认"
        lines = [
            f"🧱 <b>{title}</b>",
            f"⏰ {datetime.now(CST).strftime('%m-%d %H:%M CST')}",
            f"窗口: {tg_escape(window.label())} | 周期: {tg_escape(interval)} | 模式: {mode_text}",
            "",
            "📊 <b>本轮统计</b>",
            f"候选币: {candidates_count}",
            f"入选信号: {len(signals)}",
            f"K线请求: {diagnostics.get('budget', {}).get('klines', {}).get('used', 0)} / {diagnostics.get('budget', {}).get('klines', {}).get('limit', 0)}",
            f"OI请求: {diagnostics.get('budget', {}).get('open_interest_hist', {}).get('used', 0)} / {diagnostics.get('budget', {}).get('open_interest_hist', {}).get('limit', 0)}",
            "",
        ]
        if not signals:
            lines.append("本轮没有达到推送分数线的结构信号。")
            lines.append("")
        groups = self._group_signals(signals)
        for group_title, group_items in groups:
            if not group_items:
                continue
            lines.append(tg_quote(group_title))
            for signal in group_items:
                lines.append(coin_link(signal.symbol))
                lines.append(
                    f"{signal.level}级 {signal.score:.0f}分 | "
                    f"价 {fmt_price(signal.price)} | "
                    f"上沿 {fmt_price(signal.box_high)} | 下沿 {fmt_price(signal.box_low)}"
                )
                lines.append(
                    f"距上 {signal.distance_to_high_pct:+.2f}% | "
                    f"距下 {signal.distance_to_low_pct:+.2f}% | "
                    f"箱宽 {signal.box_width_pct:.2f}% | 位置 {signal.position_in_box:.0f}%"
                )
                lines.append(
                    f"ATR {self._optional_pct(signal.atr_pct)} | "
                    f"BB {self._optional_pct(signal.bb_width_pct)} | "
                    f"量 {self._optional_ratio(signal.volume_ratio)} | "
                    f"OI1h {self._optional_pct(signal.oi_change_pct_1h)} | "
                    f"主动买 {self._optional_ratio(signal.taker_buy_ratio, percent=True)}"
                )
                if signal.reason_lines:
                    lines.append("原因: " + "；".join(tg_escape(item) for item in signal.reason_lines[:4]))
                if signal.chart_path:
                    lines.append("图表: 已生成")
                lines.append("")
        lines.extend([
            "📖 <b>图例</b>",
            "临近上沿/下沿 = 每小时55分附近提前预警，只代表接近关键位。",
            "突破确认/跌破确认 = 整点收线后延迟确认，使用完整闭合K线。",
            "假突破/假跌破 = 之前出现临界或突破信号，后续收回箱体内。",
            "评分 = 边缘距离20 + 结构15 + 触碰10 + 压缩15 + 量10 + OI10 + 主动买卖10 + 高周期5 + 费率5。",
            "等级 = S≥85，A≥70，B≥60，C≥50；默认低于配置分数线不推送。",
        ])
        return "\n".join(lines)

    @staticmethod
    def _group_signals(signals: list[StructureSignal]) -> list[tuple[str, list[StructureSignal]]]:
        order = [
            ("提前向上临界", {SIGNAL_PRE_BREAKOUT_NEAR}),
            ("提前向下临界", {SIGNAL_PRE_BREAKDOWN_NEAR}),
            ("压缩观察", {SIGNAL_SQUEEZE_WATCH}),
            ("收线突破确认", {SIGNAL_BREAKOUT_CONFIRMED}),
            ("收线跌破确认", {SIGNAL_BREAKDOWN_CONFIRMED}),
            ("假突破/假跌破", {SIGNAL_FAKE_BREAKOUT, SIGNAL_FAKE_BREAKDOWN, SIGNAL_BACK_INSIDE_BOX}),
        ]
        result: list[tuple[str, list[StructureSignal]]] = []
        for title, allowed in order:
            items = [signal for signal in signals if signal.signal_type in allowed]
            result.append((title, items))
        return result

    @staticmethod
    def _optional_pct(value: float | None) -> str:
        if value is None:
            return "缺失"
        return pct_cell(value)

    @staticmethod
    def _optional_ratio(value: float | None, *, percent: bool = False) -> str:
        if value is None:
            return "缺失"
        if percent:
            return f"{value:.0%}"
        return f"{value:.2f}x"
