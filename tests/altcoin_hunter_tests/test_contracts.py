from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from radars.altcoin_hunter.configuration import AltcoinHunterConfig
from radars.altcoin_hunter.models import (
    BookTickerPayload, FundingPayload, LiquidationPayload, MarkPricePayload,
    OpenInterestPayload, TradeEvent, TradePayload, event_from_dict, event_to_dict,
)

TIME = 1704067200000


def trade(**overrides):
    values = dict(
        exchange="binance", market="futures", instrument_id="BTCUSDT",
        symbol="BTCUSDT", exchange_symbol="BTCUSDT", source="fixture",
        source_event_id="123", event_time_ms=TIME, receive_time_ms=TIME + 10,
        receive_monotonic_ns=1000000, payload=TradePayload("10.125", "2", False),
        sequence_start=123, sequence_end=123,
    )
    values.update(overrides)
    return TradeEvent(**values)


class EventContractTests(unittest.TestCase):
    def test_exact_envelope_roundtrip_and_immutable_payload(self):
        event = trade()
        data = event_to_dict(event)
        self.assertEqual(set(data), {
            "schema_version", "exchange", "market", "instrument_id", "canonical_asset_id",
            "symbol", "exchange_symbol", "event_type", "event_time_ms", "receive_time_ms",
            "receive_monotonic_ns", "source", "source_event_id", "sequence_start", "sequence_end",
            "connection_epoch", "quality_flags", "payload",
        })
        self.assertEqual(event_from_dict(data), event)
        self.assertIsNone(data["canonical_asset_id"])
        with self.assertRaises(FrozenInstanceError):
            event.payload.price = "99"

    def test_dedup_scopes_source_exchange_market_and_instrument(self):
        original = trade()
        self.assertEqual(len(original.dedup_key), 6)
        self.assertEqual(replace(original, connection_epoch=3).dedup_key, original.dedup_key)
        for change in ({"source": "other"}, {"exchange": "other"}, {"market": "spot"}, {"instrument_id": "other"}, {"source_event_id": "124"}):
            with self.subTest(change=change):
                self.assertNotEqual(replace(original, **change).dedup_key, original.dedup_key)

    def test_seconds_and_boolean_timestamps_are_rejected(self):
        for name in ("event_time_ms", "receive_time_ms"):
            for bad in (1704067200, True, -1, 4102444800000, 1704067200000.0):
                with self.subTest(name=name, bad=bad), self.assertRaises(ValueError):
                    trade(**{name: bad})

    def test_sequences_and_monotonic_time_are_strict(self):
        for change in ({"receive_monotonic_ns": -1}, {"receive_monotonic_ns": True}, {"sequence_start": 124}, {"sequence_end": None}, {"connection_epoch": -1}, {"sequence_start": "123"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                trade(**change)

    def test_maker_flag_rejects_coercion(self):
        for value in (None, 0, 1, "false", "true"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TradePayload("1", "1", value)

    def test_decimal_units_do_not_double_apply_multiplier(self):
        base = TradePayload("0.05", "1000", False, contract_multiplier="1000")
        contracts = TradePayload("0.05", "2", True, quantity_unit="contracts", contract_multiplier="1000")
        self.assertEqual(base.base_quantity, Decimal("1000"))
        self.assertEqual(base.quote_notional, Decimal("50"))
        self.assertEqual(contracts.base_quantity, Decimal("2000"))
        self.assertEqual(contracts.quote_notional, Decimal("100"))

    def test_decimal_product_is_exact_beyond_default_context_precision(self):
        payload = TradePayload("123456789012345.12345", "1.123456789012345", False)
        self.assertEqual(str(payload.quote_notional), "138698367765583.80781130300259899025")

    def test_nonfinite_negative_and_invalid_trade_values_rejected(self):
        for value in ("NaN", "Infinity", "-1", "0", "1e309", "1e-999", "1_000", " 1", True, 1):
            with self.subTest(value=value), self.assertRaises(ValueError):
                TradePayload(value, "1", False)
        with self.assertRaises(ValueError):
            TradePayload("1e200", "1e200", False)
        with self.assertRaises(ValueError):
            TradePayload("1e-200", "1e-200", False)

    def test_payload_must_match_event_type(self):
        with self.assertRaises(ValueError):
            trade(payload=FundingPayload("0.0001"))
        with self.assertRaises(ValueError):
            trade(event_type="funding")

    def test_all_typed_payloads_roundtrip(self):
        samples = (
            ("mark_price", MarkPricePayload("10", "9.9")),
            ("funding", FundingPayload("-0.001", 8, TIME + 1000)),
            ("open_interest", OpenInterestPayload("20", "contracts", "100", "10")),
            ("book_ticker", BookTickerPayload("9", "10", "2", "3")),
            ("liquidation", LiquidationPayload("10", "2", "sell")),
        )
        for kind, payload in samples:
            data = event_to_dict(trade())
            data.update(event_type=kind, payload=payload.__dict__)
            with self.subTest(kind=kind):
                event = event_from_dict(data)
                self.assertEqual(event.event_type, kind)
                self.assertEqual(event.payload, payload)

    def test_missing_critical_metrics_remain_null_with_reason(self):
        payload = OpenInterestPayload(None, missing_reason="source_unavailable")
        self.assertIsNone(payload.open_interest)
        with self.assertRaises(ValueError):
            OpenInterestPayload(None)
        with self.assertRaises(ValueError):
            BookTickerPayload(None, "10")

    def test_book_order_and_funding_units_validate(self):
        with self.assertRaises(ValueError):
            BookTickerPayload("11", "10")
        with self.assertRaises(ValueError):
            FundingPayload("0.1", interval_hours=0)
        with self.assertRaises(ValueError):
            OpenInterestPayload("2", unit="unknown")

    def test_ids_quality_flags_and_unknown_fields_are_bounded(self):
        for change in ({"symbol": "A\nB"}, {"source_event_id": "x" * 129}, {"quality_flags": ["gap"]}, {"schema_version": 2}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                trade(**change)
        data = event_to_dict(trade())
        data["unexpected"] = True
        with self.assertRaises(ValueError):
            event_from_dict(data)


class ConfigurationTests(unittest.TestCase):
    def test_safe_defaults_and_hash(self):
        config = AltcoinHunterConfig()
        self.assertFalse(config.enable)
        self.assertFalse(config.send_enable)
        self.assertFalse(config.live_data_enable)
        self.assertFalse(config.raw_capture_enable)
        self.assertIsNone(config.db_file)
        self.assertEqual(config.bucket_sec, 60)
        self.assertEqual(config.config_hash, AltcoinHunterConfig.from_mapping({}).config_hash)
        self.assertNotEqual(config.config_hash, replace(config, enable=True).config_hash)

    def test_explicit_mapping_parses_without_environment_reads(self):
        with patch("os.getenv", side_effect=AssertionError("environment read")):
            config = AltcoinHunterConfig.from_mapping({"ALTCOIN_HUNTER_ENABLE": "true", "ALTCOIN_HUNTER_BUCKET_SEC": "60"})
        self.assertTrue(config.enable)

    def test_unsupported_capabilities_fail_closed(self):
        for name in ("live_data_enable", "send_enable", "raw_capture_enable"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                AltcoinHunterConfig.from_mapping({name: "true"})

    def test_invalid_configuration_is_rejected(self):
        changes = ({"enable": "perhaps"}, {"enable": 1}, {"bucket_sec": 0}, {"bucket_sec": float("nan")}, {"allowed_lateness_ms": -1}, {"retention_1m_days": 0}, {"config_version": 2}, {"config_version": True}, {"unknown": "value"}, {"bucket_sec": "1e2"})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(ValueError):
                AltcoinHunterConfig.from_mapping(change)

    def test_database_is_explicit_and_constructor_never_creates_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "not_created" / "altcoin_hunter.db"
            config = AltcoinHunterConfig(db_file=path)
            self.assertFalse(path.parent.exists())
            self.assertTrue(config.redacted_status()["db_configured"])
            self.assertNotIn(str(path), str(config.redacted_status()))

    def test_legacy_relative_and_traversal_database_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            values = ["altcoin_hunter.db", Path(temporary) / "signals.db", Path(temporary) / "realtime_features.db", Path(temporary) / ".." / "hunter.db", Path(temporary) / "hunter.txt"]
            for value in values:
                with self.subTest(value=value), self.assertRaises(ValueError):
                    AltcoinHunterConfig(db_file=value)

    def test_symlink_database_component_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            link = path / "linked"
            # Exercise policy consistently on Windows without requiring the
            # machine's optional create-symbolic-link privilege.
            with patch.object(Path, "is_symlink", lambda candidate: candidate == link), self.assertRaises(ValueError):
                AltcoinHunterConfig(db_file=link / "hunter.db")

    def test_invalid_device_wildcard_and_network_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            for value in (Path(temporary) / "bad?.db", Path(temporary) / "NUL.db",
                          Path(temporary) / "bad:stream.db", Path(temporary) / "trailing " / "hunter.db",
                          "//server/share/hunter.db"):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    AltcoinHunterConfig(db_file=value)

    def test_import_configuration_models_identity_and_universe_has_no_io(self):
        script = """
import builtins, os, socket, sqlite3, threading
from pathlib import Path
from unittest.mock import patch
def deny(*args, **kwargs):
    raise AssertionError('import side effect')
with patch.object(builtins, 'open', deny), patch.object(os, 'getenv', deny), patch.object(socket, 'socket', deny), patch.object(sqlite3, 'connect', deny), patch.object(threading.Thread, 'start', deny), patch.object(Path, 'mkdir', deny):
    import radars.altcoin_hunter.configuration
    import radars.altcoin_hunter.models
    import radars.altcoin_hunter.identity
    import radars.altcoin_hunter.universe
"""
        result = subprocess.run([sys.executable, "-B", "-c", script], capture_output=True, text=True, timeout=20, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
