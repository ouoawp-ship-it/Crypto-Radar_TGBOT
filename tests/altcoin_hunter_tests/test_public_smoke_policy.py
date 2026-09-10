"""Offline authorization bounds; none of these tests opens a public transport."""
from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
import unittest
from unittest.mock import patch

from radars.altcoin_hunter.adapters.base import PROTOCOL_VERSION, Route
from radars.altcoin_hunter.adapters.binance_usdm import parse_exchange_info
from radars.altcoin_hunter.adapters.fixtures import FIXTURE_TIME_MS, fixture_exchange_info
from radars.altcoin_hunter.smoke_policy import (
    FIRST_SMOKE_SYMBOLS, SmokeConfig, seal_smoke_report, smoke_shards,
    validate_first_success_proof,
)
from radars.altcoin_hunter.subscription_plan import StreamSpec, plan_subscriptions


def first_report(**updates):
    report = {
        "report_version": 1, "tier": "first", "mode": "public_read_only_smoke",
        "status": "passed", "symbols": list(FIRST_SMOKE_SYMBOLS),
        "requested_duration_sec": 180, "actual_duration_ms": 179_500,
        "network_scope": "binance_public_only", "real_send": False,
        "production_writes": 0, "protocol_version": PROTOCOL_VERSION,
        "violations": [],
    }
    report.update(updates)
    return seal_smoke_report(report)


def invented_registry(symbols=FIRST_SMOKE_SYMBOLS):
    """Invent protocol records for identity tests, never download a directory."""
    template = fixture_exchange_info()["symbols"][0]
    rows = []
    for symbol in symbols:
        row = deepcopy(template)
        row.update(symbol=symbol, pair=symbol, baseAsset="SYNTHETIC")
        rows.append(row)
    result = parse_exchange_info({"symbols": rows}, observed_at_ms=FIXTURE_TIME_MS)
    if not result.accepted:
        raise AssertionError("invented directory must be valid")
    return dict(result.registry)


