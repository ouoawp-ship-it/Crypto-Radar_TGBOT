from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from radars.market_summary.diagnostics import (
    HISTORY_LIMIT,
    REASON_TEXT,
    build_evaluation_result,
    build_scan_summary,
    persist_scan_summary,
    summary_log_line,
)
from radars.market_summary.quality import DAY_MS, analyze_accumulation_quality
from config import Settings
from runtime.radar_engine import RadarEngine
from shared.storage import JsonStore
from shared.time_windows import closed_window


NOW_MS = 1_800_000_000_000


def daily_rows(
    *,
    days: int = 52,
    price_fn=lambda _index: 100.0,
    volume_fn=lambda index: 30_000_000 if index >= 45 else 10_000_000,
) -> list[list[object]]:
    start = NOW_MS - days * DAY_MS
    rows: list[list[object]] = []
    for index in range(days):
        price = float(price_fn(index))
        open_time = start + index * DAY_MS
        rows.append([
            open_time,
            str(price),
            str(price * 1.05),
            str(price * 0.95),
            str(price),
            "1",
            open_time + DAY_MS - 1,
            str(volume_fn(index)),
        ])
    return rows


def diagnostic(
    symbol: str,
    quality: dict[str, object],
    *,
    row_count: int,
    dark_flow: bool = False,
    evaluation_error: bool = False,
) -> dict[str, object]:
    return build_evaluation_result(
        symbol,
        quality,
        input_row_count=row_count,
        dark_flow_candidate=dark_flow,
        evaluated_at=1_700_000_000,
        evaluation_error=evaluation_error,
    )


class AccumulationReasonCodeTests(unittest.TestCase):
    def test_all_existing_decisions_have_stable_reason_codes(self) -> None:
        cases = [
            (
                daily_rows(),
                {},
                "passed",
            ),
            (
                daily_rows(days=51),
                {},
                "insufficient_history",
            ),
            (
                [[1, 2] for _ in range(52)],
                {},
                "invalid_or_missing_candles",
            ),
            (
                daily_rows(price_fn=lambda index: 200.0 if index == 20 else 100.0),
                {},
                "range_exceeded",
            ),
            (
                daily_rows(price_fn=lambda index: 100.0 + index),
                {},
                "slope_exceeded",
            ),
            (
                daily_rows(volume_fn=lambda _index: 25_000_000),
                {},
                "baseline_volume_exceeded",
            ),
            (
                daily_rows(price_fn=lambda index: 410.0 if index >= 45 else 100.0),
                {},
                "recent_price_gain_exceeded",
            ),
        ]
        for rows, kwargs, expected in cases:
            with self.subTest(expected=expected):
                quality = analyze_accumulation_quality(
                    rows,
                    now_ms=NOW_MS,
                    **kwargs,
                )
                result = diagnostic("TESTUSDT", quality, row_count=len(rows))
                self.assertEqual(result["reason_code"], expected)
                self.assertEqual(result["reason_text"], REASON_TEXT[expected])
                self.assertEqual(result["eligible"], quality["eligible"])

    def test_invalid_metrics_and_evaluation_error_are_explicit(self) -> None:
        invalid = diagnostic(
            "BADUSDT",
            {
                "eligible": False,
                "exclusion_reason": "",
                "history_days": 52,
                "required_history_days": 52,
                "observed_at": 123,
            },
            row_count=52,
        )
        failed = diagnostic(
            "ERRUSDT",
            {},
            row_count=52,
            evaluation_error=True,
        )

        self.assertEqual(invalid["reason_code"], "invalid_metrics")
        self.assertIsNone(invalid["range_pct"])
        self.assertEqual(failed["reason_code"], "evaluation_error")
        self.assertFalse(failed["eligible"])
        self.assertEqual(
            set(REASON_TEXT),
            {
                "passed",
                "insufficient_history",
                "invalid_or_missing_candles",
                "range_exceeded",
                "slope_exceeded",
                "baseline_volume_exceeded",
                "recent_price_gain_exceeded",
                "invalid_metrics",
                "evaluation_error",
            },
        )

    def test_51_rejected_and_52_allowed_without_eligible_drift(self) -> None:
        rows_51 = daily_rows(days=51)
        rows_52 = daily_rows(days=52)
        quality_51 = analyze_accumulation_quality(rows_51, now_ms=NOW_MS)
        quality_52 = analyze_accumulation_quality(rows_52, now_ms=NOW_MS)

        result_51 = diagnostic("AUSDT", quality_51, row_count=51)
        result_52 = diagnostic("BUSDT", quality_52, row_count=52)

        self.assertFalse(quality_51["eligible"])
        self.assertFalse(result_51["eligible"])
        self.assertEqual(result_51["required_history_days"], 52)
        self.assertTrue(quality_52["eligible"])
        self.assertTrue(result_52["eligible"])
        self.assertEqual(result_52["sideways_days"], 45)


