from __future__ import annotations

import math
import time
from typing import Any

from .asset_catalog import (
    ASSET_CATALOG_VERSION,
    asset_sector_view,
    public_sector_catalog,
)
from .config import Settings
from .market_cockpit import MARKET_COCKPIT_SCHEMA_VERSION, load_market_cockpit, normalize_window


FUNDS_SCHEMA_VERSION = "2026-07-17"
MARKET_TYPES = ("spot", "futures")
ASSET_SORT_KEYS = {
    "symbol",
    "price",
    "price_change_pct",
    "net_flow_usd",
    "volume_usd",
    "volume_change_pct",
    "oi_usd",
    "oi_change_pct",
    "funding_pct",
    "market_cap",
    "updated_at",
}


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def normalize_market_type(value: Any) -> str:
    requested = str(value or "spot").strip().lower()
    return requested if requested in MARKET_TYPES else "spot"


def _flow_keys(market_type: str) -> tuple[str, str, str, str]:
    normalized = normalize_market_type(market_type)
    return (
        f"{normalized}_flow_usd",
        f"{normalized}_inflow_usd",
        f"{normalized}_outflow_usd",
        "binance_spot_klines" if normalized == "spot" else "binance_futures_klines",
    )


def _asset_row(source: dict[str, Any], *, market_type: str) -> dict[str, Any]:
    flow_key, inflow_key, outflow_key, flow_source = _flow_keys(market_type)
    net_flow = _number(source.get(flow_key))
    inflow = _number(source.get(inflow_key))
    outflow = _number(source.get(outflow_key))
    sector = asset_sector_view(source.get("symbol"))
    source_status = str(source.get("status") or "degraded")
    if net_flow is None:
        data_status = "unavailable"
    elif source_status == "stale":
        data_status = "stale"
    elif source_status in {"fresh", "ready"}:
        data_status = "ready"
    else:
        data_status = "degraded"
    return {
        "symbol": str(source.get("symbol") or ""),
        "coin": str(source.get("coin") or ""),
        "price": _number(source.get("price")),
        "price_change_pct": _number(source.get("price_change_pct")),
        "price_change_window_sec": int(_number(source.get("price_change_window_sec")) or 0),
        "net_flow_usd": net_flow,
        "net_flow_change_pct": None,
        "inflow_usd": inflow,
        "outflow_usd": outflow,
        "volume_usd": _number(source.get("quote_volume")),
        "volume_change_pct": _number(source.get("volume_change_pct")),
        "oi_usd": _number(source.get("oi_usd")),
        "oi_change_pct": _number(source.get("oi_change_pct")),
        "funding_pct": _number(source.get("funding_pct")),
        "market_cap": _number(source.get("market_cap")),
        "sector": sector,
        "updated_at": str(source.get("updated_at") or ""),
        "age_sec": int(_number(source.get("age_sec")) or 0),
        "data_status": data_status,
        "quality": {
            "flow": "taker_buy_sell_window" if inflow is not None and outflow is not None else "cvd_net_only" if net_flow is not None else "missing",
            "price_change": str((source.get("quality") or {}).get("price_change_pct") or "missing"),
            "oi_change": str((source.get("quality") or {}).get("oi_change_pct") or "missing"),
        },
        "sources": {
            "price": "binance_futures_ticker",
            "volume": "binance_futures_ticker",
            "flow": flow_source,
            "oi": "binance_futures_open_interest",
            "funding": "binance_futures_premium_index",
            "market_cap": "binance_public_market_catalog" if _number(source.get("market_cap")) is not None else "unavailable",
        },
    }


def funds_asset_rows(cockpit: dict[str, Any], *, market_type: str) -> list[dict[str, Any]]:
    source = cockpit.get("assets") if isinstance(cockpit.get("assets"), list) else []
    return [_asset_row(item, market_type=market_type) for item in source if isinstance(item, dict)]


def _status(assets: list[dict[str, Any]]) -> tuple[str, dict[str, int]]:
    coverage = {
        "assets": len(assets),
        "flow": sum(1 for item in assets if item.get("net_flow_usd") is not None),
        "gross_flow": sum(1 for item in assets if item.get("inflow_usd") is not None and item.get("outflow_usd") is not None),
        "oi": sum(1 for item in assets if item.get("oi_usd") is not None),
        "market_cap": sum(1 for item in assets if item.get("market_cap") is not None),
    }
    if not assets:
        return "empty", coverage
    if coverage["flow"] == 0:
        return "unavailable", coverage
    if coverage["flow"] < max(1, math.ceil(len(assets) * 0.6)):
        return "degraded", coverage
    return "ready", coverage


