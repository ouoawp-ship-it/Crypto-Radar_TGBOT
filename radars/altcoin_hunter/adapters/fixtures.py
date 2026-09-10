"""Explicit synthetic directory for offline examples; never infer real symbols."""
from __future__ import annotations

from copy import deepcopy
from typing import Mapping

from .binance_usdm import BinanceInstrumentSpec, parse_exchange_info

FIXTURE_TIME_MS = 1_788_518_400_000


def fixture_exchange_info() -> dict:
    symbols = []
    for symbol, base in (("AAAUSDT", "AAA"), ("BBBUSDT", "BBB"), ("1000TESTUSDT", "1000TEST")):
        symbols.append({"symbol": symbol, "pair": symbol, "contractType": "PERPETUAL", "status": "TRADING",
            "baseAsset": base, "quoteAsset": "USDT", "marginAsset": "USDT", "underlyingType": "COIN",
            "underlyingSubType": ["SYNTHETIC"], "onboardDate": FIXTURE_TIME_MS - 86_400_000,
            "deliveryDate": 4_133_404_800_000, "pricePrecision": 8, "quantityPrecision": 8,
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.0100", "maxPrice": "100000", "tickSize": "0.0100"},
                {"filterType": "LOT_SIZE", "minQty": "0.100", "maxQty": "100000", "stepSize": "0.100"},
                {"filterType": "MARKET_LOT_SIZE", "minQty": "0.100", "maxQty": "10000", "stepSize": "0.100"},
                {"filterType": "MIN_NOTIONAL", "notional": "5.00"}]})
    return deepcopy({"timezone": "UTC", "serverTime": FIXTURE_TIME_MS, "assets": [],
                     "exchangeFilters": [], "rateLimits": [], "symbols": symbols})


def fixture_registry() -> Mapping[str, BinanceInstrumentSpec]:
    result = parse_exchange_info(fixture_exchange_info(), observed_at_ms=FIXTURE_TIME_MS)
    if not result.accepted:
        raise ValueError("invalid_bundled_synthetic_directory")
    return result.registry
