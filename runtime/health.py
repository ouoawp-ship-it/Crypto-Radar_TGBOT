from __future__ import annotations

import json
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from config import Settings
from shared.storage import JsonStore


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
    hourly_enabled = bool(
        settings.consolidation_breakout_enable
        and settings.consolidation_hourly_proximity_enable
        and not bool(payload.get("no_consolidation_breakout"))
    )
    diagnostics = payload.get("diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, dict) else {}
    consolidation = diagnostics.get("consolidation_breakout")
    consolidation = (
        consolidation if isinstance(consolidation, dict) else {}
    )
    hourly = consolidation.get("hourly_proximity")
    hourly = hourly if isinstance(hourly, dict) else {}
    hourly_status = str(hourly.get("status") or "").strip().lower()
    hourly_scan = hourly.get("scan")
    hourly_scan = hourly_scan if isinstance(hourly_scan, dict) else {}
    hourly_scan_status = str(
        hourly_scan.get("status") or ""
    ).strip().lower()
    try:
        hourly_state_exists = (
            settings.consolidation_hourly_proximity_state_path.is_file()
        )
    except OSError:
        hourly_state_exists = False
    if hourly_enabled and hourly_status in {
        "scan_failed",
        "shadow_commit_failed",
        "commit_failed",
    }:
        return _check(
            "runtime_status",
            "fail",
            f"1H箱体临界预警子模块失败：{hourly_status}",
            age_sec=age,
            runtime_state=state,
            hourly_proximity_status=hourly_status,
            hourly_proximity_state_file_exists=hourly_state_exists,
        )
    if state.endswith("_failed"):
        return _check(
            "runtime_status",
            "fail",
            f"主循环报告失败状态：{state}",
            age_sec=age,
            runtime_state=state,
        )
    if hourly_enabled and hourly_scan_status == "degraded":
        return _check(
            "runtime_status",
            "warn",
            "1H箱体临界预警扫描降级，请检查子诊断 errors",
            age_sec=age,
            runtime_state=state,
            hourly_proximity_status=hourly_status or "unknown",
            hourly_proximity_scan_status=hourly_scan_status,
            hourly_proximity_state_file_exists=hourly_state_exists,
        )
    if (
        hourly_enabled
        and hourly_status in {"shadow_idle", "shadow_observed", "live"}
        and not hourly_state_exists
    ):
        return _check(
            "runtime_status",
            "warn",
            "1H箱体临界预警已运行但独立状态文件尚未生成",
            age_sec=age,
            runtime_state=state,
            hourly_proximity_status=hourly_status,
            hourly_proximity_state_file_exists=False,
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


def _altcoin_production_check(
    settings: Settings,
    store: JsonStore,
    now: int,
) -> dict[str, Any]:
    path = settings.altcoin_contract_anomaly_production_status_path
    if not path.exists():
        return _check(
            "altcoin_contract_anomaly_production",
            "fail",
            "山寨合约异动生产状态尚未生成",
        )
    try:
        modified_at = int(path.stat().st_mtime)
    except OSError as exc:
        return _check(
            "altcoin_contract_anomaly_production",
            "fail",
            f"山寨合约异动生产状态无法读取：{type(exc).__name__}",
        )
    payload = store.load(path, {})
    age = max(0, now - modified_at)
    status_max_age = max(
        60,
        int(settings.altcoin_contract_anomaly_production_status_interval_sec) * 3,
    )
    if not isinstance(payload, dict):
        return _check(
            "altcoin_contract_anomaly_production",
            "fail",
            "山寨合约异动生产状态格式无效",
            age_sec=age,
        )
    if age > status_max_age:
        return _check(
            "altcoin_contract_anomaly_production",
            "fail",
            "山寨合约异动生产状态已过期",
            age_sec=age,
            max_age_sec=status_max_age,
        )
    if (
        payload.get("module") != "altcoin_contract_anomaly"
        or payload.get("mode") != "production"
        or payload.get("running") is not True
    ):
        return _check(
            "altcoin_contract_anomaly_production",
            "fail",
            "山寨合约异动生产控制器未处于运行状态",
            age_sec=age,
            state=str(payload.get("status") or "invalid")[:80],
        )
    manifest = payload.get("manifest")
    service = payload.get("service")
    refresh = payload.get("refresh")
    processor = payload.get("processor")
    telegram = payload.get("telegram")
    if not all(
        isinstance(value, dict)
        for value in (manifest, service, refresh, processor, telegram)
    ):
        return _check(
            "altcoin_contract_anomaly_production",
            "fail",
            "山寨合约异动生产状态缺少关键指标",
            age_sec=age,
        )
    manifest_age = manifest.get("age_sec")
    candidate_count = int(manifest.get("candidate_count") or 0)
    manifest_ready = (
        isinstance(manifest_age, (int, float))
        and not isinstance(manifest_age, bool)
        and 0 <= float(manifest_age)
        <= int(settings.altcoin_contract_anomaly_production_manifest_max_age_sec)
        and bool(manifest.get("valid"))
    )
    connected = str(service.get("connection_state") or "") == "connected"
    force_order_ready = bool(service.get("force_order_active"))
    market_data_ready = int(service.get("accepted_events") or 0) > 0
    coverage_ready = bool(service.get("candidate_coverage_complete"))
    process_lock_ready = payload.get("process_lock_acquired") is True
    event_sink_ready = bool(service.get("event_sink_ready"))
    if candidate_count > 0:
        candidate_data_ready = (
            float(service.get("mark_price_data_coverage_ratio") or 0.0) >= 1.0
            and int(service.get("aligned_evaluation_rounds") or 0) > 0
            and int(service.get("last_evaluation_candidate_count") or 0)
            == candidate_count
            and int(service.get("last_evaluation_complete_count") or 0)
            == candidate_count
            and int(service.get("last_evaluation_epoch_complete_count") or 0)
            == candidate_count
            and int(service.get("last_evaluation_funding_complete_count") or 0)
            == candidate_count
        )
    else:
        candidate_data_ready = True
    refresh_successes = int(
        refresh.get("refresh_successes", refresh.get("successes")) or 0
    )
    refresh_ready = (
        refresh_successes > 0
        and bool(refresh.get("running"))
        and not bool(refresh.get("stop_timed_out"))
    )
    evaluation_errors = int(service.get("evaluation_errors") or 0)
    event_sink_failures = int(service.get("event_sink_failures") or 0)
    event_sink_rejections = int(service.get("event_sink_rejections") or 0)
    service_processing_ready = (
        evaluation_errors == 0
        and event_sink_failures == 0
        and event_sink_rejections == 0
    )
    quarantined_batches = int(processor.get("quarantined_batches") or 0)
    quarantined_symbols = int(processor.get("quarantined_symbols") or 0)
    queue_rejections = int(processor.get("queue_rejections") or 0)
    processor_ready = (
        bool(processor.get("running"))
        and not bool(processor.get("stop_timed_out"))
        and quarantined_batches == 0
        and quarantined_symbols == 0
        and queue_rejections == 0
    )
    topic_ready = bool(telegram.get("route_configured"))
    send_enabled = bool(settings.altcoin_contract_anomaly_production_send_enable)
    send_ready = not send_enabled or (
        topic_ready and bool(telegram.get("real_send_enabled"))
        and not str(processor.get("last_error_class") or "")
    )
    healthy = (
        manifest_ready
        and process_lock_ready
        and connected
        and force_order_ready
        and market_data_ready
        and coverage_ready
        and event_sink_ready
        and candidate_data_ready
        and service_processing_ready
        and refresh_ready
        and processor_ready
        and send_ready
    )
    return _check(
        "altcoin_contract_anomaly_production",
        "ok" if healthy else "fail",
        "山寨合约异动生产链路正常"
        if healthy
        else "山寨合约异动生产链路未通过就绪门禁",
        age_sec=age,
        manifest_age_sec=manifest_age,
        candidate_count=candidate_count,
        candidate_coverage_complete=coverage_ready,
        process_lock_acquired=process_lock_ready,
        connection_state=str(service.get("connection_state") or "unknown")[:40],
        force_order_active=force_order_ready,
        accepted_events=int(service.get("accepted_events") or 0),
        event_sink_ready=event_sink_ready,
        evaluation_errors=evaluation_errors,
        event_sink_failures=event_sink_failures,
        event_sink_rejections=event_sink_rejections,
        candidate_data_ready=candidate_data_ready,
        mark_price_data_coverage_ratio=service.get(
            "mark_price_data_coverage_ratio"
        ),
        aligned_evaluation_rounds=int(
            service.get("aligned_evaluation_rounds") or 0
        ),
        complete_candidate_count=int(
            service.get("last_evaluation_complete_count") or 0
        ),
        refresh_successes=refresh_successes,
        refresh_running=bool(refresh.get("running")),
        processor_running=bool(processor.get("running")),
        pending_batches=int(processor.get("pending_batches") or 0),
        quarantined_batches=quarantined_batches,
        quarantined_symbols=quarantined_symbols,
        queue_rejections=queue_rejections,
        telegram_route_configured=topic_ready,
        real_send_enabled=bool(telegram.get("real_send_enabled")),
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


def _pulse_review_check(
    settings: Settings,
    store: JsonStore,
    now: int,
) -> dict[str, Any]:
    if not settings.pulse_radar_enable:
        return _check("pulse_reviews", "ok", "脉冲雷达未启用")
    path = settings.data_dir / "review_signals.json"
    if not path.exists():
        return _check("pulse_reviews", "warn", "脉冲复盘记录尚未生成")
    records = store.load(path, [])
    if not isinstance(records, list):
        return _check("pulse_reviews", "warn", "脉冲复盘记录格式无效")

    completed = 0
    overdue = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        windows = (
            (7200,)
            if str(record.get("radar") or "alert") == "divergence"
            else (3600, 14400)
        )
        outcomes = record.get("outcomes") or {}
        if not isinstance(outcomes, dict):
            outcomes = {}
        present = {
            int(key)
            for key in outcomes
            if str(key).isdigit()
        }
        if all(window in present for window in windows):
            completed += 1
        ts = int(record.get("ts") or 0)
        overdue += sum(
            1
            for window in windows
            if ts + window + 1800 <= now and window not in present
        )

    metrics = {
        "records": len(records),
        "completed": completed,
        "overdue_windows": overdue,
    }
    if overdue:
        return _check(
            "pulse_reviews",
            "warn",
            f"存在 {overdue} 个到期窗口超过30分钟仍未回填",
            **metrics,
        )
    return _check("pulse_reviews", "ok", "脉冲复盘运行正常", **metrics)


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
        _market_snapshot_check(settings, now),
        _realtime_check(settings, now),
        _signal_effectiveness_check(settings, now),
        _pulse_review_check(settings, store, now),
        _database_backup_check(settings, now),
        _disk_check(settings),
    ]
    runtime = store.load(settings.runtime_status_path, {})
    upstream = runtime.get("upstream_sources") if isinstance(runtime, dict) else None
    if isinstance(upstream, dict) and upstream.get("status") == "degraded":
        checks.append(_check("upstream_sources", "warn", "上游接口最近一次观测存在降级", snapshot=upstream))
    if settings.altcoin_contract_anomaly_production_enable:
        checks.append(_altcoin_production_check(settings, store, now))
    return checks


def lightweight_freshness_checks(
    settings: Settings,
    *,
    now_ts: int | None = None,
) -> list[dict[str, Any]]:
    """Read only the two freshness sources used by private fault alerts."""

    now = int(now_ts or time.time())
    return [
        _market_snapshot_check(settings, now),
        _realtime_check(settings, now),
    ]


__all__ = ["lightweight_freshness_checks", "runtime_health_checks"]
