from __future__ import annotations

import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from .config import Settings
from .launch_lifecycle import LaunchLifecycleStore, OUTCOME_EVALUATION_VERSION
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


def _database_backup_check(settings: Settings, now: int) -> dict[str, Any]:
    backup_root = settings.database_backup_dir
    if not backup_root.exists():
        return _check("database_backup", "warn", "数据库备份尚未生成")
    manifests = sorted(backup_root.glob("*/manifest.json"), reverse=True)
    if not manifests:
        return _check("database_backup", "warn", "数据库备份清单尚未生成")
    manifest_path = manifests[0]
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return _check("database_backup", "warn", f"最新数据库备份清单无法读取：{type(exc).__name__}")
    databases = manifest.get("databases") if isinstance(manifest, dict) else None
    created_at = int(manifest.get("created_at") or 0) if isinstance(manifest, dict) else 0
    age_sec = max(0, now - created_at) if created_at else None
    if not isinstance(databases, list) or not databases:
        return _check("database_backup", "warn", "最新数据库备份不包含可恢复数据库")
    invalid = [
        str(item.get("backup") or "")
        for item in databases
        if not isinstance(item, dict)
        or str(item.get("integrity") or "").lower() != "ok"
        or str(item.get("restore_verification") or "").lower() != "ok"
    ]
    if invalid:
        return _check(
            "database_backup",
            "warn",
            "最新数据库备份未通过恢复验证",
            invalid=invalid,
            age_sec=age_sec,
        )
    max_age_sec = max(3600, int(settings.health_database_backup_max_age_sec))
    if age_sec is None or age_sec > max_age_sec:
        return _check(
            "database_backup",
            "warn",
            "最新数据库备份已超过新鲜度上限",
            age_sec=age_sec,
            max_age_sec=max_age_sec,
            databases=len(databases),
        )
    return _check(
        "database_backup",
        "ok",
        "最新数据库备份及恢复验证正常",
        age_sec=age_sec,
        max_age_sec=max_age_sec,
        databases=len(databases),
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


def _signal_effectiveness_check(settings: Settings, now: int) -> dict[str, Any]:
    path = settings.signal_events_db_path
    if not path.exists():
        return _check("signal_effectiveness", "warn", "信号结果库尚未生成")
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'signal_outcomes'"
            ).fetchone()
            if table is None:
                return _check("signal_effectiveness", "warn", "P2 信号结果表尚未初始化")
            total, matured, pending, unavailable, latest, overdue = conn.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN status = 'matured' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END),
                       SUM(CASE WHEN status = 'unavailable' THEN 1 ELSE 0 END),
                       MAX(evaluated_at),
                       SUM(CASE WHEN status = 'pending' AND due_at < ? THEN 1 ELSE 0 END)
                FROM signal_outcomes
                """,
                (now - 30 * 60,),
            ).fetchone()
    except sqlite3.Error as exc:
        return _check("signal_effectiveness", "warn", f"信号结果统计无法读取：{type(exc).__name__}")

    total_count = int(total or 0)
    metrics = {
        "total": total_count,
        "matured": int(matured or 0),
        "pending": int(pending or 0),
        "unavailable": int(unavailable or 0),
        "overdue_pending": int(overdue or 0),
        "last_evaluated_age_sec": max(0, now - int(latest)) if latest else None,
    }
    if total_count <= 0:
        return _check("signal_effectiveness", "warn", "P2 结果追踪已初始化，正在等待可评估信号", **metrics)
    if metrics["overdue_pending"]:
        return _check(
            "signal_effectiveness",
            "warn",
            f"存在 {metrics['overdue_pending']} 条到期结果超过 30 分钟仍未回填",
            **metrics,
        )
    return _check("signal_effectiveness", "ok", "P2 信号结果追踪运行正常", **metrics)


def _launch_outcome_check(settings: Settings) -> dict[str, Any]:
    if not settings.launch_outcome_v2_enable:
        return _check("launch_outcomes", "ok", "P2.4 启动周期结果评估未启用")
    if not settings.launch_lifecycle_v2_enable:
        return _check(
            "launch_outcomes",
            "fail",
            "P2.4 已启用，但 LAUNCH_LIFECYCLE_V2_ENABLE 未启用",
        )
    path = settings.signal_events_db_path
    if not path.exists():
        return _check("launch_outcomes", "warn", "P2.4 启动周期结果库尚未生成")
    store = LaunchLifecycleStore(
        path,
        watch_score=settings.launch_watch_score,
        start_score=settings.launch_min_score_push,
        invalid_windows_required=settings.launch_lifecycle_invalid_windows,
        outcome_enabled=True,
        outcome_follow_through_pct=settings.launch_outcome_follow_through_pct,
        outcome_min_samples=settings.launch_outcome_min_samples,
        breakout_score=settings.launch_breakout_score,
        launched_score=settings.launch_launched_score,
    )
    try:
        uri = f"file:{path.resolve().as_posix()}?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as conn:
            table = conn.execute(
                """
                SELECT 1 FROM sqlite_master
                WHERE type = 'table' AND name = 'launch_lifecycle_outcomes'
                """
            ).fetchone()
            if table is None:
                return _check("launch_outcomes", "warn", "P2.4 结果表尚未初始化")
            completed, evaluated, backlog = conn.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM launch_lifecycle_cycles WHERE status = 'failed'),
                    (SELECT COUNT(*) FROM launch_lifecycle_outcomes WHERE rule_key = ?),
                    (
                        SELECT COUNT(*)
                        FROM launch_lifecycle_cycles AS cycle
                        LEFT JOIN launch_lifecycle_outcomes AS outcome
                          ON outcome.cycle_id = cycle.id
                         AND outcome.evaluation_version = ?
                         AND outcome.rule_key = cycle.outcome_rule_key
                        WHERE cycle.status = 'failed'
                          AND (
                            cycle.outcome_rule_key = ''
                            OR outcome.cycle_id IS NULL
                          )
                    )
                """,
                (
                    store.outcome_rule_key,
                    OUTCOME_EVALUATION_VERSION,
                ),
            ).fetchone()
    except sqlite3.Error as exc:
        return _check(
            "launch_outcomes",
            "warn",
            f"P2.4 启动周期结果无法读取：{type(exc).__name__}",
        )
    metrics = {
        "completed_cycles": int(completed or 0),
        "same_rule_samples": int(evaluated or 0),
        "evaluation_backlog": int(backlog or 0),
        "minimum_samples": store.outcome_min_samples,
        "rates_available": int(evaluated or 0) >= store.outcome_min_samples,
    }
    if metrics["evaluation_backlog"]:
        return _check(
            "launch_outcomes",
            "warn",
            f"存在 {metrics['evaluation_backlog']} 个已结束周期尚未按当前口径评估",
            **metrics,
        )
    return _check(
        "launch_outcomes",
        "ok",
        "P2.4 启动周期结果评估正常",
        **metrics,
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
        _signal_effectiveness_check(settings, now),
        _launch_outcome_check(settings),
        _database_backup_check(settings, now),
        _disk_check(settings),
    ]
    runtime = store.load(settings.runtime_status_path, {})
    upstream = runtime.get("upstream_sources") if isinstance(runtime, dict) else None
    if isinstance(upstream, dict) and upstream.get("status") == "degraded":
        checks.append(_check("upstream_sources", "warn", "上游接口最近一次观测存在降级", snapshot=upstream))
    return checks


__all__ = ["runtime_health_checks"]
