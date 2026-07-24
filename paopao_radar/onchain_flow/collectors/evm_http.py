from __future__ import annotations

import itertools
import re
import time
from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Iterable, Sequence

import requests

from ..config import OnchainSettings
from ..constants import BASE_CHAIN_ID, BASE_CHAIN_NAME, TRANSFER_TOPIC
from ..labels import normalize_evm_address
from ..models import NormalizedTransfer
from .base import BlockRange


HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
TOPIC_ADDRESS_RE = re.compile(r"^0x0{24}[0-9a-fA-F]{40}$")
UINT256_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
RANGE_ERROR_MARKERS = (
    "too many results",
    "response size exceeded",
    "payload too large",
    "block range",
    "query returned more than",
    "limit exceeded",
)
TIMEOUT_ERROR_MARKERS = ("timeout", "timed out", "deadline exceeded")
RATE_LIMIT_MARKERS = ("rate limit", "too many requests", "429")


class RpcError(RuntimeError):
    pass


class RpcTransportError(RpcError):
    pass


class RpcResponseError(RpcError):
    pass


class RpcRangeError(RpcResponseError):
    pass


class RpcTimeoutError(RpcTransportError):
    pass


class RpcRateLimitError(RpcTransportError):
    pass


class RpcAuthError(RpcTransportError):
    pass


class RpcServiceError(RpcTransportError):
    pass


class RpcConnectionError(RpcTransportError):
    pass


class AdaptiveRangeError(RpcError):
    pass


class FinalizedRangeConsistencyError(RpcError):
    pass


class LogValidationError(ValueError):
    pass


def hex_quantity(value: int) -> str:
    if value < 0:
        raise ValueError("JSON-RPC quantities cannot be negative")
    return hex(value)


def parse_hex_quantity(value: object, field: str) -> int:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise RpcResponseError(f"{field} is not a hex quantity")
    try:
        return int(value, 16)
    except ValueError as exc:
        raise RpcResponseError(f"{field} is not a hex quantity") from exc


def pad_topic_address(address: str) -> str:
    normalized = normalize_evm_address(address)
    return "0x" + ("0" * 24) + normalized[2:]


def decode_topic_address(topic: object) -> str:
    if not isinstance(topic, str) or not TOPIC_ADDRESS_RE.fullmatch(topic):
        raise LogValidationError("indexed address topic is malformed")
    return "0x" + topic[-40:].lower()


def transfer_log_shape(log: dict[str, object]) -> str:
    topics = log.get("topics")
    if not isinstance(topics, list):
        raise LogValidationError("Transfer log topics are malformed")
    if len(topics) not in {3, 4}:
        raise LogValidationError("Transfer log topic count is unsupported")
    if (
        not isinstance(topics[0], str)
        or topics[0].lower() != TRANSFER_TOPIC
    ):
        raise LogValidationError("log is not the canonical Transfer event")
    decode_topic_address(topics[1])
    decode_topic_address(topics[2])
    data = log.get("data")
    if (
        len(topics) == 3
        and isinstance(data, str)
        and UINT256_RE.fullmatch(data)
    ):
        return "erc20"
    if (
        len(topics) == 4
        and isinstance(topics[3], str)
        and HASH_RE.fullmatch(topics[3])
        and data == "0x"
    ):
        return "indexed_value"
    raise LogValidationError("Transfer log ABI shape is unsupported")


def canonical_log_contents(
    log: dict[str, object],
) -> tuple[object, ...]:
    topics = log.get("topics")
    return (
        str(log.get("address") or "").lower(),
        tuple(str(item).lower() for item in topics)
        if isinstance(topics, list)
        else (),
        str(log.get("data") or "").lower(),
        str(log.get("blockNumber") or "").lower(),
        str(log.get("blockHash") or "").lower(),
        str(log.get("transactionHash") or "").lower(),
        str(log.get("logIndex") or "").lower(),
        bool(log.get("removed", False)),
    )


def address_batches(
    addresses: Iterable[str], batch_size: int
) -> list[tuple[str, ...]]:
    if batch_size <= 0:
        raise ValueError("address batch size must be positive")
    normalized = sorted({normalize_evm_address(item) for item in addresses})
    return [
        tuple(normalized[index : index + batch_size])
        for index in range(0, len(normalized), batch_size)
    ]


