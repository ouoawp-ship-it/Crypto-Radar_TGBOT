from __future__ import annotations

import json
import unittest
from argparse import Namespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar import cli
from paopao_radar.config import BASE_DIR, Settings
from paopao_radar.lifecycle_store import LifecycleStore
from paopao_radar.web_services.lifecycle import (
    public_lifecycle_detail_payload,
    public_lifecycle_metrics_payload,
)


FRONTEND = BASE_DIR / "frontend"


def settings_for(tmp: str, **overrides: object) -> Settings:
    base = Path(tmp)
    values: dict[str, object] = {
        "data_dir": base,
        "signal_events_db_path": base / "signals.db",
        "lifecycle_db_path": base / "lifecycle.db",
        "lifecycle_active_max_symbols": 80,
    }
    values.update(overrides)
    return Settings(**values)


class LifecycleSurfaceConfigTests(unittest.TestCase):
    def test_lifecycle_telegram_is_disabled_by_default(self) -> None:
        self.assertFalse(Settings().lifecycle_telegram_enable)
        example = (BASE_DIR / ".env.oi.example").read_text(encoding="utf-8")
        self.assertIn("LIFECYCLE_TELEGRAM_ENABLE=false", example)
        self.assertNotIn("LIFECYCLE_TELEGRAM_ENABLE=true", example)

    def test_backfill_defaults_to_168_hours_and_80_symbols(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            args = Namespace(lookback_hours=None, limit_symbols=None, dry_run=True)
            with patch("paopao_radar.cli.Settings.load", return_value=settings):
                with patch(
                    "paopao_radar.cli.scan_lifecycles",
                    return_value={"ok": True, "counts": {"dry_run": True}},
                ) as scan:
                    code = cli.run_lifecycle_backfill(args)

        self.assertEqual(code, 0)
        self.assertEqual(scan.call_args.kwargs["lookback_hours"], 168)
        self.assertEqual(scan.call_args.kwargs["limit_symbols"], 80)
        self.assertTrue(scan.call_args.kwargs["dry_run"])
        self.assertFalse(scan.call_args.kwargs["push"])

    def test_lifecycle_cycle_respects_enable_push_and_real_send_gates(self) -> None:
        with TemporaryDirectory() as tmp:
            disabled = settings_for(tmp, lifecycle_tracker_enable=False)
            with patch("paopao_radar.cli.scan_lifecycles") as scan:
                skipped = cli.run_lifecycle_tracker_cycle(
                    disabled,
                    Namespace(send=True, confirm_real_send=True),
                )
            self.assertTrue(skipped["skipped"])
            scan.assert_not_called()

            enabled = settings_for(
                tmp,
                lifecycle_tracker_enable=True,
                lifecycle_telegram_enable=True,
                lifecycle_scan_interval_sec=900,
            )
            with patch(
                "paopao_radar.cli.scan_lifecycles",
                return_value={"ok": True, "counts": {}},
            ) as scan:
                cli.run_lifecycle_tracker_cycle(
                    enabled,
                    Namespace(send=True, confirm_real_send=False),
                )

        kwargs = scan.call_args.kwargs
        self.assertTrue(kwargs["push"])
        self.assertFalse(kwargs["send"])
        self.assertFalse(kwargs["confirm_real_send"])
        self.assertEqual(kwargs["limit_symbols"], 80)

    def test_lifecycle_cycle_failure_is_isolated(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp, lifecycle_tracker_enable=True)
            with patch("paopao_radar.cli.scan_lifecycles", side_effect=TimeoutError("Binance timeout")):
                result = cli.run_lifecycle_tracker_cycle(
                    settings,
                    Namespace(send=False, confirm_real_send=False),
                )

        self.assertFalse(result["ok"])
        self.assertIn("TimeoutError", str(result["error"]))
        self.assertIn("继续运行", str(result["message"]))

    def test_run_loop_schedules_lifecycle_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(
                tmp,
                lifecycle_tracker_enable=True,
                lifecycle_scan_interval_sec=900,
            )
            args = Namespace(
                command="daemon",
                interval=None,
                launch_interval=180,
                send=False,
                confirm_real_send=False,
                no_launch=True,
                no_flow=True,
                no_funding_alert=True,
            )
            with patch("paopao_radar.cli.make_runtime_for_args", return_value=(settings, object(), None, None)):
                with patch("paopao_radar.cli.next_closed_window_epoch", return_value=10**12):
                    with patch("paopao_radar.cli.timestamp_from_epoch", return_value="future"):
                        with patch("paopao_radar.cli.cleanup_runtime_artifacts"):
                            with patch("paopao_radar.cli.write_runtime_status"):
                                with patch(
                                    "paopao_radar.cli.run_lifecycle_tracker_cycle",
                                    return_value={"ok": True, "counts": {"signals": 0}},
                                ) as cycle:
                                    with patch("paopao_radar.cli.time.sleep", side_effect=KeyboardInterrupt):
                                        with self.assertRaises(KeyboardInterrupt):
                                            cli.run_loop(args)

        cycle.assert_called_once_with(settings, args)


class LifecycleSurfaceApiTests(unittest.TestCase):
    def test_public_metrics_and_detail_redact_nested_private_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = settings_for(tmp)
            store = LifecycleStore(settings.lifecycle_db_path)
            store.insert_snapshot(
                {
                    "symbol": "BTCUSDT",
                    "timeframe": "15m",
                    "snapshot_time": "2026-07-10T00:00:00+00:00",
                    "volume": 123,
                    "metrics": {
                        "volume": 123,
                        "nested": {
                            "chat_id": "123456",
                            "topic_id": "42",
                            "message_id": "7",
                            "dedup_key": "private",
                            "payload_json": "raw",
                            "text_html": "<b>private</b>",
                            "server_path": "/home/ubuntu/private",
                            "safe_status": "ok",
                        },
                    },
                }
            )

            metrics = public_lifecycle_metrics_payload(symbol="BTCUSDT", settings=settings)
            detail = public_lifecycle_detail_payload("BTCUSDT", settings=settings)

        text = json.dumps({"metrics": metrics, "detail": detail}, ensure_ascii=False)
        for forbidden in (
            "chat_id",
            "topic_id",
            "message_id",
            "dedup_key",
            "payload_json",
            "text_html",
            "/home/ubuntu",
        ):
            self.assertNotIn(forbidden, text)
        self.assertIn('"safe_status": "ok"', text)
        self.assertIn('"volume": 123', text)


class LifecycleSurfaceFrontendTests(unittest.TestCase):
    def test_coin_page_renders_volume_and_exchange_side_observations(self) -> None:
        source = (FRONTEND / "app/coin/[symbol]/page.tsx").read_text(encoding="utf-8")
        for marker in (
            "Binance 成交量",
            "Binance 报价成交额",
            "其他交易所旁路观察",
            "current_price",
            "funding_rate",
            "price_deviation_vs_binance_pct",
            "funding_deviation_vs_binance",
        ):
            self.assertIn(marker, source)

    def test_lifecycle_page_keeps_exact_disclaimer(self) -> None:
        source = (FRONTEND / "app/lifecycle/page.tsx").read_text(encoding="utf-8")
        self.assertIn("仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。", source)

    def test_api_docs_list_all_public_lifecycle_routes(self) -> None:
        source = (FRONTEND / "app/api-docs/page.tsx").read_text(encoding="utf-8")
        self.assertIn("生命周期接口", source)
        for route in ("summary", "list", "detail", "events", "metrics"):
            self.assertIn(f"/public-api/lifecycle/{route}", source)


if __name__ == "__main__":
    unittest.main()
