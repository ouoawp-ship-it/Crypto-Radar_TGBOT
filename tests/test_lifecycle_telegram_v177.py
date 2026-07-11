from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from paopao_radar.config import Settings
from paopao_radar.lifecycle_engine import (
    LIFECYCLE_TELEGRAM_EVENT_COOLDOWN_SEC,
    LIFECYCLE_TELEGRAM_GLOBAL_HOURLY_LIMIT,
    LIFECYCLE_TELEGRAM_SYMBOL_HOURLY_LIMIT,
    LifecycleEngine,
    _lifecycle_telegram_dedup_key,
    build_lifecycle_telegram_message,
    push_lifecycle_event,
)
from paopao_radar.lifecycle_store import public_lifecycle_redact
from paopao_radar.telegram import PushResult


def settings_for(tmp: str, **overrides: Any) -> Settings:
    root = Path(tmp)
    values: dict[str, Any] = {
        "data_dir": root,
        "signal_events_db_path": root / "signals.db",
        "lifecycle_db_path": root / "lifecycle.db",
        "tg_push_history_path": root / "push-history.json",
        "lifecycle_telegram_enable": True,
        "lifecycle_telegram_min_score": 60,
        "lifecycle_telegram_min_event_interval_sec": 3600,
        "tg_global_hourly_limit": 100,
    }
    values.update(overrides)
    return Settings(**values)


def lifecycle() -> dict[str, Any]:
    return {
        "symbol": "BTCUSDT",
        "first_signal_level": "15m",
        "first_signal_at": "2026-07-10T00:00:00+00:00",
        "current_state": "upgraded_1h",
    }


def event(**overrides: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "id": 7,
        "symbol": "BTCUSDT",
        "event_type": "timeframe_upgrade_1h",
        "event_level": "1h",
        "event_time": "2026-07-10T01:00:00+00:00",
        "new_state": "upgraded_1h",
        "event_score": 80,
        "risk_score": 20,
        "metrics": {
            "price_change_from_first_pct": 6.2,
            "volume_multiplier": 2.8,
            "oi_change_from_first_pct": 12.5,
            "futures_cvd_delta": 5,
            "spot_cvd_delta": 4,
            "funding_rate": 0.00012,
        },
        "reasons": ["15m 启动后升级到 1h，资金跟随较完整。"],
    }
    value.update(overrides)
    return value


class FakeGateway:
    def __init__(self, *, history: list[dict[str, Any]] | None = None, sent: bool = True):
        self.history = history or []
        self.sent = sent
        self.calls: list[dict[str, Any]] = []

    def _load_history(self) -> list[dict[str, Any]]:
        return self.history

    def send(self, text: str, template_id: str, dedup_key: str, **kwargs: Any) -> PushResult:
        self.calls.append({"text": text, "template_id": template_id, "dedup_key": dedup_key, **kwargs})
        return PushResult("sent" if self.sent else "dry_run", "test", self.sent)