@dataclass(frozen=True)
class TransferLogFilter:
    direction: str
    topics: tuple[object, ...]

    def as_rpc(self, block_range: BlockRange) -> dict[str, object]:
        return {
            "fromBlock": hex_quantity(block_range.start_block),
            "toBlock": hex_quantity(block_range.end_block),
            "topics": list(self.topics),
        }


def build_transfer_filters(
    addresses: Iterable[str], batch_size: int
) -> list[TransferLogFilter]:
    filters: list[TransferLogFilter] = []
    for batch in address_batches(addresses, batch_size):
        padded = [pad_topic_address(address) for address in batch]
        filters.append(
            TransferLogFilter(
                direction="outbound",
                topics=(TRANSFER_TOPIC, padded, None),
            )
        )
        filters.append(
            TransferLogFilter(
                direction="inbound",
                topics=(TRANSFER_TOPIC, None, padded),
            )
        )
    return filters


class JsonRpcClient:
    def __init__(
        self,
        url: str,
        *,
        timeout_sec: float,
        retry: int,
        backoff_sec: float,
        session: Any | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rate_limit_per_second: int = 20,
    ):
        if not url:
            raise ValueError("HTTP RPC is not configured")
        self._url = url
        self.timeout_sec = float(timeout_sec)
        self.retry = int(retry)
        self.backoff_sec = float(backoff_sec)
        self.session = session or requests.Session()
        self.sleep = sleep
        self.clock = clock
        self.rate_limit_per_second = rate_limit_per_second
        self._request_times: deque[float] = deque()
        self._request_ids = itertools.count(1)
        self.request_count = 0
        self.error_count = 0

    def call(self, method: str, params: Sequence[object]) -> object:
        request_id = next(self._request_ids)
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": list(params),
        }
        attempts = max(1, self.retry)
        last_error: RpcError | None = None
        for attempt in range(attempts):
            self._apply_rate_limit()
            self.request_count += 1
            try:
                response = self.session.post(
                    self._url,
                    json=payload,
                    timeout=self.timeout_sec,
                    headers={"Content-Type": "application/json"},
                )
                status_code = int(getattr(response, "status_code", 200))
                if status_code in {401, 403}:
                    raise RpcAuthError(f"{method} provider authentication failed")
                if status_code == 429:
                    raise RpcRateLimitError(f"{method} provider rate limited")
                if status_code >= 500:
                    raise RpcServiceError(f"{method} provider unavailable")
                if status_code >= 400:
                    raise RpcResponseError(f"{method} provider HTTP error")
                data = response.json()
                if not isinstance(data, dict):
                    raise RpcResponseError(
                        f"{method} returned a non-object response"
                    )
                if data.get("id") != request_id:
                    raise RpcResponseError(
                        f"{method} returned a mismatched request id"
                    )
                if data.get("error") is not None:
                    error = data.get("error")
                    message = (
                        str(error.get("message", ""))
                        if isinstance(error, dict)
                        else str(error)
                    )
                    safe_message = message.lower()
                    if any(
                        marker in safe_message
                        for marker in RATE_LIMIT_MARKERS
                    ):
                        error_type = RpcRateLimitError
                    elif any(
                        marker in safe_message
                        for marker in TIMEOUT_ERROR_MARKERS
                    ):
                        error_type = RpcTimeoutError
                    elif any(
                        marker in safe_message
                        for marker in RANGE_ERROR_MARKERS
                    ):
                        error_type = RpcRangeError
                    else:
                        error_type = RpcResponseError
                    raise error_type(f"{method} RPC error")
                if "result" not in data:
                    raise RpcResponseError(f"{method} response lacks result")
                return data["result"]
            except (RpcRangeError, RpcAuthError, RpcResponseError):
                self.error_count += 1
                raise
            except RpcRateLimitError as exc:
                self.error_count += 1
                last_error = exc
                if attempt + 1 < attempts:
                    self.sleep(self.backoff_sec * (2**attempt))
                    continue
                raise
            except (RpcTimeoutError, RpcServiceError) as exc:
                self.error_count += 1
                last_error = exc
                if attempt + 1 < attempts:
                    self.sleep(self.backoff_sec * (2**attempt))
                    continue
                raise
            except requests.Timeout as exc:
                self.error_count += 1
                last_error = RpcTimeoutError(
                    f"{method} timed out after bounded attempts"
                )
                if attempt + 1 < attempts:
                    self.sleep(self.backoff_sec * (2**attempt))
                    continue
                raise last_error from exc
            except requests.ConnectionError as exc:
                self.error_count += 1
                raise RpcConnectionError(
                    f"{method} provider connection failed"
                ) from exc
            except requests.RequestException as exc:
                self.error_count += 1
                raise RpcConnectionError(
                    f"{method} provider transport failed"
                ) from exc
            except ValueError as exc:
                self.error_count += 1
                raise RpcResponseError(
                    f"{method} returned malformed JSON"
                ) from exc
        raise last_error or RpcTransportError(
            f"{method} failed after {attempts} bounded attempts"
        )

    def _apply_rate_limit(self) -> None:
        now = self.clock()
        while self._request_times and now - self._request_times[0] >= 1:
            self._request_times.popleft()
        if (
            self.rate_limit_per_second > 0
            and len(self._request_times) >= self.rate_limit_per_second
        ):
            wait = max(0.0, 1 - (now - self._request_times[0]))
            self.sleep(wait)
            self._request_times.clear()
            now = self.clock()
        self._request_times.append(now)

    def chain_id(self) -> int:
        return parse_hex_quantity(self.call("eth_chainId", []), "chain id")

    def block_number(self) -> int:
        return parse_hex_quantity(
            self.call("eth_blockNumber", []), "block number"
        )

    def get_block(self, block_number: int) -> dict[str, object]:
        result = self.call(
            "eth_getBlockByNumber", [hex_quantity(block_number), False]
        )
        if not isinstance(result, dict):
            raise RpcResponseError("eth_getBlockByNumber returned no block")
        return result

    def get_logs(self, log_filter: dict[str, object]) -> list[dict[str, object]]:
        result = self.call("eth_getLogs", [log_filter])
        if not isinstance(result, list):
            raise RpcResponseError("eth_getLogs returned a non-list result")
        if not all(isinstance(item, dict) for item in result):
            raise RpcResponseError("eth_getLogs returned a malformed log")
        return result

    def get_code(self, address: str) -> str:
        result = self.call("eth_getCode", [normalize_evm_address(address), "latest"])
        if not isinstance(result, str):
            raise RpcResponseError("eth_getCode returned malformed data")
        return result

    def eth_call(self, address: str, data: str) -> str:
        result = self.call(
            "eth_call",
            [{"to": normalize_evm_address(address), "data": data}, "latest"],
        )
        if not isinstance(result, str):
            raise RpcResponseError("eth_call returned malformed data")
        return result


