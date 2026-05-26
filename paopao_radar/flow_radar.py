from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from .config import Settings
from .data_sources import BinanceDataSource, CoinglassDataSource
from .radar import fmt_money, pct_cell, to_float


CST = timezone(timedelta(hours=8))


def cst_now_text(fmt: str = "%m-%d %H:%M CST") -> str:
    return datetime.now(CST).strftime(fmt)


def tg_escape(value: Any) -> str:
    return escape(str(value), quote=False)


def tg_bold(value: Any) -> str:
    return f"<b>{tg_escape(value)}</b>"


def tg_quote(title: str) -> str:
    return f"<blockquote><b>{tg_escape(title)}</b></blockquote>"


def coinglass_tv_url(coin_or_symbol: str) -> str:
    symbol = str(coin_or_symbol).upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    return f"https://www.coinglass.com/tv/zh/Binance_{escape(symbol, quote=True)}"


def coin_link(symbol: str) -> str:
    coin = symbol.upper()
    if coin.endswith("USDT"):
        coin = coin[:-4]
    return f'<a href="{coinglass_tv_url(coin)}"><b>{tg_escape(coin)}</b></a>'


def flatten_points(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("list", "data", "items", "rows", "values", "history"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    return []


def numeric_from_point(point: Any) -> float:
    if isinstance(point, dict):
        for key in (
            "close",
            "c",
            "cvd",
            "value",
            "sum",
            "cumulative_volume_delta",
            "cumulativeVolumeDelta",
            "net_buy_volume",
            "netBuyVolume",
        ):
            if key in point:
                return to_float(point.get(key))
        buy = (
            point.get("taker_buy_volume")
            or point.get("takerBuyVolume")
            or point.get("buy_volume")
            or point.get("buyVolume")
        )
        sell = (
            point.get("taker_sell_volume")
            or point.get("takerSellVolume")
            or point.get("sell_volume")
            or point.get("sellVolume")
        )
        if buy is not None or sell is not None:
            return to_float(buy) - to_float(sell)
    if isinstance(point, (list, tuple)):
        for value in reversed(point):
            parsed = to_float(value, default=float("nan"))
            if parsed == parsed:
                return parsed
    return to_float(point)


def series_delta_info(data: Any) -> tuple[float, bool, int]:
    points = flatten_points(data)
    values = [numeric_from_point(point) for point in points]
    values = [value for value in values if value == value]
    if len(values) < 2:
        return 0.0, False, len(values)
    return values[-1] - values[0], True, len(values)


def series_delta(data: Any) -> float:
    delta, _ready, _count = series_delta_info(data)
    return delta


def normalise_pct(value: float) -> float:
    if abs(value) < 1:
        return value * 100
    return value


def market_by_symbol(data: Any) -> dict[str, dict[str, Any]]:
    items = flatten_points(data)
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or item.get("baseAsset") or "").upper().replace("USDT", "")
        if symbol:
            result[symbol] = item
    return result


def first_value_info(item: dict[str, Any], keys: tuple[str, ...], default: float = 0.0) -> tuple[float, bool]:
    for key in keys:
        if key in item and item.get(key) not in (None, ""):
            return to_float(item.get(key), default), True
    return default, False


def first_value(item: dict[str, Any], keys: tuple[str, ...], default: float = 0.0) -> float:
    value, _found = first_value_info(item, keys, default)
    return value


def fmt_cvd(value: float, ready: bool) -> str:
    return fmt_money(value) if ready else "缺失"


def binance_oi_stats(source: BinanceDataSource, symbol: str) -> tuple[float, float]:
    history = source.open_interest_hist(symbol, period="1h", limit=25)
    if len(history) < 2:
        return 0.0, 0.0
    first = to_float(history[0].get("sumOpenInterestValue") or history[0].get("sumOpenInterest"))
    last = to_float(history[-1].get("sumOpenInterestValue") or history[-1].get("sumOpenInterest"))
    if first <= 0:
        return 0.0, last
    return (last - first) / first * 100, last


def flow_category(item: dict[str, Any]) -> tuple[str, int, str]:
    price = item["price_24h"]
    oi = item["oi_24h"]
    spot = item["spot_cvd_delta"]
    futures = item["futures_cvd_delta"]
    funding = item["funding_pct"]
    spot_ready = bool(item.get("spot_cvd_ready", True))
    futures_ready = bool(item.get("futures_cvd_ready", True))
    if not spot_ready and not futures_ready:
        return ("数据不足", 0, "CoinGlass CVD 数据缺失，不能判断资金流")
    spot_positive = spot_ready and spot > 0
    spot_negative = spot_ready and spot < 0
    futures_positive = futures_ready and futures > 0
    futures_negative = futures_ready and futures < 0

    candidates: list[tuple[str, int, str]] = []
    true_launch = 0
    true_launch += 20 if price >= 3 else 0
    true_launch += 20 if oi >= 5 else 0
    true_launch += 25 if spot_positive else 0
    true_launch += 15 if futures_positive else 0
    true_launch += 10 if funding <= 0.08 else 0
    true_launch += 10 if item["quote_volume"] >= 50_000_000 else 0
    candidates.append(("真启动候选", true_launch, "现货主动买入跟随，OI同步增加，费率未过热"))

    accumulation = 0
    accumulation += 25 if abs(price) <= 5 else 0
    accumulation += 25 if oi >= 5 else 0
    accumulation += 25 if spot_positive else 0
    accumulation += 15 if funding <= 0.03 else 0
    accumulation += 10 if futures >= 0 else 0
    candidates.append(("吸筹观察", accumulation, "价格未大幅启动但资金提前进入，适合提前盯盘"))

    short_fuel = 0
    short_fuel += 25 if funding <= -0.03 else 0
    short_fuel += 25 if oi >= 5 else 0
    short_fuel += 20 if futures_negative else 0
    short_fuel += 15 if price > -5 else 0
    short_fuel += 15 if item["quote_volume"] >= 30_000_000 else 0
    candidates.append(("空头燃料", short_fuel, "负费率叠加增仓，可能形成挤空条件"))

    perp_pump = 0
    perp_pump += 25 if price >= 5 else 0
    perp_pump += 20 if oi >= 5 else 0
    perp_pump += 25 if futures_positive else 0
    perp_pump += 20 if spot_ready and not spot_positive else 0
    perp_pump += 10 if funding >= 0 else 0
    candidates.append(("合约拉盘", perp_pump, "合约主动买入强于现货，追高风险更高"))

    short_squeeze = 0
    short_squeeze += 30 if price >= 5 else 0
    short_squeeze += 30 if oi <= -3 else 0
    short_squeeze += 20 if futures_positive else 0
    short_squeeze += 10 if spot_ready and not spot_positive else 0
    short_squeeze += 10 if funding <= 0.05 else 0
    candidates.append(("挤空/止损", short_squeeze, "上涨伴随OI下降，可能是空头止损推动"))

    distribution = 0
    distribution += 25 if price >= 5 else 0
    distribution += 30 if spot_ready and not spot_positive else 0
    distribution += 20 if futures_positive else 0
    distribution += 15 if funding >= 0.05 else 0
    distribution += 10 if oi <= 0 else 0
    candidates.append(("诱多/派发", distribution, "价格上涨但现货主动买入不足，持续性存疑"))

    panic = 0
    panic += 25 if price <= -5 else 0
    panic += 25 if oi >= 5 else 0
    panic += 25 if spot_negative else 0
    panic += 15 if futures_negative else 0
    panic += 10 if funding < 0 else 0
    candidates.append(("恐慌下跌", panic, "下跌增仓且CVD走弱，空头压制或多头被套"))

    return max(candidates, key=lambda row: row[1])


class FlowRadarEngine:
    def __init__(self, settings: Settings):
        self.settings = settings

    def build(self, binance: BinanceDataSource, coinglass: CoinglassDataSource) -> dict[str, Any]:
        if not coinglass.enabled:
            return {
                "template_id": "TG_FLOW_RADAR",
                "dedup_key": f"flow-radar:{datetime.now(CST).strftime('%Y%m%d%H%M')}",
                "text": "🧭 五因子资金流雷达\n\nCoinGlass 未启用，无法计算 CVD。",
                "items": [],
                "diagnostics": {"coinglass": coinglass.diagnostics(), "binance": binance.diagnostics()},
            }

        candidates = self._candidate_symbols(binance)
        market_map = market_by_symbol(coinglass.coins_markets(
            exchange_list=self.settings.coinglass_exchange_list,
            per_page=max(10, self.settings.flow_candidate_pool),
            page=1,
        ))
        rows: list[dict[str, Any]] = []
        scanned_items: list[dict[str, Any]] = []
        for candidate in candidates[: max(1, self.settings.flow_scan_limit)]:
            symbol = candidate["symbol"]
            coin = candidate["coin"]
            market = market_map.get(coin, {})
            spot_cvd, spot_cvd_ready, spot_cvd_points = series_delta_info(coinglass.spot_aggregated_cvd_history(
                coin,
                exchange_list=self.settings.coinglass_exchange_list,
                interval="1h",
                limit=6,
            ))
            futures_cvd, futures_cvd_ready, futures_cvd_points = series_delta_info(coinglass.futures_aggregated_cvd_history(
                coin,
                exchange_list=self.settings.coinglass_exchange_list,
                interval="1h",
                limit=6,
            ))
            price_24h = first_value(
                market,
                (
                    "price_change_percent_24h",
                    "priceChangePercent24h",
                    "price_change_percent",
                    "priceChangePercent",
                    "change_percent_24h",
                    "changePercent24h",
                ),
                candidate["price_24h"],
            )
            oi_24h, oi_found = first_value_info(
                market,
                (
                    "open_interest_change_percent_24h",
                    "openInterestChangePercent24h",
                    "open_interest_change_percent",
                    "openInterestChangePercent",
                    "oi_change_percent_24h",
                    "oiChangePercent24h",
                ),
                0.0,
            )
            funding_pct = normalise_pct(first_value(
                market,
                (
                    "avg_funding_rate_by_oi",
                    "avgFundingRateByOi",
                    "funding_rate",
                    "fundingRate",
                    "funding_rate_percent",
                    "fundingRatePercent",
                ),
                candidate.get("funding_pct", 0.0),
            ))
            quote_volume = to_float(
                market.get("volume_change_usd_24h")
                or market.get("volumeChangeUsd24h")
                or market.get("volume_usd")
                or market.get("volumeUsd")
                or market.get("volume_24h")
                or market.get("volume24h")
                or market.get("turnover_usd_24h")
                or market.get("turnoverUsd24h")
                or candidate["quote_volume"]
            )
            oi_usd, oi_usd_found = first_value_info(
                market,
                ("open_interest_usd", "openInterestUsd", "open_interest", "openInterest"),
                0.0,
            )
            if not oi_found or not oi_usd_found:
                oi_fallback_pct, oi_fallback_usd = binance_oi_stats(binance, symbol)
                if not oi_found:
                    oi_24h = oi_fallback_pct
                if not oi_usd_found:
                    oi_usd = oi_fallback_usd
            item = {
                "symbol": symbol,
                "coin": coin,
                "price_24h": price_24h,
                "oi_24h": oi_24h,
                "spot_cvd_delta": spot_cvd,
                "futures_cvd_delta": futures_cvd,
                "spot_cvd_ready": spot_cvd_ready,
                "futures_cvd_ready": futures_cvd_ready,
                "spot_cvd_points": spot_cvd_points,
                "futures_cvd_points": futures_cvd_points,
                "funding_pct": funding_pct,
                "quote_volume": abs(quote_volume),
                "oi_usd": oi_usd,
            }
            category, score, reason = flow_category(item)
            item.update({"category": category, "score": score, "reason": reason})
            scanned_items.append(item)
            if score >= self.settings.flow_min_score:
                rows.append(item)

        rows.sort(key=lambda item: item["score"], reverse=True)
        rows = rows[: max(1, self.settings.flow_top_n)]
        return {
            "template_id": "TG_FLOW_RADAR",
            "dedup_key": f"flow-radar:{datetime.now(CST).strftime('%Y%m%d%H%M')}",
            "text": self._format(rows, candidates, coinglass, scanned_items),
            "items": rows,
            "diagnostics": {"coinglass": coinglass.diagnostics(), "binance": binance.diagnostics()},
        }

    def _candidate_symbols(self, source: BinanceDataSource) -> list[dict[str, Any]]:
        valid_symbols = {item.get("symbol", "") for item in source.usdt_perp_symbols()}
        premium_map = {
            item.get("symbol"): to_float(item.get("lastFundingRate")) * 100
            for item in source.premium_index()
            if item.get("symbol") in valid_symbols
        }
        candidates: list[dict[str, Any]] = []
        for item in source.ticker_24h():
            symbol = str(item.get("symbol") or "")
            if symbol not in valid_symbols:
                continue
            coin = symbol.replace("USDT", "")
            if coin in set(self.settings.excluded_base_assets):
                continue
            quote_volume = to_float(item.get("quoteVolume"))
            if quote_volume < self.settings.radar_min_quote_volume:
                continue
            price_24h = to_float(item.get("priceChangePercent"))
            candidates.append({
                "symbol": symbol,
                "coin": coin,
                "price_24h": price_24h,
                "quote_volume": quote_volume,
                "funding_pct": premium_map.get(symbol, 0.0),
            })
        candidates.sort(key=lambda item: (item["quote_volume"], abs(item["price_24h"])), reverse=True)
        return candidates[: max(1, self.settings.flow_candidate_pool)]

    def _format(
        self,
        rows: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        coinglass: CoinglassDataSource,
        scanned_items: list[dict[str, Any]],
    ) -> str:
        spot_ready_count = sum(1 for item in scanned_items if item.get("spot_cvd_ready"))
        futures_ready_count = sum(1 for item in scanned_items if item.get("futures_cvd_ready"))
        scanned_count = len(scanned_items)
        lines = [
            "🧭 <b>五因子资金流雷达</b>",
            f"⏰ {cst_now_text()}",
            "",
            tg_quote("📊 本轮统计"),
            f"候选币: {len(candidates)}",
            f"入选信号: {len(rows)}",
            f"CoinGlass请求: {coinglass.budget.used.get('coinglass', 0)} / {coinglass.budget.limits.get('coinglass', 0)}",
            f"CVD数据: 现货 {spot_ready_count}/{scanned_count} | 合约 {futures_ready_count}/{scanned_count}",
            "",
        ]
        if scanned_count and (spot_ready_count < scanned_count or futures_ready_count < scanned_count):
            lines.extend([
                "⚠️ 部分 CVD 数据缺失；缺失项不会按 0 参与资金流评分。",
                "",
            ])
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in rows:
            grouped.setdefault(item["category"], []).append(item)
        for category in ("真启动候选", "吸筹观察", "空头燃料", "合约拉盘", "挤空/止损", "诱多/派发", "恐慌下跌"):
            items = grouped.get(category, [])
            if not items:
                continue
            lines.append(tg_quote(category))
            for item in items[:4]:
                lines.append(coin_link(item["coin"]))
                lines.append(
                    f"{item['score']}分 | 价{pct_cell(item['price_24h'])} | "
                    f"OI{pct_cell(item['oi_24h'])} | "
                    f"现货CVD {fmt_cvd(item['spot_cvd_delta'], bool(item.get('spot_cvd_ready')))} | "
                    f"合约CVD {fmt_cvd(item['futures_cvd_delta'], bool(item.get('futures_cvd_ready')))} | "
                    f"费率 {item['funding_pct']:+.3f}%"
                )
                lines.append(f"判断: {tg_escape(item['reason'])}")
            lines.append("")
        if not rows:
            lines.extend([
                "暂无达标信号",
                "如果 CoinGlass请求正常但 CVD 长期缺失，通常是当前 API 权限、接口返回字段或交易所支持范围问题。",
                "",
            ])
        lines.extend([
            tg_quote("📖 图例"),
            "真启动 = 价格、OI、现货CVD、合约CVD共振且费率未过热",
            "吸筹 = 价格未明显启动，但OI和现货CVD提前增强",
            "合约拉盘 = 合约CVD强、现货CVD弱，追高风险更高",
            "诱多/派发 = 价格上涨但现货主动买入不足",
            "CVD = 主动买入量 - 主动卖出量，正值代表主动买盘更强",
        ])
        return "\n".join(lines)
