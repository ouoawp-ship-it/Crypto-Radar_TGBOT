from __future__ import annotations

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", path],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


class GitIgnoreHardeningTests(unittest.TestCase):
    def test_runtime_env_backups_are_ignored(self) -> None:
        self.assertTrue(is_ignored(".env.oi.bak.20260710_000000"))
        self.assertTrue(is_ignored("runtime-config.bak"))
        self.assertTrue(is_ignored("data/tg_push_history.json.lock"))

    def test_example_env_files_remain_trackable(self) -> None:
        self.assertFalse(is_ignored(".env.example"))
        self.assertFalse(is_ignored(".env.oi.example"))


if __name__ == "__main__":
    unittest.main()
