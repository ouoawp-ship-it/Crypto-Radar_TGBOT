from __future__ import annotations

from typing import Any

from paopao_radar.telegram import PushResult, TelegramGateway

from .config import OnchainSettings
from .constants import TEMPLATE_ID
from .notifier import build_onchain_telegram_gateway
from .report_formatter import format_token_report


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


class ReportNotifier:
    def __init__(
        self,
        settings: OnchainSettings,
        *,
        gateway: TelegramGateway | None = None,
    ):
        settings.assert_safe_paths()
        self.settings = settings
        self.gateway = gateway or build_onchain_telegram_gateway(settings)

    @staticmethod
    def card_key(payload: dict[str, object]) -> str:
        query = _mapping(payload.get("query"))
        return (
            f"oar:{int(query.get('chain_id') or 0)}:"
            f"{str(query.get('contract') or '').lower()}:"
            f"{str(query.get('window') or '')}"
        )

    def notify(
        self,
        payload: dict[str, object],
        *,
        send: bool,
        confirm_real_send: bool,
    ) -> PushResult:
        report = _mapping(payload.get("report"))
        analysis = _mapping(payload.get("analysis"))
        primary = _mapping(analysis.get("primary_behavior"))
        token = _mapping(payload.get("token"))
        query = _mapping(payload.get("query"))
        ai = _mapping(report.get("ai"))
        card_key = self.card_key(payload)
        content_hash = str(report.get("content_hash") or "")
        dedup_key = f"{card_key}:{content_hash[:16]}"
        text = format_token_report(payload)
        complete = bool(payload.get("complete")) and bool(
            analysis.get("complete")
        )
        signal_record = {
            "module": "onchain",
            "template_id": TEMPLATE_ID,
            "symbol": str(token.get("symbol") or "UNKNOWN"),
            "chain": str(query.get("chain") or "base"),
            "chain_id": int(query.get("chain_id") or 0),
            "contract": str(query.get("contract") or "").lower(),
            "status": "complete" if complete else "partial",
            "score": int(primary.get("score") or 0),
            "behavior_type": str(
                primary.get("type") or "insufficient_data"
            ),
            "behavior_label": str(primary.get("label") or "数据不足"),
            "summary": str(report.get("rule_summary_text") or "")[:1200],
            "context_hash": str(report.get("context_hash") or ""),
            "ai_status": str(ai.get("status") or "not_requested"),
            "source": "manual_token_notify",
            "oar_card_key": card_key,
            "oar_content_hash": content_hash,
            "analysis_status": str(analysis.get("status") or ""),
            "analysis_complete": complete,
        }
        history = self.gateway.history_records()
        old_cards = self._active_cards(history, card_key)

        real_attempt = bool(
            send and confirm_real_send and self.settings.real_send
        )
        if (
            real_attempt
            and not complete
            and not self.settings.oar_replace_complete_card_with_partial
            and any(self._record_complete(record) for record in old_cards)
        ):
            result = PushResult(
                "skipped",
                "partial_does_not_replace_complete",
                False,
                [],
            )
            self.gateway.record_result(
                template_id=TEMPLATE_ID,
                dedup_key=dedup_key,
                result=result,
                text=text,
                signal_records=[signal_record],
            )
            return result

        if send and confirm_real_send and not self.settings.real_send:
            result = PushResult(
                "blocked",
                "onchain_real_send_disabled",
                False,
                [],
            )
            self.gateway.record_result(
                template_id=TEMPLATE_ID,
                dedup_key=dedup_key,
                result=result,
                text=text,
                signal_records=[signal_record],
            )
            return result

        result = self.gateway.send(
            text,
            TEMPLATE_ID,
            dedup_key,
            send=bool(send),
            confirm_real_send=bool(confirm_real_send),
            cooldown_sec=self.settings.alert_cooldown_sec,
            parse_mode="HTML",
            signal_records=[signal_record],
            enrich_market_context=False,
        )
        if not result.sent:
            if (
                result.status == "skipped"
                and result.reason == "dedup_cooldown"
                and len(old_cards) > 1
            ):
                retry_ids = self._message_ids(
                    old_cards[:-1],
                    excluding=set(),
                )
                if retry_ids:
                    self.gateway.delete_messages_detailed(
                        retry_ids,
                        reason="oar_card_delete_retry",
                    )
            if result.message_ids:
                self.gateway.delete_messages_detailed(
                    list(result.message_ids),
                    reason="oar_partial_send_rollback",
                )
            return result

        new_ids = set(result.message_ids or [])
        old_ids = self._message_ids(old_cards, excluding=new_ids)
        if not old_ids:
            return result
        deletion = self.gateway.delete_messages_detailed(
            old_ids,
            reason="oar_card_replaced",
        )
        if hasattr(self.gateway, "annotate_delivery_history"):
            self.gateway.annotate_delivery_history(
                result.delivery_id,
                deleted_message_ids=list(deletion.get("deleted_ids") or []),
                failed_delete_message_ids=list(
                    deletion.get("failed_ids") or []
                ),
            )
        return result

    @staticmethod
    def _message_ids(
        records: list[dict[str, Any]],
        *,
        excluding: set[int],
    ) -> list[int]:
        result: set[int] = set()
        for record in records:
            deleted = {
                int(item)
                for item in (record.get("deleted_message_ids") or [])
                if isinstance(item, int) or str(item).isdigit()
            }
            for message_id in record.get("message_ids") or []:
                if not (
                    isinstance(message_id, int)
                    or str(message_id).isdigit()
                ):
                    continue
                normalized = int(message_id)
                if normalized not in excluding and normalized not in deleted:
                    result.add(normalized)
        return sorted(result)

    @staticmethod
    def _record_complete(record: dict[str, object]) -> bool:
        for item in record.get("signal_records") or []:
            if isinstance(item, dict):
                return bool(item.get("analysis_complete"))
        return False

    @staticmethod
    def _active_cards(
        history: list[dict[str, Any]],
        card_key: str,
    ) -> list[dict[str, Any]]:
        matches = []
        for record in history:
            if (
                record.get("template_id") != TEMPLATE_ID
                or record.get("status") != "sent"
                or record.get("lifecycle_deleted")
            ):
                continue
            signal_records = record.get("signal_records")
            if not isinstance(signal_records, list):
                continue
            if any(
                isinstance(item, dict)
                and item.get("oar_card_key") == card_key
                for item in signal_records
            ):
                matches.append(record)
        return matches
