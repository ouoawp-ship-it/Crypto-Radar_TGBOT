from __future__ import annotations

import time
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from config import Settings
from radars.pulse import review_store
from radars.pulse.divergence import (
    TEMPLATE_ID as DIVERGENCE_TEMPLATE_ID,
    DivergenceConfig,
    build_analysis,
)
from radars.pulse.radar import PulseRadar
from radars.pulse.simple_alert import (
    TEMPLATE_ID as SIMPLE_TEMPLATE_ID,
    PulseCandidate,
    SimpleAlertConfig,
    _candidate_pool,
    run_cycle,
)
from shared.binance_data import RequestBudget
from shared.storage import JsonStore


def _test_candidate(symbol: str = "ABCUSDT") -> PulseCandidate:
    return PulseCandidate(
        symbol=symbol,
        base=symbol.removesuffix("USDT"),
        quote_volume_24h=25_000_000.0,
        price_change_24h=1.0,
        market_cap=100_000_000.0,
        market_cap_source="Binance市场资料",
        market_cap_tier="medium",
        liquidity_tier="high",
        classification={"asset_family": "crypto"},
    )


class PulseRegressionTests(unittest.TestCase):
    def test_both_pulse_windows_use_existing_launch_topic(self) -> None:
        self.assertEqual(SIMPLE_TEMPLATE_ID, "TG_LAUNCH_ALERT")
        self.assertEqual(DIVERGENCE_TEMPLATE_ID, "TG_LAUNCH_ALERT")

    def test_candidate_pool_reserves_rotation_slots(self) -> None:
        class Source:
            settings = Settings(excluded_base_assets=())

            @staticmethod
            def exchange_info() -> dict[str, object]:
                return {
                    "symbols": [
                        {
                            "symbol": f"{symbol}USDT",
                            "baseAsset": symbol,
                            "quoteAsset": "USDT",
                            "status": "TRADING",
                            "contractType": "PERPETUAL",
                            "underlyingType": "COIN",
                        }
                        for symbol in (
                            "AAA", "BBB", "CCC", "DDD",
                            "EEE", "FFF", "GGG", "HHH",
                        )
                    ]
                }

            @staticmethod
            def ticker_24h() -> list[dict[str, str]]:
                symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF", "GGG", "HHH"]
                return [
                    {
                        "symbol": f"{symbol}USDT",
                        "quoteVolume": str(1000 - index * 10),
                        "priceChangePercent": "10" if index in {2, 3, 4} else "1",
                    }
                    for index, symbol in enumerate(symbols)
                ]

        selected, diagnostics = _candidate_pool(
            Source(),  # type: ignore[arg-type]
            SimpleAlertConfig(
                fixed_top=2,
                rotation_slots=2,
                ticker_filter_pct=5,
                min_quote_volume_usd=0,
            ),
            limit=5,
            window_index=1,
        )

        selected_symbols = [candidate.symbol for candidate in selected]
        self.assertEqual(selected_symbols[:2], ["AAAUSDT", "BBBUSDT"])
        self.assertEqual(
            len({"FFFUSDT", "GGGUSDT", "HHHUSDT"} & set(selected_symbols)),
            2,
        )
        self.assertFalse(diagnostics["full_coverage"])

    def test_candidate_pool_excludes_tradfi_stable_and_unknown_but_scans_all_crypto(self) -> None:
        class Source:
            settings = Settings(excluded_base_assets=())

            @staticmethod
            def exchange_info() -> dict[str, object]:
                return {"symbols": [
                    {
                        "symbol": "BTCUSDT", "baseAsset": "BTC",
                        "quoteAsset": "USDT", "status": "TRADING",
                        "contractType": "PERPETUAL", "underlyingType": "COIN",
                    },
                    {
                        "symbol": "MRNAUSDT", "baseAsset": "MRNA",
                        "quoteAsset": "USDT", "status": "TRADING",
                        "contractType": "TRADIFI_PERPETUAL", "underlyingType": "EQUITY",
                    },
                    {
                        "symbol": "USDCUSDT", "baseAsset": "USDC",
                        "quoteAsset": "USDT", "status": "TRADING",
                        "contractType": "PERPETUAL", "underlyingType": "COIN",
                    },
                    {
                        "symbol": "MYSTERYUSDT", "baseAsset": "MYSTERY",
                        "quoteAsset": "USDT", "status": "TRADING",
                        "contractType": "PERPETUAL",
                    },
                ]}

            @staticmethod
            def ticker_24h() -> list[dict[str, str]]:
                return [
                    {
                        "symbol": symbol,
                        "quoteVolume": "25000000",
                        "priceChangePercent": "1",
                    }
                    for symbol in (
                        "BTCUSDT", "MRNAUSDT", "USDCUSDT", "MYSTERYUSDT"
                    )
                ]

        selected, diagnostics = _candidate_pool(
            Source(),  # type: ignore[arg-type]
            SimpleAlertConfig(),
            limit=0,
            window_index=1,
            market_caps={"BTC": 1_000_000_000_000.0},
        )

        self.assertEqual([candidate.symbol for candidate in selected], ["BTCUSDT"])
        self.assertEqual(diagnostics["rejected"]["non_crypto_contract_type"], 1)
        self.assertEqual(diagnostics["rejected"]["stablecoin"], 1)
        self.assertEqual(diagnostics["rejected"]["unknown_asset"], 1)
        self.assertTrue(diagnostics["full_coverage"])

    def test_dry_run_does_not_create_follow_state(self) -> None:
        class Source:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            @staticmethod
            def market_caps() -> dict[str, float]:
                return {}

            @staticmethod
            def diagnostics() -> dict[str, object]:
                return {}

            @staticmethod
            def close() -> None:
                return None

        class Gateway:
            @staticmethod
            def send(*_args, **_kwargs):
                return SimpleNamespace(
                    sent=False,
                    status="dry_run",
                    reason="send_flag_not_set",
                    message_ids=[],
                )

        item = {
            "symbol": "ABCUSDT",
            "base": "ABC",
            "template": "health_up",
            "price_map": {3: 10.0},
            "oi_map": {3: 12.0},
            "current_price": 1.0,
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root)
            cfg = SimpleAlertConfig(state_path=root / "pulse_state.json")
            with (
                patch("radars.pulse.simple_alert.BinanceDataSource", Source),
                patch(
                    "radars.pulse.simple_alert._candidate_pool",
                    return_value=([_test_candidate()], {"full_coverage": True}),
                ),
                patch("radars.pulse.simple_alert._analyze_symbol", return_value=item),
                patch("radars.pulse.simple_alert._long_short_ratio", return_value=None),
                patch("radars.pulse.simple_alert._format_card", return_value="pulse"),
                redirect_stdout(StringIO()),
            ):
                run_cycle(
                    settings,
                    Gateway(),  # type: ignore[arg-type]
                    cfg,
                    send=False,
                    confirm_real_send=False,
                    now_ts=1000,
                )

            self.assertFalse((root / "pulse_state.json").exists())

    def test_simple_alert_attaches_chart_to_the_existing_single_delivery(self) -> None:
        chart = b"\x89PNG\r\n\x1a\npulse-chart"

        class Source:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            @staticmethod
            def market_caps() -> dict[str, float]:
                return {}

            @staticmethod
            def diagnostics() -> dict[str, object]:
                return {}

            @staticmethod
            def close() -> None:
                return None

        class Gateway:
            calls: list[dict[str, object]] = []

            @classmethod
            def send(cls, *_args, **kwargs):
                cls.calls.append(dict(kwargs))
                return SimpleNamespace(
                    sent=False,
                    status="dry_run",
                    reason="send_flag_not_set",
                    message_ids=[],
                )

        item = {
            "symbol": "ABCUSDT",
            "base": "ABC",
            "template": "health_up",
            "price_map": {3: 10.0},
            "oi_map": {3: 12.0},
            "current_price": 1.0,
        }
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root)
            cfg = SimpleAlertConfig(state_path=root / "pulse_state.json")
            with (
                patch("radars.pulse.simple_alert.BinanceDataSource", Source),
                patch(
                    "radars.pulse.simple_alert._candidate_pool",
                    return_value=([_test_candidate()], {"full_coverage": True}),
                ),
                patch("radars.pulse.simple_alert._analyze_symbol", return_value=item),
                patch("radars.pulse.simple_alert._long_short_ratio", return_value=None),
                patch("radars.pulse.simple_alert._format_card", return_value="pulse"),
                patch("radars.pulse.simple_alert._render_pulse_chart", return_value=chart),
                redirect_stdout(StringIO()),
            ):
                run_cycle(
                    settings,
                    Gateway(),  # type: ignore[arg-type]
                    cfg,
                    send=False,
                    confirm_real_send=False,
                    now_ts=1000,
                )

        self.assertEqual(len(Gateway.calls), 1)
        self.assertEqual(Gateway.calls[0]["photo"], chart)

    def test_full_mode_analyzes_every_candidate_once_and_reserves_chart_budget(self) -> None:
        class Source:
            instance = None

            def __init__(self, *_args, **_kwargs) -> None:
                self.budget = RequestBudget({
                    "open_interest_hist": 1,
                    "klines": 1,
                    "spot_klines": 1,
                })
                Source.instance = self

            @staticmethod
            def market_caps() -> dict[str, float]:
                return {}

            @staticmethod
            def coinpaprika_market_caps() -> dict[str, float]:
                return {}

            def diagnostics(self) -> dict[str, object]:
                return {"budget": self.budget.snapshot()}

            @staticmethod
            def close() -> None:
                return None

        candidates = [_test_candidate(f"{name}USDT") for name in ("AAA", "BBB", "CCC")]
        calls: list[str] = []

        def analyze(_source, candidate, _window_end_ms, _cfg):
            calls.append(candidate.symbol)
            return {
                "symbol": candidate.symbol,
                "base": candidate.base,
                "template": None,
            }

        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                oi_hist_budget=1,
                kline_budget=1,
            )
            with (
                patch("radars.pulse.simple_alert.BinanceDataSource", Source),
                patch(
                    "radars.pulse.simple_alert._candidate_pool",
                    return_value=(candidates, {"full_coverage": True}),
                ),
                patch(
                    "radars.pulse.simple_alert._analyze_symbol",
                    side_effect=analyze,
                ),
                redirect_stdout(StringIO()),
            ):
                diagnostics = run_cycle(
                    settings,
                    SimpleNamespace(),  # type: ignore[arg-type]
                    SimpleAlertConfig(scan_limit=0, scan_workers=3),
                    send=False,
                    confirm_real_send=False,
                    now_ts=1000,
                )

        self.assertCountEqual(calls, ["AAAUSDT", "BBBUSDT", "CCCUSDT"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(diagnostics["scan_mode"], "all_eligible_crypto")
        self.assertEqual(diagnostics["coverage_status"], "complete")
        assert Source.instance is not None
        self.assertEqual(Source.instance.budget.limits["open_interest_hist"], 43)
        self.assertEqual(Source.instance.budget.limits["klines"], 43)
        self.assertEqual(Source.instance.budget.limits["spot_klines"], 3)

    def test_divergence_analyzes_each_candidate_once(self) -> None:
        class MainSource:
            @staticmethod
            def exchange_info() -> dict[str, object]:
                crypto = [
                    {
                        "symbol": f"{symbol}USDT",
                        "baseAsset": symbol,
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "contractType": "PERPETUAL",
                        "underlyingType": "COIN",
                    }
                    for symbol in ("AAA", "BBB", "CCC")
                ]
                return {"symbols": [
                    *crypto,
                    {
                        "symbol": "MRNAUSDT",
                        "baseAsset": "MRNA",
                        "quoteAsset": "USDT",
                        "status": "TRADING",
                        "contractType": "TRADIFI_PERPETUAL",
                        "underlyingType": "EQUITY",
                    },
                ]}

            @staticmethod
            def ticker_24h() -> list[dict[str, str]]:
                return [
                    {
                        "symbol": f"{symbol}USDT",
                        "quoteVolume": "10000000",
                    }
                    for symbol in ("AAA", "BBB", "CCC", "MRNA")
                ]

        class WorkerSource:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            @staticmethod
            def close() -> None:
                return None

        calls: list[str] = []

        def analyze(_source, symbol: str, _end_ms: int) -> dict[str, object]:
            calls.append(symbol)
            return {
                "symbol": symbol,
                "coin": symbol.removesuffix("USDT"),
                "price": 1.0,
                "price_pct": 5.0,
                "oi_pct": 10.0,
                "divergence": 5.0,
            }

        with (
            patch("radars.pulse.divergence.BinanceDataSource", WorkerSource),
            patch("radars.pulse.divergence.analyze_symbol", side_effect=analyze),
        ):
            analysis = build_analysis(
                MainSource(),  # type: ignore[arg-type]
                DivergenceConfig(min_quote_volume_usd=0),
                Settings(excluded_base_assets=()),
                SimpleNamespace(end_ms=123),
            )

        self.assertCountEqual(calls, ["AAAUSDT", "BBBUSDT", "CCCUSDT"])
        self.assertEqual(analysis["analyzed"], 3)


class PulseReviewMaturityTests(unittest.TestCase):
    def test_dry_run_does_not_backfill_or_mark_review_state(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = PulseRadar(Settings(data_dir=root), JsonStore(root))
            with (
                patch("radars.pulse.radar.backfill_outcomes") as backfill,
                patch("radars.pulse.radar.send_review_replies") as replies,
            ):
                result = engine.maintain_pulse_reviews(
                    SimpleNamespace(),  # type: ignore[arg-type]
                    send=False,
                    confirm_real_send=False,
                )

        self.assertEqual(result["status"], "dry_run")
        backfill.assert_not_called()
        replies.assert_not_called()

    def test_backfill_only_fetches_mature_windows_and_does_not_repeat(self) -> None:
        class Source:
            calls: list[int] = []

            def __init__(self, *_args, **_kwargs) -> None:
                pass

            @classmethod
            def klines(cls, _symbol, **kwargs):
                cls.calls.append(int(kwargs.get("end_time") or 0))
                return [[0, "0", "0", "0", "1.1", "0"]]

            @staticmethod
            def close() -> None:
                return None

        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            signal_ts = int(time.time()) - 4000
            review_store.save_records(settings, [{
                "id": "pulse-one",
                "radar": "alert",
                "template": "health_up",
                "symbol": "ABCUSDT",
                "price": 1.0,
                "ts": signal_ts,
                "message_id": 1,
                "outcomes": {},
                "reply_sent": False,
            }])

            with patch("radars.pulse.review_store.BinanceDataSource", Source):
                review_store.backfill_outcomes(settings, now_ts=signal_ts + 4000)
                first = review_store.load_records(settings)[0]["outcomes"]
                self.assertEqual(set(first), {"3600"})
                self.assertEqual(len(Source.calls), 1)

                review_store.backfill_outcomes(settings, now_ts=signal_ts + 4000)
                self.assertEqual(len(Source.calls), 1)

                review_store.backfill_outcomes(settings, now_ts=signal_ts + 15000)
                final = review_store.load_records(settings)[0]["outcomes"]
                self.assertEqual(set(final), {"3600", "14400"})
                self.assertEqual(len(Source.calls), 2)

    def test_failed_review_reply_degrades_pulse_health(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = PulseRadar(Settings(data_dir=root), JsonStore(root))
            with (
                patch("radars.pulse.radar.backfill_outcomes", return_value=[]),
                patch(
                    "radars.pulse.radar.send_review_replies",
                    return_value=[{"status": "failed"}],
                ),
            ):
                result = engine.maintain_pulse_reviews(
                    SimpleNamespace(),  # type: ignore[arg-type]
                    send=True,
                    confirm_real_send=True,
                )

        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["failed_replies"], 1)

    def test_review_reply_waits_for_all_expected_windows(self) -> None:
        class Gateway:
            calls = 0

            @classmethod
            def send(cls, *_args, **_kwargs):
                cls.calls += 1
                return SimpleNamespace(sent=True, status="sent", reason="ok", message_ids=[2])

        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            review_store.save_records(settings, [{
                "id": "pulse-two",
                "radar": "alert",
                "template": "health_up",
                "symbol": "ABCUSDT",
                "price": 1.0,
                "ts": int(time.time()),
                "message_id": 1,
                "outcomes": {"3600": {"price": 1.1, "pct": 10.0}},
                "reply_sent": False,
            }])

            sent = review_store.send_review_replies(
                settings,
                Gateway(),  # type: ignore[arg-type]
                send=True,
                confirm_real_send=True,
            )

        self.assertEqual(sent, [])
        self.assertEqual(Gateway.calls, 0)


if __name__ == "__main__":
    unittest.main()
