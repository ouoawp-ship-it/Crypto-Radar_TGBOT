"""Pure authorization bounds for the two permitted public-data smoke tiers.

This module opens no files or sockets, reads no environment and creates no
output. The runner owns fresh temporary output and local proof-file safety.
A report digest detects accidental edits; it is not a signature or evidence
that a caller-supplied report really came from a network run.
"""
from __future__ import annotations

from dataclasses import InitVar, dataclass, field
import json
import math
import re
from typing import Any, Mapping

from .adapters.base import PROTOCOL_VERSION, Route, deterministic_digest
from .adapters.binance_usdm import BinanceInstrumentSpec
from .subscription_plan import ShardPlan, StreamSpec


FIRST_SMOKE_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")
FIRST_SMOKE_DURATION_SEC = 180
MAX_SMOKE_SYMBOLS = 20
MAX_SMOKE_DURATION_SEC = 600
SMOKE_REPORT_VERSION = 1
MAX_PROOF_BYTES = 65_536


def _integer(value: Any, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"invalid_{name}")
    return value


def _symbols(value: Any) -> tuple[str, ...]:
    if type(value) is not tuple or not 1 <= len(value) <= MAX_SMOKE_SYMBOLS:
        raise ValueError("invalid_smoke_symbols")
    if any(type(symbol) is not str or not re.fullmatch(r"[A-Z0-9_]{1,60}USDT", symbol)
           for symbol in value):
        raise ValueError("invalid_smoke_symbol")
    if len(set(value)) != len(value):
        raise ValueError("duplicate_smoke_symbol")
    return tuple(sorted(value))


