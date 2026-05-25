from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from config import Settings
from maintenance import migrate_legacy_state


class MaintenanceTests(unittest.TestCase):
    def test_migrate_state_dry_run_does_not_copy(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "bn_signal_history.json").write_text('{"x": 1}', encoding="utf-8")
            settings = Settings(base_dir=base, data_dir=base / "data")

            result = migrate_legacy_state(settings, apply=False)

            self.assertFalse((base / "data" / "bn_signal_history.json").exists())
            actions = {Path(item["source"]).name: item["action"] for item in result["actions"]}
            self.assertEqual(actions["bn_signal_history.json"], "dry_run_copy_available")

    def test_migrate_state_apply_copies_without_deleting_source(self) -> None:
        with TemporaryDirectory() as tmp:
            base = Path(tmp)
            source = base / "bn_signal_history.json"
            source.write_text('{"x": 1}', encoding="utf-8")
            settings = Settings(base_dir=base, data_dir=base / "data")

            result = migrate_legacy_state(settings, apply=True)

            target = base / "data" / "bn_signal_history.json"
            self.assertTrue(source.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), '{"x": 1}')
            actions = {Path(item["source"]).name: item["action"] for item in result["actions"]}
            self.assertEqual(actions["bn_signal_history.json"], "copied")


if __name__ == "__main__":
    unittest.main()
