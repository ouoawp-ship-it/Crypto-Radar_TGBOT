from __future__ import annotations

import sqlite3
import time
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any

from .binance_confirmation import (
    apply_binance_confirmation,
    confirmation_summary,
    confirmation_text,
)
from .config import Settings
from .data_sources import BinanceDataSource
from .market_links import coinglass_tv_url as _coinglass_tv_url
from .market_links import telegram_coin_links
from .radar import fmt_money, pct_cell, to_float
from .storage import JsonStore
from .time_windows import ClosedWindow, closed_window


CST = timezone(timedelta(hours=8))
CVD_NEUTRAL_ABS = 1.0
FLOW_CANDIDATE_STATE_SCHEMA_VERSION = 1
FLOW_MARKET_CAP_MAX_AGE_SEC = 15 * 60


def cst_now_text(fmt: str = "%m-%d %H:%M CST") -> str:
    return datetime.now(CST).strftime(fmt)


def tg_escape(value: Any) -> str:
    return escape(str(value), quote=False)


def tg_bold(value: Any) -> str:
    return f"<b>{tg_escape(value)}</b>"


def tg_quote(title: str) -> str:
    return f"<blockquote><b>{tg_escape(title)}</b></blockquote>"


def coinglass_tv_url(coin_or_symbol: str) -> str:
    return _coinglass_tv_url(coin_or_symbol)


def coin_link(symbol: str) -> str:
    return telegram_coin_links(symbol)


def compact_symbol_lines(symbols: list[str], *, per_line: int = 12) -> list[str]:
    size = max(1, int(per_line))
    return [
        " · ".join(tg_escape(symbol) for symbol in symbols[index:index + size])
        for index in range(0, len(symbols), size)
    ]


def fmt_market_cap(value: Any) -> str:
    market_cap = to_float(value)
    if market_cap >= 1_000_000_000_000:
        return f"${market_cap / 1_000_000_000_000:.1f}T"
    return fmt_money(market_cap)


def market_cap_candidate_lines(candidates: list[dict[str, Any]]) -> list[str]:
    ranked = sorted(
        (
            item
            for item in candidates
            if to_float(item.get("market_cap")) > 0
        ),
        key=lambda item: (
            -to_float(item.get("market_cap")),
            str(item.get("symbol") or ""),
        ),
    )
    missing = sorted(
        (
            item
            for item in candidates
            if to_float(item.get("market_cap")) <= 0
        ),
        key=lambda item: (
            int(item.get("priority_rank") or 0),
            str(item.get("symbol") or ""),
        ),
    )
    lines = [
        tg_quote("📋 全市场候选 · 市值排行"),
        (
            f"候选 {len(candidates)} | 市值覆盖 {len(ranked)} | "
            f"待补全 {len(missing)}"
        ),
        (
            "来源: 本地市场快照（15分钟内） | 排序: 流通市值降序"
            if ranked
            else "市值数据: 本地快照暂无可用值 | 缺失项不参与排名"
        ),
    ]
    ranked_rows = list(enumerate(ranked, start=1))
    tiers = (
        ("🏛️ ≥ $10B", 10_000_000_000, None),
        ("🔷 $1B–10B", 1_000_000_000, 10_000_000_000),
        ("🔹 $100M–1B", 100_000_000, 1_000_000_000),
        ("▪️ < $100M", 0, 100_000_000),
    )
    for title, minimum, maximum in tiers:
        tier_rows = [
            (rank, item)
            for rank, item in ranked_rows
            if to_float(item.get("market_cap")) >= minimum
            and (maximum is None or to_float(item.get("market_cap")) < maximum)
        ]
        if not tier_rows:
            continue
        lines.extend(["", f"{title}（{len(tier_rows)}）"])
        entries = [
            (
                f"{rank:03d} {tg_escape(item.get('symbol') or '')} "
                f"{fmt_market_cap(item.get('market_cap'))}"
            )
            for rank, item in tier_rows
        ]
        lines.extend(
            " · ".join(entries[index:index + 2])
            for index in range(0, len(entries), 2)
        )
    if missing:
        lines.extend([
            "",
            f"❔ 市值待补全（{len(missing)}）",
            "以下仅保留候选资格，不参与市值名次。",
            *compact_symbol_lines(
                [str(item.get("symbol") or "") for item in missing],
                per_line=4,
            ),
        ])
    return lines


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
        start_time = window.start_ms
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
        history = sorted(
            filter_points_by_time(history, start_time, end_time),
            key=point_timestamp_ms,
        )
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


def kline_cvd_flow_info(
    klines: list[list[Any]],
    window: ClosedWindow | None = None,
) -> tuple[float, float, float, bool, int]:
    taker_buy_total = 0.0
    taker_sell_total = 0.0
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
        if quote_volume < 0 or taker_buy_quote < 0 or taker_buy_quote > quote_volume:
            continue
        taker_buy_total += taker_buy_quote
        taker_sell_total += quote_volume - taker_buy_quote
        count += 1
    return taker_buy_total - taker_sell_total, taker_buy_total, taker_sell_total, count > 0, count


def kline_cvd_delta_info(klines: list[list[Any]], window: ClosedWindow | None = None) -> tuple[float, bool, int]:
    delta, _inflow, _outflow, ready, count = kline_cvd_flow_info(klines, window)
    return delta, ready, count


