from __future__ import annotations

import unittest

from runtime.cli_text import (
    check_name_text,
    format_push_result_cn,
    push_reason_text,
    push_status_text,
)


class PushResultChineseTextTests(unittest.TestCase):
    def test_readiness_names_have_plain_chinese_labels(self) -> None:
        self.assertEqual(
            check_name_text("telegram_topic_funding_alert"),
            "资金费率警报专属话题",
        )
        self.assertEqual(check_name_text("runtime_health"), "核心运行状态")
        self.assertEqual(
            check_name_text("pulse_kline_budget"),
            "脉冲图表请求余量",
        )
        self.assertEqual(
            check_name_text("telegram_topic_consolidation_breakout"),
            "盘整突破雷达专属话题",
        )
        self.assertEqual(check_name_text("new_internal_check"), "其他检查项")

    def test_all_delivery_statuses_have_plain_chinese_labels(self) -> None:
        expected = {
            "sent": "发送成功",
            "dry_run": "安全演练，未发送真实消息",
            "skipped": "已跳过",
            "blocked": "已阻止",
            "failed": "发送失败",
            "partial": "部分消息发送成功",
            "uncertain": "投递结果待确认",
        }

        for status, label in expected.items():
            with self.subTest(status=status):
                self.assertEqual(push_status_text(status), label)

    def test_common_delivery_reasons_are_translated(self) -> None:
        cases = {
            "global_hourly_limit": "本小时发送额度已用完",
            "dedup_cooldown": "防重复冷却期",
            "template_daily_limit": "今天的发送额度已用完",
            "send_flag_not_set": "未启用真实发送",
            "missing_confirm_real_send": "第二重确认",
            "telegram_not_configured": "机器人或群配置不完整",
            "telegram_topic_not_configured": "话题尚未配置",
            "telegram_topic_invalid": "话题 ID 无效",
            "telegram_api_failed": "接口调用失败",
            "delivery_quarantine": "尚未安全收口",
            "telegram_delivery_uncertain": "停止重试",
        }

        for reason, expected_text in cases.items():
            with self.subTest(reason=reason):
                rendered = push_reason_text(reason)
                self.assertIn(expected_text, rendered)
                self.assertNotIn(reason, rendered)

    def test_unknown_reason_does_not_leak_internal_code(self) -> None:
        internal_code = "provider_secret_failure_code"

        rendered = format_push_result_cn(
            "Telegram 测试",
            "failed",
            internal_code,
        )

        self.assertIn("发送失败", rendered)
        self.assertIn("内部运行记录", rendered)
        self.assertNotIn(internal_code, rendered)

    def test_index_and_note_use_chinese_sentence(self) -> None:
        rendered = format_push_result_cn(
            "脉冲雷达推送",
            "skipped",
            "chart_unavailable",
            index=2,
            note="旧卡片已保留",
        )

        self.assertEqual(
            rendered,
            "脉冲雷达推送（第2条）：已跳过"
            "（图表暂时无法生成；旧卡片已保留）",
        )


if __name__ == "__main__":
    unittest.main()
