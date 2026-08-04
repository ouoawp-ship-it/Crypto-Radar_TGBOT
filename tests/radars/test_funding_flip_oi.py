from __future__ import annotations

import unittest
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from config import Settings
from radars.funding_alert.radar import FundingAlertEngine
from radars.funding_alert.flip_oi import (
    HOUR_MS,
    FundingFlipOITracker,
    analyze_oi_segment_growth,
)
from shared.storage import JsonStore
from shared.telegram import TelegramGateway


NOW_TS = 1_800_000_000


def oi_rows(
    segment_values: tuple[float, float, float, float] = (
        100_000_000,
        102_000_000,
        105_000_000,
        109_000_000,
    ),
    *,
    now_ts: int = NOW_TS,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    start_ms = now_ts * 1000 - 48 * HOUR_MS
    for index in range(48):
        result.append({
            "timestamp": str(start_ms + index * HOUR_MS),
            "sumOpenInterestValue": str(segment_values[index // 12]),
        })
    return result


class _OISource:
    def __init__(self, rows: list[dict[str, str]] | None = None, fail: bool = False):
        self.rows = rows or []
        self.fail = fail
        self.calls = 0

    def open_interest_hist(self, *_args: object, **_kwargs: object) -> list[dict[str, str]]:
        self.calls += 1
        if self.fail:
            raise TimeoutError("degraded")
        return self.rows


class _EngineHttp:
    def __init__(self, rate: str = "0.0001"):
        self.rate = rate

    def get_json(self, url: str, params=None, **_kwargs):  # type: ignore[no-untyped-def]
        if "premiumIndex" in url:
            return {
                "symbol": "TESTUSDT",
                "lastFundingRate": self.rate,
                "nextFundingTime": (NOW_TS + 3600) * 1000,
            }
        if "fundingRate" in url:
            return []
        return {}


class _EngineSource:
    def __init__(self, http: _EngineHttp, *, now_ts: int):
        self.http = http
        self.now_ts = now_ts

    @staticmethod
    def ticker_24h() -> list[dict[str, str]]:
        return [{
            "symbol": "TESTUSDT",
            "quoteVolume": "100000000",
            "lastPrice": "1.0",
            "priceChangePercent": "0.0",
        }]

    @staticmethod
    def market_caps() -> dict[str, float]:
        return {"TEST": 100_000_000}

    def open_interest_hist(
        self,
        *_args: object,
        **_kwargs: object,
    ) -> list[dict[str, str]]:
        return oi_rows(now_ts=self.now_ts)


def funding_rows(rate: float) -> dict[str, list[dict[str, object]]]:
    return {
        "TESTUSDT": [{
            "exchange": "Binance",
            "funding_pct": rate,
        }],
    }


class FundingFlipOIAnalysisTests(unittest.TestCase):
    def test_four_segments_require_total_growth_and_increasing_shape(self) -> None:
        result = analyze_oi_segment_growth(oi_rows(), now_ms=NOW_TS * 1000)
        self.assertTrue(result["eligible"])
        self.assertGreaterEqual(result["total_growth_pct"], 8)
        self.assertEqual(len(result["segment_averages_usd"]), 4)

        low_growth = analyze_oi_segment_growth(
            oi_rows((100, 101, 102, 107)),
            now_ms=NOW_TS * 1000,
        )
        self.assertEqual(low_growth["reason"], "growth_below_threshold")

        non_increasing = analyze_oi_segment_growth(
            oi_rows((100, 110, 103, 109)),
            now_ms=NOW_TS * 1000,
        )
        self.assertEqual(non_increasing["reason"], "segments_not_increasing")

    def test_rejects_insufficient_missing_and_stale_data(self) -> None:
        short = analyze_oi_segment_growth(
            oi_rows()[-10:],
            now_ms=NOW_TS * 1000,
        )
        self.assertEqual(short["reason"], "insufficient_coverage")

        gapped = oi_rows()
        del gapped[20:27]
        gap_result = analyze_oi_segment_growth(gapped, now_ms=NOW_TS * 1000)
        self.assertIn(gap_result["reason"], {"insufficient_coverage", "missing_intervals"})

        stale = analyze_oi_segment_growth(
            oi_rows(now_ts=NOW_TS - 20_000),
            now_ms=NOW_TS * 1000,
        )
        self.assertEqual(stale["reason"], "stale_data")


class FundingFlipOITrackerTests(unittest.TestCase):
    def _settings(self, root: Path) -> Settings:
        return Settings(
            data_dir=root,
            funding_flip_oi_enable=True,
            funding_flip_oi_state_path=root / "funding_flip_oi_state.json",
            funding_flip_oi_cooldown_sec=3600,
        )

    def test_disabled_mode_has_zero_side_effects(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = Settings(
                data_dir=root,
                funding_flip_oi_enable=False,
                funding_flip_oi_state_path=root / "state.json",
            )
            source = _OISource(oi_rows())
            result = FundingFlipOITracker(settings, JsonStore(root)).evaluate(
                [{"symbol": "TESTUSDT"}],
                funding_rows(-0.1),
                source,
                now_ts=NOW_TS,
            )
            self.assertEqual(result["diagnostics"]["status"], "disabled")
            self.assertEqual(source.calls, 0)
            self.assertFalse(settings.funding_flip_oi_state_path.exists())

    def test_first_snapshot_then_positive_to_negative_alert(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            store = JsonStore(root)
            tracker = FundingFlipOITracker(settings, store)
            source = _OISource(oi_rows(now_ts=NOW_TS + 180))
            first = tracker.evaluate(
                [{"symbol": "TESTUSDT"}],
                funding_rows(0.01),
                source,
                now_ts=NOW_TS,
            )
            second = tracker.evaluate(
                [{"symbol": "TESTUSDT"}],
                funding_rows(-0.02),
                source,
                now_ts=NOW_TS + 180,
            )
            self.assertEqual(first["diagnostics"]["status"], "first_snapshot")
            self.assertEqual(first["alerts"], [])
            self.assertEqual(len(second["alerts"]), 1)
            self.assertIn("费率正转负＋OI连续增长", second["messages"][0])

    def test_negative_to_negative_does_not_trigger(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracker = FundingFlipOITracker(self._settings(root), JsonStore(root))
            source = _OISource(oi_rows(now_ts=NOW_TS + 180))
            tracker.evaluate(
                [{"symbol": "TESTUSDT"}],
                funding_rows(-0.01),
                source,
                now_ts=NOW_TS,
            )
            result = tracker.evaluate(
                [{"symbol": "TESTUSDT"}],
                funding_rows(-0.02),
                source,
                now_ts=NOW_TS + 180,
            )
            self.assertEqual(result["alerts"], [])
            self.assertEqual(source.calls, 0)

    def test_cooldown_and_restart_recovery(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            store = JsonStore(root)
            source = _OISource(oi_rows(now_ts=NOW_TS + 180))
            tracker = FundingFlipOITracker(settings, store)
            tracker.evaluate([{"symbol": "TESTUSDT"}], funding_rows(0.01), source, now_ts=NOW_TS)
            first = tracker.evaluate(
                [{"symbol": "TESTUSDT"}],
                funding_rows(-0.01),
                source,
                now_ts=NOW_TS + 180,
            )
            tracker.mark_pushed(first["alerts"], now_ts=NOW_TS + 181)
            restarted = FundingFlipOITracker(settings, JsonStore(root))
            restarted.evaluate(
                [{"symbol": "TESTUSDT"}],
                funding_rows(0.01),
                source,
                now_ts=NOW_TS + 360,
            )
            suppressed = restarted.evaluate(
                [{"symbol": "TESTUSDT"}],
                funding_rows(-0.01),
                source,
                now_ts=NOW_TS + 540,
            )
            self.assertEqual(len(first["alerts"]), 1)
            self.assertEqual(suppressed["alerts"], [])
            self.assertEqual(suppressed["diagnostics"]["cooldown_suppressed"], 1)

    def test_external_oi_failure_degrades_without_alert(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            tracker = FundingFlipOITracker(self._settings(root), JsonStore(root))
            source = _OISource(fail=True)
            tracker.evaluate([{"symbol": "TESTUSDT"}], funding_rows(0.01), source, now_ts=NOW_TS)
            result = tracker.evaluate(
                [{"symbol": "TESTUSDT"}],
                funding_rows(-0.01),
                source,
                now_ts=NOW_TS + 180,
            )
            self.assertEqual(result["alerts"], [])
            self.assertEqual(result["diagnostics"]["degraded"], 1)


class FundingFlipOIDeliveryIntegrationTests(unittest.TestCase):
    def _settings(self, root: Path, *, hourly_limit: int = 20) -> Settings:
        return Settings(
            data_dir=root,
            funding_alert_state_path=root / "funding_alert_state.json",
            funding_alert_scan_limit=1,
            funding_alert_exchanges=("BINANCE",),
            funding_alert_min_exchange_count=1,
            funding_flip_oi_enable=True,
            funding_flip_oi_state_path=root / "funding_flip_oi_state.json",
            funding_flip_oi_cooldown_sec=3600,
            tg_bot_token="test-token",
            tg_chat_id="-1000000000000",
            tg_funding_alert_topic_id="12",
            tg_push_history_path=root / "tg_push_history.json",
            tg_outbox_path=root / "tg_outbox.json",
            signal_events_db_path=root / "signals.db",
            tg_global_hourly_limit=hourly_limit,
        )

    def _build_pending(
        self,
        settings: Settings,
        store: JsonStore,
    ) -> tuple[FundingAlertEngine, _EngineSource, dict[str, object]]:
        http = _EngineHttp("0.0001")
        source = _EngineSource(http, now_ts=NOW_TS)
        engine = FundingAlertEngine(settings, store)
        with patch("radars.funding_alert.radar.time.time", return_value=NOW_TS):
            first = engine.build(source)  # type: ignore[arg-type]
        self.assertEqual(first["alerts"], [])

        http.rate = "-0.0001"
        source.now_ts = NOW_TS + 180
        with patch(
            "radars.funding_alert.radar.time.time",
            return_value=NOW_TS + 180,
        ):
            second = engine.build(source)  # type: ignore[arg-type]
        flip_alerts = [
            alert
            for alert in second["alerts"]
            if alert.get("event_family") == "funding_flip_oi"
        ]
        self.assertEqual(len(flip_alerts), 1)
        state = store.load(settings.funding_flip_oi_state_path, {})
        record = state["symbols"]["TESTUSDT"]
        self.assertIn("pending_event", record)
        self.assertNotIn("last_alert_at", record)
        self.assertNotIn("last_event_id", record)
        return engine, source, flip_alerts[0]

    def _assert_retry_after_non_sent(
        self,
        *,
        mode: str,
    ) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(
                root,
                hourly_limit=0 if mode == "skipped" else 20,
            )
            store = JsonStore(root)
            engine, source, alert = self._build_pending(settings, store)
            gateway = TelegramGateway(settings, store)
            send = mode != "dry_run"
            confirm = mode not in {"dry_run", "blocked"}
            if mode == "failed":
                with patch.object(
                    gateway,
                    "_send_real_message_ids",
                    return_value=(False, []),
                ):
                    push = gateway.send(
                        str(alert["text"]),
                        "TG_FUNDING_ALERT",
                        str(alert["dedup_key"]),
                        send=send,
                        confirm_real_send=confirm,
                        cooldown_sec=3600,
                        parse_mode="HTML",
                        signal_records=[alert],
                    )
            else:
                with (
                    patch("builtins.print")
                    if mode == "dry_run"
                    else nullcontext()
                ):
                    push = gateway.send(
                        str(alert["text"]),
                        "TG_FUNDING_ALERT",
                        str(alert["dedup_key"]),
                        send=send,
                        confirm_real_send=confirm,
                        cooldown_sec=3600,
                        parse_mode="HTML",
                        signal_records=[alert],
                    )
            self.assertEqual(push.status, mode)

            restarted = FundingAlertEngine(settings, JsonStore(root))
            source.now_ts = NOW_TS + 360
            with patch(
                "radars.funding_alert.radar.time.time",
                return_value=NOW_TS + 360,
            ):
                retry = restarted.build(source)  # type: ignore[arg-type]
            retry_alerts = [
                item
                for item in retry["alerts"]
                if item.get("event_family") == "funding_flip_oi"
            ]
            self.assertEqual(len(retry_alerts), 1)
            self.assertEqual(retry_alerts[0]["dedup_key"], alert["dedup_key"])
            state = store.load(settings.funding_flip_oi_state_path, {})
            self.assertIn("pending_event", state["symbols"]["TESTUSDT"])
            self.assertNotIn("last_event_id", state["symbols"]["TESTUSDT"])
            self.assertIsInstance(engine, FundingAlertEngine)

    def test_failed_blocked_skipped_and_dry_run_remain_retryable_after_restart(self) -> None:
        for mode in ("failed", "blocked", "skipped", "dry_run"):
            with self.subTest(mode=mode):
                self._assert_retry_after_non_sent(mode=mode)

    def test_sent_delivery_commits_only_in_mark_pushed(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            settings = self._settings(root)
            store = JsonStore(root)
            engine, source, alert = self._build_pending(settings, store)
            gateway = TelegramGateway(settings, store)
            with patch.object(
                gateway,
                "_send_real_message_ids",
                return_value=(True, [901]),
            ):
                push = gateway.send(
                    str(alert["text"]),
                    "TG_FUNDING_ALERT",
                    str(alert["dedup_key"]),
                    send=True,
                    confirm_real_send=True,
                    cooldown_sec=3600,
                    parse_mode="HTML",
                    signal_records=[alert],
                )
            self.assertEqual(push.status, "sent")
            state_before = store.load(settings.funding_flip_oi_state_path, {})
            self.assertIn(
                "pending_event",
                state_before["symbols"]["TESTUSDT"],
            )

            alert["message_ids"] = push.message_ids or []
            with patch(
                "radars.funding_alert.radar.time.time",
                return_value=NOW_TS + 181,
            ):
                engine.mark_pushed([alert])
            committed = store.load(settings.funding_flip_oi_state_path, {})
            record = committed["symbols"]["TESTUSDT"]
            self.assertNotIn("pending_event", record)
            self.assertEqual(
                record["last_event_id"],
                alert["event_snapshot"]["event_id"],
            )
            self.assertEqual(record["last_alert_at"], NOW_TS + 181)

            restarted = FundingAlertEngine(settings, JsonStore(root))
            source.now_ts = NOW_TS + 360
            with patch(
                "radars.funding_alert.radar.time.time",
                return_value=NOW_TS + 360,
            ):
                no_retry = restarted.build(source)  # type: ignore[arg-type]
            self.assertFalse(any(
                item.get("event_family") == "funding_flip_oi"
                for item in no_retry["alerts"]
            ))