def _warnings(status: str, coverage: dict[str, int]) -> list[str]:
    warnings: list[str] = []
    if status in {"degraded", "unavailable"}:
        warnings.append("资金流仅覆盖完成当前窗口 CVD 扫描的资产，未覆盖资产不按 0 计算。")
    if coverage.get("gross_flow", 0) < coverage.get("flow", 0):
        warnings.append("部分历史快照只保存 CVD 净额，买入/卖出总额保持不可用。")
    if coverage.get("market_cap", 0) < coverage.get("assets", 0):
        warnings.append("市值仅在公开币种目录成功匹配时显示，缺失值不做前端估算。")
    return warnings


def build_funds_sectors(
    cockpit: dict[str, Any],
    *,
    market_type: str = "spot",
) -> dict[str, Any]:
    normalized_market = normalize_market_type(market_type)
    assets = funds_asset_rows(cockpit, market_type=normalized_market)
    status, coverage = _status(assets)
    sector_groups: dict[str, list[dict[str, Any]]] = {}
    for item in assets:
        sector_id = str((item.get("sector") or {}).get("primary_sector_id") or "other")
        sector_groups.setdefault(sector_id, []).append(item)

    sectors: list[dict[str, Any]] = []
    catalog_by_id = {item["id"]: item for item in public_sector_catalog()}
    for sector_id, group in sector_groups.items():
        flow_assets = [item for item in group if item.get("net_flow_usd") is not None]
        gross_assets = [item for item in group if item.get("inflow_usd") is not None and item.get("outflow_usd") is not None]
        net_flow = sum(float(item["net_flow_usd"]) for item in flow_assets)
        inflow = sum(float(item["inflow_usd"]) for item in gross_assets) if gross_assets else None
        outflow = sum(float(item["outflow_usd"]) for item in gross_assets) if gross_assets else None
        ratio = len(flow_assets) / len(group) if group else 0.0
        leaders = sorted(flow_assets, key=lambda item: abs(float(item.get("net_flow_usd") or 0)), reverse=True)[:4]
        definition = catalog_by_id.get(sector_id, catalog_by_id["other"])
        sectors.append({
            "sector_id": sector_id,
            "label": definition["label"],
            "description": definition["description"],
            "market_type": normalized_market,
            "inflow_usd": round(inflow, 2) if inflow is not None else None,
            "outflow_usd": round(outflow, 2) if outflow is not None else None,
            "net_flow_usd": round(net_flow, 2) if flow_assets else None,
            "magnitude_usd": round(abs(net_flow), 2) if flow_assets else None,
            "asset_count": len(group),
            "covered_assets": len(flow_assets),
            "coverage_ratio": round(ratio, 4),
            "data_status": "ready" if ratio >= 0.6 else "degraded" if flow_assets else "unavailable",
            "leaders": [
                {"symbol": item["symbol"], "net_flow_usd": item["net_flow_usd"], "data_status": item["data_status"]}
                for item in leaders
            ],
        })
    sectors.sort(key=lambda item: float(item.get("magnitude_usd") or 0), reverse=True)
    flow_assets = [item for item in assets if item.get("net_flow_usd") is not None]
    gross_assets = [item for item in assets if item.get("inflow_usd") is not None and item.get("outflow_usd") is not None]
    total_net = sum(float(item["net_flow_usd"]) for item in flow_assets)
    total_inflow = sum(float(item["inflow_usd"]) for item in gross_assets) if gross_assets else None
    total_outflow = sum(float(item["outflow_usd"]) for item in gross_assets) if gross_assets else None
    return {
        "schema_version": FUNDS_SCHEMA_VERSION,
        "market_schema_version": cockpit.get("schema_version") or MARKET_COCKPIT_SCHEMA_VERSION,
        "catalog_version": ASSET_CATALOG_VERSION,
        "generated_at": cockpit.get("generated_at"),
        "window_sec": cockpit.get("window_sec"),
        "market_type": normalized_market,
        "data_status": status,
        "coverage": coverage,
        "warnings": _warnings(status, coverage),
        "summary": {
            "net_flow_usd": round(total_net, 2) if flow_assets else None,
            "inflow_usd": round(total_inflow, 2) if total_inflow is not None else None,
            "outflow_usd": round(total_outflow, 2) if total_outflow is not None else None,
            "asset_count": len(assets),
            "covered_assets": len(flow_assets),
            "leading_inflow_sector": next((item["sector_id"] for item in sectors if _number(item.get("net_flow_usd")) is not None and float(item["net_flow_usd"]) > 0), ""),
            "leading_outflow_sector": next((item["sector_id"] for item in sectors if _number(item.get("net_flow_usd")) is not None and float(item["net_flow_usd"]) < 0), ""),
        },
        "catalog": public_sector_catalog(),
        "sectors": sectors,
        "methodology": {
            "classification": "每个资产只按版本化分类的第一个主板块计入总额，附加标签不重复聚合。",
            "flow": "基于 Binance 封闭 K 线窗口的主动买入与主动卖出成交额；净额是 CVD 估算，不代表交易所充提净流入。",
            "coverage": "板块覆盖率=有效资金流资产数/当前扫描资产数；低于 60% 标记降级。",
        },
    }


