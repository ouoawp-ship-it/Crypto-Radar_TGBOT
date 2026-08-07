from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from config import Settings
from shared.asset_classification import (
    classify_binance_instrument,
    is_stable_crypto_asset,
)
from shared.binance_data import BinanceDataSource
from shared.cmc_data import CmcClient

from .configuration import AltcoinAnomalyConfig
from .mapping import CmcIdentityResolver, load_mapping_overrides
from .models import (
    CandidateSnapshot,
    MappingRecord,
    SCHEMA_VERSION,
    calculate_oi_market_cap_ratio,
    calculate_oi_value_usd,
    finite_float,
)
from .rules import CandidateThresholds, apply_candidate_rules
from .state import CandidatePoolStore, build_pool_document


class AltcoinAnomalyDataUnavailable(RuntimeError):
    pass


def _get(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _iso_from_epoch(value: Any) -> str | None:
    parsed = finite_float(value)
    if parsed is None or parsed <= 0:
        return None
    if parsed > 10_000_000_000:
        parsed /= 1000.0
    try:
        return datetime.fromtimestamp(parsed, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_iso(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_stale(value: str | None, *, now_ts: float, max_age_sec: int) -> bool:
    parsed = _parse_iso(value)
    if parsed is None:
        return True
    age = now_ts - parsed.timestamp()
    return age < -300 or age > max_age_sec


def _result_entries(result: Any) -> tuple[Any, ...]:
    entries = _get(result, "entries", _get(result, "items", ()))
    if isinstance(entries, Mapping):
        return tuple(entries.values())
    return tuple(entries) if isinstance(entries, (list, tuple)) else ()


def _result_quotes(result: Any) -> dict[int, Any]:
    quotes = _get(result, "quotes", _get(result, "items", {}))
    values: Iterable[Any]
    if isinstance(quotes, Mapping):
        values = quotes.values()
    elif isinstance(quotes, (list, tuple)):
        values = quotes
    else:
        values = ()
    output: dict[int, Any] = {}
    for item in values:
        try:
            cmc_id = int(_get(item, "cmc_id", _get(item, "id", 0)) or 0)
        except (TypeError, ValueError):
            continue
        if cmc_id > 0:
            output[cmc_id] = item
    return output


def _result_metric(result: Any, *names: str, default: Any = 0) -> Any:
    for name in names:
        value = _get(result, name, None)
        if value is not None:
            return value
    return default


def _premium_rows(source: BinanceDataSource, collected_at: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for item in source.premium_index():
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        source_at = _iso_from_epoch(item.get("time")) or collected_at
        result[symbol] = {
            "mark_price": item.get("markPrice"),
            "funding_rate": item.get("lastFundingRate"),
            "updated_at": source_at,
        }
    return result


def _fetch_open_interest(
    source: BinanceDataSource,
    symbols: Iterable[str],
    *,
    workers: int,
) -> dict[str, dict[str, Any]]:
    unique = sorted({str(symbol).upper() for symbol in symbols if symbol})

    def fetch(symbol: str) -> tuple[str, dict[str, Any]]:
        rows = source.open_interest_hist(symbol, period="5m", limit=1)
        row = rows[-1] if rows and isinstance(rows[-1], Mapping) else {}
        raw = row.get("sumOpenInterest")
        upstream_value = row.get("sumOpenInterestValue")
        return symbol, {
            "open_interest_raw": raw,
            "open_interest_unit": "contract_base_asset_quantity" if raw is not None else None,
            "upstream_oi_value_usd": upstream_value,
            "updated_at": _iso_from_epoch(row.get("timestamp")),
        }

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="altcoin-oi") as executor:
        futures = {executor.submit(fetch, symbol): symbol for symbol in unique}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                returned_symbol, row = future.result()
            except Exception:
                row = {}
                returned_symbol = symbol
            results[returned_symbol] = row
    return results


def _snapshot_quality(
    mapping: MappingRecord,
    *,
    missing: list[str],
    stale: list[str],
    invalid: list[str],
) -> str:
    if mapping.mapping_method == "ambiguous":
        return "mapping_conflict"
    if not mapping.is_formal:
        return "unmapped"
    if invalid:
        return "invalid"
    if stale:
        return "stale"
    if missing:
        return "partial"
    return "complete"


def _build_snapshot(
    mapping: MappingRecord,
    *,
    quote: Any | None,
    quote_source: str | None,
    premium: Mapping[str, Any] | None,
    oi: Mapping[str, Any] | None,
    collected_at: str,
    now_ts: float,
    config: AltcoinAnomalyConfig,
) -> CandidateSnapshot:
    premium = premium or {}
    oi = oi or {}
    missing: list[str] = []
    stale: list[str] = []
    invalid: list[str] = []

    raw_market_cap = _get(quote, "market_cap_usd") if quote is not None else None
    market_cap = finite_float(raw_market_cap, minimum=0.0)
    if market_cap is None:
        (missing if quote is None else invalid).append("market_cap_usd")
    elif market_cap <= 0:
        invalid.append("market_cap_usd")
        market_cap = None
    market_cap_at = str(_get(quote, "last_updated", "") or "") or None
    if market_cap is not None and _is_stale(
        market_cap_at,
        now_ts=now_ts,
        max_age_sec=config.cmc_max_data_age_sec,
    ):
        stale.append("market_cap_usd")

    raw_mark_price = premium.get("mark_price")
    mark_price = finite_float(raw_mark_price, minimum=0.0)
    if mark_price is None:
        (missing if raw_mark_price is None else invalid).append("mark_price")
    elif mark_price <= 0:
        invalid.append("mark_price")
        mark_price = None
    mark_at = str(premium.get("updated_at") or "") or None
    if mark_price is not None and _is_stale(
        mark_at,
        now_ts=now_ts,
        max_age_sec=config.binance_oi_max_age_sec,
    ):
        stale.append("mark_price")

    raw_funding_rate = premium.get("funding_rate")
    funding_rate = finite_float(raw_funding_rate)
    if funding_rate is None:
        (missing if raw_funding_rate is None else invalid).append("funding_rate")
    funding_at = str(premium.get("updated_at") or "") or None
    if funding_rate is not None and _is_stale(
        funding_at,
        now_ts=now_ts,
        max_age_sec=config.funding_max_age_sec,
    ):
        stale.append("funding_rate")

    raw_oi_input = oi.get("open_interest_raw")
    upstream_oi_input = oi.get("upstream_oi_value_usd")
    raw_oi = finite_float(raw_oi_input, minimum=0.0)
    upstream_oi_value = finite_float(upstream_oi_input, minimum=0.0)
    oi_method: str | None = None
    if upstream_oi_input is not None:
        oi_value = (
            calculate_oi_value_usd(upstream_oi_value, unit="usd_notional")
            if upstream_oi_value is not None
            else None
        )
        if oi_value is not None:
            oi_method = "binance_sum_open_interest_value"
    else:
        oi_value = calculate_oi_value_usd(
            raw_oi,
            unit=str(oi.get("open_interest_unit") or ""),
            mark_price=mark_price,
        )
        if oi_value is not None:
            oi_method = "open_interest_times_mark_price"
    if raw_oi is None:
        (missing if raw_oi_input is None else invalid).append("open_interest_raw")
    if oi_value is None:
        if upstream_oi_input is not None and upstream_oi_value is None:
            invalid.append("oi_value_usd")
        else:
            missing.append("oi_value_usd")
    oi_at = str(oi.get("updated_at") or "") or None
    if oi_value is not None and _is_stale(
        oi_at,
        now_ts=now_ts,
        max_age_sec=config.binance_oi_max_age_sec,
    ):
        stale.append("oi_value_usd")

    if not mapping.is_formal:
        if "cmc_id" not in missing:
            missing.append("cmc_id")
    ratio = calculate_oi_market_cap_ratio(oi_value, market_cap)
    snapshot = CandidateSnapshot(
        schema_version=SCHEMA_VERSION,
        symbol=mapping.binance_symbol,
        base_asset=mapping.base_asset,
        normalized_asset=mapping.normalized_asset,
        contract_multiplier=mapping.contract_multiplier,
        exchange="BINANCE",
        contract_type="USDT_PERPETUAL",
        cmc_id=mapping.cmc_id,
        mapping_method=mapping.mapping_method,
        mapping_confidence=mapping.mapping_confidence,
        market_cap_usd=market_cap,
        market_cap_source=(
            f"coinmarketcap_v3_quotes_latest:{quote_source or 'stale_cache'}"
            if quote is not None
            else None
        ),
        market_cap_updated_at=market_cap_at,
        open_interest_raw=raw_oi,
        open_interest_unit=str(oi.get("open_interest_unit") or "") or None,
        oi_value_usd=oi_value,
        mark_price=mark_price,
        funding_rate=funding_rate,
        oi_market_cap_ratio=ratio,
        data_quality=_snapshot_quality(mapping, missing=missing, stale=stale, invalid=invalid),
        missing_fields=sorted(set(missing)),
        collected_at=collected_at,
        open_interest_updated_at=oi_at,
        mark_price_updated_at=mark_at,
        funding_rate_updated_at=funding_at,
        stale_fields=sorted(set(stale)),
        invalid_fields=sorted(set(invalid)),
        mapping_evidence=list(mapping.mapping_evidence),
        mapping_rejection_reason=mapping.rejection_reason,
        oi_value_method=oi_method,
        binance_oi_usd=oi_value,
        binance_oi_market_cap_ratio=ratio,
        binance_oi_source=(
            "binance_open_interest_hist.sumOpenInterestValue"
            if oi_method == "binance_sum_open_interest_value"
            else "binance_open_interest_hist.sumOpenInterest*premiumIndex.markPrice"
            if oi_method == "open_interest_times_mark_price"
            else None
        ),
        global_oi_usd=None,
        global_oi_market_cap_ratio=None,
        global_oi_source=None,
    )
    return apply_candidate_rules(
        snapshot,
        CandidateThresholds(
            market_cap_max_usd=config.market_cap_max_usd,
            short_squeeze_min_ratio=config.short_squeeze_min_ratio,
            short_squeeze_max_funding_rate=(
                config.short_squeeze_max_funding_rate
            ),
            high_leverage_min_ratio=config.high_leverage_min_ratio,
        ),
    )


def scan_candidate_pool(
    settings: Settings,
    *,
    source: BinanceDataSource | None = None,
    cmc_client: Any | None = None,
    now_ts: float | None = None,
) -> dict[str, Any]:
    config = AltcoinAnomalyConfig.from_settings(settings)
    timestamp = float(now_ts if now_ts is not None else time.time())
    collected_at = datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    pool_store = CandidatePoolStore(config.candidate_snapshot_path, data_dir=settings.data_dir)
    previous = pool_store.load()
    owned_source = source is None
    loaded_source = source or BinanceDataSource(
        settings,
        oi_hist_budget=config.oi_request_budget,
    )
    owned_cmc = cmc_client is None
    loaded_cmc = cmc_client or CmcClient(
        api_key=config.cmc_api_key,
        cache_path=config.cmc_cache_path,
        cache_ttl_sec=config.cmc_cache_ttl_sec,
        max_data_age_sec=config.cmc_max_data_age_sec,
        connect_timeout_sec=config.cmc_connect_timeout_sec,
        read_timeout_sec=config.cmc_read_timeout_sec,
        retries=config.cmc_retry,
        backoff_base_sec=config.cmc_backoff_base_sec,
        min_request_interval_sec=config.cmc_min_request_interval_sec,
        batch_size=config.cmc_batch_size,
    )
    try:
        contracts = loaded_source.usdt_perp_symbols()
        if not contracts:
            raise AltcoinAnomalyDataUnavailable("Binance合约目录不可用")
        eligible: list[Mapping[str, Any]] = []
        excluded_reasons: dict[str, int] = {}
        excluded_contracts: list[dict[str, str]] = []
        configured_exclusions = {
            str(value).strip().upper()
            for value in settings.excluded_base_assets
            if str(value).strip()
        }
        for contract in contracts:
            base_asset = str(contract.get("baseAsset") or "").strip().upper()
            if base_asset in configured_exclusions:
                excluded_reasons["configured_exclusion"] = (
                    excluded_reasons.get("configured_exclusion", 0) + 1
                )
                excluded_contracts.append({
                    "symbol": str(contract.get("symbol") or "").strip().upper(),
                    "base_asset": base_asset,
                    "reason": "excluded_asset",
                    "detail": "configured_exclusion",
                })
                continue
            if is_stable_crypto_asset(base_asset):
                excluded_reasons["stablecoin"] = (
                    excluded_reasons.get("stablecoin", 0) + 1
                )
                excluded_contracts.append({
                    "symbol": str(contract.get("symbol") or "").strip().upper(),
                    "base_asset": base_asset,
                    "reason": "excluded_asset",
                    "detail": "stablecoin",
                })
                continue
            classification = classify_binance_instrument(
                str(contract.get("symbol") or ""),
                contract,
            )
            subclass = str(classification.get("asset_subclass") or "unknown")
            if classification.get("asset_family") == "crypto" and subclass == "altcoin":
                eligible.append(contract)
            else:
                excluded_reasons[subclass] = excluded_reasons.get(subclass, 0) + 1
                excluded_contracts.append({
                    "symbol": str(contract.get("symbol") or "").strip().upper(),
                    "base_asset": base_asset,
                    "reason": "excluded_asset",
                    "detail": subclass,
                })

        if not eligible:
            raise AltcoinAnomalyDataUnavailable("没有可用的山寨 USDT 永续合约")

        marketing_rows = loaded_source.marketing_symbols()
        map_result = loaded_cmc.load_map()
        entries = _result_entries(map_result)
        if not entries:
            raise AltcoinAnomalyDataUnavailable("CMC身份目录不可用")
        overrides = load_mapping_overrides(config.mapping_overrides_path)
        mapping_summary = CmcIdentityResolver(
            entries,
            overrides=overrides,
            verified_at=collected_at,
        ).resolve_many(eligible, marketing_rows)
        trusted_ids = sorted({record.cmc_id for record in mapping_summary.records if record.is_formal and record.cmc_id})
        quotes_result = loaded_cmc.quotes_latest(trusted_ids)
        quotes = _result_quotes(quotes_result)
        source_by_id = _get(quotes_result, "source_by_id", {})
        if not isinstance(source_by_id, Mapping):
            source_by_id = {}
        stale_quote_rows = _get(quotes_result, "stale_quotes", {})
        if isinstance(stale_quote_rows, Mapping):
            for cmc_id, quote in stale_quote_rows.items():
                try:
                    normalized_id = int(cmc_id)
                except (TypeError, ValueError):
                    continue
                quotes.setdefault(normalized_id, quote)
        premium = _premium_rows(loaded_source, collected_at)
        trusted_symbols = [record.binance_symbol for record in mapping_summary.records if record.is_formal]
        oi_rows = _fetch_open_interest(
            loaded_source,
            trusted_symbols,
            workers=config.oi_workers,
        )
        snapshots = [
            _build_snapshot(
                record,
                quote=quotes.get(record.cmc_id or -1) if record.is_formal else None,
                quote_source=(
                    str(source_by_id.get(record.cmc_id) or "stale_cache")
                    if record.is_formal and (record.cmc_id or -1) in quotes
                    else None
                ),
                premium=premium.get(record.binance_symbol),
                oi=oi_rows.get(record.binance_symbol) if record.is_formal else None,
                collected_at=collected_at,
                now_ts=timestamp,
                config=config,
            )
            for record in mapping_summary.records
        ]
        reason_counts = dict(mapping_summary.reason_counts)
        for snapshot in snapshots:
            for field in snapshot.missing_fields:
                if field == "cmc_id":
                    continue
                key = f"missing_{field.removesuffix('_usd')}"
                reason_counts[key] = reason_counts.get(key, 0) + 1
            for field in snapshot.invalid_fields:
                key = f"invalid_{field.removesuffix('_usd')}"
                reason_counts[key] = reason_counts.get(key, 0) + 1
            for field in snapshot.stale_fields:
                key = f"stale_{field.removesuffix('_usd')}"
                reason_counts[key] = reason_counts.get(key, 0) + 1
        trusted = mapping_summary.trusted_count
        eligible_count = len(eligible)
        map_source = str(_result_metric(map_result, "source", default="network"))
        source_values = set(source_by_id.values()) if isinstance(source_by_id, Mapping) else set()
        missing_quote_ids = _get(quotes_result, "missing_ids", ())
        cmc_diagnostics = (
            loaded_cmc.diagnostics()
            if hasattr(loaded_cmc, "diagnostics")
            else {}
        )
        cmc_last_error = str(cmc_diagnostics.get("last_error") or "") if isinstance(cmc_diagnostics, Mapping) else ""
        map_from_cache = map_source in {"cache", "fallback_cache"}
        quotes_from_cache = bool(source_values & {"cache", "fallback_cache"})
        fallback_used = map_source == "fallback_cache" or "fallback_cache" in source_values
        network_used = map_source == "network" or "network" in source_values
        regular_cache_used = map_source == "cache" or "cache" in source_values
        if fallback_used:
            network_status = "缓存降级"
        elif missing_quote_ids or cmc_last_error:
            network_status = "部分降级"
        elif network_used and regular_cache_used:
            network_status = "在线+缓存"
        elif regular_cache_used:
            network_status = "缓存命中"
        else:
            network_status = "在线"
        oi_budget = loaded_source.budget.snapshot().get("open_interest_hist", {})
        binance_quality = (
            loaded_source.quality.snapshot()
            if hasattr(loaded_source, "quality")
            else {}
        )
        pool = build_pool_document(
            snapshots,
            generated_at=collected_at,
            universe={
                "loaded_usdt_perpetuals": len(contracts),
                "eligible_altcoin_contracts": eligible_count,
                "excluded_contracts": len(contracts) - eligible_count,
                "excluded_reason_counts": dict(sorted(excluded_reasons.items())),
                "excluded_contract_records": sorted(
                    excluded_contracts,
                    key=lambda item: item["symbol"],
                ),
            },
            mapping_stats={
                "trusted_count": trusted,
                "trusted_coverage_ratio": trusted / eligible_count if eligible_count else 0.0,
                "not_formally_mapped_count": max(0, eligible_count - trusted),
                "diagnostic_count": mapping_summary.diagnostic_count,
                "conflict_count": mapping_summary.conflict_count,
                "unmapped_count": mapping_summary.unmapped_count,
                "reason_counts": dict(sorted(reason_counts.items())),
            },
            rule_parameters={
                "market_cap_max_usd": config.market_cap_max_usd,
                "short_squeeze_min_ratio": config.short_squeeze_min_ratio,
                "short_squeeze_max_funding_rate": (
                    config.short_squeeze_max_funding_rate
                ),
                "high_leverage_min_ratio": config.high_leverage_min_ratio,
            },
            mapping_records=mapping_summary.records,
            previous=previous,
            data_sources={
                "market_cap": "coinmarketcap_v3_quotes_latest_by_id",
                "identity": "coinmarketcap_v1_map+binance_marketing_cmc_id",
                "open_interest": "binance_open_interest_hist_sumOpenInterestValue",
                "funding_and_mark_price": "binance_premium_index",
                "global_open_interest_enabled": False,
            },
            diagnostics={
                "network_status": network_status,
                "cmc_map_cache_hit": map_from_cache,
                "cmc_quotes_cache_hit": quotes_from_cache,
                "cmc_map_source": map_source,
                "cmc_map_request_count": int(_result_metric(map_result, "request_pages", default=0) or 0),
                "cmc_quote_request_count": int(_result_metric(quotes_result, "request_batches", default=0) or 0),
                "cmc_quote_cache_hits": int(_result_metric(quotes_result, "cache_hits", default=0) or 0),
                "cmc_quote_cache_fallbacks": int(_result_metric(quotes_result, "cache_fallbacks", default=0) or 0),
                "cmc_quote_asset_count": len(quotes),
                "cmc_quote_stale_count": len(stale_quote_rows) if isinstance(stale_quote_rows, Mapping) else 0,
                "cmc_quote_missing_count": len(missing_quote_ids) if isinstance(missing_quote_ids, (list, tuple)) else 0,
                "cmc_last_error": cmc_last_error,
                "cmc_client": dict(cmc_diagnostics) if isinstance(cmc_diagnostics, Mapping) else {},
                "binance_oi_request_count": int(oi_budget.get("used") or 0),
                "binance_oi_request_budget": int(oi_budget.get("limit") or 0),
                "binance_oi_cache_hits": 0,
                "binance_quality": binance_quality,
                "realtime_listening_status": "p1_not_started_dry_run",
            },
        )
        pool_store.save(pool)
        return pool
    finally:
        if owned_source:
            loaded_source.close()
        if owned_cmc and hasattr(loaded_cmc, "close"):
            loaded_cmc.close()


def load_cached_pool(settings: Settings, *, now_ts: float | None = None) -> dict[str, Any]:
    config = AltcoinAnomalyConfig.from_settings(settings, cache_only=True)
    pool = CandidatePoolStore(
        config.candidate_snapshot_path,
        data_dir=settings.data_dir,
    ).load()
    if pool is None:
        raise AltcoinAnomalyDataUnavailable("没有可用的候选池缓存")
    generated = _parse_iso(pool.get("generated_at"))
    timestamp = float(now_ts if now_ts is not None else time.time())
    max_age = min(
        config.cmc_max_data_age_sec,
        config.binance_oi_max_age_sec,
    )
    if generated is None or timestamp - generated.timestamp() > max_age or timestamp < generated.timestamp() - 300:
        raise AltcoinAnomalyDataUnavailable("候选池缓存已过期")
    expected_rule_parameters = {
        "market_cap_max_usd": config.market_cap_max_usd,
        "short_squeeze_min_ratio": config.short_squeeze_min_ratio,
        "short_squeeze_max_funding_rate": (
            config.short_squeeze_max_funding_rate
        ),
        "high_leverage_min_ratio": config.high_leverage_min_ratio,
    }
    if pool.get("rule_parameters") != expected_rule_parameters:
        raise AltcoinAnomalyDataUnavailable("候选池规则参数与当前配置不一致")
    for snapshot in pool.get("snapshots") or []:
        if not isinstance(snapshot, Mapping) or not snapshot.get("candidate_tags"):
            continue
        required_times = {
            "market_cap_updated_at": config.cmc_max_data_age_sec,
            "open_interest_updated_at": config.binance_oi_max_age_sec,
            "mark_price_updated_at": config.binance_oi_max_age_sec,
        }
        if "short_squeeze_candidate" in set(snapshot.get("candidate_tags") or []):
            required_times["funding_rate_updated_at"] = config.funding_max_age_sec
        if any(
            _is_stale(
                str(snapshot.get(field) or "") or None,
                now_ts=timestamp,
                max_age_sec=age_limit,
            )
            for field, age_limit in required_times.items()
        ):
            raise AltcoinAnomalyDataUnavailable("候选池关键字段已过期")
        market_cap = finite_float(snapshot.get("market_cap_usd"), minimum=0.0)
        oi_value = finite_float(snapshot.get("binance_oi_usd"), minimum=0.0)
        mark_price = finite_float(snapshot.get("mark_price"), minimum=0.0)
        if market_cap is None or market_cap <= 0 or oi_value is None or mark_price is None or mark_price <= 0:
            raise AltcoinAnomalyDataUnavailable("候选池关键字段无效")
        if "short_squeeze_candidate" in set(snapshot.get("candidate_tags") or []):
            funding = finite_float(snapshot.get("funding_rate"))
            if funding is None:
                raise AltcoinAnomalyDataUnavailable("候选池资金费率无效")
    cached = dict(pool)
    diagnostics = dict(cached.get("diagnostics") or {})
    diagnostics.update({
        "network_status": "仅缓存离线",
        "candidate_snapshot_cache_hit": True,
        "candidate_snapshot_cache_age_sec": max(0, int(timestamp - generated.timestamp())),
    })
    cached["diagnostics"] = diagnostics
    return cached


__all__ = [
    "AltcoinAnomalyDataUnavailable",
    "load_cached_pool",
    "scan_candidate_pool",
]
