from __future__ import annotations

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
    funding_interval_hours,
    funding_interval_label,
    funding_time_text,
    to_float,
    to_int,
)
from .storage import JsonStore


CST = timezone(timedelta(hours=8))
TEMPLATE_ID = "TG_FUNDING_ALERT"


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


def funding_row_text(row: dict[str, Any], settings: Settings | None = None) -> str:
    exchange = str(row.get("exchange") or "Unknown").strip()
    funding_pct = to_float(row.get("funding_pct"))
    interval_hours = to_int(row.get("interval_hours"))
    text = funding_cycle_text(funding_pct, interval_hours)
    label = str(row.get("extreme_label") or funding_extreme_label(funding_pct)).strip()
    if settings is not None and not label:
        if funding_pct <= settings.funding_alert_extreme_negative_pct:
            label = "极负"
        elif funding_pct >= settings.funding_alert_extreme_positive_pct:
            label = "极正"
    if label:
        text = f"{text}（{label}）"
    next_time = str(row.get("next_funding_time") or "").strip() or "未知"
    return f"{exchange}: {text}｜下次结算 {tg_escape(next_time)}"


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
    transitions = [
        row for row in rows
        if str(row.get("funding_interval_transition") or "").strip()
    ]
    rates = [to_float(row.get("funding_pct")) for row in rows]
    divergence = max(rates) - min(rates) if len(rates) >= 2 else 0.0

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
    if super_negative or any(to_float(row.get("funding_pct")) >= abs(settings.funding_alert_super_negative_pct) for row in rows):
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
        "divergence_pct": divergence,
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
        symbols = self._candidate_symbols(source)
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

        for symbol in symbols:
            rows = client.snapshot(symbol, include_history=False)
            if not rows:
                continue
            scanned += 1
            rows_seen += len(rows)
            rows = self._apply_state_transitions(symbol, rows, state)
            classification = classify_funding_alert(rows, self.settings)
            if not classification:
                self._update_symbol_state(symbol, rows, state, now_ts)
                continue

            full_rows = client.snapshot(symbol, include_history=True)
            if full_rows:
                full_rows = self._apply_state_transitions(symbol, full_rows, state)
                rows = full_rows
                classification = classify_funding_alert(rows, self.settings) or classification
            self._update_symbol_state(symbol, rows, state, now_ts)
            alert = {
                "symbol": symbol,
                "rows": rows,
                "classification": classification,
                "dedup_key": self._dedup_key(symbol, classification),
                "text": "",
            }
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
                "candidates": len(symbols),
                "scanned": scanned,
                "funding_rows": rows_seen,
                "alerts": len(alerts),
                "exchanges": list(self.settings.funding_alert_exchanges),
            },
        }

    def _empty_result(self, reason: str) -> dict[str, Any]:
        return {
            "template_id": TEMPLATE_ID,
            "messages": [],
            "alerts": [],
            "diagnostics": {"status": reason, "alerts": 0},
        }

    def _candidate_symbols(self, source: BinanceDataSource) -> list[str]:
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
            candidates.append({"symbol": symbol, "quote_volume": quote_volume})
        candidates.sort(key=lambda item: item["quote_volume"], reverse=True)
        return [item["symbol"] for item in candidates[: self.settings.funding_alert_scan_limit]]

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
            if (
                not row.get("funding_interval_transition")
                and previous_interval > 0
                and current_interval > 0
                and current_interval < previous_interval
            ):
                previous_time = str(previous.get("next_funding_time") or funding_time_text(previous_next))
                current_time = str(row.get("next_funding_time") or funding_time_text(current_next))
                row["funding_interval_transition"] = (
                    f"{previous_time} {funding_interval_label(previous_interval)}结算一次"
                    f" → {current_time} {funding_interval_label(current_interval)}结算一次"
                )
            result.append(row)
        return result

    def _update_symbol_state(self, symbol: str, rows: list[dict[str, Any]], state: dict[str, Any], now_ts: int) -> None:
        symbols = state.setdefault("symbols", {})
        if not isinstance(symbols, dict):
            return
        symbols[symbol] = {
            "updated_at": now_ts,
            "exchanges": {
                str(row.get("exchange") or ""): {
                    "funding_pct": round(to_float(row.get("funding_pct")), 6),
                    "interval_hours": to_int(row.get("interval_hours")),
                    "next_funding_time_ms": to_int(row.get("next_funding_time_ms")),
                    "next_funding_time": str(row.get("next_funding_time") or ""),
                }
                for row in rows
                if row.get("exchange")
            },
        }

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
    def _dedup_key(symbol: str, classification: dict[str, Any]) -> str:
        return f"funding-alert:{symbol}:{classification.get('primary_kind', 'alert')}:{classification.get('risk', '')}"

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
        types = " + ".join(str(item) for item in classification.get("types", []) if item)
        if not types:
            types = "资金费率异常"
        risk = str(classification.get("risk") or "观察")
        divergence = to_float(classification.get("divergence_pct"))
        judgment = self._judgment_text(classification)
        lines = [
            f"⚠️ {tg_bold('资金费率警报')} {coin_link(symbol)}",
            f"⏰ {cst_now_text()}",
            "",
            f"{tg_bold('警报类型')}: {tg_escape(types)}",
            f"{tg_bold('风险等级')}: {tg_escape(risk)}",
            f"{tg_bold('交易所偏离')}: {divergence:.3f}%",
            "",
            tg_quote("多交易所资金费率"),
            *[funding_row_text(row, self.settings) for row in rows if isinstance(row, dict)],
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

    def _judgment_text(self, classification: dict[str, Any]) -> str:
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
