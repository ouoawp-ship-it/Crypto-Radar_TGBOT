from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paopao_radar.price_alerts import (
    AlertMarketQuote,
    PriceAlertStore,
    alert_to_dict,
    fetch_price_alert_prices,
    format_price,
    normalize_symbol,
    triggered_alerts,
)
from paopao_radar.config import Settings


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
            self.assertEqual(alert.exchange, "binance")
            self.assertEqual(alert.market_type, "futures")
            self.assertEqual(alert.pair, "BTCUSDT")
            self.assertEqual(alert.direction, "below")
            self.assertEqual(store.stats()["active"], 1)
            self.assertEqual(len(store.list_alerts(user_id="42")), 1)

            self.assertTrue(store.set_status(alert.id, "paused", user_id="42"))
            self.assertEqual(store.get_alert(alert.id).status, "paused")  # type: ignore[union-attr]
            self.assertTrue(store.set_status(alert.id, "active", user_id="42"))
            self.assertEqual(store.get_alert(alert.id).status, "active")  # type: ignore[union-attr]
            self.assertTrue(store.delete_alert(alert.id, user_id="42"))
            self.assertEqual(store.stats()["total"], 0)

    def test_create_alert_with_selected_exchange_market_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceAlertStore(Path(tmp) / "alerts.db")
            alert = store.create_alert(
                user_id="42",
                chat_id="42",
                username="tester",
                symbol="btc",
                exchange="okx",
                market_type="spot",
                pair="BTC-USDT",
                direction="above",
                target_price=70000,
            )

            self.assertEqual(alert.exchange, "okx")
            self.assertEqual(alert.market_type, "spot")
            self.assertEqual(alert.pair, "BTC-USDT")
            self.assertEqual(alert.venue_label, "OKX 现货")
            payload = alert_to_dict(alert)
            self.assertEqual(payload["exchange_label"], "OKX")
            self.assertEqual(payload["market_type_label"], "现货")

    def test_create_price_change_alert_with_repeat_policy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceAlertStore(Path(tmp) / "alerts.db")
            alert = store.create_alert(
                user_id="42",
                chat_id="42",
                username="tester",
                symbol="btc",
                exchange="binance",
                market_type="futures",
                pair="BTCUSDT",
                direction="both",
                target_price=0,
                alert_type="price_change",
                timeframe_sec=300,
                threshold_pct=2,
                repeat_policy="interval",
                repeat_interval_sec=300,
            )

            self.assertEqual(alert.alert_type, "price_change")
            self.assertEqual(alert.direction, "both")
            self.assertEqual(alert.timeframe_label, "5分钟")
            self.assertEqual(alert.repeat_policy_label, "持续提醒，每5分钟一次")
            self.assertIn("价格", alert.condition_text)

    def test_repeat_target_alert_stays_active_after_trigger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceAlertStore(Path(tmp) / "alerts.db")
            alert = store.create_alert(
                user_id="1",
                chat_id="1",
                symbol="BTC",
                direction="above",
                target_price=60000,
                repeat_policy="repeat",
            )

            self.assertTrue(store.mark_triggered(alert, 61000))
            updated = store.get_alert(alert.id)
            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.status, "active")
            self.assertEqual(updated.trigger_count, 1)

    def test_fetch_price_alert_prices_uses_alert_price_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = PriceAlertStore(Path(tmp) / "alerts.db")
            alert = store.create_alert(
                user_id="1",
                chat_id="1",
                symbol="BTC",
                exchange="bybit",
                market_type="futures",
                pair="BTCUSDT",
                direction="above",
                target_price=60000,
            )
            settings = Settings(data_dir=Path(tmp))
            quote = AlertMarketQuote(exchange="bybit", market_type="futures", symbol="BTCUSDT", pair="BTCUSDT", price=61234.5)

            with patch("paopao_radar.price_alerts.fetch_alert_market_quote", return_value=quote):
                prices = fetch_price_alert_prices(settings, [alert])

            self.assertEqual(prices, {"bybit:futures:BTCUSDT": 61234.5})
            hits = triggered_alerts([alert], prices)
            self.assertEqual([(item.exchange, item.market_type, price) for item, price in hits], [("bybit", "futures", 61234.5)])

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
