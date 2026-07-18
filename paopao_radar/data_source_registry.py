from __future__ import annotations

from typing import Any


DATA_SOURCE_REGISTRY_VERSION = "2026-07-18"

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
        "id": "binance_futures_stream", "provider": "Binance",
        "surface": "USDT-M Futures public WebSocket streams", "official": True,
        "transport": "wss_stream",
        "metrics": ["aggregate_trades", "minute_ohlc", "futures_cvd", "long_liquidations", "short_liquidations"],
        "product_roles": ["realtime_market_facts", "radar", "surge", "short_horizon_ambush", "offline_outcomes"],
        "rights_status": "provider_terms_apply", "retention_policy": "minute_features_7d",
        "content_policy": "derived_numeric_facts_only", "fallback": "retain_rest_cockpit_and_mark_realtime_unavailable",
    },
    {
        "id": "bybit_linear_stream", "provider": "Bybit",
        "surface": "V5 linear public trade and all-liquidation streams", "official": True,
        "transport": "wss_stream",
        "metrics": ["public_trades", "minute_ohlc", "futures_cvd", "long_liquidations", "short_liquidations"],
        "product_roles": ["realtime_market_facts", "radar", "surge", "short_horizon_ambush", "offline_outcomes"],
        "rights_status": "provider_terms_apply", "retention_policy": "minute_features_7d",
        "content_policy": "derived_numeric_facts_only", "fallback": "exclude_exchange_and_expose_partial_health",
    },
    {
        "id": "okx_swap_stream", "provider": "OKX",
        "surface": "V5 public SWAP trades and public instrument metadata", "official": True,
        "transport": "wss_stream_plus_https_metadata",
        "metrics": ["public_trades", "minute_ohlc", "futures_cvd", "contract_value_mapping"],
        "product_roles": ["realtime_market_facts", "radar", "surge", "short_horizon_ambush", "offline_outcomes"],
        "rights_status": "provider_terms_apply", "retention_policy": "minute_features_7d",
        "content_policy": "derived_numeric_facts_only", "fallback": "exclude_exchange_and_expose_partial_health",
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
        "id": "panews_zh_rss", "provider": "PANews", "surface": "Official Chinese RSS feed",
        "official": True, "transport": "https_rss",
        "metrics": ["title", "short_excerpt", "published_at", "canonical_url", "linked_symbols"],
        "product_roles": ["news_events", "info_zh_stream"], "rights_status": "public_rss_link",
        "retention_policy": "metadata_and_short_excerpt_90d", "content_policy": "do_not_republish_full_article",
        "fallback": "serve_last_successful_index_with_stale_marker",
    },
    {
        "id": "english_public_rss", "provider": "Decrypt / Kraken", "surface": "Publisher RSS feeds",
        "official": True, "transport": "https_rss",
        "metrics": ["title", "short_excerpt", "published_at", "canonical_url", "linked_symbols"],
        "product_roles": ["news_events", "info_en_stream"], "rights_status": "public_rss_link",
        "retention_policy": "metadata_and_short_excerpt_90d", "content_policy": "do_not_republish_full_article",
        "fallback": "exclude_failed_publisher_and_expose_partial_health",
    },
    {
        "id": "bluesky_kol_public", "provider": "Bluesky", "surface": "Official public author-feed API",
        "official": True, "transport": "https_rest",
        "metrics": ["post_excerpt", "author", "published_at", "engagement", "linked_symbols"],
        "product_roles": ["info_kol_stream"], "rights_status": "public_social_link",
        "retention_policy": "metadata_and_short_excerpt_90d", "content_policy": "public_metadata_with_canonical_link",
        "fallback": "serve_last_successful_index_with_stale_marker",
    },
    {
        "id": "bluesky_crypto_feed_public", "provider": "Bluesky", "surface": "Official public custom-feed API",
        "official": True, "transport": "https_rest",
        "metrics": ["post_excerpt", "author", "published_at", "engagement", "linked_symbols", "rule_sentiment"],
        "product_roles": ["info_plaza_stream", "social_sentiment_rank"], "rights_status": "public_social_link",
        "retention_policy": "metadata_and_short_excerpt_90d", "content_policy": "public_metadata_with_canonical_link",
        "fallback": "serve_last_successful_index_with_stale_marker",
    },
    {
        "id": "binance_market_metadata", "provider": "Binance",
        "surface": "Public market metadata", "official": True, "transport": "https_public_content",
        "metrics": ["market_cap"], "product_roles": ["market_fact_enrichment"],
        "rights_status": "provider_terms_apply", "retention_policy": "derived_numeric_facts_30d",
        "content_policy": "numeric_facts_only", "fallback": "use_coinpaprika_market_cap",
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
