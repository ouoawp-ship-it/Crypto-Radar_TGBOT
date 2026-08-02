from __future__ import annotations

import time
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from typing import Any

from .chain_capabilities import (
    ChainCapabilityError,
    EvmChainSpec,
    resolve_chain_rpc_url,
    resolve_evm_chain,
    rpc_url_valid,
)
from .classifier import classify_transfer
from .collectors.evm_http import (
    BaseHttpCollector,
    FinalizedRangeConsistencyError,
    HASH_RE,
    JsonRpcClient,
    LogValidationError,
    RpcAuthError,
    RpcError,
    RpcRateLimitError,
    RpcRequestBudgetError,
    RpcResponseError,
    RpcTimeoutError,
    TokenLogFetchResult,
    normalize_transfer_log,
    parse_hex_quantity,
    transfer_log_shape,
)
from .config import OnchainSettings
from .constants import (
    TOKEN_ACTIVITY_SCHEMA_VERSION,
)
from .labels import (
    LabelRegistry,
    LabelValidationError,
    load_labels_csv,
    is_approved_label,
    normalize_evm_address,
)
from .models import AddressLabel, NormalizedTransfer, PriceQuote, TokenMetadata
from .price_oracle import PriceProvider, build_price_provider
from .token_metadata import TokenMetadataResolver


WINDOW_SECONDS = {
    "15m": 15 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "24h": 24 * 60 * 60,
}


class TokenActivityQueryError(ValueError):
    def __init__(self, code: str, reason: str):
        super().__init__(reason)
        self.code = code


class BlockHeaderBudgetExceeded(RuntimeError):
    pass


@dataclass(frozen=True)
class TokenActivityQuery:
    chain: str
    chain_id: int
    chain_name: str
    confirmation_depth: int
    http_rpc_env: str
    explorer_tx_url: str
    contract: str
    window: str
    window_seconds: int
    max_events: int
    max_rpc_requests: int
    adaptive_max_requests: int
    max_unique_block_headers: int
    top_n: int
    block_search_max_calls: int
    with_price: bool
    min_usd: Decimal | None

    @classmethod
    def create(
        cls,
        settings: OnchainSettings,
        *,
        chain: str,
        contract: str,
        window: str,
        max_events: int | None,
        max_rpc_requests: int | None,
        top_n: int | None,
        with_price: bool,
        min_usd: str | Decimal | None,
    ) -> "TokenActivityQuery":
        try:
            chain_spec = resolve_evm_chain(settings, chain)
        except ChainCapabilityError as exc:
            raise TokenActivityQueryError(
                exc.args[0], "requested EVM chain is not configured"
            ) from exc
        try:
            normalized_contract = normalize_evm_address(contract)
        except LabelValidationError as exc:
            raise TokenActivityQueryError(
                "invalid_contract",
                "contract must be a 20-byte EVM address",
            ) from exc
        if window not in WINDOW_SECONDS:
            raise TokenActivityQueryError(
                "invalid_window", "window must be one of 15m, 1h, 4h, 24h"
            )
        window_seconds = WINDOW_SECONDS[window]
        if window_seconds > settings.token_activity_max_window_hours * 3600:
            raise TokenActivityQueryError(
                "window_limit_exceeded",
                "requested window exceeds the configured query limit",
            )
        event_limit = (
            settings.token_activity_max_events
            if max_events is None
            else max_events
        )
        rpc_limit = (
            settings.token_activity_max_rpc_requests
            if max_rpc_requests is None
            else max_rpc_requests
        )
        result_limit = (
            settings.token_activity_top_n if top_n is None else top_n
        )
        for name, value, configured_limit in (
            ("max_events", event_limit, settings.token_activity_max_events),
            (
                "max_rpc_requests",
                rpc_limit,
                settings.token_activity_max_rpc_requests,
            ),
            ("top", result_limit, settings.token_activity_top_n),
        ):
            if isinstance(value, bool) or value <= 0:
                raise TokenActivityQueryError(
                    f"invalid_{name}", f"{name} must be positive"
                )
            if value > configured_limit:
                raise TokenActivityQueryError(
                    f"{name}_limit_exceeded",
                    f"{name} exceeds the configured hard limit",
                )
        parsed_min_usd: Decimal | None = None
        if min_usd is not None:
            if not with_price:
                raise TokenActivityQueryError(
                    "min_usd_requires_price",
                    "--min-usd requires --with-price",
                )
            try:
                parsed_min_usd = Decimal(str(min_usd))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise TokenActivityQueryError(
                    "invalid_min_usd",
                    "--min-usd must be a finite non-negative decimal",
                ) from exc
            if not parsed_min_usd.is_finite() or parsed_min_usd < 0:
                raise TokenActivityQueryError(
                    "invalid_min_usd",
                    "--min-usd must be a finite non-negative decimal",
                )
        return cls(
            chain=chain_spec.slug,
            chain_id=chain_spec.chain_id,
            chain_name=chain_spec.name,
            confirmation_depth=chain_spec.confirmation_depth,
            http_rpc_env=chain_spec.http_rpc_env,
            explorer_tx_url=chain_spec.explorer_tx_url,
            contract=normalized_contract,
            window=window,
            window_seconds=window_seconds,
            max_events=event_limit,
            max_rpc_requests=rpc_limit,
            adaptive_max_requests=(
                settings.token_activity_adaptive_max_requests
            ),
            max_unique_block_headers=(
                settings.token_activity_max_unique_block_headers
            ),
            top_n=result_limit,
            block_search_max_calls=(
                settings.token_activity_block_search_max_calls
            ),
            with_price=bool(with_price),
            min_usd=parsed_min_usd,
        )


