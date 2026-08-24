from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from config.settings import Settings
from radars.pulse import review_store


def _record(
    symbol: str,
    template: str,
    *,
    radar: str = "alert",
    ts: int | None = None,
    message_id: int = 123,
    outcomes: dict[str, dict[str, float]] | None = None,
) -> dict:
    return {
        "id": f"{symbol}-{template}-{radar}",
        "radar": radar,
        "template": template,
        "symbol": symbol,
        "price": 1.0,
        "oi_pct": 0.0,
        "price_pct": 0.0,
        "divergence": 0.0,
        "ts": int(time.time()) if ts is None else ts,
        "message_id": message_id,
        "outcomes": outcomes or {},
        "reply_sent": False,
    }


class FakeGateway:
    def __init__(self, sent: bool = True) -> None:
        self.sent = sent
        self.calls: list[tuple[str, str, str, int | None]] = []

    def send(self, text, template_id, dedup_key, **kwargs):
        self.calls.append((text, template_id, dedup_key, kwargs.get("reply_to_message_id")))
        return SimpleNamespace(
            status="sent" if self.sent else "dry_run",
            reason="ok" if self.sent else "send_flag_not_set",
            sent=self.sent,
        )


class ReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(data_dir=Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_record_and_load(self) -> None:
        review_store.record_signals(self.settings, [
            {
                "radar": "alert",
                "template": "health_up",
                "symbol": "cetususdt",
                "price": 0.0215,
                "oi_pct": 31.11,
                "price_pct": 7.06,
                "message_id": 1001,
            },
        ])
        records = review_store.load_records(self.settings)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["symbol"], "CETUSUSDT")
        self.assertEqual(records[0]["price"], 0.0215)
        self.assertEqual(records[0]["message_id"], 1001)
        self.assertFalse(records[0]["reply_sent"])

    def test_template_hit(self) -> None:
        self.assertTrue(review_store.template_hit("health_up", 2.0))
        self.assertFalse(review_store.template_hit("health_up", -1.0))
        self.assertTrue(review_store.template_hit("build", 2.0))
        self.assertFalse(review_store.template_hit("panic", 2.0))
        self.assertTrue(review_store.template_hit("extreme", -5.0))
        self.assertIsNone(review_store.template_hit("unknown", 1.0))

    def test_format_review_reply(self) -> None:
        record = _record(
            "CETUSUSDT",
            "health_up",
            outcomes={
                "3600": {"price": 1.03, "pct": 3.2},
                "14400": {"price": 1.05, "pct": 5.1},
            },
        )
        text = review_store.format_review_reply(record)
        self.assertIn("CETUSUSDT", text)
        self.assertIn("健康上涨", text)
        self.assertIn("1h +3.2% ✅", text)
        self.assertIn("4h +5.1% ✅", text)
        self.assertIn("方向命中 ✅", text)

    def test_format_grouped_reply(self) -> None:
        records = [
            _record(
                "ICXUSDT", "build", radar="divergence",
                outcomes={"7200": {"price": 1.08, "pct": 8.0}},
            ),
            _record(
                "MAVUSDT", "panic", radar="divergence",
                outcomes={"7200": {"price": 0.95, "pct": -5.0}},
            ),
        ]
        text = review_store.format_grouped_reply(records)
        self.assertIn("背离卡片复盘", text)
        self.assertIn("ICXUSDT", text)
        self.assertIn("MAVUSDT", text)
        self.assertIn("+8.0% ✅", text)
        self.assertIn("-5.0% ✅", text)

    def test_week_top_gainers(self) -> None:
        now = int(time.time())
        review_store.save_records(self.settings, [
            _record(
                "AAUSDT", "health_up", ts=now,
                outcomes={"3600": {"price": 1.05, "pct": 5.0}},
            ),
            _record(
                "BBUSDT", "resonance", radar="divergence", ts=now,
                outcomes={"7200": {"price": 1.12, "pct": 12.0}},
            ),
            _record(
                "CCUSDT", "health_up", ts=now - 20 * 86400,
                outcomes={"3600": {"price": 1.20, "pct": 20.0}},
            ),
        ])
        top = review_store.week_top_gainers(self.settings, now_ts=now)
        self.assertEqual([row["symbol"] for row in top], ["BBUSDT", "AAUSDT"])
        self.assertEqual(top[0]["pct"], 12.0)

    def test_send_replies_marks_only_on_real_send(self) -> None:
        record = _record(
            "AAUSDT", "health_up",
            outcomes={
                "3600": {"price": 1.05, "pct": 5.0},
                "14400": {"price": 1.08, "pct": 8.0},
            },
        )
        review_store.save_records(self.settings, [record])

        dry_gateway = FakeGateway(sent=False)
        review_store.send_review_replies(
            self.settings, dry_gateway,
            send=False, confirm_real_send=False,
        )
        records = review_store.load_records(self.settings)
        self.assertFalse(records[0]["reply_sent"])
        self.assertNotIn("reply_status", records[0])

        real_gateway = FakeGateway(sent=True)
        review_store.send_review_replies(
            self.settings, real_gateway,
            send=True, confirm_real_send=True,
        )
        records = review_store.load_records(self.settings)
        self.assertTrue(records[0]["reply_sent"])
        self.assertEqual(len(real_gateway.calls), 1)
        self.assertEqual(real_gateway.calls[0][3], 123)

    def test_divergence_replies_grouped_by_message(self) -> None:
        records = [
            _record(
                "ICXUSDT", "build", radar="divergence",
                outcomes={"7200": {"price": 1.08, "pct": 8.0}},
            ),
            _record(
                "MAVUSDT", "panic", radar="divergence",
                outcomes={"7200": {"price": 0.95, "pct": -5.0}},
            ),
        ]
        review_store.save_records(self.settings, records)
        gateway = FakeGateway(sent=True)
        sent = review_store.send_review_replies(
            self.settings, gateway,
            send=True, confirm_real_send=True,
        )
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(gateway.calls[0][3], 123)
        self.assertIn("ICXUSDT", gateway.calls[0][0])
        self.assertIn("MAVUSDT", gateway.calls[0][0])
        saved = review_store.load_records(self.settings)
        self.assertTrue(all(row["reply_sent"] for row in saved))


if __name__ == "__main__":
    unittest.main()
