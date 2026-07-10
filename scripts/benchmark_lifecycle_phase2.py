#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paopao_radar.config import Settings  # noqa: E402
from paopao_radar.lifecycle_engine import LifecycleEngine, scan_lifecycles  # noqa: E402
from paopao_radar.lifecycle_store import LifecycleStore  # noqa: E402


TIMEFRAMES = ("15m", "1h", "4h", "24h")


def make_settings(directory: str) -> Settings:
    root = Path(directory)
    return Settings(
        data_dir=root,
        signal_events_path=root / "signals.json",
        signal_events_db_path=root / "signals.db",
        lifecycle_db_path=root / "lifecycle.db",
        tg_push_history_path=root / "push_history.json",
    )


def make_signals(symbols: int, repeats: int) -> list[dict[str, object]]:
    now = int(time.time())
    rows: list[dict[str, object]] = []
    signal_id = 1
    for symbol_index in range(symbols):
        symbol = f"C{symbol_index:04d}USDT"
        for timeframe in TIMEFRAMES:
            for _ in range(repeats):
                rows.append(
                    {
                        "id": signal_id,
                        "symbol": symbol,
                        "status": "sent",
                        "module": "launch",
                        "template_id": "TG_LAUNCH_ALERT",
                        "timeframe": timeframe,
                        "signal_type": "launch",
                        "stage": "launch",
                        "score": 82,
                        "excerpt": f"{symbol} {timeframe} lifecycle signal",
                        "time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now)),
                        "ts": now,
                    }
                )
                signal_id += 1
    return rows


def run_case(
    *,
    settings: Settings,
    signals: list[dict[str, object]],
    batched: bool,
    provider_delay_ms: float,
) -> dict[str, float | int]:
    provider_calls = 0
    connection_calls = 0

    def provider(symbol: str, timeframe: str) -> dict[str, object]:
        nonlocal provider_calls
        provider_calls += 1
        if provider_delay_ms > 0:
            time.sleep(provider_delay_ms / 1000.0)
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "price": 100.0,
            "volume": 1_000.0,
            "quote_volume": 100_000.0,
            "oi": 500.0,
            "oi_value_usdt": 50_000.0,
            "futures_cvd_delta": 10.0,
            "spot_cvd_delta": 5.0,
            "funding_rate": 0.0001,
            "market_cap_usd": 100_000_000.0,
            "data_source": "benchmark",
            "data_source_status": "ok",
            "exchange_context": {"items": []},
        }

    original_connect = LifecycleStore.connect

    @contextmanager
    def counted_connect(store: LifecycleStore):
        nonlocal connection_calls
        connection_calls += 1
        with original_connect(store) as conn:
            yield conn

    started = time.perf_counter()
    with patch.object(LifecycleStore, "connect", new=counted_connect):
        if batched:
            with patch("paopao_radar.lifecycle_engine.candidate_lifecycle_signals", return_value=signals):
                scan_lifecycles(
                    settings=settings,
                    limit_symbols=500,
                    metrics_provider=provider,
                )
        else:
            engine = LifecycleEngine(settings, metrics_provider=provider)
            for item in signals:
                engine.process_signal(item)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {
        "elapsed_ms": round(elapsed_ms, 2),
        "signals_per_second": round(len(signals) / max(elapsed_ms / 1000.0, 0.000001), 2),
        "provider_calls": provider_calls,
        "database_connections": connection_calls,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark Lifecycle Phase 2 scan batching and cache reuse.")
    parser.add_argument("--symbols", type=int, default=100)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--provider-delay-ms", type=float, default=0.5)
    args = parser.parse_args()
    symbol_count = max(1, min(args.symbols, 500))
    repeats = max(1, args.repeats)
    signals = make_signals(symbol_count, repeats)

    with TemporaryDirectory() as baseline_dir, TemporaryDirectory() as optimized_dir:
        baseline = run_case(
            settings=make_settings(baseline_dir),
            signals=signals,
            batched=False,
            provider_delay_ms=max(0.0, args.provider_delay_ms),
        )
        optimized = run_case(
            settings=make_settings(optimized_dir),
            signals=signals,
            batched=True,
            provider_delay_ms=max(0.0, args.provider_delay_ms),
        )

    report = {
        "symbols": symbol_count,
        "timeframes_per_symbol": len(TIMEFRAMES),
        "signals": len(signals),
        "provider_delay_ms": max(0.0, args.provider_delay_ms),
        "baseline_per_signal": baseline,
        "phase2_batched": optimized,
        "elapsed_speedup": round(
            float(baseline["elapsed_ms"]) / max(float(optimized["elapsed_ms"]), 0.001),
            2,
        ),
        "provider_call_reduction_pct": round(
            (1.0 - int(optimized["provider_calls"]) / max(int(baseline["provider_calls"]), 1)) * 100.0,
            2,
        ),
        "connection_reduction_pct": round(
            (1.0 - int(optimized["database_connections"]) / max(int(baseline["database_connections"]), 1)) * 100.0,
            2,
        ),
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
