from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any
from urllib.parse import quote

from .config import Settings
from .storage import JsonStore


CST = timezone(timedelta(hours=8))


def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or abs(parsed) == float("inf"):
        return None
    return parsed


def _field(value: Any, *, updated_at: int) -> dict[str, Any]:
    return {
        "value": value,
        "source": "CoinGecko",
        "updated_at": updated_at,
    }


def normalize_project_profile(payload: Any, *, updated_at: int) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("invalid_coin_detail_schema")
    market_data = payload.get("market_data")
    market_data = market_data if isinstance(market_data, dict) else {}

    def usd(field_name: str) -> float | None:
        value = market_data.get(field_name)
        if isinstance(value, dict):
            return _finite_number(value.get("usd"))
        return None

    total_supply = _finite_number(market_data.get("total_supply"))
    circulating_supply = _finite_number(market_data.get("circulating_supply"))
    circulating_ratio = (
        circulating_supply / total_supply
        if (
            circulating_supply is not None
            and total_supply is not None
            and circulating_supply >= 0
            and total_supply > 0
        )
        else None
    )
    platforms = payload.get("platforms")
    platforms = platforms if isinstance(platforms, dict) else {}
    chain = ""
    contract = ""
    for platform, address in platforms.items():
        normalized_address = str(address or "").strip()
        if normalized_address:
            chain = str(platform or "").strip()
            contract = normalized_address
            break
    categories = [
        str(item).strip()
        for item in (payload.get("categories") or [])
        if str(item).strip()
    ] if isinstance(payload.get("categories"), list) else []

    values: dict[str, Any] = {
        "current_price_usd": usd("current_price"),
        "circulating_market_cap_usd": usd("market_cap"),
        "fdv_usd": usd("fully_diluted_valuation"),
        "total_supply": total_supply,
        "circulating_supply": circulating_supply,
        "circulating_ratio": circulating_ratio,
        "chain": chain or None,
        "contract_address": contract or None,
        "categories": categories,
    }
    return {
        "status": "ok",
        "coingecko_id": str(payload.get("id") or ""),
        "symbol": str(payload.get("symbol") or "").upper(),
        "fields": {
            key: _field(value, updated_at=updated_at)
            for key, value in values.items()
        },
        "updated_at": updated_at,
        "attribution_note": "项目画像字段来自 CoinGecko；未推断 VC/投资机构关系",
    }


