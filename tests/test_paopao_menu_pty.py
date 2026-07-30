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
        *,
        extra_env: dict[str, str] | None = None,
        steps: list[tuple[str, str]] | None = None,
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
            fake_python = root / "fake-python"
            fake_python.write_text(
                "#!/usr/bin/env bash\n"
                f"printf 'python %s\\n' \"$*\" >>'{calls}'\n"
                "case \"$*\" in\n"
                "  *'scripts/paopao_config.py status --json'*)\n"
                "    printf '{}\\n'\n"
                "    ;;\n"
                "  *'scripts/paopao_config.py set '*)\n"
                "    key=\"${*: -1}\"\n"
                "    printf '请输入 %s: ' \"$key\"\n"
                "    IFS= read -r _value\n"
                "    printf '{\"status\":\"ok\",\"value\":\"configured\"}\\n'\n"
                "    ;;\n"
                "  *'onchain_main.py status'*)\n"
                "    printf '{}\\n'\n"
                "    ;;\n"
                "  *'onchain_main.py telegram-topic-link bind --stdin'*)\n"
                "    IFS= read -r _link\n"
                "    printf 'TG_ONCHAIN_FLOW_TOPIC_ID=configured\\n'\n"
                "    ;;\n"
                "  *'onchain_main.py ai-prompt show'*)\n"
                "    printf 'existing prompt\\n'\n"
                "    ;;\n"
                "  *'onchain_main.py ai-prompt save --stdin'*)\n"
                "    cat >/dev/null\n"
                "    printf '{\"status\":\"ok\"}\\n'\n"
                "    ;;\n"
                "  *) printf '{}\\n' ;;\n"
                "esac\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)
            fake_editor = root / "fake-editor"
            fake_editor.write_text(
                "#!/usr/bin/env bash\n"
                "printf 'FAKE_OPERATOR_PROMPT_VISIBLE\\n'\n"
                "printf 'FAKE_OPERATOR_PROMPT_VISIBLE\\nsecond line\\n' >\"$1\"\n",
                encoding="utf-8",
            )
            fake_editor.chmod(0o755)
            for name, body in {
                "git": (
                    "#!/usr/bin/env bash\n"
                    f"printf 'git %s\\n' \"$*\" >>'{calls}'\n"
                    "if [ \"${1:-}\" = rev-parse ]; then echo deadbee; fi\n"
                ),
                "systemctl": (
                    "#!/usr/bin/env bash\n"
                    f"printf 'systemctl %s\\n' \"$*\" >>'{calls}'\n"
                    "case \"${1:-}\" in\n"
                    "  is-active)\n"
                    "    echo \"${FAKE_SYSTEMD_ACTIVE:-inactive}\"\n"
                    "    ;;\n"
                    "  cat)\n"
                    "    [ \"${FAKE_SYSTEMD_UNIT_EXISTS:-0}\" = 1 ]\n"
                    "    ;;\n"
                    "  show)\n"
                    "    printf '%s\\n' \"${FAKE_SYSTEMD_MAIN_PID:-0}\"\n"
                    "    ;;\n"
                    "  start|stop|restart)\n"
                    "    [ \"${FAKE_SYSTEMCTL_FAIL_ACTION:-}\" != \"$1\" ]\n"
                    "    ;;\n"
                    "esac\n"
                ),
                "pgrep": (
                    "#!/usr/bin/env bash\n"
                    f"printf 'pgrep %s\\n' \"$*\" >>'{calls}'\n"
                    "if [ -n \"${FAKE_PGREP_OUTPUT:-}\" ]; then\n"
                    "  printf '%s\\n' \"$FAKE_PGREP_OUTPUT\"\n"
                    "  exit 0\n"
                    "fi\n"
                    "exit 1\n"
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
                **(extra_env or {}),
            }
            if env.pop("PAOPAO_TEST_FAKE_PYTHON", "0") == "1":
                env["PAOPAO_PYTHON_BIN"] = str(fake_python)
                env["EDITOR"] = str(fake_editor)
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
            if steps is None:
                os.write(master, user_input.encode("utf-8"))
            chunks: list[bytes] = []
            step_index = 0
            step_offset = 0
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
                    if steps is not None and step_index < len(steps):
                        output_so_far = b"".join(chunks).decode(
                            "utf-8",
                            errors="replace",
                        )
                        expected, response = steps[step_index]
                        if expected in output_so_far[step_offset:]:
                            os.write(master, response.encode("utf-8"))
                            step_index += 1
                            step_offset = len(output_so_far)
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
            if steps is not None:
                self.assertEqual(step_index, len(steps), output)
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

    def test_cex_label_approval_requires_exact_chinese_phrase(
        self,
    ) -> None:
        _, _, denied = self._run_menu(
            "6\n15\n5\ncandidate-1\nwrong\n\n0\n0\n0\n",
            extra_env={"PAOPAO_TEST_FAKE_PYTHON": "1"},
        )
        self.assertFalse([
            line
            for line in denied
            if "label-candidates approve" in line
        ])
        code, output, allowed = self._run_menu(
            "6\n15\n5\ncandidate-1\n批准CEX标签\n\n0\n0\n0\n",
            extra_env={"PAOPAO_TEST_FAKE_PYTHON": "1"},
        )
        self.assertEqual(code, 0, output)
        approvals = [
            line
            for line in allowed
            if "label-candidates approve" in line
        ]
        self.assertEqual(len(approvals), 1)

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
            "5\n20\nwrong\n\n0\n0\n"
        )
        self.assertEqual(code, 0, output)
        self.assertNotIn("ai-cache clear-results", "\n".join(calls))

    def test_missing_oar_unit_returns_to_menu_without_start(self) -> None:
        code, output, calls = self._run_menu("2\n4\n\n0\n0\n")
        self.assertEqual(code, 0, output)
        self.assertIn("服务尚未安装", output)
        self.assertFalse(
            [line for line in calls if line.startswith("systemctl start ")],
        )

    def test_manual_worker_blocks_oar_start(self) -> None:
        code, output, calls = self._run_menu(
            "2\n4\n\n0\n0\n",
            extra_env={
                "FAKE_SYSTEMD_UNIT_EXISTS": "1",
                "FAKE_PGREP_OUTPUT": (
                    "4242 python onchain_main.py watch-live --allow-network"
                ),
            },
        )
        self.assertEqual(code, 0, output)
        self.assertIn("duplicate_writer_risk", output)
        self.assertFalse(
            [line for line in calls if line.startswith("systemctl start ")],
        )

    def test_running_systemd_worker_makes_start_idempotent(self) -> None:
        code, output, calls = self._run_menu(
            "2\n4\n\n0\n0\n",
            extra_env={
                "FAKE_SYSTEMD_UNIT_EXISTS": "1",
                "FAKE_SYSTEMD_MAIN_PID": "321",
                "FAKE_PGREP_OUTPUT": (
                    "321 python onchain_main.py watch-live --allow-network"
                ),
            },
        )
        self.assertEqual(code, 0, output)
        self.assertIn("MainPID=321", output)
        self.assertFalse(
            [line for line in calls if line.startswith("systemctl start ")],
        )

    def test_extra_worker_blocks_oar_restart(self) -> None:
        code, output, calls = self._run_menu(
            "2\n6\n\n0\n0\n",
            extra_env={
                "FAKE_SYSTEMD_UNIT_EXISTS": "1",
                "FAKE_SYSTEMD_MAIN_PID": "321",
                "FAKE_PGREP_OUTPUT": (
                    "321 python onchain_main.py watch-live --allow-network\n"
                    "4242 python onchain_main.py watch-live --allow-network"
                ),
            },
        )
        self.assertEqual(code, 0, output)
        self.assertIn("duplicate_writer_risk", output)
        self.assertFalse(
            [line for line in calls if line.startswith("systemctl restart ")],
        )

    def test_systemctl_start_failure_does_not_exit_menu(self) -> None:
        code, output, calls = self._run_menu(
            "2\n4\n\n0\n0\n",
            extra_env={
                "FAKE_SYSTEMD_UNIT_EXISTS": "1",
                "FAKE_SYSTEMCTL_FAIL_ACTION": "start",
            },
        )
        self.assertEqual(code, 0, output)
        self.assertIn("OAR Watch 启动失败", output)
        starts = [
            line for line in calls if line.startswith("systemctl start ")
        ]
        self.assertEqual(len(starts), 1)

    def test_sensitive_menu_values_are_echoed_once_and_saved_redacted(
        self,
    ) -> None:
        values = {
            "bot": "123456:FAKE_VISIBLE_BOT_TOKEN",
            "chat": "-1001234567890",
            "rpc": "https://fake-rpc.invalid/v2/visible-key",
            "ai": "FAKE_VISIBLE_DEEPSEEK_KEY",
            "topic": "424242",
            "link": "https://t.me/c/1234567890/42/99",
        }
        code, output, calls = self._run_menu(
            "",
            extra_env={"PAOPAO_TEST_FAKE_PYTHON": "1"},
            steps=[
                ("请选择：", "4\n"),
                ("10. 设置 Base RPC 最大区块范围", "2\n"),
                ("请输入 TG_BOT_TOKEN: ", values["bot"] + "\n"),
                ('"configured"', "\n"),
                ("10. 设置 Base RPC 最大区块范围", "3\n"),
                ("请输入 TG_CHAT_ID: ", values["chat"] + "\n"),
                ('"configured"', "\n"),
                ("10. 设置 Base RPC 最大区块范围", "6\n"),
                (
                    "请输入 ONCHAIN_BASE_HTTP_RPC_URL: ",
                    values["rpc"] + "\n",
                ),
                ('"configured"', "\n"),
                ("10. 设置 Base RPC 最大区块范围", "7\n"),
                ("请输入 OAR_AI_API_KEY: ", values["ai"] + "\n"),
                ('"configured"', "\n"),
                ("10. 设置 Base RPC 最大区块范围", "0\n"),
                ("请选择：", "7\n"),
                ("5. 主 BOT readiness", "2\n"),
                (
                    "请输入 TG_ONCHAIN_FLOW_TOPIC_ID: ",
                    values["topic"] + "\n",
                ),
                ('"configured"', "\n"),
                ("5. 主 BOT readiness", "3\n"),
                ("消息链接：", values["link"] + "\n"),
                ("链上 Topic：configured", "\n"),
                ("5. 主 BOT readiness", "0\n"),
                ("请选择：", "0\n"),
            ],
        )
        self.assertEqual(code, 0, output)
        for value in values.values():
            self.assertEqual(output.count(value), 1, value)
            self.assertNotIn(value, "\n".join(calls))
        self.assertGreaterEqual(output.count("configured"), 6)

    def test_prompt_editor_displays_prompt_body_in_pty(self) -> None:
        code, output, calls = self._run_menu(
            "",
            extra_env={"PAOPAO_TEST_FAKE_PYTHON": "1"},
            steps=[
                ("请选择：", "5\n"),
                ("22. 禁用 AI", "13\n"),
                ("FAKE_OPERATOR_PROMPT_VISIBLE", "\n"),
                ("22. 禁用 AI", "0\n"),
                ("请选择：", "0\n"),
            ],
        )
        self.assertEqual(code, 0, output)
        self.assertIn("FAKE_OPERATOR_PROMPT_VISIBLE", output)
        self.assertTrue(
            any("ai-prompt save --stdin" in line for line in calls),
            calls,
        )


if __name__ == "__main__":
    unittest.main()
