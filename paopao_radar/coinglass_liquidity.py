from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from .config import Settings
from .data_sources import CoinglassDataSource
from .radar import fmt_price, to_float
from .structure_radar import (
    DOWN_SIGNAL_TYPES,
    UP_SIGNAL_TYPES,
    StructureSignal,
    score_level,
)


@dataclass
class LiquidityContext:
    symbol: str
    available: bool
    source: str
    upper_liquidation_zone: str | None = None
    lower_liquidation_zone: str | None = None
    upper_liquidation_score: float | None = None
    lower_liquidation_score: float | None = None
    nearest_liquidation_above_pct: float | None = None
    nearest_liquidation_below_pct: float | None = None
    liquidation_bias: str = "unavailable"
    upper_liquidity_wall: str | None = None
    lower_liquidity_wall: str | None = None
    upper_wall_distance_pct: float | None = None
    lower_wall_distance_pct: float | None = None
    liquidity_gap_direction: str = "unavailable"
    orderbook_bias: str = "unavailable"
    score_delta: float = 0.0
    reason_lines: list[str] = field(default_factory=list)


@dataclass
class LiquidityLevel:
    price: float
    distance_pct: float
    strength: float
    zone: str
    side: str = ""


_CACHE: dict[str, tuple[float, LiquidityContext]] = {}


def unavailable_context(symbol: str, reason: str, source: str = "CoinGlass") -> LiquidityContext:
    return LiquidityContext(
        symbol=symbol,
        available=False,
        source=source,
        liquidation_bias="unavailable",
        orderbook_bias="unavailable",
        liquidity_gap_direction="unavailable",
        reason_lines=[reason],
    )


def _sequence_item(values: list[Any]) -> dict[str, Any] | None:
    numeric = [to_float(value) for value in values]
    numeric = [value for value in numeric if value > 0]
    if len(numeric) < 2:
        return None
    return {"price": numeric[-2], "amount": numeric[-1]}


def _axis_values(payload: dict[str, Any]) -> list[float]:
    for key in (
        "yAxis",
        "y_axis",
        "yaxis",
        "axisY",
        "priceAxis",
        "price_axis",
        "prices",
        "priceList",
        "price_list",
        "priceLevels",
        "price_levels",
        "ticks",
    ):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("data") or value.get("values") or value.get("list")
        if isinstance(value, list):
            result = [to_float(item) for item in value]
            result = [item for item in result if item > 0]
            if result:
                return result
    return []


def _matrix_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    y_axis = _axis_values(payload)
    matrix = None
    for key in (
        "data",
        "heatmap",
        "values",
        "series",
        "liquidationData",
        "liquidation_data",
        "orderbook",
        "orderBook",
        "points",
    ):
        value = payload.get(key)
        if isinstance(value, list):
            matrix = value
            break
    if not isinstance(matrix, list):
        return []

    rows: list[dict[str, Any]] = []
    for row in matrix:
        if isinstance(row, dict):
            if _extract_price(row) is not None:
                rows.append(row)
            continue
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        if y_axis and len(row) >= 3:
            index = int(to_float(row[1]))
            if 0 <= index < len(y_axis):
                amount = to_float(row[-1])
                if amount > 0:
                    rows.append({"price": y_axis[index], "amount": amount})
                    continue
        item = _sequence_item(list(row))
        if item:
            rows.append(item)
    return rows


def _as_items(payload: Any) -> list[dict[str, Any]]:
    if payload is None:
        return []
    if isinstance(payload, list):
        if payload and all(not isinstance(item, (dict, list, tuple)) for item in payload):
            item = _sequence_item(payload)
            return [item] if item else []
        result: list[dict[str, Any]] = []
        for item in payload:
            result.extend(_as_items(item))
        return result
    if isinstance(payload, dict):
        matrix_rows = _matrix_items(payload)
        if matrix_rows:
            return matrix_rows
        for key in (
            "data",
            "result",
            "list",
            "items",
            "records",
            "rows",
            "heatmap",
            "levels",
            "points",
            "liquidationData",
            "liquidation_data",
            "orderbook",
            "orderBook",
            "asks",
            "bids",
            "long",
            "short",
            "buy",
            "sell",
            "buyWall",
            "sellWall",
        ):
            value = payload.get(key)
            if isinstance(value, list):
                rows = _as_items(value)
                if key in {"asks", "sell", "short"}:
                    for row in rows:
                        row.setdefault("side", "ask")
                if key in {"bids", "buy", "long"}:
                    for row in rows:
                        row.setdefault("side", "bid")
                return rows
            if isinstance(value, dict):
                rows = _as_items(value)
                if rows:
                    return rows
        if _extract_price(payload) is not None:
            return [payload]
    return []


