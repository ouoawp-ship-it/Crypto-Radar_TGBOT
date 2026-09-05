from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
import json
from types import MappingProxyType
import unittest
from unittest.mock import patch

from radars.altcoin_hunter.adapters.base import (
    EnvelopeLimits, ExchangeInfoPayloadLimits, ExchangeInfoPreflightObservation,
    ParseLimits, validate_combined_envelope,
)
from radars.altcoin_hunter.adapters.binance_usdm import parse_exchange_info
from radars.altcoin_hunter.adapters.fixtures import FIXTURE_TIME_MS, fixture_exchange_info


class CombinedEnvelopeContractTests(unittest.TestCase):
    def test_required_fields_and_mapping_payload_are_returned_without_rewrite(self):
        data = MappingProxyType({"e": "aggTrade", "q": "1.000"})
        frame = MappingProxyType({"stream": "aaausdt@aggTrade", "data": data})
        stream, actual, extras = validate_combined_envelope(frame)
        self.assertEqual(stream, "aaausdt@aggTrade")
        self.assertIs(actual, data)
        self.assertEqual(extras, 0)

    def test_bounded_unknown_fields_are_counted_and_not_returned_as_details(self):
        frame = {"stream": "!markPrice@arr", "data": [{"s": "AAAUSDT"}],
                 "extension": {"version": 1.25, "flags": [True, None, "future"]},
                 "future_header": "sensitive-value-never-a-diagnostic"}
        result = validate_combined_envelope(frame)
        self.assertEqual(result, (frame["stream"], frame["data"], 2))
        self.assertNotIn("sensitive", repr(result))

    def test_missing_required_fields_fail_even_with_extensions(self):
        for frame in ({"data": {}, "extension": 1}, {"stream": "a@aggTrade", "extension": 1}):
            with self.subTest(frame=frame), self.assertRaisesRegex(ValueError, "^malformed_combined_envelope$"):
                validate_combined_envelope(frame)

    def test_stream_requires_bounded_native_ascii_without_whitespace(self):
        for stream in (None, False, 123, 1.0, "", " a@aggTrade", "a@aggTrade ", "a\n@aggTrade",
                       "a @aggTrade", "妖@aggTrade", "a" * 257):
            with self.subTest(stream=stream), self.assertRaises(ValueError):
                validate_combined_envelope({"stream": stream, "data": {}})

    def test_data_must_be_mapping_or_list(self):
        for data in (None, False, 1, 1.2, "{}", (), b"{}"):
            with self.subTest(data=data), self.assertRaisesRegex(ValueError, "^invalid_field_type$"):
                validate_combined_envelope({"stream": "a@aggTrade", "data": data})

    def test_unknown_extension_depth_and_string_limits_are_enforced(self):
        nested = {"extension": {"nested": {"too_deep": 1}}}
        for extension, limits in ((nested, EnvelopeLimits(max_depth=3)),
                                  ("x" * 257, EnvelopeLimits(max_string_length=256))):
            with self.subTest(extension=type(extension).__name__), self.assertRaisesRegex(ValueError, "^oversized_payload$"):
                validate_combined_envelope({"stream": "a@aggTrade", "data": {}, "future": extension}, limits=limits)

    def test_unknown_field_counts_include_entire_tree(self):
        frame = {"stream": "a@aggTrade", "data": {}, "a": {}, "b": {}}
        with self.assertRaisesRegex(ValueError, "^too_many_items$"):
            validate_combined_envelope(frame, limits=EnvelopeLimits(max_top_level_fields=3))
        frame = {"stream": "a@aggTrade", "data": {"a": 1, "b": 2}, "future": {"c": 3, "d": 4}}
        with self.assertRaisesRegex(ValueError, "^too_many_items$"):
            validate_combined_envelope(frame, limits=EnvelopeLimits(max_total_fields=6))
        with self.assertRaisesRegex(ValueError, "^too_many_items$"):
            validate_combined_envelope(frame, limits=EnvelopeLimits(max_fields_per_object=2))

    def test_array_and_total_value_budgets_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "^too_many_items$"):
            validate_combined_envelope({"stream": "!markPrice@arr", "data": [{}] * 3},
                                      limits=EnvelopeLimits(max_items=2))
        with self.assertRaisesRegex(ValueError, "^oversized_payload$"):
            validate_combined_envelope({"stream": "!markPrice@arr", "data": [{}, {}]},
                                      limits=EnvelopeLimits(max_total_values=5))

    def test_exact_compact_utf8_bytes_include_unknown_fields_and_escaping(self):
        frame = {"stream": "a@aggTrade", "data": {}, "future": "quote\" and 中文\n"}
        size = len(json.dumps(frame, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        self.assertEqual(validate_combined_envelope(frame, limits=EnvelopeLimits(max_payload_bytes=size))[2], 1)
        with self.assertRaisesRegex(ValueError, "^oversized_payload$"):
            validate_combined_envelope(frame, limits=EnvelopeLimits(max_payload_bytes=size - 1))

    def test_nonfinite_values_cycles_and_non_json_extensions_are_rejected(self):
        cycle = []
        cycle.append(cycle)
        for value in (float("nan"), float("inf"), float("-inf"), {1: "bad-key"}, object(), cycle,
                      "\ud800"):
            with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(ValueError, "^invalid_json_shape$"):
                validate_combined_envelope({"stream": "a@aggTrade", "data": {}, "future": value})

    def test_every_envelope_limit_is_strict_finite_and_immutable(self):
        policy = EnvelopeLimits()
        for item in fields(policy):
            for invalid in (True, None, 1.5, float("inf"), -1, 0, 10**12):
                with self.subTest(field=item.name, invalid=invalid), self.assertRaises(ValueError):
                    replace(policy, **{item.name: invalid})
        with self.assertRaises(FrozenInstanceError):
            policy.max_payload_bytes = 2_000_000


class ExchangeInfoPreflightContractTests(unittest.TestCase):
    def test_directory_budget_is_separate_from_ws_frame_budget(self):
        self.assertEqual(ParseLimits().max_payload_bytes, 1_000_000)
        self.assertEqual(EnvelopeLimits().max_payload_bytes, 1_000_000)
        self.assertEqual(ExchangeInfoPayloadLimits().max_payload_bytes, 8 * 1024 * 1024)
        self.assertEqual(ExchangeInfoPayloadLimits(max_payload_bytes=16 * 1024 * 1024).max_payload_bytes,
                         16 * 1024 * 1024)

    def test_explicit_directory_budget_accepts_large_synthetic_response_only_within_limit(self):
        # Synthetic directory size tests a contract; it makes no live-size claim.
        payload = fixture_exchange_info()
        template = payload["symbols"][0]
        payload["symbols"] = []
        for index in range(2000):
            row = deepcopy(template)
            row.update(symbol=f"SYNTH{index:04d}USDT", pair=f"SYNTH{index:04d}USDT",
                       baseAsset=f"SYNTH{index:04d}")
            payload["symbols"].append(row)
        encoded = json.dumps(payload, separators=(",", ":"))
        self.assertGreater(len(encoded.encode("utf-8")), ParseLimits().max_payload_bytes)
        rejected = parse_exchange_info(encoded, observed_at_ms=FIXTURE_TIME_MS, limits=ParseLimits())
        accepted = parse_exchange_info(encoded, observed_at_ms=FIXTURE_TIME_MS,
                                       limits=ExchangeInfoPayloadLimits())
        self.assertFalse(rejected.accepted)
        self.assertTrue(accepted.accepted)
        self.assertEqual(len(accepted.instruments), 2000)
        too_small = parse_exchange_info(encoded, observed_at_ms=FIXTURE_TIME_MS,
                                        limits=ExchangeInfoPayloadLimits(max_payload_bytes=1024))
        self.assertFalse(too_small.accepted)

    def test_every_directory_limit_is_strict_finite_and_immutable(self):
        policy = ExchangeInfoPayloadLimits()
        for item in fields(policy):
            for invalid in (True, None, 1.5, float("nan"), -1, 0, 10**12):
                with self.subTest(field=item.name, invalid=invalid), self.assertRaises(ValueError):
                    replace(policy, **{item.name: invalid})
        with self.assertRaises(FrozenInstanceError):
            policy.max_payload_bytes = 1

    def test_preflight_report_records_content_length_unknown_and_actual_bytes(self):
        observed = ExchangeInfoPreflightObservation(200, None, 1_500_000, 1500, 1499, 1)
        self.assertEqual(observed.to_dict(), {"http_status": 200, "content_length": None,
                         "actual_body_bytes": 1_500_000, "symbol_count": 1500,
                         "parse_accepted_count": 1499, "parse_rejected_count": 1})
        # Content-Length can differ from decoded bytes; keep both observations.
        self.assertEqual(replace(observed, content_length=500_000).actual_body_bytes, 1_500_000)
        with self.assertRaises(FrozenInstanceError):
            observed.http_status = 201

    def test_preflight_values_are_strict_and_observation_creates_no_io(self):
        observed = ExchangeInfoPreflightObservation(200, None, 0, 0, 0, 0)
        for item in fields(observed):
            for invalid in (True, -1, float("inf"), "200"):
                with self.subTest(field=item.name, invalid=invalid), self.assertRaises(ValueError):
                    replace(observed, **{item.name: invalid})
        for status in (99, 600):
            with self.assertRaises(ValueError):
                replace(observed, http_status=status)
        with patch("socket.socket", side_effect=AssertionError("network_forbidden")), \
             patch("socket.getaddrinfo", side_effect=AssertionError("dns_forbidden")), \
             patch("sqlite3.connect", side_effect=AssertionError("database_forbidden")), \
             patch("builtins.open", side_effect=AssertionError("file_write_forbidden")):
            self.assertEqual(ExchangeInfoPreflightObservation(503, 0, 0, 0, 0, 0).http_status, 503)


if __name__ == "__main__":
    unittest.main()
