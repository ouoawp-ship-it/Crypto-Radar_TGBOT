from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

from .ai_client import (
    OarAiCache,
    OarAiError,
    OpenAiCompatibleOarClient,
    build_ai_request_diagnostics,
    validate_ai_output,
)
from .ai_context import build_ai_context
from .address_intelligence import (
    AddressIntelligenceError,
    AddressIntelligenceService,
    AddressIntelligenceStore,
    ManualCsvProvider,
    OliParquetProvider,
    PROVIDER_NAMES,
)
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
    is_approved_label,
    load_labels_csv,
    validate_live_labels,
)
from .arkham_intelligence import ArkhamIntelligenceError
from .label_candidates import (
    LabelCandidateDiscovery,
    LabelCandidateError,
    LabelCandidateStore,
    label_readiness,
)
from .live_runtime import (
    BaseOnchainRuntime,
    LiveConfigurationError,
    ReorgManualInterventionRequired,
)
from .prompt_manager import OperatorPromptError, OperatorPromptManager
from .runtime import replay_fixture
from .report import TokenReportService, restricted_ai_input
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
from .telegram_topic_link import (
    TelegramTopicLinkError,
    validate_telegram_topic_link,
)
from .telegram_route_check import TelegramRouteChecker, save_route_check
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
    subparsers.add_parser("ai-prompt-check")
    ai_prompt = subparsers.add_parser("ai-prompt")
    ai_prompt.add_argument(
        "action",
        choices=(
            "status",
            "show",
            "validate",
            "install-default",
            "save",
            "restore-default",
            "history",
            "rollback",
            "hash",
        ),
    )
    ai_prompt.add_argument("--version", default="")
    ai_prompt.add_argument("--stdin", action="store_true")
    ai_cache = subparsers.add_parser("ai-cache")
    ai_cache.add_argument(
        "action",
        choices=("status", "clear-results"),
    )
    ai_provider_check = subparsers.add_parser("ai-provider-check")
    ai_provider_check.add_argument("--allow-network", action="store_true")
    ai_smoke = subparsers.add_parser("ai-smoke")
    ai_smoke.add_argument("--allow-network", action="store_true")
    ai_request_check = subparsers.add_parser("ai-request-check")
    _add_token_query_arguments(ai_request_check)
    telegram_topic_link = subparsers.add_parser("telegram-topic-link")
    telegram_topic_link.add_argument("action", choices=("check", "bind"))
    telegram_topic_link.add_argument("--stdin", action="store_true")
    telegram_topic_link.add_argument(
        "--url",
        default=None,
        help=argparse.SUPPRESS,
    )
    telegram_route_check = subparsers.add_parser("telegram-route-check")
    telegram_route_check.add_argument("--allow-network", action="store_true")
    label_candidates = subparsers.add_parser("label-candidates")
    candidate_actions = label_candidates.add_subparsers(
        dest="candidate_action",
        required=True,
    )
    candidate_provider = candidate_actions.add_parser("provider-check")
    candidate_provider.add_argument("--allow-network", action="store_true")
    candidate_discover = candidate_actions.add_parser("discover")
    candidate_discover.add_argument(
        "--chain", choices=("base",), required=True
    )
    candidate_discover.add_argument("--contract", required=True)
    candidate_discover.add_argument(
        "--window", choices=("4h",), required=True
    )
    candidate_discover.add_argument(
        "--max-addresses", type=int, default=None
    )
    candidate_discover.add_argument("--allow-network", action="store_true")
    candidate_list = candidate_actions.add_parser("list")
    candidate_list.add_argument(
        "--status", choices=("pending", "approved", "rejected"), default=None
    )
    candidate_list.add_argument("--limit", type=int, default=100)
    candidate_approve = candidate_actions.add_parser("approve")
    candidate_approve.add_argument("--candidate-id", required=True)
    candidate_reject = candidate_actions.add_parser("reject")
    candidate_reject.add_argument("--candidate-id", required=True)
    address_intelligence = subparsers.add_parser(
        "address-intelligence"
    )
    intelligence_actions = address_intelligence.add_subparsers(
        dest="intelligence_action",
        required=True,
    )
    intelligence_actions.add_parser("providers")
    intelligence_queue = intelligence_actions.add_parser("queue")
    intelligence_queue.add_argument("--limit", type=int, default=50)
    intelligence_discover = intelligence_actions.add_parser("discover")
    intelligence_discover.add_argument(
        "--provider",
        action="append",
        choices=(*PROVIDER_NAMES, "all"),
        default=[],
    )
    intelligence_discover.add_argument(
        "--max-addresses", type=int, default=50
    )
    intelligence_discover.add_argument(
        "--allow-network", action="store_true"
    )
    intelligence_candidates = intelligence_actions.add_parser(
        "candidates"
    )
    intelligence_candidates.add_argument(
        "--status",
        choices=(
            "pending",
            "approved",
            "rejected",
            "expired",
            "conflicted",
        ),
        default=None,
    )
    intelligence_candidates.add_argument("--limit", type=int, default=100)
    intelligence_actions.add_parser("status")
    intelligence_actions.add_parser("approved")
    for review_action in ("approve", "reject", "defer", "revoke"):
        review = intelligence_actions.add_parser(review_action)
        review.add_argument("--candidate-id", required=True)
        review.add_argument("--note", default="")
    import_dune = intelligence_actions.add_parser("import-dune")
    import_dune.add_argument("--file", required=True)
    import_dune.add_argument(
        "--table",
        choices=("cex.addresses", "cex.deposit_addresses"),
        required=True,
    )
    import_oli = intelligence_actions.add_parser("import-oli")
    import_oli.add_argument("--file", required=True)
    import_basescan = intelligence_actions.add_parser("import-basescan")
    import_basescan.add_argument("--file", required=True)
    telegram_topic_link.add_argument(
        "unsafe_link_argv",
        nargs="*",
        help=argparse.SUPPRESS,
    )
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
    unresolved_summary = subparsers.add_parser("unresolved-summary")
    unresolved_summary.add_argument("--limit", type=int, default=20)
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


