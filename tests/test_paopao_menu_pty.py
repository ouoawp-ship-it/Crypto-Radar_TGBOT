from __future__ import annotations

import errno
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

if os.name != "nt":
    import pty
    import select


ROOT = Path(__file__).resolve().parents[1]
MENU = ROOT / "scripts" / "paopao_menu.sh"


@unittest.skipIf(os.name == "nt", "POSIX PTY is required")
class PaopaoMenuPtyTests(unittest.TestCase):
    def _run_menu(
        self,
        user_input: str,
    ) -> tuple[int, str, list[str]]:
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            calls = root / "calls.log"
            update = root / "update.sh"
            update.write_text(
                "#!/usr/bin/env bash\n"
                f"printf 'update %s\\n' \"$*\" >>'{calls}'\n",
                encoding="utf-8",
            )
            update.chmod(0o755)
            for name, body in {
                "git": (
                    "#!/usr/bin/env bash\n"
                    f"printf 'git %s\\n' \"$*\" >>'{calls}'\n"
                    "if [ \"${1:-}\" = rev-parse ]; then echo deadbee; fi\n"
                ),
                "systemctl": (
                    "#!/usr/bin/env bash\n"
                    f"printf 'systemctl %s\\n' \"$*\" >>'{calls}'\n"
                    "if [ \"${1:-}\" = is-active ]; then echo inactive; fi\n"
                ),
                "sudo": (
                    "#!/usr/bin/env bash\n"
                    f"printf 'sudo %s\\n' \"$*\" >>'{calls}'\n"
                    'exec "$@"\n'
                ),
            }.items():
                path = fake_bin / name
                path.write_text(body, encoding="utf-8")
                path.chmod(0o755)
            env = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PAOPAO_APP_DIR": str(ROOT),
                "PAOPAO_PYTHON_BIN": sys.executable,
                "PAOPAO_MENU_NO_CLEAR": "1",
                "PAOPAO_UPDATE_SCRIPT": str(update),
            }
            master, slave = pty.openpty()
            process = subprocess.Popen(
                ["bash", str(MENU)],
                cwd=ROOT,
                env=env,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                close_fds=True,
                start_new_session=True,
            )
            os.close(slave)
            os.write(master, user_input.encode("utf-8"))
            chunks: list[bytes] = []
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                ready, _, _ = select.select([master], [], [], 0.2)
                if ready:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError as exc:
                        if exc.errno == errno.EIO:
                            break
                        raise
                    if not chunk:
                        break
                    chunks.append(chunk)
                if process.poll() is not None and not ready:
                    break
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
                self.fail("interactive menu did not exit within timeout")
            os.close(master)
            output = b"".join(chunks).decode("utf-8", errors="replace")
            call_lines = (
                calls.read_text(encoding="utf-8").splitlines()
                if calls.exists()
                else []
            )
            return process.returncode, output, call_lines

    def test_open_and_exit_shows_chinese_menu_without_network_actions(
        self,
    ) -> None:
        code, output, calls = self._run_menu("0\n")
        self.assertEqual(code, 0, output)
        self.assertIn("FinalShell 中文运维菜单", output)
        self.assertIn("请选择", output)
        self.assertNotIn("Traceback", output)
        joined = "\n".join(calls)
        for forbidden in (
            "git fetch",
            "update ",
            "curl",
            "provider-check",
            "ai-provider-check",
            "ai-smoke",
            "token-activity",
            "token-report",
            "telegram-test",
        ):
            self.assertNotIn(forbidden, joined)

    def test_update_requires_exact_phrase_and_runs_once_when_confirmed(
        self,
    ) -> None:
        _, _, denied = self._run_menu("3\n3\nwrong\n\n0\n0\n")
        self.assertFalse(
            [line for line in denied if line.startswith("update ")],
        )
        code, output, allowed = self._run_menu(
            "3\n3\n执行安全更新\n\n0\n0\n"
        )
        self.assertEqual(code, 0, output)
        updates = [
            line for line in allowed if line.startswith("update ")
        ]
        self.assertEqual(updates, ["update --yes"])

    def test_main_service_restart_requires_exact_phrase(self) -> None:
        _, _, denied = self._run_menu("2\n2\nwrong\n\n0\n0\n")
        self.assertFalse(
            [
                line for line in denied
                if line.startswith("systemctl restart ")
            ],
        )
        code, output, allowed = self._run_menu(
            "2\n2\n重启主服务\n\n0\n0\n"
        )
        self.assertEqual(code, 0, output)
        restarts = [
            line for line in allowed
            if line.startswith("systemctl restart ")
        ]
        self.assertEqual(len(restarts), 1)

    def test_cache_menu_uses_controlled_cli_and_never_rm(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("run_onchain ai-cache clear-results", text)
        self.assertNotIn("rm -f", text[text.index("clear_ai_cache()"):text.index(
            "config_rollback()"
        )])
        code, output, calls = self._run_menu(
            "5\n18\nwrong\n\n0\n0\n"
        )
        self.assertEqual(code, 0, output)
        self.assertNotIn("ai-cache clear-results", "\n".join(calls))


if __name__ == "__main__":
    unittest.main()
