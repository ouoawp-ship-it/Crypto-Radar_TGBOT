from __future__ import annotations

from typing import Any

from .binance_liquidity import BinanceOrderbookLiquidityProvider
from .coinglass_liquidity import (
    CoinglassLiquidityAnalyzer,
    LiquidityContext,
    score_liquidity_context,
    unavailable_context,
)
from .coinalyze_liquidity import CoinalyzeLiquidationProvider
from .coinalyze_source import CoinalyzeDataSource
from .config import Settings
from .data_sources import BinanceDataSource, CoinglassDataSource
from .structure_radar import StructureSignal, score_level


class MultiSourceLiquidityAnalyzer:
    """CoinGlass-first liquidity enhancer with free-source fallback."""

    def __init__(
        self,
        settings: Settings,
        coinglass: CoinglassLiquidityAnalyzer | None = None,
        binance_orderbook: BinanceOrderbookLiquidityProvider | None = None,
        coinalyze_liquidation: CoinalyzeLiquidationProvider | None = None,
    ):
        self.settings = settings
        self.coinglass = coinglass
        self.binance_orderbook = binance_orderbook
        self.coinalyze_liquidation = coinalyze_liquidation
        self.contexts: dict[str, LiquidityContext] = {}

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
        base = self._coinglass_context(symbol, price)
        needs_liquidation = not (base.upper_liquidation_zone or base.lower_liquidation_zone)
        needs_orderbook = not (base.upper_liquidity_wall or base.lower_liquidity_wall)

        liquidation_fallback = None
        orderbook_fallback = None
        if needs_liquidation and self.coinalyze_liquidation is not None:
            liquidation_fallback = self.coinalyze_liquidation.context(symbol, price)
        if needs_orderbook and self.binance_orderbook is not None:
            orderbook_fallback = self.binance_orderbook.context(symbol, price)

        context = merge_liquidity_contexts(base, liquidation_fallback, orderbook_fallback)
        self.contexts[symbol] = context
        return context

    def _coinglass_context(self, symbol: str, price: float) -> LiquidityContext:
        if self.coinglass is None:
            return unavailable_context(symbol, "CoinGlass流动性增强未启用", source="CoinGlass")
        return self.coinglass.context(symbol, price)

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.coinglass or self.binance_orderbook or self.coinalyze_liquidation),
            "available_contexts": sum(1 for ctx in self.contexts.values() if ctx.available),
            "sources": {
                "coinglass": self.coinglass.diagnostics() if self.coinglass else {"enabled": False},
                "binance_orderbook": self.binance_orderbook.diagnostics() if self.binance_orderbook else {"enabled": False},
                "coinalyze": self.coinalyze_liquidation.diagnostics() if self.coinalyze_liquidation else {"enabled": False},
            },
        }


def merge_liquidity_contexts(
    base: LiquidityContext,
    liquidation: LiquidityContext | None,
    orderbook: LiquidityContext | None,
) -> LiquidityContext:
    sources: list[str] = []
    reasons: list[str] = []

    def add_source(ctx: LiquidityContext | None) -> None:
        if ctx and ctx.source not in sources:
            sources.append(ctx.source)

    add_source(base)
    add_source(liquidation if liquidation and liquidation.available else None)
    add_source(orderbook if orderbook and orderbook.available else None)

    if base.reason_lines:
        reasons.extend(base.reason_lines[:3])

    upper_liquidation_zone = base.upper_liquidation_zone
    lower_liquidation_zone = base.lower_liquidation_zone
    upper_liquidation_score = base.upper_liquidation_score
    lower_liquidation_score = base.lower_liquidation_score
    nearest_liquidation_above_pct = base.nearest_liquidation_above_pct
    nearest_liquidation_below_pct = base.nearest_liquidation_below_pct
    liquidation_bias = base.liquidation_bias

    if not (upper_liquidation_zone or lower_liquidation_zone) and liquidation is not None:
        if liquidation.available:
            liquidation_bias = liquidation.liquidation_bias
            reasons.extend(liquidation.reason_lines[:3])
        elif liquidation.reason_lines:
            reasons.append(f"清算降级不可用：{liquidation.reason_lines[0]}")

    upper_liquidity_wall = base.upper_liquidity_wall
    lower_liquidity_wall = base.lower_liquidity_wall
    upper_wall_distance_pct = base.upper_wall_distance_pct
    lower_wall_distance_pct = base.lower_wall_distance_pct
    liquidity_gap_direction = base.liquidity_gap_direction
    orderbook_bias = base.orderbook_bias

    if not (upper_liquidity_wall or lower_liquidity_wall) and orderbook is not None:
        if orderbook.available:
            upper_liquidity_wall = orderbook.upper_liquidity_wall
            lower_liquidity_wall = orderbook.lower_liquidity_wall
            upper_wall_distance_pct = orderbook.upper_wall_distance_pct
            lower_wall_distance_pct = orderbook.lower_wall_distance_pct
            liquidity_gap_direction = orderbook.liquidity_gap_direction
            orderbook_bias = orderbook.orderbook_bias
            reasons.extend(orderbook.reason_lines[:3])
        elif orderbook.reason_lines:
            reasons.extend(f"盘口降级不可用：{line}" for line in orderbook.reason_lines[:3])

    available = bool(
        upper_liquidation_zone
        or lower_liquidation_zone
        or upper_liquidity_wall
        or lower_liquidity_wall
        or liquidation_bias in {"up", "down"}
    )
    if not available and not reasons:
        reasons.append("CoinGlass和免费降级源均不可用")

    return LiquidityContext(
        symbol=base.symbol,
        available=available,
        source="+".join(sources) if sources else "MultiSource",
        upper_liquidation_zone=upper_liquidation_zone,
        lower_liquidation_zone=lower_liquidation_zone,
        upper_liquidation_score=upper_liquidation_score,
        lower_liquidation_score=lower_liquidation_score,
        nearest_liquidation_above_pct=nearest_liquidation_above_pct,
        nearest_liquidation_below_pct=nearest_liquidation_below_pct,
        liquidation_bias=liquidation_bias,
        upper_liquidity_wall=upper_liquidity_wall,
        lower_liquidity_wall=lower_liquidity_wall,
        upper_wall_distance_pct=upper_wall_distance_pct,
        lower_wall_distance_pct=lower_wall_distance_pct,
        liquidity_gap_direction=liquidity_gap_direction,
        orderbook_bias=orderbook_bias,
        reason_lines=reasons[:8],
    )


def build_liquidity_enhancer(settings: Settings, binance_source: BinanceDataSource) -> MultiSourceLiquidityAnalyzer | None:
    coinglass = None
    if settings.coinglass_liquidity_enable:
        coinglass = CoinglassLiquidityAnalyzer(settings, CoinglassDataSource(settings))

    binance_orderbook = None
    coinalyze_liquidation = None
    if settings.liquidity_fallback_enable:
        if settings.binance_orderbook_liquidity_enable and hasattr(binance_source, "order_book"):
            binance_orderbook = BinanceOrderbookLiquidityProvider(settings, binance_source)
        if settings.coinalyze_enable:
            coinalyze_liquidation = CoinalyzeLiquidationProvider(settings, CoinalyzeDataSource(settings))

    if not (coinglass or binance_orderbook or coinalyze_liquidation):
        return None
    return MultiSourceLiquidityAnalyzer(settings, coinglass, binance_orderbook, coinalyze_liquidation)
