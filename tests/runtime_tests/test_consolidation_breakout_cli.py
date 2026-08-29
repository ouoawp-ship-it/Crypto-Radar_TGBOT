from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from config import Settings
from runtime import cli
from shared.storage import JsonStore
from shared.telegram import PushResult, TelegramGateway


def args(*, send: bool = False, confirm: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        send=send,
        confirm_real_send=confirm,
        consolidation_scan_limit=None,
    )


class ConsolidationBreakoutCliTests(unittest.TestCase):
    @staticmethod
    def result() -> dict[str, object]:
        event = {
            "event_id": "range_breakout.v1:BTCUSDT:1d:long:breakout_up:1000",
            "dedup_key": "range_breakout.v1:BTCUSDT:1d:long:breakout_up:1000",
            "text": "BTCUSDT breakout",
            "symbol": "BTCUSDT",
        }
        return {
            "template_id": "TG_CONSOLIDATION_BREAKOUT",
            "events": [event],
            "chart_payloads": {},
            "state_updates": [],
            "diagnostics": {"status": "ok"},
        }

    @staticmethod
    def chart_payload() -> dict[str, object]:
        return {
            "candles": [
                {
                    "open_time": 0,
                    "close_time": 1_000,
                    "open": 100.0,
                    "high": 101.0,
                    "low": 99.0,
                    "close": 100.5,
                    "volume": 10.0,
                }
            ],
            "macd": [0.1],
        }

    def test_dry_run_does_not_accept_event_state(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = JsonStore(Path(tmp))
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "dry_run",
                "send_flag_not_set",
                False,
            )
            radar = Mock()
            result = self.result()
            radar.build.return_value = result
            radar.commit.return_value = {
                "status": "deferred",
                "applied": 0,
                "deferred": 1,
            }

            with (
                patch.object(
                    cli,
                    "ConsolidationBreakoutRadar",
                    return_value=radar,
                ),
                patch.object(cli, "BinanceDataSource") as source_factory,
                redirect_stdout(StringIO()),
            ):
                status, _diagnostics = cli.push_consolidation_breakout(
                    settings,
                    store,
                    gateway,
                    args(),
                )

        self.assertEqual(status, "dry_run")
        radar.commit.assert_called_once_with(result, set())
        source_factory.assert_called_once_with(settings)

    def test_sent_or_dedup_event_is_committed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = JsonStore(Path(tmp))
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "skipped",
                "dedup_cooldown",
                False,
            )
            radar = Mock()
            result = self.result()
            event_id = str(result["events"][0]["event_id"])  # type: ignore[index]
            radar.build.return_value = result
            radar.commit.return_value = {
                "status": "ok",
                "applied": 1,
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
                status, _diagnostics = cli.push_consolidation_breakout(
                    settings,
                    store,
                    gateway,
                    args(),
                )

        self.assertEqual(status, "skipped")
        radar.commit.assert_called_once_with(result, {event_id})

    def test_valid_chart_is_attached_without_polluting_signal_record(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = JsonStore(Path(tmp))
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult("sent", "ok", True, [123])
            radar = Mock()
            result = self.result()
            event = result["events"][0]  # type: ignore[index]
            event_id = str(event["event_id"])
            result["chart_payloads"] = {event_id: self.chart_payload()}
            radar.build.return_value = result
            radar.commit.return_value = {
                "status": "ok",
                "applied": 1,
                "deferred": 0,
            }
            png = b"\x89PNG\r\n\x1a\nchart"

            with (
                patch.object(
                    cli,
                    "ConsolidationBreakoutRadar",
                    return_value=radar,
                ),
                patch.object(cli, "BinanceDataSource"),
                patch.object(
                    cli,
                    "render_consolidation_chart_png",
                    return_value=png,
                ) as renderer,
                redirect_stdout(StringIO()),
            ):
                status, diagnostics = cli.push_consolidation_breakout(
                    settings,
                    store,
                    gateway,
                    args(send=True, confirm=True),
                )

        self.assertEqual(status, "sent")
        renderer.assert_called_once_with(
            event=event,
            chart_payload=result["chart_payloads"][event_id],  # type: ignore[index]
        )
        gateway.send.assert_called_once()
        send_kwargs = gateway.send.call_args.kwargs
        self.assertEqual(send_kwargs["photo"], png)
        self.assertEqual(send_kwargs["signal_records"], [event])
        self.assertNotIn("chart_payloads", event)
        self.assertEqual(diagnostics["delivery"]["charts_ready"], 1)  # type: ignore[index]
        self.assertEqual(diagnostics["delivery"]["charts_delivered"], 1)  # type: ignore[index]
        self.assertEqual(diagnostics["delivery"]["charts_text_fallback"], 0)  # type: ignore[index]
        radar.commit.assert_called_once_with(result, {event_id})

    def test_render_failure_falls_back_to_one_text_send_and_commits(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = JsonStore(Path(tmp))
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult("sent", "ok", True, [123])
            radar = Mock()
            result = self.result()
            event = result["events"][0]  # type: ignore[index]
            event_id = str(event["event_id"])
            result["chart_payloads"] = {event_id: self.chart_payload()}
            radar.build.return_value = result
            radar.commit.return_value = {
                "status": "ok",
                "applied": 1,
                "deferred": 0,
            }

            with (
                patch.object(
                    cli,
                    "ConsolidationBreakoutRadar",
                    return_value=radar,
                ),
                patch.object(cli, "BinanceDataSource"),
                patch.object(
                    cli,
                    "render_consolidation_chart_png",
                    side_effect=ValueError("bad chart"),
                ),
                redirect_stdout(StringIO()),
            ):
                status, diagnostics = cli.push_consolidation_breakout(
                    settings,
                    store,
                    gateway,
                    args(send=True, confirm=True),
                )

        self.assertEqual(status, "sent")
        gateway.send.assert_called_once()
        self.assertIsNone(gateway.send.call_args.kwargs["photo"])
        self.assertEqual(
            diagnostics["delivery"]["pushes"][0]["chart_status"],  # type: ignore[index]
            "render_failed",
        )
        radar.commit.assert_called_once_with(result, {event_id})

    def test_invalid_chart_and_long_caption_fall_back_before_send(self) -> None:
        event = self.result()["events"][0]  # type: ignore[index]
        with patch.object(
            cli,
            "render_consolidation_chart_png",
            return_value=b"not a png",
        ):
            photo, status = cli._consolidation_chart_photo(
                event,
                self.chart_payload(),
            )
        self.assertIsNone(photo)
        self.assertEqual(status, "invalid_png")

        long_event = dict(event)
        long_event["text"] = "x" * 1025
        with patch.object(cli, "render_consolidation_chart_png") as renderer:
            photo, status = cli._consolidation_chart_photo(
                long_event,
                self.chart_payload(),
            )
        self.assertIsNone(photo)
        self.assertEqual(status, "caption_too_long")
        renderer.assert_not_called()

    def test_photo_delivery_failure_is_not_retried_as_text(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = JsonStore(Path(tmp))
            gateway = Mock(spec=TelegramGateway)
            gateway.send.return_value = PushResult(
                "failed",
                "telegram_delivery_uncertain",
                False,
            )
            radar = Mock()
            result = self.result()
            event = result["events"][0]  # type: ignore[index]
            event_id = str(event["event_id"])
            result["chart_payloads"] = {event_id: self.chart_payload()}
            radar.build.return_value = result
            radar.commit.return_value = {
                "status": "deferred",
                "applied": 0,
                "deferred": 1,
            }

            with (
                patch.object(
                    cli,
                    "ConsolidationBreakoutRadar",
                    return_value=radar,
                ),
                patch.object(cli, "BinanceDataSource"),
                patch.object(
                    cli,
                    "render_consolidation_chart_png",
                    return_value=b"\x89PNG\r\n\x1a\nchart",
                ),
                redirect_stdout(StringIO()),
            ):
                status, _diagnostics = cli.push_consolidation_breakout(
                    settings,
                    store,
                    gateway,
                    args(send=True, confirm=True),
                )

        self.assertEqual(status, "failed")
        gateway.send.assert_called_once()
        self.assertIsNotNone(gateway.send.call_args.kwargs["photo"])
        radar.commit.assert_called_once_with(result, set())

    def test_valid_chart_delivery_status_matrix_preserves_commit_rules(self) -> None:
        cases = (
            ("dry_run", "send_flag_not_set", "dry_run", False),
            ("blocked", "confirmation_flag_not_set", "blocked", False),
            ("failed", "telegram_http_400", "failed", False),
            ("partial", "partial_send", "failed", False),
            ("skipped", "global_hourly_limit", "skipped", False),
            ("skipped", "dedup_cooldown", "skipped", True),
        )
        for push_status, reason, expected_status, accepted in cases:
            with self.subTest(push_status=push_status, reason=reason):
                with TemporaryDirectory() as tmp:
                    settings = Settings(data_dir=Path(tmp))
                    store = JsonStore(Path(tmp))
                    gateway = Mock(spec=TelegramGateway)
                    gateway.send.return_value = PushResult(
                        push_status,
                        reason,
                        False,
                    )
                    radar = Mock()
                    result = self.result()
                    event = result["events"][0]  # type: ignore[index]
                    event_id = str(event["event_id"])
                    result["chart_payloads"] = {
                        event_id: self.chart_payload(),
                    }
                    radar.build.return_value = result
                    radar.commit.return_value = {
                        "status": "ok" if accepted else "deferred",
                        "applied": 1 if accepted else 0,
                        "deferred": 0 if accepted else 1,
                    }

                    with (
                        patch.object(
                            cli,
                            "ConsolidationBreakoutRadar",
                            return_value=radar,
                        ),
                        patch.object(cli, "BinanceDataSource"),
                        patch.object(
                            cli,
                            "render_consolidation_chart_png",
                            return_value=b"\x89PNG\r\n\x1a\nchart",
                        ),
                        redirect_stdout(StringIO()),
                    ):
                        status, diagnostics = cli.push_consolidation_breakout(
                            settings,
                            store,
                            gateway,
                            args(send=True, confirm=True),
                        )

                self.assertEqual(status, expected_status)
                gateway.send.assert_called_once()
                self.assertIsNotNone(gateway.send.call_args.kwargs["photo"])
                radar.commit.assert_called_once_with(
                    result,
                    {event_id} if accepted else set(),
                )
                self.assertEqual(diagnostics["delivery"]["charts_ready"], 1)  # type: ignore[index]
                self.assertEqual(diagnostics["delivery"]["charts_delivered"], 0)  # type: ignore[index]

    def test_explicit_command_scans_while_daemon_switch_is_off(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                consolidation_breakout_enable=False,
            )
            store = JsonStore(Path(tmp))
            gateway = TelegramGateway(settings, store)

            with (
                patch.object(
                    cli,
                    "make_runtime_for_args",
                    return_value=(settings, store, Mock(), gateway),
                ),
                patch.object(
                    cli,
                    "push_consolidation_breakout",
                    return_value=("skipped", {"status": "ok"}),
                ) as push_mock,
                redirect_stdout(StringIO()),
            ):
                code = cli.run_consolidation_breakout(args())

        self.assertEqual(code, 0)
        active_settings = push_mock.call_args.args[0]
        self.assertTrue(active_settings.consolidation_breakout_enable)


if __name__ == "__main__":
    unittest.main()
