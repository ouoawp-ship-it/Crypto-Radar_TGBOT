from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable


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
    (
        2,
        """
        ALTER TABLE chain_cursors
            ADD COLUMN provider_status TEXT NOT NULL DEFAULT 'unknown';

        ALTER TABLE token_metadata
            ADD COLUMN price_source TEXT NOT NULL DEFAULT '';
        ALTER TABLE token_metadata
            ADD COLUMN price_observed_at INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE token_metadata
            ADD COLUMN retry_after INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE flow_events
            ADD COLUMN block_number INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE flow_events
            ADD COLUMN price_source TEXT NOT NULL DEFAULT '';
        ALTER TABLE flow_events
            ADD COLUMN price_observed_at INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE flow_events
            ADD COLUMN block_hash TEXT NOT NULL DEFAULT '';
        ALTER TABLE flow_events
            ADD COLUMN status TEXT NOT NULL DEFAULT 'active';

        ALTER TABLE alerts
            ADD COLUMN gross_inflow_usd TEXT;
        ALTER TABLE alerts
            ADD COLUMN gross_outflow_usd TEXT;
        ALTER TABLE alerts
            ADD COLUMN net_flow_usd TEXT;
        ALTER TABLE alerts
            ADD COLUMN duration_sec INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE alerts
            ADD COLUMN inflow_tx_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE alerts
            ADD COLUMN outflow_tx_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE alerts
            ADD COLUMN distinct_inbound_counterparties INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE alerts
            ADD COLUMN distinct_outbound_counterparties INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE alerts
            ADD COLUMN evaluation_block INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE alerts
            ADD COLUMN price_source TEXT NOT NULL DEFAULT '';
        ALTER TABLE alerts
            ADD COLUMN price_observed_at INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE alerts
            ADD COLUMN chain_name TEXT NOT NULL DEFAULT '';

        CREATE TABLE IF NOT EXISTS processed_blocks (
            chain_id INTEGER NOT NULL,
            block_number INTEGER NOT NULL,
            block_hash TEXT NOT NULL,
            block_time INTEGER NOT NULL,
            status TEXT NOT NULL,
            processed_at INTEGER NOT NULL,
            PRIMARY KEY (chain_id, block_number)
        );

        CREATE TABLE IF NOT EXISTS price_cache (
            chain_id INTEGER NOT NULL,
            token_address TEXT NOT NULL,
            price_usd TEXT NOT NULL,
            volume_24h_usd TEXT,
            source TEXT NOT NULL,
            observed_at INTEGER NOT NULL,
            PRIMARY KEY (chain_id, token_address, source)
        );

        CREATE TABLE IF NOT EXISTS flow_window_snapshots (
            snapshot_key TEXT PRIMARY KEY,
            chain_id INTEGER NOT NULL,
            token_address TEXT NOT NULL,
            symbol TEXT NOT NULL,
            evaluation_time INTEGER NOT NULL,
            duration_sec INTEGER NOT NULL,
            gross_inflow_usd TEXT NOT NULL,
            gross_outflow_usd TEXT NOT NULL,
            net_flow_usd TEXT NOT NULL,
            inflow_tx_count INTEGER NOT NULL,
            outflow_tx_count INTEGER NOT NULL,
            distinct_inbound_counterparties INTEGER NOT NULL,
            distinct_outbound_counterparties INTEGER NOT NULL,
            exchanges_json TEXT NOT NULL,
            active_15m_buckets INTEGER NOT NULL,
            min_label_confidence REAL NOT NULL,
            price_source TEXT NOT NULL,
            price_observed_at INTEGER NOT NULL,
            evaluation_block INTEGER NOT NULL,
            algorithm_version TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS orphaned_transfer_audit (
            audit_key TEXT PRIMARY KEY,
            event_id TEXT NOT NULL,
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
            original_confirmation_status TEXT NOT NULL,
            source TEXT NOT NULL,
            orphaned_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_processed_blocks_chain_number
            ON processed_blocks(chain_id, block_number);
        CREATE INDEX IF NOT EXISTS idx_flow_snapshots_token_time
            ON flow_window_snapshots(chain_id, token_address, evaluation_time);
        CREATE INDEX IF NOT EXISTS idx_orphaned_transfer_event
            ON orphaned_transfer_audit(event_id, orphaned_at);
        """,
    ),
    (
        3,
        """
        ALTER TABLE alerts
            ADD COLUMN notification_key TEXT NOT NULL DEFAULT '';

        ALTER TABLE alert_deliveries
            ADD COLUMN notification_key TEXT NOT NULL DEFAULT '';
        ALTER TABLE alert_deliveries
            ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE alert_deliveries
            ADD COLUMN updated_at INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE price_cache
            ADD COLUMN market_observed_at INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE price_cache
            ADD COLUMN fetched_at INTEGER NOT NULL DEFAULT 0;

        ALTER TABLE flow_window_snapshots
            ADD COLUMN inflow_exchanges_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE flow_window_snapshots
            ADD COLUMN outflow_exchanges_json TEXT NOT NULL DEFAULT '[]';
        ALTER TABLE flow_window_snapshots
            ADD COLUMN valuation_price_usd TEXT;
        ALTER TABLE flow_window_snapshots
            ADD COLUMN price_market_observed_at INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE flow_window_snapshots
            ADD COLUMN price_fetched_at INTEGER NOT NULL DEFAULT 0;

        CREATE TABLE IF NOT EXISTS single_event_decisions (
            event_id TEXT PRIMARY KEY
                REFERENCES flow_events(event_id) ON DELETE CASCADE,
            decision_status TEXT NOT NULL,
            alert_key TEXT REFERENCES alerts(alert_key),
            last_evaluation_attempt INTEGER NOT NULL,
            catchup_suppression_reason TEXT NOT NULL DEFAULT '',
            decision_reason TEXT NOT NULL DEFAULT '',
            updated_at INTEGER NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_single_event_decisions_status
            ON single_event_decisions(decision_status, updated_at);
        CREATE INDEX IF NOT EXISTS idx_alert_deliveries_notification
            ON alert_deliveries(notification_key, updated_at);
        """,
    ),
)


_ALTER_ADD_COLUMN = re.compile(
    r"^\s*ALTER\s+TABLE\s+([A-Za-z_][A-Za-z0-9_]*)\s+"
    r"ADD\s+COLUMN\s+([A-Za-z_][A-Za-z0-9_]*)\b",
    re.IGNORECASE | re.DOTALL,
)


def _statements(script: str) -> list[str]:
    statements: list[str] = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise sqlite3.OperationalError("incomplete migration statement")
    return statements


def _column_exists(
    conn: sqlite3.Connection, table: str, column: str
) -> bool:
    return any(
        str(row[1]).lower() == column.lower()
        for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    )


def _execute_statement(conn: sqlite3.Connection, statement: str) -> None:
    match = _ALTER_ADD_COLUMN.match(statement)
    if match and _column_exists(conn, match.group(1), match.group(2)):
        return
    conn.execute(statement)


def apply_migrations(
    conn: sqlite3.Connection,
    *,
    after_statement: Callable[[int, int], None] | None = None,
) -> None:
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
        statements = _statements(script)
        conn.execute("BEGIN IMMEDIATE")
        try:
            for index, statement in enumerate(statements, start=1):
                _execute_statement(conn, statement)
                if after_statement is not None:
                    after_statement(version, index)
            conn.execute(
                "INSERT INTO schema_migrations(version) VALUES (?)",
                (version,),
            )
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
