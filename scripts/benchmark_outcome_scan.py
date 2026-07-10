from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paopao_radar.config import Settings  # noqa: E402
from paopao_radar.outcome_tracker import (  # noqa: E402
    OUTCOME_WINDOWS,
    OutcomeStore,
    calculate_outcome_metrics,
    interval_for_horizon,
    scan_outcomes,
)


def _settings(base: Path) -> Settings:
    return Settings(
        data_dir=base,
        signal_events_path=base / "signal_events.json",
        signal_events_db_path=base / "signals.db",
        outcome_db_path=base / "outcomes.db",
        outcome_request_sleep_sec=0,
        outcome_scan_limit=500,
        outcome_backfill_days=7,
    )


def _seed(settings: Settings, symbols: int, now_ts: int) -> OutcomeStore:
    store = OutcomeStore(settings.outcome_db_path)
    signal_ts = now_ts - OUTCOME_WINDOWS["72h"] - 3600
    store.create_pending(
        [
            {
                "id": index + 1,
                "ts": signal_ts,
                "time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(signal_ts)),
                "symbol": f"B{index:03d}USDT",
            }
            for index in range(symbols)
        ],
        OUTCOME_WINDOWS,
    )
    return store


def _fetcher(counter: dict[str, int], delay_sec: float):
    def fetch(_symbol: str, start_ts: int, end_ts: int, interval: str, _timeout: int) -> list[dict[str, float]]:
        counter["price_requests"] += 1
        if delay_sec:
            time.sleep(delay_sec)
        step = 3600 if interval == "1m" else 86400
        timestamps = list(range(start_ts, end_ts + 1, step))
        if timestamps[-1] != end_ts:
            timestamps.append(end_ts)
        return [
            {
                "open_time": float(timestamp),
                "high": 101.0 + index,
                "low": 99.0,
                "close": 100.0 + index,
            }
            for index, timestamp in enumerate(timestamps)
        ]

    return fetch


def _legacy_scan(
    store: OutcomeStore,
    now_ts: int,
    fetch,
    counter: dict[str, int],
    *,
    legacy_per_row_commits: bool,
    full_legacy: bool,
) -> None:
    rows = store.due_outcomes(now_ts=now_ts, limit=500)

    def calculate(row: dict[str, object]) -> dict[str, object]:
        start_ts = int(time.mktime(time.strptime(str(row["signal_time"])[:19], "%Y-%m-%dT%H:%M:%S")))
        horizon_sec = int(row["horizon_sec"])
        interval = interval_for_horizon(horizon_sec)
        metrics = calculate_outcome_metrics(fetch(row["symbol"], start_ts, start_ts + horizon_sec + 60, interval, 10))
        counter["decision_calculations"] += 1
        counter["transactions"] += 1
        return {**metrics, "data_status": "success"}

    if full_legacy:
        for row in rows:
            store.update_outcome(int(row["id"]), calculate(row))
        return
    if not legacy_per_row_commits:
        for row in rows:
            calculate(row)
        return
    # Optional durable mode measures one transaction per outcome while reusing
    # a connection. --full-legacy additionally measures historical reconnects.
    with store.connect() as connection:
        for row in rows:
            values = calculate(row)
            connection.execute(
                "UPDATE signal_outcomes SET data_status = :data_status, updated_at = :updated_at WHERE id = :id",
                {"data_status": values["data_status"], "updated_at": str(now_ts), "id": int(row["id"])},
            )
            connection.commit()


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare legacy and batched outcome scans using synthetic I/O.")
    parser.add_argument("--symbols", type=int, default=100)
    parser.add_argument("--request-delay-ms", type=float, default=5.0)
    parser.add_argument("--legacy-per-row-commits", action="store_true")
    parser.add_argument("--full-legacy", action="store_true")
    args = parser.parse_args()
    symbols = max(1, min(int(args.symbols), 125))
    delay_sec = max(0.0, float(args.request_delay_ms)) / 1000.0
    now_ts = int(time.time())

    with TemporaryDirectory() as tmp:
        base = Path(tmp)
        legacy_settings = _settings(base / "legacy")
        optimized_settings = _settings(base / "optimized")
        legacy_store = _seed(legacy_settings, symbols, now_ts)
        _seed(optimized_settings, symbols, now_ts)

        legacy = {"price_requests": 0, "decision_calculations": 0, "transactions": 0}
        started = time.perf_counter()
        _legacy_scan(
            legacy_store,
            now_ts,
            _fetcher(legacy, delay_sec),
            legacy,
            legacy_per_row_commits=bool(args.legacy_per_row_commits),
            full_legacy=bool(args.full_legacy),
        )
        legacy["elapsed_sec"] = round(time.perf_counter() - started, 4)

        optimized = {"price_requests": 0, "decision_calculations": 0, "transactions": 1}

        def decision_once(_symbol: str, _settings: Settings) -> dict[str, object]:
            optimized["decision_calculations"] += 1
            return {"decision_code": "wait", "decision_label": "等待", "decision_confidence": 50, "risk_level": "low"}

        started = time.perf_counter()
        with patch("paopao_radar.outcome_tracker._candidate_signals", return_value=[]), patch(
            "paopao_radar.outcome_tracker._decision_snapshot", side_effect=decision_once
        ):
            result = scan_outcomes(
                settings=optimized_settings,
                limit=500,
                now_ts=now_ts,
                price_fetcher=_fetcher(optimized, delay_sec),
            )
        optimized["elapsed_sec"] = round(time.perf_counter() - started, 4)
        optimized["success"] = int(result["counts"]["success"])

    elapsed_reduction = 0.0
    if legacy["elapsed_sec"]:
        elapsed_reduction = (1.0 - optimized["elapsed_sec"] / legacy["elapsed_sec"]) * 100.0
    report = {
        "symbols": symbols,
        "horizons": len(OUTCOME_WINDOWS),
        "synthetic_request_delay_ms": args.request_delay_ms,
        "legacy_mode": (
            "per-row connection"
            if args.full_legacy
            else "persistent connection, per-row commit"
            if args.legacy_per_row_commits
            else "lightweight modeled writes"
        ),
        "legacy": legacy,
        "optimized": optimized,
        "improvement": {
            "elapsed_reduction_pct": round(elapsed_reduction, 2),
            "price_request_reduction_pct": round((1.0 - optimized["price_requests"] / legacy["price_requests"]) * 100.0, 2),
            "decision_reduction_pct": round((1.0 - optimized["decision_calculations"] / legacy["decision_calculations"]) * 100.0, 2),
        },
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
