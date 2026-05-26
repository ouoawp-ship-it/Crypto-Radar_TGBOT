from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlencode

import requests

from .config import Settings
from .data_sources import DataQuality, HTTP_HEADERS, RequestBudget


class CoinalyzeDataSource:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.quality = DataQuality()
        self.budget = RequestBudget({"coinalyze": settings.coinalyze_request_budget})
        self.cache: dict[str, tuple[float, Any]] = {}

    @property
    def enabled(self) -> bool:
        return bool(self.settings.coinalyze_enable and self.settings.coinalyze_api_key)

    def endpoint(self, path: str) -> str:
        return f"{self.settings.coinalyze_base_url}{path}"

    def get_json(self, path: str, params: dict[str, Any], quality_key: str = "coinalyze") -> Any:
        if not self.enabled:
            self.quality.fail(quality_key, "disabled_or_missing_api_key")
            return None
        if not self.budget.consume("coinalyze"):
            self.quality.fail(quality_key, "budget_exhausted")
            return None
        query = {**params, "api_key": self.settings.coinalyze_api_key}
        cache_query = {key: value for key, value in query.items() if key != "api_key"}
        cache_key = f"coinalyze:{path}:{urlencode(sorted(cache_query.items()))}"
        if self.settings.http_cache_enable:
            cached = self.cache.get(cache_key)
            if cached and time.time() - cached[0] <= self.settings.http_cache_ttl_sec:
                return cached[1]

        last_reason = ""
        for attempt in range(1, self.settings.http_retry + 1):
            try:
                response = requests.get(
                    self.endpoint(path),
                    params=query,
                    headers=HTTP_HEADERS,
                    timeout=self.settings.http_timeout_sec,
                )
                if response.status_code == 200:
                    data = response.json()
                    if self.settings.http_cache_enable:
                        self.cache[cache_key] = (time.time(), data)
                    self.quality.ok(quality_key)
                    return data
                last_reason = f"status={response.status_code}"
                if response.status_code in {401, 403, 418, 429}:
                    break
            except Exception as exc:
                last_reason = type(exc).__name__
            if attempt < self.settings.http_retry:
                time.sleep(self.settings.http_backoff_sec * attempt)
        self.quality.fail(quality_key, last_reason or "unknown")
        return None

    def market_symbol(self, symbol: str) -> str:
        base = symbol.upper()
        if base.endswith("USDT"):
            return f"{base}{self.settings.coinalyze_symbol_suffix}"
        return base

    def liquidation_history(self, symbol: str, from_ts: int, to_ts: int, interval: str = "1hour") -> Any:
        return self.get_json(
            "/liquidation-history",
            {
                "symbols": self.market_symbol(symbol),
                "interval": interval,
                "from": int(from_ts),
                "to": int(to_ts),
                "convert_to_usd": "true",
            },
            quality_key="coinalyzeLiquidationHistory",
        )

    def diagnostics(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "budget": self.budget.snapshot(),
            "quality": self.quality.snapshot(),
        }