class LifecycleTelegramV177Tests(unittest.TestCase):
    def test_default_is_off_and_disabled_push_never_calls_gateway(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp, lifecycle_telegram_enable=False)
            gateway = FakeGateway()
            pushed = push_lifecycle_event(
                settings=settings,
                lifecycle=lifecycle(),
                event=event(),
                send=True,
                gateway=gateway,  # type: ignore[arg-type]
            )

        self.assertFalse(Settings().lifecycle_telegram_enable)
        self.assertFalse(pushed)
        self.assertEqual(gateway.calls, [])

    def test_message_has_binance_metrics_risk_and_exact_disclaimer(self) -> None:
        message = build_lifecycle_telegram_message(
            lifecycle(),
            event(reasons=["资金跟随较完整。", "如果 OI 增加但价格不涨，存在拥挤风险。"]),
        )

        self.assertIn("Binance 跟随：", message)
        self.assertIn("资金费率：+0.0120%", message)
        self.assertIn("风险：", message)
        self.assertIn("仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。", message)
        self.assertNotIn("chat_id", message)

    def test_stable_key_and_four_hour_cooldown_are_used(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway = FakeGateway(sent=True)
            pushed = push_lifecycle_event(
                settings=settings_for(tmp),
                lifecycle=lifecycle(),
                event=event(dedup_key="database-private-key"),
                send=True,
                gateway=gateway,  # type: ignore[arg-type]
            )

        self.assertTrue(pushed)
        self.assertEqual(_lifecycle_telegram_dedup_key(event()), "lifecycle:BTCUSDT:timeframe_upgrade_1h:1h")
        self.assertEqual(gateway.calls[0]["dedup_key"], "lifecycle:BTCUSDT:timeframe_upgrade_1h:1h")
        self.assertGreaterEqual(gateway.calls[0]["cooldown_sec"], LIFECYCLE_TELEGRAM_EVENT_COOLDOWN_SEC)

    def test_dry_run_result_is_not_reported_as_pushed(self) -> None:
        with TemporaryDirectory() as tmp:
            gateway = FakeGateway(sent=False)
            pushed = push_lifecycle_event(
                settings=settings_for(tmp),
                lifecycle=lifecycle(),
                event=event(),
                send=False,
                gateway=gateway,  # type: ignore[arg-type]
            )

        self.assertFalse(pushed)
        self.assertEqual(len(gateway.calls), 1)
        self.assertFalse(gateway.calls[0]["send"])

    def test_per_symbol_hourly_limit_blocks_third_message(self) -> None:
        now = int(time.time())
        history = [
            {
                "ts": now - index,
                "template_id": "TG_LIFECYCLE_FOLLOWUP",
                "status": "sent",
                "dedup_key": f"lifecycle:BTCUSDT:event-{index}:15m",
            }
            for index in range(LIFECYCLE_TELEGRAM_SYMBOL_HOURLY_LIMIT)
        ]
        with TemporaryDirectory() as tmp:
            gateway = FakeGateway(history=history)
            pushed = push_lifecycle_event(
                settings=settings_for(tmp),
                lifecycle=lifecycle(),
                event=event(),
                send=True,
                gateway=gateway,  # type: ignore[arg-type]
            )

        self.assertFalse(pushed)
        self.assertEqual(gateway.calls, [])

    def test_global_hourly_limit_blocks_message(self) -> None:
        now = int(time.time())
        history = [
            {
                "ts": now - index,
                "template_id": "TG_LIFECYCLE_FOLLOWUP",
                "status": "sent",
                "dedup_key": f"lifecycle:COIN{index}USDT:first_signal:15m",
            }
            for index in range(LIFECYCLE_TELEGRAM_GLOBAL_HOURLY_LIMIT)
        ]
        with TemporaryDirectory() as tmp:
            gateway = FakeGateway(history=history)
            pushed = push_lifecycle_event(
                settings=settings_for(tmp),
                lifecycle=lifecycle(),
                event=event(),
                send=True,
                gateway=gateway,  # type: ignore[arg-type]
            )

        self.assertFalse(pushed)
        self.assertEqual(gateway.calls, [])

    def test_public_recursive_redactor_drops_private_nested_keys(self) -> None:
        value = public_lifecycle_redact(
            {
                "metrics": {
                    "safe": 1,
                    "nested": {
                        "chat_id": 123,
                        "topic_id": 42,
                        "dedup_key": "private",
                        "payload_json": "private",
                        "server_path": "/home/ubuntu/private",
                    },
                }
            }
        )

        text = str(value)
        self.assertEqual(value["metrics"]["safe"], 1)
        for forbidden in ("chat_id", "topic_id", "dedup_key", "payload_json", "/home/ubuntu"):
            self.assertNotIn(forbidden, text)

    def test_oi_price_divergence_emits_risk_warning_event(self) -> None:
        def metrics(price: float, oi: float) -> dict[str, Any]:
            return {
                "symbol": "BTCUSDT",
                "timeframe": "15m",
                "price": price,
                "volume": 10,
                "quote_volume": price * 10,
                "oi": oi,
                "oi_value_usdt": price * oi,
                "futures_cvd_delta": 1,
                "spot_cvd_delta": 1,
                "funding_rate": 0.0001,
                "exchange_context": {"items": []},
            }

        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            snapshots = [metrics(100, 100), metrics(99, 110)]
            engine = LifecycleEngine(settings, metrics_provider=lambda _symbol, _timeframe: snapshots.pop(0))
            first = event(event_type="first_signal", event_level="15m")
            signal_one = {
                "id": 1, "symbol": "BTCUSDT", "status": "sent", "module": "launch",
                "timeframe": "15m", "score": 80, "excerpt": "BTCUSDT 15m 启动", "ts": int(time.time()),
            }
            signal_two = {**signal_one, "id": 2, "ts": int(time.time()) + 1}
            self.assertEqual(first["event_type"], "first_signal")
            engine.process_signal(signal_one)
            result = engine.process_signal(signal_two)

        self.assertIn("risk_warning", {item["event_type"] for item in result["events"]})
        self.assertEqual(result["lifecycle"]["current_state"], "risk_warning")


if __name__ == "__main__":
    unittest.main()
