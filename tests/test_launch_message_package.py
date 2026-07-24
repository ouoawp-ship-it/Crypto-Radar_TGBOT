from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from paopao_radar.cli import push_launch_messages
from paopao_radar.config import Settings
from paopao_radar.telegram import PushResult, plain_fallback


class FakeEngine:
    def __init__(self, events: list[str], *, commit_status: str = "committed") -> None:
        self.events = events
        self.commit_status = commit_status

    def pending_launch_package_cleanups(self, *, limit: int) -> list[dict[str, object]]:
        self.events.append(f"pending:{limit}")
        return []

    def commit_launch_package(
        self,
        _alert: dict[str, object],
        message_ids: list[int],
    ) -> dict[str, object]:
        self.events.append("commit")
        self.events.append(f"commit_ids:{message_ids}")
        return {
            "status": self.commit_status,
            "cycle_id": 7,
            "delete_message_ids": [101],
        }

    def complete_launch_package_cleanup(
        self,
        *,
        cycle_id: int,
        deleted_ids: list[int],
        failed_ids: list[int],
    ) -> dict[str, object]:
        self.events.append(
            f"complete:{cycle_id}:{deleted_ids}:{failed_ids}"
        )
        return {"status": "complete"}

    def mark_launch_pushed(self, _alerts: list[dict[str, object]]) -> None:
        self.events.append("mark")


class FakeGateway:
    def __init__(
        self,
        events: list[str],
        result: PushResult,
        *,
        photo_result: PushResult | None = None,
    ) -> None:
        self.events = events
        self.result = result
        self.photo_result = photo_result or PushResult(
            "sent",
            "telegram_photo_api",
            True,
            [202],
        )
        self.send_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def send(self, *args: object, **kwargs: object) -> PushResult:
        self.send_calls.append((args, kwargs))
        if kwargs.get("photo") is not None:
            self.events.append("photo")
            return self.photo_result
        self.events.append("send")
        return self.result

    def delete_messages_detailed(
        self,
        message_ids: list[int],
        *,
        reason: str = "",
    ) -> dict[str, list[int]]:
        self.events.append(f"reason:{reason}")
        self.events.append(f"delete:{message_ids}")
        return {"deleted_ids": message_ids, "failed_ids": []}


def launch_payload() -> dict[str, object]:
    alert = {
        "symbol": "TESTUSDT",
        "stage": "breakout",
        "launch_message_package_v2": True,
        "launch_lifecycle": {
            "cycle_id": 7,
            "observation_id": 12,
        },
        "launch_package": {
            "checkpoint_reasons": ["stage_changed"],
        },
    }
    return {"messages": ["message"], "alerts": [alert]}


class LaunchMessagePackageTests(unittest.TestCase):
    def test_new_message_is_committed_before_old_message_is_deleted(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            pushes, cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(events, PushResult("sent", "telegram_api", True, [201])),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertLess(events.index("send"), events.index("commit"))
            self.assertLess(events.index("commit"), events.index("delete:[101]"))
            self.assertIn("complete:7:[101]:[]", events)
            self.assertEqual(pushes[0]["status"], "sent")
            self.assertEqual(cleanup["deleted_messages"], 1)

    def test_send_failure_never_commits_or_deletes_old_package(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(events, PushResult("failed", "telegram_api_failed")),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertIn("send", events)
            self.assertNotIn("commit", events)
            self.assertFalse(any(event.startswith("delete:") for event in events))
            self.assertEqual(pushes[0]["status"], "failed")

    def test_partial_send_is_rolled_back_without_touching_old_package(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(
                    events,
                    PushResult("failed", "telegram_api_failed", False, [201]),
                ),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertNotIn("commit", events)
            self.assertIn("delete:[201]", events)
            self.assertNotIn("delete:[101]", events)
            self.assertEqual(pushes[0]["rollback_deleted"], 1)

    def test_commit_failure_rolls_back_new_message_and_keeps_old_package(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
            )
            pushes, _cleanup = push_launch_messages(
                settings,
                FakeEngine(events, commit_status="rejected"),  # type: ignore[arg-type]
                FakeGateway(events, PushResult("sent", "telegram_api", True, [201])),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertIn("commit", events)
            self.assertIn("delete:[201]", events)
            self.assertNotIn("delete:[101]", events)
            self.assertEqual(pushes[0]["status"], "package_commit_failed")

    def test_chart_and_text_are_committed_as_one_photo_caption_message(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_chart_v2_enable=True,
            )
            payload = launch_payload()
            payload["alerts"][0]["chart_png_bytes"] = b"\x89PNG\r\n\x1a\nchart"  # type: ignore[index]
            gateway = FakeGateway(
                events,
                PushResult("sent", "telegram_api", True, [201]),
                photo_result=PushResult(
                    "sent",
                    "telegram_photo_api",
                    True,
                    [202],
                ),
            )
            pushes, cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                gateway,  # type: ignore[arg-type]
                payload,
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertLess(events.index("photo"), events.index("commit"))
            self.assertNotIn("send", events)
            self.assertIn("commit_ids:[202]", events)
            self.assertEqual(pushes[0]["status"], "sent")
            self.assertEqual(cleanup["charts_sent"], 1)
            self.assertNotIn("chart_png_bytes", payload["alerts"][0])  # type: ignore[index]
            text = str(gateway.send_calls[0][0][0])
            kwargs = gateway.send_calls[0][1]
            self.assertLessEqual(len(plain_fallback(text)), 1024)
            self.assertEqual(kwargs["photo"], b"\x89PNG\r\n\x1a\nchart")
            self.assertFalse(kwargs["enrich_market_context"])

    def test_photo_failure_retains_old_package_without_sending_separate_text(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_chart_v2_enable=True,
            )
            payload = launch_payload()
            payload["alerts"][0]["chart_png_bytes"] = b"\x89PNG\r\n\x1a\nchart"  # type: ignore[index]
            pushes, cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(
                    events,
                    PushResult("sent", "telegram_api", True, [201]),
                    photo_result=PushResult(
                        "failed",
                        "telegram_photo_api_failed",
                        False,
                        [],
                    ),
                ),  # type: ignore[arg-type]
                payload,
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertNotIn("commit", events)
            self.assertNotIn("send", events)
            self.assertIn("photo", events)
            self.assertNotIn("delete:[201]", events)
            self.assertNotIn("delete:[101]", events)
            self.assertEqual(pushes[0]["status"], "failed")
            self.assertEqual(cleanup["chart_failures"], 1)
            self.assertNotIn("chart_png_bytes", payload["alerts"][0])  # type: ignore[index]

    def test_missing_chart_skips_package_without_sending_or_deleting(self) -> None:
        with TemporaryDirectory() as tmp:
            events: list[str] = []
            settings = Settings(
                data_dir=Path(tmp),
                launch_message_package_v2_enable=True,
                launch_chart_v2_enable=True,
            )
            pushes, cleanup = push_launch_messages(
                settings,
                FakeEngine(events),  # type: ignore[arg-type]
                FakeGateway(events, PushResult("sent", "telegram_api", True, [201])),  # type: ignore[arg-type]
                launch_payload(),
                SimpleNamespace(send=True, confirm_real_send=True),
            )

            self.assertNotIn("send", events)
            self.assertNotIn("commit", events)
            self.assertFalse(any(event.startswith("delete:") for event in events))
            self.assertEqual(pushes[0]["status"], "skipped")
            self.assertEqual(cleanup["chart_failures"], 1)


if __name__ == "__main__":
    unittest.main()
