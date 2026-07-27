from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from .config import Settings
from .data_sources import BinanceDataSource
from .funding_sources import (
    MultiExchangeFundingClient,
    funding_cycle_text,
    funding_extreme_label,
    funding_last_settlement_text,
    funding_interval_hours,
    funding_interval_label,
    funding_settlement_period_text,
    funding_time_text,
    to_float,
    to_int,
)
from .market_links import coinglass_tv_url as _coinglass_tv_url
from .market_links import telegram_coin_links
from .storage import JsonStore


CST = timezone(timedelta(hours=8))
TEMPLATE_ID = "TG_FUNDING_ALERT"
VALID_FUNDING_INTERVAL_HOURS = frozenset({1, 2, 4, 8, 12, 24})
FUNDING_TRACKING_VERSION = 2
FUNDING_EVENT_HISTORY_LIMIT = 12

STAGE_LABELS = {
    "first_seen": "首次异动",
    "active": "持续活跃",
    "crowding_intensifying": "拥挤加剧",
    "high_risk_active": "高危活跃",
    "risk_release": "风险释放",
    "heat_decay": "热度衰减",
    "observation_ended": "观察结束",
}


def cst_now_text(fmt: str = "%m-%d %H:%M CST") -> str:
    return datetime.now(CST).strftime(fmt)


def tg_escape(value: Any) -> str:
    return escape(str(value), quote=False)


def tg_bold(value: Any) -> str:
    return f"<b>{tg_escape(value)}</b>"


def tg_quote(title: str) -> str:
    return f"<blockquote><b>{tg_escape(title)}</b></blockquote>"


def coinglass_tv_url(symbol: str) -> str:
    return _coinglass_tv_url(symbol)


def coin_link(symbol: str) -> str:
    return telegram_coin_links(symbol)


def is_excluded_symbol(symbol: str, excluded: tuple[str, ...]) -> bool:
    coin = str(symbol or "").upper().strip()
    if coin.endswith("USDT"):
        coin = coin[:-4]
    return coin in set(excluded)


def fmt_money(value: float) -> str:
    value = float(value or 0)
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def market_cap_tier(value: float) -> str:
    if value <= 0:
        return "未知市值"
    if value >= 10_000_000_000:
        return "高市值"
    if value >= 1_000_000_000:
        return "中市值"
    return "低市值"


def liquidity_tier(value: float) -> str:
    if value <= 0:
        return "未知流动性"
    if value >= 100_000_000:
        return "高流动性"
    if value >= 20_000_000:
        return "中流动性"
    return "低流动性"


def stage_label(stage: str) -> str:
    return STAGE_LABELS.get(str(stage or ""), str(stage or "未知"))


def short_cst_time(timestamp: Any) -> str:
    value = to_int(timestamp)
    if value <= 0:
        return "时间未知"
    return datetime.fromtimestamp(value, CST).strftime("%m-%d %H:%M")


def event_number_text(value: Any) -> str:
    number = max(1, to_int(value, 1))
    circled = "①②③④⑤⑥⑦⑧⑨⑩"
    return circled[number - 1] if number <= len(circled) else str(number)


def percent_change(current: Any, baseline: Any) -> float | None:
    current_value = to_float(current)
    baseline_value = to_float(baseline)
    if current_value <= 0 or baseline_value <= 0:
        return None
    return (current_value / baseline_value - 1.0) * 100.0


def funding_rate_label(funding_pct: float, settings: Settings) -> str:
    if funding_pct <= settings.funding_alert_super_negative_pct:
        return "超极负"
    if funding_pct <= settings.funding_alert_extreme_negative_pct:
        return "极负"
    if funding_pct >= abs(settings.funding_alert_super_negative_pct):
        return "超极正"
    if funding_pct >= settings.funding_alert_extreme_positive_pct:
        return "极正"
    return funding_extreme_label(funding_pct)


def funding_row_text(row: dict[str, Any], settings: Settings | None = None) -> str:
    exchange = str(row.get("exchange") or "Unknown").strip()
    funding_pct = to_float(row.get("funding_pct"))
    interval_hours = to_int(row.get("interval_hours"))
    text = funding_cycle_text(funding_pct, interval_hours)
    label = str(row.get("extreme_label") or "").strip()
    if settings is not None:
        label = funding_rate_label(funding_pct, settings)
    elif not label:
        label = funding_extreme_label(funding_pct)
    if label:
        text = f"{text}（{label}）"
    last_time = funding_last_settlement_text(row) or "未知"
    period = funding_settlement_period_text(row)
    next_time = str(row.get("next_funding_time") or "").strip() or "未知"
    return f"{exchange}: {text}｜上次结算 {tg_escape(last_time)}｜周期 {tg_escape(period)}｜下次结算 {tg_escape(next_time)}"


def short_funding_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", text)
    if match:
        return f"{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}"
    return text[:14]


def _display_width(value: Any) -> int:
    total = 0
    for char in str(value or ""):
        total += 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
    return total


