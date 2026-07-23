from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from paopao_radar.config import Settings
from paopao_radar.funding_alert import (
    FundingAlertEngine,
    _display_width,
    classify_funding_alert,
    funding_row_text,
    funding_table,
    funding_table_lines,
)
from paopao_radar.storage import JsonStore


CST = timezone(timedelta(hours=8))


def ms_at(hour: int) -> int:
    return int(datetime(2026, 7, 1, hour, 0, 0, tzinfo=CST).timestamp() * 1000)


class FundingHttp:
    def __init__(self, bitget_rate: str = "-0.006") -> None:
        self.bitget_rate = bitget_rate

    def get_json(self, url: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        if "premiumIndex" in url:
            return {"symbol": "TESTUSDT", "lastFundingRate": "-0.0200", "nextFundingTime": ms_at(17)}
        if "fapi/v1/fundingRate" in url:
            return [
                {"fundingTime": ms_at(8), "fundingRate": "-0.001"},
                {"fundingTime": ms_at(12), "fundingRate": "-0.002"},
                {"fundingTime": ms_at(16), "fundingRate": "-0.004"},
            ]
        if "okx.com" in url and "funding-rate-history" not in url:
            return {
                "data": [{
                    "fundingRate": "-0.0100",
                    "prevFundingTime": str(ms_at(16)),
                    "fundingTime": str(ms_at(17)),
                }]
            }
        if "okx.com" in url:
            return {"data": [{"fundingTime": str(ms_at(16)), "fundingRate": "-0.004"}]}
        if "bybit.com" in url and "tickers" in url:
            return {"result": {"list": [{"fundingRate": "0.0001", "nextFundingTime": str(ms_at(17)), "fundingIntervalHour": "1"}]}}
        if "bybit.com" in url:
            return {"result": {"list": [{"fundingRateTimestamp": str(ms_at(16)), "fundingRate": "0.0001"}]}}
        if "current-fund-rate" in url:
            return {
                "code": "00000",
                "data": [{
                    "symbol": "TESTUSDT",
                    "fundingRate": self.bitget_rate,
                    "fundingRateInterval": "1",
                    "nextUpdate": str(ms_at(17)),
                }],
            }
        if "history-fund-rate" in url:
            return {"data": [{"fundingTime": str(ms_at(16)), "fundingRate": self.bitget_rate}]}
        if url.endswith("/futures/usdt/contracts"):
            return [{
                "name": "TEST_USDT",
                "funding_rate": "0.0001",
                "funding_interval": 3600,
                "funding_next_apply": int(ms_at(17) / 1000),
            }]
        if "contracts/TEST_USDT" in url:
            return {"funding_rate": "0.0001", "funding_interval": 3600, "funding_next_apply": int(ms_at(17) / 1000)}
        if "funding_rate" in url:
            return [{"t": int(ms_at(16) / 1000), "r": "0.0001"}]
        return {}


class MissingBinanceHistoryHttp(FundingHttp):
    def get_json(self, url: str, params=None, **kwargs):  # type: ignore[no-untyped-def]
        if "fapi/v1/fundingRate" in url:
            return []
        return super().get_json(url, params=params, **kwargs)


class HourlyBinanceFundingHttp(FundingHttp):
    def get_json(self, url: str, params=None, **kwargs):  # type: ignore[no-untyped-def]
        if "fapi/v1/fundingRate" in url:
            return [
                {"fundingTime": ms_at(14), "fundingRate": "-0.001"},
                {"fundingTime": ms_at(15), "fundingRate": "-0.002"},
                {"fundingTime": ms_at(16), "fundingRate": "-0.004"},
            ]
        return super().get_json(url, params=params, **kwargs)


class FundingSource:
    def __init__(self, http: FundingHttp) -> None:
        self.http = http

    @staticmethod
    def ticker_24h() -> list[dict[str, str]]:
        return [{"symbol": "TESTUSDT", "quoteVolume": "100000000", "lastPrice": "1.23"}]

    @staticmethod
    def market_caps() -> dict[str, float]:
        return {"TEST": 123_000_000}


class FundingAlertTests(unittest.TestCase):
    def test_funding_table_shows_previous_settlement_and_interval_change(self) -> None:
        settings = Settings(data_dir=Path("."))
        row = {
            "exchange": "Bitget",
            "funding_pct": -1.216,
            "interval_hours": 1,
            "current_interval_hours": 1,
            "previous_interval_hours": 4,
            "last_funding_time": "2026-07-01 16:00:00",
            "next_funding_time": "2026-07-01 17:00:00",
        }
        table = funding_table([row], settings)
        line = funding_row_text(row, settings)

        self.assertIn("上次结算", table)
        self.assertIn("本次周期", table)
        self.assertIn("07-01 16:00", table)
        self.assertIn("4H→1H", table)
        self.assertIn("07-01 17:00", table)
        self.assertIn("上次结算 2026-07-01 16:00:00", line)
        self.assertIn("周期 4H→1H", line)

    def test_funding_table_lines_keep_columns_aligned(self) -> None:
        settings = Settings(data_dir=Path("."))
        rows = [
            {
                "exchange": "Binance",
                "funding_pct": -2.0,
                "interval_hours": 1,
                "current_interval_hours": 1,
                "previous_interval_hours": 4,
                "last_funding_time": "2026-07-01 16:00:00",
                "next_funding_time": "2026-07-01 17:00:00",
            },
            {
                "exchange": "OKX",
                "funding_pct": 0.01,
                "interval_hours": 8,
                "last_funding_time": "2026-07-01 16:00:00",
                "next_funding_time": "2026-07-02 00:00:00",
            },
        ]

        _, binance_line, okx_line = funding_table_lines(rows, settings)

        self.assertEqual(
            _display_width(binance_line.split("07-01 16:00", 1)[0]),
            _display_width(okx_line.split("07-01 16:00", 1)[0]),
        )
        binance_period_prefix = (
            binance_line.split("07-01 16:00", 1)[0]
            + "07-01 16:00"
            + binance_line.split("07-01 16:00", 1)[1].split("4H→1H", 1)[0]
        )
        okx_period_prefix = (
            okx_line.split("07-01 16:00", 1)[0]
            + "07-01 16:00"
            + okx_line.split("07-01 16:00", 1)[1].split("8H", 1)[0]
        )
        self.assertEqual(
            _display_width(binance_period_prefix),
            _display_width(okx_period_prefix),
        )
        self.assertEqual(
            _display_width(binance_line.split("07-01 17:00", 1)[0]),
            _display_width(okx_line.split("07-02 00:00", 1)[0]),
        )

    def test_classifies_multi_exchange_negative_funding(self) -> None:
        settings = Settings(
            funding_alert_extreme_negative_pct=-0.5,
            funding_alert_min_exchange_count=2,
        )
        result = classify_funding_alert([
            {"exchange": "Binance", "funding_pct": -2.0},
            {"exchange": "OKX", "funding_pct": -1.0},
            {"exchange": "Bybit", "funding_pct": 0.01},
        ], settings)

        self.assertEqual(result["primary_kind"], "multi_negative")
        self.assertEqual(result["risk"], "极高")
        self.assertIn("多所极负共振", result["types"])

    def test_single_binance_source_is_not_classified_as_multi_exchange(self) -> None:
        settings = Settings(
            funding_alert_extreme_negative_pct=-0.5,
            funding_alert_extreme_positive_pct=0.5,
            funding_alert_min_exchange_count=1,
        )

        negative = classify_funding_alert([
            {"exchange": "Binance", "funding_pct": -0.982},
        ], settings)
        positive = classify_funding_alert([
            {"exchange": "Binance", "funding_pct": 0.982},
        ], settings)

        self.assertEqual(negative["primary_kind"], "extreme_negative")
        self.assertEqual(negative["types"], ["Binance 极负资金费率"])
        self.assertEqual(negative["risk"], "高")
        self.assertEqual(positive["primary_kind"], "extreme_positive")
        self.assertEqual(positive["types"], ["Binance 极正资金费率"])
        self.assertEqual(positive["risk"], "高")

    def test_build_pushes_multi_exchange_negative_alert(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=Path(tmp) / "funding_alert_state.json",
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BINANCE", "OKX", "BYBIT"),
                funding_alert_min_exchange_count=2,
                funding_alert_cooldown_sec=3600,
            )
            store = JsonStore(Path(tmp))

            result = FundingAlertEngine(settings, store).build(FundingSource(FundingHttp()))  # type: ignore[arg-type]

            self.assertEqual(result["template_id"], "TG_FUNDING_ALERT")
            self.assertEqual(len(result["alerts"]), 1)
            self.assertIn("多所极负共振", result["messages"][0])
            self.assertIn("首次发现", result["messages"][0])
            self.assertIn("市值: $123M（低市值，来源 Binance）", result["messages"][0])
            self.assertIn("<pre>", result["messages"][0])
            self.assertIn("Binance", result["messages"][0])
            self.assertIn("-2.000%/1H 超极负", result["messages"][0])
            self.assertIn("交易所偏离", result["messages"][0])
            self.assertIn("最高资金费率和最低资金费率之间的差值", result["messages"][0])

    def test_native_exchange_rows_are_confirmed_without_external_provider(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=Path(tmp) / "funding_alert_state.json",
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BINANCE", "OKX", "BYBIT"),
                funding_alert_min_exchange_count=2,
            )

            result = FundingAlertEngine(settings, JsonStore(Path(tmp))).build(  # type: ignore[arg-type]
                FundingSource(FundingHttp())
            )

        self.assertEqual(len(result["alerts"]), 1)
        self.assertEqual(result["alerts"][0]["quality_gate"], "allow")
        self.assertEqual(result["alerts"][0]["primary_data_source"], "native_exchange_apis")
        self.assertIn("数据确认: 原生交易所接口", result["messages"][0])

    def test_single_exchange_alert_uses_its_native_exchange_fact(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=Path(tmp) / "funding_alert_state.json",
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BINANCE",),
                funding_alert_min_exchange_count=1,
            )

            result = FundingAlertEngine(settings, JsonStore(Path(tmp))).build(  # type: ignore[arg-type]
                FundingSource(HourlyBinanceFundingHttp())
            )

        self.assertEqual(len(result["alerts"]), 1)
        self.assertIn("<b>警报类型</b>: Binance 极负资金费率", result["messages"][0])
        self.assertIn("数据确认: 原生交易所接口 1所（Binance）", result["messages"][0])
        self.assertIn("Binance 出现极负费率", result["messages"][0])
        self.assertNotIn("多所极负共振", result["messages"][0])
        self.assertNotIn("多家交易所同步极负", result["messages"][0])

    def test_reply_chain_uses_previous_message_id_for_same_symbol(self) -> None:
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "funding_alert_state.json"
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=state_path,
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BINANCE", "OKX", "BYBIT"),
                funding_alert_min_exchange_count=2,
            )
            store = JsonStore(Path(tmp))
            store.save(state_path, {
                "symbols": {
                    "TESTUSDT": {
                        "alert_count": 1,
                        "stage": "high_risk_active",
                        "last_message_id": 777,
                        "last_extreme_count": 1,
                        "last_risk": "高",
                        "peak_abs_funding_pct": 1.0,
                        "exchanges": {},
                    }
                },
                "last_alerts": {},
            })

            result = FundingAlertEngine(settings, store).build(FundingSource(FundingHttp()))  # type: ignore[arg-type]

            self.assertEqual(result["alerts"][0]["reply_to_message_id"], 777)
            self.assertEqual(result["alerts"][0]["alert_count"], 2)
            self.assertIn("第2次追踪", result["messages"][0])
            self.assertIn("回复上一条同币信号", result["messages"][0])

    def test_mark_pushed_stores_message_id_for_next_reply(self) -> None:
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "funding_alert_state.json"
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=state_path,
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BINANCE", "OKX", "BYBIT"),
            )
            store = JsonStore(Path(tmp))
            engine = FundingAlertEngine(settings, store)
            result = engine.build(FundingSource(FundingHttp()))  # type: ignore[arg-type]
            result["alerts"][0]["message_ids"] = [999]

            engine.mark_pushed(result["alerts"])
            state = store.load(state_path, {})

            self.assertEqual(state["symbols"]["TESTUSDT"]["last_message_id"], 999)
            self.assertEqual(state["symbols"]["TESTUSDT"]["last_message_ids"], [999])

    def test_quiet_scans_emit_heat_decay_reply(self) -> None:
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "funding_alert_state.json"
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=state_path,
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BINANCE", "OKX", "BYBIT"),
                funding_alert_extreme_negative_pct=-5.0,
                funding_alert_extreme_positive_pct=5.0,
                funding_alert_divergence_pct=99.0,
                funding_alert_decay_quiet_scans=2,
                funding_alert_end_quiet_scans=5,
            )
            store = JsonStore(Path(tmp))
            store.save(state_path, {
                "symbols": {
                    "TESTUSDT": {
                        "alert_count": 1,
                        "stage": "high_risk_active",
                        "quiet_count": 1,
                        "last_message_id": 777,
                        "exchanges": {
                            "Binance": {"interval_hours": 0, "next_funding_time_ms": ms_at(17)},
                            "OKX": {"interval_hours": 1, "next_funding_time_ms": ms_at(16)},
                            "Bybit": {"interval_hours": 1, "next_funding_time_ms": ms_at(16)},
                        },
                    }
                },
                "last_alerts": {},
            })

            result = FundingAlertEngine(settings, store).build(FundingSource(FundingHttp()))  # type: ignore[arg-type]

            self.assertEqual(len(result["alerts"]), 1)
            self.assertEqual(result["alerts"][0]["stage"], "heat_decay")
            self.assertEqual(result["alerts"][0]["reply_to_message_id"], 777)
            self.assertIn("热度衰减", result["messages"][0])
            self.assertIn("极端资金费率已经连续回落", result["messages"][0])
            binance = next(row for row in result["alerts"][0]["rows"] if row["exchange"] == "Binance")
            self.assertEqual(binance["interval_hours"], 1)
            self.assertEqual(binance["last_funding_time_ms"], ms_at(16))
            self.assertEqual(binance["next_funding_time_ms"], ms_at(17))
            self.assertNotIn("周期数据暂不可用", result["messages"][0])

    def test_failed_history_backfill_never_uses_next_time_as_last_settlement(self) -> None:
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "funding_alert_state.json"
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=state_path,
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BINANCE",),
                funding_alert_extreme_negative_pct=-5.0,
                funding_alert_extreme_positive_pct=5.0,
                funding_alert_divergence_pct=99.0,
                funding_alert_decay_quiet_scans=2,
                funding_alert_end_quiet_scans=5,
            )
            store = JsonStore(Path(tmp))
            store.save(state_path, {
                "symbols": {
                    "TESTUSDT": {
                        "alert_count": 1,
                        "stage": "high_risk_active",
                        "quiet_count": 1,
                        "last_message_id": 777,
                        "exchanges": {
                            "Binance": {
                                "interval_hours": 0,
                                "next_funding_time_ms": ms_at(17),
                                "next_funding_time": "2026-07-01 17:00:00",
                            },
                        },
                    },
                },
                "last_alerts": {},
            })

            result = FundingAlertEngine(settings, store).build(  # type: ignore[arg-type]
                FundingSource(MissingBinanceHistoryHttp())
            )

            self.assertEqual(len(result["alerts"]), 1)
            row = result["alerts"][0]["rows"][0]
            self.assertEqual(row["interval_hours"], 0)
            self.assertEqual(row["last_funding_time_ms"], 0)
            self.assertEqual(row["last_funding_time"], "")
            self.assertEqual(row["next_funding_time_ms"], ms_at(17))
            self.assertEqual(row["funding_period_status"], "unavailable")
            self.assertIn("周期数据暂不可用", result["messages"][0])
            self.assertNotIn("07-01 17:00  未知周期  07-01 17:00", result["messages"][0])

    def test_previous_state_detects_interval_shortening(self) -> None:
        with TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "funding_alert_state.json"
            settings = Settings(
                data_dir=Path(tmp),
                funding_alert_state_path=state_path,
                funding_alert_scan_limit=1,
                funding_alert_exchanges=("BITGET",),
                funding_alert_extreme_negative_pct=-5.0,
                funding_alert_extreme_positive_pct=5.0,
                funding_alert_divergence_pct=99.0,
            )
            store = JsonStore(Path(tmp))
            store.save(state_path, {
                "symbols": {
                    "TESTUSDT": {
                        "exchanges": {
                            "Bitget": {
                                "interval_hours": 4,
                                "next_funding_time_ms": ms_at(16),
                                "next_funding_time": "2026-07-01 16:00:00",
                            }
                        }
                    }
                },
                "last_alerts": {},
            })

            result = FundingAlertEngine(settings, store).build(FundingSource(FundingHttp(bitget_rate="-0.001")))  # type: ignore[arg-type]

            self.assertEqual(len(result["alerts"]), 1)
            self.assertIn("结算周期缩短", result["messages"][0])
            self.assertIn("4H结算一次", result["messages"][0])
            self.assertIn("1H结算一次", result["messages"][0])

    def test_stale_scan_gap_is_not_treated_as_funding_interval(self) -> None:
        with TemporaryDirectory() as tmp:
            settings = Settings(data_dir=Path(tmp))
            engine = FundingAlertEngine(settings, JsonStore(Path(tmp)))
            previous_next = ms_at(16)
            current_next = previous_next + 64 * 3_600_000
            state = {
                "symbols": {
                    "TESTUSDT": {
                        "exchanges": {
                            "Binance": {
                                "interval_hours": 64,
                                "next_funding_time_ms": previous_next,
                            }
                        }
                    }
                }
            }

            rows = engine._apply_state_transitions(
                "TESTUSDT",
                [{
                    "exchange": "Binance",
                    "funding_pct": 0.01,
                    "interval_hours": 8,
                    "current_interval_hours": 8,
                    "next_funding_time_ms": current_next,
                }],
                state,
            )

        self.assertNotIn("funding_interval_transition", rows[0])
        self.assertNotIn("previous_interval_hours", rows[0])


if __name__ == "__main__":
    unittest.main()