def _ai_client(
    settings: OnchainSettings,
    *,
    operator_prompt: str = "",
    operator_prompt_hash: str = "",
    max_retries: int | None = None,
) -> OpenAiCompatibleOarClient:
    return OpenAiCompatibleOarClient(
        base_url=settings.oar_ai_base_url,
        api_key=settings.oar_ai_api_key,
        model=settings.oar_ai_model,
        timeout_sec=settings.oar_ai_timeout_sec,
        max_retries=(
            settings.oar_ai_max_retries
            if max_retries is None
            else int(max_retries)
        ),
        max_output_chars=settings.oar_ai_max_output_chars,
        provider=settings.oar_ai_provider,
        thinking_mode=settings.oar_ai_thinking_mode,
        reasoning_effort=settings.oar_ai_reasoning_effort,
        max_tokens=settings.oar_ai_max_tokens,
        operator_prompt=operator_prompt,
        operator_prompt_hash=operator_prompt_hash,
    )


def _ai_prerequisites(settings: OnchainSettings) -> None:
    if not (
        settings.oar_ai_base_url
        and settings.oar_ai_api_key
        and settings.oar_ai_model
    ):
        raise OarAiError(
            "ai_not_configured",
            "AI provider configuration is incomplete",
        )
    settings.validate()


def _ai_synthetic_context() -> dict[str, object]:
    return {
        "schema_version": 2,
        "context_hash": (
            "0000000000000000000000000000000000000000000000000000000000000000"
        ),
        "token": {
            "chain": "base",
            "chain_id": 8453,
            "contract": "0x1111111111111111111111111111111111111111",
            "symbol": "TEST",
            "name": "Synthetic OAR Fixture",
            "decimals": 18,
        },
        "query": {
            "window": "15m",
            "from_time": 1700000000,
            "to_time": 1700000900,
            "complete": False,
            "truncated": False,
        },
        "transfer_summary": {"transfer_count": 0},
        "largest_transfers": [],
        "cex_flows": {},
        "primary_behavior": {
            "type": "no_activity",
            "label": "未发现近期活动",
            "score": 0,
        },
        "behavior_candidates": [],
        "wallet_groups": [],
        "supporting_evidence": [],
        "counter_evidence": [],
        "data_limitations": ["synthetic_smoke_context"],
    }


