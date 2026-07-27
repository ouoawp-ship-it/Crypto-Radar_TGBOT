from __future__ import annotations

import time
from datetime import datetime, timezone
from html import escape
from typing import Any, Callable

from .config import Settings
from .storage import JsonStore


def _number(value: Any) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.0
    return parsed if parsed == parsed and abs(parsed) != float("inf") else 0.0


def five_day_volume_context(
    daily_klines: list[list[Any]],
    *,
    current_24h_quote_volume: float,
    now_ms: int,
    surge_ratio: float = 2.5,
) -> dict[str, Any]:
    rows: dict[int, float] = {}
    for row in daily_klines if isinstance(daily_klines, list) else []:
        if not isinstance(row, (list, tuple)) or len(row) < 8:
            continue
        open_time = int(_number(row[0]))
        close_time = int(_number(row[6]))
        quote_volume = _number(row[7])
        if open_time > 0 and 0 < close_time <= int(now_ms) and quote_volume >= 0:
            rows[open_time] = quote_volume
    values = [rows[key] for key in sorted(rows)][-5:]
    baseline = sum(values) / len(values) if len(values) == 5 else 0.0
    ratio = (
        float(current_24h_quote_volume) / baseline
        if baseline > 0 and current_24h_quote_volume >= 0
        else 0.0
    )
    return {
        "ready": len(values) == 5 and baseline > 0,
        "complete_days": len(values),
        "baseline_5d_quote_volume": round(baseline, 2),
        "current_24h_quote_volume": round(float(current_24h_quote_volume), 2),
        "volume_ratio": round(ratio, 4),
        "volume_surge": len(values) == 5 and ratio >= float(surge_ratio),
        "source": "Binance USDⓈ-M Futures 24h ticker + 已闭合1d K线",
    }


def _trending_symbols(payload: Any) -> set[str]:
    if not isinstance(payload, dict) or not isinstance(payload.get("coins"), list):
        raise ValueError("invalid_trending_schema")
    result: set[str] = set()
    for wrapper in payload["coins"]:
        item = wrapper.get("item") if isinstance(wrapper, dict) else None
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if symbol:
            result.add(symbol)
    return result


def _collect_text(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_collect_text(item))
        return result
    if isinstance(value, dict):
        result = []
        for key, item in value.items():
            if str(key).lower() in {"title", "content", "body", "description"}:
                result.extend(_collect_text(item))
            elif isinstance(item, (dict, list)):
                result.extend(_collect_text(item))
        return result
    return []


