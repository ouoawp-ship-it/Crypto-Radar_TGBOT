from __future__ import annotations

import hashlib
import json
from typing import Any

from .ai_client import (
    OarAiCache,
    OarAiClient,
    OarAiError,
    OpenAiCompatibleOarClient,
    validate_ai_output,
)
from .ai_context import build_ai_context
from .config import OnchainSettings
from .constants import (
    OAR_AI_PROMPT_VERSION,
    OAR_REPORT_ALGORITHM_VERSION,
    OAR_REPORT_SCHEMA_VERSION,
)
from .prompt_manager import (
    OperatorPromptError,
    OperatorPromptManager,
)
from .token_activity import TokenActivityQuery
from .token_analysis import TokenAnalysisService


RISK_NOTES = (
    "流入交易所不等于已经卖出。",
    "从交易所提出不等于已经买入或必然上涨。",
    "钱包关联规则分数不是概率，高分不等于确认属于同一主力。",
    "数据不完整时不能形成高确定性判断。",
)


def _mapping(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _canonical_hash(value: object) -> str:
    serialized = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def restricted_ai_input(payload: dict[str, object]) -> bool:
    analysis = _mapping(payload.get("analysis"))
    primary = _mapping(analysis.get("primary_behavior"))
    return (
        not bool(payload.get("complete"))
        or not bool(analysis.get("complete"))
        or str(analysis.get("status") or "")
        in {
            "partial_input",
            "partial_analysis",
            "insufficient_evidence",
            "no_activity",
        }
        or str(primary.get("type") or "")
        in {
            "no_activity",
            "isolated",
            "inconclusive_activity",
            "insufficient_data",
        }
    )


def build_rule_summary(payload: dict[str, object]) -> dict[str, object]:
    query = _mapping(payload.get("query"))
    token = _mapping(payload.get("token"))
    summary = _mapping(payload.get("summary"))
    analysis = _mapping(payload.get("analysis"))
    primary = _mapping(analysis.get("primary_behavior"))
    window_name = str(query.get("window") or "")
    windows = _mapping(analysis.get("windows"))
    window = _mapping(windows.get(window_name))
    groups = [
        item
        for item in _list(analysis.get("wallet_groups"))
        if isinstance(item, dict)
    ][:3]
    representatives = []
    for item in _list(payload.get("largest_transfers"))[:3]:
        if not isinstance(item, dict):
            continue
        representatives.append(
            {
                "event_id": str(item.get("event_id") or ""),
                "flow_type": str(item.get("flow_type") or ""),
                "amount": str(item.get("amount") or "0"),
                "amount_usd": (
                    None
                    if item.get("amount_usd") is None
                    else str(item.get("amount_usd"))
                ),
                "explorer_url": str(item.get("explorer_url") or ""),
            }
        )
    limitations = sorted(
        {
            str(item)
            for item in _list(analysis.get("limitations"))
            if str(item)
        }
    )
    if not bool(payload.get("complete")):
        limitations.append("query_incomplete")
    linked_by_ref = {
        str(item.get("public_ref") or ""): item
        for item in _list(payload.get("linked_market_signals"))
        if isinstance(item, dict) and str(item.get("public_ref") or "")
    }
    ordered_linked = sorted(
        linked_by_ref.values(),
        key=lambda item: (
            -int(item.get("_priority") or 0),
            -int(item.get("ts") or 0),
            str(item.get("public_ref") or ""),
        ),
    )
    linked = [
        {
            "public_ref": str(item.get("public_ref") or "")[:160],
            "module": str(item.get("module") or "")[:40],
            "symbol": str(item.get("symbol") or "")[:40],
            "score": item.get("score"),
            "stage": str(item.get("stage") or "")[:80],
            "severity": str(item.get("severity") or "")[:24],
            "ts": int(item.get("ts") or 0),
            "age_sec": max(0, int(item.get("age_sec") or 0)),
            "summary": str(item.get("summary") or "")[:300],
        }
        for item in ordered_linked
    ][:3]
    return {
        "token": {
            "chain": str(query.get("chain") or ""),
            "chain_id": query.get("chain_id"),
            "contract": str(
                query.get("contract") or token.get("contract") or ""
            ),
            "symbol": str(token.get("symbol") or ""),
            "name": str(token.get("name") or ""),
        },
        "query": {
            "window": window_name,
            "complete": bool(payload.get("complete")),
            "analysis_status": str(analysis.get("status") or ""),
        },
        "transfer_summary": {
            "transfer_count": int(summary.get("transfer_count") or 0),
            "total_token_amount": str(
                summary.get("total_token_amount") or "0"
            ),
            "unique_senders": int(summary.get("unique_senders") or 0),
            "unique_receivers": int(summary.get("unique_receivers") or 0),
        },
        "cex_flows": {
            "gross_inflow_token": str(
                window.get("gross_cex_inflow_token") or "0"
            ),
            "gross_outflow_token": str(
                window.get("gross_cex_outflow_token") or "0"
            ),
            "net_flow_token": str(
                window.get("net_cex_flow_token") or "0"
            ),
            "inflow_count": int(window.get("inflow_count") or 0),
            "outflow_count": int(window.get("outflow_count") or 0),
        },
        "primary_behavior": {
            "type": str(primary.get("type") or "insufficient_data"),
            "label": str(primary.get("label") or "数据不足"),
            "score": int(primary.get("score") or 0),
            "confidence_level": str(
                primary.get("confidence_level") or "low"
            ),
            "supporting_evidence": sorted(
                {
                    str(item)
                    for item in _list(primary.get("supporting_evidence"))
                    if str(item)
                }
            ),
            "counter_evidence": sorted(
                {
                    str(item)
                    for item in _list(primary.get("counter_evidence"))
                    if str(item)
                }
            ),
        },
        "wallet_groups": [
            {
                "group_id": str(item.get("group_id") or ""),
                "group_type": str(item.get("group_type") or ""),
                "score": int(item.get("score") or 0),
                "level": str(item.get("level") or ""),
                "supporting_evidence": sorted(
                    {
                        str(entry)
                        for entry in _list(item.get("supporting_evidence"))
                        if str(entry)
                    }
                ),
            }
            for item in groups
        ],
        "representative_transfers": representatives,
        "linked_market_signals": linked,
        "data_limitations": sorted(set(limitations)),
        "risk_notes": list(RISK_NOTES),
    }


def build_rule_summary_text(summary: dict[str, object]) -> str:
    token = _mapping(summary.get("token"))
    query = _mapping(summary.get("query"))
    transfers = _mapping(summary.get("transfer_summary"))
    flows = _mapping(summary.get("cex_flows"))
    primary = _mapping(summary.get("primary_behavior"))
    groups = _list(summary.get("wallet_groups"))
    linked = _list(summary.get("linked_market_signals"))
    lines = [
        (
            f"{token.get('symbol') or 'UNKNOWN'} / Base / "
            f"{query.get('window') or '-'}："
            f"{'数据完整' if query.get('complete') else '数据不完整'}"
        ),
        (
            f"Transfer {transfers.get('transfer_count', 0)} 笔，"
            f"独立发送/接收钱包 {transfers.get('unique_senders', 0)}/"
            f"{transfers.get('unique_receivers', 0)}。"
        ),
        (
            f"流入交易所 {flows.get('gross_inflow_token', '0')}，"
            f"从交易所提出 {flows.get('gross_outflow_token', '0')}，"
            f"净流向（流入-提出）{flows.get('net_flow_token', '0')}。"
        ),
        (
            f"行为：{primary.get('label') or '数据不足'}；"
            f"规则分数 {primary.get('score', 0)}（评分不是概率）。"
        ),
        f"钱包关联候选：{len(groups)} 组（高分不等于确认同一主力）。",
        (
            f"关联市场信号：{len(linked)} 条。"
            if linked
            else "关联市场信号：无。"
        ),
        *RISK_NOTES,
    ]
    return "\n".join(lines)


class TokenReportService:
    def __init__(
        self,
        settings: OnchainSettings,
        analysis_service: Any,
        *,
        ai_client: OarAiClient | None = None,
        ai_cache: OarAiCache | None = None,
        prompt_manager: OperatorPromptManager | None = None,
    ):
        self.settings = settings
        self.analysis_service = analysis_service
        self._ai_client = ai_client
        self._ai_cache = ai_cache
        self._prompt_manager = prompt_manager

    @classmethod
    def from_settings(
        cls,
        settings: OnchainSettings,
        query: TokenActivityQuery,
    ) -> "TokenReportService":
        return cls(
            settings,
            TokenAnalysisService.from_settings(settings, query),
        )

    def execute(
        self,
        query: TokenActivityQuery,
        *,
        with_ai: bool,
    ) -> dict[str, object]:
        payload = self.analysis_service.execute(query)
        return self.build_from_analysis(
            payload,
            with_ai=with_ai,
            linked_market_signals=[],
        )

    def build_from_analysis(
        self,
        analyzed_payload: dict[str, object],
        *,
        with_ai: bool,
        linked_market_signals: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        payload = dict(analyzed_payload)
        payload["linked_market_signals"] = list(
            linked_market_signals or []
        )
        context = build_ai_context(
            payload,
            max_chars=self.settings.oar_ai_max_context_chars,
        )
        rule_summary = build_rule_summary(payload)
        rule_text = build_rule_summary_text(rule_summary)
        analysis = _mapping(payload.get("analysis"))
        restricted_for_ai = restricted_ai_input(payload)
        ai = self._ai_result(
            context,
            requested=with_ai,
            restricted_input=restricted_for_ai,
        )
        content_basis = {
            "rule_summary": rule_summary,
            "ai_result": ai.get("result"),
        }
        content_hash = _canonical_hash(content_basis)
        report = {
            "schema_version": OAR_REPORT_SCHEMA_VERSION,
            "algorithm_version": OAR_REPORT_ALGORITHM_VERSION,
            "status": "ok",
            "complete": bool(payload.get("complete"))
            and bool(analysis.get("complete")),
            "rule_summary": rule_summary,
            "rule_summary_text": rule_text,
            "ai_context": context,
            "ai": ai,
            "context_hash": context["context_hash"],
            "content_hash": content_hash,
            "report_hash": content_hash,
        }
        result = dict(payload)
        result["report"] = report
        diagnostics = dict(_mapping(result.get("diagnostics")))
        diagnostics["ai_calls"] = int(ai.get("calls") or 0)
        result["diagnostics"] = diagnostics
        return result

    def _ai_result(
        self,
        context: dict[str, object],
        *,
        requested: bool,
        restricted_input: bool,
    ) -> dict[str, object]:
        if not requested:
            return {
                "status": "not_requested",
                "calls": 0,
                "result": None,
            }
        if not self.settings.oar_ai_enable:
            return {
                "status": "disabled",
                "calls": 0,
                "result": None,
            }
        prompt_manager = (
            self._prompt_manager
            or OperatorPromptManager.from_settings(self.settings)
        )
        try:
            operator_prompt = prompt_manager.load_for_request()
        except (OSError, OperatorPromptError):
            return {
                "status": "failed",
                "calls": 0,
                "result": None,
                "error": "operator_prompt_unavailable",
            }
        cache = self._ai_cache or OarAiCache(
            path=self.settings.oar_ai_cache_path,
            data_dir=self.settings.data_dir,
            ttl_sec=self.settings.oar_ai_cache_ttl_sec,
            max_calls_per_hour=self.settings.oar_ai_max_calls_per_hour,
        )
        context_hash = str(context["context_hash"])
        try:
            cached = cache.get(
                context_hash,
                self.settings.oar_ai_model,
                OAR_AI_PROMPT_VERSION,
                provider=self.settings.oar_ai_provider,
                operator_prompt_hash=operator_prompt.prompt_hash,
                thinking_mode=self.settings.oar_ai_thinking_mode,
                reasoning_effort=self.settings.oar_ai_reasoning_effort,
                max_tokens=self.settings.oar_ai_max_tokens,
            )
        except (OSError, ValueError):
            return {
                "status": "failed",
                "calls": 0,
                "result": None,
                "error": "ai_cache_unavailable",
            }
        if cached.result is not None:
            try:
                validated = validate_ai_output(
                    cached.result,
                    restricted_input=restricted_input,
                )
            except OarAiError:
                validated = None
            if validated is not None:
                return {
                    "status": "cached",
                    "calls": 0,
                    "result": validated,
                }
        try:
            call_reserved = cache.reserve_call()
        except (OSError, ValueError):
            return {
                "status": "failed",
                "calls": 0,
                "result": None,
                "error": "ai_cache_unavailable",
            }
        if not call_reserved:
            return {
                "status": "hourly_limit",
                "calls": 0,
                "result": None,
                "error": "ai_hourly_limit",
            }
        client = self._ai_client or OpenAiCompatibleOarClient(
            base_url=self.settings.oar_ai_base_url,
            api_key=self.settings.oar_ai_api_key,
            model=self.settings.oar_ai_model,
            timeout_sec=self.settings.oar_ai_timeout_sec,
            max_retries=self.settings.oar_ai_max_retries,
            max_output_chars=self.settings.oar_ai_max_output_chars,
            provider=self.settings.oar_ai_provider,
            thinking_mode=self.settings.oar_ai_thinking_mode,
            reasoning_effort=self.settings.oar_ai_reasoning_effort,
            max_tokens=self.settings.oar_ai_max_tokens,
            operator_prompt=operator_prompt.content,
            operator_prompt_hash=operator_prompt.prompt_hash,
        )
        try:
            result = client.analyze(
                context,
                restricted_input=restricted_input,
            )
            validated = validate_ai_output(
                result,
                restricted_input=restricted_input,
            )
        except OarAiError as exc:
            failure = {
                "status": (
                    "invalid"
                    if exc.code == "invalid_ai_output"
                    else "failed"
                ),
                "calls": 1,
                "result": None,
                "error": exc.code,
            }
            failure.update(exc.public_details())
            return failure
        except Exception:
            return {
                "status": "failed",
                "calls": 1,
                "result": None,
                "error": "ai_client_error",
            }
        response = {
            "status": "available",
            "calls": 1,
            "result": validated,
        }
        try:
            cache.put(
                context_hash,
                self.settings.oar_ai_model,
                OAR_AI_PROMPT_VERSION,
                validated,
                provider=self.settings.oar_ai_provider,
                operator_prompt_hash=operator_prompt.prompt_hash,
                thinking_mode=self.settings.oar_ai_thinking_mode,
                reasoning_effort=self.settings.oar_ai_reasoning_effort,
                max_tokens=self.settings.oar_ai_max_tokens,
            )
        except (OSError, ValueError):
            response["warning"] = "ai_cache_write_failed"
        return response