def _ai_request_check(
    settings: OnchainSettings,
    query: TokenActivityQuery,
) -> tuple[int, dict[str, object]]:
    analyzed = TokenAnalysisService.from_settings(settings, query).execute(
        query
    )
    context = build_ai_context(
        analyzed,
        max_chars=settings.oar_ai_max_context_chars,
    )
    prompt = OperatorPromptManager.from_settings(
        settings
    ).load_for_request()
    restricted = restricted_ai_input(analyzed)
    request = build_ai_request_diagnostics(
        context,
        restricted,
        settings.oar_ai_model,
        provider=settings.oar_ai_provider,
        operator_prompt=prompt.content,
        operator_prompt_hash=prompt.prompt_hash,
        thinking_mode=settings.oar_ai_thinking_mode,
        reasoning_effort=settings.oar_ai_reasoning_effort,
        max_tokens=settings.oar_ai_max_tokens,
        timeout_sec=settings.oar_ai_timeout_sec,
    )
    analysis = analyzed.get("analysis")
    analysis_map = analysis if isinstance(analysis, dict) else {}
    source_diagnostics = analyzed.get("diagnostics")
    source_diagnostics_map = (
        source_diagnostics
        if isinstance(source_diagnostics, dict)
        else {}
    )
    payload = {
        "status": analyzed.get("status"),
        "analysis_status": analysis_map.get("status"),
        "analysis_complete": bool(analysis_map.get("complete")),
        **{
            key: request[key]
            for key in (
                "provider",
                "model",
                "thinking_mode",
                "reasoning_effort",
                "timeout_sec",
                "max_tokens",
                "restricted_input",
                "operator_prompt_chars",
                "ai_context_chars",
                "request_body_chars",
            )
        },
        "network_activity": True,
        "rpc_calls": int(
            source_diagnostics_map.get("rpc_request_count")
            or analyzed.get("rpc_request_count")
            or 0
        ),
        "ai_calls": 0,
        "telegram_calls": 0,
    }
    return (0 if payload["status"] == "ok" else 2), payload


