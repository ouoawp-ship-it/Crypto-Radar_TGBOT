from __future__ import annotations

from html import escape
from typing import Any


BIAS_LABELS = {
    "bullish": "偏多",
    "bearish": "偏空",
    "neutral": "中性",
    "uncertain": "不确定",
}

CONFIDENCE_LABELS = {
    "low": "低",
    "medium": "中等",
    "high": "高",
}

AI_STATUS_LABELS = {
    "not_requested": "未请求",
    "disabled": "未启用",
    "available": "已生成",
    "cached": "使用合规缓存",
    "failed": "生成失败",
}

EVIDENCE_LABELS = {
    "wallet_count_met": "参与的不同钱包数量达到规则门槛",
    "transaction_count_met": "符合该行为的转账笔数达到规则门槛",
    "token_amount_share_met": "该行为涉及的代币数量占比达到规则门槛",
    "inflow_dominance_met": "流入交易所的方向占比达到规则门槛",
    "outflow_dominance_met": "从交易所提出的方向占比达到规则门槛",
    "inflow_transaction_count_met": "流入交易所的转账笔数达到规则门槛",
    "outflow_transaction_count_met": "从交易所提出的转账笔数达到规则门槛",
    "multiple_15m_buckets": "在多个 15 分钟子窗口中重复出现",
    "multiple_or_repeated_counterparties": "涉及多个或重复出现的对手钱包",
    "repeated_across_nested_windows": "在不同长度的时间窗口中重复出现",
    "direction_token_amount_share_met": "该方向的代币数量占比达到规则门槛",
    "opposite_cex_flow_material": "相反方向的交易所流量占比较高",
    "cex_internal_activity_dominant": "交易所内部或跨交易所流转占比较高",
}

STRUCTURE_EVIDENCE_POINTS = {
    "wallet_count_met": 30,
    "transaction_count_met": 20,
    "token_amount_share_met": 20,
    "multiple_15m_buckets": 15,
    "repeated_across_nested_windows": 15,
}

DIRECTION_EVIDENCE_POINTS = {
    "inflow_dominance_met": 25,
    "outflow_dominance_met": 25,
    "inflow_transaction_count_met": 20,
    "outflow_transaction_count_met": 20,
    "multiple_15m_buckets": 20,
    "multiple_or_repeated_counterparties": 15,
    "repeated_across_nested_windows": 10,
    "direction_token_amount_share_met": 10,
}

GROUP_TYPE_LABELS = {
    "shared_target": (
        "共同收款地址",
        "多个钱包在窗口内把币转到同一个非交易所或身份未知地址",
    ),
    "shared_source": (
        "共同付款地址",
        "同一个非交易所或身份未知地址在窗口内向多个钱包转账",
    ),
    "synchronized_cex_inflow": (
        "同步流入同一交易所",
        "多个钱包在接近的时间向同一家交易所转入代币",
    ),
    "synchronized_cex_outflow": (
        "同步从同一交易所提出",
        "同一家交易所在接近的时间向多个钱包转出代币",
    ),
}

GROUP_LEVEL_LABELS = {
    "证据不足": "证据不足",
    "弱关联": "弱关联候选",
    "中等概率关联": "中等关联候选",
    "高概率关联": "高关联候选",
    "强关联候选": "强关联候选",
}

GROUP_EVIDENCE_POINTS = {
    "repeated_shared_target": ("多个钱包共同转入同一地址", 30),
    "repeated_shared_source": ("同一地址共同转出到多个钱包", 30),
    "repeated_across_nested_windows": ("不同长度窗口中重复出现", 20),
    "time_synchronized": ("转账时间接近", 15),
    "amounts_similar": ("转账数量相近", 15),
    "direct_token_transfer_between_members": ("成员钱包之间存在直接转账", 10),
    "same_exchange_synchronized_flow": ("同步流向或来自同一交易所", 10),
}

FLOW_TYPE_LABELS = {
    "inflow": "流入交易所",
    "outflow": "从交易所提出",
    "internal": "交易所内部流转",
    "consolidation": "交易所归集",
    "cross_cex": "交易所之间流转",
    "non_cex": "非交易所地址间转账",
    "unclassified": "尚未分类",
    "mint": "铸造",
    "burn": "销毁",
}


