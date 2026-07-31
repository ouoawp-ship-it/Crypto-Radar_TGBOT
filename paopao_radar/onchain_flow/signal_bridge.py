from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .automation_store import (
    AutomationStore,
    AutomationStoreError,
    canonical_market_symbol,
    stable_payload_hash,
)
from .config import OnchainSettings
from .labels import LabelValidationError, normalize_evm_address


REQUIRED_SIGNAL_COLUMNS = {
    "id",
    "public_ref",
    "ts",
    "module",
    "template_id",
    "symbol",
    "stage",
    "severity",
    "score",
    "excerpt",
    "status",
    "sent",
    "ingest_mode",
    "quality_status",
    "payload_json",
}


class MainSignalReader:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(
        self,
        *,
        checkpoint_ts: int,
        checkpoint_id: int,
        overlap_sec: int,
        bootstrap_lookback_sec: int,
        limit: int,
        now: int,
    ) -> dict[str, object]:
        if not self.path.exists():
            return {"status": "source_not_initialized", "signals": []}
        uri = f"file:{quote(self.path.resolve().as_posix(), safe='/:')}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA busy_timeout=1000")
                object_row = conn.execute(
                    "SELECT type FROM sqlite_master WHERE name='signals'"
                ).fetchone()
                if object_row is None:
                    return {
                        "status": "source_not_initialized",
                        "signals": [],
                    }
                columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(signals)"
                    ).fetchall()
                }
                if not REQUIRED_SIGNAL_COLUMNS.issubset(columns):
                    return {
                        "status": "source_schema_incompatible",
                        "signals": [],
                    }
                if checkpoint_ts <= 0:
                    rows = self._query(
                        conn,
                        where="ts>=?",
                        params=(max(0, now - bootstrap_lookback_sec),),
                        limit=limit,
                    )
                else:
                    rows = self._read_incremental(
                        conn,
                        checkpoint_ts=checkpoint_ts,
                        checkpoint_id=checkpoint_id,
                        overlap_sec=overlap_sec,
                        limit=limit,
                    )
                return {
                    "status": "ok",
                    "signals": [self._normalize(row) for row in rows],
                }
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            status = (
                "source_locked"
                if "locked" in message or "busy" in message
                else "source_failed"
            )
            return {"status": status, "signals": []}
        except sqlite3.Error:
            return {"status": "source_failed", "signals": []}

    def read_by_public_refs(
        self,
        public_refs: list[str],
        *,
        limit: int = 100,
    ) -> dict[str, object]:
        refs = sorted(
            {
                str(value or "")[:160]
                for value in public_refs
                if str(value or "")
            }
        )[: max(1, min(int(limit), 100))]
        if not refs:
            return {"status": "ok", "signals": []}
        if not self.path.exists():
            return {"status": "source_not_initialized", "signals": []}
        uri = f"file:{quote(self.path.resolve().as_posix(), safe='/:')}?mode=ro"
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=1)
            conn.row_factory = sqlite3.Row
            try:
                conn.execute("PRAGMA query_only=ON")
                conn.execute("PRAGMA busy_timeout=1000")
                columns = {
                    str(row["name"])
                    for row in conn.execute(
                        "PRAGMA table_info(signals)"
                    ).fetchall()
                }
                if not REQUIRED_SIGNAL_COLUMNS.issubset(columns):
                    return {
                        "status": "source_schema_incompatible",
                        "signals": [],
                    }
                placeholders = ",".join("?" for _ in refs)
                rows = conn.execute(
                    f"""
                    SELECT id, public_ref, ts, module, template_id, symbol,
                           stage, severity, score, excerpt, status, sent,
                           ingest_mode, quality_status, payload_json
                    FROM signals
                    WHERE public_ref IN ({placeholders})
                    ORDER BY ts ASC, id ASC
                    LIMIT ?
                    """,
                    (*refs, len(refs)),
                ).fetchall()
                return {
                    "status": "ok",
                    "signals": [self._normalize(row) for row in rows],
                }
            finally:
                conn.close()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            status = (
                "source_locked"
                if "locked" in message or "busy" in message
                else "source_failed"
            )
            return {"status": status, "signals": []}
        except sqlite3.Error:
            return {"status": "source_failed", "signals": []}

    def _read_incremental(
        self,
        conn: sqlite3.Connection,
        *,
        checkpoint_ts: int,
        checkpoint_id: int,
        overlap_sec: int,
        limit: int,
    ) -> list[sqlite3.Row]:
        newer = self._query(
            conn,
            where="ts>? OR (ts=? AND id>?)",
            params=(checkpoint_ts, checkpoint_ts, checkpoint_id),
            limit=limit,
        )
        remaining = max(0, limit - len(newer))
        if remaining == 0:
            return newer
        overlap = self._query(
            conn,
            where=(
                "ts>=? AND (ts<? OR (ts=? AND id<=?))"
            ),
            params=(
                max(0, checkpoint_ts - overlap_sec),
                checkpoint_ts,
                checkpoint_ts,
                checkpoint_id,
            ),
            limit=remaining,
        )
        rows = {int(row["id"]): row for row in (*newer, *overlap)}
        return sorted(rows.values(), key=lambda row: (int(row["ts"]), int(row["id"])))

    @staticmethod
    def _query(
        conn: sqlite3.Connection,
        *,
        where: str,
        params: tuple[object, ...],
        limit: int,
    ) -> list[sqlite3.Row]:
        return conn.execute(
            f"""
            SELECT id, public_ref, ts, module, template_id, symbol,
                   stage, severity, score, excerpt, status, sent,
                   ingest_mode, quality_status, payload_json
            FROM signals
            WHERE {where}
            ORDER BY ts ASC, id ASC
            LIMIT ?
            """,
            (*params, int(limit)),
        ).fetchall()

    @staticmethod
    def _normalize(row: sqlite3.Row) -> dict[str, object]:
        raw_payload = str(row["payload_json"] or "{}")
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            payload = {}
        safe_payload = payload if isinstance(payload, dict) else {}
        facts = safe_payload.get("facts")
        facts = facts if isinstance(facts, dict) else {}
        candidate_chain = str(
            facts.get("chain") or facts.get("network") or ""
        ).strip().lower()
        candidate_contract = str(
            facts.get("contract")
            or facts.get("contract_address")
            or facts.get("token_address")
            or ""
        ).strip().lower()
        return {
            "id": int(row["id"]),
            "public_ref": str(row["public_ref"] or ""),
            "ts": int(row["ts"]),
            "module": str(row["module"] or "").lower(),
            "template_id": str(row["template_id"] or ""),
            "symbol": str(row["symbol"] or "").upper(),
            "stage": str(row["stage"] or "")[:80],
            "severity": str(row["severity"] or "")[:24],
            "score": row["score"],
            "excerpt": str(row["excerpt"] or "")[:300],
            "status": str(row["status"] or "").lower(),
            "sent": int(row["sent"] or 0),
            "ingest_mode": str(row["ingest_mode"] or "").lower(),
            "quality_status": str(row["quality_status"] or "").lower(),
            "payload_hash": stable_payload_hash(safe_payload),
            "candidate_chain": candidate_chain,
            "candidate_contract": candidate_contract,
        }