@dataclass(frozen=True)
class BlockHeader:
    number: int
    block_hash: str
    timestamp: int


@dataclass(frozen=True)
class TokenActivityLabelContext:
    status: str
    identity_labels: tuple[AddressLabel, ...]
    direction_labels: tuple[AddressLabel, ...]


class BlockHeaderCache:
    def __init__(self, rpc: Any, max_unique_headers: int):
        self.rpc = rpc
        self.max_unique_headers = max_unique_headers
        self._cache: dict[int, BlockHeader] = {}

    @property
    def count(self) -> int:
        return len(self._cache)

    def get(self, block_number: int) -> BlockHeader:
        if block_number in self._cache:
            return self._cache[block_number]
        if len(self._cache) >= self.max_unique_headers:
            raise BlockHeaderBudgetExceeded(
                "unique block header budget exhausted"
            )
        raw = self.rpc.get_block(block_number)
        try:
            returned_number = parse_hex_quantity(
                raw.get("number"), "block number"
            )
            timestamp = parse_hex_quantity(
                raw.get("timestamp"), "block timestamp"
            )
        except RpcResponseError as exc:
            raise TokenActivityQueryError(
                "invalid_block_header",
                "provider returned an invalid block header",
            ) from exc
        block_hash = str(raw.get("hash") or "").lower()
        if (
            returned_number != block_number
            or timestamp <= 0
            or not HASH_RE.fullmatch(block_hash)
        ):
            raise TokenActivityQueryError(
                "invalid_block_header",
                "provider returned an invalid block header",
            )
        header = BlockHeader(block_number, block_hash, timestamp)
        self._cache[block_number] = header
        return header