def _report_snapshot(report: Any) -> dict[str, Any]:
    """Copy bounded native JSON without logging or preserving caller objects."""
    if not isinstance(report, Mapping):
        raise ValueError("invalid_smoke_proof")
    parents: set[int] = set()
    nodes = 0

    def visit(value: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if depth > 8 or nodes > 4096:
            raise ValueError("smoke_proof_limit")
        if value is None or type(value) is bool:
            return value
        if type(value) is int:
            if value.bit_length() > 64:
                raise ValueError("smoke_proof_limit")
            return value
        if type(value) is float:
            if not math.isfinite(value):
                raise ValueError("invalid_smoke_proof_number")
            return value
        if type(value) is str:
            if len(value) > 4096:
                raise ValueError("smoke_proof_limit")
            return value
        if not isinstance(value, Mapping) and type(value) is not list:
            raise ValueError("invalid_smoke_proof_json")
        if id(value) in parents or len(value) > 256:
            raise ValueError("smoke_proof_limit")
        parents.add(id(value))
        try:
            if isinstance(value, Mapping):
                if any(type(key) is not str or not key or len(key) > 128 for key in value):
                    raise ValueError("invalid_smoke_proof_key")
                return {key: visit(item, depth + 1) for key, item in value.items()}
            return [visit(item, depth + 1) for item in value]
        finally:
            parents.remove(id(value))

    result = visit(report, 0)
    try:
        encoded = json.dumps(result, ensure_ascii=True, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise ValueError("invalid_smoke_proof_json") from exc
    if len(encoded) > MAX_PROOF_BYTES:
        raise ValueError("smoke_proof_limit")
    return result


def seal_smoke_report(report: Mapping[str, Any]) -> dict[str, Any]:
    """Return an isolated bounded report with its canonical integrity digest."""
    result = _report_snapshot(report)
    result.pop("deterministic_digest", None)
    result["deterministic_digest"] = deterministic_digest(result)
    if len(json.dumps(result, ensure_ascii=True, separators=(",", ":")).encode("ascii")) > MAX_PROOF_BYTES:
        raise ValueError("smoke_proof_limit")
    return result


def validate_first_success_proof(report: Mapping[str, Any]) -> str:
    """Validate the first-run result contract and return its integrity digest.

    A run requests exactly 180 seconds. The final second may be reserved for
    closing both sockets within that cap, so measured network duration must
    be between 179 and 180 seconds. Runner success additionally requires all
    runtime quality gates; this function cannot prove network execution.
    """
    value = _report_snapshot(report)
    digest = value.pop("deterministic_digest", None)
    if (type(digest) is not str or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or deterministic_digest(value) != digest):
        raise ValueError("invalid_first_smoke_digest")
    expected = {
        "report_version": SMOKE_REPORT_VERSION,
        "tier": "first",
        "mode": "public_read_only_smoke",
        "status": "passed",
        "requested_duration_sec": FIRST_SMOKE_DURATION_SEC,
        "network_scope": "binance_public_only",
        "real_send": False,
        "production_writes": 0,
        "protocol_version": PROTOCOL_VERSION,
    }
    if any(type(value.get(key)) is not type(expected_value) or value.get(key) != expected_value
           for key, expected_value in expected.items()):
        raise ValueError("invalid_first_smoke_result")
    proof_symbols = value.get("symbols")
    if type(proof_symbols) is not list or _symbols(tuple(proof_symbols)) != tuple(sorted(FIRST_SMOKE_SYMBOLS)):
        raise ValueError("invalid_first_smoke_symbols")
    _integer(value.get("actual_duration_ms"), "first_smoke_duration", 179_000, 180_000)
    if type(value.get("violations")) is not list or value["violations"]:
        raise ValueError("first_smoke_has_violations")
    return digest


@dataclass(frozen=True, slots=True)
class SmokeConfig:
    """No implicit enable, URLs, credentials, environment or production path.

    The first proof is validated on construction and only its digest is kept;
    subsequent mutation of the supplied mapping cannot change authorization.
    """

    enable: bool = False
    tier: str = "first"
    symbols: tuple[str, ...] = FIRST_SMOKE_SYMBOLS
    duration_sec: int = FIRST_SMOKE_DURATION_SEC
    first_success_proof: InitVar[Mapping[str, Any] | None] = None
    first_success_digest: str | None = field(init=False, default=None)

    def __post_init__(self, first_success_proof: Mapping[str, Any] | None) -> None:
        if type(self.enable) is not bool:
            raise ValueError("smoke_enable_must_be_bool")
        if type(self.tier) is not str or self.tier not in {"first", "expanded"}:
            raise ValueError("invalid_smoke_tier")
        symbols = _symbols(self.symbols)
        _integer(self.duration_sec, "smoke_duration", 1, MAX_SMOKE_DURATION_SEC)
        if self.tier == "first":
            if symbols != tuple(sorted(FIRST_SMOKE_SYMBOLS)) or self.duration_sec != FIRST_SMOKE_DURATION_SEC:
                raise ValueError("first_smoke_requires_fixed_five_and_180_seconds")
            if first_success_proof is not None:
                raise ValueError("first_smoke_does_not_use_prior_proof")
        else:
            digest = validate_first_success_proof(first_success_proof)
            object.__setattr__(self, "first_success_digest", digest)
        object.__setattr__(self, "symbols", symbols)

    def require_enabled(self) -> None:
        if not self.enable:
            raise ValueError("public_smoke_disabled")

    def deadline_ns(self, started_monotonic_ns: int) -> int:
        """Absolute runtime cap, including transport start and stop work."""
        _integer(started_monotonic_ns, "smoke_start", 0, 2**63 - 1)
        return started_monotonic_ns + self.duration_sec * 1_000_000_000

    def to_dict(self) -> dict[str, Any]:
        return {"enable": self.enable, "tier": self.tier, "symbols": list(self.symbols),
                "duration_sec": self.duration_sec, "first_success_digest": self.first_success_digest,
                "network_scope": "binance_public_only", "real_send": False,
                "production_writes": 0, "report_version": SMOKE_REPORT_VERSION,
                "global_streams_enabled": False}


def smoke_shards(registry: Mapping[str, BinanceInstrumentSpec], symbols: tuple[str, ...]
                 ) -> tuple[ShardPlan, ...]:
    """Plan only selected instruments; full-directory metadata adds no streams.

    Each selected instrument receives aggTrade, three-second mark price and
    real-time top-of-book. No global arrays, liquidation or depth are requested.
    """
    selected = _symbols(symbols)
    if not isinstance(registry, Mapping) or len(registry) > 100_000:
        raise ValueError("invalid_smoke_registry")
    market, public = [], []
    for symbol in selected:
        spec = registry.get(symbol)
        if (not isinstance(spec, BinanceInstrumentSpec) or not spec.eligible or spec.symbol != symbol
                or spec.identity.exchange != "binance" or spec.identity.market != "usdt_perpetual"
                or spec.identity.quote_currency != "USDT"):
            raise ValueError("smoke_instrument_unavailable")
        instrument_id = spec.identity.instrument_id
        for target, route, suffix, kind, interval in (
            (market, Route.MARKET, "@aggTrade", "agg_trade", 100),
            (market, Route.MARKET, "@markPrice", "mark_price_symbol", 3000),
            (public, Route.PUBLIC, "@bookTicker", "book_ticker", 0),
        ):
            wire = symbol.lower() + suffix
            target.append(StreamSpec(route, wire.lower(), wire, symbol, kind, interval, instrument_id))
    return (
        ShardPlan(Route.MARKET, "market-0000", tuple(sorted(market, key=lambda stream: stream.canonical_name))),
        ShardPlan(Route.PUBLIC, "public-0000", tuple(sorted(public, key=lambda stream: stream.canonical_name))),
    )
