from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


INTERVAL_MS = 15 * 60 * 1000
REQUIRED_POINTS = 17
OI_24H_REQUIRED_POINTS = 97
MARKET_FACTS_VERSION = 2

ERROR_INPUT_INVALID = "launch_market_facts_input_invalid"
ERROR_KLINE_MALFORMED = "launch_market_facts_kline_malformed"
ERROR_OI_MALFORMED = "launch_market_facts_oi_malformed"
ERROR_KLINE_DUPLICATE = "launch_market_facts_kline_duplicate"
ERROR_OI_DUPLICATE = "launch_market_facts_oi_duplicate"
ERROR_KLINE_GAP = "launch_market_facts_kline_gap"
ERROR_OI_GAP = "launch_market_facts_oi_gap"
ERROR_BOUNDARY_MISMATCH = "launch_market_facts_boundary_mismatch"
ERROR_SERIES_MISALIGNED = "launch_market_facts_series_misaligned"
ERROR_INSUFFICIENT_HISTORY = "launch_market_facts_insufficient_history"


class LaunchMarketFactsError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ClosedKline:
    period_end_ms: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


@dataclass(frozen=True)
class OpenInterestPoint:
    period_end_ms: int
    value_usd: float


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    number = _finite_number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _validate_window_end(window_end_ms: int) -> int:
    boundary = _integer(window_end_ms)
    if boundary is None or boundary <= 0 or boundary % INTERVAL_MS != 0:
        raise LaunchMarketFactsError(ERROR_INPUT_INVALID)
    return boundary


def normalize_binance_15m_klines(
    rows: Sequence[Sequence[Any]],
    *,
    window_end_ms: int,
) -> list[ClosedKline]:
    boundary = _validate_window_end(window_end_ms)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise LaunchMarketFactsError(ERROR_KLINE_MALFORMED)

    normalized: dict[int, ClosedKline] = {}
    for row in rows:
        if (
            not isinstance(row, Sequence)
            or isinstance(row, (str, bytes))
            or len(row) < 8
        ):
            raise LaunchMarketFactsError(ERROR_KLINE_MALFORMED)
        open_time_ms = _integer(row[0])
        close_time_ms = _integer(row[6])
        open_price = _finite_number(row[1])
        high = _finite_number(row[2])
        low = _finite_number(row[3])
        close = _finite_number(row[4])
        quote_volume = _finite_number(row[7])
        if (
            open_time_ms is None
            or close_time_ms is None
            or open_time_ms < 0
            or open_time_ms % INTERVAL_MS != 0
            or close_time_ms != open_time_ms + INTERVAL_MS - 1
            or open_price is None
            or high is None
            or low is None
            or close is None
            or quote_volume is None
            or min(open_price, high, low, close) <= 0
            or high < max(open_price, close)
            or low > min(open_price, close)
            or quote_volume < 0
        ):
            raise LaunchMarketFactsError(ERROR_KLINE_MALFORMED)
        period_end_ms = close_time_ms + 1
        if period_end_ms > boundary:
            raise LaunchMarketFactsError(ERROR_BOUNDARY_MISMATCH)
        if period_end_ms in normalized:
            raise LaunchMarketFactsError(ERROR_KLINE_DUPLICATE)
        normalized[period_end_ms] = ClosedKline(
            period_end_ms=period_end_ms,
            open=open_price,
            high=high,
            low=low,
            close=close,
            quote_volume=quote_volume,
        )
    return [normalized[key] for key in sorted(normalized)]


