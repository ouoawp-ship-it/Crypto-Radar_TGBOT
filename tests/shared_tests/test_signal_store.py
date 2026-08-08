from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from shared.signal_store import (
    SignalEventStore,
    append_from_push,
    build_ai_cache_key,
    signal_public_ref,
)


class CountingSignalEventStore(SignalEventStore):
    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "connection_count", 0)

    @contextmanager
    def connect(self):
        object.__setattr__(self, "connection_count", self.connection_count + 1)
        with super().connect() as conn:
            yield conn


class SignalEventStoreTests(unittest.TestCase):
    def settings_for(self, tmp: str) -> Settings:
        return Settings(
            data_dir=Path(tmp),
            signal_events_db_path=Path(tmp) / "signals.db",
        )

    @staticmethod
    def ai_context(*, direction: str = "bullish", stage: str = "forming") -> dict[str, object]:
        return {
            "discovery_score": 72,
            "rule_result": {
                "status": "ready",
                "direction": direction,
                "stage": stage,
                "score_semantics": "evidence_not_probability",
                "data_complete": True,
            },
            "launch_phase": {
                "timing_stage": "forming",
                "execution_status": "wait_confirmation",
            },
            "smc_filter": {},
            "multi_timeframe": {},
            "price_open_interest": {"price_1h_pct": 2.5, "oi_1h_pct": 3.0},
            "active_flow": {"spot_active_ratio": 0.2},
            "funding_basis": {},
            "structure": {},
            "plan": {},
            "completeness": {},
        }

    @staticmethod
    def ai_result(*, direction: str = "bullish", stage: str = "forming") -> dict[str, object]:
        return {
            "status": "available",
            "direction": direction,
            "stage": stage,
            "summary": "规则证据偏强，但仍需等待阶段确认。",
            "supporting_evidence": ["价格与持仓同向"],
            "counter_evidence": ["上方仍有压力"],
            "risk_notes": ["不要追涨"],
            "wait_for": ["等待结构保持"],
            "limitations": ["证据分不是概率"],
        }

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    def test_init_creates_table_indexes_and_compat_view(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SignalEventStore(Path(tmp) / "signals.db")
            with store.connect():
                pass
            with closing(sqlite3.connect(Path(tmp) / "signals.db")) as conn:
                objects = {
                    row[1]: row[0]
                    for row in conn.execute(
                        "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'index', 'view')"
                    ).fetchall()
                }

        self.assertEqual(objects["signals"], "table")
        self.assertEqual(objects["signal_events"], "view")
        self.assertEqual(objects["idx_signals_ts"], "index")
        self.assertEqual(objects["idx_signals_symbol_ts"], "index")
        self.assertEqual(objects["idx_signals_module_ts"], "index")
        self.assertEqual(objects["idx_signals_template_ts"], "index")
        self.assertEqual(objects["ux_signals_dedup_symbol"], "index")
        self.assertEqual(objects["signal_ai_snapshots"], "table")
        self.assertEqual(objects["signal_ai_cache"], "table")
        self.assertEqual(objects["signal_ai_audit"], "table")
        self.assertEqual(objects["idx_signal_ai_snapshots_public_ref"], "index")
        self.assertEqual(objects["idx_signal_ai_cache_signal"], "index")

    def test_append_from_push_extracts_multiple_symbols(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            count = append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:multi",
                status="sent",
                sent=True,
                text="Launch BTCUSDT and ETHUSDT\nScore: 88",
                ts=1000,
                topic_id="12",
                message_ids=[101, 102],
            )
            items = SignalEventStore(settings.signal_events_db_path).list_signals()["items"]

        self.assertEqual(count, 2)
        self.assertEqual({item["symbol"] for item in items}, {"BTCUSDT", "ETHUSDT"})
        self.assertTrue(all(item["module"] == "launch" for item in items))
        self.assertTrue(all(item["message_ids"] == [101, 102] for item in items))

    def test_prune_applies_age_and_row_limits(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for index, ts in enumerate((100, 200, 300, 400, 500), 1):
                append_from_push(
                    settings,
                    template_id="TG_TEST_MESSAGE",
                    dedup_key=f"prune:{index}",
                    status="sent",
                    sent=True,
                    text=f"BTCUSDT prune {index}",
                    ts=ts,
                )
            store = SignalEventStore(settings.signal_events_db_path)

            result = store.prune(before_ts=200, max_rows=2)
            items = store.list_signals(limit=10)["items"]

        self.assertEqual(result["before"], 5)
        self.assertEqual(result["expired"], 1)
        self.assertEqual(result["overflow"], 2)
        self.assertEqual(result["after"], 2)
        self.assertEqual([item["ts"] for item in items], [500, 400])

    def test_symbol_extraction_ignores_encoded_tradingview_url(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            count = append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="flow:url-artifact",
                status="sent",
                sent=True,
                text='<a href="https://tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P">BTCUSDT</a> 75分',
                ts=1000,
            )
            items = SignalEventStore(settings.signal_events_db_path).list_signals()["items"]

        self.assertEqual(count, 1)
        self.assertEqual([item["symbol"] for item in items], ["BTCUSDT"])
        self.assertEqual(items[0]["score"], 75)
        self.assertEqual(items[0]["ingest_mode"], "text_fallback")

    def test_structured_records_persist_per_symbol_facts(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:structured",
                status="sent",
                sent=True,
                text="BTCUSDT 75分 ETHUSDT 61分",
                ts=1000,
                structured_records=[
                    {"symbol": "BTCUSDT", "score": 75, "stage": "breakout", "price": 100.5},
                    {"symbol": "ETHUSDT", "score": 61, "stage": "watch", "price": 25.5},
                ],
            )
            items = SignalEventStore(settings.signal_events_db_path).list_signals(limit=10)["items"]

        by_symbol = {item["symbol"]: item for item in items}
        self.assertEqual(by_symbol["BTCUSDT"]["score"], 75)
        self.assertEqual(by_symbol["ETHUSDT"]["score"], 61)
        self.assertEqual(by_symbol["BTCUSDT"]["stage"], "breakout")
        self.assertEqual(by_symbol["BTCUSDT"]["ingest_mode"], "structured")
        self.assertEqual(by_symbol["BTCUSDT"]["quality_status"], "ready")
        self.assertEqual(by_symbol["BTCUSDT"]["payload"]["facts"]["price"], 100.5)
        self.assertTrue(by_symbol["BTCUSDT"]["payload"]["facts"]["evaluation_eligible"])

    def test_launch_ai_snapshot_is_bounded_and_loaded_only_by_public_ref(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            context = self.ai_context()
            append_from_push(
                settings,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:ai:snapshot",
                status="sent",
                sent=True,
                text="BTCUSDT launch signal",
                ts=1_000,
                structured_records=[{
                    "symbol": "BTCUSDT",
                    "stage": "forming",
                    "ai_context_snapshot": context,
                }],
            )
            store = SignalEventStore(settings.signal_events_db_path)
            public_ref = signal_public_ref("launch:ai:snapshot", "BTCUSDT")
            loaded = store.load_ai_context_snapshot(public_ref)
            numeric = store.load_ai_context_snapshot("1")
            detail = store.signal_detail(public_ref) or {}

        self.assertEqual(loaded["status"], "ready")
        self.assertEqual(loaded["public_ref"], public_ref)
        self.assertEqual(loaded["symbol"], "BTCUSDT")
        self.assertEqual(loaded["signal_ts"], 1_000)
        self.assertEqual(loaded["stage"], "forming")
        self.assertEqual(loaded["snapshot"], context)
        self.assertRegex(str(loaded["context_hash"]), r"^[0-9a-f]{64}$")
        self.assertEqual(numeric, {"status": "invalid_public_ref"})
        self.assertEqual(
            detail["payload"]["ai_context_snapshot_status"],
            "ready",
        )
        self.assertNotIn("text_html", loaded)
        self.assertNotIn("topic_id", loaded)
        self.assertNotIn("message_ids", loaded)

    def test_ai_snapshot_rejects_oversize_secrets_and_reasoning_without_blocking_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            records = (
                (
                    "oversize",
                    {"rule_result": {"direction": "bullish", "blob": "x" * 17_000}},
                    "snapshot_too_large",
                ),
                (
                    "secret",
                    {"rule_result": {"direction": "bullish"}, "api_key": "private-value"},
                    "snapshot_invalid",
                ),
                (
                    "reasoning",
                    {"rule_result": {"direction": "bullish"}, "reasoning_content": "hidden"},
                    "snapshot_invalid",
                ),
            )
            store = SignalEventStore(settings.signal_events_db_path)
            for index, (name, context, _expected) in enumerate(records, 1):
                store.append_from_push(
                    template_id="TG_LAUNCH_ALERT",
                    dedup_key=f"launch:ai:{name}",
                    status="sent",
                    sent=True,
                    text=f"BTCUSDT {name}",
                    ts=1_000 + index,
                    structured_records=[{
                        "symbol": "BTCUSDT",
                        "ai_context_snapshot": context,
                    }],
                )

            statuses = []
            for name, _context, expected in records:
                public_ref = signal_public_ref(f"launch:ai:{name}", "BTCUSDT")
                statuses.append(store.load_ai_context_snapshot(public_ref)["status"])
                item = store.signal_detail(public_ref) or {}
                self.assertEqual(
                    item["payload"]["ai_context_snapshot_status"],
                    expected,
                )
            with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
                snapshot_count = conn.execute(
                    "SELECT COUNT(*) FROM signal_ai_snapshots"
                ).fetchone()[0]
                database_text = " ".join(
                    str(value)
                    for row in conn.execute(
                        "SELECT context_json FROM signal_ai_snapshots"
                    ).fetchall()
                    for value in row
                )

        self.assertEqual(statuses, ["snapshot_missing"] * 3)
        self.assertEqual(snapshot_count, 0)
        self.assertNotIn("private-value", database_text)
        self.assertNotIn("hidden", database_text)

    def test_ai_snapshot_loader_requires_sent_ready_launch_signal(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            store = SignalEventStore(settings.signal_events_db_path)
            cases = (
                ("TG_FLOW_RADAR", "flow", "sent", True),
                ("TG_LAUNCH_ALERT", "dry", "dry_run", False),
                ("TG_LAUNCH_ALERT", "blocked", "blocked", False),
                ("TG_LAUNCH_ALERT", "degraded", "sent", True),
            )
            refs = []
            for index, (template_id, name, status, sent) in enumerate(cases, 1):
                dedup_key = f"strict:{name}"
                store.append_from_push(
                    template_id=template_id,
                    dedup_key=dedup_key,
                    status=status,
                    sent=sent,
                    text=f"BTCUSDT {name}",
                    ts=1_000 + index,
                    structured_records=[{
                        "symbol": "BTCUSDT",
                        "ai_context_snapshot": self.ai_context(),
                    }],
                )
                refs.append(signal_public_ref(dedup_key, "BTCUSDT"))
            with store.connect() as conn:
                conn.execute(
                    "UPDATE signals SET quality_status = 'degraded' WHERE dedup_key = 'strict:degraded'"
                )
            statuses = [store.load_ai_context_snapshot(ref)["status"] for ref in refs]

        self.assertEqual(statuses, ["signal_unavailable"] * 4)

    def test_schema_six_migrates_additively_and_old_signal_reports_snapshot_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            store = SignalEventStore(settings.signal_events_db_path)
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key="legacy:ai:missing",
                status="sent",
                sent=True,
                text="BTCUSDT legacy",
                ts=1_000,
                structured_records=[{"symbol": "BTCUSDT", "stage": "watch"}],
            )
            with store.connect() as conn:
                conn.execute("DROP TABLE signal_ai_audit")
                conn.execute("DROP TABLE signal_ai_cache")
                conn.execute("DROP TABLE signal_ai_snapshots")
                conn.execute(
                    "UPDATE signal_store_meta SET value = '6' WHERE key = 'schema_version'"
                )
            public_ref = signal_public_ref("legacy:ai:missing", "BTCUSDT")
            loaded = store.load_ai_context_snapshot(public_ref)
            detail = store.signal_detail(public_ref) or {}
            with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
                version = conn.execute(
                    "SELECT value FROM signal_store_meta WHERE key = 'schema_version'"
                ).fetchone()[0]
                ai_tables = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name LIKE 'signal_ai_%'"
                ).fetchone()[0]

        self.assertEqual(loaded["status"], "snapshot_missing")
        self.assertEqual(loaded["symbol"], "BTCUSDT")
        self.assertEqual(loaded["signal_ts"], 1_000)
        self.assertEqual(loaded["stage"], "watch")
        self.assertEqual(detail["symbol"], "BTCUSDT")
        self.assertEqual(version, "7")
        self.assertEqual(ai_tables, 3)

    def test_ai_cache_key_uses_only_all_reviewed_components(self) -> None:
        values = {
            "context_hash": self.digest("context"),
            "model": "provider/model-v1",
            "endpoint_hash": self.digest("endpoint"),
            "prompt_hash": self.digest("prompt"),
            "policy_version": "launch-ai-v1",
        }
        keys = {build_ai_cache_key(**values)}
        changes = {
            "context_hash": self.digest("context-2"),
            "model": "provider/model-v2",
            "endpoint_hash": self.digest("endpoint-2"),
            "prompt_hash": self.digest("prompt-2"),
            "policy_version": "launch-ai-v2",
        }
        for field, changed in changes.items():
            keys.add(build_ai_cache_key(**{**values, field: changed}))

        self.assertEqual(len(keys), 6)
        self.assertTrue(all(key.startswith("aic_") for key in keys))
        with self.assertRaisesRegex(ValueError, "ai_cache_hash_invalid"):
            build_ai_cache_key(**{**values, "endpoint_hash": "https://private.invalid"})

    def test_ai_cache_singleflight_success_and_audit_do_not_charge_cache_hits(self) -> None:
        now = 1_700_000_000
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            store = SignalEventStore(settings.signal_events_db_path)
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:ai:cache",
                status="sent",
                sent=True,
                text="BTCUSDT cache",
                ts=now - 60,
                structured_records=[{
                    "symbol": "BTCUSDT",
                    "stage": "forming",
                    "ai_context_snapshot": self.ai_context(),
                }],
            )
            public_ref = signal_public_ref("launch:ai:cache", "BTCUSDT")
            request = {
                "model": "provider/model-v1",
                "endpoint_hash": self.digest("endpoint"),
                "prompt_hash": self.digest("prompt"),
                "policy_version": "launch-ai-v1",
                "daily_limit": 1,
            }
            first = store.reserve_ai_interpretation(public_ref, now_ts=now, **request)
            duplicate = store.reserve_ai_interpretation(
                public_ref,
                now_ts=now + 1,
                **request,
            )
            completed = store.cache_ai_success(
                str(first["cache_key"]),
                str(first["lease_id"]),
                self.ai_result(),
                now_ts=now + 2,
            )
            cached = store.reserve_ai_interpretation(
                public_ref,
                now_ts=now + 3,
                **request,
            )
            quota = store.ai_daily_quota(now_ts=now + 3, daily_limit=1)
            audit = store.list_ai_interpretation_audit(public_ref)

        self.assertEqual(first["status"], "reserved")
        self.assertEqual(first["symbol"], "BTCUSDT")
        self.assertEqual(first["signal_ts"], now - 60)
        self.assertEqual(first["stage"], "forming")
        self.assertEqual(duplicate["status"], "in_flight")
        self.assertEqual(completed, {"status": "available", "stored": True})
        self.assertEqual(cached["status"], "available")
        self.assertEqual(cached["source"], "cache")
        self.assertEqual(cached["result"], self.ai_result())
        self.assertEqual(quota["provider_reserved"], 1)
        self.assertTrue(quota["exhausted"])
        self.assertEqual(
            [item["event"] for item in audit],
            ["cache_hit", "completed", "deduplicated", "reserved"],
        )

    def test_ai_failure_is_short_safe_cooldown_and_stale_lease_cannot_complete(self) -> None:
        now = 1_700_000_000
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            store = SignalEventStore(settings.signal_events_db_path)
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:ai:failure",
                status="sent",
                sent=True,
                text="BTCUSDT failure",
                ts=now - 60,
                structured_records=[{
                    "symbol": "BTCUSDT",
                    "ai_context_snapshot": self.ai_context(),
                }],
            )
            public_ref = signal_public_ref("launch:ai:failure", "BTCUSDT")
            request = {
                "model": "provider/model-v1",
                "endpoint_hash": self.digest("endpoint"),
                "prompt_hash": self.digest("prompt"),
                "policy_version": "launch-ai-v1",
            }
            first = store.reserve_ai_interpretation(public_ref, now_ts=now, **request)
            failure = store.cache_ai_failure(
                str(first["cache_key"]),
                str(first["lease_id"]),
                "provider body https://private.invalid secret",
                cooldown_sec=999,
                now_ts=now + 1,
            )
            cooling = store.reserve_ai_interpretation(
                public_ref,
                now_ts=now + 2,
                **request,
            )
            second = store.reserve_ai_interpretation(
                public_ref,
                now_ts=now + 302,
                **request,
            )
            stale = store.cache_ai_success(
                str(first["cache_key"]),
                str(first["lease_id"]),
                self.ai_result(),
                now_ts=now + 303,
            )
            invalid_result = {**self.ai_result(), "reasoning_content": "private chain"}
            invalid = store.cache_ai_success(
                str(second["cache_key"]),
                str(second["lease_id"]),
                invalid_result,
                now_ts=now + 304,
            )
            with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
                state, result_json, error_code, cooldown_until = conn.execute(
                    "SELECT state, result_json, error_code, cooldown_until FROM signal_ai_cache"
                ).fetchone()
                raw_database = " ".join(
                    str(value)
                    for table in ("signal_ai_cache", "signal_ai_audit")
                    for row in conn.execute(f"SELECT * FROM {table}").fetchall()
                    for value in row
                )

        self.assertEqual(failure["status"], "cooldown")
        self.assertEqual(failure["error_code"], "ai_request_failed")
        self.assertEqual(failure["retry_after"], 300)
        self.assertEqual(cooling["status"], "cooldown")
        self.assertEqual(cooling["error_code"], "ai_request_failed")
        self.assertEqual(second["status"], "reserved")
        self.assertEqual(stale, {"status": "stale_request", "stored": False})
        self.assertEqual(invalid["status"], "invalid_ai_result")
        self.assertEqual(state, "cooldown")
        self.assertEqual(json.loads(result_json), {})
        self.assertEqual(error_code, "invalid_ai_result")
        self.assertEqual(cooldown_until, now + 364)
        self.assertNotIn("private.invalid", raw_database)
        self.assertNotIn("private chain", raw_database)

    def test_ai_success_must_preserve_snapshot_direction_and_stage(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            store = SignalEventStore(settings.signal_events_db_path)
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:ai:conflict",
                status="sent",
                sent=True,
                text="BTCUSDT conflict",
                ts=1_000,
                structured_records=[{
                    "symbol": "BTCUSDT",
                    "ai_context_snapshot": self.ai_context(),
                }],
            )
            public_ref = signal_public_ref("launch:ai:conflict", "BTCUSDT")
            reserved = store.reserve_ai_interpretation(
                public_ref,
                model="provider/model-v1",
                endpoint_hash=self.digest("endpoint"),
                prompt_hash=self.digest("prompt"),
                policy_version="launch-ai-v1",
                now_ts=1_100,
            )
            conflict = store.cache_ai_success(
                str(reserved["cache_key"]),
                str(reserved["lease_id"]),
                self.ai_result(direction="bearish"),
                now_ts=1_101,
            )
            with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
                state, result_json, error_code = conn.execute(
                    "SELECT state, result_json, error_code FROM signal_ai_cache"
                ).fetchone()

        self.assertEqual(conflict["status"], "ai_rule_conflict")
        self.assertFalse(conflict["stored"])
        self.assertEqual(state, "cooldown")
        self.assertEqual(json.loads(result_json), {})
        self.assertEqual(error_code, "ai_rule_conflict")

    def test_ai_daily_limit_is_atomic_and_cache_hit_bypasses_exhaustion(self) -> None:
        now = 1_700_000_000
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            store = SignalEventStore(settings.signal_events_db_path)
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:ai:quota",
                status="sent",
                sent=True,
                text="BTCUSDT quota",
                ts=now - 60,
                structured_records=[{
                    "symbol": "BTCUSDT",
                    "ai_context_snapshot": self.ai_context(),
                }],
            )
            public_ref = signal_public_ref("launch:ai:quota", "BTCUSDT")
            endpoint_hash = self.digest("endpoint")
            prompt_hash = self.digest("prompt")

            def reserve(model: str) -> tuple[str, dict[str, object]]:
                return model, store.reserve_ai_interpretation(
                    public_ref,
                    model=model,
                    endpoint_hash=endpoint_hash,
                    prompt_hash=prompt_hash,
                    policy_version="launch-ai-v1",
                    daily_limit=1,
                    now_ts=now,
                )

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(reserve, ("provider/model-a", "provider/model-b")))
            statuses = {str(result["status"]) for _model, result in results}
            reserved_model, reserved = next(
                (model, result)
                for model, result in results
                if result["status"] == "reserved"
            )
            store.cache_ai_success(
                str(reserved["cache_key"]),
                str(reserved["lease_id"]),
                self.ai_result(),
                now_ts=now + 1,
            )
            cached = reserve(reserved_model)[1]
            quota = store.ai_daily_quota(now_ts=now + 1, daily_limit=1)
            audit = store.list_ai_interpretation_audit(public_ref)

        self.assertEqual(statuses, {"reserved", "quota_exhausted"})
        self.assertEqual(cached["status"], "available")
        self.assertEqual(cached["source"], "cache")
        self.assertEqual(quota["provider_reserved"], 1)
        self.assertEqual(quota["remaining"], 0)
        self.assertEqual(
            sum(item["event"] == "reserved" for item in audit),
            1,
        )
        self.assertEqual(
            sum(item["event"] == "quota_rejected" for item in audit),
            1,
        )

    def test_repair_legacy_signals_is_auditable_and_backed_up(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            store = SignalEventStore(settings.signal_events_db_path)
            store.append_from_push(
                template_id="TG_FLOW_RADAR",
                dedup_key="legacy:real",
                status="sent",
                sent=True,
                text='<a href="https://tradingview.com/?symbol=BINANCE%3ABTCUSDT.P">BTCUSDT</a> 82分',
                ts=1000,
            )
            with store.connect() as conn:
                conn.execute(
                    "UPDATE signals SET symbol = '3ABTCUSDT', coin = '3ABTC', score = NULL WHERE dedup_key = 'legacy:real'"
                )
                conn.execute(
                    "INSERT INTO signals (ts, time, module, template_id, signal_type, symbol, coin, dedup_key, status, sent, text_html) "
                    "VALUES (1001, '1970-01-01T00:16:41+00:00', 'flow', 'TG_FLOW_RADAR', 'flow', "
                    "'ETHUSDT', 'ETH', 'legacy:score', 'sent', 1, 'ETHUSDT 79分')"
                )
                conn.commit()

            dry_run = store.repair_legacy_signals()
            applied = store.repair_legacy_signals(apply=True)
            remaining = store.list_signals(limit=10)["items"]
            backup_exists = Path(applied["backup_path"]).exists()

        self.assertEqual(dry_run["artifact_rows"], 1)
        self.assertEqual(dry_run["recoverable_scores"], 1)
        self.assertFalse(dry_run["applied"])
        self.assertEqual(applied["deleted"], 1)
        self.assertEqual(applied["scores_recovered"], 1)
        self.assertTrue(backup_exists)
        self.assertEqual([item["symbol"] for item in remaining], ["ETHUSDT"])
        self.assertEqual(remaining[0]["score"], 79)

    def test_append_from_push_without_symbol_writes_empty_symbol_event(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            count = append_from_push(
                settings,
                template_id="TG_RADAR_SUMMARY",
                dedup_key="summary:no-symbol",
                status="dry_run",
                sent=False,
                text="推送摘要：本轮没有具体币种。",
                ts=1000,
            )
            items = SignalEventStore(settings.signal_events_db_path).list_signals()["items"]

        self.assertEqual(count, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "")
        self.assertEqual(items[0]["status"], "dry_run")

    def test_duplicate_dedup_key_and_symbol_is_upserted(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for score in (70, 90):
                append_from_push(
                    settings,
                    template_id="TG_LAUNCH_ALERT",
                    dedup_key="launch:BTC",
                    status="sent",
                    sent=True,
                    text=f"BTCUSDT\n分数: {score}",
                    ts=score,
                    message_ids=[score],
                )
            items = SignalEventStore(settings.signal_events_db_path).list_signals()["items"]

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["symbol"], "BTCUSDT")
        self.assertEqual(items[0]["score"], 90)
        self.assertEqual(items[0]["message_ids"], [90])

    def test_list_signals_supports_limit_cursor_symbol_and_status(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for idx, (symbol, status) in enumerate((("BTCUSDT", "sent"), ("ETHUSDT", "failed"), ("BTCUSDT", "dry_run")), start=1):
                append_from_push(
                    settings,
                    template_id="TG_FLOW_RADAR",
                    dedup_key=f"flow:{idx}",
                    status=status,
                    sent=status == "sent",
                    text=f"{symbol}\n分数: {idx}",
                    ts=1000 + idx,
                )
            store = SignalEventStore(settings.signal_events_db_path)
            first = store.list_signals(limit=1)
            older = store.list_signals(limit=10, cursor=first["next_cursor"])
            btc = store.list_signals(symbol="BTCUSDT")
            failed = store.list_signals(status="failed")

        self.assertEqual(first["count"], 1)
        self.assertEqual(older["count"], 2)
        self.assertEqual([item["symbol"] for item in btc["items"]], ["BTCUSDT", "BTCUSDT"])
        self.assertEqual(failed["items"][0]["symbol"], "ETHUSDT")

    def test_list_signals_supports_sort_and_time_range(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for idx, symbol in enumerate(("BTCUSDT", "ETHUSDT", "SOLUSDT"), start=1):
                append_from_push(
                    settings,
                    template_id="TG_FLOW_RADAR",
                    dedup_key=f"range:{idx}",
                    status="sent",
                    sent=True,
                    text=symbol,
                    ts=1000 + idx,
                )
            store = SignalEventStore(settings.signal_events_db_path)
            asc = store.list_signals(limit=3, sort_field="ts", sort_direction="asc")
            ranged = store.list_signals(limit=10, start_ts=1002, end_ts=1002)

        self.assertEqual([item["symbol"] for item in asc["items"]], ["BTCUSDT", "ETHUSDT", "SOLUSDT"])
        self.assertEqual(ranged["count"], 1)
        self.assertEqual(ranged["items"][0]["symbol"], "ETHUSDT")

    def test_list_signals_supports_q_search(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="search:btc",
                status="sent",
                sent=True,
                text="BTCUSDT strong flow breakout",
                ts=1000,
            )
            append_from_push(
                settings,
                template_id="TG_FUNDING_RADAR",
                dedup_key="search:eth",
                status="sent",
                sent=True,
                text="ETHUSDT funding watch",
                ts=1001,
            )
            store = SignalEventStore(settings.signal_events_db_path)
            btc = store.list_signals(q="btc")
            funding = store.list_signals(q="funding")

        self.assertEqual(btc["count"], 1)
        self.assertEqual(btc["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(funding["count"], 1)
        self.assertEqual(funding["items"][0]["symbol"], "ETHUSDT")

    def test_stats_returns_status_module_and_top_symbols(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(settings, template_id="TG_LAUNCH_ALERT", dedup_key="a", status="sent", sent=True, text="BTCUSDT", ts=1000)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="b", status="failed", sent=False, text="BTCUSDT", ts=1001)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="c", status="dry_run", sent=False, text="ETHUSDT", ts=1002)
            stats = SignalEventStore(settings.signal_events_db_path).stats(window_sec=10**10)

        self.assertEqual(stats["sent"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["dry_run"], 1)
        self.assertEqual(stats["by_status"]["sent"], 1)
        self.assertEqual(stats["by_module"]["flow"], 2)
        self.assertEqual(stats["top_symbols"][0]["symbol"], "BTCUSDT")

    def test_symbol_queries_support_coin_detail_views(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(settings, template_id="TG_LAUNCH_ALERT", dedup_key="coin:a", status="sent", sent=True, text="BTCUSDT launch", ts=1000)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="coin:b", status="failed", sent=False, text="BTCUSDT flow", ts=1001)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="coin:c", status="sent", sent=True, text="ETHUSDT flow", ts=1002)
            store = SignalEventStore(settings.signal_events_db_path)
            active = store.search_symbols(limit=10, start_ts=999, end_ts=1003)
            btc_search = store.search_symbols(q="btc", limit=10, start_ts=999, end_ts=1003)
            btc_stats = store.stats_by_symbol("BTC", start_ts=999, end_ts=1003)
            first = store.list_by_symbol("BTCUSDT", limit=1, start_ts=999, end_ts=1003)
            older = store.list_by_symbol("BTC", limit=10, cursor=first["next_cursor"], start_ts=999, end_ts=1003)

        self.assertEqual(active[0]["symbol"], "BTCUSDT")
        self.assertEqual(active[0]["count"], 2)
        self.assertEqual(btc_search[0]["symbol"], "BTCUSDT")
        self.assertEqual(btc_stats["total"], 2)
        self.assertEqual(btc_stats["failed"], 1)
        self.assertEqual(btc_stats["by_module"]["flow"], 1)
        self.assertEqual(first["count"], 1)
        self.assertEqual(older["count"], 1)

    def test_timeline_queries_support_filters_and_special_search_text(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(settings, template_id="TG_LAUNCH_ALERT", dedup_key="timeline:a", status="sent", sent=True, text="BTCUSDT launch alpha", ts=1000)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="timeline:b", status="failed", sent=False, text="BTCUSDT flow beta", ts=1001)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="timeline:c", status="sent", sent=True, text="ETHUSDT flow alpha_%", ts=1002)
            store = SignalEventStore(settings.signal_events_db_path)
            btc = store.list_timeline(symbol="BTC", limit=10, start_ts=999, end_ts=1003)
            failed = store.list_timeline(status="failed", limit=10, start_ts=999, end_ts=1003)
            flow = store.list_timeline(module="flow", limit=10, start_ts=999, end_ts=1003)
            alpha = store.list_timeline(q="alpha", limit=10, start_ts=999, end_ts=1003)
            special = store.list_timeline(q="alpha_%", limit=10, start_ts=999, end_ts=1003)
            stats = store.timeline_stats(symbol="BTCUSDT", start_ts=999, end_ts=1003)

        self.assertEqual(btc["count"], 2)
        self.assertEqual(failed["count"], 1)
        self.assertEqual(failed["items"][0]["status"], "failed")
        self.assertEqual(flow["count"], 2)
        self.assertEqual(alpha["count"], 2)
        self.assertIsInstance(special["items"], list)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["by_module"]["flow"], 1)

    def test_signal_events_view_matches_signals_table(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_TEST_MESSAGE",
                dedup_key="test:view",
                status="dry_run",
                sent=False,
                text="Telegram test message",
                ts=1000,
            )
            with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
                conn.row_factory = sqlite3.Row
                signals_count = conn.execute("SELECT COUNT(*) AS c FROM signals").fetchone()["c"]
                compat_count = conn.execute("SELECT COUNT(*) AS c FROM signal_events").fetchone()["c"]
                latest = conn.execute(
                    """
                    SELECT template_id, status, module, excerpt
                    FROM signal_events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()

        self.assertEqual(signals_count, 1)
        self.assertEqual(compat_count, 1)
        self.assertEqual(latest["template_id"], "TG_TEST_MESSAGE")
        self.assertEqual(latest["status"], "dry_run")
        self.assertEqual(latest["module"], "test")
        self.assertIn("Telegram test message", latest["excerpt"])

    def test_existing_signals_database_gets_signal_events_view(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            store = SignalEventStore(db_path)
            with store.connect():
                pass
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute("DROP VIEW signal_events")
                conn.commit()
                missing = conn.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name = 'signal_events'"
                ).fetchone()[0]
            self.assertEqual(missing, 0)

            with store.connect():
                pass

            with closing(sqlite3.connect(db_path)) as conn:
                row = conn.execute(
                    "SELECT type FROM sqlite_master WHERE name = 'signal_events'"
                ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row[0], "view")

    def test_current_schema_check_does_not_rewrite_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SignalEventStore(Path(tmp) / "signals.db")
            with store.connect():
                pass

            with store.connect() as conn:
                changes = conn.total_changes

        self.assertEqual(changes, 0)

    def test_schema_upgrade_removes_records_from_retired_modules(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for index in (1, 2):
                append_from_push(
                    settings,
                    template_id="TG_FLOW_RADAR",
                    dedup_key=f"migration:{index}",
                    status="sent",
                    sent=True,
                    text=f"BTCUSDT migration {index}",
                    ts=1000 + index,
                )
            with closing(sqlite3.connect(settings.signal_events_db_path)) as conn:
                conn.execute("UPDATE signals SET module = 'retired' WHERE dedup_key = 'migration:1'")
                conn.execute(
                    "UPDATE signal_store_meta SET value = '3' WHERE key = 'schema_version'"
                )
                conn.commit()

            store = SignalEventStore(settings.signal_events_db_path)
            with store.connect():
                pass
            items = store.list_signals(limit=10)["items"]

        self.assertEqual([item["dedup_key"] for item in items], ["migration:2"])

    def test_list_by_symbols_limits_each_symbol_in_one_query(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            for index, symbol in enumerate(("BTCUSDT", "BTCUSDT", "ETHUSDT", "ETHUSDT"), 1):
                append_from_push(
                    settings,
                    template_id="TG_TEST_MESSAGE",
                    dedup_key=f"batch:{index}",
                    status="sent",
                    sent=True,
                    text=f"{symbol} test {index}",
                    ts=1000 + index,
                )
            grouped = SignalEventStore(settings.signal_events_db_path).list_by_symbols(
                ["BTC", "ETHUSDT"],
                limit_per_symbol=1,
            )

        self.assertEqual(set(grouped), {"BTCUSDT", "ETHUSDT"})
        self.assertEqual([item["id"] for item in grouped["BTCUSDT"]], [2])
        self.assertEqual([item["id"] for item in grouped["ETHUSDT"]], [4])

    def test_compact_projection_preserves_shape_and_defers_large_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="compact:btc",
                status="sent",
                sent=True,
                text="BTCUSDT " + ("x" * 5000),
                ts=1000,
            )
            store = SignalEventStore(settings.signal_events_db_path)
            compact = store.list_signals(limit=1, compact=True)["items"][0]
            detail = store.signal_detail(int(compact["id"])) or {}
            compact_detail = store.signal_detail(int(compact["id"]), compact=True) or {}

        self.assertEqual(set(compact), set(detail))
        self.assertEqual(compact["text_html"], "")
        self.assertEqual(compact["payload"], {})
        self.assertLessEqual(len(compact["excerpt"]), 260)
        self.assertGreater(len(detail["text_html"]), 5000)
        self.assertEqual(detail["payload"]["source"], "telegram_push")
        self.assertEqual(compact_detail["text_html"], "")
        self.assertEqual(compact_detail["payload"], {})

    def test_stats_with_latest_uses_one_connection(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="stats:one-connection",
                status="sent",
                sent=True,
                text="BTCUSDT stats",
                ts=1000,
            )
            store = CountingSignalEventStore(settings.signal_events_db_path)
            payload = store.stats_with_latest(window_sec=10**10)

        self.assertEqual(store.connection_count, 1)
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["latest_sent"][0]["symbol"], "BTCUSDT")

    def test_public_stats_and_health_summaries_avoid_unused_detail_queries(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = self.settings_for(tmp)
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="stats:public-summary",
                status="sent",
                sent=True,
                text="BTCUSDT stats",
                ts=int(time.time()),
            )
            store = CountingSignalEventStore(settings.signal_events_db_path)
            public_stats = store.stats_with_recent(window_sec=86400)
            health = store.health_summary(window_sec=86400)

        self.assertEqual(store.connection_count, 2)
        self.assertEqual(public_stats["total"], 1)
        self.assertEqual(public_stats["latest"][0]["symbol"], "BTCUSDT")
        self.assertNotIn("latest_sent", public_stats)
        self.assertEqual(health["total"], 1)
        self.assertTrue(health["latest_at"])

    def test_signal_events_view_defaults_missing_legacy_columns(self) -> None:
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "signals.db"
            with closing(sqlite3.connect(db_path)) as conn:
                conn.execute(
                    """
                    CREATE TABLE signals (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        ts INTEGER NOT NULL,
                        time TEXT NOT NULL,
                        module TEXT NOT NULL,
                        template_id TEXT NOT NULL,
                        signal_type TEXT NOT NULL,
                        symbol TEXT NOT NULL DEFAULT '',
                        coin TEXT NOT NULL DEFAULT '',
                        stage TEXT NOT NULL DEFAULT '',
                        severity TEXT NOT NULL DEFAULT 'info',
                        score REAL,
                        title TEXT NOT NULL DEFAULT '',
                        excerpt TEXT NOT NULL DEFAULT '',
                        text_html TEXT NOT NULL DEFAULT '',
                        dedup_key TEXT NOT NULL,
                        status TEXT NOT NULL,
                        sent INTEGER NOT NULL DEFAULT 0,
                        topic_id TEXT NOT NULL DEFAULT '',
                        message_ids_json TEXT NOT NULL DEFAULT '[]',
                        reply_to_message_id INTEGER NOT NULL DEFAULT 0
                    )
                    """
                )
                conn.execute(
                    """
                    INSERT INTO signals (
                        ts, time, module, template_id, signal_type, symbol, coin, stage, severity,
                        score, title, excerpt, text_html, dedup_key, status, sent, topic_id,
                        message_ids_json, reply_to_message_id
                    ) VALUES (
                        1000, '1970-01-01T00:16:40+00:00', 'test', 'TG_TEST_MESSAGE', '测试',
                        'BTCUSDT', 'BTC', '', 'info', NULL, 'title', 'excerpt', 'body',
                        'dedup', 'dry_run', 0, '', '[]', 0
                    )
                    """
                )
                conn.commit()

            store = SignalEventStore(db_path)
            with store.connect():
                pass

            with closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                latest = conn.execute(
                    """
                    SELECT payload_json, error, public_ref
                    FROM signal_events
                    ORDER BY id DESC
                    LIMIT 1
                    """
                ).fetchone()
            inserted_count = store.append_from_push(
                template_id="TG_TEST_MESSAGE",
                dedup_key="legacy:write",
                status="sent",
                sent=True,
                text="ETHUSDT legacy migration write",
                ts=1001,
            )
            with closing(sqlite3.connect(db_path)) as conn:
                migrated = conn.execute(
                    "SELECT payload_json, error FROM signals WHERE dedup_key = ?",
                    ("legacy:write",),
                ).fetchone()

        self.assertEqual(latest["payload_json"], "{}")
        self.assertEqual(latest["error"], "")
        self.assertEqual(latest["public_ref"], signal_public_ref("dedup", "BTCUSDT"))
        self.assertEqual(inserted_count, 1)
        self.assertIsNotNone(migrated)
        self.assertIn("telegram_push", migrated[0])
        self.assertEqual(migrated[1], "")


if __name__ == "__main__":
    unittest.main()