def binance_spot_cvd_stats(source: BinanceDataSource, symbol: str, window: ClosedWindow) -> tuple[float, bool, int]:
    delta, _inflow, _outflow, ready, count = binance_spot_flow_stats(source, symbol, window)
    return delta, ready, count


def binance_spot_flow_stats(
    source: BinanceDataSource,
    symbol: str,
    window: ClosedWindow,
) -> tuple[float, float, float, bool, int]:
    klines = source.spot_klines(
        symbol,
        interval="1h",
        limit=3,
        start_time=window.start_ms,
        end_time=window.end_ms - 1,
    )
    return kline_cvd_flow_info(klines, window)


def binance_futures_cvd_stats(source: BinanceDataSource, symbol: str, window: ClosedWindow) -> tuple[float, bool, int]:
    delta, _inflow, _outflow, ready, count = binance_futures_flow_stats(source, symbol, window)
    return delta, ready, count


def binance_futures_flow_stats(
    source: BinanceDataSource,
    symbol: str,
    window: ClosedWindow,
) -> tuple[float, float, float, bool, int]:
    klines = source.klines(
        symbol,
        interval="1h",
        limit=3,
        start_time=window.start_ms,
        end_time=window.end_ms - 1,
    )
    return kline_cvd_flow_info(klines, window)


def legacy_flow_category(item: dict[str, Any]) -> tuple[str, int, str]:
    if not item.get("price_ready", True) or not item.get("oi_ready", True):
        return ("数据不足", 0, "价格或 OI 未覆盖完整统计窗口，暂不评分")
    if not item.get("funding_ready", True):
        return ("数据不足", 0, "Binance 资金费率缺失，暂不评分")
    price = item["price_24h"]
    oi = to_float(item.get("oi_1h", item.get("oi_24h", 0.0)))
    spot = item["spot_cvd_delta"]
    futures = item["futures_cvd_delta"]
    funding = item["funding_pct"]
    spot_ready = bool(item.get("spot_cvd_ready", True))
    futures_ready = bool(item.get("futures_cvd_ready", True))
    if not spot_ready and not futures_ready:
        return ("数据不足", 0, "Binance 主动成交数据缺失，不能判断资金流")
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
    true_launch += 10 if item.get("funding_ready", True) and funding <= 0.08 else 0
    true_launch += 10 if item["quote_volume"] >= 50_000_000 else 0
    candidates.append(("真启动候选", true_launch, "现货主动买入跟随，OI同步增加，费率未过热"))

    accumulation = 0
    accumulation += 25 if abs(price) <= 5 else 0
    accumulation += 25 if oi >= 5 else 0
    accumulation += 25 if spot_positive else 0
    accumulation += 15 if item.get("funding_ready", True) and funding <= 0.03 else 0
    accumulation += 10 if futures_positive else 0
    candidates.append(("吸筹观察", accumulation, "价格未大幅启动但资金提前进入，适合提前盯盘"))

    short_fuel = 0
    short_fuel += 25 if item.get("funding_ready", True) and funding <= -0.03 else 0
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
    perp_pump += 10 if item.get("funding_ready", True) and funding >= 0 else 0
    candidates.append(("合约拉盘", perp_pump, "合约主动买入强于现货，追高风险更高"))

    short_squeeze = 0
    short_squeeze += 30 if price >= 5 else 0
    short_squeeze += 30 if oi <= -3 else 0
    short_squeeze += 20 if futures_positive else 0
    short_squeeze += 10 if price >= 5 and spot_negative else 0
    short_squeeze += 10 if item.get("funding_ready", True) and funding <= 0.05 else 0
    candidates.append(("挤空/止损", short_squeeze, "上涨伴随OI下降，可能是空头止损推动"))

    distribution = 0
    distribution += 25 if price >= 5 else 0
    distribution += 30 if price >= 5 and spot_negative else 0
    distribution += 20 if price >= 5 and futures_positive else 0
    distribution += 15 if price >= 5 and item.get("funding_ready", True) and funding >= 0.05 else 0
    distribution += 10 if price >= 5 and oi <= 0 else 0
    candidates.append(("诱多/派发", distribution, "价格上涨但现货主动买入不足，持续性存疑"))

    panic = 0
    panic += 25 if price <= -5 else 0
    panic += 25 if oi >= 5 else 0
    panic += 25 if spot_negative else 0
    panic += 15 if futures_negative else 0
    panic += 10 if item.get("funding_ready", True) and funding < 0 else 0
    candidates.append(("恐慌下跌", panic, "下跌增仓且主动卖出增强，空头压制或多头被套"))

    return max(candidates, key=lambda row: row[1])


def flow_net_ratio_pct(net: float, inflow: Any, outflow: Any) -> float:
    gross = to_float(inflow) + to_float(outflow)
    if gross <= 0:
        return 0.0
    return to_float(net) / gross * 100


def _flow_direction(
    *,
    net: float,
    ratio_pct: float,
    ready: bool,
    min_abs_usd: float,
    min_ratio_pct: float,
) -> int:
    if not ready:
        return 0
    if abs(net) < max(0.0, min_abs_usd):
        return 0
    if abs(ratio_pct) < max(0.0, min_ratio_pct):
        return 0
    return 1 if net > 0 else -1


