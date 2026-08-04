from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit


TITLES = {
    "large_cex_inflow": "单笔大额入所",
    "large_cex_outflow": "单笔大额出所",
    "near_full_exit_to_cex": "近乎清仓入所",
    "full_exit_to_cex": "近乎清仓入所",
    "project_related_cex_inflow": "项目关联地址入所",
    "unlock_related_cex_inflow": "项目解锁关联入所",
    "sustained_cex_net_flow": "持续 CEX 净流",
    "synchronized_cex_inflow": "多钱包同步入所",
    "onchain_market_convergence": "链上综合共振",
    "insufficient_data": "数据不足 / 标签覆盖不足",
}

CONCLUSIONS = {
    "large_cex_inflow": "观察到潜在可售供应进入交易所地址，不能证明交易所内已发生卖出成交。",
    "large_cex_outflow": "观察到资产从交易所地址提出，不能证明已经买入或必然上涨。",
    "near_full_exit_to_cex": "发送方大部分可见余额进入交易所地址，需结合反向流与身份覆盖继续观察。",
    "full_exit_to_cex": "发送方接近全部可见余额进入交易所地址，属于高关注链上风险，不代表交易所内已成交。",
    "project_related_cex_inflow": "已审核或确定性项目关系地址向交易所转入，表示潜在可售供应增加。",
    "unlock_related_cex_inflow": "解锁或归属期关系地址向交易所转入，链上无法证明后续成交。",
    "sustained_cex_net_flow": "完整窗口内交易所净流持续偏向同一方向，仍需结合反向流与市场事实。",
    "synchronized_cex_inflow": "多个地址在相近时间向交易所转入，钱包关联评分不是现实身份概率。",
    "onchain_market_convergence": "链上信号与只读市场事实出现共振，不构成买卖建议。",
    "insufficient_data": "当前数据或标签覆盖不足，仅保留事实，不输出确定的交易所方向。",
}

EVIDENCE_LABELS = {
    "complete_finalized_transfer": "完整且已确认的 Transfer 事实",
    "reviewed_cex_destination": "目标地址为已审核 CEX 标签",
    "reviewed_cex_source": "来源地址为已审核 CEX 标签",
    "single_transfer_usd_threshold_met": "单笔美元规模达到规则门槛",
    "watch_supply_share": "单笔供应占比达到观察门槛",
    "high_supply_share": "单笔供应占比达到高风险门槛",
    "sender_high_exit": "发送方转出比例较高",
    "sender_near_full_exit": "发送方接近清仓转出",
    "sender_full_exit": "发送方接近全部转出",
    "historical_single_transfer_anomaly": "相对历史单笔基线显著异常",
    "identity_coverage_sufficient": "身份标签覆盖满足规则要求",
    "same_window_cex_outflow": "同窗口存在交易所反向流量",
    "limited_liquidity_impact": "可见流动性影响有限",
    "price_unavailable": "价格不可用，未计算美元价值门槛",
    "supply_share_unavailable": "供应量不可用，未计算供应占比",
    "circulating_supply_unavailable": "流通供应量来源不可用",
    "sender_balance_unavailable": "发送方余额快照不完整",
    "sender_balance_inconsistent": "区块边界余额与转账量不一致",
    "historical_baseline_unavailable": "历史单笔基线尚不可用",
    "identity_coverage_insufficient": "地址身份标签覆盖不足",
    "liquidity_impact_unknown": "流动性影响尚未量化",
    "snapshot_incomplete": "余额或供应快照不完整",
    "block_boundary_balance_snapshot": "余额按转账前后区块边界读取，并非交易内逐笔余额",
}

LEVEL_LABELS = {
    "info": "信息",
    "watch": "观察",
    "important": "重要",
    "high_risk": "高风险",
    "critical": "严重",
}

ROLE_LABELS = {
    "unclassified": "未分类",
    "cex_wallet": "交易所钱包",
    "deposit": "交易所充值地址",
    "hot": "交易所热钱包",
    "cold": "交易所冷钱包",
    "collector": "交易所归集地址",
    "treasury": "项目金库",
    "vesting": "归属/解锁地址",
    "owner": "合约 Owner",
    "proxy_admin": "代理管理员",
}


def _mapping(value: object) -> Mapping[str, object]:
    return value if isinstance(value, Mapping) else {}


def _text(value: object, *, fallback: str = "未知") -> str:
    raw = str(value or "").strip()
    return html.escape(raw if raw else fallback, quote=True)


def _decimal(value: object, *, suffix: str = "") -> str:
    if value is None or str(value).strip() == "":
        return "不可用"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return "不可用"
    if not number.is_finite():
        return "不可用"
    rendered = format(number, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    rendered = rendered or "0"
    return html.escape(rendered + suffix)


def _safe_url(value: object) -> str:
    raw = str(value or "").strip()
    parsed = urlsplit(raw)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or bool(parsed.fragment)
    ):
        return ""
    return html.escape(raw, quote=True)


