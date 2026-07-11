#!/usr/bin/env python3
"""Offline benchmark for the v1.78 lifecycle intelligence read paths.

The benchmark creates deterministic temporary SQLite data and never calls a
remote exchange or the production database.  Reported latency includes JSON
serialization so field projection and response size remain visible.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paopao_radar.config import Settings  # noqa: E402
from paopao_radar.lifecycle_analytics import (  # noqa: E402
    DEFAULT_ANALYTICS_CACHE_KEY,
)
from paopao_radar.lifecycle_intelligence import (  # noqa: E402
    INTELLIGENCE_MODEL_VERSION,
)
from paopao_radar.lifecycle_intelligence_store import IntelligenceStore  # noqa: E402
from paopao_radar.lifecycle_replay import REPLAY_MODEL_VERSION  # noqa: E402
from paopao_radar.lifecycle_similarity import (  # noqa: E402
    SIMILARITY_MODEL_VERSION,
    find_similar_for_symbol,
)
from paopao_radar.lifecycle_store import LifecycleStore  # noqa: E402
from paopao_radar.web_services.lifecycle_intelligence import (  # noqa: E402
    lifecycle_analytics_payload,
    lifecycle_intelligence_list_payload,
    lifecycle_replay_frames_payload,
    lifecycle_replay_payload,
)


PayloadCall = Callable[[], dict[str, Any]]


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _settings(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        lifecycle_db_path=root / "lifecycle.db",
        outcome_db_path=root / "outcomes.db",
        signal_events_db_path=root / "signals.db",
        web_jobs_db_path=root / "jobs.db",
        tg_push_history_path=root / "telegram.json",
        lifecycle_similarity_min_samples=5,
    )


def _seed(settings: Settings, *, lifecycles: int, replay_frames: int) -> None:
    old_store = LifecycleStore(settings.lifecycle_db_path)
    old_store.ensure_schema()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    symbols = ["BTCUSDT"] + [f"B{index:03d}USDT" for index in range(1, lifecycles)]
    with old_store.transaction() as conn:
        for index, symbol in enumerate(symbols, 1):
            first = now - timedelta(days=index % 20 + 1)
            updated = first + timedelta(hours=12 + index % 36)
            first_level = ("15m", "1h", "4h", "24h")[index % 4]
            first_rank = {"15m": 1, "1h": 2, "4h": 3, "24h": 4}[first_level]
            highest = "24h" if index % 5 == 0 else "4h"
            highest_rank = 4 if highest == "24h" else 3
            conn.execute(
                """
                INSERT INTO signal_lifecycles (
                    id, symbol, first_signal_id, first_signal_at, first_signal_module,
                    first_signal_level, first_signal_level_rank, first_price,
                    current_state, highest_level, highest_level_rank, lifecycle_score,
                    risk_score, latest_signal_id, latest_signal_at, latest_price,
                    latest_oi, latest_futures_cvd_15m, latest_spot_cvd_15m,
                    latest_funding_rate, price_change_from_first_pct,
                    oi_change_from_first_pct, futures_cvd_change_from_first,
                    spot_cvd_change_from_first, is_active, created_at, updated_at,
                    closed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    index,
                    symbol,
                    index * 10,
                    _iso(first),
                    ("structure", "flow", "launch", "funding")[index % 4],
                    first_level,
                    first_rank,
                    100.0,
                    "closed" if index > 1 else "upgraded_4h",
                    highest,
                    highest_rank,
                    float(72 + index % 24),
                    float(15 + index % 35),
                    index * 10 + 3,
                    _iso(updated),
                    104.0 + index % 8,
                    108.0 + index % 30,
                    1_000_000.0 + index,
                    900_000.0 + index,
                    0.00012,
                    float(4 + index % 8),
                    float(8 + index % 18),
                    1_000_000.0,
                    900_000.0,
                    1 if index == 1 else 0,
                    _iso(first),
                    _iso(updated),
                    None if index == 1 else _iso(updated),
                ),
            )
            event_count = replay_frames if index == 1 else 8
            for event_index in range(event_count):
                event_time = first + timedelta(minutes=15 * event_index)
                event_type = (
                    "first_signal"
                    if event_index == 0
                    else "timeframe_upgrade_1h"
                    if event_index == 1
                    else "timeframe_upgrade_4h"
                    if event_index == 2
                    else "spot_cvd_confirmed"
                )
                conn.execute(
                    """
                    INSERT INTO lifecycle_events (
                        lifecycle_id, symbol, event_time, event_type, event_level,
                        signal_id, previous_state, new_state, price,
                        price_change_from_first_pct, oi_change_pct,
                        futures_cvd_delta, spot_cvd_delta, funding_rate,
                        event_score, risk_score, metrics_json, reasons_json,
                        exchange_context_json, dedup_key, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        index,
                        symbol,
                        _iso(event_time),
                        event_type,
                        "4h" if event_index >= 2 else "1h" if event_index == 1 else first_level,
                        index * 10 + event_index,
                        "launching",
                        "upgraded_4h" if event_index >= 2 else "upgraded_1h",
                        100.0 + event_index * 0.1,
                        event_index * 0.1,
                        8.0 + event_index * 0.05,
                        1_000_000.0 + event_index,
                        900_000.0 + event_index,
                        0.00012,
                        75.0,
                        25.0,
                        "{}",
                        "[]",
                        "{}",
                        f"benchmark:{index}:{event_index}",
                        _iso(event_time),
                    ),
                )

    store = IntelligenceStore(settings)
    store.ensure_schema()
    with store.transaction() as conn:
        for index, symbol in enumerate(symbols, 1):
            score = float(80 + index % 17)
            store.upsert_intelligence(
                {
                    "lifecycle_id": index,
                    "symbol": symbol,
                    "intelligence_score": score,
                    "quality_label": "高质量启动" if score < 90 else "强趋势确认",
                    "stage_label": "趋势扩张",
                    "momentum_label": "趋势增强",
                    "capital_confirmation_label": "现货与合约同步确认",
                    "risk_label": "低风险",
                    "maturity_label": "4H 周期确认",
                    "confidence_label": "可参考",
                    "summary": f"{symbol} synthetic lifecycle intelligence",
                    "strengths": ["现货与合约主动买盘同步确认。"],
                    "risks": [],
                    "watch_points": ["持续观察资金费率。"],
                    "factors": {"benchmark": True},
                    "model_version": INTELLIGENCE_MODEL_VERSION,
                    "source_signature": f"intelligence-{index}",
                },
                conn=conn,
                fetch=False,
            )
            frame_count = replay_frames if index == 1 else 8
            frames = [
                {
                    "frame_index": frame_index + 1,
                    "event_id": frame_index + 1,
                    "event_time": _iso(now - timedelta(minutes=frame_count - frame_index)),
                    "event_type": "spot_cvd_confirmed",
                    "event_label": "现货主动买盘确认",
                    "state_before": "upgraded_1h",
                    "state_after": "upgraded_4h",
                    "signal_level": "4h",
                    "price": 100.0 + frame_index * 0.1,
                    "price_change_from_first_pct": frame_index * 0.1,
                    "oi_change_from_first_pct": 8.0 + frame_index * 0.05,
                    "spot_cvd_delta": 900_000.0 + frame_index,
                    "futures_cvd_delta": 1_000_000.0 + frame_index,
                    "funding_rate": 0.00012,
                    "lifecycle_score": 80.0,
                    "risk_score": 25.0,
                    "intelligence_score": score,
                    "summary": "现货与合约资金同步确认。",
                    "metrics": {"large_internal_blob": "x" * 4096},
                }
                for frame_index in range(frame_count)
            ]
            store.upsert_replay(
                {
                    "lifecycle_id": index,
                    "symbol": symbol,
                    "replay_version": REPLAY_MODEL_VERSION,
                    "frame_count": frame_count,
                    "duration_sec": 43_200,
                    "upgrade_path": f"{('15m' if index % 2 else '1h')} → 1h → 4h",
                    "highest_level": "4h",
                    "time_to_1h_sec": 3600,
                    "time_to_4h_sec": 14_400,
                    "max_price_gain_pct": 9.0,
                    "max_drawdown_pct": -2.0,
                    "final_return_pct": 5.0 + index % 4,
                    "final_state": "closed",
                    "result_label": "strong_success" if index % 3 == 0 else "success",
                    "outcome_status": "success",
                    "outcome_count": 4,
                    "source_signature": f"replay-{index}",
                    "summary": {"event_count": frame_count},
                },
                frames,
                conn=conn,
                fetch=False,
            )
        cached = {
            "model_version": "lifecycle-analytics-v1",
            "summary": {"total_count": lifecycles, "active_count": 1},
            "first_level": [{"key": "15m", "sample_count": lifecycles // 2}],
            "upgrade_path": [{"key": "15m → 1h → 4h", "sample_count": lifecycles // 2}],
            "module": [{"key": "structure", "sample_count": lifecycles // 4}],
            "capital_confirmation": [
                {"key": "现货与合约同步确认", "sample_count": lifecycles}
            ],
        }
        store.put_analytics_cache(
            DEFAULT_ANALYTICS_CACHE_KEY,
            {"data": cached},
            ttl_sec=21_600,
            conn=conn,
        )


def _nearest_rank_p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(0.95 * len(ordered)) - 1)]


def _measure(call: PayloadCall, *, samples: int) -> dict[str, float | int]:
    call()
    elapsed: list[float] = []
    payload_size = 0
    for _ in range(max(1, samples)):
        started = time.perf_counter()
        payload = call()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        elapsed.append((time.perf_counter() - started) * 1000.0)
        payload_size = len(encoded)
    return {
        "p95_ms": round(_nearest_rank_p95(elapsed), 3),
        "avg_ms": round(sum(elapsed) / len(elapsed), 3),
        "json_bytes": payload_size,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    with TemporaryDirectory(prefix="paopao-lifecycle-intelligence-") as directory:
        settings = _settings(Path(directory))
        _seed(
            settings,
            lifecycles=max(10, int(args.lifecycles)),
            replay_frames=max(100, int(args.frames)),
        )
        symbol = "BTCUSDT"
        results = {
            "intelligence_list": _measure(
                lambda: lifecycle_intelligence_list_payload(
                    limit=50, settings=settings, public=True
                ),
                samples=args.samples,
            ),
            "replay_summary": _measure(
                lambda: lifecycle_replay_payload(symbol, settings=settings, public=True),
                samples=args.samples,
            ),
            "replay_frames_100": _measure(
                lambda: lifecycle_replay_frames_payload(
                    symbol, limit=100, settings=settings, public=True
                ),
                samples=args.samples,
            ),
            "analytics_cached": _measure(
                lambda: lifecycle_analytics_payload(
                    "upgrade_path", settings=settings, public=True
                ),
                samples=args.samples,
            ),
            "similarity_top10": _measure(
                lambda: find_similar_for_symbol(
                    settings=settings,
                    symbol=symbol,
                    limit=10,
                    min_samples=5,
                ),
                samples=args.samples,
            ),
        }
        targets = {
            "intelligence_list": 100,
            "replay_summary": 100,
            "replay_frames_100": 150,
            "analytics_cached": 50,
            "similarity_top10": 200,
        }
        return {
            "benchmark": "lifecycle-intelligence-v1.78",
            "model_versions": {
                "intelligence": INTELLIGENCE_MODEL_VERSION,
                "replay": REPLAY_MODEL_VERSION,
                "similarity": SIMILARITY_MODEL_VERSION,
            },
            "dataset": {
                "lifecycles": max(10, int(args.lifecycles)),
                "replay_frames": max(100, int(args.frames)),
                "samples": max(1, int(args.samples)),
            },
            "results": {
                key: {
                    **value,
                    "target_p95_ms": targets[key],
                    "within_target": float(value["p95_ms"]) < targets[key],
                }
                for key, value in results.items()
            },
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lifecycles", type=int, default=120)
    parser.add_argument("--frames", type=int, default=100)
    parser.add_argument("--samples", type=int, default=25)
    args = parser.parse_args()
    print(json.dumps(run(args), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
