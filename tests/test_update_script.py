from __future__ import annotations

import unittest
from pathlib import Path


class UpdateServerScriptTests(unittest.TestCase):
    def test_update_script_runs_stable_check_after_restart_paths(self) -> None:
        script = Path("scripts/update_server.sh").read_text(encoding="utf-8")

        self.assertIn("run_post_update_stable_check()", script)
        self.assertIn('\"$PYTHON_BIN\" main.py stable-check', script)
        self.assertGreaterEqual(script.count("run_post_update_stable_check"), 3)
        self.assertIn("稳定版自检通过，长期运行就绪度", script)
        self.assertIn("稳定版自检未达标，长期运行就绪度需要处理", script)


if __name__ == "__main__":
    unittest.main()
