from __future__ import annotations

import json
import math
import re
import socket
import ssl
from collections.abc import Mapping, Sequence
from typing import Any
from urllib.parse import urlsplit


OPERATOR_PROMPT = """
你是“启动预警雷达”的中文解读员，不是交易决策者。

你只能解释输入中已经由确定性规则产生的事实和结论：
1. 必须原样复制 direction 和 stage，不得改变方向或阶段。
2. 不得重新计分，不得改写规则分、入场观察区、失效位、目标位或收益风险比。
3. 不得补造缺失数据，不得猜测庄家、机构或未审核的现实身份。
4. 必须同时说明支持证据、反向证据、当前风险、需要等待的确认和限制。
5. 分数是规则准备度，不是涨跌概率。禁止使用“确定会涨”、“确定会跌”、“立即买入”、“立即卖出”等确定性措辞。
6. 你只负责白话解释，不得输出开仓、加仓、满仓或建议买卖等交易指令，也不得输出任何数字、价位或百分比。

市场语义必须按以下边界解释：
- 价格上涨、持仓量上升、现货与合约 CVD 上升：主动买入与新增仓位同向，属于真强候选。
- 价格上涨、持仓量上升、CVD 下降：表面强势但主动买盘未跟随，属于背离或假强，不能自动写成挤空。
- 价格下跌、持仓量上升、CVD 下降：新增空头和主动卖出同向，属于真弱候选。
- 价格变化而持仓量下降：主要是平仓或去杠杆，不宜追涨或追跌，也不证明已经反转。
- CVD 背离只是观察信号，必须等待价格结构破位或放量确认。
- 资金费率和基差只表示拥挤风险，不能单独证明方向。
- 周线和日线只过滤大方向，4 小时到 1 小时确认结构，15 分钟触发，5 分钟只用于入场观察。

只输出一个 JSON 对象，不要 Markdown，不要输出推理过程。
字段必须恰好为：status、direction、stage、summary、supporting_evidence、counter_evidence、risk_notes、wait_for、limitations。
status 必须为 available；direction 和 stage 必须原样复制规则结果；summary 是简短中文；其余五项必须是中文字符串数组。不得增加其他字段。
为了避免输出被截断，必须保持紧凑：summary 不超过一百二十个汉字；其余每个数组最多两项，
每项不超过六十个汉字；不要重复输入数据，不要写前言、结尾或思考过程。
""".strip()

OUTPUT_FIELDS = (
    "status",
    "direction",
    "stage",
    "summary",
    "supporting_evidence",
    "counter_evidence",
    "risk_notes",
    "wait_for",
    "limitations",
)

_LIST_FIELDS = OUTPUT_FIELDS[4:]
_TIMEFRAMES = ("5m", "15m", "1h", "2h", "4h", "8h", "12h", "1d", "1w")
_ROLE_GROUPS = (
    "macro_direction",
    "main_structure",
    "confirmation",
    "trigger",
    "entry",
)
_FORBIDDEN_TEXT = (
    "http://",
    "https://",
    "authorization",
    "bearer ",
    "api_key",
    "apikey",
    "api-key",
    "rpc_url",
    "bot_token",
)
_FORBIDDEN_AI_OUTPUT_PHRASES = (
    "立即买入",
    "立即卖出",
    "马上买入",
    "马上卖出",
    "确定会涨",
    "确定会跌",
    "一定会涨",
    "一定会跌",
    "必然上涨",
    "必然下跌",
    "稳赚",
    "必涨",
    "必跌",
    "开仓",
    "加仓",
    "满仓",
    "重仓",
    "梭哈",
    "建议买入",
    "建议卖出",
    "建议做多",
    "建议做空",
    "buy now",
    "sell now",
    "guaranteed profit",
)
_TRADE_INSTRUCTION_PATTERN = re.compile(
    r"(?:建议|立即|马上|直接|应当|应该|可以|适合|需要|务必).{0,4}"
    r"(?:买入|卖出|做多|做空)"
)
_DETERMINISTIC_CLAIM_PATTERN = re.compile(
    r"(?:确定|一定|必然|肯定).{0,3}(?:会)?(?:涨|跌|上涨|下跌|盈利|赚钱)"
)
_NUMERIC_OUTPUT_PATTERN = re.compile(
    r"[0-9０-９%％]|百分之|第[零〇一二两三四五六七八九十百千万亿]+|"
    r"[零〇一二两三四五六七八九十百千万亿]+"
    r"(?:个|次|分钟|小时|日|天|周|月|年|倍|成|点|元|美元|分|笔|手|张|档|层|级)"
)


