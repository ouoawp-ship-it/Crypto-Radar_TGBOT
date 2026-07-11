from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "frontend" / "app" / "models" / "page.tsx"
API = ROOT / "frontend" / "lib" / "api.ts"
ADMIN = ROOT / "frontend" / "lib" / "modelRegistryAdmin.ts"
TYPES = ROOT / "frontend" / "lib" / "types.ts"
ROUTES = ROOT / "frontend" / "lib" / "routes.ts"
API_DOCS = ROOT / "frontend" / "app" / "api-docs" / "page.tsx"


class ModelRegistryFrontendV181Tests(unittest.TestCase):
    def test_page_and_navigation_exist(self) -> None:
        self.assertTrue(PAGE.exists())
        page = PAGE.read_text(encoding="utf-8")
        routes = ROUTES.read_text(encoding="utf-8")
        self.assertIn('title="模型管理"', page)
        self.assertIn('{ href: "/models", label: "模型管理" }', routes)

    def test_page_makes_manual_approval_boundary_explicit(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        for text in (
            "所有修改需要人工确认",
            "不会自动应用模型",
            "simulation → production 禁止自动跳转",
            "批准（不启用）",
            "人工启用 Production",
            "拒绝",
            "回滚",
            "Model Health Monitor",
            "Model Performance Timeline",
        ):
            self.assertIn(text, page)
        self.assertIn("window.confirm", page)

    def test_public_client_uses_four_cached_projected_endpoints(self) -> None:
        api = API.read_text(encoding="utf-8")
        endpoints = (
            "/public-api/models/current",
            "/public-api/models/history",
            "/public-api/models/performance",
            "/public-api/models/health",
        )
        for endpoint in endpoints:
            self.assertEqual(api.count(f'"{endpoint}"'), 1)
        model_client = api[api.index("export function getCurrentModel"):]
        self.assertGreaterEqual(model_client.count("revalidateSec: 30"), 4)
        self.assertIn('path.startsWith("/public-api/models/")', api)

    def test_page_requests_public_resources_once_per_load(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        match = re.search(
            r"const results = await Promise\.allSettled\(\[(.*?)\]\);",
            page,
            re.DOTALL,
        )
        self.assertIsNotNone(match)
        request_block = match.group(1) if match else ""
        for getter in (
            "getCurrentModel",
            "getModelHistory",
            "getModelPerformance",
            "getModelHealth",
        ):
            self.assertEqual(request_block.count(f"{getter}()"), 1)
        self.assertIn('invalidatePublicApiCache("/public-api/models/")', page)

    def test_private_actions_reuse_authenticated_session_without_tokens(self) -> None:
        client = ADMIN.read_text(encoding="utf-8")
        for endpoint in (
            "/api/models/list",
            "/api/models/diff",
            "/api/models/register",
            "/api/models/approve",
            "/api/models/reject",
            "/api/models/rollback",
        ):
            self.assertIn(endpoint, client)
        self.assertIn('credentials: "same-origin"', client)
        self.assertIn('cache: "no-store"', client)
        self.assertIn('"/api/auth/status"', client)
        self.assertIn('"X-CSRF-Token": csrfToken', client)
        self.assertIn("需要先登录后台", client)
        self.assertNotIn("localStorage", client)
        self.assertNotIn("Authorization", client)

    def test_actions_submit_jobs_and_never_send_arbitrary_parameters(self) -> None:
        page = PAGE.read_text(encoding="utf-8")
        client = ADMIN.read_text(encoding="utf-8")
        self.assertIn("submitModelRegistryJob", page)
        self.assertIn("job_id=", page)
        self.assertIn("activate: action === \"activate\"", page)
        self.assertIn("confirm_production:", page)
        self.assertNotIn("parameters_json", page)
        self.assertNotIn("parameters_json", client)
        self.assertNotIn("model_hash", page)
        self.assertNotIn("source_commit", page)

    def test_types_cover_public_projection_diff_and_job_shapes(self) -> None:
        types = TYPES.read_text(encoding="utf-8")
        for name in (
            "PublicModelItem",
            "ModelCurrentPayload",
            "ModelHistoryPayload",
            "ModelPerformancePayload",
            "ModelHealthPayload",
            "PrivateModelItem",
            "ModelDiffChange",
            "ModelDiffPayload",
            "ModelJobPayload",
        ):
            self.assertIn(f"export type {name}", types)

    def test_api_docs_include_projected_model_registry_group(self) -> None:
        docs = API_DOCS.read_text(encoding="utf-8")
        self.assertIn('title: "模型注册与表现"', docs)
        for endpoint in (
            "/public-api/models/current",
            "/public-api/models/history",
            "/public-api/models/performance",
            "/public-api/models/health",
        ):
            self.assertEqual(docs.count(f'"{endpoint}"'), 1)
        self.assertIn("不公开完整参数或内部配置", docs)
        self.assertIn("不会自动应用模型", docs)


if __name__ == "__main__":
    unittest.main()
