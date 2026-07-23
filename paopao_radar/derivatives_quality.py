from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Any, Iterable

from .config import Settings
from .data_sources import HttpClient


COINGLASS_OI_CHANGE_FIELDS = {
    "5m": "open_interest_change_percent_5m",
    "15m": "open_interest_change_percent_15m",
    "30m": "open_interest_change_percent_30m",
    "1h": "open_interest_change_percent_1h",
    "4h": "open_interest_change_percent_4h",
    "24h": "open_interest_change_percent_24h",
}
COINALYZE_INTERVALS = {
    "5m": "5min",
    "15m": "15min",
    "30m": "30min",
    "1h": "1hour",
    "4h": "4hour",
    "24h": "daily",
}

_RATE_LIMIT_LOCK = Lock()
_RATE_LIMIT_EVENTS: dict[str, deque[float]] = defaultdict(deque)


def _reserve_requests(provider: str, *, limit: int, cost: int = 1) -> bool:
    """Reserve a non-blocking rolling-minute request budget for one provider."""
    safe_limit = max(1, int(limit))
    safe_cost = max(1, int(cost))
    now = time.monotonic()
    with _RATE_LIMIT_LOCK:
        events = _RATE_LIMIT_EVENTS[provider]
        while events and now - events[0] >= 60:
            events.popleft()
        if len(events) + safe_cost > safe_limit:
            return False
        events.extend([now] * safe_cost)
    return True


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or abs(number) == float("inf"):
        return None
    return number


def _timestamp_seconds(value: Any) -> int:
    parsed = _number(value)
    if parsed is None or parsed <= 0:
        return 0
    if parsed > 10_000_000_000:
        parsed /= 1000
    return int(parsed)


def base_asset(symbol: str) -> str:
    value = str(symbol or "").strip().upper()
    if value.endswith(".P"):
        value = value[:-2]
    for quote in ("USDT", "USDC", "BUSD", "USD"):
        if value.endswith(quote) and len(value) > len(quote):
            return value[: -len(quote)]
    return value


def source_agreement(
    first: float | None,
    second: float | None,
    *,
    neutral_abs: float,
) -> dict[str, Any]:
    if first is None and second is None:
        return {"status": "missing", "score": 0, "relative_difference": None, "gate": "degraded"}
    if first is None or second is None:
        return {"status": "single_source", "score": 50, "relative_difference": None, "gate": "degraded"}

    a = float(first)
    b = float(second)
    threshold = max(1e-9, float(neutral_abs))
    abs_a = abs(a)
    abs_b = abs(b)
    if abs_a <= threshold and abs_b <= threshold:
        return {"status": "high", "score": 100, "relative_difference": 0.0, "gate": "allow"}
    if (a > threshold and b < -threshold) or (a < -threshold and b > threshold):
        relative = abs(a - b) / max(abs_a, abs_b, threshold)
        return {
            "status": "conflict",
            "score": 10,
            "relative_difference": round(relative, 4),
            "gate": "block",
        }
    if min(abs_a, abs_b) <= threshold < max(abs_a, abs_b):
        relative = abs(a - b) / max(abs_a, abs_b, threshold)
        return {
            "status": "low",
            "score": 55,
            "relative_difference": round(relative, 4),
            "gate": "degraded",
        }

    relative = abs(a - b) / max(abs_a, abs_b, threshold)
    if relative <= 0.10:
        status, score, gate = "high", 100, "allow"
    elif relative <= 0.25:
        status, score, gate = "medium", 80, "allow"
    else:
        status, score, gate = "low", 55, "degraded"
    return {
        "status": status,
        "score": score,
        "relative_difference": round(relative, 4),
        "gate": gate,
    }


