from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from config import Settings
from radars.altcoin_contract_anomaly.formatter import render_console, render_telegram_preview
from radars.altcoin_contract_anomaly.radar import (
    AltcoinAnomalyDataUnavailable,
    load_cached_pool,
    scan_candidate_pool,
)
from radars.altcoin_contract_anomaly.state import CandidateStatePartialUpdateError
from shared.binance_data import RequestBudget
from shared.cmc_data import CmcMapEntry, CmcMapResult, CmcQuote, CmcQuotesResult


NOW = 1_800_000_000.0
NOW_ISO = datetime.fromtimestamp(NOW, timezone.utc).isoformat()


class FakeBinanceSource:
    def __init__(self, *, oi_timestamp: float = NOW) -> None:
        self.budget = RequestBudget({"open_interest_hist": 600})
        self.open_calls: list[str] = []
        self.oi_timestamp = oi_timestamp

    def usdt_perp_symbols(self):
        return [
            {"symbol": "COTIUSDT", "baseAsset": "COTI"},
            {"symbol": "HEIUSDT", "baseAsset": "HEI"},
            {"symbol": "1000CATUSDT", "baseAsset": "1000CAT"},
            {"symbol": "NEWUSDT", "baseAsset": "NEW"},
            {"symbol": "USDCUSDT", "baseAsset": "USDC"},
            {"symbol": "BTCUSDT", "baseAsset": "BTC"},
            {
                "symbol": "DEFIUSDT",
                "baseAsset": "DEFI",
                "underlyingSubType": ["INDEX"],
            },
        ]

    def marketing_symbols(self):
        return [
            {"symbol": "COTIUSDT", "base_asset": "COTI", "cmc_id": 1, "mapper_name": "COTI"},
            {"symbol": "HEIUSDT", "base_asset": "HEI", "cmc_id": 2, "mapper_name": "HEI"},
            {"symbol": "1000CATUSDT", "base_asset": "1000CAT", "cmc_id": 3, "mapper_name": "CAT"},
        ]

    def premium_index(self):
        return [
            {"symbol": "COTIUSDT", "markPrice": "0.10", "lastFundingRate": "-0.000352", "time": NOW * 1000},
            {"symbol": "HEIUSDT", "markPrice": "0.50", "lastFundingRate": "-0.000053", "time": NOW * 1000},
            {"symbol": "1000CATUSDT", "markPrice": "0.001", "lastFundingRate": "0", "time": NOW * 1000},
            {"symbol": "NEWUSDT", "markPrice": "1", "lastFundingRate": "-0.1", "time": NOW * 1000},
        ]

    def open_interest_hist(self, symbol, period="5m", limit=1):
        self.open_calls.append(symbol)
        self.budget.consume("open_interest_hist")
        values = {
            "COTIUSDT": ("82963500", "8296350"),
            "HEIUSDT": ("33137240", "16568620"),
            # The USD field is authoritative: no extra x1000 multiplier and no raw*mark.
            "1000CATUSDT": ("6000", "6000000"),
        }
        raw, usd = values[symbol]
        return [{
            "sumOpenInterest": raw,
            "sumOpenInterestValue": usd,
            "timestamp": self.oi_timestamp * 1000,
        }]


class FakeCmcClient:
    def __init__(self, *, stale_quote_id: int | None = None) -> None:
        self.requested_ids: tuple[int, ...] = ()
        self.stale_quote_id = stale_quote_id

    def load_map(self):
        entries = tuple(
            CmcMapEntry(cmc_id, name, symbol, slug, True)
            for cmc_id, name, symbol, slug in (
                (1, "COTI", "COTI", "coti"),
                (2, "Heima", "HEI", "heima"),
                (3, "Simon's Cat", "CAT", "simons-cat"),
                (4, "New Token", "NEW", "new-token"),
            )
        )
        return CmcMapResult(entries, "network", NOW_ISO, NOW_ISO, NOW_ISO, 1)

    def quotes_latest(self, cmc_ids):
        self.requested_ids = tuple(cmc_ids)
        quotes = {
            1: CmcQuote(1, "COTI", "COTI", "coti", 23_370_000, NOW_ISO),
            2: CmcQuote(2, "Heima", "HEI", "heima", 13_430_000, NOW_ISO),
            3: CmcQuote(3, "Simon's Cat", "CAT", "simons-cat", 12_000_000, NOW_ISO),
        }
        stale = {}
        if self.stale_quote_id is not None:
            item = quotes.pop(self.stale_quote_id)
            stale[self.stale_quote_id] = CmcQuote(
                item.cmc_id,
                item.name,
                item.symbol,
                item.slug,
                item.market_cap_usd,
                datetime.fromtimestamp(NOW - 1000, timezone.utc).isoformat(),
            )
        return CmcQuotesResult(
            quotes,
            stale,
            tuple(stale),
            {cmc_id: "network" for cmc_id in quotes},
            1,
            0,
            0,
        )


