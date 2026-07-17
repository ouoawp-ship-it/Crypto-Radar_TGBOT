from __future__ import annotations

from typing import Any


DATA_SOURCE_REGISTRY_VERSION = "2026-07-17"

# Secret-free policy metadata. Provider terms must be reviewed before a source
# or its usage is expanded; this registry makes that boundary testable.
DATA_SOURCES: tuple[dict[str, Any], ...] = (
    {
        "id": "binance_futures_public", "provider": "Binance",
        "surface": "USDT-M Futures public REST API", "official": True,
        "transport": "https_rest",
        "metrics": ["price", "quote_volume", "funding", "open_interest", "futures_cvd_estimate"],
        "product_roles": ["market_facts", "radar", "signal_evidence"],
        "rights_status": "provider_terms_apply", "retention_policy": "derived_numeric_facts_30d",
        "content_policy": "numeric_facts_only", "fallback": "retain_last_verified_fact_and_mark_stale",
    },
    {
        "id": "binance_spot_public", "provider": "Binance",
        "surface": "Spot public REST API", "official": True, "transport": "https_rest",
        "metrics": ["spot_klines", "spot_cvd_estimate"], "product_roles": ["market_facts", "radar"],
        "rights_status": "provider_terms_apply", "retention_policy": "derived_numeric_facts_30d",
        "content_policy": "numeric_facts_only", "fallback": "board_unavailable_without_zero_fabrication",
    },
    {
        "id": "binance_announcements", "provider": "Binance", "surface": "Official announcements",
        "official": True, "transport": "https_public_content",
        "metrics": ["title", "published_at", "canonical_url", "linked_symbols"],
        "product_roles": ["news_events", "announcement_signals"], "rights_status": "official_link_only",
        "retention_policy": "metadata_and_short_excerpt_90d", "content_policy": "do_not_republish_full_article",
        "fallback": "serve_last_successful_index_with_stale_marker",
    },
    {
        "id": "coinpaprika_market", "provider": "CoinPaprika", "surface": "Public market API",
        "official": True, "transport": "https_rest", "metrics": ["market_cap"],
        "product_roles": ["market_fact_enrichment"], "rights_status": "provider_terms_apply",
        "retention_policy": "derived_numeric_facts_30d", "content_policy": "numeric_facts_only",
        "fallback": "metric_unavailable",
    },
    *(
        {
            "id": f"{provider.lower()}_funding_public", "provider": provider,
            "surface": "Official public derivatives API", "official": True, "transport": "https_rest",
            "metrics": ["funding", "contract_mapping"],
            "product_roles": ["cross_exchange_funding_confirmation"],
            "rights_status": "provider_terms_apply", "retention_policy": "derived_numeric_facts_30d",
            "content_policy": "numeric_facts_only", "fallback": "exclude_source_from_consensus",
        }
        for provider in ("Bybit", "OKX", "Bitget", "Gate")
    ),
)


def data_source_registry_payload() -> dict[str, Any]:
    sources = [dict(item) for item in DATA_SOURCES]
    return {
        "schema_version": DATA_SOURCE_REGISTRY_VERSION,
        "governance_status": "declared",
        "source_count": len(sources),
        "sources": sources,
        "policy": {
            "secrets": "never_exposed",
            "missing_data": "null_with_status_never_fabricated_zero",
            "terms_review": "required_before_new_source_or_usage_expansion",
            "provenance": "every_persisted_fact_has_source_and_observed_at",
        },
    }


__all__ = ["DATA_SOURCE_REGISTRY_VERSION", "DATA_SOURCES", "data_source_registry_payload"]
