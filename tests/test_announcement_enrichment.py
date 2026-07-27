from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.announcement_enrichment import (
    AnnouncementProjectEnricher,
    format_announcement_profiles,
)
from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore


NOW_TS = 1_800_000_000


def coin_detail(
    *,
    symbol: str = "abc",
    include_contract: bool = True,
    include_fdv: bool = True,
) -> dict[str, object]:
    market_data: dict[str, object] = {
        "current_price": {"usd": 1.25},
        "market_cap": {"usd": 100_000_000},
        "total_supply": 200_000_000,
        "circulating_supply": 50_000_000,
    }
    if include_fdv:
        market_data["fully_diluted_valuation"] = {"usd": 250_000_000}
    return {
        "id": "abc-token",
        "symbol": symbol,
        "market_data": market_data,
        "platforms": {"base": "0xabc"} if include_contract else {},
        "categories": ["Layer 2", "Infrastructure"],
    }


class _AnnouncementHttp:
    def __init__(
        self,
        *,
        search: object,
        detail: object,
    ):
        self.search = search
        self.detail = detail
        self.calls: list[str] = []

    def get_json(self, url: str, **_kwargs: object) -> object:
        self.calls.append(url)
        return self.search if url.endswith("/search") else self.detail


class AnnouncementEnrichmentTests(unittest.TestCase):
    def _settings(self, root: Path, **overrides: object) -> Settings:
        return Settings(
            data_dir=root,
            announcement_enrichment_enable=True,
            announcement_enrichment_cache_path=root / "announcement_cache.json",
            **overrides,
        )

    def test_disabled_mode_has_no_network_or_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                announcement_enrichment_enable=False,
                announcement_enrichment_cache_path=root / "cache.json",
            )
            http = _AnnouncementHttp(search={}, detail={})
            alerts = [{"symbols": ["ABC"]}]
            enriched, diagnostics = AnnouncementProjectEnricher(
                settings,
                JsonStore(root),
                http,
            ).enrich(alerts, now_ts=NOW_TS)
            self.assertIs(enriched, alerts)
            self.assertEqual(diagnostics["status"], "disabled")
            self.assertEqual(http.calls, [])
            self.assertFalse(settings.announcement_enrichment_cache_path.exists())

    def test_exact_symbol_profile_has_field_sources_and_supply_ratio(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = _AnnouncementHttp(
                search={"coins": [{"id": "abc-token", "symbol": "abc"}]},
                detail=coin_detail(),
            )
            enriched, diagnostics = AnnouncementProjectEnricher(
                self._settings(root),
                JsonStore(root),
                http,
            ).enrich([{"symbols": ["ABC"]}], now_ts=NOW_TS)
            profile = enriched[0]["project_profiles"]["ABC"]
            self.assertEqual(profile["status"], "ok")
            self.assertEqual(profile["fields"]["current_price_usd"]["source"], "CoinGecko")
            self.assertEqual(profile["fields"]["circulating_ratio"]["value"], 0.25)
            self.assertEqual(profile["fields"]["chain"]["value"], "base")
            self.assertEqual(diagnostics["profiles_ok"], 1)
            alert = {
                **enriched[0],
                "announcement_release_ts": NOW_TS,
            }
            text = "\n".join(format_announcement_profiles(alert))
            self.assertIn("公告发布时间", text)
            self.assertIn("字段来源: CoinGecko", text)
            self.assertIn("VC/投资机构", text)
            self.assertNotIn("launch_time", text)
            supply_line = next(
                line for line in text.splitlines()
                if line.startswith("供应:")
            )
            self.assertNotIn("$", supply_line)

    def test_same_symbol_is_ambiguous_and_no_match_is_unmatched(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            ambiguous_http = _AnnouncementHttp(
                search={"coins": [
                    {"id": "abc-one", "symbol": "abc"},
                    {"id": "abc-two", "symbol": "ABC"},
                ]},
                detail={},
            )
            ambiguous, _ = AnnouncementProjectEnricher(
                self._settings(root),
                JsonStore(root),
                ambiguous_http,
            ).enrich([{"symbols": ["ABC"]}], now_ts=NOW_TS)
            self.assertEqual(
                ambiguous[0]["project_profiles"]["ABC"]["status"],
                "ambiguous",
            )

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            unmatched_http = _AnnouncementHttp(
                search={"coins": [{"id": "xyz", "symbol": "xyz"}]},
                detail={},
            )
            unmatched, _ = AnnouncementProjectEnricher(
                self._settings(root),
                JsonStore(root),
                unmatched_http,
            ).enrich([{"symbols": ["ABC"]}], now_ts=NOW_TS)
            self.assertEqual(
                unmatched[0]["project_profiles"]["ABC"]["status"],
                "unmatched",
            )

    def test_missing_contract_and_fdv_are_preserved_as_missing_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            http = _AnnouncementHttp(
                search={"coins": [{"id": "abc-token", "symbol": "abc"}]},
                detail=coin_detail(include_contract=False, include_fdv=False),
            )
            enriched, _ = AnnouncementProjectEnricher(
                self._settings(root),
                JsonStore(root),
                http,
            ).enrich([{"symbols": ["ABC"]}], now_ts=NOW_TS)
            fields = enriched[0]["project_profiles"]["ABC"]["fields"]
            self.assertIsNone(fields["contract_address"]["value"])
            self.assertIsNone(fields["fdv_usd"]["value"])

    def test_cache_hit_avoids_external_call(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            store = JsonStore(root)
            http = _AnnouncementHttp(
                search={"coins": [{"id": "abc-token", "symbol": "abc"}]},
                detail=coin_detail(),
            )
            enricher = AnnouncementProjectEnricher(settings, store, http)
            enricher.enrich([{"symbols": ["ABC"]}], now_ts=NOW_TS)
            call_count = len(http.calls)
            second, diagnostics = enricher.enrich(
                [{"symbols": ["ABC"]}],
                now_ts=NOW_TS + 60,
            )
            self.assertEqual(len(http.calls), call_count)
            self.assertEqual(diagnostics["cache_hits"], 1)
            self.assertEqual(
                second[0]["project_profiles"]["ABC"]["cache_status"],
                "hit",
            )

    def test_external_failure_degrades_without_removing_original_alert(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = {"code": "announcement-1", "symbols": ["ABC"]}
            http = _AnnouncementHttp(search=None, detail=None)
            enriched, diagnostics = AnnouncementProjectEnricher(
                self._settings(root),
                JsonStore(root),
                http,
            ).enrich([original], now_ts=NOW_TS)
            self.assertEqual(enriched[0]["code"], "announcement-1")
            self.assertEqual(
                enriched[0]["project_profiles"]["ABC"]["status"],
                "degraded",
            )
            self.assertEqual(diagnostics["status"], "degraded")
