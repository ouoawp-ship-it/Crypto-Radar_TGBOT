from __future__ import annotations

import time
from dataclasses import asdict
from datetime import datetime
from typing import Any

from .config import Settings
from .data_sources import BinanceDataSource
from .storage import JsonStore
from .structure_radar import (
    DOWN_SIGNAL_TYPES,
    SIGNAL_BREAKDOWN_CONFIRMED,
    SIGNAL_BREAKOUT_CONFIRMED,
    SIGNAL_FAKE_BREAKDOWN,
    SIGNAL_FAKE_BREAKOUT,
    SIGNAL_PRE_BREAKDOWN_NEAR,
    SIGNAL_PRE_BREAKOUT_NEAR,
    SIGNAL_SQUEEZE_WATCH,
    UP_SIGNAL_TYPES,
    StructureSignal,
    normalize_candles,
    parse_interval_seconds,
)
from .time_windows import CST


HORIZONS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
}


def structure_signal_direction(signal_type: str) -> str:
    if signal_type in UP_SIGNAL_TYPES:
        return "up"
    if signal_type in DOWN_SIGNAL_TYPES:
        return "down"
    return "squeeze"


def review_record_id(symbol: str, signal_type: str, interval: str, signal_ts: int) -> str:
    return f"{symbol}:{signal_type}:{interval}:{int(signal_ts)}"


