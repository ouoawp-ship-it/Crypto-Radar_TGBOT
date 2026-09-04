from __future__ import annotations

from dataclasses import replace
import unittest

from radars.altcoin_hunter.identity import IdentityRegistry, InstrumentIdentity
from radars.altcoin_hunter.universe import (
    ActivityTier, EligibilityStatus, Instrument, ListingStage, SamplingPriority,
    UniverseRegistry, instrument_from_dict,
)

TIME = 1704067200000


def instrument(**overrides):
    values = dict(
        exchange="binance", market="futures", instrument_id="EXAMPLEUSDT",
        symbol="EXAMPLEUSDT", exchange_symbol="EXAMPLEUSDT", source="fixture_directory",
        effective_at_ms=TIME, eligibility_status=EligibilityStatus.ELIGIBLE,
        listing_stage=ListingStage.MATURE, data_quality="complete",
    )
    values.update(overrides)
    return Instrument(**values)


class IdentityTests(unittest.TestCase):
    def test_same_ticker_across_venues_is_not_the_same_identity(self):
        first = InstrumentIdentity("binance", "futures", "EXAMPLEUSDT", "EXAMPLEUSDT", "EXAMPLEUSDT", "asset:one", mapping_method="explicit")
        second = InstrumentIdentity("other", "futures", "EXAMPLEUSDT", "EXAMPLEUSDT", "EXAMPLEUSDT", "asset:two", mapping_method="explicit")
        registry = IdentityRegistry([first, second])
        self.assertEqual(len(registry.snapshot()), 2)
        self.assertEqual(registry.resolve(*first.key).canonical_asset_id, "asset:one")
        self.assertEqual(registry.resolve(*second.key).canonical_asset_id, "asset:two")

    def test_same_symbol_does_not_infer_canonical_asset(self):
        identity = InstrumentIdentity("binance", "futures", "1000EXAMPLEUSDT", "EXAMPLEUSDT", "1000EXAMPLEUSDT", contract_multiplier="1000")
        self.assertIsNone(identity.canonical_asset_id)
        self.assertEqual(identity.contract_multiplier, "1000")
        self.assertEqual(identity.exchange_symbol, "1000EXAMPLEUSDT")

    def test_symbol_only_mapping_is_not_trusted(self):
        with self.assertRaises(ValueError):
            InstrumentIdentity("binance", "futures", "A", "A", "A", "asset:a", mapping_method="symbol_match")
        with self.assertRaises(ValueError):
            InstrumentIdentity("binance", "futures", "A", "A", "A", "asset:a")

    def test_conflicting_mapping_is_rejected(self):
        first = InstrumentIdentity("binance", "futures", "A", "A", "A", "asset:a", mapping_method="explicit")
        with self.assertRaises(ValueError):
            IdentityRegistry([first, replace(first, canonical_asset_id="asset:b")])