def _sort_value(item: dict[str, Any], key: str) -> Any:
    value = item.get(key)
    if key in {"symbol", "updated_at"}:
        return str(value)
    return float(value)


def build_funds_assets(
    cockpit: dict[str, Any],
    *,
    market_type: str = "spot",
    search: str = "",
    sector: str = "",
    data_status: str = "",
    sort_key: str = "net_flow_usd",
    direction: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict[str, Any]:
    normalized_market = normalize_market_type(market_type)
    assets = funds_asset_rows(cockpit, market_type=normalized_market)
    overall_status, coverage = _status(assets)
    safe_search = str(search or "").strip().upper()[:24]
    safe_sector = str(sector or "").strip().lower()[:40]
    safe_status = str(data_status or "").strip().lower()[:24]
    filtered = [
        item for item in assets
        if (not safe_search or safe_search in item["symbol"] or safe_search in item["coin"])
        and (not safe_sector or safe_sector in set((item.get("sector") or {}).get("sector_ids") or []))
        and (not safe_status or item.get("data_status") == safe_status)
    ]
    normalized_sort = sort_key if sort_key in ASSET_SORT_KEYS else "net_flow_usd"
    normalized_direction = "asc" if str(direction).lower() == "asc" else "desc"
    available = [item for item in filtered if item.get(normalized_sort) not in (None, "")]
    missing = [item for item in filtered if item.get(normalized_sort) in (None, "")]
    available.sort(key=lambda item: _sort_value(item, normalized_sort), reverse=normalized_direction == "desc")
    filtered = available + missing
    safe_page_size = max(10, min(100, int(page_size or 50)))
    safe_page = max(1, int(page or 1))
    total = len(filtered)
    page_count = max(1, math.ceil(total / safe_page_size))
    safe_page = min(safe_page, page_count)
    start = (safe_page - 1) * safe_page_size
    items = filtered[start:start + safe_page_size]
    return {
        "schema_version": FUNDS_SCHEMA_VERSION,
        "catalog_version": ASSET_CATALOG_VERSION,
        "generated_at": cockpit.get("generated_at"),
        "window_sec": cockpit.get("window_sec"),
        "market_type": normalized_market,
        "data_status": overall_status,
        "coverage": coverage,
        "warnings": _warnings(overall_status, coverage),
        "filters": {"search": safe_search, "sector": safe_sector, "data_status": safe_status},
        "sort": {"key": normalized_sort, "direction": normalized_direction},
        "pagination": {
            "page": safe_page,
            "page_size": safe_page_size,
            "page_count": page_count,
            "total": total,
        },
        "items": items,
        "methodology": {
            "flow": "净流入是封闭 K 线窗口内主动买入额-主动卖出额的 CVD 估算。",
            "missing": "无有效扫描、市值或历史快照时返回 null，不以 0 补齐。",
        },
    }


def load_funds_cockpit(
    settings: Settings,
    *,
    window_sec: int = 3600,
    now_ts: int | None = None,
) -> dict[str, Any]:
    return load_market_cockpit(
        settings,
        window_sec=normalize_window(window_sec),
        board_limit=8,
        now_ts=int(now_ts or time.time()),
    )


__all__ = [
    "ASSET_SORT_KEYS",
    "FUNDS_SCHEMA_VERSION",
    "MARKET_TYPES",
    "build_funds_assets",
    "build_funds_sectors",
    "funds_asset_rows",
    "load_funds_cockpit",
    "normalize_market_type",
]
