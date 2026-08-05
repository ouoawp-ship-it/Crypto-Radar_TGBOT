from __future__ import annotations

import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from config import Settings
from shared.funding_sources import to_float, to_int
from shared.market_links import telegram_coin_links
from shared.storage import JsonStore


CST = timezone(timedelta(hours=8))
HOUR_MS = 3_600_000


def analyze_oi_segment_growth(
    rows: list[dict[str, Any]],
    *,
    now_ms: int,
    window_points: int = 48,
    min_coverage: float = 0.90,
    max_age_sec: int = 10_800,
    min_growth_pct: float = 8.0,
    segment_tolerance_pct: float = 0.5,
) -> dict[str, Any]:
    expected = min(48, max(12, int(window_points)))
    closed_before = int(now_ms) - HOUR_MS
    points_by_time: dict[int, float] = {}
    invalid_rows = 0
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            invalid_rows += 1
            continue
        timestamp_ms = to_int(row.get("timestamp") or row.get("time") or row.get("T"))
        oi_usd = to_float(row.get("sumOpenInterestValue"))
        if (
            timestamp_ms <= 0
            or timestamp_ms > closed_before
            or not math.isfinite(oi_usd)
            or oi_usd <= 0
        ):
            invalid_rows += 1
            continue
        points_by_time[timestamp_ms] = oi_usd

    points = sorted(points_by_time.items())[-expected:]
    coverage = len(points) / expected if expected else 0.0
    minimum_points = max(12, int(expected * max(0.0, min(1.0, min_coverage)) + 0.9999))
    result: dict[str, Any] = {
        "eligible": False,
        "reason": "",
        "expected_points": expected,
        "valid_points": len(points),
        "invalid_rows": invalid_rows,
        "coverage": round(coverage, 4),
        "missing_points": max(0, expected - len(points)),
        "segment_averages_usd": [],
        "segment_changes_pct": [],
        "total_growth_pct": 0.0,
        "latest_timestamp_ms": points[-1][0] if points else 0,
        "source": "Binance USDⓈ-M Futures openInterestHist 1h",
    }
    if len(points) < minimum_points:
        result["reason"] = "insufficient_coverage"
        return result

    timestamps = [timestamp for timestamp, _value in points]
    gap_count = sum(
        max(0, int(round((current - previous) / HOUR_MS)) - 1)
        for previous, current in zip(timestamps, timestamps[1:])
        if current - previous > int(HOUR_MS * 1.5)
    )
    result["gap_count"] = gap_count
    if gap_count > max(0, expected - minimum_points):
        result["reason"] = "missing_intervals"
        return result

    latest_age_sec = max(0, (int(now_ms) - timestamps[-1]) // 1000)
    result["latest_age_sec"] = latest_age_sec
    if latest_age_sec > max(HOUR_MS // 1000, int(max_age_sec)):
        result["reason"] = "stale_data"
        return result

    usable_count = (len(points) // 4) * 4
    if usable_count < 12:
        result["reason"] = "insufficient_segments"
        return result
    values = [value for _timestamp, value in points[-usable_count:]]
    segment_size = usable_count // 4
    segments = [
        sum(values[index * segment_size:(index + 1) * segment_size]) / segment_size
        for index in range(4)
    ]
    changes = [
        ((current / previous) - 1.0) * 100.0 if previous > 0 else 0.0
        for previous, current in zip(segments, segments[1:])
    ]
    total_growth = ((segments[-1] / segments[0]) - 1.0) * 100.0 if segments[0] > 0 else 0.0
    tolerance = abs(float(segment_tolerance_pct))
    monotonic = all(change >= -tolerance for change in changes)
    increasing_steps = sum(1 for change in changes if change > 0)
    result.update({
        "segment_averages_usd": [round(value, 2) for value in segments],
        "segment_changes_pct": [round(value, 4) for value in changes],
        "total_growth_pct": round(total_growth, 4),
        "monotonic_with_tolerance": monotonic,
        "increasing_steps": increasing_steps,
    })
    if total_growth < float(min_growth_pct):
        result["reason"] = "growth_below_threshold"
        return result
    if not monotonic or increasing_steps < 2:
        result["reason"] = "segments_not_increasing"
        return result
    result["eligible"] = True
    result["reason"] = "confirmed"
    return result


class FundingFlipOITracker:
    def __init__(self, settings: Settings, store: JsonStore):
        self.settings = settings
        self.store = store

    def evaluate(
        self,
        candidates: list[dict[str, Any]],
        rows_by_symbol: dict[str, list[dict[str, Any]]],
        source: Any,
        *,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        if not self.settings.funding_flip_oi_enable:
            return {"alerts": [], "messages": [], "diagnostics": {"status": "disabled"}}

        observed_at = int(now_ts if now_ts is not None else time.time())
        state = self.store.load(self.settings.funding_flip_oi_state_path, {})
        if not isinstance(state, dict):
            state = {}
        was_initialized = bool(state.get("initialized_at"))
        symbols_state = state.get("symbols")
        if not isinstance(symbols_state, dict):
            symbols_state = {}
        candidate_by_symbol = {
            str(item.get("symbol") or "").upper(): item
            for item in candidates
            if isinstance(item, dict) and str(item.get("symbol") or "").strip()
        }
        alerts: list[dict[str, Any]] = []
        checked = 0
        flip_candidates = 0
        cooldown_suppressed = 0
        degraded = 0
        pending_retried = 0
        pending_expired = 0
        rate_max_age = max(60, int(self.settings.funding_flip_oi_rate_max_age_sec))
        cooldown = max(60, int(self.settings.funding_flip_oi_cooldown_sec))

        for symbol, candidate in candidate_by_symbol.items():
            binance_row = next(
                (
                    row for row in rows_by_symbol.get(symbol, [])
                    if isinstance(row, dict)
                    and str(row.get("exchange") or "").strip().upper() == "BINANCE"
                ),
                None,
            )
            if not isinstance(binance_row, dict):
                continue
            raw_current_rate = binance_row.get("funding_pct")
            try:
                current_rate = float(raw_current_rate)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(current_rate):
                continue
            previous = symbols_state.get(symbol)
            previous = previous if isinstance(previous, dict) else {}
            record = {
                **previous,
                "funding_pct": current_rate,
                "observed_at": observed_at,
            }
            checked += 1
            pending = previous.get("pending_event")
            pending = pending if isinstance(pending, dict) else {}
            pending_detected_at = to_int(pending.get("detected_at"))
            pending_alert = pending.get("alert")
            if (
                pending_detected_at > 0
                and observed_at - pending_detected_at <= cooldown
                and isinstance(pending_alert, dict)
                and str(pending_alert.get("symbol") or "") == symbol
            ):
                alert = dict(pending_alert)
                alert["text"] = format_funding_flip_oi_alert(alert)
                alerts.append(alert)
                symbols_state[symbol] = record
                pending_retried += 1
                continue
            if pending:
                record.pop("pending_event", None)
                pending_expired += 1

            previous_rate = previous.get("funding_pct")
            previous_ts = to_int(previous.get("observed_at"))
            try:
                parsed_previous_rate = float(previous_rate)
            except (TypeError, ValueError):
                parsed_previous_rate = float("nan")
            is_fresh_previous = (
                math.isfinite(parsed_previous_rate)
                and previous_ts > 0
                and observed_at - previous_ts <= rate_max_age
            )
            just_turned_negative = (
                is_fresh_previous
                and parsed_previous_rate >= 0
                and current_rate < 0
            )
            symbols_state[symbol] = record
            if not just_turned_negative:
                continue
            flip_candidates += 1
            last_alert_at = to_int(previous.get("last_alert_at"))
            if last_alert_at > 0 and observed_at - last_alert_at < cooldown:
                cooldown_suppressed += 1
                continue

            try:
                oi_rows = source.open_interest_hist(
                    symbol,
                    period="1h",
                    limit=min(48, max(12, int(self.settings.funding_flip_oi_window_points))),
                )
            except Exception:
                oi_rows = []
            analysis = analyze_oi_segment_growth(
                oi_rows,
                now_ms=observed_at * 1000,
                window_points=self.settings.funding_flip_oi_window_points,
                min_coverage=self.settings.funding_flip_oi_min_coverage,
                max_age_sec=self.settings.funding_flip_oi_max_age_sec,
                min_growth_pct=self.settings.funding_flip_oi_min_growth_pct,
                segment_tolerance_pct=self.settings.funding_flip_oi_segment_tolerance_pct,
            )
            if not analysis.get("eligible"):
                degraded += 1
                record["last_analysis"] = analysis
                continue

            event_id = f"{symbol}:{previous_ts}:{observed_at}"
            alert = {
                **candidate,
                "symbol": symbol,
                "event_family": "funding_flip_oi",
                "primary_kind": "funding_flip_oi",
                "previous_funding_pct": parsed_previous_rate,
                "funding_pct": current_rate,
                "oi_analysis": analysis,
                "observed_at": observed_at,
                "dedup_key": f"funding-flip-oi:{event_id}",
                "cooldown_sec": cooldown,
                "event_snapshot": {
                    "event_id": event_id,
                    "event_family": "funding_flip_oi",
                    "observed_at": observed_at,
                },
                "data_quality_status": "confirmed",
                "quality_gate": "allow",
                "primary_data_source": "binance_native",
            }
            alert["text"] = format_funding_flip_oi_alert(alert)
            alerts.append(alert)
            record["pending_event"] = {
                "event_id": event_id,
                "detected_at": observed_at,
                "alert": alert,
            }
            record["last_analysis"] = analysis

        state["symbols"] = symbols_state
        state.setdefault("initialized_at", observed_at)
        state["updated_at"] = observed_at
        state["last_diagnostics"] = {
            "checked": checked,
            "flip_candidates": flip_candidates,
            "alerts": len(alerts),
            "cooldown_suppressed": cooldown_suppressed,
            "degraded": degraded,
            "pending_retried": pending_retried,
            "pending_expired": pending_expired,
        }
        self.store.save(self.settings.funding_flip_oi_state_path, state)
        status = "first_snapshot" if checked and not was_initialized else "ok"
        return {
            "alerts": alerts,
            "messages": [str(alert.get("text") or "") for alert in alerts],
            "diagnostics": {"status": status, **state["last_diagnostics"]},
        }

    def mark_pushed(
        self,
        alerts: list[dict[str, Any]],
        *,
        now_ts: int | None = None,
    ) -> int:
        if not self.settings.funding_flip_oi_enable:
            return 0
        committed_at = int(now_ts if now_ts is not None else time.time())
        state = self.store.load(self.settings.funding_flip_oi_state_path, {})
        if not isinstance(state, dict):
            return 0
        symbols_state = state.get("symbols")
        if not isinstance(symbols_state, dict):
            return 0
        committed = 0
        for alert in alerts:
            if str(alert.get("event_family") or "") != "funding_flip_oi":
                continue
            symbol = str(alert.get("symbol") or "").upper()
            event = alert.get("event_snapshot")
            event = event if isinstance(event, dict) else {}
            event_id = str(event.get("event_id") or "")
            record = symbols_state.get(symbol)
            if not symbol or not event_id or not isinstance(record, dict):
                continue
            pending = record.get("pending_event")
            pending = pending if isinstance(pending, dict) else {}
            if str(pending.get("event_id") or "") != event_id:
                continue
            record["last_alert_at"] = committed_at
            record["last_event_id"] = event_id
            record["last_analysis"] = dict(alert.get("oi_analysis") or {})
            record.pop("pending_event", None)
            symbols_state[symbol] = record
            committed += 1
        if committed:
            state["symbols"] = symbols_state
            state["updated_at"] = committed_at
            self.store.save(self.settings.funding_flip_oi_state_path, state)
        return committed


def format_funding_flip_oi_alert(alert: dict[str, Any]) -> str:
    symbol = str(alert.get("symbol") or "")
    previous_rate = to_float(alert.get("previous_funding_pct"))
    current_rate = to_float(alert.get("funding_pct"))
    analysis = alert.get("oi_analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    segments = [
        float(value)
        for value in (analysis.get("segment_averages_usd") or [])
        if isinstance(value, (int, float))
    ]
    segment_text = " → ".join(f"${value / 1_000_000:.2f}M" for value in segments)
    observed_at = to_int(alert.get("observed_at"))
    observed_text = datetime.fromtimestamp(observed_at, CST).strftime("%m-%d %H:%M CST")
    return "\n".join([
        f"🔄 <b>费率正转负＋OI连续增长</b> {telegram_coin_links(symbol)}",
        f"⏰ {observed_text}",
        "",
        f"<b>资金费率</b>: {previous_rate:+.4f}% → {current_rate:+.4f}%",
        f"<b>OI四段均值</b>: {segment_text}",
        f"<b>OI总增长</b>: {to_float(analysis.get('total_growth_pct')):+.2f}%",
        (
            f"<b>数据覆盖</b>: {to_int(analysis.get('valid_points'))}/"
            f"{to_int(analysis.get('expected_points'))} 个已闭合1h点"
        ),
        "",
        "<b>判断</b>: 费率刚由非负转为负值，同时OI分段持续增长，说明新增仓位与空头拥挤正在同步累积。",
        "<b>来源</b>: Binance USDⓈ-M Futures 原生资金费率与 openInterestHist。",
        "该事件只表示拥挤结构变化，不构成直接买卖建议。",
    ])
