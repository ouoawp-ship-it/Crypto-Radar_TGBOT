from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import Iterable, Sequence

from .config import OnchainSettings
from .migrations import apply_migrations
from .models import (
    AddressLabel,
    ChainCursor,
    ClassifiedFlow,
    FlowWindow,
    NormalizedTransfer,
    OnchainAlert,
    PriceQuote,
    ProcessedBlock,
    RollingFlowSnapshot,
    TokenMetadata,
)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None or str(value).strip() == "":
        return None
    return Decimal(str(value))


def _upsert_transfer_row(
    conn: sqlite3.Connection, transfer: NormalizedTransfer
) -> bool:
    exists = conn.execute(
        "SELECT 1 FROM transfer_events WHERE event_id=?",
        (transfer.event_id,),
    ).fetchone()
    conn.execute(
        """
        INSERT INTO transfer_events(
            event_id, chain_id, chain_name, block_number, block_hash,
            block_time, tx_hash, log_index, token_address, from_address,
            to_address, amount_raw, removed, confirmation_status, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(event_id) DO UPDATE SET
            chain_name=excluded.chain_name,
            block_number=excluded.block_number,
            block_hash=excluded.block_hash,
            block_time=excluded.block_time,
            token_address=excluded.token_address,
            from_address=excluded.from_address,
            to_address=excluded.to_address,
            amount_raw=excluded.amount_raw,
            removed=excluded.removed,
            confirmation_status=excluded.confirmation_status,
            source=excluded.source
        """,
        (
            transfer.event_id,
            transfer.chain_id,
            transfer.chain_name,
            transfer.block_number,
            transfer.block_hash,
            transfer.block_time,
            transfer.tx_hash,
            transfer.log_index,
            transfer.token_address,
            transfer.from_address,
            transfer.to_address,
            str(transfer.amount_raw),
            int(transfer.removed),
            transfer.confirmation_status,
            transfer.source,
        ),
    )
    return exists is None


def _upsert_flow_row(
    conn: sqlite3.Connection, flow: ClassifiedFlow
) -> None:
    conn.execute(
        """
        INSERT INTO flow_events(
            event_id, chain_id, token_address, symbol, block_time,
            flow_type, exchange_from, exchange_to, counterparty_address,
            amount, amount_usd, label_confidence, price_status,
            block_number, block_hash, price_source, price_observed_at, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
        ON CONFLICT(event_id) DO UPDATE SET
            chain_id=excluded.chain_id,
            token_address=excluded.token_address,
            symbol=excluded.symbol,
            block_time=excluded.block_time,
            flow_type=excluded.flow_type,
            exchange_from=excluded.exchange_from,
            exchange_to=excluded.exchange_to,
            counterparty_address=excluded.counterparty_address,
            amount=excluded.amount,
            amount_usd=excluded.amount_usd,
            label_confidence=excluded.label_confidence,
            price_status=excluded.price_status,
            block_number=excluded.block_number,
            block_hash=excluded.block_hash,
            price_source=excluded.price_source,
            price_observed_at=excluded.price_observed_at,
            status='active'
        """,
        (
            flow.event_id,
            flow.chain_id,
            flow.token_address,
            flow.symbol,
            flow.block_time,
            flow.flow_type,
            flow.exchange_from,
            flow.exchange_to,
            flow.counterparty_address,
            str(flow.amount) if flow.amount is not None else None,
            str(flow.amount_usd) if flow.amount_usd is not None else None,
            flow.label_confidence,
            flow.price_status,
            flow.block_number,
            flow.block_hash,
            flow.price_source,
            flow.price_observed_at,
        ),
    )


