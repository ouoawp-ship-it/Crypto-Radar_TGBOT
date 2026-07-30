from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
UNIT = ROOT / "ops" / "systemd" / "paopao-oar-watch.service"
INSTALLER = ROOT / "scripts" / "install_oar_watch.sh"
MENU = ROOT / "scripts" / "paopao_menu.sh"


class OarWatchServiceTests(unittest.TestCase):
    def test_unit_runs_only_bounded_observe_worker(self) -> None:
        text = UNIT.read_text(encoding="utf-8")
        exec_line = next(
            line for line in text.splitlines() if line.startswith("ExecStart=")
        )
        self.assertIn("onchain_main.py watch-live --allow-network", exec_line)
        for forbidden in (
            "--notify-dry-run",
            "--with-ai",
            "--send",
            "--confirm-real-send",
        ):
            self.assertNotIn(forbidden, exec_line)

    def test_unit_uses_private_onchain_environment_and_non_root_user(
        self,
    ) -> None:
        text = UNIT.read_text(encoding="utf-8")
        self.assertIn("User=ubuntu", text)
        self.assertIn(
            "EnvironmentFile=/home/ubuntu/paopao-crypto-radar/.env.onchain",
            text,
        )
        self.assertIn("Restart=on-failure", text)
        self.assertIn("KillSignal=SIGINT", text)
        self.assertIn("NoNewPrivileges=true", text)
        self.assertIn("PrivateTmp=true", text)
        self.assertIn("UMask=0077", text)

    def test_installer_is_oar_only_and_rejects_duplicate_writer(self) -> None:
        text = INSTALLER.read_text(encoding="utf-8")
        self.assertIn("duplicate_writer_risk", text)
        self.assertIn("pgrep -af '[o]nchain_main.py.*watch-live'", text)
        self.assertIn("systemctl daemon-reload", text)
        self.assertIn("service_started=false", text)
        self.assertNotIn("enable --now", text)
        self.assertNotIn("paopao-radar", text)
        self.assertNotIn("paopao-market-stream", text)

    def test_menu_has_enforcing_worker_guard(self) -> None:
        text = MENU.read_text(encoding="utf-8")
        self.assertIn("show_oar_workers()", text)
        self.assertIn("assert_no_conflicting_oar_worker()", text)
        self.assertIn("duplicate_writer_risk", text)
        self.assertNotIn("duplicate_worker_check()", text)


if __name__ == "__main__":
    unittest.main()
