from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Sequence

from .collectors.replay import FixtureValidationError
from .collectors.evm_http import RpcError
from .collectors.evm_ws import WssError
from .config import OnchainSettings, SettingsValidationError, UnsafeOnchainPath
from .constants import PRODUCTION_WRITE_PATHS
from .db import OnchainStore
from .health import read_runtime_status
from .labels import (
    LabelValidationError,
    load_labels_csv,
    validate_live_labels,
)
from .live_runtime import (
    BaseOnchainRuntime,
    LiveConfigurationError,
    ReorgManualInterventionRequired,
)
from .runtime import replay_fixture
from .token_activity import (
    TokenActivityQuery,
    TokenActivityQueryError,
    TokenActivityQueryService,
    failed_token_activity_payload,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Isolated Base on-chain CEX flow listener (P3.1 dry-run)"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    subparsers.add_parser("doctor")
    subparsers.add_parser("labels-check")
    subparsers.add_parser("db-check")
    provider_check = subparsers.add_parser("provider-check")
    provider_check.add_argument("--chain", choices=("base",), required=True)
    cursor_status = subparsers.add_parser("cursor-status")
    cursor_status.add_argument("--chain", choices=("base",), required=True)
    token_activity = subparsers.add_parser("token-activity")
    token_activity.add_argument("--chain", required=True)
    token_activity.add_argument("--contract", required=True)
    token_activity.add_argument("--window", required=True)
    token_activity.add_argument("--allow-network", action="store_true")
    token_activity.add_argument("--max-events", type=int, default=None)
    token_activity.add_argument("--max-rpc-requests", type=int, default=None)
    token_activity.add_argument("--top", type=int, default=None)
    token_activity.add_argument("--with-price", action="store_true")
    token_activity.add_argument("--min-usd", default=None)
    token_activity.add_argument("--pretty", action="store_true")
    token_activity.add_argument("--output-file", default=None)

    replay = subparsers.add_parser("replay")
    replay.add_argument("--fixture", required=True)
    replay.add_argument("--send", action="store_true")
    replay.add_argument("--confirm-real-send", action="store_true")

    for command in ("once", "live"):
        command_parser = subparsers.add_parser(command)
        command_parser.add_argument("--send", action="store_true")
        command_parser.add_argument("--confirm-real-send", action="store_true")
        if command == "live":
            command_parser.add_argument(
                "--duration-minutes", type=float, default=None
            )
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
    settings.validate()
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
        if settings.enable or settings.base_enable:
            validate_live_labels(
                labels,
                min_confidence=settings.min_label_confidence,
                chain_id=settings.base_chain_id,
            )
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
            "base_http_configured": bool(settings.base_http_rpc_url),
            "base_wss_configured": bool(settings.base_wss_rpc_url),
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
    settings.validate()
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
    if not settings.base_enable:
        print(
            json.dumps(
                {
                    "command": command,
                    "status": "disabled",
                    "reason": "ONCHAIN_BASE_ENABLE=false",
                    "network_activity": False,
                    "database_writes": False,
                    "telegram_calls": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0
    return -1


def _write_token_activity_output(
    path: Path,
    payload: dict[str, object],
    *,
    pretty: bool,
) -> None:
    parent = path.resolve().parent
    if not parent.exists():
        raise OSError("output directory does not exist")
    text = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )
    temporary_name = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    finally:
        if temporary_name and Path(temporary_name).exists():
            Path(temporary_name).unlink()


def _validate_token_activity_output_path(
    settings: OnchainSettings, raw_path: str
) -> None:
    path = Path(raw_path).resolve()
    data_root = (settings.base_dir / "data").resolve()
    protected = {
        item.resolve() for item in settings.writable_paths
    } | {
        (settings.base_dir / relative).resolve()
        for relative in PRODUCTION_WRITE_PATHS
    } | {
        (settings.base_dir / ".env.oi").resolve(),
        (settings.base_dir / ".env.onchain").resolve(),
    }
    if path in protected or path.is_relative_to(data_root):
        raise TokenActivityQueryError(
            "unsafe_output_file",
            "--output-file cannot overwrite a BOT or on-chain state file",
        )


def _print_token_activity(
    payload: dict[str, object],
    *,
    pretty: bool,
    output_file: str | None,
) -> None:
    output: dict[str, object] = payload
    if output_file:
        output_path = Path(output_file)
        _write_token_activity_output(output_path, payload, pretty=pretty)
        summary = payload.get("summary")
        output = {
            "schema_version": payload.get("schema_version"),
            "status": payload.get("status"),
            "complete": payload.get("complete"),
            "truncated": payload.get("truncated"),
            "truncation_reason": payload.get("truncation_reason"),
            "transfer_count": (
                summary.get("transfer_count")
                if isinstance(summary, dict)
                else None
            ),
            "output_file": str(output_path),
        }
    print(
        json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
            indent=2 if pretty else None,
            separators=None if pretty else (",", ":"),
        )
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: OnchainSettings | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    runtime: BaseOnchainRuntime | None = None
    token_activity_network_started = False
    try:
        settings = settings or OnchainSettings.load()
        if args.command == "token-activity":
            settings.validate()
            query = TokenActivityQuery.create(
                settings,
                chain=args.chain,
                contract=args.contract,
                window=args.window,
                max_events=args.max_events,
                max_rpc_requests=args.max_rpc_requests,
                top_n=args.top,
                with_price=bool(args.with_price),
                min_usd=args.min_usd,
            )
            if args.output_file:
                _validate_token_activity_output_path(
                    settings, args.output_file
                )
            if not args.allow_network:
                payload = failed_token_activity_payload(
                    TokenActivityQueryError(
                        "allow_network_required",
                        "token-activity requires explicit --allow-network",
                    ),
                    network_activity=False,
                )
                _print_token_activity(
                    payload,
                    pretty=bool(args.pretty),
                    output_file=None,
                )
                return 1
            service = TokenActivityQueryService.from_settings(
                settings, query
            )
            token_activity_network_started = True
            payload = service.execute(query)
            _print_token_activity(
                payload,
                pretty=bool(args.pretty),
                output_file=args.output_file,
            )
            return 0 if payload["status"] == "ok" else 2
        if args.command == "status":
            payload = settings.diagnostic()
            payload["db_exists"] = settings.db_path.exists()
            payload["runtime"] = read_runtime_status(
                settings.runtime_status_path
            )
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "doctor":
            code, payload = _doctor(settings)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return code
        if args.command == "labels-check":
            settings.validate()
            labels = load_labels_csv(settings.labels_path)
            if settings.enable or settings.base_enable:
                validate_live_labels(
                    labels,
                    min_confidence=settings.min_label_confidence,
                    chain_id=settings.base_chain_id,
                )
            print(
                json.dumps(
                    {"status": "ok", "labels": len(labels)},
                    ensure_ascii=False,
                    sort_keys=True,
                )
            )
            return 0
        if args.command == "db-check":
            settings.validate()
            result = OnchainStore.integrity_check_existing(settings.db_path)
            print(json.dumps({"integrity_check": result}, sort_keys=True))
            return 0 if result in {"ok", "not_initialized"} else 1
        if args.command == "provider-check":
            payload = BaseOnchainRuntime(settings).provider_check()
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command == "cursor-status":
            settings.validate()
            if not settings.db_path.exists():
                payload = {
                    "status": "not_initialized",
                    "chain": "base",
                    "chain_id": settings.base_chain_id,
                    "cursor_block": None,
                }
            else:
                cursor = OnchainStore(settings).cursor(
                    settings.base_chain_id
                )
                payload = {
                    "status": "ok" if cursor is not None else "not_initialized",
                    "chain": "base",
                    "chain_id": settings.base_chain_id,
                    "cursor_block": (
                        cursor.last_finalized_block
                        if cursor is not None
                        else None
                    ),
                    "last_seen_head": (
                        cursor.last_seen_head if cursor is not None else None
                    ),
                    "updated_at": (
                        cursor.updated_at if cursor is not None else None
                    ),
                }
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
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
            disabled_code = _disabled_command(settings, args.command)
            if disabled_code >= 0:
                return disabled_code
            runtime = BaseOnchainRuntime(settings)
            if args.command == "once":
                payload = runtime.process_once(
                    send=bool(args.send),
                    confirm_real_send=bool(args.confirm_real_send),
                )
            else:
                if (
                    args.duration_minutes is not None
                    and args.duration_minutes < 0
                ):
                    raise ValueError(
                        "--duration-minutes must be non-negative"
                    )
                payload = runtime.run_live(
                    duration_minutes=args.duration_minutes,
                    send=bool(args.send),
                    confirm_real_send=bool(args.confirm_real_send),
                )
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0
    except TokenActivityQueryError as exc:
        payload = failed_token_activity_payload(
            exc,
            network_activity=token_activity_network_started,
        )
        _print_token_activity(
            payload,
            pretty=bool(getattr(args, "pretty", False)),
            output_file=None,
        )
        return 1
    except (
        FixtureValidationError,
        LabelValidationError,
        LiveConfigurationError,
        ReorgManualInterventionRequired,
        RpcError,
        SettingsValidationError,
        UnsafeOnchainPath,
        WssError,
        sqlite3.Error,
        OSError,
        ValueError,
    ) as exc:
        if runtime is not None and args.command in {"once", "live"}:
            try:
                runtime.record_failure(exc, mode=args.command)
            except (OSError, ValueError):
                pass
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