class AnnouncementProjectEnricher:
    def __init__(self, settings: Settings, store: JsonStore, http: Any):
        self.settings = settings
        self.store = store
        self.http = http

    def enrich(
        self,
        alerts: list[dict[str, Any]],
        *,
        now_ts: int | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if not self.settings.announcement_enrichment_enable:
            return alerts, {"status": "disabled", "network_calls": 0}

        observed_at = int(now_ts if now_ts is not None else time.time())
        state = self.store.load(self.settings.announcement_enrichment_cache_path, {})
        if not isinstance(state, dict):
            state = {}
        entries = state.get("symbols")
        entries = entries if isinstance(entries, dict) else {}
        limit = max(0, int(self.settings.announcement_enrichment_candidate_limit))
        symbols: list[str] = []
        for alert in alerts:
            for symbol in alert.get("symbols", []):
                normalized = str(symbol or "").upper().removesuffix("USDT")
                if normalized and normalized not in symbols:
                    symbols.append(normalized)
                if len(symbols) >= limit:
                    break
            if len(symbols) >= limit:
                break

        profiles: dict[str, dict[str, Any]] = {}
        misses: list[str] = []
        ttl = max(60, int(self.settings.announcement_enrichment_cache_ttl_sec))
        for symbol in symbols:
            cached = entries.get(symbol)
            cached = cached if isinstance(cached, dict) else {}
            if cached and observed_at - int(cached.get("fetched_at") or 0) <= ttl:
                profiles[symbol] = {
                    **dict(cached.get("profile") or {}),
                    "cache_status": "hit",
                }
            else:
                misses.append(symbol)

        max_workers = max(
            1,
            min(
                max(1, int(self.settings.announcement_enrichment_max_concurrency)),
                len(misses) or 1,
            ),
        )
        if misses:
            with ThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="announcement-profile",
            ) as executor:
                futures = {
                    executor.submit(self._fetch_profile, symbol, observed_at): symbol
                    for symbol in misses
                }
                for future in as_completed(futures):
                    symbol = futures[future]
                    try:
                        profile = future.result()
                    except Exception as exc:
                        profile = {
                            "status": "degraded",
                            "reason": type(exc).__name__,
                            "updated_at": observed_at,
                        }
                    profiles[symbol] = profile
                    entries[symbol] = {
                        "profile": profile,
                        "fetched_at": observed_at,
                    }

        enriched_alerts: list[dict[str, Any]] = []
        for alert in alerts:
            copied = dict(alert)
            copied["project_profiles"] = {
                symbol: profiles[symbol]
                for raw_symbol in alert.get("symbols", [])
                for symbol in [str(raw_symbol or "").upper().removesuffix("USDT")]
                if symbol in profiles
            }
            enriched_alerts.append(copied)

        state["symbols"] = entries
        state["updated_at"] = observed_at
        retention_cutoff = observed_at - 30 * 24 * 3600
        retained = {
            symbol: value
            for symbol, value in entries.items()
            if (
                isinstance(value, dict)
                and int(value.get("fetched_at") or 0) >= retention_cutoff
            )
        }
        if len(retained) > 500:
            retained = dict(sorted(
                retained.items(),
                key=lambda pair: int(pair[1].get("fetched_at") or 0),
                reverse=True,
            )[:500])
        state["symbols"] = retained
        self.store.save(self.settings.announcement_enrichment_cache_path, state)
        ok_count = sum(
            1 for profile in profiles.values()
            if str(profile.get("status") or "") == "ok"
        )
        return enriched_alerts, {
            "status": "ok" if ok_count == len(profiles) else "degraded",
            "requested": len(symbols),
            "cache_hits": len(symbols) - len(misses),
            "network_symbols": len(misses),
            "profiles_ok": ok_count,
            "max_concurrency": max_workers,
            "updated_at": observed_at,
        }

    def _fetch_profile(self, symbol: str, observed_at: int) -> dict[str, Any]:
        timeout = max(1, int(self.settings.announcement_enrichment_timeout_sec))
        search = self.http.get_json(
            f"{self.settings.coingecko_api_base_url}/search",
            params={"query": symbol},
            quality_key="announcementCoinGeckoSearch",
            timeout=timeout,
            retries=1,
            cache=False,
        )
        coins = search.get("coins") if isinstance(search, dict) else None
        if not isinstance(coins, list):
            return {
                "status": "degraded",
                "reason": "search_unavailable",
                "updated_at": observed_at,
            }
        exact = {
            str(item.get("id") or ""): item
            for item in coins
            if isinstance(item, dict)
            and str(item.get("symbol") or "").upper() == symbol
            and str(item.get("id") or "")
        }
        if not exact:
            return {
                "status": "unmatched",
                "reason": "no_exact_symbol",
                "updated_at": observed_at,
            }
        if len(exact) != 1:
            return {
                "status": "ambiguous",
                "reason": "multiple_exact_symbols",
                "match_count": len(exact),
                "updated_at": observed_at,
            }
        coin_id = next(iter(exact))
        detail = self.http.get_json(
            f"{self.settings.coingecko_api_base_url}/coins/{quote(coin_id, safe='')}",
            params={
                "localization": "false",
                "tickers": "false",
                "market_data": "true",
                "community_data": "false",
                "developer_data": "false",
                "sparkline": "false",
            },
            quality_key="announcementCoinGeckoDetail",
            timeout=timeout,
            retries=1,
            cache=False,
        )
        try:
            return normalize_project_profile(detail, updated_at=observed_at)
        except ValueError:
            return {
                "status": "degraded",
                "reason": "detail_unavailable",
                "updated_at": observed_at,
            }


