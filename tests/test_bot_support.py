from __future__ import annotations


# Source group: test_ai_prompts.py

import json
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from paopao_radar import ai_prompts
from paopao_radar.ai_prompts import DEFAULT_ANALYST_PROMPT, DEFAULT_ASSISTANT_PROMPT, load_ai_prompts, reset_ai_prompts, save_ai_prompts
from paopao_radar.config import Settings


class AiPromptsTests(unittest.TestCase):
    def test_load_uses_defaults_when_file_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=Path(tmp) / "ai_prompts.json")

            payload = load_ai_prompts(settings)

        self.assertTrue(payload["ok"])
        self.assertFalse(payload["exists"])
        self.assertEqual(payload["prompts"]["assistant_prompt"], DEFAULT_ASSISTANT_PROMPT)
        self.assertEqual(payload["prompts"]["analyst_prompt"], DEFAULT_ANALYST_PROMPT)

    def test_default_assistant_prompt_supports_playful_style_and_expert_routing(self) -> None:
        self.assertIn("有一点皮", DEFAULT_ASSISTANT_PROMPT)
        self.assertIn("生活问题", DEFAULT_ASSISTANT_PROMPT)
        self.assertIn("专业分析师模式", DEFAULT_ASSISTANT_PROMPT)
        self.assertIn("不能用自然语言直接创建", DEFAULT_ASSISTANT_PROMPT)

    def test_save_and_reset_prompts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "ai_prompts.json"
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=prompt_path)

            saved = save_ai_prompts(
                {
                    "assistant_prompt": "普通助手",
                    "analyst_prompt": "专业分析师",
                },
                settings,
            )
            loaded = load_ai_prompts(settings)
            reset = reset_ai_prompts(settings)
            restored = load_ai_prompts(settings)

        self.assertTrue(saved["ok"])
        self.assertEqual(set(saved["changed"]), {"assistant_prompt", "analyst_prompt"})
        self.assertEqual(loaded["prompts"]["assistant_prompt"], "普通助手")
        self.assertEqual(loaded["prompts"]["analyst_prompt"], "专业分析师")
        self.assertTrue(reset["ok"])
        self.assertEqual(restored["prompts"]["assistant_prompt"], DEFAULT_ASSISTANT_PROMPT)
        self.assertEqual(restored["prompts"]["analyst_prompt"], DEFAULT_ANALYST_PROMPT)

    def test_save_rejects_empty_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=Path(tmp) / "ai_prompts.json")

            result = save_ai_prompts({"assistant_prompt": "", "analyst_prompt": "x"}, settings)

        self.assertFalse(result["ok"])
        self.assertIn("不能为空", result["error"])

    def test_save_preserves_other_value_from_legacy_json_object(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "ai_prompts.json"
            prompt_path.write_text(
                json.dumps(
                    {
                        "assistant_prompt": "旧助手提示词",
                        "analyst_prompt": "旧分析师提示词",
                        "updated_at": "legacy",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=prompt_path)

            result = save_ai_prompts({"assistant_prompt": "新助手提示词"}, settings)
            loaded = load_ai_prompts(settings)

        self.assertTrue(result["ok"])
        self.assertEqual(result["changed"], ["assistant_prompt"])
        self.assertEqual(loaded["prompts"]["assistant_prompt"], "新助手提示词")
        self.assertEqual(loaded["prompts"]["analyst_prompt"], "旧分析师提示词")

    def test_concurrent_partial_prompt_updates_do_not_lose_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            prompt_path = Path(tmp) / "ai_prompts.json"
            settings = Settings(data_dir=Path(tmp), ai_prompts_path=prompt_path)
            save_ai_prompts(
                {"assistant_prompt": "初始助手", "analyst_prompt": "初始分析师"},
                settings,
            )
            barrier = threading.Barrier(2)
            real_update = ai_prompts.locked_update_json

            def synchronized_update(*args, **kwargs):
                barrier.wait(timeout=5)
                return real_update(*args, **kwargs)

            with patch.object(ai_prompts, "locked_update_json", side_effect=synchronized_update) as update_mock:
                with ThreadPoolExecutor(max_workers=2) as executor:
                    futures = [
                        executor.submit(save_ai_prompts, {"assistant_prompt": "并发助手"}, settings),
                        executor.submit(save_ai_prompts, {"analyst_prompt": "并发分析师"}, settings),
                    ]
                    results = [future.result(timeout=10) for future in futures]

            payload = json.loads(prompt_path.read_text(encoding="utf-8"))

        self.assertTrue(all(result["ok"] for result in results))
        self.assertEqual(update_mock.call_count, 2)
        self.assertEqual(payload["assistant_prompt"], "并发助手")
        self.assertEqual(payload["analyst_prompt"], "并发分析师")


if __name__ == "__main__":
    unittest.main()


# Source group: test_price_alerts.py

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paopao_radar.price_alerts import (
    AlertMarketQuote,
    PriceAlertStore,
    alert_market_pair_candidates,
    alert_to_dict,
    clear_alert_market_cache,
    contract_pair_multiplier,
    discover_alert_markets,
    fetch_alert_market_quote,
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

    def test_futures_pair_candidates_include_1000_prefix_for_binance_bybit(self) -> None:
        self.assertEqual(
            alert_market_pair_candidates("PEPE", exchange="binance", market_type="futures"),
            ["PEPEUSDT", "1000PEPEUSDT", "10000PEPEUSDT", "1000000PEPEUSDT"],
        )
        self.assertEqual(
            alert_market_pair_candidates("MOG", exchange="bybit", market_type="futures"),
            ["MOGUSDT", "1000MOGUSDT", "10000MOGUSDT", "1000000MOGUSDT"],
        )
        self.assertEqual(
            alert_market_pair_candidates("PEPE", exchange="okx", market_type="futures"),
            ["PEPE-USDT-SWAP"],
        )
        self.assertEqual(
            alert_market_pair_candidates("PEPE", exchange="binance", market_type="spot"),
            ["PEPEUSDT"],
        )

    def test_contract_pair_multiplier_detects_prefixed_futures_units(self) -> None:
        self.assertEqual(contract_pair_multiplier("1000PEPEUSDT", "PEPEUSDT"), 1000)
        self.assertEqual(contract_pair_multiplier("1000000MOGUSDT", "MOGUSDT"), 1_000_000)
        self.assertEqual(contract_pair_multiplier("MOG_USDT", "MOGUSDT"), 1)
        self.assertEqual(contract_pair_multiplier("PEPE-USDT-SWAP", "PEPEUSDT"), 1)

    def test_fetch_alert_market_quote_falls_back_to_1000_futures_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            requested_pairs: list[str] = []

            def fake_fetch(settings: Settings, exchange: str, market_type: str, pair: str, timeout: int) -> float | None:
                requested_pairs.append(pair)
                return 0.00256 if pair == "1000PEPEUSDT" else None

            with patch("paopao_radar.price_alerts._fetch_alert_market_price", side_effect=fake_fetch):
                quote = fetch_alert_market_quote(settings, "PEPE", exchange="binance", market_type="futures")

            self.assertEqual(requested_pairs, ["PEPEUSDT", "1000PEPEUSDT"])
            self.assertIsNotNone(quote)
            assert quote is not None
            self.assertEqual(quote.pair, "1000PEPEUSDT")
            self.assertEqual(quote.symbol, "PEPEUSDT")

    def test_fetch_alert_market_quote_falls_back_to_1000000_futures_pair(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            requested_pairs: list[str] = []

            def fake_fetch(settings: Settings, exchange: str, market_type: str, pair: str, timeout: int) -> float | None:
                requested_pairs.append(pair)
                return 0.00000012 if pair == "1000000MOGUSDT" else None

            with patch("paopao_radar.price_alerts._fetch_alert_market_price", side_effect=fake_fetch):
                quote = fetch_alert_market_quote(settings, "MOG", exchange="bybit", market_type="futures")

            self.assertEqual(requested_pairs, ["MOGUSDT", "1000MOGUSDT", "10000MOGUSDT", "1000000MOGUSDT"])
            self.assertIsNotNone(quote)
            assert quote is not None
            self.assertEqual(quote.pair, "1000000MOGUSDT")
            self.assertEqual(quote.symbol, "MOGUSDT")

    def test_fetch_alert_market_quote_caches_exact_quote_briefly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            clear_alert_market_cache()
            calls: list[str] = []

            def fake_fetch(settings: Settings, exchange: str, market_type: str, pair: str, timeout: int) -> float | None:
                calls.append(pair)
                return 61234.5

            try:
                with patch("paopao_radar.price_alerts._fetch_alert_market_price", side_effect=fake_fetch):
                    first = fetch_alert_market_quote(settings, "BTC", exchange="binance", market_type="futures", pair="BTCUSDT", cache_ttl_sec=30)
                    second = fetch_alert_market_quote(settings, "BTC", exchange="binance", market_type="futures", pair="BTCUSDT", cache_ttl_sec=30)
            finally:
                clear_alert_market_cache()

            self.assertIsNotNone(first)
            self.assertIsNotNone(second)
            assert first is not None and second is not None
            self.assertEqual(first.price, 61234.5)
            self.assertEqual(second.price, 61234.5)
            self.assertEqual(calls, ["BTCUSDT"])

    def test_discover_alert_markets_fetches_concurrently_and_caches(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            clear_alert_market_cache()
            calls: list[tuple[str, str]] = []

            def fake_fetch(settings: Settings, symbol: str, exchange: str, market_type: str, pair: str | None = None) -> AlertMarketQuote | None:
                time.sleep(0.05)
                calls.append((exchange, market_type))
                if exchange == "binance" and market_type == "futures":
                    return AlertMarketQuote(exchange=exchange, market_type=market_type, symbol=symbol, pair=symbol, price=61234.5)
                return None

            with patch("paopao_radar.price_alerts.fetch_alert_market_quote", side_effect=fake_fetch):
                started = time.time()
                first = discover_alert_markets(settings, "BTC", cache_ttl_sec=60)
                elapsed = time.time() - started
                second = discover_alert_markets(settings, "BTC", cache_ttl_sec=60)

            self.assertEqual(len(calls), 10)
            self.assertLess(elapsed, 0.45)
            self.assertEqual([(quote.exchange, quote.market_type) for quote in first], [("binance", "futures")])
            self.assertEqual(second, first)
            clear_alert_market_cache()

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


# Source group: test_market_links.py

import unittest

from paopao_radar.market_links import (
    binance_usdt_symbol,
    coinglass_tv_url,
    telegram_coin_links,
    tradingview_tv_url,
)


class MarketLinksTests(unittest.TestCase):
    def test_normalizes_base_coin_and_pair(self) -> None:
        self.assertEqual(binance_usdt_symbol(" btc "), "BTCUSDT")
        self.assertEqual(binance_usdt_symbol("btcusdt"), "BTCUSDT")

    def test_keeps_coinglass_link(self) -> None:
        self.assertEqual(
            coinglass_tv_url("BTC"),
            "https://www.coinglass.com/tv/zh/Binance_BTCUSDT",
        )

    def test_builds_direct_tradingview_link(self) -> None:
        self.assertEqual(
            tradingview_tv_url("BTCUSDT"),
            "https://www.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P",
        )

    def test_telegram_links_include_copyable_pair_and_both_charts(self) -> None:
        links = telegram_coin_links("BTC")

        self.assertIn('href="https://www.coinglass.com/tv/zh/Binance_BTCUSDT"', links)
        self.assertIn("<b>BTC</b>", links)
        self.assertIn("📋 <code>BTCUSDT</code>", links)
        self.assertIn('href="https://www.tradingview.com/chart/?symbol=BINANCE%3ABTCUSDT.P"', links)
        self.assertIn("<b>TV</b>", links)


if __name__ == "__main__":
    unittest.main()
