from __future__ import annotations

from collections.abc import Callable, Iterable
import time
from typing import Any
from urllib.parse import quote

import requests

from .labels import LabelValidationError, normalize_evm_address


ARKHAM_REQUEST_HARD_LIMIT = 6
BASE_CHAIN = "base"


class ArkhamIntelligenceError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _normalized_address(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return normalize_evm_address(value)
    except LabelValidationError:
        return None


class ArkhamIntelligenceClient:
    """Bounded, explicit-only Arkham client for CEX label candidates."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_sec: int,
        max_retries: int,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_sec = int(timeout_sec)
        self.max_retries = int(max_retries)
        self.session = session or requests.Session()
        self.sleep = sleep
        self.request_count = 0

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        json_body: dict[str, object] | None = None,
    ) -> object:
        response: Any | None = None
        for attempt in range(self.max_retries + 1):
            if self.request_count >= ARKHAM_REQUEST_HARD_LIMIT:
                raise ArkhamIntelligenceError(
                    "arkham_request_budget_exhausted"
                )
            self.request_count += 1
            try:
                response = self.session.request(
                    method,
                    f"{self.base_url}{path}",
                    headers={
                        "API-Key": self.api_key,
                        "Content-Type": "application/json",
                    },
                    params=params,
                    json=json_body,
                    timeout=self.timeout_sec,
                    allow_redirects=False,
                )
            except requests.Timeout as exc:
                if attempt < self.max_retries:
                    self.sleep(min(2**attempt, 2))
                    continue
                raise ArkhamIntelligenceError("arkham_timeout") from exc
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    self.sleep(min(2**attempt, 2))
                    continue
                raise ArkhamIntelligenceError(
                    "arkham_provider_unavailable"
                ) from exc

            status = int(response.status_code)
            if 300 <= status < 400:
                raise ArkhamIntelligenceError("arkham_invalid_response")
            if status in {401, 403}:
                raise ArkhamIntelligenceError("arkham_auth_failed")
            if status == 402:
                raise ArkhamIntelligenceError(
                    "arkham_credit_or_subscription_required"
                )
            if status == 429:
                if attempt < self.max_retries:
                    self.sleep(min(2**attempt, 2))
                    continue
                raise ArkhamIntelligenceError("arkham_rate_limited")
            if 500 <= status < 600:
                if attempt < self.max_retries:
                    self.sleep(min(2**attempt, 2))
                    continue
                raise ArkhamIntelligenceError(
                    "arkham_provider_unavailable"
                )
            if status < 200 or status >= 300:
                raise ArkhamIntelligenceError("arkham_invalid_response")
            try:
                return response.json()
            except (TypeError, ValueError) as exc:
                raise ArkhamIntelligenceError(
                    "arkham_invalid_response"
                ) from exc
        raise ArkhamIntelligenceError("arkham_provider_unavailable")

    def provider_check(self) -> dict[str, object]:
        if not self.api_key:
            raise ArkhamIntelligenceError("arkham_not_configured")
        payload = self._request("GET", "/networks/status")
        if not isinstance(payload, (dict, list)):
            raise ArkhamIntelligenceError("arkham_invalid_response")
        return {
            "status": "ok",
            "provider": "arkham",
            "authenticated": True,
            "request_count": self.request_count,
        }

    def address_batch(
        self,
        addresses: Iterable[str],
    ) -> dict[str, dict[str, object]]:
        normalized = [normalize_evm_address(value) for value in addresses]
        if not normalized:
            return {}
        payload = self._request(
            "POST",
            "/intelligence/address/batch",
            params={"chain": BASE_CHAIN},
            json_body={"addresses": normalized},
        )
        if not isinstance(payload, dict):
            raise ArkhamIntelligenceError("arkham_invalid_response")
        raw_addresses = payload.get("addresses")
        if not isinstance(raw_addresses, dict):
            raise ArkhamIntelligenceError("arkham_invalid_response")
        expected = set(normalized)
        results: dict[str, dict[str, object]] = {}
        for query_address, value in raw_addresses.items():
            normalized_query = _normalized_address(query_address)
            if normalized_query not in expected or not isinstance(value, dict):
                continue
            results[normalized_query] = value
        return results

    def address(self, address: str) -> dict[str, object]:
        normalized = normalize_evm_address(address)
        payload = self._request(
            "GET",
            f"/intelligence/address/{quote(normalized, safe='')}",
            params={"chain": BASE_CHAIN},
        )
        if not isinstance(payload, dict):
            raise ArkhamIntelligenceError("arkham_invalid_response")
        return payload

    def address_intelligence(
        self,
        addresses: Iterable[str],
    ) -> dict[str, dict[str, object]]:
        normalized = list(dict.fromkeys(
            normalize_evm_address(value) for value in addresses
        ))
        results = self.address_batch(normalized)
        for address in normalized:
            if address in results:
                continue
            # Keep two requests in reserve for the only permitted seed path.
            if self.request_count >= ARKHAM_REQUEST_HARD_LIMIT - 2:
                break
            results[address] = self.address(address)
        return results

    def seed_cex_transfers(self) -> list[dict[str, object]]:
        results: list[dict[str, object]] = []
        filters = (
            {"from": "type:cex"},
            {"to": "type:cex"},
        )
        for index, direction in enumerate(filters):
            if index:
                self.sleep(1.1)
            payload = self._request(
                "GET",
                "/transfers",
                params={
                    "chains": BASE_CHAIN,
                    **direction,
                    "timeLast": "24h",
                    "limit": 20,
                    "sortKey": "time",
                    "sortDir": "desc",
                },
            )
            if not isinstance(payload, dict):
                raise ArkhamIntelligenceError("arkham_invalid_response")
            transfers = payload.get("transfers")
            if not isinstance(transfers, list):
                raise ArkhamIntelligenceError("arkham_invalid_response")
            results.extend(
                item for item in transfers if isinstance(item, dict)
            )
        return results


__all__ = [
    "ARKHAM_REQUEST_HARD_LIMIT",
    "ArkhamIntelligenceClient",
    "ArkhamIntelligenceError",
]
