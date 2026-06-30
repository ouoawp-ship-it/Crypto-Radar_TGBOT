from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from paopao_radar.price_alerts import (
    PriceAlertStore,
    format_price,
    normalize_symbol,
    triggered_alerts,
)


class PriceAlertStoreTests(unittest.TestCase):
    def test_normalize_symbol_adds_usdt(self) -> None:
        self.assertEqual(normalize_symbol("btc"), "BTCUSDT")
        self.assertEqual(normalize_symbol("ETH/USDT"), "ETHUSDT")

    def test_create_list_pause_resume_delete_alert(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceAlertStore(Path(tmp) / "alerts.db")
            alert = store.create_alert(
                user_id="42",
                chat_id="42",
                username="tester",
                symbol="btc",
                direction="below",
                target_price=58000,
            )

            self.assertEqual(alert.symbol, "BTCUSDT")
            self.assertEqual(alert.direction, "below")
            self.assertEqual(store.stats()["active"], 1)
            self.assertEqual(len(store.list_alerts(user_id="42")), 1)

            self.assertTrue(store.set_status(alert.id, "paused", user_id="42"))
            self.assertEqual(store.get_alert(alert.id).status, "paused")  # type: ignore[union-attr]
            self.assertTrue(store.set_status(alert.id, "active", user_id="42"))
            self.assertEqual(store.get_alert(alert.id).status, "active")  # type: ignore[union-attr]
            self.assertTrue(store.delete_alert(alert.id, user_id="42"))
            self.assertEqual(store.stats()["total"], 0)

    def test_triggered_alerts_direction(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceAlertStore(Path(tmp) / "alerts.db")
            below = store.create_alert(user_id="1", chat_id="1", symbol="BTC", direction="below", target_price=58000)
            above = store.create_alert(user_id="1", chat_id="1", symbol="ETH", direction="above", target_price=4200)

            hits = triggered_alerts([below, above], {"BTCUSDT": 57000, "ETHUSDT": 4100})

            self.assertEqual([(item.symbol, price) for item, price in hits], [("BTCUSDT", 57000)])

    def test_format_price(self) -> None:
        self.assertEqual(format_price(58000), "$58,000.00")
        self.assertEqual(format_price(0.123456), "$0.123456")


if __name__ == "__main__":
    unittest.main()
