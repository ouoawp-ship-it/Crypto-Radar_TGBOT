#!/usr/bin/env python3
"""Reproducible synthetic benchmark for the Phase 2 Web API changes.

The benchmark is intentionally offline.  It creates temporary SQLite databases,
seeds deterministic data, warms every code path, and compares legacy-style query
patterns with the current request-scoped/projection implementations.  Timings
include JSON serialization because projection also reduces serialization work.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Iterator
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paopao_radar.config import Settings  # noqa: E402
from paopao_radar.lifecycle_engine import (  # noqa: E402
    NOT_ADVICE,
    enrich_event_display,
    enrich_lifecycle_display,
)
from paopao_radar.lifecycle_store import LifecycleStore  # noqa: E402
from paopao_radar.outcome_tracker import OUTCOME_WINDOWS, OutcomeStore  # noqa: E402
from paopao_radar.signal_store import SignalEventStore  # noqa: E402
from paopao_radar.web import signals_payload  # noqa: E402
from paopao_radar.web_services import decision as decision_api  # noqa: E402
from paopao_radar.web_services import outcomes as outcome_api  # noqa: E402
from paopao_radar.web_services.api_core import api_ok, redact_api_payload  # noqa: E402
from paopao_radar.web_services.lifecycle import lifecycle_detail_payload  # noqa: E402


Payload = dict[str, Any]
PayloadCall = Callable[[], Payload]


def _iso(timestamp: int | float | None = None) -> str:
    value = time.time() if timestamp is None else float(timestamp)
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


def _settings(root: Path) -> Settings:
    return Settings(
        data_dir=root,
        signal_events_path=root / "signal_events.json",
        signal_events_db_path=root / "signals.db",
        outcome_db_path=root / "outcomes.db",
        lifecycle_db_path=root / "lifecycle.db",
        tg_push_history_path=root / "push_history.json",
    )


def _seed_signals(settings: Settings, *, symbols: int, rows_per_symbol: int, blob_bytes: int) -> list[str]:
    store = SignalEventStore(settings.signal_events_db_path)
    now = int(time.time())
    modules = ("launch", "flow", "structure", "funding")
    names = [f"B{index:03d}USDT" for index in range(symbols)]
    rows: list[tuple[Any, ...]] = []
    for symbol_index, symbol in enumerate(names):
        for row_index in range(rows_per_symbol):
            timestamp = now - symbol_index - row_index
            module = modules[row_index % len(modules)]
            blob = chr(97 + row_index % 26) * blob_bytes
            rows.append(
                (
                    timestamp,
                    _iso(timestamp),
                    module,
                    f"TG_{module.upper()}_RADAR",
                    f"{module}_signal",
                    symbol,
                    symbol.removesuffix("USDT"),
                    "launching",
                    "info",
                    float(65 + row_index % 25),
                    f"{symbol} synthetic signal",
                    f"{symbol} {module} {blob}",
                    f"<p>{blob}</p>",
                    f"benchmark:{symbol}:{row_index}",
                    "sent",
                    1,
                    "",
                    "[]",
                    0,
                    json.dumps({"source": "benchmark", "blob": blob}, separators=(",", ":")),
                    "",
                )
            )
    with store.connect() as connection:
        connection.executemany(
            """
            INSERT INTO signals (
                ts, time, module, template_id, signal_type, symbol, coin, stage,
                severity, score, title, excerpt, text_html, dedup_key, status,
                sent, topic_id, message_ids_json, reply_to_message_id,
                payload_json, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    return names


def _seed_outcomes(settings: Settings, symbols: list[str]) -> None:
    store = OutcomeStore(settings.outcome_db_path)
    now = int(time.time())
    signals = [
        {
            "id": index + 1,
            "symbol": symbol,
            "time": _iso(now - 7200 - index),
            "ts": now - 7200 - index,
            "module": "launch",
            "signal_type": "launch_signal",
            "score": 75,
            "stage": "launching",
        }
        for index, symbol in enumerate(symbols)
    ]
    store.create_pending(signals, OUTCOME_WINDOWS)
    with store.connect() as connection:
        connection.execute(
            """
            UPDATE signal_outcomes
            SET entry_price = 100.0,
                future_price = 104.0,
                max_high_price = 108.0,
                min_low_price = 97.0,
                final_return_pct = 4.0,
                max_gain_pct = 8.0,
                max_drawdown_pct = -3.0,
                result_label = 'strong',
                result_tone = 'positive',
                decision_code = 'probe',
                decision_label = 'probe',
                decision_confidence = 72,
                risk_level = 'low',
                data_status = 'success',
                data_source = 'synthetic',
                updated_at = ?
            """,
            (_iso(now),),
        )


def _seed_lifecycles(settings: Settings, symbols: list[str], *, blob_bytes: int) -> None:
    store = LifecycleStore(settings.lifecycle_db_path)
    now = int(time.time())
    blob = "m" * blob_bytes
    with store.connect() as connection:
        for index, symbol in enumerate(symbols):
            timestamp = now - index
            lifecycle, _created = store.create_lifecycle(
                {
                    "symbol": symbol,
                    "first_signal_id": index + 1,
                    "first_signal_at": _iso(timestamp),
                    "first_signal_module": "launch",
                    "first_signal_template": "TG_LAUNCH_RADAR",
                    "first_signal_type": "launch_signal",
                    "first_signal_level": "1h",
                    "first_signal_level_rank": 2,
                    "first_signal_score": 75,
                    "first_signal_excerpt": f"{symbol} {blob}",
                    "first_price": 100.0,
                    "current_state": "launching",
                    "highest_level": "1h",
                    "highest_level_rank": 2,
                    "lifecycle_score": 75.0,
                    "risk_score": 20.0,
                    "latest_signal_id": index + 1,
                    "latest_signal_at": _iso(timestamp),
                    "latest_price": 104.0,
                    "exchange_context": {"source": "synthetic", "blob": blob},
                    "metrics": {"price": 104.0, "blob": blob},
                    "reasons": ["synthetic", blob],
                    "is_active": 1,
                },
                conn=connection,
            )
            lifecycle_id = int(lifecycle.get("id") or 0)
            for event_index in range(2):
                store.insert_event(
                    {
                        "lifecycle_id": lifecycle_id,
                        "symbol": symbol,
                        "event_time": _iso(timestamp - event_index),
                        "event_type": "state_change",
                        "event_level": "1h",
                        "event_level_rank": 2,
                        "signal_id": (index + 1) * 10 + event_index,
                        "source_module": "launch",
                        "source_template": "TG_LAUNCH_RADAR",
                        "source_excerpt": f"{symbol} event {blob}",
                        "previous_state": "warming",
                        "new_state": "launching",
                        "event_score": 75.0,
                        "risk_score": 20.0,
                        "metrics": {"blob": blob},
                        "reasons": [blob],
                        "exchange_context": {"blob": blob},
                        "dedup_key": f"benchmark:{symbol}:{event_index}",
                    },
                    conn=connection,
                )
            for snapshot_index in range(3):
                store.insert_snapshot(
                    {
                        "symbol": symbol,
                        "timeframe": "1h",
                        "snapshot_time": _iso(timestamp - snapshot_index),
                        "price": 100.0 + snapshot_index,
                        "volume": 1_000.0,
                        "quote_volume": 100_000.0,
                        "oi": 500.0,
                        "oi_value_usdt": 50_000.0,
                        "funding_rate": 0.0001,
                        "market_cap_usd": 100_000_000.0,
                        "metrics": {"blob": blob},
                    },
                    conn=connection,
                )


@contextmanager
def _connection_counter(store_type: type[Any]) -> Iterator[dict[str, int]]:
    original = store_type.connect
    counter = {"count": 0}

    @contextmanager
    def counted(store: Any) -> Iterator[Any]:
        counter["count"] += 1
        with original(store) as connection:
            yield connection

    with patch.object(store_type, "connect", new=counted):
        yield counter


def _p95(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)]


