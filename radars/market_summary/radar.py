from __future__ import annotations

import sys
import time
from typing import Any, Optional

from .quality import analyze_accumulation_quality
from .diagnostics import (
    build_evaluation_result,
    build_scan_summary,
    persist_scan_summary,
    scan_summary_without_results,
    summary_log_line,
)
from shared.binance_confirmation import apply_binance_confirmation, confirmation_summary
from shared.binance_data import BinanceDataSource
from shared.time_windows import ClosedWindow, closed_window
from ..common import (
    RadarComponent,
    append_metric_row,
    coin_link,
    cst_now_text,
    estimate_sideways_days,
    fmt_money,
    fmt_price,
    funding_trend,
    pct,
    pct_cell,
    score_cell,
    score_funding,
    score_mcap,
    score_oi,
    score_sideways,
    seconds_text,
    tg_quote,
    to_float,
)


class MarketSummaryRadar(RadarComponent):
    def build_money_radar_summary(self, source: BinanceDataSource) -> dict[str, Any]:
        scan_started = time.time()
        window = closed_window(
            interval_sec=self.settings.radar_summary_min_interval_sec,
            delay_sec=self.settings.radar_summary_close_delay_sec,
        )
        items = self._load_market_items(source, window)
        now = cst_now_text()
        if not items:
            quality = source.diagnostics()
            diagnostics = self._record_accumulation_quality_scan(
                items,
                window=window,
                scan_started=scan_started,
            )
            if diagnostics:
                quality["accumulation_quality_v2"] = scan_summary_without_results(
                    diagnostics
                )
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
                "quality": quality,
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
                if (
                    item["ambush_score"] >= 35
                    and (item["sideways_days"] >= 45 or self._is_dark_flow(item))
                )
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
        diagnostics = self._record_accumulation_quality_scan(
            items,
            window=window,
            scan_started=scan_started,
        )
        if diagnostics:
            quality["accumulation_quality_v2"] = scan_summary_without_results(
                diagnostics
            )
        return {
            "template_id": "TG_RADAR_SUMMARY",
            "dedup_key": f"radar-summary:{window.end.strftime('%Y%m%d%H%M')}",
            "text": text,
            "quality": quality,
            "context_records": context_records,
        }

    def _record_accumulation_quality_scan(
        self,
        items: list[dict[str, Any]],
        *,
        window: ClosedWindow,
        scan_started: float,
    ) -> dict[str, Any] | None:
        completed = time.time()
        summary = build_scan_summary(
            items,
            scan_id=f"accumulation-quality:{window.end_ms}",
            scan_started_at=int(scan_started),
            scan_completed_at=int(completed),
            duration_sec=completed - scan_started,
            feature_enabled=True,
        )
        try:
            persist_scan_summary(
                self.store,
                self.settings.accumulation_quality_diagnostics_path,
                summary,
            )
        except Exception as exc:
            print(
                "accumulation_quality_diagnostics warning="
                f"{type(exc).__name__}",
                file=sys.stderr,
            )
        print(summary_log_line(summary))
        return summary

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
            accumulation_quality = analyze_accumulation_quality(
                daily,
                now_ms=int(time.time() * 1000),
                min_history_days=self.settings.accumulation_min_history_days,
                max_range_pct=self.settings.accumulation_max_range_pct,
                max_abs_slope_pct=self.settings.accumulation_max_abs_slope_pct,
                max_avg_daily_quote_volume=(
                    self.settings.accumulation_max_avg_daily_quote_volume
                ),
                recent_days=self.settings.accumulation_recent_days,
                max_recent_price_gain_pct=(
                    self.settings.accumulation_max_recent_price_gain_pct
                ),
            )
            accumulation_diagnostic = build_evaluation_result(
                symbol,
                accumulation_quality,
                input_row_count=len(daily),
                dark_flow_candidate=(
                    oi_6h > 2 and abs(price_window) < 5
                ),
                evaluated_at=int(time.time()),
            )
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
                "accumulation_quality_v2": accumulation_quality,
                "accumulation_quality_diagnostic": accumulation_diagnostic,
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
