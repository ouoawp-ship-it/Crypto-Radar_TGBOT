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
            "state_updates": [],
            "diagnostics": {"status": "ok"},
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
