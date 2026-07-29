from __future__ import annotations

import argparse
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Sequence

from .automation_store import AutomationStore, AutomationStoreError
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
from .report import TokenReportService
from .report_notifier import ReportNotifier
from .registry import RegistryService
from .signal_bridge import MainSignalReader, SignalBridge
from .token_activity import (
    TokenActivityQuery,
    TokenActivityQueryError,
    TokenActivityQueryService,
    failed_token_activity_payload,
)
from .token_analysis import TokenAnalysisService
from .watch_scanner import WatchScanner


def _add_token_query_arguments(
    parser: argparse.ArgumentParser,
) -> None:
    parser.add_argument("--chain", required=True)
    parser.add_argument("--contract", required=True)
    parser.add_argument("--window", required=True)
    parser.add_argument("--allow-network", action="store_true")
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--max-rpc-requests", type=int, default=None)
    parser.add_argument("--top", type=int, default=None)
    parser.add_argument("--with-price", action="store_true")
    parser.add_argument("--min-usd", default=None)
    parser.add_argument("--pretty", action="store_true")
    parser.add_argument("--output-file", default=None)


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
    _add_token_query_arguments(subparsers.add_parser("token-activity"))
    _add_token_query_arguments(subparsers.add_parser("token-analysis"))
    token_report = subparsers.add_parser("token-report")
    _add_token_query_arguments(token_report)
    token_report.add_argument("--with-ai", action="store_true")
    token_notify = subparsers.add_parser("token-notify")
    _add_token_query_arguments(token_notify)
    token_notify.add_argument("--with-ai", action="store_true")
    token_notify.add_argument("--send", action="store_true")
    token_notify.add_argument("--confirm-real-send", action="store_true")

    registry_add = subparsers.add_parser("registry-add")
    registry_add.add_argument("--market-symbol", required=True)
    registry_add.add_argument("--chain", choices=("base",), required=True)
    registry_add.add_argument("--contract", required=True)
    registry_add.add_argument("--source", default="manual")
    registry_add.add_argument("--note", default="")
    registry_verify = subparsers.add_parser("registry-verify")
    registry_verify.add_argument("--token-key", required=True)
    registry_verify.add_argument("--allow-network", action="store_true")
    registry_verify.add_argument("--set-primary", action="store_true")
    registry_verify.add_argument(
        "--accept-symbol-mismatch", action="store_true"
    )
    registry_list = subparsers.add_parser("registry-list")
    registry_list.add_argument("--status", default=None)
    registry_list.add_argument("--market-symbol", default=None)
    registry_list.add_argument("--limit", type=int, default=100)
    registry_disable = subparsers.add_parser("registry-disable")
    registry_disable.add_argument("--token-key", required=True)

    watch_add = subparsers.add_parser("watch-add")
    watch_add.add_argument("--token-key", required=True)
    watch_add.add_argument("--ttl-hours", type=int, default=None)
    watch_add.add_argument("--priority", type=int, default=None)
    watch_list = subparsers.add_parser("watch-list")
    watch_list.add_argument("--status", default=None)
    watch_list.add_argument("--due-only", action="store_true")
    watch_list.add_argument("--limit", type=int, default=100)
    watch_remove = subparsers.add_parser("watch-remove")
    watch_remove.add_argument("--token-key", required=True)

    subparsers.add_parser("bridge-once")
    watch_once = subparsers.add_parser("watch-once")
    watch_once.add_argument("--allow-network", action="store_true")
    watch_once.add_argument("--notify-dry-run", action="store_true")
    watch_once.add_argument("--with-ai", action="store_true")
    watch_once.add_argument("--send", action="store_true")
    watch_once.add_argument("--confirm-real-send", action="store_true")
    watch_live = subparsers.add_parser("watch-live")
    watch_live.add_argument("--allow-network", action="store_true")
    watch_live.add_argument("--duration-minutes", type=float, default=None)
    watch_live.add_argument("--notify-dry-run", action="store_true")
    watch_live.add_argument("--with-ai", action="store_true")
    watch_live.add_argument("--send", action="store_true")
    watch_live.add_argument("--confirm-real-send", action="store_true")

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
    automation = AutomationStore.from_settings(settings)
    try:
        automation_check = automation.doctor()
        checks["oar_automation"] = automation_check
        ok = ok and automation_check["status"] in {
            "ok",
            "not_initialized",
        }
    except (AutomationStoreError, sqlite3.Error) as exc:
        checks["oar_automation"] = {
            "status": "failed",
            "reason": str(exc),
        }
        ok = False
    try:
        source_check = MainSignalReader(settings.main_signal_db_path).read(
            checkpoint_ts=0,
            checkpoint_id=0,
            overlap_sec=0,
            bootstrap_lookback_sec=60,
            limit=1,
            now=0,
        )
        checks["main_signal_reader"] = {
            "status": source_check["status"],
            "read_only": True,
        }
        ok = ok and source_check["status"] in {
            "ok",
            "source_not_initialized",
            "source_locked",
        }
    except sqlite3.Error as exc:
        checks["main_signal_reader"] = {
            "status": "failed",
            "reason": str(exc),
        }
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
    parent = path.parent
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
        try:
            os.link(temporary_name, path)
        except FileExistsError as exc:
            code = (
                "unsafe_output_file"
                if path.is_symlink()
                else "output_file_exists"
            )
            raise TokenActivityQueryError(
                code,
                "the output target became unavailable before finalization",
            ) from exc
        except OSError as exc:
            raise TokenActivityQueryError(
                "output_write_failed",
                "the output file could not be finalized safely",
            ) from exc
    except TokenActivityQueryError:
        raise
    except OSError as exc:
        raise TokenActivityQueryError(
            "output_write_failed",
            "the output file could not be written safely",
        ) from exc
    finally:
        if temporary_name and Path(temporary_name).exists():
            try:
                Path(temporary_name).unlink()
            except OSError as exc:
                raise TokenActivityQueryError(
                    "output_write_failed",
                    "the temporary output file could not be removed safely",
                ) from exc


