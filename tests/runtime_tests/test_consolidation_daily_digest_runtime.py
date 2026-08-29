from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from config import Settings
from radars.consolidation_breakout.daily_digest import (
    ConsolidationDailyDigestAccumulator,
)
from runtime import cli
from shared.storage import JsonStore
from shared.telegram import PushResult, TelegramGateway


TARGET_MS = 1_700_006_399_999


def args(*, send: bool = False, confirm: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        send=send,
        confirm_real_send=confirm,
        consolidation_scan_limit=None,
    )


def daily_structure(symbol: str) -> dict[str, object]:
    return {
        "box_id": f"{symbol}:long:{TARGET_MS}",
        "horizon": "long",
        "horizon_label": "长期",
        "box_age": 240,
        "formed_close_time": TARGET_MS,
        "box_lower": 90.0,
        "box_upper": 110.0,
        "width_pct": 20.0,
        "width_atr": 8.0,
        "upper_touches": 3,
        "lower_touches": 3,
        "structure_quality": "strong",
        "quality_reasons": ["边界稳定", "触碰充分"],
        "lifecycle_state": "continuing",
    }


def daily_batch(*, complete: bool = True) -> dict[str, object]:
    observations: list[dict[str, object]] = [{
        "symbol": "AAAUSDT",
        "target_close_time": TARGET_MS,
        "status": "success",
        "structures": [daily_structure("AAAUSDT")],
    }]
    return {
        "target_close_time": TARGET_MS,
        "expected_symbols": (
            ["AAAUSDT"] if complete else ["AAAUSDT", "BBBUSDT"]
        ),
        "observations": observations,
        "round_completed": False,
        "round_token": "",
    }


def radar_result(batch: object = None) -> dict[str, object]:
    result: dict[str, object] = {
        "template_id": "TG_CONSOLIDATION_BREAKOUT",
        "events": [],
        "chart_payloads": {},
        "state_updates": [],
        "diagnostics": {"status": "ok"},
    }
    if batch is not None:
        result["daily_digest_batch"] = batch
    return result


def settings_for(
    root: Path,
    *,
    shadow: bool,
    max_items: int = 20,
    split_limit: int = 3800,
) -> Settings:
    return Settings(
        data_dir=root,
        consolidation_daily_product_enable=True,
        consolidation_daily_shadow_mode=shadow,
        consolidation_daily_digest_enable=True,
        consolidation_daily_digest_max_items=max_items,
        consolidation_daily_retry_rounds=2,
        consolidation_daily_max_wait_sec=10_800,
        consolidation_daily_digest_state_path=root / "daily_digest.json",
        tg_push_split_limit=split_limit,
    )


