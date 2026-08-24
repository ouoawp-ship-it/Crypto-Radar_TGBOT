from __future__ import annotations

import json
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import runtime.private_alerts as private_alerts
from runtime.private_alerts import PrivateAlertEvaluator


class CapturingSender:
    def __init__(self, *outcomes: object) -> None:
        self.outcomes = list(outcomes) or [True]
        self.messages: list[str] = []

    def __call__(self, message: str) -> bool:
        self.messages.append(message)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome is True


def radar_payload(
    *,
    status: str = "running",
    real_send: bool = False,
    radars: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "status": status,
        "real_send": real_send,
        "radars": radars or {},
    }


class PrivateAlertEvaluatorTests(unittest.TestCase):
    def evaluator(
        self,
        root: Path,
        *,
        sender: CapturingSender,
        clock=lambda: 1_000.0,
        radar: object | None = None,
        data: object | None = None,
        quota: object | None = None,
        enabled: bool = True,
        cooldown_sec: int = 100,
        failure_backoff_sec: int = 10,
    ) -> PrivateAlertEvaluator:
        return PrivateAlertEvaluator(
            enabled=enabled,
            state_path=root / "private_alert_state.json",
            sender=sender,
            radar_status_reader=(
                radar if callable(radar) else lambda: radar or radar_payload()
            ),
            data_freshness_reader=(
                data if callable(data) else lambda: data or {"checks": []}
            ),
            delivery_quota_reader=(
                quota if callable(quota) else lambda: quota or {}
            ),
            clock=clock,
            cooldown_sec=cooldown_sec,
            failure_backoff_sec=failure_backoff_sec,
        )

    def test_disabled_is_offline_and_does_not_touch_readers_or_state(self) -> None:
        calls: list[str] = []

        def reader() -> dict[str, object]:
            calls.append("reader")
            return {}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sender = CapturingSender()
            evaluator = PrivateAlertEvaluator(
                state_path=root / "state.json",
                sender=sender,
                radar_status_reader=reader,
                data_freshness_reader=reader,
                delivery_quota_reader=reader,
            )
            result = evaluator.run_once()

            self.assertFalse((root / "state.json").exists())

        self.assertEqual(result["status"], "disabled")
        self.assertEqual(result["sender_calls"], 0)
        self.assertEqual(calls, [])
        self.assertEqual(sender.messages, [])

    def test_fixed_events_are_merged_into_one_sanitized_chinese_alert(self) -> None:
        secret = "999999:secret-token https://private.invalid/data"
        radar = radar_payload(
            real_send=True,
            radars={
                "launch_alert": {
                    "state": "degraded",
                    "last_error_code": secret,
                    "chat_id": 987654321,
                },
                "flow_radar": {"state": "stale", "detail": secret},
                "unknown_radar": {"state": "failed", "detail": secret},
            },
        )
        data = {
            "checks": [
                {
                    "name": "market_snapshots_freshness",
                    "status": "fail",
                    "detail": secret,
                    "path": "/private/database.db",
                },
                {
                    "name": "unknown_private_check",
                    "status": "critical",
                    "detail": secret,
                },
            ]
        }
        with TemporaryDirectory() as tmp:
            sender = CapturingSender(True)
            result = self.evaluator(
                Path(tmp),
                sender=sender,
                radar=radar,
                data=data,
                quota={"limit": 20, "used": 20, "private": secret},
            ).run_once()

        self.assertEqual(result["status"], "sent")
        self.assertEqual(result["active_incidents"], 4)
        self.assertEqual(result["due_incidents"], 4)
        self.assertEqual(result["sender_calls"], 1)
        self.assertEqual(len(sender.messages), 1)
        message = sender.messages[0]
        for expected in (
            "泡泡雷达主动故障提醒",
            "脉冲雷达：运行异常或计划周期逾期",
            "五因子资金流：运行异常或计划周期逾期",
            "市场快照：数据过期或不可用",
            "真实推送额度：最近一小时额度已用完",
        ):
            self.assertIn(expected, message)
        self.assertNotIn(secret, message)
        self.assertNotIn("987654321", message)
        self.assertNotIn("unknown_private_check", message)
        self.assertNotIn("/private/database.db", message)
        self.assertLessEqual(len(message), 3900)

    def test_runtime_failure_is_one_event_and_suppresses_radar_fanout(self) -> None:
        radar = radar_payload(
            status="stale",
            real_send=True,
            radars={
                key: {"state": "degraded"}
                for key in (
                    "launch_alert",
                    "radar_summary",
                    "funding_alert",
                    "flow_radar",
                    "announcement_risk",
                )
            },
        )
        with TemporaryDirectory() as tmp:
            sender = CapturingSender(True)
            result = self.evaluator(
                Path(tmp),
                sender=sender,
                radar=radar,
                quota={"limit": 0, "used": 0},
            ).run_once()

        self.assertEqual(result["active_incidents"], 1)
        self.assertIn("主 BOT：未运行或心跳已过期", sender.messages[0])
        self.assertNotIn("脉冲雷达：", sender.messages[0])
        self.assertNotIn("真实推送额度", sender.messages[0])

    def test_warning_disabled_waiting_and_unknown_events_are_ignored(self) -> None:
        radar = radar_payload(radars={
            "launch_alert": {"state": "disabled"},
            "radar_summary": {"state": "waiting_first_cycle"},
            "funding_alert": {"state": "running"},
            "unlisted": {"state": "failed"},
        })
        data = {"checks": [
            {"name": "market_snapshots_freshness", "status": "warn"},
            {"name": "disk_space", "status": "fail"},
        ]}
        with TemporaryDirectory() as tmp:
            sender = CapturingSender()
            result = self.evaluator(
                Path(tmp),
                sender=sender,
                radar=radar,
                data=data,
                quota={"limit": 20, "used": 20},
            ).run_once()

        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["active_incidents"], 0)
        self.assertEqual(sender.messages, [])

    def test_cooldown_is_persistent_across_evaluator_instances(self) -> None:
        now = [1_000.0]
        radar = radar_payload(radars={
            "funding_alert": {"state": "degraded"},
        })
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sender = CapturingSender(True, True)
            first = self.evaluator(
                root,
                sender=sender,
                radar=radar,
                clock=lambda: now[0],
            ).run_once()
            now[0] += 50
            second = self.evaluator(
                root,
                sender=sender,
                radar=radar,
                clock=lambda: now[0],
            ).run_once()
            now[0] += 50
            third = self.evaluator(
                root,
                sender=sender,
                radar=radar,
                clock=lambda: now[0],
            ).run_once()

            state_path = root / "private_alert_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            temporary_files = list(root.glob("private_alert_state.json.tmp.*"))
            mode = stat.S_IMODE(state_path.stat().st_mode)

        self.assertEqual(first["status"], "sent")
        self.assertEqual(second["status"], "suppressed")
        self.assertEqual(third["status"], "sent")
        self.assertEqual(len(sender.messages), 2)
        self.assertEqual(state["schema_version"], 1)
        self.assertEqual(
            set(state["incidents"]),
            {"radar:funding_alert"},
        )
        self.assertEqual(temporary_files, [])
        if os.name == "posix":
            self.assertEqual(mode, 0o600)

    def test_resolution_does_not_bypass_cooldown_on_quick_recurrence(self) -> None:
        now = [1_000.0]
        state = ["degraded"]

        def radar() -> dict[str, object]:
            return radar_payload(radars={
                "radar_summary": {"state": state[0]},
            })

        with TemporaryDirectory() as tmp:
            sender = CapturingSender(True, True)
            evaluator = self.evaluator(
                Path(tmp),
                sender=sender,
                radar=radar,
                clock=lambda: now[0],
            )
            first = evaluator.run_once()
            now[0] = 1_020
            state[0] = "running"
            resolved = evaluator.run_once()
            now[0] = 1_030
            state[0] = "degraded"
            recurrence = evaluator.run_once()
            now[0] = 1_100
            after_cooldown = evaluator.run_once()

        self.assertEqual(first["status"], "sent")
        self.assertEqual(resolved["status"], "idle")
        self.assertEqual(recurrence["status"], "suppressed")
        self.assertEqual(after_cooldown["status"], "sent")
        self.assertEqual(len(sender.messages), 2)

    def test_failed_send_uses_short_backoff_without_leaking_exception(self) -> None:
        now = [1_000.0]
        secret = "secret provider response and private identifier 123456"
        radar = radar_payload(radars={
            "announcement_risk": {"state": "failed"},
        })
        with TemporaryDirectory() as tmp:
            sender = CapturingSender(RuntimeError(secret), True)
            evaluator = self.evaluator(
                Path(tmp),
                sender=sender,
                radar=radar,
                clock=lambda: now[0],
            )
            failed = evaluator.run_once()
            now[0] = 1_009
            backed_off = evaluator.run_once()
            now[0] = 1_010
            retried = evaluator.run_once()

        self.assertEqual(failed["status"], "send_failed")
        self.assertEqual(backed_off["status"], "suppressed")
        self.assertEqual(retried["status"], "sent")
        self.assertEqual(len(sender.messages), 2)
        self.assertNotIn(secret, json.dumps(failed))

    def test_state_write_failure_is_fail_closed_with_zero_sends(self) -> None:
        secret = "private state path and secret payload"
        radar = radar_payload(radars={
            "launch_alert": {"state": "stale"},
        })
        with TemporaryDirectory() as tmp:
            sender = CapturingSender(True)
            evaluator = self.evaluator(
                Path(tmp),
                sender=sender,
                radar=radar,
            )
            with patch(
                "runtime.private_alerts.locked_update_json",
                side_effect=OSError(secret),
            ):
                result = evaluator.run_once()

        self.assertEqual(result["status"], "state_unavailable")
        self.assertEqual(result["sender_calls"], 0)
        self.assertEqual(sender.messages, [])
        self.assertNotIn(secret, json.dumps(result))

    def test_success_state_write_failure_keeps_reserved_full_cooldown(self) -> None:
        now = [1_000.0]
        radar = radar_payload(radars={
            "launch_alert": {"state": "degraded"},
        })
        real_update = private_alerts.locked_update_json
        calls = [0]

        def fail_second_write(*args: object, **kwargs: object) -> object:
            calls[0] += 1
            if calls[0] == 2:
                raise OSError("private post-send state failure")
            return real_update(*args, **kwargs)

        with TemporaryDirectory() as tmp:
            sender = CapturingSender(True, True)
            evaluator = self.evaluator(
                Path(tmp),
                sender=sender,
                radar=radar,
                clock=lambda: now[0],
            )
            with patch(
                "runtime.private_alerts.locked_update_json",
                side_effect=fail_second_write,
            ):
                first = evaluator.run_once()
            now[0] = 1_010
            immediate_retry = evaluator.run_once()

        self.assertEqual(first["status"], "sent_state_unavailable")
        self.assertEqual(immediate_retry["status"], "suppressed")
        self.assertEqual(len(sender.messages), 1)

    def test_failed_reader_is_sanitized_and_does_not_resolve_old_incident(self) -> None:
        now = [1_000.0]
        secret = "token=private and /secret/path"
        reader_state = ["failed"]

        def radar() -> dict[str, object]:
            if reader_state[0] == "failed":
                raise RuntimeError(secret)
            return radar_payload(radars={
                "flow_radar": {"state": "degraded"},
            })

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            sender = CapturingSender(True, True)
            healthy_reader = radar_payload(radars={
                "flow_radar": {"state": "degraded"},
            })
            first = self.evaluator(
                root,
                sender=sender,
                radar=healthy_reader,
                clock=lambda: now[0],
            ).run_once()
            now[0] = 1_100
            failed = self.evaluator(
                root,
                sender=sender,
                radar=radar,
                clock=lambda: now[0],
            ).run_once()
            reader_state[0] = "recovered"
            recovered = self.evaluator(
                root,
                sender=sender,
                radar=radar,
                clock=lambda: now[0],
            ).run_once()

        self.assertEqual(first["status"], "sent")
        self.assertEqual(failed["status"], "idle")
        self.assertEqual(failed["reader_failures"], ("radar_status",))
        self.assertEqual(recovered["status"], "sent")
        self.assertEqual(len(sender.messages), 2)
        self.assertNotIn(secret, json.dumps(failed))

    def test_quota_alert_requires_real_active_runtime(self) -> None:
        quota = {"limit": 20, "used": 20, "remaining": 0}
        cases = (
            (radar_payload(status="running", real_send=False), "idle"),
            (radar_payload(status="not_running", real_send=True), "sent"),
            (radar_payload(status="running", real_send=True), "sent"),
        )
        for index, (radar, expected) in enumerate(cases):
            with self.subTest(index=index), TemporaryDirectory() as tmp:
                sender = CapturingSender(True)
                result = self.evaluator(
                    Path(tmp),
                    sender=sender,
                    radar=radar,
                    quota=quota,
                ).run_once()

                self.assertEqual(result["status"], expected)
                message = sender.messages[0] if sender.messages else ""
                if index == 2:
                    self.assertIn("真实推送额度", message)
                else:
                    self.assertNotIn("真实推送额度", message)


if __name__ == "__main__":
    unittest.main()
