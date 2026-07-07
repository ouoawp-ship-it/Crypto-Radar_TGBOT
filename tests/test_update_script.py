from __future__ import annotations

import unittest
import os
from pathlib import Path


class UpdateServerScriptTests(unittest.TestCase):
    def test_update_script_runs_stable_check_after_restart_paths(self) -> None:
        script = Path("scripts/update_server.sh").read_text(encoding="utf-8")

        self.assertIn("run_post_update_stable_check()", script)
        self.assertIn('\"$PYTHON_BIN\" main.py stable-check', script)
        self.assertGreaterEqual(script.count("run_post_update_stable_check"), 3)
        self.assertIn("稳定版自检通过，长期运行就绪度", script)
        self.assertIn("稳定版自检未达标，长期运行就绪度需要处理", script)
        self.assertIn("趋势变化", script)
        self.assertIn("发生回退", script)

    def test_https_deploy_check_script_contract(self) -> None:
        path = Path("scripts/check_https_deploy.sh")
        self.assertTrue(path.exists())
        self.assertTrue(os.access(path, os.X_OK))

        script = path.read_text(encoding="utf-8")
        self.assertIn("Paoxx Signal Radar", script)
        self.assertIn("泡泡雷达控制台", script)
        self.assertIn("/public-api/signals", script)
        self.assertIn("/api/dashboard", script)
        self.assertIn("401", script)
        self.assertIn("certbot renew --dry-run", script)
        self.assertIn(".venv/bin/python main.py stable-check", script)
        self.assertIn("curl -sS --connect-timeout", script)
        self.assertNotIn("curl -I", script)
        self.assertIn("sudo ss -ltnp", script)
        self.assertIn("sqlite database is locked", script)
        self.assertIn("BrokenPipeError", script)
        self.assertIn("ConnectionResetError", script)
        self.assertIn("ReadTimeout", script)

    def test_https_deploy_docs_include_public_urls_and_commands(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        install = Path("docs/INSTALL_CN.md").read_text(encoding="utf-8")
        combined = readme + "\n" + install

        self.assertIn("https://paoxx.com/", combined)
        self.assertIn("https://paoxx.com/admin", combined)
        self.assertIn("https://paoxx.com/public-api/*", combined)
        self.assertIn("https://paoxx.com/api/*", combined)
        self.assertIn("scripts/check_https_deploy.sh", combined)
        self.assertIn("--with-stable-check", combined)
        self.assertIn("--with-certbot-dry-run", combined)
        self.assertIn("curl -I https://paoxx.com", combined)
        self.assertIn("501", combined)
        self.assertIn("8080", combined)


if __name__ == "__main__":
    unittest.main()
