from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import paopao_radar.cli as cli
from paopao_radar import web


class WebConsoleTests(unittest.TestCase):
    def test_config_payload_masks_secret_values(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text(
                "TG_BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz\n"
                "TG_CHAT_ID=-1001234567890\n",
                encoding="utf-8",
            )

            payload = web.config_payload(env_path)

        telegram_fields = {
            item["key"]: item
            for item in payload["sections"]["Telegram"]
        }
        self.assertEqual(telegram_fields["TG_BOT_TOKEN"]["value"], "")
        self.assertIn("...", telegram_fields["TG_BOT_TOKEN"]["display_value"])
        self.assertTrue(telegram_fields["TG_BOT_TOKEN"]["configured"])
        self.assertIn("...", telegram_fields["TG_BOT_TOKEN"]["masked"])
        self.assertEqual(telegram_fields["TG_CHAT_ID"]["value"], "-1001234567890")
        self.assertEqual(telegram_fields["TG_CHAT_ID"]["display_value"], "-1001234567890")

    def test_write_env_updates_preserves_existing_lines_and_creates_backup(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text(
                "# header\n"
                "TG_CHAT_ID=-1001111111111\n"
                "UNCHANGED=value\n",
                encoding="utf-8",
            )

            result = web.write_env_updates(
                {
                    "TG_CHAT_ID": "-1002222222222",
                    "COINALYZE_ENABLE": True,
                    "STRUCTURE_MIN_SCORE": "70",
                },
                path=env_path,
            )
            text = env_path.read_text(encoding="utf-8")
            backups = list(Path(tmp).glob(".env.oi.bak.web.*"))

        self.assertTrue(result["ok"])
        self.assertIn("TG_CHAT_ID=-1002222222222", text)
        self.assertIn("UNCHANGED=value", text)
        self.assertIn("COINALYZE_ENABLE=true", text)
        self.assertIn("STRUCTURE_MIN_SCORE=70", text)
        self.assertEqual(len(backups), 1)

    def test_write_env_updates_rejects_unknown_key(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("TG_CHAT_ID=-1001111111111\n", encoding="utf-8")

            result = web.write_env_updates({"DANGEROUS": "1"}, path=env_path)

        self.assertFalse(result["ok"])
        self.assertIn("DANGEROUS", result["errors"])

    def test_non_loopback_web_requires_token(self) -> None:
        with patch.dict(os.environ, {"WEB_ADMIN_TOKEN": ""}):
            self.assertEqual(web.run_web_server("0.0.0.0", 8080, ""), 2)

    def test_index_localizes_bool_options_and_explains_actions(self) -> None:
        html = web.INDEX_HTML

        self.assertIn('value="true" ${selectedTrue}>开启', html)
        self.assertIn('value="false" ${selectedFalse}>关闭', html)
        self.assertNotIn(">true</option>", html)
        self.assertNotIn(">false</option>", html)
        self.assertIn("readiness 是真实推送前的门禁检查", html)
        self.assertIn("OK 表示通过，WAIT 表示还需要补配置或继续 dry-run 观察", html)
        self.assertIn("当前使用：", html)
        self.assertIn("输入新值才会替换当前值", html)
        self.assertIn("安全起见只显示遮罩值", html)

    def test_overview_uses_readable_summaries_and_collapsed_raw_data(self) -> None:
        html = web.INDEX_HTML

        self.assertIn("主服务运行摘要", html)
        self.assertIn("结构雷达运行摘要", html)
        self.assertIn("Telegram 配置", html)
        self.assertIn("高级排查：原始运行状态 JSON", html)
        self.assertIn('active: "运行中"', html)
        self.assertNotIn("systemd 是否 active", html)
        self.assertNotIn("systemd", html)

    def test_service_page_explains_controls(self) -> None:
        html = web.INDEX_HTML

        self.assertIn("这个页面是控制后台服务开关的，不是普通测试按钮", html)
        self.assertIn("建议优先使用“重启”", html)
        self.assertIn("会暂停对应功能。点击后需要输入 STOP 二次确认", html)
        self.assertIn("改完 .env.oi、推送配置、扫描参数后通常点这个", html)
        self.assertIn("三个不同的后台服务", html)
        self.assertIn('${neutralPill("系统服务")}', html)
        self.assertIn("${escapeHtml(action.button)}</button>", html)
        self.assertNotIn("serviceList.map", html)

    def test_cli_web_command_starts_web_without_runtime_init(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            with patch.object(cli, "make_runtime", side_effect=AssertionError("should not init runtime")):
                with patch("paopao_radar.web.run_web_server", return_value=0) as run_web:
                    code = cli.main(["web", "--host", "127.0.0.1", "--port", "8090", "--web-token", "secret"])

        self.assertEqual(code, 0)
        run_web.assert_called_once_with("127.0.0.1", 8090, "secret")


if __name__ == "__main__":
    unittest.main()
