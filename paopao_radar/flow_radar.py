from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from .config import Settings
from .data_sources import BinanceDataSource
from .radar import fmt_money, pct_cell, to_float
from .time_windows import ClosedWindow, closed_window


CST = timezone(timedelta(hours=8))
CVD_NEUTRAL_ABS = 1.0


def cst_now_text(fmt: str = "%m-%d %H:%M CST") -> str:
    return datetime.now(CST).strftime(fmt)


def tg_escape(value: Any) -> str:
    return escape(str(value), quote=False)


def tg_bold(value: Any) -> str:
    return f"<b>{tg_escape(value)}</b>"


def tg_quote(title: str) -> str:
    return f"<blockquote><b>{tg_escape(title)}</b></blockquote>"


def seconds_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def binance_futures_url(coin_or_symbol: str) -> str:
    symbol = str(coin_or_symbol).upper()
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    return f"https://www.binance.com/zh-CN/futures/{escape(symbol, quote=True)}"


def coin_link(symbol: str) -> str:
    coin = symbol.upper()
    if coin.endswith("USDT"):
        coin = coin[:-4]
    return f'<a href="{binance_futures_url(coin)}"><b>{tg_escape(coin)}</b></a>'


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


def normalize_timestamp_ms(value: Any) -> int:
    try:
        ts = float(value)
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    if ts < 10_000_000_000:
        ts *= 1000
    return int(ts)


def point_timestamp_ms(point: Any) -> int:
    if isinstance(point, dict):
        for key in (
            "time",
            "timestamp",
            "t",
            "T",
            "openTime",
            "open_time",
            "createTime",
            "create_time",
            "dataTime",
            "data_time",
        ):
            if key in point:
                ts = normalize_timestamp_ms(point.get(key))
                if ts:
                    return ts
    if isinstance(point, (list, tuple)):
        for value in point[:2]:
            ts = normalize_timestamp_ms(value)
            if ts:
                return ts
    return 0


def filter_points_by_time(data: Any, start_ms: int | None, end_ms: int | None) -> list[Any]:
    points = flatten_points(data)
    if start_ms is None and end_ms is None:
        return points
    filtered: list[Any] = []
    for point in points:
        ts = point_timestamp_ms(point)
        if not ts:
            continue
        if start_ms is not None and ts < start_ms:
            continue
        if end_ms is not None and ts > end_ms:
            continue
        filtered.append(point)
    return filtered


def series_delta_info(
    data: Any,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
) -> tuple[float, bool, int]:
    points = filter_points_by_time(data, start_ms, end_ms)
    values = [numeric_from_point(point) for point in points]
    values = [value for value in values if value == value]
    if len(values) < 2:
        return 0.0, False, len(values)
    return values[-1] - values[0], True, len(values)


def cvd_positive(value: float, ready: bool) -> bool:
    return ready and value > CVD_NEUTRAL_ABS


def cvd_negative(value: float, ready: bool) -> bool:
    return ready and value < -CVD_NEUTRAL_ABS


def fmt_signed_money(value: float) -> str:
    sign = "+" if value > 0 else "-"
    amount = abs(value)
    if amount >= 1_000_000_000:
        return f"{sign}${amount / 1_000_000_000:.1f}B"
    if amount >= 1_000_000:
        return f"{sign}${amount / 1_000_000:.1f}M"
    if amount >= 1_000:
        return f"{sign}${amount / 1_000:.1f}K"
    if amount >= 1:
        return f"{sign}${amount:.0f}"
    return f"{sign}${amount:.3f}"


def fmt_cvd(value: float, ready: bool) -> str:
    if not ready:
        return "缺失"
    if abs(value) <= CVD_NEUTRAL_ABS:
        return "近0"
    return fmt_signed_money(value)


def binance_oi_stats(
    source: BinanceDataSource,
    symbol: str,
    *,
    window: ClosedWindow | None = None,
    period: str = "1h",
    limit: int = 25,
) -> tuple[float, float, bool, int]:
    start_time = None
    end_time = None
    if window is not None:
        start_time = max(0, window.start_ms - window.interval_ms)
        end_time = window.end_ms
        limit = max(limit, 3)
    history = source.open_interest_hist(
        symbol,
        period=period,
        limit=limit,
        start_time=start_time,
        end_time=end_time,
    )
    if window is not None:
        history = filter_points_by_time(history, start_time, end_time)
    if len(history) < 2:
        return 0.0, 0.0, False, len(history)
    first = to_float(history[0].get("sumOpenInterestValue") or history[0].get("sumOpenInterest"))
    last = to_float(history[-1].get("sumOpenInterestValue") or history[-1].get("sumOpenInterest"))
    if first <= 0:
        return 0.0, last, False, len(history)
    return (last - first) / first * 100, last, True, len(history)


