from __future__ import annotations

import json
import unittest
from pathlib import Path

from paopao_radar.onchain_flow.signal_formatter import OarSignalCardFormatter


ROOT = Path(__file__).resolve().parents[2]


class OarSignalFormatterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        fixture = json.loads(
            (ROOT / "tests" / "fixtures" / "onchain" / "p7_signal_cards.json")
            .read_text(encoding="utf-8")
        )
        cls.signal_types = fixture["signal_types"]
        cls.base_payload = fixture["payload"]
        cls.formatter = OarSignalCardFormatter()

    def test_all_required_templates_are_formatter_only(self) -> None:
        for signal_type in self.signal_types:
            with self.subTest(signal_type=signal_type):
                payload = dict(self.base_payload)
                payload["signal_type"] = signal_type
                text = self.formatter.format(payload)
                self.assertIn("等级：", text)
                self.assertIn("时间：", text)
                self.assertIn("链：", text)
                self.assertIn("Token：", text)
                self.assertIn("合约：", text)
                self.assertIn("Transfer / 窗口事实", text)
                self.assertIn("地址路径", text)
                self.assertIn("https://bscscan.com/address/", text)
                self.assertIn("发送方余额与供应占比", text)
                self.assertIn("CEX 标签", text)
                self.assertIn("支持证据", text)
                self.assertIn("反证", text)
                self.assertIn("数据完整性", text)
                self.assertIn("结论", text)
                self.assertIn("限制", text)
                self.assertIn("https://bscscan.com/tx/", text)
                self.assertIn("rule_score_not_probability", text)
                self.assertNotIn("circulating_supply_unavailable", text)

    def test_forbidden_claims_and_secrets_are_absent(self) -> None:
        payload = dict(self.base_payload)
        payload["signal_type"] = "large_cex_inflow"
        payload["api_key"] = "PRODUCTION_SECRET"
        payload["rpc_url"] = "https://secret.invalid/rpc"
        text = self.formatter.format(payload)
        for forbidden in (
            "确定会跌",
            "立即卖出",
            "确定是庄家",
            "已经卖出",
            "PRODUCTION_SECRET",
            "secret.invalid",
            "Chat ID",
            "Topic ID",
        ):
            self.assertNotIn(forbidden, text)

    def test_untrusted_url_and_html_are_rejected_or_escaped(self) -> None:
        payload = dict(self.base_payload)
        payload["signal_type"] = "large_cex_inflow"
        payload["token"] = {
            "symbol": "<script>",
            "contract": "0x" + "a" * 40,
            "contract_url": "http://example.invalid/token",
        }
        payload["transfer"] = {
            "amount_token": "1",
            "explorer_url": "javascript:alert(1)",
        }
        text = self.formatter.format(payload)
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("javascript:", text)
        self.assertNotIn("http://example.invalid", text)


if __name__ == "__main__":
    unittest.main()
