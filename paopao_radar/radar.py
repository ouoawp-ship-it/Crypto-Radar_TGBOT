from __future__ import annotations

import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Callable, Optional

from .binance_confirmation import (
    apply_binance_confirmation,
    confirmation_summary,
    confirmation_text,
)
from .config import Settings
from .data_sources import BinanceDataSource
from .funding_alert import funding_table
from .funding_sources import funding_last_settlement_text, funding_settlement_period_text
from .launch_chart import render_launch_chart_png
from .launch_lifecycle import LaunchLifecycleStore
from .market_links import coinglass_tv_url as _coinglass_tv_url
from .market_links import telegram_coin_links
from .storage import JsonStore
from .time_windows import ClosedWindow, closed_window


OPPORTUNITY_KEYWORDS = [
    "alpha", "airdrop", "tge", "token generation", "will list", "will launch", "将上线",
    "上线", "launchpool", "hodler", "megadrop", "binance wallet", "exclusive",
    "trading tournament", "trade to share", "token vouchers", "campaign", "rewards",
]
RISK_KEYWORDS = [
    "delist", "delisting", "remove", "will remove", "will delist", "下架", "移除",
    "停止交易", "cease trading", "suspend trading",
]
EXCLUDE_OPPORTUNITY_KEYWORDS = [
    "completed", "已完成", "maintenance", "维护", "trading bots services",
    "futures will launch", "perpetual contract", "usdⓈ-margined", "usd-margined",
    "coin-margined", "tradfi", "pre-ipo", "margin will add", "trading pairs",
]
ANNOUNCEMENT_WORD_BLACKLIST = {
    "BINANCE", "ALPHA", "WILL", "LIST", "LAUNCH", "REMOVE", "DELIST", "DELIS",
    "DELISTING", "MARGIN", "LOANS", "FUTURES", "SPOT", "EARN", "HODLER",
    "AIRDROPS", "AIRDROP", "WITH", "AND", "ON", "THE", "TO", "FOR", "TAG",
    "SEED", "APPLIED", "INTRODUCING", "USDT", "USD", "FDUSD", "USDC", "NFT",
    "API", "VIP", "BNB", "BSC",
}
CHAIN_CONTEXT_SYMBOLS = {
    "SOL", "BSC", "ETH", "BASE", "ARB", "OP", "BNB", "TRX", "TRON", "AVAX",
    "POLYGON", "MATIC", "SUI", "APT", "TON",
}
CHAIN_SYMBOL_TOKEN_NAMES = {
    "SOL": {"solana"},
    "ETH": {"ethereum"},
    "BNB": {"bnb", "binance coin"},
    "MATIC": {"matic", "polygon"},
    "AVAX": {"avalanche"},
    "APT": {"aptos"},
    "SUI": {"sui"},
    "TON": {"toncoin"},
}
CST = timezone(timedelta(hours=8))


def cst_now_text(fmt: str = "%m-%d %H:%M CST") -> str:
    return datetime.now(CST).strftime(fmt)


def tg_escape(value: Any) -> str:
    return escape(str(value), quote=False)


def tg_bold(value: Any) -> str:
    return f"<b>{tg_escape(value)}</b>"


def tg_quote(title: str) -> str:
    return f"<blockquote><b>{tg_escape(title)}</b></blockquote>"


def launch_funds_direction(spot_active_net_usd: Any, futures_active_net_usd: Any) -> str:
    spot = to_float(spot_active_net_usd)
    futures = to_float(futures_active_net_usd)
    if spot == 0 or futures == 0:
        return "unknown"
    if spot > 0 and futures > 0:
        return "both_buy"
    if spot < 0 and futures < 0:
        return "both_sell"
    if spot > 0 > futures:
        return "divergence_spot_buy_futures_sell"
    return "divergence_spot_sell_futures_buy"


def seconds_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def coinglass_tv_url(coin_or_symbol: str) -> str:
    return _coinglass_tv_url(coin_or_symbol)


def coin_link(item: dict[str, Any]) -> str:
    raw = str(item.get("coin") or item.get("symbol") or "")
    return telegram_coin_links(raw)


def pct_cell(value: float, width: int = 7, decimals: int = 1) -> str:
    return f"{value:+.{decimals}f}%".rjust(width)


def score_cell(value: int) -> str:
    return f"{value:>3}分"


