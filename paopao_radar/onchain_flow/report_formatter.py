from __future__ import annotations

from html import escape
from typing import Any


BIAS_LABELS = {
    "bullish": "偏多",
    "bearish": "偏空",
    "neutral": "中性",
    "uncertain": "不确定",
}


def _map(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _short_address(value: object) -> str:
    address = str(value or "")
    if len(address) <= 14:
        return address
    return f"{address[:8]}…{address[-6:]}"


def _lines(values: object, *, limit: int) -> list[str]:
    return [
        f"- {escape(str(item))}"
        for item in _items(values)[:limit]
        if str(item).strip()
    ]


def format_token_report(payload: dict[str, object]) -> str:
    report = _map(payload.get("report"))
    summary = _map(report.get("rule_summary"))
    token = _map(summary.get("token"))
    query = _map(summary.get("query"))
    transfers = _map(summary.get("transfer_summary"))
    flows = _map(summary.get("cex_flows"))
    primary = _map(summary.get("primary_behavior"))
    symbol = escape(str(token.get("symbol") or "UNKNOWN"))
    contract = _short_address(token.get("contract"))
    complete = bool(query.get("complete"))
    completeness = "数据完整" if complete else "⚠️ 数据不完整"
    lines = [
        f"🔗 <b>链上活动雷达 · {symbol}</b>",
        "",
        "<b>查询</b>",
        f"- Base · 最近 {escape(str(query.get('window') or '-'))} · {completeness}",
        f"- 合约：<code>{escape(contract)}</code>",
        "",
        "<b>链上概览</b>",
        f"- Transfer：{int(transfers.get('transfer_count') or 0)} 笔",
        (
            f"- Token 总量：{escape(str(transfers.get('total_token_amount') or '0'))}"
        ),
        (
            "- 流入交易所 / 从交易所提出："
            f"{escape(str(flows.get('gross_inflow_token') or '0'))} / "
            f"{escape(str(flows.get('gross_outflow_token') or '0'))}"
        ),
        f"- 净流向（流入-提出）：{escape(str(flows.get('net_flow_token') or '0'))}",
        (
            "- 独立发送/接收钱包："
            f"{int(transfers.get('unique_senders') or 0)} / "
            f"{int(transfers.get('unique_receivers') or 0)}"
        ),
        "",
        "<b>行为判断</b>",
        f"- {escape(str(primary.get('label') or '数据不足'))}",
        (
            f"- 规则分数：{int(primary.get('score') or 0)}"
            "（评分不是概率）"
        ),
        (
            f"- 证据强度：{escape(str(primary.get('confidence_level') or 'low'))}"
        ),
    ]
    support = _lines(primary.get("supporting_evidence"), limit=4)
    counter = _lines(primary.get("counter_evidence"), limit=3)
    if support:
        lines.extend(["- 支持证据：", *support])
    if counter:
        lines.extend(["- 反证：", *counter])

    groups = [
        item
        for item in _items(summary.get("wallet_groups"))
        if isinstance(item, dict)
    ][:3]
    lines.extend(["", "<b>钱包关联候选</b>"])
    if groups:
        for item in groups:
            lines.append(
                "- "
                f"{escape(str(item.get('group_type') or '候选群组'))} · "
                f"{escape(str(item.get('level') or '证据不足'))} · "
                f"{int(item.get('score') or 0)}分"
            )
    else:
        lines.append("- 未形成达到门槛的钱包关联候选")

    ai = _map(report.get("ai"))
    ai_result = _map(ai.get("result"))
    lines.extend(["", "<b>AI 解读</b>"])
    if ai_result:
        lines.extend(
            [
                (
                    "- 倾向："
                    f"{BIAS_LABELS.get(str(ai_result.get('bias')), '不确定')} · "
                    f"置信度 {escape(str(ai_result.get('confidence') or 'low'))}"
                ),
                (
                    "- 主要假设："
                    f"{escape(str(ai_result.get('primary_hypothesis') or ''))}"
                ),
            ]
        )
        next_actions = _lines(
            ai_result.get("likely_next_actions"), limit=3
        )
        watch = _lines(ai_result.get("watch_signals"), limit=3)
        invalidation = _lines(
            ai_result.get("invalidation_conditions"), limit=3
        )
        if next_actions:
            lines.extend(["- 可能下一步：", *next_actions])
        if watch:
            lines.extend(["- 继续观察：", *watch])
        if invalidation:
            lines.extend(["- 失效条件：", *invalidation])
    else:
        lines.append(
            f"- 未使用 AI（状态：{escape(str(ai.get('status') or 'not_requested'))}），"
            "以上为确定性规则摘要。"
        )

    representatives = [
        item
        for item in _items(summary.get("representative_transfers"))
        if isinstance(item, dict)
    ][:3]
    if representatives:
        lines.extend(["", "<b>代表性交易</b>"])
        for index, item in enumerate(representatives, 1):
            url = escape(str(item.get("explorer_url") or ""), quote=True)
            amount = escape(str(item.get("amount") or "0"))
            flow = escape(str(item.get("flow_type") or "unclassified"))
            if url.startswith("https://basescan.org/tx/"):
                lines.append(
                    f'- <a href="{url}">交易 {index}</a> · {amount} · {flow}'
                )
            else:
                lines.append(f"- 交易 {index} · {amount} · {flow}")

    lines.extend(
        [
            "",
            "<b>限制</b>",
            "- 入所不等于已经卖出；提币不等于已经买入或必然上涨。",
            "- 钱包关联分数不是概率，高分不等于确认同一主力。",
            "- 数据不完整时降低结论等级，不形成高确定性判断。",
        ]
    )
    return "\n".join(lines)
