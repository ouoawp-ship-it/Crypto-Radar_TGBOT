from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol, Sequence

import requests

from .config import OnchainSettings
from .constants import BASE_CHAIN_ID
from .db import OnchainStore
from .labels import normalize_evm_address
from .models import PriceQuote


class PriceProvider(Protocol):
    def quote_many(
        self, chain_id: int, token_addresses: Sequence[str]
    ) -> dict[str, PriceQuote]:
        ...


class PriceConfigurationError(ValueError):
    pass


def _market_timestamp(value: object) -> int:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = int(value)
        if timestamp > 0:
            return timestamp
        raise ValueError("market timestamp must be positive")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("market timestamp is missing")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    timestamp = int(parsed.timestamp())
    if timestamp <= 0:
        raise ValueError("market timestamp must be positive")
    return timestamp


class StaticPriceProvider:
    def __init__(self, quotes: Mapping[tuple[int, str], PriceQuote]):
        self.quotes = {
            (chain_id, address.lower()): quote
            for (chain_id, address), quote in quotes.items()
        }

    def quote_many(
        self, chain_id: int, token_addresses: Sequence[str]
    ) -> dict[str, PriceQuote]:
        return {
            address.lower(): quote
            for address in token_addresses
            if (
                quote := self.quotes.get((chain_id, address.lower()))
            )
            is not None
        }


class CoinGeckoOnchainPriceProvider:
    def __init__(
        self,
        settings: OnchainSettings,
        *,
        session: Any | None = None,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.session = session or requests.Session()
        self.clock = clock
        self.calls: deque[float] = deque()
        self.last_status = "not_called"

    def quote_many(
        self, chain_id: int, token_addresses: Sequence[str]
    ) -> dict[str, PriceQuote]:
        if chain_id != BASE_CHAIN_ID:
            self.last_status = "unsupported_chain"
            return {}
        if not self.settings.coingecko_api_key:
            self.last_status = "missing_api_key"
            return {}
        now = self.clock()
        cutoff = now - 60
        while self.calls and self.calls[0] <= cutoff:
            self.calls.popleft()
        if len(self.calls) >= self.settings.price_rate_limit_per_minute:
            self.last_status = "rate_limited"
            return {}
        normalized = [
            normalize_evm_address(address) for address in token_addresses
        ][: self.settings.price_batch_size]
        if not normalized:
            return {}
        self.calls.append(now)
        endpoint = (
            self.settings.coingecko_api_base_url
            + "/onchain/networks/base/tokens/multi/"
            + ",".join(normalized)
        )
        try:
            response = self.session.get(
                endpoint,
                headers={"x-cg-pro-api-key": self.settings.coingecko_api_key},
                timeout=float(self.settings.rpc_timeout_sec),
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                self.last_status = "provider_error"
                return {}
            payload = response.json()
        except (requests.RequestException, ValueError):
            self.last_status = "provider_error"
            return {}
        data = payload.get("data") if isinstance(payload, dict) else None
        records = data if isinstance(data, list) else [data]
        quotes: dict[str, PriceQuote] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            attributes = record.get("attributes")
            if not isinstance(attributes, dict):
                continue
            address = str(attributes.get("address") or "").lower()
            if address not in normalized:
                continue
            try:
                price = Decimal(str(attributes["price_usd"]))
                volume_data = attributes.get("volume_usd")
                volume_raw = (
                    volume_data.get("h24")
                    if isinstance(volume_data, dict)
                    else None
                )
                volume = (
                    Decimal(str(volume_raw))
                    if volume_raw not in {None, ""}
                    else None
                )
                market_observed_at = _market_timestamp(
                    attributes["last_trade_timestamp"]
                )
            except (
                KeyError,
                InvalidOperation,
                TypeError,
                ValueError,
                OverflowError,
            ):
                continue
            if not price.is_finite() or price <= 0:
                continue
            if volume is not None and (
                not volume.is_finite() or volume < 0
            ):
                continue
            quotes[address] = PriceQuote(
                chain_id=chain_id,
                token_address=address,
                price_usd=price,
                volume_24h_usd=volume,
                source="coingecko_onchain",
                observed_at=market_observed_at,
                market_observed_at=market_observed_at,
                fetched_at=int(now),
            )
        self.last_status = "ok" if quotes else "unpriced"
        return quotes


class CachedPriceService:
    def __init__(
        self,
        settings: OnchainSettings,
        store: OnchainStore,
        provider: PriceProvider | None,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.settings = settings
        self.store = store
        self.provider = provider
        self.clock = clock

    def quotes(
        self,
        chain_id: int,
        token_addresses: Sequence[str],
        *,
        force_refresh: bool = False,
    ) -> dict[str, PriceQuote]:
        now = int(self.clock())
        result: dict[str, PriceQuote] = {}
        missing: list[str] = []
        for raw_address in token_addresses:
            address = normalize_evm_address(raw_address)
            cached = self.store.cached_price(chain_id, address)
            if (
                not force_refresh
                and cached is not None
                and now - cached.freshness_timestamp
                <= self.settings.price_max_age_sec
            ):
                result[address] = cached
            else:
                missing.append(address)
        if self.provider is not None and missing:
            for index in range(0, len(missing), self.settings.price_batch_size):
                batch = missing[index : index + self.settings.price_batch_size]
                fresh = self.provider.quote_many(chain_id, batch)
                for address, quote in fresh.items():
                    self.store.cache_price(quote)
                    if (
                        now - quote.freshness_timestamp
                        <= self.settings.price_max_age_sec
                    ):
                        result[address] = quote
        if force_refresh:
            for address in missing:
                if address in result:
                    continue
                cached = self.store.cached_price(chain_id, address)
                if (
                    cached is not None
                    and now - cached.freshness_timestamp
                    <= self.settings.price_max_age_sec
                ):
                    result[address] = cached
        return result


def build_price_provider(
    settings: OnchainSettings,
) -> PriceProvider | None:
    if not settings.price_enable or settings.price_provider == "none":
        return None
    if settings.price_provider == "coingecko_onchain":
        return CoinGeckoOnchainPriceProvider(settings)
    if settings.price_provider == "static":
        raise PriceConfigurationError(
            "static price provider requires an injected fixture provider"
        )
    return None
