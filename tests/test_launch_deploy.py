from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


class LaunchDeployTemplateTests(unittest.TestCase):
    def test_systemd_service_template_has_safe_production_defaults(self) -> None:
        path = ROOT / "deploy" / "paopao-launch-radar.service"
        text = path.read_text(encoding="utf-8")

        self.assertIn("EnvironmentFile=-__PROJECT_DIR__/.env.oi", text)
        self.assertIn("--web-host 127.0.0.1", text)
        self.assertIn("--web-port 18090", text)
        self.assertIn("--web-mode real", text)
        self.assertIn("Restart=always", text)
        self.assertIn("RestartSec=5", text)
        self.assertNotIn("--web-host 0.0.0.0", text)

    def test_nginx_template_proxies_page_and_api_with_forwarded_headers(self) -> None:
        path = ROOT / "deploy" / "nginx-paoxx-launch-radar.conf"
        text = path.read_text(encoding="utf-8")

        self.assertIn("location = /launch-radar", text)
        self.assertIn("proxy_pass http://127.0.0.1:18090/launch-radar", text)
        self.assertIn("location ^~ /api/", text)
        self.assertIn("proxy_pass http://127.0.0.1:18090/api/", text)
        self.assertIn("proxy_set_header Host $host", text)
        self.assertIn("proxy_set_header X-Real-IP $remote_addr", text)
        self.assertIn("proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for", text)
        self.assertIn("proxy_set_header X-Forwarded-Proto $scheme", text)

    def test_install_script_does_not_overwrite_nginx_config(self) -> None:
        path = ROOT / "deploy" / "install_launch_radar_web.sh"
        text = path.read_text(encoding="utf-8")

        self.assertIn("compileall", text)
        self.assertIn("systemctl enable --now", text)
        self.assertIn("journalctl -u ${SERVICE_NAME}.service -f", text)
        self.assertIn("does not overwrite your existing Nginx site config", text)
        self.assertNotIn("cp ${NGINX_TEMPLATE}", text)
        self.assertNotIn("install -m 0644 \"${NGINX_TEMPLATE}\"", text)


if __name__ == "__main__":
    unittest.main()
