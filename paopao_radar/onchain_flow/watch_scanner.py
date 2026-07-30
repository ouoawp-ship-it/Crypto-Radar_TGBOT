from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from .address_intelligence import AddressIntelligenceStore
from .automation_store import AutomationStore, AutomationStoreError
from .config import OnchainSettings
from .report import TokenReportService
from .report_notifier import ReportNotifier
from .signal_bridge import SignalBridge
from .token_activity import TokenActivityQuery, TokenActivityQueryError
from .token_analysis import TokenAnalysisService


ACTIONABLE_BEHAVIORS = {
    "accumulation_candidate",
    "distribution_candidate",
    "wallet_consolidation_candidate",
    "fanout_candidate",
}


class WatchScanner:
    def __init__(
        self,
        settings: OnchainSettings,
        store: AutomationStore,
        *,
        bridge: SignalBridge | None = None,
        analysis_factory: Callable[
            [OnchainSettings, TokenActivityQuery], Any
        ] | None = None,
        report_factory: Callable[[OnchainSettings], Any] | None = None,
        notifier_factory: Callable[[OnchainSettings], Any] | None = None,
        address_intelligence_store: Any | None = None,
        clock: Any = time.time,
        sleeper: Any = time.sleep,
    ):
        self.settings = settings
        self.store = store
        self.bridge = bridge or SignalBridge(settings, store, clock=clock)
        self.analysis_factory = analysis_factory or (
            lambda current_settings, query: TokenAnalysisService.from_settings(
                current_settings, query
            )
        )
        self.report_factory = report_factory or (
            lambda current_settings: TokenReportService(
                current_settings, None
            )
        )
        self.notifier_factory = notifier_factory or ReportNotifier
        self.address_intelligence_store = (
            address_intelligence_store
            if address_intelligence_store is not None
            else AddressIntelligenceStore.from_settings(settings)
        )
        self.clock = clock
        self.sleeper = sleeper

    def run_once(
        self,
        *,
        allow_network: bool,
        notify_dry_run: bool,
        with_ai: bool,
        send: bool,
        confirm_real_send: bool,
    ) -> dict[str, object]:
        if not allow_network:
            return self._gate_failure("allow_network_required")
        if notify_dry_run and send:
            return self._gate_failure("conflicting_notification_modes")
        self.settings.validate()
        if (
            self.settings.oar_watch_max_events_per_token
            > self.settings.token_activity_max_events
            or self.settings.oar_watch_max_rpc_requests_per_token
            > self.settings.token_activity_max_rpc_requests
            or self.settings.oar_watch_top_transfers
            > self.settings.token_activity_top_n
        ):
            return self._gate_failure(
                "automation_query_budget_exceeds_token_activity_limit"
            )
        now = int(self.clock())
        bridge_result = self.bridge.run_once()
        self.store.expire_and_recompute(
            manual_priority=self.settings.oar_watch_manual_priority,
            now=now,
        )
        result: dict[str, object] = {
            "status": "ok",
            "bridge": bridge_result,
            "claimed_tokens": 0,
            "scanned_tokens": 0,
            "deferred_tokens": 0,
            "deferred_token_keys": [],
            "cycle_budget_exhausted": False,
            "actionable_tokens": 0,
            "notifications_attempted": 0,
            "notifications_sent": 0,
            "partial_tokens": 0,
            "failed_tokens": 0,
            "stale_tokens": 0,
            "rpc_request_count": 0,
            "rpc_budget_consumed": 0,
            "results": [],
            "network_activity": False,
            "database_writes": True,
            "telegram_calls": False,
            "ai_calls": False,
        }
        remaining_rpc = self.settings.oar_watch_max_rpc_requests_per_cycle
        while (
            int(result["claimed_tokens"])
            < self.settings.oar_watch_max_tokens_per_cycle
            and remaining_rpc > 0
        ):
            owner = f"watch-once:{uuid.uuid4().hex}"
            claimed = self.store.claim_due(
                owner=owner,
                limit=1,
                lease_sec=self.settings.oar_watch_lease_sec,
                now=int(self.clock()),
            )
            if not claimed:
                break
            item = claimed[0]
            result["claimed_tokens"] = int(result["claimed_tokens"]) + 1
            token_result = self._scan_item(
                item,
                now=now,
                remaining_rpc=remaining_rpc,
                lease_owner=owner,
                notify_requested=bool(notify_dry_run or send),
                with_ai=with_ai,
                send=send,
                confirm_real_send=confirm_real_send,
            )
            results = result["results"]
            assert isinstance(results, list)
            results.append(token_result)
            if token_result.get("analysis_started"):
                result["scanned_tokens"] = int(result["scanned_tokens"]) + 1
                result["network_activity"] = True
            used = int(token_result.get("rpc_request_count") or 0)
            budget_used = int(
                token_result.get("rpc_budget_consumed") or used
            )
            remaining_rpc = max(0, remaining_rpc - budget_used)
            result["rpc_request_count"] = (
                int(result["rpc_request_count"]) + used
            )
            result["rpc_budget_consumed"] = (
                int(result["rpc_budget_consumed"]) + budget_used
            )
            if token_result["status"] == "partial":
                result["partial_tokens"] = int(result["partial_tokens"]) + 1
            if token_result["status"] == "failed":
                result["failed_tokens"] = int(result["failed_tokens"]) + 1
            if token_result["status"] == "stale":
                result["stale_tokens"] = int(result["stale_tokens"]) + 1
            if token_result.get("actionable"):
                result["actionable_tokens"] = (
                    int(result["actionable_tokens"]) + 1
                )
            if token_result.get("notification_attempted"):
                result["notifications_attempted"] = (
                    int(result["notifications_attempted"]) + 1
                )
                result["telegram_calls"] = bool(
                    send
                    and confirm_real_send
                    and self.settings.real_send
                )
            if token_result.get("notification_status") == "sent":
                result["notifications_sent"] = (
                    int(result["notifications_sent"]) + 1
                )
            if int(token_result.get("ai_calls") or 0):
                result["ai_calls"] = True
        result["cycle_budget_exhausted"] = remaining_rpc <= 0
        if remaining_rpc <= 0:
            remaining_slots = max(
                0,
                self.settings.oar_watch_max_tokens_per_cycle
                - int(result["claimed_tokens"]),
            )
            if remaining_slots:
                deferred = self.store.due_token_keys(
                    limit=remaining_slots,
                    now=int(self.clock()),
                )
                result["deferred_token_keys"] = deferred
                result["deferred_tokens"] = len(deferred)
        if int(result["failed_tokens"]) or int(result["partial_tokens"]):
            result["status"] = "partial"
        if int(result["stale_tokens"]):
            result["status"] = "partial"
        return result

    def _scan_item(
        self,
        item: dict[str, object],
        *,
        now: int,
        remaining_rpc: int,
        lease_owner: str,
        notify_requested: bool,
        with_ai: bool,
        send: bool,
        confirm_real_send: bool,
    ) -> dict[str, object]:
        token_key = str(item["token_key"])
        started_at = int(self.clock())
        source_rows = self.store.active_sources(token_key, now=now, limit=20)
        linked = self._linked_market_signals(source_rows, now=now)
        source_refs = [
            str(source["public_ref"])
            for source in linked
            if str(source.get("public_ref") or "")
        ]
        if remaining_rpc <= 0:
            released = self.store.release_claim_without_failure(
                token_key,
                lease_owner=lease_owner,
                reason="cycle_rpc_budget_exhausted",
                now=int(self.clock()),
            )
            return {
                "token_key": token_key,
                "status": "deferred" if released else "stale",
                "error": (
                    "cycle_rpc_budget_exhausted"
                    if released
                    else "lease_lost"
                ),
                "actionable": False,
                "analysis_started": False,
                "rpc_request_count": 0,
                "rpc_budget_consumed": 0,
                "notification_attempted": False,
                "notification_status": "not_requested",
                "ai_calls": 0,
            }
        per_token_rpc = min(
            self.settings.oar_watch_max_rpc_requests_per_token,
            remaining_rpc,
        )
        analysis_started = False
        try:
            query = TokenActivityQuery.create(
                self.settings,
                chain="base",
                contract=str(item["contract_address"]),
                window=str(item["query_window"]),
                max_events=self.settings.oar_watch_max_events_per_token,
                max_rpc_requests=per_token_rpc,
                top_n=self.settings.oar_watch_top_transfers,
                with_price=False,
                min_usd=None,
            )
            analysis_service = self.analysis_factory(self.settings, query)
            analysis_started = True
            analyzed = analysis_service.execute(query)
        except (AutomationStoreError, TokenActivityQueryError, ValueError) as exc:
            code = getattr(exc, "code", type(exc).__name__)
            return self._record_failure(
                token_key,
                started_at=started_at,
                code=str(code),
                source_refs=source_refs,
                rpc_budget_consumed=per_token_rpc,
                lease_owner=lease_owner,
                analysis_started=analysis_started,
            )
        except Exception:
            return self._record_failure(
                token_key,
                started_at=started_at,
                code="token_scan_failed",
                source_refs=source_refs,
                rpc_budget_consumed=per_token_rpc,
                lease_owner=lease_owner,
                analysis_started=analysis_started,
            )

        diagnostics = analyzed.get("diagnostics")
        diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
        rpc_count = int(diagnostics.get("rpc_request_count") or 0)
        if not self._renew_lease(token_key, lease_owner):
            return self._record_stale(
                token_key,
                lease_owner=lease_owner,
                started_at=started_at,
                source_refs=source_refs,
                rpc_request_count=rpc_count,
                analysis_started=True,
            )
        analysis = analyzed.get("analysis")
        analysis = analysis if isinstance(analysis, dict) else {}
        primary = analysis.get("primary_behavior")
        primary = primary if isinstance(primary, dict) else {}
        groups = [
            group
            for group in (analysis.get("wallet_groups") or [])
            if isinstance(group, dict)
        ]
        max_wallet_score = max(
            (int(group.get("score") or 0) for group in groups),
            default=0,
        )
        actionable = self._notification_gate(
            analyzed,
            behavior_type=str(primary.get("type") or ""),
            behavior_score=int(primary.get("score") or 0),
            max_wallet_score=max_wallet_score,
        )
        notification_status = "not_requested"
        notification_reason = "notification_gate_not_met"
        context_hash = ""
        ai_calls = 0
        reported: dict[str, object] | None = None
        if actionable:
            if not self._renew_lease(token_key, lease_owner):
                return self._record_stale(
                    token_key,
                    lease_owner=lease_owner,
                    started_at=started_at,
                    source_refs=source_refs,
                    rpc_request_count=rpc_count,
                    analysis_started=True,
                )
            report_service = self.report_factory(self.settings)
            reported = report_service.build_from_analysis(
                analyzed,
                with_ai=bool(with_ai),
                linked_market_signals=linked,
            )
            report = reported.get("report")
            report = report if isinstance(report, dict) else {}
            ai = report.get("ai")
            ai = ai if isinstance(ai, dict) else {}
            ai_calls = int(ai.get("calls") or 0)
            context_hash = str(report.get("context_hash") or "")
        if actionable and notify_requested and reported is not None:
            if not self._renew_lease(token_key, lease_owner):
                return self._record_stale(
                    token_key,
                    lease_owner=lease_owner,
                    started_at=started_at,
                    source_refs=source_refs,
                    rpc_request_count=rpc_count,
                    analysis_started=True,
                )
            notifier = self.notifier_factory(self.settings)
            notification = notifier.notify(
                reported,
                send=bool(send),
                confirm_real_send=bool(confirm_real_send),
                source="watchlist_automation",
                linked_source_refs=source_refs,
                linked_source_modules=[
                    str(source.get("module") or "") for source in linked
                ],
                watch_priority=int(item.get("priority") or 0),
                watch_reason_count=len(source_refs)
                + int(bool(item.get("manual_watch"))),
            )
            notification_status = str(notification.status)
            notification_reason = str(notification.reason)
        elif actionable:
            notification_reason = "observe_mode"

        activity_complete = bool(analyzed.get("complete"))
        analysis_complete = bool(analysis.get("complete"))
        scan_status = (
            "ok"
            if activity_complete and analysis_complete
            else "partial"
        )
        partial_reason = ""
        if scan_status == "partial":
            partial_reason = str(
                analyzed.get("truncation_reason")
                or analysis.get("status")
                or "partial_result"
            )
        summary = analyzed.get("summary")
        summary = summary if isinstance(summary, dict) else {}
        if not self._renew_lease(token_key, lease_owner):
            return self._record_stale(
                token_key,
                lease_owner=lease_owner,
                started_at=started_at,
                source_refs=source_refs,
                rpc_request_count=rpc_count,
                analysis_started=True,
            )
        completion = self.store.record_scan(
            token_key,
            lease_owner=lease_owner,
            started_at=started_at,
            status=scan_status,
            activity_complete=activity_complete,
            analysis_complete=analysis_complete,
            analysis_status=str(analysis.get("status") or ""),
            behavior_type=str(primary.get("type") or ""),
            behavior_score=int(primary.get("score") or 0),
            max_wallet_group_score=max_wallet_score,
            transfer_count=int(summary.get("transfer_count") or 0),
            rpc_request_count=rpc_count,
            context_hash=context_hash,
            notification_status=notification_status,
            notification_reason=notification_reason,
            error_code=partial_reason,
            source_refs=source_refs,
            scan_interval_sec=self.settings.oar_watch_scan_interval_sec,
            max_consecutive_failures=(
                self.settings.oar_watch_max_consecutive_failures
            ),
            now=int(self.clock()),
        )
        if completion == "lease_lost":
            return self._stale_result(
                token_key,
                rpc_request_count=rpc_count,
                analysis_started=True,
            )
        queue_result: dict[str, int] = {
            "observed": 0,
            "created": 0,
            "updated": 0,
        }
        try:
            queue_result = (
                self.address_intelligence_store.observe_complete_scan(
                    analyzed,
                    observed_at=int(self.clock()),
                )
            )
        except Exception:
            # Address intelligence is local best-effort enrichment. It must
            # never turn a successful scan into a failure or invoke a
            # provider on the Watch hot path.
            queue_result = {
                "observed": 0,
                "created": 0,
                "updated": 0,
            }
        return {
            "token_key": token_key,
            "status": scan_status,
            "activity_complete": activity_complete,
            "analysis_complete": analysis_complete,
            "analysis_status": str(analysis.get("status") or ""),
            "behavior_type": str(primary.get("type") or ""),
            "behavior_score": int(primary.get("score") or 0),
            "max_wallet_group_score": max_wallet_score,
            "actionable": actionable,
            "rpc_request_count": rpc_count,
            "rpc_budget_consumed": rpc_count,
            "notification_attempted": bool(actionable and notify_requested),
            "notification_status": notification_status,
            "notification_reason": notification_reason,
            "error": partial_reason,
            "ai_calls": ai_calls,
            "analysis_started": True,
            "unknown_addresses_queued": int(
                queue_result.get("observed") or 0
            ),
            "external_label_provider_calls": 0,
        }

    def _record_failure(
        self,
        token_key: str,
        *,
        started_at: int,
        code: str,
        source_refs: list[str],
        rpc_budget_consumed: int,
        lease_owner: str,
        analysis_started: bool,
    ) -> dict[str, object]:
        completion = self.store.record_scan(
            token_key,
            lease_owner=lease_owner,
            started_at=started_at,
            status="failed",
            activity_complete=None,
            analysis_complete=None,
            error_code=code,
            source_refs=source_refs,
            scan_interval_sec=self.settings.oar_watch_scan_interval_sec,
            max_consecutive_failures=(
                self.settings.oar_watch_max_consecutive_failures
            ),
            now=int(self.clock()),
        )
        if completion == "lease_lost":
            return self._stale_result(
                token_key,
                rpc_request_count=0,
                rpc_budget_consumed=rpc_budget_consumed,
                analysis_started=analysis_started,
            )
        return {
            "token_key": token_key,
            "status": "failed",
            "error": code,
            "actionable": False,
            "rpc_request_count": 0,
            "rpc_budget_consumed": max(0, int(rpc_budget_consumed)),
            "notification_attempted": False,
            "notification_status": "not_requested",
            "ai_calls": 0,
            "analysis_started": analysis_started,
        }

    def _renew_lease(self, token_key: str, lease_owner: str) -> bool:
        return self.store.renew_lease(
            token_key,
            lease_owner=lease_owner,
            lease_sec=self.settings.oar_watch_lease_sec,
            now=int(self.clock()),
        )

    def _record_stale(
        self,
        token_key: str,
        *,
        lease_owner: str,
        started_at: int,
        source_refs: list[str],
        rpc_request_count: int,
        analysis_started: bool,
    ) -> dict[str, object]:
        self.store.record_scan(
            token_key,
            lease_owner=lease_owner,
            started_at=started_at,
            status="stale",
            activity_complete=None,
            analysis_complete=None,
            error_code="lease_lost",
            source_refs=source_refs,
            scan_interval_sec=self.settings.oar_watch_scan_interval_sec,
            max_consecutive_failures=(
                self.settings.oar_watch_max_consecutive_failures
            ),
            now=int(self.clock()),
        )
        return self._stale_result(
            token_key,
            rpc_request_count=rpc_request_count,
            analysis_started=analysis_started,
        )

    @staticmethod
    def _stale_result(
        token_key: str,
        *,
        rpc_request_count: int,
        rpc_budget_consumed: int | None = None,
        analysis_started: bool,
    ) -> dict[str, object]:
        return {
            "token_key": token_key,
            "status": "stale",
            "error": "lease_lost",
            "actionable": False,
            "rpc_request_count": max(0, int(rpc_request_count)),
            "rpc_budget_consumed": max(
                0,
                int(
                    rpc_request_count
                    if rpc_budget_consumed is None
                    else rpc_budget_consumed
                ),
            ),
            "notification_attempted": False,
            "notification_status": "not_requested",
            "ai_calls": 0,
            "analysis_started": analysis_started,
        }

    def _notification_gate(
        self,
        payload: dict[str, object],
        *,
        behavior_type: str,
        behavior_score: int,
        max_wallet_score: int,
    ) -> bool:
        analysis = payload.get("analysis")
        analysis = analysis if isinstance(analysis, dict) else {}
        if not (
            bool(payload.get("complete"))
            and bool(analysis.get("complete"))
            and analysis.get("status") == "ok"
        ):
            return False
        return (
            behavior_type in ACTIONABLE_BEHAVIORS
            and behavior_score
            >= self.settings.oar_watch_notify_min_behavior_score
        ) or (
            max_wallet_score
            >= self.settings.oar_watch_notify_min_wallet_score
        )

    @staticmethod
    def _linked_market_signals(
        sources: list[dict[str, object]], *, now: int
    ) -> list[dict[str, object]]:
        by_ref = {
            str(source.get("source_public_ref") or ""): source
            for source in sources
            if str(source.get("source_public_ref") or "")
        }
        ordered = sorted(
            by_ref.values(),
            key=lambda source: (
                -int(source.get("source_priority") or 0),
                -int(source.get("source_ts") or 0),
                str(source.get("source_public_ref") or ""),
            ),
        )[:10]
        return [
            {
                "public_ref": str(source.get("source_public_ref") or ""),
                "module": str(source.get("source_module") or ""),
                "symbol": str(source.get("source_symbol") or ""),
                "score": source.get("source_score"),
                "stage": str(source.get("source_stage") or ""),
                "severity": str(source.get("source_severity") or ""),
                "ts": int(source.get("source_ts") or 0),
                "age_sec": max(
                    0, now - int(source.get("source_ts") or now)
                ),
                "summary": str(source.get("source_summary") or "")[:300],
                "_priority": int(source.get("source_priority") or 0),
            }
            for source in ordered
        ]

    def run_live(
        self,
        *,
        allow_network: bool,
        duration_minutes: float | None,
        notify_dry_run: bool,
        with_ai: bool,
        send: bool,
        confirm_real_send: bool,
    ) -> dict[str, object]:
        if not self.settings.oar_automation_enable:
            return self._gate_failure("automation_disabled")
        if not allow_network:
            return self._gate_failure("allow_network_required")
        if notify_dry_run and send:
            return self._gate_failure("conflicting_notification_modes")
        if duration_minutes is not None and duration_minutes < 0:
            return self._gate_failure("invalid_duration")
        started = float(self.clock())
        deadline = (
            None
            if duration_minutes is None
            else started + float(duration_minutes) * 60
        )
        cycles = 0
        last: dict[str, object] = {}
        while deadline is None or float(self.clock()) < deadline:
            last = self.run_once(
                allow_network=True,
                notify_dry_run=notify_dry_run,
                with_ai=with_ai,
                send=send,
                confirm_real_send=confirm_real_send,
            )
            cycles += 1
            if deadline is not None and float(self.clock()) >= deadline:
                break
            delay = float(self.settings.oar_watch_live_poll_sec)
            if deadline is not None:
                delay = min(delay, max(0.0, deadline - float(self.clock())))
            if delay > 0:
                self.sleeper(delay)
        return {
            "status": "ok",
            "cycles": cycles,
            "last_cycle": last,
            "network_activity": bool(cycles),
            "database_writes": bool(cycles),
            "telegram_calls": bool(last.get("telegram_calls", False)),
            "ai_calls": bool(last.get("ai_calls", False)),
        }

    @staticmethod
    def _gate_failure(reason: str) -> dict[str, object]:
        return {
            "status": "failed",
            "reason": reason,
            "network_activity": False,
            "database_writes": False,
            "telegram_calls": False,
            "ai_calls": False,
        }