def _mapping(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_mapping(source: Mapping[str, Any], *keys: str) -> Mapping[str, Any]:
    for key in keys:
        value = source.get(key)
        if isinstance(value, Mapping):
            return value
    return {}


def _safe_scalar(value: object) -> str | int | float | bool | None:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    text = str(value).strip()[:240]
    lowered = text.lower()
    if any(marker in lowered for marker in _FORBIDDEN_TEXT):
        return "redacted"
    return text


def _safe_list(value: object, *, limit: int = 12) -> list[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    result: list[Any] = []
    for item in value[:limit]:
        if isinstance(item, Mapping):
            continue
        safe = _safe_scalar(item)
        if safe not in {None, ""}:
            result.append(safe)
    return result


def _pick(mapping: Mapping[str, Any], keys: Sequence[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in keys:
        if key not in mapping:
            continue
        value = mapping.get(key)
        if isinstance(value, Mapping):
            continue
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            result[key] = _safe_list(value)
        else:
            result[key] = _safe_scalar(value)
    return result


def _pick_score_groups(value: object) -> dict[str, int | float | None]:
    mapping = _mapping(value)
    allowed = (
        "price_oi_participation",
        "active_funds",
        "structure",
        "execution_quality",
    )
    return {
        key: safe
        for key in allowed
        if key in mapping
        and isinstance((safe := _safe_scalar(mapping.get(key))), (int, float))
        and not isinstance(safe, bool)
    }


def _pick_evidence(value: object) -> dict[str, list[Any]]:
    mapping = _mapping(value)
    return {
        key: _safe_list(mapping.get(key))
        for key in ("bullish", "bearish", "supporting", "counter")
        if key in mapping
    }


def _multi_timeframe_context(value: object) -> dict[str, Any]:
    source = _mapping(value)
    frames = _mapping(source.get("timeframes"))
    groups = _mapping(source.get("role_groups"))
    safe_frames: dict[str, Any] = {}
    for timeframe in _TIMEFRAMES:
        frame = _mapping(frames.get(timeframe))
        if not frame:
            continue
        structure = _mapping(frame.get("structure"))
        fvg = _mapping(frame.get("fvg"))
        safe_frames[timeframe] = {
            **_pick(
                frame,
                (
                    "data_status",
                    "direction",
                    "structure_event",
                    "liquidity_sweep",
                    "last_closed_end_ms",
                ),
            ),
            "structure": _pick(structure, ("high", "low", "bias", "source")),
            "fvg": _pick(fvg, ("status", "zone_low", "zone_high")),
        }
    safe_groups = {
        key: _pick(
            _mapping(groups.get(key)),
            (
                "data_status",
                "direction",
                "vote",
                "timeframes",
                "ready_timeframes",
            ),
        )
        for key in _ROLE_GROUPS
        if isinstance(groups.get(key), Mapping)
    }
    vote = _pick(
        _mapping(source.get("vote_summary")),
        (
            "bullish_groups",
            "bearish_groups",
            "neutral_or_mixed_groups",
            "net_group_vote",
            "direction",
            "semantics",
        ),
    )
    return {
        **_pick(source, ("status", "window_end_ms")),
        "role_groups": safe_groups,
        "timeframes": safe_frames,
        "vote_summary": vote,
    }


def _values_from(
    mappings: Sequence[Mapping[str, Any]],
    keys: Sequence[str],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in keys:
        for mapping in mappings:
            if key in mapping:
                result[key] = _safe_scalar(mapping.get(key))
                break
    return result


def _smc_filter_context(source: Mapping[str, Any]) -> dict[str, Any]:
    """Return only the reviewed, bounded SMC confirmation summary."""

    smc_filter = _first_mapping(source, "smc_filter")
    if not smc_filter:
        return {}
    return {
        **_pick(
            smc_filter,
            (
                "version",
                "status",
                "signal_direction",
                "one_hour_structure",
                "four_hour_structure",
                "data_complete",
                "blocks_publication",
                "ai_eligible",
                "score_adjustment",
                "semantics",
            ),
        ),
        "opposing_zone_timeframes": _safe_list(
            smc_filter.get("opposing_zone_timeframes"),
            limit=2,
        ),
        "reasons": _safe_list(smc_filter.get("reasons"), limit=6),
    }


def build_launch_ai_context(source: Mapping[str, Any]) -> dict[str, Any]:
    """Build a bounded, content-only context without credentials or raw series."""

    if not isinstance(source, Mapping):
        source = {}
    rule = _first_mapping(
        source,
        "rule_result",
        "directional_readiness",
        "directional_model",
    ) or source
    market = _first_mapping(source, "market_facts", "launch_market_facts")
    multi_timeframe = _first_mapping(source, "multi_timeframe", "timeframe_analysis")
    structure = _first_mapping(source, "structure", "price_action", "smc")
    plan = _first_mapping(source, "plan", "trade_plan", "execution_plan")
    completeness = _first_mapping(source, "completeness", "data_completeness")
    lookups = (source, rule, market)

    rule_context = _pick(
        rule,
        (
            "version",
            "status",
            "direction",
            "stage",
            "score_semantics",
            "bullish_readiness",
            "bearish_readiness",
            "bullish_raw_score",
            "bearish_raw_score",
            "participation_pattern",
            "asset_profile",
            "data_complete",
            "missing_fields",
            "limitations",
        ),
    )
    rule_context["bullish_group_scores"] = _pick_score_groups(
        rule.get("bullish_group_scores")
    )
    rule_context["bearish_group_scores"] = _pick_score_groups(
        rule.get("bearish_group_scores")
    )
    rule_context["evidence"] = _pick_evidence(rule.get("evidence"))

    return {
        "rule_result": rule_context,
        "smc_filter": _smc_filter_context(source),
        "multi_timeframe": _multi_timeframe_context(multi_timeframe),
        "price_open_interest": _values_from(
            lookups,
            (
                "price_change_pct",
                "price_15m_pct",
                "price_1h_pct",
                "price_4h_pct",
                "price_24h_rolling_pct",
                "oi_change_pct",
                "oi_15m_pct",
                "oi_1h_pct",
                "oi_4h_pct",
                "oi_24h_closed_pct",
                "oi_24h_status",
            ),
        ),
        "active_flow": _values_from(
            lookups,
            (
                "spot_cvd_ratio",
                "futures_cvd_ratio",
                "spot_active_ratio",
                "futures_active_ratio",
                "spot_active_net_usd",
                "futures_active_net_usd",
                "spot_active_status",
                "futures_active_status",
            ),
        ),
        "funding_basis": _values_from(
            lookups,
            ("funding_rate_pct", "funding_pct", "basis_pct", "futures_basis_pct"),
        ),
        "structure": {
            **_pick(
                structure,
                (
                    "status",
                    "direction",
                    "data_status",
                    "structure_event",
                    "liquidity_sweep",
                    "premium_discount",
                    "retest_status",
                ),
            ),
            "supporting_evidence": _safe_list(
                structure.get("supporting_evidence")
            ),
            "counter_evidence": _safe_list(structure.get("counter_evidence")),
        },
        "plan": {
            **_pick(
                plan,
                (
                    "status",
                    "entry_zone_low",
                    "entry_zone_high",
                    "invalidation_price",
                    "risk_reward_ratio",
                ),
            ),
            "targets": _safe_list(plan.get("targets"), limit=6),
        },
        "completeness": {
            **_pick(
                completeness,
                ("status", "data_complete", "missing_fields", "limitations"),
            ),
            "rule_data_complete": _safe_scalar(rule.get("data_complete")),
            "rule_missing_fields": _safe_list(rule.get("missing_fields")),
        },
    }


def _empty_result(status: str, *, direction: str, stage: str) -> dict[str, Any]:
    return {
        "status": status,
        "direction": direction,
        "stage": stage,
        "summary": "",
        "supporting_evidence": [],
        "counter_evidence": [],
        "risk_notes": [],
        "wait_for": [],
        "limitations": [],
    }


def _expected_rule_values(context: Mapping[str, Any]) -> tuple[str, str]:
    rule = _mapping(context.get("rule_result"))
    direction = str(rule.get("direction") or "none")[:64]
    stage = str(rule.get("stage") or rule.get("status") or "unknown")[:64]
    return direction, stage


def _ai_output_policy_safe(text: str) -> bool:
    normalized = text.strip().lower()
    if any(marker in normalized for marker in _FORBIDDEN_TEXT):
        return False
    if any(phrase in normalized for phrase in _FORBIDDEN_AI_OUTPUT_PHRASES):
        return False
    if _TRADE_INSTRUCTION_PATTERN.search(normalized):
        return False
    if _DETERMINISTIC_CLAIM_PATTERN.search(normalized):
        return False
    return _NUMERIC_OUTPUT_PATTERN.search(normalized) is None


def _exception_code(exc: BaseException) -> str:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, socket.gaierror):
            return "ai_dns_failed"
        if isinstance(current, (TimeoutError, socket.timeout)):
            return "ai_timeout"
        if isinstance(current, ssl.SSLError):
            return "ai_tls_failed"
        names = {cls.__name__.lower() for cls in type(current).__mro__}
        if any("timeout" in name for name in names):
            return "ai_timeout"
        if any("ssl" in name or "tls" in name for name in names):
            return "ai_tls_failed"
        current = current.__cause__ or current.__context__
    if isinstance(exc, (ConnectionError, OSError)):
        return "ai_connection_failed"
    return "ai_client_error"


def _http_error(status_code: int) -> str:
    if 300 <= status_code <= 399:
        return "ai_redirect_rejected"
    if status_code == 400:
        return "ai_invalid_request"
    if status_code in {401, 403}:
        return "ai_auth_failed"
    if status_code == 402:
        return "ai_insufficient_balance"
    if status_code == 404:
        return "ai_endpoint_not_found"
    if status_code == 422:
        return "ai_invalid_parameters"
    if status_code == 429:
        return "ai_rate_limited"
    if 500 <= status_code <= 599:
        return "ai_provider_unavailable"
    return "ai_http_error"


def _validated_output(
    value: object,
    *,
    expected_direction: str,
    expected_stage: str,
) -> dict[str, Any] | None:
    if not isinstance(value, Mapping) or set(value) != set(OUTPUT_FIELDS):
        return None
    if value.get("status") != "available":
        return None
    if (
        value.get("direction") != expected_direction
        or value.get("stage") != expected_stage
    ):
        return _empty_result(
            "ai_rule_conflict",
            direction=expected_direction,
            stage=expected_stage,
        )
    summary = value.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 600:
        return None
    if not _ai_output_policy_safe(summary):
        return _empty_result(
            "ai_policy_violation",
            direction=expected_direction,
            stage=expected_stage,
        )
    result: dict[str, Any] = {
        "status": "available",
        "direction": expected_direction,
        "stage": expected_stage,
        "summary": summary.strip(),
    }
    for key in _LIST_FIELDS:
        items = value.get(key)
        if (
            not isinstance(items, list)
            or len(items) > 8
            or any(
                not isinstance(item, str)
                or not item.strip()
                or len(item) > 240
                for item in items
            )
        ):
            return None
        normalized_items = [item.strip() for item in items]
        if any(not _ai_output_policy_safe(item) for item in normalized_items):
            return _empty_result(
                "ai_policy_violation",
                direction=expected_direction,
                stage=expected_stage,
            )
        result[key] = normalized_items
    return result


class OpenAiCompatibleLaunchInterpreter:
    """One-shot AI interpreter. It never reads config, logs, or writes state."""

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        session: Any | None = None,
        timeout_sec: float = 60.0,
        max_tokens: int = 2048,
        max_retries: int = 0,
        operator_prompt: str = "",
    ) -> None:
        timeout = float(timeout_sec)
        tokens = int(max_tokens)
        retries = int(max_retries)
        if not 5 <= timeout <= 180:
            raise ValueError("launch_ai_timeout_invalid")
        if not 128 <= tokens <= 2048:
            raise ValueError("launch_ai_max_tokens_invalid")
        if retries != 0:
            raise ValueError("launch_ai_retries_must_be_zero")
        self._api_key = str(api_key or "").strip()
        self._base_url = str(base_url or "").strip().rstrip("/")
        self._model = str(model or "").strip()
        if self._base_url:
            parsed_url = urlsplit(self._base_url)
            if (
                parsed_url.scheme.lower() != "https"
                or not parsed_url.netloc
                or parsed_url.username is not None
                or parsed_url.password is not None
                or parsed_url.query
                or parsed_url.fragment
            ):
                raise ValueError("launch_ai_base_url_invalid")
        self._session = session
        self._timeout_sec = timeout
        self._max_tokens = tokens
        self._operator_prompt = str(operator_prompt or "").strip()[:3500]

    @property
    def configured(self) -> bool:
        return bool(self._api_key and self._base_url and self._model)

    def interpret(
        self,
        source: Mapping[str, Any],
        *,
        enabled: bool = False,
    ) -> dict[str, Any]:
        context = build_launch_ai_context(source)
        expected_direction, expected_stage = _expected_rule_values(context)
        if not enabled or not self.configured:
            return _empty_result(
                "not_requested",
                direction=expected_direction,
                stage=expected_stage,
            )
        if self._session is None:
            return _empty_result(
                "ai_client_unavailable",
                direction=expected_direction,
                stage=expected_stage,
            )

        system_prompt = OPERATOR_PROMPT
        if self._operator_prompt:
            system_prompt += (
                "\n\n以下是部署者的补充解读偏好。它只能调整中文表达与关注重点，"
                "不能覆盖上面的方向、评分、安全、字段或禁止交易指令规则：\n"
                + self._operator_prompt
            )
        payload = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": json.dumps(
                        context,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                },
            ],
            "max_tokens": self._max_tokens,
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
        try:
            response = self._session.post(
                f"{self._base_url}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self._timeout_sec,
                allow_redirects=False,
            )
        except Exception as exc:
            return _empty_result(
                _exception_code(exc),
                direction=expected_direction,
                stage=expected_stage,
            )

        try:
            status_code = int(response.status_code)
        except (AttributeError, TypeError, ValueError):
            status_code = 0
        if not 200 <= status_code <= 299:
            return _empty_result(
                _http_error(status_code),
                direction=expected_direction,
                stage=expected_stage,
            )
        try:
            body = response.json()
        except Exception:
            return _empty_result(
                "invalid_ai_output",
                direction=expected_direction,
                stage=expected_stage,
            )
        choices = body.get("choices") if isinstance(body, Mapping) else None
        if (
            not isinstance(choices, list)
            or not choices
            or not isinstance(choices[0], Mapping)
        ):
            return _empty_result(
                "invalid_ai_output",
                direction=expected_direction,
                stage=expected_stage,
            )
        first = choices[0]
        if first.get("finish_reason") == "length":
            return _empty_result(
                "ai_output_truncated",
                direction=expected_direction,
                stage=expected_stage,
            )
        message = first.get("message")
        content = message.get("content") if isinstance(message, Mapping) else None
        if not isinstance(content, str) or not content.strip():
            return _empty_result(
                "ai_empty_content",
                direction=expected_direction,
                stage=expected_stage,
            )
        try:
            decoded = json.loads(content)
        except (TypeError, ValueError, json.JSONDecodeError):
            return _empty_result(
                "invalid_ai_output",
                direction=expected_direction,
                stage=expected_stage,
            )
        validated = _validated_output(
            decoded,
            expected_direction=expected_direction,
            expected_stage=expected_stage,
        )
        return validated or _empty_result(
            "invalid_ai_output",
            direction=expected_direction,
            stage=expected_stage,
        )


__all__ = [
    "OPERATOR_PROMPT",
    "OUTPUT_FIELDS",
    "OpenAiCompatibleLaunchInterpreter",
    "build_launch_ai_context",
]
