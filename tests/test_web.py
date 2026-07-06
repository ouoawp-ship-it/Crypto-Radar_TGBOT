from __future__ import annotations

import os
import json
import time
import unittest
from io import BytesIO, StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import paopao_radar.cli as cli
from paopao_radar import web
from paopao_radar.config import Settings
from paopao_radar.signal_store import append_from_push
from paopao_radar.web_services import jobs
from paopao_radar.web_services.api_core import api_contract_self_test
from paopao_radar.web_services.coins import coin_detail_payload, coin_search_payload, coin_timeline_payload
from paopao_radar.web_services.dashboard import dashboard_payload


class WebConsoleTests(unittest.TestCase):
    def test_server_status_payload_includes_core_resource_sections(self) -> None:
        payload = web.server_status_payload()

        self.assertIn("updated_at", payload)
        self.assertIn("host", payload)
        self.assertIn("cpu", payload)
        self.assertIn("memory", payload)
        self.assertIn("disks", payload)
        self.assertGreaterEqual(payload["cpu"]["cores"], 1)
        self.assertIsInstance(payload["disks"], list)
        self.assertTrue(payload["disks"])
        self.assertIn("base_dir", payload["host"])

    def test_proc_cpu_totals_parser_reads_idle_and_total(self) -> None:
        with TemporaryDirectory() as tmp:
            stat_path = Path(tmp) / "stat"
            stat_path.write_text("cpu  10 20 30 40 5 6 7 8 9 10\n", encoding="utf-8")

            total, idle = web.read_proc_cpu_totals(stat_path) or (0, 0)

        self.assertEqual(total, 145)
        self.assertEqual(idle, 45)

    def test_config_payload_exposes_current_secret_values_for_admin_ui(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text(
                "TG_BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz\n"
                "WEB_ADMIN_TOKEN=admin-secret-token\n"
                "TG_CHAT_ID=-1001234567890\n",
                encoding="utf-8",
            )

            payload = web.config_payload(env_path)

        telegram_fields = {
            item["key"]: item
            for item in payload["sections"]["Telegram"]
        }
        web_fields = {
            item["key"]: item
            for item in payload["sections"]["Web 控制台"]
        }
        self.assertEqual(telegram_fields["TG_BOT_TOKEN"]["value"], "123456:abcdefghijklmnopqrstuvwxyz")
        self.assertEqual(telegram_fields["TG_BOT_TOKEN"]["display_value"], "123456:abcdefghijklmnopqrstuvwxyz")
        self.assertTrue(telegram_fields["TG_BOT_TOKEN"]["configured"])
        self.assertIn("...", telegram_fields["TG_BOT_TOKEN"]["masked"])
        self.assertEqual(web_fields["WEB_ADMIN_TOKEN"]["value"], "admin-secret-token")
        self.assertEqual(web_fields["WEB_ADMIN_TOKEN"]["display_value"], "admin-secret-token")
        self.assertEqual(telegram_fields["TG_CHAT_ID"]["value"], "-1001234567890")
        self.assertEqual(telegram_fields["TG_CHAT_ID"]["display_value"], "-1001234567890")

    def test_config_payload_exposes_engineered_field_explanations(self) -> None:
        payload = web.config_payload(Path("__missing_env_for_test__"))

        fields = {
            item["key"]: item
            for items in payload["sections"].values()
            for item in items
        }

        self.assertIn("群推送机器人令牌", fields["TG_BOT_TOKEN"]["purpose"])
        self.assertIn("Telegram 真实推送", fields["TG_CHAT_ID"]["affects"])
        self.assertIn("自动重启 AI 助手服务", fields["AI_MODEL"]["apply"])
        self.assertIn("自动延迟重启 Web 控制台", fields["WEB_PORT"]["apply"])
        self.assertIn("资金费率警报", fields["FUNDING_ALERT_INTERVAL_SEC"]["affects"])
        self.assertIn("结构雷达", fields["STRUCTURE_MIN_SCORE"]["affects"])
        self.assertIn("不影响市值数据", fields["COINALYZE_API_KEY"]["affects"])

    def test_config_payload_exposes_ai_assistant_secret_values(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text(
                "AI_ASSISTANT_ENABLE=true\n"
                "AI_BOT_TOKEN=987654:ai-bot-token\n"
                "AI_API_KEY=sk-ai-secret\n"
                "AI_ALLOWED_CHAT_IDS=-1001234567890,@vip_channel\n"
                "AI_MODEL=deepseek-chat\n"
                "AI_REQUEST_TIMEOUT_SEC=120\n",
                encoding="utf-8",
            )

            payload = web.config_payload(env_path)

        ai_fields = {item["key"]: item for item in payload["sections"]["AI 助手"]}
        self.assertEqual(ai_fields["AI_BOT_TOKEN"]["value"], "987654:ai-bot-token")
        self.assertEqual(ai_fields["AI_BOT_TOKEN"]["display_value"], "987654:ai-bot-token")
        self.assertEqual(ai_fields["AI_API_KEY"]["value"], "sk-ai-secret")
        self.assertEqual(ai_fields["AI_API_KEY"]["display_value"], "sk-ai-secret")
        self.assertEqual(ai_fields["AI_ALLOWED_CHAT_IDS"]["value"], "-1001234567890,@vip_channel")
        self.assertEqual(ai_fields["AI_ASSISTANT_ENABLE"]["value"], "true")
        self.assertEqual(ai_fields["AI_REQUEST_TIMEOUT_SEC"]["value"], "120")
        self.assertEqual(ai_fields["AI_REQUEST_TIMEOUT_SEC"]["kind"], "int")
        self.assertIn("SIGNAL_EVENTS_FILE", ai_fields)
        self.assertIn("SIGNAL_EVENTS_DB_FILE", ai_fields)
        self.assertEqual(ai_fields["SIGNAL_EVENTS_LIMIT"]["kind"], "int")
        self.assertEqual(ai_fields["SIGNAL_EVENTS_RETENTION_DAYS"]["kind"], "int")

    def test_config_payload_reads_auto_created_topic_routes(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            route_path = Path(tmp) / "data" / "tg_topic_routes.json"
            route_path.parent.mkdir()
            env_path.write_text(
                "TG_BOT_TOKEN=123456:abcdefghijklmnopqrstuvwxyz\n"
                "TG_CHAT_ID=-1001234567890\n"
                "TG_AUTO_CREATE_TOPICS=true\n",
                encoding="utf-8",
            )
            route_path.write_text(json.dumps({
                "routes": {
                    "TG_RADAR_SUMMARY": {"name": "资金摘要", "topic_id": "11"},
                    "TG_LAUNCH_ALERT": {"name": "启动预警", "topic_id": "12"},
                    "TG_FLOW_RADAR": {"name": "资金流雷达", "topic_id": "15"},
                    "TG_FUNDING_ALERT": {"name": "资金费率警报", "topic_id": "18"},
                }
            }), encoding="utf-8")

            payload = web.config_payload(env_path, topic_routes_path=route_path)

        telegram_fields = {
            item["key"]: item
            for item in payload["sections"]["Telegram"]
        }
        self.assertEqual(telegram_fields["TG_RADAR_SUMMARY_TOPIC_ID"]["value"], "")
        self.assertEqual(telegram_fields["TG_RADAR_SUMMARY_TOPIC_ID"]["display_value"], "11")
        self.assertEqual(telegram_fields["TG_RADAR_SUMMARY_TOPIC_ID"]["source"], "auto_route")
        self.assertEqual(telegram_fields["TG_RADAR_SUMMARY_TOPIC_ID"]["route_name"], "资金摘要")
        self.assertTrue(telegram_fields["TG_LAUNCH_ALERT_TOPIC_ID"]["configured"])
        self.assertEqual(telegram_fields["TG_LAUNCH_ALERT_TOPIC_ID"]["display_value"], "12")
        self.assertEqual(telegram_fields["TG_FLOW_RADAR_TOPIC_ID"]["display_value"], "15")
        self.assertEqual(telegram_fields["TG_FUNDING_ALERT_TOPIC_ID"]["display_value"], "18")
        self.assertEqual(telegram_fields["TG_FUNDING_ALERT_TOPIC_ID"]["source"], "auto_route")

    def test_config_payload_resolves_relative_topic_routes_file_under_data_dir(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            route_path = Path(tmp) / "data" / "tg_topic_routes.json"
            route_path.parent.mkdir()
            env_path.write_text(
                "TG_CHAT_ID=-1001234567890\n"
                "TG_TOPIC_ROUTES_FILE=tg_topic_routes.json\n",
                encoding="utf-8",
            )
            route_path.write_text(json.dumps({
                "routes": {
                    "TG_LAUNCH_ALERT": {"name": "启动预警", "topic_id": "30"},
                }
            }), encoding="utf-8")

            payload = web.config_payload(env_path)

        telegram_fields = {
            item["key"]: item
            for item in payload["sections"]["Telegram"]
        }
        self.assertEqual(payload["topic_routes_file"], str(route_path))
        self.assertTrue(payload["topic_routes_found"])
        self.assertEqual(telegram_fields["TG_LAUNCH_ALERT_TOPIC_ID"]["display_value"], "30")
        self.assertEqual(telegram_fields["TG_LAUNCH_ALERT_TOPIC_ID"]["source"], "auto_route")

    def test_config_payload_exposes_structure_review_recommendation_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text(
                "STRUCTURE_MIN_SCORE=65\n"
                "STRUCTURE_SEND_CHART_TOP_N=3\n",
                encoding="utf-8",
            )

            payload = web.config_payload(env_path)

        radar_fields = {
            item["key"]: item
            for item in payload["sections"]["雷达参数"]
        }
        self.assertEqual(radar_fields["STRUCTURE_MIN_SCORE"]["display_value"], "65")
        self.assertEqual(radar_fields["STRUCTURE_SEND_CHART_TOP_N"]["display_value"], "3")
        self.assertIn("复盘建议", radar_fields["STRUCTURE_MIN_SCORE"]["help"])
        self.assertIn("复盘建议", radar_fields["STRUCTURE_SEND_CHART_TOP_N"]["help"])

    def test_config_payload_exposes_module_switches(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text(
                "STRUCTURE_RADAR_ENABLE=true\n"
                "STRUCTURE_REVIEW_ENABLE=true\n"
                "CLEANUP_ENABLE=true\n",
                encoding="utf-8",
            )

            payload = web.config_payload(env_path)

        switch_fields = {item["key"]: item for item in payload["sections"]["模块开关"]}
        self.assertEqual(switch_fields["STRUCTURE_RADAR_ENABLE"]["kind"], "bool")
        self.assertEqual(switch_fields["STRUCTURE_REVIEW_ENABLE"]["kind"], "bool")
        self.assertEqual(switch_fields["CLEANUP_ENABLE"]["kind"], "bool")

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
                    "STRUCTURE_SEND_CHART_TOP_N": "2",
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
        self.assertIn("STRUCTURE_SEND_CHART_TOP_N=2", text)
        self.assertEqual(len(backups), 1)

    def test_write_env_updates_rejects_unknown_key(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("TG_CHAT_ID=-1001111111111\n", encoding="utf-8")

            result = web.write_env_updates({"DANGEROUS": "1"}, path=env_path)

        self.assertFalse(result["ok"])
        self.assertIn("DANGEROUS", result["errors"])

    def test_write_env_updates_normalizes_ai_allowed_chat_ids(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("AI_ALLOWED_CHAT_IDS=\n", encoding="utf-8")

            result = web.write_env_updates(
                {"AI_ALLOWED_CHAT_IDS": "-1001111111111 @vip_channel，-1002222222222"},
                path=env_path,
            )
            text = env_path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertIn("AI_ALLOWED_CHAT_IDS=-1001111111111,@vip_channel,-1002222222222", text)

    def test_write_env_updates_normalizes_ai_model_assignment(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("AI_MODEL=deepseek-v4-flash\n", encoding="utf-8")

            result = web.write_env_updates({"AI_MODEL": "AIMODEL=deepseek-v4-pro"}, path=env_path)
            text = env_path.read_text(encoding="utf-8")

        self.assertTrue(result["ok"])
        self.assertIn("AI_MODEL=deepseek-v4-pro", text)
        self.assertNotIn("AI_MODEL=AIMODEL=", text)

    def test_restore_env_backup_restores_file_and_reports_changes(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            backup_path = Path(tmp) / ".env.oi.bak.web.20260630_010203"
            env_path.write_text("TG_CHAT_ID=-1001111111111\nSTRUCTURE_MIN_SCORE=65\n", encoding="utf-8")
            backup_path.write_text("TG_CHAT_ID=-1002222222222\nSTRUCTURE_MIN_SCORE=70\n", encoding="utf-8")

            result = web.restore_env_backup(backup_path.name, path=env_path)
            text = env_path.read_text(encoding="utf-8")
            backups = list(Path(tmp).glob(".env.oi.bak.web.*"))

        self.assertTrue(result["ok"])
        self.assertIn("TG_CHAT_ID=-1002222222222", text)
        self.assertIn("STRUCTURE_MIN_SCORE=70", text)
        self.assertIn("TG_CHAT_ID", result["changed"])
        self.assertIn("STRUCTURE_MIN_SCORE", result["changed"])
        self.assertGreaterEqual(len(backups), 2)

    def test_delete_env_backup_removes_web_backup(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            backup_path = Path(tmp) / ".env.oi.bak.web.20260630_010203"
            env_path.write_text("TG_CHAT_ID=-1001111111111\n", encoding="utf-8")
            backup_path.write_text("TG_CHAT_ID=-1002222222222\n", encoding="utf-8")

            result = web.delete_env_backup(backup_path.name, path=env_path)
            backup_exists_after = backup_path.exists()

        self.assertTrue(result["ok"])
        self.assertEqual(result["deleted"], ".env.oi.bak.web.20260630_010203")
        self.assertFalse(backup_exists_after)

    def test_delete_env_backup_rejects_unsafe_or_manual_backup(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            manual_backup = Path(tmp) / ".env.oi.bak.manual"
            env_path.write_text("TG_CHAT_ID=-1001111111111\n", encoding="utf-8")
            manual_backup.write_text("TG_CHAT_ID=-1002222222222\n", encoding="utf-8")

            unsafe_result = web.delete_env_backup("../.env.oi", path=env_path)
            manual_result = web.delete_env_backup(manual_backup.name, path=env_path)
            manual_backup_exists_after = manual_backup.exists()

        self.assertFalse(unsafe_result["ok"])
        self.assertFalse(manual_result["ok"])
        self.assertTrue(manual_backup_exists_after)

    def test_web_audit_records_safe_operation_summary(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)

            web.append_web_audit(
                "/api/config",
                {
                    "updates": {
                        "TG_BOT_TOKEN": "123456:secret-token",
                        "TG_CHAT_ID": "-1001234567890",
                    },
                    "clear": [],
                },
                {"ok": True, "changed": ["TG_BOT_TOKEN", "TG_CHAT_ID"], "message": "配置已保存"},
                status=200,
                started_at=web.time.time(),
                data_dir=data_dir,
            )
            payload = web.web_audit_payload(data_dir=data_dir, search="TG_BOT_TOKEN")

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["matched"], 1)
        record = payload["records"][0]
        self.assertEqual(record["action"], "保存配置")
        self.assertEqual(record["path"], "/api/config")
        self.assertTrue(record["ok"])
        self.assertIn("TG_BOT_TOKEN", record["details"]["keys"])
        self.assertNotIn("secret-token", json.dumps(record, ensure_ascii=False))

    def test_web_audit_payload_filters_failed_records(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            started = web.time.time()

            web.append_web_audit(
                "/api/service",
                {"name": "restart-main"},
                {"ok": True, "message": "已重启"},
                status=200,
                started_at=started,
                data_dir=data_dir,
            )
            web.append_web_audit(
                "/api/action",
                {"name": "telegram-test"},
                {"ok": False, "stderr": "Telegram Forbidden"},
                status=200,
                started_at=started,
                data_dir=data_dir,
            )
            payload = web.web_audit_payload(data_dir=data_dir, result="failed", search="telegram")

        self.assertEqual(payload["total"], 2)
        self.assertEqual(payload["matched"], 1)
        self.assertEqual(payload["records"][0]["action"], "执行检查测试")
        self.assertIn("Telegram Forbidden", payload["records"][0]["error"])

    def test_problem_state_updates_redacts_and_clears_records(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            result = web.update_problem_state_payload(
                {
                    "fingerprint": "abc123",
                    "status": "acknowledged",
                    "title": "AI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz failed",
                    "key": "log-errors",
                    "target": "logs",
                    "note": "token=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
                },
                data_dir=data_dir,
            )
            payload = web.problem_state_payload(data_dir=data_dir)
            clear = web.update_problem_state_payload({"fingerprint": "abc123", "status": "clear"}, data_dir=data_dir)
            cleared = web.problem_state_payload(data_dir=data_dir)

        self.assertTrue(result["ok"])
        self.assertEqual(payload["total"], 1)
        self.assertEqual(payload["records"][0]["status"], "acknowledged")
        payload_text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("<redacted", payload_text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", payload_text)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", payload_text)
        self.assertTrue(clear["ok"])
        self.assertEqual(cleared["total"], 0)

    def test_stable_check_web_action_creates_background_job(self) -> None:
        with patch.object(web, "create_job_payload", return_value={"ok": True, "job": {"id": 12, "job_type": "stable-check"}}) as create_job:
            result = web.run_cli_action("stable-check")

        self.assertTrue(result["ok"])
        self.assertTrue(result["job_created"])
        self.assertEqual(result["job"]["id"], 12)
        self.assertEqual(result["label"], web.CLI_ACTIONS["stable-check"]["label"])
        create_job.assert_called_once()

    def test_ops_snapshot_payload_redacts_sensitive_log_values(self) -> None:
        summary = {
            "git": {"version": "v-test", "branch": "main", "commit": "abc123"},
            "services": {"main": {"active_ok": True}},
            "health": [{"label": "主服务", "status": "ok", "value": "运行中", "detail": ""}],
            "recent_errors": [],
            "runtime": {"main": {"status": "running"}},
            "config": {"telegram": {"bot_token_configured": True}},
            "state_files": [],
        }

        def fake_logs(target: str, lines: int) -> dict[str, object]:
            return {
                "ok": True,
                "source": f"fake:{target}",
                "text": (
                    "INFO ok\n"
                    "ERROR bot token=123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi failed\n"
                    "ERROR AI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz timeout\n"
                ),
            }

        with TemporaryDirectory() as tmp:
            with patch.object(web, "summary_payload", return_value=summary):
                with patch.object(web, "web_audit_payload", return_value={"records": [], "total": 0, "matched": 0}):
                    with patch.object(web, "logs_payload", side_effect=fake_logs):
                        with patch.object(Settings, "load", return_value=Settings(data_dir=Path(tmp))):
                            payload = web.ops_snapshot_payload()

        payload_text = json.dumps(payload, ensure_ascii=False)
        self.assertTrue(payload["ok"])
        self.assertIn("log_errors", payload)
        self.assertIn("issues", payload)
        self.assertIn("stability", payload)
        self.assertIn("stability_history", payload)
        self.assertIn("problem_center", payload)
        self.assertIn("release_readiness", payload)
        self.assertIn("release_trend", payload)
        self.assertIn("recommendations", payload)
        self.assertNotIn("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", payload_text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", payload_text)
        self.assertIn("<redacted", payload_text)

    def test_summary_payload_includes_web_config_for_deployment_acceptance(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp) / "data"
            data_dir.mkdir()
            settings = Settings(data_dir=data_dir)
            with patch.object(Settings, "load", return_value=settings):
                with patch.object(
                    Settings,
                    "redacted_status",
                    return_value={
                        "env_file_exists": True,
                        "telegram": {},
                        "runtime": {},
                        "web": {"host": "0.0.0.0", "port": 8080, "admin_token_configured": True},
                        "liquidity": {},
                        "coinalyze": {},
                        "ai_assistant": {},
                        "structure_radar": {},
                    },
                ):
                    with patch.object(web, "service_status", return_value={"active_ok": True}):
                        payload = web.summary_payload()

        self.assertEqual(payload["config"]["web"]["host"], "0.0.0.0")
        self.assertEqual(payload["config"]["web"]["port"], 8080)
        self.assertTrue(payload["config"]["web"]["admin_token_configured"])

    def test_stability_checks_ready_when_core_snapshot_is_clean(self) -> None:
        snapshot = {
            "git": {"version": "v1.35.0", "branch": "main", "commit": "abc123"},
            "services": {
                "main": {"active_ok": True},
                "structure": {"active_ok": True},
                "web": {"active_ok": True},
                "ai": {"active_ok": True},
            },
            "health": [{"label": "主服务", "status": "ok", "value": "运行中"}],
            "issues": [],
            "audit": {"failed_recent": []},
            "log_errors": {
                "main": {"error_count": 0, "transient_count": 0},
                "ai": {"error_count": 0, "transient_count": 2},
            },
            "config": {
                "telegram": {"bot_token_configured": True, "chat_id_configured": True},
                "ai_assistant": {"enable": True, "bot_token_configured": True},
            },
        }

        stability = web.build_stability_checks(snapshot)

        self.assertEqual(stability["status"], "ready")
        self.assertEqual(stability["fail_count"], 0)
        self.assertEqual(stability["warn_count"], 0)
        self.assertTrue(all(item["status"] == "ok" for item in stability["checks"]))

    def test_stability_checks_block_when_services_or_health_fail(self) -> None:
        snapshot = {
            "git": {"version": "unknown", "branch": "main", "commit": "unknown"},
            "services": {
                "main": {"active_ok": False},
                "structure": {"active_ok": True},
                "web": {"active_ok": True},
                "ai": {"active_ok": True},
            },
            "health": [{"label": "主服务", "status": "bad", "value": "failed"}],
            "issues": [{"severity": "critical", "title": "主服务异常"}],
            "audit": {"failed_recent": []},
            "log_errors": {"main": {"error_count": 25, "transient_count": 0}},
            "config": {
                "telegram": {"bot_token_configured": False, "chat_id_configured": True},
                "ai_assistant": {"enable": True, "bot_token_configured": True},
            },
        }

        stability = web.build_stability_checks(snapshot)

        self.assertEqual(stability["status"], "blocked")
        labels = "\n".join(item["label"] for item in stability["checks"] if item["status"] == "fail")
        self.assertIn("版本信息", labels)
        self.assertIn("后台服务", labels)
        self.assertIn("健康门禁", labels)
        self.assertIn("关键配置", labels)

    def test_save_stability_snapshot_writes_latest_and_trimmed_history(self) -> None:
        with TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            first = {
                "generated_at": "2026-07-04 08:00:00",
                "git": {"version": "v1.37.0", "branch": "main", "commit": "aaa111"},
                "stability": {"status": "ready", "label": "达到稳定版标准", "summary": "第一次", "ok_count": 7, "warn_count": 0, "fail_count": 0},
                "problem_center": {"status": "ok", "summary": "ok", "counts": {"log_errors": 0, "failed_audit": 0, "transient_timeouts": 0}},
                "issues": [],
                "log_errors": {"main": {"error_count": 0, "transient_count": 0}},
            }
            second = {
                "generated_at": "2026-07-04 09:00:00",
                "git": {"version": "v1.37.0", "branch": "main", "commit": "bbb222"},
                "stability": {"status": "attention", "label": "基本可运行，建议关注", "summary": "第二次", "ok_count": 6, "warn_count": 1, "fail_count": 0},
                "problem_center": {"status": "attention", "summary": "warning", "counts": {"log_errors": 1, "failed_audit": 0, "transient_timeouts": 3}},
                "issues": [{"severity": "warning"}],
                "log_errors": {"ai": {"error_count": 1, "transient_count": 3}},
            }
            third = {
                "generated_at": "2026-07-04 10:00:00",
                "git": {"version": "v1.37.0", "branch": "main", "commit": "ccc333"},
                "stability": {"status": "blocked", "label": "未达稳定版标准", "summary": "第三次", "ok_count": 4, "warn_count": 1, "fail_count": 2},
                "problem_center": {
                    "status": "blocked",
                    "summary": "blocked",
                    "counts": {"log_errors": 2, "failed_audit": 0, "transient_timeouts": 0},
                    "problem_state": {
                        "review": {
                            "status": "attention",
                            "summary": "1 个已标记解决的问题仍然存在。",
                            "counts": {"resolved_active": 1, "resolved_missing": 0, "tracked_active": 1, "tracked_missing": 0},
                        }
                    },
                },
                "issues": [{"severity": "critical"}],
                "log_errors": {"main": {"error_count": 2, "transient_count": 0}},
            }

            web.save_stability_snapshot(first, data_dir=data_dir, limit=2)
            web.save_stability_snapshot(second, data_dir=data_dir, limit=2)
            result = web.save_stability_snapshot(third, data_dir=data_dir, limit=2)

            latest = json.loads(Path(result["latest_path"]).read_text(encoding="utf-8"))
            history = web.load_stability_history(data_dir=data_dir, limit=10)
            payload = web.stability_history_payload(data_dir=data_dir, limit=10)

        self.assertEqual(latest["stability"]["status"], "blocked")
        self.assertEqual(latest["release_readiness"]["status"], "blocked")
        self.assertEqual(latest["release_trend"]["status"], "regressed")
        self.assertEqual(latest["release_trend"]["previous_status"], "candidate")
        self.assertTrue(any(item["key"] == "release-trend" for item in latest["problem_center"]["action_plan"]))
        self.assertEqual(latest["stability_history"]["records"][0]["release_status"], "blocked")
        self.assertIsInstance(latest["stability_history"]["records"][0]["release_score"], int)
        self.assertEqual(latest["stability_history"]["records"][0]["closure_target_version"], "v1.50.0")
        self.assertEqual(latest["stability_history"]["records"][0]["closure_current_stage"], "v1.47.0")
        self.assertEqual(latest["stability_history"]["records"][0]["deployment_status"], "blocked")
        self.assertEqual([row["commit"] for row in history], ["ccc333", "bbb222"])
        self.assertEqual([row["release_status"] for row in history], ["blocked", "candidate"])
        self.assertEqual(payload["latest"]["commit"], "ccc333")
        self.assertEqual(payload["latest"]["release_status"], "blocked")
        self.assertEqual(payload["count"], 2)

    def test_release_trend_detects_improvement_regression_and_single_history(self) -> None:
        improved = web.build_release_trend(
            {
                "records": [
                    {"release_status": "complete_candidate", "release_label": "完整稳定版候选", "release_score": 100, "ts": "t2"},
                    {"release_status": "candidate", "release_label": "准稳定候选", "release_score": 84, "ts": "t1"},
                ]
            }
        )
        regressed = web.build_release_trend(
            {
                "records": [
                    {"release_status": "blocked", "release_label": "需要处理", "release_score": 58, "ts": "t3"},
                    {"release_status": "candidate", "release_label": "准稳定候选", "release_score": 84, "ts": "t2"},
                ]
            }
        )
        single = web.build_release_trend({"records": [{"release_status": "candidate", "release_score": 84}]})

        self.assertEqual(improved["status"], "improved")
        self.assertEqual(improved["score_delta"], 16)
        self.assertEqual(regressed["status"], "regressed")
        self.assertEqual(regressed["previous_status"], "candidate")
        self.assertEqual(single["status"], "single")

    def test_problem_center_reports_ok_when_snapshot_is_clean(self) -> None:
        snapshot = {
            "health": [{"label": "主服务", "status": "ok"}],
            "recent_errors": [],
            "audit": {"failed_recent": []},
            "log_errors": {"ai": {"error_count": 0, "transient_count": 2}},
            "issues": [],
            "stability": {"status": "ready", "fail_count": 0, "warn_count": 0},
        }

        center = web.build_problem_center(snapshot)

        self.assertEqual(center["status"], "ok")
        self.assertEqual(center["label"], "当前健康")
        self.assertEqual(center["counts"]["transient_timeouts"], 2)
        self.assertEqual(center["modules"], [])
        self.assertEqual(center["action_plan"][0]["key"], "observe")
        self.assertEqual(center["action_plan"][0]["target"], "actions")
        self.assertIn("暂无需要立即处理", "\n".join(center["next_steps"]))

    def test_problem_center_blocks_on_critical_issues_and_summarizes_modules(self) -> None:
        snapshot = {
            "health": [{"label": "主服务", "status": "bad"}],
            "recent_errors": [{"source": "主服务", "level": "异常", "message": "boom"}],
            "audit": {"failed_recent": [{"action": "保存配置"}]},
            "log_errors": {"main": {"error_count": 12, "transient_count": 0}},
            "issues": [
                {"severity": "critical", "module": "主服务", "title": "主服务异常", "count": 1, "target": "main"},
                {"severity": "warning", "module": "Web 后台操作", "title": "保存失败", "count": 2, "target": "audit"},
            ],
            "stability": {"status": "blocked", "fail_count": 2, "warn_count": 1},
        }

        center = web.build_problem_center(snapshot)

        self.assertEqual(center["status"], "blocked")
        self.assertEqual(center["counts"]["critical"], 1)
        self.assertEqual(center["counts"]["warning"], 1)
        self.assertEqual(center["counts"]["log_errors"], 12)
        self.assertEqual(center["counts"]["failed_audit"], 1)
        self.assertEqual(center["modules"][0]["module"], "主服务")
        keys = [item["key"] for item in center["action_plan"]]
        self.assertIn("service-health", keys)
        self.assertIn("log-errors", keys)
        self.assertIn("failed-audit", keys)
        self.assertIn("stable-check", keys)
        self.assertTrue(any(item["target"] == "logs" and item["log_target"] == "main" for item in center["action_plan"]))
        self.assertTrue(any("严重问题" in item for item in center["next_steps"]))

    def test_problem_center_merges_problem_state_into_action_plan(self) -> None:
        snapshot = {
            "health": [{"label": "主服务", "status": "bad"}],
            "recent_errors": [],
            "audit": {"failed_recent": []},
            "log_errors": {"main": {"error_count": 12, "transient_count": 0}},
            "issues": [{"severity": "critical", "module": "主服务", "title": "主服务异常", "count": 1, "target": "main"}],
            "stability": {"status": "blocked", "fail_count": 1, "warn_count": 0},
        }
        first_center = web.build_problem_center(snapshot)
        log_action = next(item for item in first_center["action_plan"] if item["key"] == "log-errors")
        state = {
            "records": [
                {
                    "fingerprint": log_action["fingerprint"],
                    "status": "resolved",
                    "label": "已解决观察中",
                    "updated_at": "2026-07-04 12:00:00",
                },
                {
                    "fingerprint": "missing-resolved",
                    "status": "resolved",
                    "label": "已解决观察中",
                    "updated_at": "2026-07-04 12:05:00",
                },
            ],
            "total": 2,
        }

        center = web.build_problem_center(snapshot, state)
        action = next(item for item in center["action_plan"] if item["key"] == "log-errors")

        self.assertEqual(action["state_status"], "resolved")
        self.assertEqual(action["state_label"], "已解决观察中")
        self.assertEqual(center["counts"]["action_resolved"], 1)
        self.assertEqual(center["counts"]["action_open"], len([item for item in center["action_plan"] if item["key"] != "observe"]) - 1)
        self.assertEqual(center["counts"]["state_resolved_active"], 1)
        self.assertEqual(center["counts"]["state_resolved_missing"], 1)
        self.assertEqual(center["problem_state"]["total"], 2)
        self.assertEqual(center["problem_state"]["review"]["status"], "attention")
        review_statuses = {item["fingerprint"]: item["review_status"] for item in center["problem_state"]["records"]}
        self.assertEqual(review_statuses[log_action["fingerprint"]], "still_active")
        self.assertEqual(review_statuses["missing-resolved"], "missing_after_resolved")

    def test_problem_center_action_plan_routes_config_failures(self) -> None:
        snapshot = {
            "health": [{"label": "Telegram 推送", "status": "bad"}],
            "recent_errors": [],
            "audit": {"failed_recent": []},
            "log_errors": {},
            "issues": [],
            "stability": {
                "status": "blocked",
                "fail_count": 1,
                "warn_count": 0,
                "checks": [{"key": "config", "status": "fail", "label": "关键配置"}],
            },
        }

        center = web.build_problem_center(snapshot)

        self.assertEqual(center["status"], "blocked")
        self.assertTrue(any(item["key"] == "config-check" and item["target"] == "config" for item in center["action_plan"]))

    def test_problem_center_promotes_release_trend_regression_to_action_plan(self) -> None:
        snapshot = {
            "health": [],
            "recent_errors": [],
            "audit": {"failed_recent": []},
            "log_errors": {},
            "issues": [],
            "stability": {"status": "ready", "fail_count": 0, "warn_count": 0},
            "release_trend": {
                "status": "regressed",
                "label": "发生回退",
                "summary": "长期运行就绪度从候选状态回退到需要处理。",
                "action": "优先打开问题中心和日志中心。",
            },
        }

        center = web.build_problem_center(snapshot)

        self.assertEqual(center["status"], "blocked")
        self.assertEqual(center["counts"]["release_trend_regressed"], 1)
        self.assertTrue(any(item["key"] == "release-trend" and item["target"] == "report" for item in center["action_plan"]))
        self.assertTrue(any(item["button"] == "查看趋势详情" for item in center["action_plan"]))
        self.assertTrue(any("长期运行趋势发生回退" in item for item in center["next_steps"]))

    def test_problem_center_treats_pure_release_trend_worse_as_observation(self) -> None:
        snapshot = {
            "health": [{"label": "main", "status": "ok"}],
            "recent_errors": [],
            "audit": {"failed_recent": []},
            "log_errors": {"main": {"error_count": 0, "transient_count": 0}},
            "issues": [],
            "stability": {"status": "ready", "fail_count": 0, "warn_count": 0},
            "release_trend": {"status": "worse", "summary": "score down"},
        }

        center = web.build_problem_center(snapshot)

        self.assertEqual(center["status"], "ok")
        self.assertEqual(center["counts"]["release_trend_worse"], 1)
        self.assertFalse(any(item["key"] == "release-trend" for item in center["action_plan"]))
        self.assertEqual(center["action_plan"][0]["key"], "observe")
        self.assertTrue(any("stable-check" in item for item in center["next_steps"]))

    def test_release_readiness_complete_candidate_when_clean_and_history_ready(self) -> None:
        snapshot = {
            "stability": {"status": "ready", "summary": "ok"},
            "problem_center": {
                "status": "ok",
                "summary": "ok",
                "counts": {"log_errors": 0, "failed_audit": 0, "transient_timeouts": 0},
            },
            "stability_history": {
                "latest": {"status": "ready"},
                "records": [{"status": "ready"}, {"status": "ready"}],
            },
        }

        readiness = web.build_release_readiness(snapshot)

        self.assertEqual(readiness["status"], "complete_candidate")
        self.assertEqual(readiness["score"], 100)
        self.assertEqual(readiness["fail_count"], 0)
        self.assertEqual(readiness["warn_count"], 0)
        self.assertEqual(readiness["closure_plan"]["target_version"], "v1.50.0")
        self.assertTrue(readiness["closure_plan"]["no_new_major_features"])
        self.assertEqual(readiness["closure_plan"]["current_stage"]["version"], "v1.47.0")
        self.assertEqual(readiness["closure_plan"]["current_stage"]["status"], "ready_to_advance")
        self.assertTrue(any(item["key"] == "stability_history" and item["status"] == "ok" for item in readiness["checks"]))

    def test_release_readiness_does_not_penalize_pure_release_trend_worse(self) -> None:
        snapshot = {
            "git": {"version": "v1.50.0"},
            "stability": {"status": "ready", "summary": "ok"},
            "problem_center": {
                "status": "ok",
                "summary": "ok",
                "counts": {"log_errors": 0, "failed_audit": 0, "transient_timeouts": 0},
            },
            "stability_history": {
                "latest": {"status": "ready"},
                "records": [{"status": "ready"}, {"status": "ready"}],
            },
            "release_trend": {"status": "worse", "summary": "score down"},
        }

        readiness = web.build_release_readiness(snapshot)
        trend_check = next(item for item in readiness["checks"] if item["key"] == "release_trend")

        self.assertEqual(readiness["status"], "complete_candidate")
        self.assertEqual(readiness["score"], 100)
        self.assertEqual(readiness["warn_count"], 0)
        self.assertEqual(trend_check["status"], "ok")

    def test_release_readiness_marks_v150_as_final_release_when_clean(self) -> None:
        snapshot = {
            "git": {"version": "v1.50.0"},
            "stability": {"status": "ready", "summary": "ok"},
            "problem_center": {
                "status": "ok",
                "summary": "ok",
                "counts": {"log_errors": 0, "failed_audit": 0, "transient_timeouts": 0},
            },
            "stability_history": {
                "latest": {"status": "ready"},
                "records": [{"status": "ready"}, {"status": "ready"}],
            },
            "release_trend": {"status": "stable", "summary": "稳定"},
        }

        readiness = web.build_release_readiness(snapshot)
        closure = readiness["closure_plan"]

        self.assertEqual(readiness["status"], "complete_candidate")
        self.assertEqual(readiness["next_version_goal"], "v1.50.0：已经达到 v1 完整稳定版发布门槛，后续进入长期维护。")
        self.assertEqual(closure["mode"], "v1 完整稳定版发布")
        self.assertEqual(closure["current_stage"]["version"], "v1.50.0")
        self.assertEqual(closure["current_stage"]["status"], "complete")
        self.assertIsNone(closure["next_stage"])
        self.assertTrue(closure["final_release"])
        self.assertIn("v2 规划", closure["maintenance_policy"])

    def test_release_readiness_candidate_when_history_is_not_enough(self) -> None:
        snapshot = {
            "stability": {"status": "ready", "summary": "ok"},
            "problem_center": {
                "status": "ok",
                "summary": "ok",
                "counts": {"log_errors": 0, "failed_audit": 0, "transient_timeouts": 0},
            },
            "stability_history": {
                "latest": {"status": "ready"},
                "records": [{"status": "ready"}],
            },
        }

        readiness = web.build_release_readiness(snapshot)

        self.assertEqual(readiness["status"], "candidate")
        self.assertEqual(readiness["fail_count"], 0)
        self.assertGreater(readiness["warn_count"], 0)
        self.assertLess(readiness["score"], 100)

    def test_release_readiness_blocks_on_current_failures(self) -> None:
        snapshot = {
            "stability": {"status": "blocked", "summary": "blocked"},
            "problem_center": {
                "status": "blocked",
                "summary": "bad",
                "counts": {"log_errors": 12, "failed_audit": 1, "transient_timeouts": 0},
            },
            "stability_history": {
                "latest": {"status": "blocked"},
                "records": [{"status": "blocked"}],
            },
        }

        readiness = web.build_release_readiness(snapshot)

        self.assertEqual(readiness["status"], "blocked")
        self.assertGreater(readiness["fail_count"], 0)
        self.assertLess(readiness["score"], 80)
        self.assertTrue(any(item["key"] == "log_errors" and item["status"] == "fail" for item in readiness["checks"]))

    def test_release_readiness_blocks_when_resolved_problem_is_still_active(self) -> None:
        snapshot = {
            "git": {"version": "v1.47.0"},
            "stability": {"status": "ready", "summary": "ok"},
            "problem_center": {
                "status": "ok",
                "summary": "ok",
                "counts": {"log_errors": 0, "failed_audit": 0, "transient_timeouts": 0},
                "problem_state": {
                    "review": {
                        "status": "attention",
                        "summary": "1 个已标记解决的问题仍然存在。",
                        "counts": {"resolved_active": 1, "resolved_missing": 0},
                    }
                },
            },
            "stability_history": {
                "latest": {"status": "ready"},
                "records": [{"status": "ready"}, {"status": "ready"}],
            },
            "release_trend": {"status": "stable", "summary": "趋势持平"},
        }

        readiness = web.build_release_readiness(snapshot)

        self.assertEqual(readiness["status"], "blocked")
        self.assertEqual(readiness["closure_plan"]["current_stage"]["status"], "blocked")
        self.assertTrue(any(item["key"] == "problem_state_review" and item["status"] == "fail" for item in readiness["checks"]))

    def test_deployment_acceptance_ready_when_server_contract_is_met(self) -> None:
        snapshot = {
            "git": {"version": "v1.48.0", "commit": "abc123"},
            "services": {
                "main": {"active_ok": True},
                "structure": {"active_ok": True},
                "web": {"active_ok": True},
                "ai": {"active_ok": True},
            },
            "config": {
                "web": {"host": "0.0.0.0", "port": 8080, "admin_token_configured": True},
                "telegram": {"bot_token_configured": True, "chat_id_configured": True},
                "ai_assistant": {"enable": True, "bot_token_configured": True},
            },
            "stability": {"status": "ready"},
            "release_readiness": {"status": "complete_candidate"},
            "log_errors": {"main": {"error_count": 0}},
            "audit": {"failed_recent": []},
        }

        deployment = web.build_deployment_acceptance(snapshot)

        self.assertEqual(deployment["status"], "ready")
        self.assertEqual(deployment["fail_count"], 0)
        self.assertTrue(any(item["key"] == "web_entry" and item["status"] == "ok" for item in deployment["checks"]))

    def test_deployment_acceptance_blocks_on_missing_web_token(self) -> None:
        snapshot = {
            "git": {"version": "v1.48.0", "commit": "abc123"},
            "services": {
                "main": {"active_ok": True},
                "structure": {"active_ok": True},
                "web": {"active_ok": True},
            },
            "config": {
                "web": {"host": "0.0.0.0", "port": 8080, "admin_token_configured": False},
                "telegram": {"bot_token_configured": True, "chat_id_configured": True},
                "ai_assistant": {"enable": False},
            },
            "stability": {"status": "ready"},
            "release_readiness": {"status": "candidate"},
            "log_errors": {},
            "audit": {"failed_recent": []},
        }

        deployment = web.build_deployment_acceptance(snapshot)

        self.assertEqual(deployment["status"], "blocked")
        self.assertTrue(any(item["key"] == "web_entry" and item["status"] == "fail" for item in deployment["checks"]))

    def test_log_error_excerpt_ignores_empty_errors_field(self) -> None:
        def fake_logs(target: str, lines: int) -> dict[str, object]:
            return {
                "ok": True,
                "source": f"fake:{target}",
                "text": (
                    'ai-assistant: alert_check elapsed=0.52s queue=0 {"ok": true, "enabled": true, "errors": []}\n'
                    'ai-assistant: price_check ok=true error=""\n'
                    "ai-assistant: running username=VIPpao_bot poll_timeout=5s alert_interval=30s workers=8\n"
                    "INFO normal line\n"
                    "ERROR real failure timeout\n"
                ),
            }

        with patch.object(web, "logs_payload", side_effect=fake_logs):
            payload = web.log_error_excerpt("ai", lines=80, limit=10)

        self.assertEqual(payload["error_count"], 1)
        self.assertEqual(payload["lines"], ["ERROR real failure timeout"])

    def test_log_error_excerpt_classifies_getupdates_timeout_as_transient(self) -> None:
        def fake_logs(target: str, lines: int) -> dict[str, object]:
            return {
                "ok": True,
                "source": f"fake:{target}",
                "text": (
                    "ai-assistant: getUpdates failed ReadTimeout: HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out. (read timeout=10)\n"
                    "ai-assistant: getUpdates failed ReadTimeout: HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out. (read timeout=10)\n"
                    "ai-assistant: getUpdates failed ReadTimeout: HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out. (read timeout=10)\n"
                ),
            }

        with patch.object(web, "logs_payload", side_effect=fake_logs):
            payload = web.log_error_excerpt("ai", lines=80, limit=10)

        self.assertEqual(payload["error_count"], 0)
        self.assertEqual(payload["transient_count"], 3)
        self.assertEqual(len(payload["transient_lines"]), 3)

    def test_log_error_excerpt_classifies_market_cap_timeout_as_transient(self) -> None:
        def fake_logs(target: str, lines: int) -> dict[str, object]:
            return {
                "ok": True,
                "source": f"fake:{target}",
                "text": (
                    'Jul 05 05:04:21 python[438959]:         "coinpaprikaMarketCaps: ReadTimeout",\n'
                    "Jul 05 05:04:22 python[438959]: normal scan finished\n"
                ),
            }

        with patch.object(web, "logs_payload", side_effect=fake_logs):
            payload = web.log_error_excerpt("main", lines=80, limit=10)

        self.assertEqual(payload["error_count"], 0)
        self.assertEqual(payload["transient_count"], 1)
        self.assertIn("coinpaprikaMarketCaps", payload["transient_lines"][0])

    def test_log_error_excerpt_ignores_web_client_disconnect_noise(self) -> None:
        def fake_logs(target: str, lines: int) -> dict[str, object]:
            return {
                "ok": True,
                "source": f"fake:{target}",
                "text": (
                    "Jul 04 11:07:34 python[206331]: Exception occurred during processing of request from ('112.46.214.29', 4909)\n"
                    "Jul 04 11:07:34 python[206331]: Traceback (most recent call last):\n"
                    "Jul 04 11:07:34 python[206331]: ConnectionResetError: [Errno 104] Connection reset by peer\n"
                ),
            }

        with patch.object(web, "logs_payload", side_effect=fake_logs):
            payload = web.log_error_excerpt("web", lines=80, limit=10)

        self.assertEqual(payload["error_count"], 0)
        self.assertEqual(payload["transient_count"], 3)
        self.assertEqual(len(payload["transient_lines"]), 3)

    def test_log_error_excerpt_keeps_real_web_traceback_errors(self) -> None:
        def fake_logs(target: str, lines: int) -> dict[str, object]:
            return {
                "ok": True,
                "source": f"fake:{target}",
                "text": (
                    "Jul 04 11:10:00 python[206331]: Traceback (most recent call last):\n"
                    "Jul 04 11:10:00 python[206331]: ValueError: bad config\n"
                ),
            }

        with patch.object(web, "logs_payload", side_effect=fake_logs):
            payload = web.log_error_excerpt("web", lines=80, limit=10)

        self.assertEqual(payload["error_count"], 1)
        self.assertEqual(payload["lines"], ["Jul 04 11:10:00 python[206331]: ValueError: bad config"])

    def test_ops_snapshot_does_not_recommend_low_transient_timeouts_as_errors(self) -> None:
        snapshot = {
            "health": [{"label": "主服务", "status": "ok"}],
            "recent_errors": [],
            "audit": {"failed_recent": []},
            "log_errors": {"ai": {"error_count": 0, "transient_count": 3}},
        }

        recommendations = web.build_ops_recommendations(snapshot)

        joined = "\n".join(recommendations)
        self.assertIn("当前快照没有发现明显异常", joined)
        self.assertNotIn("错误/异常关键字", joined)

    def test_ops_snapshot_recommends_failed_audit_and_bad_health(self) -> None:
        snapshot = {
            "health": [{"label": "主服务", "status": "bad"}],
            "recent_errors": [{"source": "主服务", "message": "boom"}],
            "audit": {"failed_recent": [{"action": "保存配置"}]},
            "log_errors": {"main": {"error_count": 2}},
        }

        recommendations = web.build_ops_recommendations(snapshot)

        joined = "\n".join(recommendations)
        self.assertIn("异常健康项", joined)
        self.assertIn("失败的 Web 后台操作", joined)
        self.assertIn("日志", joined)

    def test_ops_snapshot_recommends_release_trend_regression(self) -> None:
        snapshot = {
            "health": [],
            "recent_errors": [],
            "audit": {"failed_recent": []},
            "log_errors": {},
            "release_trend": {
                "status": "regressed",
                "label": "发生回退",
                "summary": "长期运行就绪度从候选状态回退到需要处理。",
            },
        }

        recommendations = web.build_ops_recommendations(snapshot)

        self.assertTrue(any("长期运行趋势发生回退" in item for item in recommendations))

    def test_ops_snapshot_recommends_problem_state_still_active(self) -> None:
        snapshot = {
            "health": [],
            "recent_errors": [],
            "audit": {"failed_recent": []},
            "log_errors": {},
            "problem_center": {"counts": {"state_resolved_active": 2, "state_resolved_missing": 0}},
        }

        recommendations = web.build_ops_recommendations(snapshot)

        self.assertTrue(any("已标记解决的问题仍然存在" in item for item in recommendations))

    def test_ops_issues_classify_modules_severity_and_actions(self) -> None:
        snapshot = {
            "health": [{"label": "主服务", "status": "bad", "value": "failed", "detail": "paopao-radar"}],
            "recent_errors": [{"source": "AI 助手", "level": "警告", "message": "接口超时"}],
            "audit": {"failed_recent": [{"action": "保存配置", "message": "HTTP 500"}]},
            "log_errors": {
                "main": {"error_count": 2, "lines": ["ERROR boom"], "transient_count": 0},
                "ai": {"error_count": 0, "lines": [], "transient_count": 12},
            },
        }

        issues = web.build_ops_issues(snapshot)

        titles = "\n".join(str(item["title"]) for item in issues)
        self.assertIn("主服务异常", titles)
        self.assertIn("AI 助手 · 警告", titles)
        self.assertIn("存在失败的 Web 后台操作", titles)
        self.assertIn("主服务日志出现错误关键字", titles)
        self.assertIn("AI 助手网络超时较多", titles)
        self.assertEqual(issues[0]["severity"], "critical")
        self.assertTrue(any(item["target"] == "audit" for item in issues))
        self.assertTrue(any("查看" in item["action"] or "进入" in item["action"] for item in issues))

    def test_structure_review_recommendations_payload_returns_updates(self) -> None:
        with TemporaryDirectory() as tmp:
            stats_path = Path(tmp) / "structure_stats.json"
            stats_path.write_text(
                json.dumps(
                    {
                        "summary": {"total": 40, "reviewed": 12, "hit_rate": 0.1},
                        "by_level": {"B": {"reviewed": 4, "fake_rate": 0.5}},
                        "by_signal_type": {
                            "PRE_BREAKOUT_NEAR": {"total": 16},
                            "PRE_BREAKDOWN_NEAR": {"total": 14},
                        },
                        "by_symbol": {"BTCUSDT": {"total": 12}},
                    }
                ),
                encoding="utf-8",
            )

            class DummySettings:
                structure_stats_path = stats_path
                structure_review_min_sample = 1
                structure_min_score = 65
                structure_send_chart_top_n = 3
                structure_near_edge_pct = 1.5
                structure_cooldown_sec = 3600

            with patch.object(web.Settings, "load", return_value=DummySettings()):
                payload = web.structure_review_recommendations_payload(stats_path)

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["updates"]["STRUCTURE_MIN_SCORE"], "70")
        self.assertEqual(payload["updates"]["STRUCTURE_SEND_CHART_TOP_N"], "2")
        self.assertEqual(payload["updates"]["STRUCTURE_NEAR_EDGE_PCT"], "1.2")
        self.assertEqual(payload["updates"]["STRUCTURE_COOLDOWN_SEC"], "7200")

    def test_health_items_and_recent_errors_summarize_runtime(self) -> None:
        services = {
            "main": {"active_ok": True, "active": "active", "service": "paopao-radar"},
            "structure": {"active_ok": False, "active": "failed", "service": "paopao-structure"},
            "web": {"active_ok": True, "active": "active", "service": "paopao-web"},
        }
        runtime = {
            "main": {"status": "running", "updated_at": "2026-06-30 01:00:00"},
            "structure": {"status": "running", "last_error": "boom"},
        }
        config = {
            "telegram": {"bot_token_configured": True, "chat_id_configured": True, "use_topic": False},
            "liquidity": {"fallback_enable": True},
            "structure_radar": {"enable": True},
        }

        health = web.build_health_items(services, runtime, config)
        errors = web.recent_errors_payload(runtime)

        self.assertTrue(any(item["label"] == "结构雷达" and item["status"] == "bad" for item in health))
        self.assertEqual(errors[0]["source"], "结构雷达")
        self.assertIn("boom", errors[0]["message"])

    def test_recent_errors_suppresses_low_count_funding_failures(self) -> None:
        runtime = {
            "main": {
                "status": "running",
                "diagnostics": {
                    "funding_alert": {
                        "quality": {
                            "failures": {"funding:Gate": 1}
                        }
                    }
                },
            },
            "structure": {"status": "running"},
        }

        errors = web.recent_errors_payload(runtime)

        self.assertEqual(errors, [])

    def test_recent_errors_translates_repeated_funding_failures(self) -> None:
        runtime = {
            "main": {
                "status": "running",
                "diagnostics": {
                    "funding_alert": {
                        "quality": {
                            "failures": {"funding:Gate": 4}
                        }
                    }
                },
            },
            "structure": {"status": "running"},
        }

        errors = web.recent_errors_payload(runtime)

        self.assertEqual(errors[0]["source"], "主服务")
        self.assertEqual(errors[0]["level"], "警告")
        self.assertIn("Gate 资金费率接口失败 4 次", errors[0]["message"])
        self.assertIn("主服务仍在运行", errors[0]["message"])
        self.assertNotIn("{", errors[0]["message"])

    def test_push_preview_payload_is_static_and_safe(self) -> None:
        payload = web.push_preview_payload()

        self.assertTrue(payload["ok"])
        self.assertGreaterEqual(len(payload["previews"]), 3)
        self.assertIn("不会真实发送", payload["message"])

    def test_auto_apply_config_changes_restarts_needed_services(self) -> None:
        with patch.object(web, "run_service_action", return_value={"ok": True, "returncode": 0}) as service_action:
            with patch.object(web, "schedule_service_action", return_value={"ok": True, "scheduled": True}) as scheduled:
                result = web.auto_apply_config_changes(["STRUCTURE_MIN_SCORE", "WEB_PORT"])

        self.assertTrue(result["ok"])
        self.assertEqual(
            [call.args[0] for call in service_action.call_args_list],
            ["restart-main", "restart-structure"],
        )
        scheduled.assert_called_once_with("restart-web")
        self.assertIn("自动应用", result["message"])
        self.assertIn("impact", result)
        self.assertTrue(any(item["service"] == web.WEB_SERVICE for item in result["impact"]["service_actions"]))

    def test_auto_apply_ai_config_restarts_only_ai_service(self) -> None:
        with patch.object(web, "run_service_action", return_value={"ok": True, "returncode": 0}) as service_action:
            with patch.object(web, "schedule_service_action") as scheduled:
                result = web.auto_apply_config_changes(["AI_BOT_TOKEN", "AI_PROVIDER_ENABLE", "AI_ALLOWED_CHAT_IDS"])

        self.assertTrue(result["ok"])
        self.assertEqual([call.args[0] for call in service_action.call_args_list], ["restart-ai"])
        scheduled.assert_not_called()

    def test_config_change_impact_describes_modules_services_and_warnings(self) -> None:
        impact = web.config_change_impact(["WEB_PORT", "AI_API_KEY", "TG_CHAT_ID"])

        self.assertIn("Web 控制台", impact["modules"])
        self.assertIn("AI 助手", impact["modules"])
        self.assertTrue(any(item["service"] == web.WEB_SERVICE and item["scheduled"] for item in impact["service_actions"]))
        self.assertTrue(any(item["service"] == web.AI_SERVICE for item in impact["service_actions"]))
        self.assertTrue(any("敏感配置" in item for item in impact["warnings"]))
        self.assertTrue(any("Telegram" in item for item in impact["warnings"]))
        self.assertIn("备份", impact["rollback"])

    def test_config_impact_payload_validates_without_exposing_secret_values(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("WEB_PORT=8080\nAI_API_KEY=old-secret\n", encoding="utf-8")

            result = web.config_impact_payload(
                {"updates": {"WEB_PORT": "9090", "AI_API_KEY": "sk-secret-value-for-test"}, "clear": []},
                path=env_path,
            )

        text = json.dumps(result, ensure_ascii=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], ["AI_API_KEY", "WEB_PORT"])
        self.assertNotIn("sk-secret-value-for-test", text)
        self.assertTrue(any(item["secret"] for item in result["impact"]["changed_fields"]))

    def test_config_impact_payload_reports_validation_errors(self) -> None:
        with TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env.oi"
            env_path.write_text("WEB_PORT=8080\n", encoding="utf-8")

            result = web.config_impact_payload({"updates": {"WEB_PORT": "99999"}, "clear": []}, path=env_path)

        self.assertFalse(result["ok"])
        self.assertIn("WEB_PORT", result["errors"])
        self.assertEqual(result["changed"], [])

    def test_auto_apply_signal_event_config_restarts_writers_and_ai(self) -> None:
        with patch.object(web, "run_service_action", return_value={"ok": True, "returncode": 0}) as service_action:
            result = web.auto_apply_config_changes(["SIGNAL_EVENTS_LIMIT"])

        self.assertTrue(result["ok"])
        self.assertEqual(
            [call.args[0] for call in service_action.call_args_list],
            ["restart-main", "restart-structure", "restart-ai"],
        )

    def test_non_loopback_web_requires_token(self) -> None:
        with patch.object(web, "load_env_file", return_value={}):
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
        self.assertIn("当前值会完整显示", html)
        self.assertIn("自动话题：", html)
        self.assertIn("当前 ID 来自自动创建的话题路由文件", html)
        self.assertIn("对应复盘建议里的 STRUCTURE_MIN_SCORE", html)
        self.assertIn("对应复盘建议里的 STRUCTURE_SEND_CHART_TOP_N", html)
        self.assertIn("配置已保存，后台服务正在自动应用", html)
        self.assertIn("配置改动预览", html)
        self.assertIn("保存影响预检", html)
        self.assertIn("/api/config-impact", html)
        self.assertIn("fetchConfigImpact", html)
        self.assertIn("影响模块", html)
        self.assertIn("自动应用", html)
        self.assertIn("回滚方式", html)
        self.assertIn("配置现在按功能模块分开管理", html)
        self.assertIn("page-intro", html)
        self.assertIn("renderPageIntro", html)
        self.assertIn("emptyState", html)
        self.assertIn("tableEmpty", html)
        self.assertIn("运维总览", html)
        self.assertIn("AI 助手运行中心", html)
        self.assertIn("按功能模块管理配置", html)
        self.assertIn("没有匹配的审计记录", html)
        self.assertIn("还没有匹配的监控提醒", html)
        self.assertIn("禁止任意命令", html)
        self.assertIn("保存后自动重启", html)
        self.assertIn("fieldExplainHtml", html)
        self.assertIn("configFieldAllowed", html)
        self.assertIn("做什么", html)
        self.assertIn("影响什么", html)
        self.assertIn("改完是否自动重启", html)
        self.assertIn("AI Bot", html)
        self.assertIn("价格提醒", html)
        self.assertIn("主雷达参数", html)
        self.assertIn("行情源 / 外部接口", html)
        self.assertIn("保存后怎么生效", html)
        self.assertIn("excludeKeys", html)
        self.assertIn("price-alerts", html)
        self.assertIn("配置中心", html)
        self.assertIn("日志中心", html)
        self.assertIn("审计记录", html)
        self.assertIn("诊断报告", html)
        self.assertIn("雷达服务", html)
        self.assertIn("Telegram 推送", html)
        self.assertIn("资金费率警报", html)
        self.assertIn("返回配置首页", html)
        self.assertIn("configSaveToolbar", html)
        self.assertIn("配置备份和恢复", html)
        self.assertIn("删除备份", html)
        self.assertIn("应用这些建议并保存", html)
        self.assertIn("运行健康度", html)
        self.assertIn("最近错误", html)
        self.assertIn("更新备份", html)
        self.assertIn('data-ui-version="v1.60.0"', html)
        self.assertIn("platformStrip", html)
        self.assertIn("platform-pill", html)
        self.assertIn("Crypto Radar Ops", html)
        self.assertIn("versionBadge", html)
        self.assertIn("loadVersionBadge", html)
        self.assertIn("autoRefreshIntervalsMs", html)
        self.assertIn("refreshInFlight", html)
        self.assertIn("brand-subtitle", html)
        self.assertIn("sidebar-section", html)
        self.assertIn("nav-dot", html)
        self.assertIn("nav-text", html)
        self.assertIn("breadcrumbView", html)
        self.assertIn("topbar-actions", html)
        self.assertIn('data-view="server"', html)
        self.assertIn('id="serverGrid"', html)
        self.assertIn("/api/server-status", html)
        self.assertIn("/api/version", html)
        self.assertIn("serverMetricHistory", html)
        self.assertIn("meter-dial", html)
        self.assertIn("meter-needle", html)
        self.assertIn("serverRefreshHint", html)
        self.assertIn("怎么看这些数据", html)
        self.assertIn("sparkline", html)
        self.assertIn("--sidebar", html)
        self.assertIn("--topbar", html)
        self.assertIn('data-view="price"', html)
        self.assertIn('data-view="signals"', html)
        self.assertIn('id="signalsGrid"', html)
        self.assertIn("/api/signals", html)
        self.assertIn("/api/signals/latest", html)
        self.assertIn("/api/signals/stats", html)
        self.assertIn("/api/symbol-timeline", html)
        self.assertIn("/api/signals/detail", html)
        self.assertIn("signals.db", html)
        self.assertIn('data-view="audit"', html)
        self.assertIn('data-view="report"', html)
        self.assertIn("/api/audit", html)
        self.assertIn("/api/ops-snapshot", html)
        self.assertIn("/api/problem-state", html)
        self.assertIn("审计记录是 Web 后台的操作账本", html)
        self.assertIn("不保存 Token、API Key 或提示词正文", html)
        self.assertIn("renderAuditRows", html)
        self.assertIn("一键诊断报告用于排查问题", html)
        self.assertIn("copyReport", html)
        self.assertIn("reportText", html)
        self.assertIn("copyTextToClipboard", html)
        self.assertIn("document.execCommand(\"copy\")", html)
        self.assertIn("reportCopyStatus", html)
        self.assertIn("问题中心", html)
        self.assertIn("问题中心总览", html)
        self.assertIn("problemCenterPanel", html)
        self.assertIn("problemCenterStatusPill", html)
        self.assertIn("problemStateControls", html)
        self.assertIn("problemStateRecentRows", html)
        self.assertIn("problemReviewStatusPill", html)
        self.assertIn("markProblemState", html)
        self.assertIn("问题编号", html)
        self.assertIn("最近处理记录", html)
        self.assertIn("自动复查", html)
        self.assertIn("已消失待复查", html)
        self.assertIn("处理复查", html)
        self.assertIn("长期运行就绪度", html)
        self.assertIn("releaseReadinessPanel", html)
        self.assertIn("releaseReadinessStatusPill", html)
        self.assertIn("v1.50.0 收口路线", html)
        self.assertIn("功能冻结收口", html)
        self.assertIn("不新增大模块", html)
        self.assertIn("收口阶段", html)
        self.assertIn("发布后维护规则", html)
        self.assertIn("v2 规划", html)
        self.assertIn("closurePlanRows", html)
        self.assertIn("closure_current_stage", html)
        self.assertIn("服务器部署验收", html)
        self.assertIn("deploymentAcceptancePanel", html)
        self.assertIn("deployment_status", html)
        self.assertIn("Web 入口", html)
        self.assertIn("完整稳定版候选", html)
        self.assertIn("下一版本目标", html)
        self.assertIn("长期就绪度", html)
        self.assertIn("release_status", html)
        self.assertIn("release_score", html)
        self.assertIn("长期运行趋势", html)
        self.assertIn("releaseTrendPanel", html)
        self.assertIn("releaseTrendStatusPill", html)
        self.assertIn("趋势变好", html)
        self.assertIn("发生回退", html)
        self.assertIn("scoreText", html)
        self.assertIn("趋势回退", html)
        self.assertIn("处理清单", html)
        self.assertIn("actionPlanCards", html)
        self.assertIn("actionPlanButton", html)
        self.assertIn("执行稳定版验收", html)
        self.assertIn("stable-check", html)
        self.assertIn("v1 完整稳定版收口指引", html)
        self.assertIn("日常检查", html)
        self.assertIn("更新流程", html)
        self.assertIn("排错流程", html)
        self.assertIn("完整标准", html)
        self.assertIn("服务器执行 paopao update --yes", html)
        self.assertIn("issueCards", html)
        self.assertIn("issueSeverityPill", html)
        self.assertIn("查看失败审计", html)
        self.assertIn("按严重程度排序", html)
        self.assertIn("浏览器拒绝自动复制", html)
        self.assertIn("countTransientLogs", html)
        self.assertIn("网络超时", html)
        self.assertIn("Telegram 自动重试类", html)
        self.assertIn("价格提醒是独立的个人监控中心", html)
        self.assertIn("priceOutput", html)
        self.assertIn("priceStatusFilter", html)
        self.assertIn("priceTypeFilter", html)
        self.assertIn("priceSearch", html)
        self.assertIn("renderPriceAlertTable", html)
        self.assertIn("建议下一步：查看提醒列表里的状态和条件", html)
        self.assertIn("只看错误", html)
        self.assertIn("自动刷新：关闭", html)
        self.assertIn("toggleAutoRefresh", html)
        self.assertIn("自动刷新中", html)
        self.assertIn("日志筛选摘要", html)
        self.assertIn("搜索币种、错误或关键词", html)
        self.assertIn('<option value="ai">AI 助手</option>', html)
        self.assertIn('<option value="funding">资金费率</option>', html)
        self.assertIn("检查 GitHub 更新", html)
        self.assertIn("推送预览只展示格式", html)
        self.assertIn("AI 提示词", html)
        self.assertIn("saveAiPrompts", html)
        self.assertIn("专业分析师提示词", html)
        self.assertIn("价格提醒不再靠自然语言猜", html)
        self.assertIn("Web API 自诊断", html)
        self.assertIn("runWebSelfCheck", html)
        self.assertIn("apiErrorMessage", html)
        self.assertIn("apiMetaLine", html)
        self.assertIn("renderErrorPanel", html)
        self.assertIn("renderViewError", html)
        self.assertIn("apiOrError", html)
        self.assertIn("partialErrorPanels", html)
        self.assertIn("页面加载失败", html)
        self.assertIn("打开诊断报告", html)
        self.assertIn("打开日志中心", html)
        self.assertIn("部分信息读取失败，其余可用信息已显示", html)
        self.assertIn("浏览器耗时", html)
        self.assertIn("HTTP ${res.status}", html)
        self.assertIn("Web API 自诊断通过", html)
        self.assertIn('confirmWord: "SEND"', html)
        self.assertIn('confirmWord: "CLEANUP"', html)

    def test_web_api_meta_uses_path_status_and_request_id(self) -> None:
        handler = object.__new__(web.WebHandler)
        handler.path = "/api/summary?x=1"

        meta = web.WebHandler.api_meta(handler, 200)

        self.assertEqual(meta["path"], "/api/summary")
        self.assertEqual(meta["status"], 200)
        self.assertIn("served_at", meta)
        self.assertIn("request_id", meta)

    def test_signal_web_payloads_return_expected_structures(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
            )
            store = web.signal_store_for_settings(settings)
            store.append_from_push(
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:BTC",
                status="sent",
                sent=True,
                text="BTCUSDT\n分数: 88",
                ts=1000,
                topic_id="12",
                message_ids=[321],
            )

            listing = web.signals_payload(settings=settings, symbol="BTC", limit=10)
            latest = web.signals_latest_payload(settings=settings, after_id=0)
            stats = web.signals_stats_payload(settings=settings, window_sec=10**10)
            timeline = web.symbol_timeline_payload("BTC", settings=settings)
            detail = web.signal_detail_payload(listing["items"][0]["id"], settings=settings)

        self.assertTrue(listing["ok"])
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(listing["message"], "已读取信号推送记录")
        self.assertTrue(latest["ok"])
        self.assertEqual(latest["count"], 1)
        self.assertEqual(stats["by_status"]["sent"], 1)
        self.assertEqual(stats["top_symbols"][0]["symbol"], "BTCUSDT")
        self.assertEqual(timeline["symbol"], "BTCUSDT")
        self.assertEqual(detail["item"]["message_ids"], [321])

    def test_signal_payload_display_fields_and_q_search(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
            )
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="display:sent", status="sent", sent=True, text="BTCUSDT flow signal", ts=1000)
            append_from_push(settings, template_id="TG_TEST_MESSAGE", dedup_key="display:dry", status="dry_run", sent=False, text="无币种测试消息", ts=1001)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="display:failed", status="failed", sent=False, text="ETHUSDT failed delivery", ts=1002)

            sent_payload = web.signals_payload(settings=settings, status="sent", q="flow", limit=10)
            dry_payload = web.signals_payload(settings=settings, status="dry_run", limit=10)
            failed_payload = web.signals_payload(settings=settings, status="failed", limit=10)

        self.assertTrue(sent_payload["ok"])
        self.assertEqual(sent_payload["count"], 1)
        sent_display = sent_payload["items"][0]["display"]
        self.assertEqual(sent_display["card_tone"], "good")
        self.assertEqual(sent_display["status_label"], "已发送")
        self.assertIn("pagination", sent_payload)
        self.assertIn("filters", sent_payload)
        self.assertIn("sort", sent_payload)
        dry_display = dry_payload["items"][0]["display"]
        self.assertEqual(dry_display["status_label"], "Dry-run")
        self.assertEqual(dry_display["symbol_label"], "全局/无币种")
        self.assertEqual(dry_display["score_label"], "-")
        failed_display = failed_payload["items"][0]["display"]
        self.assertEqual(failed_display["card_tone"], "bad")
        self.assertEqual(failed_display["status_label"], "失败")

    def test_signal_detail_payload_exposes_detail_sections_and_related(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
            )
            for idx in range(12):
                append_from_push(
                    settings,
                    template_id="TG_FLOW_RADAR",
                    dedup_key=f"detail:btc:{idx}",
                    status="sent",
                    sent=True,
                    text=f"BTCUSDT detail event {idx}",
                    ts=1000 + idx,
                    topic_id="42",
                    message_ids=[idx],
                )
            listing = web.signals_payload(settings=settings, symbol="BTC", limit=1)
            signal_id = int(listing["items"][0]["id"])
            with web.signal_store_for_settings(settings).connect() as conn:
                conn.execute("UPDATE signals SET payload_json = ? WHERE id = ?", ("{bad json", signal_id))
            detail = web.signal_detail_payload(signal_id, settings=settings)
            missing = web.signal_detail_payload(999999, settings=settings)

        self.assertTrue(detail["ok"])
        self.assertIn("detail", detail)
        view = detail["detail"]
        self.assertIn("header", view)
        self.assertIn("sections", view)
        self.assertIn("raw", view)
        self.assertIn("related", view)
        self.assertLessEqual(len(view["related"]["same_symbol"]), 10)
        self.assertIn("payload_json", view["raw"])
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "not_found")

    def test_signal_stats_payload_exposes_display_fields(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
            )
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="stats:sent", status="sent", sent=True, text="BTCUSDT", ts=1000)
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="stats:failed", status="failed", sent=False, text="ETHUSDT", ts=1001)
            stats = web.signals_stats_payload(settings=settings, window_sec=10**10)

        self.assertEqual(stats["by_status"]["sent"], 1)
        self.assertIn("by_module_display", stats)
        self.assertIn("by_status_display", stats)
        self.assertIn("top_symbols_display", stats)
        self.assertEqual(stats["latest_sent"][0]["display"]["status_label"], "已发送")
        self.assertEqual(stats["latest_failed"][0]["display"]["card_tone"], "bad")

    def test_dashboard_payload_latest_signals_include_display(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
                web_jobs_db_path=Path(tmp) / "jobs.db",
            )
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="dashboard:btc", status="sent", sent=True, text="BTCUSDT dashboard", ts=int(time.time()))
            payload = dashboard_payload(settings=settings)

        self.assertTrue(payload["ok"])
        latest = payload["data"]["signals"]["latest"]
        self.assertTrue(latest)
        self.assertIn("display", latest[0])
        self.assertIn("top_symbols_display", payload["data"]["signals"])
        self.assertIn("coins", payload["data"])
        self.assertEqual(payload["data"]["coins"]["top_active"][0]["symbol"], "BTCUSDT")

    def test_coin_detail_payloads_and_frontend_contract_are_present(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
            )
            append_from_push(settings, template_id="TG_FLOW_RADAR", dedup_key="coin-web:btc", status="sent", sent=True, text="BTCUSDT coin detail", ts=int(time.time()))
            detail = coin_detail_payload("BTC", settings=settings, window_sec=10**10)
            missing = coin_detail_payload("", settings=settings)
            search = coin_search_payload("btc", settings=settings, window_sec=10**10)
            timeline = coin_timeline_payload("BTCUSDT", settings=settings)

        self.assertTrue(detail["ok"])
        self.assertEqual(detail["symbol"], "BTCUSDT")
        self.assertIn("summary", detail)
        self.assertIn("module_counts", detail)
        self.assertIn("status_counts", detail)
        self.assertIn("timeline", detail)
        self.assertIn("telegram", detail)
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["code"], "bad_request")
        self.assertTrue(search["ok"])
        self.assertEqual(search["items"][0]["symbol"], "BTCUSDT")
        self.assertTrue(timeline["ok"])
        self.assertEqual(timeline["symbol"], "BTCUSDT")

        html = web.INDEX_HTML
        self.assertIn('data-view="coin"', html)
        self.assertIn('id="coinGrid"', html)
        self.assertIn("/api/coin-detail", html)
        self.assertIn("/api/coin-search", html)
        self.assertIn("openCoinDetail", html)
        self.assertIn("Coin Detail", html)

    def test_send_json_wraps_dict_payload_with_api_meta(self) -> None:
        handler = object.__new__(web.WebHandler)
        handler.path = "/api/test?x=1"
        handler.wfile = BytesIO()
        statuses: list[int] = []
        headers: list[tuple[str, str]] = []
        handler.send_response = lambda status: statuses.append(status)
        handler.send_header = lambda key, value: headers.append((key, value))
        handler.end_headers = lambda: None

        web.WebHandler.send_json(handler, {"ok": True, "message": "ok"}, 201)

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(statuses, [201])
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["message"], "ok")
        self.assertEqual(payload["_meta"]["path"], "/api/test")
        self.assertEqual(payload["_meta"]["status"], 201)
        self.assertIn("served_at", payload["_meta"])
        self.assertIn("request_id", payload["_meta"])
        self.assertIn(("Cache-Control", "no-store"), headers)
        self.assertTrue(any(key == "Content-Type" and "application/json" in value for key, value in headers))

    def test_send_error_json_uses_stable_error_contract(self) -> None:
        handler = object.__new__(web.WebHandler)
        handler.path = "/api/missing"
        handler.wfile = BytesIO()
        statuses: list[int] = []
        headers: list[tuple[str, str]] = []
        handler.send_response = lambda status: statuses.append(status)
        handler.send_header = lambda key, value: headers.append((key, value))
        handler.end_headers = lambda: None

        web.WebHandler.send_error_json(handler, "接口不存在", 404, "not_found")

        payload = json.loads(handler.wfile.getvalue().decode("utf-8"))
        self.assertEqual(statuses, [404])
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["error"], "接口不存在")
        self.assertEqual(payload["message"], "接口不存在")
        self.assertEqual(payload["code"], "not_found")
        self.assertEqual(payload["_meta"]["path"], "/api/missing")
        self.assertEqual(payload["_meta"]["status"], 404)
        self.assertIn(("Cache-Control", "no-store"), headers)

    def test_send_html_ignores_broken_pipe_client_disconnect(self) -> None:
        class BrokenWriter:
            def write(self, payload: bytes) -> None:
                raise BrokenPipeError("client closed")

        handler = object.__new__(web.WebHandler)
        handler.path = "/"
        handler.wfile = BrokenWriter()
        handler.send_response = lambda status: None
        handler.send_header = lambda key, value: None
        handler.end_headers = lambda: None

        stderr = StringIO()
        with patch("paopao_radar.web.sys.stderr", stderr):
            web.WebHandler.send_html(handler, "<html></html>")

        self.assertEqual(stderr.getvalue(), "[web] client disconnected during response\n")
        self.assertNotIn("Traceback", stderr.getvalue())
        self.assertFalse(web.is_error_log_line(stderr.getvalue()))

    def test_send_json_ignores_connection_reset_client_disconnect(self) -> None:
        class ResetWriter:
            def write(self, payload: bytes) -> None:
                raise ConnectionResetError("client reset")

        handler = object.__new__(web.WebHandler)
        handler.path = "/api/test"
        handler.wfile = ResetWriter()
        handler.send_response = lambda status: None
        handler.send_header = lambda key, value: None
        handler.end_headers = lambda: None

        stderr = StringIO()
        with patch("paopao_radar.web.sys.stderr", stderr):
            web.WebHandler.send_json(handler, {"ok": True})

        self.assertEqual(stderr.getvalue(), "[web] client disconnected during response\n")
        self.assertNotIn("Traceback", stderr.getvalue())

    def test_overview_uses_readable_summaries_and_collapsed_raw_data(self) -> None:
        html = web.INDEX_HTML

        self.assertIn("主服务运行摘要", html)
        self.assertIn("结构雷达运行摘要", html)
        self.assertIn("Telegram 配置", html)
        self.assertIn("结构雷达参数", html)
        self.assertIn("结构图数量", html)
        self.assertIn("高级排查：原始运行状态 JSON", html)
        self.assertIn("单人管理员入口", html)
        self.assertIn("所有运维权限都集中给你本人使用", html)
        self.assertIn("查看相关日志", html)
        self.assertIn("openLogsForError", html)
        self.assertIn('active: "运行中"', html)
        self.assertNotIn("systemd 是否 active", html)
        self.assertNotIn("systemd", html)

    def test_service_page_explains_controls(self) -> None:
        html = web.INDEX_HTML

        self.assertIn("这个页面是控制后台服务开关的，不是普通测试按钮", html)
        self.assertIn("建议优先使用“重启”", html)
        self.assertIn("会暂停对应功能。点击后需要输入 STOP 二次确认", html)
        self.assertIn("改完 .env.oi、推送配置、扫描参数后通常点这个", html)
        self.assertIn("四个不同的后台服务", html)
        self.assertIn('${neutralPill("系统服务")}', html)
        self.assertIn("${escapeHtml(action.button)}</button>", html)
        self.assertIn("renderOperationResult(\"serviceOutput\"", html)
        self.assertIn("await refreshCurrent();", html)
        self.assertIn("高级详情：原始执行结果 JSON", html)
        self.assertIn("建议下一步：回到总览确认服务状态", html)
        self.assertNotIn("serviceList.map", html)

    def test_web_explains_external_api_sources(self) -> None:
        html = web.INDEX_HTML

        self.assertIn("外部接口和 API Key 说明", html)
        self.assertIn("Binance 免费公开数据", html)
        self.assertIn("CoinPaprika 免费市值数据", html)
        self.assertIn("不用填写 API Key", html)
        self.assertIn("本项目没有用 Coinalyze 获取市值", html)
        self.assertIn("CoinMarketCap API", html)
        self.assertIn("预留，未接入", html)
        self.assertIn("当前源码没有接入", html)
        self.assertIn('brand: "telegram"', html)
        self.assertIn('brand: "binance"', html)
        self.assertIn('brand: "coinpaprika"', html)
        self.assertIn('brand: "coinalyze"', html)
        self.assertIn('brand: "coinmarketcap"', html)
        self.assertIn("https://www.google.com/s2/favicons?domain=telegram.org&sz=64", html)
        self.assertIn("https://www.google.com/s2/favicons?domain=binance.com&sz=64", html)
        self.assertIn("https://www.google.com/s2/favicons?domain=coinpaprika.com&sz=64", html)
        self.assertIn("https://www.google.com/s2/favicons?domain=coinalyze.net&sz=64", html)
        self.assertIn("https://www.google.com/s2/favicons?domain=coinmarketcap.com&sz=64", html)
        self.assertIn("apiLogo(source.brand, source.name, source.logoUrl)", html)
        self.assertIn("<img src=\"${safeUrl}\"", html)

    def test_ai_prompts_test_requires_provider(self) -> None:
        with patch.object(web.Settings, "load", return_value=web.Settings(ai_provider_enable=False, ai_api_key="")):
            result = web.ai_prompts_test_payload({"mode": "analyst", "text": "BTC"})

        self.assertFalse(result["ok"])
        self.assertIn("AI 问答接口未启用", result["error"])

    def test_ai_prompts_test_uses_current_editor_prompt(self) -> None:
        settings = web.Settings(
            ai_provider_enable=True,
            ai_api_key="sk-test",
            ai_base_url="https://api.example.com",
            ai_model="deepseek-v4-pro",
        )
        response = Mock()
        response.json.return_value = {"choices": [{"message": {"content": "测试回复"}}]}

        with patch.object(web.Settings, "load", return_value=settings):
            with patch("paopao_radar.ai_assistant.requests.post", return_value=response) as post:
                result = web.ai_prompts_test_payload(
                    {
                        "mode": "analyst",
                        "text": "BTC 数据",
                        "assistant_prompt": "普通助手",
                        "analyst_prompt": "专业分析师",
                    }
                )

        self.assertTrue(result["ok"])
        self.assertEqual(result["reply"], "测试回复")
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["model"], "deepseek-v4-pro")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "high")
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["messages"][0]["content"], "专业分析师")
        self.assertEqual(payload["messages"][1]["content"], "BTC 数据")

    def test_cli_web_command_starts_web_without_runtime_init(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            with patch.object(cli, "make_runtime", side_effect=AssertionError("should not init runtime")):
                with patch("paopao_radar.web.run_web_server", return_value=0) as run_web:
                    code = cli.main(["web", "--host", "127.0.0.1", "--port", "8090", "--web-token", "secret"])

        self.assertEqual(code, 0)
        run_web.assert_called_once_with("127.0.0.1", 8090, "secret")

    def test_jobs_payloads_report_and_update_status_use_temp_db(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), web_jobs_db_path=Path(tmp) / "jobs.db")
            store = jobs.JobStore(settings.web_jobs_db_path)
            update = store.create_job("update-check")
            store.finish_job(
                int(update["id"]),
                status="success",
                returncode=0,
                stdout_tail=(
                    "当前版本 : v1.61.0 (6b9d485) feat\n"
                    "GitHub版本: v1.62.0 (abc1234) feat\n"
                    "发现新版本，可以更新\n"
                ),
            )
            failed = store.create_job("doctor")
            store.finish_job(int(failed["id"]), status="failed", returncode=1, stderr_tail="Traceback: bad")

            stats = web.jobs_stats_payload(settings=settings)
            report = web.job_report_payload(int(failed["id"]), settings=settings)
            update_status = web.update_check_status_payload(settings=settings)

        self.assertTrue(stats["ok"])
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["success"], 1)
        self.assertTrue(report["ok"])
        self.assertIn("doctor", report["report"]["text"])
        self.assertEqual(update_status["current_version"], "v1.61.0")
        self.assertEqual(update_status["parsed"]["remote_version"], "v1.62.0")
        self.assertIs(update_status["update_available"], True)

    def test_failed_jobs_enter_ops_issues_and_problem_center(self) -> None:
        snapshot = {
            "health": [],
            "recent_errors": [],
            "audit": {"failed_recent": []},
            "log_errors": {},
            "jobs": {
                "recent_failed": [
                    {
                        "id": 7,
                        "job_type": "stable-check",
                        "status": "failed",
                        "error_summary": "stable failed",
                    }
                ]
            },
            "stability": {"status": "ready", "checks": []},
            "release_trend": {"status": "stable"},
        }

        issues = web.build_ops_issues(snapshot)
        snapshot["issues"] = issues
        center = web.build_problem_center(snapshot)
        recommendations = web.build_ops_recommendations(snapshot)

        self.assertTrue(any(item["target"] == "jobs" for item in issues))
        self.assertTrue(any(item["severity"] == "critical" for item in issues))
        self.assertTrue(any(item["key"] == "failed-jobs" for item in center["action_plan"]))
        self.assertGreaterEqual(center["counts"]["failed_jobs"], 1)
        self.assertTrue(any("任务中心" in item for item in recommendations))

    def test_jobs_frontend_and_api_routes_are_present(self) -> None:
        html = web.INDEX_HTML

        self.assertIn("/api/dashboard", html)
        self.assertIn("/api/jobs/stats", html)
        self.assertIn("/api/jobs/report", html)
        self.assertIn("/api/jobs/rerun", html)
        self.assertIn("/api/jobs/cleanup", html)
        self.assertIn("/api/update-status", html)
        self.assertIn("复制任务报告", html)
        self.assertIn("清理旧任务", html)
        self.assertIn('attention: ["warning", "关注"]', html)
        self.assertIn('["attention", "关注"]', html)
        self.assertIn("last_attention_by_type", html)
        self.assertIn("signal-card", html)
        self.assertIn("signal-detail-panel", html)
        self.assertIn("signalSearchFilter", html)
        self.assertIn("applySignalFilter", html)

    def test_signals_payload_exposes_api_core_metadata_and_symbol_normalization(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
            )
            append_from_push(
                settings,
                template_id="TG_FLOW_RADAR",
                dedup_key="api-core:btc",
                status="sent",
                sent=True,
                text="BTCUSDT",
                ts=1000,
            )
            payload = web.signals_payload(
                limit=10,
                symbol="BTC",
                status="sent",
                sort_field="id",
                sort_direction="asc",
                pagination={"limit": 10, "cursor": None},
                filters={"symbol": "BTCUSDT", "status": "sent"},
                sort={"field": "id", "direction": "asc", "raw": "id"},
                settings=settings,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["symbol"], "BTCUSDT")
        self.assertEqual(payload["filters"]["symbol"], "BTCUSDT")
        self.assertEqual(payload["sort"]["raw"], "id")
        self.assertIn("pagination", payload)

    def test_jobs_payload_keeps_attention_status_and_api_core_metadata(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), web_jobs_db_path=Path(tmp) / "jobs.db")
            store = jobs.JobStore(settings.web_jobs_db_path)
            job = store.create_job("stable-check")
            store.finish_job(int(job["id"]), status="failed", returncode=1, stdout_tail="attention")
            payload = jobs.jobs_payload(
                limit=10,
                status="attention",
                pagination={"limit": 10},
                filters={"status": "attention"},
                sort={"field": "id", "direction": "desc", "raw": "-id"},
                settings=settings,
            )

        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["jobs"][0]["status"], "attention")
        self.assertEqual(payload["filters"]["status"], "attention")

    def test_dashboard_payload_has_platform_contract_without_secrets(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
                web_jobs_db_path=Path(tmp) / "jobs.db",
            )
            payload = dashboard_payload(settings=settings)
            text = json.dumps(payload, ensure_ascii=False)

        self.assertTrue(payload["ok"])
        data = payload["data"]
        self.assertIn("version", data)
        self.assertIn("services", data)
        self.assertIn("signals", data)
        self.assertIn("jobs", data)
        self.assertIn("resources", data)
        self.assertNotIn("TG_BOT_TOKEN", text)
        self.assertNotIn("AI_API_KEY", text)

    def test_api_contract_self_test_returns_required_checks(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                signal_events_db_path=Path(tmp) / "signals.db",
                web_jobs_db_path=Path(tmp) / "jobs.db",
            )
            payload = api_contract_self_test(settings=settings)

        self.assertTrue(payload["ok"])
        names = {item["name"] for item in payload["checks"]}
        self.assertIn("dashboard", names)
        self.assertIn("signals", names)
        self.assertIn("jobs", names)
        self.assertIn("update-status", names)
        self.assertIn("coin-search", names)
        self.assertIn("coin-detail", names)
        self.assertIn("coin-timeline", names)

    def test_jobs_audit_summary_stays_minimal(self) -> None:
        rerun = web.audit_request_summary("/api/jobs/rerun", {"id": 12, "stdout_tail": "secret"})
        cleanup = web.audit_request_summary("/api/jobs/cleanup", {"retention_days": 30, "limit": 500, "stderr_tail": "secret"})

        self.assertEqual(rerun["details"], {"job_id": 12})
        self.assertEqual(cleanup["details"], {"retention_days": 30, "limit": 500})
        self.assertNotIn("stdout_tail", rerun["details"])
        self.assertNotIn("stderr_tail", cleanup["details"])


if __name__ == "__main__":
    unittest.main()
