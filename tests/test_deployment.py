from __future__ import annotations


# Source group: test_gitignore.py

import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def is_ignored(path: str) -> bool:
    result = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", path],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


class GitIgnoreHardeningTests(unittest.TestCase):
    def test_runtime_env_backups_are_ignored(self) -> None:
        self.assertTrue(is_ignored(".env.oi.bak.20260710_000000"))
        self.assertTrue(is_ignored("runtime-config.bak"))
        self.assertTrue(is_ignored("data/tg_push_history.json.lock"))

    def test_example_env_files_remain_trackable(self) -> None:
        self.assertFalse(is_ignored(".env.example"))
        self.assertFalse(is_ignored(".env.oi.example"))


if __name__ == "__main__":
    unittest.main()


# Source group: test_env_sync.py

import importlib.util
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


def load_sync_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sync_env.py"
    spec = importlib.util.spec_from_file_location("sync_env", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EnvSyncTests(unittest.TestCase):
    def test_sync_updates_defaults_and_preserves_secrets(self) -> None:
        module = load_sync_module()
        with TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env.oi"
            example = Path(tmp) / ".env.oi.example"
            env.write_text(
                "TG_BOT_TOKEN=secret\nTG_CHAT_ID=-1001234567890\nRADAR_SUMMARY_MIN_INTERVAL_SEC=1800\nWEB_PORT=80\nSTRUCTURE_RADAR_ENABLE=true\nCUSTOM_KEEP=1\n",
                encoding="utf-8",
            )
            example.write_text(
                "TG_BOT_TOKEN=\nTG_CHAT_ID=\nRADAR_SUMMARY_MIN_INTERVAL_SEC=21600\nWEB_PORT=8080\nAI_REQUEST_TIMEOUT_SEC=90\n",
                encoding="utf-8",
            )
            result = module.sync_env(env, example)
            text = env.read_text(encoding="utf-8")
        self.assertIn("TG_BOT_TOKEN=secret", text)
        self.assertIn("TG_CHAT_ID=-1001234567890", text)
        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC=21600", text)
        self.assertIn("WEB_PORT=8080", text)
        self.assertIn("AI_REQUEST_TIMEOUT_SEC=90", text)
        self.assertIn("CUSTOM_KEEP=1", text)
        self.assertNotIn("STRUCTURE_RADAR_ENABLE", text)
        self.assertIn("RADAR_SUMMARY_MIN_INTERVAL_SEC", result["updated"])
        self.assertIn("STRUCTURE_RADAR_ENABLE", result["removed"])

if __name__ == "__main__":
    unittest.main()


# Source group: test_update_script.py

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
        self.assertIn("/api/summary", script)
        self.assertIn("401", script)
        self.assertIn("certbot renew --dry-run --no-random-sleep-on-renew", script)
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
        self.assertIn('sudo nginx -T 2>&1 | grep -nE "configuration file|server_name paoxx.com|listen 80|listen 443"', script)
        self.assertIn("请只保留 /etc/nginx/conf.d/00-paoxx-frontend.conf 作为 active 入口", script)
        self.assertIn("/etc/nginx/conf.d/00-paoxx-frontend.conf", script)
        self.assertIn("grep -aF", script)
        self.assertIn("path_exists_maybe_sudo()", script)
        self.assertIn("sudo test -f", script)
        self.assertIn("certbot certificates --cert-name", script)
        self.assertIn("CERTBOT_DRY_RUN_OK=1", script)
        self.assertIn("普通用户无法直接读取部分证书路径，但 certbot dry-run 已通过", script)
        self.assertIn("诊断输出保留在 ${stdout_file} 和 ${stderr_file}", script)
        self.assertIn("[certbot stdout 尾部]", script)
        self.assertIn('tail -n 20 "${stderr_file}"', script)
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
        self.assertIn("check_public_intelligence_budget()", script)
        self.assertIn("公开情报接口响应超过 256KiB", script)
        self.assertIn('PUBLIC_SLO_MS="${PUBLIC_SLO_MS:-800}"', script)
        self.assertIn("3 次冷请求中位数超标", script)
        self.assertIn("window_base=$((86000 + $(date +%s) % 500))", script)
        self.assertIn("check_public_signal_context_actions()", script)
        self.assertIn("Web -> AI 分析/提醒深链闭环可用", script)
        self.assertIn("公开信号详情性能超标", script)
        self.assertIn("公开信号详情 3 次请求中位数达标", script)
        self.assertIn("公开前台安全响应头生效", script)

        self.assertIn("no such table", script)
        self.assertIn("匹配规则:", script)
        self.assertIn("判定原因:", script)
        self.assertIn("日志:", script)
        self.assertIn("日志发现 ${service} 阻断错误", script)
        self.assertNotIn("local pattern=", script)
        self.assertNotIn("| 500 |", script)

    def test_frontend_and_nginx_publish_security_and_compression_headers(self) -> None:
        next_config = Path("frontend/next.config.mjs").read_text(encoding="utf-8")
        install = Path("scripts/install_server.sh").read_text(encoding="utf-8")
        update = Path("scripts/update_server.sh").read_text(encoding="utf-8")

        for source in (next_config, install, update):
            self.assertIn("X-Content-Type-Options", source)
            self.assertIn("X-Frame-Options", source)
            self.assertIn("Strict-Transport-Security", source)
        for source in (install, update):
            self.assertIn("gzip on;", source)
            self.assertIn("gzip_types application/json", source)
            self.assertIn("proxy_hide_header X-Content-Type-Options;", source)
            self.assertIn("proxy_hide_header X-Frame-Options;", source)
            self.assertIn("proxy_hide_header Strict-Transport-Security;", source)

        https_check = Path("scripts/check_https_deploy.sh").read_text(encoding="utf-8")
        self.assertIn("header_count()", https_check)
        self.assertIn("header_count \"${headers_file}\" 'X-Content-Type-Options'", https_check)
        self.assertIn("header_count \"${frontend_headers_file}\" 'Strict-Transport-Security'", https_check)

    def test_ai_username_is_required_for_web_deep_links(self) -> None:
        from paopao_radar.web import build_deployment_acceptance, build_health_items, build_stability_checks

        config = {
            "telegram": {"bot_token_configured": True, "chat_id_configured": True},
            "ai_assistant": {"enable": True, "bot_token_configured": True, "bot_username": ""},
            "web": {
                "host": "0.0.0.0",
                "port": 8080,
                "auth_mode": "password",
                "admin_password_hash_configured": True,
                "session_secret_configured": True,
            },
        }
        services = {key: {"active_ok": True, "active": "active"} for key in ("main", "web", "ai")}
        health = build_health_items(services, {}, config)
        ai_health = next(item for item in health if item["label"] == "AI 助手 Bot")
        snapshot = {
            "config": config,
            "services": services,
            "health": health,
            "git": {"version": "v1.87.3", "commit": "abc123"},
            "stability": {"status": "ready"},
            "release_readiness": {"status": "candidate"},
            "logs": {},
            "audit": {},
        }
        stability = build_stability_checks(snapshot)
        deployment = build_deployment_acceptance(snapshot)
        config_check = next(item for item in stability["checks"] if item["key"] == "config")
        ai_deploy = next(item for item in deployment["checks"] if item["key"] == "ai_bot")

        self.assertEqual(ai_health["status"], "warn")
        self.assertEqual(ai_health["value"], "缺 AI_BOT_USERNAME")
        self.assertEqual(config_check["status"], "warn")
        self.assertIn("AI_BOT_USERNAME", config_check["detail"])
        self.assertEqual(ai_deploy["status"], "warn")
        self.assertIn("Web 分析和提醒深链不可用", ai_deploy["detail"])

    def test_https_deploy_docs_include_public_urls_and_commands(self) -> None:
        readme = Path("README.md").read_text(encoding="utf-8")
        install = Path("docs/INSTALL_CN.md").read_text(encoding="utf-8")
        combined = readme + "\n" + install

        self.assertIn("https://paoxx.com/", combined)
        self.assertIn("https://paoxx.com/admin", combined)
        self.assertIn("https://paoxx.com/public-api/signals", combined)
        self.assertIn("https://paoxx.com/admin", combined)
        self.assertIn("scripts/check_https_deploy.sh", combined)
        self.assertIn("--with-stable-check", combined)
        self.assertIn("--with-certbot-dry-run", combined)
        self.assertIn("8080", combined)

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


# Source group: test_launch_report.py

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.cli import build_launch_report, format_launch_report, format_observe_report
from paopao_radar.storage import JsonStore


class LaunchReportTests(unittest.TestCase):
    def test_launch_report_summarizes_scores_and_buckets(self) -> None:
        settings = Settings(base_dir=Path("."), data_dir=Path("data"))
        report = build_launch_report([
            {
                "top_score": 20,
                "scanned": 2,
                "alert_count": 0,
                "buckets": {"idle": 2, "watching": 0},
                "top_symbols": ["BTCUSDT", "ETHUSDT"],
            },
            {
                "top_score": 60,
                "scanned": 2,
                "alert_count": 1,
                "buckets": {"idle": 1, "primed": 1},
                "top_symbols": ["ETHUSDT", "BTCUSDT"],
            },
        ], settings)

        self.assertEqual(report["records"], 2)
        self.assertEqual(report["total_scanned"], 4)
        self.assertEqual(report["total_alerts"], 1)
        self.assertEqual(report["max_top_score"], 60)
        self.assertEqual(report["avg_top_score"], 40)
        self.assertEqual(report["buckets"]["primed"], 1)
        self.assertEqual(report["top_symbols"][0], ("BTCUSDT", 2))

    def test_launch_report_low_score_suggestion_after_enough_samples(self) -> None:
        settings = Settings(base_dir=Path("."), data_dir=Path("data"), launch_watch_score=45)
        records = [
            {"top_score": 0, "scanned": 2, "alert_count": 0, "buckets": {"idle": 2}, "top_symbols": ["BTCUSDT"]}
            for _ in range(5)
        ]

        report = build_launch_report(records, settings)

        self.assertIn("无需下调", report["suggestion"])

    def test_launch_report_ignores_excluded_symbols_in_frequency(self) -> None:
        settings = Settings(
            base_dir=Path("."),
            data_dir=Path("data"),
            excluded_base_assets=("XAU", "XAG"),
        )
        report = build_launch_report([
            {
                "top_score": 10,
                "scanned": 3,
                "alert_count": 0,
                "buckets": {"idle": 3},
                "top_symbols": ["XAUUSDT", "BTCUSDT", "XAGUSDT"],
            }
        ], settings)

        self.assertEqual(report["top_symbols"], [("BTCUSDT", 1)])

    def test_format_launch_report_handles_empty_history(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                base_dir=Path(tmp),
                data_dir=Path(tmp),
                launch_watch_history_path=Path(tmp) / "launch_watch_history.json",
            )
            store = JsonStore(Path(tmp))

            text = format_launch_report(settings, store, record_limit=10, top_n=5)

            self.assertIn("暂无启动观察历史", text)

    def test_format_observe_report_includes_session_status(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                base_dir=Path(tmp),
                data_dir=Path(tmp),
                launch_watch_history_path=Path(tmp) / "launch_watch_history.json",
            )
            store = JsonStore(Path(tmp))

            text = format_observe_report(
                settings,
                store,
                record_limit=10,
                top_n=5,
                started_at="2026-05-25 19:00:00",
                cycles=2,
                failures=1,
                status="running",
                last_error="Timeout",
            )

            self.assertIn("状态: running", text)
            self.assertIn("已跑轮数: 2", text)
            self.assertIn("错误次数: 1", text)
            self.assertIn("最近错误: Timeout", text)
            self.assertIn("dry-run", text)


if __name__ == "__main__":
    unittest.main()


# Source group: test_main_commands.py

import argparse
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paopao_radar.cli as main
from paopao_radar.config import Settings
from paopao_radar.radar import RadarEngine
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import TelegramGateway


class MainCommandTests(unittest.TestCase):
    def make_runtime(self, tmp: str):
        settings = Settings(
            base_dir=Path(tmp),
            data_dir=Path(tmp),
            tg_push_history_path=Path(tmp) / "push_history.json",
            runtime_status_path=Path(tmp) / "runtime_status.json",
            radar_state_path=Path(tmp) / "radar_state.json",
            funding_snapshot_path=Path(tmp) / "funding_snapshot.json",
            launch_state_path=Path(tmp) / "launch_state.json",
            launch_watchlist_path=Path(tmp) / "launch_watchlist.json",
            launch_watch_history_path=Path(tmp) / "launch_watch_history.json",
            divergence_state_path=Path(tmp) / "oi_divergence_state.json",
            divergence_cooldown_path=Path(tmp) / "oi_divergence_cooldown.json",
        )
        store = JsonStore(Path(tmp))
        gateway = TelegramGateway(settings, store)
        return settings, store, None, gateway

    def test_telegram_test_defaults_to_dry_run(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test"])

        self.assertEqual(code, 0)
        self.assertIn("telegram_test: dry_run", output.getvalue())

    def test_telegram_test_blocks_real_send_without_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test", "--send"])

        self.assertEqual(code, 2)
        self.assertIn("telegram_test: blocked", output.getvalue())

    def test_readiness_reports_wait_when_history_missing(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["readiness"])

        self.assertEqual(code, 1)
        self.assertIn("真实推送准备度", output.getvalue())
        self.assertIn("WAIT", output.getvalue())

    def test_readiness_rejects_invalid_telegram_token_even_with_history(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            settings = Settings(
                base_dir=settings.base_dir,
                data_dir=settings.data_dir,
                tg_bot_token="",
                tg_chat_id="-1001234567890",
                tg_push_history_path=settings.tg_push_history_path,
                runtime_status_path=settings.runtime_status_path,
                launch_watch_history_path=settings.launch_watch_history_path,
            )
            for idx in range(5):
                store.append_record(settings.launch_watch_history_path, {"top_score": 1, "scanned": 1, "alert_count": 0, "top_symbols": [f"T{idx}"]})

            with redirect_stdout(StringIO()) as output:
                code = main.print_readiness(settings, store)

        self.assertEqual(code, 1)
        self.assertIn("TG_BOT_TOKEN 缺失或格式无效", output.getvalue())

    def test_telegram_test_blocks_invalid_config_before_real_send(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["telegram-test", "--send", "--confirm-real-send"])

        self.assertEqual(code, 2)
        self.assertIn("invalid Telegram config", output.getvalue())

    def test_live_requires_explicit_real_send_confirmation(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["live"])

        self.assertEqual(code, 2)
        self.assertIn("真实推送已阻止", output.getvalue())

    def test_runtime_status_reports_empty_before_first_write(self) -> None:
        with TemporaryDirectory() as tmp:
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                with redirect_stdout(StringIO()) as output:
                    code = main.main(["runtime-status"])

        self.assertEqual(code, 0)
        self.assertIn('"status": "empty"', output.getvalue())

    def test_stable_check_prints_summary_and_returns_ready_code(self) -> None:
        snapshot = {
            "generated_at": "2026-07-04 08:00:00",
            "git": {"version": "v1.36.0", "branch": "main", "commit": "abc123"},
            "stability": {
                "status": "ready",
                "label": "达到稳定版标准",
                "summary": "核心服务正常",
                "checks": [{"label": "后台服务", "status": "ok", "detail": "全部运行中"}],
            },
            "release_readiness": {
                "status": "complete_candidate",
                "label": "完整稳定版候选",
                "summary": "当前快照达到长期运行候选标准",
                "score": 100,
                "ok_count": 6,
                "warn_count": 0,
                "fail_count": 0,
                "next_version_goal": "可以进入下一阶段",
                "checks": [{"label": "当前稳定版验收", "status": "ok", "detail": "当前 stable-check 已通过"}],
            },
            "release_trend": {
                "status": "improved",
                "label": "趋势变好",
                "summary": "长期运行就绪度比上一次验收更好。",
                "current_score": 100,
                "previous_score": 84,
                "score_delta": 16,
                "action": "继续观察。",
            },
            "deployment_acceptance": {
                "status": "ready",
                "label": "部署验收通过",
                "summary": "服务器部署达到当前收口标准",
                "ok_count": 8,
                "warn_count": 0,
                "fail_count": 0,
                "next_action": "可以进入下一阶段收口。",
                "checks": [{"label": "Web 入口", "status": "ok", "detail": "监听 0.0.0.0:8080"}],
            },
            "recommendations": ["当前快照没有发现明显异常。"],
        }

        with patch("paopao_radar.web.ops_snapshot_payload", return_value=snapshot):
            with redirect_stdout(StringIO()) as output:
                code = main.main(["stable-check", "--no-save"])

        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("泡泡雷达稳定版自检", text)
        self.assertIn("达到稳定版标准", text)
        self.assertIn("长期运行就绪度", text)
        self.assertIn("完整稳定版候选", text)
        self.assertIn("评分: 100/100", text)
        self.assertIn("下一目标: 可以进入下一阶段", text)
        self.assertIn("趋势变化", text)
        self.assertIn("趋势变好", text)
        self.assertIn("变化 16", text)
        self.assertIn("服务器部署验收", text)
        self.assertIn("部署验收通过", text)
        self.assertIn("Web 入口: 通过", text)
        self.assertIn("后台服务: 通过", text)
        self.assertIn("本次未保存", text)

    def test_stable_check_json_outputs_snapshot_and_blocked_code(self) -> None:
        snapshot = {
            "ok": True,
            "stability": {
                "status": "blocked",
                "label": "未达稳定版标准",
                "summary": "1 个阻断项",
                "checks": [],
            },
        }

        with patch("paopao_radar.web.ops_snapshot_payload", return_value=snapshot):
            with redirect_stdout(StringIO()) as output:
                code = main.main(["stable-check", "--json", "--no-save"])

        self.assertEqual(code, 2)
        self.assertEqual(__import__("json").loads(output.getvalue())["stability"]["status"], "blocked")

    def test_write_runtime_status_persists_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, _gateway = self.make_runtime(tmp)
            payload = main.write_runtime_status(settings, store, "test", "running", task="unit")
            saved = store.load(settings.runtime_status_path, {})

        self.assertEqual(payload["mode"], "test")
        self.assertEqual(saved["status"], "running")
        self.assertEqual(saved["task"], "unit")

    def test_make_runtime_for_args_applies_scan_limit_overrides(self) -> None:
        with TemporaryDirectory() as tmp:
            args = argparse.Namespace(radar_scan_limit=4, launch_scan_limit=3, flow_scan_limit=2, funding_scan_limit=5)
            with patch.object(main, "make_runtime", side_effect=lambda: self.make_runtime(tmp)):
                settings, _store, _engine, _gateway = main.make_runtime_for_args(args)

        self.assertEqual(settings.radar_scan_limit, 4)
        self.assertEqual(settings.launch_scan_limit, 3)
        self.assertEqual(settings.flow_scan_limit, 2)
        self.assertEqual(settings.funding_alert_scan_limit, 5)

    def test_next_interval_epoch_aligns_hourly_jobs_to_top_of_hour(self) -> None:
        base = main.datetime(2026, 5, 26, 17, 46, 30).timestamp()
        expected = main.datetime(2026, 5, 26, 18, 0, 0).timestamp()

        self.assertEqual(main.next_interval_epoch(base, 3600), expected)

    def test_next_closed_window_epoch_adds_post_close_delay(self) -> None:
        from datetime import timedelta, timezone
        from paopao_radar.time_windows import next_closed_window_epoch

        tz = timezone(timedelta(hours=8))
        base = main.datetime(2026, 5, 26, 17, 46, 30, tzinfo=tz).timestamp()
        expected = main.datetime(2026, 5, 26, 18, 5, 0, tzinfo=tz).timestamp()

        self.assertEqual(
            next_closed_window_epoch(base, interval_sec=3600, delay_sec=300),
            expected,
        )

    def test_announcements_test_prints_diagnostics(self) -> None:
        with TemporaryDirectory() as tmp:
            settings, store, _engine, gateway = self.make_runtime(tmp)
            engine = RadarEngine(settings, store)
            with patch.object(main, "make_runtime", return_value=(settings, store, engine, gateway)):
                with patch.object(main.BinanceDataSource, "announcements", return_value=[]):
                    with patch.object(main.BinanceDataSource, "usdt_perp_symbols", return_value=[]):
                        with redirect_stdout(StringIO()) as output:
                            code = main.main(["announcements-test"])

        self.assertEqual(code, 0)
        self.assertIn("announcements_test: ok", output.getvalue())
        self.assertIn("articles_scanned", output.getvalue())


if __name__ == "__main__":
    unittest.main()