def _measure(call: PayloadCall, *, samples: int, counter: dict[str, int]) -> dict[str, float | int]:
    # A warm request excludes first-use imports, schema checks, and filesystem cache
    # population from the measured series while retaining the normal request path.
    warm_payload = call()
    json.dumps(warm_payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")

    durations: list[float] = []
    connections: list[int] = []
    payload_sizes: list[int] = []
    for _ in range(samples):
        before_connections = counter["count"]
        started = time.perf_counter()
        payload = call()
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        durations.append((time.perf_counter() - started) * 1000.0)
        connections.append(counter["count"] - before_connections)
        payload_sizes.append(len(encoded))
    return {
        "samples": samples,
        "p95_latency_ms": round(_p95(durations), 3),
        "mean_latency_ms": round(statistics.fmean(durations), 3),
        "database_connections_per_request": round(statistics.fmean(connections), 3),
        "json_payload_bytes": int(statistics.median(payload_sizes)),
    }


def _change(legacy: dict[str, Any], optimized: dict[str, Any]) -> dict[str, float]:
    def reduction(key: str) -> float:
        baseline = float(legacy[key])
        current = float(optimized[key])
        return round((1.0 - current / baseline) * 100.0, 2) if baseline else 0.0

    return {
        "p95_latency_reduction_pct": reduction("p95_latency_ms"),
        "connection_reduction_pct": reduction("database_connections_per_request"),
        "payload_reduction_pct": reduction("json_payload_bytes"),
    }


def _legacy_outcomes_payload(settings: Settings, *, limit: int) -> Payload:
    """Model the old list + stats request where each helper owned a connection."""
    store = OutcomeStore(settings.outcome_db_path)
    window_sec = outcome_api._safe_window(604800, 604800)
    start_time, end_time = outcome_api._time_bounds(window_sec)
    listed = store.list_outcomes(limit=limit, start_time=start_time, end_time=end_time, sort="-id")
    stats = store.stats()
    items = redact_api_payload(listed.get("items", []))
    filters = {
        "symbol": "",
        "horizon": "",
        "decision": "",
        "result": "",
        "module": "",
        "data_status": "",
        "window_sec": window_sec,
    }
    summary = outcome_api._summary_from_stats(stats)
    data = {
        "items": items,
        "summary": summary,
        "stats": redact_api_payload(stats),
        "filters": filters,
        "pagination": {"limit": limit, "next_cursor": listed.get("next_cursor")},
    }
    payload = api_ok(data, message="synthetic outcome benchmark")
    payload.update(
        {
            "items": items,
            "count": len(items),
            "next_cursor": listed.get("next_cursor"),
            "summary": summary,
            "filters": filters,
            "pagination": data["pagination"],
        }
    )
    return redact_api_payload(payload)


def _legacy_lifecycle_detail(settings: Settings, symbol: str) -> Payload:
    """Model the old detail request with lifecycle/events/snapshots connections."""
    store = LifecycleStore(settings.lifecycle_db_path)
    lifecycle = enrich_lifecycle_display(store.get_lifecycle(symbol))
    events = [enrich_event_display(item) for item in store.list_events(symbol=symbol, limit=30)]
    snapshots = store.list_snapshots(symbol=symbol, limit=60)
    data = {
        "symbol": symbol,
        "lifecycle": lifecycle,
        "events": events,
        "metrics": snapshots,
        "not_advice": NOT_ADVICE,
    }
    safe_data = redact_api_payload(data)
    payload = api_ok(safe_data, message="synthetic lifecycle benchmark")
    payload.update(safe_data)
    return payload


def _legacy_decision_results(
    symbols: list[str],
    *,
    window_sec: int,
    limit_per_symbol: int = 50,
    settings: Settings | None = None,
    **_ignored: Any,
) -> dict[str, Payload]:
    """Model the old N+1 decision path: one query/connection per symbol."""
    results: dict[str, Payload] = {}
    for symbol in symbols:
        payload = decision_api.decision_for_symbol_payload(
            symbol,
            window_sec=window_sec,
            limit=limit_per_symbol,
            settings=settings,
        )
        # Preserve the exact object shape consumed by decisions_payload.
        if payload.get("ok"):
            results[symbol] = payload
    return results


def run_benchmark(*, symbols: int, rows_per_symbol: int, blob_bytes: int, samples: int) -> Payload:
    with TemporaryDirectory(prefix="paopao-api-phase2-") as temporary:
        settings = _settings(Path(temporary))
        names = _seed_signals(
            settings,
            symbols=symbols,
            rows_per_symbol=rows_per_symbol,
            blob_bytes=blob_bytes,
        )
        _seed_outcomes(settings, names)
        _seed_lifecycles(settings, names, blob_bytes=blob_bytes)

        endpoints: dict[str, Any] = {}

        original_list_signals = SignalEventStore.list_signals

        def full_signal_list(store: SignalEventStore, *args: Any, **kwargs: Any) -> Payload:
            kwargs["compact"] = False
            return original_list_signals(store, *args, **kwargs)

        with _connection_counter(SignalEventStore) as counter:
            with patch.object(SignalEventStore, "list_signals", new=full_signal_list):
                legacy = _measure(
                    lambda: signals_payload(settings=settings, limit=50),
                    samples=samples,
                    counter=counter,
                )
            optimized = _measure(
                lambda: signals_payload(settings=settings, limit=50),
                samples=samples,
                counter=counter,
            )
        endpoints["/signals"] = {
            "scenario": "50-row list: SELECT * / large fields versus compact projection",
            "legacy": legacy,
            "optimized": optimized,
            "change": _change(legacy, optimized),
        }

        decision_limit = min(symbols, 20)
        with _connection_counter(SignalEventStore) as counter:
            with patch.object(
                decision_api,
                "_decision_results_for_symbols",
                new=_legacy_decision_results,
            ):
                legacy = _measure(
                    lambda: decision_api.decisions_payload(
                        settings=settings,
                        limit=decision_limit,
                        window_sec=86400,
                    ),
                    samples=samples,
                    counter=counter,
                )
            optimized = _measure(
                lambda: decision_api.decisions_payload(
                    settings=settings,
                    limit=decision_limit,
                    window_sec=86400,
                ),
                samples=samples,
                counter=counter,
            )
        endpoints["/decision"] = {
            "scenario": f"{decision_limit} symbols: N+1 per-symbol reads versus one request connection",
            "legacy": legacy,
            "optimized": optimized,
            "change": _change(legacy, optimized),
        }

        with _connection_counter(OutcomeStore) as counter:
            legacy = _measure(
                lambda: _legacy_outcomes_payload(settings, limit=50),
                samples=samples,
                counter=counter,
            )
            optimized = _measure(
                lambda: outcome_api.outcomes_payload(settings=settings, limit=50),
                samples=samples,
                counter=counter,
            )
        endpoints["/outcomes"] = {
            "scenario": "list + aggregate stats: independent connections versus request-scoped connection",
            "legacy": legacy,
            "optimized": optimized,
            "change": _change(legacy, optimized),
        }

        detail_symbol = names[0]
        with _connection_counter(LifecycleStore) as counter:
            legacy = _measure(
                lambda: _legacy_lifecycle_detail(settings, detail_symbol),
                samples=samples,
                counter=counter,
            )
            optimized = _measure(
                lambda: lifecycle_detail_payload(detail_symbol, settings=settings),
                samples=samples,
                counter=counter,
            )
        endpoints["/lifecycle"] = {
            "scenario": "detail lifecycle + events + metrics: three connections versus one request connection",
            "legacy": legacy,
            "optimized": optimized,
            "change": _change(legacy, optimized),
        }

    return {
        "benchmark": "Performance Optimization Phase 2 - Web API",
        "offline": True,
        "database": "temporary SQLite",
        "methodology": {
            "latency": "nearest-rank P95; service execution plus UTF-8 JSON serialization",
            "warmup_requests_per_variant": 1,
            "samples_per_variant": samples,
            "symbols": symbols,
            "signal_rows_per_symbol": rows_per_symbol,
            "synthetic_large_field_bytes": blob_bytes,
        },
        "endpoints": endpoints,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Phase 2 Web API SQLite/query optimizations.")
    parser.add_argument("--symbols", type=int, default=10)
    parser.add_argument("--rows-per-symbol", type=int, default=8)
    parser.add_argument("--blob-bytes", type=int, default=4_000)
    parser.add_argument("--samples", type=int, default=12)
    args = parser.parse_args()
    report = run_benchmark(
        symbols=max(2, min(int(args.symbols), 50)),
        rows_per_symbol=max(2, min(int(args.rows_per_symbol), 20)),
        blob_bytes=max(128, min(int(args.blob_bytes), 20_000)),
        samples=max(5, min(int(args.samples), 100)),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
