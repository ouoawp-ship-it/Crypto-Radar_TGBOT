from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from paopao_radar.config import Settings
from paopao_radar.data_sources import BinanceDataSource, DataQuality, HttpClient, UpstreamSourceMetrics


class MarketCapSourceTests(unittest.TestCase):
    def test_upstream_metrics_bound_source_cardinality(self) -> None:
        metrics = UpstreamSourceMetrics(source_limit=3)
        for index in range(8):
            metrics.record_network(f"source-{index}", success=True, duration_ms=index)

        snapshot = metrics.snapshot()

        self.assertLessEqual(len(snapshot["sources"]), 3)
        self.assertIn("other", snapshot["sources"])
        self.assertEqual(snapshot["collapsed_sources"], 6)

    def test_upstream_metrics_report_latency_success_cache_and_data_age(self) -> None:
        now = [1_000.0]
        metrics = UpstreamSourceMetrics(sample_limit=20, clock=lambda: now[0])
        metrics.record_cache("binance_spot_public", hit=False)
        for duration in range(1, 21):
            metrics.record_network(
                "binance_spot_public",
                success=duration < 20,
                duration_ms=duration,
                error="status=503" if duration == 20 else "",
            )
        now[0] = 1_005.0
        metrics.record_cache("binance_spot_public", hit=True)

        source = metrics.snapshot()["sources"]["binance_spot_public"]

        self.assertEqual(source["attempts"], 20)
        self.assertEqual(source["success_rate"], 0.95)
        self.assertEqual(source["p50_ms"], 10.0)
        self.assertEqual(source["p95_ms"], 19.0)
        self.assertEqual(source["cache_hit_rate"], 0.5)
        self.assertEqual(source["data_age_sec"], 5)
        self.assertEqual(source["last_error"], "status=503")

    def test_upstream_metrics_do_not_retain_arbitrary_error_details(self) -> None:
        metrics = UpstreamSourceMetrics()

        metrics.record_network(
            "binance_spot_public",
            success=False,
            duration_ms=10,
            error="Authorization=Bearer must-not-leak",
        )

        source = metrics.snapshot()["sources"]["binance_spot_public"]
        self.assertEqual(source["last_error"], "upstream_error")

    def test_http_cache_is_bounded_and_reports_source_metrics(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), http_cache_enable=True, http_cache_ttl_sec=60)
            session = Mock()
            response = session.get.return_value
            response.status_code = 200
            response.json.side_effect = [{"value": 1}, {"value": 2}, {"value": 3}]
            metrics = UpstreamSourceMetrics()
            client = HttpClient(
                settings,
                DataQuality(),
                session=session,
                metrics=metrics,
                cache_max_entries=2,
            )

            first = client.get_json("https://example.test/one", cache_key="one", quality_key="spotKlines")
            cached = client.get_json("https://example.test/one", cache_key="one", quality_key="spotKlines")
            client.get_json("https://example.test/two", cache_key="two", quality_key="spotKlines")
            client.get_json("https://example.test/three", cache_key="three", quality_key="spotKlines")

        self.assertEqual(first, cached)
        self.assertEqual(session.get.call_count, 3)
        self.assertEqual(client.diagnostics()["entries"], 2)
        self.assertEqual(client.diagnostics()["evictions"], 1)
        source = metrics.snapshot()["sources"]["binance_spot_public"]
        self.assertEqual(source["successes"], 3)
        self.assertEqual(source["cache_hits"], 1)
        self.assertEqual(source["cache_misses"], 3)

    def test_http_cache_prunes_expired_entries_before_reuse(self) -> None:
        with TemporaryDirectory() as tmp:
            now = [100.0]
            settings = Settings(data_dir=Path(tmp), http_cache_enable=True, http_cache_ttl_sec=1)
            session = Mock()
            response = session.get.return_value
            response.status_code = 200
            response.json.side_effect = [{"value": 1}, {"value": 2}]
            client = HttpClient(settings, DataQuality(), session=session, cache_max_entries=2)

            with patch("paopao_radar.data_sources.time.time", side_effect=lambda: now[0]):
                first = client.get_json("https://example.test/one", cache_key="one")
                now[0] = 102.0
                second = client.get_json("https://example.test/one", cache_key="one")
                diagnostics = client.diagnostics()

        self.assertNotEqual(first, second)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(diagnostics["entries"], 1)
        self.assertEqual(diagnostics["expired_pruned"], 1)

    def test_http_cache_uses_configured_limit_and_can_be_bypassed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                http_cache_enable=True,
                http_cache_ttl_sec=60,
                http_cache_max_entries=7,
            )
            session = Mock()
            session.get.return_value.status_code = 200
            session.get.return_value.json.side_effect = [{"value": 1}, {"value": 2}]
            client = HttpClient(settings, DataQuality(), session=session)

            client.get_json("https://example.test/live", cache_key="live", cache=False)
            client.get_json("https://example.test/live", cache_key="live", cache=False)

        self.assertEqual(client.cache_max_entries, 7)
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(client.diagnostics()["entries"], 0)

    def test_http_client_reuses_owned_session(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), http_cache_enable=False)
            with patch("requests.Session") as session_factory:
                session = session_factory.return_value
                response = session.get.return_value
                response.status_code = 200
                response.json.return_value = {"ok": True}
                client = HttpClient(settings, DataQuality())
                client.get_json("https://example.test/one")
                client.get_json("https://example.test/two")
                self.assertEqual(client.diagnostics()["entries"], 0)
                client.close()

        session_factory.assert_called_once_with()
        self.assertEqual(session.get.call_count, 2)
        session.close.assert_called_once_with()

    def test_http_client_does_not_close_injected_session(self) -> None:
        with TemporaryDirectory() as tmp:
            session = Mock()
            client = HttpClient(Settings(data_dir=Path(tmp)), DataQuality(), session=session)
            client.close()

        session.close.assert_not_called()

    def test_binance_source_context_closes_owned_http_client(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch("requests.Session") as session_factory:
                with BinanceDataSource(Settings(data_dir=Path(tmp))):
                    pass

        session_factory.return_value.close.assert_called_once_with()

    def test_coinpaprika_market_caps_parse_usd_quotes_and_prefer_better_rank(self) -> None:
        with TemporaryDirectory() as tmp:
            source = BinanceDataSource(Settings(data_dir=Path(tmp)))
            payload = [
                {"symbol": "TEST", "rank": 200, "quotes": {"USD": {"market_cap": 10_000_000}}},
                {"symbol": "TEST", "rank": 50, "quotes": {"USD": {"market_cap": 123_000_000}}},
                {"symbol": "BAD", "rank": 10, "quotes": {"USD": {"market_cap": 0}}},
            ]

            with patch.object(source.http, "get_json", return_value=payload) as get_json:
                result = source.coinpaprika_market_caps()
            source.close()

        self.assertEqual(result, {"TEST": 123_000_000})
        get_json.assert_called_once()


if __name__ == "__main__":
    unittest.main()
