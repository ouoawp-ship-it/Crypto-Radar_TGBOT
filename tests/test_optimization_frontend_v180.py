from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "frontend" / "app" / "optimization" / "page.tsx"
API = ROOT / "frontend" / "lib" / "api.ts"
TYPES = ROOT / "frontend" / "lib" / "types.ts"
ROUTES = ROOT / "frontend" / "lib" / "routes.ts"
API_DOCS = ROOT / "frontend" / "app" / "api-docs" / "page.tsx"


class OptimizationFrontendV180Tests(unittest.TestCase):
    def test_page_and_navigation_exist(self) -> None:
        self.assertTrue(PAGE.exists())
        page = PAGE.read_text(encoding="utf-8")
        routes = ROUTES.read_text(encoding="utf-8")
        self.assertIn('title="模型优化模拟"', page)
        self.assertIn('{ href: "/optimization", label: "模型优化模拟" }', routes)

    def test_page_makes_the_read_only_boundary_explicit(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        for text in (
            "生产模型版本",
            "生产模型 immutable",
            "候选方案与因子 old / new",
            "production / candidate / delta",
            "建议、原因与人工审核",
            "Optimization Readiness",
            "不会自动修改模型",
            "auto_apply=false",
            "仅模拟、不构成投资建议",
        ):
            self.assertIn(text, page)
        self.assertNotIn("自动应用参数", page)

    def test_client_uses_exactly_four_cached_public_endpoints(self) -> None:
        api = API.read_text(encoding="utf-8")
        expected = (
            "/public-api/optimization/summary",
            "/public-api/optimization/scenarios",
            "/public-api/optimization/report",
            "/public-api/optimization/readiness",
        )
        for endpoint in expected:
            self.assertEqual(api.count(f'"{endpoint}"'), 1)
        optimization_client = api[api.index("export function getOptimizationSummary"):]
        self.assertNotIn('"/api/optimization/', optimization_client)
        self.assertIn('path.startsWith("/public-api/optimization/")', api)
        self.assertGreaterEqual(optimization_client.count("revalidateSec: 30"), 4)

    def test_page_requests_each_resource_once_per_load(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        match = re.search(
            r"const results = await Promise\.allSettled\(\[(.*?)\]\);",
            page,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        request_block = match.group(1) if match else ""
        for getter in (
            "getOptimizationSummary",
            "getOptimizationScenarios",
            "getOptimizationReport",
            "getOptimizationReadiness",
        ):
            self.assertEqual(request_block.count(f"{getter}()"), 1)
        self.assertIn('invalidatePublicApiCache("/public-api/optimization/")', page)

    def test_types_accept_projected_and_full_report_shapes(self) -> None:
        types = TYPES.read_text(encoding="utf-8")
        for name in (
            "OptimizationFactorChange",
            "OptimizationComparisonMetric",
            "OptimizationScenarioItem",
            "OptimizationSummaryPayload",
            "OptimizationScenariosPayload",
            "OptimizationReportPayload",
            "OptimizationReadinessPayload",
        ):
            self.assertIn(f"export type {name}", types)
        for field in (
            "production_model?",
            "immutable?",
            "auto_apply?",
            "factor_changes?",
            "comparisons?",
            "manual_review_required?",
        ):
            self.assertIn(field, types)

    def test_public_api_docs_include_optimization_group(self) -> None:
        docs = API_DOCS.read_text(encoding="utf-8")
        self.assertIn('title: "模型优化模拟"', docs)
        for endpoint in (
            "/public-api/optimization/summary",
            "/public-api/optimization/scenarios",
            "/public-api/optimization/report",
            "/public-api/optimization/readiness",
        ):
            self.assertEqual(docs.count(f'"{endpoint}"'), 1)
        self.assertIn("生产模型保持 immutable", docs)
        self.assertIn("auto_apply=false", docs)

    def test_factor_changes_are_authoritative_for_old_new_pairs(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        authoritative = page.index("const authoritative = factorRows(scenario.factor_changes)")
        fallback = page.index("const compatible = factorRows(scenario.parameter_changes || scenario.factors)")
        derived = page.index("const production = asRecord(", authoritative)
        self.assertLess(authoritative, fallback)
        self.assertLess(fallback, derived)
        self.assertIn("if (authoritative.length) return authoritative", page)
        self.assertIn("70 -> 75/80", page)
        self.assertIn("10/10 -> 15/5", page)

    def test_auto_apply_guard_recursively_checks_recommendations(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        self.assertIn("function hasAutoApplyTrue(value: unknown): boolean", page)
        self.assertIn("hasAutoApplyTrue(reportRecommendations)", page)
        self.assertIn("hasAutoApplyTrue(item.recommendations)", page)
        self.assertIn('key === "auto_apply" && item === true', page)
        self.assertIn("安全边界异常：接口返回了 auto_apply=true", page)


if __name__ == "__main__":
    unittest.main()