def pct_change(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return (new - old) / old * 100


def mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


class StructureReviewEngine:
    def __init__(self, settings: Settings, store: JsonStore):
        self.settings = settings
        self.store = store

    def record_signals(
        self,
        signals: list[StructureSignal],
        *,
        mode: str,
        window: dict[str, Any] | None = None,
        push_status: str = "",
    ) -> int:
        if not self.settings.structure_review_enable or not signals:
            return 0
        records = self._load_records()
        existing = {str(record.get("id")): record for record in records if isinstance(record, dict)}
        signal_ts = int(time.time())
        if isinstance(window, dict) and int(window.get("end_ms", 0) or 0) > 0:
            signal_ts = int(int(window.get("end_ms", 0)) / 1000)
        added = 0
        for signal in signals:
            record_id = review_record_id(signal.symbol, signal.signal_type, signal.interval, signal_ts)
            if record_id in existing:
                continue
            liquidity_context = signal.liquidity_context
            existing[record_id] = {
                "id": record_id,
                "symbol": signal.symbol,
                "interval": signal.interval,
                "signal_type": signal.signal_type,
                "direction": structure_signal_direction(signal.signal_type),
                "level": signal.level,
                "score": signal.score,
                "base_score": signal.base_score if signal.base_score is not None else signal.score,
                "liquidity_score_delta": signal.liquidity_score_delta,
                "final_score": signal.final_score if signal.final_score is not None else signal.score,
                "liquidation_bias": liquidity_context.liquidation_bias if liquidity_context else "unavailable",
                "orderbook_bias": liquidity_context.orderbook_bias if liquidity_context else "unavailable",
                "coinglass_available": bool(liquidity_context.available) if liquidity_context else False,
                "price": signal.price,
                "box_high": signal.box_high,
                "box_low": signal.box_low,
                "box_width_pct": signal.box_width_pct,
                "position_in_box": signal.position_in_box,
                "signal_ts": signal_ts,
                "signal_time": datetime.fromtimestamp(signal_ts, CST).isoformat(),
                "mode": mode,
                "window": window or {},
                "push_status": push_status,
                "status": "pending",
                "outcome": "pending",
                "metrics": {},
                "signal": asdict(signal),
                "created_at": int(time.time()),
                "updated_at": int(time.time()),
            }
            added += 1
        if added:
            self._save_records(list(existing.values()))
        return added

    def update(
        self,
        source: BinanceDataSource,
        *,
        lookback_hours: int | None = None,
    ) -> dict[str, Any]:
        lookback = max(1, int(lookback_hours or self.settings.structure_review_lookback_hours))
        now = int(time.time())
        records = self._load_records()
        cutoff = now - lookback * 3600
        changed = False
        for record in records:
            if not isinstance(record, dict):
                continue
            signal_ts = int(record.get("signal_ts", 0) or 0)
            if signal_ts <= 0 or signal_ts < cutoff - self.settings.structure_review_forward_hours * 3600:
                continue
            if now - signal_ts < max(1, self.settings.structure_review_min_age_minutes) * 60:
                record["status"] = "pending"
                record["outcome"] = "pending"
                continue
            before = dict(record)
            self._review_record(source, record, now)
            changed = changed or before != record
        if changed:
            self._save_records(records)
        selected = [
            record for record in records
            if isinstance(record, dict) and int(record.get("signal_ts", 0) or 0) >= cutoff
        ]
        stats = self.aggregate(selected)
        stats["lookback_hours"] = lookback
        stats["updated_at"] = now
        stats["updated_at_text"] = datetime.fromtimestamp(now, CST).strftime("%Y-%m-%d %H:%M:%S CST")
        stats["suggestions"] = self.suggestions(stats)
        self.store.save(self.settings.structure_stats_path, stats)
        text = self.format_report(stats, selected)
        self.settings.structure_review_report_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.structure_review_report_path.write_text(text, encoding="utf-8")
        return {
            "template_id": "TG_STRUCTURE_REVIEW",
            "dedup_key": f"structure-review:{lookback}:{now // max(60, self.settings.structure_review_max_report_interval_sec)}",
            "text": text,
            "report_path": str(self.settings.structure_review_report_path),
            "stats": stats,
            "records": selected,
        }

    def _review_record(self, source: BinanceDataSource, record: dict[str, Any], now: int) -> None:
        symbol = str(record.get("symbol") or "")
        interval = str(record.get("interval") or self.settings.structure_interval)
        signal_ts = int(record.get("signal_ts", 0) or 0)
        entry = float(record.get("price", 0) or 0)
        if not symbol or signal_ts <= 0 or entry <= 0:
            return
        forward_sec = max(15 * 60, int(self.settings.structure_review_forward_hours) * 3600)
        interval_sec = parse_interval_seconds(interval)
        limit = max(10, min(1000, int(forward_sec / interval_sec) + 8))
        rows = source.klines(
            symbol,
            interval=interval,
            limit=limit,
            start_time=signal_ts * 1000,
            end_time=(signal_ts + forward_sec) * 1000,
        )
        candles = [
            candle for candle in normalize_candles(rows)
            if candle.close_time >= signal_ts * 1000
        ]
        if not candles:
            record["status"] = "pending"
            record["outcome"] = "pending"
            record["updated_at"] = now
            return

        metrics: dict[str, Any] = {}
        for label, horizon in HORIZONS.items():
            close = self._close_at(candles, signal_ts + horizon)
            metrics[f"price_change_{label}"] = pct_change(close, entry)
        box_high = float(record.get("box_high", 0) or 0)
        box_low = float(record.get("box_low", 0) or 0)
        direction = str(record.get("direction") or structure_signal_direction(str(record.get("signal_type") or "")))
        highs = [candle.high for candle in candles]
        lows = [candle.low for candle in candles]
        closes = [candle.close for candle in candles]
        max_high = max(highs) if highs else entry
        min_low = min(lows) if lows else entry
        broke_up = box_high > 0 and max_high > box_high
        broke_down = box_low > 0 and min_low < box_low
        back_inside = bool(
            box_high > box_low > 0
            and any(box_low <= close <= box_high for close in closes[1:])
            and (broke_up or broke_down)
        )
        fake_up = bool(broke_up and back_inside and direction in {"up", "squeeze"})
        fake_down = bool(broke_down and back_inside and direction in {"down", "squeeze"})

        if direction == "down":
            mfe = (entry - min_low) / entry * 100
            mae = (entry - max_high) / entry * 100
        else:
            mfe = (max_high - entry) / entry * 100
            mae = (min_low - entry) / entry * 100
        metrics.update({
            "broke_box_high": broke_up,
            "broke_box_low": broke_down,
            "back_inside_box": back_inside,
            "fake_breakout": fake_up,
            "fake_breakdown": fake_down,
            "mfe_pct": mfe,
            "mae_pct": mae,
        })
        outcome = "pending"
        if fake_up:
            outcome = "fake_breakout"
        elif fake_down:
            outcome = "fake_breakdown"
        elif direction == "up" and broke_up:
            outcome = "valid_breakout"
        elif direction == "down" and broke_down:
            outcome = "valid_breakdown"
        elif direction == "squeeze" and broke_up:
            outcome = "valid_breakout"
        elif direction == "squeeze" and broke_down:
            outcome = "valid_breakdown"

        completed = now - signal_ts >= forward_sec
        if outcome == "pending" and completed:
            outcome = "invalid_range"
        record["metrics"] = metrics
        record["outcome"] = outcome
        record["status"] = "completed" if completed or outcome != "pending" else "pending"
        record["updated_at"] = now

    @staticmethod
    def _close_at(candles: list[Any], target_ts: int) -> float | None:
        target_ms = target_ts * 1000
        for candle in candles:
            if candle.close_time >= target_ms:
                return candle.close
        return None

    def aggregate(self, records: list[dict[str, Any]]) -> dict[str, Any]:
        dimensions = {
            "by_signal_type": {},
            "by_level": {},
            "by_direction": {},
            "by_symbol": {},
            "by_interval": {},
        }
        summary = self._empty_bucket()
        for record in records:
            self._add_record(summary, record)
            for name, key in (
                ("by_signal_type", str(record.get("signal_type") or "unknown")),
                ("by_level", str(record.get("level") or "unknown")),
                ("by_direction", str(record.get("direction") or "unknown")),
                ("by_symbol", str(record.get("symbol") or "unknown")),
                ("by_interval", str(record.get("interval") or "unknown")),
            ):
                bucket = dimensions[name].setdefault(key, self._empty_bucket())
                self._add_record(bucket, record)
        self._finalize_bucket(summary)
        for values in dimensions.values():
            for bucket in values.values():
                self._finalize_bucket(bucket)
        return {"summary": summary, **dimensions}

    @staticmethod
    def _empty_bucket() -> dict[str, Any]:
        return {
            "total": 0,
            "reviewed": 0,
            "pending": 0,
            "valid_breakouts": 0,
            "fake_breakouts": 0,
            "invalid_ranges": 0,
            "_15m": [],
            "_1h": [],
            "_4h": [],
            "_mfe": [],
            "_mae": [],
        }

    @staticmethod
    def _add_record(bucket: dict[str, Any], record: dict[str, Any]) -> None:
        bucket["total"] += 1
        status = str(record.get("status") or "pending")
        outcome = str(record.get("outcome") or "pending")
        metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
        if status == "pending" or outcome == "pending":
            bucket["pending"] += 1
        else:
            bucket["reviewed"] += 1
        if outcome in {"valid_breakout", "valid_breakdown"}:
            bucket["valid_breakouts"] += 1
        elif outcome in {"fake_breakout", "fake_breakdown"}:
            bucket["fake_breakouts"] += 1
        elif outcome == "invalid_range":
            bucket["invalid_ranges"] += 1
        for metric, target in (
            ("price_change_15m", "_15m"),
            ("price_change_1h", "_1h"),
            ("price_change_4h", "_4h"),
            ("mfe_pct", "_mfe"),
            ("mae_pct", "_mae"),
        ):
            value = metrics.get(metric)
            if isinstance(value, (int, float)):
                bucket[target].append(float(value))

    @staticmethod
    def _finalize_bucket(bucket: dict[str, Any]) -> None:
        reviewed = int(bucket.get("reviewed", 0) or 0)
        valid = int(bucket.get("valid_breakouts", 0) or 0)
        fake = int(bucket.get("fake_breakouts", 0) or 0)
        bucket["hit_rate"] = valid / reviewed if reviewed else 0.0
        bucket["fake_rate"] = fake / reviewed if reviewed else 0.0
        bucket["avg_15m_change"] = mean(bucket.pop("_15m", []))
        bucket["avg_1h_change"] = mean(bucket.pop("_1h", []))
        bucket["avg_4h_change"] = mean(bucket.pop("_4h", []))
        bucket["avg_mfe"] = mean(bucket.pop("_mfe", []))
        bucket["avg_mae"] = mean(bucket.pop("_mae", []))

    def suggestions(self, stats: dict[str, Any]) -> list[str]:
        summary = stats.get("summary", {}) if isinstance(stats.get("summary"), dict) else {}
        total = int(summary.get("total", 0) or 0)
        reviewed = int(summary.get("reviewed", 0) or 0)
        if reviewed < max(1, self.settings.structure_review_min_sample):
            return ["样本不足，暂不建议调整参数。"]
        suggestions: list[str] = []
        by_level = stats.get("by_level", {}) if isinstance(stats.get("by_level"), dict) else {}
        b_bucket = by_level.get("B", {}) if isinstance(by_level.get("B"), dict) else {}
        if int(b_bucket.get("reviewed", 0) or 0) >= 3 and float(b_bucket.get("fake_rate", 0) or 0) >= 0.45:
            suggestions.append(
                f"当前 B级假突破率偏高，建议 STRUCTURE_MIN_SCORE 从 {self.settings.structure_min_score} 提高到 {max(self.settings.structure_min_score + 5, 70)}。"
            )
        by_type = stats.get("by_signal_type", {}) if isinstance(stats.get("by_signal_type"), dict) else {}
        pre_total = sum(
            int((by_type.get(key, {}) if isinstance(by_type.get(key), dict) else {}).get("total", 0) or 0)
            for key in (SIGNAL_PRE_BREAKOUT_NEAR, SIGNAL_PRE_BREAKDOWN_NEAR)
        )
        if total and pre_total / total >= 0.7 and float(summary.get("hit_rate", 0) or 0) < 0.35:
            suggestions.append(
                f"当前临界信号占比偏高且命中率不足，建议 STRUCTURE_NEAR_EDGE_PCT 从 {self.settings.structure_near_edge_pct} 降到 {max(0.5, self.settings.structure_near_edge_pct - 0.3):.1f}。"
            )
        by_symbol = stats.get("by_symbol", {}) if isinstance(stats.get("by_symbol"), dict) else {}
        if by_symbol:
            max_symbol_count = max(int(bucket.get("total", 0) or 0) for bucket in by_symbol.values() if isinstance(bucket, dict))
            if max_symbol_count >= max(4, total // 4):
                suggestions.append(
                    f"当前同币重复信号较多，建议 STRUCTURE_COOLDOWN_SEC 从 {self.settings.structure_cooldown_sec} 提高到 {max(self.settings.structure_cooldown_sec * 2, 7200)}。"
                )
        if total >= 30 and self.settings.structure_send_chart_top_n > 2:
            suggestions.append(
                f"当前结构信号数量较多，建议 STRUCTURE_SEND_CHART_TOP_N 从 {self.settings.structure_send_chart_top_n} 降到 2，减少图片刷屏。"
            )
        return suggestions or ["当前样本未显示明显参数问题，暂不建议调整。"]

    def format_report(self, stats: dict[str, Any], records: list[dict[str, Any]]) -> str:
        lookback = int(stats.get("lookback_hours", self.settings.structure_review_lookback_hours) or 0)
        summary = stats.get("summary", {}) if isinstance(stats.get("summary"), dict) else {}
        lines = [
            f"📊 结构雷达复盘统计｜过去{lookback}小时",
            "",
            f"总信号：{int(summary.get('total', 0) or 0)}",
            f"已完成复盘：{int(summary.get('reviewed', 0) or 0)}",
            f"待复盘：{int(summary.get('pending', 0) or 0)}",
            f"有效突破：{int(summary.get('valid_breakouts', 0) or 0)}",
            f"假突破：{int(summary.get('fake_breakouts', 0) or 0)}",
            f"无效震荡：{int(summary.get('invalid_ranges', 0) or 0)}",
            "",
        ]
        by_level = stats.get("by_level", {}) if isinstance(stats.get("by_level"), dict) else {}
        for level in ("S", "A", "B", "C"):
            bucket = by_level.get(level, {}) if isinstance(by_level.get(level), dict) else {}
            if not bucket:
                continue
            reviewed = int(bucket.get("reviewed", 0) or 0)
            hit = float(bucket.get("hit_rate", 0) or 0) * 100
            lines.append(f"{level}级：{int(bucket.get('total', 0) or 0)}个，命中率 {hit:.0f}%（已复盘{reviewed}）")
        best = self._best_records(records)
        if best:
            lines.extend(["", "最佳信号："])
            for idx, record in enumerate(best, start=1):
                lines.extend(self._record_lines(idx, record))
        fake = self._fake_records(records)
        if fake:
            lines.extend(["", "假突破信号："])
            for idx, record in enumerate(fake, start=1):
                lines.extend(self._record_lines(idx, record))
        lines.extend(["", "参数建议："])
        for suggestion in stats.get("suggestions", []) or ["样本不足，暂不建议调整参数。"]:
            lines.append(f"- {suggestion}")
        return "\n".join(lines)

    def _best_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        completed = [
            record for record in records
            if str(record.get("outcome") or "") in {"valid_breakout", "valid_breakdown"}
        ]
        completed.sort(
            key=lambda record: float((record.get("metrics") or {}).get("mfe_pct", 0) or 0),
            reverse=True,
        )
        return completed[: max(1, self.settings.structure_review_report_top_n)]

    def _fake_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        fake = [
            record for record in records
            if str(record.get("outcome") or "") in {"fake_breakout", "fake_breakdown"}
        ]
        fake.sort(key=lambda record: int(record.get("signal_ts", 0) or 0), reverse=True)
        return fake[: max(1, self.settings.structure_review_report_top_n)]

    @staticmethod
    def _record_lines(idx: int, record: dict[str, Any]) -> list[str]:
        metrics = record.get("metrics") if isinstance(record.get("metrics"), dict) else {}
        return [
            f"{idx}. {record.get('symbol', '')} {record.get('signal_type', '')} {record.get('level', '')}级",
            (
                f"   15m {StructureReviewEngine._fmt_pct(metrics.get('price_change_15m'))} | "
                f"1h {StructureReviewEngine._fmt_pct(metrics.get('price_change_1h'))} | "
                f"4h {StructureReviewEngine._fmt_pct(metrics.get('price_change_4h'))} | "
                f"{record.get('outcome', '')}"
            ),
        ]

    @staticmethod
    def _fmt_pct(value: Any) -> str:
        if not isinstance(value, (int, float)):
            return "待定"
        return f"{float(value):+.1f}%"

    def _load_records(self) -> list[dict[str, Any]]:
        data = self.store.load(self.settings.structure_review_path, [])
        if isinstance(data, dict) and isinstance(data.get("records"), list):
            return [record for record in data["records"] if isinstance(record, dict)]
        return [record for record in data if isinstance(record, dict)] if isinstance(data, list) else []

    def _save_records(self, records: list[dict[str, Any]]) -> None:
        records.sort(key=lambda record: (int(record.get("signal_ts", 0) or 0), str(record.get("id") or "")))
        limit = max(500, self.settings.structure_review_lookback_hours * 80)
        self.store.save(self.settings.structure_review_path, records[-limit:])