def binance_window_price_pct(source: BinanceDataSource, symbol: str, window: ClosedWindow) -> tuple[float, bool]:
    klines = source.klines(
        symbol,
        interval="1h",
        limit=3,
        start_time=window.start_ms,
        end_time=window.end_ms - 1,
    )
    selected = [
        kline for kline in klines
        if isinstance(kline, list)
        and kline
        and window.start_ms <= normalize_timestamp_ms(kline[0]) < window.end_ms
    ]
    if not selected:
        return 0.0, False
    kline = selected[-1]
    if len(kline) < 5:
        return 0.0, False
    open_price = to_float(kline[1])
    close_price = to_float(kline[4])
    if open_price <= 0:
        return 0.0, False
    return (close_price - open_price) / open_price * 100, True


def kline_cvd_delta_info(klines: list[list[Any]], window: ClosedWindow | None = None) -> tuple[float, bool, int]:
    total = 0.0
    count = 0
    for kline in klines:
        if not isinstance(kline, list) or len(kline) < 11:
            continue
        if window is not None:
            ts = normalize_timestamp_ms(kline[0])
            if not ts or ts < window.start_ms or ts >= window.end_ms:
                continue
        quote_volume = to_float(kline[7], default=float("nan"))
        taker_buy_quote = to_float(kline[10], default=float("nan"))
        if quote_volume != quote_volume or taker_buy_quote != taker_buy_quote:
            continue
        total += taker_buy_quote * 2 - quote_volume
        count += 1
    return total, count > 0, count


def binance_spot_cvd_stats(source: BinanceDataSource, symbol: str, window: ClosedWindow) -> tuple[float, bool, int]:
    klines = source.spot_klines(
        symbol,
        interval="1h",
        limit=3,
        start_time=window.start_ms,
        end_time=window.end_ms - 1,
    )
    return kline_cvd_delta_info(klines, window)


def binance_futures_cvd_stats(source: BinanceDataSource, symbol: str, window: ClosedWindow) -> tuple[float, bool, int]:
    klines = source.klines(
        symbol,
        interval="1h",
        limit=3,
        start_time=window.start_ms,
        end_time=window.end_ms - 1,
    )
    return kline_cvd_delta_info(klines, window)


