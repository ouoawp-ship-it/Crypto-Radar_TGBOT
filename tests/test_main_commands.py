from __future__ import annotations

import argparse
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paopao_radar.cli as main
from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import TelegramGateway


class MainCommandTests(unittest.TestCase):
    def make_runtime(self, tmp: str):
        settings = Settings(
            base_dir=Path(tmp),
            data_dir=Path(tmp),
            tg_push_history_path=Path(tmp) / "push_history.json",
            runtime_status_path=Path(tmp) / "runtime_status.json",
            radar_state_path=Path(tmp) / "radar_state.json",
            funding_snapshot_path=Path(tmp) / "funding_snapshot.json",
            launch_state_path=Path(tmp) / "launch_state.json",
            launch_watchlist_path=Path(tmp) / "launch_watchlist.json",
            launch_watch_history_path=Path(tmp) / "launch_watch_history.json",
            divergence_state_path=Path(tmp) / "oi_divergence_state.json",
            divergence_cooldown_path=Path(tmp) / "oi_divergence_cooldown.json",
            coinglass_api_key="",
        )
        store = JsonStore(Path(tmp))
        gateway = TelegramGateway(settings, store)
        return settings, store, None, gateway

    def test_telegram_test_defaults_to_dry_run(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test"])

        self.assertEqual(code, 0)
        self.assertIn("telegram_test: dry_run", output.getvalue())

    def test_telegram_test_blocks_real_send_without_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test", "--send"])

        self.assertEqual(code, 2)
        self.assertIn("telegram_test: blocked", output.getvalue())

    def test_readiness_reports_wait_when_history_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["readiness"])

        self.assertEqual(code, 1)
        self.assertIn("真实推送准备度", output.getvalue())
        self.assertIn("WAIT", output.getvalue())

    def test_readiness_rejects_invalid_telegram_token_even_with_history(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            settings = Settings(
                base_dir=settings.base_dir,
                data_dir=settings.data_dir,
                tg_bot_token="",
                tg_chat_id="-1001234567890",
                tg_push_history_path=settings.tg_push_history_path,
                runtime_status_path=settings.runtime_status_path,
                launch_watch_history_path=settings.launch_watch_history_path,
            )
            for idx in range(5):
                store.append_record(settings.launch_watch_history_path, {"top_score": 1, "scanned": 1, "alert_count": 0, "top_symbols": [f"T{idx}"]})

            with redirect_stdout(StringIO()) as output:
                code = main.print_readiness(settings, store)

        self.assertEqual(code, 1)
        self.assertIn("TG_BOT_TOKEN 缺失或格式无效", output.getvalue())

    def test_telegram_test_blocks_invalid_config_before_real_send(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test", "--send", "--confirm-real-send"])

        self.assertEqual(code, 2)
        self.assertIn("invalid Telegram config", output.getvalue())

    def test_live_requires_explicit_real_send_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["live"])

        self.assertEqual(code, 2)
        self.assertIn("真实推送已阻止", output.getvalue())

    def test_runtime_status_reports_empty_before_first_write(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["runtime-status"])

        self.assertEqual(code, 0)
        self.assertIn('"status": "empty"', output.getvalue())

    def test_write_runtime_status_persists_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            payload = main.write_runtime_status(settings, store, "test", "running", task="unit")
            saved = store.load(settings.runtime_status_path, {})

        self.assertEqual(payload["mode"], "test")
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["task"], "unit")

    def test_make_runtime_for_args_applies_scan_limit_overrides(self) -> None:
        with TemporaryDirectory() as tmp:
            args = argparse.Namespace(radar_scan_limit=4, launch_scan_limit=3)
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                settings, _store, _engine, _gateway = main.make_runtime_for_args(args)

        self.assertEqual(settings.radar_scan_limit, 4)
        self.assertEqual(settings.launch_scan_limit, 3)

    def test_coinglass_test_blocks_when_disabled(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["coinglass-test"])

        self.assertEqual(code, 2)
        self.assertIn("COINGLASS_ENABLE=false", output.getvalue())


if __name__ == "__main__":
    unittest.main()
