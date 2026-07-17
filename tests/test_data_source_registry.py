from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from paopao_radar.data_source_registry import data_source_registry_payload
from paopao_radar.web_services.public import public_data_sources_payload


class DataSourceRegistryTests(unittest.TestCase):
    def test_registry_declares_provenance_rights_retention_and_fallback(self) -> None:
        payload = data_source_registry_payload()

        self.assertGreaterEqual(payload["source_count"], 8)
        self.assertEqual(payload["source_count"], len(payload["sources"]))
        for source in payload["sources"]:
            self.assertTrue(source["id"])
            self.assertTrue(source["metrics"])
            self.assertTrue(source["rights_status"])
            self.assertTrue(source["retention_policy"])
            self.assertTrue(source["fallback"])

    def test_public_registry_is_secret_free(self) -> None:
        runtime = {
            "scope": "process",
            "status": "ready",
            "source_limit": 32,
            "collapsed_sources": 0,
            "sources": {
                "binance_spot_public": {
                    "status": "ready", "attempts": 4, "successes": 4, "failures": 0,
                    "success_rate": 1.0, "cache_hit_rate": 0.5, "data_age_sec": 2,
                    "last_error": "Authorization=Bearer must-not-leak",
                },
            },
        }
        with patch("paopao_radar.web_services.public.UPSTREAM_SOURCE_METRICS.snapshot", return_value=runtime):
            payload = public_data_sources_payload()
        serialized = json.dumps(payload, ensure_ascii=False).lower()

        self.assertTrue(payload["ok"])
        by_id = {source["id"]: source for source in payload["data"]["sources"]}
        self.assertEqual(by_id["binance_spot_public"]["runtime"]["attempts"], 4)
        self.assertEqual(by_id["coinpaprika_market"]["runtime"]["status"], "unobserved")
        self.assertEqual(payload["data"]["runtime"]["status"], "ready")
        self.assertNotIn("must-not-leak", serialized)
        for forbidden in ("api_key", "bot_token", "password", "authorization", "cookie"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