def _validate_token_activity_output_path(
    settings: OnchainSettings, raw_path: str
) -> Path:
    lexical_path = Path(os.path.abspath(os.path.expanduser(raw_path)))
    if lexical_path.is_symlink():
        raise TokenActivityQueryError(
            "unsafe_output_file",
            "--output-file cannot be a symbolic link",
        )
    if os.path.lexists(lexical_path):
        raise TokenActivityQueryError(
            "output_file_exists",
            "--output-file must name a new file",
        )
    repository_root = settings.base_dir.resolve()
    allowed_repository_root = repository_root / "reports" / "onchain"
    lexical_inside_repository = lexical_path.is_relative_to(repository_root)
    if lexical_inside_repository and not (
        lexical_path.is_relative_to(allowed_repository_root)
    ):
        raise TokenActivityQueryError(
            "unsafe_output_file",
            "repository output is limited to reports/onchain",
        )
    protected = {
        item.resolve() for item in settings.writable_paths
    } | {
        (settings.base_dir / relative).resolve()
        for relative in PRODUCTION_WRITE_PATHS
    } | {
        (settings.base_dir / ".env.oi").resolve(),
        (settings.base_dir / ".env.onchain").resolve(),
    }
    protected_roots = {
        (settings.base_dir / "data").resolve(),
        settings.data_dir.resolve(),
    }
    if lexical_path in protected or any(
        lexical_path.is_relative_to(root) for root in protected_roots
    ):
        raise TokenActivityQueryError(
            "unsafe_output_file",
            "--output-file cannot overwrite a BOT or on-chain state file",
        )

    parent = lexical_path.parent
    if not parent.exists() or not parent.is_dir():
        raise TokenActivityQueryError(
            "output_parent_missing",
            "--output-file parent directory must already exist",
        )
    path = parent.resolve(strict=True) / lexical_path.name
    if lexical_inside_repository:
        if not allowed_repository_root.exists():
            raise TokenActivityQueryError(
                "output_parent_missing",
                "reports/onchain must exist before repository output",
            )
        resolved_allowed_root = allowed_repository_root.resolve(strict=True)
        if (
            resolved_allowed_root != allowed_repository_root
            or not path.is_relative_to(resolved_allowed_root)
        ):
            raise TokenActivityQueryError(
                "unsafe_output_file",
                "repository output cannot traverse symbolic-link directories",
            )
    elif path.is_relative_to(repository_root):
        if (
            not allowed_repository_root.exists()
            or allowed_repository_root.resolve(strict=True)
            != allowed_repository_root
            or not path.is_relative_to(allowed_repository_root)
        ):
            raise TokenActivityQueryError(
                "unsafe_output_file",
                "resolved output path points to a protected repository path",
            )
    if path in protected or any(
        path.is_relative_to(root) for root in protected_roots
    ):
        raise TokenActivityQueryError(
            "unsafe_output_file",
            "resolved output path points to a BOT or on-chain state path",
        )
    return path