def append_metric_row(lines: list[str], item: dict[str, Any], metrics: str) -> None:
    lines.append(coin_link(item))
    lines.append(tg_escape(metrics))


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def pct(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return (current - previous) / previous * 100


def fmt_price(value: float) -> str:
    if value >= 1:
        return f"${value:.3g}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6g}"


def fmt_money(value: float) -> str:
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


def funding_trend(previous: Optional[float], current: float) -> str:
    if previous is None:
        return "🆕"
    if previous >= 0 and current < 0:
        return "⬇️变负"
    delta = current - previous
    if current < 0 and delta < -0.01:
        return "🔥加速"
    if current < 0 and delta > 0.01:
        return "⬆️回升"
    return "➡️"


def funding_time_text(value_ms: float) -> str:
    if value_ms <= 0:
        return ""
    return datetime.fromtimestamp(value_ms / 1000, CST).strftime("%Y-%m-%d %H:%M:%S")


def funding_interval_hours(value_ms: float) -> int:
    if value_ms <= 0:
        return 0
    hours = int(round(value_ms / 3_600_000))
    return hours if hours > 0 else 0


def funding_interval_label(hours: int) -> str:
    if hours <= 0:
        return "未知周期"
    return f"{hours}H"


def funding_cycle_text(funding_pct: float, interval_hours: int) -> str:
    if interval_hours > 0:
        return f"{funding_pct:+.3f}%/{funding_interval_label(interval_hours)}"
    return f"{funding_pct:+.3f}%"


def funding_extreme_label(funding_pct: float) -> str:
    if funding_pct <= -1.0:
        return "极负"
    if funding_pct <= -0.5:
        return "极负"
    return ""


def funding_interval_transition(history: list[dict[str, Any]], next_time_ms: int = 0) -> dict[str, Any]:
    points = sorted(
        [
            {
                "time": int(to_float(item.get("fundingTime"))),
                "rate": to_float(item.get("fundingRate")) * 100,
            }
            for item in history
            if to_float(item.get("fundingTime")) > 0
        ],
        key=lambda item: item["time"],
    )
    if next_time_ms > 0 and (not points or next_time_ms > points[-1]["time"]):
        points.append({"time": next_time_ms, "rate": 0.0})
    if len(points) < 3:
        return {}
    previous_interval = funding_interval_hours(points[-2]["time"] - points[-3]["time"])
    current_interval = funding_interval_hours(points[-1]["time"] - points[-2]["time"])
    if previous_interval <= 0 or current_interval <= 0:
        return {}
    if current_interval >= previous_interval:
        return {"current_interval_hours": current_interval}
    previous_time = points[-2]["time"]
    current_time = points[-1]["time"]
    return {
        "current_interval_hours": current_interval,
        "previous_interval_hours": previous_interval,
        "previous_funding_time_ms": previous_time,
        "current_funding_time_ms": current_time,
        "transition_text": (
            f"{funding_time_text(previous_time)} {funding_interval_label(previous_interval)}结算一次"
            f" → {funding_time_text(current_time)} {funding_interval_label(current_interval)}结算一次"
        ),
    }


def estimate_sideways_days(klines: list[list[Any]], max_range_pct: float = 80.0) -> int:
    if not klines:
        return 0
    highs: list[float] = []
    lows: list[float] = []
    days = 0
    for kline in reversed(klines):
        high = to_float(kline[2])
        low = to_float(kline[3])
        if high <= 0 or low <= 0:
            continue
        highs.append(high)
        lows.append(low)
        days += 1
        low_all = min(lows)
        high_all = max(highs)
        if low_all > 0 and (high_all - low_all) / low_all * 100 > max_range_pct:
            return max(0, days - 1)
    return days


def score_funding(funding_pct: float) -> int:
    if funding_pct < -0.5:
        return 25
    if funding_pct < -0.1:
        return 22
    if funding_pct < -0.05:
        return 18
    if funding_pct < -0.03:
        return 14
    if funding_pct < -0.01:
        return 10
    if funding_pct < 0:
        return 5
    return 0


def score_mcap(mcap: float, max_score: int = 25) -> int:
    if mcap <= 0:
        return 0
    if 0 < mcap < 50_000_000:
        return max_score
    if mcap < 100_000_000:
        return max_score - 3
    if mcap < 200_000_000:
        return max_score - 5
    if mcap < 300_000_000:
        return max_score - 8
    if mcap < 500_000_000:
        return max_score - 13
    if mcap < 1_000_000_000:
        return max(0, max_score - 18)
    return 0


def score_sideways(days: int, max_score: int = 25) -> int:
    if days >= 120:
        return max_score
    if days >= 90:
        return max_score - 3
    if days >= 75:
        return max_score - 7
    if days >= 60:
        return max_score - 11
    if days >= 45:
        return max_score - 15
    return 0


def score_oi(oi_pct: float, max_score: int = 25) -> int:
    value = abs(oi_pct)
    if value >= 15:
        return max_score
    if value >= 8:
        return max_score - 3
    if value >= 5:
        return max_score - 7
    if value >= 3:
        return max_score - 11
    if value >= 2:
        return max_score - 15
    return 0


class RadarEngine:
    def __init__(self, settings: Settings, store: JsonStore):
        self.settings = settings
        self.store = store

    def run_once(self, include_launch: bool = True, include_announcements: bool = True) -> dict[str, Any]:
        summary_source = BinanceDataSource(self.settings)
        launch_source = BinanceDataSource(self.settings)
        announcement_source = BinanceDataSource(self.settings)
        try:
            summary = self.build_money_radar_summary(summary_source)
            launch = self.build_launch_alerts(launch_source) if include_launch else {
                "template_id": "TG_LAUNCH_ALERT",
                "messages": [],
                "alerts": [],
            }
            announcements = self.build_announcement_alerts(announcement_source) if include_announcements else {
                "template_id": "TG_ANNOUNCEMENT_ALERT",
                "messages": [],
                "alerts": [],
            }
            return {
                "summary": summary,
                "launch": launch,
                "announcements": announcements,
                "diagnostics": {
                    "summary": summary_source.diagnostics(),
                    "launch": launch_source.diagnostics() if include_launch else {},
                    "announcements": announcement_source.diagnostics() if include_announcements else {},
                },
            }
        finally:
            summary_source.close()
            launch_source.close()
            announcement_source.close()

    def build_money_radar_summary(self, source: BinanceDataSource) -> dict[str, Any]:
        window = closed_window(
            interval_sec=self.settings.radar_summary_min_interval_sec,
            delay_sec=self.settings.radar_summary_close_delay_sec,
        )
        items = self._load_market_items(source, window)
        now = cst_now_text()
        if not items:
            return {
                "template_id": "TG_RADAR_SUMMARY",
                "dedup_key": f"radar-summary:{window.end.strftime('%Y%m%d%H%M')}",
                "text": "\n".join([
                    "🏦 <b>资金雷达摘要</b>",
                    f"⏰ {now}",
                    f"统计窗口: {window.label()}",
                    "",
                    "暂无有效数据，可能是接口失败或候选不足。",
                ]),
                "quality": source.diagnostics(),
                "context_records": [],
            }

        top_n = self.settings.radar_top_n

        for item in items:
            item["combined_score"] = (
                score_funding(item["funding_pct"])
                + score_mcap(item["mcap"])
                + score_sideways(item["sideways_days"])
                + score_oi(item["oi_6h"])
            )
            ambush_oi_score = score_oi(item["oi_6h"], 30)
            if item["oi_6h"] > 2 and abs(item["price_window"]) < 5:
                ambush_oi_score = min(30, ambush_oi_score + 5)
            item["ambush_score"] = (
                score_mcap(item["mcap"], 35)
                + ambush_oi_score
                + score_sideways(item["sideways_days"], 20)
                + min(15, score_funding(item["funding_pct"]))
            )
            item["momentum_score"] = (
                min(35, score_oi(item["oi_6h"], 35))
                + min(25, int(abs(item["price_window"]) * 1.8))
                + min(25, int(item["quote_volume"] / 20_000_000))
                + (15 if item["funding_pct"] < 0 else 0)
            )
            item["new_score"] = (
                min(30, score_oi(item["oi_6h"], 30))
                + min(25, int(abs(item["price_window"]) * 1.5))
                + min(25, int(item["quote_volume"] / 15_000_000))
                + (20 if item["funding_pct"] < 0 else 0)
            )
            item["divergence"] = item["oi_6h"] - item["price_window"]

        for item in items:
            apply_binance_confirmation(
                item,
                {
                    "价格窗口": True,
                    "OI窗口": True,
                    "24h成交额": to_float(item.get("quote_volume")) > 0,
                    "资金费率": bool(item.get("funding_ready")),
                    "日K历史": int(item.get("history_days") or 0) > 0,
                },
                scope="Binance USDⓈ-M Futures",
                window=f"{max(1, int(window.interval_sec / 3600))}h闭合窗口",
                observed_at=int(window.end.timestamp()),
            )

        oi_items = [item for item in items if self._summary_oi_allowed(item)]
        negative = sorted(
            [item for item in oi_items if item["funding_pct"] < 0],
            key=lambda item: item["funding_pct"],
        )[:top_n]
        combined = sorted([item for item in oi_items if item["combined_score"] >= 25], key=lambda item: item["combined_score"], reverse=True)[:top_n]
        ambush = sorted(
            [
                item for item in oi_items
                if item["ambush_score"] >= 35 and (item["sideways_days"] >= 45 or self._is_dark_flow(item))
            ],
            key=lambda item: item["ambush_score"],
            reverse=True,
        )[:top_n]
        momentum = sorted([item for item in oi_items if item["momentum_score"] >= 35], key=lambda item: item["momentum_score"], reverse=True)[:top_n]
        new_pool = sorted([item for item in oi_items if item["history_days"] < 30], key=lambda item: item["new_score"], reverse=True)[:top_n]
        divergence_raw = [
            classified for classified in (self._classify_divergence_item(item) for item in oi_items)
            if classified is not None
        ]
        divergence, divergence_stats = self._update_divergence_states(divergence_raw)
        divergence = sorted(
            divergence,
            key=lambda item: (item["priority"], abs(item["divergence"]), abs(item["oi_6h"])),
            reverse=True,
        )[:5]

        context_records: list[dict[str, Any]] = []
        context_symbols: set[str] = set()
        for group in (combined, ambush, momentum, new_pool, negative, divergence):
            for item in group:
                symbol = str(item.get("symbol") or "").upper()
                if not symbol or symbol in context_symbols:
                    continue
                context_records.append(item)
                context_symbols.add(symbol)
                if len(context_records) >= 3:
                    break
            if len(context_records) >= 3:
                break

        derivatives_quality = confirmation_summary(items)
        quality = source.diagnostics()
        quality["derivatives_quality"] = derivatives_quality
        text = self._format_summary(
            now,
            negative,
            combined,
            ambush,
            momentum,
            new_pool,
            divergence,
            items,
            source,
            divergence_stats,
            window,
            derivatives_quality=derivatives_quality,
        )
        return {
            "template_id": "TG_RADAR_SUMMARY",
            "dedup_key": f"radar-summary:{window.end.strftime('%Y%m%d%H%M')}",
            "text": text,
            "quality": quality,
            "context_records": context_records,
        }

    def _load_market_items(self, source: BinanceDataSource, window: ClosedWindow) -> list[dict[str, Any]]:
        budget_cap = min(self.settings.oi_hist_budget, max(1, self.settings.kline_budget // 2))
        if self.settings.radar_scan_limit <= 0 or budget_cap <= 0:
            return []
        symbols_info = source.usdt_perp_symbols()
        valid_symbols = {item.get("symbol", "") for item in symbols_info}
        onboard_map = {item.get("symbol", ""): int(item.get("onboardDate", 0) or 0) for item in symbols_info}
        ticker_map = {
            item.get("symbol"): item
            for item in source.ticker_24h()
            if item.get("symbol") in valid_symbols
            and not self._is_excluded_symbol(str(item.get("symbol") or ""))
        }
        premium_map = {
            item.get("symbol"): to_float(item.get("lastFundingRate"))
            for item in source.premium_index()
            if item.get("symbol") in valid_symbols
            and not self._is_excluded_symbol(str(item.get("symbol") or ""))
        }
        mcap_map = source.market_caps()
        previous_funding = self.store.load(self.settings.funding_snapshot_path, {})
        current_funding: dict[str, float] = {}

        candidates: list[dict[str, Any]] = []
        for symbol, ticker in ticker_map.items():
            quote_volume = to_float(ticker.get("quoteVolume"))
            if quote_volume < self.settings.radar_min_quote_volume:
                continue
            candidates.append({
                "symbol": symbol,
                "coin": symbol.replace("USDT", ""),
                "quote_volume": quote_volume,
                "price": to_float(ticker.get("lastPrice")),
                "price_24h": to_float(ticker.get("priceChangePercent")),
                "funding": premium_map.get(symbol, 0.0),
                "funding_ready": symbol in premium_map,
            })
        candidates.sort(key=lambda item: item["quote_volume"], reverse=True)
        candidates = candidates[: self.settings.radar_scan_limit]
        candidates = candidates[:budget_cap]

        result: list[dict[str, Any]] = []
        for item in candidates:
            symbol = item["symbol"]
            coin = item["coin"]
            funding_pct = item["funding"] * 100
            current_funding[symbol] = funding_pct

            hours = max(1, int(window.interval_sec / 3600))
            oi_hist = source.open_interest_hist(
                symbol,
                period="1h",
                limit=hours + 2,
                start_time=max(0, window.start_ms - 3_600_000),
                end_time=window.end_ms,
            )
            oi_6h = 0.0
            oi_usd = 0.0
            circulating_supply = 0.0
            oi_ready = False
            oi_6h, latest_oi_row, oi_ready = self._oi_window_change(
                oi_hist,
                start_ms=window.start_ms,
                end_ms=window.end_ms,
            )
            if latest_oi_row:
                oi_usd = to_float(latest_oi_row.get("sumOpenInterestValue"))
                circulating_supply = to_float(latest_oi_row.get("CMCCirculatingSupply"))

            hourly = source.klines(
                symbol,
                interval="1h",
                limit=hours + 1,
                start_time=window.start_ms,
                end_time=window.end_ms - 1,
            )
            price_window = 0.0
            price_window_ready = False
            if hourly:
                first_open = to_float(hourly[0][1]) if len(hourly[0]) > 4 else 0.0
                last_close = to_float(hourly[-1][4]) if len(hourly[-1]) > 4 else 0.0
                if first_open > 0 and last_close > 0:
                    price_window = pct(last_close, first_open)
                    price_window_ready = True
            if not oi_ready or not price_window_ready:
                continue

            daily = source.klines(symbol, interval="1d", limit=140)
            history_days = len(daily)
            onboard_ms = onboard_map.get(symbol, 0)
            if onboard_ms > 0:
                onboard_days = max(0, int((time.time() * 1000 - onboard_ms) / 86_400_000))
                history_days = min(history_days or onboard_days, onboard_days)
            sideways_days = estimate_sideways_days(daily)

            mcap = mcap_map.get(coin, 0.0)
            mcap_source = "Binance市场资料" if mcap > 0 else ""
            if not mcap and circulating_supply > 0 and item["price"] > 0:
                mcap = circulating_supply * item["price"]
                mcap_source = "Binance流通量×现价"

            result.append({
                **item,
                "funding_pct": funding_pct,
                "funding_trend": funding_trend(previous_funding.get(symbol), funding_pct),
                "oi_6h": oi_6h,
                "price_window": price_window,
                "oi_usd": oi_usd,
                "mcap": mcap,
                "mcap_source": mcap_source,
                "sideways_days": sideways_days,
                "history_days": history_days,
                "dark_flow": oi_6h > 2 and abs(price_window) < 5,
            })

        self.store.save(self.settings.funding_snapshot_path, current_funding)
        return result

    @staticmethod
    def _oi_window_change(
        rows: list[dict[str, Any]],
        *,
        start_ms: int,
        end_ms: int,
    ) -> tuple[float, dict[str, Any], bool]:
        points = [
            row for row in rows
            if isinstance(row, dict)
            and 0 < int(to_float(row.get("timestamp"))) <= end_ms
            and to_float(row.get("sumOpenInterestValue")) > 0
        ]
        if len(points) < 2:
            return 0.0, {}, False
        baseline = min(
            points,
            key=lambda row: abs(int(to_float(row.get("timestamp"))) - start_ms),
        )
        latest = max(points, key=lambda row: int(to_float(row.get("timestamp"))))
        baseline_time = int(to_float(baseline.get("timestamp")))
        latest_time = int(to_float(latest.get("timestamp")))
        first = to_float(baseline.get("sumOpenInterestValue"))
        last = to_float(latest.get("sumOpenInterestValue"))
        if baseline_time >= latest_time or first <= 0 or last <= 0:
            return 0.0, latest, False
        return pct(last, first), latest, True

    def build_announcement_alerts(self, source: BinanceDataSource, include_seen: bool = False) -> dict[str, Any]:
        articles = source.announcements(page_size=self.settings.announcement_page_size)
        news_ingestion: dict[str, Any]
        try:
            from .news_intelligence import ingest_binance_announcements

            news_ingestion = ingest_binance_announcements(
                self.settings,
                articles=articles,
                source=source,
            )
        except Exception as exc:
            # Announcement pushes must remain available when the independent
            # information index is temporarily unwritable.
            news_ingestion = {"status": "degraded", "error": type(exc).__name__}
        state = self.store.load(self.settings.announcement_state_path, {})
        if not isinstance(state, dict):
            state = {}
        seen = state.get("seen", {})
        if not isinstance(seen, dict):
            seen = {}

        contract_symbols = self._announcement_contract_symbols(source)
        alerts: list[dict[str, Any]] = []
        for article in articles:
            if not self._announcement_is_current(article):
                continue
            alert = self._classify_announcement(article, contract_symbols)
            if not alert:
                continue
            code = alert["code"]
            if not include_seen and code in seen:
                continue
            alerts.append(alert)

        messages = [self._format_announcement(alert) for alert in alerts[:8]]
        return {
            "template_id": "TG_ANNOUNCEMENT_ALERT",
            "messages": messages,
            "alerts": alerts[:8],
            "articles_scanned": len(articles),
            "alerts_classified": len(alerts),
            "news_ingestion": news_ingestion,
        }

    def cleanup_expired_announcements(
        self,
        delete_messages: Callable[[list[int]], int] | None = None,
    ) -> dict[str, int]:
        state = self.store.load(self.settings.announcement_state_path, {})
        if not isinstance(state, dict):
            return {"expired": 0, "deleted_messages": 0}
        seen = state.get("seen", {})
        if not isinstance(seen, dict):
            return {"expired": 0, "deleted_messages": 0}

        now_ts = int(time.time())
        cutoff = now_ts - 14 * 24 * 3600
        expired = 0
        deleted_messages = 0
        retained: dict[str, Any] = {}
        changed = False
        for key, value in seen.items():
            if not isinstance(value, dict):
                changed = True
                continue
            expires_at = int(value.get("expires_at", 0) or 0)
            seen_at = int(value.get("seen_at", now_ts) or now_ts)
            if expires_at > 0 and expires_at <= now_ts:
                message_ids = [
                    int(message_id)
                    for message_id in value.get("message_ids", [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                ]
                if message_ids and not delete_messages:
                    retained[key] = {**value, "delete_pending": True}
                    changed = True
                    continue
                if delete_messages and message_ids:
                    deleted_now = delete_messages(message_ids)
                    deleted_messages += deleted_now
                    if deleted_now < len(message_ids):
                        retained[key] = {
                            **value,
                            "delete_pending": True,
                            "last_delete_attempt": now_ts,
                        }
                        changed = True
                        continue
                expired += 1
                changed = True
                continue
            if seen_at < cutoff:
                changed = True
                continue
            retained[key] = value

        if changed:
            self.store.save(self.settings.announcement_state_path, {"seen": retained})
        return {"expired": expired, "deleted_messages": deleted_messages}

    def mark_announcements_seen(self, alerts: list[dict[str, Any]]) -> None:
        state = self.store.load(self.settings.announcement_state_path, {})
        if not isinstance(state, dict):
            state = {}
        seen = state.get("seen", {})
        if not isinstance(seen, dict):
            seen = {}
        now_ts = int(time.time())
        for alert in alerts:
            seen[alert["code"]] = {
                "title": alert["title"],
                "kind": alert["kind"],
                "symbol": alert.get("symbol", ""),
                "symbols": alert.get("symbols", []),
                "contract_symbols": alert.get("contract_symbols", []),
                "non_contract_symbols": alert.get("non_contract_symbols", []),
                "url": alert.get("url", ""),
                "release_ts": int(alert.get("release_ts", 0) or 0),
                "expires_at": int(alert.get("expires_at", 0) or 0),
                "message_ids": alert.get("message_ids", []),
                "seen_at": now_ts,
            }
        cutoff = now_ts - 14 * 24 * 3600
        seen = {
            key: value for key, value in seen.items()
            if int(value.get("seen_at", now_ts)) >= cutoff
        }
        self.store.save(self.settings.announcement_state_path, {"seen": seen})

    def _classify_announcement(
        self,
        article: dict[str, Any],
        contract_symbols: set[str] | None = None,
    ) -> Optional[dict[str, Any]]:
        title = str(article.get("title") or "")
        if not title:
            return None
        lowered = title.lower()
        code = str(article.get("code") or article.get("id") or title)
        symbols = self._extract_symbols(title)
        if not symbols:
            return None
        contract_symbols = contract_symbols or set()
        symbols_with_contract = [
            symbol for symbol in symbols
            if self._announcement_symbol_has_contract(symbol, contract_symbols)
        ]
        symbols_without_contract = [
            symbol for symbol in symbols
            if symbol not in symbols_with_contract
        ]
        symbol = self._format_symbol_list(symbols)
        url = self._announcement_url(article)
        release_ts = self._announcement_release_ts(article)
        expires_at = self._announcement_expires_at(article)
        if any(keyword in lowered for keyword in RISK_KEYWORDS):
            return {
                "kind": "risk",
                "code": code,
                "title": title,
                "symbol": symbol,
                "symbols": symbols,
                "contract_symbols": symbols_with_contract,
                "non_contract_symbols": symbols_without_contract,
                "url": url,
                "release_ts": release_ts,
                "expires_at": expires_at,
                "priority": "high",
                "reason": "命中下架/移除/停止交易关键词",
            }
        if any(keyword in lowered for keyword in EXCLUDE_OPPORTUNITY_KEYWORDS):
            return None
        if any(keyword in lowered for keyword in OPPORTUNITY_KEYWORDS):
            return {
                "kind": "opportunity",
                "code": code,
                "title": title,
                "symbol": symbol,
                "symbols": symbols,
                "contract_symbols": symbols_with_contract,
                "non_contract_symbols": symbols_without_contract,
                "url": url,
                "release_ts": release_ts,
                "expires_at": expires_at,
                "priority": "normal",
                "reason": "命中上新/Alpha/活动关键词",
            }
        return None

    @staticmethod
    def _extract_symbols(title: str) -> list[str]:
        title = RadarEngine._remove_chain_context_parentheses(title)
        symbols: list[str] = []
        for pattern in (r"\(([A-Z0-9]{2,12})\)", r"（([A-Z0-9]{2,12})）"):
            for match in re.finditer(pattern, title):
                RadarEngine._append_announcement_symbol(symbols, match.group(1))
        words = re.findall(r"\b[A-Z][A-Z0-9]{1,12}\b", title)
        for word in words:
            RadarEngine._append_announcement_symbol(symbols, word)
        return symbols[:20]

    @staticmethod
    def _append_announcement_symbol(symbols: list[str], symbol: str) -> None:
        normalized = symbol.strip().upper()
        if not normalized:
            return
        if normalized in ANNOUNCEMENT_WORD_BLACKLIST:
            return
        if re.fullmatch(r"20\d{2}", normalized):
            return
        if normalized not in symbols:
            symbols.append(normalized)

    @staticmethod
    def _remove_chain_context_parentheses(title: str) -> str:
        def replace(match: re.Match[str]) -> str:
            token_name = match.group(1).strip().lower()
            chain_symbol = match.group(2).upper()
            if chain_symbol not in CHAIN_CONTEXT_SYMBOLS:
                return match.group(0)
            valid_names = CHAIN_SYMBOL_TOKEN_NAMES.get(chain_symbol, set())
            if token_name in valid_names:
                return match.group(0)
            return match.group(1)

        return re.sub(r"\b([A-Za-z][A-Za-z0-9-]{1,32})\s*[\(（]([A-Z0-9]{2,12})[\)）]", replace, title)

    @staticmethod
    def _format_symbol_list(symbols: list[str], max_count: int = 8) -> str:
        if not symbols:
            return ""
        shown = ", ".join(symbols[:max_count])
        if len(symbols) > max_count:
            shown += f" +{len(symbols) - max_count}"
        return shown

    def _announcement_contract_symbols(self, source: BinanceDataSource) -> set[str]:
        try:
            symbols_info = source.usdt_perp_symbols()
        except Exception:
            return set()
        result: set[str] = set()
        for item in symbols_info:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            if symbol.endswith("USDT"):
                result.add(symbol[:-4])
        return result

    @staticmethod
    def _announcement_symbol_has_contract(symbol: str, contract_symbols: set[str]) -> bool:
        normalized = symbol.upper()
        if normalized.endswith("USDT"):
            normalized = normalized[:-4]
        return normalized in contract_symbols

    def _announcement_is_current(self, article: dict[str, Any]) -> bool:
        if not self.settings.announcement_only_today:
            return True
        today = datetime.now(CST).date()
        title_date = self._announcement_title_date(str(article.get("title") or ""))
        if title_date and title_date.date() < today:
            return False
        release_ts = self._announcement_release_ts(article)
        if release_ts <= 0:
            return True
        release_date = datetime.fromtimestamp(release_ts, CST).date()
        return release_date >= today

    def _announcement_expires_at(self, article: dict[str, Any]) -> int:
        title_date = self._announcement_title_date(str(article.get("title") or ""))
        if title_date:
            return int(title_date.timestamp())
        release_ts = self._announcement_release_ts(article)
        if release_ts <= 0:
            return 0
        days = max(1, int(self.settings.announcement_default_ttl_days))
        release_date = datetime.fromtimestamp(release_ts, CST).date()
        expires = datetime(
            release_date.year,
            release_date.month,
            release_date.day,
            23,
            59,
            59,
            tzinfo=CST,
        ) + timedelta(days=days)
        return int(expires.timestamp())

    @staticmethod
    def _announcement_title_date(title: str) -> Optional[datetime]:
        match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", title)
        if not match:
            return None
        try:
            year, month, day = (int(match.group(index)) for index in (1, 2, 3))
            return datetime(year, month, day, 23, 59, 59, tzinfo=CST)
        except ValueError:
            return None

    @staticmethod
    def _announcement_release_ts(article: dict[str, Any]) -> int:
        for key in (
            "releaseDate",
            "releaseTime",
            "publishDate",
            "publishedAt",
            "publishTime",
            "createdAt",
            "date",
        ):
            value = article.get(key)
            if value in (None, ""):
                continue
            ts = RadarEngine._coerce_timestamp(value)
            if ts > 0:
                return ts
        return 0

    @staticmethod
    def _coerce_timestamp(value: Any) -> int:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000
            return int(ts)
        text = str(value).strip()
        if not text:
            return 0
        if text.isdigit():
            return RadarEngine._coerce_timestamp(int(text))
        try:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return 0

    @staticmethod
    def _announcement_url(article: dict[str, Any]) -> str:
        url = str(article.get("url") or article.get("webLink") or "")
        if url.startswith("http"):
            return url
        code = article.get("code")
        if code:
            return f"https://www.binance.com/zh-CN/support/announcement/{code}"
        return "https://www.binance.com/zh-CN/support/announcement"

    def _format_announcement(self, alert: dict[str, Any]) -> str:
        symbol_block = self._format_announcement_symbol_links(alert)
        title = tg_escape(alert["title"])
        url = escape(alert["url"], quote=True)
        contract_count = len(alert.get("contract_symbols", []))
        no_contract_count = len(alert.get("non_contract_symbols", []))
        market_note = f"有合约{contract_count}个 | 无合约{no_contract_count}个"
        if alert["kind"] == "risk":
            return "\n".join([
                f"⚠️ {tg_bold('风险提醒')}",
                "",
                f"{tg_bold('币种')}: {symbol_block}",
                f"{tg_bold('合约状态')}: {market_note}",
                f"{tg_bold('风险')}: 下架 / 移除交易对 / 停止交易",
                f"{tg_bold('公告')}: {title}",
                "",
                tg_bold("影响"),
                "- 合约或现货流动性可能快速下降",
                "- 观察状态中该币应标记为 风险",
                "",
                tg_bold("处理"),
                "暂停新增观察，只保留风险记录",
                f"{tg_bold('链接')}: <a href=\"{url}\">Binance 公告</a>",
            ])
        return "\n".join([
            f"📢 {tg_bold('公告机会')}",
            "",
            f"{tg_bold('币种')}: {symbol_block}",
            f"{tg_bold('合约状态')}: {market_note}",
            f"{tg_bold('事件')}: Binance Alpha / 上新 / 活动",
            f"{tg_bold('公告')}: {title}",
            f"{tg_bold('等级')}: 待资金面确认",
            "",
            tg_bold("原因"),
            f"- {tg_escape(alert['reason'])}",
            "- Binance 官方公告触发",
            "",
            tg_bold("处理"),
            "已记录为机会事件，等待资金面确认",
            f"{tg_bold('链接')}: <a href=\"{url}\">Binance 公告</a>",
        ])

    def _format_announcement_symbol_links(self, alert: dict[str, Any], max_count: int = 20) -> str:
        symbols = [str(symbol).upper() for symbol in alert.get("symbols", []) if str(symbol).strip()]
        if not symbols:
            return "未识别具体币种（不生成K线链接）"
        contract_symbols = {
            str(symbol).upper().replace("USDT", "")
            for symbol in alert.get("contract_symbols", [])
        }
        parts: list[str] = []
        for symbol in symbols[:max_count]:
            base_symbol = symbol[:-4] if symbol.endswith("USDT") else symbol
            if base_symbol in contract_symbols:
                parts.append(telegram_coin_links(symbol))
            else:
                parts.append(f"{tg_bold(symbol)}（无合约）")
        if len(symbols) > max_count:
            parts.append(f"+{len(symbols) - max_count}个")
        if len(parts) <= 4:
            return "、".join(parts)
        return "\n" + "\n".join(f"- {part}" for part in parts)

    def _format_summary(
        self,
        now: str,
        negative: list[dict[str, Any]],
        combined: list[dict[str, Any]],
        ambush: list[dict[str, Any]],
        momentum: list[dict[str, Any]],
        new_pool: list[dict[str, Any]],
        divergence: list[dict[str, Any]],
        all_items: list[dict[str, Any]],
        source: BinanceDataSource,
        divergence_stats: dict[str, int],
        window: ClosedWindow,
        derivatives_quality: dict[str, Any] | None = None,
    ) -> str:
        derivatives_quality = derivatives_quality or {
            "checked": 0,
            "status_counts": {},
            "blocked_symbols": [],
        }
        lines = [
            "🏦 <b>资金雷达摘要</b>",
            f"⏰ {now}",
            f"统计窗口: {window.label()}",
            f"数据规则: 收线后延迟 {seconds_text(window.delay_sec)}抓取上一完整窗口",
            "",
            tg_quote("📊 本轮统计"),
            f"扫描合约: {len(all_items)}",
            f"OI请求: {source.budget.used.get('open_interest_hist', 0)} / {source.budget.limits.get('open_interest_hist', 0)}",
            f"K线请求: {source.budget.used.get('klines', 0)} / {source.budget.limits.get('klines', 0)}",
            f"接口异常: {sum(source.quality.failures.values())}",
            (
                f"Binance数据确认: 完整 {derivatives_quality.get('confirmed', 0)} / "
                f"{derivatives_quality.get('checked', 0)} | "
                f"缺项 {derivatives_quality.get('incomplete', 0)}"
            ),
            (
                f"背离状态  : 首次{divergence_stats.get('first', 0)} | "
                f"持续{divergence_stats.get('continued', 0)} | "
                f"增强{divergence_stats.get('enhanced', 0)} | "
                f"重新{divergence_stats.get('reappeared', 0)}"
            ),
            "",
        ]
        self._append_negative(lines, negative)
        self._append_combined(lines, combined)
        self._append_ambush(lines, ambush)
        self._append_momentum(lines, momentum)
        self._append_new_pool(lines, new_pool)
        self._append_divergence(lines, divergence)
        self._append_highlights(lines, negative, combined, ambush, momentum, divergence)
        lines.extend([
            "",
            tg_quote("📖 图例"),
            "负费率 = 空头拥挤，可能形成反向燃料",
            "🔥加速 = 费率继续变负",
            "⬇️变负 = 刚从正费率转为负费率",
            "⬆️回升 = 负费率缓和",
            "暗流 = OI增加但价格没动",
            "窗口 = 本次统计窗口内的完整收线数据",
            "背离 = OI窗口变化% - 价格窗口变化%",
            "OI·币安 = OI来自 Binance USDⓈ-M 已闭合窗口，不再使用外部聚合源改写",
            "市值 = Binance市场资料；缺失时为0分，不再使用成交额/OI倍数猜测市值",
            "链接 = 点击币种打开 CoinGlass，点击代码复制交易对，点击 TV 打开 TradingView",
        ])
        return "\n".join(lines)

    def _append_negative(self, lines: list[str], items: list[dict[str, Any]]) -> None:
        lines.append(tg_quote("🔥 负费率榜（按费率由负到正，找空头拥挤燃料）"))
        if not items:
            lines.append("暂无明显负费率标的")
            lines.append("")
            return
        for item in items:
            metrics = (
                f"费率 {pct_cell(item['funding_pct'], 8, 3)} {item['funding_trend']:<4} | "
                f"24h {pct_cell(item['price_24h'])} | "
                f"市值 {fmt_money(item['mcap']).rjust(7)} | "
                f"现价 {fmt_price(item['price']).rjust(10)}"
            )
            append_metric_row(lines, item, metrics)
        lines.append("")

    def _append_combined(self, lines: list[str], items: list[dict[str, Any]]) -> None:
        lines.append(tg_quote("📊 综合榜（评分=费率25 + 市值25 + 横盘25 + OI25）"))
        for item in items:
            metrics = (
                f"{score_cell(item['combined_score'])} | "
                f"费率 {pct_cell(item['funding_pct'], 7, 2)} | "
                f"市值 {fmt_money(item['mcap']).rjust(7)} | "
                f"横盘 {str(item['sideways_days']).rjust(3)}天 | "
                f"OI {pct_cell(item['oi_6h'])}·{self._summary_oi_quality_badge(item)} | "
                f"{fmt_price(item['price']).rjust(10)}"
            )
            append_metric_row(lines, item, metrics)
        if not items:
            lines.append("暂无")
        lines.append("")

    def _append_ambush(self, lines: list[str], items: list[dict[str, Any]]) -> None:
        lines.append(tg_quote("🎯 埋伏池（评分=市值35 + OI30 + 横盘20 + 费率15）"))
        for item in items:
            tag = "暗流" if self._is_dark_flow(item) else "横盘"
            metrics = (
                f"{score_cell(item['ambush_score'])} | "
                f"市值 {fmt_money(item['mcap']).rjust(7)} | "
                f"OI {pct_cell(item['oi_6h'])}·{self._summary_oi_quality_badge(item)} | "
                f"横盘 {str(item['sideways_days']).rjust(3)}天 | "
                f"费率 {pct_cell(item['funding_pct'], 7, 2)} | "
                f"{tag}"
            )
            append_metric_row(lines, item, metrics)
        if not items:
            lines.append("暂无")
        lines.append("")

    def _append_momentum(self, lines: list[str], items: list[dict[str, Any]]) -> None:
        lines.append(tg_quote("⚡ 动量池（评分=OI35 + 窗口涨跌25 + 成交额25 + 负费率15）"))
        for item in items:
            metrics = (
                f"{score_cell(item['momentum_score'])} | "
                f"OI {pct_cell(item['oi_6h'])}·{self._summary_oi_quality_badge(item)} | "
                f"窗口 {pct_cell(item['price_window'])} | "
                f"Vol {fmt_money(item['quote_volume']).rjust(7)} | "
                f"历史 {str(item['history_days']).rjust(3)}天"
            )
            append_metric_row(lines, item, metrics)
        if not items:
            lines.append("暂无")
        lines.append("")

    def _append_new_pool(self, lines: list[str], items: list[dict[str, Any]]) -> None:
        lines.append(tg_quote("🆕 新币池（评分=OI30 + 窗口涨跌25 + 成交额25 + 负费率20）"))
        for item in items:
            metrics = (
                f"{score_cell(item['new_score'])} | "
                f"历史 {str(item['history_days']).rjust(3)}天 | "
                f"OI {pct_cell(item['oi_6h'])}·{self._summary_oi_quality_badge(item)} | "
                f"窗口 {pct_cell(item['price_window'])} | "
                f"Vol {fmt_money(item['quote_volume']).rjust(7)}"
            )
            append_metric_row(lines, item, metrics)
        if not items:
            lines.append("暂无")
        lines.append("")

    def _append_divergence(self, lines: list[str], items: list[dict[str, Any]]) -> None:
        lines.append(tg_quote("⚖️ 背离雷达（背离=OI窗口变化% - 价格窗口变化%）"))
        for item in items:
            metrics = (
                f"OI {pct_cell(item['oi_6h'])}·{self._summary_oi_quality_badge(item)} | "
                f"价格 {pct_cell(item['price_window'])} | "
                f"背离 {item['divergence']:+6.1f} | "
                f"{item['level']} | {item['status_text']}"
            )
            append_metric_row(lines, item, metrics)
        if not items:
            lines.append("暂无")
        lines.append("")

    def _append_highlights(
        self,
        lines: list[str],
        negative: list[dict[str, Any]],
        combined: list[dict[str, Any]],
        ambush: list[dict[str, Any]],
        momentum: list[dict[str, Any]],
        divergence: list[dict[str, Any]],
    ) -> None:
        highlights: list[tuple[str, str]] = []
        combined_coins = {item["coin"] for item in combined[:5]}
        momentum_coins = {item["coin"] for item in momentum[:5]}
        for item in negative[:4]:
            if "加速" in item["funding_trend"] or item["coin"] in combined_coins:
                highlights.append((
                    item["coin"],
                    f"🔥 {coin_link(item)}\n费率{item['funding_pct']:+.3f}% {item['funding_trend']}，空头燃料明显",
                ))
        for item in combined[:4]:
            if item["coin"] in momentum_coins:
                highlights.append((
                    item["coin"],
                    f"⭐ {coin_link(item)}\n综合榜+动量池同时出现",
                ))
        for item in ambush[:4]:
            if self._is_dark_flow(item):
                highlights.append((
                    item["coin"],
                    f"🎯 {coin_link(item)}\nOI{item['oi_6h']:+.1f}%但窗口价格没动，低位暗流",
                ))
        for item in divergence[:2]:
            if abs(item["divergence"]) >= 20:
                highlights.append((
                    item["coin"],
                    f"⚠️ {coin_link(item)}\n极端背离，先按风险处理",
                ))
        deduped: list[str] = []
        seen: set[str] = set()
        for coin, line in highlights:
            if coin in seen:
                continue
            seen.add(coin)
            deduped.append(line)
        lines.append(tg_quote("💡 值得关注"))
        if deduped:
            lines.extend(deduped[:5])
        else:
            lines.append("暂无高优先级结论")

    @staticmethod
    def _is_dark_flow(item: dict[str, Any]) -> bool:
        return item.get("oi_6h", 0) > 2 and abs(item.get("price_window", item.get("price_24h", 0))) < 5

    @staticmethod
    def _summary_oi_allowed(item: dict[str, Any]) -> bool:
        return item.get("quality_gate") != "block"

    @staticmethod
    def _summary_oi_quality_badge(item: dict[str, Any]) -> str:
        return {
            "confirmed": "币安",
            "incomplete": "缺项",
        }.get(str(item.get("data_quality_status") or ""), "未确认")

    def _classify_divergence_item(self, item: dict[str, Any]) -> Optional[dict[str, Any]]:
        oi = item["oi_6h"]
        price = item.get("price_window", item.get("price_24h", 0))
        divergence = item["divergence"]
        if abs(divergence) < 6 and abs(oi) < 5:
            return None

        if abs(divergence) >= 20 or abs(price) >= 15:
            signal_type = "极端背离"
            priority = 5
            level = "🚨极端"
            reference = "剧烈波动，先按风险处理，必须等待更多确认。"
        elif oi >= 6 and -3 <= price <= 3:
            signal_type = "建仓背离"
            priority = 4
            level = "🔴强" if abs(divergence) >= 10 else "🟡中"
            reference = "OI明显增加但价格没动，疑似资金提前布局。"
        elif oi >= 5 and price >= 4:
            signal_type = "多头共振"
            priority = 3
            level = "🟢共振"
            reference = "持仓和价格同步上升，趋势较强但注意追高。"
        elif oi >= 5 and price <= -4:
            signal_type = "增仓下跌"
            priority = 3
            level = "🟡压制"
            reference = "持仓增加但价格下跌，可能是空头压制或多头被套。"
        elif oi <= -5 and price >= 4:
            signal_type = "减仓上涨"
            priority = 2
            level = "🟡止损"
            reference = "价格上涨但持仓减少，可能是空头止损推动。"
        elif oi <= -5 and price <= -4:
            signal_type = "恐慌抛售"
            priority = 2
            level = "🟠出清"
            reference = "持仓和价格同步下降，不急于判断反转。"
        else:
            signal_type = "普通背离"
            priority = 1
            level = "🟡中" if abs(divergence) >= 6 else "🟢弱"
            reference = "资金和价格开始不同步，先观察持续性。"

        return {
            **item,
            "signal_type": signal_type,
            "priority": priority,
            "level": level,
            "reference": reference,
        }

    def _update_divergence_states(self, results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, int]]:
        state = self.store.load(self.settings.divergence_state_path, {})
        if not isinstance(state, dict):
            state = {}
        stats = {"first": 0, "continued": 0, "enhanced": 0, "weakened": 0, "reappeared": 0}
        now_text = cst_now_text()
        current_keys = {self._divergence_key(item) for item in results}

        enriched: list[dict[str, Any]] = []
        for item in results:
            key = self._divergence_key(item)
            previous = state.get(key, {})
            if not isinstance(previous, dict):
                previous = {}
            status_text, status_kind = self._divergence_status(previous, int(item["priority"]))
            first_seen = previous.get("first_seen") or now_text
            appear_count = int(previous.get("appear_count", 0) or 0) + 1
            continuous_count = (
                int(previous.get("continuous_count", 0) or 0) + 1
                if previous and int(previous.get("missing_count", 0) or 0) == 0
                else 1
            )
            state[key] = {
                "symbol": item["symbol"],
                "coin": item["coin"],
                "signal_type": item["signal_type"],
                "first_seen": first_seen,
                "last_seen": now_text,
                "appear_count": appear_count,
                "continuous_count": continuous_count,
                "missing_count": 0,
                "last_priority": item["priority"],
                "last_oi_6h": round(item["oi_6h"], 4),
                "last_price_24h": round(item["price_24h"], 4),
                "last_divergence": round(item["divergence"], 4),
                "status": status_text,
            }
            stats[status_kind] = stats.get(status_kind, 0) + 1
            enriched.append({
                **item,
                "status_text": status_text,
                "first_seen": first_seen,
                "appear_count": appear_count,
                "continuous_count": continuous_count,
            })

        for key, record in list(state.items()):
            if key in current_keys:
                continue
            if not isinstance(record, dict):
                del state[key]
                continue
            missing_count = int(record.get("missing_count", 0) or 0) + 1
            record["missing_count"] = missing_count
            record["continuous_count"] = 0
            record["status"] = "❌ 消失"
            if missing_count > 12:
                del state[key]

        self.store.save(self.settings.divergence_state_path, state)
        return enriched, stats

    @staticmethod
    def _divergence_key(item: dict[str, Any]) -> str:
        return f"{item['symbol']}:{item['signal_type']}"

    @staticmethod
    def _divergence_status(previous: dict[str, Any], current_priority: int) -> tuple[str, str]:
        if not previous:
            return "🆕 首次出现", "first"
        if int(previous.get("missing_count", 0) or 0) > 0:
            return "⚠️ 重新出现", "reappeared"
        previous_priority = int(previous.get("last_priority", current_priority) or current_priority)
        if current_priority > previous_priority:
            return "🔥 信号增强", "enhanced"
        if current_priority < previous_priority:
            return "🧊 信号减弱", "weakened"
        continuous = int(previous.get("continuous_count", 0) or 0) + 1
        return f"🔁 持续第{continuous}次", "continued"

    def build_launch_alerts(self, source: BinanceDataSource) -> dict[str, Any]:
        budget_cap = min(self.settings.oi_hist_budget, self.settings.kline_budget)
        if self.settings.launch_scan_limit <= 0 or budget_cap <= 0:
            return {
                "template_id": "TG_LAUNCH_ALERT",
                "messages": [],
                "alerts": [],
            }
        state = self.store.load(self.settings.launch_state_path, {})
        if not isinstance(state, dict):
            state = {}
        now_ts = int(time.time())
        self._prune_launch_state(state, now_ts)

        lifecycle_store: LaunchLifecycleStore | None = None
        lifecycle_diagnostics: dict[str, Any] = {
            "enabled": bool(self.settings.launch_lifecycle_v2_enable),
            "status": "disabled",
            "active_symbols": 0,
            "forced_symbols": 0,
            "recorded": 0,
            "opened": 0,
            "failed": 0,
            "frozen": 0,
            "publish_candidates": 0,
            "silent_observations": 0,
            "errors": 0,
            "outcome_v2": {
                "enabled": bool(self.settings.launch_outcome_v2_enable),
                "status": "disabled",
            },
        }
        lifecycle_active_symbols: list[str] = []
        if self.settings.launch_lifecycle_v2_enable:
            try:
                lifecycle_store = LaunchLifecycleStore(
                    self.settings.signal_events_db_path,
                    watch_score=self.settings.launch_watch_score,
                    start_score=self.settings.launch_min_score_push,
                    invalid_windows_required=self.settings.launch_lifecycle_invalid_windows,
                    package_enabled=self.settings.launch_message_package_v2_enable,
                    package_score_delta=self.settings.launch_package_score_delta,
                    package_price_delta_pct=self.settings.launch_package_price_delta_pct,
                    package_oi_delta_pct=self.settings.launch_package_oi_delta_pct,
                    outcome_enabled=self.settings.launch_outcome_v2_enable,
                    outcome_follow_through_pct=self.settings.launch_outcome_follow_through_pct,
                    outcome_min_samples=self.settings.launch_outcome_min_samples,
                    breakout_score=self.settings.launch_breakout_score,
                    launched_score=self.settings.launch_launched_score,
                )
                lifecycle_active_symbols = lifecycle_store.list_active_symbols()
                lifecycle_diagnostics["outcome_v2"] = lifecycle_store.refresh_outcomes(
                    evaluated_at=now_ts
                )
                lifecycle_diagnostics["status"] = (
                    "package_active"
                    if self.settings.launch_message_package_v2_enable
                    else "shadow"
                )
                lifecycle_diagnostics["active_symbols"] = len(lifecycle_active_symbols)
            except (OSError, sqlite3.Error, ValueError) as exc:
                lifecycle_diagnostics["status"] = "degraded"
                lifecycle_diagnostics["errors"] = 1
                lifecycle_diagnostics["error"] = type(exc).__name__

        forced_symbols: list[str] = []
        if lifecycle_store is not None:
            forced_symbols.extend(lifecycle_active_symbols)
            forced_symbols.extend(
                str(symbol)
                for symbol, record in state.items()
                if isinstance(record, dict)
                and str(record.get("stage") or "") in {"watching", "primed", "breakout", "launched", "cooling"}
            )
        forced_symbol_order = {
            symbol: position
            for position, symbol in enumerate(dict.fromkeys(forced_symbols))
        }
        lifecycle_diagnostics["forced_symbols"] = len(forced_symbol_order)

        ticker_map = {
            item.get("symbol"): item
            for item in source.ticker_24h()
            if str(item.get("symbol", "")).endswith("USDT")
        }
        try:
            premium_items = source.premium_index() if hasattr(source, "premium_index") else []
        except Exception:
            premium_items = []
        premium_map = {
            item.get("symbol"): item
            for item in premium_items
            if str(item.get("symbol", "")).endswith("USDT")
        }
        binance_market_caps = source.market_caps() if hasattr(source, "market_caps") else {}
        binance_market_caps = binance_market_caps or {}
        candidates: list[dict[str, Any]] = []
        missing_mcap_coins: set[str] = set()
        for symbol, ticker in ticker_map.items():
            quote_volume = to_float(ticker.get("quoteVolume"))
            if self._is_excluded_symbol(str(symbol or "")):
                continue
            if quote_volume < self.settings.radar_min_quote_volume and symbol not in forced_symbol_order:
                continue
            coin = str(symbol).replace("USDT", "")
            mcap = to_float(binance_market_caps.get(coin))
            if mcap <= 0:
                missing_mcap_coins.add(coin)
            premium = premium_map.get(symbol, {}) if isinstance(premium_map, dict) else {}
            candidates.append({
                "symbol": symbol,
                "coin": coin,
                "quote_volume": quote_volume,
                "price_24h": to_float(ticker.get("priceChangePercent")),
                "price": to_float(ticker.get("lastPrice")),
                "funding_available": isinstance(premium, dict) and bool(premium),
                "funding_pct": to_float(premium.get("lastFundingRate")) * 100 if isinstance(premium, dict) else 0.0,
                "funding_next_time_ms": int(to_float(premium.get("nextFundingTime"))) if isinstance(premium, dict) else 0,
                "mcap": mcap,
                "mcap_source": "Binance" if mcap > 0 else "",
                "market_cap_tier": market_cap_tier(mcap),
                "liquidity_tier": liquidity_tier(quote_volume),
            })
        if missing_mcap_coins and hasattr(source, "coinpaprika_market_caps"):
            coinpaprika_market_caps = source.coinpaprika_market_caps() or {}
            for item in candidates:
                if item["mcap"] > 0 or item["coin"] not in missing_mcap_coins:
                    continue
                mcap = to_float(coinpaprika_market_caps.get(item["coin"]))
                if mcap > 0:
                    item["mcap"] = mcap
                    item["mcap_source"] = "CoinPaprika"
                    item["market_cap_tier"] = market_cap_tier(mcap)
        candidates.sort(
            key=lambda item: (
                0 if item["symbol"] in forced_symbol_order else 1,
                forced_symbol_order.get(item["symbol"], 0),
                -item["quote_volume"],
            )
        )
        candidates = candidates[: min(self.settings.launch_scan_limit, budget_cap)]

        alerts: list[dict[str, Any]] = []
        watchlist: list[dict[str, Any]] = []

        analyzed_items: list[dict[str, Any]] = []
        for item in candidates:
            analyzed = self._analyze_launch_symbol(source, item)
            if not analyzed:
                continue
            analyzed_items.append(analyzed)

        for analyzed in analyzed_items:
            apply_binance_confirmation(
                analyzed,
                {
                    "15m价格": True,
                    "1h价格": True,
                    "15m/1h OI": True,
                    "成交量": True,
                    "突破结构": True,
                },
                scope="Binance USDⓈ-M Futures",
                window="15m闭合窗口（1h=4根）",
                observed_at=int(analyzed.get("window_end_ts") or now_ts),
            )

        if self.settings.launch_message_package_v2_enable and analyzed_items:
            from .bot_market_context import closed_market_contexts_for_symbols

            package_symbols = [
                str(analyzed.get("symbol") or "")
                for analyzed in analyzed_items
                if int(to_float(analyzed.get("score"))) >= self.settings.launch_watch_score
                or str(analyzed.get("symbol") or "") in lifecycle_active_symbols
            ]
            market_contexts = closed_market_contexts_for_symbols(
                self.settings,
                package_symbols,
                now_ts=now_ts,
            )
            for analyzed in analyzed_items:
                market = market_contexts.get(str(analyzed.get("symbol") or ""))
                if not isinstance(market, dict):
                    continue
                analyzed["spot_active_net_usd"] = market.get("spot_flow_usd")
                analyzed["futures_active_net_usd"] = market.get("futures_flow_usd")
                analyzed["funds_direction"] = launch_funds_direction(
                    market.get("spot_flow_usd"),
                    market.get("futures_flow_usd"),
                )

        if lifecycle_store is not None and analyzed_items:
            try:
                lifecycle_results = lifecycle_store.record_observations([
                    (
                        analyzed,
                        self._launch_stage(int(analyzed["score"])),
                        now_ts,
                    )
                    for analyzed in analyzed_items
                ])
                for analyzed, lifecycle in zip(analyzed_items, lifecycle_results):
                    analyzed["launch_lifecycle"] = lifecycle
                    lifecycle_status = str(lifecycle.get("status") or "")
                    if lifecycle_status in {"opened", "active", "failed"}:
                        lifecycle_diagnostics["recorded"] += 1
                    if lifecycle_status == "opened":
                        lifecycle_diagnostics["opened"] += 1
                    if lifecycle_status == "failed":
                        lifecycle_diagnostics["failed"] += 1
                    if lifecycle_status == "frozen":
                        lifecycle_diagnostics["frozen"] += 1
                    publication = lifecycle.get("publication")
                    if isinstance(publication, dict) and publication.get("enabled"):
                        if publication.get("publish_required"):
                            lifecycle_diagnostics["publish_candidates"] += 1
                        elif lifecycle_status in {"opened", "active", "failed", "duplicate"}:
                            lifecycle_diagnostics["silent_observations"] += 1
            except (OSError, sqlite3.Error, ValueError) as exc:
                lifecycle_diagnostics["status"] = "degraded"
                lifecycle_diagnostics["errors"] += 1
                lifecycle_diagnostics["error"] = type(exc).__name__

        observed_symbols: set[str] = set()
        for analyzed in analyzed_items:
            observed_symbols.add(str(analyzed["symbol"]))
            next_stage = self._launch_stage(analyzed["score"])
            watchlist.append(self._launch_watch_record(analyzed, now_ts))
            previous = state.get(analyzed["symbol"], {})
            lifecycle = analyzed.get("launch_lifecycle")
            publication = (
                lifecycle.get("publication")
                if isinstance(lifecycle, dict)
                and isinstance(lifecycle.get("publication"), dict)
                else {}
            )
            if (
                self.settings.launch_message_package_v2_enable
                and isinstance(lifecycle, dict)
                and publication.get("enabled")
            ):
                current_stage = str(lifecycle.get("current_stage") or next_stage)
                previous_published = publication.get("previous_published")
                previous_stage = (
                    str(previous_published.get("stage") or "idle")
                    if isinstance(previous_published, dict)
                    else "idle"
                )
                record = {
                    **(previous if isinstance(previous, dict) else {}),
                    **analyzed,
                    "stage": current_stage,
                    "first_seen": int(lifecycle.get("first_window_end") or now_ts),
                    "last_seen": now_ts,
                    "last_active_at": now_ts,
                    "appear_count": int(lifecycle.get("observation_no") or 1),
                    "previous_stage": previous_stage,
                    "reply_to_message_id": 0,
                    "launch_message_package_v2": True,
                    "launch_package": publication,
                }
                for lifecycle_key in ("cooling_at", "delete_pending"):
                    record.pop(lifecycle_key, None)
                if str(lifecycle.get("cycle_status") or "") == "failed":
                    record["failed_at"] = int(lifecycle.get("window_end_ts") or now_ts)
                    record["fail_reason"] = str(lifecycle.get("end_reason") or "lifecycle_failed")
                else:
                    record.pop("failed_at", None)
                    record.pop("fail_reason", None)
                if publication.get("publish_required"):
                    alerts.append(record)
                state[analyzed["symbol"]] = record
                continue
            if next_stage == "idle":
                inactive = self._inactive_launch_record(
                    previous,
                    now_ts,
                    fail_reason="launch_score_fell",
                )
                if inactive is not None:
                    state[analyzed["symbol"]] = inactive
                continue

            previous_stage = previous.get("stage", "idle")
            if (
                previous_stage == "failed"
                and self.settings.launch_message_cleanup_enable
                and not previous.get("message_cleanup_complete")
            ):
                previous["last_seen"] = now_ts
                state[analyzed["symbol"]] = previous
                continue
            if previous_stage == "failed":
                previous = {}
                previous_stage = "idle"
            stage_changed = self._stage_rank(next_stage) > self._stage_rank(previous_stage)
            last_pushed = int(previous.get("last_pushed", 0) or 0)
            cooldown_ok = now_ts - last_pushed >= self.settings.launch_stage_cooldown_sec
            appear_count = int(previous.get("appear_count", 0) or 0) + 1
            record = {
                **previous,
                **analyzed,
                "stage": next_stage,
                "first_seen": previous.get("first_seen", now_ts),
                "last_seen": now_ts,
                "last_active_at": now_ts,
                "appear_count": appear_count,
                "previous_stage": previous_stage,
                "reply_to_message_id": int(previous.get("last_message_id", 0) or 0),
            }
            for lifecycle_key in ("cooling_at", "failed_at", "fail_reason", "delete_pending"):
                record.pop(lifecycle_key, None)
            if stage_changed and cooldown_ok and analyzed["score"] >= self.settings.launch_min_score_push:
                alerts.append(record)
            state[analyzed["symbol"]] = record

        if analyzed_items and lifecycle_store is None:
            for symbol, previous in list(state.items()):
                if symbol in observed_symbols or not isinstance(previous, dict):
                    continue
                inactive = self._inactive_launch_record(
                    previous,
                    now_ts,
                    fail_reason="launch_candidate_disappeared",
                )
                if inactive is not None:
                    state[symbol] = inactive

        self.store.save(self.settings.launch_state_path, state)
        self.store.save(self.settings.launch_watchlist_path, {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(watchlist),
            "items": sorted(watchlist, key=lambda item: item["score"], reverse=True)[:30],
        })
        self.store.append_record(
            self.settings.launch_watch_history_path,
            self._launch_history_record(watchlist, alerts, now_ts),
            limit=self.settings.launch_watch_history_limit,
        )
        alerts = alerts[:5]
        chart_diagnostics: dict[str, Any] = {
            "enabled": bool(self.settings.launch_chart_v2_enable),
            "status": (
                "active"
                if self.settings.launch_chart_v2_enable
                and self.settings.launch_message_package_v2_enable
                else "misconfigured"
                if self.settings.launch_chart_v2_enable
                else "disabled"
            ),
            "ready": 0,
            "unavailable": 0,
        }
        if (
            self.settings.launch_chart_v2_enable
            and self.settings.launch_message_package_v2_enable
        ):
            for alert in alerts:
                if not alert.get("launch_message_package_v2"):
                    continue
                if self._attach_launch_chart(source, alert):
                    chart_diagnostics["ready"] += 1
                else:
                    chart_diagnostics["unavailable"] += 1
        messages = [self._format_launch_alert(alert) for alert in alerts]
        return {
            "template_id": "TG_LAUNCH_ALERT",
            "messages": messages,
            "alerts": alerts,
            "watchlist_count": len(watchlist),
            "diagnostics": {
                "binance_confirmation": confirmation_summary(analyzed_items),
                "lifecycle_v2": lifecycle_diagnostics,
                "chart_v2": chart_diagnostics,
            },
        }

    def mark_launch_pushed(self, alerts: list[dict[str, Any]]) -> None:
        if not alerts:
            return
        state = self.store.load(self.settings.launch_state_path, {})
        if not isinstance(state, dict):
            return
        now_ts = int(time.time())
        for alert in alerts:
            symbol = alert.get("symbol")
            if symbol in state and isinstance(state[symbol], dict):
                state[symbol]["last_pushed"] = now_ts
                state[symbol]["last_pushed_stage"] = alert.get("stage")
                message_ids = [
                    int(message_id)
                    for message_id in (alert.get("message_ids") or [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                ]
                if message_ids:
                    existing_message_ids = [
                        int(message_id)
                        for message_id in (state[symbol].get("message_ids") or [])
                        if isinstance(message_id, int) or str(message_id).isdigit()
                    ]
                    state[symbol]["last_message_id"] = message_ids[0]
                    state[symbol]["last_message_ids"] = message_ids
                    state[symbol]["message_ids"] = (
                        message_ids
                        if alert.get("launch_message_package_v2")
                        else list(dict.fromkeys(
                            [*existing_message_ids, *message_ids]
                        ))[-100:]
                    )
                    state[symbol]["last_message_stage"] = alert.get("stage")
        self.store.save(self.settings.launch_state_path, state)

    def commit_launch_package(
        self,
        alert: dict[str, Any],
        message_ids: list[int],
        *,
        published_at: int | None = None,
    ) -> dict[str, Any]:
        lifecycle = alert.get("launch_lifecycle")
        publication = alert.get("launch_package")
        if (
            not self.settings.launch_message_package_v2_enable
            or not isinstance(lifecycle, dict)
            or not isinstance(publication, dict)
        ):
            return {"status": "disabled", "delete_message_ids": []}
        return self._launch_lifecycle_store(package_enabled=True).commit_package(
            cycle_id=int(lifecycle.get("cycle_id") or 0),
            observation_id=int(lifecycle.get("observation_id") or 0),
            message_ids=message_ids,
            checkpoint_reasons=list(publication.get("checkpoint_reasons") or []),
            published_at=int(published_at or time.time()),
        )

    def complete_launch_package_cleanup(
        self,
        *,
        cycle_id: int,
        deleted_ids: list[int],
        failed_ids: list[int],
        updated_at: int | None = None,
        expire_latest: bool = False,
    ) -> dict[str, Any]:
        return self._launch_lifecycle_store(package_enabled=True).complete_package_cleanup(
            cycle_id=int(cycle_id),
            deleted_ids=deleted_ids,
            failed_ids=failed_ids,
            updated_at=int(updated_at or time.time()),
            expire_latest=expire_latest,
        )

    def pending_launch_package_cleanups(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if not self.settings.launch_message_package_v2_enable:
            return []
        return self._launch_lifecycle_store(package_enabled=True).list_pending_cleanups(
            limit=limit,
            now_ts=int(time.time()),
            max_age_sec=self.settings.launch_message_cleanup_max_age_sec,
        )

    def _launch_lifecycle_store(self, *, package_enabled: bool | None = None) -> LaunchLifecycleStore:
        return LaunchLifecycleStore(
            self.settings.signal_events_db_path,
            watch_score=self.settings.launch_watch_score,
            start_score=self.settings.launch_min_score_push,
            invalid_windows_required=self.settings.launch_lifecycle_invalid_windows,
            package_enabled=(
                self.settings.launch_message_package_v2_enable
                if package_enabled is None
                else bool(package_enabled)
            ),
            package_score_delta=self.settings.launch_package_score_delta,
            package_price_delta_pct=self.settings.launch_package_price_delta_pct,
            package_oi_delta_pct=self.settings.launch_package_oi_delta_pct,
            outcome_enabled=self.settings.launch_outcome_v2_enable,
            outcome_follow_through_pct=self.settings.launch_outcome_follow_through_pct,
            outcome_min_samples=self.settings.launch_outcome_min_samples,
            breakout_score=self.settings.launch_breakout_score,
            launched_score=self.settings.launch_launched_score,
        )

    def cleanup_failed_launch_messages(
        self,
        delete_messages: Callable[[list[int]], dict[str, list[int]] | int] | None = None,
        *,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Delete expired per-symbol launch messages while retaining signal evidence."""

        result: dict[str, Any] = {
            "enabled": bool(self.settings.launch_message_cleanup_enable),
            "failed_signals": 0,
            "candidate_messages": 0,
            "deleted_messages": 0,
            "undeletable_messages": 0,
            "failed_deletions": 0,
            "pending_signals": 0,
            "dry_run": delete_messages is None,
        }
        if not self.settings.launch_message_cleanup_enable:
            return result

        state = self.store.load(self.settings.launch_state_path, {})
        if not isinstance(state, dict):
            return result

        from .signal_store import SignalEventStore

        event_store = SignalEventStore(self.settings.signal_events_db_path)
        current_ts = int(now_ts or time.time())
        max_age_sec = max(1, int(self.settings.launch_message_cleanup_max_age_sec))
        delete_budget = max(0, int(self.settings.launch_message_cleanup_limit))
        attempted_messages = 0
        changed = False

        for symbol, record in state.items():
            if not isinstance(record, dict) or record.get("stage") != "failed":
                continue
            if record.get("launch_message_package_v2"):
                continue
            result["failed_signals"] += 1
            cycle_started_at = int(
                record.get("first_seen")
                or record.get("last_pushed")
                or record.get("failed_at")
                or current_ts
            )
            candidates = event_store.launch_message_cleanup_candidates(
                symbol=str(symbol),
                cycle_started_at=cycle_started_at,
                now_ts=current_ts,
                max_age_sec=max_age_sec,
            )
            deletable_ids = {
                int(message_id) for message_id in candidates["deletable_ids"]
            }
            undeletable_ids = {
                int(message_id) for message_id in candidates["undeletable_ids"]
            }
            database_ids = deletable_ids | undeletable_ids

            state_message_ids = {
                int(message_id)
                for key in ("message_ids", "last_message_ids")
                for message_id in (record.get(key) or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            }
            last_message_id = record.get("last_message_id")
            if isinstance(last_message_id, int) or str(last_message_id or "").isdigit():
                state_message_ids.add(int(last_message_id))
            completed_ids = {
                int(message_id)
                for key in ("deleted_message_ids", "undeletable_message_ids")
                for message_id in (record.get(key) or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            }
            state_only_ids = state_message_ids - database_ids - completed_ids
            sent_at = int(record.get("last_pushed", 0) or 0)
            if sent_at >= current_ts - max_age_sec:
                deletable_ids.update(state_only_ids)
            else:
                undeletable_ids.update(state_only_ids)

            result["candidate_messages"] += len(deletable_ids) + len(undeletable_ids)
            if undeletable_ids:
                event_store.mark_launch_message_cleanup(
                    symbol=str(symbol),
                    cycle_started_at=cycle_started_at,
                    message_ids=sorted(undeletable_ids),
                    outcome="undeletable",
                    now_ts=current_ts,
                )
                existing = {
                    int(message_id)
                    for message_id in (record.get("undeletable_message_ids") or [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                }
                record["undeletable_message_ids"] = sorted(existing | undeletable_ids)
                result["undeletable_messages"] += len(undeletable_ids)
                changed = True

            deleted_ids: set[int] = set()
            failed_ids: set[int] = set()
            available = max(0, delete_budget - attempted_messages)
            attempt_ids = sorted(deletable_ids)[:available]
            if delete_messages is not None and attempt_ids:
                attempted_messages += len(attempt_ids)
                delete_result = delete_messages(attempt_ids)
                if isinstance(delete_result, dict):
                    deleted_ids = {
                        int(message_id)
                        for message_id in (delete_result.get("deleted_ids") or [])
                        if isinstance(message_id, int) or str(message_id).isdigit()
                    } & set(attempt_ids)
                    failed_ids = {
                        int(message_id)
                        for message_id in (delete_result.get("failed_ids") or [])
                        if isinstance(message_id, int) or str(message_id).isdigit()
                    } & set(attempt_ids)
                else:
                    deleted_ids = set(attempt_ids[:max(0, int(delete_result))])
                    failed_ids = set(attempt_ids) - deleted_ids
                failed_ids.update(set(attempt_ids) - deleted_ids)
                if deleted_ids:
                    event_store.mark_launch_message_cleanup(
                        symbol=str(symbol),
                        cycle_started_at=cycle_started_at,
                        message_ids=sorted(deleted_ids),
                        outcome="deleted",
                        now_ts=current_ts,
                    )
                    existing = {
                        int(message_id)
                        for message_id in (record.get("deleted_message_ids") or [])
                        if isinstance(message_id, int) or str(message_id).isdigit()
                    }
                    record["deleted_message_ids"] = sorted(existing | deleted_ids)
                result["deleted_messages"] += len(deleted_ids)
                result["failed_deletions"] += len(failed_ids)
                record["last_delete_attempt"] = current_ts
                changed = True

            remaining_ids = deletable_ids - deleted_ids
            if remaining_ids:
                record["delete_pending"] = True
                record["message_cleanup_complete"] = False
                result["pending_signals"] += 1
            else:
                record["delete_pending"] = False
                record["message_cleanup_complete"] = True
                record["message_cleanup_completed_at"] = current_ts
                record["last_message_id"] = 0
                record["last_message_ids"] = []
                record["message_ids"] = []
            changed = True

        if changed:
            self.store.save(self.settings.launch_state_path, state)
        return result

    def _attach_launch_chart(
        self,
        source: BinanceDataSource,
        alert: dict[str, Any],
    ) -> bool:
        lifecycle = alert.get("launch_lifecycle")
        publication = alert.get("launch_package")
        if not isinstance(lifecycle, dict) or not isinstance(publication, dict):
            alert["chart_status"] = "unavailable"
            alert["chart_error"] = "missing_lifecycle_context"
            return False

        first_window_end = int(to_float(lifecycle.get("first_window_end")))
        current_window_end = int(to_float(lifecycle.get("window_end_ts")))
        if first_window_end <= 0 or current_window_end <= 0:
            alert["chart_status"] = "unavailable"
            alert["chart_error"] = "invalid_chart_window"
            return False
        interval_sec = 15 * 60
        requested_start = min(
            first_window_end - 16 * interval_sec,
            current_window_end - 96 * interval_sec,
        )
        start_ts = max(
            0,
            requested_start,
            current_window_end - 1000 * interval_sec,
        )
        candle_count = max(
            96,
            (current_window_end - start_ts) // interval_sec,
        )
        candle_count = min(1000, candle_count)
        try:
            rows = source.klines(
                str(alert.get("symbol") or ""),
                interval="15m",
                limit=int(candle_count),
                start_time=start_ts * 1000,
                end_time=current_window_end * 1000 - 1,
            )
            candles = [
                {
                    "close_ts": int(to_float(row[0])) // 1000 + interval_sec,
                    "open": to_float(row[1]),
                    "high": to_float(row[2]),
                    "low": to_float(row[3]),
                    "close": to_float(row[4]),
                    "quote_volume": to_float(row[7]),
                }
                for row in rows
                if isinstance(row, list) and len(row) >= 8
            ]
            checkpoints = [
                dict(checkpoint)
                for checkpoint in (publication.get("checkpoints") or [])
                if isinstance(checkpoint, dict)
            ]
            current = publication.get("current")
            if isinstance(current, dict):
                current_checkpoint = dict(current)
                current_checkpoint["checkpoint_no"] = int(
                    publication.get("checkpoint_no") or len(checkpoints) + 1
                )
                checkpoints.append(current_checkpoint)
            image = render_launch_chart_png(
                symbol=str(alert.get("symbol") or ""),
                candles=candles,
                checkpoints=checkpoints,
                cycle_no=int(lifecycle.get("cycle_no") or 1),
            )
        except Exception as exc:
            alert["chart_status"] = "unavailable"
            alert["chart_error"] = type(exc).__name__
            return False

        alert["chart_png_bytes"] = image
        alert["chart_status"] = "ready"
        alert["chart_candle_count"] = len(candles)
        alert["chart_checkpoint_count"] = len(checkpoints)
        alert["chart_generated_in_memory"] = True
        return True

    def _analyze_launch_symbol(self, source: BinanceDataSource, item: dict[str, Any]) -> Optional[dict[str, Any]]:
        symbol = item["symbol"]
        window = closed_window(interval_sec=15 * 60, delay_sec=self.settings.launch_close_delay_sec)
        lookback_ms = 17 * 15 * 60 * 1000
        klines = source.klines(
            symbol,
            interval="15m",
            limit=17,
            start_time=max(0, window.end_ms - lookback_ms),
            end_time=window.end_ms - 1,
        )
        oi_hist = source.open_interest_hist(
            symbol,
            period="15m",
            limit=17,
            start_time=max(0, window.end_ms - lookback_ms),
            end_time=window.end_ms,
        )
        if len(klines) < 5 or len(oi_hist) < 5:
            return None

        closes = [to_float(kline[4]) for kline in klines]
        highs = [to_float(kline[2]) for kline in klines]
        quote_volumes = [to_float(kline[7]) for kline in klines]
        oi_values = [to_float(row.get("sumOpenInterestValue")) for row in oi_hist]
        if min(closes[-5:]) <= 0 or min(oi_values[-5:]) <= 0:
            return None

        price_15m = pct(closes[-1], closes[-2])
        price_1h = pct(closes[-1], closes[-5])
        oi_15m = pct(oi_values[-1], oi_values[-2])
        oi_1h = pct(oi_values[-1], oi_values[-5])
        avg_volume = sum(quote_volumes[:-1]) / max(1, len(quote_volumes[:-1]))
        volume_ratio = quote_volumes[-1] / avg_volume if avg_volume > 0 else 0
        previous_high = max(highs[:-1])
        breakout = closes[-1] > previous_high if previous_high > 0 else False

        score = 0
        reasons: list[str] = []
        if price_15m >= 4:
            score += 25
            reasons.append(f"15m价格 {price_15m:+.1f}%")
        if price_1h >= 5:
            score += 15
            reasons.append(f"1h价格 {price_1h:+.1f}%")
        if breakout:
            score += 25
            reasons.append("突破近4h高点")
        if volume_ratio >= 2:
            score += 20
            reasons.append(f"成交 {volume_ratio:.1f}x 均值")
        if oi_15m >= 3:
            score += 15
            reasons.append(f"15m OI {oi_15m:+.1f}%")
        if oi_1h >= 6:
            score += 15
            reasons.append(f"1h OI {oi_1h:+.1f}%")
        if oi_1h >= 3 and abs(price_1h) <= 2:
            score += 15
            reasons.append("资金暗流但价格未大动")

        funding_pct = to_float(item.get("funding_pct"))
        next_funding_time_ms = int(to_float(item.get("funding_next_time_ms")))
        if score >= self.settings.launch_watch_score or funding_pct <= -0.5:
            funding_context = self._launch_funding_context(source, symbol, funding_pct, next_funding_time_ms)
        else:
            funding_context = {
                "funding_available": bool(item.get("funding_available")),
                "funding_pct": funding_pct,
                "funding_next_time_ms": next_funding_time_ms,
                "funding_interval_hours": 0,
                "funding_interval_transition": "",
            }
        if funding_pct <= -0.5:
            reasons.append(f"资金费率{funding_cycle_text(funding_pct, int(funding_context.get('funding_interval_hours', 0) or 0))}极负")
        elif funding_context.get("funding_interval_transition"):
            reasons.append("资金费率结算周期缩短")

        return {
            **item,
            "score": score,
            "closed_price": closes[-1],
            "closed_oi_usd": oi_values[-1],
            "closed_quote_volume": quote_volumes[-1],
            "price_15m": price_15m,
            "price_1h": price_1h,
            "oi_15m": oi_15m,
            "oi_1h": oi_1h,
            "volume_ratio": volume_ratio,
            "breakout": breakout,
            "breakout_price": previous_high,
            "reasons": reasons[:5],
            "kline_points": len(klines),
            "oi_points": len(oi_hist),
            "window_end_ts": int(window.end.timestamp()),
            **funding_context,
        }

    def _launch_funding_context(
        self,
        source: BinanceDataSource,
        symbol: str,
        funding_pct: float,
        next_funding_time_ms: int = 0,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "funding_available": True,
            "funding_pct": funding_pct,
            "funding_next_time_ms": next_funding_time_ms,
            "funding_interval_hours": 0,
            "funding_interval_transition": "",
        }
        if not hasattr(source, "funding_rate"):
            return context
        try:
            history = source.funding_rate(symbol, limit=4)
        except Exception:
            history = []
        if not isinstance(history, list):
            history = []
        transition = funding_interval_transition(history, next_funding_time_ms)
        if transition:
            context["funding_interval_hours"] = int(transition.get("current_interval_hours", 0) or 0)
            context["funding_interval_transition"] = str(transition.get("transition_text") or "")
            context["funding_previous_interval_hours"] = int(transition.get("previous_interval_hours", 0) or 0)
            context["funding_previous_time_ms"] = int(transition.get("previous_funding_time_ms", 0) or 0)
            context["funding_current_time_ms"] = int(transition.get("current_funding_time_ms", 0) or 0)
            return context

        points = sorted(
            [int(to_float(item.get("fundingTime"))) for item in history if to_float(item.get("fundingTime")) > 0]
        )
        if len(points) >= 2:
            context["funding_interval_hours"] = funding_interval_hours(points[-1] - points[-2])
        elif len(points) == 1 and next_funding_time_ms > points[-1]:
            context["funding_interval_hours"] = funding_interval_hours(next_funding_time_ms - points[-1])
        return context

    def _launch_stage(self, score: int) -> str:
        return self.launch_stage_for_score(
            score,
            watching=self.settings.launch_watch_score,
            primed=self.settings.launch_primed_score,
            breakout=self.settings.launch_breakout_score,
            launched=self.settings.launch_launched_score,
        )

    def _inactive_launch_record(
        self,
        previous: Any,
        now_ts: int,
        *,
        fail_reason: str,
    ) -> dict[str, Any] | None:
        if not isinstance(previous, dict) or not previous:
            return None
        record = dict(previous)
        stage = str(record.get("stage") or "idle")
        active_stages = {"watching", "primed", "breakout", "launched"}
        grace_sec = max(0, int(self.settings.launch_invalidation_grace_sec))

        if stage in active_stages:
            record["previous_stage"] = stage
            record["stage"] = "cooling"
            record["cooling_at"] = int(now_ts)
            record["last_seen"] = int(now_ts)
            if grace_sec > 0:
                return record
            stage = "cooling"

        if stage == "cooling":
            cooling_at = int(record.get("cooling_at", now_ts) or now_ts)
            record["last_seen"] = int(now_ts)
            if int(now_ts) - cooling_at < grace_sec:
                return record
            record["stage"] = "failed"
            record["failed_at"] = int(now_ts)
            record["fail_reason"] = str(fail_reason)
            record["delete_pending"] = True
            record["message_cleanup_complete"] = False
            return record

        if stage == "failed":
            return record
        return None

    @staticmethod
    def launch_stage_for_score(
        score: int,
        *,
        watching: int = 45,
        primed: int = 60,
        breakout: int = 75,
        launched: int = 90,
    ) -> str:
        if score >= launched:
            return "launched"
        if score >= breakout:
            return "breakout"
        if score >= primed:
            return "primed"
        if score >= watching:
            return "watching"
        return "idle"

    @staticmethod
    def _stage_rank(stage: str) -> int:
        return {
            "idle": 0,
            "cooling": 0,
            "failed": 0,
            "risk": 0,
            "watching": 1,
            "primed": 2,
            "breakout": 3,
            "launched": 4,
        }.get(stage, 0)

    @staticmethod
    def _stage_label(stage: str) -> str:
        return {
            "idle": "未触发",
            "cooling": "降温确认",
            "failed": "失效",
            "risk": "风险",
            "watching": "提前观察",
            "primed": "提前预警",
            "breakout": "启动确认",
            "launched": "启动瞬间",
        }.get(stage, stage or "未知")

    @staticmethod
    def _format_launch_funding_exchange(row: dict[str, Any]) -> str:
        funding_pct = to_float(row.get("funding_pct"))
        interval_hours = int(to_float(row.get("interval_hours")))
        text = funding_cycle_text(funding_pct, interval_hours)
        label = str(row.get("extreme_label") or "").strip()
        if label:
            text = f"{text}（{label}）"
        last_time = funding_last_settlement_text(row) or "未知"
        period = funding_settlement_period_text(row)
        next_time = str(row.get("next_funding_time") or "").strip() or "未知"
        exchange = str(row.get("exchange") or "Unknown").strip()
        return f"{exchange}: {text}｜上次结算 {last_time}｜周期 {period}｜下次结算 {next_time}"

    @staticmethod
    def _format_launch_funding_transitions(rows: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for row in rows:
            transition = str(row.get("funding_interval_transition") or "").strip()
            if transition:
                exchange = str(row.get("exchange") or "Unknown").strip()
                lines.append(f"{exchange}周期: {transition}")
        return lines

    @staticmethod
    def _launch_package_time(value: Any) -> str:
        timestamp = int(to_float(value))
        if timestamp <= 0:
            return "未知"
        return datetime.fromtimestamp(timestamp, CST).strftime("%m-%d %H:%M")

    @staticmethod
    def _launch_package_duration(value: Any) -> str:
        seconds = max(0, int(to_float(value)))
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        if hours:
            return f"{hours}小时{minutes:02d}分钟"
        return f"{minutes}分钟"

    @staticmethod
    def _launch_package_delta(current: Any, base: Any) -> float | None:
        current_value = to_float(current)
        base_value = to_float(base)
        if base_value <= 0:
            return None
        return (current_value / base_value - 1.0) * 100.0

    @staticmethod
    def _launch_package_funding(snapshot: dict[str, Any] | None) -> str:
        if not isinstance(snapshot, dict):
            return "暂不可用"
        funding = to_float(snapshot.get("funding_pct"))
        interval = int(to_float(snapshot.get("funding_interval_hours")))
        return funding_cycle_text(funding, interval) if interval > 0 else f"{funding:+.4f}%/周期暂不可用"

    @staticmethod
    def _launch_package_direction(value: Any) -> str:
        return {
            "both_buy": "现货与合约主动买入同步",
            "both_sell": "现货与合约主动卖出同步",
            "divergence_spot_buy_futures_sell": "现货主动买入、合约主动卖出",
            "divergence_spot_sell_futures_buy": "现货主动卖出、合约主动买入",
            "unknown": "主动成交方向暂不可用",
        }.get(str(value or "unknown"), "主动成交方向暂不可用")

    def _format_launch_package(self, item: dict[str, Any]) -> str:
        lifecycle = item.get("launch_lifecycle")
        publication = item.get("launch_package")
        if not isinstance(lifecycle, dict) or not isinstance(publication, dict):
            return self._format_launch_alert({**item, "launch_message_package_v2": False})

        first = publication.get("first") if isinstance(publication.get("first"), dict) else {}
        previous = (
            publication.get("previous_published")
            if isinstance(publication.get("previous_published"), dict)
            else first
        )
        current = publication.get("current") if isinstance(publication.get("current"), dict) else {}
        checkpoint_no = int(publication.get("checkpoint_no") or 1)
        current_stage = self._stage_label(str(current.get("stage") or item.get("stage") or "idle"))
        first_stage = self._stage_label(str(first.get("stage") or "idle"))
        peak_stage = self._stage_label(str(lifecycle.get("peak_stage") or item.get("stage") or "idle"))
        price_from_first = self._launch_package_delta(current.get("price"), first.get("price"))
        oi_from_first = self._launch_package_delta(current.get("oi_usd"), first.get("oi_usd"))
        price_from_previous = self._launch_package_delta(current.get("price"), previous.get("price"))
        oi_from_previous = self._launch_package_delta(current.get("oi_usd"), previous.get("oi_usd"))
        reasons = [
            {
                "cycle_opened": "首次达到启动阈值",
                "stage_changed": "生命周期阶段变化",
                "score_delta": f"分数变化≥{self.settings.launch_package_score_delta}",
                "price_delta": f"价格变化≥{self.settings.launch_package_price_delta_pct:g}%",
                "oi_delta": f"OI变化≥{self.settings.launch_package_oi_delta_pct:g}%",
                "funding_interval_changed": "资金费率结算周期变化",
                "funds_divergence": "现货/合约主动成交方向背离",
            }.get(str(reason), str(reason))
            for reason in (publication.get("checkpoint_reasons") or [])
        ]

        checkpoints = [
            checkpoint
            for checkpoint in (publication.get("checkpoints") or [])
            if isinstance(checkpoint, dict)
        ]
        timeline = checkpoints[-5:] + [current]
        timeline_lines: list[str] = []
        for point in timeline:
            event_no = (
                checkpoint_no
                if point is current
                else int(point.get("checkpoint_no") or 0)
            )
            timeline_lines.append(
                f"{event_no:02d}. "
                f"{self._launch_package_time(point.get('window_end_ts'))}｜"
                f"{self._stage_label(str(point.get('stage') or 'idle'))} "
                f"{int(point.get('score') or 0)}分"
            )

        first_funding = self._launch_package_funding(first)
        current_funding = self._launch_package_funding(current)
        direction_text = self._launch_package_direction(current.get("funds_direction"))
        outcome_evaluation = (
            lifecycle.get("outcome_evaluation")
            if isinstance(lifecycle.get("outcome_evaluation"), dict)
            else {}
        )
        progress = (
            outcome_evaluation.get("progress")
            if isinstance(outcome_evaluation.get("progress"), dict)
            else {}
        )
        outcome = (
            outcome_evaluation.get("outcome")
            if isinstance(outcome_evaluation.get("outcome"), dict)
            else {}
        )
        reliability = (
            outcome_evaluation.get("reliability")
            if isinstance(outcome_evaluation.get("reliability"), dict)
            else {}
        )
        outcome_lines: list[str] = []
        if outcome_evaluation.get("enabled") and progress:
            follow_threshold = to_float(
                reliability.get("follow_through_threshold_pct")
                or self.settings.launch_outcome_follow_through_pct
            )
            if outcome:
                outcome_label = {
                    "launched_follow_through": "达到启动瞬间且价格完成跟随",
                    "confirmed_follow_through": "达到启动确认且价格完成跟随",
                    "price_follow_through_only": "价格完成跟随但未达到启动确认",
                    "confirmed_no_follow_through": "达到启动确认但价格未完成跟随",
                    "false_start": "未达到启动确认且价格未完成跟随",
                }.get(str(outcome.get("label") or ""), "本轮已完成评估")
                outcome_lines.extend([
                    "",
                    tg_quote("本轮结果"),
                    f"状态: 已结束｜{outcome_label}",
                    f"结束收益: {to_float(progress.get('end_return_pct')):+.2f}%｜"
                    f"有效观察: {int(progress.get('observation_count') or 0)}根15m",
                ])
            else:
                outcome_lines.extend([
                    "",
                    tg_quote("本轮进展"),
                    "状态: 监控中｜本轮结束后才计入历史样本",
                ])
            outcome_lines.extend([
                f"最高/最低收盘变动: "
                f"{to_float(progress.get('max_favorable_return_pct')):+.2f}% / "
                f"{to_float(progress.get('max_adverse_return_pct')):+.2f}%",
                f"OI最高/最低变动: "
                f"{to_float(progress.get('max_oi_increase_pct')):+.2f}% / "
                f"{to_float(progress.get('max_oi_decrease_pct')):+.2f}%",
                "达到启动确认: "
                + (
                    self._launch_package_duration(progress.get("time_to_confirm_sec"))
                    if progress.get("confirmed")
                    else "尚未达到"
                ),
                "达到启动瞬间: "
                + (
                    self._launch_package_duration(progress.get("time_to_launch_sec"))
                    if progress.get("launched")
                    else "尚未达到"
                ),
                "",
                tg_quote("历史可靠度"),
            ])
            completed_samples = int(reliability.get("completed_samples") or 0)
            minimum_samples = int(
                reliability.get("minimum_samples")
                or self.settings.launch_outcome_min_samples
            )
            if reliability.get("rates_available"):
                outcome_lines.extend([
                    f"状态: 已达到复盘门槛｜同口径 {completed_samples} 轮",
                    f"启动确认率 {to_float(reliability.get('confirmed_rate_pct')):.1f}%｜"
                    f"启动瞬间率 {to_float(reliability.get('launched_rate_pct')):.1f}%｜"
                    f"收盘跟随率 {to_float(reliability.get('followed_through_rate_pct')):.1f}%",
                    f"中位最高/最低收盘变动 "
                    f"{to_float(reliability.get('median_max_favorable_return_pct')):+.2f}% / "
                    f"{to_float(reliability.get('median_max_adverse_return_pct')):+.2f}%",
                ])
            else:
                outcome_lines.extend([
                    f"状态: 样本积累中｜同口径已完成 {completed_samples}/{minimum_samples} 轮",
                    f"原始计数: 启动确认 {int(reliability.get('confirmed_count') or 0)}｜"
                    f"启动瞬间 {int(reliability.get('launched_count') or 0)}｜"
                    f"收盘涨幅达到 +{follow_threshold:g}% "
                    f"{int(reliability.get('followed_through_count') or 0)}",
                    "样本未达门槛，不展示比例，也不自动调整信号参数。",
                ])
            symbol_samples = int(
                reliability.get("symbol_completed_samples") or 0
            )
            if symbol_samples:
                outcome_lines.append(
                    f"该币历史完整周期: {symbol_samples}轮；样本不足时不单列比率。"
                )
        status = str(lifecycle.get("cycle_status") or "active")
        ending_lines = (
            [
                "",
                tg_quote("本轮结束"),
                f"失效原因: {tg_escape(str(lifecycle.get('end_reason') or '失效条件成立'))}",
            ]
            if status == "failed"
            else []
        )
        return "\n".join([
            f"🚀 {coin_link(item)}｜第{int(lifecycle.get('cycle_no') or 1)}轮启动跟踪｜事件{checkpoint_no:02d}",
            f"⏰ {cst_now_text()}",
            "",
            f"{tg_bold('当前')}: {current_stage} {int(current.get('score') or item.get('score') or 0)}分",
            f"{tg_bold('首次出现')}: {self._launch_package_time(first.get('window_end_ts'))}｜{first_stage} {int(first.get('score') or 0)}分",
            f"{tg_bold('持续时间')}: {self._launch_package_duration(lifecycle.get('duration_sec'))}",
            f"{tg_bold('最高阶段')}: {peak_stage}",
            f"{tg_bold('本次更新')}: {tg_escape('、'.join(reasons) or '重要状态更新')}",
            "",
            tg_quote("相对首次"),
            f"价格: {price_from_first:+.2f}%" if price_from_first is not None else "价格: 暂不可比",
            f"OI: {oi_from_first:+.2f}%" if oi_from_first is not None else "OI: 暂不可比",
            f"资金费率: {first_funding} → {current_funding}",
            "",
            tg_quote("相对上次发布"),
            f"价格: {price_from_previous:+.2f}%" if price_from_previous is not None else "价格: 暂不可比",
            f"OI: {oi_from_previous:+.2f}%" if oi_from_previous is not None else "OI: 暂不可比",
            f"分数: {int(previous.get('score') or 0)} → {int(current.get('score') or 0)}",
            f"主动成交: {tg_escape(direction_text)}",
            "",
            tg_quote("事件轴"),
            *timeline_lines,
            *outcome_lines,
            "",
            f"{tg_bold('数据确认')}: {tg_escape(confirmation_text(item))}",
            *ending_lines,
        ])

    def _format_launch_alert(self, item: dict[str, Any]) -> str:
        if item.get("launch_message_package_v2"):
            return self._format_launch_package(item)
        stage_name = self._stage_label(str(item.get("stage", "")))
        previous_stage = self._stage_label(str(item.get("previous_stage", "idle")))
        current_stage = self._stage_label(str(item.get("stage", "")))
        market_cap = to_float(item.get("mcap"))
        quote_volume = to_float(item.get("quote_volume"))
        market_cap_source = str(item.get("mcap_source") or "").strip()
        market_cap_text = (
            f"{fmt_money(market_cap)}（{market_cap_tier(market_cap)}，来源 {market_cap_source or '未知'}）"
            if market_cap > 0
            else "未收录（Binance/CoinPaprika 均无）"
        )
        liquidity_text = (
            f"{fmt_money(quote_volume)}/24h（{liquidity_tier(quote_volume)}）"
            if quote_volume > 0
            else "暂无数据（未知流动性）"
        )
        funding_pct = to_float(item.get("funding_pct"))
        funding_interval = int(to_float(item.get("funding_interval_hours")))
        funding_text = funding_cycle_text(funding_pct, funding_interval)
        funding_label = funding_extreme_label(funding_pct)
        if funding_label:
            funding_text = f"{funding_text}（{funding_label}）"
        funding_transition = str(item.get("funding_interval_transition") or "").strip()
        funding_available = bool(item.get("funding_available")) or funding_interval > 0 or bool(funding_transition)
        raw_funding_exchanges = item.get("funding_exchanges", [])
        if not isinstance(raw_funding_exchanges, list):
            raw_funding_exchanges = []
        funding_exchanges = [
            row for row in raw_funding_exchanges
            if isinstance(row, dict) and row.get("exchange")
        ]
        funding_exchange_table = funding_table(funding_exchanges, self.settings) if funding_exchanges else ""
        funding_transition_lines = self._format_launch_funding_transitions(funding_exchanges)
        single_funding_available = funding_available and not funding_exchange_table
        score_legend = (
            f"分数图例: <{self.settings.launch_watch_score}未触发 | "
            f"{self.settings.launch_watch_score}-{self.settings.launch_primed_score - 1}提前观察 | "
            f"{self.settings.launch_primed_score}-{self.settings.launch_breakout_score - 1}提前预警 | "
            f"{self.settings.launch_breakout_score}-{self.settings.launch_launched_score - 1}启动确认 | "
            f"≥{self.settings.launch_launched_score}启动瞬间"
        )
        lines = [
            f"🚀 {tg_bold('启动雷达')} {coin_link(item)}",
            f"⏰ {cst_now_text()}",
            "",
            f"{tg_bold('阶段')}: {stage_name}",
            f"{tg_bold('分数')}: {item['score']}",
            f"{tg_bold('状态')}: {previous_stage} -> {current_stage} | 累计{item.get('appear_count', 1)}次",
            "",
            tg_quote("市场概况"),
            f"市值: {market_cap_text}",
            f"流动性: {liquidity_text}",
            "",
            tg_quote("触发明细"),
            f"15m价格: {item['price_15m']:+.1f}%",
            f"1h价格: {item['price_1h']:+.1f}%",
            f"15m OI: {item['oi_15m']:+.1f}%",
            f"1h OI: {item['oi_1h']:+.1f}%",
            f"成交量: {item['volume_ratio']:.1f}x 均值",
            f"数据确认: ✅ {tg_escape(confirmation_text(item))}",
            *([f"资金费率: {funding_text}"] if single_funding_available else []),
            *([f"结算周期: {funding_transition}"] if funding_transition and single_funding_available else []),
            *(
                ["", tg_quote("多交易所资金费率"), funding_exchange_table, *funding_transition_lines]
                if funding_exchange_table
                else []
            ),
            "",
            tg_quote("判断"),
            "资金和价格开始共振，疑似进入启动阶段" if item.get("breakout") else "资金开始异动，进入观察状态",
            "",
            tg_quote("分数说明"),
            "构成(最高130): 15m价25 + 1h价15 + 突破25 + 成交20 + 15m OI15 + 1h OI15 + 暗流15",
            tg_escape(score_legend),
            "",
            tg_quote("数据与计算口径"),
            "来源: Binance USDⓈ-M Futures 原生公开行情",
            "价格/OI: 只使用已收线15m数据；1h变化由连续4个15m窗口计算",
            "成交倍数: 最新完整15m成交额 / 前序完整15m平均成交额",
            "突破: 最新15m收盘价高于前序窗口最高价",
            "",
            tg_quote("风险"),
            "跌回突破位则启动失败；同币同阶段会进入冷却",
        ]
        return "\n".join(lines)

    def _prune_launch_state(self, state: dict[str, Any], now_ts: int) -> None:
        for symbol, record in list(state.items()):
            if not isinstance(record, dict):
                del state[symbol]
                continue
            last_seen = int(record.get("last_seen", 0) or 0)
            if last_seen <= 0:
                del state[symbol]
                continue
            stage = str(record.get("stage") or "")
            if (
                stage == "failed"
                and self.settings.launch_message_cleanup_enable
                and not record.get("message_cleanup_complete")
            ):
                record["delete_pending"] = True
                continue
            anchor = int(record.get("failed_at", last_seen) or last_seen) if stage == "failed" else last_seen
            age = now_ts - anchor
            ttl = self.settings.launch_failed_ttl_sec if stage == "failed" else self.settings.launch_state_ttl_sec
            if ttl > 0 and age > ttl:
                del state[symbol]

    @staticmethod
    def _launch_watch_record(item: dict[str, Any], now_ts: int) -> dict[str, Any]:
        return {
            "ts": now_ts,
            "symbol": item["symbol"],
            "coin": item["coin"],
            "score": item["score"],
            "closed_price": round(to_float(item.get("closed_price")), 12),
            "closed_oi_usd": round(to_float(item.get("closed_oi_usd")), 2),
            "closed_quote_volume": round(to_float(item.get("closed_quote_volume")), 2),
            "price_15m": round(item["price_15m"], 4),
            "price_1h": round(item["price_1h"], 4),
            "oi_15m": round(item["oi_15m"], 4),
            "oi_1h": round(item["oi_1h"], 4),
            "data_quality_status": str(item.get("data_quality_status") or "not_checked"),
            "data_quality_score": round(to_float(item.get("data_quality_score")), 2),
            "quality_gate": str(item.get("quality_gate") or "degraded"),
            "primary_data_source": str(item.get("primary_data_source") or "binance"),
            "volume_ratio": round(item["volume_ratio"], 4),
            "breakout": bool(item["breakout"]),
            "quote_volume": round(item["quote_volume"], 2),
            "mcap": round(to_float(item.get("mcap")), 2),
            "mcap_source": str(item.get("mcap_source") or ""),
            "market_cap_tier": market_cap_tier(to_float(item.get("mcap"))),
            "liquidity_tier": liquidity_tier(to_float(item.get("quote_volume"))),
            "funding_available": bool(item.get("funding_available")),
            "funding_pct": round(to_float(item.get("funding_pct")), 6),
            "funding_interval_hours": int(to_float(item.get("funding_interval_hours"))),
            "funding_interval_transition": str(item.get("funding_interval_transition") or ""),
            "launch_lifecycle": item.get("launch_lifecycle", {}),
            "reasons": item.get("reasons", []),
        }

    def _is_excluded_symbol(self, symbol: str) -> bool:
        coin = symbol.upper()
        if coin.endswith("USDT"):
            coin = coin[:-4]
        return coin in set(self.settings.excluded_base_assets)

    def _launch_history_record(
        self,
        watchlist: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        now_ts: int,
    ) -> dict[str, Any]:
        sorted_items = sorted(watchlist, key=lambda item: item["score"], reverse=True)
        buckets = {"idle": 0, "watching": 0, "primed": 0, "breakout": 0, "launched": 0}
        for item in sorted_items:
            stage = self._launch_stage(int(item.get("score", 0)))
            buckets[stage] = buckets.get(stage, 0) + 1
        return {
            "ts": now_ts,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "scanned": len(watchlist),
            "alert_count": len(alerts),
            "top_score": int(sorted_items[0]["score"]) if sorted_items else 0,
            "buckets": buckets,
            "top_symbols": [item["symbol"] for item in sorted_items[:8]],
            "items": sorted_items[:10],
        }
