from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import math
from pathlib import Path
import random
from threading import RLock
import time
from typing import Any, Callable, Iterable, Mapping

import requests

from .atomic_json import locked_read_json, locked_update_json


CACHE_SCHEMA_VERSION = 1
CMC_BASE_URL = "https://pro-api.coinmarketcap.com"
CMC_MAP_PATH = "/v1/cryptocurrency/map"
CMC_QUOTES_PATH = "/v3/cryptocurrency/quotes/latest"
MAX_QUOTE_BATCH_SIZE = 100
MAX_MAP_PAGE_SIZE = 5000
MAX_RETRY_SLEEP_SEC = 60.0
DEFAULT_MIN_REQUEST_INTERVAL_SEC = 2.0
_CACHE_FALLBACK_ERROR_KINDS = frozenset({
    "network_error",
    "rate_limit_error",
    "server_error",
})


class CmcClientError(RuntimeError):
    """A deliberately redacted CMC failure suitable for logs and diagnostics."""

    def __init__(
        self,
        kind: str,
        *,
        status_code: int | None = None,
        retry_after_sec: float | None = None,
    ) -> None:
        self.kind = str(kind or "internal_error")
        self.status_code = status_code
        self.retry_after_sec = retry_after_sec
        super().__init__(self._safe_message())

    @property
    def retryable(self) -> bool:
        return self.kind in {"network_error", "rate_limit_error", "server_error"}

    def _safe_message(self) -> str:
        suffix = f" (status={self.status_code})" if self.status_code is not None else ""
        return f"cmc_{self.kind}{suffix}"

    def __str__(self) -> str:
        return self._safe_message()

    def __repr__(self) -> str:
        return (
            f"CmcClientError(kind={self.kind!r}, status_code={self.status_code!r}, "
            f"retry_after_sec={self.retry_after_sec!r})"
        )


@dataclass(frozen=True)
class CmcMapEntry:
    cmc_id: int
    name: str
    symbol: str
    slug: str
    is_active: bool
    token_address: str | None = None
    platform_name: str | None = None
    platform_symbol: str | None = None
    platform_slug: str | None = None


@dataclass(frozen=True)
class CmcQuote:
    cmc_id: int
    name: str
    symbol: str
    slug: str
    market_cap_usd: float | None
    last_updated: str


@dataclass(frozen=True)
class CmcMapResult:
    entries: tuple[CmcMapEntry, ...]
    source: str
    generated_at: str | None
    data_updated_at: str | None
    expires_at: str | None
    request_pages: int

    @property
    def by_id(self) -> dict[int, CmcMapEntry]:
        return {entry.cmc_id: entry for entry in self.entries}


@dataclass(frozen=True)
class CmcQuotesResult:
    quotes: Mapping[int, CmcQuote]
    stale_quotes: Mapping[int, CmcQuote]
    missing_ids: tuple[int, ...]
    source_by_id: Mapping[int, str]
    request_batches: int
    cache_hits: int
    cache_fallbacks: int