class AccumulationScanSummaryTests(unittest.TestCase):
    def test_counts_only_evaluated_items_and_tracks_dark_flow(self) -> None:
        passed_quality = analyze_accumulation_quality(
            daily_rows(),
            now_ms=NOW_MS,
        )
        rejected_quality = analyze_accumulation_quality(
            daily_rows(days=51),
            now_ms=NOW_MS,
        )
        items = [
            {
                "symbol": "PASSUSDT",
                "accumulation_quality_diagnostic": diagnostic(
                    "PASSUSDT",
                    passed_quality,
                    row_count=52,
                    dark_flow=True,
                ),
            },
            {
                "symbol": "FAILUSDT",
                "accumulation_quality_diagnostic": diagnostic(
                    "FAILUSDT",
                    rejected_quality,
                    row_count=51,
                    dark_flow=True,
                ),
            },
            {"symbol": "NOT_EVALUATED_USDT"},
        ]

        summary = build_scan_summary(
            items,
            scan_id="scan-1",
            scan_started_at=100,
            scan_completed_at=105,
            duration_sec=5.25,
            feature_enabled=True,
        )

        self.assertEqual(summary["scanned_market_count"], 3)
        self.assertEqual(summary["evaluated_count"], 2)
        self.assertEqual(summary["passed_count"], 1)
        self.assertEqual(summary["rejected_count"], 1)
        self.assertEqual(
            summary["evaluated_count"],
            summary["passed_count"] + summary["rejected_count"],
        )
        self.assertEqual(summary["reason_counts"], {"insufficient_history": 1})
        self.assertEqual(
            sum(summary["reason_counts"].values()),
            summary["rejected_count"],
        )
        self.assertEqual(len(summary["results"]), summary["evaluated_count"])
        self.assertEqual(summary["dark_flow_evaluated_count"], 2)
        self.assertEqual(summary["dark_flow_passed_count"], 1)
        self.assertEqual(summary["dark_flow_rejected_count"], 1)
        self.assertEqual(
            summary_log_line(summary),
            (
                "accumulation_quality_summary evaluated=2 passed=1 rejected=1 "
                'reasons={"insufficient_history":1}'
            ),
        )

    def test_each_evaluated_symbol_has_exactly_one_result(self) -> None:
        quality = analyze_accumulation_quality(daily_rows(), now_ms=NOW_MS)
        items = [
            {
                "symbol": f"T{index}USDT",
                "accumulation_quality_diagnostic": diagnostic(
                    f"T{index}USDT",
                    quality,
                    row_count=52,
                ),
            }
            for index in range(4)
        ]
        summary = build_scan_summary(
            items,
            scan_id="scan-unique",
            scan_started_at=1,
            scan_completed_at=2,
            duration_sec=1,
            feature_enabled=True,
        )

        symbols = [item["symbol"] for item in summary["results"]]
        self.assertEqual(len(symbols), summary["evaluated_count"])
        self.assertEqual(len(symbols), len(set(symbols)))
        self.assertNotIn("passed", summary["reason_counts"])


