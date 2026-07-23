from __future__ import annotations

import threading
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from paopao_radar.onchain_flow.cli import main
from paopao_radar.onchain_flow.config import (
    OnchainSettings,
    UnsafeOnchainPath,
)
from paopao_radar.onchain_flow.labels import (
    LabelValidationError,
    load_labels_csv,
)

from .support import make_settings


class OnchainConfigTests(unittest.TestCase):
    def test_env_onchain_overrides_shared_env_without_exposing_secret(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.oi").write_text(
                "TG_BOT_TOKEN=shared-secret\n"
                "TG_CHAT_ID=shared-chat\n"
                "ONCHAIN_TG_HOURLY_LIMIT=4\n",
                encoding="utf-8",
            )
            (root / ".env.onchain").write_text(
                "TG_BOT_TOKEN=onchain-secret\n"
                "ONCHAIN_TG_HOURLY_LIMIT=6\n",
                encoding="utf-8",
            )

            settings = OnchainSettings.load(base_dir=root, environ={})
            diagnostic = settings.diagnostic()

            self.assertEqual(settings.tg_bot_token, "onchain-secret")
            self.assertEqual(settings.tg_chat_id, "shared-chat")
            self.assertEqual(settings.tg_hourly_limit, 6)
            self.assertNotIn("onchain-secret", str(diagnostic))
            self.assertTrue(diagnostic["telegram"]["bot_token_configured"])

    def test_path_guard_rejects_every_production_write_path(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            paths = (
                root / "data" / "signals.db",
                root / "data" / "market_snapshots.db",
                root / "data" / "realtime_features.db",
                root / "data" / "tg_push_history.json",
                root / "data" / "tg_outbox.json",
            )
            fields = (
                "db_path",
                "db_path",
                "db_path",
                "tg_push_history_path",
                "tg_outbox_path",
            )
            for field, path in zip(fields, paths):
                with self.subTest(path=path):
                    with self.assertRaises(UnsafeOnchainPath):
                        replace(settings, **{field: path}).assert_safe_paths()

    def test_path_guard_rejects_write_path_escaping_data_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root)
            with self.assertRaises(UnsafeOnchainPath):
                replace(
                    settings,
                    signal_events_path=root / "elsewhere" / "events.json",
                ).assert_safe_paths()

    def test_disabled_once_and_live_have_zero_side_effects(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root, enable=False)
            with (
                patch("requests.sessions.Session.request") as request_mock,
                patch.object(threading.Thread, "start") as thread_mock,
                patch("paopao_radar.onchain_flow.cli.OnchainStore") as store_mock,
            ):
                self.assertEqual(
                    main(
                        ["once", "--send", "--confirm-real-send"],
                        settings=settings,
                    ),
                    0,
                )
                self.assertEqual(main(["live"], settings=settings), 0)

            request_mock.assert_not_called()
            thread_mock.assert_not_called()
            store_mock.assert_not_called()
            self.assertFalse(settings.data_dir.exists())

    def test_offline_diagnostics_do_not_create_database(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = make_settings(Path(tmp))
            self.assertEqual(main(["status"], settings=settings), 0)
            self.assertEqual(main(["doctor"], settings=settings), 0)
            self.assertEqual(main(["labels-check"], settings=settings), 0)
            self.assertFalse(settings.db_path.exists())


class OnchainLabelTests(unittest.TestCase):
    def test_labels_are_normalized_and_validated(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.csv"
            path.write_text(
                "chain_id,address,entity_name,entity_type,address_type,source,"
                "confidence,valid_from,valid_to\n"
                "8453,0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,"
                "Example,cex,hot,test,0.90,,\n",
                encoding="utf-8",
            )
            labels = load_labels_csv(path)
            self.assertEqual(
                labels[0].address,
                "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            )

    def test_duplicate_label_fails_closed(self) -> None:
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "labels.csv"
            path.write_text(
                "chain_id,address,entity_name,entity_type,address_type,source,"
                "confidence,valid_from,valid_to\n"
                "8453,0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa,"
                "A,cex,hot,test,0.90,,\n"
                "8453,0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,"
                "B,cex,hot,test,0.90,,\n",
                encoding="utf-8",
            )
            with self.assertRaises(LabelValidationError):
                load_labels_csv(path)


if __name__ == "__main__":
    unittest.main()
