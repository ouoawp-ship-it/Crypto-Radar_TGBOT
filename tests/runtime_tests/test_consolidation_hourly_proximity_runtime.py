from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, call, patch

from config import Settings
from runtime import cli
from shared.storage import JsonStore
from shared.telegram import PushResult, TelegramGateway


CLOSE_TIME = 1_700_006_399_999


def args(*, send: bool = False, confirm: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        send=send,
        confirm_real_send=confirm,
        consolidation_scan_limit=None,
    )


def base_result(
    *,
    event: dict[str, object] | None = None,
    events: list[dict[str, object]] | None = None,
    withheld: int = 0,
) -> dict[str, object]:
    selected_events = events if events is not None else (
        [event] if event is not None else []
    )
    return {
        "template_id": "TG_CONSOLIDATION_BREAKOUT",
        "events": selected_events,
        "chart_payloads": {},
        "state_updates": [],
        "diagnostics": {
            "status": "ok",
            "withheld_event_count": withheld,
        },
    }


def proximity_event(
    *,
    symbol: str = "BTCUSDT",
    close_time: int = CLOSE_TIME,
) -> dict[str, object]:
    event_id = (
        "range_proximity.v1:"
        f"{symbol}:1h:long:upper:15m:{close_time}:0"
    )
    return {
        "event_id": event_id,
        "dedup_key": event_id,
        "text": f"{symbol} 1H upper proximity",
        "symbol": symbol,
        "close_time": close_time,
        "event_time": close_time,
        "event": "proximity_upper",
        "structure_timeframe": "1h",
        "trigger_timeframe": "15m",
    }


def proximity_result(
    *,
    event: dict[str, object] | None = None,
) -> dict[str, object]:
    events = [event] if event is not None else []
    return {
        "template_id": "TG_CONSOLIDATION_BREAKOUT",
        "events": events,
        "chart_payloads": {},
        "state_updates": [],
        "rotation_update": {"after_symbol": "BTCUSDT", "round": 1},
        "diagnostics": {"status": "ok", "event_count": len(events)},
    }


def enabled_settings(root: Path, *, shadow: bool) -> Settings:
    return Settings(
        data_dir=root,
        consolidation_hourly_proximity_enable=True,
        consolidation_hourly_proximity_shadow_mode=shadow,
        consolidation_hourly_proximity_kline_budget=60,
    )


def source_context() -> MagicMock:
    context = MagicMock()
    context.__enter__.return_value = object()
    context.__exit__.return_value = None
    return context


