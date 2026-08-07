from __future__ import annotations

import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from radars.altcoin_contract_anomaly.formatter import (
    candidate_line,
    render_console,
    render_json,
    render_telegram_preview,
)
from radars.altcoin_contract_anomaly.models import (
    CandidateSnapshot,
    RULES_VERSION,
    SCHEMA_VERSION,
)
from radars.altcoin_contract_anomaly.rules import (
    HIGH_LEVERAGE_CANDIDATE,
    SHORT_SQUEEZE_CANDIDATE,
)
from radars.altcoin_contract_anomaly.state import (
    MODULE_ID,
    CandidatePoolStore,
    CandidateStatePartialUpdateError,
    CandidateStateSchemaError,
    build_pool_document,
    candidate_pool_hash,
    candidate_snapshot_hash,
)
from radars.common import tg_escape


NOW = "2026-08-07T00:00:00+00:00"


def candidate(
    symbol: str,
    *,
    ratio: float | None = 0.30,
    tags: list[str] | None = None,
    collected_at: str = NOW,
    **overrides: object,
) -> CandidateSnapshot:
    normalized = symbol.removesuffix("USDT")
    selected_tags = list(tags if tags is not None else [SHORT_SQUEEZE_CANDIDATE])
    oi_value = 20_000_000.0 * ratio if ratio is not None else None
    values: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "symbol": symbol,
        "base_asset": normalized,
        "normalized_asset": normalized,
        "contract_multiplier": 1,
        "exchange": "binance",
        "contract_type": "PERPETUAL",
        "cmc_id": 100,
        "mapping_method": "existing_verified_anchor",
        "mapping_confidence": "high",
        "market_cap_usd": 20_000_000.0,
        "market_cap_source": "coinmarketcap_official",
        "market_cap_updated_at": NOW,
        "open_interest_raw": oi_value,
        "open_interest_unit": "usd_notional",
        "oi_value_usd": oi_value,
        "mark_price": 1.0,
        "funding_rate": -0.000352,
        "oi_market_cap_ratio": ratio,
        "candidate_tags": selected_tags,
        "matched_rules": [
            f"{RULES_VERSION}:short_squeeze"
            if tag == SHORT_SQUEEZE_CANDIDATE
            else f"{RULES_VERSION}:high_leverage"
            for tag in selected_tags
        ],
        "data_quality": "complete",
        "missing_fields": [],
        "collected_at": collected_at,
        "open_interest_updated_at": NOW,
        "mark_price_updated_at": NOW,
        "funding_rate_updated_at": NOW,
        "stale_fields": [],
        "invalid_fields": [],
        "mapping_evidence": ["binance_cmc_unique_id"],
        "mapping_rejection_reason": None,
        "oi_value_method": "binance_reported_usd_notional",
        "binance_oi_usd": oi_value,
        "binance_oi_market_cap_ratio": ratio,
        "binance_oi_source": "binance_open_interest_hist.sumOpenInterestValue",
        "global_oi_usd": None,
        "global_oi_market_cap_ratio": None,
        "global_oi_source": None,
    }
    values.update(overrides)
    return CandidateSnapshot(**values)  # type: ignore[arg-type]


def pool_document(
    snapshots: list[CandidateSnapshot],
    *,
    previous: dict[str, object] | None = None,
    data_sources: dict[str, object] | None = None,
) -> dict[str, object]:
    return build_pool_document(
        snapshots,
        generated_at=NOW,
        universe={
            "loaded_usdt_perpetuals": len(snapshots) + 3,
            "eligible_altcoin_contracts": len(snapshots),
            "excluded_contracts": 3,
        },
        mapping_stats={
            "trusted_count": len(snapshots),
            "diagnostic_count": 1,
            "conflict_count": 1,
            "unmapped_count": 2,
            "reason_counts": {"ambiguous_symbol": 1, "missing_cmc_id": 1},
        },
        rule_parameters={
            "market_cap_max_usd": 30_000_000.0,
            "short_squeeze_min_ratio": 0.20,
            "short_squeeze_max_funding_rate": 0.0,
            "high_leverage_min_ratio": 0.50,
        },
        mapping_records=[
            {
                "binance_symbol": snapshot.symbol,
                "cmc_id": snapshot.cmc_id,
                "mapping_method": snapshot.mapping_method,
                "mapping_confidence": snapshot.mapping_confidence,
            }
            for snapshot in snapshots
        ],
        previous=previous,
        data_sources=data_sources or {
            "market_cap": "CoinMarketCap official API",
            "open_interest": "Binance USD-M Futures",
        },
        diagnostics={"network_status": "离线测试"},
    )


