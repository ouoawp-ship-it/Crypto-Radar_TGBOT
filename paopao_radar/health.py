from __future__ import annotations

import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .config import Settings
from .storage import JsonStore


def _check(name: str, status: str, detail: str, **metrics: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"name": name, "status": status, "detail": detail}
    if metrics:
        item["metrics"] = metrics
    return item


def _quick_check(name: str, path: Path) -> dict[str, Any]:
    if not path.exists():
        return _check(name, "warn", f"{path.name} 尚未生成")
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            result = str(conn.execute("PRAGMA quick_check").fetchone()[0])
    except (OSError, sqlite3.Error) as exc:
        return _check(name, "fail", f"{path.name} 无法校验：{type(exc).__name__}")
    if result != "ok":
        return _check(name, "fail", f"{path.name} 完整性异常：{result[:120]}")
    return _check(name, "ok", f"{path.name} 完整性正常")


def _scalar(path: Path, sql: str) -> tuple[Any, ...]:
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
        row = conn.execute(sql).fetchone()
    return tuple(row or ())


def _runtime_check(settings: Settings, store: JsonStore, now: int) -> dict[str, Any]:
    path = settings.runtime_status_path
    if not path.exists():
        return _check("runtime_status", "warn", "主循环状态尚未生成")
    try:
        modified_at = int(path.stat().st_mtime)
    except OSError as exc:
        return _check("runtime_status", "fail", f"主循环状态无法读取：{type(exc).__name__}")
    payload = store.load(path, {})
    age = max(0, now - modified_at)
    max_age = max(60, int(settings.health_runtime_max_age_sec))
    state = str(payload.get("status") or "unknown") if isinstance(payload, dict) else "invalid"
    if not isinstance(payload, dict):
        return _check("runtime_status", "fail", "主循环状态格式无效", age_sec=age)
    if age > max_age:
        return _check(
            "runtime_status",
            "fail",
            f"主循环状态已过期：{age}s > {max_age}s",
            age_sec=age,
            max_age_sec=max_age,
            runtime_state=state,
        )
    if state.endswith("_failed"):
        return _check(
            "runtime_status",
            "fail",
            f"主循环报告失败状态：{state}",
            age_sec=age,
            runtime_state=state,
        )
    return _check(
        "runtime_status",
        "ok",
        f"主循环状态新鲜：{age}s",
        age_sec=age,
        max_age_sec=max_age,
        runtime_state=state,
    )


def _market_snapshot_check(settings: Settings, now: int) -> dict[str, Any]:
    path = settings.market_snapshots_db_path
    if not path.exists():
        return _check("market_snapshots_freshness", "warn", "市场快照尚未生成")
    try:
        total, latest = _scalar(
            path,
            "SELECT COUNT(*), MAX(observed_at) FROM market_snapshots",
        )
    except sqlite3.Error as exc:
        return _check("market_snapshots_freshness", "fail", f"市场快照无法读取：{type(exc).__name__}")
    if not int(total or 0) or not int(latest or 0):
        return _check("market_snapshots_freshness", "warn", "市场快照为空")
    age = max(0, now - int(latest))
    budget = max(900, int(settings.market_snapshot_interval_sec) * 3)
    status = "ok" if age <= budget else "fail"
    return _check(
        "market_snapshots_freshness",
        status,
        f"市场快照年龄 {age}s（上限 {budget}s）",
        age_sec=age,
        max_age_sec=budget,
        rows=int(total),
    )


