"""Explicit offline-only Hunter CLI; importing this module performs no work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Altcoin Hunter P1A offline foundation (no live data or delivery)")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
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