class CandidatePoolStateTests(unittest.TestCase):
    def test_document_and_store_round_trip_use_module_schema_namespace(self) -> None:
        document = pool_document([candidate("COTIUSDT")])

        self.assertEqual(document["schema_version"], SCHEMA_VERSION)
        self.assertEqual(document["module"], MODULE_ID)
        self.assertEqual(document["candidate_symbols"], ["COTIUSDT"])
        self.assertEqual(document["snapshots"][0]["exchange"], "binance")
        self.assertEqual(document["snapshots"][0]["oi_market_cap_ratio"], 0.30)
        self.assertEqual(
            document["snapshots"][0]["binance_oi_market_cap_ratio"],
            0.30,
        )

        with TemporaryDirectory() as tmp:
            path = Path(tmp) / MODULE_ID / "candidate_pool.json"
            store = CandidatePoolStore(path)
            store.save(document)

            loaded = store.load()

        self.assertEqual(loaded, document)

    def test_atomic_replace_failure_preserves_previous_complete_snapshot(self) -> None:
        first = pool_document([candidate("AAAUSDT")])
        second = pool_document([candidate("BBBUSDT")], previous=first)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / MODULE_ID / "candidate_pool.json"
            store = CandidatePoolStore(path)
            store.save(first)

            with patch("shared.atomic_json.os.replace", side_effect=OSError("replace failed")):
                with self.assertRaises(OSError):
                    store.save(second)

            persisted = json.loads(path.read_text(encoding="utf-8"))
            temporary_files = list(path.parent.glob(f"{path.name}.tmp.*"))

        self.assertEqual(persisted["candidate_symbols"], ["AAAUSDT"])
        self.assertEqual(temporary_files, [])

    def test_corrupt_json_is_quarantined_and_treated_as_no_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / MODULE_ID / "candidate_pool.json"
            path.parent.mkdir(parents=True)
            path.write_text("{not-valid-json", encoding="utf-8")
            store = CandidatePoolStore(path)

            loaded = store.load()
            quarantined = list(path.parent.glob(f"{path.name}.corrupt.*"))

            self.assertIsNone(loaded)
            self.assertFalse(path.exists())
            self.assertEqual(len(quarantined), 1)
            self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "{not-valid-json")

    def test_old_schema_is_rejected_and_cannot_be_overwritten(self) -> None:
        old = {"schema_version": 0, "module": MODULE_ID, "snapshots": []}
        replacement = pool_document([candidate("NEWUSDT")])
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / MODULE_ID / "candidate_pool.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(old), encoding="utf-8")
            store = CandidatePoolStore(path)

            with self.assertRaises(CandidateStateSchemaError):
                store.load()
            with self.assertRaises(CandidateStateSchemaError):
                store.save(replacement)

            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(persisted, old)

    def test_incomplete_same_schema_document_cannot_replace_complete_state(self) -> None:
        complete = pool_document([candidate("SAFEUSDT")])
        incomplete = {"schema_version": SCHEMA_VERSION, "module": MODULE_ID}
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / MODULE_ID / "candidate_pool.json"
            store = CandidatePoolStore(path)
            store.save(complete)

            with self.assertRaises(CandidateStateSchemaError):
                store.save(incomplete)

            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(persisted["candidate_symbols"], ["SAFEUSDT"])

    def test_semantically_inconsistent_same_schema_document_is_rejected(self) -> None:
        complete = pool_document([candidate("SAFEUSDT")])
        inconsistent = json.loads(json.dumps(complete))
        inconsistent["snapshots"] = []
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / MODULE_ID / "candidate_pool.json"
            store = CandidatePoolStore(path)
            store.save(complete)

            with self.assertRaises(CandidateStateSchemaError):
                store.save(inconsistent)

            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(persisted["candidate_symbols"], ["SAFEUSDT"])

    def test_partial_snapshot_cannot_persist_stale_formal_candidate_tags(self) -> None:
        invalid = candidate(
            "PARTIALUSDT",
            ratio=None,
            tags=[SHORT_SQUEEZE_CANDIDATE, HIGH_LEVERAGE_CANDIDATE],
            market_cap_usd=None,
            missing_fields=[
                "market_cap_usd",
                "open_interest_raw",
                "oi_value_usd",
            ],
            data_quality="partial",
        )
        document = pool_document([invalid])

        with TemporaryDirectory() as tmp:
            store = CandidatePoolStore(Path(tmp) / "candidate_pool.json")
            with self.assertRaises(CandidateStateSchemaError):
                store.save(document)

    def test_partial_refresh_cannot_remove_a_previous_complete_candidate(self) -> None:
        complete = pool_document([candidate("SAFEUSDT")])
        partial = pool_document([
            candidate(
                "SAFEUSDT",
                ratio=None,
                tags=[],
                missing_fields=["open_interest_raw", "oi_value_usd"],
                data_quality="partial",
            )
        ], previous=complete)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate_pool.json"
            store = CandidatePoolStore(path)
            store.save(complete)

            with self.assertRaises(CandidateStatePartialUpdateError):
                store.save(partial)

            persisted = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(persisted["candidate_symbols"], ["SAFEUSDT"])

    def test_complete_data_can_remove_a_candidate_when_rules_no_longer_match(self) -> None:
        complete = pool_document([candidate("SAFEUSDT")])
        disqualified = pool_document([
            candidate("SAFEUSDT", ratio=0.10, tags=[])
        ], previous=complete)
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "candidate_pool.json"
            store = CandidatePoolStore(path)
            store.save(complete)
            store.save(disqualified)

            persisted = store.load()

        self.assertEqual(persisted["candidate_symbols"], [])
        self.assertEqual(persisted["delta"]["removed"], ["SAFEUSDT"])

    def test_candidate_hash_is_order_and_collection_time_independent(self) -> None:
        first = [
            candidate("AAAUSDT", collected_at="2026-08-07T00:00:00+00:00"),
            candidate("BBBUSDT", ratio=0.40, collected_at="2026-08-07T00:00:01+00:00"),
        ]
        second = [
            candidate("BBBUSDT", ratio=0.40, collected_at="2026-08-08T00:00:00+00:00"),
            candidate("AAAUSDT", collected_at="2026-08-08T00:00:01+00:00"),
        ]

        self.assertEqual(candidate_pool_hash(first), candidate_pool_hash(second))
        changed = [candidate("AAAUSDT", funding_rate=-0.001), second[0]]
        self.assertEqual(candidate_pool_hash(first), candidate_pool_hash(changed))
        self.assertNotEqual(
            candidate_snapshot_hash(first),
            candidate_snapshot_hash(changed),
        )
        membership_changed = [
            candidate("AAAUSDT", tags=[HIGH_LEVERAGE_CANDIDATE]),
            second[0],
        ]
        self.assertNotEqual(
            candidate_pool_hash(first),
            candidate_pool_hash(membership_changed),
        )

    def test_candidate_hashes_include_the_effective_rule_parameters(self) -> None:
        rows = [candidate("AAAUSDT")]
        first_parameters = {
            "market_cap_max_usd": 30_000_000.0,
            "short_squeeze_min_ratio": 0.20,
            "short_squeeze_max_funding_rate": 0.0,
            "high_leverage_min_ratio": 0.50,
        }
        changed_parameters = {**first_parameters, "high_leverage_min_ratio": 0.60}

        self.assertNotEqual(
            candidate_pool_hash(rows, rule_parameters=first_parameters),
            candidate_pool_hash(rows, rule_parameters=changed_parameters),
        )
        self.assertNotEqual(
            candidate_snapshot_hash(rows, rule_parameters=first_parameters),
            candidate_snapshot_hash(rows, rule_parameters=changed_parameters),
        )

    def test_pool_delta_tracks_added_retained_and_removed_with_set_deduplication(self) -> None:
        previous = {
            "candidate_symbols": ["AAAUSDT", "BBBUSDT"],
            "candidate_pool_hash": "previous-hash",
        }
        current = [
            candidate("BBBUSDT", tags=[SHORT_SQUEEZE_CANDIDATE]),
            candidate(
                "CCCUSDT",
                ratio=0.50,
                tags=[SHORT_SQUEEZE_CANDIDATE, HIGH_LEVERAGE_CANDIDATE],
            ),
        ]

        document = pool_document(current, previous=previous)

        self.assertEqual(document["delta"], {
            "added": ["CCCUSDT"],
            "retained": ["BBBUSDT"],
            "removed": ["AAAUSDT"],
        })
        self.assertEqual(document["stats"]["short_squeeze_count"], 2)
        self.assertEqual(document["stats"]["high_leverage_count"], 1)
        self.assertEqual(document["stats"]["dual_match_count"], 1)
        self.assertEqual(document["stats"]["merged_candidate_count"], 2)
        self.assertEqual(document["candidate_symbols"], ["BBBUSDT", "CCCUSDT"])

    def test_rule_lists_sort_by_ratio_descending_then_symbol(self) -> None:
        dual_tags = [SHORT_SQUEEZE_CANDIDATE, HIGH_LEVERAGE_CANDIDATE]
        document = pool_document([
            candidate("ZZZUSDT", ratio=0.60, tags=dual_tags),
            candidate("MIDUSDT", ratio=0.80, tags=dual_tags),
            candidate("AAAUSDT", ratio=0.60, tags=dual_tags),
        ])

        self.assertEqual(
            document["short_squeeze_symbols"],
            ["MIDUSDT", "AAAUSDT", "ZZZUSDT"],
        )
        self.assertEqual(
            document["high_leverage_symbols"],
            ["MIDUSDT", "AAAUSDT", "ZZZUSDT"],
        )
        self.assertEqual(
            document["dual_match_symbols"],
            ["MIDUSDT", "AAAUSDT", "ZZZUSDT"],
        )