class CoinGlassClient:
    def __init__(self, settings: Settings, http: HttpClient):
        self.settings = settings
        self.http = http

    @property
    def available(self) -> bool:
        return bool(self.settings.coinglass_enable and self.settings.coinglass_api_key)

    @property
    def headers(self) -> dict[str, str]:
        return {"CG-API-KEY": self.settings.coinglass_api_key}

    @staticmethod
    def _data(payload: Any) -> Any:
        if not isinstance(payload, dict) or str(payload.get("code")) != "0":
            return None
        return payload.get("data")

    def oi_snapshot(self, symbol: str, *, now_ts: int | None = None) -> dict[str, Any] | None:
        if not self.available:
            return None
        if not _reserve_requests(
            "coinglass",
            limit=self.settings.coinglass_rate_limit_per_minute,
        ):
            return None
        coin = base_asset(symbol)
        payload = self.http.get_json(
            f"{self.settings.coinglass_api_base_url}/api/futures/open-interest/exchange-list",
            params={"symbol": coin},
            cache_key=f"coinglass:oi:exchange-list:{coin}",
            quality_key="coinglass:open_interest",
            retries=1,
            headers=self.headers,
        )
        rows = self._data(payload)
        if not isinstance(rows, list):
            return None
        aggregate = next(
            (
                row for row in rows
                if isinstance(row, dict) and str(row.get("exchange") or "").strip().lower() == "all"
            ),
            None,
        )
        if not isinstance(aggregate, dict):
            return None
        changes = {
            timeframe: _number(aggregate.get(field))
            for timeframe, field in COINGLASS_OI_CHANGE_FIELDS.items()
        }
        return {
            "provider": "coinglass",
            "scope": "aggregate_all_exchanges",
            "symbol": f"{coin}USDT",
            "observed_at": int(now_ts or time.time()),
            "oi_usd": _number(aggregate.get("open_interest_usd")),
            "changes": changes,
        }

    def funding_snapshots(
        self,
        symbols: Iterable[str],
        *,
        now_ts: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        targets = {base_asset(symbol): str(symbol).upper() for symbol in symbols if symbol}
        if not self.available or not targets:
            return {}
        if not _reserve_requests(
            "coinglass",
            limit=self.settings.coinglass_rate_limit_per_minute,
        ):
            return {}
        payload = self.http.get_json(
            f"{self.settings.coinglass_api_base_url}/api/futures/funding-rate/exchange-list",
            cache_key="coinglass:funding:exchange-list",
            quality_key="coinglass:funding_rate",
            retries=1,
            headers=self.headers,
        )
        rows = self._data(payload)
        if not isinstance(rows, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in rows:
            if not isinstance(item, dict):
                continue
            coin = str(item.get("symbol") or "").upper()
            target = targets.get(coin)
            if not target:
                continue
            stable = item.get("stablecoin_margin_list")
            if not isinstance(stable, list):
                continue
            binance = next(
                (
                    row for row in stable
                    if isinstance(row, dict) and str(row.get("exchange") or "").strip().lower() == "binance"
                ),
                None,
            )
            if not isinstance(binance, dict):
                continue
            result[target] = {
                "provider": "coinglass",
                "scope": "binance_stablecoin_perpetual",
                "symbol": target,
                "observed_at": int(now_ts or time.time()),
                # CoinGlass V4 exposes funding_rate in percentage points.
                "funding_pct": _number(binance.get("funding_rate")),
                "interval_hours": int(_number(binance.get("funding_rate_interval")) or 0),
                "next_funding_time_ms": int(_number(binance.get("next_funding_time")) or 0),
            }
        return result


class CoinalyzeClient:
    def __init__(self, settings: Settings, http: HttpClient):
        self.settings = settings
        self.http = http
        self._market_map: dict[str, str] | None = None

    @property
    def available(self) -> bool:
        return bool(self.settings.coinalyze_enable and self.settings.coinalyze_api_key)

    @property
    def headers(self) -> dict[str, str]:
        return {"api_key": self.settings.coinalyze_api_key}

    def market_map(self) -> dict[str, str]:
        if self._market_map is not None:
            return dict(self._market_map)
        self._market_map = {}
        if not self.available:
            return {}
        if not _reserve_requests(
            "coinalyze",
            limit=self.settings.coinalyze_rate_limit_per_minute,
        ):
            return {}
        payload = self.http.get_json(
            f"{self.settings.coinalyze_base_url}/future-markets",
            cache_key="coinalyze:future-markets",
            quality_key="coinalyze:future_markets",
            retries=1,
            headers=self.headers,
        )
        if not isinstance(payload, list):
            return {}
        candidates: dict[str, tuple[int, str]] = {}
        for item in payload:
            if not isinstance(item, dict) or item.get("is_perpetual") is not True:
                continue
            provider_symbol = str(item.get("symbol") or "").upper()
            exchange_symbol = str(item.get("symbol_on_exchange") or "").upper()
            exchange = str(item.get("exchange") or "").upper()
            quote = str(item.get("quote_asset") or "").upper()
            margined = str(item.get("margined") or "").upper()
            if not provider_symbol or not exchange_symbol or quote != "USDT":
                continue
            if exchange not in {"A", "BINANCE"}:
                continue
            priority = 0 if margined == "STABLE" else 1
            previous = candidates.get(exchange_symbol)
            if previous is None or priority < previous[0]:
                candidates[exchange_symbol] = (priority, provider_symbol)
        self._market_map = {symbol: item[1] for symbol, item in candidates.items()}
        return dict(self._market_map)

    def _provider_symbols(self, symbols: Iterable[str]) -> tuple[dict[str, str], list[str]]:
        market_map = self.market_map()
        reverse: dict[str, str] = {}
        providers: list[str] = []
        for raw in symbols:
            symbol = str(raw or "").upper()
            provider = market_map.get(symbol)
            if not provider:
                continue
            reverse[provider] = symbol
            providers.append(provider)
        return reverse, providers[:20]

    def oi_snapshots(
        self,
        symbols: Iterable[str],
        *,
        timeframe: str,
        now_ts: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not self.available:
            return {}
        reverse, provider_symbols = self._provider_symbols(symbols)
        interval = COINALYZE_INTERVALS.get(timeframe)
        if not provider_symbols or not interval:
            return {}
        if not _reserve_requests(
            "coinalyze",
            limit=self.settings.coinalyze_rate_limit_per_minute,
            cost=len(provider_symbols),
        ):
            return {}
        now = int(now_ts or time.time())
        seconds = {"5m": 300, "15m": 900, "30m": 1800, "1h": 3600, "4h": 14400, "24h": 86400}[timeframe]
        payload = self.http.get_json(
            f"{self.settings.coinalyze_base_url}/open-interest-history",
            params={
                "symbols": ",".join(provider_symbols),
                "interval": interval,
                "from": now - seconds * 3,
                "to": now,
                "convert_to_usd": "true",
            },
            cache_key=f"coinalyze:oi:{timeframe}:{','.join(provider_symbols)}:{now // max(60, seconds)}",
            quality_key="coinalyze:open_interest",
            retries=1,
            headers=self.headers,
        )
        if not isinstance(payload, list):
            return {}
        result: dict[str, dict[str, Any]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            provider_symbol = str(item.get("symbol") or "").upper()
            symbol = reverse.get(provider_symbol)
            history = item.get("history")
            if not symbol or not isinstance(history, list):
                continue
            valid = [row for row in history if isinstance(row, dict) and _number(row.get("c")) is not None]
            valid.sort(key=lambda row: _timestamp_seconds(row.get("t")))
            if len(valid) < 2:
                continue
            previous = _number(valid[-2].get("c"))
            current = _number(valid[-1].get("c"))
            if previous is None or current is None or previous <= 0:
                continue
            result[symbol] = {
                "provider": "coinalyze",
                "scope": "binance_usdt_perpetual",
                "symbol": symbol,
                "provider_symbol": provider_symbol,
                "observed_at": _timestamp_seconds(valid[-1].get("t")) or now,
                "oi_usd": current,
                "change_pct": (current - previous) / previous * 100,
            }
        return result

    def funding_snapshots(
        self,
        symbols: Iterable[str],
        *,
        now_ts: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        if not self.available:
            return {}
        reverse, provider_symbols = self._provider_symbols(symbols)
        if not provider_symbols:
            return {}
        request_cost = len(provider_symbols)
        if not _reserve_requests(
            "coinalyze",
            limit=self.settings.coinalyze_rate_limit_per_minute,
            cost=request_cost * 2,
        ):
            return {}
        common = {
            "params": {"symbols": ",".join(provider_symbols)},
            "retries": 1,
            "headers": self.headers,
        }
        current = self.http.get_json(
            f"{self.settings.coinalyze_base_url}/funding-rate",
            cache_key=f"coinalyze:funding:{','.join(provider_symbols)}",
            quality_key="coinalyze:funding_rate",
            **common,
        )
        predicted = self.http.get_json(
            f"{self.settings.coinalyze_base_url}/predicted-funding-rate",
            cache_key=f"coinalyze:predicted-funding:{','.join(provider_symbols)}",
            quality_key="coinalyze:predicted_funding_rate",
            **common,
        )
        current_map = {
            str(item.get("symbol") or "").upper(): item
            for item in current if isinstance(item, dict)
        } if isinstance(current, list) else {}
        predicted_map = {
            str(item.get("symbol") or "").upper(): item
            for item in predicted if isinstance(item, dict)
        } if isinstance(predicted, list) else {}
        now = int(now_ts or time.time())
        result: dict[str, dict[str, Any]] = {}
        for provider_symbol, symbol in reverse.items():
            current_item = current_map.get(provider_symbol, {})
            predicted_item = predicted_map.get(provider_symbol, {})
            current_value = _number(current_item.get("value"))
            predicted_value = _number(predicted_item.get("value"))
            if current_value is None and predicted_value is None:
                continue
            current_pct = current_value * 100 if current_value is not None else None
            predicted_pct = predicted_value * 100 if predicted_value is not None else None
            result[symbol] = {
                "provider": "coinalyze",
                "scope": "binance_usdt_perpetual",
                "symbol": symbol,
                "provider_symbol": provider_symbol,
                "observed_at": _timestamp_seconds(current_item.get("update")) or now,
                "funding_pct": current_pct,
                "predicted_funding_pct": predicted_pct,
                "funding_acceleration_pct": (
                    predicted_pct - current_pct
                    if current_pct is not None and predicted_pct is not None
                    else None
                ),
            }
        return result


class DerivativesQualityService:
    def __init__(self, settings: Settings, http: HttpClient):
        self.settings = settings
        self.coinglass = CoinGlassClient(settings, http)
        self.coinalyze = CoinalyzeClient(settings, http)

    @property
    def configured(self) -> bool:
        return self.coinglass.available or self.coinalyze.available

    def _limited_symbols(self, rows: Iterable[dict[str, Any]]) -> list[str]:
        limit = max(1, min(20, int(self.settings.derivatives_validation_symbol_limit)))
        result: list[str] = []
        for row in rows:
            symbol = str(row.get("symbol") or "").upper()
            if symbol and symbol not in result:
                result.append(symbol)
            if len(result) >= limit:
                break
        return result

    @staticmethod
    def _comparison(
        values: dict[str, float | None],
        *,
        neutral_abs: float,
    ) -> tuple[dict[str, Any], list[str]]:
        if values.get("coinglass") is not None and values.get("coinalyze") is not None:
            pair = ["coinglass", "coinalyze"]
        elif values.get("coinglass") is not None and values.get("binance") is not None:
            pair = ["coinglass", "binance"]
        elif values.get("coinalyze") is not None and values.get("binance") is not None:
            pair = ["coinalyze", "binance"]
        else:
            available = [name for name, value in values.items() if value is not None]
            pair = available[:1]
        first = values.get(pair[0]) if pair else None
        second = values.get(pair[1]) if len(pair) > 1 else None
        return source_agreement(first, second, neutral_abs=neutral_abs), pair

    def validate_oi_rows(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        timeframe: str,
        local_field: str,
        now_ts: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        materialized = [row for row in rows if isinstance(row, dict)]
        symbols = self._limited_symbols(materialized)
        if not symbols:
            return {}
        now = int(now_ts or time.time())
        coinglass = {
            symbol: snapshot
            for symbol in symbols
            if (snapshot := self.coinglass.oi_snapshot(symbol, now_ts=now)) is not None
        }
        coinalyze = self.coinalyze.oi_snapshots(symbols, timeframe=timeframe, now_ts=now)
        by_symbol = {str(row.get("symbol") or "").upper(): row for row in materialized}
        result: dict[str, dict[str, Any]] = {}
        for symbol in symbols:
            local = _number(by_symbol.get(symbol, {}).get(local_field))
            cg_snapshot = coinglass.get(symbol, {})
            ca_snapshot = coinalyze.get(symbol, {})
            cg_change = _number(cg_snapshot.get("changes", {}).get(timeframe)) if cg_snapshot else None
            ca_change = _number(ca_snapshot.get("change_pct")) if ca_snapshot else None
            source_ages = {
                "coinglass": max(0, now - _timestamp_seconds(cg_snapshot.get("observed_at"))) if cg_snapshot else None,
                "coinalyze": max(0, now - _timestamp_seconds(ca_snapshot.get("observed_at"))) if ca_snapshot else None,
            }
            timeframe_sec = {
                "5m": 300,
                "15m": 900,
                "30m": 1800,
                "1h": 3600,
                "4h": 14400,
                "24h": 86400,
            }[timeframe]
            max_age = max(300, timeframe_sec * 3)
            stale_sources = [
                source for source, age in source_ages.items()
                if age is not None and age > max_age
            ]
            if "coinglass" in stale_sources:
                cg_change = None
            if "coinalyze" in stale_sources:
                ca_change = None
            values = {"binance": local, "coinglass": cg_change, "coinalyze": ca_change}
            agreement, compared = self._comparison(values, neutral_abs=0.10)
            selected = cg_change if cg_change is not None else local if local is not None else ca_change
            primary = "coinglass" if cg_change is not None else "binance" if local is not None else "coinalyze" if ca_change is not None else ""
            status = agreement["status"]
            if not self.configured:
                status = "not_configured"
                agreement = {"status": status, "score": 35, "relative_difference": None, "gate": "degraded"}
            result[symbol] = {
                "metric": "open_interest_change_pct",
                "timeframe": timeframe,
                "status": status,
                "score": agreement["score"],
                "gate": agreement["gate"],
                "compared_sources": compared,
                "relative_difference": agreement["relative_difference"],
                "source_values": values,
                "source_scopes": {
                    "binance": "binance_usdt_perpetual",
                    "coinglass": cg_snapshot.get("scope") if cg_snapshot else "",
                    "coinalyze": ca_snapshot.get("scope") if ca_snapshot else "",
                },
                "source_ages_sec": source_ages,
                "stale_sources": stale_sources,
                "selected_change_pct": selected,
                "primary_source": primary,
                "observed_at": now,
            }
        return result

    def validate_funding_rows(
        self,
        rows: Iterable[dict[str, Any]],
        *,
        now_ts: int | None = None,
    ) -> dict[str, dict[str, Any]]:
        materialized = [row for row in rows if isinstance(row, dict)]
        symbols = self._limited_symbols(materialized)
        if not symbols:
            return {}
        now = int(now_ts or time.time())
        coinglass = self.coinglass.funding_snapshots(symbols, now_ts=now)
        coinalyze = self.coinalyze.funding_snapshots(symbols, now_ts=now)
        result: dict[str, dict[str, Any]] = {}
        for row in materialized:
            symbol = str(row.get("symbol") or "").upper()
            if symbol not in symbols:
                continue
            local_rows = row.get("rows")
            local_binance = next(
                (
                    item for item in local_rows
                    if isinstance(item, dict) and str(item.get("exchange") or "").upper() == "BINANCE"
                ),
                None,
            ) if isinstance(local_rows, list) else None
            local = _number(local_binance.get("funding_pct")) if isinstance(local_binance, dict) else _number(row.get("funding_pct"))
            cg_snapshot = coinglass.get(symbol, {})
            ca_snapshot = coinalyze.get(symbol, {})
            cg_rate = _number(cg_snapshot.get("funding_pct")) if cg_snapshot else None
            ca_rate = _number(ca_snapshot.get("funding_pct")) if ca_snapshot else None
            source_ages = {
                "coinglass": max(0, now - _timestamp_seconds(cg_snapshot.get("observed_at"))) if cg_snapshot else None,
                "coinalyze": max(0, now - _timestamp_seconds(ca_snapshot.get("observed_at"))) if ca_snapshot else None,
            }
            max_age = 30 * 60
            stale_sources = [
                source for source, age in source_ages.items()
                if age is not None and age > max_age
            ]
            if "coinglass" in stale_sources:
                cg_rate = None
            if "coinalyze" in stale_sources:
                ca_rate = None
            values = {"binance": local, "coinglass": cg_rate, "coinalyze": ca_rate}
            agreement, compared = self._comparison(values, neutral_abs=0.005)
            selected = cg_rate if cg_rate is not None else local if local is not None else ca_rate
            primary = "coinglass" if cg_rate is not None else "binance" if local is not None else "coinalyze" if ca_rate is not None else ""
            status = agreement["status"]
            if not self.configured:
                status = "not_configured"
                agreement = {"status": status, "score": 35, "relative_difference": None, "gate": "degraded"}
            result[symbol] = {
                "metric": "funding_rate_pct",
                "status": status,
                "score": agreement["score"],
                "gate": agreement["gate"],
                "compared_sources": compared,
                "relative_difference": agreement["relative_difference"],
                "source_values": values,
                "source_ages_sec": source_ages,
                "stale_sources": stale_sources,
                "selected_funding_pct": selected,
                "primary_source": primary,
                "predicted_funding_pct": _number(ca_snapshot.get("predicted_funding_pct")) if ca_snapshot else None,
                "funding_acceleration_pct": _number(ca_snapshot.get("funding_acceleration_pct")) if ca_snapshot else None,
                "observed_at": now,
            }
        return result

    @staticmethod
    def summary(validations: dict[str, dict[str, Any]]) -> dict[str, Any]:
        counts: dict[str, int] = {}
        blocked: list[str] = []
        for symbol, item in validations.items():
            status = str(item.get("status") or "missing")
            counts[status] = counts.get(status, 0) + 1
            if item.get("gate") == "block":
                blocked.append(symbol)
        return {
            "checked": len(validations),
            "status_counts": counts,
            "blocked_symbols": blocked,
        }


__all__ = [
    "CoinGlassClient",
    "CoinalyzeClient",
    "DerivativesQualityService",
    "base_asset",
    "source_agreement",
]
