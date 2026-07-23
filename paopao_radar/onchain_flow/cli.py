from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Sequence

from .collectors.replay import FixtureValidationError
from .config import OnchainSettings, UnsafeOnchainPath
from .db import OnchainStore
from .labels import LabelValidationError, load_labels_csv
from .runtime import replay_fixture


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Isolated on-chain CEX flow listener (P3.0 replay only)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("doctor")
    subparsers.add_parser("labels-check")
    subparsers.add_parser("db-check")

    replay = subparsers.add_parser("replay")
    replay.add_argument("--fixture", required=True)
    replay.add_argument("--send", action="store_true")
    replay.add_argument("--confirm-real-send", action="store_true")

    for command in ("once", "live"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--send", action="store_true")
        command_parser.add_argument("--confirm-real-send", action="store_true")
    return parser


def _load_chains(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        raise ValueError(f"chains file not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("chains"), list):
        raise ValueError("chains file must contain a chains list")
    chains = [item for item in data["chains"] if isinstance(item, dict)]
    for chain in chains:
        if "chain_id" not in chain or "name" not in chain:
            raise ValueError("each chain requires chain_id and name")
    return chains


def _doctor(settings: OnchainSettings) -> tuple[int, dict[str, object]]:
    checks: dict[str, object] = {}
    ok = True
    try:
        settings.assert_safe_paths()
        checks["path_isolation"] = "ok"
    except UnsafeOnchainPath as exc:
        checks["path_isolation"] = f"failed: {exc}"
        ok = False
    try:
        labels = load_labels_csv(settings.labels_path)
        checks["labels"] = {"status": "ok", "count": len(labels)}
    except LabelValidationError as exc:
        checks["labels"] = {"status": "failed", "reason": str(exc)}
        ok = False
    try:
        chains = _load_chains(settings.chains_path)
        checks["chains"] = {
            "status": "ok",
            "configured": len(chains),
            "enabled": sum(bool(chain.get("enabled", False)) for chain in chains),
            "network_checked": False,
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        checks["chains"] = {"status": "failed", "reason": str(exc)}
        ok = False
    try:
        integrity = OnchainStore.integrity_check_existing(settings.db_path)
        checks["sqlite_integrity"] = integrity
        ok = ok and integrity in {"ok", "not_initialized"}
    except sqlite3.Error as exc:
        checks["sqlite_integrity"] = f"failed: {exc}"
        ok = False
    checks["telegram"] = {
        "bot_token_configured": bool(settings.tg_bot_token),
        "chat_id_configured": bool(settings.tg_chat_id),
        "topic_id_configured": bool(settings.tg_onchain_flow_topic_id),
        "credential_values_exposed": False,
    }
    return (0 if ok else 1), {"status": "ok" if ok else "failed", "checks": checks}
def _disabled_command(settings: OnchainSettings, command: str) -> int:
    settings.assert_safe_paths()
    if not settings.enable:
        print(
            json.dumps(
                {
                    "command": command,
                    "status": "disabled",
                    "reason": "ONCHAIN_ENABLE=false",
                    "network_activity": False,
                    "database_writes": False,
                    "telegram_calls": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    print(
        json.dumps(
            {
                "command": command,
                "status": "unavailable_in_p3_0",
                "reason": "real HTTP/WSS collectors are intentionally deferred to P3.1",
                "network_activity": False,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: OnchainSettings | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or OnchainSettings.load()
    try:
        if args.command == "status":
            payload = settings.diagnostic()
            payload["db_exists"] = settings.db_path.exists()
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "doctor":
            code, payload = _doctor(settings)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return code
        if args.command == "labels-check":
            settings.assert_safe_paths()
            labels = load_labels_csv(settings.labels_path)
            print(
                json.dumps(
                    {"status": "ok", "labels": len(labels)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "db-check":
            settings.assert_safe_paths()
            result = OnchainStore.integrity_check_existing(settings.db_path)
            print(json.dumps({"integrity_check": result}, sort_keys=True))
            return 0 if result in {"ok", "not_initialized"} else 1
        if args.command == "replay":
            summary = replay_fixture(
                settings,
                Path(args.fixture),
                send=bool(args.send),
                confirm_real_send=bool(args.confirm_real_send),
            )
            print(
                json.dumps(
                    summary.as_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command in {"once", "live"}:
            return _disabled_command(settings, args.command)
    except (
        FixtureValidationError,
        LabelValidationError,
        UnsafeOnchainPath,
        OSError,
        ValueError,
    ) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": type(exc).__name__,
                    "reason": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    return 2