class ConsolidationDailyDigestRuntimeTests(unittest.TestCase):
    def run_cycle(
        self,
        *,
        settings: Settings,
        store: JsonStore,
        gateway: Mock,
        result: dict[str, object],
        runtime_args: argparse.Namespace,
    ) -> tuple[str, dict[str, object], Mock]:
        radar = Mock()
        radar.build.return_value = result
        radar.commit.return_value = {
            "status": "ok",
            "applied": 0,
            "deferred": 0,
        }
        with (
            patch.object(
                cli,
                "ConsolidationBreakoutRadar",
                return_value=radar,
            ),
            patch.object(cli, "BinanceDataSource"),
            redirect_stdout(StringIO()),
        ):
            status, diagnostics = cli.push_consolidation_breakout(
                settings,
                store,
                gateway,
                runtime_args,
            )
        return status, diagnostics, radar

    def test_shadow_mode_persists_accumulation_without_gateway_call(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root, shadow=True)
            store = JsonStore(root)
            gateway = Mock(spec=TelegramGateway)

            status, diagnostics, radar = self.run_cycle(
                settings=settings,
                store=store,
                gateway=gateway,
                result=radar_result(daily_batch(complete=False)),
                runtime_args=args(send=True, confirm=True),
            )
            state = store.load(settings.consolidation_daily_digest_state_path, {})

        self.assertEqual(status, "skipped")
        gateway.send.assert_not_called()
        radar.commit.assert_called_once()
        self.assertEqual(
            diagnostics["daily_digest"]["status"],
            "shadow_accumulating",
        )
        self.assertEqual(state["active"]["target_close_time"], TARGET_MS)
        self.assertEqual(sorted(state["active"]["observations"]), ["AAAUSDT"])
        self.assertEqual(state["pending_digests"], [])

    def test_non_shadow_persists_pending_before_send_and_completes_on_sent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root, shadow=False)
            store = JsonStore(root)
            gateway = Mock(spec=TelegramGateway)

            def sent_after_persist(*_args: object, **_kwargs: object) -> PushResult:
                saved = store.load(
                    settings.consolidation_daily_digest_state_path,
                    {},
                )
                self.assertEqual(len(saved["pending_digests"]), 1)
                return PushResult("sent", "telegram_api", True, [321])

            gateway.send.side_effect = sent_after_persist
            status, diagnostics, radar = self.run_cycle(
                settings=settings,
                store=store,
                gateway=gateway,
                result=radar_result(daily_batch()),
                runtime_args=args(send=True, confirm=True),
            )
            state = store.load(settings.consolidation_daily_digest_state_path, {})

        self.assertEqual(status, "sent")
        radar.commit.assert_called_once()
        gateway.send.assert_called_once()
        send_kwargs = gateway.send.call_args.kwargs
        self.assertTrue(send_kwargs["send"])
        self.assertTrue(send_kwargs["confirm_real_send"])
        self.assertIsNone(send_kwargs["photo"])
        self.assertEqual(send_kwargs["parse_mode"], "HTML")
        self.assertEqual(len(send_kwargs["signal_records"]), 1)
        record = send_kwargs["signal_records"][0]
        self.assertEqual(record["event"], "daily_consolidation_digest")
        self.assertEqual(record["structure_timeframe"], "1d")
        self.assertEqual(record["trigger_timeframe"], "1d")
        self.assertNotIn("score", record)
        self.assertEqual(diagnostics["daily_digest"]["status"], "delivered")
        self.assertEqual(state["pending_digests"], [])
        self.assertEqual(state["last_delivered_close_time"], TARGET_MS)
        self.assertEqual(len(state["recent_snapshots"]), 1)
        self.assertEqual(
            state["recent_snapshots"][0]["archive"]["status"],
            "delivered",
        )

    def test_failed_delivery_remains_pending_and_retries_without_new_batch(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root, shadow=False)
            store = JsonStore(root)
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "failed",
                "telegram_api_failed",
                False,
            )

            with patch.object(cli.time, "time", return_value=1_000):
                status, diagnostics, _radar = self.run_cycle(
                    settings=settings,
                    store=store,
                    gateway=gateway,
                    result=radar_result(daily_batch()),
                    runtime_args=args(send=True, confirm=True),
                )
            failed_state = store.load(
                settings.consolidation_daily_digest_state_path,
                {},
            )
            self.assertEqual(status, "failed")
            self.assertEqual(
                diagnostics["daily_digest"]["status"],
                "pending_retained",
            )
            self.assertEqual(len(failed_state["pending_digests"]), 1)
            self.assertEqual(
                failed_state["pending_digests"][0]["delivery"]["status"],
                "failed",
            )

            gateway.send.reset_mock()
            gateway.send.return_value = PushResult(
                "skipped",
                "dedup_cooldown",
                False,
            )
            with patch.object(cli.time, "time", return_value=1_299):
                backoff_status, backoff_diagnostics, _radar = self.run_cycle(
                    settings=settings,
                    store=store,
                    gateway=gateway,
                    result=radar_result(),
                    runtime_args=args(send=True, confirm=True),
                )
            self.assertEqual(backoff_status, "skipped")
            gateway.send.assert_not_called()
            self.assertEqual(
                backoff_diagnostics["daily_digest"]["status"],
                "retry_backoff",
            )

            with patch.object(cli.time, "time", return_value=1_300):
                retry_status, retry_diagnostics, _radar = self.run_cycle(
                    settings=settings,
                    store=store,
                    gateway=gateway,
                    result=radar_result(),
                    runtime_args=args(send=True, confirm=True),
                )
            recovered_state = store.load(
                settings.consolidation_daily_digest_state_path,
                {},
            )

        self.assertEqual(retry_status, "skipped")
        gateway.send.assert_called_once()
        self.assertEqual(
            retry_diagnostics["daily_digest"]["batch_status"],
            "unavailable",
        )
        self.assertEqual(
            retry_diagnostics["daily_digest"]["status"],
            "delivered",
        )
        self.assertEqual(recovered_state["pending_digests"], [])
        self.assertEqual(recovered_state["last_delivered_close_time"], TARGET_MS)
        self.assertEqual(
            recovered_state["recent_snapshots"][0]["archive"]["status"],
            "already_delivered",
        )

    def test_absent_batch_is_compatible_and_does_not_send_without_pending(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root, shadow=False)
            store = JsonStore(root)
            gateway = Mock(spec=TelegramGateway)
            status, diagnostics, _radar = self.run_cycle(
                settings=settings,
                store=store,
                gateway=gateway,
                result=radar_result(),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "skipped")
        gateway.send.assert_not_called()
        self.assertEqual(
            diagnostics["daily_digest"]["status"],
            "batch_unavailable",
        )

    def test_runtime_limits_message_and_signal_records_but_archives_full_snapshot(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(
                root,
                shadow=False,
                max_items=3,
                split_limit=900,
            )
            store = JsonStore(root)
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "sent",
                "telegram_api",
                True,
                [321],
            )
            aaa_best = daily_structure("AAAUSDT")
            aaa_best.update({
                "box_id": "AAAUSDT:long:best",
                "horizon": "long",
                "box_age": 300,
                "structure_quality": "strong",
            })
            aaa_weaker = daily_structure("AAAUSDT")
            aaa_weaker.update({
                "box_id": "AAAUSDT:short:weaker",
                "horizon": "short",
                "box_age": 30,
                "structure_quality": "observe",
            })
            bbb = daily_structure("BBBUSDT")
            batch = {
                "target_close_time": TARGET_MS,
                "expected_symbols": ["AAAUSDT", "BBBUSDT"],
                "observations": [
                    {
                        "symbol": "AAAUSDT",
                        "target_close_time": TARGET_MS,
                        "status": "success",
                        "structures": [aaa_weaker, aaa_best],
                    },
                    {
                        "symbol": "BBBUSDT",
                        "target_close_time": TARGET_MS,
                        "status": "success",
                        "structures": [bbb],
                    },
                ],
                "round_completed": False,
                "round_token": "",
            }

            status, _diagnostics, _radar = self.run_cycle(
                settings=settings,
                store=store,
                gateway=gateway,
                result=radar_result(batch),
                runtime_args=args(send=True, confirm=True),
            )
            state = store.load(settings.consolidation_daily_digest_state_path, {})

        self.assertEqual(status, "sent")
        send_args = gateway.send.call_args.args
        send_kwargs = gateway.send.call_args.kwargs
        self.assertLessEqual(len(send_args[0]), 900)
        records = send_kwargs["signal_records"]
        self.assertEqual([item["symbol"] for item in records], [
            "AAAUSDT",
            "BBBUSDT",
        ])
        self.assertEqual(records[0]["horizon"], "long")
        archived_structures = state["recent_snapshots"][0]["structures"]
        self.assertEqual(len(archived_structures), 3)

    def test_legacy_backlog_sends_only_latest_and_archives_old_without_burst(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = settings_for(root, shadow=False)
            store = JsonStore(root)
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "sent",
                "telegram_api",
                True,
                [321],
            )
            first_accumulator = ConsolidationDailyDigestAccumulator()
            old_pending = first_accumulator.ingest_batch(
                target_close_time=TARGET_MS,
                expected_symbols=["AAAUSDT"],
                observations=[daily_batch()["observations"][0]],
                now_ts=100,
            )
            new_target = TARGET_MS + 86_400_000
            second_accumulator = ConsolidationDailyDigestAccumulator()
            new_observation = dict(daily_batch()["observations"][0])
            new_observation["target_close_time"] = new_target
            new_pending = second_accumulator.ingest_batch(
                target_close_time=new_target,
                expected_symbols=["AAAUSDT"],
                observations=[new_observation],
                now_ts=200,
            )
            legacy_state = first_accumulator.snapshot()
            legacy_state["pending_digests"] = [old_pending, new_pending]
            store.save(settings.consolidation_daily_digest_state_path, legacy_state)

            status, _diagnostics, _radar = self.run_cycle(
                settings=settings,
                store=store,
                gateway=gateway,
                result=radar_result(),
                runtime_args=args(send=True, confirm=True),
            )
            state = store.load(settings.consolidation_daily_digest_state_path, {})

        self.assertEqual(status, "sent")
        gateway.send.assert_called_once()
        self.assertEqual(gateway.send.call_args.args[2], new_pending["dedup_key"])
        self.assertEqual(state["pending_digests"], [])
        self.assertEqual(
            [item["digest_id"] for item in state["recent_snapshots"]],
            [old_pending["digest_id"], new_pending["digest_id"]],
        )
        self.assertEqual(
            state["recent_snapshots"][0]["archive"]["status"],
            "superseded",
        )


if __name__ == "__main__":
    unittest.main()