def _extract_price(item: dict[str, Any]) -> float | None:
    for key in (
        "price",
        "priceLevel",
        "price_level",
        "liqPrice",
        "liquidationPrice",
        "liquidation_price",
        "wallPrice",
        "wall_price",
        "priceUsd",
        "price_usd",
        "y",
        "p",
    ):
        value = to_float(item.get(key))
        if value > 0:
            return value
    for key in ("range", "zone", "priceRange", "price_range"):
        value = item.get(key)
        if isinstance(value, str):
            parts = [to_float(part) for part in value.replace("-", ",").split(",")]
            parts = [part for part in parts if part > 0]
            if parts:
                return sum(parts) / len(parts)
    return None


def _extract_strength(item: dict[str, Any]) -> float:
    for key in (
        "amount",
        "value",
        "size",
        "qty",
        "volume",
        "vol",
        "liquidationAmount",
        "liquidation_amount",
        "openInterest",
        "open_interest",
        "liquidity",
        "liquidityUsd",
        "liquidity_usd",
        "bidSize",
        "bid_size",
        "askSize",
        "ask_size",
        "sum",
        "total",
        "score",
    ):
        value = to_float(item.get(key))
        if value > 0:
            return value
    return 1.0


def parsed_item_count(payload: Any) -> int:
    """Return the number of parseable price/strength rows without exposing payload values."""
    return len(_as_items(payload))


def _scalar_shape(value: Any) -> dict[str, Any]:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "bool"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if isinstance(value, str):
        numeric = to_float(value) > 0
        return {"type": "str", "len": len(value), "numeric": numeric}
    return {"type": type(value).__name__}


def payload_shape_summary(payload: Any, *, max_depth: int = 3, max_keys: int = 12) -> dict[str, Any]:
    """Summarize a CoinGlass response shape without printing raw market data or secrets."""

    def walk(value: Any, depth: int) -> dict[str, Any]:
        if depth >= max_depth:
            if isinstance(value, dict):
                return {"type": "dict", "keys": list(value.keys())[:max_keys]}
            if isinstance(value, list):
                return {"type": "list", "len": len(value)}
            return _scalar_shape(value)

        if isinstance(value, dict):
            keys = list(value.keys())[:max_keys]
            children: dict[str, Any] = {}
            for key in keys[:6]:
                children[str(key)] = walk(value.get(key), depth + 1)
            return {"type": "dict", "keys": keys, "children": children}
        if isinstance(value, list):
            return {
                "type": "list",
                "len": len(value),
                "first": walk(value[0], depth + 1) if value else None,
            }
        if isinstance(value, tuple):
            return {
                "type": "tuple",
                "len": len(value),
                "first": walk(value[0], depth + 1) if value else None,
            }
        return _scalar_shape(value)

    return walk(payload, 0)


def api_status_summary(payload: Any) -> dict[str, str] | None:
    """Expose only CoinGlass business status fields; no API key or market payload is printed."""
    if not isinstance(payload, dict):
        return None
    if "code" not in payload and "msg" not in payload:
        return None
    result: dict[str, str] = {}
    if "code" in payload:
        result["code"] = str(payload.get("code"))[:32]
    if "msg" in payload:
        result["msg"] = str(payload.get("msg"))[:120]
    return result


def _zone_text(item: dict[str, Any], price: float) -> str:
    for key in ("range", "zone", "priceRange", "price_range"):
        value = item.get(key)
        if value:
            return str(value)
    return fmt_price(price)


def _levels_from_payload(payload: Any, current_price: float) -> tuple[list[LiquidityLevel], list[LiquidityLevel]]:
    above: list[LiquidityLevel] = []
    below: list[LiquidityLevel] = []
    for item in _as_items(payload):
        price = _extract_price(item)
        if price is None or price <= 0 or current_price <= 0:
            continue
        side = str(item.get("side") or item.get("direction") or item.get("type") or "").lower()
        distance = (price - current_price) / current_price * 100
        level = LiquidityLevel(
            price=price,
            distance_pct=distance,
            strength=_extract_strength(item),
            zone=_zone_text(item, price),
            side=side,
        )
        if side in {"ask", "sell", "short", "upper"}:
            above.append(level)
        elif side in {"bid", "buy", "long", "lower"}:
            below.append(level)
        elif distance > 0:
            above.append(level)
        elif distance < 0:
            below.append(level)
    return above, below


def _choose_near_level(
    levels: list[LiquidityLevel],
    min_distance_pct: float,
    max_distance_pct: float,
) -> LiquidityLevel | None:
    candidates = [
        level for level in levels
        if max(0.0, min_distance_pct) <= abs(level.distance_pct) <= max(0.1, max_distance_pct)
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda item: (abs(item.distance_pct), -item.strength))
    return candidates[0]


def _near_score(level: LiquidityLevel | None) -> float:
    if not level:
        return 0.0
    return max(1.0, level.strength)