def normalize_transfer_log(
    log: dict[str, object],
    *,
    block_time: int,
    chain_id: int = BASE_CHAIN_ID,
) -> NormalizedTransfer:
    if transfer_log_shape(log) != "erc20":
        raise LogValidationError("Transfer log is not an ERC-20 shape")
    topics = log.get("topics")
    assert isinstance(topics, list)
    from_address = decode_topic_address(topics[1])
    to_address = decode_topic_address(topics[2])
    data = log.get("data")
    assert isinstance(data, str)
    token_address = normalize_evm_address(str(log.get("address") or ""))
    tx_hash = str(log.get("transactionHash") or "")
    block_hash = str(log.get("blockHash") or "")
    if not HASH_RE.fullmatch(tx_hash) or not HASH_RE.fullmatch(block_hash):
        raise LogValidationError("Transfer hash is malformed")
    try:
        block_number = parse_hex_quantity(
            log.get("blockNumber"), "log block number"
        )
        log_index = parse_hex_quantity(log.get("logIndex"), "log index")
    except RpcResponseError as exc:
        raise LogValidationError(str(exc)) from exc
    return NormalizedTransfer.create(
        chain_id=chain_id,
        chain_name=BASE_CHAIN_NAME,
        block_number=block_number,
        block_hash=block_hash,
        block_time=block_time,
        tx_hash=tx_hash,
        log_index=log_index,
        token_address=token_address,
        from_address=from_address,
        to_address=to_address,
        amount_raw=int(data, 16),
        removed=bool(log.get("removed", False)),
        confirmation_status="finalized",
        source="evm_http",
    )