class HeatContextEnricher:
    def __init__(self, settings: Settings, store: JsonStore, http: Any):
        self.settings = settings
        self.store = store
        self.http = http

    def enrich(
        self,
        items: list[dict[str, Any]],
        *,
        now_ts: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self.settings.heat_context_enable:
            return items, {"status": "disabled", "network_calls": 0}

        observed_at = int(now_ts if now_ts is not None else time.time())
        state = self.store.load(self.settings.heat_context_cache_path, {})
        if not isinstance(state, dict):
            state = {}
        network_calls = 0
        trending, trending_status, trending_updated, called = self._load_trending(
            state,
            observed_at,
        )
        network_calls += called
        square_texts: list[str] = []
        square_status = "disabled"
        square_updated = 0
        if self.settings.binance_square_heat_enable:
            square_texts, square_status, square_updated, called = self._load_square(
                state,
                observed_at,
            )
            network_calls += called

        limit = max(0, int(self.settings.heat_context_candidate_limit))
        enriched: list[dict[str, Any]] = []
        for index, item in enumerate(items):
            copied = dict(item)
            if index >= limit:
                enriched.append(copied)
                continue
            coin = str(item.get("coin") or item.get("symbol") or "").upper()
            if coin.endswith("USDT"):
                coin = coin[:-4]
            square_mentions = 0
            if self.settings.binance_square_heat_enable and index < max(
                0,
                int(self.settings.binance_square_heat_candidate_limit),
            ):
                square_mentions = sum(
                    1
                    for text in square_texts
                    if coin and coin in str(text).upper()
                )
            volume = item.get("volume_context")
            volume = dict(volume) if isinstance(volume, dict) else {}
            degraded = trending_status not in {"ok", "cache_hit"}
            if self.settings.binance_square_heat_enable:
                degraded = degraded or square_status not in {"ok", "cache_hit"}
            copied["heat_context"] = {
                "coingecko_trending": coin in trending,
                "binance_square_mentions": square_mentions,
                "volume_ratio": _number(volume.get("volume_ratio")),
                "volume_surge": bool(volume.get("volume_surge")),
                "sources": [
                    {
                        "name": "CoinGecko Trending",
                        "status": trending_status,
                        "updated_at": trending_updated,
                    },
                    {
                        "name": "Binance成交量",
                        "status": "ok" if volume.get("ready") else "insufficient_history",
                        "updated_at": observed_at,
                    },
                    *(
                        [{
                            "name": "Binance Square（非公开接口）",
                            "status": square_status,
                            "updated_at": square_updated,
                        }]
                        if self.settings.binance_square_heat_enable
                        else []
                    ),
                ],
                "updated_at": max(trending_updated, square_updated, observed_at),
                "degraded": degraded,
                "context_only": True,
            }
            enriched.append(copied)

        state["updated_at"] = observed_at
        self.store.save(self.settings.heat_context_cache_path, state)
        degraded_statuses = {"degraded", "stale_degraded"}
        overall_degraded = trending_status in degraded_statuses or (
            self.settings.binance_square_heat_enable
            and square_status in degraded_statuses
        )
        return enriched, {
            "status": "degraded" if overall_degraded else "ok",
            "trending_status": trending_status,
            "square_status": square_status,
            "candidate_limit": limit,
            "network_calls": network_calls,
            "updated_at": observed_at,
        }

    def _load_trending(
        self,
        state: dict[str, Any],
        now_ts: int,
    ) -> tuple[set[str], str, int, int]:
        cached = state.get("coingecko_trending")
        cached = cached if isinstance(cached, dict) else {}
        fetched_at = int(cached.get("fetched_at") or 0)
        ttl = max(60, int(self.settings.heat_context_cache_ttl_sec))
        symbols = {
            str(symbol).upper()
            for symbol in (cached.get("symbols") or [])
            if str(symbol).strip()
        }
        if fetched_at > 0 and now_ts - fetched_at <= ttl:
            return symbols, "cache_hit", fetched_at, 0
        payload = self.http.get_json(
            f"{self.settings.coingecko_api_base_url}/search/trending",
            quality_key="coingeckoTrending",
            timeout=max(1, int(self.settings.heat_context_timeout_sec)),
            retries=1,
            cache=False,
        )
        try:
            fresh = _trending_symbols(payload)
        except ValueError:
            if symbols:
                return symbols, "stale_degraded", fetched_at, 1
            return set(), "degraded", 0, 1
        state["coingecko_trending"] = {
            "symbols": sorted(fresh),
            "fetched_at": now_ts,
        }
        return fresh, "ok", now_ts, 1

    def _load_square(
        self,
        state: dict[str, Any],
        now_ts: int,
    ) -> tuple[list[str], str, int, int]:
        cached = state.get("binance_square")
        cached = cached if isinstance(cached, dict) else {}
        fetched_at = int(cached.get("fetched_at") or 0)
        ttl = max(60, int(self.settings.heat_context_cache_ttl_sec))
        texts = [str(item) for item in (cached.get("texts") or []) if str(item).strip()]
        if texts and now_ts - fetched_at <= ttl:
            return texts, "cache_hit", fetched_at, 0
        payload = self.http.get_json(
            "https://www.binance.com/bapi/composite/v1/public/cms/article/list/query",
            params={"type": 1, "pageNo": 1, "pageSize": 20},
            quality_key="binanceSquareHeat",
            timeout=max(1, int(self.settings.heat_context_timeout_sec)),
            retries=1,
            cache=False,
        )
        fresh = _collect_text(payload)
        if not fresh:
            if texts:
                return texts, "stale_degraded", fetched_at, 1
            return [], "degraded", 0, 1
        state["binance_square"] = {"texts": fresh[:100], "fetched_at": now_ts}
        return fresh, "ok", now_ts, 1


def heat_context_time_text(timestamp: int) -> str:
    if timestamp <= 0:
        return "未知"
    return datetime.fromtimestamp(timestamp, timezone.utc).strftime("%m-%d %H:%M UTC")


def format_heat_context_lines(
    items: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    *,
    top_n: int,
    square_enabled: bool,
    link_formatter: Callable[[dict[str, Any]], str],
) -> list[str]:
    lines = ["<blockquote><b>🌡️ 轻量热度上下文（只解释候选，不触发信号）</b></blockquote>"]
    selected = [
        item
        for item in items
        if isinstance(item.get("heat_context"), dict)
        and (
            item["heat_context"].get("coingecko_trending")
            or item["heat_context"].get("volume_surge")
            or _number(item["heat_context"].get("binance_square_mentions")) > 0
        )
    ][: max(1, int(top_n))]
    if not selected:
        lines.append("本轮已有候选中暂无显著热度标签")
    for item in selected:
        context = item["heat_context"]
        tags: list[str] = []
        if context.get("coingecko_trending"):
            tags.append("CoinGecko热榜")
        if context.get("volume_surge"):
            tags.append(f"24h量能{_number(context.get('volume_ratio')):.2f}x")
        mentions = int(_number(context.get("binance_square_mentions")))
        if mentions > 0:
            tags.append(f"Square提及{mentions}")
        lines.extend([link_formatter(item), escape(" · ".join(tags), quote=False)])
    source_text = "来源: CoinGecko Trending + Binance 24h/已闭合日K"
    if square_enabled:
        source_text += " + Binance Square（可选）"
    lines.append(escape(source_text, quote=False))
    status = str(diagnostics.get("status") or "unknown")
    lines.append(escape(
        f"更新时间: {heat_context_time_text(int(diagnostics.get('updated_at') or 0))} · "
        f"状态: {'降级' if status == 'degraded' else '正常'}",
        quote=False,
    ))
    lines.append("")
    return lines
