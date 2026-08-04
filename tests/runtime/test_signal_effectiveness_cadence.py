from __future__ import annotations

import argparse
import unittest
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

import runtime.cli as main
from config import Settings
from shared.storage import JsonStore


class SignalEffectivenessCadenceTests(unittest.TestCase):
    def test_background_refresh_waits_fifteen_minutes_after_success_or_failure(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root)
            store = JsonStore(root)
            with patch.object(
                main,
                "refresh_signal_effectiveness",
                return_value={"status": "ok"},
            ) as refresh:
                result, next_run = main.refresh_signal_effectiveness_if_due(
                    settings,
                    store,
                    now=100.0,
                    next_run_at=0.0,
                )
                skipped, unchanged = main.refresh_signal_effectiveness_if_due(
                    settings,
                    store,
                    now=999.0,
                    next_run_at=next_run,
                )

            self.assertEqual(result, {"status": "ok"})
            self.assertEqual(next_run, 1000.0)
            self.assertIsNone(skipped)
            self.assertEqual(unchanged, next_run)
            refresh.assert_called_once_with(settings)

            with patch.object(
                main,
                "refresh_signal_effectiveness",
                side_effect=OSError("test failure"),
            ) as refresh:
                failed, retry_at = main.refresh_signal_effectiveness_if_due(
                    settings,
                    store,
                    now=2_000.0,
                    next_run_at=0.0,
                )

            self.assertEqual(failed, {"status": "failed", "error": "OSError"})
            self.assertEqual(retry_at, 2_900.0)
            refresh.assert_called_once_with(settings)
            self.assertEqual(
                main.load_signal_effectiveness_next_run_at(
                    settings,
                    JsonStore(root),
                    now=2_100.0,
                ),
                2_900.0,
            )

    def test_background_cadence_survives_process_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root)
            first_store = JsonStore(root)
            with patch.object(
                main,
                "refresh_signal_effectiveness",
                return_value={"status": "ok"},
            ) as refresh:
                result, _next_run = main.refresh_signal_effectiveness_if_due(
                    settings,
                    first_store,
                    now=100.0,
                    next_run_at=0.0,
                )
                restarted_store = JsonStore(root)
                restored_next_run = main.load_signal_effectiveness_next_run_at(
                    settings,
                    restarted_store,
                    now=200.0,
                )
                skipped, unchanged = main.refresh_signal_effectiveness_if_due(
                    settings,
                    restarted_store,
                    now=200.0,
                    next_run_at=restored_next_run,
                )
                second, next_run = main.refresh_signal_effectiveness_if_due(
                    settings,
                    restarted_store,
                    now=1_000.0,
                    next_run_at=unchanged,
                )

            self.assertEqual(result, {"status": "ok"})
            self.assertEqual(restored_next_run, 1_000.0)
            self.assertIsNone(skipped)
            self.assertEqual(unchanged, 1_000.0)
            self.assertEqual(second, {"status": "ok"})
            self.assertEqual(next_run, 1_900.0)
            self.assertEqual(refresh.call_count, 2)

    def test_cadence_state_write_failure_does_not_block_refresh(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(data_dir=root)
            with (
                patch.object(
                    main,
                    "_persist_signal_effectiveness_attempt",
                    side_effect=OSError("test failure"),
                ),
                patch.object(
                    main,
                    "refresh_signal_effectiveness",
                    return_value={"status": "ok"},
                ) as refresh,
                redirect_stderr(StringIO()) as errors,
            ):
                result, next_run = main.refresh_signal_effectiveness_if_due(
                    settings,
                    JsonStore(root),
                    now=100.0,
                    next_run_at=0.0,
                )

            self.assertEqual(result, {"status": "ok"})
            self.assertEqual(next_run, 1_000.0)
            self.assertIn("signal_effectiveness_cadence warning=OSError", errors.getvalue())
            refresh.assert_called_once_with(settings)

    def test_trial_refreshes_only_its_first_cycle(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                runtime_status_path=root / "runtime_status.json",
            )
            store = JsonStore(root)
            engine = MagicMock()
            engine.build_launch_alerts.return_value = {
                "messages": [],
                "alerts": [],
                "diagnostics": {},
                "watchlist_count": 0,
            }
            engine.cleanup_failed_launch_messages.return_value = {}
            gateway = MagicMock()
            source = MagicMock()
            source.diagnostics.return_value = {}
            args = argparse.Namespace(
                command="trial",
                cycles=3,
                launch_interval=30,
                send=False,
                confirm_real_send=False,
            )
            with (
                patch.object(
                    main,
                    "make_runtime_for_args",
                    return_value=(settings, store, engine, gateway),
                ),
                patch.object(main, "BinanceDataSource", return_value=source),
                patch.object(
                    main,
                    "persist_market_batch",
                    return_value={"status": "saved"},
                ),
                patch.object(
                    main,
                    "push_launch_messages",
                    return_value=([], {}),
                ),
                patch.object(
                    main,
                    "refresh_signal_effectiveness",
                    return_value={"status": "ok"},
                ) as refresh,
                patch.object(main.time, "sleep"),
                redirect_stdout(StringIO()),
            ):
                main.run_trial(args)

        refresh.assert_called_once_with(settings)

    def test_observe_refreshes_only_its_first_nested_trial(self) -> None:
        class Clock:
            value = 0.0

            def time(self) -> float:
                return self.value

            def sleep(self, seconds: float) -> None:
                self.value += seconds

        clock = Clock()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                runtime_status_path=root / "runtime_status.json",
            )
            store = JsonStore(root)
            args = argparse.Namespace(
                command="observe",
                duration_minutes=2,
                launch_interval=60,
                send=False,
                confirm_real_send=False,
                records=100,
                top=10,
            )
            with (
                patch.object(
                    main,
                    "make_runtime_for_args",
                    return_value=(settings, store, MagicMock(), MagicMock()),
                ),
                patch.object(main, "run_trial") as run_trial,
                patch.object(
                    main,
                    "save_observe_report",
                    return_value=root / "observe.txt",
                ),
                patch.object(main, "format_observe_report", return_value="ok"),
                patch.object(main.time, "time", side_effect=clock.time),
                patch.object(main.time, "sleep", side_effect=clock.sleep),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(main.run_observe(args), 0)

        self.assertEqual(run_trial.call_count, 3)
        self.assertEqual(
            [call.kwargs["refresh_effectiveness"] for call in run_trial.call_args_list],
            [True, False, False],
        )


if __name__ == "__main__":
    unittest.main()