class PublicSmokePolicyTests(unittest.TestCase):
    def test_defaults_are_disabled_immutable_and_fixed_first_tier(self):
        config = SmokeConfig()
        self.assertFalse(config.enable)
        self.assertEqual(config.tier, "first")
        self.assertEqual(set(config.symbols), set(FIRST_SMOKE_SYMBOLS))
        self.assertEqual(config.duration_sec, 180)
        self.assertIsNone(config.first_success_digest)
        with self.assertRaisesRegex(ValueError, "public_smoke_disabled"):
            config.require_enabled()
        with self.assertRaises(FrozenInstanceError):
            config.enable = True

    def test_enable_requires_native_bool(self):
        for value in ("true", "false", 0, 1, None, 1.0):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SmokeConfig(enable=value)
        SmokeConfig(enable=True).require_enabled()

    def test_first_tier_requires_all_and_only_fixed_five(self):
        for symbols in (FIRST_SMOKE_SYMBOLS[:-1], FIRST_SMOKE_SYMBOLS + ("OTHERUSDT",),
                        (*FIRST_SMOKE_SYMBOLS[:-1], "OTHERUSDT")):
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                SmokeConfig(symbols=symbols)
        self.assertEqual(SmokeConfig(symbols=tuple(reversed(FIRST_SMOKE_SYMBOLS))), SmokeConfig())

    def test_first_duration_cannot_be_shortened_or_extended(self):
        for value in (0, -1, 179, 181, 600, 180.0, "180", True, float("inf"), float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SmokeConfig(duration_sec=value)

    def test_symbol_container_and_members_are_strict(self):
        for symbols in (list(FIRST_SMOKE_SYMBOLS), "BTCUSDT", (),
                        ("btcusdt",), (" BTCUSDT",), ("BTCUSDT\n",), (True,),
                        (None,), ("BTCUSDT", "BTCUSDT"), ("BTCUSD",), ("X" * 61 + "USDT",)):
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                SmokeConfig(symbols=symbols)

    def test_tier_is_explicit_and_cannot_be_coerced(self):
        for value in (None, True, 1, "live", "FIRST", "expanded "):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SmokeConfig(tier=value)

    def test_first_tier_does_not_accept_unnecessary_proof(self):
        with self.assertRaisesRegex(ValueError, "does_not_use_prior_proof"):
            SmokeConfig(first_success_proof=first_report())

    def test_expanded_tier_requires_a_valid_first_proof(self):
        for value in (None, False, {}, {"status": "passed"}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                SmokeConfig(tier="expanded", first_success_proof=value)
        report = first_report()
        config = SmokeConfig(enable=True, tier="expanded", symbols=("BTCUSDT",), duration_sec=600,
                             first_success_proof=report)
        self.assertEqual(config.first_success_digest, report["deterministic_digest"])

    def test_expanded_twenty_symbols_and_ten_minutes_are_hard_ceilings(self):
        symbols = tuple(f"COIN{i}USDT" for i in range(20))
        config = SmokeConfig(tier="expanded", symbols=symbols, duration_sec=600,
                             first_success_proof=first_report())
        self.assertEqual(len(config.symbols), 20)
        with self.assertRaises(ValueError):
            SmokeConfig(tier="expanded", symbols=symbols + ("COIN20USDT",),
                        first_success_proof=first_report())
        for duration in (0, -1, 601, 600.0, True, "600"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                SmokeConfig(tier="expanded", duration_sec=duration, first_success_proof=first_report())

    def test_valid_proof_digest_is_checked_before_expansion(self):
        report = first_report()
        self.assertEqual(validate_first_success_proof(report), report["deterministic_digest"])
        report["actual_duration_ms"] -= 1
        with self.assertRaisesRegex(ValueError, "digest"):
            validate_first_success_proof(report)

    def test_proof_must_explicitly_show_first_public_success(self):
        invalid = {"report_version": True, "tier": "expanded", "mode": "offline_dry_run",
                   "status": "failed", "requested_duration_sec": 179,
                   "network_scope": "testnet", "real_send": True, "production_writes": 1,
                   "protocol_version": "old"}
        for key, value in invalid.items():
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_first_success_proof(first_report(**{key: value}))
        for key in invalid:
            report = first_report()
            report.pop(key)
            with self.subTest(missing=key), self.assertRaises(ValueError):
                validate_first_success_proof(seal_smoke_report(report))

    def test_proof_bool_cannot_impersonate_numeric_zero(self):
        for key, value in (("production_writes", False), ("real_send", 0),
                           ("requested_duration_sec", 180.0), ("report_version", 1.0)):
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_first_success_proof(first_report(**{key: value}))

    def test_proof_duration_and_violations_are_not_inferred(self):
        for duration in (None, True, 0, 178_999, 180_001, 179_500.0, "179500"):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                validate_first_success_proof(first_report(actual_duration_ms=duration))
        for violations in (None, {}, ["protocol_incompatible"], False):
            with self.subTest(violations=violations), self.assertRaises(ValueError):
                validate_first_success_proof(first_report(violations=violations))
        for duration in (179_000, 180_000):
            validate_first_success_proof(first_report(actual_duration_ms=duration))

    def test_proof_symbol_cap_cannot_be_hidden_by_success_status(self):
        for symbols in (list(FIRST_SMOKE_SYMBOLS[:-1]), list(FIRST_SMOKE_SYMBOLS) + ["OTHERUSDT"],
                        ["BTCUSDT"] * 5, None, "BTCUSDT"):
            with self.subTest(symbols=symbols), self.assertRaises(ValueError):
                validate_first_success_proof(first_report(symbols=symbols))

    def test_bounded_additional_report_metrics_are_included_in_digest(self):
        report = first_report(metrics={"accepted_events": 2500, "latency_ms": 3.5})
        self.assertEqual(validate_first_success_proof(report), report["deterministic_digest"])
        reordered = dict(reversed(tuple(report.items())))
        self.assertEqual(validate_first_success_proof(reordered), report["deterministic_digest"])

    def test_report_sealing_does_not_mutate_or_alias_caller_data(self):
        raw = {"metrics": {"count": 5}}
        sealed = seal_smoke_report(raw)
        self.assertNotIn("deterministic_digest", raw)
        sealed["metrics"]["count"] = 6
        self.assertEqual(raw["metrics"]["count"], 5)

    def test_report_limits_cover_unknown_values_and_cycles(self):
        deep = {}
        for _ in range(10):
            deep = {"nested": deep}
        cyclic = {}
        cyclic["itself"] = cyclic
        for value in ("x" * 4097, [0] * 257, deep, cyclic, float("nan"), float("inf"),
                      2**65, {1: "invalid key"}, {"x": object()}, ["x" * 4096] * 17):
            with self.subTest(kind=type(value).__name__), self.assertRaises(ValueError):
                seal_smoke_report({"unknown": value})

    def test_config_retains_only_digest_of_validated_proof(self):
        report = first_report()
        config = SmokeConfig(tier="expanded", first_success_proof=report)
        digest = config.first_success_digest
        report["status"] = "failed"
        self.assertEqual(config.first_success_digest, digest)
        self.assertEqual(config.to_dict()["first_success_digest"], digest)
        with self.assertRaises(FrozenInstanceError):
            config.first_success_digest = "0" * 64

    def test_no_url_path_or_secret_configuration_surface(self):
        for option in ("url", "rest_url", "websocket_url", "db_file", "output_dir", "token"):
            with self.subTest(option=option), self.assertRaises(TypeError):
                SmokeConfig(**{option: "forbidden"})
        status = SmokeConfig().to_dict()
        self.assertFalse(status["real_send"])
        self.assertEqual(status["production_writes"], 0)
        self.assertFalse(status["global_streams_enabled"])

    def test_configuration_does_not_read_environment_or_open_files(self):
        with patch("os.getenv", side_effect=AssertionError("no environment")), \
             patch("builtins.open", side_effect=AssertionError("no files")), \
             patch("socket.getaddrinfo", side_effect=AssertionError("no DNS")):
            config = SmokeConfig()
            report = first_report()
            validate_first_success_proof(report)
            self.assertEqual(config.deadline_ns(500), 180_000_000_500)

    def test_deadline_uses_only_explicit_monotonic_start(self):
        config = SmokeConfig()
        self.assertEqual(config.deadline_ns(0), 180_000_000_000)
        for value in (None, True, -1, 1.0, "1", 2**63):
            with self.subTest(value=value), self.assertRaises(ValueError):
                config.deadline_ns(value)

    def test_five_instruments_use_ten_market_and_five_public_streams(self):
        shards = smoke_shards(invented_registry(), FIRST_SMOKE_SYMBOLS)
        self.assertEqual([(shard.route, len(shard.streams)) for shard in shards],
                         [(Route.MARKET, 10), (Route.PUBLIC, 5)])
        self.assertEqual({stream.symbol for shard in shards for stream in shard.streams}, set(FIRST_SMOKE_SYMBOLS))
        self.assertTrue(all(stream.instrument_id is not None for shard in shards for stream in shard.streams))
        self.assertTrue(all(not stream.wire_name.startswith("!") for shard in shards for stream in shard.streams))

    def test_full_metadata_directory_does_not_expand_subscription_scope(self):
        registry = invented_registry(FIRST_SMOKE_SYMBOLS + tuple(f"COIN{i}USDT" for i in range(20)))
        shards = smoke_shards(registry, FIRST_SMOKE_SYMBOLS)
        self.assertEqual(sum(len(shard.streams) for shard in shards), 15)
        self.assertEqual({stream.symbol for shard in shards for stream in shard.streams}, set(FIRST_SMOKE_SYMBOLS))

    def test_plan_order_is_deterministic(self):
        registry = invented_registry()
        self.assertEqual(smoke_shards(registry, FIRST_SMOKE_SYMBOLS),
                         smoke_shards(dict(reversed(tuple(registry.items()))), tuple(reversed(FIRST_SMOKE_SYMBOLS))))

    def test_twenty_symbol_plan_still_uses_only_two_bounded_connections(self):
        symbols = tuple(f"COIN{i}USDT" for i in range(20))
        shards = smoke_shards(invented_registry(symbols), symbols)
        self.assertEqual([len(shard.streams) for shard in shards], [40, 20])
        with self.assertRaises(ValueError):
            smoke_shards({}, symbols + ("COIN20USDT",))

    def test_registry_missing_ineligible_or_wrong_identity_fails_closed(self):
        registry = invented_registry()
        for value in (None, "BTCUSDT", replace(registry["BTCUSDT"], status="SETTLING"),
                      replace(registry["BTCUSDT"], identity=replace(registry["BTCUSDT"].identity, exchange="other")),
                      registry["ETHUSDT"]):
            invalid = dict(registry)
            invalid["BTCUSDT"] = value
            with self.subTest(value=type(value).__name__), self.assertRaises(ValueError):
                smoke_shards(invalid, FIRST_SMOKE_SYMBOLS)

    def test_explicit_instrument_ids_are_not_rebuilt_from_symbols(self):
        registry = invented_registry()
        spec = registry["BTCUSDT"]
        registry["BTCUSDT"] = replace(spec, identity=replace(spec.identity, instrument_id="explicit:btc:perp"))
        shards = smoke_shards(registry, FIRST_SMOKE_SYMBOLS)
        for stream in (stream for shard in shards for stream in shard.streams if stream.symbol == "BTCUSDT"):
            self.assertEqual(stream.instrument_id, "explicit:btc:perp")

    def test_per_symbol_mark_stream_spelling_and_intervals(self):
        for milliseconds, wire in ((3000, "btcusdt@markPrice"), (1000, "btcusdt@markPrice@1s")):
            spec = StreamSpec(Route.MARKET, wire.lower(), wire, "BTCUSDT", "mark_price_symbol",
                              milliseconds, "explicit-btc")
            self.assertEqual(spec.instrument_id, "explicit-btc")
        for interval, wire in ((3000, "btcusdt@markPrice@3s"), (2000, "btcusdt@markPrice")):
            with self.subTest(interval=interval), self.assertRaises(ValueError):
                StreamSpec(Route.MARKET, wire.lower(), wire, "BTCUSDT", "mark_price_symbol", interval, "BTCUSDT")

    def test_per_symbol_mark_requires_explicit_symbol_and_instrument(self):
        for symbol, instrument in ((None, None), ("BTCUSDT", None), ("btcusdt", "btc"), ("BTCUSDT", "")):
            wire = (symbol or "").lower() + "@markPrice"
            with self.subTest(symbol=symbol, instrument=instrument), self.assertRaises(ValueError):
                StreamSpec(Route.MARKET, wire.lower(), wire, symbol, "mark_price_symbol", 3000, instrument)

    def test_existing_global_plan_defaults_are_preserved(self):
        registry = invented_registry()
        plan = plan_subscriptions(spec.to_hunter_instrument() for spec in registry.values())
        wires = {stream.wire_name for shard in plan.shards for stream in shard.streams}
        self.assertIn("!markPrice@arr", wires)
        self.assertIn("!bookTicker", wires)
        self.assertEqual(len(wires), len(FIRST_SMOKE_SYMBOLS) + 2)
        self.assertEqual(plan.coverage["mark_price"]["covered"], len(FIRST_SMOKE_SYMBOLS))


if __name__ == "__main__":
    unittest.main()
