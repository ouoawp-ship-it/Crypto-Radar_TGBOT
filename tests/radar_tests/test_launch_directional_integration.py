from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from config import Settings
from radars.launch_warning.radar import LaunchWarningRadar
from shared.binance_data import RequestBudget
from shared.storage import JsonStore
from shared.telegram import topic_intro_message, topic_intro_version


INTERVALS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}


def _rows(interval: str, limit: int, end_ms: int) -> list[list[object]]:
    step = INTERVALS[interval]
    boundary = (end_ms + 1) // step * step
    start = boundary - limit * step
    return [
        [
            start + index * step,
            str(100 + index * 0.2),
            str(101 + index * 0.2),
            str(99 + index * 0.2),
            str(100.85 + index * 0.2),
            "10",
            start + (index + 1) * step - 1,
            "1000",
            10,
            "6",
            "600",
            "0",
        ]
        for index in range(limit)
    ]


class FakeSource:
    def __init__(self) -> None:
        self.budget = RequestBudget({
            "klines": 30,
            "spot_klines": 30,
        })

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        end_time: int,
    ) -> list[list[object]]:
        del symbol
        assert self.budget.consume("klines")
        return _rows(interval, limit, end_time)

    def spot_klines(
        self,
        symbol: str,
        interval: str,
        limit: int,
        start_time: int,
        end_time: int,
    ) -> list[list[object]]:
        del symbol, start_time
        assert self.budget.consume("spot_klines")
        return _rows(interval, limit, end_time)


class FuturesOnlySource(FakeSource):
    @staticmethod
    def spot_symbols() -> set[str]:
        return set()


class FakeAiResponse:
    status_code = 200

    @staticmethod
    def json() -> dict[str, object]:
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps({
                    "status": "available",
                    "direction": "bullish",
                    "stage": "多头确认",
                    "summary": "多头规则证据占优，但仍需等回踩确认。",
                    "supporting_evidence": ["现货与合约主动买入同步"],
                    "counter_evidence": ["上方仍有结构压力"],
                    "risk_notes": ["不要追涨"],
                    "wait_for": ["回踩后保持结构"],
                    "limitations": ["规则分不是概率"],
                }, ensure_ascii=False)},
            }],
        }


class FakeAiSession:
    def __init__(self) -> None:
        self.calls = 0

    def post(self, *_args: object, **_kwargs: object) -> FakeAiResponse:
        self.calls += 1
        return FakeAiResponse()

    def close(self) -> None:
        pass


def _supportive_smc_filter() -> dict[str, object]:
    return {
        "status": "supportive",
        "ai_eligible": True,
    }


def _eligible_launch_phase() -> dict[str, object]:
    return {
        "timing_stage": "confirmed",
        "execution_status": "retest_ready",
        "position_status": "middle",
        "primary_block_reason": "none",
        "initial_alert_eligible": True,
        "ai_eligible": True,
    }


