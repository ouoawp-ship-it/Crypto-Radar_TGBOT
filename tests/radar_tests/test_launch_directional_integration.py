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
        self.assertIn("看涨候选｜证据增强，尚未确认", text)

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
            })
        )
        self.assertTrue(
            LaunchWarningRadar._directional_candidate_publishable({
                "launch_directional_cycle": False,
                "directional_analysis_status": "legacy_cycle",
                "trigger_path": "momentum",
            })
        )

    def test_directional_topic_intro_replaces_old_fusion_intro(self) -> None:
        settings = self.settings()

        text = topic_intro_message("TG_LAUNCH_ALERT", settings)

        self.assertIn("AI只把已计算的数据和规则翻译成白话", text)
        self.assertIn("看涨、看跌、风险和数据可靠度分开显示", text)
        self.assertIn("1小时现货/合约主动买卖", text)
        self.assertIn("directional", topic_intro_version("TG_LAUNCH_ALERT", settings))

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


if __name__ == "__main__":
    unittest.main()
