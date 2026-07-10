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
        self.assertEqual(package.get("version"), "1.76.0")
        self.assertIn("build", package.get("scripts", {}))
        self.assertIn("start", package.get("scripts", {}))
        self.assertIn("typecheck", package.get("scripts", {}))
        self.assertIn("next", package.get("dependencies", {}))
        self.assertIn("react", package.get("dependencies", {}))
        self.assertIn("tailwindcss", package.get("devDependencies", {}))
        self.assertNotIn("recharts", package.get("dependencies", {}))

        for relative in (
            "app/page.tsx",
            "app/radar/page.tsx",
            "app/decision/page.tsx",
            "app/outcomes/page.tsx",
            "app/backtest/page.tsx",
            "app/lifecycle/page.tsx",
            "app/coin/[symbol]/page.tsx",
            "app/api-docs/page.tsx",
            "app/not-found.tsx",
            "app/error.tsx",
            "lib/api.ts",
            "styles/globals.css",
        ):
            self.assertTrue((FRONTEND / relative).exists(), relative)

    def test_public_api_client_hydrates_server_and_browser(self) -> None:
        api_source = (FRONTEND / "lib/api.ts").read_text(encoding="utf-8")
        type_source = (FRONTEND / "lib/types.ts").read_text(encoding="utf-8")
        combined = api_source + "\n" + type_source

        self.assertIn("PAOXX_PUBLIC_API_INTERNAL_BASE", api_source)
        self.assertIn("http://127.0.0.1:8080", api_source)
        self.assertIn("PAOXX_PUBLIC_API_TIMEOUT_MS", api_source)
        self.assertIn("15000", api_source)
        self.assertIn("typeof window === \"undefined\"", api_source)
        self.assertIn("publicFetchResult", api_source)
        self.assertIn("ApiResult", api_source)
        self.assertIn("payload.data", api_source)
        self.assertIn("items", combined)
        self.assertIn("summary", combined)
        self.assertIn("公开接口返回格式异常", api_source)
        self.assertIn("公开接口响应超时", api_source)
        self.assertIn("loadHomeDashboardData", api_source)

    def test_public_frontend_uses_only_public_api(self) -> None:
        api_source = (FRONTEND / "lib/api.ts").read_text(encoding="utf-8")

        for path in (
            "/public-api/signals",
            "/public-api/signals/stats",
            "/public-api/signal-timeline",
            "/public-api/coin-search",
            "/public-api/coin-detail",
            "/public-api/decisions",
            "/public-api/decisions/stats",
            "/public-api/decision",
            "/public-api/outcomes",
            "/public-api/outcomes/stats",
            "/public-api/symbol-outcomes",
            "/public-api/backtest/decision",
            "/public-api/backtest/decision/matrix",
            "/public-api/backtest/decision/detail",
            "/public-api/lifecycle/summary",
            "/public-api/lifecycle/list",
            "/public-api/lifecycle/detail",
            "/public-api/lifecycle/events",
            "/public-api/lifecycle/metrics",
        ):
            self.assertIn(path, api_source)
        self.assertNotIn("/public-api/signals/latest", api_source)

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
            "BOT_TOKEN",
            "Authorization",
            "Cookie",
        )
        for text in forbidden:
            self.assertNotIn(text, api_source)

    def test_homepage_has_server_prefetch_and_no_static_zero_dashboard(self) -> None:
        page = (FRONTEND / "app/page.tsx").read_text(encoding="utf-8")
        home = (FRONTEND / "components/HomeDashboard.tsx").read_text(encoding="utf-8")

        self.assertIn("loadHomeDashboardData", page)
        self.assertIn("initialData", home)
        self.assertIn("hasAnyData", home)
        self.assertIn("公开数据暂时不可用", home)
        self.assertNotIn("部分数据暂时不可用", home)
        self.assertIn("今日信号数", home)
        self.assertIn("最新信号卡片", home)
        self.assertIn("决策分布", home)
        self.assertIn("结果追踪", home)
        self.assertIn("决策回测摘要", home)
        self.assertNotIn("signalStats?.total || 0", home)
        self.assertNotIn("outcomeStats?.success_count || 0", home)

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
            "正在加载数据",
            "暂无数据",
            "数据暂时不可用",
        ):
            self.assertIn(text, source)

        for text in (
            "Public crypto signal feed",
            "Read-only, redacted",
            "Admin Console",
            "Latest Signal Cards",
            "Active Coins",
            "This page could not be found",
        ):
            self.assertNotIn(text, source)

    def test_chinese_not_found_and_error_pages(self) -> None:
        not_found = (FRONTEND / "app/not-found.tsx").read_text(encoding="utf-8")
        error_page = (FRONTEND / "app/error.tsx").read_text(encoding="utf-8")
        self.assertIn("页面不存在", not_found)
        self.assertIn("返回总览", not_found)
        self.assertIn("页面加载失败", error_page)
        self.assertIn("数据暂时不可用，请稍后重试", error_page)

    def test_deploy_scripts_include_frontend_build_service_and_api_base(self) -> None:
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
        self.assertIn("Environment=PAOXX_PUBLIC_API_INTERNAL_BASE=http://127.0.0.1:8080", combined)
        self.assertIn("Environment=PAOXX_PUBLIC_API_TIMEOUT_MS=15000", combined)
        self.assertIn("enable --now", combined)
        self.assertIn("command -v npm", combined)
        self.assertIn("Node.js 22 LTS", combined)
        self.assertIn("Next.js Dashboard", combined)
        self.assertIn("127.0.0.1:3000", check)
        self.assertIn("paoxx-frontend", check)
        self.assertIn("nextjs-dashboard", check)
        self.assertIn("/etc/nginx/conf.d/00-paoxx-frontend.conf", combined)
        self.assertIn("cleanup_duplicate_paoxx_nginx_servers", combined)
        self.assertIn("conflicting server name", combined)
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
            root_idx = source.index("proxy_pass http://127.0.0.1:3000;", next_idx)
            self.assertLess(admin_idx, root_idx)
            self.assertLess(api_idx, root_idx)
            self.assertLess(public_idx, root_idx)
            self.assertLess(next_idx, root_idx)
            self.assertIn("proxy_pass http://127.0.0.1:8080;", source[admin_idx:root_idx])
            self.assertIn("proxy_pass http://127.0.0.1:3000;", source[next_idx:])

    def test_https_check_requires_nextjs_marker_and_duplicate_nginx_guard(self) -> None:
        check = (ROOT / "scripts/check_https_deploy.sh").read_text(encoding="utf-8")
        self.assertIn("paopao-frontend", check)
        self.assertIn("127.0.0.1:3000", check)
        self.assertIn("paoxx-frontend", check)
        self.assertIn("nextjs-dashboard", check)
        self.assertIn("check_nginx_active_routes", check)
        self.assertIn("check_nginx_duplicate_server_names", check)
        self.assertIn('conflicting server name "paoxx.com"', check)
        self.assertIn('sudo grep -RIn "server_name .*paoxx.com"', check)
        self.assertIn("nginx -T", check)
        self.assertIn("/etc/nginx/conf.d/00-paoxx-frontend.conf", check)
        self.assertIn("location ^~ /_next/", check)
        self.assertIn("proxy_pass http://127.0.0.1:3000;", check)
        self.assertIn("proxy_pass http://127.0.0.1:8080;", check)

    def test_docs_describe_v175_frontend_hydration(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        install = (ROOT / "docs/INSTALL_CN.md").read_text(encoding="utf-8")
        combined = readme + "\n" + install

        self.assertIn("v1.75.0", combined)
        self.assertIn("真实数据水合", combined)
        self.assertIn("PAOXX_PUBLIC_API_INTERNAL_BASE", combined)
        self.assertIn("http://127.0.0.1:8080", combined)
        self.assertIn("/public-api/*", combined)
        self.assertIn("v1.76.0", combined)
        self.assertIn("Binance-Centric Signal Lifecycle Tracker", combined)
        self.assertIn("/public-api/lifecycle/summary", combined)
        self.assertIn("lifecycle-backfill", combined)
        self.assertIn("lifecycle-scan", combined)
        self.assertIn("不访问 `/api/*`", combined)
        self.assertIn("不改 Telegram 主推送流程", combined)
        self.assertIn("不引入自动交易", combined)


if __name__ == "__main__":
    unittest.main()