class UniverseTests(unittest.TestCase):
    def test_new_listing_extreme_and_insufficient_quality_are_orthogonal(self):
        record = instrument(
            listing_stage=ListingStage.NEW_LISTING, activity_tier=ActivityTier.EXTREME,
            sampling_priority=SamplingPriority.CRITICAL, data_quality="insufficient",
            reason_codes=("new_listing", "insufficient_history"),
        )
        self.assertEqual(record.listing_stage, ListingStage.NEW_LISTING)
        self.assertEqual(record.activity_tier, ActivityTier.EXTREME)
        self.assertEqual(record.data_quality, "insufficient")
        self.assertEqual(instrument_from_dict(record.to_dict()), record)

    def test_refresh_history_contains_only_semantic_changes(self):
        registry = UniverseRegistry()
        first = instrument()
        initial = registry.refresh([first], observed_at_ms=TIME)
        self.assertEqual(len(initial.changes), 1)
        unchanged = registry.refresh([replace(first, effective_at_ms=TIME + 1000)], observed_at_ms=TIME + 1000)
        self.assertEqual(unchanged.changes, ())
        hot = registry.refresh([replace(first, activity_tier=ActivityTier.HOT, effective_at_ms=TIME + 2000)], observed_at_ms=TIME + 2000)
        self.assertEqual(len(hot.changes), 1)
        self.assertEqual(len(registry.history), 2)

    def test_failed_or_partial_directory_keeps_last_good_without_consuming_input(self):
        registry = UniverseRegistry([instrument()])
        def unavailable():
            raise AssertionError("failed directory must not be consumed")
            yield
        for flags in ({"complete": False}, {"source_healthy": False}):
            with self.subTest(flags=flags):
                result = registry.refresh(unavailable(), observed_at_ms=TIME + 1000, **flags)
                self.assertFalse(result.accepted)
                self.assertEqual(result.instruments, (instrument(),))
        self.assertEqual(registry.history, ())

    def test_empty_directory_does_not_remove_existing_instruments(self):
        registry = UniverseRegistry([instrument()])
        result = registry.refresh([], observed_at_ms=TIME)
        self.assertFalse(result.accepted)
        self.assertEqual(len(registry.snapshot()), 1)

    def test_explicit_delisting_is_retained_and_idempotent(self):
        original = instrument()
        registry = UniverseRegistry([original])
        result = registry.mark_delisting(original.key, effective_at_ms=TIME + 1000)
        self.assertTrue(result.accepted)
        self.assertEqual(len(result.instruments), 1)
        self.assertEqual(result.instruments[0].listing_stage, ListingStage.DELISTING)
        self.assertEqual(result.instruments[0].eligibility_status, EligibilityStatus.INELIGIBLE)
        self.assertEqual(result.changes[0].reason, "delisting")
        registry.mark_delisting(original.key, effective_at_ms=TIME + 2000)
        self.assertEqual(len(registry.history), 1)

    def test_venue_and_market_keys_do_not_collide(self):
        first = instrument()
        second = replace(first, exchange="other")
        third = replace(first, market="spot")
        result = UniverseRegistry().refresh([second, first, third], observed_at_ms=TIME)
        self.assertEqual(len(result.instruments), 3)
        self.assertEqual([record.key for record in result.instruments], sorted([first.key, second.key, third.key]))

    def test_duplicate_instrument_rejects_whole_refresh(self):
        registry = UniverseRegistry()
        with self.assertRaises(ValueError):
            registry.refresh([instrument(), instrument()], observed_at_ms=TIME)
        self.assertEqual(registry.snapshot(), ())
        self.assertEqual(registry.history, ())

    def test_stale_metadata_rejects_whole_refresh(self):
        old = instrument(metadata_version=2)
        registry = UniverseRegistry([old])
        new_record = instrument(instrument_id="NEWUSDT")
        result = registry.refresh([new_record, replace(old, metadata_version=1)], observed_at_ms=TIME + 1)
        self.assertFalse(result.accepted)
        self.assertEqual(registry.snapshot(), (old,))
        self.assertEqual(registry.history, ())

    def test_absence_does_not_infer_delisting(self):
        original = instrument()
        registry = UniverseRegistry([original])
        registry.refresh([replace(original, instrument_id="NEWUSDT")], observed_at_ms=TIME + 1)
        self.assertEqual(len(registry.snapshot()), 2)
        self.assertEqual(registry.snapshot()[0].listing_stage, ListingStage.MATURE)

    def test_capacity_rejection_is_atomic(self):
        registry = UniverseRegistry([instrument()], max_instruments=1)
        with self.assertRaises(ValueError):
            registry.refresh([instrument(instrument_id="NEWUSDT")], observed_at_ms=TIME)
        self.assertEqual(len(registry.snapshot()), 1)
        self.assertEqual(registry.history, ())

    def test_recent_history_is_bounded_without_dropping_returned_changes(self):
        registry = UniverseRegistry(max_history=2)
        returned = []
        for version in range(1, 6):
            result = registry.refresh([instrument(metadata_version=version)], observed_at_ms=TIME + version)
            returned.extend(result.changes)
        self.assertEqual(len(returned), 5)
        self.assertEqual([change.current.metadata_version for change in registry.history], [4, 5])
        self.assertEqual(registry.history_truncated, 3)
        self.assertEqual(registry.snapshot()[0].metadata_version, 5)
        with self.assertRaises(ValueError):
            UniverseRegistry(max_history=0)

    def test_oversized_directory_generator_is_stopped_at_capacity(self):
        registry = UniverseRegistry(max_instruments=1)
        seen = []
        def oversized():
            for index in range(100):
                seen.append(index)
                yield instrument(instrument_id=f"ASSET{index}USDT")
        with self.assertRaises(ValueError):
            registry.refresh(oversized(), observed_at_ms=TIME)
        self.assertEqual(seen, [0, 1])
        self.assertEqual(registry.snapshot(), ())

    def test_invalid_enum_timestamp_and_delisting_eligibility_rejected(self):
        for change in ({"listing_stage": "HOT"}, {"effective_at_ms": 1704067200}, {"metadata_version": 0}, {"listing_stage": ListingStage.DELISTING}, {"activity_tier": "BLACKLIST"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                instrument(**change)


if __name__ == "__main__":
    unittest.main()
