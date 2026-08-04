from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
MENU = ROOT / "scripts" / "paopao_menu.sh"


class PaopaoMenuTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "POSIX bash is required")
    def test_non_interactive_menu_returns_help(self) -> None:
        bash = shutil.which("bash")
        if not bash:
            self.skipTest("bash is unavailable")
        result = subprocess.run(
            [bash, str(MENU), "help"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("四个雷达运行状态", result.stdout)
        self.assertNotIn("链上活动雷达", result.stdout)

    def test_all_inputs_remain_visible(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertNotIn("read -s", text)
        self.assertNotIn("stty -echo", text)
        self.assertNotIn("getpass", text)

    def test_removed_onchain_surfaces_are_absent(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        for removed in (
            "onchain_main.py",
            "paopao-oar-watch",
            "paopao-oar-query",
            "OAR_WATCH",
            "OAR_AI",
            "ONCHAIN_",
            "链上活动雷达",
        ):
            self.assertNotIn(removed, text)


if __name__ == "__main__":
    unittest.main()
