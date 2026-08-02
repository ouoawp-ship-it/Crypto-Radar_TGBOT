from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from .config import OnchainSettings, parse_env_file


RPC_ENV_RE = re.compile(r"^ONCHAIN_[A-Z0-9_]+_HTTP_RPC_URL$")
CHAIN_SLUG_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


class ChainCapabilityError(ValueError):
    pass


@dataclass(frozen=True)
class EvmChainSpec:
    chain_id: int
    slug: str
    name: str
    enabled: bool
    confirmation_depth: int
    bootstrap_lookback_blocks: int
    reorg_lookback_blocks: int
    http_rpc_env: str
    explorer_tx_url: str

    def transaction_url(self, tx_hash: str) -> str:
        return self.explorer_tx_url.replace("{tx_hash}", tx_hash)


def _explorer_valid(value: str) -> bool:
    if value.count("{tx_hash}") != 1:
        return False
    parsed = urlsplit(value)
    return bool(
        parsed.scheme.lower() == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
    )


def load_evm_chain_specs(path: Path) -> tuple[str, tuple[EvmChainSpec, ...]]:
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

    chain_ids: set[int] = set()
    slugs: set[str] = set()
    names: set[str] = set()
    rpc_env_names: set[str] = set()
    specs: list[EvmChainSpec] = []
    for row in payload["chains"]:
        if not isinstance(row, dict):
            raise ChainCapabilityError("chain_registry_invalid")
        try:
            raw_chain_id = row["chain_id"]
            if isinstance(raw_chain_id, bool):
                raise ValueError("boolean chain id")
            chain_id = int(raw_chain_id)
            name = str(row["name"]).strip()
            slug = str(row.get("slug") or name.lower()).strip().lower()
            raw_enabled = row.get("enabled", False)
            if not isinstance(raw_enabled, bool):
                raise ValueError("enabled must be boolean")
            rpc_env = str(row.get("http_rpc_env") or "").strip()
            confirmation_depth = int(row["confirmation_depth"])
            bootstrap_lookback = int(row["bootstrap_lookback_blocks"])
            reorg_lookback = int(row["reorg_lookback_blocks"])
            explorer_tx_url = str(row["explorer_tx_url"]).strip()
        except (KeyError, TypeError, ValueError) as exc:
            raise ChainCapabilityError("chain_registry_invalid") from exc
        canonical_name = name.casefold()
        if (
            chain_id <= 0
            or not name
            or not CHAIN_SLUG_RE.fullmatch(slug)
            or not RPC_ENV_RE.fullmatch(rpc_env)
            or confirmation_depth <= 0
            or bootstrap_lookback <= 0
            or reorg_lookback <= 0
            or not _explorer_valid(explorer_tx_url)
        ):
            raise ChainCapabilityError("chain_registry_invalid")
        if (
            chain_id in chain_ids
            or slug in slugs
            or canonical_name in names
            or rpc_env in rpc_env_names
        ):
            raise ChainCapabilityError("chain_registry_duplicate")
        chain_ids.add(chain_id)
        slugs.add(slug)
        names.add(canonical_name)
        rpc_env_names.add(rpc_env)
        specs.append(
            EvmChainSpec(
                chain_id=chain_id,
                slug=slug,
                name=name,
                enabled=raw_enabled,
                confirmation_depth=confirmation_depth,
                bootstrap_lookback_blocks=bootstrap_lookback,
                reorg_lookback_blocks=reorg_lookback,
                http_rpc_env=rpc_env,
                explorer_tx_url=explorer_tx_url,
            )
        )
    return schema_version, tuple(specs)


def resolve_evm_chain(settings: OnchainSettings, chain: str) -> EvmChainSpec:
    _schema, specs = load_evm_chain_specs(settings.chains_path)
    target = str(chain or "").strip().casefold()
    matches = [
        spec
        for spec in specs
        if target
        in {spec.slug.casefold(), spec.name.casefold(), str(spec.chain_id)}
    ]
    if len(matches) != 1:
        raise ChainCapabilityError("chain_not_configured")
    return matches[0]


def resolve_chain_rpc_url(
    settings: OnchainSettings,
    spec: EvmChainSpec,
    *,
    environ: Mapping[str, str] | None = None,
) -> str:
    if spec.chain_id == settings.base_chain_id and settings.base_http_rpc_url:
        return settings.base_http_rpc_url
    file_values = parse_env_file(settings.base_dir / ".env.onchain")
    runtime_values = os.environ if environ is None else environ
    return str(
        runtime_values.get(spec.http_rpc_env)
        or file_values.get(spec.http_rpc_env)
        or ""
    )


def rpc_url_valid(value: str) -> bool:
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
    schema_version, specs = load_evm_chain_specs(settings.chains_path)
    capabilities: list[dict[str, object]] = []
    for spec in specs:
        effective_enabled = (
            bool(settings.base_enable)
            if spec.chain_id == settings.base_chain_id
            else spec.enabled
        )
        rpc_value = resolve_chain_rpc_url(
            settings, spec, environ=environ
        )
        rpc_configured = bool(rpc_value)
        rpc_valid = rpc_url_valid(rpc_value)
        if not rpc_configured:
            query_status = "rpc_not_configured"
        elif not rpc_valid:
            query_status = "rpc_configuration_invalid"
        else:
            query_status = "ready_offline"
        token_activity_supported = query_status == "ready_offline"

        if not effective_enabled:
            watch_status = "disabled"
        elif spec.chain_id != settings.base_chain_id:
            watch_status = "watch_adapter_not_implemented"
        elif not token_activity_supported:
            watch_status = query_status
        else:
            watch_status = "ready_offline"
        watch_supported = watch_status == "ready_offline"
        capabilities.append(
            {
                "chain_id": spec.chain_id,
                "slug": spec.slug,
                "name": spec.name,
                "configured_enabled": spec.enabled,
                "effective_enabled": effective_enabled,
                "token_activity_adapter": "evm_token_activity_v1",
                "watch_adapter": (
                    "base_watch_v1"
                    if spec.chain_id == settings.base_chain_id
                    else "not_implemented"
                ),
                "confirmation_depth": spec.confirmation_depth,
                "bootstrap_lookback_blocks": (
                    spec.bootstrap_lookback_blocks
                ),
                "reorg_lookback_blocks": spec.reorg_lookback_blocks,
                "rpc_configured": rpc_configured,
                "rpc_configuration_valid": rpc_valid,
                "token_activity_status": query_status,
                "watch_status": watch_status,
                "token_activity_supported": token_activity_supported,
                "watch_supported": watch_supported,
                "activation_blocked": bool(
                    effective_enabled and not watch_supported
                ),
            }
        )

    ready_count = sum(
        bool(item["token_activity_supported"]) for item in capabilities
    )
    return {
        "status": "ok",
        "schema_version": schema_version,
        "core_available": bool(capabilities),
        "multichain_runtime_ready": ready_count > 1,
        "configured_chain_count": len(capabilities),
        "runtime_ready_chain_count": ready_count,
        "chains": capabilities,
        "network_activity": False,
        "database_writes": False,
        "telegram_calls": 0,
        "ai_calls": 0,
    }
