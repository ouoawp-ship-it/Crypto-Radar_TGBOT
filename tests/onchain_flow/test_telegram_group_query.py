from __future__ import annotations

from dataclasses import replace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import PushResult, TelegramGateway
from paopao_radar.onchain_flow.config import (
    OAR_TELEGRAM_QUERY_ACK,
    OnchainSettings,
)
from paopao_radar.onchain_flow.automation_store import AutomationStoreError
from paopao_radar.onchain_flow.telegram_query import (
    TelegramQueryError,
    TelegramQueryHttpClient,
    TelegramQueryService,
    TelegramQueryState,
    parse_telegram_query,
)
from scripts.paopao_config import ConfigManager


CONTRACT = "0xcbD06E5A2B0C65597161de254AA074E489dEb510"


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def send(self, text: str, template_id: str, dedup_key: str, **kwargs):
        self.calls.append({
            "text": text,
            "template_id": template_id,
            "dedup_key": dedup_key,
            **kwargs,
        })
        return PushResult("sent", "telegram_api", True, [1001])


class FakeAutomationStore:
    def __init__(self, status: str = "resolved") -> None:
        self.status = status
        self.calls: list[str] = []

    def resolve_registry(self, symbol: str) -> dict[str, object]:
        self.calls.append(symbol)
        if self.status == "resolved" and symbol == "CBDOGEUSDT":
            return {
                "status": "resolved",
                "token": {"contract_address": CONTRACT},
            }
        return {"status": self.status, "token": None}


class FailingAutomationStore:
    def resolve_registry(self, _symbol: str) -> dict[str, object]:
        raise AutomationStoreError("database_unavailable", "private detail")


class RejectRegistryAccess:
    def resolve_registry(self, _symbol: str) -> dict[str, object]:
        raise AssertionError("a full contract must not consult symbol registry")


class FakeReportService:
    def execute(self, _query, *, with_ai: bool) -> dict[str, object]:
        if with_ai:
            raise AssertionError("group queries must not enable AI")
        return {
            "complete": True,
            "report": {
                "rule_summary": {
                    "token": {
                        "symbol": "cbDOGE",
                        "contract": CONTRACT.lower(),
                    },
                    "query": {"window": "15m", "complete": True},
                    "transfer_summary": {
                        "transfer_count": 2,
                        "total_token_amount": "10",
                        "unique_senders": 2,
                        "unique_receivers": 2,
                    },
                    "cex_flows": {
                        "gross_inflow_token": "0",
                        "gross_outflow_token": "0",
                        "net_flow_token": "0",
                    },
                    "primary_behavior": {
                        "label": "证据不足",
                        "score": 0,
                        "confidence_level": "low",
                    },
                    "wallet_groups": [],
                    "representative_transfers": [],
                },
                "ai": {"status": "not_requested", "result": None},
            },
        }


class FakeResponse:
    def __init__(self, status_code: int, body: object):
        self.status_code = status_code
        self._body = body

    def json(self):
        return self._body


class FakeHttp:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = list(responses)
        self.requests: list[dict[str, object]] = []

    def post(self, url: str, **kwargs):
        self.requests.append({"url": url, **kwargs})
        return self.responses.pop(0)


class FakeApi:
    def __init__(self, responses: dict[str, object]):
        self.responses = responses
        self.calls: list[str] = []
        self.http_calls = 0

    def call(self, method: str, _payload: dict[str, object], **_kwargs):
        self.calls.append(method)
        self.http_calls += 1
        return self.responses[method]