class ConsolidationHourlyProximityRuntimeTests(unittest.TestCase):
    def test_read_only_state_inventory_includes_hourly_state_file(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = enabled_settings(Path(tmp), shadow=True)

            paths = cli.state_paths(settings)

        self.assertIn(
            settings.consolidation_hourly_proximity_state_path,
            paths,
        )

    def test_hard_child_status_maps_to_parent_cycle_error(self) -> None:
        cases = (
            "scan_failed",
            "shadow_commit_failed",
            "commit_failed",
        )
        for status in cases:
            with self.subTest(status=status):
                self.assertEqual(
                    cli._consolidation_hourly_proximity_error_code({
                        "hourly_proximity": {"status": status},
                    }),
                    f"hourly_proximity_{status}",
                )
        self.assertEqual(
            cli._consolidation_hourly_proximity_error_code({
                "hourly_proximity": {"status": "shadow_idle"},
            }),
            "",
        )

    def test_one_shot_returns_failure_for_hard_child_status(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = enabled_settings(Path(tmp), shadow=False)
            runtime_args = args()
            with (
                patch.object(
                    cli,
                    "make_runtime_for_args",
                    return_value=(settings, Mock(), Mock(), Mock()),
                ),
                patch.object(
                    cli,
                    "push_consolidation_breakout",
                    return_value=(
                        "sent",
                        {"hourly_proximity": {"status": "scan_failed"}},
                    ),
                ),
                redirect_stdout(StringIO()),
            ):
                exit_code = cli.run_consolidation_breakout(runtime_args)

        self.assertEqual(exit_code, 1)

    def test_run_once_records_hard_child_failure_in_parent_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                enabled_settings(Path(tmp), shadow=False),
                consolidation_breakout_enable=True,
            )
            store = Mock()
            engine = Mock()
            engine.run_once.return_value = {
                "summary": {
                    "text": "summary",
                    "template_id": "TG_MARKET_RADAR",
                    "dedup_key": "summary:1",
                    "context_records": [],
                },
                "announcements": {"status": "ok"},
                "announcement_evidence": {},
                "diagnostics": {},
            }
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "dry_run",
                "send_flag_not_set",
                False,
            )
            runtime_args = argparse.Namespace(
                send=False,
                confirm_real_send=False,
                no_launch=True,
                no_announcements=True,
                no_flow=True,
                no_funding_alert=True,
                no_consolidation_breakout=False,
            )
            with (
                patch.object(
                    cli,
                    "make_runtime_for_args",
                    return_value=(settings, store, engine, gateway),
                ),
                patch.object(
                    cli,
                    "push_consolidation_breakout",
                    return_value=(
                        "sent",
                        {"hourly_proximity": {"status": "scan_failed"}},
                    ),
                ),
                patch.object(cli, "write_runtime_status") as write_status,
                redirect_stdout(StringIO()),
            ):
                exit_code = cli.run_once(
                    runtime_args,
                    refresh_effectiveness=False,
                )

        self.assertEqual(exit_code, 1)
        final_status = write_status.call_args_list[-1]
        self.assertEqual(
            final_status.args[3],
            "consolidation_breakout_failed",
        )
        self.assertEqual(
            final_status.kwargs["consolidation_breakout_cycle_status"],
            "failed",
        )
        self.assertEqual(
            final_status.kwargs["consolidation_breakout_error_code"],
            "hourly_proximity_scan_failed",
        )

    def run_cycle(
        self,
        *,
        settings: Settings,
        gateway: Mock,
        base: dict[str, object],
        proximity: dict[str, object] | None = None,
        proximity_error: Exception | None = None,
        proximity_commit_error: Exception | None = None,
        runtime_args: argparse.Namespace | None = None,
    ) -> tuple[
        str,
        dict[str, object],
        Mock,
        Mock,
        Mock,
        Mock,
    ]:
        store = JsonStore(settings.data_dir)
        base_radar = Mock()
        base_radar.build.return_value = base
        base_radar.commit.return_value = {
            "status": "ok",
            "applied": len(base.get("events") or []),
            "deferred": 0,
        }
        proximity_radar = Mock()
        if proximity_error is not None:
            proximity_radar.build.side_effect = proximity_error
        else:
            proximity_radar.build.return_value = proximity or proximity_result()
        if proximity_commit_error is not None:
            proximity_radar.commit.side_effect = proximity_commit_error
        else:
            proximity_radar.commit.return_value = {
                "status": "ok",
                "applied": 1,
                "deferred": 0,
                "rotation_advanced": True,
            }
        source_factory = Mock()
        source_factory.side_effect = [source_context(), source_context()]

        with (
            patch.object(
                cli,
                "ConsolidationBreakoutRadar",
                return_value=base_radar,
            ),
            patch.object(
                cli,
                "ConsolidationHourlyProximityRadar",
                return_value=proximity_radar,
            ) as proximity_factory,
            patch.object(cli, "BinanceDataSource", source_factory),
            redirect_stdout(StringIO()),
        ):
            status, diagnostics = cli.push_consolidation_breakout(
                settings,
                store,
                gateway,
                runtime_args or args(),
            )
        return (
            status,
            diagnostics,
            base_radar,
            proximity_radar,
            proximity_factory,
            source_factory,
        )

    def test_disabled_does_not_construct_proximity_radar_or_source(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            gateway = Mock(spec=TelegramGateway)
            (
                status,
                diagnostics,
                _base_radar,
                proximity_radar,
                proximity_factory,
                source_factory,
            ) = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(),
            )

        self.assertEqual(status, "skipped")
        gateway.send.assert_not_called()
        proximity_factory.assert_not_called()
        proximity_radar.build.assert_not_called()
        source_factory.assert_called_once_with(settings)
        self.assertEqual(
            diagnostics["hourly_proximity"]["status"],
            "disabled",
        )

    def test_shadow_commits_observation_without_gateway_delivery(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = enabled_settings(Path(tmp), shadow=True)
            gateway = Mock(spec=TelegramGateway)
            event = proximity_event()
            (
                status,
                diagnostics,
                _base_radar,
                proximity_radar,
                _proximity_factory,
                source_factory,
            ) = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(),
                proximity=proximity_result(event=event),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "skipped")
        gateway.send.assert_not_called()
        proximity_radar.commit.assert_called_once_with(
            proximity_radar.build.return_value,
            {event["event_id"]},
        )
        self.assertEqual(
            diagnostics["hourly_proximity"]["status"],
            "shadow_observed",
        )
        self.assertEqual(
            diagnostics["hourly_proximity"]["accepted"],
            1,
        )
        self.assertEqual(
            source_factory.call_args_list,
            [
                call(settings),
                call(settings, kline_budget=60),
            ],
        )

    def test_live_dry_run_does_not_consume_event(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = enabled_settings(Path(tmp), shadow=False)
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "dry_run",
                "send_flag_not_set",
                False,
            )
            event = proximity_event()
            (
                status,
                diagnostics,
                _base_radar,
                proximity_radar,
                _proximity_factory,
                _source_factory,
            ) = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(),
                proximity=proximity_result(event=event),
            )

        self.assertEqual(status, "dry_run")
        gateway.send.assert_called_once()
        send_kwargs = gateway.send.call_args.kwargs
        self.assertEqual(
            gateway.send.call_args.args[1],
            "TG_CONSOLIDATION_BREAKOUT",
        )
        self.assertFalse(send_kwargs["send"])
        self.assertFalse(send_kwargs["confirm_real_send"])
        self.assertEqual(send_kwargs["cooldown_sec"], 7 * 86400)
        self.assertEqual(send_kwargs["signal_records"], [event])
        self.assertIsNone(send_kwargs["photo"])
        proximity_radar.commit.assert_called_once_with(
            proximity_radar.build.return_value,
            set(),
        )
        self.assertEqual(
            diagnostics["hourly_proximity"]["accepted"],
            0,
        )

    def test_live_sent_or_exact_dedup_commits_event(self) -> None:
        cases = (
            PushResult("sent", "telegram_api", True, [321]),
            PushResult("skipped", "dedup_cooldown", False),
        )
        for push_result in cases:
            with self.subTest(
                status=push_result.status,
                reason=push_result.reason,
            ):
                with TemporaryDirectory() as tmp:
                    settings = enabled_settings(Path(tmp), shadow=False)
                    gateway = Mock(spec=TelegramGateway)
                    gateway.send.return_value = push_result
                    event = proximity_event()
                    (
                        status,
                        diagnostics,
                        _base_radar,
                        proximity_radar,
                        _proximity_factory,
                        _source_factory,
                    ) = self.run_cycle(
                        settings=settings,
                        gateway=gateway,
                        base=base_result(),
                        proximity=proximity_result(event=event),
                        runtime_args=args(send=True, confirm=True),
                    )

                self.assertEqual(status, push_result.status)
                proximity_radar.commit.assert_called_once_with(
                    proximity_radar.build.return_value,
                    {event["event_id"]},
                )
                self.assertEqual(
                    diagnostics["hourly_proximity"]["accepted"],
                    1,
                )

    def test_same_symbol_and_close_is_suppressed_after_base_event(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = enabled_settings(Path(tmp), shadow=False)
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "sent",
                "telegram_api",
                True,
                [321],
            )
            event = proximity_event()
            structure_event = {
                "event_id": "range_breakout.v1:BTCUSDT:4h:long:breakout_up",
                "dedup_key": "range_breakout.v1:BTCUSDT:4h:long:breakout_up",
                "text": "BTCUSDT breakout",
                "symbol": "BTCUSDT",
                "close_time": CLOSE_TIME,
                "event_time": CLOSE_TIME,
            }
            (
                status,
                diagnostics,
                base_radar,
                proximity_radar,
                _proximity_factory,
                _source_factory,
            ) = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(event=structure_event),
                proximity=proximity_result(event=event),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "sent")
        gateway.send.assert_called_once()
        self.assertEqual(
            gateway.send.call_args.kwargs["signal_records"],
            [structure_event],
        )
        base_radar.commit.assert_called_once_with(
            base_radar.build.return_value,
            {structure_event["event_id"]},
        )
        proximity_radar.commit.assert_called_once_with(
            proximity_radar.build.return_value,
            {event["event_id"]},
        )
        proximity_diag = diagnostics["hourly_proximity"]
        self.assertEqual(proximity_diag["suppressed_by_structure"], 1)
        self.assertEqual(
            proximity_diag["pushes"][0]["reason"],
            "base_structure_same_close",
        )

    def test_saturated_base_capacity_defers_lower_priority_proximity(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                enabled_settings(Path(tmp), shadow=False),
                consolidation_breakout_max_signals_per_scan=1,
            )
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "sent",
                "telegram_api",
                True,
                [321],
            )
            event = proximity_event(symbol="ETHUSDT")
            structure_event = {
                "event_id": "range_breakout.v1:BTCUSDT:4h:long:breakout_up",
                "dedup_key": "range_breakout.v1:BTCUSDT:4h:long:breakout_up",
                "text": "BTCUSDT breakout",
                "symbol": "BTCUSDT",
                "close_time": CLOSE_TIME,
            }
            (
                status,
                diagnostics,
                _base_radar,
                proximity_radar,
                _proximity_factory,
                _source_factory,
            ) = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(event=structure_event),
                proximity=proximity_result(event=event),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "sent")
        gateway.send.assert_called_once()
        self.assertEqual(
            gateway.send.call_args.kwargs["signal_records"],
            [structure_event],
        )
        proximity_radar.commit.assert_called_once_with(
            proximity_radar.build.return_value,
            set(),
        )
        proximity_diag = diagnostics["hourly_proximity"]
        self.assertEqual(
            proximity_diag["deferred_by_structure_capacity"],
            1,
        )
        self.assertEqual(
            proximity_diag["pushes"][0]["reason"],
            "base_structure_capacity_saturated",
        )

    def test_withheld_base_candidate_defers_even_when_event_keys_collapse(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                enabled_settings(Path(tmp), shadow=False),
                consolidation_breakout_max_signals_per_scan=2,
            )
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "sent",
                "telegram_api",
                True,
                [321],
            )
            structure_events = [
                {
                    "event_id": f"base:{index}",
                    "dedup_key": f"base:{index}",
                    "text": f"BTCUSDT structure {index}",
                    "symbol": "BTCUSDT",
                    "close_time": CLOSE_TIME,
                }
                for index in range(2)
            ]
            event = proximity_event(symbol="ETHUSDT")

            status, diagnostics, *_rest = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(
                    events=structure_events,
                    withheld=1,
                ),
                proximity=proximity_result(event=event),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "sent")
        self.assertEqual(gateway.send.call_count, 2)
        proximity_diag = diagnostics["hourly_proximity"]
        self.assertTrue(proximity_diag["base_events_withheld"])
        self.assertTrue(proximity_diag["base_capacity_saturated"])
        self.assertEqual(
            proximity_diag["pushes"][0]["reason"],
            "base_structure_capacity_saturated",
        )

    def test_underfilled_base_without_withheld_allows_proximity(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                enabled_settings(Path(tmp), shadow=False),
                consolidation_breakout_max_signals_per_scan=2,
            )
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "sent",
                "telegram_api",
                True,
                [321],
            )
            structure_event = {
                "event_id": "base:btc",
                "dedup_key": "base:btc",
                "text": "BTCUSDT structure",
                "symbol": "BTCUSDT",
                "close_time": CLOSE_TIME - 1,
            }
            event = proximity_event(symbol="ETHUSDT")

            status, diagnostics, *_rest = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(event=structure_event),
                proximity=proximity_result(event=event),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "sent")
        self.assertEqual(gateway.send.call_count, 2)
        proximity_diag = diagnostics["hourly_proximity"]
        self.assertFalse(proximity_diag["base_events_withheld"])
        self.assertFalse(proximity_diag["base_capacity_saturated"])
        self.assertEqual(proximity_diag["accepted"], 1)

    def test_unaccepted_base_delivery_defers_lower_priority_proximity(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = enabled_settings(Path(tmp), shadow=False)
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "blocked",
                "topic_route_missing",
                False,
            )
            event = proximity_event(symbol="ETHUSDT")
            structure_event = {
                "event_id": "range_breakout.v1:BTCUSDT:4h:long:breakout_up",
                "dedup_key": "range_breakout.v1:BTCUSDT:4h:long:breakout_up",
                "text": "BTCUSDT breakout",
                "symbol": "BTCUSDT",
                "close_time": CLOSE_TIME,
            }
            (
                status,
                diagnostics,
                _base_radar,
                proximity_radar,
                _proximity_factory,
                _source_factory,
            ) = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(event=structure_event),
                proximity=proximity_result(event=event),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "blocked")
        gateway.send.assert_called_once()
        proximity_radar.commit.assert_called_once_with(
            proximity_radar.build.return_value,
            set(),
        )
        proximity_diag = diagnostics["hourly_proximity"]
        self.assertEqual(
            proximity_diag["deferred_by_structure_delivery"],
            1,
        )
        self.assertEqual(
            proximity_diag["pushes"][0]["reason"],
            "base_structure_delivery_pending",
        )

    def test_proximity_scan_failure_does_not_override_base_delivery(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = enabled_settings(Path(tmp), shadow=False)
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "sent",
                "telegram_api",
                True,
                [321],
            )
            structure_event = {
                "event_id": "range_breakout.v1:ETHUSDT:4h:long:breakout_up",
                "dedup_key": "range_breakout.v1:ETHUSDT:4h:long:breakout_up",
                "text": "ETHUSDT breakout",
                "symbol": "ETHUSDT",
                "close_time": CLOSE_TIME,
            }
            (
                status,
                diagnostics,
                base_radar,
                proximity_radar,
                _proximity_factory,
                _source_factory,
            ) = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(event=structure_event),
                proximity_error=RuntimeError("upstream unavailable"),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "sent")
        gateway.send.assert_called_once()
        base_radar.commit.assert_called_once()
        proximity_radar.commit.assert_not_called()
        self.assertEqual(
            diagnostics["hourly_proximity"]["status"],
            "scan_failed",
        )
        self.assertEqual(
            diagnostics["hourly_proximity"]["error_code"],
            "RuntimeError",
        )

    def test_live_commit_failure_marks_overall_cycle_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = enabled_settings(Path(tmp), shadow=False)
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "sent",
                "telegram_api",
                True,
                [321],
            )
            event = proximity_event()

            status, diagnostics, *_rest = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(),
                proximity=proximity_result(event=event),
                proximity_commit_error=OSError("state unavailable"),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "failed")
        self.assertEqual(
            diagnostics["hourly_proximity"]["status"],
            "commit_failed",
        )
        self.assertEqual(
            diagnostics["hourly_proximity"]["delivery_status"],
            "sent",
        )

    def test_shadow_commit_failure_marks_overall_cycle_failed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = enabled_settings(Path(tmp), shadow=True)
            gateway = Mock(spec=TelegramGateway)
            event = proximity_event()

            status, diagnostics, *_rest = self.run_cycle(
                settings=settings,
                gateway=gateway,
                base=base_result(),
                proximity=proximity_result(event=event),
                proximity_commit_error=OSError("state unavailable"),
                runtime_args=args(send=True, confirm=True),
            )

        self.assertEqual(status, "failed")
        gateway.send.assert_not_called()
        self.assertEqual(
            diagnostics["hourly_proximity"]["status"],
            "shadow_commit_failed",
        )


if __name__ == "__main__":
    unittest.main()
