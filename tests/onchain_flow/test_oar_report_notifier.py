from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from paopao_radar.onchain_flow.report import TokenReportService
from paopao_radar.onchain_flow.report_notifier import ReportNotifier
from paopao_radar.onchain_flow.token_activity import TokenActivityQuery
from paopao_radar.onchain_flow.token_analysis import TokenAnalysisService
from paopao_radar.telegram import PushResult

from tests.onchain_flow.analysis_support import fixture_case
from tests.onchain_flow.support import make_settings


class StaticActivity:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload

    def execute(self, query: object) -> dict[str, object]:
        del query
        return deepcopy(self.payload)


class FakeGateway:
    def __init__(
        self,
        *,
        history: list[dict[str, object]] | None = None,
        result: PushResult | None = None,
        failed_delete_ids: list[int] | None = None,
    ):
        self.history = list(history or [])
        self.result = result or PushResult(
            "sent",
            "telegram_api",
            True,
            [900, 901],
            "delivery-new",
        )
        self.failed_delete_ids = set(failed_delete_ids or [])
        self.events: list[tuple[str, object]] = []
        self.send_kwargs: dict[str, object] = {}
        self.recorded: list[PushResult] = []
        self.annotations: list[dict[str, object]] = []

    def history_records(self) -> list[dict[str, object]]:
        return deepcopy(self.history)

    def send(
        self,
        text: str,
        template_id: str,
        dedup_key: str,
        **kwargs: object,
    ) -> PushResult:
        self.events.append(("send", dedup_key))
        self.send_kwargs = {
            "text": text,
            "template_id": template_id,
            "dedup_key": dedup_key,
            **kwargs,
        }
        return self.result

    def record_result(self, **kwargs: object) -> None:
        result = kwargs["result"]
        self.events.append(("record", result.reason))
        self.recorded.append(result)

    def delete_messages_detailed(
        self,
        message_ids: list[int],
        *,
        reason: str,
    ) -> dict[str, list[int]]:
        self.events.append(("delete", list(message_ids)))
        return {
            "deleted_ids": [
                item for item in message_ids
                if item not in self.failed_delete_ids
            ],
            "failed_ids": [
                item for item in message_ids
                if item in self.failed_delete_ids
            ],
        }

    def annotate_delivery_history(
        self,
        delivery_id: str,
        *,
        deleted_message_ids: list[int],
        failed_delete_message_ids: list[int],
    ) -> None:
        self.annotations.append(
            {
                "delivery_id": delivery_id,
                "deleted": deleted_message_ids,
                "failed": failed_delete_message_ids,
            }
        )


def old_card(
    *,
    contract: str,
    content_hash: str,
    message_ids: list[int],
    complete: bool = True,
    context_hash: str | None = None,
    ai_status: str | None = None,
) -> dict[str, object]:
    signal_record: dict[str, object] = {
        "oar_card_key": f"oar:8453:{contract}:4h",
        "oar_content_hash": content_hash,
        "analysis_complete": complete,
    }
    if context_hash is not None:
        signal_record["context_hash"] = context_hash
    if ai_status is not None:
        signal_record["ai_status"] = ai_status
    return {
        "template_id": "TG_ONCHAIN_FLOW_ALERT",
        "status": "sent",
        "message_ids": message_ids,
        "signal_records": [signal_record],
    }


class OarReportNotifierTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.query = TokenActivityQuery.create(
            self.settings,
            chain="base",
            contract="0x9999999999999999999999999999999999999999",
            window="4h",
            max_events=None,
            max_rpc_requests=None,
            top_n=None,
            with_price=False,
            min_usd=None,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def report(self, case: str = "accumulation") -> dict[str, object]:
        return TokenReportService(
            self.settings,
            TokenAnalysisService(
                self.settings,
                StaticActivity(fixture_case(case)),
            ),
        ).execute(self.query, with_ai=False)

    def test_dry_run_uses_existing_template_and_zero_real_send(self) -> None:
        gateway = FakeGateway(
            result=PushResult(
                "dry_run",
                "send_flag_not_set",
                False,
                [],
            )
        )
        result = ReportNotifier(
            self.settings,
            gateway=gateway,
        ).notify(self.report(), send=False, confirm_real_send=False)
        self.assertEqual(result.status, "dry_run")
        self.assertEqual(
            gateway.send_kwargs["template_id"],
            "TG_ONCHAIN_FLOW_ALERT",
        )
        self.assertFalse(gateway.send_kwargs["send"])
        self.assertFalse(gateway.send_kwargs["confirm_real_send"])
        self.assertFalse(gateway.send_kwargs["enrich_market_context"])

    def test_missing_confirm_is_forwarded_as_blocked_gate(self) -> None:
        gateway = FakeGateway(
            result=PushResult(
                "blocked",
                "missing_confirm_real_send",
                False,
                [],
            )
        )
        result = ReportNotifier(
            replace(self.settings, real_send=True),
            gateway=gateway,
        ).notify(self.report(), send=True, confirm_real_send=False)
        self.assertEqual(result.status, "blocked")
        self.assertTrue(gateway.send_kwargs["send"])
        self.assertFalse(gateway.send_kwargs["confirm_real_send"])

    def test_real_send_setting_is_third_gate(self) -> None:
        gateway = FakeGateway()
        result = ReportNotifier(
            self.settings,
            gateway=gateway,
        ).notify(self.report(), send=True, confirm_real_send=True)
        self.assertEqual(result.status, "blocked")
        self.assertEqual(result.reason, "onchain_real_send_disabled")
        self.assertFalse(gateway.send_kwargs)
        self.assertEqual(gateway.events[0][0], "record")

    def test_success_sends_before_deleting_only_same_card(self) -> None:
        contract = "0x9999999999999999999999999999999999999999"
        history = [
            old_card(
                contract=contract,
                content_hash="old",
                message_ids=[10, 11],
            ),
            old_card(
                contract="0x8888888888888888888888888888888888888888",
                content_hash="other",
                message_ids=[20],
            ),
            {
                "template_id": "TG_ONCHAIN_FLOW_ALERT",
                "status": "sent",
                "message_ids": [30],
                "signal_records": [],
                "topic_intro": True,
            },
        ]
        gateway = FakeGateway(history=history)
        notifier = ReportNotifier(
            replace(self.settings, real_send=True),
            gateway=gateway,
        )
        result = notifier.notify(
            self.report(),
            send=True,
            confirm_real_send=True,
        )
        self.assertTrue(result.sent)
        self.assertEqual(gateway.events[0][0], "send")
        self.assertEqual(gateway.events[1], ("delete", [10, 11]))
        self.assertNotIn(20, gateway.events[1][1])
        self.assertNotIn(30, gateway.events[1][1])

    def test_failed_new_send_keeps_old_card_and_rolls_back_partial(self) -> None:
        contract = "0x9999999999999999999999999999999999999999"
        gateway = FakeGateway(
            history=[
                old_card(
                    contract=contract,
                    content_hash="old",
                    message_ids=[10],
                )
            ],
            result=PushResult(
                "failed",
                "telegram_api_failed",
                False,
                [99],
            ),
        )
        result = ReportNotifier(
            replace(self.settings, real_send=True),
            gateway=gateway,
        ).notify(self.report(), send=True, confirm_real_send=True)
        self.assertFalse(result.sent)
        self.assertEqual(gateway.events, [
            ("send", gateway.send_kwargs["dedup_key"]),
            ("delete", [99]),
        ])
        self.assertNotIn(("delete", [10]), gateway.events)

    def test_partial_does_not_replace_complete_card(self) -> None:
        partial = self.report("partial_input")
        contract = "0x9999999999999999999999999999999999999999"
        gateway = FakeGateway(
            history=[
                old_card(
                    contract=contract,
                    content_hash="complete",
                    message_ids=[10],
                    complete=True,
                )
            ]
        )
        result = ReportNotifier(
            replace(self.settings, real_send=True),
            gateway=gateway,
        ).notify(partial, send=True, confirm_real_send=True)
        self.assertEqual(result.status, "skipped")
        self.assertEqual(
            result.reason,
            "partial_does_not_replace_complete",
        )
        self.assertFalse(gateway.send_kwargs)
        self.assertFalse(any(event[0] == "delete" for event in gateway.events))

    def test_partial_protection_precedes_ai_degradation_protection(self) -> None:
        partial = self.report("partial_input")
        context_hash = partial["report"]["context_hash"]
        gateway = FakeGateway(
            history=[
                old_card(
                    contract=(
                        "0x9999999999999999999999999999999999999999"
                    ),
                    content_hash="complete",
                    message_ids=[10],
                    complete=True,
                    context_hash=context_hash,
                    ai_status="available",
                )
            ]
        )
        result = ReportNotifier(
            replace(self.settings, real_send=True),
            gateway=gateway,
        ).notify(partial, send=True, confirm_real_send=True)
        self.assertEqual(
            result.reason,
            "partial_does_not_replace_complete",
        )
        self.assertFalse(gateway.send_kwargs)

    def test_delete_failure_is_audited_and_remains_retryable(self) -> None:
        contract = "0x9999999999999999999999999999999999999999"
        gateway = FakeGateway(
            history=[
                old_card(
                    contract=contract,
                    content_hash="old",
                    message_ids=[10, 11],
                )
            ],
            failed_delete_ids=[11],
        )
        ReportNotifier(
            replace(self.settings, real_send=True),
            gateway=gateway,
        ).notify(self.report(), send=True, confirm_real_send=True)
        self.assertEqual(gateway.annotations[0]["deleted"], [10])
        self.assertEqual(gateway.annotations[0]["failed"], [11])

    def test_identical_content_dedup_retries_stale_card_deletion(self) -> None:
        contract = "0x9999999999999999999999999999999999999999"
        gateway = FakeGateway(
            history=[
                old_card(
                    contract=contract,
                    content_hash="stale",
                    message_ids=[10],
                ),
                old_card(
                    contract=contract,
                    content_hash="current",
                    message_ids=[20],
                ),
            ],
            result=PushResult(
                "skipped",
                "dedup_cooldown",
                False,
                [],
            ),
        )
        result = ReportNotifier(
            replace(self.settings, real_send=True),
            gateway=gateway,
        ).notify(self.report(), send=True, confirm_real_send=True)
        self.assertEqual(result.status, "skipped")
        self.assertIn(("delete", [10]), gateway.events)
        self.assertNotIn(("delete", [20]), gateway.events)

    def test_ai_degradation_does_not_replace_richer_same_context(self) -> None:
        for old_status, new_status in (
            ("available", "failed"),
            ("cached", "hourly_limit"),
            ("available", "not_requested"),
        ):
            with self.subTest(
                old_status=old_status,
                new_status=new_status,
            ):
                payload = self.report()
                payload["report"]["ai"]["status"] = new_status
                context_hash = payload["report"]["context_hash"]
                gateway = FakeGateway(
                    history=[
                        old_card(
                            contract=(
                                "0x9999999999999999999999999999999999999999"
                            ),
                            content_hash="rich",
                            message_ids=[10],
                            context_hash=context_hash,
                            ai_status=old_status,
                        )
                    ]
                )
                result = ReportNotifier(
                    replace(self.settings, real_send=True),
                    gateway=gateway,
                ).notify(
                    payload,
                    send=True,
                    confirm_real_send=True,
                )
                self.assertEqual(result.status, "skipped")
                self.assertEqual(
                    result.reason,
                    "ai_degradation_does_not_replace_richer_card",
                )
                self.assertFalse(gateway.send_kwargs)
                self.assertFalse(
                    any(event[0] == "delete" for event in gateway.events)
                )

    def test_changed_context_allows_rule_only_fact_card(self) -> None:
        payload = self.report()
        payload["report"]["ai"]["status"] = "failed"
        gateway = FakeGateway(
            history=[
                old_card(
                    contract=(
                        "0x9999999999999999999999999999999999999999"
                    ),
                    content_hash="rich",
                    message_ids=[10],
                    context_hash="different-context",
                    ai_status="available",
                )
            ]
        )
        result = ReportNotifier(
            replace(self.settings, real_send=True),
            gateway=gateway,
        ).notify(payload, send=True, confirm_real_send=True)
        self.assertTrue(result.sent)
        self.assertEqual(gateway.events[0][0], "send")
        self.assertIn(("delete", [10]), gateway.events)

    def test_dry_run_writes_only_independent_onchain_signal_store(self) -> None:
        payload = self.report()
        with patch(
            "paopao_radar.telegram.requests.post",
            side_effect=AssertionError("real Telegram must not be called"),
        ):
            with redirect_stdout(io.StringIO()):
                result = ReportNotifier(self.settings).notify(
                    payload,
                    send=False,
                    confirm_real_send=False,
                )
        self.assertEqual(result.status, "dry_run")
        self.assertFalse((self.root / "data" / "signals.db").exists())
        self.assertTrue(self.settings.signal_events_db_path.exists())
        with closing(
            sqlite3.connect(self.settings.signal_events_db_path)
        ) as conn:
            row = conn.execute(
                "SELECT module, template_id, symbol, score, payload_json "
                "FROM signals ORDER BY id DESC LIMIT 1"
            ).fetchone()
        self.assertEqual(row[0], "onchain")
        self.assertEqual(row[1], "TG_ONCHAIN_FLOW_ALERT")
        self.assertEqual(row[2], "TST")
        stored_payload = json.loads(row[4])
        stored_facts = stored_payload["facts"]
        self.assertEqual(row[3], stored_facts["behavior_score"])
        self.assertEqual(
            stored_facts["score"],
            stored_facts["behavior_score"],
        )
        self.assertIn("behavior_type", stored_facts)
        self.assertTrue(self.settings.tg_push_history_path.exists())
        history = json.loads(
            self.settings.tg_push_history_path.read_text(encoding="utf-8")
        )
        audit = history[-1]["signal_records"][0]
        self.assertEqual(audit["score"], audit["behavior_score"])
        self.assertEqual(
            audit["context_hash"],
            payload["report"]["context_hash"],
        )
        self.assertNotIn("summary", audit)
        serialized = json.dumps(audit, ensure_ascii=False)
        self.assertNotIn("OAR_AI_API_KEY", serialized)
        self.assertNotIn("RPC_URL", serialized)
        self.assertNotIn("private", serialized.lower())


if __name__ == "__main__":
    unittest.main()