def _display_ljust(value: Any, width: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    result = []
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if used + char_width > width:
            break
        result.append(char)
        used += char_width
    return "".join(result) + (" " * max(0, width - used))


FUNDING_TABLE_COLUMNS = (
    ("交易所", 8),
    ("费率/周期", 18),
    ("上次结算", 12),
    ("本次周期", 16),
    ("下次结算", 12),
)


def _funding_table_row(values: list[Any]) -> str:
    padded = [
        _display_ljust(value, width)
        for value, (_, width) in zip(values, FUNDING_TABLE_COLUMNS)
    ]
    return "  ".join(padded).rstrip()


def funding_table_lines(rows: list[dict[str, Any]], settings: Settings) -> list[str]:
    lines = [_funding_table_row([label for label, _ in FUNDING_TABLE_COLUMNS])]
    for row in rows:
        exchange = str(row.get("exchange") or "Unknown").strip()
        funding_pct = to_float(row.get("funding_pct"))
        interval_hours = to_int(row.get("interval_hours"))
        rate = funding_cycle_text(funding_pct, interval_hours)
        label = funding_rate_label(funding_pct, settings)
        rate_text = f"{rate} {label}".strip()
        last_time = short_funding_time(funding_last_settlement_text(row))
        period = funding_settlement_period_text(row)
        next_time = short_funding_time(str(row.get("next_funding_time") or ""))
        lines.append(_funding_table_row([exchange, rate_text, last_time, period, next_time]))
    return lines


def funding_table(rows: list[dict[str, Any]], settings: Settings) -> str:
    return "<pre>" + tg_escape("\n".join(funding_table_lines(rows, settings))) + "</pre>"


def funding_alert_cards(rows: list[dict[str, Any]], settings: Settings) -> str:
    cards: list[str] = []
    for row in rows:
        exchange = str(row.get("exchange") or "Unknown").strip()
        funding_pct = to_float(row.get("funding_pct"))
        period = funding_settlement_period_text(row)
        label = funding_rate_label(funding_pct, settings)
        summary = (
            f"{tg_bold(exchange)}｜{tg_bold(f'{funding_pct:+.3f}%')}"
            f" · {tg_escape(period)}"
        )
        if label:
            summary += f" · {tg_escape(label)}"

        last_time = short_funding_time(funding_last_settlement_text(row))
        next_time = short_funding_time(str(row.get("next_funding_time") or ""))
        cards.append(
            f"{summary}\n"
            f"结算｜{tg_escape(last_time)} → {tg_escape(next_time)}"
        )
    return "\n\n".join(cards)


def classify_funding_alert(rows: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    if not rows:
        return {}
    extreme_negative = [
        row for row in rows
        if to_float(row.get("funding_pct")) <= settings.funding_alert_extreme_negative_pct
    ]
    super_negative = [
        row for row in rows
        if to_float(row.get("funding_pct")) <= settings.funding_alert_super_negative_pct
    ]
    extreme_positive = [
        row for row in rows
        if to_float(row.get("funding_pct")) >= settings.funding_alert_extreme_positive_pct
    ]
    super_positive = [
        row for row in rows
        if to_float(row.get("funding_pct")) >= abs(settings.funding_alert_super_negative_pct)
    ]
    transitions = [
        row for row in rows
        if str(row.get("funding_interval_transition") or "").strip()
    ]
    rates = [to_float(row.get("funding_pct")) for row in rows]
    divergence = max(rates) - min(rates) if len(rates) >= 2 else 0.0
    max_abs_rate = max((abs(rate) for rate in rates), default=0.0)
    negative_exchanges = list(dict.fromkeys(
        str(row.get("exchange") or "").strip()
        for row in extreme_negative
        if str(row.get("exchange") or "").strip()
    ))
    positive_exchanges = list(dict.fromkeys(
        str(row.get("exchange") or "").strip()
        for row in extreme_positive
        if str(row.get("exchange") or "").strip()
    ))
    multi_exchange_count = max(2, settings.funding_alert_min_exchange_count)

    types: list[str] = []
    primary_kind = ""
    if transitions:
        types.append("结算周期缩短")
        primary_kind = primary_kind or "interval_shortened"
    if len(negative_exchanges) >= multi_exchange_count:
        types.append("多所极负共振")
        primary_kind = primary_kind or "multi_negative"
    elif extreme_negative:
        exchange = negative_exchanges[0] if len(negative_exchanges) == 1 else ""
        types.append(f"{exchange} 极负资金费率".strip())
        primary_kind = primary_kind or "extreme_negative"
    if len(positive_exchanges) >= multi_exchange_count:
        types.append("多所极正共振")
        primary_kind = primary_kind or "multi_positive"
    elif extreme_positive:
        exchange = positive_exchanges[0] if len(positive_exchanges) == 1 else ""
        types.append(f"{exchange} 极正资金费率".strip())
        primary_kind = primary_kind or "extreme_positive"
    if divergence >= settings.funding_alert_divergence_pct:
        types.append("交易所费率偏离")
        primary_kind = primary_kind or "exchange_divergence"

    if not types:
        return {}

    risk = "观察"
    if super_negative or super_positive:
        risk = "极高"
    elif transitions or len(extreme_negative) >= settings.funding_alert_min_exchange_count or len(extreme_positive) >= settings.funding_alert_min_exchange_count:
        risk = "高"

    return {
        "types": types,
        "primary_kind": primary_kind or "funding_alert",
        "risk": risk,
        "negative_count": len(extreme_negative),
        "positive_count": len(extreme_positive),
        "negative_exchanges": negative_exchanges,
        "positive_exchanges": positive_exchanges,
        "transition_count": len(transitions),
        "extreme_count": len(extreme_negative) + len(extreme_positive),
        "divergence_pct": divergence,
        "max_abs_funding_pct": max_abs_rate,
        "direction": "偏空拥挤" if len(extreme_negative) >= len(extreme_positive) else "偏多拥挤",
    }


class FundingAlertEngine:
    def __init__(self, settings: Settings, store: JsonStore):
        self.settings = settings
        self.store = store

    def build(self, source: BinanceDataSource) -> dict[str, Any]:
        if not self.settings.funding_alert_enable:
            return self._empty_result("disabled")
        if self.settings.funding_alert_scan_limit <= 0:
            return self._empty_result("scan_limit_zero")
        http = getattr(source, "http", None)
        if http is None:
            return self._empty_result("missing_http")

        state = self._load_state()
        candidates = self._candidate_items(source)
        funding_settings = replace(
            self.settings,
            launch_funding_exchanges=self.settings.funding_alert_exchanges,
            launch_funding_history_limit=self.settings.funding_alert_history_limit,
        )
        client = MultiExchangeFundingClient(funding_settings, http)
        now_ts = int(time.time())
        alerts: list[dict[str, Any]] = []
        scanned = 0
        rows_seen = 0
        symbols = [str(candidate.get("symbol") or "") for candidate in candidates]
        current_rows_by_symbol = client.snapshot_many(symbols, include_history=False)
        scan_metrics = dict(client.last_batch_metrics)
        prepared: list[tuple[dict[str, Any], str, list[dict[str, Any]], dict[str, Any]]] = []
        history_symbols: list[str] = []
        for candidate in candidates:
            symbol = str(candidate.get("symbol") or "")
            rows = current_rows_by_symbol.get(symbol, [])
            if not rows:
                continue
            scanned += 1
            rows_seen += len(rows)
            rows = self._apply_state_transitions(symbol, rows, state)
            classification = classify_funding_alert(rows, self.settings)
            prepared.append((candidate, symbol, rows, classification))
            if classification:
                history_symbols.append(symbol)

        full_rows_by_symbol = (
            client.snapshot_many(history_symbols, include_history=True)
            if history_symbols
            else {}
        )
        history_metrics = dict(client.last_batch_metrics) if history_symbols else {}
        try:
            from .bot_market_context import closed_market_contexts_for_symbols

            market_contexts = closed_market_contexts_for_symbols(
                self.settings,
                [symbol for _candidate, symbol, _rows, _classification in prepared],
                now_ts=now_ts,
            )
        except Exception:
            market_contexts = {}
        for candidate, symbol, rows, classification in prepared:
            if not classification:
                decay_alert = self._maybe_decay_alert(symbol, candidate, rows, state, now_ts)
                if decay_alert:
                    alerts.append(decay_alert)
                continue

            full_rows = full_rows_by_symbol.get(symbol, [])
            if full_rows:
                full_rows = self._apply_state_transitions(symbol, full_rows, state)
                rows = full_rows
                classification = classify_funding_alert(rows, self.settings) or classification
            tracking = self._tracking_info(symbol, candidate, rows, classification, state, now_ts)
            alert = {
                "symbol": symbol,
                "rows": rows,
                "classification": classification,
                "dedup_key": self._dedup_key(symbol, classification, tracking["stage"]),
                "text": "",
                "market_context": market_contexts.get(symbol, {}),
                **candidate,
                **tracking,
            }
            self._update_symbol_state(symbol, rows, state, now_ts, candidate, classification, tracking)
            if self._cooldown_ok(alert["dedup_key"], state, now_ts):
                alerts.append(alert)

        alerts = self._backfill_outbound_binance_history(
            alerts,
            funding_settings,
            http,
            state,
            now_ts,
        )
        confirmation_candidates = len(alerts)
        validated_alerts: list[dict[str, Any]] = []
        for alert in alerts:
            symbol = str(alert.get("symbol") or "")
            alert.setdefault("market_context", market_contexts.get(symbol, {}))
            rows = [row for row in (alert.get("rows") or []) if isinstance(row, dict)]
            exchanges = [
                str(row.get("exchange") or "").strip()
                for row in rows
                if str(row.get("exchange") or "").strip()
            ]
            alert.update({
                "data_quality_status": "confirmed" if exchanges else "incomplete",
                "data_quality_score": 100 if exchanges else 0,
                "quality_gate": "allow" if exchanges else "block",
                "primary_data_source": "native_exchange_apis",
                "data_confirmation": {
                    "provider": "native_exchange_apis",
                    "exchanges": exchanges,
                    "count": len(exchanges),
                    "status": "confirmed" if exchanges else "incomplete",
                },
            })
            if not exchanges:
                continue
            current_event = self._event_snapshot(alert, now_ts)
            published_events = [
                dict(event)
                for event in (alert.get("published_events_before") or [])
                if isinstance(event, dict)
            ]
            alert["event_snapshot"] = current_event
            alert["first_snapshot"] = (
                dict(published_events[0]) if published_events else dict(current_event)
            )
            alert["previous_snapshot"] = (
                dict(published_events[-1]) if published_events else {}
            )
            alert["text"] = self._format_alert(alert)
            validated_alerts.append(alert)
        alerts = validated_alerts

        state["updated_at"] = datetime.now(CST).isoformat(timespec="seconds")
        state["last_scanned"] = scanned
        state["last_alert_count"] = len(alerts)
        self.store.save(self.settings.funding_alert_state_path, state)
        return {
            "template_id": TEMPLATE_ID,
            "messages": [alert["text"] for alert in alerts],
            "alerts": alerts,
            "diagnostics": {
                "status": "ok",
                "candidates": len(candidates),
                "scanned": scanned,
                "funding_rows": rows_seen,
                "alerts": len(alerts),
                "exchanges": list(self.settings.funding_alert_exchanges),
                "scan_metrics": scan_metrics,
                "history_metrics": history_metrics,
                "native_funding_confirmation": {
                    "checked": confirmation_candidates,
                    "confirmed": len(validated_alerts),
                    "incomplete": max(0, confirmation_candidates - len(validated_alerts)),
                },
            },
        }

    def mark_pushed(self, alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not alerts:
            return []
        state = self._load_state()
        now_ts = int(time.time())
        changed = False
        cleanup_jobs: list[dict[str, Any]] = []
        rollback_jobs: list[tuple[Any, list[int]]] = []
        delete_callback = None
        for alert in alerts:
            symbol = str(alert.get("symbol") or "")
            if not symbol:
                continue
            record = state.get("symbols", {}).get(symbol, {})
            if not isinstance(record, dict):
                continue
            message_ids = [
                int(message_id)
                for message_id in (alert.get("message_ids") or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            ]
            if not message_ids:
                continue
            callback = alert.get("_funding_delete_callback")
            if callable(callback):
                delete_callback = callback
                rollback_jobs.append((callback, message_ids))
            event = alert.get("event_snapshot")
            if not isinstance(event, dict):
                continue
            events = [
                dict(item)
                for item in (alert.get("published_events_before") or [])
                if isinstance(item, dict)
            ]
            event_id = str(event.get("event_id") or "")
            if not any(str(item.get("event_id") or "") == event_id for item in events):
                events.append(dict(event))
            if len(events) > FUNDING_EVENT_HISTORY_LIMIT:
                events = [
                    events[0],
                    *events[-(FUNDING_EVENT_HISTORY_LIMIT - 1):],
                ]
            old_message_ids = {
                int(message_id)
                for message_id in (
                    list(alert.get("replace_message_ids") or [])
                    + list(record.get("pending_delete_message_ids") or [])
                )
                if isinstance(message_id, int) or str(message_id).isdigit()
            }
            old_message_ids.difference_update(message_ids)
            record["last_message_id"] = message_ids[0]
            record["last_message_ids"] = message_ids
            record["pending_delete_message_ids"] = sorted(old_message_ids)
            record["last_message_stage"] = str(alert.get("stage") or "")
            record["last_pushed"] = now_ts
            record["last_pushed_kind"] = str(alert.get("primary_kind") or alert.get("classification", {}).get("primary_kind") or "")
            record["funding_tracking_version"] = FUNDING_TRACKING_VERSION
            record["cycle_no"] = max(1, to_int(alert.get("cycle_no"), 1))
            record["published_events"] = events
            record["published_event_count"] = max(
                len(events),
                to_int(alert.get("event_no"), len(events)),
            )
            record["first_snapshot"] = dict(events[0])
            record["last_published_snapshot"] = dict(events[-1])
            record["alert_count"] = record["published_event_count"]
            self._mark_alert(str(alert.get("dedup_key") or ""), state, now_ts)
            state["symbols"][symbol] = record
            changed = True
            if old_message_ids:
                cleanup_jobs.append({
                    "symbol": symbol,
                    "message_ids": sorted(old_message_ids),
                })
        if changed:
            try:
                self.store.save(self.settings.funding_alert_state_path, state)
            except Exception:
                for callback, message_ids in rollback_jobs:
                    callback(
                        message_ids,
                        reason="funding_state_commit_rollback",
                    )
                raise
        if callable(delete_callback):
            for job in self.pending_message_cleanups(limit=20):
                deletion = delete_callback(
                    list(job.get("message_ids") or []),
                    reason="funding_message_replaced",
                )
                self.complete_message_cleanup(
                    symbol=str(job.get("symbol") or ""),
                    deleted_ids=list(deletion.get("deleted_ids") or []),
                    failed_ids=list(deletion.get("failed_ids") or []),
                )
        return cleanup_jobs

    def pending_message_cleanups(self, *, limit: int = 20) -> list[dict[str, Any]]:
        state = self._load_state()
        jobs: list[dict[str, Any]] = []
        for symbol, raw in state.get("symbols", {}).items():
            record = raw if isinstance(raw, dict) else {}
            message_ids = [
                int(message_id)
                for message_id in (record.get("pending_delete_message_ids") or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            ]
            if message_ids:
                jobs.append({
                    "symbol": str(symbol),
                    "message_ids": message_ids,
                })
            if len(jobs) >= max(1, int(limit)):
                break
        return jobs

    def complete_message_cleanup(
        self,
        *,
        symbol: str,
        deleted_ids: list[int],
        failed_ids: list[int],
    ) -> None:
        state = self._load_state()
        record = state.get("symbols", {}).get(str(symbol), {})
        if not isinstance(record, dict):
            return
        deleted = {int(message_id) for message_id in deleted_ids}
        failed = {int(message_id) for message_id in failed_ids}
        pending = {
            int(message_id)
            for message_id in (record.get("pending_delete_message_ids") or [])
            if isinstance(message_id, int) or str(message_id).isdigit()
        }
        record["pending_delete_message_ids"] = sorted((pending - deleted) | failed)
        state["symbols"][str(symbol)] = record
        self.store.save(self.settings.funding_alert_state_path, state)

    def _empty_result(self, reason: str) -> dict[str, Any]:
        return {
            "template_id": TEMPLATE_ID,
            "messages": [],
            "alerts": [],
            "diagnostics": {"status": reason, "alerts": 0},
        }

    def _candidate_items(self, source: BinanceDataSource) -> list[dict[str, Any]]:
        try:
            tickers = source.ticker_24h()
        except Exception:
            tickers = []
        candidates: list[dict[str, Any]] = []
        for item in tickers if isinstance(tickers, list) else []:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper().strip()
            if not symbol.endswith("USDT") or is_excluded_symbol(symbol, self.settings.excluded_base_assets):
                continue
            quote_volume = to_float(item.get("quoteVolume"))
            if quote_volume < self.settings.funding_alert_min_quote_volume:
                continue
            coin = symbol[:-4]
            candidates.append({
                "symbol": symbol,
                "coin": coin,
                "quote_volume": quote_volume,
                "price_24h_pct": to_float(item.get("priceChangePercent")),
                "last_price": to_float(item.get("lastPrice")),
                "mcap": 0.0,
                "mcap_source": "",
            })
        candidates.sort(key=lambda item: item["quote_volume"], reverse=True)
        batch_limit = max(1, int(getattr(self.settings, "funding_max_symbols_per_batch", 120) or 120))
        candidates = candidates[: min(self.settings.funding_alert_scan_limit, batch_limit)]
        self._enrich_market_caps(source, candidates)
        return candidates

    def _enrich_market_caps(self, source: BinanceDataSource, candidates: list[dict[str, Any]]) -> None:
        if not candidates:
            return
        market_caps: dict[str, float] = {}
        if hasattr(source, "market_caps"):
            try:
                raw = source.market_caps()
                market_caps = raw if isinstance(raw, dict) else {}
            except Exception:
                market_caps = {}
        missing: set[str] = set()
        for item in candidates:
            coin = str(item.get("coin") or "")
            mcap = to_float(market_caps.get(coin))
            if mcap > 0:
                item["mcap"] = mcap
                item["mcap_source"] = "Binance"
            else:
                missing.add(coin)
        if not missing or not hasattr(source, "coinpaprika_market_caps"):
            return
        try:
            fallback = source.coinpaprika_market_caps()
            fallback = fallback if isinstance(fallback, dict) else {}
        except Exception:
            fallback = {}
        for item in candidates:
            if item["mcap"] > 0 or item["coin"] not in missing:
                continue
            mcap = to_float(fallback.get(item["coin"]))
            if mcap > 0:
                item["mcap"] = mcap
                item["mcap_source"] = "CoinPaprika"

    def _load_state(self) -> dict[str, Any]:
        state = self.store.load(self.settings.funding_alert_state_path, {})
        if not isinstance(state, dict):
            state = {}
        if not isinstance(state.get("symbols"), dict):
            state["symbols"] = {}
        if not isinstance(state.get("last_alerts"), dict):
            state["last_alerts"] = {}
        return state

    def _apply_state_transitions(
        self,
        symbol: str,
        rows: list[dict[str, Any]],
        state: dict[str, Any],
    ) -> list[dict[str, Any]]:
        symbol_state = state.get("symbols", {}).get(symbol, {})
        exchanges = symbol_state.get("exchanges", {}) if isinstance(symbol_state, dict) else {}
        result: list[dict[str, Any]] = []
        for raw in rows:
            row = dict(raw)
            exchange = str(row.get("exchange") or "")
            previous = exchanges.get(exchange, {}) if isinstance(exchanges, dict) else {}
            previous_next = to_int(previous.get("next_funding_time_ms")) if isinstance(previous, dict) else 0
            previous_interval = to_int(previous.get("interval_hours")) if isinstance(previous, dict) else 0
            current_next = to_int(row.get("next_funding_time_ms"))
            current_interval = to_int(row.get("interval_hours"))
            if current_interval <= 0 and previous_next > 0 and current_next > previous_next:
                inferred_interval = funding_interval_hours(current_next - previous_next)
                if inferred_interval in VALID_FUNDING_INTERVAL_HOURS:
                    current_interval = inferred_interval
                    row["interval_hours"] = current_interval
                    row["current_interval_hours"] = current_interval
            if (
                not row.get("funding_interval_transition")
                and previous_interval in VALID_FUNDING_INTERVAL_HOURS
                and current_interval in VALID_FUNDING_INTERVAL_HOURS
                and current_interval < previous_interval
            ):
                row["previous_interval_hours"] = previous_interval
                row["current_interval_hours"] = current_interval
                previous_time = str(previous.get("next_funding_time") or funding_time_text(previous_next))
                current_time = str(row.get("next_funding_time") or funding_time_text(current_next))
                row["funding_interval_transition"] = (
                    f"{previous_time} {funding_interval_label(previous_interval)}结算一次"
                    f" → {current_time} {funding_interval_label(current_interval)}结算一次"
                )
            result.append(row)
        return result

    @staticmethod
    def _binance_history_required(row: dict[str, Any]) -> bool:
        if str(row.get("exchange") or "").strip().upper() != "BINANCE":
            return False
        if to_int(row.get("interval_hours")) <= 0:
            return True
        last_ms = to_int(row.get("last_funding_time_ms"))
        next_ms = to_int(row.get("next_funding_time_ms"))
        if last_ms > 0 and next_ms > 0 and last_ms == next_ms:
            return True
        last_time = str(row.get("last_funding_time") or "").strip()
        next_time = str(row.get("next_funding_time") or "").strip()
        return bool(last_time and next_time and last_time == next_time)

    def _backfill_outbound_binance_history(
        self,
        alerts: list[dict[str, Any]],
        funding_settings: Settings,
        http: Any,
        state: dict[str, Any],
        now_ts: int,
    ) -> list[dict[str, Any]]:
        symbols = list(dict.fromkeys(
            str(alert.get("symbol") or "")
            for alert in alerts
            if any(
                isinstance(row, dict) and self._binance_history_required(row)
                for row in (alert.get("rows") or [])
            )
        ))
        symbols = [symbol for symbol in symbols if symbol]
        if not symbols:
            return alerts

        history_client = MultiExchangeFundingClient(
            replace(funding_settings, launch_funding_exchanges=("BINANCE",)),
            http,
        )
        history_rows = history_client.snapshot_many(symbols, include_history=True)
        for alert in alerts:
            symbol = str(alert.get("symbol") or "")
            fetched_binance = next(
                (
                    row for row in history_rows.get(symbol, [])
                    if isinstance(row, dict)
                    and str(row.get("exchange") or "").strip().upper() == "BINANCE"
                ),
                None,
            )
            rows: list[dict[str, Any]] = []
            for raw in alert.get("rows") or []:
                if not isinstance(raw, dict) or not self._binance_history_required(raw):
                    if isinstance(raw, dict):
                        rows.append(raw)
                    continue
                if fetched_binance and not self._binance_history_required(fetched_binance):
                    rows.append(dict(fetched_binance))
                    continue
                unavailable = dict(raw)
                unavailable.update({
                    "interval_hours": 0,
                    "current_interval_hours": 0,
                    "previous_interval_hours": 0,
                    "last_funding_time_ms": 0,
                    "last_funding_time": "",
                    "funding_interval_transition": "",
                    "funding_period_status": "unavailable",
                })
                rows.append(unavailable)
            alert["rows"] = rows
            self._update_symbol_state(symbol, rows, state, now_ts)
        return alerts

    def _maybe_decay_alert(
        self,
        symbol: str,
        candidate: dict[str, Any],
        rows: list[dict[str, Any]],
        state: dict[str, Any],
        now_ts: int,
    ) -> dict[str, Any] | None:
        previous = state.get("symbols", {}).get(symbol, {})
        if not isinstance(previous, dict) or to_int(previous.get("alert_count")) <= 0:
            self._update_symbol_state(symbol, rows, state, now_ts, candidate, None, {"stage": "observation_ended", "quiet_count": 0})
            return None
        quiet_count = to_int(previous.get("quiet_count")) + 1
        stage = "observation_ended" if quiet_count >= self.settings.funding_alert_end_quiet_scans else str(previous.get("stage") or "active")
        if (
            quiet_count >= self.settings.funding_alert_end_quiet_scans
            and previous.get("stage") != "observation_ended"
        ):
            classification = self._decay_classification(rows)
            classification.update({
                "types": ["本轮观察结束"],
                "primary_kind": "observation_ended",
                "risk": "结束",
            })
            tracking = self._tracking_info(
                symbol,
                candidate,
                rows,
                classification,
                state,
                now_ts,
                forced_stage="observation_ended",
                quiet_count=quiet_count,
            )
            alert = {
                "symbol": symbol,
                "rows": rows,
                "classification": classification,
                "dedup_key": self._dedup_key(
                    symbol,
                    classification,
                    tracking["stage"],
                ),
                "text": "",
                **candidate,
                **tracking,
            }
            self._update_symbol_state(
                symbol,
                rows,
                state,
                now_ts,
                candidate,
                classification,
                tracking,
            )
            if self._cooldown_ok(alert["dedup_key"], state, now_ts):
                return alert
            return None
        if quiet_count >= self.settings.funding_alert_decay_quiet_scans and previous.get("stage") not in {"heat_decay", "observation_ended"}:
            classification = self._decay_classification(rows)
            tracking = self._tracking_info(symbol, candidate, rows, classification, state, now_ts, forced_stage="heat_decay", quiet_count=quiet_count)
            alert = {
                "symbol": symbol,
                "rows": rows,
                "classification": classification,
                "dedup_key": self._dedup_key(symbol, classification, tracking["stage"]),
                "text": "",
                **candidate,
                **tracking,
            }
            self._update_symbol_state(symbol, rows, state, now_ts, candidate, classification, tracking)
            if self._cooldown_ok(alert["dedup_key"], state, now_ts):
                return alert
            return None
        self._update_symbol_state(symbol, rows, state, now_ts, candidate, None, {"stage": stage, "quiet_count": quiet_count})
        return None

    def _decay_classification(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        rates = [to_float(row.get("funding_pct")) for row in rows]
        divergence = max(rates) - min(rates) if len(rates) >= 2 else 0.0
        return {
            "types": ["热度衰减"],
            "primary_kind": "heat_decay",
            "risk": "观察",
            "negative_count": 0,
            "positive_count": 0,
            "transition_count": 0,
            "extreme_count": 0,
            "divergence_pct": divergence,
            "max_abs_funding_pct": max((abs(rate) for rate in rates), default=0.0),
            "direction": "费率回归",
        }

    def _tracking_info(
        self,
        symbol: str,
        candidate: dict[str, Any],
        rows: list[dict[str, Any]],
        classification: dict[str, Any],
        state: dict[str, Any],
        now_ts: int,
        forced_stage: str = "",
        quiet_count: int = 0,
    ) -> dict[str, Any]:
        previous = state.get("symbols", {}).get(symbol, {})
        previous = previous if isinstance(previous, dict) else {}
        version_current = (
            to_int(previous.get("funding_tracking_version"))
            == FUNDING_TRACKING_VERSION
        )
        previous_events = [
            dict(event)
            for event in (previous.get("published_events") or [])
            if isinstance(event, dict)
        ] if version_current else []
        cycle_no = max(1, to_int(previous.get("cycle_no"), 1))
        starts_new_cycle = (
            version_current
            and str(previous.get("stage") or "") == "observation_ended"
        )
        if starts_new_cycle:
            cycle_no += 1
            previous_events = []
        stage_previous = previous if version_current and not starts_new_cycle else {}
        stage = forced_stage or self._next_stage(classification, stage_previous)
        replace_message_ids = {
            int(message_id)
            for message_id in (
                list(previous.get("last_message_ids") or [])
                + list(previous.get("pending_delete_message_ids") or [])
            )
            if isinstance(message_id, int) or str(message_id).isdigit()
        }
        if to_int(previous.get("last_message_id")) > 0:
            replace_message_ids.add(to_int(previous.get("last_message_id")))
        previous_event_count = (
            to_int(previous.get("published_event_count"), len(previous_events))
            if previous_events
            else 0
        )
        event_no = previous_event_count + 1
        return {
            "stage": stage,
            "stage_label": stage_label(stage),
            "previous_stage": str(previous.get("stage") or ""),
            "previous_stage_label": stage_label(str(previous.get("stage") or "")),
            "alert_count": event_no,
            "event_no": event_no,
            "cycle_no": cycle_no,
            "published_events_before": previous_events,
            "first_seen": (
                to_int(previous_events[0].get("observed_at"))
                if previous_events
                else now_ts
            ),
            "last_seen": now_ts,
            "quiet_count": quiet_count,
            "reply_to_message_id": 0,
            "replace_message_ids": sorted(replace_message_ids),
            "primary_kind": str(classification.get("primary_kind") or ""),
            "risk": str(classification.get("risk") or ""),
        }

    def _next_stage(self, classification: dict[str, Any], previous: dict[str, Any]) -> str:
        if to_int(previous.get("alert_count")) <= 0:
            return "first_seen"
        current_abs = to_float(classification.get("max_abs_funding_pct"))
        current_extreme_count = to_int(classification.get("extreme_count"))
        previous_peak = to_float(previous.get("peak_abs_funding_pct"))
        previous_extreme_count = to_int(previous.get("last_extreme_count"))
        previous_risk = str(previous.get("last_risk") or "")
        current_risk = str(classification.get("risk") or "")
        if (
            current_extreme_count > previous_extreme_count
            or current_abs >= previous_peak + 0.2
            or self._risk_rank(current_risk) > self._risk_rank(previous_risk)
        ):
            return "crowding_intensifying"
        if (
            current_risk in {"高", "极高"}
            and (
                current_extreme_count >= max(1, self.settings.funding_alert_min_exchange_count)
                or to_int(classification.get("transition_count")) > 0
            )
        ):
            return "high_risk_active"
        if previous_peak > 0 and current_abs <= previous_peak * 0.65:
            return "risk_release"
        return "active"

    @staticmethod
    def _risk_rank(risk: str) -> int:
        return {"观察": 1, "高": 2, "极高": 3}.get(str(risk or ""), 0)

    def _update_symbol_state(
        self,
        symbol: str,
        rows: list[dict[str, Any]],
        state: dict[str, Any],
        now_ts: int,
        candidate: dict[str, Any] | None = None,
        classification: dict[str, Any] | None = None,
        tracking: dict[str, Any] | None = None,
    ) -> None:
        symbols = state.setdefault("symbols", {})
        if not isinstance(symbols, dict):
            return
        previous = symbols.get(symbol, {})
        record = dict(previous) if isinstance(previous, dict) else {}
        record["updated_at"] = now_ts
        record["last_seen"] = now_ts
        record.setdefault("first_seen", now_ts)
        record["exchanges"] = {
            str(row.get("exchange") or ""): {
                "funding_pct": round(to_float(row.get("funding_pct")), 6),
                "interval_hours": to_int(row.get("interval_hours")),
                "current_interval_hours": to_int(row.get("current_interval_hours")) or to_int(row.get("interval_hours")),
                "previous_interval_hours": to_int(row.get("previous_interval_hours")),
                "last_funding_time_ms": to_int(row.get("last_funding_time_ms")),
                "last_funding_time": str(row.get("last_funding_time") or ""),
                "next_funding_time_ms": to_int(row.get("next_funding_time_ms")),
                "next_funding_time": str(row.get("next_funding_time") or ""),
            }
            for row in rows
            if row.get("exchange")
        }
        if candidate:
            record["coin"] = str(candidate.get("coin") or "")
            record["quote_volume"] = round(to_float(candidate.get("quote_volume")), 2)
            record["mcap"] = round(to_float(candidate.get("mcap")), 2)
            record["mcap_source"] = str(candidate.get("mcap_source") or "")
            record["last_price"] = to_float(candidate.get("last_price"))
            record["price_24h_pct"] = to_float(candidate.get("price_24h_pct"))
        if tracking:
            record["stage"] = str(tracking.get("stage") or record.get("stage") or "")
            record["previous_stage"] = str(tracking.get("previous_stage") or "")
            record["quiet_count"] = to_int(tracking.get("quiet_count"))
            record["cycle_no"] = max(
                1,
                to_int(tracking.get("cycle_no"), to_int(record.get("cycle_no"), 1)),
            )
        if classification:
            record["last_primary_kind"] = str(classification.get("primary_kind") or "")
            record["last_risk"] = str(classification.get("risk") or "")
            record["last_extreme_count"] = to_int(classification.get("extreme_count"))
            record["last_divergence_pct"] = round(to_float(classification.get("divergence_pct")), 6)
            record["last_max_abs_funding_pct"] = round(to_float(classification.get("max_abs_funding_pct")), 6)
            record["peak_abs_funding_pct"] = max(
                to_float(record.get("peak_abs_funding_pct")),
                to_float(classification.get("max_abs_funding_pct")),
            )
        symbols[symbol] = record

    def _cooldown_ok(self, key: str, state: dict[str, Any], now_ts: int) -> bool:
        last_alerts = state.setdefault("last_alerts", {})
        if not isinstance(last_alerts, dict):
            return True
        last_ts = to_int(last_alerts.get(key))
        return now_ts - last_ts >= max(60, self.settings.funding_alert_cooldown_sec)

    def _mark_alert(self, key: str, state: dict[str, Any], now_ts: int) -> None:
        last_alerts = state.setdefault("last_alerts", {})
        if isinstance(last_alerts, dict):
            last_alerts[key] = now_ts

    @staticmethod
    def _dedup_key(symbol: str, classification: dict[str, Any], stage: str = "") -> str:
        return f"funding-alert:{symbol}:{classification.get('primary_kind', 'alert')}:{classification.get('risk', '')}:{stage or 'state'}"

    @staticmethod
    def _focus_row(rows: list[dict[str, Any]]) -> dict[str, Any]:
        valid = [row for row in rows if isinstance(row, dict)]
        return next(
            (
                row for row in valid
                if str(row.get("exchange") or "").strip().upper() == "BINANCE"
            ),
            max(valid, key=lambda row: abs(to_float(row.get("funding_pct"))), default={}),
        )

    def _event_snapshot(self, alert: dict[str, Any], now_ts: int) -> dict[str, Any]:
        rows = [
            row for row in (alert.get("rows") or [])
            if isinstance(row, dict)
        ]
        focus = self._focus_row(rows)
        market = alert.get("market_context")
        market = market if isinstance(market, dict) else {}

        def optional_number(value: Any) -> float | None:
            if value in (None, ""):
                return None
            try:
                number = float(value)
            except (TypeError, ValueError):
                return None
            return number if number == number and abs(number) != float("inf") else None

        classification = alert.get("classification")
        classification = classification if isinstance(classification, dict) else {}
        cycle_no = max(1, to_int(alert.get("cycle_no"), 1))
        event_no = max(1, to_int(alert.get("event_no"), 1))
        return {
            "event_id": f"funding:{alert.get('symbol')}:{cycle_no}:{event_no}:{now_ts}",
            "event_no": event_no,
            "observed_at": now_ts,
            "stage": str(alert.get("stage") or ""),
            "stage_label": str(alert.get("stage_label") or ""),
            "risk": str(classification.get("risk") or "观察"),
            "primary_kind": str(classification.get("primary_kind") or ""),
            "types": [
                str(value)
                for value in (classification.get("types") or [])
                if value
            ],
            "judgment": self._judgment_text(
                classification,
                str(alert.get("stage") or ""),
            ),
            "exchange": str(focus.get("exchange") or ""),
            "funding_pct": to_float(focus.get("funding_pct")),
            "interval_hours": to_int(focus.get("interval_hours")),
            "next_funding_time": str(focus.get("next_funding_time") or ""),
            "price": optional_number(market.get("price"))
            or optional_number(alert.get("last_price")),
            "oi_usd": optional_number(market.get("oi_usd")),
            "price_15m_pct": optional_number(market.get("price_change_pct")),
            "oi_15m_pct": optional_number(market.get("oi_change_pct")),
            "spot_active_net_usd": optional_number(market.get("spot_flow_usd")),
            "futures_active_net_usd": optional_number(market.get("futures_flow_usd")),
            "market_data_status": str(market.get("status") or "unavailable"),
        }

    @staticmethod
    def _snapshot_rate(snapshot: dict[str, Any]) -> str:
        rate = to_float(snapshot.get("funding_pct"))
        interval = to_int(snapshot.get("interval_hours"))
        suffix = f"{interval}H" if interval > 0 else "周期暂不可用"
        return f"{rate:+.3f}%/{suffix}"

    @staticmethod
    def _snapshot_price(snapshot: dict[str, Any]) -> str:
        value = snapshot.get("price")
        return f"${float(value):.6g}" if value not in (None, "") else "暂不可用"

    @staticmethod
    def _snapshot_oi(snapshot: dict[str, Any]) -> str:
        value = snapshot.get("oi_usd")
        return fmt_money(float(value)) if value not in (None, "") else "暂不可用"

    @staticmethod
    def _signed_money(value: Any) -> str:
        if value in (None, ""):
            return "暂不可用"
        number = float(value)
        return f"{'+' if number >= 0 else '-'}{fmt_money(abs(number))}"

    @classmethod
    def _active_flow_text(cls, snapshot: dict[str, Any]) -> str:
        return (
            f"现货 {cls._signed_money(snapshot.get('spot_active_net_usd'))}｜"
            f"合约 {cls._signed_money(snapshot.get('futures_active_net_usd'))}"
        )

    @staticmethod
    def _relative_metric(
        label: str,
        first: dict[str, Any],
        current: dict[str, Any],
        key: str,
    ) -> str:
        change = percent_change(current.get(key), first.get(key))
        if change is None:
            return f"{label}: 暂不可比"
        return f"{label}: {change:+.2f}%"

    @staticmethod
    def _funding_change_text(
        first: dict[str, Any],
        current: dict[str, Any],
    ) -> str:
        start = to_float(first.get("funding_pct"))
        latest = to_float(current.get("funding_pct"))
        if start < 0 and latest < start:
            trend = "负费率加深"
        elif start < 0 and latest > start:
            trend = "负费率缓解"
        elif start > 0 and latest > start:
            trend = "正费率升高"
        elif start > 0 and latest < start:
            trend = "正费率回落"
        else:
            trend = "方向发生变化" if start * latest < 0 else "变化有限"
        return (
            f"资金费率: {FundingAlertEngine._snapshot_rate(first)} → "
            f"{FundingAlertEngine._snapshot_rate(current)}（{trend}）"
        )

    @staticmethod
    def _interval_change_text(
        first: dict[str, Any],
        current: dict[str, Any],
    ) -> str:
        start = to_int(first.get("interval_hours"))
        latest = to_int(current.get("interval_hours"))
        if start <= 0 or latest <= 0:
            return "结算周期: 暂不可比"
        if latest < start:
            change = "结算频率加快"
        elif latest > start:
            change = "结算频率放缓"
        else:
            change = "不变"
        return f"结算周期: {start}H → {latest}H（{change}）"

    def _tracking_conclusion(
        self,
        first: dict[str, Any],
        current: dict[str, Any],
    ) -> str:
        funding_start = to_float(first.get("funding_pct"))
        funding_now = to_float(current.get("funding_pct"))
        price_change = percent_change(current.get("price"), first.get("price"))
        oi_change = percent_change(current.get("oi_usd"), first.get("oi_usd"))
        if (
            funding_now < funding_start < 0
            and price_change is not None
            and price_change >= 0
            and oi_change is not None
            and oi_change > 0
        ):
            return "负费率继续加深，但价格抗跌且OI增加，空头拥挤正在增强。"
        if (
            funding_now > funding_start > 0
            and price_change is not None
            and price_change <= 0
            and oi_change is not None
            and oi_change > 0
        ):
            return "正费率继续升高，但价格滞涨且OI增加，多头拥挤正在增强。"
        if abs(funding_now) < abs(funding_start):
            return "资金费率极端程度较首次缓解，继续观察OI是否同步下降。"
        return "资金费率仍处于异常区间，需要结合价格、OI和主动成交继续确认。"

    def _risk_change_text(
        self,
        previous: dict[str, Any],
        current: dict[str, Any],
    ) -> str:
        if not previous:
            return "首次确认"
        previous_rank = self._risk_rank(str(previous.get("risk") or ""))
        current_rank = self._risk_rank(str(current.get("risk") or ""))
        if current_rank > previous_rank:
            return "继续升高 ↑"
        if current_rank < previous_rank:
            return "正在缓解 ↓"
        previous_rate = abs(to_float(previous.get("funding_pct")))
        current_rate = abs(to_float(current.get("funding_pct")))
        if current_rate > previous_rate + 0.05:
            return "极端程度扩大 ↑"
        if current_rate + 0.05 < previous_rate:
            return "极端程度回落 ↓"
        return "基本稳定 →"

    def _format_alert(self, alert: dict[str, Any]) -> str:
        symbol = str(alert.get("symbol") or "")
        rows = alert.get("rows", [])
        rows = rows if isinstance(rows, list) else []
        classification = alert.get("classification", {})
        classification = classification if isinstance(classification, dict) else {}
        types = " + ".join(str(item) for item in classification.get("types", []) if item) or "资金费率异常"
        risk = str(classification.get("risk") or "观察")
        stage = str(alert.get("stage") or "")
        stage_text = str(alert.get("stage_label") or stage_label(stage))
        judgment = self._judgment_text(classification, stage)
        data_confirmation = alert.get("data_confirmation")
        data_confirmation = data_confirmation if isinstance(data_confirmation, dict) else {}
        confirmed_exchanges = [
            str(exchange) for exchange in (data_confirmation.get("exchanges") or []) if exchange
        ]
        confirmation_text = (
            f"原生交易所接口 {len(confirmed_exchanges)}所（{' / '.join(confirmed_exchanges)}）"
            if confirmed_exchanges
            else "原生交易所接口缺失"
        )
        current = alert.get("event_snapshot")
        current = current if isinstance(current, dict) else {}
        first = alert.get("first_snapshot")
        first = first if isinstance(first, dict) else current
        previous = alert.get("previous_snapshot")
        previous = previous if isinstance(previous, dict) else {}
        cycle_no = max(1, to_int(alert.get("cycle_no"), 1))
        event_no = max(1, to_int(alert.get("event_no"), 1))
        duration_sec = max(
            0,
            to_int(current.get("observed_at")) - to_int(first.get("observed_at")),
        )
        duration_text = (
            f"{duration_sec // 3600}小时{(duration_sec % 3600) // 60:02d}分钟"
            if duration_sec >= 3600
            else f"{duration_sec // 60}分钟"
        )
        next_settlement = str(current.get("next_funding_time") or "暂不可用")
        direction = str(classification.get("direction") or "拥挤方向待确认")
        event_history = [
            dict(event)
            for event in (alert.get("published_events_before") or [])
            if isinstance(event, dict)
        ] + [dict(current)]
        market_snapshot_text = (
            "Binance 15m闭合窗口完整"
            if (
                str(current.get("market_data_status") or "")
                not in {"", "unavailable", "stale"}
                and current.get("price") not in (None, "")
                and current.get("oi_usd") not in (None, "")
            )
            else "Binance 15m闭合窗口部分缺失"
        )
        lines = [
            (
                f"⚠️ {coin_link(symbol)}｜第{cycle_no}轮资金费率跟踪｜"
                f"事件{event_number_text(event_no)}"
            ),
            f"⏰ {cst_now_text()}",
            "",
            tg_quote("当前状态"),
            f"阶段: {tg_escape(stage_text)}",
            f"持续时间: {tg_escape(duration_text)}",
            f"当前费率: {tg_bold(self._snapshot_rate(current))}",
            f"下次结算: {tg_escape(next_settlement)}",
            f"警报类型: {tg_escape(types)}",
            f"拥挤方向: {tg_escape(direction)}",
            f"风险等级: {tg_escape(risk)}",
            f"风险变化: {tg_escape(self._risk_change_text(previous, current))}",
            f"15m主动成交: {tg_escape(self._active_flow_text(current))}",
            "",
            tg_quote("交易所资金费率"),
            funding_alert_cards(
                [row for row in rows if isinstance(row, dict)],
                self.settings,
            ),
            "",
            tg_quote("开始监控时"),
            f"时间: {short_cst_time(first.get('observed_at'))} CST",
            f"触发类型: {tg_escape(' + '.join(first.get('types') or []) or types)}",
            f"资金费率: {tg_escape(self._snapshot_rate(first))}",
            f"价格: {tg_escape(self._snapshot_price(first))}",
            f"OI: {tg_escape(self._snapshot_oi(first))}",
            f"主动成交: {tg_escape(self._active_flow_text(first))}",
            f"当时判断: {tg_escape(str(first.get('judgment') or '继续观察资金费率与市场结构'))}",
            "",
            tg_quote("相对首次信号"),
            tg_escape(self._funding_change_text(first, current)),
            tg_escape(self._interval_change_text(first, current)),
            tg_escape(self._relative_metric("价格", first, current, "price")),
            tg_escape(self._relative_metric("OI", first, current, "oi_usd")),
            (
                "合约主动净额: "
                f"{tg_escape(self._signed_money(first.get('futures_active_net_usd')))}"
                " → "
                f"{tg_escape(self._signed_money(current.get('futures_active_net_usd')))}"
            ),
            f"结论: {tg_escape(self._tracking_conclusion(first, current))}",
        ]
        if previous:
            lines.extend([
                "",
                tg_quote("相对上次更新"),
                tg_escape(self._funding_change_text(previous, current)),
                tg_escape(self._interval_change_text(previous, current)),
                tg_escape(self._relative_metric("价格", previous, current, "price")),
                tg_escape(self._relative_metric("OI", previous, current, "oi_usd")),
                (
                    "合约主动净额: "
                    f"{tg_escape(self._signed_money(previous.get('futures_active_net_usd')))}"
                    " → "
                    f"{tg_escape(self._signed_money(current.get('futures_active_net_usd')))}"
                ),
            ])
        lines.extend(["", tg_quote("本轮事件轴")])
        display_events = (
            [event_history[0], *event_history[-7:]]
            if len(event_history) > 8
            else event_history
        )
        for event in display_events:
            lines.append(
                f"{event_number_text(event.get('event_no'))} "
                f"{short_cst_time(event.get('observed_at'))}｜"
                f"{tg_escape(self._snapshot_rate(event))}｜"
                f"{tg_escape(str(event.get('stage_label') or stage_label(str(event.get('stage') or ''))))}"
            )
        if len(event_history) > len(display_events):
            lines.append(
                f"… 中间 {len(event_history) - len(display_events)} 次事件已归档"
            )
        lines.extend([
            "",
            tg_quote("当前判断"),
            tg_escape(judgment),
            "",
            (
                f"数据确认: {tg_escape(confirmation_text)}｜"
                f"{tg_escape(market_snapshot_text)}｜本轮已发布事件 {event_no}次"
            ),
        ])
        return "\n".join(lines)

    def _judgment_text(self, classification: dict[str, Any], stage: str = "") -> str:
        if stage == "observation_ended":
            return "资金费率异常已经连续多个扫描周期未再满足触发条件，本轮跟踪结束；以后重新触发将作为新一轮记录。"
        if stage == "heat_decay":
            return "极端资金费率已经连续回落，说明拥挤交易正在降温；后续重点看价格是否完成风险释放，避免把热度衰减误判成新启动。"
        if stage == "risk_release":
            return "资金费率仍异常，但极端程度相对前高明显回落，说明部分拥挤仓位可能已经释放；继续观察价格是否出现插针或反向波动。"
        if stage == "crowding_intensifying":
            return "相较上一次追踪，资金费率更极端或异常交易所更多，说明拥挤正在加剧；这是风险升级信号，不宜只按普通费率异常处理。"
        primary = str(classification.get("primary_kind") or "")
        if primary == "interval_shortened":
            return "交易所缩短资金费率结算周期，说明该合约波动和风险正在上升，应按高风险事件处理。"
        if primary == "multi_negative":
            return "多家交易所同步极负，说明空头拥挤严重；如果价格不继续下跌，容易形成挤空燃料。"
        if primary == "extreme_negative":
            exchanges = " / ".join(str(item) for item in classification.get("negative_exchanges", []) if item)
            source = exchanges or "单交易所"
            return f"{source} 出现极负费率，说明该合约空头拥挤；如果价格不再继续下跌，同时 OI、主动成交出现反转，可能形成挤空条件。"
        if primary == "multi_positive":
            return "多家交易所同步极正，说明多头拥挤，价格滞涨时追高风险明显上升。"
        if primary == "extreme_positive":
            exchanges = " / ".join(str(item) for item in classification.get("positive_exchanges", []) if item)
            source = exchanges or "单交易所"
            return f"{source} 出现极正费率，说明该合约多头拥挤；价格滞涨时应注意回落和插针风险。"
        if primary == "exchange_divergence":
            return "不同交易所资金费率差距过大，可能存在单所盘口异常、资金拥挤或套利资金迁移。"
        return "资金费率出现异常，需要结合价格、OI、成交量和结算周期继续确认。"
