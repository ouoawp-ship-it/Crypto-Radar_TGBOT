#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paopao_radar.config import Settings  # noqa: E402
from paopao_radar.funding_sources import MultiExchangeFundingClient  # noqa: E402


EXCHANGES = ("BINANCE", "OKX", "BYBIT", "BITGET", "GATE")


class SyntheticFundingHttp:
    def __init__(self, latency_sec: float) -> None:
        self.latency_sec = latency_sec
        self.lock = threading.Lock()
        self.active = 0
        self.peak_active = 0

    def get_json(self, url: str, params=None, **_kwargs: Any) -> Any:
        params = dict(params or {})
        with self.lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
        try:
            time.sleep(self.latency_sec)
            next_time = 1_788_000_000_000
            if "premiumIndex" in url:
                return {
                    "symbol": str(params.get("symbol") or "BTCUSDT"),
                    "lastFundingRate": "0.0001",
                    "nextFundingTime": next_time,
                }
            if "okx.com" in url:
                return {"data": [{"fundingRate": "0.0001", "fundingTime": str(next_time)}]}
            if "bybit.com" in url:
                return {"result": {"list": [{"fundingRate": "0.0001", "nextFundingTime": str(next_time)}]}}
            if "bitget.com" in url:
                return {"data": [{"fundingRate": "0.0001", "nextUpdate": str(next_time)}]}
            return {
                "funding_rate": "0.0001",
                "funding_interval": 3600,
                "funding_next_apply": int(next_time / 1000),
            }
        finally:
            with self.lock:
                self.active -= 1


def serial_baseline(
    client: MultiExchangeFundingClient,
    symbols: list[str],
) -> dict[str, float | int]:
    started = time.perf_counter()
    succeeded = 0
    failed = 0
    for symbol in symbols:
        for exchange in EXCHANGES:
            if client._snapshot_one(symbol, exchange, include_history=False):
                succeeded += 1
            else:
                failed += 1
    elapsed = time.perf_counter() - started
    requests = succeeded + failed
    return {
        "elapsed_sec": round(elapsed, 4),
        "succeeded": succeeded,
        "failed": failed,
        "success_rate": round(succeeded / requests, 4) if requests else 0.0,
        "failure_rate": round(failed / requests, 4) if requests else 0.0,
        "average_response_ms": round(elapsed * 1000 / requests, 3) if requests else 0.0,
        "peak_concurrency": 1 if requests else 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark bounded funding scan concurrency")
    parser.add_argument("--symbols", type=int, default=120)
    parser.add_argument("--latency-ms", type=float, default=2.0)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()

    symbol_count = max(1, min(120, args.symbols))
    symbols = [f"COIN{index}USDT" for index in range(symbol_count)]
    settings = Settings(
        launch_funding_exchanges=EXCHANGES,
        funding_scan_concurrency=max(6, min(8, args.concurrency)),
        funding_request_timeout_sec=8,
        funding_max_symbols_per_batch=120,
    )
    latency_sec = max(0.0, args.latency_ms) / 1000

    serial_http = SyntheticFundingHttp(latency_sec)
    serial = serial_baseline(MultiExchangeFundingClient(settings, serial_http), symbols)  # type: ignore[arg-type]

    concurrent_http = SyntheticFundingHttp(latency_sec)
    concurrent_client = MultiExchangeFundingClient(settings, concurrent_http)  # type: ignore[arg-type]
    concurrent_client.snapshot_many(symbols, include_history=False)
    concurrent = dict(concurrent_client.last_batch_metrics)
    serial_elapsed = float(serial["elapsed_sec"])
    concurrent_elapsed = float(concurrent["elapsed_sec"])

    print(json.dumps({
        "workload": {
            "symbols": symbol_count,
            "exchanges": len(EXCHANGES),
            "requests": symbol_count * len(EXCHANGES),
            "simulated_latency_ms": args.latency_ms,
        },
        "serial": serial,
        "bounded_concurrent": concurrent,
        "speedup": round(serial_elapsed / concurrent_elapsed, 2) if concurrent_elapsed else 0.0,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
