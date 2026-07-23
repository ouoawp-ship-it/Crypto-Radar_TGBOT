from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.derivatives_quality import (
    CoinGlassClient,
    CoinalyzeClient,
    DerivativesQualityService,
    source_agreement,
)


class FakeHttp:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[tuple[str, dict[str, object]]] = []

    def get_json(self, url: str, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((url, kwargs))
        for suffix, response in self.responses.items():
            if url.endswith(suffix):
                return response
        return None


class DerivativesQualityTests(unittest.TestCase):
    def test_source_agreement_distinguishes_high_low_and_conflict(self) -> None:
        self.assertEqual(source_agreement(10.0, 9.5, neutral_abs=0.1)["status"], "high")
        self.assertEqual(source_agreement(10.0, 6.0, neutral_abs=0.1)["status"], "low")
        conflict = source_agreement(2.0, -1.0, neutral_abs=0.1)
        self.assertEqual(conflict["status"], "conflict")
        self.assertEqual(conflict["gate"], "block")
        self.assertEqual(source_agreement(None, 1.0, neutral_abs=0.1)["status"], "single_source")

    def test_coinglass_normalizes_aggregate_oi_and_auth_header(self) -> None:
        settings = Settings(
            coinglass_enable=True,
            coinglass_api_key="secret",
        )
        http = FakeHttp({
            "/api/futures/open-interest/exchange-list": {
                "code": "0",
                "data": [
                    {
                        "exchange": "All",
                        "symbol": "BTC",
                        "open_interest_usd": 1000,
                        "open_interest_change_percent_15m": 1.25,
                        "open_interest_change_percent_1h": 4.5,
                    },
                    {"exchange": "Binance", "open_interest_usd": 500},
                ],
            }
        })

        snapshot = CoinGlassClient(settings, http).oi_snapshot("BTCUSDT", now_ts=1000)  # type: ignore[arg-type]

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot["scope"], "aggregate_all_exchanges")
        self.assertEqual(snapshot["oi_usd"], 1000)
        self.assertEqual(snapshot["changes"]["15m"], 1.25)
        self.assertEqual(snapshot["changes"]["1h"], 4.5)
        self.assertEqual(http.calls[0][1]["headers"], {"CG-API-KEY": "secret"})

    def test_coinglass_funding_is_already_expressed_in_percentage_points(self) -> None:
        settings = Settings(coinglass_enable=True, coinglass_api_key="secret")
        http = FakeHttp({
            "/api/futures/funding-rate/exchange-list": {
                "code": "0",
                "data": [{
                    "symbol": "BTC",
                    "stablecoin_margin_list": [{
                        "exchange": "Binance",
                        "funding_rate": -0.0123,
                        "funding_rate_interval": 8,
                    }],
                }],
            }
        })

        result = CoinGlassClient(settings, http).funding_snapshots(["BTCUSDT"], now_ts=1000)  # type: ignore[arg-type]

        self.assertEqual(result["BTCUSDT"]["funding_pct"], -0.0123)

    def test_coinglass_requests_validate_provider_business_code(self) -> None:
        settings = Settings(coinglass_enable=True, coinglass_api_key="secret")
        http = FakeHttp({
            "/api/futures/open-interest/exchange-list": {
                "code": "401",
                "msg": "Upgrade plan",
            }
        })

        result = CoinGlassClient(settings, http).oi_snapshot("BTCUSDT", now_ts=1000)  # type: ignore[arg-type]

        self.assertIsNone(result)
        validator = http.calls[0][1].get("payload_error")
        self.assertTrue(callable(validator))
        self.assertEqual(validator({"code": "401", "msg": "Upgrade plan"}), "api_code_401")

    def test_coinalyze_maps_binance_perpetual_and_normalizes_oi(self) -> None:
        settings = Settings(
            coinalyze_enable=True,
            coinalyze_api_key="secret",
        )
        http = FakeHttp({
            "/future-markets": [
                {
                    "symbol": "BTCUSDT_PERP.A",
                    "exchange": "A",
                    "symbol_on_exchange": "BTCUSDT",
                    "base_asset": "BTC",
                    "quote_asset": "USDT",
                    "is_perpetual": True,
                    "margined": "STABLE",
                },
                {
                    "symbol": "BTCUSD_QUARTER.A",
                    "exchange": "A",
                    "symbol_on_exchange": "BTCUSD",
                    "base_asset": "BTC",
                    "quote_asset": "USD",
                    "is_perpetual": False,
                    "margined": "COIN",
                },
            ],
            "/open-interest-history": [
                {
                    "symbol": "BTCUSDT_PERP.A",
                    "history": [
                        {"t": 900, "o": 90, "h": 110, "l": 90, "c": 100},
                        {"t": 1000, "o": 100, "h": 125, "l": 99, "c": 120},
                    ],
                }
            ],
        })
        client = CoinalyzeClient(settings, http)  # type: ignore[arg-type]

        snapshots = client.oi_snapshots(["BTCUSDT"], timeframe="1h", now_ts=1100)

        self.assertEqual(client.market_map(), {"BTCUSDT": "BTCUSDT_PERP.A"})
        self.assertEqual(snapshots["BTCUSDT"]["change_pct"], 20.0)
        self.assertEqual(snapshots["BTCUSDT"]["oi_usd"], 120)
        history_call = next(call for call in http.calls if call[0].endswith("/open-interest-history"))
        self.assertEqual(history_call[1]["params"]["convert_to_usd"], "true")
        self.assertEqual(history_call[1]["headers"], {"api_key": "secret"})

    def test_coinalyze_supports_exact_six_hour_oi_validation_window(self) -> None:
        settings = Settings(coinalyze_enable=True, coinalyze_api_key="secret")
        http = FakeHttp({
            "/future-markets": [{
                "symbol": "BTCUSDT_PERP.A",
                "exchange": "A",
                "symbol_on_exchange": "BTCUSDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "is_perpetual": True,
                "margined": "STABLE",
            }],
            "/open-interest-history": [{
                "symbol": "BTCUSDT_PERP.A",
                "history": [
                    {"t": 900, "c": 100},
                    {"t": 1000, "c": 112},
                ],
            }],
        })

        result = CoinalyzeClient(settings, http).oi_snapshots(
            ["BTCUSDT"],
            timeframe="6h",
            now_ts=1100,
        )

        self.assertEqual(result["BTCUSDT"]["change_pct"], 12.0)
        history_call = next(call for call in http.calls if call[0].endswith("/open-interest-history"))
        self.assertEqual(history_call[1]["params"]["interval"], "6hour")

    def test_coinalyze_normalizes_current_and_predicted_funding_to_percent(self) -> None:
        settings = Settings(coinalyze_enable=True, coinalyze_api_key="secret")
        http = FakeHttp({
            "/future-markets": [{
                "symbol": "BTCUSDT_PERP.A",
                "exchange": "A",
                "symbol_on_exchange": "BTCUSDT",
                "base_asset": "BTC",
                "quote_asset": "USDT",
                "is_perpetual": True,
                "margined": "STABLE",
            }],
            "/funding-rate": [{"symbol": "BTCUSDT_PERP.A", "value": 0.0001, "update": 1000}],
            "/predicted-funding-rate": [{"symbol": "BTCUSDT_PERP.A", "value": 0.0003, "update": 1000}],
        })

        result = CoinalyzeClient(settings, http).funding_snapshots(["BTCUSDT"], now_ts=1000)  # type: ignore[arg-type]

        self.assertAlmostEqual(result["BTCUSDT"]["funding_pct"], 0.01)
        self.assertAlmostEqual(result["BTCUSDT"]["predicted_funding_pct"], 0.03)
        self.assertAlmostEqual(result["BTCUSDT"]["funding_acceleration_pct"], 0.02)

    def test_service_prefers_coinglass_and_blocks_external_direction_conflict(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                coinglass_enable=True,
                coinglass_api_key="cg",
                coinalyze_enable=True,
                coinalyze_api_key="ca",
            )
            service = DerivativesQualityService(settings, FakeHttp({}))  # type: ignore[arg-type]

            class CoinGlass:
                available = True

                @staticmethod
                def oi_snapshot(symbol: str, now_ts: int):
                    return {"scope": "aggregate_all_exchanges", "changes": {"1h": 8.0}}

            class Coinalyze:
                available = True

                @staticmethod
                def oi_snapshots(symbols, timeframe: str, now_ts: int):  # type: ignore[no-untyped-def]
                    return {"BTCUSDT": {"scope": "binance_usdt_perpetual", "change_pct": -3.0}}

            service.coinglass = CoinGlass()  # type: ignore[assignment]
            service.coinalyze = Coinalyze()  # type: ignore[assignment]

            result = service.validate_oi_rows(
                [{"symbol": "BTCUSDT", "oi_1h": 7.5}],
                timeframe="1h",
                local_field="oi_1h",
                now_ts=1000,
            )["BTCUSDT"]

        self.assertEqual(result["status"], "conflict")
        self.assertEqual(result["gate"], "block")
        self.assertEqual(result["selected_change_pct"], 8.0)
        self.assertEqual(result["primary_source"], "coinglass")

    def test_service_explicitly_marks_unconfigured_fallback(self) -> None:
        service = DerivativesQualityService(Settings(), FakeHttp({}))  # type: ignore[arg-type]

        result = service.validate_oi_rows(
            [{"symbol": "BTCUSDT", "oi_1h": 2.0}],
            timeframe="1h",
            local_field="oi_1h",
            now_ts=1000,
        )["BTCUSDT"]

        self.assertEqual(result["status"], "not_configured")
        self.assertEqual(result["primary_source"], "binance")
        self.assertEqual(result["gate"], "degraded")

    def test_service_excludes_stale_external_observations(self) -> None:
        settings = Settings(coinalyze_enable=True, coinalyze_api_key="ca")
        service = DerivativesQualityService(settings, FakeHttp({}))  # type: ignore[arg-type]

        class CoinGlass:
            available = False

            @staticmethod
            def oi_snapshot(symbol: str, now_ts: int):
                return None

        class Coinalyze:
            available = True

            @staticmethod
            def oi_snapshots(symbols, timeframe: str, now_ts: int):  # type: ignore[no-untyped-def]
                return {"BTCUSDT": {"observed_at": 1, "change_pct": -10.0}}

        service.coinglass = CoinGlass()  # type: ignore[assignment]
        service.coinalyze = Coinalyze()  # type: ignore[assignment]

        result = service.validate_oi_rows(
            [{"symbol": "BTCUSDT", "oi_1h": 2.0}],
            timeframe="1h",
            local_field="oi_1h",
            now_ts=20_000,
        )["BTCUSDT"]

        self.assertEqual(result["source_values"]["coinalyze"], None)
        self.assertEqual(result["stale_sources"], ["coinalyze"])
        self.assertEqual(result["status"], "single_source")


if __name__ == "__main__":
    unittest.main()
