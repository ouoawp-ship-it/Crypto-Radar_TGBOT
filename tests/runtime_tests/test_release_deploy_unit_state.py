from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(
    os.name == "posix" and shutil.which("bash") and shutil.which("git"),
    "requires native POSIX bash and git",
)
class ReleaseDeployUnitStateIntegrationTests(unittest.TestCase):
    def test_inactive_disabled_units_remain_inactive_and_disabled(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            checkout = root / "checkout"
            fake_bin = root / "bin"
            state_path = root / "systemctl-state.json"
            systemctl_log = root / "systemctl.log"
            install_log = root / "install.log"
            backup_root = root / "release-backups"
            fake_bin.mkdir()

            subprocess.run(
                ["git", "init", "--bare", str(remote)],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "init", "-b", "main", str(checkout)],
                check=True,
                capture_output=True,
                text=True,
            )

            def git(*args: str) -> str:
                completed = subprocess.run(
                    ["git", *args],
                    cwd=checkout,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                return completed.stdout.strip()

            git("config", "user.name", "Release State Test")
            git("config", "user.email", "release-state@example.invalid")
            git("remote", "add", "origin", str(remote))
            (checkout / "scripts").mkdir()
            (checkout / ".gitignore").write_text(
                ".venv/\n/config/.env.oi\n/data/\n/backups/\n",
                encoding="utf-8",
            )
            (checkout / "VERSION").write_text("v2.0.0\n", encoding="utf-8")
            (checkout / "main.py").write_text("\n", encoding="utf-8")
            (checkout / "requirements.lock").write_text("\n", encoding="utf-8")
            shutil.copy2(
                ROOT / "scripts" / "release_runtime_data.py",
                checkout / "scripts" / "release_runtime_data.py",
            )
            (checkout / "scripts" / "install_server.sh").write_text(
                """#!/usr/bin/env bash
set -Eeuo pipefail
printf 'AUTO_START=%s\\n' "${AUTO_START:-unset}" >"$TEST_INSTALL_LOG"
systemctl enable paopao-radar paopao-market-stream paopao-health.timer paopao-backup.timer paopao-cleanup.timer
if [ "${AUTO_START:-1}" = "1" ]; then
  systemctl restart paopao-radar paopao-market-stream paopao-health.timer paopao-backup.timer paopao-cleanup.timer
fi
""",
                encoding="utf-8",
            )
            git("add", ".gitignore", "VERSION", "main.py", "requirements.lock", "scripts")
            git("commit", "-m", "base release")
            base_commit = git("rev-parse", "HEAD")
            (checkout / "VERSION").write_text("v2.0.1\n", encoding="utf-8")
            git("add", "VERSION")
            git("commit", "-m", "target release")
            git("tag", "-a", "v2.0.1", "-m", "v2.0.1")
            git("push", "origin", "main", "refs/tags/v2.0.1")
            git("checkout", "--detach", base_commit)

            config_dir = checkout / "config"
            config_dir.mkdir()
            env_path = config_dir / ".env.oi"
            env_path.write_text("TG_BOT_TOKEN=not-used\n", encoding="utf-8")
            env_path.chmod(0o600)
            (checkout / "data").mkdir()
            fake_python = checkout / ".venv" / "bin" / "python"
            fake_python.parent.mkdir(parents=True)
            fake_python.write_text(
                "#!/usr/bin/env bash\nprintf '{\"status\":\"ok\"}\\n'\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o700)

            units = (
                "paopao-radar.service",
                "paopao-market-stream.service",
                "paopao-private-control.service",
                "paopao-health.service",
                "paopao-health.timer",
                "paopao-backup.service",
                "paopao-backup.timer",
                "paopao-cleanup.service",
                "paopao-cleanup.timer",
            )
            state_path.write_text(
                json.dumps(
                    {
                        unit: {"active": False, "enabled": False}
                        for unit in units
                    }
                ),
                encoding="utf-8",
            )
            (fake_bin / "systemctl").write_text(
                """#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

state_path = Path(os.environ["TEST_SYSTEMCTL_STATE"])
log_path = Path(os.environ["TEST_SYSTEMCTL_LOG"])
args = sys.argv[1:]
with log_path.open("a", encoding="utf-8") as handle:
    handle.write(" ".join(args) + "\\n")
state = json.loads(state_path.read_text(encoding="utf-8"))
action = args[0]
units = [
    value if "." in value else f"{value}.service"
    for value in args[1:]
    if not value.startswith("-")
]
if action in {"is-active", "is-enabled"}:
    unit = units[-1]
    key = "active" if action == "is-active" else "enabled"
    raise SystemExit(0 if state.get(unit, {}).get(key, False) else 1)
if action == "daemon-reload":
    raise SystemExit(0)
for unit in units:
    record = state.setdefault(unit, {"active": False, "enabled": False})
    if action == "enable":
        record["enabled"] = True
        if "--now" in args:
            record["active"] = True
    elif action == "disable":
        record["enabled"] = False
        if "--now" in args:
            record["active"] = False
    elif action in {"restart", "start"}:
        record["active"] = True
    elif action == "stop":
        record["active"] = False
    else:
        raise SystemExit(2)
state_path.write_text(json.dumps(state), encoding="utf-8")
""",
                encoding="utf-8",
            )
            (fake_bin / "sudo").write_text(
                "#!/usr/bin/env bash\nexec \"$@\"\n",
                encoding="utf-8",
            )
            (fake_bin / "pgrep").write_text(
                "#!/usr/bin/env bash\nprintf '0\\n'\n",
                encoding="utf-8",
            )
            for executable in ("systemctl", "sudo", "pgrep"):
                (fake_bin / executable).chmod(0o700)

            completed = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "deploy_tag.sh"),
                    "--tag",
                    "v2.0.1",
                    "--yes",
                ],
                env={
                    **os.environ,
                    "PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                    "PAOPAO_APP_DIR": str(checkout),
                    "PAOPAO_RELEASE_BACKUP_DIR": str(backup_root),
                    "TEST_SYSTEMCTL_STATE": str(state_path),
                    "TEST_SYSTEMCTL_LOG": str(systemctl_log),
                    "TEST_INSTALL_LOG": str(install_log),
                },
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn(
                "release_readiness_skipped=services_previously_inactive",
                completed.stdout,
            )
            self.assertEqual(
                install_log.read_text(encoding="utf-8").strip(),
                "AUTO_START=0",
            )
            final_state = json.loads(state_path.read_text(encoding="utf-8"))
            for unit in units:
                with self.subTest(unit=unit):
                    self.assertFalse(final_state[unit]["active"])
                    self.assertFalse(final_state[unit]["enabled"])
            self.assertNotIn(
                "restart ",
                systemctl_log.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()
