from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "frontend" / "app" / "calibration" / "page.tsx"
API = ROOT / "frontend" / "lib" / "api.ts"
TYPES = ROOT / "frontend" / "lib" / "types.ts"
ROUTES = ROOT / "frontend" / "lib" / "routes.ts"


class CalibrationFrontendV179Tests(unittest.TestCase):
    def test_calibration_page_and_navigation_exist(self) -> None:
        self.assertTrue(PAGE.exists())
        page = PAGE.read_text(encoding="utf-8")
        routes = ROUTES.read_text(encoding="utf-8")
        self.assertIn('title="模型校准"', page)
        self.assertIn('{ href: "/calibration", label: "模型校准" }', routes)

    def test_page_covers_all_read_only_calibration_sections(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        for text in (
            "Decision 校准表现",
            "生命周期周期表现",
            "15m / 1h / 4h / 24h",
            "资金与市场因子验证",
            "OI、Spot CVD、Futures CVD、Volume 与 Funding",
            "风险警报有效性",
            "模型校准准入",
        ):
            self.assertIn(text, page)
        self.assertIn("不会自动修改 Decision Model 阈值或 Lifecycle Intelligence 权重", page)
        self.assertIn("不构成投资建议，不执行自动交易", page)

    def test_client_uses_only_the_six_public_calibration_endpoints(self) -> None:
        api = API.read_text(encoding="utf-8")
        expected = (
            "/public-api/calibration/summary",
            "/public-api/calibration/decision",
            "/public-api/calibration/lifecycle",
            "/public-api/calibration/factors",
            "/public-api/calibration/risk",
            "/public-api/calibration/readiness",
        )
        for endpoint in expected:
            self.assertIn(f'"{endpoint}"', api)
        calibration_client = api[api.index("export function getCalibrationSummary"):]
        self.assertNotIn('"/api/calibration/', calibration_client)
        self.assertIn('path.startsWith("/public-api/calibration/")', api)

    def test_types_allow_projected_items_and_full_report_sections(self) -> None:
        types = TYPES.read_text(encoding="utf-8")
        for name in (
            "CalibrationMetricItem",
            "CalibrationSummaryPayload",
            "CalibrationSectionPayload",
            "CalibrationReadinessPayload",
        ):
            self.assertIn(f"export type {name}", types)
        for field in (
            "decision_labels?",
            "first_levels?",
            "upgrade_paths?",
            "factors?",
            "risk_alerts?",
        ):
            self.assertIn(field, types)


if __name__ == "__main__":
    unittest.main()
