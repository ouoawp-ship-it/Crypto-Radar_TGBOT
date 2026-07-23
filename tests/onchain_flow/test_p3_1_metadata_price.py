from __future__ import annotations

import unittest
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.onchain_flow.collectors.evm_http import RpcTransportError
from paopao_radar.onchain_flow.db import OnchainStore
from paopao_radar.onchain_flow.models import PriceQuote
from paopao_radar.onchain_flow.price_oracle import (
    CachedPriceService,
    CoinGeckoOnchainPriceProvider,
    StaticPriceProvider,
)
from paopao_radar.onchain_flow.token_metadata import (
    DECIMALS_SELECTOR,
    NAME_SELECTOR,
    SYMBOL_SELECTOR,
    TOTAL_SUPPLY_SELECTOR,
    TokenMetadataResolver,
)

from .support import make_settings


TOKEN = "0x9999999999999999999999999999999999999999"
TOKEN_2 = "0x8888888888888888888888888888888888888888"


def uint256(value: int) -> str:
    return "0x" + f"{value:064x}"


def abi_string(value: str) -> str:
    raw = value.encode()
    padded = raw + (b"\x00" * ((32 - len(raw) % 32) % 32))
    return "0x" + (
        (32).to_bytes(32, "big")
        + len(raw).to_bytes(32, "big")
        + padded
    ).hex()


class FakeMetadataRpc:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get_code(self, address):
        self.calls.append(("code", address))
        result = self.responses.get("code", "0x6000")
        if isinstance(result, Exception):
            raise result
        return result

    def eth_call(self, address, selector):
        self.calls.append((selector, address))
        result = self.responses[selector]
        if isinstance(result, Exception):
            raise result
        return result


class MetadataTests(unittest.TestCase):
    def resolve(self, responses, *, clock=lambda: 1000):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = OnchainStore(make_settings(Path(temporary.name)))
        store.migrate()
        rpc = FakeMetadataRpc(responses)
        metadata = TokenMetadataResolver(
            rpc, store, clock=clock
        ).resolve(8453, TOKEN)
        return metadata, rpc, store

    def test_verified_erc20_metadata_and_bytes32_symbol(self) -> None:
        symbol = "ABC".encode().ljust(32, b"\x00")
        metadata, _rpc, _store = self.resolve(
            {
                DECIMALS_SELECTOR: uint256(6),
                TOTAL_SUPPLY_SELECTOR: uint256(1_000_000),
                SYMBOL_SELECTOR: "0x" + symbol.hex(),
                NAME_SELECTOR: abi_string("Alpha Beta Coin"),
            }
        )
        self.assertEqual(metadata.metadata_status, "verified_erc20")
        self.assertEqual(metadata.token_kind, "erc20")
        self.assertEqual(metadata.decimals, 6)
        self.assertEqual(metadata.symbol, "ABC")
        self.assertEqual(metadata.name, "Alpha Beta Coin")

    def test_erc721_false_positive_without_decimals_is_rejected(self) -> None:
        metadata, _rpc, _store = self.resolve(
            {
                DECIMALS_SELECTOR: "0x",
                TOTAL_SUPPLY_SELECTOR: uint256(1),
                SYMBOL_SELECTOR: abi_string("NFT"),
                NAME_SELECTOR: abi_string("NFT"),
            }
        )
        self.assertEqual(metadata.metadata_status, "rejected_non_erc20")

    def test_malformed_total_supply_is_cached_as_malformed(self) -> None:
        metadata, _rpc, _store = self.resolve(
            {
                DECIMALS_SELECTOR: uint256(18),
                TOTAL_SUPPLY_SELECTOR: "0x1234",
                SYMBOL_SELECTOR: abi_string("ABC"),
                NAME_SELECTOR: abi_string("ABC"),
            }
        )
        self.assertEqual(metadata.metadata_status, "malformed")

    def test_rpc_failure_is_retryable_not_permanent_rejection(self) -> None:
        now = [1000]
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = OnchainStore(make_settings(Path(temporary.name)))
        store.migrate()
        rpc = FakeMetadataRpc(
            {
                "code": RpcTransportError("offline"),
                DECIMALS_SELECTOR: uint256(18),
                TOTAL_SUPPLY_SELECTOR: uint256(1),
                SYMBOL_SELECTOR: abi_string("ABC"),
                NAME_SELECTOR: abi_string("ABC"),
            }
        )
        resolver = TokenMetadataResolver(
            rpc, store, clock=lambda: now[0], retry_delay_sec=60
        )
        failed = resolver.resolve(8453, TOKEN)
        call_count = len(rpc.calls)
        cached = resolver.resolve(8453, TOKEN)
        self.assertEqual(failed.metadata_status, "rpc_failed")
        self.assertEqual(cached.metadata_status, "rpc_failed")
        self.assertEqual(len(rpc.calls), call_count)
        now[0] = 1061
        rpc.responses["code"] = "0x6000"
        verified = resolver.resolve(8453, TOKEN)
        self.assertEqual(verified.metadata_status, "verified_erc20")


