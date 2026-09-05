"""Reproducible OFFLINE protocol capacity observations; never network throughput."""
from __future__ import annotations

from dataclasses import replace
import json
import time
import tracemalloc

from radars.altcoin_hunter.adapters.base import Route, deterministic_digest
from radars.altcoin_hunter.adapters.binance_protocol import parse_binance_payload
from radars.altcoin_hunter.adapters.fixtures import FIXTURE_TIME_MS, fixture_registry
from radars.altcoin_hunter.connection import run_connection_scenario
from radars.altcoin_hunter.subscription_plan import diff_plans, plan_subscriptions, synthetic_instruments
from radars.altcoin_hunter.rest_budget import FakeCoordinator
from radars.altcoin_hunter.rest_scheduler import OiSamplingPlanner, RestScheduler


def measure_plans(count: int) -> dict:
    records = tuple(synthetic_instruments(count))
    promoted = tuple(record["exchange_symbol"] for record in records[:count // 20])
    tracemalloc.start()
    started = time.perf_counter()
    plan = plan_subscriptions(records, promoted_symbols=promoted)
    plan_elapsed = time.perf_counter() - started
    added = plan_subscriptions(tuple(synthetic_instruments(count + count // 10)), promoted_symbols=promoted, previous=plan)
    remaining = records[:count - count // 10]
    removed = plan_subscriptions(remaining, promoted_symbols=promoted, previous=plan)
    started = time.perf_counter()
    add_diff, remove_diff = diff_plans(plan, added), diff_plans(plan, removed)
    diff_elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert not plan.uncovered_streams
    assert all(len(shard.streams) <= 800 for shard in plan.shards)
    assert not add_diff["moved"] and not remove_diff["moved"]
    stable = {"plan": plan.to_dict(), "added": add_diff, "removed": remove_diff}
    return {"instruments": count, "promoted": len(promoted),
            "market_connections": sum(shard.route == Route.MARKET for shard in plan.shards),
            "public_connections": sum(shard.route == Route.PUBLIC for shard in plan.shards),
            "streams_per_connection": {shard.shard_id: len(shard.streams) for shard in plan.shards},
            "uncovered_instruments": len(plan.coverage["uncovered_agg_trade_symbols"]),
            "plan_seconds": plan_elapsed, "two_plan_diff_seconds": diff_elapsed,
            "add_10_percent_migrations": len(add_diff["moved"]),
            "remove_10_percent_migrations": len(remove_diff["moved"]),
            "peak_python_allocation_bytes": peak, "digest": deterministic_digest(stable)}


def measure_parser(bad_percent: int) -> dict:
    original = fixture_registry()["AAAUSDT"]
    registry, rows = {}, []
    for index in range(2000):
        symbol = f"COIN{index}USDT"
        identity = replace(original.identity, instrument_id=symbol, symbol=symbol, exchange_symbol=symbol)
        registry[symbol] = replace(original, identity=identity)
        row = {"e": "markPriceUpdate", "E": FIXTURE_TIME_MS, "s": symbol,
               "p": "10.0000", "i": "9.9900", "r": "0.0001", "T": FIXTURE_TIME_MS + 3600000, "st": 1}
        if index < 2000 * bad_percent // 100:
            row.pop("p")
        rows.append(row)
    frame = {"stream": "!markPrice@arr", "data": rows}
    tracemalloc.start()
    started = time.perf_counter()
    result = parse_binance_payload(frame, "mark_price", registry, receive_time_ms=FIXTURE_TIME_MS + 100,
                                  receive_monotonic_ns=1000000, route=Route.MARKET)
    elapsed = time.perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rejected = result.diagnostics.get("rejected_count", 0)
    assert rejected == 2000 * bad_percent // 100
    assert len(result.events) == (2000 - rejected) * 2
    assert len(result.rejected_items) <= 64
    return {"items": 2000, "bad_percent": bad_percent, "parsed_events": len(result.events),
            "rejected_items": rejected, "elapsed_seconds": elapsed,
            "events_per_second": len(result.events) / elapsed,
            "rejected_items_per_second": rejected / elapsed,
            "peak_python_allocation_bytes": peak, "rejected_details": len(result.rejected_items),
            "diagnostic_detail_capacity": 64, "digest": deterministic_digest(result.to_dict())}


def measure_oi(count: int = 1500) -> dict:
    clock = [FIXTURE_TIME_MS]
    coordinator = FakeCoordinator()
    scheduler = RestScheduler(clock=lambda: clock[0], coordinator=coordinator)
    planner = OiSamplingPlanner(max_instruments=count)
    planner.update_universe({f"COIN{i}USDT": "HOT" if i < 100 else "NORMAL" for i in range(count)}, clock[0])
    planner.schedule(scheduler, clock[0])
    while due := scheduler.poll_due(clock[0]):
        for request in due:
            completion = scheduler.complete(request, status_code=200, response_time_ms=clock[0])
            assert completion.accepted
            planner.record_completion(scheduler, completion, event_time_ms=clock[0], now_ms=clock[0])
    initial = planner.coverage(clock[0])
    clock[0] += 60000
    refreshed = planner.schedule(scheduler, clock[0])
    result = {"initial": initial, "due_at_60s": len(refreshed),
            "at_60s_before_response": planner.coverage(clock[0]),
            "budget": coordinator.diagnostics(clock[0]), "scheduler": scheduler.diagnostics(clock[0]),
            "completion_model": "synthetic_zero_latency_200_responses_not_real_sampling"}
    for request in refreshed:
        scheduler.cancel(request.request_id)
    result["after_cleanup"] = scheduler.diagnostics(clock[0])
    return result


def run_capacity() -> dict:
    plans = [measure_plans(count) for count in (600, 1000, 1500)]
    parsers = [measure_parser(percent) for percent in (1, 10, 50)]
    reconnect = run_connection_scenario({"name": "reconnect_storm", "reconnects": 100}, seed=42)
    # 499 trades + one global mark stream = ten batches; one lost ACK is exactly 10%.
    loss = run_connection_scenario({"name": "ack_loss", "instruments": 499, "ack_loss_ratio": .1}, seed=42)
    assert reconnect["epoch"] == 101
    assert reconnect["pending_ack_peak"] <= 128
    assert loss["ack_loss_observations"]["actual_ratio"] == .1
    summary = {"plans": plans, "parser": parsers,
               "connection": {"reconnect_epoch": reconnect["epoch"], "reconnect_counts": reconnect["counts"],
                   "pending_ack_peak": max(reconnect["pending_ack_peak"], loss["pending_ack_peak"]),
                   "ack_loss_state": loss["state"], "ack_loss_counts": loss["counts"],
                   "requested_ack_loss_ratio": .1,
                   "ack_loss_observations": loss["ack_loss_observations"],
                   "diagnostics_capacity": loss["diagnostics"]["capacity"],
                   "digest": deterministic_digest({"reconnect": reconnect, "loss": loss})},
               "oi": measure_oi(), "mode": "offline_dry_run", "network_calls": 0,
               "dns_calls": 0, "telegram_calls": 0, "production_file_writes": 0}
    # Timing/allocation intentionally excluded from deterministic fingerprints.
    summary["deterministic_digest"] = deterministic_digest({
        "plans": [record["digest"] for record in plans], "parsers": [record["digest"] for record in parsers],
        "connection": summary["connection"]["digest"], "oi": summary["oi"]})
    return summary


if __name__ == "__main__":
    print(json.dumps(run_capacity(), ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))
