from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.storage import JsonStore
from paopao_radar.symbol_dossier import (
    append_signal_events_from_push,
    build_symbol_dossier,
    extract_symbol_from_query,
    extract_symbols_from_text,
    format_symbol_dossier_report,
    is_symbol_dossier_request,
)


class FakeDossierSource:
    def ticker_24h(self):  # type: ignore[no-untyped-def]
        return [{
            "symbol": "TESTUSDT",
            "lastPrice": "116",
            "priceChangePercent": "12.5",
            "quoteVolume": "65000000",
        }]

    def premium_index(self):  # type: ignore[no-untyped-def]
        return [{
            "symbol": "TESTUSDT",
            "lastFundingRate": "-0.008",
            "nextFundingTime": "1783008000000",
        }]

    def klines(self, symbol: str, interval: str = "15m", limit: int = 64, **_kwargs):  # type: ignore[no-untyped-def]
        rows = []
        base = 100.0
        for idx in range(limit):
            close = base + idx * 0.25
            if idx == limit - 1:
                close = 116.0
            high = close * 1.01
            low = close * 0.99
            rows.append([
                idx * 900000,
                str(close * 0.995),
                str(high),
                str(low),
                str(close),
                "1000",
                idx * 900000 + 899999,
                str(1_000_000 + idx * 10_000),
                100,
                "600",
                "600000",
            ])
        return rows

    def open_interest_hist(self, symbol: str, period: str = "15m", limit: int = 17, **_kwargs):  # type: ignore[no-untyped-def]
        return [
            {"sumOpenInterestValue": str(1_000_000 + idx * 40_000)}
            for idx in range(limit)
        ]

    def market_caps(self) -> dict[str, float]:
        return {"TEST": 123_000_000}

    def coinpaprika_market_caps(self) -> dict[str, float]:
        return {}

    def diagnostics(self) -> dict[str, object]:
        return {"quality": {"warnings": []}}


class SymbolDossierTests(unittest.TestCase):
    def test_extracts_symbol_from_signal_and_query(self) -> None:
        text = "🚀 启动雷达 [GWEI](https://www.coinglass.com/tv/zh/Binance_GWEIUSDT)"

        self.assertEqual(extract_symbols_from_text(text), ["GWEIUSDT"])
        self.assertEqual(extract_symbol_from_query("GWEI 怎么看"), "GWEIUSDT")
        self.assertTrue(is_symbol_dossier_request("SOL 可以做多吗"))

    def test_append_signal_events_from_push_writes_symbol_index(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
            )
            store = JsonStore(Path(tmp))

            count = append_signal_events_from_push(
                settings,
                store,
                template_id="TG_LAUNCH_ALERT",
                dedup_key="launch:TEST",
                status="sent",
                sent=True,
                text="🚀 启动雷达 [TEST](https://www.coinglass.com/tv/zh/Binance_TESTUSDT)\n分数: 90",
                ts=int(time.time()),
                message_ids=[321],
            )
            events = store.load(settings.signal_events_path, [])

        self.assertEqual(count, 1)
        self.assertEqual(events[0]["symbol"], "TESTUSDT")
        self.assertEqual(events[0]["signal_type"], "启动雷达")
        self.assertEqual(events[0]["message_ids"], [321])

    def test_build_symbol_dossier_combines_history_snapshot_and_verdict(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                signal_events_path=Path(tmp) / "signal_events.json",
                launch_state_path=Path(tmp) / "launch_state.json",
                launch_watch_history_path=Path(tmp) / "launch_watch_history.json",
                structure_review_path=Path(tmp) / "structure_review.json",
                structure_history_path=Path(tmp) / "structure_history.json",
                funding_alert_state_path=Path(tmp) / "funding_alert_state.json",
            )
            store = JsonStore(Path(tmp))
            store.save(settings.signal_events_path, [{
                "source": "telegram_push",
                "ts": 1000,
                "symbol": "TESTUSDT",
                "signal_type": "启动雷达",
                "template_id": "TG_LAUNCH_ALERT",
                "excerpt": "启动雷达 TEST 分数 90",
            }])
            store.save(settings.structure_review_path, [{
                "symbol": "TESTUSDT",
                "signal_ts": 1100,
                "signal_type": "BREAKOUT_CONFIRMED",
                "level": "A",
                "score": 82,
                "outcome": "valid_breakout",
                "status": "completed",
                "metrics": {"price_change_1h": 4.2},
            }])

            dossier = build_symbol_dossier(settings, "TEST 怎么看", store=store, source=FakeDossierSource())  # type: ignore[arg-type]
            report = format_symbol_dossier_report(dossier)

        self.assertEqual(dossier["symbol"], "TESTUSDT")
        self.assertGreaterEqual(len(dossier["history"]), 2)
        self.assertEqual(dossier["snapshot"]["market_cap_tier"], "低市值")
        self.assertIn(dossier["verdict"]["stance"], {"偏多", "高风险观望", "观望"})
        self.assertIn("TESTUSDT 币种雷达档案", report)
        self.assertIn("历史雷达信号", report)
        self.assertIn("本地规则结论", report)


if __name__ == "__main__":
    unittest.main()
