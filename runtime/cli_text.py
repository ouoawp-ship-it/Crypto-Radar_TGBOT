from __future__ import annotations


_PUSH_STATUS_TEXT = {
    "sent": "发送成功",
    "dry_run": "安全演练，未发送真实消息",
    "skipped": "已跳过",
    "blocked": "已阻止",
    "failed": "发送失败",
    "partial": "部分消息发送成功",
    "uncertain": "投递结果待确认",
}

_PUSH_REASON_TEXT = {
    "telegram_api": "Telegram 已接收消息",
    "telegram_photo_api": "Telegram 已接收图片消息",
    "telegram_api_failed": "Telegram 接口调用失败",
    "telegram_photo_api_failed": "Telegram 图片接口调用失败",
    "dedup_cooldown": "同类内容仍在防重复冷却期内",
    "template_daily_limit": "该类消息今天的发送额度已用完",
    "global_hourly_limit": "本小时发送额度已用完，本次没有发送，请稍后再试",
    "send_flag_not_set": "当前未启用真实发送",
    "missing_confirm_real_send": "未完成真实发送的第二重确认",
    "telegram_not_configured": "Telegram 机器人或群配置不完整",
    "telegram_topic_not_configured": "对应的 Telegram 话题尚未配置",
    "delivery_quarantine": "上一次投递尚未安全收口，为避免重复发送已暂停",
    "telegram_delivery_uncertain": "Telegram 是否已收到本次消息暂时无法确认；为避免重复发送，系统已停止重试",
    "invalid_telegram_config": "Telegram 配置无效或不完整",
    "invalid_png": "图片格式无效",
    "photo_too_large": "图片大小超过 Telegram 限制",
    "caption_too_long": "图片说明文字超过 Telegram 限制",
    "chart_unavailable": "图表暂时无法生成",
    "chart_generation_failed": "图表生成失败",
}

_CHECK_NAME_TEXT = {
    "telegram_bot_token": "Telegram 机器人密钥",
    "telegram_chat_id": "Telegram 群 ID",
    "telegram_topic_radar_summary": "资金摘要专属话题",
    "telegram_topic_launch_alert": "脉冲雷达专属话题",
    "telegram_topic_announcement_alert": "公告风险专属话题",
    "telegram_topic_flow_radar": "五因子资金流专属话题",
    "telegram_topic_funding_alert": "资金费率警报专属话题",
    "runtime_health": "核心运行状态",
}


def push_status_text(status: str) -> str:
    """Translate one delivery status without changing its stored code."""

    return _PUSH_STATUS_TEXT.get(str(status or "").strip(), "状态未知")


def push_reason_text(reason: str) -> str:
    """Translate one safe reason; unknown codes stay out of the user view."""

    normalized = str(reason or "").strip()
    if not normalized:
        return ""
    return _PUSH_REASON_TEXT.get(
        normalized,
        "详细原因已保留在内部运行记录中",
    )


def check_name_text(name: str) -> str:
    """Translate a readiness item while retaining its internal key elsewhere."""

    return _CHECK_NAME_TEXT.get(str(name or "").strip(), "其他检查项")


def format_push_result_cn(
    title: str,
    status: str,
    reason: str = "",
    *,
    index: int | None = None,
    note: str = "",
) -> str:
    """Return one concise Chinese line for a CLI delivery result."""

    display_title = str(title or "推送").strip() or "推送"
    if index is not None:
        display_title = f"{display_title}（第{max(1, int(index))}条）"
    result = f"{display_title}：{push_status_text(status)}"
    details = [value for value in (push_reason_text(reason), str(note).strip()) if value]
    if details:
        result += f"（{'；'.join(details)}）"
    return result


__all__ = [
    "check_name_text",
    "format_push_result_cn",
    "push_reason_text",
    "push_status_text",
]
