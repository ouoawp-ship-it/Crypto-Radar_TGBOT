from __future__ import annotations

import threading
import time
from decimal import Decimal
from math import isfinite
from typing import Any, Callable, Mapping

import requests


class ArkhamError(RuntimeError):
    pass


class ArkhamAuthError(ArkhamError):
    pass


class ArkhamRateLimitError(ArkhamError):
    pass


class ArkhamServiceError(ArkhamError):
    pass


class ArkhamTransportError(ArkhamError):
    pass


class ArkhamResponseError(ArkhamError):
    def __init__(self, message: str, *, status_code: int = 0):
        super().__init__(message)
        self.status_code = int(status_code)


class ArkhamSchemaError(ArkhamError):
    pass


def _rate_limit_headers(headers: Mapping[str, object]) -> dict[str, str]:
    allowed = {
        "ratelimit-limit",
        "ratelimit-remaining",
        "ratelimit-reset",
        "retry-after",
        "x-ratelimit-limit",
        "x-ratelimit-remaining",
        "x-ratelimit-reset",
    }
    result: dict[str, str] = {}
    for name, value in headers.items():
        normalized = str(name).strip().lower()
        text = str(value).strip()
        if normalized in allowed and text and len(text) <= 64:
            result[normalized] = text
    return result


class ArkhamRestClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_sec: float = 10,
        retry: int = 2,
        backoff_sec: float = 1,
        retry_after_max_sec: int = 60,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        if not base_url:
            raise ValueError("Arkham API base URL is not configured")
        if not api_key:
            raise ValueError("Arkham API key is not configured")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self.timeout_sec = float(timeout_sec)
        self.retry = min(3, max(0, int(retry)))
        self.backoff_sec = max(0.0, float(backoff_sec))
        if (
            isinstance(retry_after_max_sec, bool)
            or int(retry_after_max_sec) <= 0
        ):
            raise ValueError("Arkham Retry-After cap must be positive")
        self.retry_after_max_sec = min(
            3600, int(retry_after_max_sec)
        )
        self._session = session or requests.Session()
        self._sleep = sleep
        self._clock = clock
        self._heavy_lock = threading.Lock()
        self._last_heavy_started: float | None = None
        self.last_rate_limit_info: dict[str, str] = {}

    def _wait_for_heavy_budget(self) -> None:
        with self._heavy_lock:
            now = self._clock()
            if self._last_heavy_started is not None:
                wait = max(0.0, 1.0 - (now - self._last_heavy_started))
                if wait:
                    self._sleep(wait)
                    now = max(self._clock(), now + wait)
            self._last_heavy_started = now

    def _request(
        self,
        path: str,
        *,
        params: Mapping[str, object] | None = None,
        heavy: bool = False,
        expect_json: bool = True,
    ) -> object:
        attempts = self.retry + 1
        for attempt in range(attempts):
            if heavy:
                self._wait_for_heavy_budget()
            try:
                response = self._session.get(
                    self._base_url + path,
                    params=dict(params or {}),
                    headers={"API-Key": self._api_key},
                    timeout=self.timeout_sec,
                    allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt + 1 >= attempts:
                    raise ArkhamTransportError(
                        "Arkham request transport failed"
                    ) from exc
                self._sleep(self._bounded_delay(None, attempt))
                continue
            except requests.RequestException as exc:
                raise ArkhamTransportError(
                    "Arkham request transport failed"
                ) from exc

            self.last_rate_limit_info = _rate_limit_headers(
                getattr(response, "headers", {}) or {}
            )
            status = int(getattr(response, "status_code", 0))
            if status in {401, 403}:
                raise ArkhamAuthError(
                    f"Arkham authentication failed with HTTP {status}"
                )
            if status == 429:
                if attempt + 1 >= attempts:
                    raise ArkhamRateLimitError(
                        "Arkham rate limit remained exhausted"
                    )
                retry_after = self.last_rate_limit_info.get(
                    "retry-after", ""
                )
                self._sleep(self._bounded_delay(retry_after, attempt))
                continue
            if 500 <= status <= 599:
                if attempt + 1 >= attempts:
                    raise ArkhamServiceError(
                        f"Arkham service failed with HTTP {status}"
                    )
                self._sleep(self._bounded_delay(None, attempt))
                continue
            if status < 200 or status >= 300:
                raise ArkhamResponseError(
                    f"Arkham request rejected with HTTP {status}",
                    status_code=status,
                )
            if not expect_json:
                text = getattr(response, "text", None)
                if not isinstance(text, str):
                    raise ArkhamSchemaError(
                        "Arkham text response is malformed"
                    )
                return text
            try:
                return response.json()
            except (TypeError, ValueError) as exc:
                raise ArkhamSchemaError(
                    "Arkham response is not valid JSON"
                ) from exc
        raise ArkhamTransportError("Arkham request attempts exhausted")

    def _bounded_delay(
        self, retry_after: object | None, attempt: int
    ) -> float:
        fallback = self.backoff_sec * (2**attempt)
        try:
            delay = float(retry_after)
        except (TypeError, ValueError):
            delay = fallback
        if not isfinite(delay) or delay < 0:
            delay = fallback
        return min(float(self.retry_after_max_sec), max(0.0, delay))

    def health(self) -> str:
        payload = self._request("/health", expect_json=False)
        if not isinstance(payload, str) or payload.strip().lower() != "ok":
            raise ArkhamSchemaError("Arkham health response is malformed")
        return "ok"

    def chains(self) -> list[object]:
        payload = self._request("/chains")
        if isinstance(payload, dict):
            payload = payload.get("chains")
        if not isinstance(payload, list):
            raise ArkhamSchemaError("Arkham chains response is malformed")
        if any(not isinstance(item, (str, dict)) for item in payload):
            raise ArkhamSchemaError("Arkham chain entry is malformed")
        return payload

    def transfers(
        self, params: Mapping[str, object]
    ) -> tuple[list[dict[str, object]], int]:
        payload = self._request(
            "/transfers", params=params, heavy=True
        )
        if not isinstance(payload, dict):
            raise ArkhamSchemaError(
                "Arkham transfers response is malformed"
            )
        transfers = payload.get("transfers")
        count = payload.get("count")
        if not isinstance(transfers, list) or any(
            not isinstance(item, dict) for item in transfers
        ):
            raise ArkhamSchemaError(
                "Arkham transfers list is malformed"
            )
        if isinstance(count, bool) or not isinstance(count, (int, float)):
            raise ArkhamSchemaError(
                "Arkham transfers count is malformed"
            )
        parsed_count = int(count)
        if parsed_count < 0:
            raise ArkhamSchemaError(
                "Arkham transfers count is malformed"
            )
        return transfers, parsed_count

    def capability_check(
        self,
        *,
        global_usd_gte: Decimal,
        chains: tuple[str, ...],
        limit: int,
    ) -> dict[str, object]:
        supported_chains = self.chains()
        base_params: dict[str, object] = {
            "timeLast": "1h",
            "usdGte": str(max(global_usd_gte, Decimal("10000000"))),
            "sortKey": "time",
            "sortDir": "asc",
            "limit": min(2, max(1, int(limit))),
            "offset": 0,
        }
        if chains:
            base_params["chains"] = ",".join(chains)
        supported = True
        for side in ("to", "from"):
            params = {**base_params, side: "type:cex"}
            try:
                self.transfers(params)
            except ArkhamResponseError as exc:
                if exc.status_code == 400:
                    supported = False
                    continue
                raise
        return {
            "authenticated": True,
            "supported_chain_count": len(supported_chains),
            "type_cex_rest_supported": supported,
            "rate_limit": dict(self.last_rate_limit_info),
            "rest_capability_status": (
                "ok"
                if supported
                else "explicit_cex_entity_ids_required"
            ),
            "websocket_check": "not_run_p3_2a",
            "requires_explicit_cex_entity_ids": not supported,
        }
