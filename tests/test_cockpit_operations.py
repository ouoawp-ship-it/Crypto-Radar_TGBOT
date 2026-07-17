from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings, normalize_cockpit_v2_mode
from paopao_radar.signal_store import SignalEventStore
from paopao_radar.web import build_deployment_acceptance
from paopao_radar.web_services.public import (
    public_info_feed_payload,
    public_market_overview_payload,
    public_stream_batch,
)


class CockpitOperationsTest(unittest.TestCase):
    def test_feature_mode_is_normalized_and_disables_only_v2_contracts(self) -> None:
        self.assertEqual(normalize_cockpit_v2_mode("PREVIEW"), "preview")
        self.assertEqual(normalize_cockpit_v2_mode("invalid"), "enabled")
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), cockpit_v2_mode="disabled")
            info = public_info_feed_payload(settings=settings, now_ts=100, refresh=False)
            overview = public_market_overview_payload(settings=settings, now_ts=100)
            store = SignalEventStore(settings.signal_events_db_path)
            store.append_from_push(template_id="TG_RADAR_SUMMARY", dedup_key="compat", status="sent", sent=True, text="BTCUSDT compatibility signal", ts=100)
            stream = public_stream_batch(0, settings=settings)

        self.assertEqual(info["code"], "feature_disabled")
        self.assertEqual(overview["code"], "feature_disabled")
        self.assertEqual(stream["count"], 1)

    def test_stream_projection_is_bounded_and_excludes_private_payload(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            store = SignalEventStore(settings.signal_events_db_path)
            first = store.append_from_push(template_id="TG_FLOW_RADAR", dedup_key="flow:btc", status="sent", sent=True, text="BTCUSDT flow event", ts=100)
            store.append_from_push(template_id="TG_FLOW_RADAR", dedup_key="flow:eth", status="sent", sent=True, text="ETHUSDT flow event", ts=101)
            batch = public_stream_batch(first, limit=1, settings=settings)

        self.assertEqual(batch["count"], 1)
        self.assertEqual(batch["items"][0]["symbol"], "ETHUSDT")
        serialized = json.dumps(batch, ensure_ascii=False).lower()
        for forbidden in ("payload_json", "message_ids", "dedup_key", "bot_token"):
            self.assertNotIn(forbidden, serialized)

    def test_stable_check_reports_preview_and_disabled_rollout_modes(self) -> None:
        base = {
            "services": {"main": {"active_ok": True}, "web": {"active_ok": True}},
            "git": {"version": "v1.88.0", "commit": "abc123"},
            "stability": {"status": "ready"},
            "release_readiness": {"status": "candidate"},
            "logs": {},
            "audit": {},
        }
        checks = []
        for mode in ("preview", "disabled"):
            snapshot = {
                **base,
                "config": {
                    "telegram": {"bot_token_configured": True, "chat_id_configured": True},
                    "ai_assistant": {"enable": False},
                    "web": {"host": "0.0.0.0", "port": 8080, "auth_mode": "password", "admin_password_hash_configured": True, "session_secret_configured": True},
                    "cockpit_v2": {"mode": mode, "news_events_db_exists": True, "agent_insights_db_exists": True},
                },
            }
            result = build_deployment_acceptance(snapshot)
            checks.append(next(item for item in result["checks"] if item["key"] == "cockpit_v2"))

        self.assertEqual([item["status"] for item in checks], ["warn", "warn"])
        self.assertIn("回滚", checks[1]["detail"])

    def test_deployment_scripts_include_v2_build_gate_and_sse_proxy(self) -> None:
        install = Path("scripts/install_server.sh").read_text(encoding="utf-8")
        update = Path("scripts/update_server.sh").read_text(encoding="utf-8")
        acceptance = Path("scripts/check_https_deploy.sh").read_text(encoding="utf-8")
        for source in (install, update):
            self.assertIn("NEXT_PUBLIC_PAOXX_COCKPIT_V2_MODE", source)
            self.assertIn("location = /public-api/stream", source)
            self.assertIn("proxy_buffering off", source)
        self.assertIn("check_v2_cockpit_contracts", acceptance)
        self.assertIn("SSE 增量通道", acceptance)


if __name__ == "__main__":
    unittest.main()
