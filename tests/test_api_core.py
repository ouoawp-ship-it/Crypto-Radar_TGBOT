from __future__ import annotations

import unittest

from paopao_radar.web_services.api_core import (
    api_error,
    api_ok,
    normalize_symbol_filter,
    pagination_params,
    redact_api_payload,
    sort_params,
    time_range_params,
)


class ApiCoreTests(unittest.TestCase):
    def test_pagination_params_clamps_limit_and_reads_cursor(self) -> None:
        params = pagination_params({"limit": ["999"], "cursor": ["42"], "offset": ["3"], "page": ["2"]}, default_limit=50, max_limit=200)
        fallback = pagination_params({"limit": ["bad"]}, default_limit=25, max_limit=100)

        self.assertEqual(params["limit"], 200)
        self.assertEqual(params["cursor"], 42)
        self.assertEqual(params["offset"], 3)
        self.assertEqual(params["page"], 2)
        self.assertEqual(fallback["limit"], 25)

    def test_sort_params_accepts_direction_and_falls_back(self) -> None:
        desc = sort_params({"sort": ["-id"]}, {"id", "ts"}, default="-ts")
        asc = sort_params({"sort": ["ts"]}, {"id", "ts"}, default="-id")
        fallback = sort_params({"sort": ["bad"]}, {"id", "ts"}, default="-id")

        self.assertEqual(desc, {"field": "id", "direction": "desc", "raw": "-id"})
        self.assertEqual(asc, {"field": "ts", "direction": "asc", "raw": "ts"})
        self.assertEqual(fallback, {"field": "id", "direction": "desc", "raw": "-id"})

    def test_time_range_params_supports_window_and_explicit_range(self) -> None:
        defaulted = time_range_params({})
        explicit = time_range_params({"start_ts": ["100"], "end_ts": ["200"], "window_sec": ["60"]})
        window = time_range_params({"window_sec": ["60"]})

        self.assertEqual(defaulted["window_sec"], 86400)
        self.assertFalse(defaulted["applied"])
        self.assertEqual(explicit["start_ts"], 100)
        self.assertEqual(explicit["end_ts"], 200)
        self.assertTrue(window["applied"])
        self.assertIsNotNone(window["start_ts"])
        self.assertIsNotNone(window["end_ts"])

    def test_normalize_symbol_filter_supports_coin_and_pair(self) -> None:
        self.assertEqual(normalize_symbol_filter("BTC")["symbol"], "BTCUSDT")
        self.assertEqual(normalize_symbol_filter("BTCUSDT")["coin"], "BTC")
        self.assertEqual(normalize_symbol_filter("btc/usd")["symbol"], "BTCUSDT")

    def test_api_ok_error_and_redaction_shape(self) -> None:
        ok = api_ok({"value": 1}, message="done")
        err = api_error("bad", code="bad_request", details={"token": "secret"})
        redacted = redact_api_payload({"AI_API_KEY": "sk-" + "abcdefghijklmnopqrstuvwxyz", "text": "ok"})

        self.assertTrue(ok["ok"])
        self.assertEqual(ok["data"], {"value": 1})
        self.assertFalse(err["ok"])
        self.assertEqual(err["code"], "bad_request")
        self.assertEqual(err["details"]["token"], "<redacted>")
        self.assertEqual(redacted["AI_API_KEY"], "<redacted>")


if __name__ == "__main__":
    unittest.main()