class OnchainStore:
    def __init__(self, settings: OnchainSettings):
        settings.assert_safe_paths()
        self.settings = settings
        self.path = settings.db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def migrate(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as conn:
            apply_migrations(conn)

    @staticmethod
    def integrity_check_existing(path: Path) -> str:
        if not path.exists():
            return "not_initialized"
        uri = f"{path.resolve().as_uri()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1.0)) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA busy_timeout=1000")
            row = conn.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"

    def integrity_check(self) -> str:
        return self.integrity_check_existing(self.path)

    def replace_labels(self, labels: Iterable[AddressLabel]) -> int:
        rows = list(labels)
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM address_labels")
            conn.executemany(
                """
                INSERT INTO address_labels(
                    chain_id, address, entity_name, entity_type, address_type,
                    source, confidence, valid_from, valid_to
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        label.chain_id,
                        label.address,
                        label.entity_name,
                        label.entity_type,
                        label.address_type,
                        label.source,
                        label.confidence,
                        label.valid_from,
                        label.valid_to,
                    )
                    for label in rows
                ],
            )
        return len(rows)

    def list_labels(self) -> list[AddressLabel]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT chain_id, address, entity_name, entity_type, address_type,
                       source, confidence, valid_from, valid_to
                FROM address_labels
                ORDER BY chain_id, address
                """
            ).fetchall()
        return [
            AddressLabel(
                chain_id=int(row["chain_id"]),
                address=str(row["address"]),
                entity_name=str(row["entity_name"]),
                entity_type=str(row["entity_type"]),
                address_type=str(row["address_type"]),
                source=str(row["source"]),
                confidence=float(row["confidence"]),
                valid_from=(
                    int(row["valid_from"]) if row["valid_from"] is not None else None
                ),
                valid_to=int(row["valid_to"]) if row["valid_to"] is not None else None,
            )
            for row in rows
        ]

    def upsert_token_metadata(self, metadata: TokenMetadata) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO token_metadata(
                    chain_id, token_address, symbol, name, decimals, token_kind,
                    metadata_status, updated_at, price_usd, volume_24h_usd,
                    historical_single_p99_usd, historical_15m_p99_usd,
                    historical_60m_p99_usd, historical_window_median_usd,
                    historical_window_mad_usd, price_source, price_observed_at,
                    retry_after
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, token_address) DO UPDATE SET
                    symbol=excluded.symbol,
                    name=excluded.name,
                    decimals=excluded.decimals,
                    token_kind=excluded.token_kind,
                    metadata_status=excluded.metadata_status,
                    updated_at=excluded.updated_at,
                    price_usd=excluded.price_usd,
                    volume_24h_usd=excluded.volume_24h_usd,
                    historical_single_p99_usd=excluded.historical_single_p99_usd,
                    historical_15m_p99_usd=excluded.historical_15m_p99_usd,
                    historical_60m_p99_usd=excluded.historical_60m_p99_usd,
                    historical_window_median_usd=excluded.historical_window_median_usd,
                    historical_window_mad_usd=excluded.historical_window_mad_usd,
                    price_source=excluded.price_source,
                    price_observed_at=excluded.price_observed_at,
                    retry_after=excluded.retry_after
                """,
                (
                    metadata.chain_id,
                    metadata.token_address,
                    metadata.symbol,
                    metadata.name,
                    metadata.decimals,
                    metadata.token_kind,
                    metadata.metadata_status,
                    metadata.updated_at,
                    str(metadata.price_usd) if metadata.price_usd is not None else None,
                    (
                        str(metadata.volume_24h_usd)
                        if metadata.volume_24h_usd is not None
                        else None
                    ),
                    (
                        str(metadata.historical_single_p99_usd)
                        if metadata.historical_single_p99_usd is not None
                        else None
                    ),
                    (
                        str(metadata.historical_15m_p99_usd)
                        if metadata.historical_15m_p99_usd is not None
                        else None
                    ),
                    (
                        str(metadata.historical_60m_p99_usd)
                        if metadata.historical_60m_p99_usd is not None
                        else None
                    ),
                    (
                        str(metadata.historical_window_median_usd)
                        if metadata.historical_window_median_usd is not None
                        else None
                    ),
                    (
                        str(metadata.historical_window_mad_usd)
                        if metadata.historical_window_mad_usd is not None
                        else None
                    ),
                    metadata.price_source,
                    metadata.price_observed_at,
                    metadata.retry_after,
                ),
            )

    def metadata_map(self) -> dict[tuple[int, str], TokenMetadata]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM token_metadata ORDER BY chain_id, token_address"
            ).fetchall()
        result: dict[tuple[int, str], TokenMetadata] = {}
        for row in rows:
            metadata = TokenMetadata(
                chain_id=int(row["chain_id"]),
                token_address=str(row["token_address"]),
                symbol=str(row["symbol"]),
                name=str(row["name"]),
                decimals=(
                    int(row["decimals"]) if row["decimals"] is not None else None
                ),
                token_kind=str(row["token_kind"]),
                metadata_status=str(row["metadata_status"]),
                updated_at=int(row["updated_at"]),
                price_usd=_decimal_or_none(row["price_usd"]),
                volume_24h_usd=_decimal_or_none(row["volume_24h_usd"]),
                historical_single_p99_usd=_decimal_or_none(
                    row["historical_single_p99_usd"]
                ),
                historical_15m_p99_usd=_decimal_or_none(
                    row["historical_15m_p99_usd"]
                ),
                historical_60m_p99_usd=_decimal_or_none(
                    row["historical_60m_p99_usd"]
                ),
                historical_window_median_usd=_decimal_or_none(
                    row["historical_window_median_usd"]
                ),
                historical_window_mad_usd=_decimal_or_none(
                    row["historical_window_mad_usd"]
                ),
                price_source=str(row["price_source"]),
                price_observed_at=int(row["price_observed_at"]),
                retry_after=int(row["retry_after"]),
            )
            result[(metadata.chain_id, metadata.token_address)] = metadata
        return result

    def upsert_transfer(self, transfer: NormalizedTransfer) -> bool:
        with closing(self._connect()) as conn, conn:
            return _upsert_transfer_row(conn, transfer)

    def upsert_flow(self, flow: ClassifiedFlow) -> None:
        with closing(self._connect()) as conn, conn:
            _upsert_flow_row(conn, flow)

    def finalized_flows(self) -> list[ClassifiedFlow]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT f.*
                FROM flow_events f
                JOIN transfer_events t ON t.event_id=f.event_id
                WHERE t.removed=0
                  AND t.confirmation_status='finalized'
                  AND f.status='active'
                ORDER BY f.block_time, f.event_id
                """
            ).fetchall()
        return [
            ClassifiedFlow(
                event_id=str(row["event_id"]),
                chain_id=int(row["chain_id"]),
                token_address=str(row["token_address"]),
                symbol=str(row["symbol"]),
                block_time=int(row["block_time"]),
                flow_type=str(row["flow_type"]),
                exchange_from=(
                    str(row["exchange_from"])
                    if row["exchange_from"] is not None
                    else None
                ),
                exchange_to=(
                    str(row["exchange_to"])
                    if row["exchange_to"] is not None
                    else None
                ),
                counterparty_address=str(row["counterparty_address"]),
                amount=_decimal_or_none(row["amount"]),
                amount_usd=_decimal_or_none(row["amount_usd"]),
                label_confidence=float(row["label_confidence"]),
                price_status=str(row["price_status"]),
                block_number=int(row["block_number"]),
                block_hash=str(row["block_hash"]),
                price_source=str(row["price_source"]),
                price_observed_at=int(row["price_observed_at"]),
            )
            for row in rows
        ]

    def finalized_flows_since(
        self, chain_id: int, minimum_block_time: int
    ) -> list[ClassifiedFlow]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT f.*
                FROM flow_events f
                JOIN transfer_events t ON t.event_id=f.event_id
                WHERE t.removed=0
                  AND t.confirmation_status='finalized'
                  AND f.status='active'
                  AND f.chain_id=?
                  AND f.block_time>=?
                ORDER BY f.block_time, f.event_id
                """,
                (chain_id, minimum_block_time),
            ).fetchall()
        return [
            ClassifiedFlow(
                event_id=str(row["event_id"]),
                chain_id=int(row["chain_id"]),
                token_address=str(row["token_address"]),
                symbol=str(row["symbol"]),
                block_time=int(row["block_time"]),
                flow_type=str(row["flow_type"]),
                exchange_from=(
                    str(row["exchange_from"])
                    if row["exchange_from"] is not None
                    else None
                ),
                exchange_to=(
                    str(row["exchange_to"])
                    if row["exchange_to"] is not None
                    else None
                ),
                counterparty_address=str(row["counterparty_address"]),
                amount=_decimal_or_none(row["amount"]),
                amount_usd=_decimal_or_none(row["amount_usd"]),
                label_confidence=float(row["label_confidence"]),
                price_status=str(row["price_status"]),
                block_number=int(row["block_number"]),
                block_hash=str(row["block_hash"]),
                price_source=str(row["price_source"]),
                price_observed_at=int(row["price_observed_at"]),
            )
            for row in rows
        ]

    def cursor(self, chain_id: int) -> ChainCursor | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT chain_id, last_seen_block, last_finalized_block,
                       block_hash, updated_at
                FROM chain_cursors
                WHERE chain_id=?
                """,
                (chain_id,),
            ).fetchone()
        if row is None:
            return None
        return ChainCursor(
            chain_id=int(row["chain_id"]),
            last_seen_head=int(row["last_seen_block"]),
            last_finalized_block=int(row["last_finalized_block"]),
            finalized_block_hash=str(row["block_hash"]),
            updated_at=int(row["updated_at"]),
        )

    def processed_blocks_desc(
        self, chain_id: int, start_block: int, limit: int
    ) -> list[ProcessedBlock]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                """
                SELECT chain_id, block_number, block_hash, block_time,
                       status, processed_at
                FROM processed_blocks
                WHERE chain_id=? AND block_number<=?
                ORDER BY block_number DESC
                LIMIT ?
                """,
                (chain_id, start_block, limit),
            ).fetchall()
        return [
            ProcessedBlock(
                chain_id=int(row["chain_id"]),
                block_number=int(row["block_number"]),
                block_hash=str(row["block_hash"]),
                block_time=int(row["block_time"]),
                status=str(row["status"]),
                processed_at=int(row["processed_at"]),
            )
            for row in rows
        ]

    def commit_finalized_range(
        self,
        *,
        blocks: Sequence[ProcessedBlock],
        transfers: Sequence[NormalizedTransfer],
        flows: Sequence[ClassifiedFlow],
        last_seen_head: int,
        provider_status: str,
        updated_at: int,
    ) -> tuple[int, int]:
        ordered_blocks = sorted(blocks, key=lambda item: item.block_number)
        if not ordered_blocks:
            raise ValueError("cannot commit an empty finalized block range")
        inserted = 0
        duplicates = 0
        with closing(self._connect()) as conn, conn:
            for transfer in transfers:
                if _upsert_transfer_row(conn, transfer):
                    inserted += 1
                else:
                    duplicates += 1
            for flow in flows:
                _upsert_flow_row(conn, flow)
            conn.executemany(
                """
                INSERT INTO processed_blocks(
                    chain_id, block_number, block_hash, block_time,
                    status, processed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, block_number) DO UPDATE SET
                    block_hash=excluded.block_hash,
                    block_time=excluded.block_time,
                    status=excluded.status,
                    processed_at=excluded.processed_at
                """,
                [
                    (
                        block.chain_id,
                        block.block_number,
                        block.block_hash,
                        block.block_time,
                        block.status,
                        block.processed_at or updated_at,
                    )
                    for block in ordered_blocks
                ],
            )
            final_block = ordered_blocks[-1]
            conn.execute(
                """
                INSERT INTO chain_cursors(
                    chain_id, last_seen_block, last_finalized_block,
                    block_hash, updated_at, provider_status
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id) DO UPDATE SET
                    last_seen_block=excluded.last_seen_block,
                    last_finalized_block=excluded.last_finalized_block,
                    block_hash=excluded.block_hash,
                    updated_at=excluded.updated_at,
                    provider_status=excluded.provider_status
                """,
                (
                    final_block.chain_id,
                    last_seen_head,
                    final_block.block_number,
                    final_block.block_hash,
                    updated_at,
                    provider_status,
                ),
            )
        return inserted, duplicates

    def update_head_status(
        self,
        chain_id: int,
        *,
        last_seen_head: int,
        provider_status: str,
        updated_at: int,
    ) -> None:
        with closing(self._connect()) as conn, conn:
            updated = conn.execute(
                """
                UPDATE chain_cursors
                SET last_seen_block=?, provider_status=?, updated_at=?
                WHERE chain_id=?
                """,
                (
                    last_seen_head,
                    provider_status,
                    updated_at,
                    chain_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("cannot update head without a durable cursor")

    def rollback_to_block(
        self, chain_id: int, ancestor_block: int, updated_at: int
    ) -> int:
        with closing(self._connect()) as conn, conn:
            ancestor = conn.execute(
                """
                SELECT block_hash FROM processed_blocks
                WHERE chain_id=? AND block_number=?
                """,
                (chain_id, ancestor_block),
            ).fetchone()
            if ancestor is None:
                raise ValueError("reorg ancestor is not in processed_blocks")
            conn.execute(
                """
                INSERT OR IGNORE INTO orphaned_transfer_audit(
                    audit_key, event_id, chain_id, chain_name, block_number,
                    block_hash, block_time, tx_hash, log_index, token_address,
                    from_address, to_address, amount_raw,
                    original_confirmation_status, source, orphaned_at
                )
                SELECT
                    event_id || ':' || block_hash || ':' || ?,
                    event_id, chain_id, chain_name, block_number, block_hash,
                    block_time, tx_hash, log_index, token_address,
                    from_address, to_address, amount_raw,
                    confirmation_status, source, ?
                FROM transfer_events
                WHERE chain_id=? AND block_number>?
                  AND removed=0
                """,
                (
                    updated_at,
                    updated_at,
                    chain_id,
                    ancestor_block,
                ),
            )
            orphaned = conn.execute(
                """
                UPDATE transfer_events
                SET removed=1, confirmation_status='orphaned'
                WHERE chain_id=? AND block_number>?
                """,
                (chain_id, ancestor_block),
            )
            conn.execute(
                """
                UPDATE flow_events
                SET status='orphaned'
                WHERE chain_id=? AND block_number>?
                """,
                (chain_id, ancestor_block),
            )
            conn.execute(
                """
                UPDATE processed_blocks
                SET status='orphaned', processed_at=?
                WHERE chain_id=? AND block_number>?
                """,
                (updated_at, chain_id, ancestor_block),
            )
            conn.execute(
                """
                UPDATE flow_window_snapshots
                SET status='orphaned'
                WHERE chain_id=? AND evaluation_block>?
                """,
                (chain_id, ancestor_block),
            )
            conn.execute(
                """
                UPDATE alerts
                SET status='orphaned'
                WHERE chain_id=? AND evaluation_block>?
                """,
                (chain_id, ancestor_block),
            )
            conn.execute(
                """
                UPDATE chain_cursors
                SET last_finalized_block=?, block_hash=?, updated_at=?,
                    provider_status='reorg_recovered'
                WHERE chain_id=?
                """,
                (
                    ancestor_block,
                    str(ancestor["block_hash"]),
                    updated_at,
                    chain_id,
                ),
            )
        return int(orphaned.rowcount)

    def upsert_snapshot(self, snapshot: RollingFlowSnapshot) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO flow_window_snapshots(
                    snapshot_key, chain_id, token_address, symbol,
                    evaluation_time, duration_sec, gross_inflow_usd,
                    gross_outflow_usd, net_flow_usd, inflow_tx_count,
                    outflow_tx_count, distinct_inbound_counterparties,
                    distinct_outbound_counterparties, exchanges_json,
                    active_15m_buckets, min_label_confidence, price_source,
                    price_observed_at, evaluation_block, algorithm_version,
                    status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                ON CONFLICT(snapshot_key) DO UPDATE SET
                    gross_inflow_usd=excluded.gross_inflow_usd,
                    gross_outflow_usd=excluded.gross_outflow_usd,
                    net_flow_usd=excluded.net_flow_usd,
                    inflow_tx_count=excluded.inflow_tx_count,
                    outflow_tx_count=excluded.outflow_tx_count,
                    distinct_inbound_counterparties=excluded.distinct_inbound_counterparties,
                    distinct_outbound_counterparties=excluded.distinct_outbound_counterparties,
                    exchanges_json=excluded.exchanges_json,
                    active_15m_buckets=excluded.active_15m_buckets,
                    min_label_confidence=excluded.min_label_confidence,
                    price_source=excluded.price_source,
                    price_observed_at=excluded.price_observed_at,
                    evaluation_block=excluded.evaluation_block,
                    status='active'
                """,
                (
                    snapshot.snapshot_key,
                    snapshot.chain_id,
                    snapshot.token_address,
                    snapshot.symbol,
                    snapshot.evaluation_time,
                    snapshot.duration_sec,
                    str(snapshot.gross_inflow_usd),
                    str(snapshot.gross_outflow_usd),
                    str(snapshot.net_flow_usd),
                    snapshot.inflow_tx_count,
                    snapshot.outflow_tx_count,
                    snapshot.distinct_inbound_counterparties,
                    snapshot.distinct_outbound_counterparties,
                    json.dumps(snapshot.exchanges, ensure_ascii=False),
                    snapshot.active_15m_buckets,
                    snapshot.min_label_confidence,
                    snapshot.price_source,
                    snapshot.price_observed_at,
                    snapshot.evaluation_block,
                    snapshot.algorithm_version,
                ),
            )

    def cache_price(self, quote: PriceQuote) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO price_cache(
                    chain_id, token_address, price_usd, volume_24h_usd,
                    source, observed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(chain_id, token_address, source) DO UPDATE SET
                    price_usd=excluded.price_usd,
                    volume_24h_usd=excluded.volume_24h_usd,
                    observed_at=excluded.observed_at
                """,
                (
                    quote.chain_id,
                    quote.token_address,
                    str(quote.price_usd),
                    (
                        str(quote.volume_24h_usd)
                        if quote.volume_24h_usd is not None
                        else None
                    ),
                    quote.source,
                    quote.observed_at,
                ),
            )

    def cached_price(
        self, chain_id: int, token_address: str
    ) -> PriceQuote | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                SELECT * FROM price_cache
                WHERE chain_id=? AND token_address=?
                ORDER BY observed_at DESC
                LIMIT 1
                """,
                (chain_id, token_address.lower()),
            ).fetchone()
        if row is None:
            return None
        return PriceQuote(
            chain_id=int(row["chain_id"]),
            token_address=str(row["token_address"]),
            price_usd=Decimal(str(row["price_usd"])),
            volume_24h_usd=_decimal_or_none(row["volume_24h_usd"]),
            source=str(row["source"]),
            observed_at=int(row["observed_at"]),
        )

    def replace_windows(self, windows: Iterable[FlowWindow]) -> int:
        rows = list(windows)
        with closing(self._connect()) as conn, conn:
            conn.execute("DELETE FROM flow_windows")
            conn.executemany(
                """
                INSERT INTO flow_windows(
                    window_key, chain_id, token_address, symbol, direction,
                    window_start, window_end, duration_sec, total_usd, tx_count,
                    distinct_counterparties, exchanges_json, active_15m_buckets,
                    min_label_confidence, algorithm_version, threshold_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        window.window_key,
                        window.chain_id,
                        window.token_address,
                        window.symbol,
                        window.direction,
                        window.window_start,
                        window.window_end,
                        window.duration_sec,
                        str(window.total_usd),
                        window.tx_count,
                        window.distinct_counterparties,
                        json.dumps(window.exchanges, ensure_ascii=False),
                        window.active_15m_buckets,
                        window.min_label_confidence,
                        window.algorithm_version,
                        window.threshold_version,
                    )
                    for window in rows
                ],
            )
        return len(rows)

    def sync_alerts(self, alerts: Iterable[OnchainAlert]) -> int:
        rows = list(alerts)
        with closing(self._connect()) as conn, conn:
            conn.execute("UPDATE alerts SET status='inactive'")
            for alert in rows:
                conn.execute(
                    """
                    INSERT INTO alerts(
                        alert_key, chain_id, token_address, symbol, direction,
                        score, horizon, confidence, reasons_json,
                        detection_types_json, window_start, window_end, total_usd,
                        tx_count, exchanges_json, label_confidence, price_status,
                        created_at, severity_version, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                    ON CONFLICT(alert_key) DO UPDATE SET
                        score=excluded.score,
                        horizon=excluded.horizon,
                        confidence=excluded.confidence,
                        reasons_json=excluded.reasons_json,
                        detection_types_json=excluded.detection_types_json,
                        total_usd=excluded.total_usd,
                        tx_count=excluded.tx_count,
                        exchanges_json=excluded.exchanges_json,
                        label_confidence=excluded.label_confidence,
                        price_status=excluded.price_status,
                        status='active'
                    """,
                    (
                        alert.alert_key,
                        alert.chain_id,
                        alert.token_address,
                        alert.symbol,
                        alert.direction,
                        alert.score,
                        alert.horizon,
                        alert.confidence,
                        json.dumps(alert.reasons, ensure_ascii=False),
                        json.dumps(alert.detection_types, ensure_ascii=False),
                        alert.window_start,
                        alert.window_end,
                        str(alert.total_usd),
                        alert.tx_count,
                        json.dumps(alert.exchanges, ensure_ascii=False),
                        alert.label_confidence,
                        alert.price_status,
                        alert.created_at,
                        alert.severity_version,
                    ),
                )
        return len(rows)

    def upsert_alert(self, alert: OnchainAlert) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO alerts(
                    alert_key, chain_id, token_address, symbol, direction,
                    score, horizon, confidence, reasons_json,
                    detection_types_json, window_start, window_end, total_usd,
                    tx_count, exchanges_json, label_confidence, price_status,
                    created_at, severity_version, status,
                    gross_inflow_usd, gross_outflow_usd, net_flow_usd,
                    duration_sec, inflow_tx_count, outflow_tx_count,
                    distinct_inbound_counterparties,
                    distinct_outbound_counterparties, evaluation_block,
                    price_source, price_observed_at, chain_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active',
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(alert_key) DO NOTHING
                """,
                (
                    alert.alert_key,
                    alert.chain_id,
                    alert.token_address,
                    alert.symbol,
                    alert.direction,
                    alert.score,
                    alert.horizon,
                    alert.confidence,
                    json.dumps(alert.reasons, ensure_ascii=False),
                    json.dumps(alert.detection_types, ensure_ascii=False),
                    alert.window_start,
                    alert.window_end,
                    str(alert.total_usd),
                    alert.tx_count,
                    json.dumps(alert.exchanges, ensure_ascii=False),
                    alert.label_confidence,
                    alert.price_status,
                    alert.created_at,
                    alert.severity_version,
                    (
                        str(alert.gross_inflow_usd)
                        if alert.gross_inflow_usd is not None
                        else None
                    ),
                    (
                        str(alert.gross_outflow_usd)
                        if alert.gross_outflow_usd is not None
                        else None
                    ),
                    (
                        str(alert.net_flow_usd)
                        if alert.net_flow_usd is not None
                        else None
                    ),
                    alert.duration_sec,
                    alert.inflow_tx_count,
                    alert.outflow_tx_count,
                    alert.distinct_inbound_counterparties,
                    alert.distinct_outbound_counterparties,
                    alert.evaluation_block,
                    alert.price_source,
                    alert.price_observed_at,
                    alert.chain_name,
                ),
            )

    def active_alerts(self) -> list[OnchainAlert]:
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT * FROM alerts WHERE status='active' ORDER BY alert_key"
            ).fetchall()
        return [
            OnchainAlert(
                alert_key=str(row["alert_key"]),
                chain_id=int(row["chain_id"]),
                token_address=str(row["token_address"]),
                symbol=str(row["symbol"]),
                direction=str(row["direction"]),
                score=int(row["score"]),
                horizon=str(row["horizon"]),
                confidence=str(row["confidence"]),
                reasons=tuple(json.loads(str(row["reasons_json"]))),
                detection_types=tuple(
                    json.loads(str(row["detection_types_json"]))
                ),
                window_start=int(row["window_start"]),
                window_end=int(row["window_end"]),
                total_usd=Decimal(str(row["total_usd"])),
                tx_count=int(row["tx_count"]),
                exchanges=tuple(json.loads(str(row["exchanges_json"]))),
                label_confidence=float(row["label_confidence"]),
                price_status=str(row["price_status"]),
                created_at=int(row["created_at"]),
                severity_version=str(row["severity_version"]),
                gross_inflow_usd=_decimal_or_none(row["gross_inflow_usd"]),
                gross_outflow_usd=_decimal_or_none(row["gross_outflow_usd"]),
                net_flow_usd=_decimal_or_none(row["net_flow_usd"]),
                duration_sec=int(row["duration_sec"]),
                inflow_tx_count=int(row["inflow_tx_count"]),
                outflow_tx_count=int(row["outflow_tx_count"]),
                distinct_inbound_counterparties=int(
                    row["distinct_inbound_counterparties"]
                ),
                distinct_outbound_counterparties=int(
                    row["distinct_outbound_counterparties"]
                ),
                evaluation_block=int(row["evaluation_block"]),
                price_source=str(row["price_source"]),
                price_observed_at=int(row["price_observed_at"]),
                chain_name=str(row["chain_name"]),
            )
            for row in rows
        ]

    def record_delivery(
        self,
        alert_key: str,
        *,
        status: str,
        sent: bool,
        reason: str,
        created_at: int,
    ) -> None:
        delivery_key = f"{alert_key}:{status}"
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                INSERT INTO alert_deliveries(
                    delivery_key, alert_key, status, sent, reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(delivery_key) DO UPDATE SET
                    sent=excluded.sent,
                    reason=excluded.reason
                """,
                (
                    delivery_key,
                    alert_key,
                    status,
                    int(sent),
                    reason,
                    created_at,
                ),
            )

    def table_counts(self) -> dict[str, int]:
        tables = (
            "address_labels",
            "token_metadata",
            "transfer_events",
            "flow_events",
            "flow_windows",
            "alerts",
            "alert_deliveries",
            "processed_blocks",
            "price_cache",
            "flow_window_snapshots",
            "orphaned_transfer_audit",
        )
        with closing(self._connect()) as conn:
            return {
                table: int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }
