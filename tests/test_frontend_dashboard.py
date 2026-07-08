from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "frontend"


def read_frontend_sources() -> str:
    parts: list[str] = []
    for pattern in ("app/**/*.tsx", "components/**/*.tsx", "lib/**/*.ts", "styles/**/*.css"):
        for path in FRONTEND.glob(pattern):
            parts.append(path.read_text(encoding="utf-8"))
    return "\n".join(parts)


class NextjsPublicDashboardTests(unittest.TestCase):
    def test_frontend_project_contract(self) -> None:
        package_path = FRONTEND / "package.json"
        self.assertTrue(package_path.exists())
        package = json.loads(package_path.read_text(encoding="utf-8"))

        self.assertEqual(package.get("name"), "paoxx-public-dashboard")
        self.assertIn("build", package.get("scripts", {}))
        self.assertIn("start", package.get("scripts", {}))
        self.assertIn("typecheck", package.get("scripts", {}))
        self.assertIn("next", package.get("dependencies", {}))
        self.assertIn("react", package.get("dependencies", {}))
        self.assertIn("tailwindcss", package.get("devDependencies", {}))
        self.assertIn("recharts", package.get("dependencies", {}))

        for relative in (
            "app/page.tsx",
            "app/radar/page.tsx",
            "app/decision/page.tsx",
            "app/outcomes/page.tsx",
            "app/backtest/page.tsx",
            "app/coin/[symbol]/page.tsx",
            "app/api-docs/page.tsx",
            "lib/api.ts",
            "styles/globals.css",
        ):
            self.assertTrue((FRONTEND / relative).exists(), relative)

    def test_public_frontend_uses_only_public_api(self) -> None:
        api_source = (FRONTEND / "lib/api.ts").read_text(encoding="utf-8")

        for path in (
            "/public-api/signals",
            "/public-api/signals/stats",
            "/public-api/signal-timeline",
            "/public-api/decisions",
            "/public-api/decisions/stats",
            "/public-api/decision",
            "/public-api/outcomes",
            "/public-api/outcomes/stats",
            "/public-api/symbol-outcomes",
            "/public-api/backtest/decision",
            "/public-api/backtest/decision/matrix",
            "/public-api/backtest/decision/detail",
        ):
            self.assertIn(path, api_source)

        forbidden = (
            "/api/dashboard",
            "/api/jobs",
            "/api/config",
            "/api/audit",
            "/api/logs",
            "/api/decision",
            "/api/decisions",
            "/api/outcomes",
            "WEB_ADMIN_TOKEN",
            "WEB_SESSION_SECRET",
            "WEB_ADMIN_PASSWORD_HASH",
            "Authorization",
            "Cookie",
        )
        for text in forbidden:
            self.assertNotIn(text, api_source)

    def test_public_frontend_chinese_dashboard_copy(self) -> None:
        source = read_frontend_sources()
        for text in (
            "Paoxx 信号雷达",
            "paoxx-frontend",
            "nextjs-dashboard",
            "专业加密数据仪表盘",
            "信号雷达",
            "决策模型",
            "结果追踪",
            "决策回测",
            "模型诊断",
            "样本质量",
            "平均最终涨跌",
            "正收益比例",
            "风险警报",
            "可试仓",
            "禁止追高",
            "等待回踩",
            "公开 API",
            "后台控制台",
        ):
            self.assertIn(text, source)

    def test_deploy_scripts_include_frontend_build_and_service(self) -> None:
        install = (ROOT / "scripts/install_server.sh").read_text(encoding="utf-8")
        update = (ROOT / "scripts/update_server.sh").read_text(encoding="utf-8")
        check = (ROOT / "scripts/check_https_deploy.sh").read_text(encoding="utf-8")
        menu = (ROOT / "scripts/paopao_menu.sh").read_text(encoding="utf-8")
        combined = "\n".join([install, update, check, menu])

        self.assertIn("paopao-frontend", combined)
        self.assertIn("npm ci", install)
        self.assertIn("npm install", install)
        self.assertIn("npm ci", update)
        self.assertIn("npm run build", install)
        self.assertIn("npm install", update)
        self.assertIn("npm run build", update)
        self.assertIn("--hostname 127.0.0.1 --port 3000", combined)
        self.assertIn("Environment=HOSTNAME=127.0.0.1", combined)
        self.assertIn("enable --now", combined)
        self.assertIn("command -v npm", combined)
        self.assertIn("Node.js 22 LTS", combined)
        self.assertIn("Next.js Dashboard", combined)
        self.assertIn("paopao-frontend", check)
        self.assertIn("127.0.0.1:3000", check)
        self.assertIn("paoxx-frontend", check)
        self.assertIn("nextjs-dashboard", check)
        self.assertIn("/etc/nginx/conf.d/00-paoxx-frontend.conf", combined)
        self.assertIn("NGINX_ACTIVE_SITE_PATH", combined)
        self.assertIn("disabled-by-paopao", combined)
        self.assertIn("nginx -T", combined)

    def test_nginx_routes_keep_backend_paths_before_frontend_root(self) -> None:
        install = (ROOT / "scripts/install_server.sh").read_text(encoding="utf-8")
        update = (ROOT / "scripts/update_server.sh").read_text(encoding="utf-8")
        docs = (ROOT / "docs/INSTALL_CN.md").read_text(encoding="utf-8")
        for source in (install, update, docs):
            admin_idx = source.index("location ^~ /admin")
            api_idx = source.index("location ^~ /api/")
            public_idx = source.index("location ^~ /public-api/")
            next_idx = source.index("location ^~ /_next/")
            root_idx = source.index("location / {")
            self.assertLess(admin_idx, root_idx)
            self.assertLess(api_idx, root_idx)
            self.assertLess(public_idx, root_idx)
            self.assertLess(next_idx, root_idx)
            self.assertIn("proxy_pass http://127.0.0.1:8080;", source[admin_idx:root_idx])
            self.assertIn("proxy_pass http://127.0.0.1:3000;", source[next_idx:root_idx])
            self.assertIn("proxy_pass http://127.0.0.1:3000;", source[root_idx:])

    def test_https_check_requires_nextjs_marker_not_legacy_public_copy(self) -> None:
        check = (ROOT / "scripts/check_https_deploy.sh").read_text(encoding="utf-8")
        self.assertIn('"本机 Next.js 前台" "http://127.0.0.1:3000/" "paoxx-frontend" "nextjs-dashboard"', check)
        self.assertIn('"HTTPS 公开前台" "${BASE_URL}${ROOT_PATH}" "paoxx-frontend" "nextjs-dashboard"', check)
        self.assertNotIn('"HTTPS 公开前台" "${BASE_URL}${ROOT_PATH}" "Paoxx 信号雷达"', check)
        self.assertIn("check_nginx_active_routes", check)
        self.assertIn("nginx_active_config_dump", check)
        self.assertIn("nginx -T", check)
        self.assertIn("location ^~ /_next/", check)
        self.assertIn("proxy_pass http://127.0.0.1:3000;", check)
        self.assertIn("proxy_pass http://127.0.0.1:8080;", check)
        self.assertIn("日志阻断片段", check)

    def test_docs_describe_nextjs_frontend_split(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        install = (ROOT / "docs/INSTALL_CN.md").read_text(encoding="utf-8")
        combined = readme + "\n" + install

        self.assertIn("v1.74.0", combined)
        self.assertIn("v1.74.1", combined)
        self.assertIn("v1.74.2", combined)
        self.assertIn("v1.74.3", combined)
        self.assertIn("frontend/", combined)
        self.assertIn("Next.js", combined)
        self.assertIn("paopao-frontend", combined)
        self.assertIn("127.0.0.1:3000", combined)
        self.assertIn("/etc/nginx/conf.d/00-paoxx-frontend.conf", combined)
        self.assertIn("/_next/", combined)
        self.assertIn("paoxx-frontend=nextjs-dashboard", combined)
        self.assertIn("/admin", combined)
        self.assertIn("/api/*", combined)
        self.assertIn("/public-api/*", combined)
        self.assertIn("不改 Telegram 主推送流程", combined)
        self.assertIn("不实现自动交易", combined)


if __name__ == "__main__":
    unittest.main()