def _iso_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _epoch(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.timestamp()


def _positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


class CmcClient:
    """Official CoinMarketCap identity and circulating-market-cap client.

    Quotes are always requested by numeric CMC ID. The on-disk cache contains
    only public asset data and is replaced atomically through ``atomic_json``.
    """

    def __init__(
        self,
        *,
        api_key: str | None,
        cache_path: Path,
        base_url: str = CMC_BASE_URL,
        cache_ttl_sec: int = 300,
        max_data_age_sec: int = 900,
        connect_timeout_sec: float = 5.0,
        read_timeout_sec: float = 15.0,
        retries: int = 2,
        backoff_base_sec: float = 0.5,
        min_request_interval_sec: float = DEFAULT_MIN_REQUEST_INTERVAL_SEC,
        batch_size: int = MAX_QUOTE_BATCH_SIZE,
        map_page_size: int = MAX_MAP_PAGE_SIZE,
        max_map_pages: int = 10,
        session: requests.Session | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
        random_fn: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not 1 <= int(batch_size) <= MAX_QUOTE_BATCH_SIZE:
            raise ValueError(f"batch_size must be between 1 and {MAX_QUOTE_BATCH_SIZE}")
        if not 1 <= int(map_page_size) <= MAX_MAP_PAGE_SIZE:
            raise ValueError(f"map_page_size must be between 1 and {MAX_MAP_PAGE_SIZE}")
        if int(max_map_pages) < 1:
            raise ValueError("max_map_pages must be positive")
        if float(connect_timeout_sec) <= 0 or float(read_timeout_sec) <= 0:
            raise ValueError("CMC timeouts must be positive")
        if int(retries) < 0 or float(backoff_base_sec) < 0:
            raise ValueError("CMC retry settings must be non-negative")
        if (
            not math.isfinite(float(min_request_interval_sec))
            or float(min_request_interval_sec) < 0
        ):
            raise ValueError("CMC request interval must be finite and non-negative")

        self._api_key = str(api_key or "").strip()
        self.cache_path = Path(cache_path)
        self.base_url = str(base_url).rstrip("/")
        self.cache_ttl_sec = max(0, int(cache_ttl_sec))
        self.max_data_age_sec = max(0, int(max_data_age_sec))
        self.connect_timeout_sec = float(connect_timeout_sec)
        self.read_timeout_sec = float(read_timeout_sec)
        self.retries = int(retries)
        self.backoff_base_sec = float(backoff_base_sec)
        self.min_request_interval_sec = float(min_request_interval_sec)
        self.batch_size = int(batch_size)
        self.map_page_size = int(map_page_size)
        self.max_map_pages = int(max_map_pages)
        self._owns_session = session is None
        self.session = session if session is not None else requests.Session()
        self._closed = False
        self._sleep = sleep_fn
        self._random = random_fn
        self._clock = clock
        self._request_attempts = 0
        self._request_batches = 0
        self._cache_hits = 0
        self._cache_misses = 0
        self._last_error_kind = ""
        self._request_slot_lock = RLock()
        self._last_request_started_at: float | None = None
        self._request_clock_floor = float(self._clock())
        self._rate_limit_waits = 0
        self._rate_limit_sleep_sec = 0.0

    def __repr__(self) -> str:
        return (
            "CmcClient("
            f"api_key_configured={bool(self._api_key)!r}, cache_path={str(self.cache_path)!r}, "
            f"batch_size={self.batch_size!r}, "
            f"min_request_interval_sec={self.min_request_interval_sec!r})"
        )

    def __enter__(self) -> CmcClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_session and not self._closed:
            self.session.close()
            self._closed = True

    def diagnostics(self) -> dict[str, Any]:
        return {
            "api_key_configured": bool(self._api_key),
            "request_attempts": self._request_attempts,
            "request_batches": self._request_batches,
            "cache_hits": self._cache_hits,
            "cache_misses": self._cache_misses,
            "last_error": self._last_error_kind,
            "min_request_interval_sec": self.min_request_interval_sec,
            "rate_limit_waits": self._rate_limit_waits,
            "rate_limit_sleep_sec": round(self._rate_limit_sleep_sec, 6),
        }

    def load_map(self, *, cache_only: bool = False) -> CmcMapResult:
        now = self._clock()
        cached = self._cached_map(now)
        if cached is not None:
            self._cache_hits += 1
            return cached
        self._cache_misses += 1
        if cache_only:
            return CmcMapResult((), "cache_miss", None, None, None, 0)

        fallback = self._cached_map(now, allow_expired_ttl=True)

        entries_by_id: dict[int, CmcMapEntry] = {}
        request_pages = 0
        provider_updated_at: str | None = None
        exhausted = True
        try:
            for page_index in range(self.max_map_pages):
                start = page_index * self.map_page_size + 1
                payload = self._request_json(
                    CMC_MAP_PATH,
                    {
                        "listing_status": "active",
                        "start": start,
                        "limit": self.map_page_size,
                        "aux": "platform",
                    },
                )
                request_pages += 1
                data = payload.get("data")
                if not isinstance(data, list):
                    raise self._error("protocol_error")
                for item in data:
                    entry = self._parse_map_entry(item)
                    previous = entries_by_id.get(entry.cmc_id)
                    if previous is not None:
                        raise self._error("protocol_error")
                    entries_by_id[entry.cmc_id] = entry
                timestamp = self._provider_timestamp(payload)
                if timestamp is not None:
                    provider_updated_at = max(provider_updated_at or timestamp, timestamp)
                if len(data) < self.map_page_size:
                    exhausted = False
                    break
            if exhausted:
                raise self._error("protocol_error")
        except CmcClientError as exc:
            self._last_error_kind = exc.kind
            if fallback is not None and exc.kind in _CACHE_FALLBACK_ERROR_KINDS:
                self._cache_hits += 1
                return CmcMapResult(
                    fallback.entries,
                    "fallback_cache",
                    fallback.generated_at,
                    fallback.data_updated_at,
                    fallback.expires_at,
                    request_pages,
                )
            raise

        generated_at = _iso_at(now)
        effective_updated_at = provider_updated_at or generated_at
        updated_epoch = _epoch(effective_updated_at)
        if (
            updated_epoch is None
            or not -300.0 <= now - updated_epoch <= self.max_data_age_sec
        ):
            raise self._error("protocol_error")
        expires_at = _iso_at(now + self.cache_ttl_sec)
        entries = tuple(sorted(entries_by_id.values(), key=lambda item: item.cmc_id))
        self._store_map(entries, generated_at, effective_updated_at, expires_at)
        return CmcMapResult(
            entries,
            "network",
            generated_at,
            effective_updated_at,
            expires_at,
            request_pages,
        )

    def quotes_latest(
        self,
        cmc_ids: Iterable[int],
        *,
        cache_only: bool = False,
    ) -> CmcQuotesResult:
        requested = self._validated_ids(cmc_ids)
        if not requested:
            return CmcQuotesResult({}, {}, (), {}, 0, 0, 0)

        now = self._clock()
        document = self._cache_document()
        cached_records = self._quote_cache_records(document)
        quotes: dict[int, CmcQuote] = {}
        stale_quotes: dict[int, CmcQuote] = {}
        source_by_id: dict[int, str] = {}
        fallback_quotes: dict[int, CmcQuote] = {}
        network_ids: list[int] = []
        cache_hits = 0

        for cmc_id in requested:
            cached = self._cached_quote(
                cached_records.get(str(cmc_id)),
                now,
                expected_id=cmc_id,
            )
            if cached is None:
                network_ids.append(cmc_id)
                continue
            quote, ttl_fresh, data_fresh = cached
            if ttl_fresh and data_fresh:
                quotes[cmc_id] = quote
                source_by_id[cmc_id] = "cache"
                cache_hits += 1
            else:
                stale_quotes[cmc_id] = quote
                if data_fresh:
                    fallback_quotes[cmc_id] = quote
                network_ids.append(cmc_id)

        self._cache_hits += cache_hits
        self._cache_misses += len(network_ids)
        if cache_only:
            missing = tuple(cmc_id for cmc_id in requested if cmc_id not in quotes)
            return CmcQuotesResult(
                dict(sorted(quotes.items())),
                dict(sorted(stale_quotes.items())),
                missing,
                dict(sorted(source_by_id.items())),
                0,
                cache_hits,
                0,
            )

        request_batches = 0
        cache_fallbacks = 0
        for offset in range(0, len(network_ids), self.batch_size):
            batch = network_ids[offset : offset + self.batch_size]
            try:
                payload = self._request_json(
                    CMC_QUOTES_PATH,
                    {
                        "id": ",".join(map(str, batch)),
                        "convert": "USD",
                        "skip_invalid": "true",
                    },
                )
            except CmcClientError as exc:
                if exc.kind not in _CACHE_FALLBACK_ERROR_KINDS:
                    raise
                remaining_ids = network_ids[offset:]
                fallback_ids = [
                    cmc_id for cmc_id in remaining_ids if cmc_id in fallback_quotes
                ]
                if fallback_ids:
                    for cmc_id in fallback_ids:
                        quotes[cmc_id] = fallback_quotes[cmc_id]
                        source_by_id[cmc_id] = "fallback_cache"
                        stale_quotes.pop(cmc_id, None)
                    cache_fallbacks += len(fallback_ids)
                    self._cache_hits += len(fallback_ids)
                    request_batches += 1
                    break
                if quotes:
                    request_batches += 1
                    break
                raise
            request_batches += 1
            try:
                parsed = self._parse_quotes(payload.get("data"), frozenset(batch))
            except CmcClientError as exc:
                self._last_error_kind = exc.kind
                raise
            fetched_at = self._clock()
            for quote in parsed.values():
                updated_at = _epoch(quote.last_updated)
                if updated_at is None or updated_at > fetched_at + 300.0:
                    raise self._error("protocol_error")
            fresh_for_cache = [
                quote for quote in parsed.values()
                if self._quote_data_fresh(quote, fetched_at)
            ]
            self._store_quotes(fresh_for_cache, fetched_at)
            for cmc_id, quote in parsed.items():
                stale_quotes.pop(cmc_id, None)
                if self._quote_data_fresh(quote, fetched_at):
                    quotes[cmc_id] = quote
                    source_by_id[cmc_id] = "network"
                else:
                    stale_quotes[cmc_id] = quote

        missing = tuple(cmc_id for cmc_id in requested if cmc_id not in quotes)
        return CmcQuotesResult(
            dict(sorted(quotes.items())),
            dict(sorted(stale_quotes.items())),
            missing,
            dict(sorted(source_by_id.items())),
            request_batches,
            cache_hits,
            cache_fallbacks,
        )

    def _request_json(self, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if not self._api_key:
            raise self._error("config_error")
        url = f"{self.base_url}{path}"
        self._request_batches += 1
        for attempt in range(self.retries + 1):
            self._request_attempts += 1
            response: Any = None
            try:
                self._wait_for_request_slot()
                response = self.session.get(
                    url,
                    params=dict(params),
                    headers={
                        "Accept": "application/json",
                        "X-CMC_PRO_API_KEY": self._api_key,
                    },
                    timeout=(self.connect_timeout_sec, self.read_timeout_sec),
                )
            except requests.RequestException:
                error = self._error("network_error")
            else:
                status_code = int(getattr(response, "status_code", 0) or 0)
                if status_code == 200:
                    try:
                        payload = response.json()
                    except (TypeError, ValueError):
                        raise self._error("protocol_error", status_code=200) from None
                    if not isinstance(payload, dict):
                        raise self._error("protocol_error", status_code=200)
                    envelope_error = self._envelope_error(payload)
                    if envelope_error is None:
                        return payload
                    error = envelope_error
                elif status_code == 401:
                    raise self._error("authentication_error", status_code=status_code)
                elif status_code == 403:
                    raise self._error("authorization_error", status_code=status_code)
                elif status_code == 429:
                    provider_error: CmcClientError | None = None
                    try:
                        error_payload = response.json()
                    except (TypeError, ValueError):
                        error_payload = None
                    if (
                        isinstance(error_payload, dict)
                        and isinstance(error_payload.get("status"), dict)
                    ):
                        provider_error = self._envelope_error(error_payload)
                    if (
                        provider_error is not None
                        and provider_error.kind == "credit_exhausted_error"
                    ):
                        raise self._error(
                            "credit_exhausted_error",
                            status_code=status_code,
                        )
                    retry_after = self._retry_after(getattr(response, "headers", {}), self._clock())
                    error = self._error(
                        "rate_limit_error",
                        status_code=status_code,
                        retry_after_sec=retry_after,
                    )
                elif 500 <= status_code <= 599:
                    error = self._error("server_error", status_code=status_code)
                else:
                    raise self._error("client_error", status_code=status_code)

            if not error.retryable or attempt >= self.retries:
                raise error
            delay = error.retry_after_sec
            if delay is None:
                delay = self.backoff_base_sec * (2**attempt) + self.backoff_base_sec * max(
                    0.0, min(1.0, float(self._random()))
                )
            if delay > MAX_RETRY_SLEEP_SEC:
                # Respect a long Retry-After by not issuing another request. The
                # caller can then use a still-fresh cache instead of blocking a
                # one-shot scan for an unbounded provider cooldown.
                raise error
            self._sleep_for(max(0.0, delay))
        raise self._error("internal_error")  # pragma: no cover - loop always returns or raises

    def _wait_for_request_slot(self) -> None:
        """Apply a bounded, plan-safe cadence to successful calls and retries."""

        if self.min_request_interval_sec <= 0:
            return
        with self._request_slot_lock:
            now = self._request_clock_now()
            if self._last_request_started_at is not None:
                remaining = (
                    self.min_request_interval_sec
                    - (now - self._last_request_started_at)
                )
                if remaining > 0:
                    self._rate_limit_waits += 1
                    self._rate_limit_sleep_sec += remaining
                    self._sleep_for(remaining)
                    now = self._request_clock_now()
            self._last_request_started_at = now

    def _request_clock_now(self) -> float:
        return max(float(self._clock()), self._request_clock_floor)

    def _sleep_for(self, delay: float) -> None:
        before = self._request_clock_now()
        self._sleep(delay)
        self._request_clock_floor = max(float(self._clock()), before + delay)

    def _envelope_error(self, payload: Mapping[str, Any]) -> CmcClientError | None:
        status = payload.get("status")
        if not isinstance(status, dict):
            return self._error("protocol_error", status_code=200)
        try:
            error_code = int(status.get("error_code", 0))
        except (TypeError, ValueError):
            return self._error("protocol_error", status_code=200)
        if error_code == 0:
            return None
        if error_code in {1001, 1002}:
            return self._error("authentication_error", status_code=200)
        if error_code in {1006}:
            return self._error("authorization_error", status_code=200)
        if error_code == 1007:
            return self._error("rate_limit_error", status_code=200)
        if error_code == 1008:
            return self._error("credit_exhausted_error", status_code=200)
        if error_code in {1009, 1010, 1011}:
            return self._error("authorization_error", status_code=200)
        return self._error("protocol_error", status_code=200)

    def _error(
        self,
        kind: str,
        *,
        status_code: int | None = None,
        retry_after_sec: float | None = None,
    ) -> CmcClientError:
        self._last_error_kind = kind
        return CmcClientError(
            kind,
            status_code=status_code,
            retry_after_sec=retry_after_sec,
        )

    @staticmethod
    def _retry_after(headers: Any, now: float) -> float | None:
        value = ""
        if hasattr(headers, "get"):
            value = str(headers.get("Retry-After") or headers.get("retry-after") or "").strip()
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            pass
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, parsed.timestamp() - now)

    @staticmethod
    def _provider_timestamp(payload: Mapping[str, Any]) -> str | None:
        status = payload.get("status")
        if not isinstance(status, dict):
            return None
        value = str(status.get("timestamp") or "").strip()
        return value if _epoch(value) is not None else None

    @staticmethod
    def _parse_map_entry(item: Any) -> CmcMapEntry:
        if not isinstance(item, dict):
            raise CmcClientError("protocol_error")
        cmc_id = item.get("id")
        if isinstance(cmc_id, bool) or not isinstance(cmc_id, int) or cmc_id <= 0:
            raise CmcClientError("protocol_error")
        name = str(item.get("name") or "").strip()
        symbol = str(item.get("symbol") or "").strip().upper()
        slug = str(item.get("slug") or "").strip()
        if not name or not symbol or not slug:
            raise CmcClientError("protocol_error")
        platform = item.get("platform") if isinstance(item.get("platform"), dict) else {}
        active_value = item.get("is_active", 1)
        return CmcMapEntry(
            cmc_id=cmc_id,
            name=name,
            symbol=symbol,
            slug=slug,
            is_active=active_value is True or active_value == 1 or str(active_value).strip() == "1",
            token_address=str(platform.get("token_address") or "").strip() or None,
            platform_name=str(platform.get("name") or "").strip() or None,
            platform_symbol=str(platform.get("symbol") or "").strip() or None,
            platform_slug=str(platform.get("slug") or "").strip() or None,
        )

    @staticmethod
    def _parse_quotes(data: Any, requested: frozenset[int]) -> dict[int, CmcQuote]:
        keyed_items: list[tuple[int | None, Any]] = []
        if isinstance(data, list):
            keyed_items = [(None, item) for item in data]
        elif isinstance(data, dict):
            for key, item in data.items():
                try:
                    key_id = int(key)
                except (TypeError, ValueError):
                    raise CmcClientError("protocol_error") from None
                keyed_items.append((key_id, item))
        else:
            raise CmcClientError("protocol_error")

        parsed: dict[int, CmcQuote] = {}
        for key_id, item in keyed_items:
            if not isinstance(item, dict):
                raise CmcClientError("protocol_error")
            item_id = item.get("id")
            if isinstance(item_id, bool) or not isinstance(item_id, int):
                raise CmcClientError("protocol_error")
            if item_id not in requested or (key_id is not None and key_id != item_id):
                raise CmcClientError("protocol_error")
            if item_id in parsed:
                raise CmcClientError("protocol_error")
            parsed[item_id] = CmcClient._parse_quote_item(item)
        return parsed

    @staticmethod
    def _parse_quote_item(item: Mapping[str, Any]) -> CmcQuote:
        quote_payload = item.get("quote")
        if isinstance(quote_payload, dict):
            usd = quote_payload.get("USD", {})
        elif isinstance(quote_payload, list):
            usd_rows = [
                row for row in quote_payload
                if isinstance(row, dict)
                and str(row.get("symbol") or "").strip().upper() == "USD"
            ]
            if len(usd_rows) != 1:
                raise CmcClientError("protocol_error")
            usd = usd_rows[0]
        else:
            usd = {}
        if not isinstance(usd, dict):
            raise CmcClientError("protocol_error")
        last_updated = str(usd.get("last_updated") or item.get("last_updated") or "").strip()
        if _epoch(last_updated) is None:
            raise CmcClientError("protocol_error")
        return CmcQuote(
            cmc_id=int(item["id"]),
            name=str(item.get("name") or "").strip(),
            symbol=str(item.get("symbol") or "").strip().upper(),
            slug=str(item.get("slug") or "").strip(),
            market_cap_usd=_positive_float(usd.get("market_cap")),
            last_updated=last_updated,
        )

    @staticmethod
    def _validated_ids(cmc_ids: Iterable[int]) -> tuple[int, ...]:
        values: set[int] = set()
        for value in cmc_ids:
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("cmc_ids must contain positive integers")
            values.add(value)
        return tuple(sorted(values))

    def _quote_data_fresh(self, quote: CmcQuote, now: float) -> bool:
        updated_at = _epoch(quote.last_updated)
        if updated_at is None:
            return False
        age = now - updated_at
        return -300.0 <= age <= self.max_data_age_sec

    def _cache_document(self) -> dict[str, Any]:
        document = locked_read_json(self.cache_path, {}, quarantine_corrupt=True)
        if not isinstance(document, dict) or document.get("schema_version") != CACHE_SCHEMA_VERSION:
            return {}
        return document

    def _cached_map(self, now: float, *, allow_expired_ttl: bool = False) -> CmcMapResult | None:
        section = self._cache_document().get("map")
        if not isinstance(section, dict):
            return None
        expires_at_epoch = _epoch(section.get("expires_at"))
        if expires_at_epoch is None:
            return None
        if expires_at_epoch < now and not allow_expired_ttl:
            return None
        data_updated_epoch = _epoch(section.get("data_updated_at"))
        data_age = now - data_updated_epoch if data_updated_epoch is not None else None
        if data_age is None or not -300.0 <= data_age <= self.max_data_age_sec:
            return None
        raw_entries = section.get("entries")
        if not isinstance(raw_entries, list):
            return None
        try:
            entries = tuple(self._map_entry_from_cache(item) for item in raw_entries)
        except (KeyError, TypeError, ValueError):
            return None
        ids = [entry.cmc_id for entry in entries]
        if len(ids) != len(set(ids)):
            return None
        generated_epoch = _epoch(section.get("generated_at"))
        if (
            generated_epoch is None
            or data_updated_epoch is None
            or data_updated_epoch > generated_epoch + 300.0
        ):
            return None
        return CmcMapResult(
            tuple(sorted(entries, key=lambda item: item.cmc_id)),
            "cache",
            str(section.get("generated_at") or "") or None,
            str(section.get("data_updated_at") or "") or None,
            str(section.get("expires_at") or "") or None,
            0,
        )

    @staticmethod
    def _quote_cache_records(document: Mapping[str, Any]) -> dict[str, Any]:
        section = document.get("quotes")
        if not isinstance(section, dict) or not isinstance(section.get("entries"), dict):
            return {}
        return section["entries"]

    def _cached_quote(
        self,
        record: Any,
        now: float,
        *,
        expected_id: int | None = None,
    ) -> tuple[CmcQuote, bool, bool] | None:
        if not isinstance(record, dict) or not isinstance(record.get("value"), dict):
            return None
        try:
            quote = self._quote_from_cache(record["value"])
        except (KeyError, TypeError, ValueError):
            return None
        if expected_id is not None and quote.cmc_id != expected_id:
            return None
        generated_at = _epoch(record.get("generated_at"))
        data_updated_at = _epoch(record.get("data_updated_at"))
        quote_updated_at = _epoch(quote.last_updated)
        if (
            generated_at is None
            or data_updated_at is None
            or quote_updated_at is None
            or abs(data_updated_at - quote_updated_at) > 0.001
            or quote_updated_at > generated_at + 300.0
        ):
            return None
        expires_at = _epoch(record.get("expires_at"))
        return quote, expires_at is not None and expires_at >= now, self._quote_data_fresh(quote, now)

    def _store_map(
        self,
        entries: tuple[CmcMapEntry, ...],
        generated_at: str,
        data_updated_at: str,
        expires_at: str,
    ) -> None:
        serialized = [self._map_entry_to_cache(entry) for entry in entries]

        def update(current: Any) -> dict[str, Any]:
            document = self._valid_or_empty_cache(current, generated_at)
            document["generated_at"] = generated_at
            document["map"] = {
                "generated_at": generated_at,
                "data_updated_at": data_updated_at,
                "expires_at": expires_at,
                "entries": serialized,
            }
            return document

        locked_update_json(self.cache_path, update, {})

    def _store_quotes(self, quotes: Iterable[CmcQuote], fetched_at: float) -> None:
        quote_list = list(quotes)
        if not quote_list:
            return
        generated_at = _iso_at(fetched_at)
        expires_at = _iso_at(fetched_at + self.cache_ttl_sec)

        def update(current: Any) -> dict[str, Any]:
            document = self._valid_or_empty_cache(current, generated_at)
            section = document.get("quotes")
            records = dict(section.get("entries", {})) if isinstance(section, dict) else {}
            for quote in quote_list:
                records[str(quote.cmc_id)] = {
                    "generated_at": generated_at,
                    "data_updated_at": quote.last_updated,
                    "expires_at": expires_at,
                    "value": self._quote_to_cache(quote),
                }
            record_values = [item for item in records.values() if isinstance(item, dict)]
            expiry_values = [str(item.get("expires_at")) for item in record_values if item.get("expires_at")]
            data_values = [str(item.get("data_updated_at")) for item in record_values if item.get("data_updated_at")]
            document["generated_at"] = generated_at
            document["quotes"] = {
                "generated_at": generated_at,
                "data_updated_at": max(data_values) if data_values else generated_at,
                "expires_at": min(expiry_values) if expiry_values else expires_at,
                "entries": records,
            }
            return document

        locked_update_json(self.cache_path, update, {})

    @staticmethod
    def _valid_or_empty_cache(current: Any, generated_at: str) -> dict[str, Any]:
        if isinstance(current, dict) and current.get("schema_version") == CACHE_SCHEMA_VERSION:
            return dict(current)
        return {"schema_version": CACHE_SCHEMA_VERSION, "generated_at": generated_at}

    @staticmethod
    def _map_entry_to_cache(entry: CmcMapEntry) -> dict[str, Any]:
        return {
            "cmc_id": entry.cmc_id,
            "name": entry.name,
            "symbol": entry.symbol,
            "slug": entry.slug,
            "is_active": entry.is_active,
            "token_address": entry.token_address,
            "platform_name": entry.platform_name,
            "platform_symbol": entry.platform_symbol,
            "platform_slug": entry.platform_slug,
        }

    @staticmethod
    def _map_entry_from_cache(item: Mapping[str, Any]) -> CmcMapEntry:
        raw_id = item["cmc_id"]
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise ValueError("invalid cmc id")
        cmc_id = raw_id
        name = str(item["name"]).strip()
        symbol = str(item["symbol"]).strip().upper()
        slug = str(item["slug"]).strip()
        active = item["is_active"]
        if cmc_id <= 0 or not name or not symbol or not slug or not isinstance(active, bool):
            raise ValueError("invalid cached map identity")
        return CmcMapEntry(
            cmc_id=cmc_id,
            name=name,
            symbol=symbol,
            slug=slug,
            is_active=active,
            token_address=str(item["token_address"]) if item.get("token_address") else None,
            platform_name=str(item["platform_name"]) if item.get("platform_name") else None,
            platform_symbol=str(item["platform_symbol"]) if item.get("platform_symbol") else None,
            platform_slug=str(item["platform_slug"]) if item.get("platform_slug") else None,
        )

    @staticmethod
    def _quote_to_cache(quote: CmcQuote) -> dict[str, Any]:
        return {
            "cmc_id": quote.cmc_id,
            "name": quote.name,
            "symbol": quote.symbol,
            "slug": quote.slug,
            "market_cap_usd": quote.market_cap_usd,
            "last_updated": quote.last_updated,
        }

    @staticmethod
    def _quote_from_cache(item: Mapping[str, Any]) -> CmcQuote:
        market_cap = item.get("market_cap_usd")
        if market_cap is not None:
            market_cap = _positive_float(market_cap)
        last_updated = str(item["last_updated"])
        if _epoch(last_updated) is None:
            raise ValueError("invalid timestamp")
        raw_id = item["cmc_id"]
        if isinstance(raw_id, bool) or not isinstance(raw_id, int):
            raise ValueError("invalid cmc id")
        cmc_id = raw_id
        name = str(item.get("name") or "").strip()
        symbol = str(item.get("symbol") or "").strip().upper()
        slug = str(item.get("slug") or "").strip()
        if cmc_id <= 0 or not name or not symbol or not slug:
            raise ValueError("invalid cached quote identity")
        return CmcQuote(
            cmc_id=cmc_id,
            name=name,
            symbol=symbol,
            slug=slug,
            market_cap_usd=market_cap,
            last_updated=last_updated,
        )


__all__ = [
    "CACHE_SCHEMA_VERSION",
    "DEFAULT_MIN_REQUEST_INTERVAL_SEC",
    "CmcClient",
    "CmcClientError",
    "CmcMapEntry",
    "CmcMapResult",
    "CmcQuote",
    "CmcQuotesResult",
    "MAX_MAP_PAGE_SIZE",
    "MAX_QUOTE_BATCH_SIZE",
]