def _bias(upper: LiquidityLevel | None, lower: LiquidityLevel | None, *, unavailable: bool) -> str:
    if unavailable:
        return "unavailable"
    upper_score = _near_score(upper)
    lower_score = _near_score(lower)
    if upper_score <= 0 and lower_score <= 0:
        return "neutral"
    if upper_score >= lower_score * 1.2:
        return "up"
    if lower_score >= upper_score * 1.2:
        return "down"
    return "neutral"


def _signal_direction(signal: StructureSignal) -> str:
    if signal.signal_type in UP_SIGNAL_TYPES:
        return "up"
    if signal.signal_type in DOWN_SIGNAL_TYPES:
        return "down"
    return "neutral"


def score_liquidity_context(
    signal: StructureSignal,
    context: LiquidityContext,
    max_delta: float,
) -> float:
    if not context.available:
        return 0.0
    direction = _signal_direction(signal)
    if direction == "neutral":
        return 0.0
    delta = 0.0
    max_distance = 8.0

    upper_near = (
        context.nearest_liquidation_above_pct is not None
        and 0 < context.nearest_liquidation_above_pct <= max_distance
    )
    lower_near = (
        context.nearest_liquidation_below_pct is not None
        and 0 < abs(context.nearest_liquidation_below_pct) <= max_distance
    )
    upper_wall_near = (
        context.upper_wall_distance_pct is not None
        and 0 < context.upper_wall_distance_pct <= max_distance
    )
    lower_wall_near = (
        context.lower_wall_distance_pct is not None
        and 0 < abs(context.lower_wall_distance_pct) <= max_distance
    )

    if direction == "up":
        if context.liquidation_bias == "up":
            delta += 6
        if upper_near:
            distance = context.nearest_liquidation_above_pct or max_distance
            delta += max(1.0, 5.0 - distance * 0.45)
        if context.liquidity_gap_direction == "up":
            delta += 3
        if context.orderbook_bias == "up":
            delta += 3
        if context.orderbook_bias == "down" or upper_wall_near:
            delta -= 5 if upper_wall_near else 3
        if lower_near and not upper_near:
            delta -= 4
    elif direction == "down":
        if context.liquidation_bias == "down":
            delta += 6
        if lower_near:
            distance = abs(context.nearest_liquidation_below_pct or max_distance)
            delta += max(1.0, 5.0 - distance * 0.45)
        if context.liquidity_gap_direction == "down":
            delta += 3
        if context.orderbook_bias == "down":
            delta += 3
        if context.orderbook_bias == "up" or lower_wall_near:
            delta -= 5 if lower_wall_near else 3
        if upper_near and not lower_near:
            delta -= 4

    cap = max(0.0, float(max_delta))
    return max(-cap, min(cap, round(delta, 2)))


