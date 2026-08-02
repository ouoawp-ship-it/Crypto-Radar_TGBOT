from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .config import OnchainSettings, parse_env_file


RPC_ENV_RE = re.compile(r"^ONCHAIN_[A-Z0-9_]+_HTTP_RPC_URL$")
IMPLEMENTED_RUNTIME_ADAPTERS = {8453: "base_v1"}


class ChainCapabilityError(ValueError):
    pass


def _load_chain_rows(path: Path) -> tuple[str, list[dict[str, object]]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ChainCapabilityError("chain_registry_invalid") from exc
    if not isinstance(payload, dict) or not isinstance(
        payload.get("chains"), list
    ):
        raise ChainCapabilityError("chain_registry_invalid")
    schema_version = str(payload.get("schema_version") or "")
    if not schema_version:
        raise ChainCapabilityError("chain_registry_schema_missing")
    rows = [item for item in payload["chains"] if isinstance(item, dict)]
    if len(rows) != len(payload["chains"]):
        raise ChainCapabilityError("chain_registry_invalid")
    return schema_version, rows


def _rpc_valid(value: str) -> bool:
    if not value:
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme.lower() in {"http", "https"}
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def chain_capability_report(
    settings: OnchainSettings,
    *,
    environ: Mapping[str, str] | None = None,
) -> dict[str, object]:
    schema_version, rows = _load_chain_rows(settings.chains_path)
    file_values = parse_env_file(settings.base_dir / ".env.onchain")
    runtime_values = os.environ if environ is None else environ
    chain_ids: set[int] = set()
    names: set[str] = set()
    rpc_env_names: set[str] = set()
    capabilities: list[dict[str, object]] = []

    for row in rows:
        try:
            raw_chain_id = row["chain_id"]
            if isinstance(raw_chain_id, bool):
                raise ValueError("boolean chain id")
            chain_id = int(raw_chain_id)
            name = str(row["name"]).strip()
            raw_enabled = row.get("enabled", False)
            if not isinstance(raw_enabled, bool):
                raise ValueError("enabled must be boolean")
            configured_enabled = raw_enabled
            rpc_env = str(row.get("http_rpc_env") or "").strip()
            confirmation_depth = int(row["confirmation_depth"])
            bootstrap_lookback = int(row["bootstrap_lookback_blocks"])
            reorg_lookback = int(row["reorg_lookback_blocks"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ChainCapabilityError("chain_registry_invalid") from exc
        canonical_name = name.casefold()
        if (
            chain_id <= 0
            or not name
            or not RPC_ENV_RE.fullmatch(rpc_env)
            or confirmation_depth <= 0
            or bootstrap_lookback <= 0
            or reorg_lookback <= 0
        ):
            raise ChainCapabilityError("chain_registry_invalid")
        if (
            chain_id in chain_ids
            or canonical_name in names
            or rpc_env in rpc_env_names
        ):
            raise ChainCapabilityError("chain_registry_duplicate")
        chain_ids.add(chain_id)
        names.add(canonical_name)
        rpc_env_names.add(rpc_env)

        adapter = IMPLEMENTED_RUNTIME_ADAPTERS.get(chain_id, "")
        effective_enabled = (
            bool(settings.base_enable)
            if chain_id == settings.base_chain_id
            else configured_enabled
        )
        rpc_value = str(
            runtime_values.get(rpc_env) or file_values.get(rpc_env) or ""
        )
        if chain_id == settings.base_chain_id and settings.base_http_rpc_url:
            rpc_value = settings.base_http_rpc_url
        rpc_configured = bool(rpc_value)
        rpc_valid = _rpc_valid(rpc_value) if rpc_configured else False

        if not effective_enabled:
            status = "disabled"
        elif not adapter:
            status = "runtime_adapter_not_implemented"
        elif not rpc_configured:
            status = "rpc_not_configured"
        elif not rpc_valid:
            status = "rpc_configuration_invalid"
        else:
            status = "ready_offline"
        runtime_ready = status == "ready_offline"
        capabilities.append(
            {
                "chain_id": chain_id,
                "name": name,
                "configured_enabled": configured_enabled,
                "effective_enabled": effective_enabled,
                "runtime_adapter": adapter or "not_implemented",
                "confirmation_depth": confirmation_depth,
                "bootstrap_lookback_blocks": bootstrap_lookback,
                "reorg_lookback_blocks": reorg_lookback,
                "rpc_configured": rpc_configured,
                "rpc_configuration_valid": rpc_valid,
                "status": status,
                "token_activity_supported": runtime_ready,
                "watch_supported": runtime_ready,
                "activation_blocked": bool(
                    effective_enabled and not runtime_ready
                ),
            }
        )

    ready_count = sum(
        bool(item["token_activity_supported"]) for item in capabilities
    )
    return {
        "status": "ok",
        "schema_version": schema_version,
        "core_available": any(
            item["runtime_adapter"] != "not_implemented"
            for item in capabilities
        ),
        "multichain_runtime_ready": ready_count > 1,
        "configured_chain_count": len(capabilities),
        "runtime_ready_chain_count": ready_count,
        "chains": capabilities,
        "network_activity": False,
        "database_writes": False,
        "telegram_calls": 0,
        "ai_calls": 0,
    }