def _list_lines(
    values: object,
    *,
    empty: str,
) -> list[str]:
    rows = (
        [str(value) for value in values]
        if isinstance(values, Sequence) and not isinstance(values, (str, bytes))
        else []
    )
    if not rows:
        return [f"- {empty}"]
    return [
        "- " + _text(EVIDENCE_LABELS.get(value, value), fallback=empty)
        for value in rows[:12]
    ]


class OarSignalCardFormatter:
    """Formatter-only P7 cards. It never creates or calls a gateway."""

    def format(self, payload: Mapping[str, object]) -> str:
        signal_type = str(payload.get("signal_type") or "insufficient_data")
        if signal_type not in TITLES:
            signal_type = "insufficient_data"
        chain = _mapping(payload.get("chain"))
        token = _mapping(payload.get("token"))
        transfer = _mapping(payload.get("transfer"))
        address_path = _mapping(payload.get("address_path"))
        snapshot = _mapping(payload.get("snapshot"))
        cex = _mapping(payload.get("cex_label"))

        contract = _text(token.get("contract"))
        contract_url = _safe_url(token.get("contract_url"))
        contract_display = (
            f'<a href="{contract_url}">{contract}</a>'
            if contract_url
            else contract
        )
        explorer_url = _safe_url(transfer.get("explorer_url"))
        tx_display = (
            f'<a href="{explorer_url}">查看交易</a>'
            if explorer_url
            else "不可用"
        )
        source_address = _text(address_path.get("source_address"), fallback="未知")
        destination_address = _text(
            address_path.get("destination_address"), fallback="未知"
        )
        source_url = _safe_url(address_path.get("source_url"))
        destination_url = _safe_url(address_path.get("destination_url"))
        source_display = (
            f'<a href="{source_url}">{source_address}</a>'
            if source_url
            else source_address
        )
        destination_display = (
            f'<a href="{destination_url}">{destination_address}</a>'
            if destination_url
            else destination_address
        )
        support = _list_lines(
            payload.get("support_evidence"), empty="无充分支持证据"
        )
        counter = _list_lines(
            payload.get("counter_evidence"), empty="未观察到明确反证"
        )
        limitations = _list_lines(
            payload.get("limitations"), empty="保留通用链上解释限制"
        )

        lines = [
            f"🔗 <b>{_text(TITLES[signal_type])}</b>",
            f"等级：{_text(LEVEL_LABELS.get(str(payload.get('level')), '信息'))}",
            f"时间：{_text(payload.get('observed_at'))}",
            f"链：{_text(chain.get('name'))}（{_text(chain.get('chain_id'))}）",
            f"Token：{_text(token.get('symbol'))}",
            f"合约：{contract_display}",
            "",
            "<b>Transfer / 窗口事实</b>",
            f"- Token 数量：{_decimal(transfer.get('amount_token'))}",
            f"- 美元估值：{_decimal(transfer.get('usd_value'), suffix=' USD')}",
            f"- 窗口净流：{_decimal(payload.get('window_net_flow'), suffix=' USD')}",
            f"- Explorer：{tx_display}",
            "",
            "<b>地址路径</b>",
            f"- {source_display} → {destination_display}",
            f"- {_text(ROLE_LABELS.get(str(address_path.get('source_role')), '未分类'))} → "
            f"{_text(ROLE_LABELS.get(str(address_path.get('destination_role')), '未分类'))}",
            f"- 项目关系：{_text(address_path.get('project_relationship'), fallback='unclassified')}",
            "",
            "<b>发送方余额与供应占比</b>",
            f"- 转账前 / 后：{_decimal(snapshot.get('sender_balance_before'))} / "
            f"{_decimal(snapshot.get('sender_balance_after'))}",
            f"- 转出比例：{_decimal(snapshot.get('sender_exit_ratio'))}",
            f"- totalSupply 占比：{_decimal(snapshot.get('total_supply_share'))}",
            f"- 流通供应占比：{_decimal(snapshot.get('circulating_supply_share'))}",
            "",
            "<b>CEX 标签</b>",
            f"- 状态：{_text(cex.get('status'), fallback='insufficient_cex_coverage')}",
            f"- 角色：{_text(cex.get('role'), fallback='unclassified')}",
            "",
            f"<b>支持证据 · 规则分 {int(payload.get('rule_score') or 0)}</b>",
            *support,
            "",
            "<b>反证</b>",
            *counter,
            "",
            f"数据完整性：{_text(payload.get('data_completeness'), fallback='partial')}",
            f"身份覆盖：{_text(payload.get('identity_coverage'), fallback='insufficient')}",
            f"证据强度：{_text(payload.get('evidence_strength'), fallback='limited')}",
            '评分语义：rule_score_not_probability（规则分，不是概率）',
            "",
            f"<b>结论</b>\n{_text(CONCLUSIONS[signal_type])}",
            "",
            "<b>限制</b>",
            *limitations,
            "- 进入 CEX 只表示潜在可售供应增加；从 CEX 提出不代表已经买入。",
            "- 链上无法证明交易所内部是否已经成交，不构成投资建议。",
        ]
        return "\n".join(lines)