def _realtime_check(settings: Settings, now: int) -> dict[str, Any]:
    path = settings.realtime_features_db_path
    if not path.exists():
        return _check("realtime_features_freshness", "warn", "实时行情尚未生成")
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            rows = conn.execute(
                "SELECT exchange, COUNT(*), COUNT(DISTINCT symbol), "
                "MAX(bucket_start + bucket_sec) FROM realtime_market_features GROUP BY exchange"
            ).fetchall()
    except sqlite3.Error as exc:
        return _check("realtime_features_freshness", "fail", f"实时行情无法读取：{type(exc).__name__}")
    if not rows:
        return _check("realtime_features_freshness", "warn", "实时行情为空")
    expected = {"binance"}
    if settings.realtime_bybit_enable:
        expected.add("bybit")
    if settings.realtime_okx_enable:
        expected.add("okx")
    fresh_sec = max(60, int(settings.health_realtime_fresh_sec))
    exchanges: dict[str, dict[str, int | str]] = {}
    stale: list[str] = []
    for exchange, count, symbols, latest in rows:
        key = str(exchange)
        age = max(0, now - int(latest or 0)) if latest else fresh_sec + 1
        exchanges[key] = {
            "status": "ready" if age <= fresh_sec else "stale",
            "age_sec": age,
            "rows": int(count or 0),
            "symbols": int(symbols or 0),
        }
        if key in expected and age > fresh_sec:
            stale.append(key)
    missing = sorted(expected - set(exchanges))
    if missing or stale:
        detail = "；".join(filter(None, [
            f"缺少 {','.join(missing)}" if missing else "",
            f"过期 {','.join(sorted(stale))}" if stale else "",
        ]))
        return _check(
            "realtime_features_freshness",
            "fail",
            f"实时交易所数据异常：{detail}",
            fresh_sec=fresh_sec,
            exchanges=exchanges,
        )
    return _check(
        "realtime_features_freshness",
        "ok",
        "实时交易所数据均在新鲜度范围内",
        fresh_sec=fresh_sec,
        exchanges=exchanges,
    )


def _disk_check(settings: Settings) -> dict[str, Any]:
    target = settings.data_dir if settings.data_dir.exists() else settings.base_dir
    free_mb = int(shutil.disk_usage(target).free / 1024 / 1024)
    fail_mb = max(64, int(settings.health_disk_fail_mb))
    warn_mb = max(fail_mb, int(settings.health_disk_warn_mb))
    status = "fail" if free_mb < fail_mb else "warn" if free_mb < warn_mb else "ok"
    return _check(
        "disk_space",
        status,
        f"数据盘剩余 {free_mb} MiB",
        free_mb=free_mb,
        warn_mb=warn_mb,
        fail_mb=fail_mb,
    )


def _derivatives_provider_check(settings: Settings) -> dict[str, Any]:
    missing_keys: list[str] = []
    if settings.coinglass_enable and not settings.coinglass_api_key:
        missing_keys.append("CoinGlass")
    if settings.coinalyze_enable and not settings.coinalyze_api_key:
        missing_keys.append("Coinalyze")
    if missing_keys:
        return _check(
            "derivatives_validation",
            "fail",
            f"已启用但缺少 API Key：{', '.join(missing_keys)}",
        )

    active = [
        name for name, ready in (
            ("CoinGlass", settings.coinglass_enable and bool(settings.coinglass_api_key)),
            ("Coinalyze", settings.coinalyze_enable and bool(settings.coinalyze_api_key)),
        )
        if ready
    ]
    if len(active) == 2:
        return _check(
            "derivatives_validation",
            "ok",
            "CoinGlass 主源与 Coinalyze 校验源均已配置",
            active_sources=active,
        )
    if len(active) == 1:
        return _check(
            "derivatives_validation",
            "warn",
            f"当前仅启用 {active[0]}，多源一致性将降级运行",
            active_sources=active,
        )
    return _check(
        "derivatives_validation",
        "warn",
        "衍生品外部校验源未启用，继续使用交易所原生数据",
        active_sources=[],
    )


def runtime_health_checks(
    settings: Settings,
    store: JsonStore,
    *,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    now = int(now_ts or time.time())
    checks = [
        _runtime_check(settings, store, now),
        _quick_check("signal_store_integrity", settings.signal_events_db_path),
        _quick_check("market_snapshots_integrity", settings.market_snapshots_db_path),
        _quick_check("realtime_features_integrity", settings.realtime_features_db_path),
        _quick_check("news_store_integrity", settings.news_events_db_path),
        _market_snapshot_check(settings, now),
        _realtime_check(settings, now),
        _derivatives_provider_check(settings),
        _disk_check(settings),
    ]
    runtime = store.load(settings.runtime_status_path, {})
    upstream = runtime.get("upstream_sources") if isinstance(runtime, dict) else None
    if isinstance(upstream, dict) and upstream.get("status") == "degraded":
        checks.append(_check("upstream_sources", "warn", "上游接口最近一次观测存在降级", snapshot=upstream))
    return checks


__all__ = ["runtime_health_checks"]