def _bounded_strength(value: float, threshold: float, points: int) -> int:
    threshold = max(abs(threshold), 1e-9)
    return min(points, max(0, round(abs(value) / threshold * (points / 2))))


def flow_classification(
    item: dict[str, Any],
    settings: Settings | None = None,
) -> dict[str, Any]:
    settings = settings or Settings()
    model_version = "flow_p0_1"
    required = {
        "价格": bool(item.get("price_ready", True)),
        "oi": bool(item.get("oi_ready", True)),
        "Binance 现货主动成交": bool(item.get("spot_cvd_ready", True)),
        "Binance 合约主动成交": bool(item.get("futures_cvd_ready", True)),
        "Binance 资金费率": bool(item.get("funding_ready", True)),
    }
    missing = [name for name, ready in required.items() if not ready]
    if missing:
        if (
            "Binance 现货主动成交" in missing
            and "Binance 合约主动成交" in missing
        ):
            reason = "Binance 主动成交数据缺失；本轮不评分"
        elif "Binance 资金费率" in missing:
            reason = "Binance 资金费率缺失；本轮不评分"
        else:
            reason = f"核心数据缺失：{', '.join(missing)}；本轮不评分"
        return {
            "model_version": model_version,
            "category": "数据不足",
            "score": 0,
            "reason": reason,
            "eligible": False,
            "gates": required,
            "spot_net_ratio_pct": 0.0,
            "futures_net_ratio_pct": 0.0,
            "category_margin": 0,
            "alternatives": [],
        }

    price = to_float(item.get("price_24h"))
    oi = to_float(item.get("oi_1h", item.get("oi_24h")))
    spot_net = to_float(item.get("spot_cvd_delta"))
    futures_net = to_float(item.get("futures_cvd_delta"))
    funding = to_float(item.get("funding_pct"))
    quote_volume = abs(to_float(item.get("quote_volume")))
    spot_ratio = to_float(
        item.get(
            "spot_net_ratio_pct",
            flow_net_ratio_pct(
                spot_net,
                item.get("spot_inflow_usd"),
                item.get("spot_outflow_usd"),
            ),
        )
    )
    futures_ratio = to_float(
        item.get(
            "futures_net_ratio_pct",
            flow_net_ratio_pct(
                futures_net,
                item.get("futures_inflow_usd"),
                item.get("futures_outflow_usd"),
            ),
        )
    )
    spot_direction = _flow_direction(
        net=spot_net,
        ratio_pct=spot_ratio,
        ready=True,
        min_abs_usd=settings.flow_spot_net_min_usd,
        min_ratio_pct=settings.flow_spot_net_ratio_min_pct,
    )
    futures_direction = _flow_direction(
        net=futures_net,
        ratio_pct=futures_ratio,
        ready=True,
        min_abs_usd=settings.flow_futures_net_min_usd,
        min_ratio_pct=settings.flow_futures_net_ratio_min_pct,
    )
    move = max(0.01, settings.flow_price_move_min_pct)
    flat = max(move, settings.flow_price_flat_max_pct)
    oi_build = max(0.01, settings.flow_oi_build_min_pct)
    oi_unwind = min(-0.01, settings.flow_oi_unwind_max_pct)

    def score_candidate(
        *,
        include_spot: bool = True,
        include_futures: bool = True,
        funding_strength: float = 0.0,
        oi_value: float | None = None,
    ) -> int:
        score = 60
        score += _bounded_strength(price, move, 10)
        score += _bounded_strength(oi if oi_value is None else oi_value, oi_build, 10)
        if include_spot:
            score += _bounded_strength(
                spot_ratio,
                settings.flow_spot_net_ratio_min_pct,
                8,
            )
        if include_futures:
            score += _bounded_strength(
                futures_ratio,
                settings.flow_futures_net_ratio_min_pct,
                8,
            )
        score += min(6, max(0, round(funding_strength)))
        if quote_volume >= 100_000_000:
            score += 4
        elif quote_volume >= 30_000_000:
            score += 2
        return min(100, score)

    candidates: list[dict[str, Any]] = []

    def add(
        category: str,
        passed: bool,
        reason: str,
        *,
        include_spot: bool = True,
        include_futures: bool = True,
        funding_strength: float = 0.0,
        oi_value: float | None = None,
    ) -> None:
        if passed:
            candidates.append({
                "category": category,
                "score": score_candidate(
                    include_spot=include_spot,
                    include_futures=include_futures,
                    funding_strength=funding_strength,
                    oi_value=oi_value,
                ),
                "reason": reason,
            })

    add(
        "真启动候选",
        price >= move
        and oi >= oi_build
        and spot_direction > 0
        and futures_direction > 0
        and funding <= 0.05,
        "价格与OI同步上升，现货和合约主动买入均通过净额与净占比门槛，费率未过热",
        funding_strength=max(0.0, (0.05 - funding) / 0.01),
    )
    add(
        "吸筹观察",
        abs(price) <= flat
        and oi >= oi_build
        and spot_direction > 0
        and futures_direction >= 0
        and funding <= 0.03,
        "价格仍在窄幅区间，OI增加且现货主动买入通过双门槛，合约未出现显著主动卖出",
        include_futures=futures_direction != 0,
        funding_strength=max(0.0, (0.03 - funding) / 0.01),
    )
    add(
        "空头燃料",
        funding <= -0.03
        and oi >= oi_build
        and price > -flat
        and futures_direction < 0,
        "负费率、增仓和合约主动卖出同时成立，但价格尚未明显下跌，属于潜在挤空燃料",
        include_spot=spot_direction != 0,
        funding_strength=abs(funding) / 0.03 * 2,
    )
    add(
        "合约拉盘",
        price >= move
        and oi >= oi_build
        and futures_direction > 0
        and spot_direction <= 0
        and funding >= 0,
        "价格与OI上涨主要由合约主动买入推动，现货买盘未通过门槛，持续性需要谨慎",
        include_spot=spot_direction != 0,
        funding_strength=funding / 0.02,
    )
    add(
        "挤空/止损",
        price >= move
        and oi <= oi_unwind
        and futures_direction > 0
        and funding <= 0.05,
        "价格上涨而OI明显下降，合约主动买入增强，更接近空头止损或回补推动",
        include_spot=spot_direction != 0,
        funding_strength=max(0.0, (0.05 - funding) / 0.01),
        oi_value=abs(oi),
    )
    add(
        "诱多/派发",
        price >= move
        and spot_direction < 0
        and (futures_direction > 0 or funding >= 0.03)
        and oi < oi_build,
        "价格上涨但现货主动卖出占优，合约买盘或正费率托住价格，存在诱多或派发风险",
        funding_strength=max(0.0, funding / 0.02),
    )
    add(
        "恐慌下跌",
        price <= -move
        and oi >= oi_build
        and spot_direction < 0
        and futures_direction < 0,
        "价格下跌、OI增加，现货与合约主动卖出均通过双门槛，属于增仓下跌风险",
        funding_strength=max(0.0, abs(min(funding, 0.0)) / 0.02),
    )

    candidates.sort(key=lambda row: int(row["score"]), reverse=True)
    if not candidates:
        return {
            "model_version": model_version,
            "category": "观察",
            "score": 0,
            "reason": (
                f"未通过任一完整核心门禁；现货净占比 {spot_ratio:+.2f}%，"
                f"合约净占比 {futures_ratio:+.2f}%"
            ),
            "eligible": False,
            "gates": required,
            "spot_net_ratio_pct": spot_ratio,
            "futures_net_ratio_pct": futures_ratio,
            "category_margin": 0,
            "alternatives": [],
        }
    best = candidates[0]
    margin = int(best["score"]) - (
        int(candidates[1]["score"]) if len(candidates) > 1 else 0
    )
    return {
        "model_version": model_version,
        "category": str(best["category"]),
        "score": int(best["score"]),
        "reason": (
            f"{best['reason']}；现货净占比 {spot_ratio:+.2f}%，"
            f"合约净占比 {futures_ratio:+.2f}%"
        ),
        "eligible": True,
        "gates": required,
        "spot_net_ratio_pct": spot_ratio,
        "futures_net_ratio_pct": futures_ratio,
        "category_margin": margin,
        "alternatives": [
            {"category": row["category"], "score": row["score"]}
            for row in candidates[1:3]
        ],
    }