def _print_token_activity(
    payload: dict[str, object],
    *,
    pretty: bool,
    output_file: str | None,
    settings: OnchainSettings | None = None,
) -> None:
    output: dict[str, object] = payload
    if output_file:
        if settings is None:
            raise TokenActivityQueryError(
                "unsafe_output_file",
                "output settings are unavailable",
            )
        output_path = _validate_token_activity_output_path(
            settings, output_file
        )
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
    automation_network_started = False
    try:
        settings = settings or OnchainSettings.load()
        if args.command in {
            "registry-add",
            "registry-verify",
            "registry-list",
            "registry-disable",
            "watch-add",
            "watch-list",
            "watch-remove",
            "bridge-once",
            "watch-once",
            "watch-live",
        }:
            settings.validate()
            store = AutomationStore.from_settings(settings)
            if args.command == "registry-add":
                item = store.add_registry(
                    market_symbol=args.market_symbol,
                    contract=args.contract,
                    source=args.source,
                    note=args.note,
                )
                print(
                    json.dumps(
                        {"status": "ok", "token": item},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            if args.command == "registry-verify":
                if not args.allow_network:
                    raise AutomationStoreError(
                        "allow_network_required",
                        "registry-verify requires explicit --allow-network",
                    )
                automation_network_started = True
                item = RegistryService(settings, store).verify(
                    args.token_key,
                    allow_network=True,
                    set_primary=bool(args.set_primary),
                    accept_symbol_mismatch=bool(
                        args.accept_symbol_mismatch
                    ),
                )
                verification = item.pop("verification", {})
                reconciliation = item.pop("reconciliation", {})
                print(
                    json.dumps(
                        {
                            "status": "ok",
                            "token": item,
                            "verification": verification,
                            "reconciliation": reconciliation,
                            "network_activity": True,
                            "telegram_calls": False,
                            "ai_calls": False,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            if args.command == "registry-list":
                items = store.list_registry(
                    status=args.status,
                    market_symbol=args.market_symbol,
                    limit=args.limit,
                )
                print(
                    json.dumps(
                        {
                            "status": (
                                "not_initialized"
                                if items is None
                                else "ok"
                            ),
                            "tokens": items or [],
                            "database_writes": False,
                            "network_activity": False,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            if args.command == "registry-disable":
                item = store.disable_registry(args.token_key)
                print(
                    json.dumps(
                        {"status": "ok", "token": item},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            if args.command == "watch-add":
                ttl_hours = (
                    int(args.ttl_hours)
                    if args.ttl_hours is not None
                    else (settings.oar_watch_manual_ttl_sec + 3599) // 3600
                )
                priority = (
                    int(args.priority)
                    if args.priority is not None
                    else settings.oar_watch_manual_priority
                )
                if ttl_hours <= 0 or ttl_hours > 365 * 24:
                    raise AutomationStoreError(
                        "invalid_watch_ttl",
                        "--ttl-hours must be in [1, 8760]",
                    )
                if priority < 0 or priority > 100:
                    raise AutomationStoreError(
                        "invalid_watch_priority",
                        "--priority must be in [0, 100]",
                    )
                item = store.add_manual_watch(
                    args.token_key,
                    ttl_sec=ttl_hours * 3600,
                    priority=priority,
                    query_window=settings.oar_watch_query_window,
                    scan_interval_sec=settings.oar_watch_scan_interval_sec,
                    max_active_tokens=settings.oar_watch_max_active_tokens,
                )
                print(
                    json.dumps(
                        {"status": "ok", "watch": item},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            if args.command == "watch-list":
                items = store.list_watch_items(
                    status=args.status,
                    due_only=bool(args.due_only),
                    limit=args.limit,
                )
                print(
                    json.dumps(
                        {
                            "status": (
                                "not_initialized"
                                if items is None
                                else "ok"
                            ),
                            "watch_items": items or [],
                            "database_writes": False,
                            "network_activity": False,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            if args.command == "watch-remove":
                item = store.remove_manual_watch(args.token_key)
                print(
                    json.dumps(
                        {"status": "ok", "watch": item},
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
            if args.command == "bridge-once":
                payload = SignalBridge(settings, store).run_once()
                print(
                    json.dumps(
                        payload, ensure_ascii=False, sort_keys=True
                    )
                )
                return (
                    0
                    if payload["source_status"]
                    in {"ok", "source_not_initialized", "source_locked"}
                    else 1
                )
            scanner = WatchScanner(settings, store)
            if args.command == "watch-once":
                payload = scanner.run_once(
                    allow_network=bool(args.allow_network),
                    notify_dry_run=bool(args.notify_dry_run),
                    with_ai=bool(args.with_ai),
                    send=bool(args.send),
                    confirm_real_send=bool(args.confirm_real_send),
                )
            else:
                payload = scanner.run_live(
                    allow_network=bool(args.allow_network),
                    duration_minutes=args.duration_minutes,
                    notify_dry_run=bool(args.notify_dry_run),
                    with_ai=bool(args.with_ai),
                    send=bool(args.send),
                    confirm_real_send=bool(args.confirm_real_send),
                )
            print(
                json.dumps(payload, ensure_ascii=False, sort_keys=True)
            )
            return 0 if payload["status"] == "ok" else 1
        if args.command in {
            "token-activity",
            "token-analysis",
            "token-report",
            "token-notify",
        }:
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
                        f"{args.command} requires explicit --allow-network",
                    ),
                    network_activity=False,
                )
                if args.command in {"token-report", "token-notify"}:
                    payload["ai_calls"] = False
                _print_token_activity(
                    payload,
                    pretty=bool(args.pretty),
                    output_file=None,
                )
                return 1
            if args.command == "token-activity":
                service = TokenActivityQueryService.from_settings(
                    settings, query
                )
            elif args.command == "token-analysis":
                service = TokenAnalysisService.from_settings(settings, query)
            else:
                service = TokenReportService.from_settings(settings, query)
            token_activity_network_started = True
            if args.command in {"token-report", "token-notify"}:
                payload = service.execute(
                    query,
                    with_ai=bool(args.with_ai),
                )
            else:
                payload = service.execute(query)
            notification_status = ""
            if args.command == "token-notify":
                notification = ReportNotifier(settings).notify(
                    payload,
                    send=bool(args.send),
                    confirm_real_send=bool(args.confirm_real_send),
                )
                payload["notification"] = {
                    "status": notification.status,
                    "reason": notification.reason,
                    "sent": notification.sent,
                    "message_ids": notification.message_ids or [],
                    "delivery_id": notification.delivery_id,
                }
                notification_status = notification.status
            _print_token_activity(
                payload,
                pretty=bool(args.pretty),
                output_file=args.output_file,
                settings=settings,
            )
            if args.command in {
                "token-analysis",
                "token-report",
                "token-notify",
            }:
                analysis = payload.get("analysis")
                if (
                    isinstance(analysis, dict)
                    and not bool(analysis.get("complete"))
                ):
                    return 2
            if notification_status in {"blocked", "failed"}:
                return 1
            return 0 if payload["status"] == "ok" else 2
        if args.command == "status":
            payload = settings.diagnostic()
            payload["db_exists"] = settings.db_path.exists()
            payload["automation_status"] = (
                AutomationStore.from_settings(settings).status_summary()
            )
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
        if args.command in {"token-report", "token-notify"}:
            payload["ai_calls"] = False
        _print_token_activity(
            payload,
            pretty=bool(getattr(args, "pretty", False)),
            output_file=None,
        )
        return 1
    except AutomationStoreError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": exc.code,
                    "reason": str(exc),
                    "network_activity": automation_network_started,
                    "database_writes": False,
                    "telegram_calls": False,
                    "ai_calls": False,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
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
