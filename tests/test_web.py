from __future__ import annotations

import unittest

from paopao_radar import web
from paopao_radar.web_services.jobs import JOB_SPECS, LONG_ACTION_JOB_TYPES


class WebSurfaceTests(unittest.TestCase):
    def test_public_surface_only_exposes_signals(self) -> None:
        self.assertIn("/public-api/signals", web.PUBLIC_INDEX_HTML)
        self.assertIn("/admin", web.PUBLIC_INDEX_HTML)

    def test_admin_surface_contains_only_operational_pages(self) -> None:
        for page in ("运行总览", "信号记录", "雷达服务", "任务中心", "日志中心", "配置中心", "审计记录"):
            self.assertIn(page, web.INDEX_HTML)

    def test_config_surface_has_core_signal_keys(self) -> None:
        keys = {field.key for field in web.EDITABLE_CONFIG_FIELDS}
        self.assertIn("SIGNAL_EVENTS_DB_FILE", keys)

    def test_job_surface_is_operational_only(self) -> None:
        self.assertEqual(set(JOB_SPECS), {"stable-check", "doctor", "readiness", "cleanup", "update-check", "api-self-test"})
        self.assertEqual(LONG_ACTION_JOB_TYPES, {"stable-check", "doctor", "readiness", "cleanup"})


if __name__ == "__main__":
    unittest.main()
