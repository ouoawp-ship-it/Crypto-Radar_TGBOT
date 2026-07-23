from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from time import time
from unittest.mock import patch

from paopao_radar.config import Settings
from paopao_radar.onchain_flow.db import OnchainStore
from paopao_radar.onchain_flow.models import OnchainAlert
from paopao_radar.onchain_flow.notifier import OnchainNotifier
from paopao_radar.storage import JsonStore
from paopao_radar.telegram import TelegramGateway, topic_intro_message

from .support import make_settings


def sample_alert() -> OnchainAlert:
    return OnchainAlert(
        alert_key=(
            "8453:0x9999999999999999999999999999999999999999:"
            "inflow:1700000000:900:p3.0-v1"
        ),
        chain_id=8453,
        token_address="0x9999999999999999999999999999999999999999",
        symbol="ABC",
        direction="inflow",
        score=-60,
        horizon="1h-4h",
        confidence="high",
        reasons=("流入交易所增加潜在可售供应",),
        detection_types=("batch_flow",),
        window_start=1700000000,
        window_end=1700000900,
        total_usd=Decimal("2500000"),
        tx_count=5,
        exchanges=("Binance", "OKX"),
        label_confidence=0.95,
        price_status="available",
        created_at=1700000900,
        severity_version="p3.0-v1",
    )


class OnchainNotifierTests(unittest.TestCase):
    def test_dry_run_has_no_network_and_uses_only_onchain_history(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(root, tg_hourly_limit=1)
            production_history_path = root / "data" / "tg_push_history.json"
            production_record = {
                "ts": int(time()),
                "status": "sent",
                "template_id": "TG_FLOW_RADAR",
            }
            JsonStore(root / "data").save(
                production_history_path,
                [production_record],
            )
            store = OnchainStore(settings)
            store.migrate()
            alert = sample_alert()
            store.sync_alerts([alert])
            notifier = OnchainNotifier(settings, store)

            with patch("paopao_radar.telegram.requests.post") as post_mock:
                result = notifier.notify(
                    alert,
                    send=False,
                    confirm_real_send=False,
                )

            self.assertEqual(result.status, "dry_run")
            post_mock.assert_not_called()
            self.assertTrue(settings.tg_push_history_path.exists())
            self.assertEqual(
                JsonStore(root / "data").load(production_history_path, []),
                [production_record],
            )
            self.assertFalse((root / "data" / "tg_outbox.json").exists())

    def test_real_send_requires_env_gate_and_both_cli_flags(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(
                root,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
            )
            store = OnchainStore(settings)
            store.migrate()
            alert = sample_alert()
            store.sync_alerts([alert])

            with patch("paopao_radar.telegram.requests.post") as post_mock:
                result = OnchainNotifier(settings, store).notify(
                    alert,
                    send=True,
                    confirm_real_send=True,
                )
            self.assertEqual(result.status, "dry_run")
            post_mock.assert_not_called()

            enabled = replace(settings, real_send=True)
            with patch("paopao_radar.telegram.requests.post") as post_mock:
                result = OnchainNotifier(enabled, store).notify(
                    alert,
                    send=True,
                    confirm_real_send=False,
                )
            self.assertEqual(result.status, "blocked")
            self.assertEqual(result.reason, "missing_confirm_real_send")
            post_mock.assert_not_called()

    def test_onchain_outbox_and_hourly_limit_are_independent(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = make_settings(
                root,
                real_send=True,
                tg_bot_token="123456:ABCDEFGHIJKLMNOPQRSTUVWXYZ",
                tg_chat_id="-1001234567890",
                tg_hourly_limit=6,
                tg_onchain_flow_topic_id="22",
            )
            store = OnchainStore(settings)
            store.migrate()
            alert = sample_alert()
            store.sync_alerts([alert])
            notifier = OnchainNotifier(settings, store)
            with (
                patch.object(
                    notifier.gateway,
                    "_send_real_message_ids",
                    return_value=(True, [123]),
                ),
                patch.object(notifier.gateway, "_ensure_topic_intro"),
            ):
                result = notifier.notify(
                    alert,
                    send=True,
                    confirm_real_send=True,
                )
            self.assertTrue(result.sent)
            self.assertEqual(notifier.gateway.settings.tg_global_hourly_limit, 6)
            self.assertTrue(settings.tg_outbox_path.exists())
            self.assertFalse((root / "data" / "tg_outbox.json").exists())

    def test_dedicated_topic_extension_keeps_existing_routes(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                tg_radar_summary_topic_id="11",
                tg_onchain_flow_topic_id="22",
            )
            gateway = TelegramGateway(settings, JsonStore(Path(tmp)))
            self.assertEqual(
                gateway._topic_id_for_template("TG_RADAR_SUMMARY"),
                "11",
            )
            self.assertEqual(
                gateway._topic_id_for_template("TG_ONCHAIN_FLOW_ALERT"),
                "22",
            )
            intro = topic_intro_message("TG_ONCHAIN_FLOW_ALERT", settings)
            self.assertIn("独立历史、outbox、冷却和小时配额", intro)
            self.assertIn("方向评分不是概率", intro)


if __name__ == "__main__":
    unittest.main()