def _map(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _items(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _lines(values: object, *, limit: int) -> list[str]:
    return [
        f"- {escape(str(item))}"
        for item in _items(values)[:limit]
        if str(item).strip()
    ]


def _evidence_lines(
    values: object,
    *,
    limit: int,
    points: dict[str, int] | None = None,
) -> list[str]:
    result: list[str] = []
    for item in _items(values)[:limit]:
        code = str(item).strip()
        if not code:
            continue
        label = EVIDENCE_LABELS.get(code, "其他已记录的规则证据")
        score = (points or {}).get(code)
        suffix = f"（+{score}分）" if score is not None else ""
        result.append(f"- {escape(label)}{suffix}")
    return result


def _contract_line(value: object, chain_id: object) -> str:
    contract = str(value or "")
    if (
        str(chain_id or "8453") == "8453"
        and len(contract) == 42
        and contract.startswith("0x")
        and all(character in "0123456789abcdefABCDEF" for character in contract[2:])
    ):
        safe_contract = escape(contract)
        safe_url = escape(
            f"https://basescan.org/token/{contract}", quote=True
        )
        return f'- 合约：<a href="{safe_url}">{safe_contract}</a>'
    return f"- 合约：<code>{escape(contract or '-')}</code>"


def format_token_report(payload: dict[str, object]) -> str:
    report = _map(payload.get("report"))
    summary = _map(report.get("rule_summary"))
    token = _map(summary.get("token"))
    query = _map(summary.get("query"))
    transfers = _map(summary.get("transfer_summary"))
    flows = _map(summary.get("cex_flows"))
    label_coverage = _map(summary.get("label_coverage"))
    primary = _map(summary.get("primary_behavior"))
    symbol = escape(str(token.get("symbol") or "UNKNOWN"))
    contract = token.get("contract")
    chain_label = escape(
        str(token.get("chain_name") or token.get("chain") or "Base")
    )
    complete = bool(query.get("complete"))
    completeness = "数据完整" if complete else "⚠️ 数据不完整"
    lines = [
        f"🔗 <b>链上活动雷达 · {symbol}</b>",
        "",
        "<b>查询</b>",
        f"- {chain_label} · 最近 {escape(str(query.get('window') or '-'))} · {completeness}",
        _contract_line(contract, token.get("chain_id")),
        "",
        "<b>链上概览</b>",
        (
            "- Transfer（转账记录）："
            f"{int(transfers.get('transfer_count') or 0)} 笔"
            "（本窗口已读取的 ERC-20 转账笔数）"
        ),
        (
            "- 代币转账总量："
            f"{escape(str(transfers.get('total_token_amount') or '0'))} {symbol}"
        ),
        "  说明：这是每笔转账数量的合计，不是成交额、净流入或买卖量。",
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
    ]
    label_status = str(label_coverage.get("status") or "missing")
    if label_status == "ok":
        lines.append(
            "- 交易所标签覆盖：就绪"
            "（可用于方向分类的已审核标签 "
            f"{int(label_coverage.get('classification_eligible_cex_count') or 0)} 条）"
        )
    else:
        lines.extend(
            [
                "- ⚠️ 交易所标签覆盖：不足",
                "  说明：未分类转账可能包含尚未识别的交易所地址；"
                "当前显示 0 流入/0 提出，不代表已经确认没有入所或提币。",
            ]
        )
    lines.extend(
        [
            "",
            "<b>行为判断</b>",
            f"- {escape(str(primary.get('label') or '数据不足'))}",
            (
                f"- 规则分数：{int(primary.get('score') or 0)}"
                "（评分不是概率）"
            ),
            (
                "- 证据强度："
                f"{CONFIDENCE_LABELS.get(str(primary.get('confidence_level')), '低')}"
                "（表示规则证据的多少和质量，不是成功概率）"
            ),
        ]
    )
    primary_type = str(primary.get("type") or "")
    evidence_points = (
        STRUCTURE_EVIDENCE_POINTS
        if primary_type in {
            "wallet_consolidation_candidate",
            "fanout_candidate",
        }
        else DIRECTION_EVIDENCE_POINTS
    )
    support = _evidence_lines(
        primary.get("supporting_evidence"),
        limit=5,
        points=evidence_points,
    )
    counter = _evidence_lines(primary.get("counter_evidence"), limit=3)
    if support:
        lines.extend(["- 支持证据（也是规则加分项）：", *support])
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
            group_type = str(item.get("group_type") or "")
            group_label, group_explanation = GROUP_TYPE_LABELS.get(
                group_type,
                ("钱包关联候选", "多个钱包出现可解释的共同链上活动"),
            )
            level = GROUP_LEVEL_LABELS.get(
                str(item.get("level") or ""),
                "证据不足",
            )
            lines.append(
                "- "
                f"{escape(group_label)} · {escape(level)} · "
                f"{int(item.get('score') or 0)}分（规则分，不是概率）"
            )
            lines.append(f"  含义：{escape(group_explanation)}。")
            score_parts = [
                f"{label} +{points}分"
                for code in _items(item.get("supporting_evidence"))
                if (detail := GROUP_EVIDENCE_POINTS.get(str(code)))
                for label, points in [detail]
            ]
            if score_parts:
                lines.append(f"  评分依据：{escape('；'.join(score_parts))}。")
            lines.append("  注意：这只是关联线索，不能确认这些钱包属于同一主体。")
    else:
        lines.append("- 未形成达到门槛的钱包关联候选")

    linked = [
        item
        for item in _items(summary.get("linked_market_signals"))
        if isinstance(item, dict)
    ][:3]
    if linked:
        module_labels = {
            "launch": "启动预警",
            "flow": "资金流雷达",
            "funding": "资金费率警报",
            "announcement": "公告风险",
            "manual": "手工关注",
        }
        lines.extend(["", "<b>关联市场信号</b>"])
        for item in linked:
            module = str(item.get("module") or "")
            direction = str(item.get("direction") or "").lower()
            direction_text = {
                "long": " · 方向假设：看多",
                "short": " · 方向假设：看空",
            }.get(direction, "")
            score = item.get("score")
            score_text = (
                f" · {escape(str(score))}分" if score is not None else ""
            )
            age_minutes = max(0, int(item.get("age_sec") or 0)) // 60
            lines.append(
                "- "
                f"{module_labels.get(module, escape(module or '市场信号'))}"
                f"{score_text}{direction_text} · {age_minutes}分钟前"
            )
        lines.append("- 方向仅为结构化信号假设，不代表因果关系或概率。")

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
        ai_status = str(ai.get("status") or "not_requested")
        lines.append(
            f"- 未使用 AI（状态：{AI_STATUS_LABELS.get(ai_status, '不可用')}），"
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
            raw_flow = str(item.get("flow_type") or "unclassified")
            flow = escape(FLOW_TYPE_LABELS.get(raw_flow, "尚未分类"))
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
