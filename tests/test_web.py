from __future__ import annotations

import os
import json
import unittest
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import paopao_radar.cli as cli
from paopao_radar import web


class WebConsoleTests(unittest.TestCase):
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

        with patch.object(web, "summary_payload", return_value=summary):
            with patch.object(web, "web_audit_payload", return_value={"records": [], "total": 0, "matched": 0}):
                with patch.object(web, "logs_payload", side_effect=fake_logs):
                    payload = web.ops_snapshot_payload()

        payload_text = json.dumps(payload, ensure_ascii=False)
        self.assertTrue(payload["ok"])
        self.assertIn("log_errors", payload)
        self.assertIn("recommendations", payload)
        self.assertNotIn("123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi", payload_text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz", payload_text)
        self.assertIn("<redacted", payload_text)

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
        self.assertIn('data-view="price"', html)
        self.assertIn('data-view="audit"', html)
        self.assertIn('data-view="report"', html)
        self.assertIn("/api/audit", html)
        self.assertIn("/api/ops-snapshot", html)
        self.assertIn("审计记录是 Web 后台的操作账本", html)
        self.assertIn("不保存 Token、API Key 或提示词正文", html)
        self.assertIn("renderAuditRows", html)
        self.assertIn("一键诊断报告用于排查问题", html)
        self.assertIn("copyReport", html)
        self.assertIn("reportText", html)
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


if __name__ == "__main__":
    unittest.main()
