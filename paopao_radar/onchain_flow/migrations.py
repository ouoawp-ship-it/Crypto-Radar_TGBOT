from __future__ import annotations

import sqlite3


MIGRATIONS = (
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS chain_cursors (
            chain_id INTEGER PRIMARY KEY,
            last_seen_block INTEGER NOT NULL DEFAULT 0,
            last_finalized_block INTEGER NOT NULL DEFAULT 0,
            block_hash TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS address_labels (
            chain_id INTEGER NOT NULL,
            address TEXT NOT NULL,
            entity_name TEXT NOT NULL,
            entity_type TEXT NOT NULL,
            address_type TEXT NOT NULL,
            source TEXT NOT NULL,
            confidence REAL NOT NULL,
            valid_from INTEGER,
            valid_to INTEGER,
            PRIMARY KEY (chain_id, address)
        );

        CREATE TABLE IF NOT EXISTS token_metadata (
            chain_id INTEGER NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT NOT NULL,
            name TEXT NOT NULL,
            decimals INTEGER,
            token_kind TEXT NOT NULL,
            metadata_status TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            price_usd TEXT,
            volume_24h_usd TEXT,
            historical_single_p99_usd TEXT,
            historical_15m_p99_usd TEXT,
            historical_60m_p99_usd TEXT,
            historical_window_median_usd TEXT,
            historical_window_mad_usd TEXT,
            PRIMARY KEY (chain_id, token_address)
        );

        CREATE TABLE IF NOT EXISTS transfer_events (
            event_id TEXT PRIMARY KEY,
            chain_id INTEGER NOT NULL,
            chain_name TEXT NOT NULL,
            block_number INTEGER NOT NULL,
            block_hash TEXT NOT NULL,
            block_time INTEGER NOT NULL,
            tx_hash TEXT NOT NULL,
            log_index INTEGER NOT NULL,
            token_address TEXT NOT NULL,
            from_address TEXT NOT NULL,
            to_address TEXT NOT NULL,
            amount_raw TEXT NOT NULL,
            removed INTEGER NOT NULL,
            confirmation_status TEXT NOT NULL,
            source TEXT NOT NULL,
            UNIQUE (chain_id, tx_hash, log_index)
        );

        CREATE TABLE IF NOT EXISTS flow_events (
            event_id TEXT PRIMARY KEY REFERENCES transfer_events(event_id) ON DELETE CASCADE,
            chain_id INTEGER NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT NOT NULL,
            block_time INTEGER NOT NULL,
            flow_type TEXT NOT NULL,
            exchange_from TEXT,
            exchange_to TEXT,
            counterparty_address TEXT NOT NULL,
            amount TEXT,
            amount_usd TEXT,
            label_confidence REAL NOT NULL,
            price_status TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS flow_windows (
            window_key TEXT PRIMARY KEY,
            chain_id INTEGER NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            window_end INTEGER NOT NULL,
            duration_sec INTEGER NOT NULL,
            total_usd TEXT NOT NULL,
            tx_count INTEGER NOT NULL,
            distinct_counterparties INTEGER NOT NULL,
            exchanges_json TEXT NOT NULL,
            active_15m_buckets INTEGER NOT NULL,
            min_label_confidence REAL NOT NULL,
            algorithm_version TEXT NOT NULL,
            threshold_version TEXT NOT NULL,
            UNIQUE (chain_id, token_address, direction, window_start, duration_sec)
        );

        CREATE TABLE IF NOT EXISTS alerts (
            alert_key TEXT PRIMARY KEY,
            chain_id INTEGER NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            score INTEGER NOT NULL,
            horizon TEXT NOT NULL,
            confidence TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            detection_types_json TEXT NOT NULL,
            window_start INTEGER NOT NULL,
            window_end INTEGER NOT NULL,
            total_usd TEXT NOT NULL,
            tx_count INTEGER NOT NULL,
            exchanges_json TEXT NOT NULL,
            label_confidence REAL NOT NULL,
            price_status TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            severity_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            UNIQUE (alert_key)
        );

        CREATE TABLE IF NOT EXISTS alert_deliveries (
            delivery_key TEXT PRIMARY KEY,
            alert_key TEXT NOT NULL REFERENCES alerts(alert_key) ON DELETE CASCADE,
            status TEXT NOT NULL,
            sent INTEGER NOT NULL,
            reason TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_transfer_events_token_time
            ON transfer_events(chain_id, token_address, block_time);
        CREATE INDEX IF NOT EXISTS idx_flow_events_token_time
            ON flow_events(chain_id, token_address, block_time);
        CREATE INDEX IF NOT EXISTS idx_flow_windows_token_time
            ON flow_windows(chain_id, token_address, window_start);
        """,
    ),
)


def apply_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at INTEGER NOT NULL DEFAULT (unixepoch())
        )
        """
    )
    applied = {
        int(row[0])
        for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
    }
    for version, script in MIGRATIONS:
        if version in applied:
            continue
        with conn:
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