class CandidatePoolFormatterTests(unittest.TestCase):
    def test_console_summary_is_chinese_and_contains_candidate_metrics(self) -> None:
        document = pool_document([candidate("COTIUSDT", ratio=0.355)])

        text = render_console(document)

        self.assertIn("山寨合约异动雷达｜候选池扫描", text)
        self.assertIn("已加载USDT永续：4", text)
        self.assertIn("潜在逼空 1", text)
        self.assertIn("合并监控 1", text)
        self.assertIn(
            "COTIUSDT｜市值 $20.00M｜OI/市值 35.5%｜费率 -0.0352%",
            text,
        )
        self.assertIn("未进入正式池原因", text)

    def test_telegram_preview_contains_required_fields_and_html_escapes_sources(self) -> None:
        document = pool_document(
            [candidate("A&BUSDT", ratio=0.355)],
            data_sources={
                "market_cap": "CMC <official>&verified",
                "open_interest": "Binance <futures>",
                "funding_and_mark_price": "Binance <premium>",
            },
        )

        pages = render_telegram_preview(document, max_chars=2_000)
        text = "\n".join(pages)

        self.assertEqual(len(pages), 1)
        for label in (
            "山寨合约异动雷达",
            "已加载合约数：4",
            "可信市值映射数：1",
            "未映射数（未进入可信池）：0",
            "合并监控数量：1",
            "数据源：",
            "数据完整度：1/1",
            "潜在逼空（1）",
            "潜在狗庄候选（0）",
            "双重命中（0）",
        ):
            self.assertIn(label, text)
        self.assertIn("A&amp;BUSDT｜市值 $20.00M｜OI/市值 35.5%｜费率 -0.0352%", text)
        self.assertIn("CMC &lt;official&gt;&amp;verified", text)
        self.assertIn("Binance &lt;futures&gt;", text)
        self.assertIn("Binance &lt;premium&gt;", text)
        self.assertIn("不构成对操纵主体的事实认定", text)

    def test_pagination_keeps_every_candidate_row_whole_and_within_limit(self) -> None:
        snapshots = [
            candidate(f"COIN{index:02d}USDT", ratio=0.20 + index / 1_000)
            for index in range(18)
        ]
        document = pool_document(snapshots)

        pages = render_telegram_preview(document, max_chars=420)

        self.assertGreater(len(pages), 1)
        for index, page in enumerate(pages, start=1):
            self.assertLessEqual(len(page), 420)
            self.assertIn(f"第{index}/{len(pages)}页", page)
        all_lines = [line for page in pages for line in page.splitlines()]
        for item in snapshots:
            expected = tg_escape(candidate_line(item.to_dict()))
            self.assertEqual(all_lines.count(expected), 1, expected)

    def test_missing_values_use_explicit_chinese_placeholders(self) -> None:
        line = candidate_line({
            "symbol": "NEWUSDT",
            "market_cap_usd": None,
            "oi_market_cap_ratio": None,
            "funding_rate": None,
        })

        self.assertEqual(
            line,
            "NEWUSDT｜市值 缺失｜OI/市值 缺失｜费率 缺失",
        )

    def test_json_output_never_serializes_nan_or_infinity(self) -> None:
        document = pool_document([
            candidate(
                "BADUSDT",
                ratio=math.nan,
                market_cap_usd=math.nan,
                oi_value_usd=math.inf,
                funding_rate=-math.inf,
            ),
        ])

        rendered = render_json(document)
        parsed = json.loads(rendered)

        self.assertNotIn("NaN", rendered)
        self.assertNotIn("Infinity", rendered)
        row = parsed["snapshots"][0]
        self.assertIsNone(row["market_cap_usd"])
        self.assertIsNone(row["oi_value_usd"])
        self.assertIsNone(row["oi_market_cap_ratio"])
        self.assertIsNone(row["funding_rate"])


if __name__ == "__main__":
    unittest.main()