class InvalidOiBinanceSource(FakeBinanceSource):
    def open_interest_hist(self, symbol, period="5m", limit=1):
        rows = super().open_interest_hist(symbol, period=period, limit=limit)
        if symbol == "HEIUSDT":
            rows[0]["sumOpenInterestValue"] = "-1"
        return rows


class AltcoinCandidateScanTests(unittest.TestCase):
    def make_settings(self, root: Path) -> Settings:
        return Settings(
            data_dir=root,
            altcoin_contract_anomaly_enable=True,
            altcoin_contract_anomaly_cmc_api_key="fake-cmc-key",
            altcoin_contract_anomaly_cmc_cache_path=root / "cmc.json",
            altcoin_contract_anomaly_candidate_snapshot_path=root / "pool.json",
            altcoin_contract_anomaly_mapping_overrides_path=(
                Path(__file__).resolve().parents[2] / "config" / "altcoin_contract_anomaly_overrides.json"
            ),
            altcoin_contract_anomaly_cmc_max_data_age_sec=900,
            altcoin_contract_anomaly_binance_oi_max_age_sec=600,
            altcoin_contract_anomaly_funding_max_age_sec=600,
        )

    def test_no_network_fixture_scan_builds_complete_candidate_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = FakeBinanceSource()
            cmc = FakeCmcClient()
            pool = scan_candidate_pool(
                self.make_settings(root),
                source=source,
                cmc_client=cmc,
                now_ts=NOW,
            )

            self.assertEqual(pool["universe"]["loaded_usdt_perpetuals"], 7)
            self.assertEqual(pool["universe"]["eligible_altcoin_contracts"], 4)
            self.assertEqual(
                {item["symbol"] for item in pool["universe"]["excluded_contract_records"]},
                {"BTCUSDT", "DEFIUSDT", "USDCUSDT"},
            )
            self.assertEqual(pool["mapping_stats"]["trusted_count"], 3)
            self.assertEqual(pool["mapping_stats"]["diagnostic_count"], 1)
            self.assertEqual(pool["short_squeeze_symbols"], ["HEIUSDT", "COTIUSDT"])
            self.assertEqual(pool["high_leverage_symbols"], ["HEIUSDT", "1000CATUSDT"])
            self.assertEqual(pool["dual_match_symbols"], ["HEIUSDT"])
            self.assertEqual(pool["candidate_symbols"], ["1000CATUSDT", "COTIUSDT", "HEIUSDT"])
            self.assertEqual(cmc.requested_ids, (1, 2, 3))
            self.assertEqual(sorted(source.open_calls), ["1000CATUSDT", "COTIUSDT", "HEIUSDT"])

            rows = {item["symbol"]: item for item in pool["snapshots"]}
            mappings = {item["binance_symbol"]: item for item in pool["mappings"]}
            self.assertEqual(mappings["1000CATUSDT"]["cmc_slug"], "simons-cat")
            self.assertIn("multiplier_mapper_name_consistent", mappings["1000CATUSDT"]["mapping_evidence"])
            self.assertEqual(rows["1000CATUSDT"]["contract_multiplier"], 1000)
            self.assertEqual(
                rows["1000CATUSDT"]["open_interest_unit"],
                "contract_base_asset_quantity",
            )
            self.assertEqual(rows["1000CATUSDT"]["oi_value_usd"], 6_000_000)
            self.assertEqual(rows["1000CATUSDT"]["binance_oi_usd"], 6_000_000)
            self.assertEqual(
                rows["1000CATUSDT"]["binance_oi_market_cap_ratio"],
                0.5,
            )
            self.assertIsNone(rows["1000CATUSDT"]["global_oi_usd"])
            self.assertIsNone(rows["1000CATUSDT"]["global_oi_source"])
            self.assertEqual(
                rows["1000CATUSDT"]["market_cap_source"],
                "coinmarketcap_v3_quotes_latest:network",
            )
            self.assertEqual(
                rows["1000CATUSDT"]["oi_value_method"],
                "binance_sum_open_interest_value",
            )
            self.assertEqual(rows["NEWUSDT"]["mapping_method"], "unique_symbol_diagnostic")
            self.assertEqual(rows["NEWUSDT"]["candidate_tags"], [])
            self.assertTrue((root / "pool.json").exists())
            json.loads((root / "pool.json").read_text(encoding="utf-8"))

            console = render_console(pool)
            self.assertIn("已加载USDT永续：7", console)
            self.assertIn("潜在狗庄候选", console)
            pages = render_telegram_preview(pool, max_chars=700)
            self.assertTrue(all(len(page) <= 700 for page in pages))
            self.assertIn("第1/", pages[0])

    def test_stale_market_cap_is_observable_and_cannot_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pool = scan_candidate_pool(
                self.make_settings(root),
                source=FakeBinanceSource(),
                cmc_client=FakeCmcClient(stale_quote_id=2),
                now_ts=NOW,
            )
            row = next(item for item in pool["snapshots"] if item["symbol"] == "HEIUSDT")
            self.assertEqual(row["data_quality"], "stale")
            self.assertIn("market_cap_usd", row["stale_fields"])
            self.assertEqual(row["candidate_tags"], [])

    def test_cache_only_reuses_fresh_snapshot_without_any_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.make_settings(root)
            first = scan_candidate_pool(
                settings,
                source=FakeBinanceSource(),
                cmc_client=FakeCmcClient(),
                now_ts=NOW,
            )
            cached = load_cached_pool(settings, now_ts=NOW + 10)
            self.assertEqual(cached["candidate_pool_hash"], first["candidate_pool_hash"])
            self.assertEqual(cached["diagnostics"]["network_status"], "仅缓存离线")
            self.assertEqual(cached["diagnostics"]["candidate_snapshot_cache_age_sec"], 10)

    def test_cache_only_rechecks_each_candidate_field_freshness(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.make_settings(root)
            scan_candidate_pool(
                settings,
                source=FakeBinanceSource(oi_timestamp=NOW - 599),
                cmc_client=FakeCmcClient(),
                now_ts=NOW,
            )

            with self.assertRaises(AltcoinAnomalyDataUnavailable):
                load_cached_pool(settings, now_ts=NOW + 2)

    def test_cache_only_rejects_snapshot_built_with_different_rule_parameters(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.make_settings(root)
            scan_candidate_pool(
                settings,
                source=FakeBinanceSource(),
                cmc_client=FakeCmcClient(),
                now_ts=NOW,
            )
            changed = replace(
                settings,
                altcoin_contract_anomaly_high_leverage_min_oi_market_cap_ratio=0.60,
            )

            with self.assertRaises(AltcoinAnomalyDataUnavailable):
                load_cached_pool(changed, now_ts=NOW + 1)

    def test_invalid_reported_oi_value_never_silently_falls_back_to_raw_math(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pool = scan_candidate_pool(
                self.make_settings(root),
                source=InvalidOiBinanceSource(),
                cmc_client=FakeCmcClient(),
                now_ts=NOW,
            )

            row = next(item for item in pool["snapshots"] if item["symbol"] == "HEIUSDT")
            self.assertIsNone(row["oi_value_usd"])
            self.assertIn("oi_value_usd", row["invalid_fields"])
            self.assertEqual(row["candidate_tags"], [])

    def test_partial_oi_failure_preserves_the_previous_complete_candidate_pool(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = self.make_settings(root)
            complete = scan_candidate_pool(
                settings,
                source=FakeBinanceSource(),
                cmc_client=FakeCmcClient(),
                now_ts=NOW,
            )

            with self.assertRaises(CandidateStatePartialUpdateError):
                scan_candidate_pool(
                    settings,
                    source=InvalidOiBinanceSource(),
                    cmc_client=FakeCmcClient(),
                    now_ts=NOW,
                )

            persisted = json.loads((root / "pool.json").read_text(encoding="utf-8"))

        self.assertEqual(
            persisted["candidate_symbols"],
            complete["candidate_symbols"],
        )


if __name__ == "__main__":
    unittest.main()
