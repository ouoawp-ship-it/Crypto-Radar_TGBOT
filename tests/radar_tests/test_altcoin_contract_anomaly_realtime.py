from __future__ import annotations

import json
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from radars.altcoin_contract_anomaly.models import CandidateSnapshot, SCHEMA_VERSION
from radars.altcoin_contract_anomaly.realtime import (
    AltcoinRealtimeController,
    CandidateManifestConsumer,
    CandidateMarkPriceBook,
    CandidateOiSampler,
    ClosedRealtimeFeatureBuilder,
    ValidatedCandidateManifest,
    run_realtime_confirmation_session,
)
from radars.altcoin_contract_anomaly.rules import (
    HIGH_LEVERAGE_CANDIDATE,
    SHORT_SQUEEZE_CANDIDATE,
)
from radars.altcoin_contract_anomaly.state import CandidatePoolStore, build_pool_document
from shared.binance_data import DataQuality, RequestBudget


NOW = 1_800_000_000


def iso(value: int = NOW) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def settings(root: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": root,
        "market_snapshots_db_path": root / "market.db",
        "realtime_features_db_path": root / "realtime.db",
        "altcoin_contract_anomaly_candidate_snapshot_path": root / "pool.json",
        "altcoin_contract_anomaly_realtime_enable": True,
        "altcoin_contract_anomaly_manifest_max_age_sec": 1_200,
        "altcoin_contract_anomaly_cmc_max_data_age_sec": 900,
        "altcoin_contract_anomaly_binance_oi_max_age_sec": 900,
        "altcoin_contract_anomaly_funding_max_age_sec": 900,
        "altcoin_contract_anomaly_realtime_data_max_age_sec": 180,
        "altcoin_contract_anomaly_oi_refresh_sec": 300,
        "altcoin_contract_anomaly_realtime_oi_max_age_sec": 900,
        "altcoin_contract_anomaly_realtime_oi_workers": 2,
        "altcoin_contract_anomaly_realtime_oi_request_budget": 10,
        "altcoin_contract_anomaly_feature_1m_window_sec": 60,
        "altcoin_contract_anomaly_feature_5m_window_sec": 300,
        "altcoin_contract_anomaly_volume_baseline_buckets": 5,
        "altcoin_contract_anomaly_volume_min_samples": 5,
        "altcoin_contract_anomaly_volume_min_coverage": 1.0,
        "altcoin_contract_anomaly_price_1m_move_ratio": 0.01,
        "altcoin_contract_anomaly_price_5m_move_ratio": 0.02,
        "altcoin_contract_anomaly_volume_expansion_ratio": 2.0,
        "altcoin_contract_anomaly_aggressive_buy_ratio": 0.60,
        "altcoin_contract_anomaly_aggressive_sell_ratio": 0.40,
        "altcoin_contract_anomaly_open_interest_move_ratio": 0.03,
        "altcoin_contract_anomaly_funding_positive_rate": 0.0005,
        "altcoin_contract_anomaly_funding_change_ratio": 0.0001,
        "altcoin_contract_anomaly_liquidation_min_usd": 100_000,
        "altcoin_contract_anomaly_price_stall_ratio": 0.003,
        "altcoin_contract_anomaly_weakening_volume_ratio": 1.2,
        "altcoin_contract_anomaly_weakening_windows": 2,
        "altcoin_contract_anomaly_realtime_state_path": root / "p2-state.json",
        "altcoin_contract_anomaly_realtime_event_path": root / "p2-events.jsonl",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def candidate(
    symbol: str = "TESTUSDT",
    *,
    ratio: float = 0.30,
    funding: float = -0.001,
    observed_at: int = NOW,
    funding_stale: bool = False,
) -> CandidateSnapshot:
    if ratio >= 0.50 and funding < 0 and not funding_stale:
        tags = [SHORT_SQUEEZE_CANDIDATE, HIGH_LEVERAGE_CANDIDATE]
    elif ratio >= 0.50:
        tags = [HIGH_LEVERAGE_CANDIDATE]
    elif ratio >= 0.20 and funding < 0:
        tags = [SHORT_SQUEEZE_CANDIDATE]
    else:
        tags = []
    matched = [
        "altcoin_contract_anomaly.p1.v1:short_squeeze"
        if tag == SHORT_SQUEEZE_CANDIDATE
        else "altcoin_contract_anomaly.p1.v1:high_leverage"
        for tag in tags
    ]
    cap = 20_000_000.0
    oi = cap * ratio
    timestamp = iso(observed_at)
    return CandidateSnapshot(
        schema_version=SCHEMA_VERSION,
        symbol=symbol,
        base_asset=symbol.removesuffix("USDT"),
        normalized_asset=symbol.removesuffix("USDT"),
        contract_multiplier=1,
        exchange="BINANCE",
        contract_type="USDT_PERPETUAL",
        cmc_id=123,
        mapping_method="existing_verified_anchor",
        mapping_confidence="high",
        market_cap_usd=cap,
        market_cap_source="coinmarketcap_v3_quotes_latest:network",
        market_cap_updated_at=timestamp,
        open_interest_raw=oi,
        open_interest_unit="usd_notional",
        oi_value_usd=oi,
        mark_price=1.0,
        funding_rate=funding,
        oi_market_cap_ratio=ratio,
        candidate_tags=tags,
        matched_rules=matched,
        data_quality="stale" if funding_stale else "complete",
        missing_fields=[],
        collected_at=timestamp,
        open_interest_updated_at=timestamp,
        mark_price_updated_at=timestamp,
        funding_rate_updated_at=timestamp,
        stale_fields=["funding_rate"] if funding_stale else [],
        invalid_fields=[],
        mapping_evidence=["binance_cmc_unique_id"],
        mapping_rejection_reason=None,
        oi_value_method="binance_sum_open_interest_value",
        binance_oi_usd=oi,
        binance_oi_market_cap_ratio=ratio,
        binance_oi_source="binance_open_interest_hist.sumOpenInterestValue",
        global_oi_usd=None,
        global_oi_market_cap_ratio=None,
        global_oi_source=None,
    )


def pool(rows: list[CandidateSnapshot], *, generated_at: int = NOW, previous=None):
    return build_pool_document(
        rows,
        generated_at=iso(generated_at),
        universe={
            "loaded_usdt_perpetuals": len(rows),
            "eligible_altcoin_contracts": len(rows),
            "excluded_contracts": 0,
        },
        mapping_stats={
            "trusted_count": len(rows),
            "diagnostic_count": 0,
            "conflict_count": 0,
            "unmapped_count": 0,
            "reason_counts": {},
        },
        rule_parameters={
            "market_cap_max_usd": 30_000_000.0,
            "short_squeeze_min_ratio": 0.20,
            "short_squeeze_max_funding_rate": 0.0,
            "high_leverage_min_ratio": 0.50,
        },
        mapping_records=[{
            "binance_symbol": row.symbol,
            "cmc_id": row.cmc_id,
            "mapping_method": row.mapping_method,
            "mapping_confidence": row.mapping_confidence,
        } for row in rows],
        previous=previous,
        data_sources={},
        diagnostics={},
    )


class ManifestConsumerTests(unittest.TestCase):
    def test_valid_manifest_reuses_p1_validation_and_exposes_only_formal_candidates(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = settings(root)
            CandidatePoolStore(root / "pool.json").save(pool([candidate()]))
            consumer = CandidateManifestConsumer(configured)

            result = consumer.poll(now_ts=NOW)

        self.assertEqual(result["status"], "valid_changed")
        self.assertEqual(consumer.last_valid.symbols, ("TESTUSDT",))
        self.assertTrue(consumer.event_ready)

    def test_bad_hash_keeps_last_valid_manifest_and_never_becomes_event_ready(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = settings(root)
            store = CandidatePoolStore(root / "pool.json")
            store.save(pool([candidate()]))
            consumer = CandidateManifestConsumer(configured)
            self.assertEqual(consumer.poll(now_ts=NOW)["status"], "valid_changed")
            damaged = json.loads((root / "pool.json").read_text(encoding="utf-8"))
            damaged["candidate_pool_hash"] = "0" * 64
            (root / "pool.json").write_text(json.dumps(damaged), encoding="utf-8")

            degraded = consumer.poll(now_ts=NOW + 1)

        self.assertEqual(degraded["status"], "manifest_degraded")
        self.assertEqual(degraded["retained_candidate_count"], 1)
        self.assertEqual(consumer.last_valid.symbols, ("TESTUSDT",))
        self.assertFalse(consumer.event_ready)

    def test_high_leverage_candidate_with_stale_funding_is_rejected_by_p2_complete_gate(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = settings(root)
            row = candidate(ratio=0.60, funding=0.0, funding_stale=True)
            CandidatePoolStore(root / "pool.json").save(pool([row]))

            result = CandidateManifestConsumer(configured).poll(now_ts=NOW)

        self.assertEqual(result["status"], "manifest_degraded")
        self.assertIn("untrusted", result["reason"])

    def test_p2_manifest_age_is_independent_from_shorter_p1_cache_age(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = settings(
                root,
                altcoin_contract_anomaly_binance_oi_max_age_sec=600,
                altcoin_contract_anomaly_manifest_max_age_sec=1_200,
            )
            CandidatePoolStore(root / "pool.json").save(pool([candidate()]))
            consumer = CandidateManifestConsumer(configured)

            still_valid = consumer.poll(now_ts=NOW + 700)
            too_old = consumer.poll(now_ts=NOW + 1_201)

        self.assertEqual(still_valid["status"], "valid_changed")
        self.assertEqual(too_old["status"], "manifest_degraded")
        self.assertIn("stale", too_old["reason"])
        self.assertEqual(too_old["retained_candidate_count"], 1)

    def test_candidate_fields_must_be_fresh_when_manifest_is_generated(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = settings(root)
            fresh_at_generation = candidate(observed_at=NOW - 500)
            CandidatePoolStore(root / "pool.json").save(
                pool([fresh_at_generation], generated_at=NOW)
            )
            consumer = CandidateManifestConsumer(configured)

            valid_later = consumer.poll(now_ts=NOW + 1_000)

            stale_at_generation = candidate(observed_at=NOW - 901)
            CandidatePoolStore(root / "pool.json").save(
                pool([stale_at_generation], generated_at=NOW)
            )
            stale = CandidateManifestConsumer(configured).poll(now_ts=NOW)

        self.assertEqual(valid_later["status"], "valid_changed")
        self.assertEqual(stale["status"], "manifest_degraded")
        self.assertIn("field_stale", stale["reason"])


class FakeFeatureStore:
    def __init__(self, rows):
        self.rows = list(rows)

    def recent_rows(self, **_kwargs):
        return list(self.rows)


def minute_row(start: int, *, volume: float = 100.0, price_open=100.0, price_close=100.0):
    buy = volume * 0.6
    sell = volume - buy
    return {
        "exchange": "binance",
        "market": "futures",
        "symbol": "TESTUSDT",
        "bucket_start": start,
        "bucket_sec": 60,
        "trade_buy_usd": buy,
        "trade_sell_usd": sell,
        "cvd_usd": buy - sell,
        "trade_count": 2,
        "price_open": price_open,
        "price_high": max(price_open, price_close),
        "price_low": min(price_open, price_close),
        "price_close": price_close,
        "long_liquidation_usd": 10.0,
        "short_liquidation_usd": 20.0,
    }


class ClosedFeatureTests(unittest.TestCase):
    def test_builds_one_and_five_minute_features_only_from_contiguous_closed_rows(self) -> None:
        rows = [minute_row(start) for start in range(600, 901, 60)]
        rows[-1] = minute_row(900, volume=300, price_open=100, price_close=102)
        with TemporaryDirectory() as tmp:
            builder = ClosedRealtimeFeatureBuilder(settings(Path(tmp)), FakeFeatureStore(rows))
            result = builder.build_many(["TESTUSDT"], now_ts=1_000)["TESTUSDT"]

        self.assertEqual(result["data_quality"], "complete")
        self.assertAlmostEqual(result["price_change_1m"], 0.02)
        self.assertAlmostEqual(result["price_change_5m"], 0.02)
        self.assertEqual(result["quote_volume_1m_usd"], 300)
        self.assertEqual(result["quote_volume_5m_usd"], 700)
        self.assertEqual(result["volume_anomaly_multiple"], 3)
        self.assertEqual(result["window_coverage_5m"], 1)
        self.assertEqual(builder.last_stats["closed_1m_ready"], 1)
        self.assertEqual(builder.last_stats["closed_5m_ready"], 1)
        self.assertEqual(builder.last_stats["volume_baseline_ready"], 1)
        self.assertEqual(builder.last_stats["complete_coverage_ratio"], 1.0)

    def test_missing_minute_is_not_filled_with_zero(self) -> None:
        rows = [minute_row(start) for start in range(600, 901, 60) if start != 780]
        with TemporaryDirectory() as tmp:
            result = ClosedRealtimeFeatureBuilder(
                settings(Path(tmp)), FakeFeatureStore(rows)
            ).build_many(["TESTUSDT"], now_ts=1_000)["TESTUSDT"]

        self.assertEqual(result["data_quality"], "insufficient_history")
        self.assertIn("closed_5m", result["missing_fields"])
        self.assertIn("volume_baseline", result["missing_fields"])

    def test_unclosed_and_liquidation_only_rows_cannot_supply_price_features(self) -> None:
        rows = [minute_row(start) for start in range(600, 901, 60)]
        rows[-1] = {**minute_row(900), "trade_count": 0, "price_open": 0, "price_close": 0}
        rows.append(minute_row(960, price_open=100, price_close=120))
        with TemporaryDirectory() as tmp:
            result = ClosedRealtimeFeatureBuilder(
                settings(Path(tmp)), FakeFeatureStore(rows)
            ).build_many(["TESTUSDT"], now_ts=1_000)["TESTUSDT"]

        self.assertEqual(result["window_end"], iso(960))
        self.assertIn("closed_1m", result["missing_fields"])

    def test_missing_bucket_metric_is_not_silently_filled_with_zero(self) -> None:
        rows = [minute_row(start) for start in range(600, 901, 60)]
        rows[-2].pop("cvd_usd")
        with TemporaryDirectory() as tmp:
            builder = ClosedRealtimeFeatureBuilder(settings(Path(tmp)), FakeFeatureStore(rows))
            result = builder.build_many(["TESTUSDT"], now_ts=1_000)["TESTUSDT"]

        self.assertEqual(result["data_quality"], "insufficient_history")
        self.assertIn("closed_5m", result["missing_fields"])
        self.assertEqual(builder.last_stats["closed_5m_coverage_ratio"], 0.0)


class FakeMarketStore:
    def __init__(self, points=None):
        self.points = points or []
        self.symbols: list[str] = []

    def symbol_series(self, symbol, **_kwargs):
        self.symbols.append(symbol)
        return list(self.points)


class FakeOiSource:
    def __init__(self, rows_by_symbol):
        self.rows_by_symbol = rows_by_symbol
        self.calls: list[str] = []
        self.budget = RequestBudget({"open_interest_hist": 10})
        self.quality = DataQuality()
        self.closed = False

    def open_interest_hist(self, symbol, period="5m", limit=2):
        self.calls.append(symbol)
        self.budget.consume("open_interest_hist")
        return list(self.rows_by_symbol.get(symbol, []))

    def close(self):
        self.closed = True


class OiSamplerTests(unittest.TestCase):
    def test_only_candidates_are_fetched_and_exact_adjacent_rest_points_form_delta(self) -> None:
        boundary = NOW // 300 * 300
        source = FakeOiSource({
            "TESTUSDT": [
                {"sumOpenInterestValue": "100", "timestamp": (boundary - 300) * 1000},
                {"sumOpenInterestValue": "110", "timestamp": boundary * 1000},
            ],
        })
        market = FakeMarketStore([{
            "observed_at": NOW - 30,
            "oi_usd": 109,
            "sources": ["binance_futures_batch"],
        }])
        with TemporaryDirectory() as tmp:
            sampler = CandidateOiSampler(
                settings(Path(tmp)),
                market_store=market,
                source_factory=lambda *_args: source,
            )
            values = sampler.refresh(["TESTUSDT"], now_ts=NOW)

        self.assertEqual(source.calls, ["TESTUSDT"])
        self.assertEqual(market.symbols, ["TESTUSDT"])
        self.assertAlmostEqual(values["TESTUSDT"]["oi_change_5m"], 0.10)
        self.assertEqual(values["TESTUSDT"]["data_quality"], "complete")
        self.assertEqual(sampler.last_stats["requests"], 1)
        self.assertEqual(sampler.last_stats["cache_hits"], 1)
        self.assertTrue(source.closed)

    def test_same_five_minute_bucket_never_creates_a_change(self) -> None:
        boundary = NOW // 300 * 300
        source = FakeOiSource({
            "TESTUSDT": [
                {"sumOpenInterestValue": "100", "timestamp": boundary * 1000},
                {"sumOpenInterestValue": "110", "timestamp": (boundary + 10) * 1000},
            ],
        })
        with TemporaryDirectory() as tmp:
            sampler = CandidateOiSampler(
                settings(Path(tmp)),
                market_store=FakeMarketStore(),
                source_factory=lambda *_args: source,
            )
            value = sampler.refresh(["TESTUSDT"], now_ts=NOW)["TESTUSDT"]

        self.assertEqual(value["data_quality"], "partial")
        self.assertIsNone(value["oi_change_5m"])

    def test_oi_delta_uses_fresh_current_point_and_one_adjacent_baseline(self) -> None:
        boundary = NOW // 300 * 300
        source = FakeOiSource({
            "TESTUSDT": [
                {"sumOpenInterestValue": "100", "timestamp": (boundary - 300) * 1000},
                {"sumOpenInterestValue": "110", "timestamp": boundary * 1000},
            ],
        })
        with TemporaryDirectory() as tmp:
            sampler = CandidateOiSampler(
                settings(
                    Path(tmp),
                    altcoin_contract_anomaly_realtime_oi_max_age_sec=600,
                ),
                market_store=FakeMarketStore(),
                source_factory=lambda *_args: source,
            )
            fresh = sampler.refresh(["TESTUSDT"], now_ts=boundary + 600)["TESTUSDT"]
            stale = sampler._value(
                "TESTUSDT",
                now_ts=boundary + 601,
                max_age=600,
            )

        self.assertEqual(fresh["data_quality"], "complete")
        self.assertAlmostEqual(fresh["oi_change_5m"], 0.10)
        self.assertEqual(fresh["data_age_sec"], 600.0)
        self.assertEqual(stale["data_quality"], "partial")

    def test_market_cache_rejects_non_binance_and_mixed_oi_sources(self) -> None:
        boundary = NOW // 300 * 300
        source = FakeOiSource({
            "TESTUSDT": [
                {"sumOpenInterestValue": "100", "timestamp": (boundary - 300) * 1000},
                {"sumOpenInterestValue": "110", "timestamp": boundary * 1000},
            ],
        })
        market = FakeMarketStore([
            {
                "observed_at": NOW - 10,
                "oi_usd": 999,
                "sources": ["coinglass_derivatives"],
            },
            {
                "observed_at": NOW - 5,
                "oi_usd": 888,
                "sources": ["binance_futures_batch", "coinglass_derivatives"],
            },
        ])
        with TemporaryDirectory() as tmp:
            sampler = CandidateOiSampler(
                settings(Path(tmp)),
                market_store=market,
                source_factory=lambda *_args: source,
            )
            value = sampler.refresh(["TESTUSDT"], now_ts=NOW)["TESTUSDT"]

        self.assertEqual(sampler.last_stats["cache_hits"], 0)
        self.assertEqual(value["source"], "binance_open_interest_hist.sumOpenInterestValue")
        self.assertEqual(value["oi_value_usd"], 110.0)
        self.assertAlmostEqual(value["oi_change_5m"], 0.10)

    def test_restored_oi_samples_reject_foreign_mixed_and_mislabeled_sources(self) -> None:
        boundary = NOW // 300 * 300
        source = FakeOiSource({
            "TESTUSDT": [
                {"sumOpenInterestValue": "100", "timestamp": (boundary - 300) * 1000},
                {"sumOpenInterestValue": "110", "timestamp": boundary * 1000},
            ],
        })
        restored = {
            "TESTUSDT": [
                {
                    "observed_at": NOW - 1,
                    "oi_value_usd": 999,
                    "source": "coinglass_derivatives",
                    "exact_5m": False,
                },
                {
                    "observed_at": NOW - 2,
                    "oi_value_usd": 888,
                    "source": "binance_futures_batch+coinglass_derivatives",
                    "exact_5m": False,
                },
                {
                    "observed_at": boundary - 600,
                    "oi_value_usd": 777,
                    "source": "binance_futures_batch",
                    "exact_5m": True,
                },
            ],
        }
        with TemporaryDirectory() as tmp:
            sampler = CandidateOiSampler(
                settings(Path(tmp)),
                market_store=FakeMarketStore(),
                samples=restored,
                source_factory=lambda *_args: source,
            )
            value = sampler.refresh(["TESTUSDT"], now_ts=NOW)["TESTUSDT"]

        self.assertEqual(value["source"], "binance_open_interest_hist.sumOpenInterestValue")
        self.assertEqual(value["oi_value_usd"], 110.0)
        self.assertAlmostEqual(value["oi_change_5m"], 0.10)

    def test_restored_exact_oi_samples_must_align_to_closed_five_minute_buckets(self) -> None:
        boundary = NOW // 300 * 300
        source = FakeOiSource({
            "TESTUSDT": [
                {"sumOpenInterestValue": "100", "timestamp": (boundary - 300) * 1000},
                {"sumOpenInterestValue": "110", "timestamp": boundary * 1000},
            ],
        })
        restored = {
            "TESTUSDT": [
                {
                    "observed_at": boundary - 299,
                    "oi_value_usd": 100,
                    "source": "binance_open_interest_hist.sumOpenInterestValue",
                    "exact_5m": True,
                },
                {
                    "observed_at": boundary + 1,
                    "oi_value_usd": 150,
                    "source": "binance_open_interest_hist.sumOpenInterestValue",
                    "exact_5m": True,
                },
            ],
        }
        with TemporaryDirectory() as tmp:
            sampler = CandidateOiSampler(
                settings(Path(tmp)),
                market_store=FakeMarketStore(),
                samples=restored,
                source_factory=lambda *_args: source,
            )
            value = sampler.refresh(["TESTUSDT"], now_ts=boundary + 100)["TESTUSDT"]

        self.assertEqual(source.calls, ["TESTUSDT"])
        self.assertEqual(value["oi_value_usd"], 110.0)
        self.assertAlmostEqual(value["oi_change_5m"], 0.10)

    def test_restored_oi_samples_reject_coerced_booleans_and_strings(self) -> None:
        valid_source = "binance_open_interest_hist.sumOpenInterestValue"
        samples = {
            "TESTUSDT": [
                {
                    "observed_at": str(NOW),
                    "oi_value_usd": 100.0,
                    "source": valid_source,
                    "exact_5m": True,
                },
                {
                    "observed_at": NOW,
                    "oi_value_usd": "100",
                    "source": valid_source,
                    "exact_5m": True,
                },
                {
                    "observed_at": NOW,
                    "oi_value_usd": True,
                    "source": valid_source,
                    "exact_5m": True,
                },
                {
                    "observed_at": True,
                    "oi_value_usd": 100.0,
                    "source": valid_source,
                    "exact_5m": True,
                },
            ],
        }
        with TemporaryDirectory() as tmp:
            sampler = CandidateOiSampler(
                settings(Path(tmp)),
                market_store=FakeMarketStore(),
                samples=samples,
            )

        self.assertEqual(sampler.samples, {})

    def test_request_budget_reports_unattempted_candidates_as_failures(self) -> None:
        boundary = NOW // 300 * 300
        source = FakeOiSource({
            "AAAUSDT": [
                {"sumOpenInterestValue": "100", "timestamp": (boundary - 300) * 1000},
                {"sumOpenInterestValue": "110", "timestamp": boundary * 1000},
            ],
        })
        with TemporaryDirectory() as tmp:
            sampler = CandidateOiSampler(
                settings(
                    Path(tmp),
                    altcoin_contract_anomaly_realtime_oi_request_budget=1,
                ),
                market_store=FakeMarketStore(),
                source_factory=lambda *_args: source,
            )
            values = sampler.refresh(["BBBUSDT", "AAAUSDT"], now_ts=NOW)

        self.assertEqual(source.calls, ["AAAUSDT"])
        self.assertEqual(values["AAAUSDT"]["data_quality"], "complete")
        self.assertEqual(values["BBBUSDT"]["data_quality"], "partial")
        self.assertEqual(sampler.last_stats["budget_exhausted"], 1)
        self.assertEqual(sampler.last_stats["failures"], 1)


class MarkPriceBookTests(unittest.TestCase):
    def test_rejects_duplicate_and_old_updates_and_uses_distinct_upstream_times(self) -> None:
        book = CandidateMarkPriceBook()
        first = {"e": "markPriceUpdate", "s": "TESTUSDT", "p": "1", "r": "-0.001", "E": 1_000, "T": 2_000}
        second = {"e": "markPriceUpdate", "s": "TESTUSDT", "p": "1.1", "r": "-0.0012", "E": 2_000, "T": 3_000}

        self.assertTrue(book.apply(first))
        self.assertFalse(book.apply(first))
        self.assertTrue(book.apply(second))
        self.assertFalse(book.apply({**first, "E": 1_500}))
        row = book.snapshot("TESTUSDT")

        self.assertAlmostEqual(row["funding_rate_change"], -0.0002)
        self.assertEqual(row["previous_event_time_ms"], 1_000)

    def test_controller_accepts_normalized_snapshot_from_shared_mark_book(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            CandidatePoolStore(root / "pool.json").save(pool([candidate()]))
            controller = AltcoinRealtimeController(
                settings(root),
                feature_store=FakeFeatureStore([]),
                market_store=FakeMarketStore(),
            )
            controller.poll_manifest(now_ts=NOW)
            first = {
                "symbol": "TESTUSDT",
                "mark_price": 1.0,
                "funding_rate": -0.001,
                "next_funding_time_ms": (NOW + 3_600) * 1_000,
                "event_time_ms": NOW * 1_000,
                "exchange": "binance",
                "market": "futures",
                "source": "binance_ws_mark_price",
            }
            second = {
                **first,
                "funding_rate": -0.0012,
                "event_time_ms": (NOW + 1) * 1_000,
            }

            self.assertTrue(controller.handle_mark_price(first))
            self.assertTrue(controller.handle_mark_price(second))
            self.assertFalse(controller.handle_mark_price(first))
            row = controller.mark_price_book.snapshot("TESTUSDT")

        self.assertAlmostEqual(row["funding_rate_change"], -0.0002)
        self.assertEqual(row["funding_previous_event_time_ms"], NOW * 1_000)


class StaticFeatureBuilder:
    def __init__(self, row):
        self.row = row

    def build_many(self, symbols, *, now_ts):
        return {symbol: dict(self.row) for symbol in symbols}


class StaticOiSampler:
    def __init__(self, change=0.05):
        self.change = change
        self.samples = {}
        self.last_stats = {"requests": 0, "cache_hits": 1}

    def refresh(self, symbols, *, now_ts):
        return {symbol: {
            "symbol": symbol,
            "oi_value_usd": 1_000_000,
            "oi_change_5m": self.change,
            "updated_at": iso(int(now_ts)),
            "change_start_at": iso(int(now_ts) - 300),
            "change_end_at": iso(int(now_ts)),
            "data_quality": "complete",
            "missing_fields": [],
        } for symbol in symbols}


class StaticMarkBook:
    def __init__(self, *, now=NOW, funding=-0.001, change=-0.0002):
        self.row = {
            "symbol": "TESTUSDT",
            "mark_price": 1.0,
            "funding_rate": funding,
            "funding_rate_change": change,
            "event_time_ms": now * 1000,
            "funding_previous_event_time_ms": (now - 1) * 1000,
        }

    def snapshot(self, _symbol):
        return dict(self.row)


def feature(**overrides):
    values = {
        "symbol": "TESTUSDT",
        "window_start": iso(NOW - 60),
        "window_end": iso(NOW),
        "price_change_1m": 0.0,
        "price_change_5m": 0.0,
        "quote_volume_1m_usd": 300,
        "quote_volume_5m_usd": 700,
        "volume_anomaly_multiple": 3.0,
        "aggressive_buy_ratio_5m": 0.30,
        "aggressive_sell_ratio_5m": 0.70,
        "cvd_5m_usd": -100,
        "long_liquidation_5m_usd": 0,
        "short_liquidation_5m_usd": 0,
        "data_quality": "complete",
        "missing_fields": [],
        "stale_fields": [],
        "source_timestamps": {"closed_1m_end": iso(NOW)},
    }
    values.update(overrides)
    return values


def subscription(now=NOW):
    return {
        "connected": True,
        "last_receive_ms": now * 1000,
        "active_candidate_symbols": ["TESTUSDT"],
        "candidate_coverage_complete": True,
        "force_order_active": True,
        "subscription_generation": 3,
    }


def controller_for(root: Path, row: CandidateSnapshot, feature_row, mark=None, oi_change=0.05):
    configured = settings(root)
    CandidatePoolStore(root / "pool.json").save(pool([row]))
    controller = AltcoinRealtimeController(
        configured,
        feature_store=FakeFeatureStore([]),
        market_store=FakeMarketStore(),
        mark_price_book=mark or StaticMarkBook(),
    )
    self_result = controller.poll_manifest(now_ts=NOW)
    if self_result["status"] != "valid_changed":
        raise AssertionError(self_result)
    controller.feature_builder = StaticFeatureBuilder(feature_row)
    controller.oi_sampler = StaticOiSampler(oi_change)
    return controller


class DryRunEventTests(unittest.TestCase):
    def test_mark_price_allows_small_upstream_clock_skew_but_rejects_future_data(self) -> None:
        with TemporaryDirectory() as tmp:
            tolerated = controller_for(
                Path(tmp),
                candidate(),
                feature(),
                mark=StaticMarkBook(now=NOW + 3),
            )
            events = tolerated.evaluate(subscription(), now_ts=NOW)

        self.assertTrue(events)
        self.assertEqual(tolerated.stats()["last_evaluation_complete_count"], 1)

        with TemporaryDirectory() as tmp:
            rejected = controller_for(
                Path(tmp),
                candidate(),
                feature(),
                mark=StaticMarkBook(now=NOW + 6),
            )
            events = rejected.evaluate(subscription(), now_ts=NOW)

        self.assertEqual(events, [])
        self.assertEqual(rejected.stats()["last_evaluation_complete_count"], 0)
        self.assertEqual(
            rejected.stats()["data_quality_skip_reasons"]["mark_stale"],
            1,
        )

    def test_short_fuel_and_squeeze_rules_count_independent_families_once(self) -> None:
        with TemporaryDirectory() as tmp:
            fuel = controller_for(Path(tmp), candidate(), feature())
            fuel_events = fuel.evaluate(subscription(), now_ts=NOW)
        self.assertEqual([event["event_type"] for event in fuel_events], ["short_fuel_building"])
        self.assertEqual(len(fuel_events[0]["confirmed_factor_families"]), len(set(fuel_events[0]["confirmed_factor_families"])))

        squeeze_feature = feature(
            price_change_1m=0.02,
            price_change_5m=0.03,
            aggressive_buy_ratio_5m=0.70,
            aggressive_sell_ratio_5m=0.30,
            cvd_5m_usd=200,
            short_liquidation_5m_usd=200_000,
        )
        with TemporaryDirectory() as tmp:
            squeeze = controller_for(
                Path(tmp), candidate(), squeeze_feature, oi_change=-0.05
            )
            events = squeeze.evaluate(subscription(), now_ts=NOW)
            duplicate = squeeze.evaluate(subscription(), now_ts=NOW)
        self.assertIn("short_squeeze_ignition", [event["event_type"] for event in events])
        event = next(event for event in events if event["event_type"] == "short_squeeze_ignition")
        self.assertEqual(event["confirmed_factor_families"].count("price_momentum"), 1)
        self.assertTrue(event["dry_run"])
        self.assertNotIn("score", event)
        self.assertEqual(duplicate, [])

        with TemporaryDirectory() as tmp:
            no_longer_crowded = controller_for(
                Path(tmp),
                candidate(),
                squeeze_feature,
                mark=StaticMarkBook(funding=0.0001, change=0.0002),
                oi_change=-0.05,
            )
            invalid_basis_events = no_longer_crowded.evaluate(
                subscription(),
                now_ts=NOW,
            )
        self.assertNotIn(
            "short_squeeze_ignition",
            [event["event_type"] for event in invalid_basis_events],
        )

    def test_high_leverage_and_long_crowding_events_are_structured(self) -> None:
        high_row = candidate(ratio=0.60, funding=0.0)
        down_feature = feature(
            price_change_1m=-0.02,
            price_change_5m=-0.03,
            long_liquidation_5m_usd=200_000,
        )
        with TemporaryDirectory() as tmp:
            high = controller_for(Path(tmp), high_row, down_feature)
            high_events = high.evaluate(subscription(), now_ts=NOW)
        anomaly = next(event for event in high_events if event["event_type"] == "high_leverage_anomaly")
        self.assertEqual(anomaly["direction"], "mixed")
        self.assertGreaterEqual(len(anomaly["confirmed_factor_families"]), 2)

        with TemporaryDirectory() as tmp:
            crowded = controller_for(
                Path(tmp),
                high_row,
                down_feature,
                mark=StaticMarkBook(funding=0.001, change=0.0002),
            )
            crowd_events = crowded.evaluate(subscription(), now_ts=NOW)
        crowd = next(event for event in crowd_events if event["event_type"] == "long_crowding_risk")
        self.assertEqual(crowd["direction"], "down")
        self.assertGreaterEqual(len(crowd["confirmed_factor_families"]), 3)

    def test_weakening_requires_prior_confirmation_two_closed_windows_and_is_restart_idempotent(self) -> None:
        ignition = feature(
            price_change_1m=0.02,
            price_change_5m=0.03,
            aggressive_buy_ratio_5m=0.70,
            aggressive_sell_ratio_5m=0.30,
            cvd_5m_usd=200,
        )
        neutral = feature(
            volume_anomaly_multiple=1.0,
            aggressive_buy_ratio_5m=0.50,
            aggressive_sell_ratio_5m=0.50,
            cvd_5m_usd=0,
        )
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            controller = controller_for(root, candidate(), ignition, oi_change=-0.05)
            controller.evaluate(subscription(), now_ts=NOW)
            controller.feature_builder.row = {**neutral, "window_start": iso(NOW), "window_end": iso(NOW + 60)}
            controller.oi_sampler.change = 0.0
            controller.mark_price_book.row["event_time_ms"] = (NOW + 60) * 1000
            controller.mark_price_book.row["funding_rate_change"] = 0.0
            first = controller.evaluate(subscription(NOW + 60), now_ts=NOW + 60)
            same_window = controller.evaluate(subscription(NOW + 60), now_ts=NOW + 60)
            controller.feature_builder.row = {**neutral, "window_start": iso(NOW + 60), "window_end": iso(NOW + 120)}
            controller.mark_price_book.row["event_time_ms"] = (NOW + 120) * 1000
            second = controller.evaluate(subscription(NOW + 120), now_ts=NOW + 120)

        self.assertEqual(first, [])
        self.assertEqual(same_window, [])
        self.assertEqual([event["event_type"] for event in second], ["anomaly_weakening"])

    def test_only_new_valid_manifest_can_emit_candidate_invalidation(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            configured = settings(root)
            store = CandidatePoolStore(root / "pool.json")
            first = pool([candidate()])
            store.save(first)
            controller = AltcoinRealtimeController(
                configured,
                feature_store=FakeFeatureStore([]),
                market_store=FakeMarketStore(),
            )
            controller.poll_manifest(now_ts=NOW)
            damaged = json.loads((root / "pool.json").read_text(encoding="utf-8"))
            damaged["candidate_snapshot_hash"] = "f" * 64
            (root / "pool.json").write_text(json.dumps(damaged), encoding="utf-8")
            bad = controller.poll_manifest(now_ts=NOW + 1)
            self.assertEqual(bad["events" if "events" in bad else "status"], "manifest_degraded")
            self.assertEqual(controller.recent_events, [])

            # Restore the previous valid file, then persist a complete rules-based removal.
            (root / "pool.json").write_text(json.dumps(first), encoding="utf-8")
            store.save(pool([candidate(ratio=0.10, observed_at=NOW + 60)], generated_at=NOW + 60, previous=first))
            valid = controller.poll_manifest(now_ts=NOW + 60)

        self.assertEqual([event["event_type"] for event in valid["events"]], ["candidate_condition_invalidated"])
        self.assertEqual(valid["events"][0]["confirmed_factor_families"], [])

    def test_stale_websocket_blocks_all_confirmation_events(self) -> None:
        with TemporaryDirectory() as tmp:
            controller = controller_for(Path(tmp), candidate(), feature())
            events = controller.evaluate(subscription(NOW - 1_000), now_ts=NOW)

        self.assertEqual(events, [])
        stats = controller.stats()
        self.assertGreater(stats["data_quality_skips"], 0)
        self.assertEqual(stats["data_quality_skip_reasons"]["websocket_stale"], 1)
        self.assertEqual(stats["manifest_age_sec"], 0.0)

    def test_base_capacity_trim_does_not_block_complete_candidate_coverage(self) -> None:
        with TemporaryDirectory() as tmp:
            controller = controller_for(Path(tmp), candidate(), feature())
            base_trimmed = {**subscription(), "capacity_degraded": True}
            events = controller.evaluate(base_trimmed, now_ts=NOW)

        self.assertEqual([event["event_type"] for event in events], ["short_fuel_building"])

        with TemporaryDirectory() as tmp:
            controller = controller_for(Path(tmp), candidate(), feature())
            candidate_trimmed = {
                **subscription(),
                "capacity_degraded": True,
                "candidate_capacity_degraded": True,
            }
            events = controller.evaluate(candidate_trimmed, now_ts=NOW)

        self.assertEqual(events, [])
        self.assertGreater(controller.stats()["data_quality_skips"], 0)

    def test_bounded_session_wrapper_preserves_data_and_exit_status(self) -> None:
        service_result = {
            "started_at": iso(),
            "ended_at": iso(NOW + 30),
            "duration_sec_requested": 30.0,
            "duration_sec_actual": 30.0,
            "interrupted": False,
            "failures": [],
            "events": [],
            "stats": {
                "manifest_event_ready": True,
                "manifest_hash": "pool-hash",
                "manifest_snapshot_hash": "snapshot-hash",
                "candidate_count": 1,
                "accepted_events": 20,
                "candidate_coverage_complete": True,
                "mark_price_data_coverage_ratio": 1.0,
                "feature_coverage": {
                    "candidate_count": 1,
                    "complete_coverage_ratio": 1.0,
                },
                "last_evaluation_candidate_count": 1,
                "last_evaluation_complete_count": 1,
                "last_evaluation_complete_ratio": 1.0,
                "force_order_subscription_count": 1,
                "event_counts": {"short_squeeze_ignition": 0},
            },
        }
        manifest = ValidatedCandidateManifest(
            generated_at=iso(),
            candidate_pool_hash="pool-hash",
            candidate_snapshot_hash="snapshot-hash",
            rules_fingerprint="rules-hash",
            rules_version="p1",
            candidates={"TESTUSDT": {}},
        )

        def run(service_payload):
            with (
                patch(
                    "radars.altcoin_contract_anomaly.realtime.CandidateManifestConsumer"
                ) as consumer_type,
                patch(
                    "radars.altcoin_contract_anomaly.realtime.time.time",
                    return_value=NOW,
                ),
                patch(
                    "radars.altcoin_contract_anomaly.realtime.run_realtime_market_session",
                    return_value=service_payload,
                ),
            ):
                consumer_type.return_value.poll.return_value = {
                    "status": "valid_changed"
                }
                consumer_type.return_value.last_valid = manifest
                return run_realtime_confirmation_session(Settings(), duration_sec=30)

        result = run(service_result)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["candidate_pool_hash"], "pool-hash")
        self.assertEqual(result["subscriptions"]["force_order_subscription_count"], 1)
        self.assertFalse(result["telegram"]["enabled"])

        unavailable = {
            **service_result,
            "stats": {**service_result["stats"], "manifest_event_ready": False},
        }
        result = run(unavailable)
        self.assertEqual(result["status"], "data_unavailable")
        self.assertEqual(result["exit_code"], 3)

        incomplete = {
            **service_result,
            "stats": {
                **service_result["stats"],
                "feature_coverage": {"complete_coverage_ratio": 0.0},
                "last_evaluation_complete_count": 0,
                "last_evaluation_complete_ratio": 0.0,
            },
        }
        result = run(incomplete)
        self.assertEqual(result["status"], "data_unavailable")
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("candidate_closed_features_incomplete", result["failures"])

        evaluation_failed = {
            **service_result,
            "stats": {**service_result["stats"], "evaluation_errors": 1},
        }
        result = run(evaluation_failed)
        self.assertEqual(result["status"], "internal_error")
        self.assertEqual(result["exit_code"], 1)
        self.assertIn("realtime_evaluation_internal_error", result["failures"])

        interrupted = {**service_result, "interrupted": True}
        result = run(interrupted)
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(result["exit_code"], 130)

    def test_bounded_session_fails_before_opening_websocket_when_manifest_lifetime_is_short(self) -> None:
        manifest = ValidatedCandidateManifest(
            generated_at=iso(NOW - 1_190),
            candidate_pool_hash="pool-hash",
            candidate_snapshot_hash="snapshot-hash",
            rules_fingerprint="rules-hash",
            rules_version="p1",
            candidates={"TESTUSDT": {}},
        )
        with (
            patch(
                "radars.altcoin_contract_anomaly.realtime.CandidateManifestConsumer"
            ) as consumer_type,
            patch(
                "radars.altcoin_contract_anomaly.realtime.time.time",
                return_value=NOW,
            ),
            patch(
                "radars.altcoin_contract_anomaly.realtime.run_realtime_market_session"
            ) as market_session,
        ):
            consumer_type.return_value.poll.return_value = {
                "status": "valid_changed"
            }
            consumer_type.return_value.last_valid = manifest
            result = run_realtime_confirmation_session(Settings(), duration_sec=30)

        market_session.assert_not_called()
        self.assertEqual(result["status"], "data_unavailable")
        self.assertEqual(result["exit_code"], 3)
        self.assertIn("candidate_manifest_lifetime_insufficient", result["failures"])
        self.assertEqual(result["elapsed_duration_sec"], 0.0)


if __name__ == "__main__":
    unittest.main()
