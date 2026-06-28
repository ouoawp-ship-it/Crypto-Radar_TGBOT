from __future__ import annotations

from typing import Any

from .config import Settings
from .data_sources import BinanceDataSource
from .liquidity_context import (
    LiquidityContext,
    LiquidityLevel,
    choose_near_level,
    liquidity_bias,
    unavailable_context,
)
from .radar import fmt_price, to_float


class BinanceOrderbookLiquidityProvider:
    """Free fallback liquidity provider based on Binance futures order book snapshots."""

    def __init__(self, settings: Settings, source: BinanceDataSource):
        self.settings = settings
        self.source = source
        self.requested_symbols = 0
        self.available_contexts = 0
        self.warnings: list[str] = []

    def context(self, symbol: str, price: float) -> LiquidityContext:
        symbol = symbol.upper()
        if not self.settings.liquidity_fallback_enable:
            return unavailable_context(symbol, "LIQUIDITY_FALLBACK_ENABLE=false", source="BinanceOrderBook")
        if not self.settings.binance_orderbook_liquidity_enable:
            return unavailable_context(symbol, "BINANCE_ORDERBOOK_LIQUIDITY_ENABLE=false", source="BinanceOrderBook")
        if price <= 0:
            return unavailable_context(symbol, "当前价格无效，无法计算盘口距离", source="BinanceOrderBook")

        self.requested_symbols += 1
        payload = self.source.order_book(symbol, self.settings.binance_orderbook_depth_limit)
        upper, lower = self._levels(payload, price)
        min_distance = self.settings.liquidity_min_distance_pct
        max_distance = self.settings.liquidity_max_distance_pct
        upper_wall = choose_near_level(upper, min_distance, max_distance)
        lower_wall = choose_near_level(lower, min_distance, max_distance)
        available = bool(upper_wall or lower_wall)
        if available:
            self.available_contexts += 1
        else:
            self.warnings.append("Binance盘口快照没有命中配置距离内的买卖墙")

        orderbook_bias = liquidity_bias(lower_wall, upper_wall, unavailable=not available)
        if orderbook_bias == "up":
            gap_direction = "down" if upper_wall else "none"
        elif orderbook_bias == "down":
            gap_direction = "up" if lower_wall else "none"
        elif lower_wall and not upper_wall:
            gap_direction = "up"
        elif upper_wall and not lower_wall:
            gap_direction = "down"
        else:
            gap_direction = "none" if available else "unavailable"

        reasons = ["盘口热力降级为 Binance 免费深度快照估算"]
        if upper_wall:
            reasons.append(f"上方卖墙距离{upper_wall.distance_pct:+.2f}%")
        if lower_wall:
            reasons.append(f"下方买墙距离{lower_wall.distance_pct:+.2f}%")
        if not available:
            reasons.append("未发现配置距离内的明显盘口墙")

        return LiquidityContext(
            symbol=symbol,
            available=available,
            source="BinanceOrderBook",
            upper_liquidity_wall=upper_wall.zone if upper_wall else None,
            lower_liquidity_wall=lower_wall.zone if lower_wall else None,
            upper_wall_distance_pct=upper_wall.distance_pct if upper_wall else None,
            lower_wall_distance_pct=lower_wall.distance_pct if lower_wall else None,
            liquidity_gap_direction=gap_direction,
            orderbook_bias=orderbook_bias,
            liquidation_bias="unavailable",
            reason_lines=reasons[:6],
        )

    @staticmethod
    def _levels(payload: dict[str, Any], current_price: float) -> tuple[list[LiquidityLevel], list[LiquidityLevel]]:
        upper: list[LiquidityLevel] = []
        lower: list[LiquidityLevel] = []
        for key, side, target in (("asks", "ask", upper), ("bids", "bid", lower)):
            rows = payload.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) < 2:
                    continue
                level_price = to_float(row[0])
                qty = to_float(row[1])
                if level_price <= 0 or qty <= 0:
                    continue
                distance = (level_price - current_price) / current_price * 100
                if side == "ask" and distance <= 0:
                    continue
                if side == "bid" and distance >= 0:
                    continue
                target.append(
                    LiquidityLevel(
                        price=level_price,
                        distance_pct=distance,
                        strength=level_price * qty,
                        zone=fmt_price(level_price),
                        side=side,
                    )
                )
        return upper, lower

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.liquidity_fallback_enable and self.settings.binance_orderbook_liquidity_enable),
            "requested_symbols": self.requested_symbols,
            "available_contexts": self.available_contexts,
            "depth_limit": self.settings.binance_orderbook_depth_limit,
            "warnings": self.warnings[:8],
            "source": self.source.diagnostics(),
        }