class CoinglassLiquidityAnalyzer:
    def __init__(self, settings: Settings, source: CoinglassDataSource):
        self.settings = settings
        self.source = source
        self.requested_symbols = 0
        self.cache_hits = 0
        self.contexts: dict[str, LiquidityContext] = {}
        self.warnings: list[str] = []

    def enhance(self, signal: StructureSignal) -> StructureSignal:
        base_score = signal.base_score if signal.base_score is not None else signal.score
        context = self.context(signal.symbol, signal.price)
        delta = score_liquidity_context(
            signal,
            context,
            self.settings.coinglass_liquidity_score_max_delta,
        )
        context.score_delta = delta
        final_score = max(0.0, min(100.0, float(base_score) + delta))
        signal.base_score = round(float(base_score), 2)
        signal.liquidity_context = context
        signal.liquidity_score_delta = round(delta, 2)
        signal.final_score = round(final_score, 2)
        signal.score = round(final_score, 2)
        signal.level = score_level(signal.score)
        return signal

    def context(self, symbol: str, price: float) -> LiquidityContext:
        symbol = symbol.upper()
        if not self.settings.coinglass_liquidity_enable:
            return unavailable_context(symbol, "COINGLASS_LIQUIDITY_ENABLE=false")
        if not self.source.enabled:
            return unavailable_context(symbol, "CoinGlass Key未配置或COINGLASS_ENABLE=false")
        if self.requested_symbols >= max(0, self.settings.coinglass_liquidity_max_symbols):
            return unavailable_context(symbol, "本轮CoinGlass增强数量达到上限")

        use_global_cache = isinstance(self.source, CoinglassDataSource)
        cache_key = f"{self.settings.coinglass_base_url}:{symbol}:{round(float(price or 0), 8)}"
        if use_global_cache:
            cached = _CACHE.get(cache_key)
            if cached and time.time() - cached[0] <= max(60, self.settings.coinglass_liquidity_cache_sec):
                self.cache_hits += 1
                return cached[1]

        self.requested_symbols += 1
        try:
            context = self._fetch_context(symbol, price)
        except Exception as exc:
            context = unavailable_context(symbol, f"CoinGlass解析失败: {type(exc).__name__}")
        if use_global_cache:
            _CACHE[cache_key] = (time.time(), context)
        self.contexts[symbol] = context
        if not context.available:
            self.warnings.extend(context.reason_lines[:1])
        return context

    def _fetch_context(self, symbol: str, price: float) -> LiquidityContext:
        liquidation_payload = self.source.liquidation_heatmap(
            self.settings.coinglass_exchange_list,
            symbol,
            range_="24h",
        )
        orderbook_payload = self.source.orderbook_heatmap(
            self.settings.coinglass_exchange_list,
            symbol,
            range_="24h",
        )
        liquidation_unavailable = liquidation_payload is None
        orderbook_unavailable = orderbook_payload is None
        upper_liq, lower_liq = _levels_from_payload(liquidation_payload, price)
        upper_wall, lower_wall = _levels_from_payload(orderbook_payload, price)

        min_distance = self.settings.coinglass_liquidity_min_distance_pct
        max_distance = self.settings.coinglass_liquidity_max_distance_pct
        upper_liq_level = _choose_near_level(upper_liq, min_distance, max_distance)
        lower_liq_level = _choose_near_level(lower_liq, min_distance, max_distance)
        upper_wall_level = _choose_near_level(upper_wall, min_distance, max_distance)
        lower_wall_level = _choose_near_level(lower_wall, min_distance, max_distance)

        available = any((upper_liq_level, lower_liq_level, upper_wall_level, lower_wall_level))
        reasons: list[str] = []
        if liquidation_unavailable:
            reasons.append("清算热力图接口不可用或无权限")
        if orderbook_unavailable:
            reasons.append("盘口流动性接口不可用或无权限")
        if not available and not reasons:
            reasons.append("CoinGlass返回数据为空或无法解析")

        liquidation_bias = _bias(upper_liq_level, lower_liq_level, unavailable=liquidation_unavailable)
        orderbook_bias = _bias(lower_wall_level, upper_wall_level, unavailable=orderbook_unavailable)
        if orderbook_bias == "up":
            liquidity_gap_direction = "down" if upper_wall_level else "none"
        elif orderbook_bias == "down":
            liquidity_gap_direction = "up" if lower_wall_level else "none"
        elif upper_liq_level and not upper_wall_level:
            liquidity_gap_direction = "up"
        elif lower_liq_level and not lower_wall_level:
            liquidity_gap_direction = "down"
        else:
            liquidity_gap_direction = "none" if not orderbook_unavailable else "unavailable"

        if upper_liq_level:
            reasons.append(f"上方清算区距离{upper_liq_level.distance_pct:+.2f}%")
        if lower_liq_level:
            reasons.append(f"下方清算区距离{lower_liq_level.distance_pct:+.2f}%")
        if upper_wall_level:
            reasons.append(f"上方卖墙距离{upper_wall_level.distance_pct:+.2f}%")
        if lower_wall_level:
            reasons.append(f"下方买墙距离{lower_wall_level.distance_pct:+.2f}%")

        return LiquidityContext(
            symbol=symbol,
            available=available,
            source="CoinGlass",
            upper_liquidation_zone=upper_liq_level.zone if upper_liq_level else None,
            lower_liquidation_zone=lower_liq_level.zone if lower_liq_level else None,
            upper_liquidation_score=upper_liq_level.strength if upper_liq_level else None,
            lower_liquidation_score=lower_liq_level.strength if lower_liq_level else None,
            nearest_liquidation_above_pct=upper_liq_level.distance_pct if upper_liq_level else None,
            nearest_liquidation_below_pct=lower_liq_level.distance_pct if lower_liq_level else None,
            liquidation_bias=liquidation_bias,
            upper_liquidity_wall=upper_wall_level.zone if upper_wall_level else None,
            lower_liquidity_wall=lower_wall_level.zone if lower_wall_level else None,
            upper_wall_distance_pct=upper_wall_level.distance_pct if upper_wall_level else None,
            lower_wall_distance_pct=lower_wall_level.distance_pct if lower_wall_level else None,
            liquidity_gap_direction=liquidity_gap_direction,
            orderbook_bias=orderbook_bias,
            reason_lines=reasons[:6],
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.coinglass_liquidity_enable),
            "source_enabled": bool(self.source.enabled),
            "requested_symbols": self.requested_symbols,
            "cache_hits": self.cache_hits,
            "max_symbols": self.settings.coinglass_liquidity_max_symbols,
            "available_contexts": sum(1 for ctx in self.contexts.values() if ctx.available),
            "warnings": self.warnings[:8],
            "source": self.source.diagnostics(),
        }