class TokenActivityQueryService:
    def __init__(
        self,
        settings: OnchainSettings,
        rpc: Any,
        *,
        chain_spec: EvmChainSpec | None = None,
        price_provider: PriceProvider | None = None,
        clock: Any = time.monotonic,
    ):
        self.settings = settings
        self.rpc = rpc
        self.chain_spec = chain_spec or resolve_evm_chain(settings, "base")
        self.collector = BaseHttpCollector(
            rpc,
            settings,
            chain_id=self.chain_spec.chain_id,
            chain_name=self.chain_spec.name,
            confirmation_depth=self.chain_spec.confirmation_depth,
        )
        self.price_provider = price_provider
        self.clock = clock

    @classmethod
    def from_settings(
        cls,
        settings: OnchainSettings,
        query: TokenActivityQuery,
    ) -> "TokenActivityQueryService":
        try:
            chain_spec = resolve_evm_chain(settings, query.chain)
        except ChainCapabilityError as exc:
            raise TokenActivityQueryError(
                exc.args[0], "requested EVM chain is not configured"
            ) from exc
        if chain_spec.chain_id != query.chain_id:
            raise TokenActivityQueryError(
                "chain_configuration_changed",
                "chain configuration changed after query validation",
            )
        rpc_url = resolve_chain_rpc_url(settings, chain_spec)
        if not rpc_url:
            raise TokenActivityQueryError(
                "rpc_not_configured", "EVM HTTP RPC is not configured"
            )
        if not rpc_url_valid(rpc_url):
            raise TokenActivityQueryError(
                "rpc_configuration_invalid",
                "EVM HTTP RPC configuration is invalid",
            )
        rpc = JsonRpcClient(
            rpc_url,
            timeout_sec=float(settings.rpc_timeout_sec),
            retry=settings.rpc_retry,
            backoff_sec=float(settings.rpc_backoff_sec),
            rate_limit_per_second=settings.rpc_rate_limit_per_second,
            max_requests=query.max_rpc_requests,
        )
        price_provider = (
            build_price_provider(settings) if query.with_price else None
        )
        return cls(
            settings,
            rpc,
            chain_spec=chain_spec,
            price_provider=price_provider,
        )

    def execute(self, query: TokenActivityQuery) -> dict[str, object]:
        started = self.clock()
        request_start = int(getattr(self.rpc, "request_count", 0))
        labels, labels_file_status = self._load_labels()
        try:
            chain_id = self.rpc.chain_id()
        except RpcAuthError as exc:
            raise TokenActivityQueryError(
                "rpc_auth_failed", "EVM RPC authentication failed"
            ) from exc
        except RpcRequestBudgetError as exc:
            raise TokenActivityQueryError(
                "query_budget_exhausted_before_any_result",
                "RPC request budget was exhausted before the query started",
            ) from exc
        except RpcError as exc:
            raise TokenActivityQueryError(
                "rpc_unavailable", "EVM RPC provider check failed"
            ) from exc
        if chain_id != query.chain_id or chain_id != self.chain_spec.chain_id:
            raise TokenActivityQueryError(
                "wrong_chain", "configured RPC chain ID is incorrect"
            )

        metadata_resolver = TokenMetadataResolver(
            self.rpc, None, raise_rpc_errors=True
        )
        try:
            metadata = metadata_resolver.resolve(chain_id, query.contract)
        except RpcAuthError as exc:
            raise TokenActivityQueryError(
                "rpc_auth_failed", "EVM RPC authentication failed"
            ) from exc
        except RpcRequestBudgetError as exc:
            raise TokenActivityQueryError(
                "query_budget_exhausted_before_any_result",
                "RPC request budget was exhausted during Token verification",
            ) from exc
        except RpcError as exc:
            raise TokenActivityQueryError(
                "token_metadata_unavailable",
                "Token metadata could not be verified",
            ) from exc
        if metadata.metadata_status == "rpc_failed":
            if self._rpc_requests(request_start) >= query.max_rpc_requests:
                code = "query_budget_exhausted_before_any_result"
            else:
                code = "token_metadata_unavailable"
            raise TokenActivityQueryError(
                code, "Token metadata could not be verified"
            )
        if (
            metadata.token_kind != "erc20"
            or metadata.metadata_status != "verified_erc20"
            or metadata.decimals is None
        ):
            code = (
                "token_not_contract"
                if metadata_resolver.last_resolution_reason == "not_contract"
                else "invalid_decimals"
                if metadata_resolver.last_resolution_reason
                == "invalid_decimals"
                else "token_not_erc20"
            )
            raise TokenActivityQueryError(
                code,
                "contract could not be verified as ERC-20",
            )
        chain_and_metadata_end = int(
            getattr(self.rpc, "request_count", request_start)
        )

        try:
            head = self.rpc.block_number()
        except RpcRequestBudgetError as exc:
            raise TokenActivityQueryError(
                "query_budget_exhausted_before_any_result",
                "RPC request budget was exhausted before range discovery",
            ) from exc
        except RpcError as exc:
            raise TokenActivityQueryError(
                "rpc_unavailable", "could not read the EVM head block"
            ) from exc
        to_block = max(0, head - query.confirmation_depth)
        headers = BlockHeaderCache(
            self.rpc, query.max_unique_block_headers
        )
        try:
            to_header = headers.get(to_block)
            from_time = to_header.timestamp - query.window_seconds
            from_block = self._find_first_block_at_or_after(
                headers,
                to_block=to_block,
                target_time=from_time,
                max_calls=query.block_search_max_calls,
            )
        except (RpcRequestBudgetError, BlockHeaderBudgetExceeded) as exc:
            raise TokenActivityQueryError(
                "query_budget_exhausted_before_any_result",
                "query budget was exhausted during block range discovery",
            ) from exc
        except RpcError as exc:
            raise TokenActivityQueryError(
                "rpc_unavailable", "could not resolve the query block range"
            ) from exc

        label_context = self._prepare_labels(
            labels,
            chain_id=query.chain_id,
            file_status=labels_file_status,
            from_time=from_time,
            to_time=to_header.timestamp,
        )
        identity_registry = LabelRegistry(label_context.identity_labels)
        direction_registry = LabelRegistry(label_context.direction_labels)
        range_discovery_end = int(
            getattr(self.rpc, "request_count", chain_and_metadata_end)
        )

        try:
            fetched = self.collector.fetch_token_logs(
                from_block,
                to_block,
                query.contract,
                max_events=query.max_events,
                adaptive_max_requests=query.adaptive_max_requests,
            )
        except (
            FinalizedRangeConsistencyError,
            LogValidationError,
        ) as exc:
            raise TokenActivityQueryError(
                "malformed_log",
                "provider returned an inconsistent Transfer log",
            ) from exc
        except RpcAuthError as exc:
            raise TokenActivityQueryError(
                "rpc_auth_failed", "EVM RPC authentication failed"
            ) from exc
        except RpcError as exc:
            raise TokenActivityQueryError(
                "rpc_unavailable", "Token Transfer query failed"
            ) from exc
        transfer_logs_end = int(
            getattr(self.rpc, "request_count", range_discovery_end)
        )

        transfers, skipped, header_truncation = self._normalize_logs(
            fetched,
            query=query,
            headers=headers,
            from_time=from_time,
            to_time=to_header.timestamp,
        )
        block_headers_end = int(
            getattr(self.rpc, "request_count", transfer_logs_end)
        )
        truncation_reason = fetched.truncation_reason or header_truncation
        truncated = fetched.truncated or header_truncation is not None
        if truncated and not transfers:
            raise TokenActivityQueryError(
                "query_budget_exhausted_before_any_result",
                (
                    "the query became incomplete before any reliable "
                    f"Transfer fact was obtained ({truncation_reason})"
                ),
            )

        quote, price_status, price_warning = self._price(
            query, metadata, to_header.timestamp
        )
        price_end = int(
            getattr(self.rpc, "request_count", block_headers_end)
        )
        rpc_phase_requests = {
            "chain_and_metadata": max(
                0, chain_and_metadata_end - request_start
            ),
            "range_discovery": max(
                0, range_discovery_end - chain_and_metadata_end
            ),
            "transfer_logs": max(
                0, transfer_logs_end - range_discovery_end
            ),
            "block_headers": max(
                0, block_headers_end - transfer_logs_end
            ),
            "price": max(0, price_end - block_headers_end),
        }
        if quote is not None:
            metadata = replace(
                metadata,
                price_usd=quote.price_usd,
                volume_24h_usd=quote.volume_24h_usd,
                price_source=quote.source,
                price_observed_at=quote.observed_at,
            )

        records = [
            self._record(
                transfer,
                metadata,
                identity_registry,
                direction_registry,
                labels_status=label_context.status,
                price_status=price_status,
                explorer_tx_url=query.explorer_tx_url,
            )
            for transfer in transfers
        ]
        usd_filter_applied = False
        warnings: list[str] = []
        if label_context.status == "missing":
            warnings.append(
                "地址标签文件缺失；地址显示为未知钱包，不生成交易所方向分类"
            )
        elif label_context.status == "insufficient_cex_coverage":
            warnings.append(
                "标签文件缺少查询窗口内有效的高置信度当前链 CEX 标签；"
                "地址身份仍显示，交易所方向分类不可用"
            )
        if query.with_price:
            warnings.append("美元金额按查询时可用价格估算")
        if price_warning:
            warnings.append(price_warning)
        if query.min_usd is not None:
            if quote is None:
                truncated = True
                truncation_reason = (
                    truncation_reason
                    or "price_unavailable_for_usd_filter"
                )
                warnings.append(
                    "价格不可用，--min-usd 未应用；已保留链上原始事实"
                )
            else:
                records = [
                    record
                    for record in records
                    if Decimal(str(record["amount_usd"])) >= query.min_usd
                ]
                usd_filter_applied = True

        records.sort(
            key=lambda item: (
                int(item["block_number"]),
                int(item["log_index"]),
                str(item["tx_hash"]),
            )
        )
        largest = self._largest(records, query.top_n, quote is not None)
        summary = self._summary(records)
        status = "partial" if truncated else "ok"
        elapsed_ms = max(0, int((self.clock() - started) * 1000))
        return {
            "schema_version": TOKEN_ACTIVITY_SCHEMA_VERSION,
            "status": status,
            "complete": not truncated,
            "truncated": truncated,
            "truncation_reason": truncation_reason,
            "query": {
                "chain": query.chain,
                "chain_id": chain_id,
                "contract": query.contract,
                "window": query.window,
                "window_seconds": query.window_seconds,
                "from_block": from_block,
                "to_block": to_block,
                "from_time": from_time,
                "to_time": to_header.timestamp,
                "confirmation_depth": query.confirmation_depth,
                "min_usd": (
                    self._decimal_string(query.min_usd)
                    if query.min_usd is not None
                    else None
                ),
                "usd_filter_applied": usd_filter_applied,
            },
            "token": {
                "contract": metadata.token_address,
                "symbol": metadata.symbol,
                "name": metadata.name,
                "decimals": metadata.decimals,
                "metadata_status": metadata.metadata_status,
            },
            "price": {
                "enabled": query.with_price,
                "status": price_status,
                "price_usd": (
                    self._decimal_string(quote.price_usd)
                    if quote is not None
                    else None
                ),
                "source": quote.source if quote is not None else "",
                "observed_at": quote.observed_at if quote is not None else 0,
                "historical_price": False,
            },
            "labels": {
                "status": label_context.status,
                "count": len(label_context.identity_labels),
                "identity_label_count": len(
                    label_context.identity_labels
                ),
                "classification_eligible_cex_count": len(
                    label_context.direction_labels
                ),
            },
            "summary": summary,
            "largest_transfers": largest,
            "transfers": records,
            "limits": {
                "max_events": query.max_events,
                "max_rpc_requests": query.max_rpc_requests,
                "adaptive_max_requests": query.adaptive_max_requests,
                "max_unique_block_headers": query.max_unique_block_headers,
                "top_n": query.top_n,
            },
            "diagnostics": {
                "rpc_request_count": self._rpc_requests(request_start),
                "rpc_phase_requests": rpc_phase_requests,
                "adaptive_split_count": fetched.adaptive_split_count,
                "duplicate_log_count": fetched.duplicate_log_count,
                "skipped_indexed_value_count": skipped,
                "unique_block_header_count": headers.count,
                "elapsed_ms": elapsed_ms,
            },
            "warnings": warnings,
        }

    def _load_labels(self) -> tuple[list[AddressLabel], str]:
        if not self.settings.labels_path.exists():
            return [], "missing"
        try:
            return load_labels_csv(self.settings.labels_path), "ok"
        except (LabelValidationError, OSError) as exc:
            raise TokenActivityQueryError(
                "label_file_invalid",
                "configured address labels could not be parsed",
            ) from exc

    def _prepare_labels(
        self,
        labels: list[AddressLabel],
        *,
        chain_id: int,
        file_status: str,
        from_time: int,
        to_time: int,
    ) -> TokenActivityLabelContext:
        if file_status == "missing":
            return TokenActivityLabelContext("missing", (), ())
        if any(
            label.chain_id == chain_id
            and label.entity_type == "cex"
            and label.source.strip().lower() == "synthetic_fixture"
            for label in labels
        ):
            raise TokenActivityQueryError(
                "label_file_invalid",
                "synthetic CEX labels are not allowed for network queries",
            )

        def overlaps_query(label: AddressLabel) -> bool:
            return (
                label.valid_to is None or label.valid_to >= from_time
            ) and (
                label.valid_from is None or label.valid_from <= to_time
            )

        direction_labels = tuple(
            label
            for label in labels
            if label.chain_id == chain_id
            and label.entity_type == "cex"
            and label.confidence >= self.settings.min_label_confidence
            and overlaps_query(label)
            and is_approved_label(label)
        )
        status = (
            "ok" if direction_labels else "insufficient_cex_coverage"
        )
        return TokenActivityLabelContext(
            status,
            tuple(labels),
            direction_labels,
        )

    def _find_first_block_at_or_after(
        self,
        headers: BlockHeaderCache,
        *,
        to_block: int,
        target_time: int,
        max_calls: int,
    ) -> int:
        low, high, calls = 0, to_block, 0
        while low < high:
            if calls >= max_calls:
                raise TokenActivityQueryError(
                    "block_search_budget_exhausted",
                    "bounded block timestamp search did not converge",
                )
            midpoint = (low + high) // 2
            calls += 1
            if headers.get(midpoint).timestamp < target_time:
                low = midpoint + 1
            else:
                high = midpoint
        return low

    def _normalize_logs(
        self,
        fetched: TokenLogFetchResult,
        *,
        query: TokenActivityQuery,
        headers: BlockHeaderCache,
        from_time: int,
        to_time: int,
    ) -> tuple[list[NormalizedTransfer], int, str | None]:
        unique: dict[str, NormalizedTransfer] = {}
        skipped_indexed = 0
        header_truncation: str | None = None
        for raw in fetched.logs:
            try:
                shape = transfer_log_shape(raw)
            except LogValidationError as exc:
                raise TokenActivityQueryError(
                    "malformed_log",
                    "provider returned a malformed Transfer log",
                ) from exc
            if shape == "indexed_value":
                skipped_indexed += 1
                continue
            try:
                block_number = parse_hex_quantity(
                    raw.get("blockNumber"), "log block number"
                )
                header = headers.get(block_number)
            except BlockHeaderBudgetExceeded:
                header_truncation = "max_block_headers"
                break
            except RpcRequestBudgetError:
                header_truncation = "max_rpc_requests"
                break
            except RpcRateLimitError:
                header_truncation = "provider_rate_limit"
                break
            except RpcTimeoutError:
                header_truncation = "provider_timeout"
                break
            except RpcError as exc:
                if unique:
                    header_truncation = "provider_unavailable"
                    break
                raise TokenActivityQueryError(
                    "rpc_unavailable",
                    "could not load Transfer block timestamps",
                ) from exc
            raw_hash = str(raw.get("blockHash") or "").lower()
            if raw_hash != header.block_hash:
                raise TokenActivityQueryError(
                    "malformed_log",
                    "Transfer block hash does not match the finalized header",
                )
            try:
                transfer = normalize_transfer_log(
                    raw,
                    block_time=header.timestamp,
                    chain_id=query.chain_id,
                    chain_name=query.chain_name,
                )
            except (LogValidationError, ValueError) as exc:
                raise TokenActivityQueryError(
                    "malformed_log",
                    "provider returned a malformed ERC-20 Transfer",
                ) from exc
            if transfer.token_address != query.contract or transfer.removed:
                raise TokenActivityQueryError(
                    "malformed_log",
                    "provider returned a non-canonical finalized Transfer",
                )
            if not from_time <= transfer.block_time <= to_time:
                continue
            existing = unique.get(transfer.event_id)
            if existing is not None and existing != transfer:
                raise TokenActivityQueryError(
                    "malformed_log",
                    "duplicate Transfer identity has conflicting facts",
                )
            unique[transfer.event_id] = transfer
        ordered = sorted(
            unique.values(),
            key=lambda item: (
                item.block_number,
                item.log_index,
                item.tx_hash,
            ),
        )
        return ordered, skipped_indexed, header_truncation

    def _price(
        self,
        query: TokenActivityQuery,
        metadata: TokenMetadata,
        observed_at: int,
    ) -> tuple[PriceQuote | None, str, str]:
        if not query.with_price:
            return None, "disabled", ""
        if self.price_provider is None:
            return None, "missing", "价格 Provider 未启用或未配置"
        try:
            quote = self.price_provider.quote_many(
                query.chain_id, [query.contract]
            ).get(query.contract)
        except Exception:
            return None, "failed", "价格 Provider 查询失败"
        if quote is None:
            return None, "missing", "当前价格不可用"
        try:
            price = Decimal(str(quote.price_usd))
        except (InvalidOperation, TypeError, ValueError):
            return None, "failed", "价格 Provider 返回了无效价格"
        if not price.is_finite() or price <= 0:
            return None, "failed", "价格 Provider 返回了无效价格"
        if quote.observed_at <= 0:
            quote = replace(quote, observed_at=observed_at)
        return quote, "available", ""

    def _record(
        self,
        transfer: NormalizedTransfer,
        metadata: TokenMetadata,
        identity_registry: LabelRegistry,
        direction_registry: LabelRegistry,
        *,
        labels_status: str,
        price_status: str,
        explorer_tx_url: str,
    ) -> dict[str, object]:
        flow = classify_transfer(transfer, metadata, direction_registry)
        from_label = identity_registry.lookup(
            transfer.chain_id, transfer.from_address, transfer.block_time
        )
        to_label = identity_registry.lookup(
            transfer.chain_id, transfer.to_address, transfer.block_time
        )
        from_direction_label = direction_registry.lookup(
            transfer.chain_id, transfer.from_address, transfer.block_time
        )
        to_direction_label = direction_registry.lookup(
            transfer.chain_id, transfer.to_address, transfer.block_time
        )
        if flow.flow_type in {"mint", "burn"}:
            flow_type = flow.flow_type
        elif labels_status != "ok":
            flow_type = "unclassified"
        elif (
            from_label is not None
            and from_label.entity_type == "cex"
            and from_direction_label is None
        ) or (
            to_label is not None
            and to_label.entity_type == "cex"
            and to_direction_label is None
        ):
            flow_type = "unclassified"
        elif flow.flow_type == "non_cex":
            def confirms_non_cex(label: AddressLabel | None) -> bool:
                return (
                    label is not None
                    and label.entity_type != "cex"
                    and label.confidence
                    >= self.settings.min_label_confidence
                    and label.source.strip().lower()
                    != "synthetic_fixture"
                )

            identities_confirm_non_cex = (
                confirms_non_cex(from_label)
                and confirms_non_cex(to_label)
            )
            flow_type = (
                "non_cex"
                if identities_confirm_non_cex
                else "unclassified"
            )
        else:
            flow_type = flow.flow_type
        amount = Decimal(transfer.amount_raw) / (
            Decimal(10) ** int(metadata.decimals or 0)
        )
        amount_usd = (
            amount * metadata.price_usd
            if metadata.price_usd is not None
            else None
        )
        return {
            "event_id": transfer.event_id,
            "block_number": transfer.block_number,
            "block_hash": transfer.block_hash,
            "block_time": transfer.block_time,
            "block_time_iso": self._utc_iso(transfer.block_time),
            "tx_hash": transfer.tx_hash,
            "log_index": transfer.log_index,
            "explorer_url": explorer_tx_url.replace(
                "{tx_hash}", transfer.tx_hash
            ),
            "token_contract": transfer.token_address,
            "from": self._address_payload(
                transfer.from_address,
                from_label,
                classification_eligible=from_direction_label is not None,
            ),
            "to": self._address_payload(
                transfer.to_address,
                to_label,
                classification_eligible=to_direction_label is not None,
            ),
            "amount_raw": str(transfer.amount_raw),
            "amount": self._decimal_string(amount),
            "amount_usd": (
                self._decimal_string(amount_usd)
                if amount_usd is not None
                else None
            ),
            "price_status": price_status,
            "flow_type": flow_type,
        }

    @staticmethod
    def _address_payload(
        address: str,
        label: AddressLabel | None,
        *,
        classification_eligible: bool,
    ) -> dict[str, object]:
        if label is None:
            return {
                "address": address,
                "known": False,
                "classification_eligible": False,
                "entity_name": "未知钱包",
                "entity_type": "",
                "address_type": "",
                "source": "",
                "confidence": 0,
            }
        return {
            "address": address,
            "known": True,
            "classification_eligible": classification_eligible,
            "entity_name": label.entity_name,
            "entity_type": label.entity_type,
            "address_type": label.address_type,
            "source": label.source,
            "confidence": label.confidence,
        }

    @classmethod
    def _summary(
        cls, records: list[dict[str, object]]
    ) -> dict[str, object]:
        counts = {
            name: 0
            for name in (
                "mint",
                "burn",
                "non_cex",
                "unclassified",
                "inflow",
                "outflow",
                "internal",
                "consolidation",
                "cross_cex",
            )
        }
        total_amount = Decimal("0")
        total_usd = Decimal("0")
        priced = 0
        senders: set[str] = set()
        receivers: set[str] = set()
        for record in records:
            flow_type = str(record["flow_type"])
            if flow_type in counts:
                counts[flow_type] += 1
            total_amount += Decimal(str(record["amount"]))
            if record["amount_usd"] is not None:
                total_usd += Decimal(str(record["amount_usd"]))
                priced += 1
            senders.add(str(record["from"]["address"]))
            receivers.add(str(record["to"]["address"]))
        return {
            "transfer_count": len(records),
            **{f"{name}_count": value for name, value in counts.items()},
            "unique_senders": len(senders),
            "unique_receivers": len(receivers),
            "total_token_amount": cls._decimal_string(total_amount),
            "total_usd": (
                cls._decimal_string(total_usd) if priced else None
            ),
            "unpriced_transfer_count": len(records) - priced,
        }

    @staticmethod
    def _largest(
        records: list[dict[str, object]],
        top_n: int,
        priced: bool,
    ) -> list[dict[str, object]]:
        def key(record: dict[str, object]) -> tuple[Decimal, int, int, str]:
            value = Decimal(
                str(
                    record["amount_usd"]
                    if priced
                    else record["amount"]
                )
            )
            return (
                -value,
                int(record["block_number"]),
                int(record["log_index"]),
                str(record["tx_hash"]),
            )

        return sorted(records, key=key)[:top_n]

    def _rpc_requests(self, start: int) -> int:
        return max(0, int(getattr(self.rpc, "request_count", 0)) - start)

    @staticmethod
    def _decimal_string(value: Decimal) -> str:
        text = format(value, "f")
        if "." in text:
            text = text.rstrip("0").rstrip(".")
        return text or "0"

    @staticmethod
    def _utc_iso(timestamp: int) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


def failed_token_activity_payload(
    error: TokenActivityQueryError,
    *,
    network_activity: bool,
) -> dict[str, object]:
    return {
        "schema_version": TOKEN_ACTIVITY_SCHEMA_VERSION,
        "status": "failed",
        "complete": False,
        "truncated": False,
        "truncation_reason": None,
        "error": error.code,
        "reason": str(error),
        "network_activity": network_activity,
        "database_writes": False,
        "telegram_calls": False,
    }