class SignalBridge:
    def __init__(
        self,
        settings: OnchainSettings,
        store: AutomationStore,
        *,
        reader: MainSignalReader | None = None,
        clock: Any = time.time,
    ):
        self.settings = settings
        self.store = store
        self.reader = reader or MainSignalReader(settings.main_signal_db_path)
        self.clock = clock

    def run_once(self) -> dict[str, object]:
        self.settings.validate()
        now = int(self.clock())
        before = self.store.bridge_checkpoint()
        read_result = self.reader.read(
            checkpoint_ts=before[0],
            checkpoint_id=before[1],
            overlap_sec=self.settings.oar_bridge_overlap_sec,
            bootstrap_lookback_sec=(
                self.settings.oar_bridge_bootstrap_lookback_sec
            ),
            limit=self.settings.oar_bridge_max_signals_per_cycle,
            now=now,
        )
        summary: dict[str, object] = {
            "source_status": read_result["status"],
            "scanned_signals": 0,
            "eligible_signals": 0,
            "resolved": 0,
            "unresolved": 0,
            "ambiguous": 0,
            "watch_created": 0,
            "watch_refreshed": 0,
            "capacity_rejected": 0,
            "ignored_onchain": 0,
            "ignored_not_sent": 0,
            "checkpoint_before": {
                "last_signal_ts": before[0],
                "last_signal_id": before[1],
            },
            "checkpoint_after": {
                "last_signal_ts": before[0],
                "last_signal_id": before[1],
            },
            "database_writes": False,
            "network_activity": False,
            "telegram_calls": False,
            "ai_calls": False,
        }
        if read_result["status"] != "ok":
            return summary
        for signal in read_result["signals"]:
            if not isinstance(signal, dict):
                continue
            summary["scanned_signals"] = int(summary["scanned_signals"]) + 1
            eligible, reason = self._eligibility(signal)
            if eligible:
                summary["eligible_signals"] = (
                    int(summary["eligible_signals"]) + 1
                )
                resolution = self._resolve(signal)
            else:
                if reason in {"ignored_onchain", "ignored_not_sent"}:
                    summary[reason] = (
                        int(summary[reason]) + 1
                    )
                    self.store.checkpoint_ignored_signal(
                        signal,
                        now=now,
                    )
                    summary["database_writes"] = True
                    continue
                resolution = {"status": "ineligible_signal", "token": None}
            module = str(signal.get("module") or "")
            outcome = self.store.process_bridge_signal(
                signal,
                resolution=resolution,
                source_ttl_sec=self._source_ttl(module),
                source_priority=self._source_priority(module),
                query_window=self.settings.oar_watch_query_window,
                scan_interval_sec=self.settings.oar_watch_scan_interval_sec,
                max_active_tokens=self.settings.oar_watch_max_active_tokens,
                now=now,
            )
            summary["database_writes"] = True
            if outcome in {"watch_created", "watch_refreshed"}:
                summary["resolved"] = int(summary["resolved"]) + 1
                summary[outcome] = int(summary[outcome]) + 1
            elif outcome == "ambiguous_contract":
                summary["ambiguous"] = int(summary["ambiguous"]) + 1
                summary["unresolved"] = int(summary["unresolved"]) + 1
            elif outcome == "capacity_exceeded":
                summary["capacity_rejected"] = (
                    int(summary["capacity_rejected"]) + 1
                )
                summary["unresolved"] = int(summary["unresolved"]) + 1
            elif eligible:
                summary["unresolved"] = int(summary["unresolved"]) + 1
        after = self.store.bridge_checkpoint()
        summary["checkpoint_after"] = {
            "last_signal_ts": after[0],
            "last_signal_id": after[1],
        }
        reconciliation = self.reconcile_open(now=now, limit=100)
        summary["reconciliation"] = reconciliation
        summary["database_writes"] = bool(
            summary["database_writes"]
            or reconciliation.get("database_writes")
        )
        return summary

    def reconcile_token(
        self,
        token: dict[str, object],
        *,
        now: int | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        if (
            str(token.get("status") or "") != "verified"
            or int(token.get("is_primary") or 0) != 1
        ):
            return self._reconciliation_summary(
                status="not_primary",
                remaining_open=self.store.open_unresolved_count(
                    market_symbol=str(token.get("market_symbol") or "")
                ),
            )
        rows = self.store.list_open_unresolved(
            market_symbol=str(token["market_symbol"]),
            limit=limit,
        )
        if not rows:
            return self._reconciliation_summary(
                status="ok",
                remaining_open=self.store.open_unresolved_count(
                    market_symbol=str(token["market_symbol"])
                ),
            )
        return self._reconcile_rows(
            rows,
            expected_token_key=str(token["token_key"]),
            now=int(now if now is not None else self.clock()),
        )

    def reconcile_open(
        self,
        *,
        now: int | None = None,
        limit: int = 100,
    ) -> dict[str, object]:
        rows = self.store.list_open_unresolved(
            reasons=(
                "unresolved_contract",
                "registry_not_verified",
                "ambiguous_contract",
                "ineligible_signal",
            ),
            limit=limit,
        )
        return self._reconcile_rows(
            rows,
            expected_token_key=None,
            now=int(now if now is not None else self.clock()),
        )

    def _reconcile_rows(
        self,
        rows: list[dict[str, object]],
        *,
        expected_token_key: str | None,
        now: int,
    ) -> dict[str, object]:
        if not rows:
            return self._reconciliation_summary(
                status="ok",
                remaining_open=(
                    self.store.open_unresolved_count()
                    if expected_token_key is None
                    else 0
                ),
            )
        read_result = self.reader.read_by_public_refs(
            [str(row.get("source_public_ref") or "") for row in rows],
            limit=min(len(rows), 100),
        )
        if read_result["status"] != "ok":
            return self._reconciliation_summary(
                status="source_unavailable",
                remaining_open=(
                    self.store.open_unresolved_count()
                    if expected_token_key is None
                    else len(rows)
                ),
                source_status=str(read_result["status"]),
            )
        signals = {
            str(signal.get("public_ref") or ""): signal
            for signal in read_result["signals"]
            if isinstance(signal, dict)
        }
        result = self._reconciliation_summary(status="ok")
        unresolved_remaining = False
        for unresolved in rows:
            result["examined"] = int(result["examined"]) + 1
            signal = signals.get(str(unresolved["source_public_ref"]))
            if signal is None:
                unresolved_remaining = True
                continue
            eligible, reason = self._eligibility(signal)
            if not eligible:
                if reason == "ignored_not_sent" and self.store.resolve_unresolved(
                    int(unresolved["id"]),
                    status="expired",
                    note="source_not_sent",
                    now=now,
                ):
                    result["expired"] = int(result["expired"]) + 1
                    result["database_writes"] = True
                    continue
                unresolved_remaining = True
                continue
            module = str(signal.get("module") or "")
            ttl = self._source_ttl(module)
            if int(signal.get("ts") or 0) + ttl <= now:
                if self.store.resolve_unresolved(
                    int(unresolved["id"]),
                    status="expired",
                    note="source_ttl_expired",
                    now=now,
                ):
                    result["expired"] = int(result["expired"]) + 1
                    result["database_writes"] = True
                continue
            resolution = self._resolve(signal)
            token = resolution.get("token")
            if (
                resolution.get("status") != "resolved"
                or not isinstance(token, dict)
                or (
                    expected_token_key is not None
                    and str(token.get("token_key") or "")
                    != expected_token_key
                )
            ):
                unresolved_remaining = True
                continue
            outcome = self.store.process_bridge_signal(
                signal,
                resolution=resolution,
                source_ttl_sec=ttl,
                source_priority=self._source_priority(module),
                query_window=self.settings.oar_watch_query_window,
                scan_interval_sec=self.settings.oar_watch_scan_interval_sec,
                max_active_tokens=self.settings.oar_watch_max_active_tokens,
                now=now,
            )
            result["database_writes"] = True
            if outcome not in {"watch_created", "watch_refreshed"}:
                unresolved_remaining = True
                continue
            if self.store.resolve_unresolved(
                int(unresolved["id"]),
                status="resolved",
                token_key=str(token["token_key"]),
                note=outcome,
                now=now,
            ):
                result["resolved"] = int(result["resolved"]) + 1
                result[outcome] = int(result[outcome]) + 1
        result["remaining_open"] = (
            self.store.open_unresolved_count()
            if expected_token_key is None
            else self.store.open_unresolved_count(
                market_symbol=str(rows[0]["source_symbol"])
            )
        )
        if unresolved_remaining or int(result["remaining_open"]):
            result["status"] = "partial"
        return result

    @staticmethod
    def _reconciliation_summary(
        *,
        status: str,
        remaining_open: int = 0,
        source_status: str = "ok",
    ) -> dict[str, object]:
        return {
            "status": status,
            "source_status": source_status,
            "examined": 0,
            "resolved": 0,
            "expired": 0,
            "remaining_open": int(remaining_open),
            "watch_created": 0,
            "watch_refreshed": 0,
            "database_writes": False,
        }

    def _eligibility(
        self, signal: dict[str, object]
    ) -> tuple[bool, str]:
        module = str(signal.get("module") or "")
        if module == "onchain":
            return False, "ignored_onchain"
        if module not in self.settings.oar_bridge_allowed_modules:
            return False, "ineligible_signal"
        if (
            signal.get("status") != "sent"
            or int(signal.get("sent") or 0) != 1
        ):
            return False, "ignored_not_sent"
        if (
            signal.get("ingest_mode") != "structured"
            or signal.get("quality_status") != "ready"
            or not str(signal.get("symbol") or "")
        ):
            return False, "ineligible_signal"
        return True, ""

    def _resolve(self, signal: dict[str, object]) -> dict[str, object]:
        try:
            symbol = canonical_market_symbol(str(signal.get("symbol") or ""))
        except AutomationStoreError:
            return {"status": "invalid_symbol", "token": None}
        resolution = self.store.resolve_registry(symbol)
        if resolution["status"] != "unresolved_contract":
            return resolution
        chain = str(signal.get("candidate_chain") or "").lower()
        contract = str(signal.get("candidate_contract") or "").lower()
        if not chain and not contract:
            return resolution
        if chain != "base":
            return {"status": "unsupported_chain", "token": None}
        try:
            normalized_contract = normalize_evm_address(contract)
        except LabelValidationError:
            return {"status": "unresolved_contract", "token": None}
        try:
            self.store.add_registry(
                market_symbol=symbol,
                contract=normalized_contract,
                source=f"signal:{signal.get('module')}",
                note="unverified structured signal candidate",
            )
        except AutomationStoreError as exc:
            if exc.code == "contract_market_symbol_conflict":
                return {"status": "ambiguous_contract", "token": None}
            raise
        return {"status": "registry_not_verified", "token": None}

    def _source_ttl(self, module: str) -> int:
        return {
            "launch": self.settings.oar_watch_launch_ttl_sec,
            "flow": self.settings.oar_watch_flow_ttl_sec,
            "funding": self.settings.oar_watch_funding_ttl_sec,
            "announcement": self.settings.oar_watch_announcement_ttl_sec,
        }.get(module, 60)

    def _source_priority(self, module: str) -> int:
        return {
            "launch": self.settings.oar_watch_launch_priority,
            "flow": self.settings.oar_watch_flow_priority,
            "funding": self.settings.oar_watch_funding_priority,
            "announcement": self.settings.oar_watch_announcement_priority,
        }.get(module, 0)