def normalize_binance_15m_open_interest(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_end_ms: int,
) -> list[OpenInterestPoint]:
    boundary = _validate_window_end(window_end_ms)
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        raise LaunchMarketFactsError(ERROR_OI_MALFORMED)

    normalized: dict[int, OpenInterestPoint] = {}
    for row in rows:
        if not isinstance(row, Mapping):
            raise LaunchMarketFactsError(ERROR_OI_MALFORMED)
        period_end_ms = _integer(row.get("timestamp"))
        value_usd = _finite_number(row.get("sumOpenInterestValue"))
        if (
            period_end_ms is None
            or period_end_ms <= 0
            or period_end_ms % INTERVAL_MS != 0
            or period_end_ms > boundary
            or value_usd is None
            or value_usd <= 0
        ):
            raise LaunchMarketFactsError(ERROR_OI_MALFORMED)
        if period_end_ms in normalized:
            raise LaunchMarketFactsError(ERROR_OI_DUPLICATE)
        normalized[period_end_ms] = OpenInterestPoint(
            period_end_ms=period_end_ms,
            value_usd=value_usd,
        )
    return [normalized[key] for key in sorted(normalized)]


def _validate_continuous_boundaries(
    boundaries: Sequence[int],
    *,
    window_end_ms: int,
    gap_error: str,
) -> None:
    if len(boundaries) < REQUIRED_POINTS:
        raise LaunchMarketFactsError(ERROR_INSUFFICIENT_HISTORY)
    recent = list(boundaries[-REQUIRED_POINTS:])
    if recent[-1] != window_end_ms:
        raise LaunchMarketFactsError(ERROR_BOUNDARY_MISMATCH)
    if any(
        recent[index] - recent[index - 1] != INTERVAL_MS
        for index in range(1, len(recent))
    ):
        raise LaunchMarketFactsError(gap_error)


def _pct(current: float, previous: float) -> float | None:
    if previous <= 0:
        return None
    return (current / previous - 1.0) * 100.0


def _realized_volatility(closes: Sequence[float]) -> float | None:
    changes = [
        change
        for current, previous in zip(closes[1:], closes[:-1])
        if (change := _pct(current, previous)) is not None
    ]
    if not changes:
        return None
    return math.sqrt(sum(change * change for change in changes) / len(changes))


def price_oi_quadrant(
    price_change_pct: float | None,
    oi_change_pct: float | None,
) -> dict[str, Any]:
    if price_change_pct is None or oi_change_pct is None:
        return {
            "key": "insufficient_data",
            "label": "数据不足",
            "meaning": "价格或持仓数据缺失，不能判断资金方向。",
            "counter_evidence": False,
        }
    if price_change_pct > 0 and oi_change_pct > 0:
        return {
            "key": "price_up_oi_up",
            "label": "价格上涨、持仓增加",
            "meaning": "新增仓位与上涨同步，方向偏多，但不代表一定延续。",
            "counter_evidence": False,
        }
    if price_change_pct > 0 and oi_change_pct < 0:
        return {
            "key": "price_up_oi_down",
            "label": "价格上涨、持仓减少",
            "meaning": "可能以空头回补或去杠杆为主，属于追涨持续性的反证。",
            "counter_evidence": True,
        }
    if price_change_pct < 0 and oi_change_pct > 0:
        return {
            "key": "price_down_oi_up",
            "label": "价格下跌、持仓增加",
            "meaning": "新增仓位与下跌同步，方向偏空，属于上涨型启动的反证。",
            "counter_evidence": True,
        }
    if price_change_pct < 0 and oi_change_pct < 0:
        return {
            "key": "price_down_oi_down",
            "label": "价格下跌、持仓减少",
            "meaning": "可能以多头止损或去杠杆为主，属于趋势持续性的反证。",
            "counter_evidence": True,
        }
    return {
        "key": "neutral",
        "label": "价格或持仓基本不变",
        "meaning": "当前方向证据不足，需要等待下一完整窗口。",
        "counter_evidence": False,
    }


def _empty_result(error: str) -> dict[str, Any]:
    return {
        "version": MARKET_FACTS_VERSION,
        "status": "invalid",
        "error": error,
        "window_end_ms": None,
        "aligned_points": 0,
        "closed_price": None,
        "closed_oi_usd": None,
        "closed_quote_volume": None,
        "price_15m_pct": None,
        "price_1h_pct": None,
        "price_4h_pct": None,
        "oi_15m_pct": None,
        "oi_1h_pct": None,
        "oi_4h_pct": None,
        "oi_24h_closed_pct": None,
        "oi_24h_status": "core_invalid",
        "oi_24h_points": 0,
        "oi_24h_semantics": "closed_15m_boundaries_24h",
        "volume_ratio_15m": None,
        "recent_volatility_pct": None,
        "price_24h_rolling_pct": None,
        "price_24h_semantics": "rolling_24h_not_closed_window",
        "quadrants": {},
    }


def _optional_ticker_change(ticker_24h: Mapping[str, Any] | None) -> float | None:
    if not isinstance(ticker_24h, Mapping):
        return None
    return _finite_number(ticker_24h.get("priceChangePercent"))


def closed_24h_open_interest_change(
    rows: Sequence[Mapping[str, Any]],
    *,
    window_end_ms: int,
) -> dict[str, Any]:
    """Compute a true closed-window 24h OI change from 97 aligned 15m points."""

    result: dict[str, Any] = {
        "value_pct": None,
        "status": "insufficient_history",
        "points": 0,
        "semantics": "closed_15m_boundaries_24h",
    }
    try:
        boundary = _validate_window_end(window_end_ms)
        normalized = normalize_binance_15m_open_interest(
            rows,
            window_end_ms=boundary,
        )
    except LaunchMarketFactsError as exc:
        result["status"] = (
            "boundary_missing"
            if exc.code == ERROR_BOUNDARY_MISMATCH
            else "invalid"
        )
        return result

    result["points"] = len(normalized)
    if len(normalized) < OI_24H_REQUIRED_POINTS:
        return result
    recent = normalized[-OI_24H_REQUIRED_POINTS:]
    boundaries = [row.period_end_ms for row in recent]
    if boundaries[-1] != boundary:
        result["status"] = "boundary_missing"
        return result
    if any(
        boundaries[index] - boundaries[index - 1] != INTERVAL_MS
        for index in range(1, len(boundaries))
    ):
        result["status"] = "gap"
        return result
    value = _pct(recent[-1].value_usd, recent[0].value_usd)
    if value is None:
        result["status"] = "invalid"
        return result
    result.update({"value_pct": value, "status": "ok", "points": len(recent)})
    return result


def closed_kline_active_flow(
    row: Sequence[Any] | None,
    *,
    window_end_ms: int,
) -> dict[str, Any]:
    """Derive taker net flow from one exact closed Binance 15m kline."""

    unavailable = {
        "status": "window_incomplete",
        "net_usd": None,
        "gross_usd": None,
        "ratio": None,
    }
    try:
        boundary = _validate_window_end(window_end_ms)
    except LaunchMarketFactsError:
        return unavailable
    if (
        not isinstance(row, Sequence)
        or isinstance(row, (str, bytes))
        or len(row) < 11
    ):
        return unavailable
    open_time_ms = _integer(row[0])
    close_time_ms = _integer(row[6])
    gross = _finite_number(row[7])
    taker_buy = _finite_number(row[10])
    if (
        open_time_ms != boundary - INTERVAL_MS
        or close_time_ms != boundary - 1
        or gross is None
        or taker_buy is None
        or gross < 0
        or taker_buy < 0
        or taker_buy > gross
    ):
        return unavailable
    if gross == 0:
        return {
            "status": "no_trades",
            "net_usd": 0.0,
            "gross_usd": 0.0,
            "ratio": None,
        }
    net = taker_buy - (gross - taker_buy)
    return {
        "status": "available",
        "net_usd": net,
        "gross_usd": gross,
        "ratio": max(-1.0, min(1.0, net / gross)),
    }