class BaseHttpCollector:
    def __init__(self, client: JsonRpcClient, settings: OnchainSettings):
        self.client = client
        self.settings = settings

    def provider_check(self) -> dict[str, object]:
        chain_id = self.client.chain_id()
        if chain_id != BASE_CHAIN_ID or chain_id != self.settings.base_chain_id:
            raise RpcResponseError("configured provider chain ID is not Base")
        head = self.client.block_number()
        target = max(0, head - self.settings.base_confirmation_depth)
        block = self.client.get_block(target)
        block_hash = block.get("hash")
        if not isinstance(block_hash, str) or not HASH_RE.fullmatch(block_hash):
            raise RpcResponseError("provider block lookup returned no hash")
        return {
            "status": "ok",
            "chain": BASE_CHAIN_NAME,
            "chain_id": chain_id,
            "latest_head": head,
            "target_finalized": target,
            "block_lookup": "ok",
            "provider_configured": True,
        }

    def fetch_cex_logs(
        self, start_block: int, end_block: int, addresses: Iterable[str]
    ) -> list[dict[str, object]]:
        if end_block < start_block:
            return []
        filters = build_transfer_filters(
            addresses, self.settings.rpc_topic_address_batch
        )
        if not filters:
            return []
        results: list[dict[str, object]] = []
        cursor = start_block
        while cursor <= end_block:
            batch_end = min(
                end_block,
                cursor + self.settings.rpc_max_block_range - 1,
            )
            for transfer_filter in filters:
                results.extend(
                    self._fetch_adaptive(
                        BlockRange(cursor, batch_end), transfer_filter
                    )
                )
            cursor = batch_end + 1
        deduplicated: dict[str, dict[str, object]] = {}
        malformed: list[dict[str, object]] = []
        for log in results:
            tx_hash = log.get("transactionHash")
            log_index = log.get("logIndex")
            if isinstance(tx_hash, str) and isinstance(log_index, str):
                key = f"{BASE_CHAIN_ID}:{tx_hash.lower()}:{log_index.lower()}"
                existing = deduplicated.get(key)
                if (
                    existing is not None
                    and canonical_log_contents(existing)
                    != canonical_log_contents(log)
                ):
                    raise FinalizedRangeConsistencyError(
                        "duplicate event key has conflicting canonical contents"
                    )
                deduplicated[key] = log
            else:
                malformed.append(log)
        return list(deduplicated.values()) + malformed

    def _fetch_adaptive(
        self,
        block_range: BlockRange,
        transfer_filter: TransferLogFilter,
        *,
        budget: dict[str, int] | None = None,
        depth: int = 0,
    ) -> list[dict[str, object]]:
        if budget is None:
            budget = {"requests": 0}
        if depth > self.settings.rpc_adaptive_max_depth:
            raise AdaptiveRangeError("adaptive range maximum depth exceeded")
        budget["requests"] += 1
        if budget["requests"] > self.settings.rpc_adaptive_max_requests:
            raise AdaptiveRangeError("adaptive range request budget exhausted")
        try:
            return self.client.get_logs(transfer_filter.as_rpc(block_range))
        except (RpcRangeError, RpcTimeoutError) as exc:
            size = block_range.end_block - block_range.start_block + 1
            if size <= self.settings.rpc_min_block_range:
                raise AdaptiveRangeError(
                    "minimum block range failed; cursor must not advance"
                ) from exc
            midpoint = (block_range.start_block + block_range.end_block) // 2
            left = BlockRange(block_range.start_block, midpoint)
            right = BlockRange(midpoint + 1, block_range.end_block)
            return self._fetch_adaptive(
                left,
                transfer_filter,
                budget=budget,
                depth=depth + 1,
            ) + self._fetch_adaptive(
                right,
                transfer_filter,
                budget=budget,
                depth=depth + 1,
            )
