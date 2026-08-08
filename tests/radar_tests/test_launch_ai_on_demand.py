import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from radars.launch_warning.ai_on_demand import (
    LaunchAiOnDemandService,
    build_launch_ai_deep_link,
    format_on_demand_ai_result,
    positive_telegram_user_id,
)
from shared.signal_store import SignalEventStore, signal_public_ref


PUBLIC_REF = "sig_0123456789abcdefabcd"


def available_result() -> dict[str, object]:
    return {
        "status": "available",
        "direction": "bullish",
        "stage": "forming",
        "summary": "规则证据偏多，但当前位置仍需等待确认。",
        "supporting_evidence": ["价格与持仓方向一致"],
        "counter_evidence": ["高周期仍有压力"],
        "risk_notes": ["不要追涨"],
        "wait_for": ["等待回踩后结构保持"],
        "limitations": ["证据分不是概率"],
    }


class FakeResponse:
    status_code = 200

    def json(self) -> dict[str, object]:
        import json

        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"content": json.dumps(available_result(), ensure_ascii=False)},
            }]
        }


class FakeSession:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.calls = 0
        self.last_kwargs: dict[str, object] = {}

    def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls += 1
        self.last_kwargs = dict(kwargs)
        if self.error is not None:
            raise self.error
        return FakeResponse()