def _prompt_command(
    settings: OnchainSettings,
    args: argparse.Namespace,
) -> int:
    manager = OperatorPromptManager.from_settings(settings)
    if args.command == "ai-prompt-check":
        payload = {
            **manager.status(),
            "network_activity": False,
            "rpc_calls": 0,
            "ai_calls": 0,
            "telegram_calls": 0,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0 if payload["status"] != "invalid" else 1
    action = args.action
    if action == "show":
        print(manager.show())
        return 0
    if action == "status":
        result = manager.status()
    elif action == "validate":
        result = manager.validate()
    elif action == "install-default":
        prompt = manager.install_default()
        result = {
            "status": "ok",
            "length": len(prompt.content),
            "prompt_hash": prompt.prompt_hash,
        }
    elif action == "save":
        if not args.stdin:
            raise OperatorPromptError(
                "ai-prompt save requires --stdin"
            )
        prompt = manager.save(sys.stdin.read())
        result = {
            "status": "ok",
            "length": len(prompt.content),
            "prompt_hash": prompt.prompt_hash,
        }
    elif action == "restore-default":
        prompt = manager.restore_default()
        result = {
            "status": "ok",
            "length": len(prompt.content),
            "prompt_hash": prompt.prompt_hash,
        }
    elif action == "history":
        result = {"status": "ok", "history": manager.history()}
    elif action == "rollback":
        if not args.version:
            raise OperatorPromptError(
                "ai-prompt rollback requires --version"
            )
        prompt = manager.rollback(args.version)
        result = {
            "status": "ok",
            "length": len(prompt.content),
            "prompt_hash": prompt.prompt_hash,
        }
    elif action == "hash":
        result = {"status": "ok", "prompt_hash": manager.hash()}
    else:  # pragma: no cover - argparse constrains the action
        raise OperatorPromptError("unsupported operator prompt action")
    result.update(
        {
            "network_activity": False,
            "rpc_calls": 0,
            "ai_calls": 0,
            "telegram_calls": 0,
        }
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _ai_cache_command(
    settings: OnchainSettings,
    args: argparse.Namespace,
) -> int:
    cache = OarAiCache(
        path=settings.oar_ai_cache_path,
        data_dir=settings.data_dir,
        ttl_sec=settings.oar_ai_cache_ttl_sec,
        max_calls_per_hour=settings.oar_ai_max_calls_per_hour,
    )
    result = (
        cache.status()
        if args.action == "status"
        else cache.clear_results()
    )
    if args.action == "status":
        result.pop("status", None)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


def _ai_network_command(
    settings: OnchainSettings,
    args: argparse.Namespace,
) -> int:
    if not args.allow_network:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": "allow_network_required",
                    "network_activity": False,
                    "rpc_calls": 0,
                    "ai_calls": 0,
                    "telegram_calls": 0,
                },
                sort_keys=True,
            )
        )
        return 1
    started = time.perf_counter()
    network_started = False
    try:
        _ai_prerequisites(settings)
        if args.command == "ai-provider-check":
            network_started = True
            result = _ai_client(settings).check_model()
            payload = {
                **result,
                "provider": settings.oar_ai_provider,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "network_activity": True,
                "generation_calls": 0,
                "rpc_calls": 0,
                "telegram_calls": 0,
            }
        else:
            prompt = OperatorPromptManager.from_settings(
                settings
            ).load_for_request()
            network_started = True
            ai_result = _ai_client(
                settings,
                operator_prompt=prompt.content,
                operator_prompt_hash=prompt.prompt_hash,
                max_retries=0,
            ).analyze(
                _ai_synthetic_context(),
                restricted_input=True,
            )
            validate_ai_output(ai_result, restricted_input=True)
            payload = {
                "status": "ok",
                "model": settings.oar_ai_model,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "schema_valid": True,
            }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    except OarAiError as exc:
        payload = {
            "status": "failed",
            "error": exc.code,
            **exc.public_details(),
            "network_activity": network_started,
            "ai_calls": (
                1
                if args.command == "ai-smoke" and network_started
                else 0
            ),
            "rpc_calls": 0,
            "telegram_calls": 0,
        }
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 1
    except (OperatorPromptError, SettingsValidationError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": type(exc).__name__,
                    "reason": str(exc),
                    "network_activity": network_started,
                    "ai_calls": 0,
                    "rpc_calls": 0,
                    "telegram_calls": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1


def _telegram_topic_link_command(
    settings: OnchainSettings,
    args: argparse.Namespace,
) -> int:
    if (
        not args.stdin
        or args.url is not None
        or bool(args.unsafe_link_argv)
    ):
        raise TelegramTopicLinkError(
            "topic_link_invalid",
            "Telegram topic link must be provided through stdin",
        )
    raw_link = sys.stdin.readline(4097)
    parsed = validate_telegram_topic_link(
        raw_link,
        configured_chat_id=settings.tg_chat_id,
    )
    if args.action == "check":
        payload = parsed.public_result()
        payload.pop("topic_configured", None)
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0

    from scripts.paopao_config import ConfigManager, ConfigManagerError

    try:
        ConfigManager(settings.base_dir).set(
            "TG_ONCHAIN_FLOW_TOPIC_ID",
            str(parsed.topic_id),
        )
    except (ConfigManagerError, OSError, UnicodeError) as exc:
        raise TelegramTopicLinkError(
            "topic_link_invalid",
            "Telegram topic configuration failed",
        ) from exc
    print("TG_ONCHAIN_FLOW_TOPIC_ID=configured")
    return 0


def main(
    argv: Sequence[str] | None = None,
    *,
    settings: OnchainSettings | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    runtime: BaseOnchainRuntime | None = None
    token_activity_network_started = False
    automation_network_started = False
    arkham_network_started = False
    address_provider_network_started = False
    try:
        settings = settings or OnchainSettings.load()
        if args.command == "ai-cache":
            settings.validate()
            return _ai_cache_command(settings, args)
        if args.command in {"ai-prompt-check", "ai-prompt"}:
            settings.validate()
            return _prompt_command(settings, args)
        if args.command in {"ai-provider-check", "ai-smoke"}:
            return _ai_network_command(settings, args)
        if args.command == "telegram-topic-link":
            settings.validate()
            return _telegram_topic_link_command(settings, args)
        if args.command == "telegram-route-check":
            settings.validate()
            if not args.allow_network:
                payload = TelegramRouteChecker._empty_result()
                payload["error"] = "allow_network_required"
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
                return 1
            payload = TelegramRouteChecker(settings).check()
            save_route_check(settings, payload)
            print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            return 0 if payload["status"] == "ok" else 1
        if args.command == "address-intelligence":
            settings.validate()
            store = AddressIntelligenceStore.from_settings(settings)
            action = args.intelligence_action
            if action == "providers":
                print(json.dumps(
                    AddressIntelligenceService(settings).provider_status(),
                    ensure_ascii=False,
                    sort_keys=True,
                ))
                return 0
            if action == "status":
                print(json.dumps(
                    store.status(),
                    ensure_ascii=False,
                    sort_keys=True,
                ))
                return 0
            if action == "queue":
                items = store.unknown_queue(limit=args.limit)
                print(json.dumps({
                    "status": (
                        "ok"
                        if settings.address_intelligence_path.exists()
                        else "not_initialized"
                    ),
                    "items": items,
                    "count": len(items),
                    "network_activity": False,
                    "telegram_calls": 0,
                    "ai_calls": 0,
                }, ensure_ascii=False, sort_keys=True))
                return 0
            if action == "approved":
                labels = (
                    load_labels_csv(settings.labels_path)
                    if settings.labels_path.exists()
                    else []
                )
                approved = [
                    {
                        "chain_id": label.chain_id,
                        "address": label.address,
                        "entity_name": label.entity_name,
                        "entity_type": label.entity_type,
                        "address_role": label.address_type,
                        "source": label.source,
                        "confidence": label.confidence,
                        "valid_from": label.valid_from,
                        "valid_to": label.valid_to,
                        "evidence_hash": label.evidence_hash,
                    }
                    for label in labels
                    if is_approved_label(label)
                ]
                print(json.dumps({
                    "status": "ok",
                    "labels": approved,
                    "count": len(approved),
                    "network_activity": False,
                    "telegram_calls": 0,
                    "ai_calls": 0,
                }, ensure_ascii=False, sort_keys=True))
                return 0
            if action == "discover":
                providers = args.provider or [
                    "behavior_inference",
                    "dune_cex_deposit",
                    "dune_cex",
                    "arkham_optional",
                ]
                address_provider_network_started = bool(
                    args.allow_network
                    and (
                        settings.dune_api_key
                        or settings.arkham_api_key
                    )
                    and any(
                        name in {
                            "dune_cex",
                            "dune_cex_deposit",
                            "arkham_optional",
                            "all",
                        }
                        for name in providers
                    )
                )
                payload = AddressIntelligenceService(
                    settings
                ).discover(
                    provider_names=providers,
                    allow_network=bool(args.allow_network),
                    limit=args.max_addresses,
                )
                print(json.dumps(
                    payload, ensure_ascii=False, sort_keys=True
                ))
                return 0
            if action == "candidates":
                items = store.list_candidates(
                    status=args.status,
                    limit=args.limit,
                )
                for item in items:
                    if item.get("approval_block_reason") == (
                        "more_specific_role_candidate_available"
                    ):
                        item["approval_message"] = (
                            "存在更具体的地址角色候选，请审核具体角色候选。"
                        )
                print(json.dumps({
                    "status": "ok",
                    "candidates": items,
                    "count": len(items),
                    "network_activity": False,
                    "telegram_calls": 0,
                    "ai_calls": 0,
                }, ensure_ascii=False, sort_keys=True))
                return 0
            if action == "approve":
                payload = store.approve(
                    args.candidate_id,
                    labels_path=settings.labels_path,
                )
            elif action == "reject":
                payload = store.reject(
                    args.candidate_id,
                    note=args.note or "manual_review",
                )
            elif action == "defer":
                payload = store.defer(
                    args.candidate_id,
                    note=args.note or "manual_defer",
                )
            elif action == "revoke":
                payload = store.revoke(
                    args.candidate_id,
                    labels_path=settings.labels_path,
                )
            elif action == "import-dune":
                provider_name = (
                    "dune_cex_deposit"
                    if args.table == "cex.deposit_addresses"
                    else "dune_cex"
                )
                candidates = ManualCsvProvider(
                    provider_name
                ).import_csv(Path(args.file))
                merged = store.merge_candidates(candidates)
                payload = {
                    "status": "ok",
                    "provider": provider_name,
                    "imported": len(candidates),
                    **merged,
                }
            elif action == "import-basescan":
                candidates = ManualCsvProvider(
                    "basescan_manual"
                ).import_csv(Path(args.file))
                merged = store.merge_candidates(candidates)
                payload = {
                    "status": "ok",
                    "provider": "basescan_manual",
                    "imported": len(candidates),
                    **merged,
                }
            elif action == "import-oli":
                candidates = OliParquetProvider().import_parquet(
                    Path(args.file)
                )
                merged = store.merge_candidates(candidates)
                payload = {
                    "status": "ok",
                    "provider": "oli",
                    "imported": len(candidates),
                    **merged,
                }
            else:
                raise AddressIntelligenceError(
                    "address_intelligence_action_invalid"
                )
            payload.update({
                "network_activity": False,
                "telegram_calls": 0,
                "ai_calls": 0,
            })
            print(json.dumps(
                payload, ensure_ascii=False, sort_keys=True
            ))
            return 0
        if args.command == "label-candidates":
            settings.validate()
            action = args.candidate_action
            if action in {"provider-check", "discover"}:
                if not args.allow_network:
                    raise LabelCandidateError("allow_network_required")
                if not settings.arkham_api_key:
                    payload: dict[str, object] = {
                        "status": "optional_disabled",
                        "reason": "arkham_not_configured",
                        "provider": "arkham",
                        "configured": False,
                        "arkham_request_count": 0,
                        "network_activity": False,
                        "telegram_calls": 0,
                        "ai_calls": 0,
                    }
                    if action == "discover":
                        payload.update({
                            "candidates_found": 0,
                            "created": 0,
                            "refreshed": 0,
                        })
                    print(json.dumps(
                        payload, ensure_ascii=False, sort_keys=True
                    ))
                    return 0
                arkham_network_started = True
            if action == "provider-check":
                payload = LabelCandidateDiscovery(
                    settings
                ).provider_check()
                payload.update({
                    "network_activity": True,
                    "telegram_calls": 0,
                    "ai_calls": 0,
                })
                print(json.dumps(
                    payload, ensure_ascii=False, sort_keys=True
                ))
                return 0
            if action == "discover":
                maximum = (
                    settings.oar_label_candidate_max_addresses
                    if args.max_addresses is None
                    else int(args.max_addresses)
                )
                payload = LabelCandidateDiscovery(settings).discover(
                    chain=args.chain,
                    contract=args.contract,
                    window=args.window,
                    max_addresses=maximum,
                )
                print(json.dumps(
                    payload, ensure_ascii=False, sort_keys=True
                ))
                return 0
            store = LabelCandidateStore.from_settings(settings)
            if action == "list":
                candidates = store.list(
                    status=args.status,
                    limit=args.limit,
                )
                print(json.dumps({
                    "status": (
                        "not_initialized"
                        if not settings.label_candidates_path.exists()
                        else "ok"
                    ),
                    "candidates": candidates,
                    "count": len(candidates),
                    "network_activity": False,
                    "telegram_calls": 0,
                    "ai_calls": 0,
                }, ensure_ascii=False, sort_keys=True))
                return 0
            if action == "approve":
                result = store.approve(
                    args.candidate_id,
                    labels_path=settings.labels_path,
                    min_confidence=settings.min_label_confidence,
                )
                print(json.dumps({
                    "status": "ok",
                    "candidate_id": result["candidate"]["candidate_id"],
                    "candidate_status": "approved",
                    "private_labels_updated": True,
                    "backup_created": result["backup_created"],
                    "network_activity": False,
                    "telegram_calls": 0,
                    "ai_calls": 0,
                }, ensure_ascii=False, sort_keys=True))
                return 0
            candidate = store.reject(args.candidate_id)
            print(json.dumps({
                "status": "ok",
                "candidate_id": candidate["candidate_id"],
                "candidate_status": "rejected",
                "network_activity": False,
                "telegram_calls": 0,
                "ai_calls": 0,
            }, ensure_ascii=False, sort_keys=True))
            return 0
        if args.command in {
            "registry-add",
            "registry-verify",
            "registry-list",
            "registry-disable",
            "watch-add",
            "watch-list",
            "watch-remove",
            "bridge-once",
            "unresolved-summary",
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
            if args.command == "unresolved-summary":
                items = store.unresolved_summary(limit=args.limit)
                print(
                    json.dumps(
                        {
                            "status": (
                                "not_initialized"
                                if not settings.oar_automation_db_path.exists()
                                else "ok"
                            ),
                            "items": items,
                            "count": len(items),
                            "database_writes": False,
                            "network_activity": False,
                            "telegram_calls": False,
                            "ai_calls": False,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
                return 0
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
            "ai-request-check",
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
                if args.command in {
                    "ai-request-check",
                    "token-report",
                    "token-notify",
                }:
                    payload["ai_calls"] = 0
                    payload["telegram_calls"] = 0
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
            elif args.command == "ai-request-check":
                code, payload = _ai_request_check(settings, query)
                print(
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2 if bool(args.pretty) else None,
                    )
                )
                return code
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
                    "diagnostics": (
                        notification.diagnostics.public_dict()
                        if notification.diagnostics is not None
                        else None
                    ),
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
            if notification_status in {"blocked", "failed", "partial"}:
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
            payload = label_readiness(
                settings.labels_path,
                min_confidence=settings.min_label_confidence,
                chain_id=settings.base_chain_id,
            )
            if settings.enable or settings.base_enable:
                labels = load_labels_csv(settings.labels_path)
                validate_live_labels(
                    labels,
                    min_confidence=settings.min_label_confidence,
                    chain_id=settings.base_chain_id,
                )
            payload["labels"] = payload["total_labels"]
            print(json.dumps(
                payload, ensure_ascii=False, sort_keys=True
            ))
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
    except TelegramTopicLinkError as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": exc.code,
                    "network_activity": False,
                    "rpc_calls": 0,
                    "ai_calls": 0,
                    "telegram_calls": 0,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    except (ArkhamIntelligenceError, LabelCandidateError) as exc:
        print(json.dumps({
            "status": "failed",
            "error": exc.code,
            "network_activity": arkham_network_started,
            "telegram_calls": 0,
            "ai_calls": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 1
    except AddressIntelligenceError as exc:
        print(json.dumps({
            "status": "failed",
            "error": exc.code,
            "network_activity": address_provider_network_started,
            "core_services_affected": False,
            "telegram_calls": 0,
            "ai_calls": 0,
        }, ensure_ascii=False, sort_keys=True))
        return 1
    except TokenActivityQueryError as exc:
        payload = failed_token_activity_payload(
            exc,
            network_activity=token_activity_network_started,
        )
        if args.command in {
            "ai-request-check",
            "token-report",
            "token-notify",
        }:
            payload["ai_calls"] = 0
            payload["telegram_calls"] = 0
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