class LaunchDirectionalIntegrationTests(unittest.TestCase):
    def settings(self, **changes: object) -> Settings:
        values = {
            "launch_fusion_enable": True,
            "launch_lifecycle_v2_enable": True,
            "launch_message_package_v2_enable": True,
            "launch_directional_enable": True,
            "launch_directional_max_candidates": 6,
        }
        values.update(changes)
        return replace(Settings(), **values)

    def test_deep_analysis_uses_five_futures_and_one_spot_request(self) -> None:
        radar = LaunchWarningRadar(self.settings(), object())  # type: ignore[arg-type]
        source = FakeSource()
        monday_boundary = 4 * INTERVALS["1d"] + 35 * 7 * INTERVALS["1d"]
        item = {
            "symbol": "DEMOUSDT",
            "coin": "DEMO",
            "score": 80,
            "price_1h": 3.2,
            "oi_1h": 3.5,
            "price_24h": 8.0,
            "oi_24h": 7.0,
            "funding_available": True,
            "funding_pct": 0.01,
            "basis_pct": 0.08,
            "liquidity_tier": "中流动性",
            "asset_subclass": "altcoin",
        }

        diagnostics = radar._enrich_directional_candidates(
            source, [item], window_end_ms=monday_boundary
        )

        self.assertEqual(source.budget.used["klines"], 5)
        self.assertEqual(source.budget.used["spot_klines"], 1)
        self.assertEqual(diagnostics["network_calls"], 6)
        self.assertEqual(item["directional_analysis_status"], "ready")
        self.assertEqual(item["multi_timeframe"]["status"], "ok")
        self.assertEqual(
            item["directional_readiness"]["score_semantics"],
            "rule_readiness_not_probability",
        )
        self.assertEqual(item["discovery_score"], 80)
        self.assertIn(item["smc_filter"]["status"], {
            "supportive",
            "neutral",
            "conflicting",
            "insufficient",
        })
        self.assertEqual(item["smc_filter"]["score_adjustment"], 0)

    def test_futures_only_pair_is_visible_but_never_complete_or_ai_eligible(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
            ),
            object(),  # type: ignore[arg-type]
        )
        source = FuturesOnlySource()
        monday_boundary = 4 * INTERVALS["1d"] + 35 * 7 * INTERVALS["1d"]
        item = {
            "symbol": "FUTURESONLYUSDT",
            "coin": "FUTURESONLY",
            "score": 80,
            "price_1h": 3.2,
            "oi_1h": 3.5,
            "price_24h": 8.0,
            "oi_24h": 7.0,
            "funding_available": True,
            "funding_pct": 0.01,
            "basis_pct": 0.08,
            "liquidity_tier": "中流动性",
            "asset_subclass": "single_stock",
        }

        diagnostics = radar._enrich_directional_candidates(
            source, [item], window_end_ms=monday_boundary
        )

        signal = item["directional_readiness"]
        self.assertEqual(item["directional_analysis_status"], "ready")
        self.assertTrue(signal["observation_ready"])
        self.assertFalse(signal["data_complete"])
        self.assertEqual(
            signal["observation_mode"],
            "futures_only_spot_pair_not_listed",
        )
        self.assertEqual(source.budget.used.get("spot_klines", 0), 0)
        self.assertEqual(diagnostics["ready"], 1)

        ai_diagnostics = radar._interpret_directional_alerts([item])
        self.assertEqual(ai_diagnostics["calls"], 0)
        self.assertEqual(ai_diagnostics["status"], "no_eligible_alert")
        self.assertEqual(
            item["ai_interpretation_status"],
            "not_eligible_directional_incomplete",
        )
        self.assertEqual(item["ai_interpretation_source"], "none")

    def test_ai_disabled_makes_zero_requests_and_rule_card_still_formats(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(launch_ai_interpreter_enable=False),
            object(),  # type: ignore[arg-type]
        )
        alert = {
            "symbol": "DEMOUSDT",
            "directional_readiness": {
                "status": "多头候选",
                "direction": "bullish_candidate",
                "bullish_readiness": 70,
                "bearish_readiness": 20,
                "data_complete": True,
            },
        }

        diagnostics = radar._interpret_directional_alerts([alert])
        text = radar._format_launch_package(alert)

        self.assertEqual(diagnostics["calls"], 0)
        self.assertEqual(diagnostics["status"], "disabled")
        self.assertEqual(alert["ai_interpretation_status"], "disabled")
        self.assertIn("看涨候选｜证据增强，尚未确认", text)
        self.assertIn("AI参与</b>：未开启", text)

    def test_directional_deep_analysis_failure_cannot_fall_back_to_legacy_card(self) -> None:
        for status in ("budget_deferred", "degraded", "local_error"):
            with self.subTest(status=status):
                self.assertFalse(
                    LaunchWarningRadar._directional_candidate_publishable({
                        "launch_directional_cycle": True,
                        "directional_analysis_status": status,
                        "score": 90,
                        "trigger_path": "momentum",
                    })
                )
        self.assertFalse(
            LaunchWarningRadar._directional_candidate_publishable({
                "launch_directional_cycle": True,
                "directional_analysis_status": "ready",
                "trigger_path": "none",
            })
        )
        self.assertTrue(
            LaunchWarningRadar._directional_candidate_publishable({
                "launch_directional_cycle": True,
                "directional_analysis_status": "ready",
                "trigger_path": "directional:bearish_divergence_watch",
                "launch_phase": _eligible_launch_phase(),
            })
        )

    def test_smc_conflict_blocks_only_a_new_first_publication(self) -> None:
        first = {
            "launch_directional_cycle": True,
            "smc_filter": {"blocks_publication": True},
        }
        existing = {
            **first,
            "launch_package": {
                "enabled": True,
                "previous_published": {"observation_id": 10},
            },
        }
        stale_previous_cycle_message = {
            **first,
            "last_message_id": 99,
            "reply_to_message_id": 99,
            "launch_package": {
                "enabled": True,
                "previous_published": {},
            },
        }
        invalidated = {
            **existing,
            "directional_cycle_invalidated": {"reason": "direction_changed"},
        }
        unpublished_invalidated = {
            **stale_previous_cycle_message,
            "directional_cycle_invalidated": {"reason": "direction_changed"},
        }

        self.assertFalse(LaunchWarningRadar._smc_filter_publishable(first))
        self.assertTrue(LaunchWarningRadar._smc_filter_publishable(existing))
        self.assertFalse(
            LaunchWarningRadar._smc_filter_publishable(
                stale_previous_cycle_message
            )
        )
        self.assertTrue(LaunchWarningRadar._smc_filter_publishable(invalidated))
        self.assertFalse(
            LaunchWarningRadar._smc_filter_publishable(
                unpublished_invalidated
            )
        )

    def test_new_cycle_clears_previous_directional_invalidation_before_publish(self) -> None:
        now_ts = 2_000_000_000

        class LifecycleStore:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                pass

            @staticmethod
            def refresh_outcomes(**_kwargs: object) -> dict[str, object]:
                return {"status": "ok"}

            @staticmethod
            def list_active_symbols() -> list[str]:
                return ["DEMOUSDT"]

            @staticmethod
            def active_symbol_modes() -> dict[str, bool]:
                return {"DEMOUSDT": True}

            @staticmethod
            def active_symbol_profiles() -> dict[str, dict[str, object]]:
                return {
                    "DEMOUSDT": {
                        "directional": True,
                        "direction": "bullish",
                    }
                }

            @staticmethod
            def record_observations(
                _observations: object,
            ) -> list[dict[str, object]]:
                current = {
                    "observation_id": 22,
                    "observation_no": 1,
                    "window_end_ts": now_ts,
                    "stage": "breakout",
                    "score": 80,
                    "price": 1.0,
                    "oi_usd": 1_000_000.0,
                }
                return [{
                    "status": "opened",
                    "cycle_status": "active",
                    "cycle_no": 2,
                    "current_stage": "breakout",
                    "first_window_end": now_ts,
                    "window_end_ts": now_ts,
                    "observation_no": 1,
                    "publication": {
                        "enabled": True,
                        "publish_required": True,
                        "previous_published": {},
                        "reply_message_ids": [99],
                        "first": current,
                        "current": current,
                        "checkpoints": [],
                    },
                }]

        class Source:
            @staticmethod
            def ticker_24h() -> list[dict[str, str]]:
                return [{
                    "symbol": "DEMOUSDT",
                    "quoteVolume": "100000000",
                    "priceChangePercent": "3",
                    "lastPrice": "1",
                }]

            @staticmethod
            def market_caps() -> dict[str, float]:
                return {"DEMO": 50_000_000.0}

        def analyze(
            _source: object,
            item: dict[str, object],
            **_kwargs: object,
        ) -> dict[str, object]:
            return {
                **item,
                "score": 80,
                "closed_price": 1.0,
                "closed_oi_usd": 1_000_000.0,
                "closed_quote_volume": 10_000.0,
                "price_15m": 1.0,
                "price_1h": 2.0,
                "oi_15m": 1.0,
                "oi_1h": 3.0,
                "volume_ratio": 2.0,
                "breakout": False,
                "window_end_ts": now_ts,
            }

        def enrich(
            _source: object,
            items: list[dict[str, object]],
            **_kwargs: object,
        ) -> dict[str, object]:
            items[0].update({
                "directional_analysis_status": "ready",
                "trigger_path": "directional:bullish_candidate",
                "directional_readiness": {
                    "direction": "bullish_candidate",
                    "data_complete": True,
                },
                "smc_filter": {
                    "status": "supportive",
                    "blocks_publication": False,
                    "ai_eligible": True,
                },
                "launch_phase": _eligible_launch_phase(),
            })
            return {
                "selected": 1,
                "ready": 1,
                "degraded": 0,
                "network_calls": 0,
            }

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self.settings(
                data_dir=root,
                launch_state_path=root / "launch_state.json",
                launch_watchlist_path=root / "launch_watchlist.json",
                launch_watch_history_path=root / "launch_watch_history.jsonl",
                signal_events_db_path=root / "signals.db",
                launch_scan_limit=1,
                launch_chart_v2_enable=False,
            )
            store = JsonStore(root)
            store.save(settings.launch_state_path, {
                "DEMOUSDT": {
                    "stage": "failed",
                    "last_seen": now_ts,
                    "last_message_id": 99,
                    "directional_cycle_invalidated": {
                        "reason": "direction_changed",
                    },
                }
            })
            radar = LaunchWarningRadar(settings, store)
            with (
                patch("radars.launch_warning.radar.time.time", return_value=now_ts),
                patch("radars.launch_warning.radar.LaunchLifecycleStore", LifecycleStore),
                patch(
                    "shared.bot_market_context.closed_market_contexts_for_symbols",
                    return_value={},
                ),
                patch.object(radar, "_analyze_launch_symbol", side_effect=analyze),
                patch.object(radar, "_enrich_directional_candidates", side_effect=enrich),
                patch.object(radar, "_apply_launch_fusion_score", return_value=None),
                patch.object(radar, "_format_launch_alert", return_value="ok"),
            ):
                result = radar.build_launch_alerts(Source())  # type: ignore[arg-type]

            self.assertEqual(len(result["alerts"]), 1)
            self.assertNotIn(
                "directional_cycle_invalidated",
                result["alerts"][0],
            )
            saved = store.load(settings.launch_state_path, {})
            self.assertNotIn(
                "directional_cycle_invalidated",
                saved["DEMOUSDT"],
            )

    def test_smc_internal_error_fails_open_without_losing_directional_result(self) -> None:
        radar = LaunchWarningRadar(self.settings(), object())  # type: ignore[arg-type]
        source = FakeSource()
        monday_boundary = 4 * INTERVALS["1d"] + 35 * 7 * INTERVALS["1d"]
        item = {
            "symbol": "DEMOUSDT",
            "coin": "DEMO",
            "score": 80,
            "price_1h": 3.2,
            "oi_1h": 3.5,
            "price_24h": 8.0,
            "oi_24h": 7.0,
            "funding_available": True,
            "funding_pct": 0.01,
            "basis_pct": 0.08,
            "liquidity_tier": "中流动性",
            "asset_subclass": "altcoin",
        }

        with patch(
            "radars.launch_warning.radar.evaluate_smc_filter",
            side_effect=RuntimeError("provider-secret-detail"),
        ):
            diagnostics = radar._enrich_directional_candidates(
                source,
                [item],
                window_end_ms=monday_boundary,
            )

        self.assertEqual(item["directional_analysis_status"], "ready")
        self.assertIn("directional_readiness", item)
        self.assertEqual(item["smc_filter"]["status"], "insufficient")
        self.assertEqual(
            item["smc_filter"]["reasons"],
            ["smc_filter_local_error"],
        )
        self.assertFalse(item["smc_filter"]["blocks_publication"])
        self.assertFalse(item["smc_filter"]["ai_eligible"])
        self.assertEqual(diagnostics["network_calls"], 6)
        self.assertNotIn("provider-secret-detail", str(item))

    def test_smc_non_supportive_card_never_calls_or_reuses_ai(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
            ),
            object(),  # type: ignore[arg-type]
        )
        alert = {
            "symbol": "DEMOUSDT",
            "directional_readiness": {
                "status": "多头确认",
                "direction": "bullish",
                "data_complete": True,
            },
            "launch_phase": _eligible_launch_phase(),
            "smc_filter": {
                "status": "conflicting",
                "ai_eligible": False,
            },
            "launch_ai_interpreter_cache": {
                "key": "must-not-be-read",
                "result": {"status": "available", "summary": "旧解读"},
            },
        }

        with patch("radars.launch_warning.radar.requests.Session") as session:
            result = radar._interpret_directional_alerts([alert])

        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["cached"], 0)
        self.assertEqual(
            alert["ai_interpretation_status"],
            "not_eligible_smc_conflict",
        )
        self.assertNotIn("ai_interpretation", alert)
        session.assert_not_called()
        self.assertTrue(
            LaunchWarningRadar._directional_candidate_publishable({
                "launch_directional_cycle": False,
                "directional_analysis_status": "legacy_cycle",
                "trigger_path": "momentum",
            })
        )

    def test_missing_or_forged_smc_filter_never_calls_ai(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
            ),
            object(),  # type: ignore[arg-type]
        )
        base = {
            "symbol": "DEMOUSDT",
            "directional_readiness": {
                "status": "多头确认",
                "direction": "bullish",
                "data_complete": True,
            },
            "launch_phase": _eligible_launch_phase(),
        }
        missing = dict(base)
        forged = {
            **base,
            "smc_filter": {
                "status": "neutral",
                "ai_eligible": True,
            },
        }

        with patch("radars.launch_warning.radar.requests.Session") as session:
            result = radar._interpret_directional_alerts([missing, forged])

        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["cached"], 0)
        self.assertEqual(
            missing["ai_interpretation_status"],
            "not_eligible_smc_insufficient",
        )
        self.assertEqual(
            forged["ai_interpretation_status"],
            "not_eligible_smc_neutral",
        )
        session.assert_not_called()

    def test_non_eligible_phase_never_reads_cache_or_creates_session(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
            ),
            object(),  # type: ignore[arg-type]
        )
        directional = {
            "status": "多头确认",
            "direction": "bullish",
            "data_complete": True,
        }
        cached = {
            "key": "must-not-be-read",
            "result": {
                "status": "available",
                "summary": "旧解读不得复用",
            },
        }
        alerts = [
            {
                "symbol": "MISSINGUSDT",
                "directional_readiness": directional,
                "smc_filter": _supportive_smc_filter(),
                "launch_ai_interpreter_cache": cached,
            },
            {
                "symbol": "EXTENDEDUSDT",
                "directional_readiness": directional,
                "launch_phase": {
                    "timing_stage": "extended_no_chase",
                    "execution_status": "blocked_extension",
                    "ai_eligible": False,
                },
                "smc_filter": _supportive_smc_filter(),
                "launch_ai_interpreter_cache": cached,
            },
            {
                "symbol": "INSUFFICIENTUSDT",
                "directional_readiness": directional,
                "launch_phase": {
                    "timing_stage": "insufficient",
                    "execution_status": "blocked_data",
                    "ai_eligible": False,
                },
                "smc_filter": _supportive_smc_filter(),
                "launch_ai_interpreter_cache": cached,
            },
            {
                "symbol": "LOWVOLUMEUSDT",
                "directional_readiness": directional,
                "launch_phase": {
                    "timing_stage": "forming",
                    "execution_status": "blocked_volume",
                    "ai_eligible": False,
                },
                "smc_filter": _supportive_smc_filter(),
                "launch_ai_interpreter_cache": cached,
            },
        ]

        with patch("radars.launch_warning.radar.requests.Session") as session:
            result = radar._interpret_directional_alerts(alerts)

        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["cached"], 0)
        self.assertEqual(result["eligible"], 0)
        self.assertEqual(result["status"], "no_eligible_alert")
        self.assertEqual(
            [item["ai_interpretation_status"] for item in alerts],
            [
                "not_eligible_phase_missing",
                "not_eligible_phase_extended",
                "not_eligible_phase_insufficient",
                "not_eligible_phase_low_volume",
            ],
        )
        for item in alerts:
            self.assertNotIn("ai_interpretation", item)
        session.assert_not_called()

    def test_watch_record_persists_only_safe_phase_summary(self) -> None:
        item = {
            "symbol": "DEMOUSDT",
            "coin": "DEMO",
            "score": 72,
            "closed_price": 1.0,
            "closed_oi_usd": 2_000_000.0,
            "closed_quote_volume": 3_000_000.0,
            "price_15m": 1.5,
            "price_1h": 3.0,
            "oi_15m": 2.0,
            "oi_1h": 4.0,
            "volume_ratio": 1.2,
            "breakout": True,
            "quote_volume": 4_000_000.0,
            "mcap": 20_000_000.0,
            "launch_phase": {
                "timing_stage": "extended_no_chase",
                "execution_status": "blocked_extension",
                "position_status": "high_extended",
                "primary_block_reason": "bullish_72h_high_extended",
                "one_hour_rows": [["must-not-persist"]],
                "reason_codes": ["must-not-persist"],
                "provider_error": "secret-provider-error-body",
            },
        }

        record = LaunchWarningRadar._launch_watch_record(item, 123)

        self.assertEqual(record["timing_stage"], "extended_no_chase")
        self.assertEqual(record["execution_status"], "blocked_extension")
        self.assertEqual(record["position_status"], "high_extended")
        self.assertEqual(
            record["primary_block_reason"],
            "bullish_72h_high_extended",
        )
        self.assertNotIn("launch_phase", record)
        self.assertNotIn("one_hour_rows", record)
        self.assertNotIn("reason_codes", record)
        self.assertNotIn("provider_error", record)
        self.assertNotIn("secret-provider-error-body", str(record))

    def test_directional_topic_intro_replaces_old_fusion_intro(self) -> None:
        settings = self.settings()

        text = topic_intro_message("TG_LAUNCH_ALERT", settings)

        self.assertIn("AI只把已计算的数据和规则翻译成白话", text)
        self.assertIn("看涨、看跌、风险和数据可靠度分开显示", text)
        self.assertIn("1小时现货/合约主动买卖", text)
        self.assertIn("launch", topic_intro_version("TG_LAUNCH_ALERT", settings))

    def test_ai_interpreter_runs_once_only_for_publishable_directional_alert(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
            ),
            object(),  # type: ignore[arg-type]
        )
        alert = {
            "symbol": "DEMOUSDT",
            "directional_readiness": {
                "status": "多头确认",
                "direction": "bullish",
                "bullish_readiness": 82,
                "bearish_readiness": 15,
                "data_complete": True,
            },
            "launch_phase": _eligible_launch_phase(),
            "smc_filter": _supportive_smc_filter(),
        }
        session = FakeAiSession()

        with patch(
            "radars.launch_warning.radar.requests.Session",
            return_value=session,
        ):
            diagnostics = radar._interpret_directional_alerts([alert])

        self.assertEqual(session.calls, 1)
        self.assertEqual(diagnostics["calls"], 1)
        self.assertEqual(diagnostics["available"], 1)
        self.assertEqual(alert["ai_interpretation_status"], "available")
        self.assertEqual(alert["ai_interpretation_source"], "provider")
        self.assertIn("风险：不要追涨", alert["ai_interpretation"])
        self.assertIn("等待：回踩后保持结构", alert["ai_interpretation"])


    def test_same_observation_uses_safe_ai_cache_without_second_request(self) -> None:
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "launch_state.json"
            settings = self.settings(
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
                launch_state_path=state_path,
            )
            store = JsonStore(Path(tmp))
            store.save(state_path, {"DEMOUSDT": {"symbol": "DEMOUSDT"}})
            radar = LaunchWarningRadar(settings, store)
            alert = {
                "symbol": "DEMOUSDT",
                "launch_lifecycle": {"observation_id": 77},
                "directional_readiness": {
                    "status": "多头确认",
                    "direction": "bullish",
                    "bullish_readiness": 82,
                    "bearish_readiness": 15,
                    "data_complete": True,
                },
                "launch_phase": _eligible_launch_phase(),
                "smc_filter": _supportive_smc_filter(),
            }
            first_session = FakeAiSession()
            with patch(
                "radars.launch_warning.radar.requests.Session",
                return_value=first_session,
            ):
                first = radar._interpret_directional_alerts([alert])

            cached_record = store.load(state_path, {})["DEMOUSDT"]
            retry_alert = {
                **alert,
                **cached_record,
                "launch_lifecycle": {"observation_id": 77},
            }
            second_session = FakeAiSession()
            with patch(
                "radars.launch_warning.radar.requests.Session",
                return_value=second_session,
            ):
                second = radar._interpret_directional_alerts([retry_alert])

            self.assertEqual(first["calls"], 1)
            self.assertEqual(first_session.calls, 1)
            self.assertEqual(second["calls"], 0)
            self.assertEqual(second["cached"], 1)
            self.assertEqual(second_session.calls, 0)
            self.assertEqual(retry_alert["ai_interpretation_source"], "cache")
            self.assertNotIn("reasoning_content", cached_record)

    def test_ai_interpreter_makes_at_most_one_request_per_cycle(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
            ),
            object(),  # type: ignore[arg-type]
        )
        alerts = [
            {
                "symbol": symbol,
                "directional_readiness": {
                    "status": "多头确认",
                    "direction": "bullish",
                    "bullish_readiness": 82,
                    "bearish_readiness": 15,
                    "data_complete": True,
                },
                "launch_phase": _eligible_launch_phase(),
                "smc_filter": _supportive_smc_filter(),
            }
            for symbol in ("ONEUSDT", "TWOUSDT")
        ]
        session = FakeAiSession()
        with patch(
            "radars.launch_warning.radar.requests.Session",
            return_value=session,
        ):
            result = radar._interpret_directional_alerts(alerts)

        self.assertEqual(session.calls, 1)
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["eligible"], 2)
        self.assertEqual(result["deferred"], 1)
        self.assertEqual(alerts[0]["ai_interpretation_status"], "available")
        self.assertEqual(
            alerts[1]["ai_interpretation_status"],
            "deferred_cycle_limit",
        )

    def test_enabled_but_missing_configuration_is_visible_without_network(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(launch_ai_interpreter_enable=True),
            object(),  # type: ignore[arg-type]
        )
        alert = {
            "symbol": "DEMOUSDT",
            "directional_readiness": {
                "status": "多头候选",
                "direction": "bullish_candidate",
                "data_complete": True,
            },
            "launch_phase": _eligible_launch_phase(),
            "smc_filter": _supportive_smc_filter(),
        }

        with patch("radars.launch_warning.radar.requests.Session") as session:
            result = radar._interpret_directional_alerts([alert])

        self.assertEqual(result["calls"], 0)
        self.assertEqual(alert["ai_interpretation_status"], "not_configured")
        self.assertEqual(alert["ai_interpretation_source"], "none")
        session.assert_not_called()

    def test_cached_first_alert_does_not_starve_next_uncached_alert(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
            ),
            object(),  # type: ignore[arg-type]
        )
        first = {
            "symbol": "ONEUSDT",
            "launch_lifecycle": {"observation_id": 11},
            "directional_readiness": {
                "status": "多头确认",
                "direction": "bullish",
                "bullish_readiness": 82,
                "bearish_readiness": 15,
                "data_complete": True,
            },
            "launch_phase": _eligible_launch_phase(),
            "smc_filter": _supportive_smc_filter(),
        }
        first["launch_ai_interpreter_cache"] = {
            "key": radar._directional_ai_cache_key(
                first,
                model="fake-model",
                base_url="https://provider.invalid/v1",
            ),
            "result": {
                "status": "available",
                "direction": "bullish",
                "stage": "多头确认",
                "summary": "规则证据偏多，仍需等待结构确认。",
                "supporting_evidence": ["主动买入与结构方向一致"],
                "counter_evidence": ["上方仍有压力"],
                "risk_notes": ["避免追涨"],
                "wait_for": ["等待回踩保持结构"],
                "limitations": ["规则分不是概率"],
            },
        }
        second = {
            "symbol": "TWOUSDT",
            "launch_lifecycle": {"observation_id": 12},
            "directional_readiness": {
                "status": "多头确认",
                "direction": "bullish",
                "bullish_readiness": 82,
                "bearish_readiness": 15,
                "data_complete": True,
            },
            "launch_phase": _eligible_launch_phase(),
            "smc_filter": _supportive_smc_filter(),
        }
        session = FakeAiSession()

        with patch(
            "radars.launch_warning.radar.requests.Session",
            return_value=session,
        ):
            result = radar._interpret_directional_alerts([first, second])

        self.assertEqual(result["cached"], 1)
        self.assertEqual(result["calls"], 1)
        self.assertEqual(result["deferred"], 0)
        self.assertEqual(session.calls, 1)
        self.assertIn("ai_interpretation", first)
        self.assertIn("ai_interpretation", second)

    def test_ai_cache_changes_when_endpoint_or_operator_prompt_changes(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(),
            object(),  # type: ignore[arg-type]
        )
        alert = {
            "launch_lifecycle": {"observation_id": 11},
        }
        original = radar._directional_ai_cache_key(
            alert,
            model="same-model",
            base_url="https://provider-one.invalid/v1",
            operator_prompt="简洁",
        )
        endpoint_changed = radar._directional_ai_cache_key(
            alert,
            model="same-model",
            base_url="https://provider-two.invalid/v1",
            operator_prompt="简洁",
        )
        prompt_changed = radar._directional_ai_cache_key(
            alert,
            model="same-model",
            base_url="https://provider-one.invalid/v1",
            operator_prompt="详细",
        )

        self.assertNotEqual(original, endpoint_changed)
        self.assertNotEqual(original, prompt_changed)

    def test_previous_success_cache_is_reused_but_previous_truncation_retries_once(self) -> None:
        radar = LaunchWarningRadar(
            self.settings(
                launch_ai_interpreter_enable=True,
                ai_api_key="fake-key",
                ai_base_url="https://provider.invalid/v1",
                ai_model="fake-model",
            ),
            object(),  # type: ignore[arg-type]
        )

        def alert_with(status: str) -> dict[str, object]:
            alert: dict[str, object] = {
                "symbol": f"{status.upper()}USDT",
                "launch_lifecycle": {"observation_id": 91},
                "directional_readiness": {
                    "status": "多头确认",
                    "direction": "bullish",
                    "data_complete": True,
                },
                "launch_phase": _eligible_launch_phase(),
                "smc_filter": _supportive_smc_filter(),
            }
            result: dict[str, object] = {
                "status": status,
                "direction": "bullish",
                "stage": "多头确认",
                "summary": "规则证据偏多，仍需等待结构确认。" if status == "available" else "",
                "supporting_evidence": [],
                "counter_evidence": [],
                "risk_notes": [],
                "wait_for": [],
                "limitations": [],
            }
            alert["launch_ai_interpreter_cache"] = {
                "key": radar._directional_ai_cache_key(
                    alert,
                    model="fake-model",
                    base_url="https://provider.invalid/v1",
                    cache_version=radar._PREVIOUS_AI_CACHE_VERSION,
                ),
                "result": result,
            }
            return alert

        previous_success = alert_with("available")
        success_session = FakeAiSession()
        with patch(
            "radars.launch_warning.radar.requests.Session",
            return_value=success_session,
        ):
            reused = radar._interpret_directional_alerts([previous_success])

        self.assertEqual(reused["calls"], 0)
        self.assertEqual(reused["cached"], 1)
        self.assertEqual(success_session.calls, 0)
        self.assertEqual(previous_success["ai_interpretation_source"], "cache")

        previous_truncation = alert_with("ai_output_truncated")
        retry_session = FakeAiSession()
        with patch(
            "radars.launch_warning.radar.requests.Session",
            return_value=retry_session,
        ):
            retried = radar._interpret_directional_alerts([previous_truncation])

        self.assertEqual(retried["calls"], 1)
        self.assertEqual(retried["cached"], 0)
        self.assertEqual(retry_session.calls, 1)
        self.assertEqual(previous_truncation["ai_interpretation_status"], "available")

        previous_timeout = alert_with("ai_timeout")
        timeout_session = FakeAiSession()
        with patch(
            "radars.launch_warning.radar.requests.Session",
            return_value=timeout_session,
        ):
            timeout_reused = radar._interpret_directional_alerts([previous_timeout])

        self.assertEqual(timeout_reused["calls"], 0)
        self.assertEqual(timeout_reused["cached"], 1)
        self.assertEqual(timeout_session.calls, 0)
        self.assertEqual(previous_timeout["ai_interpretation_status"], "ai_timeout")


if __name__ == "__main__":
    unittest.main()
