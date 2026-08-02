from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock, patch

from paopao_radar.onchain_flow.automation_store import AutomationStore
from paopao_radar.onchain_flow.token_analysis import TokenAnalysisService
from paopao_radar.onchain_flow.watch_scanner import WatchScanner
from paopao_radar.telegram import PushResult

from tests.onchain_flow.analysis_support import fixture_case
from tests.onchain_flow.support import make_settings


CONTRACT_A = "0x1111111111111111111111111111111111111111"
CONTRACT_B = "0x2222222222222222222222222222222222222222"


class StaticActivity:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls = 0

    def execute(self, query: object) -> dict[str, object]:
        del query
        self.calls += 1
        return deepcopy(self.payload)


class StaticAnalysis:
    def __init__(self, payload: dict[str, object]):
        self.payload = payload
        self.calls = 0

    def execute(self, query: object) -> dict[str, object]:
        del query
        self.calls += 1
        return deepcopy(self.payload)


class NoopBridge:
    def __init__(self):
        self.calls = 0

    def run_once(self) -> dict[str, object]:
        self.calls += 1
        return {"source_status": "source_not_initialized"}


class FakeReport:
    def __init__(self):
        self.calls = 0
        self.linked: list[dict[str, object]] = []

    def build_from_analysis(
        self,
        payload: dict[str, object],
        *,
        with_ai: bool,
        linked_market_signals: list[dict[str, object]],
    ) -> dict[str, object]:
        self.calls += 1
        self.linked = deepcopy(linked_market_signals)
        result = deepcopy(payload)
        result["linked_market_signals"] = deepcopy(linked_market_signals)
        result["report"] = {
            "context_hash": "c" * 64,
            "content_hash": "d" * 64,
            "rule_summary_text": "safe",
            "ai": {
                "status": "available" if with_ai else "not_requested",
                "calls": 1 if with_ai else 0,
                "result": None,
            },
        }
        return result


class FakeNotifier:
    def __init__(self):
        self.calls: list[dict[str, object]] = []

    def notify(self, payload: dict[str, object], **kwargs: object) -> PushResult:
        self.calls.append({"payload": payload, **kwargs})
        return PushResult("dry_run", "dry_run", False, [])


class WatchScannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = make_settings(self.root)
        self.store = AutomationStore.from_settings(self.settings)
        self.bridge = NoopBridge()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def watch(
        self,
        contract: str = CONTRACT_A,
        symbol: str = "AAAUSDT",
        *,
        priority: int = 100,
    ) -> str:
        item = self.store.add_registry(
            market_symbol=symbol,
            contract=contract,
            source="manual",
            now=1000,
        )
        key = str(item["token_key"])
        self.store.verify_registry(
            key,
            token_symbol=symbol[:-4],
            token_name=symbol[:-4],
            decimals=18,
            metadata_hash="a" * 64,
            verification_method="fixture",
            set_primary=True,
            now=1001,
        )
        self.store.add_manual_watch(
            key,
            ttl_sec=100000,
            priority=priority,
            query_window="4h",
            scan_interval_sec=900,
            now=1002,
        )
        return key

    def analyzed(self, name: str) -> dict[str, object]:
        activity = fixture_case(name)
        activity["diagnostics"]["rpc_request_count"] = 7
        return TokenAnalysisService(
            self.settings, StaticActivity(activity)
        ).execute(object())

    def scanner(
        self,
        payloads: list[dict[str, object]],
        *,
        report: FakeReport | None = None,
        notifier: FakeNotifier | None = None,
        settings: object | None = None,
        order: list[str] | None = None,
        address_store: object | None = None,
    ) -> WatchScanner:
        queue = list(payloads)

        def analysis_factory(
            current_settings: object, query: object
        ) -> StaticAnalysis:
            del current_settings
            if order is not None:
                order.append(str(getattr(query, "contract")))
            return StaticAnalysis(queue.pop(0))

        return WatchScanner(
            settings or self.settings,
            self.store,
            bridge=self.bridge,
            analysis_factory=analysis_factory,
            report_factory=(
                (lambda current: report)
                if report is not None
                else lambda current: (_ for _ in ()).throw(
                    AssertionError("report created below gate")
                )
            ),
            notifier_factory=(
                (lambda current: notifier)
                if notifier is not None
                else lambda current: (_ for _ in ()).throw(
                    AssertionError("notifier created below gate")
                )
            ),
            address_intelligence_store=address_store,
            clock=lambda: 2000,
            sleeper=lambda value: None,
        )

    def test_watch_only_queues_locally_and_never_calls_providers(
        self,
    ) -> None:
        key = self.watch()
        queue_store = Mock()
        queue_store.observe_complete_scan.return_value = {
            "observed": 2,
            "created": 2,
            "updated": 0,
        }
        with patch(
            "paopao_radar.onchain_flow.address_intelligence."
            "AddressIntelligenceService.discover",
            side_effect=AssertionError("provider called from Watch"),
        ) as discover:
            result = self.scanner(
                [self.analyzed("isolated")],
                address_store=queue_store,
            ).run_once(
                allow_network=True,
                notify_dry_run=False,
                with_ai=False,
                send=False,
                confirm_real_send=False,
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["results"][0]["token_key"], key)
        self.assertEqual(
            result["results"][0]["external_label_provider_calls"], 0
        )
        self.assertEqual(
            result["results"][0]["unknown_addresses_queued"], 2
        )
        self.assertEqual(
            result["results"][0][
                "address_intelligence_queue_status"
            ],
            "ok",
        )
        self.assertEqual(
            result["results"][0][
                "address_intelligence_queue_error"
            ],
            "",
        )
        queue_store.observe_complete_scan.assert_called_once()
        discover.assert_not_called()

    def test_local_queue_error_is_observable_without_failing_scan(
        self,
    ) -> None:
        self.watch()
        queue_store = Mock()
        queue_store.observe_complete_scan.side_effect = OSError(
            "secret path must not leak"
        )
        result = self.scanner(
            [self.analyzed("isolated")],
            address_store=queue_store,
        ).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        item = result["results"][0]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(
            item["address_intelligence_queue_status"], "local_error"
        )
        self.assertEqual(
            item["address_intelligence_queue_error"],
            "address_intelligence_local_error",
        )
        self.assertNotIn("secret path", str(item))
        self.assertEqual(item["external_label_provider_calls"], 0)

    def test_complete_scans_build_and_persist_historical_baseline(self) -> None:
        key = self.watch()
        with self.store.connect() as conn:
            conn.executemany(
                """
                INSERT INTO watch_scan_runs(
                    scan_id, token_key, started_at, completed_at, status,
                    activity_complete, analysis_complete, query_window,
                    transfer_count, total_token_amount, unique_senders,
                    unique_receivers, behavior_score, max_wallet_group_score,
                    baseline_status, baseline_json
                ) VALUES(?, ?, ?, ?, 'ok', 1, 1, '4h', ?, ?, ?, ?, ?, ?, 'cold_start', ?)
                """,
                [
                    (
                        f"baseline-{index}", key, 1500 + index, 1500 + index,
                        2, "20", 2, 2, 10, 10,
                        json.dumps(
                            {
                                "windows": {
                                    name: {
                                        "current": {
                                            "transfer_count": "2",
                                            "total_token_amount": "20",
                                            "unique_senders": "2",
                                            "unique_receivers": "2",
                                            "inflow_count": "0",
                                            "outflow_count": "0",
                                            "unclassified_count": "2",
                                            "active_15m_buckets": "1",
                                        }
                                    }
                                    for name in ("15m", "1h")
                                }
                            }
                        ),
                    )
                    for index in range(4)
                ],
            )
            conn.commit()
        payload = self.analyzed("isolated")
        payload["summary"].update(
            {
                "transfer_count": 20,
                "total_token_amount": "200",
                "unique_senders": 20,
                "unique_receivers": 20,
            }
        )
        for name in ("15m", "1h"):
            payload["analysis"]["windows"][name].update(
                {
                    "transfer_count": 20,
                    "total_token_amount": "200",
                    "unique_senders": 20,
                    "unique_receivers": 20,
                    "unclassified_count": 20,
                    "active_15m_buckets": 2,
                }
            )
        settings = make_settings(
            self.root,
            oar_watch_baseline_min_samples=4,
            oar_watch_baseline_max_samples=8,
        )
        result = self.scanner([payload], settings=settings).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        baseline = result["results"][0]["historical_baseline"]
        self.assertEqual(baseline["status"], "ready")
        self.assertTrue(baseline["anomaly"])
        self.assertIn("transfer_count", baseline["anomalous_metrics"])
        self.assertEqual(
            baseline["anomalous_windows"], ["15m", "1h"]
        )
        self.assertTrue(baseline["multi_window_anomaly"])
        persisted = self.store.latest_scan_baseline(key) or {}
        self.assertEqual(persisted["baseline_status"], "ready")
        self.assertTrue(persisted["baseline_anomaly"])

    def test_incomplete_scan_never_claims_historical_anomaly(self) -> None:
        self.watch()
        payload = self.analyzed("partial_input")
        result = self.scanner([payload]).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        baseline = result["results"][0]["historical_baseline"]
        self.assertEqual(baseline["status"], "skipped_incomplete")
        self.assertFalse(baseline["anomaly"])
        self.assertEqual(baseline["windows"], {})

    def test_baseline_local_error_does_not_fail_watch_or_leak_exception(self) -> None:
        self.watch()
        with patch.object(
            self.store,
            "complete_scan_history",
            side_effect=OSError("secret local path"),
        ):
            result = self.scanner([self.analyzed("isolated")]).run_once(
                allow_network=True,
                notify_dry_run=False,
                with_ai=False,
                send=False,
                confirm_real_send=False,
            )
        item = result["results"][0]
        self.assertEqual(item["status"], "ok")
        self.assertEqual(
            item["historical_baseline"]["status"], "local_error"
        )
        self.assertNotIn("secret local path", str(item))

    def test_incomplete_scan_skips_local_queue(self) -> None:
        self.watch()
        queue_store = Mock()
        payload = self.analyzed("isolated")
        payload["complete"] = False
        payload["analysis"]["complete"] = False
        result = self.scanner(
            [payload],
            address_store=queue_store,
        ).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        item = result["results"][0]
        self.assertEqual(
            item["address_intelligence_queue_status"],
            "skipped_incomplete",
        )
        self.assertEqual(item["address_intelligence_queue_error"], "")
        queue_store.observe_complete_scan.assert_not_called()

    def test_no_network_gate_has_zero_writes_and_factories(self) -> None:
        self.watch()
        before = self.store.path.read_bytes()
        result = self.scanner([]).run_once(
            allow_network=False,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["reason"], "allow_network_required")
        self.assertFalse(result["network_activity"])
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(self.bridge.calls, 0)

    def test_only_due_tokens_are_scanned_in_priority_order(self) -> None:
        first = self.watch(CONTRACT_A, "AAAUSDT", priority=70)
        second = self.watch(CONTRACT_B, "BBBUSDT", priority=90)
        order: list[str] = []
        result = self.scanner(
            [self.analyzed("isolated"), self.analyzed("isolated")],
            order=order,
        ).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["scanned_tokens"], 2)
        self.assertEqual(order, [CONTRACT_B, CONTRACT_A])
        self.assertEqual(self.store.get_watch(first)["last_scan_status"], "ok")
        self.assertEqual(self.store.get_watch(second)["last_scan_status"], "ok")

    def test_cycle_token_limit_is_enforced(self) -> None:
        self.watch(CONTRACT_A, "AAAUSDT")
        self.watch(CONTRACT_B, "BBBUSDT")
        settings = make_settings(
            self.root, oar_watch_max_tokens_per_cycle=1
        )
        result = self.scanner(
            [self.analyzed("isolated")], settings=settings
        ).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["claimed_tokens"], 1)

    def test_non_actionable_results_do_not_create_report_ai_or_gateway(self) -> None:
        for name in (
            "no_activity",
            "isolated",
            "mixed_opposing_flows",
            "partial_input",
        ):
            with self.subTest(name=name):
                self.store.path.unlink(missing_ok=True)
                self.store = AutomationStore.from_settings(self.settings)
                self.watch()
                result = self.scanner([self.analyzed(name)]).run_once(
                    allow_network=True,
                    notify_dry_run=True,
                    with_ai=True,
                    send=False,
                    confirm_real_send=False,
                )
                self.assertEqual(result["notifications_attempted"], 0)
                self.assertFalse(result["ai_calls"])
                self.assertFalse(result["telegram_calls"])

    def test_actionable_behavior_uses_existing_report_and_notifier_once(self) -> None:
        key = self.watch()
        signal = {
            "id": 1,
            "public_ref": "launch:1",
            "ts": 1900,
            "module": "launch",
            "symbol": "AAAUSDT",
            "score": 82,
            "stage": "active",
            "severity": "high",
            "excerpt": "market source",
            "payload_hash": "f" * 64,
        }
        self.store.process_bridge_signal(
            signal,
            resolution={
                "status": "resolved",
                "token": self.store.get_registry(key),
            },
            source_ttl_sec=3600,
            source_priority=90,
            query_window="4h",
            scan_interval_sec=900,
            max_active_tokens=50,
            now=1950,
        )
        report = FakeReport()
        notifier = FakeNotifier()
        result = self.scanner(
            [self.analyzed("accumulation")],
            report=report,
            notifier=notifier,
        ).run_once(
            allow_network=True,
            notify_dry_run=True,
            with_ai=True,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["actionable_tokens"], 1)
        self.assertEqual(report.calls, 1)
        self.assertEqual(len(notifier.calls), 1)
        self.assertEqual(report.linked[0]["public_ref"], "launch:1")
        self.assertEqual(
            notifier.calls[0]["source"], "watchlist_automation"
        )
        self.assertEqual(
            notifier.calls[0]["linked_source_refs"], ["launch:1"]
        )
        self.assertTrue(result["ai_calls"])

    def test_actionable_observe_builds_report_without_gateway(self) -> None:
        self.watch()
        report = FakeReport()
        result = self.scanner(
            [self.analyzed("distribution")],
            report=report,
        ).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["actionable_tokens"], 1)
        self.assertEqual(result["notifications_attempted"], 0)
        self.assertEqual(report.calls, 1)
        self.assertFalse(result["ai_calls"])

    def test_explicit_ai_runs_only_after_gate_in_observe_mode(self) -> None:
        self.watch()
        report = FakeReport()
        result = self.scanner(
            [self.analyzed("distribution")],
            report=report,
        ).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=True,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(report.calls, 1)
        self.assertTrue(result["ai_calls"])
        self.assertEqual(result["notifications_attempted"], 0)

    def test_partial_never_notifies_and_is_audited(self) -> None:
        key = self.watch()
        result = self.scanner(
            [self.analyzed("partial_input")]
        ).run_once(
            allow_network=True,
            notify_dry_run=True,
            with_ai=True,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["partial_tokens"], 1)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["notifications_attempted"], 0)
        self.assertEqual(self.store.get_watch(key)["last_scan_status"], "partial")
        self.assertEqual(self.store.get_watch(key)["consecutive_failures"], 1)

    def test_rpc_query_is_not_repeated_for_report(self) -> None:
        self.watch()
        analyzed = self.analyzed("accumulation")
        static = StaticAnalysis(analyzed)
        report = FakeReport()
        notifier = FakeNotifier()
        scanner = WatchScanner(
            self.settings,
            self.store,
            bridge=self.bridge,
            analysis_factory=lambda settings, query: static,
            report_factory=lambda settings: report,
            notifier_factory=lambda settings: notifier,
            clock=lambda: 2000,
        )
        scanner.run_once(
            allow_network=True,
            notify_dry_run=True,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(static.calls, 1)
        self.assertEqual(report.calls, 1)

    def test_wallet_score_can_open_notification_gate(self) -> None:
        self.watch()
        analyzed = self.analyzed("synchronized_cex_cohort")
        analysis = analyzed["analysis"]
        analysis["primary_behavior"]["type"] = "inconclusive_activity"
        analysis["primary_behavior"]["score"] = 0
        analysis["status"] = "ok"
        analysis["complete"] = True
        analysis["wallet_groups"][0]["score"] = 60
        report = FakeReport()
        notifier = FakeNotifier()
        result = self.scanner(
            [analyzed], report=report, notifier=notifier
        ).run_once(
            allow_network=True,
            notify_dry_run=True,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["actionable_tokens"], 1)

    def test_scan_failure_releases_lease_and_continues(self) -> None:
        first = self.watch(CONTRACT_A, "AAAUSDT", priority=100)
        second = self.watch(CONTRACT_B, "BBBUSDT", priority=90)

        class FailingAnalysis:
            def execute(self, query: object) -> dict[str, object]:
                del query
                raise RuntimeError("fixture")

        queue: list[object] = [
            FailingAnalysis(),
            StaticAnalysis(self.analyzed("isolated")),
        ]
        scanner = WatchScanner(
            self.settings,
            self.store,
            bridge=self.bridge,
            analysis_factory=lambda settings, query: queue.pop(0),
            report_factory=lambda settings: Mock(),
            notifier_factory=lambda settings: Mock(),
            clock=lambda: 2000,
        )
        result = scanner.run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["scanned_tokens"], 2)
        self.assertEqual(result["failed_tokens"], 1)
        self.assertEqual(self.store.get_watch(first)["lease_owner"], "")
        self.assertEqual(self.store.get_watch(second)["last_scan_status"], "ok")

    def test_failed_token_reserves_budget_before_next_token(self) -> None:
        self.watch(CONTRACT_A, "AAAUSDT", priority=100)
        self.watch(CONTRACT_B, "BBBUSDT", priority=90)
        calls = 0

        class FailingAnalysis:
            def execute(self, query: object) -> dict[str, object]:
                del query
                raise RuntimeError("fixture")

        def factory(settings: object, query: object) -> FailingAnalysis:
            nonlocal calls
            del settings, query
            calls += 1
            return FailingAnalysis()

        settings = make_settings(
            self.root,
            oar_watch_max_rpc_requests_per_token=100,
            oar_watch_max_rpc_requests_per_cycle=100,
        )
        scanner = WatchScanner(
            settings,
            self.store,
            bridge=self.bridge,
            analysis_factory=factory,
            report_factory=lambda settings: Mock(),
            notifier_factory=lambda settings: Mock(),
            clock=lambda: 2000,
        )
        result = scanner.run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(calls, 1)
        self.assertEqual(result["claimed_tokens"], 1)
        self.assertEqual(result["scanned_tokens"], 1)
        self.assertEqual(result["deferred_tokens"], 1)
        self.assertEqual(result["failed_tokens"], 1)
        self.assertEqual(result["partial_tokens"], 0)
        self.assertEqual(result["rpc_budget_consumed"], 100)
        self.assertLessEqual(
            result["rpc_budget_consumed"],
            settings.oar_watch_max_rpc_requests_per_cycle,
        )

    def test_cycle_budget_defers_fifth_token_without_failure(self) -> None:
        keys = [
            self.watch(
                f"0x{index:040x}",
                f"T{index}USDT",
                priority=100 - index,
            )
            for index in range(1, 6)
        ]
        payloads = []
        for _ in range(4):
            payload = self.analyzed("isolated")
            payload["diagnostics"]["rpc_request_count"] = 100
            payloads.append(payload)
        settings = make_settings(
            self.root,
            oar_watch_max_tokens_per_cycle=5,
            oar_watch_max_rpc_requests_per_token=100,
            oar_watch_max_rpc_requests_per_cycle=400,
        )
        result = self.scanner(payloads, settings=settings).run_once(
            allow_network=True,
            notify_dry_run=True,
            with_ai=True,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["claimed_tokens"], 4)
        self.assertEqual(result["scanned_tokens"], 4)
        self.assertEqual(result["deferred_tokens"], 1)
        self.assertEqual(result["deferred_token_keys"], [keys[4]])
        self.assertTrue(result["cycle_budget_exhausted"])
        self.assertEqual(result["failed_tokens"], 0)
        self.assertEqual(result["partial_tokens"], 0)
        self.assertEqual(result["rpc_budget_consumed"], 400)
        self.assertEqual(result["notifications_attempted"], 0)
        self.assertFalse(result["telegram_calls"])
        self.assertFalse(result["ai_calls"])
        deferred = self.store.get_watch(keys[4]) or {}
        self.assertEqual(deferred["status"], "active")
        self.assertEqual(deferred["lease_owner"], "")
        self.assertEqual(deferred["consecutive_failures"], 0)
        self.assertEqual(deferred["last_error_code"], "")
        self.assertEqual(deferred["next_scan_at"], 1002)
        next_payload = self.analyzed("isolated")
        next_payload["diagnostics"]["rpc_request_count"] = 7
        next_cycle = self.scanner(
            [next_payload], settings=settings
        ).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(next_cycle["scanned_tokens"], 1)
        self.assertEqual(
            (self.store.get_watch(keys[4]) or {})["last_scan_status"],
            "ok",
        )

    def test_stale_worker_cannot_report_notify_or_overwrite_new_owner(self) -> None:
        key = self.watch()
        analyzed = self.analyzed("accumulation")
        report = FakeReport()
        notifier = FakeNotifier()
        lease_sec = self.settings.oar_watch_lease_sec

        class StealingAnalysis:
            def execute(inner_self, query: object) -> dict[str, object]:
                del inner_self, query
                claimed = self.store.claim_due(
                    owner="worker-b",
                    limit=1,
                    lease_sec=lease_sec,
                    now=2000 + lease_sec + 1,
                )
                self.assertEqual(len(claimed), 1)
                return deepcopy(analyzed)

        scanner = WatchScanner(
            self.settings,
            self.store,
            bridge=self.bridge,
            analysis_factory=lambda settings, query: StealingAnalysis(),
            report_factory=lambda settings: report,
            notifier_factory=lambda settings: notifier,
            clock=lambda: 2000,
        )
        result = scanner.run_once(
            allow_network=True,
            notify_dry_run=True,
            with_ai=True,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["stale_tokens"], 1)
        self.assertEqual(result["results"][0]["error"], "lease_lost")
        self.assertEqual(report.calls, 0)
        self.assertEqual(len(notifier.calls), 0)
        watch = self.store.get_watch(key) or {}
        self.assertEqual(watch["lease_owner"], "worker-b")
        self.assertEqual(watch["consecutive_failures"], 0)

    def test_linked_signal_order_is_deterministic_and_bounded(self) -> None:
        sources = [
            {
                "source_public_ref": f"ref-{index}",
                "source_module": "flow",
                "source_symbol": "AAAUSDT",
                "source_score": index,
                "source_stage": "",
                "source_severity": "",
                "source_ts": 1000 + index,
                "source_summary": "x" * 400,
                "source_priority": index,
            }
            for index in range(12)
        ]
        linked = WatchScanner._linked_market_signals(
            list(reversed(sources)), now=2000
        )
        self.assertEqual(len(linked), 10)
        self.assertEqual(linked[0]["public_ref"], "ref-11")
        self.assertEqual(len(linked[0]["summary"]), 300)

    def test_automation_disabled_live_has_zero_side_effects(self) -> None:
        scanner = self.scanner([])
        result = scanner.run_live(
            allow_network=True,
            duration_minutes=0,
            notify_dry_run=False,
            with_ai=False,
            send=False,
            confirm_real_send=False,
        )
        self.assertEqual(result["reason"], "automation_disabled")
        self.assertFalse(result["network_activity"])
        self.assertEqual(self.bridge.calls, 0)

    def test_live_rejects_dry_run_and_send_together_before_cycles(self) -> None:
        settings = make_settings(self.root, oar_automation_enable=True)
        scanner = self.scanner([], settings=settings)
        result = scanner.run_live(
            allow_network=True,
            duration_minutes=1,
            notify_dry_run=True,
            with_ai=False,
            send=True,
            confirm_real_send=True,
        )
        self.assertEqual(result["reason"], "conflicting_notification_modes")
        self.assertFalse(result["network_activity"])
        self.assertEqual(self.bridge.calls, 0)

    def test_real_http_indicator_requires_all_three_send_gates(self) -> None:
        self.watch()
        report = FakeReport()
        notifier = FakeNotifier()
        result = self.scanner(
            [self.analyzed("accumulation")],
            report=report,
            notifier=notifier,
        ).run_once(
            allow_network=True,
            notify_dry_run=False,
            with_ai=False,
            send=True,
            confirm_real_send=True,
        )
        self.assertEqual(result["notifications_attempted"], 1)
        self.assertFalse(result["telegram_calls"])


if __name__ == "__main__":
    unittest.main()
