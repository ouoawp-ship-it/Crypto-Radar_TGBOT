from __future__ import annotations

import json
import unittest

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
        payload = public_data_sources_payload()
        serialized = json.dumps(payload, ensure_ascii=False).lower()

        self.assertTrue(payload["ok"])
        for forbidden in ("api_key", "bot_token", "password", "authorization", "cookie"):
            self.assertNotIn(forbidden, serialized)


if __name__ == "__main__":
    unittest.main()
