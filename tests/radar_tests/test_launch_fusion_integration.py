from __future__ import annotations

import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from config import Settings
from radars.launch_warning.market_facts import INTERVAL_MS
from runtime.radar_engine import RadarEngine
from shared.storage import JsonStore


NOW_TS = 120 * 15 * 60 + 120
EXPECTED_WINDOW_END_MS = 120 * INTERVAL_MS


class _Budget:
    used = {"open_interest_hist": 0, "klines": 0, "spot_klines": 0}
    limits = {"open_interest_hist": 80, "klines": 120, "spot_klines": 120}


class _Quality:
    failures: dict[str, int] = {}


class _Source:
    budget = _Budget()
    quality = _Quality()

    def __init__(
        self,
        *,
        ticker_symbols: list[str],
        exchange_symbols: list[str],
        gap: str = "",
        legacy_short_rows: bool = False,
        active_ratios: dict[str, tuple[float, float]] | None = None,
        spot_exchange_symbols: list[str] | None = None,
        spot_catalog_available: bool = True,
    ) -> None:
        self.ticker_symbols = ticker_symbols
        self.exchange_symbols = exchange_symbols
        self.gap = gap
        self.legacy_short_rows = legacy_short_rows
        self.active_ratios = active_ratios or {}
        self.spot_exchange_symbols = (
            list(exchange_symbols)
            if spot_exchange_symbols is None
            else list(spot_exchange_symbols)
        )
        self.spot_catalog_available = spot_catalog_available
        self.kline_symbols: list[str] = []
        self.oi_symbols: list[str] = []
        self.oi_limits: list[int] = []
        self.oi_start_times: list[int] = []
        self.spot_kline_symbols: list[str] = []
        self.requested_boundaries: list[int] = []

    def usdt_perp_symbols(self) -> list[dict[str, object]]:
        return [
            {
                "symbol": symbol,
                "baseAsset": symbol.removesuffix("USDT"),
                "quoteAsset": "USDT",
                "contractType": "PERPETUAL",
                "underlyingType": "COIN",
            }
            for symbol in self.exchange_symbols
        ]

    def ticker_24h(self) -> list[dict[str, str]]:
        return [
            {
                "symbol": symbol,
                "quoteVolume": "50000000",
                "priceChangePercent": "4.0",
                "lastPrice": "104.0",
            }
            for symbol in self.ticker_symbols
        ]

    @staticmethod
    def premium_index() -> list[dict[str, object]]:
        return []

    @staticmethod
    def market_caps() -> dict[str, float]:
        return {}

    @staticmethod
    def funding_rate(*_args: object, **_kwargs: object) -> list[dict[str, object]]:
        return []

    @staticmethod
    def _kline(
        open_time_ms: int,
        close: float,
        volume: float,
        *,
        active_ratio: float = 0.4,
    ) -> list[object]:
        taker_buy_quote = volume * ((active_ratio + 1.0) / 2.0)
        return [
            open_time_ms,
            str(close - 0.5),
            str(close + 1.0),
            str(close - 1.0),
            str(close),
            "0",
            open_time_ms + INTERVAL_MS - 1,
            str(volume),
            10,
            "0",
            str(taker_buy_quote),
            "0",
        ]

    def spot_symbols(self) -> frozenset[str] | None:
        if not self.spot_catalog_available:
            return None
        return frozenset(self.spot_exchange_symbols)

    def spot_klines(
        self,
        symbol: str,
        *,
        end_time: int,
        **_kwargs: object,
    ) -> list[list[object]]:
        boundary = int(end_time) + 1
        self.spot_kline_symbols.append(symbol)
        return [
            self._kline(
                boundary - INTERVAL_MS,
                104.0,
                1_000.0,
                active_ratio=self.active_ratios.get(symbol, (0.4, 0.4))[0],
            )
        ]

    def klines(
        self,
        symbol: str,
        *,
        end_time: int,
        **_kwargs: object,
    ) -> list[list[object]]:
        boundary = int(end_time) + 1
        self.kline_symbols.append(symbol)
        self.requested_boundaries.append(boundary)
        count = 5 if self.legacy_short_rows else 17
        open_times = [
            boundary - (count - index) * INTERVAL_MS
            for index in range(count)
        ]
        if self.gap == "kline" and count == 17:
            open_times = [
                boundary - 18 * INTERVAL_MS,
                *open_times[:8],
                *open_times[9:],
            ]
        rows = [
            self._kline(
                open_time,
                104.0 if index == len(open_times) - 1 else 100.0,
                300.0 if index == len(open_times) - 1 else 100.0,
                active_ratio=self.active_ratios.get(symbol, (0.4, 0.4))[1],
            )
            for index, open_time in enumerate(open_times)
        ]
        return list(reversed(rows))

    def open_interest_hist(
        self,
        symbol: str,
        *,
        end_time: int,
        **_kwargs: object,
    ) -> list[dict[str, str]]:
        boundary = int(end_time)
        self.oi_symbols.append(symbol)
        requested_limit = int(_kwargs.get("limit", 0) or 0)
        self.oi_limits.append(requested_limit)
        self.oi_start_times.append(int(_kwargs.get("start_time", 0) or 0))
        self.requested_boundaries.append(boundary)
        count = 5 if self.legacy_short_rows else max(17, requested_limit)
        boundaries = [
            boundary - (count - 1 - index) * INTERVAL_MS
            for index in range(count)
        ]
        if self.gap == "oi" and count >= 17:
            boundaries = [
                boundary - count * INTERVAL_MS,
                *boundaries[:-9],
                *boundaries[-8:],
            ]
        rows = [
            {
                **({} if self.legacy_short_rows else {"timestamp": str(point)}),
                "sumOpenInterestValue": (
                    "1050" if index == len(boundaries) - 1 else "1000"
                ),
            }
            for index, point in enumerate(boundaries)
        ]
        return list(reversed(rows))