class FakePriceResponse:
    status_code = 200

    def __init__(self, addresses):
        self.addresses = addresses

    def json(self):
        return {
            "data": [
                {
                    "attributes": {
                        "address": address,
                        "price_usd": "2.5",
                        "volume_usd": {"h24": "1000000"},
                    }
                }
                for address in self.addresses
            ]
        }


class FakePriceSession:
    def __init__(self):
        self.calls = []

    def get(self, url, *, headers, timeout):
        self.calls.append((url, headers, timeout))
        addresses = url.rsplit("/", 1)[-1].split(",")
        return FakePriceResponse(addresses)


class PriceTests(unittest.TestCase):
    def test_static_provider_is_contract_address_keyed(self) -> None:
        quote = PriceQuote(
            chain_id=8453,
            token_address=TOKEN,
            price_usd=Decimal("1"),
            volume_24h_usd=Decimal("10"),
            source="static",
            observed_at=1000,
        )
        provider = StaticPriceProvider({(8453, TOKEN): quote})
        self.assertEqual(provider.quote_many(8453, [TOKEN]), {TOKEN: quote})
        self.assertEqual(provider.quote_many(8453, [TOKEN_2]), {})

    def test_symbol_collision_cannot_price_another_contract(self) -> None:
        quote = PriceQuote(
            chain_id=8453,
            token_address=TOKEN,
            price_usd=Decimal("1"),
            volume_24h_usd=Decimal("10"),
            source="static",
            observed_at=1000,
        )
        provider = StaticPriceProvider({(8453, TOKEN): quote})
        result = provider.quote_many(8453, [TOKEN, TOKEN_2])
        self.assertEqual(result, {TOKEN: quote})

    def test_fresh_cache_is_used_and_stale_cache_is_suppressed(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = replace(
                make_settings(Path(tmp)),
                price_max_age_sec=300,
            )
            store = OnchainStore(settings)
            store.migrate()
            store.cache_price(
                PriceQuote(
                    chain_id=8453,
                    token_address=TOKEN,
                    price_usd=Decimal("2"),
                    volume_24h_usd=None,
                    source="test",
                    observed_at=900,
                )
            )
            fresh = CachedPriceService(
                settings, store, None, clock=lambda: 1000
            ).quotes(8453, [TOKEN])
            stale = CachedPriceService(
                settings, store, None, clock=lambda: 1301
            ).quotes(8453, [TOKEN])
        self.assertIn(TOKEN, fresh)
        self.assertEqual(stale, {})

    def test_missing_key_and_rate_limit_degrade_without_crash(self) -> None:
        with TemporaryDirectory() as tmp:
            base = replace(
                make_settings(Path(tmp)),
                price_enable=True,
                price_provider="coingecko_onchain",
                price_rate_limit_per_minute=1,
            )
            missing = CoinGeckoOnchainPriceProvider(base, clock=lambda: 1000)
            self.assertEqual(missing.quote_many(8453, [TOKEN]), {})
            self.assertEqual(missing.last_status, "missing_api_key")

            session = FakePriceSession()
            enabled = replace(base, coingecko_api_key="private-key")
            provider = CoinGeckoOnchainPriceProvider(
                enabled, session=session, clock=lambda: 1000
            )
            first = provider.quote_many(8453, [TOKEN])
            second = provider.quote_many(8453, [TOKEN_2])
        self.assertEqual(first[TOKEN].price_usd, Decimal("2.5"))
        self.assertEqual(second, {})
        self.assertEqual(provider.last_status, "rate_limited")
        self.assertNotIn("private-key", session.calls[0][0])


if __name__ == "__main__":
    unittest.main()