def flow_category(item: dict[str, Any]) -> tuple[str, int, str]:
    if not item.get("price_ready", True) or not item.get("oi_ready", True):
        return ("数据不足", 0, "价格或 OI 未覆盖完整统计窗口，暂不评分")
    price = item["price_24h"]
    oi = item["oi_24h"]
    spot = item["spot_cvd_delta"]
    futures = item["futures_cvd_delta"]
    funding = item["funding_pct"]
    spot_ready = bool(item.get("spot_cvd_ready", True))
    futures_ready = bool(item.get("futures_cvd_ready", True))
    if not spot_ready and not futures_ready:
        return ("数据不足", 0, "Binance CVD 数据缺失，不能判断资金流")
    spot_positive = cvd_positive(spot, spot_ready)
    spot_negative = cvd_negative(spot, spot_ready)
    futures_positive = cvd_positive(futures, futures_ready)
    futures_negative = cvd_negative(futures, futures_ready)

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
    accumulation += 10 if futures_positive else 0
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
    perp_pump += 20 if price >= 5 and spot_negative else 0
    perp_pump += 10 if funding >= 0 else 0
    candidates.append(("合约拉盘", perp_pump, "合约主动买入强于现货，追高风险更高"))

    short_squeeze = 0
    short_squeeze += 30 if price >= 5 else 0
    short_squeeze += 30 if oi <= -3 else 0
    short_squeeze += 20 if futures_positive else 0
    short_squeeze += 10 if price >= 5 and spot_negative else 0
    short_squeeze += 10 if funding <= 0.05 else 0
    candidates.append(("挤空/止损", short_squeeze, "上涨伴随OI下降，可能是空头止损推动"))

    distribution = 0
    distribution += 25 if price >= 5 else 0
    distribution += 30 if price >= 5 and spot_negative else 0
    distribution += 20 if price >= 5 and futures_positive else 0
    distribution += 15 if price >= 5 and funding >= 0.05 else 0
    distribution += 10 if price >= 5 and oi <= 0 else 0
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

    def build(self, binance: BinanceDataSource) -> dict[str, Any]:
        window = closed_window(
            interval_sec=self.settings.flow_interval_sec,
            delay_sec=self.settings.flow_close_delay_sec,
        )
        candidates = self._candidate_symbols(binance)
        rows: list[dict[str, Any]] = []
        scanned_items: list[dict[str, Any]] = []
        for candidate in candidates[: max(1, self.settings.flow_scan_limit)]:
            symbol = candidate["symbol"]
            coin = candidate["coin"]
            spot_cvd, spot_cvd_ready, spot_cvd_points = binance_spot_cvd_stats(binance, symbol, window)
            futures_cvd, futures_cvd_ready, futures_cvd_points = binance_futures_cvd_stats(binance, symbol, window)
            price_pct, price_ready = binance_window_price_pct(binance, symbol, window)
            oi_24h, oi_fallback_usd, oi_ready, oi_points = binance_oi_stats(
                binance,
                symbol,
                window=window,
                period="1h",
                limit=4,
            )
            funding_pct = to_float(candidate.get("funding_pct", 0.0))
            quote_volume = to_float(candidate["quote_volume"])
            oi_usd = oi_fallback_usd
            item = {
                "symbol": symbol,
                "coin": coin,
                "price_24h": price_pct,
                "price_ready": price_ready,
                "oi_24h": oi_24h,
                "oi_ready": oi_ready,
                "oi_points": oi_points,
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
            "dedup_key": f"flow-radar:{window.end.strftime('%Y%m%d%H%M')}",
            "text": self._format(rows, candidates, scanned_items, window),
            "items": rows,
            "diagnostics": {"binance": binance.diagnostics()},
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
        scanned_items: list[dict[str, Any]],
        window: ClosedWindow,
    ) -> str:
        spot_ready_count = sum(1 for item in scanned_items if item.get("spot_cvd_ready"))
        futures_ready_count = sum(1 for item in scanned_items if item.get("futures_cvd_ready"))
        price_ready_count = sum(1 for item in scanned_items if item.get("price_ready"))
        oi_ready_count = sum(1 for item in scanned_items if item.get("oi_ready"))
        spot_active_count = sum(
            1 for item in scanned_items
            if item.get("spot_cvd_ready") and abs(float(item.get("spot_cvd_delta") or 0.0)) > CVD_NEUTRAL_ABS
        )
        futures_active_count = sum(
            1 for item in scanned_items
            if item.get("futures_cvd_ready") and abs(float(item.get("futures_cvd_delta") or 0.0)) > CVD_NEUTRAL_ABS
        )
        scanned_count = len(scanned_items)
        lines = [
            "🧭 <b>五因子资金流雷达</b>",
            f"⏰ {cst_now_text()}",
            f"统计窗口: {window.label()}",
            f"数据规则: 整点收线后延迟 {seconds_text(window.delay_sec)}抓取上一完整窗口",
            "",
            tg_quote("📊 本轮统计"),
            f"候选币: {len(candidates)}",
            f"入选信号: {len(rows)}",
            "数据源: Binance 免费公开接口（现货K线、合约K线、合约OI、资金费率）",
            f"窗口数据: 价格 {price_ready_count}/{scanned_count} | OI {oi_ready_count}/{scanned_count}",
            f"CVD数据(Binance估算): 现货有效 {spot_active_count}/{scanned_count}，可读 {spot_ready_count}/{scanned_count} | 合约有效 {futures_active_count}/{scanned_count}，可读 {futures_ready_count}/{scanned_count}",
            "",
        ]
        if scanned_count and (price_ready_count < scanned_count or oi_ready_count < scanned_count):
            lines.extend([
                "⚠️ 部分价格/OI 未覆盖完整统计窗口；这些币不会进入资金流评分。",
                "",
            ])
        if scanned_count and (spot_ready_count < scanned_count or futures_ready_count < scanned_count):
            lines.extend([
                "⚠️ 部分 CVD 数据缺失；缺失项不会按 0 参与资金流评分。",
                "",
            ])
        if scanned_count and (spot_active_count < spot_ready_count or futures_active_count < futures_ready_count):
            lines.extend([
                "ℹ️ 部分 CVD 近0；近0只作为中性状态，不按主动买入或主动卖出评分。",
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
                "如果 CVD 长期缺失，通常是币种没有对应 Binance 现货交易对、接口限频或窗口数据尚未完整。",
                "",
            ])
        lines.extend([
            tg_quote("📖 图例"),
            "显示分类 = 真启动候选 / 吸筹观察 / 空头燃料 / 合约拉盘 / 挤空/止损 / 诱多/派发 / 恐慌下跌；本轮只显示达标分类",
            "真启动 = 价格、OI、现货CVD、合约CVD共振且费率未过热",
            "吸筹 = 价格未明显启动，但OI和现货CVD提前增强",
            "空头燃料 = 负费率叠加增仓，偏挤空候选",
            "合约拉盘 = 合约CVD强、现货CVD弱，追高风险更高",
            "挤空/止损 = 上涨伴随OI下降，可能是空头止损推动",
            "诱多/派发 = 价格上涨但现货主动买入不足",
            "恐慌下跌 = 下跌增仓且CVD走弱，先按风险处理",
            "CVD = 主动买入量 - 主动卖出量，正值代表主动买盘更强",
        ])
        return "\n".join(lines)