def _fusion_settings(root: Path, **changes: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": root,
        "launch_fusion_enable": True,
        "launch_lifecycle_v2_enable": True,
        "launch_message_package_v2_enable": True,
        "launch_scan_limit": 10,
        "radar_min_quote_volume": 0.0,
    }
    values.update(changes)
    return Settings(**values)  # type: ignore[arg-type]


def _market_contexts(
    values: dict[str, tuple[float | None, float | None]],
    *,
    window_end_ts: int = EXPECTED_WINDOW_END_MS // 1000,
):
    def load(
        _settings: object,
        symbols: list[str],
        *,
        now_ts: int | None = None,
    ) -> dict[str, dict[str, object]]:
        del now_ts
        return {
            symbol: {
                "window_end_ts": window_end_ts,
                "spot_flow_usd": 100_000.0,
                "futures_flow_usd": 200_000.0,
                "spot_active_ratio": values.get(symbol, (None, None))[0],
                "futures_active_ratio": values.get(symbol, (None, None))[1],
            }
            for symbol in symbols
        }

    return load


class LaunchFusionIntegrationTests(unittest.TestCase):
    def _run(
        self,
        root: Path,
        source: _Source,
        *,
        contexts: dict[str, tuple[float | None, float | None]] | None = None,
        context_window_end_ts: int = EXPECTED_WINDOW_END_MS // 1000,
        settings: Settings | None = None,
    ) -> tuple[dict[str, object], RadarEngine]:
        engine = RadarEngine(settings or _fusion_settings(root), JsonStore(root))
        with (
            patch("radars.launch_warning.radar.time.time", return_value=NOW_TS),
            patch(
                "shared.bot_market_context.closed_market_contexts_for_symbols",
                side_effect=_market_contexts(
                    contexts or {},
                    window_end_ts=context_window_end_ts,
                ),
            ),
        ):
            result = engine.build_launch_alerts(source)  # type: ignore[arg-type]
        return result, engine

    def test_all_symbols_use_one_closed_15m_boundary(self) -> None:
        with TemporaryDirectory() as tmp:
            source = _Source(
                ticker_symbols=["AAAUSDT", "BBBUSDT"],
                exchange_symbols=["AAAUSDT", "BBBUSDT"],
            )

            result, _engine = self._run(Path(tmp), source)

            self.assertEqual(result["watchlist_count"], 2)
            self.assertEqual(set(source.kline_symbols), {"AAAUSDT", "BBBUSDT"})
            self.assertEqual(set(source.oi_symbols), {"AAAUSDT", "BBBUSDT"})
            self.assertEqual(
                set(source.spot_kline_symbols),
                {"AAAUSDT", "BBBUSDT"},
            )
            self.assertEqual(len(source.spot_kline_symbols), 2)
            self.assertEqual(source.oi_limits, [97, 97])
            self.assertEqual(
                set(source.oi_start_times),
                {EXPECTED_WINDOW_END_MS - 96 * INTERVAL_MS},
            )
            self.assertEqual(
                set(source.requested_boundaries),
                {EXPECTED_WINDOW_END_MS},
            )

            watchlist = _engine.store.load(
                _engine.settings.launch_watchlist_path,
                {},
            )
            self.assertEqual(
                {item["oi_24h_status"] for item in watchlist["items"]},
                {"ok"},
            )
            self.assertEqual(
                {item["spot_active_status"] for item in watchlist["items"]},
                {"available"},
            )
            self.assertEqual(
                {
                    item["futures_active_status"]
                    for item in watchlist["items"]
                },
                {"available"},
            )
            self.assertEqual(
                {item["spot_active_ratio"] for item in watchlist["items"]},
                {0.4},
            )
            self.assertEqual(
                {
                    item["futures_active_ratio"]
                    for item in watchlist["items"]
                },
                {0.4},
            )

    def test_strict_kline_or_oi_gap_never_reaches_watchlist_or_lifecycle(self) -> None:
        for gap, error in (
            ("kline", "launch_market_facts_kline_gap"),
            ("oi", "launch_market_facts_oi_gap"),
        ):
            with self.subTest(gap=gap), TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = _Source(
                    ticker_symbols=["GAPUSDT"],
                    exchange_symbols=["GAPUSDT"],
                    gap=gap,
                )

                result, engine = self._run(root, source)

                self.assertEqual(result["watchlist_count"], 0)
                quality = result["diagnostics"]["lifecycle_v2"]["analysis_quality"]  # type: ignore[index]
                self.assertEqual(quality["skipped_by_reason"], {error: 1})
                conn = sqlite3.connect(engine.settings.signal_events_db_path)
                try:
                    count = conn.execute(
                        "SELECT COUNT(*) FROM launch_lifecycle_observations"
                    ).fetchone()[0]
                finally:
                    conn.close()
                self.assertEqual(count, 0)

    def test_exchange_info_limits_scan_to_confirmed_usdt_perpetuals(self) -> None:
        with TemporaryDirectory() as tmp:
            source = _Source(
                ticker_symbols=["CONFIRMEDUSDT", "ROGUEUSDT"],
                exchange_symbols=["CONFIRMEDUSDT"],
            )

            result, _engine = self._run(Path(tmp), source)

            self.assertEqual(result["watchlist_count"], 1)
            self.assertEqual(source.kline_symbols, ["CONFIRMEDUSDT"])
            self.assertEqual(source.oi_symbols, ["CONFIRMEDUSDT"])
            self.assertEqual(source.oi_limits, [97])

    def test_empty_exchange_info_keeps_only_forced_active_symbol(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = _fusion_settings(root)
            JsonStore(root).save(
                settings.launch_state_path,
                {
                    "FORCEUSDT": {
                        "stage": "watching",
                        "score": 45,
                        "last_seen": NOW_TS,
                    },
                },
            )
            source = _Source(
                ticker_symbols=["FORCEUSDT", "OTHERUSDT"],
                exchange_symbols=[],
            )

            result, _engine = self._run(root, source, settings=settings)

            self.assertEqual(result["watchlist_count"], 1)
            self.assertEqual(source.kline_symbols, ["FORCEUSDT"])

    def test_local_active_funds_support_and_counter_evidence_change_score(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _Source(
                ticker_symbols=["SUPPORTUSDT", "COUNTERUSDT"],
                exchange_symbols=["SUPPORTUSDT", "COUNTERUSDT"],
                active_ratios={
                    "SUPPORTUSDT": (0.20, 0.18),
                    "COUNTERUSDT": (-0.20, -0.18),
                },
            )

            result, engine = self._run(
                root,
                source,
                contexts={
                    "SUPPORTUSDT": (0.20, 0.18),
                    "COUNTERUSDT": (-0.20, -0.18),
                },
            )
            watchlist = engine.store.load(
                engine.settings.launch_watchlist_path,
                {},
            )
            items = {
                item["symbol"]: item
                for item in watchlist["items"]
            }
            support = items["SUPPORTUSDT"]
            counter = items["COUNTERUSDT"]

            self.assertEqual(result["watchlist_count"], 2)
            self.assertIn("spot_active_buying_met", support["supporting_evidence"])
            self.assertEqual(support["trigger_path"], "momentum")
            self.assertIn(
                "active_selling_against_move",
                counter["counter_evidence"],
            )
            self.assertEqual(counter["trigger_path"], "none")
            self.assertLessEqual(support["raw_rule_score"], 100)
            self.assertLessEqual(counter["raw_rule_score"], 100)
            self.assertGreater(support["score"], counter["score"])
            self.assertLess(counter["score"], engine.settings.launch_min_score_push)
            self.assertEqual(
                counter["policy_block_reason"],
                "no_independent_evidence_path",
            )
            self.assertNotIn(
                "COUNTERUSDT",
                {alert["symbol"] for alert in result["alerts"]},
            )

    def test_previous_window_context_cannot_override_current_direct_flow(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _Source(
                ticker_symbols=["AAAUSDT"],
                exchange_symbols=["AAAUSDT"],
                active_ratios={"AAAUSDT": (0.25, 0.20)},
            )

            _result, engine = self._run(
                root,
                source,
                contexts={"AAAUSDT": (-0.90, -0.90)},
                context_window_end_ts=(EXPECTED_WINDOW_END_MS // 1000) - 900,
            )
            watchlist = engine.store.load(
                engine.settings.launch_watchlist_path,
                {},
            )
            item = watchlist["items"][0]

            self.assertEqual(item["spot_active_ratio"], 0.25)
            self.assertEqual(item["futures_active_ratio"], 0.2)

    def test_legacy_flag_false_keeps_short_untimestamped_oi_semantics(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                launch_fusion_enable=False,
                launch_lifecycle_v2_enable=False,
                launch_message_package_v2_enable=False,
                launch_scan_limit=1,
                radar_min_quote_volume=0.0,
            )
            source = _Source(
                ticker_symbols=["LEGACYUSDT"],
                exchange_symbols=[],
                legacy_short_rows=True,
            )

            result, engine = self._run(root, source, settings=settings)
            watchlist = engine.store.load(settings.launch_watchlist_path, {})
            item = watchlist["items"][0]

            self.assertEqual(result["watchlist_count"], 1)
            self.assertAlmostEqual(item["price_15m"], (104 / 100 - 1) * 100)
            self.assertIsNone(item["price_4h"])
            self.assertEqual(item["score_semantics"], "")
            self.assertEqual(source.kline_symbols, ["LEGACYUSDT"])
            self.assertEqual(source.oi_limits, [17])

    def test_active_legacy_cycle_uses_legacy_analyzer_when_global_fusion_is_on(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = RadarEngine(_fusion_settings(root), JsonStore(root))
            source = _Source(
                ticker_symbols=["LEGACYUSDT"],
                exchange_symbols=[],
                legacy_short_rows=True,
            )
            item = {
                "symbol": "LEGACYUSDT",
                "coin": "LEGACY",
                "funding_pct": 0.0,
                "funding_next_time_ms": 0,
                "funding_available": False,
                "launch_lifecycle_active": True,
                "launch_fusion_cycle": False,
            }

            analyzed = engine._analyze_launch_symbol(source, item)  # type: ignore[arg-type]

            self.assertIsNotNone(analyzed)
            self.assertEqual(analyzed["analysis_status"], "ready")  # type: ignore[index]
            self.assertIsNone(analyzed["market_facts"])  # type: ignore[index]

    def test_active_fusion_cycle_keeps_strict_analyzer_when_global_fusion_is_off(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                launch_fusion_enable=False,
                launch_lifecycle_v2_enable=True,
                launch_message_package_v2_enable=True,
            )
            engine = RadarEngine(settings, JsonStore(root))
            source = _Source(
                ticker_symbols=["FUSIONUSDT"],
                exchange_symbols=["FUSIONUSDT"],
            )
            item = {
                "symbol": "FUSIONUSDT",
                "coin": "FUSION",
                "funding_pct": 0.0,
                "funding_next_time_ms": 0,
                "funding_available": False,
                "launch_lifecycle_active": True,
                "launch_fusion_cycle": True,
                "liquidity_tier": "中流动性",
            }

            analyzed = engine._analyze_launch_symbol(source, item)  # type: ignore[arg-type]

            self.assertIsNotNone(analyzed)
            self.assertEqual(analyzed["analysis_status"], "ready")  # type: ignore[index]
            self.assertEqual(analyzed["market_facts"]["status"], "ok")  # type: ignore[index]
            self.assertIsNotNone(analyzed["fusion_analysis"])  # type: ignore[index]

    def test_futures_only_pair_explains_missing_spot_without_fake_zero(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = RadarEngine(_fusion_settings(root), JsonStore(root))
            source = _Source(
                ticker_symbols=["FUTURESONLYUSDT"],
                exchange_symbols=["FUTURESONLYUSDT"],
                spot_exchange_symbols=[],
            )
            item = {
                "symbol": "FUTURESONLYUSDT",
                "coin": "FUTURESONLY",
                "funding_pct": 0.0,
                "funding_next_time_ms": 0,
                "funding_available": False,
                "launch_lifecycle_active": True,
                "launch_fusion_cycle": True,
                "liquidity_tier": "低流动性",
            }

            with patch(
                "radars.launch_warning.radar.time.time",
                return_value=NOW_TS,
            ):
                analyzed = engine._analyze_launch_symbol(source, item)  # type: ignore[arg-type]

            self.assertIsNotNone(analyzed)
            self.assertEqual(analyzed["spot_active_status"], "spot_pair_not_listed")  # type: ignore[index]
            self.assertIsNone(analyzed["spot_active_net_usd"])  # type: ignore[index]
            self.assertEqual(analyzed["futures_active_status"], "available")  # type: ignore[index]
            self.assertEqual(source.spot_kline_symbols, [])

    def test_spot_catalogue_outage_stops_before_per_symbol_requests(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = RadarEngine(_fusion_settings(root), JsonStore(root))
            source = _Source(
                ticker_symbols=["AAAUSDT"],
                exchange_symbols=["AAAUSDT"],
                spot_catalog_available=False,
            )

            result = engine._closed_spot_active_flow(  # type: ignore[attr-defined]
                source,
                "AAAUSDT",
                window_end_ms=EXPECTED_WINDOW_END_MS,
            )

            self.assertEqual(result["status"], "binance_unavailable")
            self.assertIsNone(result["net_usd"])
            self.assertEqual(source.spot_kline_symbols, [])

    def test_spot_budget_exhaustion_stops_before_request(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = RadarEngine(_fusion_settings(root), JsonStore(root))
            source = _Source(
                ticker_symbols=["AAAUSDT"],
                exchange_symbols=["AAAUSDT"],
            )
            source.budget = SimpleNamespace(
                used={"spot_klines": 1},
                limits={"spot_klines": 1},
            )

            result = engine._closed_spot_active_flow(  # type: ignore[attr-defined]
                source,
                "AAAUSDT",
                window_end_ms=EXPECTED_WINDOW_END_MS,
            )

            self.assertEqual(result["status"], "budget_exhausted")
            self.assertEqual(source.spot_kline_symbols, [])

    def test_futures_only_status_cannot_be_overwritten_by_local_context(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = _Source(
                ticker_symbols=["FUTURESONLYUSDT"],
                exchange_symbols=["FUTURESONLYUSDT"],
                spot_exchange_symbols=[],
            )

            _result, engine = self._run(
                root,
                source,
                contexts={"FUTURESONLYUSDT": (0.25, 0.25)},
            )
            watchlist = engine.store.load(
                engine.settings.launch_watchlist_path,
                {},
            )
            item = watchlist["items"][0]

            self.assertEqual(item["spot_active_status"], "spot_pair_not_listed")
            self.assertIsNone(item["spot_active_net_usd"])
            self.assertEqual(item["futures_active_status"], "available")


if __name__ == "__main__":
    unittest.main()