class TelegramGroupQueryTests(unittest.TestCase):
    def test_parses_explicit_mention_contract_and_window(self) -> None:
        parsed = parse_telegram_query(
            f"@paopao_bot 查询 {CONTRACT} 1h", "paopao_bot"
        )
        self.assertTrue(parsed.invoked)
        self.assertEqual(parsed.target, CONTRACT.lower())
        self.assertEqual(parsed.window, "1h")

    def test_parses_targeted_command_and_registry_symbol(self) -> None:
        parsed = parse_telegram_query(
            "/oar@paopao_bot CBDOGE 4h", "paopao_bot"
        )
        self.assertTrue(parsed.invoked)
        self.assertEqual(parsed.target, "CBDOGE")
        self.assertEqual(parsed.window, "4h")

    def test_parses_visible_chinese_punctuation_after_mention(self) -> None:
        parsed = parse_telegram_query(
            "@paopao_bot，查询 CBDOGE 15m", "paopao_bot"
        )
        self.assertTrue(parsed.invoked)
        self.assertEqual(parsed.target, "CBDOGE")

    def test_ignores_other_bot_and_unmentioned_text(self) -> None:
        self.assertFalse(
            parse_telegram_query("@other_bot CBDOGE", "paopao_bot").invoked
        )
        self.assertFalse(
            parse_telegram_query("查询 CBDOGE", "paopao_bot").invoked
        )

    def test_rejects_24h_and_multiple_contracts(self) -> None:
        parsed = parse_telegram_query(
            "@paopao_bot 查询 CBDOGE 24h", "paopao_bot"
        )
        self.assertEqual(parsed.error, "query_window_not_allowed")
        parsed = parse_telegram_query(
            f"@paopao_bot {CONTRACT} 0x{'1' * 40}", "paopao_bot"
        )
        self.assertEqual(parsed.error, "query_contract_ambiguous")

    def test_state_hashes_user_and_enforces_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = [1000.0]
            state = TelegramQueryState(
                root / "state.json", root, clock=lambda: now[0]
            )
            self.assertEqual(
                state.allow_query(12345, cooldown_sec=60, max_per_hour=5),
                (True, ""),
            )
            self.assertEqual(
                state.allow_query(12345, cooldown_sec=60, max_per_hour=5),
                (False, "query_user_cooldown"),
            )
            content = (root / "state.json").read_text(encoding="utf-8")
            self.assertNotIn("12345", content)

    def test_http_client_classifies_polling_conflict_without_body(self) -> None:
        http = FakeHttp([
            FakeResponse(409, {
                "ok": False,
                "description": "Conflict: another getUpdates request",
            })
        ])
        client = TelegramQueryHttpClient("123:fake", http_client=http)
        with self.assertRaises(TelegramQueryError) as caught:
            client.call("getUpdates", {})
        self.assertEqual(caught.exception.reason, "telegram_polling_conflict")
        self.assertNotIn("another getUpdates", str(caught.exception))

    def test_disabled_gate_performs_no_network_or_filesystem_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data" / "onchain"
            settings = OnchainSettings.load(base_dir=root, environ={})
            api = FakeApi({})
            service = TelegramQueryService(settings, api=api)
            with self.assertRaises(TelegramQueryError) as caught:
                service.validate_gate(
                    allow_network=True,
                    send=True,
                    confirm_real_send=True,
                )
            self.assertEqual(
                caught.exception.reason,
                "telegram_query_configuration_blocked",
            )
            self.assertEqual(api.calls, [])
            self.assertFalse(data_dir.exists())

    def test_startup_requires_admin_for_reliable_mentions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                OnchainSettings(),
                base_dir=root,
                data_dir=root / "onchain",
                oar_telegram_query_state_path=root / "onchain" / "state.json",
                tg_chat_id="-100123",
                tg_bot_token="123:fake",
            )
            api = FakeApi({
                "getWebhookInfo": {"url": ""},
                "getMe": {"id": 7, "username": "paopao_bot"},
                "getChat": {"type": "supergroup", "is_forum": True},
                "getChatMember": {"status": "member"},
            })
            service = TelegramQueryService(settings, api=api)
            with self.assertRaises(TelegramQueryError) as caught:
                service._startup()
            self.assertEqual(
                caught.exception.reason,
                "telegram_query_admin_required",
            )
            self.assertEqual(
                api.calls,
                ["getWebhookInfo", "getMe", "getChat", "getChatMember"],
            )

    def test_startup_rejects_non_forum_chat(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                OnchainSettings(),
                base_dir=root,
                data_dir=root / "onchain",
                oar_telegram_query_state_path=root / "onchain" / "state.json",
                tg_chat_id="-100123",
                tg_bot_token="123:fake",
            )
            api = FakeApi({
                "getWebhookInfo": {"url": ""},
                "getMe": {"id": 7, "username": "paopao_bot"},
                "getChat": {"type": "supergroup", "is_forum": False},
            })
            service = TelegramQueryService(settings, api=api)
            with self.assertRaises(TelegramQueryError) as caught:
                service._startup()
            self.assertEqual(
                caught.exception.reason,
                "telegram_query_forum_required",
            )

    def test_process_update_runs_rule_report_without_ai(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                OnchainSettings(),
                base_dir=root,
                data_dir=root / "onchain",
                oar_telegram_query_state_path=root / "onchain" / "state.json",
                tg_chat_id="-100123",
                tg_onchain_flow_topic_id="42",
                oar_telegram_query_cooldown_sec=60,
                oar_telegram_query_max_per_hour=12,
            )
            gateway = FakeGateway()
            automation_store = FakeAutomationStore()
            service = TelegramQueryService(
                settings,
                gateway=gateway,
                automation_store=automation_store,
                report_factory=lambda _settings, _query: FakeReportService(),
                clock=lambda: 2000.0,
            )
            service.bot_username = "paopao_bot"
            outcome = service.process_update({
                "update_id": 9,
                "message": {
                    "message_id": 77,
                    "message_thread_id": 42,
                    "date": 2000,
                    "text": "@paopao_bot 查询 CBDOGE 15m",
                    "chat": {"id": -100123},
                    "from": {"id": 12345, "is_bot": False},
                },
            })
            self.assertEqual(outcome, "query_completed")
            self.assertEqual(automation_store.calls, ["CBDOGEUSDT"])
            self.assertEqual(len(gateway.calls), 1)
            call = gateway.calls[0]
            self.assertEqual(call["template_id"], "TG_ONCHAIN_QUERY")
            self.assertTrue(call["send"])
            self.assertTrue(call["confirm_real_send"])
            self.assertEqual(call["reply_to_message_id"], 77)
            self.assertIn("未调用 AI", str(call["text"]))

    def test_registry_error_returns_safe_reply_without_crashing_worker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                OnchainSettings(),
                base_dir=root,
                data_dir=root / "onchain",
                oar_telegram_query_state_path=root / "onchain" / "state.json",
                tg_chat_id="-100123",
                tg_onchain_flow_topic_id="42",
            )
            gateway = FakeGateway()
            service = TelegramQueryService(
                settings,
                gateway=gateway,
                automation_store=FailingAutomationStore(),
                clock=lambda: 2000.0,
            )
            service.bot_username = "paopao_bot"
            outcome = service.process_update({
                "update_id": 91,
                "message": {
                    "message_id": 92,
                    "message_thread_id": 42,
                    "date": 2000,
                    "text": "@paopao_bot 查询 CBDOGE 15m",
                    "chat": {"id": -100123},
                    "from": {"id": 54321, "is_bot": False},
                },
            })
            self.assertEqual(outcome, "query_rejected")
            self.assertEqual(len(gateway.calls), 1)
            reply = str(gateway.calls[0]["text"])
            self.assertIn("Registry 暂时无法完成解析", reply)
            self.assertNotIn("private detail", reply)

    def test_full_contract_query_does_not_guess_or_consult_registry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                OnchainSettings(),
                base_dir=root,
                data_dir=root / "onchain",
                oar_telegram_query_state_path=root / "onchain" / "state.json",
                tg_chat_id="-100123",
                tg_onchain_flow_topic_id="42",
            )
            gateway = FakeGateway()
            service = TelegramQueryService(
                settings,
                gateway=gateway,
                automation_store=RejectRegistryAccess(),
                report_factory=lambda _settings, _query: FakeReportService(),
                clock=lambda: 2000.0,
            )
            service.bot_username = "paopao_bot"
            outcome = service.process_update({
                "update_id": 10,
                "message": {
                    "message_id": 78,
                    "message_thread_id": 42,
                    "date": 2000,
                    "text": f"@paopao_bot 查询 {CONTRACT} 15m",
                    "chat": {"id": -100123},
                    "from": {"id": 12346, "is_bot": False},
                },
            })
            self.assertEqual(outcome, "query_completed")
            self.assertEqual(len(gateway.calls), 1)

    def test_rate_limit_is_silent_after_first_invoked_reply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                OnchainSettings(),
                base_dir=root,
                data_dir=root / "onchain",
                oar_telegram_query_state_path=root / "onchain" / "state.json",
                tg_chat_id="-100123",
                tg_onchain_flow_topic_id="42",
                oar_telegram_query_cooldown_sec=60,
            )
            gateway = FakeGateway()
            service = TelegramQueryService(
                settings,
                gateway=gateway,
                automation_store=FakeAutomationStore(),
                clock=lambda: 2000.0,
            )
            service.bot_username = "paopao_bot"
            update = {
                "update_id": 11,
                "message": {
                    "message_id": 79,
                    "message_thread_id": 42,
                    "date": 2000,
                    "text": "@paopao_bot 查询 UNKNOWN 15m",
                    "chat": {"id": -100123},
                    "from": {"id": 12347, "is_bot": False},
                },
            }
            self.assertEqual(service.process_update(update), "query_rejected")
            update["update_id"] = 12
            update["message"]["message_id"] = 80
            self.assertEqual(
                service.process_update(update), "query_user_cooldown"
            )
            self.assertEqual(len(gateway.calls), 1)

    def test_process_update_isolated_to_configured_chat_and_topic(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = replace(
                OnchainSettings(),
                base_dir=root,
                data_dir=root / "onchain",
                oar_telegram_query_state_path=root / "onchain" / "state.json",
                tg_chat_id="-100123",
                tg_onchain_flow_topic_id="42",
            )
            gateway = FakeGateway()
            service = TelegramQueryService(
                settings,
                gateway=gateway,
                automation_store=FakeAutomationStore(),
            )
            service.bot_username = "paopao_bot"
            base = {
                "update_id": 1,
                "message": {
                    "message_id": 2,
                    "message_thread_id": 99,
                    "text": "@paopao_bot CBDOGE",
                    "chat": {"id": -100123},
                    "from": {"id": 3, "is_bot": False},
                },
            }
            self.assertEqual(service.process_update(base), "ignored_other_topic")
            base["message"]["message_thread_id"] = 42
            base["message"]["chat"] = {"id": -100999}
            self.assertEqual(service.process_update(base), "ignored_other_chat")
            self.assertEqual(gateway.calls, [])

    def test_config_manager_enable_is_atomic_and_redacted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".env.oi").write_text(
                "TG_BOT_TOKEN=123456:fake_token\nTG_CHAT_ID=-100123\n",
                encoding="utf-8",
            )
            (root / ".env.onchain").write_text(
                "ONCHAIN_DATA_DIR=data/onchain\n"
                "TG_ONCHAIN_FLOW_TOPIC_ID=42\n",
                encoding="utf-8",
            )
            manager = ConfigManager(root)
            payload = manager.telegram_query("enable")
            self.assertEqual(payload["status"], "ok")
            status = manager.status()
            self.assertTrue(status["OAR_TELEGRAM_QUERY_ENABLE"])
            self.assertEqual(status["OAR_TELEGRAM_QUERY_ACK"], "configured")
            manager.telegram_query("disable")
            self.assertFalse(manager.status()["OAR_TELEGRAM_QUERY_ENABLE"])

    def test_unit_and_wrapper_preserve_explicit_send_gate(self) -> None:
        root = Path(__file__).resolve().parents[2]
        unit = (root / "ops/systemd/paopao-oar-query.service").read_text(
            encoding="utf-8"
        )
        wrapper = (root / "scripts/run_oar_query.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("ExecStart=/home/ubuntu/paopao-crypto-radar/scripts/run_oar_query.sh", unit)
        self.assertIn("RestartPreventExitStatus=2", unit)
        self.assertIn('"--send"', wrapper)
        self.assertIn('"--confirm-real-send"', wrapper)
        self.assertNotIn("eval ", wrapper)
        cli = (root / "paopao_radar/onchain_flow/cli.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"telegram_query_admin_required"', cli)
        self.assertIn('"telegram_query_forum_required"', cli)

    def test_query_reply_is_not_indexed_as_a_signal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            settings = Settings(
                base_dir=root,
                data_dir=data_dir,
                tg_push_history_path=data_dir / "query_history.json",
                tg_outbox_path=data_dir / "query_outbox.json",
                signal_events_path=data_dir / "signal_events.json",
                signal_events_db_path=data_dir / "signals.db",
            )
            gateway = TelegramGateway(settings, JsonStore(data_dir))
            with redirect_stdout(io.StringIO()):
                result = gateway.send(
                    "query reply",
                    "TG_ONCHAIN_QUERY",
                    "query:1",
                    send=False,
                    confirm_real_send=False,
                )
            self.assertEqual(result.status, "dry_run")
            self.assertFalse(settings.signal_events_path.exists())
            self.assertFalse(settings.signal_events_db_path.exists())


if __name__ == "__main__":
    unittest.main()
