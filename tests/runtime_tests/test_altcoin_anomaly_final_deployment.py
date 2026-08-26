from __future__ import annotations

import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest


ROOT = Path(__file__).resolve().parents[2]


class AltcoinAnomalyFinalDeploymentTests(unittest.TestCase):
    def read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def test_market_stream_systemd_uses_explicit_fail_closed_wrapper(self) -> None:
        runner = self.read("scripts/run_market_stream.sh")
        installer = self.read("scripts/install_market_stream_service.sh")

        self.assertIn("ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_ENABLE", runner)
        self.assertIn('args=("$PYTHON_BIN" "${APP_DIR}/main.py" market-stream "--altcoin-production")', runner)
        self.assertIn('args+=("--send" "--confirm-real-send")', runner)
        self.assertIn("ENABLE_ALTCOIN_ANOMALY_REAL_SEND", runner)
        self.assertIn("TG_ALTCOIN_CONTRACT_ANOMALY_TOPIC_ID", runner)
        self.assertIn("altcoin_production_real_send_gate_blocked", runner)
        self.assertIn("ExecStart=${APP_DIR}/scripts/run_market_stream.sh", installer)
        self.assertIn("RestartPreventExitStatus=2", installer)
        self.assertIn("Restart=on-failure", installer)
        self.assertNotIn("main.py market-stream", installer)

    def test_release_tag_deploy_has_immutable_identity_gates(self) -> None:
        script = self.read("scripts/deploy_tag.sh")

        self.assertIn("tracked_worktree_not_clean", script)
        self.assertIn('git status --porcelain)', script)
        self.assertNotIn("--untracked-files=no", script)
        self.assertIn('git cat-file -t "refs/tags/${tag}"', script)
        self.assertIn("annotated_release_tag_required", script)
        self.assertIn('remote_main="$(git rev-parse FETCH_HEAD)"', script)
        self.assertIn('git merge-base --is-ancestor "$commit" "$remote_main"', script)
        self.assertIn('git show "${commit}:VERSION"', script)
        self.assertIn('git show --check --format= "$commit"', script)
        self.assertIn("release_tag_version_mismatch", script)
        self.assertIn('git -C "$APP_DIR" checkout --detach "$commit"', script)
        self.assertIn("bounded_p2_process_conflict", script)
        self.assertIn("multiple_market_stream_processes_detected", script)
        self.assertIn('flock -n 9 || fail "release_deployment_already_running"', script)
        self.assertNotIn("git reset --hard", script)
        self.assertNotIn("git clean", script)
        self.assertNotIn("--force", script)
        self.assertIn("--refresh-pulse-topic-intro", script)
        self.assertIn("telegram-topic-refresh", script)
        self.assertIn("--send --confirm-real-send", script)
        self.assertIn("pulse_topic_intro_refresh_requires_deploy_mode", script)

    def test_release_backup_and_rollback_cover_runtime_boundaries(self) -> None:
        script = self.read("scripts/deploy_tag.sh")

        self.assertIn('install -m 0600 "$APP_DIR/config/.env.oi"', script)
        self.assertIn('"$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" database-backup', script)
        self.assertIn('"$APP_DIR/scripts/release_runtime_data.py" backup', script)
        self.assertIn('--exclude-root "$BACKUP_ROOT"', script)
        self.assertIn('"$backup_dir/release-runtime-data.py" restore', script)
        self.assertIn("systemd-inventory.tsv", script)
        self.assertIn("unit-state.tsv", script)
        self.assertIn("SHA256SUMS", script)
        self.assertIn("sha256sum --check --quiet", script)
        self.assertIn('install -m 0600 "$backup_dir/config/.env.oi"', script)
        self.assertIn('git -C "$APP_DIR" checkout --detach "$previous_commit"', script)
        self.assertIn("trap handle_exit EXIT", script)
        self.assertNotIn("trap rollback_on_error ERR", script)
        self.assertIn("ROLLBACK_READY=0", script)
        self.assertIn("release_rollback_completed=true", script)
        self.assertIn('bash "$APP_DIR/scripts/install_server.sh"', script)
        self.assertIn('"$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" readiness', script)
        self.assertIn('"$APP_DIR/.venv/bin/python" "$APP_DIR/main.py" stable-check --no-save', script)

    def test_release_deploy_uses_config_preflight_then_bounded_runtime_poll(self) -> None:
        deploy = self.read("scripts/deploy_tag.sh")
        install = self.read("scripts/install_server.sh")

        self.assertIn("scripts/validate_runtime_config.py", install)
        self.assertNotIn("main.py stable-check --no-save\n  local code", install)
        self.assertIn("PAOPAO_RELEASE_READINESS_TIMEOUT_SEC", deploy)
        self.assertIn("PAOPAO_RELEASE_READINESS_INTERVAL_SEC", deploy)
        self.assertIn("PAOPAO_RELEASE_READINESS_SUCCESSES", deploy)
        self.assertIn("wait_for_deployed_runtime", deploy)
        self.assertIn("READINESS_REQUIRED_SUCCESSES", deploy)
        self.assertIn("release_readiness_timeout", deploy)

    def test_release_success_restores_previous_unit_policy(self) -> None:
        deploy = self.read("scripts/deploy_tag.sh")

        self.assertIn("PYTHON_BIN=python3 AUTO_START=0", deploy)
        restore_index = deploy.index(
            'restore_unit_activity "$CREATED_BACKUP/unit-state.tsv"'
        )
        verify_index = deploy.index(
            'verify_unit_activity "$CREATED_BACKUP/unit-state.tsv"'
        )
        readiness_index = deploy.index(
            'if full_runtime_was_active "$CREATED_BACKUP/unit-state.tsv"'
        )
        self.assertLess(restore_index, verify_index)
        self.assertLess(verify_index, readiness_index)
        self.assertIn("unit_enabled_state_mismatch", deploy)
        self.assertIn(
            "release_readiness_skipped=services_previously_inactive",
            deploy,
        )

    def test_release_tag_push_runs_the_same_ci_suite(self) -> None:
        workflow = self.read(".github/workflows/tests.yml")

        self.assertIn('tags:\n      - "v*"', workflow)
        self.assertIn("python -m compileall -q", workflow)
        self.assertIn("python -m unittest discover", workflow)

    def test_release_version_and_final_runbook_are_consistent(self) -> None:
        self.assertEqual(self.read("VERSION").strip(), "v2.1.0")
        readme = self.read("README.md")
        runbook = self.read("docs/ALTCOIN_CONTRACT_ANOMALY_FINAL_CN.md")

        self.assertIn("ALTCOIN_CONTRACT_ANOMALY_FINAL_CN.md", readme)
        self.assertIn("deploy_tag.sh --tag v2.1.0 --yes", readme)
        self.assertIn("每个进程只有一个", runbook)
        self.assertIn("不会自动创建该话题", runbook)
        self.assertIn("不代表综合分数、成功率或涨跌概率", runbook)
        self.assertIn("ALTCOIN_CONTRACT_ANOMALY_PRODUCTION_SEND_ENABLE=false", runbook)
        self.assertIn("--rollback /home/ubuntu/paopao-crypto-radar", runbook)

    @unittest.skipIf(os.name == "nt", "Windows bash is a WSL launcher")
    def test_shell_entry_points_parse(self) -> None:
        for relative in (
            "scripts/deploy_tag.sh",
            "scripts/run_market_stream.sh",
            "scripts/install_market_stream_service.sh",
        ):
            with self.subTest(relative=relative):
                subprocess.run(
                    ["bash", "-n", str(ROOT / relative)],
                    check=True,
                    cwd=ROOT,
                    capture_output=True,
                    text=True,
                )

    @unittest.skipIf(os.name == "nt", "Windows bash is a WSL launcher")
    def test_tag_preflight_accepts_only_annotated_main_release(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            remote = root / "remote.git"
            checkout = root / "checkout"
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

            def git(*args: str) -> None:
                subprocess.run(
                    ["git", *args],
                    cwd=checkout,
                    check=True,
                    capture_output=True,
                    text=True,
                )

            git("config", "user.name", "Test Release")
            git("config", "user.email", "release@example.invalid")
            git("remote", "add", "origin", str(remote))
            (checkout / "VERSION").write_text("v2.0.1\n", encoding="utf-8")
            git("add", "VERSION")
            git("commit", "-m", "release")
            git("tag", "-a", "v2.0.1", "-m", "v2.0.1")
            git("push", "origin", "main", "refs/tags/v2.0.1")

            env = {**os.environ, "PAOPAO_APP_DIR": str(checkout), "REMOTE": "origin"}
            accepted = subprocess.run(
                ["bash", str(ROOT / "scripts" / "deploy_tag.sh"), "--check-tag", "v2.0.1"],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertEqual(accepted.returncode, 0, accepted.stderr)
            self.assertIn("release_tag_valid=true", accepted.stdout)

            unexpected = checkout / "unexpected-source.py"
            unexpected.write_text("raise SystemExit\n", encoding="utf-8")
            dirty = subprocess.run(
                [
                    "bash",
                    str(ROOT / "scripts" / "deploy_tag.sh"),
                    "--check-tag",
                    "v2.0.1",
                ],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(dirty.returncode, 0)
            self.assertIn("tracked_worktree_not_clean", dirty.stderr)
            unexpected.unlink()

            (checkout / "VERSION").write_text("v2.0.2\n", encoding="utf-8")
            git("add", "VERSION")
            git("commit", "-m", "lightweight release")
            git("tag", "v2.0.2")
            git("push", "origin", "main", "refs/tags/v2.0.2")
            rejected = subprocess.run(
                ["bash", str(ROOT / "scripts" / "deploy_tag.sh"), "--check-tag", "v2.0.2"],
                env=env,
                check=False,
                capture_output=True,
                text=True,
            )
            self.assertNotEqual(rejected.returncode, 0)
            self.assertIn("annotated_release_tag_required", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