def build_launch_market_facts(
    klines: Sequence[Sequence[Any]],
    oi_rows: Sequence[Mapping[str, Any]],
    *,
    window_end_ms: int,
    ticker_24h: Mapping[str, Any] | None = None,
    oi_24h_rows: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    try:
        boundary = _validate_window_end(window_end_ms)
        normalized_klines = normalize_binance_15m_klines(
            klines,
            window_end_ms=boundary,
        )
        normalized_oi = normalize_binance_15m_open_interest(
            oi_rows,
            window_end_ms=boundary,
        )
        _validate_continuous_boundaries(
            [row.period_end_ms for row in normalized_klines],
            window_end_ms=boundary,
            gap_error=ERROR_KLINE_GAP,
        )
        _validate_continuous_boundaries(
            [row.period_end_ms for row in normalized_oi],
            window_end_ms=boundary,
            gap_error=ERROR_OI_GAP,
        )
        recent_klines = normalized_klines[-REQUIRED_POINTS:]
        recent_oi = normalized_oi[-REQUIRED_POINTS:]
        if [row.period_end_ms for row in recent_klines] != [
            row.period_end_ms for row in recent_oi
        ]:
            raise LaunchMarketFactsError(ERROR_SERIES_MISALIGNED)

        closes = [row.close for row in recent_klines]
        oi_values = [row.value_usd for row in recent_oi]
        prior_volumes = [row.quote_volume for row in recent_klines[:-1]]
        average_volume = sum(prior_volumes) / len(prior_volumes)
        volume_ratio = (
            recent_klines[-1].quote_volume / average_volume
            if average_volume > 0
            else None
        )
        price_changes = {
            "15m": _pct(closes[-1], closes[-2]),
            "1h": _pct(closes[-1], closes[-5]),
            "4h": _pct(closes[-1], closes[-17]),
        }
        oi_changes = {
            "15m": _pct(oi_values[-1], oi_values[-2]),
            "1h": _pct(oi_values[-1], oi_values[-5]),
            "4h": _pct(oi_values[-1], oi_values[-17]),
        }
        oi_24h = closed_24h_open_interest_change(
            oi_24h_rows if oi_24h_rows is not None else oi_rows,
            window_end_ms=boundary,
        )
        return {
            "version": MARKET_FACTS_VERSION,
            "status": "ok",
            "error": "",
            "window_end_ms": boundary,
            "aligned_points": REQUIRED_POINTS,
            "closed_price": closes[-1],
            "closed_oi_usd": oi_values[-1],
            "closed_quote_volume": recent_klines[-1].quote_volume,
            "price_15m_pct": price_changes["15m"],
            "price_1h_pct": price_changes["1h"],
            "price_4h_pct": price_changes["4h"],
            "oi_15m_pct": oi_changes["15m"],
            "oi_1h_pct": oi_changes["1h"],
            "oi_4h_pct": oi_changes["4h"],
            "oi_24h_closed_pct": oi_24h["value_pct"],
            "oi_24h_status": oi_24h["status"],
            "oi_24h_points": oi_24h["points"],
            "oi_24h_semantics": oi_24h["semantics"],
            "volume_ratio_15m": volume_ratio,
            "recent_volatility_pct": _realized_volatility(closes),
            "price_24h_rolling_pct": _optional_ticker_change(ticker_24h),
            "price_24h_semantics": "rolling_24h_not_closed_window",
            "quadrants": {
                timeframe: price_oi_quadrant(
                    price_changes[timeframe],
                    oi_changes[timeframe],
                )
                for timeframe in ("15m", "1h", "4h")
            },
        }
    except LaunchMarketFactsError as exc:
        return _empty_result(exc.code)


__all__ = [
    "ERROR_BOUNDARY_MISMATCH",
    "ERROR_INSUFFICIENT_HISTORY",
    "ERROR_INPUT_INVALID",
    "ERROR_KLINE_DUPLICATE",
    "ERROR_KLINE_GAP",
    "ERROR_KLINE_MALFORMED",
    "ERROR_OI_DUPLICATE",
    "ERROR_OI_GAP",
    "ERROR_OI_MALFORMED",
    "ERROR_SERIES_MISALIGNED",
    "INTERVAL_MS",
    "OI_24H_REQUIRED_POINTS",
    "LaunchMarketFactsError",
    "build_launch_market_facts",
    "closed_kline_active_flow",
    "closed_24h_open_interest_change",
    "normalize_binance_15m_klines",
    "normalize_binance_15m_open_interest",
    "price_oi_quadrant",
]
