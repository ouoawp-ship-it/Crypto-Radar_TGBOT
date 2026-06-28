from __future__ import annotations

from dataclasses import dataclass, field

from .structure_radar import DOWN_SIGNAL_TYPES, UP_SIGNAL_TYPES, StructureSignal


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


def unavailable_context(symbol: str, reason: str = "", source: str = "Liquidity") -> LiquidityContext:
    return LiquidityContext(
        symbol=symbol,
        available=False,
        source=source,
        liquidation_bias="unavailable",
        orderbook_bias="unavailable",
        liquidity_gap_direction="unavailable",
        reason_lines=[reason] if reason else [],
    )


def choose_near_level(
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


def liquidity_bias(upper: LiquidityLevel | None, lower: LiquidityLevel | None, *, unavailable: bool) -> str:
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