class AccumulationDiagnosticPersistenceTests(unittest.TestCase):
    @staticmethod
    def summary(scan_id: str) -> dict[str, object]:
        return build_scan_summary(
            [],
            scan_id=scan_id,
            scan_started_at=1,
            scan_completed_at=2,
            duration_sec=1,
            feature_enabled=True,
        )

    def test_atomic_history_is_bounded_and_recovers_after_restart(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "accumulation_quality_diagnostics.json"
            store = JsonStore(root)
            for index in range(HISTORY_LIMIT + 2):
                persist_scan_summary(store, path, self.summary(f"scan-{index}"))

            payload = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["scans"]), HISTORY_LIMIT)
            self.assertEqual(payload["scans"][0]["scan_id"], "scan-2")
            self.assertFalse(list(root.glob("*.tmp.*")))

            restarted_store = JsonStore(root)
            persist_scan_summary(
                restarted_store,
                path,
                self.summary("scan-after-restart"),
            )
            recovered = restarted_store.load(path, {})
            self.assertEqual(recovered["scans"][-1]["scan_id"], "scan-after-restart")
            self.assertEqual(len(recovered["scans"]), HISTORY_LIMIT)

    def test_duplicate_scan_replaces_and_corrupt_file_degrades_safely(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "accumulation_quality_diagnostics.json"
            store = JsonStore(root)
            first = self.summary("same")
            persist_scan_summary(store, path, first)
            replacement = {**first, "duration_sec": 9.5}
            persist_scan_summary(store, path, replacement)
            payload = store.load(path, {})
            self.assertEqual(len(payload["scans"]), 1)
            self.assertEqual(payload["scans"][0]["duration_sec"], 9.5)

            path.write_text("{broken", encoding="utf-8")
            persist_scan_summary(store, path, self.summary("recovered"))
            payload = store.load(path, {})
            self.assertEqual(payload["scans"][0]["scan_id"], "recovered")
            self.assertTrue(list(root.glob("*.corrupt.*")))

            path.write_text('{"schema_version":"bad","scans":[]}', encoding="utf-8")
            persist_scan_summary(store, path, self.summary("invalid-schema"))
            payload = store.load(path, {})
            self.assertEqual(payload["scans"][0]["scan_id"], "invalid-schema")


class AccumulationRadarIntegrationTests(unittest.TestCase):
    @staticmethod
    def item() -> dict[str, object]:
        quality = analyze_accumulation_quality(daily_rows(), now_ms=NOW_MS)
        return {
            "symbol": "TESTUSDT",
            "coin": "TEST",
            "quote_volume": 10_000_000,
            "price": 1.0,
            "price_24h": 0.0,
            "funding_pct": 0.0,
            "funding_ready": True,
            "funding_trend": "",
            "oi_6h": 0.0,
            "price_window": 0.0,
            "oi_usd": 1_000_000,
            "mcap": 10_000_000,
            "mcap_source": "Binance",
            "sideways_days": 45,
            "history_days": 52,
            "dark_flow": False,
            "accumulation_quality_v2": quality,
            "accumulation_quality_diagnostic": diagnostic(
                "TESTUSDT",
                quality,
                row_count=52,
            ),
        }

    def test_supporting_evidence_is_recorded_without_changing_scores(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = RadarEngine(
                Settings(data_dir=root),
                JsonStore(root),
            )
            window = closed_window(interval_sec=21600, delay_sec=300)
            output = io.StringIO()

            with redirect_stdout(output):
                result = engine._record_accumulation_quality_scan(
                    [],
                    window=window,
                    scan_started=1.0,
                )

            self.assertIsNotNone(result)
            self.assertEqual(result["feature_enabled"], True)
            self.assertIn("accumulation_quality_summary", output.getvalue())
            self.assertTrue(
                (root / "accumulation_quality_diagnostics.json").exists()
            )

    def test_write_failure_does_not_block_scan_result(self) -> None:
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = RadarEngine(
                Settings(data_dir=root),
                JsonStore(root),
            )
            window = closed_window(interval_sec=21600, delay_sec=300)
            stderr = io.StringIO()

            with (
                patch.object(engine.store, "update", side_effect=OSError("disk")),
                redirect_stderr(stderr),
                redirect_stdout(io.StringIO()),
            ):
                result = engine._record_accumulation_quality_scan(
                    [self.item()],
                    window=window,
                    scan_started=1.0,
                )

            self.assertIsNotNone(result)
            self.assertEqual(result["evaluated_count"], 1)
            self.assertIn("warning=OSError", stderr.getvalue())

    def test_diagnostics_add_no_requests_and_preserve_summary_text(self) -> None:
        class Source:
            diagnostics_calls = 0

            def diagnostics(self) -> dict[str, object]:
                self.diagnostics_calls += 1
                return {}

        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            engine = RadarEngine(
                Settings(data_dir=root),
                JsonStore(root),
            )
            source = Source()
            with (
                patch.object(engine, "_load_market_items", return_value=[self.item()]) as load,
                patch.object(engine, "_format_summary", return_value="unchanged-message"),
                redirect_stdout(io.StringIO()),
            ):
                result = engine.build_money_radar_summary(source)  # type: ignore[arg-type]

            load.assert_called_once()
            self.assertEqual(source.diagnostics_calls, 1)
            self.assertEqual(result["text"], "unchanged-message")
            self.assertTrue(
                (root / "accumulation_quality_diagnostics.json").exists()
            )


if __name__ == "__main__":
    unittest.main()
