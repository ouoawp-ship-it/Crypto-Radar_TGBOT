from __future__ import annotations

import math
from typing import Any

from .config import Settings
from .data_sources import HttpClient


CROSS_EXCHANGE_OI_SCHEMA_VERSION = "workstation.funds.open-interest.v1"


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any) -> float | None:
    number = _number(value)
    return number if number is not None and number > 0 else None


def _first(value: Any, *path: str) -> dict[str, Any]:
    current = value
    for key in path:
        current = current.get(key) if isinstance(current, dict) else None
    if isinstance(current, list) and current and isinstance(current[0], dict):
        return current[0]
    return current if isinstance(current, dict) else {}


def build_cross_exchange_open_interest(
    symbol: str,
    *,
    mark_price: float | None,
    payloads: dict[str, Any],
) -> dict[str, Any]:
    target = str(symbol or "").upper()
    price = _positive(mark_price)
    binance_raw = payloads.get("binance") if isinstance(payloads.get("binance"), dict) else {}
    bybit_raw = _first(payloads.get("bybit"), "result", "list")
    okx_raw = _first(payloads.get("okx"), "data")

    binance_native = _positive(binance_raw.get("openInterest"))
    bybit_native = _positive(bybit_raw.get("openInterest"))
    okx_native = _positive(okx_raw.get("oiCcy")) or _positive(okx_raw.get("oi"))
    values = {
        "binance": binance_native * price if binance_native is not None and price is not None else None,
        "bybit": bybit_native * price if bybit_native is not None and price is not None else None,
        "okx": _positive(okx_raw.get("oiUsd"))
        or (okx_native * price if okx_native is not None and price is not None else None),
    }
    natives = {"binance": binance_native, "bybit": bybit_native, "okx": okx_native}
    sources = {
        "binance": "binance_fapi_open_interest",
        "bybit": "bybit_v5_open_interest",
        "okx": "okx_public_open_interest",
    }
    available_values = [float(value) for value in values.values() if value is not None]
    total = sum(available_values) if available_values else None
    rows = []
    for exchange in ("binance", "bybit", "okx"):
        value = values[exchange]
        rows.append({
            "exchange": exchange,
            "oi_usd": round(value, 2) if value is not None else None,
            "oi_native": natives[exchange],
            "share_pct": round(value / total * 100, 4) if value is not None and total else None,
            "status": "ready" if value is not None else "unavailable",
            "source": sources[exchange],
        })
    ready = sum(1 for row in rows if row["status"] == "ready")
    top_share = max((float(row["share_pct"]) for row in rows if row["share_pct"] is not None), default=None)
    return {
        "schema_version": CROSS_EXCHANGE_OI_SCHEMA_VERSION,
        "symbol": target,
        "data_status": "ready" if ready >= 2 else "degraded" if ready else "unavailable",
        "coverage": {"exchanges": ready, "target": len(rows)},
        "mark_price": price,
        "total_oi_usd": round(total, 2) if total is not None else None,
        "top_exchange_share_pct": round(top_share, 4) if top_share is not None else None,
        "exchanges": rows,
        "methodology": {
            "normalization": "Binance/Bybit base-asset OI uses the current Binance mark price; OKX prefers its reported oiUsd.",
            "concentration": "Exchange share equals venue OI USD divided by the available-venue total; missing venues are excluded, never treated as zero.",
        },
    }


def collect_cross_exchange_open_interest(
    settings: Settings,
    symbol: str,
    *,
    http: HttpClient | None = None,
) -> dict[str, Any]:
    target = str(symbol or "").upper()
    own_http = http is None
    client = http or HttpClient(settings)
    try:
        mark = client.get_json(
            f"{settings.binance_fapi_base_url.rstrip('/')}/fapi/v1/premiumIndex",
            {"symbol": target},
            cache_key=f"workstation:oi:mark:{target}",
            quality_key="binanceOpenInterest",
            retries=1,
        )
        payloads = {
            "binance": client.get_json(
                f"{settings.binance_fapi_base_url.rstrip('/')}/fapi/v1/openInterest",
                {"symbol": target},
                cache_key=f"workstation:oi:binance:{target}",
                quality_key="binanceOpenInterest",
                retries=1,
            ),
            "bybit": client.get_json(
                f"{settings.bybit_public_rest_url.rstrip('/')}/v5/market/open-interest",
                {"category": "linear", "symbol": target, "intervalTime": "5min", "limit": 1},
                cache_key=f"workstation:oi:bybit:{target}",
                quality_key="bybitOpenInterest",
                retries=1,
            ),
            "okx": client.get_json(
                f"{settings.okx_public_rest_url.rstrip('/')}/api/v5/public/open-interest",
                {"instType": "SWAP", "instId": f"{target[:-4]}-USDT-SWAP"},
                cache_key=f"workstation:oi:okx:{target}",
                quality_key="okxOpenInterest",
                retries=1,
            ),
        }
        return build_cross_exchange_open_interest(
            target,
            mark_price=_positive(mark.get("markPrice")) if isinstance(mark, dict) else None,
            payloads=payloads,
        )
    finally:
        if own_http:
            client.close()


__all__ = [
    "CROSS_EXCHANGE_OI_SCHEMA_VERSION",
    "build_cross_exchange_open_interest",
    "collect_cross_exchange_open_interest",
]
