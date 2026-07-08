from __future__ import annotations

import unittest
import os
from pathlib import Path


class UpdateServerScriptTests(unittest.TestCase):
    def test_update_script_runs_stable_check_after_restart_paths(self) -> None:
        script = Path("scripts/update_server.sh").read_text(encoding="utf-8")

        self.assertIn("run_post_update_stable_check()", script)
        self.assertIn('\"$PYTHON_BIN\" main.py stable-check', script)
        self.assertGreaterEqual(script.count("run_post_update_stable_check"), 3)
        self.assertIn("稳定版自检通过，长期运行就绪度", script)
        self.assertIn("稳定版自检未达标，长期运行就绪度需要处理", script)
        self.assertIn("趋势变化", script)
        self.assertIn("发生回退", script)

    def test_https_deploy_check_script_contract(self) -> None:
        path = Path("scripts/check_https_deploy.sh")
        self.assertTrue(path.exists())
        self.assertTrue(os.access(path, os.X_OK))

        script = path.read_text(encoding="utf-8")
        self.assertIn("paoxx-frontend", script)
        self.assertIn("nextjs-dashboard", script)
        self.assertIn("泡泡雷达控制台", script)
        self.assertIn("/public-api/signals", script)
        self.assertIn("/api/dashboard", script)
        self.assertIn("401", script)
        self.assertIn("certbot renew --dry-run", script)
        self.assertIn(".venv/bin/python main.py stable-check", script)
        self.assertIn("curl -sS -L", script)
        self.assertIn("--connect-timeout", script)
        self.assertNotIn("curl -I", script)
        self.assertIn("sudo ss -ltnp", script)
        self.assertIn("nginx_test_output()", script)
        self.assertIn("check_nginx_duplicate_server_names()", script)
        self.assertIn("conflicting server name \"paoxx.com\"", script)
        self.assertIn("Nginx 存在重复 paoxx.com server block", script)
        self.assertIn('sudo grep -RIn "server_name .*paoxx.com"', script)
        self.assertIn("/etc/nginx/conf.d/00-paoxx-frontend.conf", script)
        self.assertIn("grep -aF", script)
        self.assertIn("path_exists_maybe_sudo()", script)
        self.assertIn("sudo test -f", script)
        self.assertIn("certbot certificates --cert-name", script)
        self.assertIn("CERTBOT_DRY_RUN_OK=1", script)
        self.assertIn("普通用户无法直接读取部分证书路径，但 certbot dry-run 已通过", script)
        self.assertIn("HTTP_CODE=", script)
        self.assertIn("下载字节数", script)
        self.assertIn("页面前 8 行摘要", script)
        self.assertIn('"泡泡雷达控制台" "brand-title" "/admin"', script)
        self.assertIn("sqlite database is locked", script)
        self.assertIn("BrokenPipeError", script)
        self.assertIn("ConnectionResetError", script)
        self.assertIn("ReadTimeout", script)
        self.assertIn("is_benign_deploy_log_line()", script)
        self.assertIn("deploy_log_block_rule()", script)
        self.assertIn("OK observe_history", script)
        self.assertIn("启动观察历史", script)
        self.assertIn("可自动重试网络超时", script)
        self.assertIn("500 Internal Server Error", script)
        self.assertIn("Traceback", script)
        self.assertIn("no such table", script)
        self.assertIn("匹配规则:", script)
        self.assertIn("判定原因:", script)
        self.assertIn("日志:", script)
        self.assertIn("日志发现 ${service} 阻断错误", script)
        self.assertNotIn("local pattern=", script)
        self.assertNotIn("| 500 |", script)

    def test_https_deploy_docs_include_public_urls_and_commands(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        install = Path("docs/INSTALL_CN.md").read_text(encoding="utf-8")
        combined = readme + "\n" + install

        self.assertIn("https://paoxx.com/", combined)
        self.assertIn("https://paoxx.com/admin", combined)
        self.assertIn("https://paoxx.com/public-api/*", combined)
        self.assertIn("https://paoxx.com/api/*", combined)
        self.assertIn("scripts/check_https_deploy.sh", combined)
        self.assertIn("--with-stable-check", combined)
        self.assertIn("--with-certbot-dry-run", combined)
        self.assertIn("curl -I https://paoxx.com", combined)
        self.assertIn("501", combined)
        self.assertIn("8080", combined)
        self.assertIn("/etc/letsencrypt/live", combined)
        self.assertIn("普通用户无法读取", combined)
        self.assertIn("sudo test -f", combined)
        self.assertIn("certbot certificates", combined)

    def test_update_script_points_to_https_public_and_admin_entries(self) -> None:
        script = Path("scripts/update_server.sh").read_text(encoding="utf-8")

        self.assertIn("https://paoxx.com/", script)
        self.assertIn("https://paoxx.com/admin", script)
        self.assertIn("本机后端入口 8080 仅供 Nginx 反代使用", script)
        self.assertNotIn("http://服务器IP:8080/", script)

    def test_server_menu_uses_https_entries_and_redacts_token_by_default(self) -> None:
        menu = Path("scripts/paopao_menu.sh").read_text(encoding="utf-8")
        install = Path("scripts/install_server.sh").read_text(encoding="utf-8")
        combined = menu + "\n" + install

        self.assertIn("https://paoxx.com/", combined)
        self.assertIn("https://paoxx.com/admin", combined)
        self.assertIn("后台登录", combined)
        self.assertIn("设置后台账号密码", combined)
        self.assertIn("admin-password", combined)
        self.assertNotIn("查看后台访问令牌", combined)
        self.assertNotIn("访问令牌: 已配置，默认不在菜单首页明文显示", combined)
        self.assertIn("8080 仅作为 Nginx 反代后端入口，不作为公网入口", combined)
        self.assertNotIn("Web 地址: $(web_public_url)", menu)
        self.assertNotIn("访问令牌: ${token:-", menu)
        self.assertNotIn("http://服务器IP:8080/", combined)


if __name__ == "__main__":
    unittest.main()
