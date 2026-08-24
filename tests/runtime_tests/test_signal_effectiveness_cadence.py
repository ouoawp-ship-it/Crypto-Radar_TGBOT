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

if __name__ == "__main__":
    unittest.main()
