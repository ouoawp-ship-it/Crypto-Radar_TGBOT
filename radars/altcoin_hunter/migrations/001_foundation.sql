CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at_ms INTEGER NOT NULL
);

CREATE TABLE instruments (
    exchange TEXT NOT NULL,
    market TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    canonical_asset_id TEXT,
    eligibility_status TEXT NOT NULL,
    listing_stage TEXT NOT NULL,
    activity_tier TEXT NOT NULL,
    sampling_priority TEXT NOT NULL CHECK(sampling_priority IN ('BASE', 'ELEVATED', 'CRITICAL')),
    effective_at_ms INTEGER NOT NULL,
    metadata_version INTEGER NOT NULL,
    content_checksum TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (exchange, market, instrument_id)
);
CREATE INDEX idx_instruments_symbol ON instruments(symbol);

CREATE TABLE universe_history (
    change_id TEXT PRIMARY KEY,
    exchange TEXT NOT NULL,
    market TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    effective_at_ms INTEGER NOT NULL,
    previous_checksum TEXT,
    content_checksum TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX idx_universe_history_instrument_time
    ON universe_history(exchange, market, instrument_id, effective_at_ms);

CREATE TABLE market_buckets_1m (
    source TEXT NOT NULL,
    exchange TEXT NOT NULL,
    market TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    symbol TEXT NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    connection_epoch INTEGER NOT NULL,
    quality_status TEXT NOT NULL,
    content_checksum TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (source, exchange, market, instrument_id, start_ms)
);
CREATE INDEX idx_market_buckets_time ON market_buckets_1m(start_ms);
CREATE INDEX idx_market_buckets_symbol_time ON market_buckets_1m(symbol, start_ms);

CREATE TABLE baseline_state (
    source TEXT NOT NULL,
    exchange TEXT NOT NULL,
    market TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    feature TEXT NOT NULL,
    window_sec INTEGER NOT NULL,
    baseline_version TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (source, exchange, market, instrument_id, feature, window_sec, baseline_version)
);

CREATE TABLE ingest_checkpoints (
    checkpoint_key TEXT PRIMARY KEY,
    checkpoint_kind TEXT NOT NULL CHECK(checkpoint_kind IN ('source', 'batch')),
    source TEXT NOT NULL DEFAULT '',
    exchange TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',
    instrument_id TEXT NOT NULL DEFAULT '',
    committed_through_ms INTEGER NOT NULL,
    batch_id TEXT NOT NULL,
    content_checksum TEXT NOT NULL,
    record_json TEXT NOT NULL
);
CREATE INDEX idx_checkpoints_kind ON ingest_checkpoints(checkpoint_kind);

CREATE TABLE health_rollups_1m (
    source TEXT NOT NULL,
    exchange TEXT NOT NULL,
    market TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    minute_ms INTEGER NOT NULL,
    content_checksum TEXT NOT NULL,
    record_json TEXT NOT NULL,
    PRIMARY KEY (source, exchange, market, instrument_id, minute_ms)
);
CREATE INDEX idx_health_rollups_time ON health_rollups_1m(minute_ms);