def flow_category(
    item: dict[str, Any],
    settings: Settings | None = None,
) -> tuple[str, int, str]:
    classification = flow_classification(item, settings)
    return (
        str(classification["category"]),
        int(classification["score"]),
        str(classification["reason"]),
    )


class FlowRadarEngine:
    def __init__(self, settings: Settings, store: JsonStore | None = None):
        self.settings = settings
        self.store = store or JsonStore(settings.data_dir)

    def build(self, binance: BinanceDataSource) -> dict[str, Any]:
        window = closed_window(
            interval_sec=self.settings.flow_interval_sec,
            delay_sec=self.settings.flow_close_delay_sec,
        )
        candidates = self._candidate_symbols(binance)
        market_cap_status = self._enrich_cached_market_caps(candidates)
        rotation_candidates, rotation_state = self._rotation_candidates(candidates)
        rows: list[dict[str, Any]] = []
        scanned_items: list[dict[str, Any]] = []
        for candidate in rotation_candidates:
            symbol = candidate["symbol"]
            coin = candidate["coin"]
            spot_cvd, spot_inflow, spot_outflow, spot_cvd_ready, spot_cvd_points = binance_spot_flow_stats(binance, symbol, window)
            futures_cvd, futures_inflow, futures_outflow, futures_cvd_ready, futures_cvd_points = binance_futures_flow_stats(binance, symbol, window)
            price_pct, price_ready = binance_window_price_pct(binance, symbol, window)
            oi_1h, oi_fallback_usd, oi_ready, oi_points = binance_oi_stats(
                binance,
                symbol,
                window=window,
                period="1h",
                limit=4,
            )
            funding_pct = to_float(candidate.get("funding_pct", 0.0))
            quote_volume = to_float(candidate["quote_volume"])
            oi_usd = oi_fallback_usd
            spot_ratio_pct = flow_net_ratio_pct(
                spot_cvd,
                spot_inflow,
                spot_outflow,
            )
            futures_ratio_pct = flow_net_ratio_pct(
                futures_cvd,
                futures_inflow,
                futures_outflow,
            )
            item = {
                "symbol": symbol,
                "coin": coin,
                "price": candidate.get("price"),
                "price_24h": price_pct,
                "price_ready": price_ready,
                "oi_1h": oi_1h,
                # Compatibility alias for persisted snapshots created before P1.
                "oi_24h": oi_1h,
                "oi_change_pct": oi_1h,
                "oi_ready": oi_ready,
                "oi_points": oi_points,
                "spot_cvd_delta": spot_cvd,
                "spot_inflow_usd": spot_inflow if spot_cvd_ready else None,
                "spot_outflow_usd": spot_outflow if spot_cvd_ready else None,
                "spot_net_ratio_pct": spot_ratio_pct,
                "futures_cvd_delta": futures_cvd,
                "futures_inflow_usd": futures_inflow if futures_cvd_ready else None,
                "futures_outflow_usd": futures_outflow if futures_cvd_ready else None,
                "futures_net_ratio_pct": futures_ratio_pct,
                "spot_cvd_ready": spot_cvd_ready,
                "futures_cvd_ready": futures_cvd_ready,
                "spot_cvd_points": spot_cvd_points,
                "futures_cvd_points": futures_cvd_points,
                "funding_pct": funding_pct,
                "funding_ready": bool(candidate.get("funding_ready")),
                "quote_volume": abs(quote_volume),
                "oi_usd": oi_usd,
            }
            legacy_category, legacy_score, legacy_reason = legacy_flow_category(item)
            classification = flow_classification(item, self.settings)
            item.update({
                "category": classification["category"],
                "score": classification["score"],
                "reason": classification["reason"],
                "flow_model_version": classification["model_version"],
                "flow_model_eligible": classification["eligible"],
                "flow_core_gates": classification["gates"],
                "category_margin": classification["category_margin"],
                "category_alternatives": classification["alternatives"],
                "legacy_category": legacy_category,
                "legacy_score": legacy_score,
                "legacy_reason": legacy_reason,
            })
            scanned_items.append(item)

        for item in scanned_items:
            apply_binance_confirmation(
                item,
                {
                    "价格K线": bool(item.get("price_ready")),
                    "OI": bool(item.get("oi_ready")) and int(item.get("oi_points") or 0) >= 2,
                    "现货主动成交": bool(item.get("spot_cvd_ready")),
                    "合约主动成交": bool(item.get("futures_cvd_ready")),
                    "资金费率": bool(item.get("funding_ready")),
                },
                scope="Binance Spot + USDⓈ-M Futures",
                window="1h闭合窗口",
                observed_at=int(window.end.timestamp()),
            )
            if (
                item.get("flow_model_eligible")
                and
                item["score"] >= self.settings.flow_min_score
                and item.get("quality_gate") == "allow"
            ):
                rows.append(item)

        rows.sort(key=lambda item: item["score"], reverse=True)
        rows = rows[: max(1, self.settings.flow_top_n)]
        comparison_status = self._record_model_comparison(scanned_items, window)
        rotation_status = self._save_candidate_state(
            candidates,
            rotation_state,
            selected_symbols={str(item["symbol"]) for item in rotation_candidates},
            observed_at=int(window.end.timestamp()),
        )
        return {
            "template_id": "TG_FLOW_RADAR",
            "dedup_key": f"flow-radar:{window.end.strftime('%Y%m%d%H%M')}",
            "text": self._format(
                rows,
                candidates,
                scanned_items,
                window,
                rotation_status=rotation_status,
            ),
            "items": rows,
            "snapshots": scanned_items,
            "observed_at": int(window.end.timestamp()),
            "window_sec": int(window.interval_sec),
            "diagnostics": {
                "binance": binance.diagnostics(),
                "binance_confirmation": confirmation_summary(scanned_items),
                "flow_model_comparison": comparison_status,
                "candidate_rotation": rotation_status,
                "market_cap_ranking": market_cap_status,
            },
        }

    def _enrich_cached_market_caps(
        self,
        candidates: list[dict[str, Any]],
        *,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        symbols = [
            str(item.get("symbol") or "")
            for item in candidates
            if str(item.get("symbol") or "")
        ]
        if not symbols:
            return {
                "status": "empty",
                "known_count": 0,
                "missing_count": 0,
                "network_calls": 0,
            }
        path = self.settings.market_snapshots_db_path
        if not path.exists():
            return {
                "status": "not_available",
                "known_count": 0,
                "missing_count": len(symbols),
                "network_calls": 0,
            }
        observed_after = int(now_ts or time.time()) - FLOW_MARKET_CAP_MAX_AGE_SEC
        placeholders = ",".join("?" for _symbol in symbols)
        conn: sqlite3.Connection | None = None
        try:
            uri = f"{path.resolve().as_uri()}?mode=ro"
            conn = sqlite3.connect(uri, uri=True, timeout=0.2)
            conn.execute("PRAGMA query_only = ON")
            rows = conn.execute(
                f"""
                SELECT symbol, market_cap, observed_at
                FROM market_snapshots
                WHERE symbol IN ({placeholders})
                  AND market_cap > 0
                  AND observed_at >= ?
                ORDER BY symbol, observed_at DESC
                """,
                (*symbols, observed_after),
            ).fetchall()
        except (OSError, sqlite3.Error):
            return {
                "status": "local_error",
                "error": "market_cap_snapshot_read_failed",
                "known_count": 0,
                "missing_count": len(symbols),
                "network_calls": 0,
            }
        finally:
            if conn is not None:
                conn.close()
        latest: dict[str, tuple[float, int]] = {}
        for symbol, market_cap, observed_at in rows:
            normalized = str(symbol or "")
            value = to_float(market_cap)
            if normalized not in latest and value > 0:
                latest[normalized] = (value, int(observed_at or 0))
        for candidate in candidates:
            snapshot = latest.get(str(candidate.get("symbol") or ""))
            if snapshot is None:
                candidate["market_cap"] = None
                candidate["market_cap_source"] = ""
                candidate["market_cap_observed_at"] = None
                continue
            candidate["market_cap"] = snapshot[0]
            candidate["market_cap_source"] = "local_market_snapshot"
            candidate["market_cap_observed_at"] = snapshot[1]
        known_count = len(latest)
        return {
            "status": "ok" if known_count == len(symbols) else "partial",
            "source": "local_market_snapshot",
            "max_age_sec": FLOW_MARKET_CAP_MAX_AGE_SEC,
            "known_count": known_count,
            "missing_count": len(symbols) - known_count,
            "network_calls": 0,
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
                "price": to_float(item.get("lastPrice")),
                "price_24h": price_24h,
                "quote_volume": quote_volume,
                "funding_pct": premium_map.get(symbol, 0.0),
                "funding_ready": symbol in premium_map,
            })
        liquidity = sorted(
            candidates,
            key=lambda item: item["quote_volume"],
            reverse=True,
        )
        movers = sorted(
            candidates,
            key=lambda item: abs(item["price_24h"]),
            reverse=True,
        )
        funding_extremes = sorted(
            (item for item in candidates if item.get("funding_ready")),
            key=lambda item: abs(item["funding_pct"]),
            reverse=True,
        )
        selected: list[dict[str, Any]] = []
        selected_symbols: set[str] = set()
        reason_map: dict[str, set[str]] = {}
        rankings = (
            ("liquidity", liquidity),
            ("price_mover", movers),
            ("funding_extreme", funding_extremes),
        )
        rank = 0
        while len(selected) < len(candidates) and any(rank < len(rows) for _, rows in rankings):
            for reason, ranked in rankings:
                if rank >= len(ranked):
                    continue
                candidate = ranked[rank]
                symbol = str(candidate["symbol"])
                reason_map.setdefault(symbol, set()).add(reason)
                if symbol not in selected_symbols:
                    selected.append(candidate)
                    selected_symbols.add(symbol)
                    if len(selected) >= len(candidates):
                        break
            rank += 1
        for priority_rank, candidate in enumerate(selected, start=1):
            candidate["selection_reasons"] = sorted(
                reason_map.get(str(candidate["symbol"]), set())
            )
            candidate["priority_rank"] = priority_rank
        return selected

    def _load_candidate_state(self) -> dict[str, Any]:
        state = self.store.load(self.settings.flow_candidate_state_path, {})
        if not isinstance(state, dict):
            return {}
        if state.get("schema_version") != FLOW_CANDIDATE_STATE_SCHEMA_VERSION:
            return {}
        if not isinstance(state.get("candidates"), list):
            return {}
        return state

    @staticmethod
    def _candidate_history(state: dict[str, Any]) -> dict[str, dict[str, int]]:
        history: dict[str, dict[str, int]] = {}
        for item in state.get("candidates", []):
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "")
            if not symbol:
                continue
            history[symbol] = {
                "scan_count": max(0, int(item.get("scan_count") or 0)),
                "last_scanned_at": max(0, int(item.get("last_scanned_at") or 0)),
            }
        return history

    def _rotation_candidates(
        self,
        candidates: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        state = self._load_candidate_state()
        history = self._candidate_history(state)
        ordered = sorted(
            candidates,
            key=lambda item: (
                history.get(str(item["symbol"]), {}).get("scan_count", 0),
                history.get(str(item["symbol"]), {}).get("last_scanned_at", 0),
                int(item.get("priority_rank") or 0),
                str(item["symbol"]),
            ),
        )
        return ordered[: max(1, self.settings.flow_scan_limit)], state

    def _save_candidate_state(
        self,
        candidates: list[dict[str, Any]],
        previous_state: dict[str, Any],
        *,
        selected_symbols: set[str],
        observed_at: int,
    ) -> dict[str, Any]:
        history = self._candidate_history(previous_state)
        entries: list[dict[str, Any]] = []
        for candidate in candidates:
            symbol = str(candidate["symbol"])
            prior = history.get(symbol, {})
            scan_count = max(0, int(prior.get("scan_count") or 0))
            last_scanned_at = max(0, int(prior.get("last_scanned_at") or 0))
            selected = symbol in selected_symbols
            if selected:
                scan_count += 1
                last_scanned_at = observed_at
            entries.append({
                "priority_rank": int(candidate.get("priority_rank") or 0),
                "symbol": symbol,
                "coin": str(candidate.get("coin") or ""),
                "price": candidate.get("price"),
                "price_24h": to_float(candidate.get("price_24h")),
                "quote_volume": to_float(candidate.get("quote_volume")),
                "market_cap": (
                    to_float(candidate.get("market_cap"))
                    if to_float(candidate.get("market_cap")) > 0
                    else None
                ),
                "market_cap_source": str(candidate.get("market_cap_source") or ""),
                "market_cap_observed_at": candidate.get("market_cap_observed_at"),
                "funding_pct": to_float(candidate.get("funding_pct")),
                "funding_ready": bool(candidate.get("funding_ready")),
                "selection_reasons": list(candidate.get("selection_reasons") or []),
                "scan_count": scan_count,
                "last_scanned_at": last_scanned_at or None,
                "selected_this_cycle": selected,
            })
        next_order = sorted(
            entries,
            key=lambda item: (
                int(item["scan_count"]),
                int(item.get("last_scanned_at") or 0),
                int(item["priority_rank"]),
                str(item["symbol"]),
            ),
        )
        next_rank = {
            str(item["symbol"]): rank
            for rank, item in enumerate(next_order, start=1)
        }
        for entry in entries:
            entry["next_rotation_rank"] = next_rank[str(entry["symbol"])]
        payload = {
            "schema_version": FLOW_CANDIDATE_STATE_SCHEMA_VERSION,
            "updated_at": observed_at,
            "pool_mode": "unlimited",
            "total_candidates": len(entries),
            "scan_limit": max(1, self.settings.flow_scan_limit),
            "selected_count": len(selected_symbols),
            "unscanned_count": sum(1 for item in entries if int(item["scan_count"]) == 0),
            "candidates": entries,
        }
        try:
            self.store.save(self.settings.flow_candidate_state_path, payload)
        except Exception:
            return {
                "status": "local_error",
                "error": "flow_candidate_state_write_failed",
                "pool_mode": "unlimited",
                "total_candidates": len(entries),
                "selected_count": len(selected_symbols),
                "next_symbols": [
                    str(item["symbol"])
                    for item in next_order[: max(1, self.settings.flow_scan_limit)]
                ],
            }
        return {
            "status": "ok",
            "pool_mode": "unlimited",
            "total_candidates": len(entries),
            "scan_limit": max(1, self.settings.flow_scan_limit),
            "selected_count": len(selected_symbols),
            "unscanned_count": payload["unscanned_count"],
            "next_symbols": [
                str(item["symbol"])
                for item in next_order[: max(1, self.settings.flow_scan_limit)]
            ],
        }

    def _record_model_comparison(
        self,
        items: list[dict[str, Any]],
        window: ClosedWindow,
    ) -> dict[str, Any]:
        if not self.settings.flow_model_comparison_enable:
            return {"status": "disabled"}
        legacy_eligible = [
            item for item in items
            if int(item.get("legacy_score") or 0) >= 50
            and item.get("quality_gate") == "allow"
        ]
        p0_eligible = [
            item for item in items
            if bool(item.get("flow_model_eligible"))
            and int(item.get("score") or 0) >= self.settings.flow_min_score
            and item.get("quality_gate") == "allow"
        ]
        changed_count = sum(
            1 for item in items
            if item.get("legacy_category") != item.get("category")
        )
        legacy_suppressed_count = sum(
            1 for item in legacy_eligible
            if item not in p0_eligible
        )
        record = {
            "schema_version": 1,
            "model_version": "flow_p0_1",
            "observed_at": int(window.end.timestamp()),
            "window_start": int(window.start.timestamp()),
            "window_end": int(window.end.timestamp()),
            "legacy_eligible_count": len(legacy_eligible),
            "p0_eligible_count": len(p0_eligible),
            "category_changed_count": changed_count,
            "legacy_suppressed_count": legacy_suppressed_count,
            "items": [
                {
                    "symbol": item.get("symbol"),
                    "price_1h_pct": item.get("price_24h"),
                    "oi_1h_pct": item.get("oi_1h"),
                    "spot_net_usd": item.get("spot_cvd_delta"),
                    "spot_net_ratio_pct": item.get("spot_net_ratio_pct"),
                    "futures_net_usd": item.get("futures_cvd_delta"),
                    "futures_net_ratio_pct": item.get("futures_net_ratio_pct"),
                    "funding_pct": item.get("funding_pct"),
                    "legacy_category": item.get("legacy_category"),
                    "legacy_score": item.get("legacy_score"),
                    "p0_category": item.get("category"),
                    "p0_score": item.get("score"),
                    "p0_eligible": item.get("flow_model_eligible"),
                    "quality_gate": item.get("quality_gate"),
                    "category_margin": item.get("category_margin"),
                }
                for item in items
            ],
        }
        try:
            self.store.append_record(
                self.settings.flow_model_comparison_path,
                record,
                limit=max(1, self.settings.flow_model_comparison_history_limit),
            )
        except Exception as exc:
            return {
                "status": "failed",
                "error_type": type(exc).__name__,
            }
        return {
            "status": "recorded",
            "path": str(self.settings.flow_model_comparison_path),
            "legacy_eligible_count": len(legacy_eligible),
            "p0_eligible_count": len(p0_eligible),
            "category_changed_count": changed_count,
            "legacy_suppressed_count": legacy_suppressed_count,
        }

    def _format(
        self,
        rows: list[dict[str, Any]],
        candidates: list[dict[str, Any]],
        scanned_items: list[dict[str, Any]],
        window: ClosedWindow,
        rotation_status: dict[str, Any] | None = None,
    ) -> str:
        rotation = rotation_status or {}
        spot_ready_count = sum(1 for item in scanned_items if item.get("spot_cvd_ready"))
        futures_ready_count = sum(1 for item in scanned_items if item.get("futures_cvd_ready"))
        price_ready_count = sum(1 for item in scanned_items if item.get("price_ready"))
        oi_ready_count = sum(1 for item in scanned_items if item.get("oi_ready"))
        confirmed_count = sum(
            1 for item in scanned_items
            if item.get("data_quality_status") == "confirmed"
        )
        spot_active_count = sum(
            1 for item in scanned_items
            if _flow_direction(
                net=to_float(item.get("spot_cvd_delta")),
                ratio_pct=to_float(item.get("spot_net_ratio_pct")),
                ready=bool(item.get("spot_cvd_ready")),
                min_abs_usd=self.settings.flow_spot_net_min_usd,
                min_ratio_pct=self.settings.flow_spot_net_ratio_min_pct,
            )
        )
        futures_active_count = sum(
            1 for item in scanned_items
            if _flow_direction(
                net=to_float(item.get("futures_cvd_delta")),
                ratio_pct=to_float(item.get("futures_net_ratio_pct")),
                ready=bool(item.get("futures_cvd_ready")),
                min_abs_usd=self.settings.flow_futures_net_min_usd,
                min_ratio_pct=self.settings.flow_futures_net_ratio_min_pct,
            )
        )
        scanned_count = len(scanned_items)
        remaining_first_coverage = (
            int(rotation["unscanned_count"])
            if "unscanned_count" in rotation
            else max(0, len(candidates) - scanned_count)
        )
        lines = [
            "🧭 <b>五因子资金流雷达</b>",
            f"⏰ {cst_now_text()}",
            f"统计窗口: {window.label()}",
            "",
            tg_quote("📊 本轮统计"),
            f"全市场候选: {len(candidates)}（无固定数量上限）",
            f"本轮优先轮换: {scanned_count}/{len(candidates)}",
            f"首次覆盖待轮换: {remaining_first_coverage}",
            f"入选信号: {len(rows)}",
            f"数据确认: 完整 {confirmed_count}/{scanned_count} | 缺项 {scanned_count - confirmed_count}/{scanned_count}",
            f"窗口数据: 价格 {price_ready_count}/{scanned_count} | OI {oi_ready_count}/{scanned_count}",
            f"主动成交: 现货双门槛有效 {spot_active_count}/{scanned_count}，可读 {spot_ready_count}/{scanned_count} | 合约双门槛有效 {futures_active_count}/{scanned_count}，可读 {futures_ready_count}/{scanned_count}",
            "",
        ]
        if scanned_count and (price_ready_count < scanned_count or oi_ready_count < scanned_count):
            lines.extend([
                "⚠️ 部分价格/OI 未覆盖完整统计窗口；这些币不会进入资金流评分。",
                "",
            ])
        if scanned_count and (spot_ready_count < scanned_count or futures_ready_count < scanned_count):
            lines.extend([
                "⚠️ 部分主动成交数据缺失；缺失项不会按 0 参与资金流评分。",
                "",
            ])
        if scanned_count and (spot_active_count < spot_ready_count or futures_active_count < futures_ready_count):
            lines.extend([
                "ℹ️ 主动成交必须同时通过绝对净额和主动净占比门槛；未通过只按中性状态处理。",
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
                    f"OI 1h{pct_cell(item['oi_1h'])} | "
                    f"现货 {fmt_cvd(item['spot_cvd_delta'], bool(item.get('spot_cvd_ready')))}"
                    f"/{to_float(item.get('spot_net_ratio_pct')):+.1f}% | "
                    f"合约 {fmt_cvd(item['futures_cvd_delta'], bool(item.get('futures_cvd_ready')))}"
                    f"/{to_float(item.get('futures_net_ratio_pct')):+.1f}% | "
                    f"费率 {item['funding_pct']:+.3f}%"
                )
                lines.append(f"判断: {tg_escape(item['reason'])}")
                lines.append(f"数据确认: ✅ {tg_escape(confirmation_text(item))}")
            lines.append("")
        if not rows:
            lines.extend([
                "暂无达标信号",
                "如果主动成交数据长期缺失，通常是币种没有对应 Binance 现货交易对、接口限频或窗口数据尚未完整。",
                "",
            ])
        current_symbols = [str(item.get("symbol") or "") for item in scanned_items]
        next_symbols = [
            str(symbol)
            for symbol in rotation.get("next_symbols") or []
            if str(symbol)
        ]
        lines.extend([
            tg_quote("🔄 候选轮换"),
            f"本轮深度扫描（{len(current_symbols)}）",
            *compact_symbol_lines(current_symbols, per_line=6),
            "",
            f"下一轮优先队列（{len(next_symbols)}）",
            *compact_symbol_lines(next_symbols, per_line=6),
            "",
            *market_cap_candidate_lines(candidates),
            "",
            "说明：市值排名不改变候选资格和轮换顺序；只有本轮 24 个完成五因子深度扫描。",
        ])
        return "\n".join(lines)