class FakeStore:
    def __init__(self, reservation_status: str = "reserved") -> None:
        self.reservation_status = reservation_status
        self.reserve_kwargs: dict[str, object] = {}
        self.success_calls = 0
        self.failure_codes: list[str] = []
        self.loaded = {
            "status": "ready",
            "captured_at": 1_000,
            "signal_ts": 1_000,
            "symbol": "DEMOUSDT",
            "snapshot": {
                "discovery_score": 72,
                "price_open_interest": {"price_1h_pct": 3.5},
                "active_flow": {"spot_active_ratio": 12.0},
                "funding_basis": {"funding_rate_pct": -0.01},
                "launch_phase": {
                    "timing_stage": "forming",
                    "execution_status": "wait_confirmation",
                },
                "multi_timeframe": {},
                "structure": {},
                "plan": {},
                "completeness": {},
                "rule_result": {
                    "direction": "bullish",
                    "stage": "forming",
                    "status": "多头候选",
                    "data_complete": True,
                }
            },
        }

    def load_ai_context_snapshot(self, public_ref: str) -> dict[str, object]:
        return dict(self.loaded)

    def reserve_ai_interpretation(self, public_ref: str, **kwargs):  # type: ignore[no-untyped-def]
        self.reserve_kwargs = dict(kwargs)
        if self.reservation_status == "available":
            return {"status": "available", "result": available_result()}
        if self.reservation_status != "reserved":
            return {"status": self.reservation_status, "error_code": "ai_timeout"}
        return {
            "status": "reserved",
            "cache_key": "aic_" + "1" * 64,
            "lease_id": "2" * 32,
            "snapshot": dict(self.loaded["snapshot"]),
        }

    def cache_ai_success(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.success_calls += 1
        return {"status": "available", "stored": True}

    def cache_ai_failure(self, cache_key: str, lease_id: str, error_code: str, **kwargs):  # type: ignore[no-untyped-def]
        self.failure_codes.append(error_code)
        return {"status": "cooldown", "stored": True}

    def signal_detail(self, public_ref: str, *, compact: bool = False):  # type: ignore[no-untyped-def]
        return {"symbol": "DEMOUSDT", "ts": 1_000}


def enabled_settings(**overrides: object) -> Settings:
    values = {
        "launch_ai_interpreter_enable": True,
        "launch_ai_auto_enable": False,
        "ai_api_key": "fake-private-key",
        "ai_base_url": "https://provider.invalid/v1",
        "ai_model": "fake-model",
        "ai_timeout_sec": 60,
        "ai_on_demand_daily_limit": 7,
    }
    values.update(overrides)
    return Settings(**values)


class LaunchAiOnDemandTests(unittest.TestCase):
    def test_telegram_admin_id_is_positive_bounded_and_never_raises(self) -> None:
        maximum = 9_223_372_036_854_775_807
        for value, expected in (
            (1, 1),
            ("123456789", 123456789),
            (str(maximum), maximum),
            (None, None),
            (True, None),
            (0, None),
            ("001", None),
            ("-1", None),
            ("1٢", None),
            (str(maximum + 1), None),
            ("9" * 5_000, None),
            ("not-a-user-id", None),
        ):
            with self.subTest(value=str(value)[:24]):
                self.assertEqual(positive_telegram_user_id(value), expected)

    def test_deep_link_contains_only_bot_username_and_opaque_reference(self) -> None:
        link = build_launch_ai_deep_link("VIPpao_bot", PUBLIC_REF)
        self.assertEqual(
            link,
            f"https://t.me/VIPpao_bot?start=ai_{PUBLIC_REF}",
        )
        self.assertEqual(build_launch_ai_deep_link("bad name", PUBLIC_REF), "")
        self.assertEqual(build_launch_ai_deep_link("VIPpao_bot", "12"), "")

    def test_disabled_and_missing_configuration_never_touch_provider(self) -> None:
        for settings, expected in (
            (enabled_settings(launch_ai_interpreter_enable=False), "disabled"),
            (enabled_settings(ai_api_key=""), "not_configured"),
        ):
            with self.subTest(expected=expected):
                session = FakeSession()
                result = LaunchAiOnDemandService(
                    settings_reader=lambda settings=settings: settings,
                    signal_store=FakeStore(),  # type: ignore[arg-type]
                    session=session,
                    clock=lambda: 1_100,
                ).request(PUBLIC_REF)
                self.assertEqual(result["status"], expected)
                self.assertEqual(session.calls, 0)

    def test_missing_and_expired_snapshots_do_not_call_provider(self) -> None:
        missing = FakeStore()
        missing.loaded = {"status": "snapshot_missing"}
        expired = FakeStore()
        expired.loaded["captured_at"] = 1
        for store, now, expected in (
            (missing, 1_100, "not_found"),
            (expired, 8 * 24 * 3600, "expired"),
        ):
            with self.subTest(expected=expected):
                session = FakeSession()
                result = LaunchAiOnDemandService(
                    settings_reader=enabled_settings,
                    signal_store=store,  # type: ignore[arg-type]
                    session=session,
                    clock=lambda now=now: now,
                ).request(PUBLIC_REF)
                self.assertEqual(result["status"], expected)
                self.assertEqual(session.calls, 0)

    def test_provider_success_is_cached_and_rendered_as_explicit_ai_text(self) -> None:
        store = FakeStore()
        session = FakeSession()
        result = LaunchAiOnDemandService(
            settings_reader=enabled_settings,
            signal_store=store,  # type: ignore[arg-type]
            session=session,
            clock=lambda: 1_100,
        ).request(PUBLIC_REF)

        self.assertEqual(result["status"], "completed")
        self.assertEqual(session.calls, 1)
        self.assertEqual(store.success_calls, 1)
        self.assertEqual(store.reserve_kwargs["daily_limit"], 7)
        self.assertIn("AI按需解读 · DEMOUSDT", result["text"])
        self.assertIn("AI观点", result["text"])
        self.assertIn("AI只负责解释", result["text"])
        request_context = session.last_kwargs["json"]["messages"][1]["content"]
        self.assertIn('"price_1h_pct":3.5', request_context)
        self.assertIn('"spot_active_ratio":12.0', request_context)
        self.assertIn('"funding_rate_pct":-0.01', request_context)

    def test_cached_result_and_inflight_do_not_call_provider(self) -> None:
        for store, expected in (
            (FakeStore("available"), "cached"),
            (FakeStore("in_flight"), "processing"),
            (FakeStore("quota_exhausted"), "quota_exhausted"),
        ):
            with self.subTest(expected=expected):
                session = FakeSession()
                result = LaunchAiOnDemandService(
                    settings_reader=enabled_settings,
                    signal_store=store,  # type: ignore[arg-type]
                    session=session,
                    clock=lambda: 1_100,
                ).request(PUBLIC_REF)
                self.assertEqual(result["status"], expected)
                self.assertEqual(session.calls, 0)

    def test_provider_timeout_enters_short_cooldown_without_retry(self) -> None:
        store = FakeStore()
        session = FakeSession(error=TimeoutError("secret provider detail"))
        result = LaunchAiOnDemandService(
            settings_reader=enabled_settings,
            signal_store=store,  # type: ignore[arg-type]
            session=session,
            clock=lambda: 1_100,
        ).request(PUBLIC_REF)

        self.assertEqual(result, {"status": "timeout"})
        self.assertEqual(session.calls, 1)
        self.assertEqual(store.failure_codes, ["ai_timeout"])
        self.assertNotIn("secret provider detail", str(result))

    def test_slow_timeout_starts_cooldown_when_provider_finishes(self) -> None:
        class MutableClock:
            value = 1_100

            def __call__(self) -> int:
                return self.value

        class SlowTimeoutSession:
            def __init__(self, clock: MutableClock) -> None:
                self.clock = clock
                self.calls = 0

            def post(self, *args, **kwargs):  # type: ignore[no-untyped-def]
                self.calls += 1
                self.clock.value += 61
                raise TimeoutError("private timeout detail")

        with TemporaryDirectory() as tmp:
            store = SignalEventStore(Path(tmp) / "signals.db")
            dedup_key = "launch-package:7:slow-timeout"
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key=dedup_key,
                status="sent",
                sent=True,
                text="TESTUSDT 启动预警",
                ts=1_000,
                structured_records=[{
                    "symbol": "TESTUSDT",
                    "stage": "forming",
                    "ai_context_snapshot": dict(FakeStore().loaded["snapshot"]),
                }],
            )
            public_ref = signal_public_ref(dedup_key, "TESTUSDT")
            clock = MutableClock()
            session = SlowTimeoutSession(clock)
            service = LaunchAiOnDemandService(
                settings_reader=enabled_settings,
                signal_store=store,
                session=session,
                clock=clock,
            )

            first = service.request(public_ref)
            second = service.request(public_ref)

        self.assertEqual(first, {"status": "timeout"})
        self.assertEqual(second, {"status": "timeout"})
        self.assertEqual(session.calls, 1)

    def test_formatter_keeps_ai_and_rule_text_visibly_separate(self) -> None:
        text = format_on_demand_ai_result(
            symbol="TSTUSDT",
            signal_ts=1_000,
            result=available_result(),
        )
        self.assertIn("🤖 AI按需解读", text)
        self.assertIn("🧭 原规则", text)
        self.assertIn("💬 AI观点", text)
        self.assertLessEqual(len(text), 3400)

    def test_real_sent_snapshot_is_interpreted_once_then_served_from_cache(self) -> None:
        with TemporaryDirectory() as tmp:
            store = SignalEventStore(Path(tmp) / "signals.db")
            dedup_key = "launch-package:7:12"
            snapshot = dict(FakeStore().loaded["snapshot"])
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key=dedup_key,
                status="sent",
                sent=True,
                text="TESTUSDT 启动预警",
                ts=1_000,
                structured_records=[{
                    "symbol": "TESTUSDT",
                    "stage": "forming",
                    "ai_context_snapshot": snapshot,
                }],
            )
            public_ref = signal_public_ref(dedup_key, "TESTUSDT")
            session = FakeSession()
            service = LaunchAiOnDemandService(
                settings_reader=enabled_settings,
                signal_store=store,
                session=session,
                clock=lambda: 1_100,
            )

            first = service.request(public_ref)
            second = service.request(public_ref)

            self.assertEqual(first["status"], "completed")
            self.assertEqual(second["status"], "cached")
            self.assertEqual(session.calls, 1)
            self.assertEqual(
                store.ai_daily_quota(now_ts=1_100, daily_limit=7)[
                    "provider_reserved"
                ],
                1,
            )
            self.assertEqual(
                [
                    item["event"]
                    for item in store.list_ai_interpretation_audit(public_ref)
                ],
                ["cache_hit", "completed", "reserved"],
            )


if __name__ == "__main__":
    unittest.main()
