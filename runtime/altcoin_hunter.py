"""Explicit offline-only Hunter CLI; importing this module performs no work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PureWindowsPath
import sqlite3
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Altcoin Hunter offline foundation and protocol simulation (no live data or delivery)")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate_parser = commands.add_parser("migrate", help="explicitly initialize an isolated database")
    migrate_parser.add_argument("--db", required=True, type=Path)
    status = commands.add_parser("status", help="read a closed/checkpointed database without writes")
    status.add_argument("--db", required=True, type=Path)
    replay = commands.add_parser("replay", help="replay into a fresh, explicitly migrated empty database")
    replay.add_argument("--db", required=True, type=Path)
    inputs = replay.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--fixture", type=Path)
    inputs.add_argument("--instruments", type=int)
    replay.add_argument("--minutes", type=int, default=10)
    replay.add_argument("--seed", type=int, default=42)
    replay.add_argument("--pattern", choices=("normal", "duplicates", "burst", "out_of_order", "late", "gap", "epoch"), default="normal")
    replay.add_argument("--no-baselines", action="store_true", help="capacity mode: committed windows remain enabled")
    validate = commands.add_parser("validate-binance-fixture", help="validate local public-protocol JSON without transport")
    validate.add_argument("--fixture", required=True, type=Path)
    validate.add_argument("--kind", required=True)
    validate.add_argument("--receive-time-ms", required=True, type=int)
    validate.add_argument("--receive-monotonic-ns", required=True, type=int)
    validate.add_argument("--universe", type=Path, help="optional explicit exchangeInfo fixture")
    plan = commands.add_parser("plan-binance-subscriptions", help="plan route-separated subscriptions offline")
    plan.add_argument("--universe", required=True, type=Path)
    plan.add_argument("--max-streams-per-connection", type=int, default=800)
    plan.add_argument("--promoted-symbols", default="", help="comma-separated exchange symbols")
    simulate = commands.add_parser("simulate-binance-connection", help="run a deterministic local connection scenario")
    simulate.add_argument("--scenario", required=True, help="built-in scenario name or local JSON file")
    simulate.add_argument("--seed", required=True, type=int)
    return parser


def _read_offline_json(path: Path):
    # Bound before decoding; no Settings/.env, URI, or remote loader is available.
    import os
    raw_path = str(path)
    windows = PureWindowsPath(raw_path)
    if raw_path.startswith(("\\\\", "//")) or windows.drive.startswith("\\\\") or "://" in raw_path:
        raise ValueError("network_fixture_path_forbidden")
    if os.name == "nt":
        import ctypes
        local_path = Path(os.path.abspath(path))
        # Mapped network drives can look like ordinary drive letters.
        drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(local_path.anchor))
        if drive_type not in (2, 3, 5, 6):
            raise ValueError("nonlocal_fixture_drive_forbidden")
        # Reject every reparse component before opening beneath it. Resolving a
        # junction first could itself follow a remote target.
        for component in (*reversed(local_path.parents), local_path):
            attributes = getattr(component.lstat(), "st_file_attributes", 0)
            if attributes & 0x400:
                raise ValueError("reparse_fixture_path_forbidden")
    with path.open("rb") as handle:
        raw = handle.read(2_000_001)
    if len(raw) > 2_000_000:
        raise ValueError("oversized_fixture")
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result
    return json.loads(raw, object_pairs_hook=pairs,
                      parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite_json")))


def _binance_offline(args) -> dict:
    from radars.altcoin_hunter.adapters.base import PROTOCOL_VERSION, deterministic_digest, plain
    from radars.altcoin_hunter.adapters.binance_usdm import parse_exchange_info
    from radars.altcoin_hunter.adapters.fixtures import FIXTURE_TIME_MS, fixture_registry
    from radars.altcoin_hunter.models import strict_int, timestamp_ms

    result = {"status": "ok", "mode": "offline_dry_run", "network_calls": 0,
              "dns_calls": 0, "real_send": False, "protocol_version": PROTOCOL_VERSION,
              "parsed_events": 0, "rejected_items": 0, "route": None,
              "stream_coverage": {}, "connection_states": {}, "epochs": {},
              "subscription_shards": [], "budget_status": {"status": "not_executed"}}
    if args.command == "validate-binance-fixture":
        from radars.altcoin_hunter.adapters.binance_protocol import parse_binance_payload, parse_funding_info, parse_server_time
        timestamp_ms(args.receive_time_ms)
        strict_int(args.receive_monotonic_ns, "receive_monotonic_ns")
        fixture = _read_offline_json(args.fixture)
        payload = fixture.get("payload", fixture) if isinstance(fixture, dict) else fixture
        directory = _read_offline_json(args.universe) if args.universe else (
            fixture.get("exchange_info") if isinstance(fixture, dict) else None)
        if isinstance(directory, dict):
            directory = directory.get("exchange_info", directory)
            parsed_directory = parse_exchange_info(directory, observed_at_ms=args.receive_time_ms)
            if not parsed_directory.accepted:
                raise ValueError("invalid_offline_directory")
            registry = parsed_directory.registry
        else:
            # Explicit, fictional static test registry; never register message symbols.
            registry = fixture_registry()
        if args.kind in {"exchange_info", "exchangeInfo"}:
            parsed = parse_exchange_info(payload, observed_at_ms=args.receive_time_ms)
            result.update(directory_status=parsed.status, instruments=[spec.to_dict() for spec in parsed.instruments],
                          rejected_items=(parsed.diagnostics or {}).get("rejected_count", len(parsed.rejected_items)), diagnostics=plain(parsed.diagnostics))
            if not parsed.accepted:
                result["status"] = "degraded"
        elif args.kind in {"funding_info", "fundingInfo"}:
            from dataclasses import asdict
            parsed = parse_funding_info(payload, registry)
            result.update(funding_info={symbol: asdict(item) for symbol, item in parsed.entries.items()},
                          rejected_items=parsed.diagnostics.get("rejected_count", len(parsed.rejected_items)), diagnostics=plain(parsed.diagnostics))
        elif args.kind in {"server_time", "serverTime"}:
            result["server_time_ms"] = parse_server_time(payload)
        else:
            parsed = parse_binance_payload(payload, args.kind, registry,
                        receive_time_ms=args.receive_time_ms,
                        receive_monotonic_ns=args.receive_monotonic_ns)
            result.update(parsed_events=len(parsed.events),
                          rejected_items=parsed.diagnostics.get("rejected_count", len(parsed.rejected_items)),
                          parse_result=parsed.to_dict())
            routes = sorted({str(item.get("route")) for item in parsed.event_metadata if item.get("route")})
            result["route"] = routes[0] if len(routes) == 1 else routes
        if result["rejected_items"]:
            result["status"] = "degraded"
    elif args.command == "plan-binance-subscriptions":
        from radars.altcoin_hunter.subscription_plan import plan_subscriptions
        from radars.altcoin_hunter.universe import instrument_from_dict
        payload = _read_offline_json(args.universe)
        if isinstance(payload, dict) and ("symbols" in payload or "exchange_info" in payload):
            directory = parse_exchange_info(payload.get("exchange_info", payload), observed_at_ms=FIXTURE_TIME_MS)
            if not directory.accepted:
                raise ValueError("invalid_offline_directory")
            instruments = tuple(spec.to_hunter_instrument() for spec in directory.instruments)
        else:
            records = payload.get("instruments") if isinstance(payload, dict) else payload
            if type(records) is not list or len(records) > 4096:
                raise ValueError("invalid_offline_universe")
            instruments = tuple(instrument_from_dict(record) for record in records)
        promoted = tuple(args.promoted_symbols.split(",")) if args.promoted_symbols else ()
        plan = plan_subscriptions(instruments, max_streams_per_connection=args.max_streams_per_connection,
                                  promoted_symbols=promoted)
        document = plan.to_dict()
        result.update(plan=document, subscription_shards=document["shards"],
                      stream_coverage=document["coverage"], route=["MARKET", "PUBLIC"])
        if plan.uncovered_streams:
            result["status"] = "degraded"
    else:
        from radars.altcoin_hunter.connection import run_connection_scenario
        scenario = args.scenario
        if scenario.endswith(".json"):
            scenario = _read_offline_json(Path(scenario))
        simulation = run_connection_scenario(scenario, args.seed)
        result.update(simulation)
        result.update(connection_states={simulation["shard_id"]: simulation["state"]},
                      epochs={simulation["shard_id"]: simulation["epoch"]},
                      stream_coverage={"required": simulation["required_streams"],
                                       "acknowledged": simulation["acknowledged_streams"],
                                       "connection_active": simulation["state"] == "ACTIVE"},
                      subscription_shards=[{"shard_id": simulation["shard_id"],
                                            "route": simulation["route"], "stream_count": simulation["required_streams"]}])
        from radars.altcoin_hunter.rest_budget import FakeCoordinator
        from radars.altcoin_hunter.rest_scheduler import OiSamplingPlanner, RestScheduler
        now_ms = simulation["now_ms"]
        coordinator = FakeCoordinator()
        scheduler = RestScheduler(clock=lambda: now_ms, coordinator=coordinator)
        sampler = OiSamplingPlanner()
        sampler.update_universe({"FAKE_OI_INSTRUMENT": "NORMAL"}, now_ms)
        sampler.schedule(scheduler, now_ms)
        due = scheduler.poll_due(now_ms)
        # Admission is demonstrated; no synthetic successful response is invented.
        result["budget_status"] = {"scheduler": scheduler.diagnostics(now_ms),
                                   "coordinator": coordinator.diagnostics(now_ms),
                                   "oi_coverage": sampler.coverage(now_ms),
                                   "admitted_request_count": len(due), "transport_executed": False}
        for request in due:
            scheduler.cancel(request.request_id)
        result["budget_status"]["after_cleanup"] = scheduler.diagnostics(now_ms)
        # A scenario is structurally incapable of using a transport implementation.
        result.update(mode="offline_dry_run", network_calls=0, dns_calls=0, real_send=False,
                      protocol_version=PROTOCOL_VERSION)
    result = plain(result)
    result.pop("deterministic_digest", None)
    result["deterministic_digest"] = deterministic_digest(result)
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command in {"validate-binance-fixture", "plan-binance-subscriptions", "simulate-binance-connection"}:
        try:
            result = _binance_offline(args)
        except (ValueError, TypeError, KeyError, OSError, RecursionError):
            result = {"status": "error", "reason": "invalid_offline_input", "mode": "offline_dry_run",
                      "network_calls": 0, "dns_calls": 0, "real_send": False}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
        return 0 if result["status"] == "ok" else 2
    # These are isolated-domain imports; Settings and existing runtimes are never loaded.
    from radars.altcoin_hunter.configuration import AltcoinHunterConfig, validate_database_path
    from radars.altcoin_hunter.read_model import HunterReadModel
    from radars.altcoin_hunter.replay import iter_synthetic_records, load_fixture, run_replay
    from radars.altcoin_hunter.storage import StorageError, migrate
    from radars.altcoin_hunter.windows import WINDOW_MINUTES

    try:
        db_path = validate_database_path(args.db)
        if args.command == "migrate":
            migration = migrate(db_path)
            result = {"status": "ok", "mode": "explicit_migration", "migration": migration,
                      "schema_version": migration["schema_version"]}
        elif args.command == "status":
            result = HunterReadModel(db_path).status()
        else:
            records = load_fixture(args.fixture) if args.fixture else iter_synthetic_records(
                args.instruments, args.minutes, args.seed, pattern=args.pattern)
            result = run_replay(db_path, records, config=AltcoinHunterConfig(enable=True, db_file=db_path),
                                max_instruments=max(1024, args.instruments or 0),
                                baseline_windows=() if args.no_baselines else WINDOW_MINUTES)
    except (StorageError, ValueError, TypeError, KeyError, OSError, sqlite3.Error) as exc:
        # Do not echo arbitrary fixture strings or local paths into diagnostics.
        reason = str(exc) if isinstance(exc, StorageError) else "invalid_offline_input"
        result = {"status": "error", "reason": reason, "mode": "offline_dry_run", "real_send": False}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
