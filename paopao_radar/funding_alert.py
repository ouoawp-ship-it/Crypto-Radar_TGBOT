from __future__ import annotations

import re
import time
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
from .storage import JsonStore


CST = timezone(timedelta(hours=8))
TEMPLATE_ID = "TG_FUNDING_ALERT"

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
    text = str(symbol or "").upper().strip()
    if not text.endswith("USDT"):
        text = f"{text}USDT"
    return f"https://www.coinglass.com/tv/zh/Binance_{escape(text, quote=True)}"


def coin_link(symbol: str) -> str:
    text = str(symbol or "").upper().strip()
    coin = text[:-4] if text.endswith("USDT") else text
    return f'<a href="{coinglass_tv_url(text)}"><b>{tg_escape(coin)}</b></a>'


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


def funding_table(rows: list[dict[str, Any]], settings: Settings) -> str:
    lines = [f"{'交易所':<9}{'费率/周期':<18}{'上次结算':<14}{'本次周期':<9}{'下次结算'}"]
    for row in rows:
        exchange = str(row.get("exchange") or "Unknown").strip()[:9]
        funding_pct = to_float(row.get("funding_pct"))
        interval_hours = to_int(row.get("interval_hours"))
        rate = funding_cycle_text(funding_pct, interval_hours)
        label = funding_rate_label(funding_pct, settings)
        rate_text = f"{rate} {label}".strip()
        last_time = short_funding_time(funding_last_settlement_text(row))
        period = funding_settlement_period_text(row)
        next_time = short_funding_time(str(row.get("next_funding_time") or ""))
        lines.append(f"{exchange:<9}{rate_text:<18}{last_time:<14}{period:<9}{next_time}")
    return "<pre>" + tg_escape("\n".join(lines)) + "</pre>"


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

    types: list[str] = []
    primary_kind = ""
    if transitions:
        types.append("结算周期缩短")
        primary_kind = primary_kind or "interval_shortened"
    if len(extreme_negative) >= max(1, settings.funding_alert_min_exchange_count):
        types.append("多所极负共振")
        primary_kind = primary_kind or "multi_negative"
    elif extreme_negative:
        types.append("极负资金费率")
        primary_kind = primary_kind or "extreme_negative"
    if len(extreme_positive) >= max(1, settings.funding_alert_min_exchange_count):
        types.append("多所极正共振")
        primary_kind = primary_kind or "multi_positive"
    elif extreme_positive:
        types.append("极正资金费率")
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

        for candidate in candidates:
            symbol = str(candidate.get("symbol") or "")
            rows = client.snapshot(symbol, include_history=False)
            if not rows:
                continue
            scanned += 1
            rows_seen += len(rows)
            rows = self._apply_state_transitions(symbol, rows, state)
            classification = classify_funding_alert(rows, self.settings)
            if not classification:
                decay_alert = self._maybe_decay_alert(symbol, candidate, rows, state, now_ts)
                if decay_alert:
                    alerts.append(decay_alert)
                    self._mark_alert(decay_alert["dedup_key"], state, now_ts)
                continue

            full_rows = client.snapshot(symbol, include_history=True)
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
                **candidate,
                **tracking,
            }
            self._update_symbol_state(symbol, rows, state, now_ts, candidate, classification, tracking)
            if self._cooldown_ok(alert["dedup_key"], state, now_ts):
                alert["text"] = self._format_alert(alert)
                alerts.append(alert)
                self._mark_alert(alert["dedup_key"], state, now_ts)

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
            },
        }

    def mark_pushed(self, alerts: list[dict[str, Any]]) -> None:
        if not alerts:
            return
        state = self._load_state()
        now_ts = int(time.time())
        changed = False
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
            record["last_message_id"] = message_ids[0]
            record["last_message_ids"] = message_ids
            record["last_message_stage"] = str(alert.get("stage") or "")
            record["last_pushed"] = now_ts
            record["last_pushed_kind"] = str(alert.get("primary_kind") or alert.get("classification", {}).get("primary_kind") or "")
            state["symbols"][symbol] = record
            changed = True
        if changed:
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
        candidates = candidates[: self.settings.funding_alert_scan_limit]
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
                current_interval = funding_interval_hours(current_next - previous_next)
                row["interval_hours"] = current_interval
                row["current_interval_hours"] = current_interval
            if previous_next > 0 and not row.get("last_funding_time_ms"):
                row["last_funding_time_ms"] = previous_next
                row["last_funding_time"] = str(previous.get("next_funding_time") or funding_time_text(previous_next))
            if (
                not row.get("funding_interval_transition")
                and previous_interval > 0
                and current_interval > 0
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
                alert["text"] = self._format_alert(alert)
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
        previous_count = to_int(previous.get("alert_count"))
        stage = forced_stage or self._next_stage(classification, previous)
        reply_to_message_id = (
            to_int(previous.get("last_message_id"))
            if self.settings.funding_alert_reply_chain_enable and previous_count > 0
            else 0
        )
        return {
            "stage": stage,
            "stage_label": stage_label(stage),
            "previous_stage": str(previous.get("stage") or ""),
            "previous_stage_label": stage_label(str(previous.get("stage") or "")),
            "alert_count": previous_count + 1,
            "first_seen": previous.get("first_seen") or now_ts,
            "last_seen": now_ts,
            "quiet_count": quiet_count,
            "reply_to_message_id": reply_to_message_id,
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
            if "alert_count" in tracking:
                record["alert_count"] = to_int(tracking.get("alert_count"))
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

    def _format_alert(self, alert: dict[str, Any]) -> str:
        symbol = str(alert.get("symbol") or "")
        rows = alert.get("rows", [])
        rows = rows if isinstance(rows, list) else []
        classification = alert.get("classification", {})
        classification = classification if isinstance(classification, dict) else {}
        transition_lines = [
            f"{row.get('exchange')}: {row.get('funding_interval_transition')}"
            for row in rows
            if isinstance(row, dict) and row.get("funding_interval_transition")
        ]
        types = " + ".join(str(item) for item in classification.get("types", []) if item) or "资金费率异常"
        risk = str(classification.get("risk") or "观察")
        divergence = to_float(classification.get("divergence_pct"))
        stage = str(alert.get("stage") or "")
        stage_text = str(alert.get("stage_label") or stage_label(stage))
        count = max(1, to_int(alert.get("alert_count"), 1))
        track_text = "首次发现" if count <= 1 else f"第{count}次追踪"
        if to_int(alert.get("reply_to_message_id")) > 0:
            track_text = f"{track_text}｜回复上一条同币信号"
        market_cap = to_float(alert.get("mcap"))
        market_cap_source = str(alert.get("mcap_source") or "").strip()
        quote_volume = to_float(alert.get("quote_volume"))
        market_cap_text = (
            f"{fmt_money(market_cap)}（{market_cap_tier(market_cap)}，来源 {market_cap_source or '未知'}）"
            if market_cap > 0
            else "暂无数据（未知市值）"
        )
        liquidity_text = (
            f"{fmt_money(quote_volume)}/24h（{liquidity_tier(quote_volume)}）"
            if quote_volume > 0
            else "暂无数据（未知流动性）"
        )
        judgment = self._judgment_text(classification, stage)
        lines = [
            f"⚠️ {tg_bold('资金费率警报')} {coin_link(symbol)}",
            f"⏰ {cst_now_text()}",
            "",
            f"{tg_bold('阶段')}: {tg_escape(stage_text)}",
            f"{tg_bold('追踪')}: {tg_escape(track_text)}",
            f"{tg_bold('警报类型')}: {tg_escape(types)}",
            f"{tg_bold('风险等级')}: {tg_escape(risk)}",
            "",
            tg_quote("市场概况"),
            f"市值: {tg_escape(market_cap_text)}",
            f"24h成交额: {tg_escape(liquidity_text)}",
            "",
            f"{tg_bold('交易所偏离')}: {divergence:.3f}%",
            "说明: 最高资金费率和最低资金费率之间的差值；偏离越大，越说明不同交易所合约拥挤程度不一致，可能是单所盘口异常、局部清算压力或套利资金迁移。",
            "",
            tg_quote("多交易所资金费率"),
            funding_table([row for row in rows if isinstance(row, dict)], self.settings),
        ]
        if transition_lines:
            lines.extend(["", tg_quote("周期变化"), *[tg_escape(line) for line in transition_lines]])
        lines.extend([
            "",
            tg_quote("判断"),
            tg_escape(judgment),
            "",
            tg_quote("风险"),
            "资金费率只代表合约拥挤程度；极端费率币种容易上下插针，必须结合价格、OI 和流动性确认。",
        ])
        return "\n".join(lines)

    def _judgment_text(self, classification: dict[str, Any], stage: str = "") -> str:
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
            return "单交易所出现极负费率，优先判断该交易所合约是否出现空头拥挤或盘口异常。"
        if primary == "multi_positive":
            return "多家交易所同步极正，说明多头拥挤，价格滞涨时追高风险明显上升。"
        if primary == "extreme_positive":
            return "单交易所出现极正费率，说明局部多头拥挤，注意回落和插针风险。"
        if primary == "exchange_divergence":
            return "不同交易所资金费率差距过大，可能存在单所盘口异常、资金拥挤或套利资金迁移。"
        return "资金费率出现异常，需要结合价格、OI、成交量和结算周期继续确认。"