def _money(value: Any, *, price: bool = False) -> str:
    parsed = _finite_number(value)
    if parsed is None:
        return "暂缺"
    if price:
        if parsed >= 1:
            return f"${parsed:.3g}"
        if parsed >= 0.01:
            return f"${parsed:.4f}"
        return f"${parsed:.6g}"
    if parsed >= 1_000_000_000:
        return f"${parsed / 1_000_000_000:.1f}B"
    if parsed >= 1_000_000:
        return f"${parsed / 1_000_000:.0f}M"
    if parsed >= 1_000:
        return f"${parsed / 1_000:.0f}K"
    return f"${parsed:.0f}"


def _quantity(value: Any) -> str:
    parsed = _finite_number(value)
    if parsed is None:
        return "暂缺"
    absolute = abs(parsed)
    if absolute >= 1_000_000_000:
        return f"{parsed / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{parsed / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{parsed / 1_000:.2f}K"
    return f"{parsed:,.4f}".rstrip("0").rstrip(".")


def format_announcement_profiles(alert: dict[str, Any]) -> list[str]:
    if "project_profiles" not in alert:
        return []
    release_ts = int(
        alert.get("announcement_release_ts")
        or alert.get("release_ts")
        or 0
    )
    release_text = (
        datetime.fromtimestamp(release_ts, CST).strftime("%m-%d %H:%M CST")
        if release_ts > 0
        else "暂缺"
    )
    lines = [
        "",
        "<b>项目画像（可选补充）</b>",
        f"公告发布时间: {release_text}",
    ]
    profiles = alert.get("project_profiles")
    profiles = profiles if isinstance(profiles, dict) else {}
    for symbol, profile_value in profiles.items():
        profile = profile_value if isinstance(profile_value, dict) else {}
        status = str(profile.get("status") or "degraded")
        if status != "ok":
            reason = escape(str(profile.get("reason") or status), quote=False)
            lines.append(f"<b>{escape(str(symbol), quote=False)}</b>: 暂不可用（{reason}）")
            continue
        fields = profile.get("fields")
        fields = fields if isinstance(fields, dict) else {}

        def value(name: str) -> Any:
            record = fields.get(name)
            return record.get("value") if isinstance(record, dict) else None

        ratio = _finite_number(value("circulating_ratio"))
        categories = value("categories")
        categories = categories if isinstance(categories, list) else []
        updated_at = int(profile.get("updated_at") or 0)
        updated_text = (
            datetime.fromtimestamp(updated_at, timezone.utc).strftime("%m-%d %H:%M UTC")
            if updated_at > 0
            else "未知"
        )
        lines.extend([
            f"<b>{escape(str(symbol), quote=False)}</b>",
            (
                f"价格 {_money(value('current_price_usd'), price=True)} · "
                f"流通市值 {_money(value('circulating_market_cap_usd'))} · "
                f"FDV {_money(value('fdv_usd'))}"
            ),
            (
                f"供应: 流通 {_quantity(value('circulating_supply'))} / "
                f"总量 {_quantity(value('total_supply'))} · "
                f"比例 {f'{ratio * 100:.1f}%' if ratio is not None else '暂缺'}"
            ),
            (
                f"链/合约: {escape(str(value('chain') or '暂缺'), quote=False)} / "
                f"{escape(str(value('contract_address') or '暂缺'), quote=False)}"
            ),
            f"叙事/类别: {escape('、'.join(str(item) for item in categories[:5]) or '暂缺', quote=False)}",
            f"字段来源: CoinGecko · 更新时间: {updated_text}",
        ])
    if not profiles:
        lines.append("没有可安全匹配的项目画像")
    lines.append("VC/投资机构仅在可靠来源明确给出时展示；本模块不作推断。")
    return lines
