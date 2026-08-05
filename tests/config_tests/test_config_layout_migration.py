from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.migrate_config_layout import (
    ConfigMigrationError,
    migrate,
)


class ConfigLayoutMigrationTests(unittest.TestCase):
    def _root(self, raw: str) -> Path:
        root = Path(raw)
        config_dir = root / "config"
        config_dir.mkdir()
        (config_dir / ".env.oi.example").write_text(
            "TG_BOT_TOKEN=\nTG_CHAT_ID=\n",
            encoding="utf-8",
        )
        return root

    def test_legacy_file_is_preserved_until_finalize(self) -> None:
        with TemporaryDirectory() as raw:
            root = self._root(raw)
            legacy = root / ".env.oi"
            original = b"TG_BOT_TOKEN=123456:fake-token\nTG_CHAT_ID=-1001\n"
            legacy.write_bytes(original)

            result = migrate(root)

            canonical = root / "config" / ".env.oi"
            self.assertEqual(result["migration"], "legacy_copied")
            self.assertEqual(canonical.read_bytes(), original)
            self.assertEqual(legacy.read_bytes(), original)
            self.assertEqual(
                len(list((root / "backups").glob("config-migration-*"))),
                1,
            )
            self.assertNotIn("fake-token", str(result))

            finalized = migrate(root, finalize=True)
            self.assertEqual(
                finalized["migration"],
                "duplicate_legacy_removed",
            )
            self.assertFalse(legacy.exists())
            self.assertEqual(canonical.read_bytes(), original)

    def test_matching_dual_files_remain_available_before_finalize(self) -> None:
        with TemporaryDirectory() as raw:
            root = self._root(raw)
            legacy = root / ".env.oi"
            canonical = root / "config" / ".env.oi"
            content = "TG_CHAT_ID=-1001\n"
            legacy.write_text(content, encoding="utf-8")
            canonical.write_text(content, encoding="utf-8")

            result = migrate(root)

            self.assertEqual(result["migration"], "ready_to_finalize")
            self.assertTrue(legacy.exists())
            self.assertTrue(canonical.exists())

    def test_sync_changes_can_resume_and_finalize_safely(self) -> None:
        with TemporaryDirectory() as raw:
            root = self._root(raw)
            legacy = root / ".env.oi"
            canonical = root / "config" / ".env.oi"
            legacy.write_text("TG_CHAT_ID=-1001\n", encoding="utf-8")

            migrate(root)
            canonical.write_text(
                "TG_CHAT_ID=-1001\nMAIN_BOT_DELIVERY_MODE=dry_run\n",
                encoding="utf-8",
            )

            resumed = migrate(root)
            finalized = migrate(root, finalize=True)

            self.assertEqual(resumed["migration"], "ready_to_finalize")
            self.assertEqual(
                finalized["migration"],
                "duplicate_legacy_removed",
            )
            self.assertFalse(legacy.exists())
            self.assertIn(
                "MAIN_BOT_DELIVERY_MODE=dry_run",
                canonical.read_text(encoding="utf-8"),
            )

    def test_legacy_change_after_copy_blocks_finalize(self) -> None:
        with TemporaryDirectory() as raw:
            root = self._root(raw)
            legacy = root / ".env.oi"
            canonical = root / "config" / ".env.oi"
            legacy.write_text("TG_CHAT_ID=-1001\n", encoding="utf-8")
            migrate(root)
            canonical.write_text(
                "TG_CHAT_ID=-1001\nMAIN_BOT_DELIVERY_MODE=dry_run\n",
                encoding="utf-8",
            )
            legacy.write_text("TG_CHAT_ID=-1002\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ConfigMigrationError,
                "env_path_conflict",
            ):
                migrate(root, finalize=True)

            self.assertTrue(legacy.exists())
            self.assertTrue(canonical.exists())

    def test_different_old_and_new_files_fail_closed(self) -> None:
        with TemporaryDirectory() as raw:
            root = self._root(raw)
            legacy = root / ".env.oi"
            canonical = root / "config" / ".env.oi"
            legacy.write_text("TG_CHAT_ID=-1001\n", encoding="utf-8")
            canonical.write_text("TG_CHAT_ID=-1002\n", encoding="utf-8")

            with self.assertRaisesRegex(
                ConfigMigrationError,
                "env_path_conflict",
            ):
                migrate(root)

            self.assertEqual(
                legacy.read_text(encoding="utf-8"),
                "TG_CHAT_ID=-1001\n",
            )
            self.assertEqual(
                canonical.read_text(encoding="utf-8"),
                "TG_CHAT_ID=-1002\n",
            )

    def test_missing_config_creates_safe_template_and_stops(self) -> None:
        with TemporaryDirectory() as raw:
            root = self._root(raw)

            result = migrate(root)

            canonical = root / "config" / ".env.oi"
            self.assertEqual(result["status"], "needs_configuration")
            self.assertEqual(result["configuration"], "not_configured")
            self.assertEqual(
                canonical.read_text(encoding="utf-8"),
                "TG_BOT_TOKEN=\nTG_CHAT_ID=\n",
            )

    def test_current_layout_is_idempotent(self) -> None:
        with TemporaryDirectory() as raw:
            root = self._root(raw)
            canonical = root / "config" / ".env.oi"
            canonical.write_text("TG_CHAT_ID=-1001\n", encoding="utf-8")

            first = migrate(root)
            second = migrate(root)

            self.assertEqual(first["migration"], "already_current")
            self.assertEqual(second["migration"], "already_current")
            self.assertEqual(
                canonical.read_text(encoding="utf-8"),
                "TG_CHAT_ID=-1001\n",
            )
