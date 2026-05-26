from __future__ import annotations

import time
from typing import Any

from .coinglass_liquidity import LiquidityContext, unavailable_context
from .coinalyze_source import CoinalyzeDataSource
from .config import Settings
from .radar import to_float


class CoinalyzeLiquidationProvider:
    """Free-key liquidation fallback based on historical liquidation volume.

    Coinalyze does not expose future liquidation price buckets here, so this source only
    contributes directional liquidation pressure and clearly marks itself as history based.
    """

    def __init__(self, settings: Settings, source: CoinalyzeDataSource):
        self.settings = settings
        self.source = source
        self.requested_symbols = 0
        self.available_contexts = 0
        self.warnings: list[str] = []

    def context(self, symbol: str, price: float) -> LiquidityContext:
        symbol = symbol.upper()
        if not self.settings.liquidity_fallback_enable:
            return unavailable_context(symbol, "LIQUIDITY_FALLBACK_ENABLE=false", source="CoinalyzeHistory")
        if not self.source.enabled:
            return unavailable_context(symbol, "Coinalyze Key未配置或COINALYZE_ENABLE=false", source="CoinalyzeHistory")

        self.requested_symbols += 1
        now = int(time.time())
        lookback = max(1, self.settings.coinalyze_liquidation_lookback_hours) * 3600
        payload = self.source.liquidation_history(
            symbol,
            now - lookback,
            now,
            interval=self.settings.coinalyze_liquidation_interval,
        )
        long_liq, short_liq = self._totals(payload)
        total = long_liq + short_liq
        if total <= 0:
            self.warnings.append("Coinalyze清算历史为空")
            return unavailable_context(symbol, "Coinalyze清算历史为空", source="CoinalyzeHistory")

        bias = "neutral"
        if short_liq >= long_liq * 1.3:
            bias = "up"
        elif long_liq >= short_liq * 1.3:
            bias = "down"
        self.available_contexts += 1
        return LiquidityContext(
            symbol=symbol,
            available=bias != "neutral",
            source="CoinalyzeHistory",
            liquidation_bias=bias,
            orderbook_bias="unavailable",
            liquidity_gap_direction="unavailable",
            reason_lines=[
                "清算热力降级为 Coinalyze 历史清算量，仅作方向辅助",
                f"多头清算${long_liq:,.0f} | 空头清算${short_liq:,.0f}",
            ],
        )

    @staticmethod
    def _totals(payload: Any) -> tuple[float, float]:
        long_liq = 0.0
        short_liq = 0.0
        rows = payload if isinstance(payload, list) else []
        for item in rows:
            if not isinstance(item, dict):
                continue
            for row in item.get("history", []) if isinstance(item.get("history"), list) else []:
                if not isinstance(row, dict):
                    continue
                long_liq += to_float(row.get("l"))
                short_liq += to_float(row.get("s"))
        return long_liq, short_liq

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.settings.liquidity_fallback_enable and self.source.enabled),
            "requested_symbols": self.requested_symbols,
            "available_contexts": self.available_contexts,
            "warnings": self.warnings[:8],
            "source": self.source.diagnostics(),
        }
