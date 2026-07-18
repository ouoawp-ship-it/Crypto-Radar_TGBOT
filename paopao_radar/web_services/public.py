from __future__ import annotations

import re
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..atomic_json import locked_read_json, locked_write_json
from ..coin_evidence import (
    build_kline_chart,
    build_snapshot_series,
    normalize_chart_interval,
    normalize_chart_market,
)
from ..agent_intelligence import build_agent_overview
from ..config import Settings
from ..data_sources import BinanceDataSource, UPSTREAM_SOURCE_METRICS
from ..data_source_registry import data_source_registry_payload
from ..market_cockpit import (
    MarketSnapshotStore,
    collect_binance_market_rows,
    load_market_cockpit,
    load_market_cockpit_windows,
    normalize_window,
)
from ..market_funds import build_funds_assets, build_funds_sectors, normalize_market_type
from ..news_intelligence import NEWS_SCHEMA_VERSION, NewsEventStore, ingest_binance_announcements
from ..realtime_market import RealtimeFeatureStore, build_realtime_radar_boards
from ..realtime_intelligence import (
    REALTIME_INTELLIGENCE_SCHEMA_VERSION,
    build_open_interest_anomaly_events,
    build_realtime_intelligence,
    build_realtime_intelligence_radar_boards,
)
from ..runtime_cache import get_or_set as runtime_cache_get_or_set
from ..runtime_cache import invalidate as invalidate_runtime_cache
from ..runtime_cache import stats as runtime_cache_stats
from ..signal_intelligence import build_radar_intelligence
from ..signal_store import SignalEventStore
from ..symbol_dossier import current_market_snapshot
from ..workstation_funds import collect_cross_exchange_open_interest
from ..web_observability import PUBLIC_API_LIMITER, PUBLIC_API_METRICS, PUBLIC_STREAM_METRICS, PUBLIC_TELEMETRY
from .api_core import api_error, api_ok, normalize_symbol_filter, redact_api_payload
from .signals import enhance_signal_item, signal_display, signal_detail_view, signal_stats_display


FORBIDDEN_PUBLIC_KEYS = {
    "dedup_key",
    "topic_id",
    "message_ids",
    "message_ids_json",
    "reply_to_message_id",
    "payload",
    "payload_json",
    "raw",
    "text_html",
    "config",
    "jobs",
    "logs",
    "audit",
    "service",
    "services",
    "token",
    "chat_id",
    "api_key",
    "secret",
    "authorization",
    "cookie",
    "telegram",
    "bot_token",
}

PUBLIC_CONTEXT_SCHEMA_VERSION = "2026-07-17"
PUBLIC_SNAPSHOT_TTL_SEC = 30
PUBLIC_SNAPSHOT_MAX_STALE_SEC = 300
PUBLIC_INTELLIGENCE_TTL_SEC = 15
PUBLIC_INTELLIGENCE_REFRESH_SEC = 60
PUBLIC_INTELLIGENCE_MAX_STALE_SEC = 900
PUBLIC_INTELLIGENCE_BOARD_LIMIT = 14
PUBLIC_MARKET_COCKPIT_TTL_SEC = 15
PUBLIC_FUNDS_TTL_SEC = 30
PUBLIC_INFO_TTL_SEC = 60
PUBLIC_AGENTS_TTL_SEC = 120
PUBLIC_INTELLIGENCE_RESPONSE_LIMIT = 40
PUBLIC_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")
PUBLIC_SIGNAL_REF_RE = re.compile(r"^(?:[0-9]{1,12}|sig_[a-f0-9]{20})$")
WORKSTATION_RADAR_WINDOWS = {
    "15m": 900,
    "30m": 1800,
    "1h": 3600,
    "4h": 14400,
    "1d": 86400,
}
SnapshotLoader = Callable[[Settings, str], dict[str, Any]]
ChartLoader = Callable[[Settings, str, str, str, int], list[list[Any]]]
_MARKET_WARMUP_LOCK = threading.Lock()
_MARKET_WARMUPS: set[str] = set()
_MARKET_WARMUP_NEXT_ALLOWED: dict[str, float] = {}
_NEWS_REFRESH_LOCK = threading.Lock()
_NEWS_REFRESHES: set[str] = set()
_NEWS_REFRESH_NEXT_ALLOWED: dict[str, float] = {}
_NEWS_REFRESH_FAILURES: dict[str, str] = {}
_INTELLIGENCE_SNAPSHOT_LOCK = threading.Lock()
_INTELLIGENCE_SNAPSHOTS: dict[str, tuple[float, dict[str, Any]]] = {}
_INTELLIGENCE_REFRESHES: set[str] = set()


def _v2_disabled(settings: Settings) -> bool:
    return str(settings.cockpit_v2_mode or "enabled").strip().lower() == "disabled"


def _v2_disabled_payload() -> dict[str, Any]:
    return api_error("V2 驾驶舱当前已通过回滚开关停用", code="feature_disabled")


def _store(settings: Settings | None = None) -> SignalEventStore:
    loaded = settings or Settings.load()
    return SignalEventStore(loaded.signal_events_db_path)


def _short(value: Any, limit: int = 260) -> str:
    text = str(redact_api_payload(value if value is not None else ""))
    return text[: max(0, int(limit))]


def _strip_forbidden(value: Any) -> Any:
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text in FORBIDDEN_PUBLIC_KEYS:
                continue
            if any(secret in key_text.lower() for secret in ("token", "api_key", "apikey", "secret", "password")):
                continue
            clean[key_text] = _strip_forbidden(item)
        return clean
    if isinstance(value, list):
        return [_strip_forbidden(item) for item in value]
    redacted = redact_api_payload(value)
    if isinstance(redacted, str):
        if any(marker in redacted.lower() for marker in ("authorization", "cookie")):
            return "<redacted:sensitive-line>"
        for marker in ("WEB_ADMIN_TOKEN", "BOT_TOKEN", "TELEGRAM", "Telegram", "Authorization", "Cookie"):
            redacted = redacted.replace(marker, "<redacted>")
    return redacted


def public_signal_item(item: dict[str, Any]) -> dict[str, Any]:
    enhanced = enhance_signal_item(item)
    source_display = dict(enhanced.get("display") or signal_display(enhanced))
    display_limits = {
        "title": 120,
        "module_label": 48,
        "status_label": 48,
        "symbol_label": 32,
        "time_label": 32,
        "score_label": 24,
        "stage_label": 48,
        "summary": 180,
        "card_tone": 16,
    }
    display = {
        key: _short(source_display.get(key), limit)
        for key, limit in display_limits.items()
        if source_display.get(key) not in (None, "")
    }
    display["summary"] = _short(display.get("summary") or enhanced.get("excerpt") or "", 180)
    public = {
        "id": enhanced.get("id"),
        "public_ref": enhanced.get("public_ref") or "",
        "time": enhanced.get("time") or "",
        "module": enhanced.get("module") or "",
        "symbol": enhanced.get("symbol") or "",
        "status": enhanced.get("status") or "",
        "signal_type": _short(enhanced.get("signal_type") or "", 80),
        "score": enhanced.get("score"),
        "stage": _short(enhanced.get("stage") or "", 48),
        "excerpt": _short(enhanced.get("excerpt") or enhanced.get("title") or "", 180),
        "display": display,
    }
    return _strip_forbidden(public)


def public_stream_batch(
    last_signal_id: int = 0,
    *,
    limit: int = 50,
    settings: Settings | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    rows = _store(loaded).events_after_id(last_signal_id, limit=limit)
    items = [public_signal_item(row) for row in rows]
    cursor = max([max(0, int(last_signal_id or 0)), *[int(item.get("id") or 0) for item in items]])
    return {
        "schema_version": PUBLIC_CONTEXT_SCHEMA_VERSION,
        "generated_at": _utc_time_text(int(time.time())),
        "cursor": cursor,
        "items": items,
        "count": len(items),
    }


def _public_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [public_signal_item(item) for item in items]


def _utc_time_text(value: int | float) -> str:
    if float(value or 0) <= 0:
        return ""
    return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> int | float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return int(number) if number.is_integer() else number


def _safe_symbol(value: Any) -> dict[str, str]:
    normalized = normalize_symbol_filter(value)
    symbol = str(normalized.get("symbol") or "")
    coin = str(normalized.get("coin") or "")
    if not symbol or not coin or not PUBLIC_SYMBOL_RE.fullmatch(symbol):
        return {"symbol": "", "coin": ""}
    return {"symbol": symbol, "coin": coin}


def _snapshot_state(snapshot: dict[str, Any], *, now_ts: int) -> tuple[str, int]:
    updated_at = int(snapshot.get("updated_at") or 0)
    age_sec = max(0, int(now_ts) - updated_at) if updated_at > 0 else PUBLIC_SNAPSHOT_MAX_STALE_SEC + 1
    price = _number(snapshot.get("price"))
    quote_volume = _number(snapshot.get("quote_volume"))
    oi_value = _number(snapshot.get("oi_value"))
    funding_pct = _number(snapshot.get("funding_pct"))
    available = sum((
        price is not None and float(price) > 0,
        quote_volume is not None and float(quote_volume) > 0,
        oi_value is not None and float(oi_value) > 0,
        funding_pct is not None,
    ))
    if available == 0:
        return "unavailable", age_sec
    if age_sec > PUBLIC_SNAPSHOT_MAX_STALE_SEC:
        return "stale", age_sec
    if price is None or float(price) <= 0 or available < 3:
        return "degraded", age_sec
    return "fresh", age_sec


def _metric(
    snapshot: dict[str, Any],
    key: str,
    *,
    unit: str,
    source: str,
    observed_at: str,
    age_sec: int,
    snapshot_status: str,
    quality: str = "direct",
    zero_is_missing: bool = False,
) -> dict[str, Any]:
    value = _number(snapshot.get(key))
    available = value is not None and not (zero_is_missing and float(value) == 0)
    status = snapshot_status if available else "unavailable"
    return {
        "value": value if available else None,
        "unit": unit,
        "source": source,
        "observed_at": observed_at,
        "age_sec": age_sec,
        "status": status,
        "quality": quality if available else "missing",
    }


def _public_funding_rows(value: Any) -> list[dict[str, Any]]:
    rows = value if isinstance(value, list) else []
    safe_rows: list[dict[str, Any]] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        safe_rows.append({
            "exchange": _short(row.get("exchange") or "", 30),
            "funding_pct": _number(row.get("funding_pct")),
            "interval_hours": int(_number(row.get("interval_hours")) or 0),
            "last_funding_time": _short(row.get("last_funding_time") or "", 40),
            "next_funding_time": _short(row.get("next_funding_time") or "", 40),
            "extreme_label": _short(row.get("extreme_label") or "", 30),
        })
    return safe_rows


def public_market_snapshot_view(snapshot: dict[str, Any], *, now_ts: int | None = None) -> dict[str, Any]:
    now = int(now_ts or time.time())
    status, age_sec = _snapshot_state(snapshot, now_ts=now)
    updated_at = int(snapshot.get("updated_at") or 0)
    observed_at = _utc_time_text(updated_at)
    market_cap_source = _short(snapshot.get("market_cap_source") or "", 40) or "aggregated"
    metrics = {
        "price": _metric(snapshot, "price", unit="usd", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, zero_is_missing=True),
        "price_15m_pct": _metric(snapshot, "price_15m_pct", unit="percent", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, quality="derived"),
        "price_1h_pct": _metric(snapshot, "price_1h_pct", unit="percent", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, quality="derived"),
        "price_4h_pct": _metric(snapshot, "price_4h_pct", unit="percent", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, quality="derived"),
        "price_24h_pct": _metric(snapshot, "price_24h_pct", unit="percent", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status),
        "quote_volume": _metric(snapshot, "quote_volume", unit="usd", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, zero_is_missing=True),
        "volume_ratio": _metric(snapshot, "volume_ratio", unit="ratio", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, quality="derived"),
        "oi_value": _metric(snapshot, "oi_value", unit="usd", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, zero_is_missing=True),
        "oi_15m_pct": _metric(snapshot, "oi_15m_pct", unit="percent", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, quality="derived"),
        "oi_1h_pct": _metric(snapshot, "oi_1h_pct", unit="percent", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, quality="derived"),
        "oi_4h_pct": _metric(snapshot, "oi_4h_pct", unit="percent", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status, quality="derived"),
        "funding_pct": _metric(snapshot, "funding_pct", unit="percent_per_cycle", source="binance_futures", observed_at=observed_at, age_sec=age_sec, snapshot_status=status),
        "market_cap": _metric(snapshot, "market_cap", unit="usd", source=market_cap_source, observed_at=observed_at, age_sec=age_sec, snapshot_status=status, zero_is_missing=True),
    }
    return _strip_forbidden({
        "schema_version": PUBLIC_CONTEXT_SCHEMA_VERSION,
        "symbol": str(snapshot.get("symbol") or ""),
        "coin": str(snapshot.get("coin") or ""),
        "status": status,
        "updated_at": observed_at,
        "age_sec": age_sec,
        "metrics": metrics,
        "funding_exchanges": _public_funding_rows(snapshot.get("funding_exchanges")),
        "tiers": {
            "market_cap": _short(snapshot.get("market_cap_tier") or "", 40),
            "liquidity": _short(snapshot.get("liquidity_tier") or "", 40),
        },
    })


def _load_public_snapshot(
    settings: Settings,
    symbol: str,
    *,
    snapshot_loader: SnapshotLoader | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loader = snapshot_loader or current_market_snapshot
    if snapshot_loader is None:
        snapshot = runtime_cache_get_or_set(
            f"public:market-snapshot:{symbol}",
            PUBLIC_SNAPSHOT_TTL_SEC,
            lambda: loader(settings, symbol),
        )
    else:
        snapshot = loader(settings, symbol)
    if not isinstance(snapshot, dict):
        raise ValueError("市场快照格式无效")
    return public_market_snapshot_view(snapshot, now_ts=now_ts)


def public_market_snapshot_payload(
    symbol: str,
    *,
    settings: Settings | None = None,
    snapshot_loader: SnapshotLoader | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    normalized = _safe_symbol(symbol)
    if not normalized["symbol"]:
        return api_error("币种格式无效", code="invalid_symbol")
    loaded = settings or Settings.load()
    try:
        snapshot = _load_public_snapshot(
            loaded,
            normalized["symbol"],
            snapshot_loader=snapshot_loader,
            now_ts=now_ts,
        )
    except Exception:
        return api_error("市场快照暂时不可用", code="upstream_unavailable")
    return api_ok(snapshot, message="已读取市场快照")


def _market_cockpit_raw(
    settings: Settings,
    *,
    window_sec: int,
    board_limit: int,
    now_ts: int | None = None,
) -> dict[str, Any]:
    safe_window = normalize_window(window_sec)
    safe_limit = max(3, min(20, int(board_limit or 8)))

    def load() -> dict[str, Any]:
        payload = load_market_cockpit(
            settings,
            window_sec=safe_window,
            board_limit=safe_limit,
            now_ts=now_ts,
            live_rows=[],
        )
        if now_ts is None and str(payload.get("data_status") or "") in {"empty", "warming_up", "partial", "stale"}:
            _schedule_market_warmup(settings)
            if not payload.get("assets"):
                warnings = list(payload.get("warnings") or [])
                warnings.insert(0, "市场快照正在后台预热，稍后刷新即可看到榜单。")
                payload["warnings"] = list(dict.fromkeys(warnings))
        return payload

    if now_ts is not None:
        return load()
    cache_key = f"public:market-cockpit:{settings.market_snapshots_db_path}:{safe_window}:{safe_limit}"
    return runtime_cache_get_or_set(cache_key, PUBLIC_MARKET_COCKPIT_TTL_SEC, load)


def _schedule_market_warmup(settings: Settings) -> bool:
    key = str(settings.market_snapshots_db_path)
    interval = max(60, int(settings.market_snapshot_interval_sec))
    now_wall = int(time.time())
    now_mono = time.monotonic()
    latest = MarketSnapshotStore(settings.market_snapshots_db_path).latest_timestamp("binance_futures_batch")

    with _MARKET_WARMUP_LOCK:
        if key in _MARKET_WARMUPS or now_mono < _MARKET_WARMUP_NEXT_ALLOWED.get(key, 0):
            return False
        if latest and now_wall - latest < interval:
            _MARKET_WARMUP_NEXT_ALLOWED[key] = now_mono + (interval - (now_wall - latest))
            return False
        _MARKET_WARMUPS.add(key)

    def worker() -> None:
        cooldown = 60.0
        source: BinanceDataSource | None = None
        try:
            source = BinanceDataSource(settings)
            rows = collect_binance_market_rows(settings, source=source, oi_source=source)
            count = MarketSnapshotStore(settings.market_snapshots_db_path).append_many(rows)
            if count:
                cooldown = float(interval)
                invalidate_runtime_cache("public:")
        except Exception as exc:
            print(f"[public-market] background warmup failed {type(exc).__name__}: {exc}", file=sys.stderr)
        finally:
            try:
                if source is not None:
                    source.http.close()
            except Exception:
                pass
            with _MARKET_WARMUP_LOCK:
                _MARKET_WARMUPS.discard(key)
                _MARKET_WARMUP_NEXT_ALLOWED[key] = time.monotonic() + cooldown

    thread = threading.Thread(target=worker, name="public-market-warmup", daemon=True)
    try:
        thread.start()
    except RuntimeError:
        with _MARKET_WARMUP_LOCK:
            _MARKET_WARMUPS.discard(key)
            _MARKET_WARMUP_NEXT_ALLOWED[key] = time.monotonic() + 60
        return False
    return True


def _schedule_news_refresh(settings: Settings) -> bool:
    key = str(settings.news_events_db_path)
    now_wall = int(time.time())
    now_mono = time.monotonic()
    latest = NewsEventStore(settings.news_events_db_path).latest_collected_at()

    with _NEWS_REFRESH_LOCK:
        if key in _NEWS_REFRESHES or now_mono < _NEWS_REFRESH_NEXT_ALLOWED.get(key, 0):
            return False
        if latest and now_wall - latest < PUBLIC_INFO_TTL_SEC:
            _NEWS_REFRESH_NEXT_ALLOWED[key] = now_mono + (PUBLIC_INFO_TTL_SEC - (now_wall - latest))
            return False
        _NEWS_REFRESHES.add(key)

    def worker() -> None:
        failure = ""
        try:
            ingest_binance_announcements(settings, max_pages=1, now_ts=int(time.time()))
            invalidate_runtime_cache("public:info")
        except Exception as exc:
            failure = type(exc).__name__
            print(f"[public-info] background refresh failed {failure}: {exc}", file=sys.stderr)
        finally:
            with _NEWS_REFRESH_LOCK:
                _NEWS_REFRESHES.discard(key)
                _NEWS_REFRESH_NEXT_ALLOWED[key] = time.monotonic() + PUBLIC_INFO_TTL_SEC
                if failure:
                    _NEWS_REFRESH_FAILURES[key] = failure
                else:
                    _NEWS_REFRESH_FAILURES.pop(key, None)

    thread = threading.Thread(target=worker, name="public-info-refresh", daemon=True)
    try:
        thread.start()
    except RuntimeError as exc:
        with _NEWS_REFRESH_LOCK:
            _NEWS_REFRESHES.discard(key)
            _NEWS_REFRESH_NEXT_ALLOWED[key] = time.monotonic() + PUBLIC_INFO_TTL_SEC
            _NEWS_REFRESH_FAILURES[key] = type(exc).__name__
        return False
    return True


def public_market_overview_payload(
    *,
    window_sec: int = 3600,
    settings: Settings | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    if _v2_disabled(loaded):
        return _v2_disabled_payload()
    try:
        cockpit = _market_cockpit_raw(
            loaded,
            window_sec=window_sec,
            board_limit=8,
            now_ts=now_ts,
        )
    except Exception:
        return api_error("市场总览暂时不可用", code="upstream_unavailable")
    payload = {
        "schema_version": cockpit.get("schema_version"),
        "generated_at": cockpit.get("generated_at"),
        "window_sec": cockpit.get("window_sec"),
        "data_status": cockpit.get("data_status"),
        "warnings": cockpit.get("warnings") or [],
        "coverage": cockpit.get("coverage") or {},
        "readiness": cockpit.get("readiness") or {},
        "overview": cockpit.get("overview") or {},
    }
    return api_ok(_strip_forbidden(payload), message="已读取市场总览")


def _public_data_sources_registry_base_payload() -> dict[str, Any]:
    return api_ok(data_source_registry_payload(), message="已读取数据源治理清单")


def public_data_sources_payload() -> dict[str, Any]:
    payload = data_source_registry_payload()
    runtime = UPSTREAM_SOURCE_METRICS.snapshot()
    observed = runtime.get("sources") if isinstance(runtime.get("sources"), dict) else {}
    for source in payload["sources"]:
        source["runtime"] = observed.get(source["id"], {
            "status": "unobserved",
            "attempts": 0,
            "successes": 0,
            "failures": 0,
            "success_rate": None,
            "cache_hit_rate": None,
            "data_age_sec": None,
        })
    payload["runtime"] = {key: value for key, value in runtime.items() if key != "sources"}
    response = _public_data_sources_registry_base_payload()
    response["data"] = _strip_forbidden(payload)
    return response


def public_radar_boards_payload(
    *,
    window_sec: int = 3600,
    board_limit: int = 8,
    settings: Settings | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    if _v2_disabled(loaded):
        return _v2_disabled_payload()
    try:
        cockpit = _market_cockpit_raw(
            loaded,
            window_sec=window_sec,
            board_limit=board_limit,
            now_ts=now_ts,
        )
    except Exception:
        return api_error("雷达榜单暂时不可用", code="upstream_unavailable")
    coverage = dict(cockpit.get("coverage") or {})
    boards = list(cockpit.get("boards") or [])
    methodology = dict(cockpit.get("methodology") or {})
    current_ts = int(now_ts or time.time())
    realtime_history: list[dict[str, Any]] = []
    try:
        realtime_store = RealtimeFeatureStore(loaded.realtime_features_db_path)
        realtime_rows = realtime_store.latest_by_symbol(
            now_ts=current_ts,
            max_age_sec=180,
        )
        if realtime_rows:
            realtime_history = realtime_store.recent_rows(now_ts=current_ts, window_sec=86_400)
    except Exception:
        realtime_rows = []
    coverage["realtime"] = len({str(row.get("symbol") or "") for row in realtime_rows})
    coverage["realtime_exchanges"] = len({str(row.get("exchange") or "") for row in realtime_rows})
    if realtime_rows:
        boards.extend(build_realtime_radar_boards(realtime_rows, limit=board_limit))
        methodology["realtime"] = "Binance/Bybit/OKX 公共 WebSocket 成交与可用清算按封闭分钟输出；不可用时保留 REST 榜单。"
        realtime_intelligence = build_realtime_intelligence(
            realtime_history,
            now_ts=current_ts,
            limit=board_limit,
            include_backtest=False,
        )
        if realtime_intelligence.get("data_status") == "ready":
            boards.extend(build_realtime_intelligence_radar_boards(realtime_intelligence, limit=board_limit))
            coverage["realtime_intelligence"] = int(
                (realtime_intelligence.get("coverage") or {}).get("symbols") or 0
            )
            methodology["realtime_intelligence"] = (
                "Surge、短周期潜伏、24h 总榜和五窗口方向共振仅使用已封闭分钟特征；规则阈值不是收益预测。"
            )
    payload = {
        "schema_version": cockpit.get("schema_version"),
        "generated_at": cockpit.get("generated_at"),
        "window_sec": cockpit.get("window_sec"),
        "data_status": cockpit.get("data_status"),
        "warnings": cockpit.get("warnings") or [],
        "coverage": coverage,
        "readiness": cockpit.get("readiness") or {},
        "boards": boards,
        "methodology": methodology,
    }
    return api_ok(_strip_forbidden(payload), message="已读取雷达榜单")


def public_workstation_radar_momentum_payload(
    *,
    window: str = "1h",
    board_limit: int = 8,
    settings: Settings | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Project one workstation momentum window from Paoxx-owned market facts."""
    safe_window = str(window or "1h").strip().lower()
    window_sec = WORKSTATION_RADAR_WINDOWS.get(safe_window)
    if window_sec is None:
        return api_error("Unsupported radar window", code="invalid_window")

    source = public_radar_boards_payload(
        window_sec=window_sec,
        board_limit=board_limit,
        settings=settings,
        now_ts=now_ts,
    )
    if not source.get("ok"):
        return source
    source_data = dict(source.get("data") or {})
    core_keys = {"price", "oi", "futures_flow", "spot_flow"}
    boards = [
        board
        for board in list(source_data.get("boards") or [])
        if str((board or {}).get("key") or "") in core_keys
    ]
    payload = {
        "schema_version": "workstation.radar.momentum.v1",
        "generated_at": source_data.get("generated_at"),
        "window": safe_window,
        "window_sec": window_sec,
        "data_status": source_data.get("data_status"),
        "warnings": source_data.get("warnings") or [],
        "coverage": source_data.get("coverage") or {},
        "readiness": source_data.get("readiness") or {},
        "boards": boards,
        "methodology": {
            **dict(source_data.get("methodology") or {}),
            "amount_rank": "Ranks absolute values inside the selected closed window.",
            "strength_rank": "Ranks cross-sectional empirical strength separately from absolute amount.",
            "closed_window": True,
        },
    }
    return api_ok(_strip_forbidden(payload), message="Workstation momentum window loaded")


def public_workstation_radar_momentum_windows_payload(
    *,
    board_limit: int = 8,
    settings: Settings | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    """Return all workstation momentum windows from one history scan."""
    loaded = settings or Settings.load()
    if _v2_disabled(loaded):
        return _v2_disabled_payload()
    safe_limit = max(3, min(20, int(board_limit or 8)))
    window_items = tuple(WORKSTATION_RADAR_WINDOWS.items())

    def load() -> dict[str, Any]:
        try:
            sources = load_market_cockpit_windows(
                loaded,
                window_secs=tuple(window_sec for _, window_sec in window_items),
                board_limit=safe_limit,
                now_ts=now_ts,
                live_rows=[],
            )
        except Exception:
            return api_error("Radar momentum windows unavailable", code="upstream_unavailable")
        core_keys = {"price", "oi", "futures_flow", "spot_flow"}
        windows: dict[str, Any] = {}
        for window_key, window_sec in window_items:
            source_data = dict(sources.get(window_sec) or {})
            windows[window_key] = {
                "schema_version": "workstation.radar.momentum.v1",
                "generated_at": source_data.get("generated_at"),
                "window": window_key,
                "window_sec": window_sec,
                "data_status": source_data.get("data_status"),
                "warnings": source_data.get("warnings") or [],
                "coverage": source_data.get("coverage") or {},
                "readiness": source_data.get("readiness") or {},
                "boards": [
                    board
                    for board in list(source_data.get("boards") or [])
                    if str((board or {}).get("key") or "") in core_keys
                ],
                "methodology": {
                    **dict(source_data.get("methodology") or {}),
                    "amount_rank": "Ranks absolute values inside the selected closed window.",
                    "strength_rank": "Ranks cross-sectional empirical strength separately from absolute amount.",
                    "closed_window": True,
                },
            }
        return api_ok(
            _strip_forbidden({
                "schema_version": "workstation.radar.momentum-windows.v1",
                "windows": windows,
            }),
            message="Workstation momentum windows loaded",
        )

    if now_ts is not None:
        return load()
    cache_key = f"public:workstation-radar-windows:{loaded.market_snapshots_db_path}:{safe_limit}"
    return runtime_cache_get_or_set(cache_key, PUBLIC_MARKET_COCKPIT_TTL_SEC, load)


def public_realtime_market_payload(
    *,
    symbol: str = "",
    limit: int = 80,
    max_age_sec: int = 180,
    settings: Settings | Any | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    normalized = ""
    if str(symbol or "").strip():
        parsed = normalize_symbol_filter(symbol)
        normalized = str(parsed.get("symbol") or "")
        if not normalized:
            return api_error(str(parsed.get("error") or "币种格式无效"), code="invalid_symbol")
    now = int(now_ts or time.time())
    safe_age = max(30, min(900, int(max_age_sec or 180)))
    safe_limit = max(1, min(200, int(limit or 80)))
    store = RealtimeFeatureStore(loaded.realtime_features_db_path)
    try:
        rows = store.latest_by_symbol(now_ts=now, max_age_sec=safe_age)
    except Exception:
        return api_error("实时市场特征暂时不可用", code="upstream_unavailable")
    items = []
    for row in rows:
        if normalized and str(row.get("symbol") or "") != normalized:
            continue
        bucket_start = int(row.get("bucket_start") or 0)
        bucket_sec = max(1, int(row.get("bucket_sec") or 60))
        bucket_end = bucket_start + bucket_sec
        age_sec = max(0, now - bucket_end)
        price_open = _number(row.get("price_open"))
        price_close = _number(row.get("price_close"))
        price_change_pct = (
            (price_close - price_open) / price_open * 100
            if price_open is not None and price_open > 0 and price_close is not None
            else None
        )
        items.append({
            "exchange": str(row.get("exchange") or ""),
            "market": str(row.get("market") or ""),
            "symbol": str(row.get("symbol") or ""),
            "bucket_start": bucket_start,
            "bucket_end": bucket_end,
            "bucket_sec": bucket_sec,
            "observed_at": _utc_time_text(bucket_end),
            "age_sec": age_sec,
            "data_status": "ready" if age_sec <= max(90, bucket_sec * 2) else "stale",
            "trade_buy_usd": _number(row.get("trade_buy_usd")),
            "trade_sell_usd": _number(row.get("trade_sell_usd")),
            "cvd_usd": _number(row.get("cvd_usd")),
            "trade_count": int(_number(row.get("trade_count")) or 0),
            "price_open": price_open,
            "price_high": _number(row.get("price_high")),
            "price_low": _number(row.get("price_low")),
            "price_close": price_close,
            "price_change_pct": round(price_change_pct, 6) if price_change_pct is not None else None,
            "long_liquidation_usd": _number(row.get("long_liquidation_usd")),
            "short_liquidation_usd": _number(row.get("short_liquidation_usd")),
            "liquidation_count": int(_number(row.get("liquidation_count")) or 0),
            "source": f"{str(row.get('exchange') or 'unknown')}_futures_websocket",
        })
        if len(items) >= safe_limit:
            break
    data_status = "ready" if any(item["data_status"] == "ready" for item in items) else "stale" if items else "unavailable"
    return api_ok({
        "schema_version": "2026-07-17",
        "generated_at": _utc_time_text(now),
        "data_status": data_status,
        "count": len(items),
        "filters": {"symbol": normalized, "max_age_sec": safe_age},
        "items": items,
        "methodology": {
            "cvd": "逐笔聚合成交按主动方方向计算的美元成交差。",
            "liquidations": "Binance/Bybit 按官方强平方向语义映射多空持仓；OKX 不提供公开全市场强平流。",
            "closed_buckets_only": True,
        },
    }, message="已读取实时市场特征")


def _project_realtime_intelligence_payload(payload: dict[str, Any], *, limit: int) -> dict[str, Any]:
    """Bound the public payload without changing full-universe coverage metadata."""

    safe_limit = max(1, min(30, int(limit or 10)))
    projected = dict(payload)
    projected["items"] = list(payload.get("items") or [])[:safe_limit]
    projected["anomaly_events"] = list(payload.get("anomaly_events") or [])[:safe_limit]
    board_limit = min(PUBLIC_INTELLIGENCE_BOARD_LIMIT, safe_limit)
    projected["boards"] = [
        {
            **board,
            "items": list(board.get("items") or [])[:board_limit],
        }
        for board in list(payload.get("boards") or [])
        if isinstance(board, dict)
    ]
    return projected


def _realtime_intelligence_snapshot_path(settings: Any) -> Path:
    database_path = Path(settings.realtime_features_db_path)
    return database_path.with_name(f"{database_path.stem}.intelligence.json")


def _load_realtime_intelligence_snapshot(
    cache_key: str,
    snapshot_path: Path,
) -> tuple[float, dict[str, Any]] | None:
    with _INTELLIGENCE_SNAPSHOT_LOCK:
        cached = _INTELLIGENCE_SNAPSHOTS.get(cache_key)
    if cached is not None:
        return cached

    stored = locked_read_json(snapshot_path, {}, quarantine_corrupt=True)
    if not isinstance(stored, dict):
        return None
    try:
        stored_at = float(stored.get("stored_at") or 0)
    except (TypeError, ValueError):
        return None
    payload = stored.get("payload")
    if (
        stored_at <= 0
        or not isinstance(payload, dict)
        or payload.get("schema_version") != REALTIME_INTELLIGENCE_SCHEMA_VERSION
    ):
        return None
    snapshot = (stored_at, payload)
    with _INTELLIGENCE_SNAPSHOT_LOCK:
        _INTELLIGENCE_SNAPSHOTS[cache_key] = snapshot
    return snapshot


def _store_realtime_intelligence_snapshot(
    cache_key: str,
    snapshot_path: Path,
    payload: dict[str, Any],
    *,
    stored_at: float | None = None,
) -> tuple[float, dict[str, Any]]:
    snapshot = (float(stored_at or time.time()), payload)
    with _INTELLIGENCE_SNAPSHOT_LOCK:
        _INTELLIGENCE_SNAPSHOTS[cache_key] = snapshot
    try:
        locked_write_json(snapshot_path, {"stored_at": snapshot[0], "payload": payload})
    except OSError:
        pass
    return snapshot


def _refresh_realtime_intelligence_snapshot(
    cache_key: str,
    snapshot_path: Path,
    builder: Callable[[], dict[str, Any]],
) -> None:
    with _INTELLIGENCE_SNAPSHOT_LOCK:
        if cache_key in _INTELLIGENCE_REFRESHES:
            return
        _INTELLIGENCE_REFRESHES.add(cache_key)

    def refresh() -> None:
        try:
            payload = builder()
            _store_realtime_intelligence_snapshot(cache_key, snapshot_path, payload)
        except Exception:
            pass
        finally:
            with _INTELLIGENCE_SNAPSHOT_LOCK:
                _INTELLIGENCE_REFRESHES.discard(cache_key)

    threading.Thread(
        target=refresh,
        name="public-realtime-intelligence-refresh",
        daemon=True,
    ).start()


def public_realtime_intelligence_payload(
    *,
    limit: int = 10,
    include_backtest: bool = False,
    settings: Settings | Any | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    safe_limit = max(1, min(30, int(limit or 10)))

    def build(
        build_limit: int,
        *,
        build_backtest: bool = False,
        build_now: int | None = None,
    ) -> dict[str, Any]:
        observed_now = int(build_now if build_now is not None else time.time())
        rows = RealtimeFeatureStore(loaded.realtime_features_db_path).recent_rows(
            now_ts=observed_now,
            window_sec=86_400,
        )
        payload = build_realtime_intelligence(
            rows,
            now_ts=observed_now,
            limit=build_limit,
            include_backtest=build_backtest,
        )
        market_snapshot_path = getattr(loaded, "market_snapshots_db_path", None)
        if market_snapshot_path:
            try:
                oi_rows = MarketSnapshotStore(market_snapshot_path).recent_metric_rows(
                    "oi_usd",
                    now_ts=observed_now,
                    window_sec=90_000,
                )
                oi_events = build_open_interest_anomaly_events(
                    oi_rows,
                    now_ts=observed_now,
                    limit=max(40, build_limit * 3),
                )
                if oi_events:
                    payload["anomaly_events"] = sorted(
                        [*list(payload.get("anomaly_events") or []), *oi_events],
                        key=lambda item: (str(item.get("observed_at") or ""), str(item.get("id") or "")),
                        reverse=True,
                    )[:build_limit]
                    payload.setdefault("coverage", {})["oi_anomaly_events"] = len(oi_events)
            except Exception:
                payload.setdefault("coverage", {})["oi_anomaly_events"] = 0
        return _project_realtime_intelligence_payload(_strip_forbidden(payload), limit=build_limit)

    try:
        if now_ts is not None or include_backtest:
            payload = build(
                safe_limit,
                build_backtest=bool(include_backtest),
                build_now=now_ts,
            )
        else:
            cache_key = (
                f"public:realtime-intelligence:{loaded.realtime_features_db_path}:"
                f"{getattr(loaded, 'market_snapshots_db_path', '')}"
            )
            snapshot_path = _realtime_intelligence_snapshot_path(loaded)
            snapshot = _load_realtime_intelligence_snapshot(cache_key, snapshot_path)
            snapshot_age = time.time() - snapshot[0] if snapshot is not None else float("inf")
            if snapshot is None or snapshot_age > PUBLIC_INTELLIGENCE_MAX_STALE_SEC:
                snapshot = _store_realtime_intelligence_snapshot(
                    cache_key,
                    snapshot_path,
                    build(30),
                )
            elif snapshot_age >= PUBLIC_INTELLIGENCE_REFRESH_SEC:
                _refresh_realtime_intelligence_snapshot(
                    cache_key,
                    snapshot_path,
                    lambda: build(30),
                )
            payload = _project_realtime_intelligence_payload(snapshot[1], limit=safe_limit)
    except Exception:
        return api_error("实时异常情报暂时不可用", code="upstream_unavailable")
    return api_ok(_strip_forbidden(payload), message="已读取实时异常情报")


def public_funds_sectors_payload(
    *,
    window_sec: int = 3600,
    market_type: str = "spot",
    settings: Settings | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    if _v2_disabled(loaded):
        return _v2_disabled_payload()
    safe_window = normalize_window(window_sec)
    safe_market = normalize_market_type(market_type)

    def build() -> dict[str, Any]:
        cockpit = _market_cockpit_raw(
            loaded,
            window_sec=safe_window,
            board_limit=8,
            now_ts=now_ts,
        )
        return build_funds_sectors(cockpit, market_type=safe_market)

    try:
        if now_ts is not None:
            payload = build()
        else:
            cache_key = f"public:funds:sectors:{loaded.market_snapshots_db_path}:{safe_window}:{safe_market}"
            payload = runtime_cache_get_or_set(cache_key, PUBLIC_FUNDS_TTL_SEC, build)
    except Exception:
        return api_error("板块资金暂时不可用", code="upstream_unavailable")
    return api_ok(_strip_forbidden(payload), message="已读取板块资金")


def public_funds_assets_payload(
    *,
    window_sec: int = 3600,
    market_type: str = "spot",
    search: str = "",
    sector: str = "",
    data_status: str = "",
    sort_key: str = "net_flow_usd",
    direction: str = "desc",
    page: int = 1,
    page_size: int = 50,
    settings: Settings | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    if _v2_disabled(loaded):
        return _v2_disabled_payload()
    try:
        cockpit = _market_cockpit_raw(
            loaded,
            window_sec=window_sec,
            board_limit=8,
            now_ts=now_ts,
        )
        payload = build_funds_assets(
            cockpit,
            market_type=market_type,
            search=search,
            sector=sector,
            data_status=data_status,
            sort_key=sort_key,
            direction=direction,
            page=page,
            page_size=page_size,
        )
    except Exception:
        return api_error("资产资金表暂时不可用", code="upstream_unavailable")
    return api_ok(_strip_forbidden(payload), message="已读取资产资金表")


def public_workstation_funds_open_interest_payload(
    symbol: str,
    *,
    settings: Settings | None = None,
    collector: Callable[[Settings, str], dict[str, Any]] | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    parsed = normalize_symbol_filter(symbol)
    target = str(parsed.get("symbol") or "")
    if not target:
        return api_error(str(parsed.get("error") or "币种格式无效"), code="invalid_symbol")

    def build() -> dict[str, Any]:
        source = collector or collect_cross_exchange_open_interest
        return source(loaded, target)

    try:
        if now_ts is not None or collector is not None:
            payload = build()
        else:
            payload = runtime_cache_get_or_set(
                f"public:workstation:funds:oi:{target}",
                PUBLIC_FUNDS_TTL_SEC,
                build,
            )
    except Exception:
        return api_error("跨交易所 OI 暂时不可用", code="upstream_unavailable")
    return api_ok(_strip_forbidden(payload), message="已读取跨交易所 OI")


def public_info_feed_payload(
    *,
    source_type: str = "",
    language: str = "",
    importance: str = "",
    symbol: str = "",
    search: str = "",
    page: int = 1,
    page_size: int = 30,
    window_sec: int = 7 * 86_400,
    settings: Settings | None = None,
    now_ts: int | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    if _v2_disabled(loaded):
        return _v2_disabled_payload()
    now = int(now_ts or time.time())
    safe_window = max(3600, min(30 * 86_400, int(window_sec or 7 * 86_400)))
    safe_source_type = str(source_type or "").strip()[:60]
    safe_language = str(language or "").strip().lower()
    safe_importance = str(importance or "").strip().lower()
    safe_symbol = _safe_symbol(symbol)["symbol"] if symbol else ""
    warnings: list[str] = []

    def build() -> dict[str, Any]:
        store = NewsEventStore(loaded.news_events_db_path)
        ingestion: dict[str, Any] = {"status": "cached"}
        latest = store.latest_collected_at()
        should_refresh = refresh and now_ts is None and (latest <= 0 or now - latest >= PUBLIC_INFO_TTL_SEC)
        if should_refresh:
            scheduled = _schedule_news_refresh(loaded)
            with _NEWS_REFRESH_LOCK:
                last_failure = _NEWS_REFRESH_FAILURES.get(str(loaded.news_events_db_path), "")
            if scheduled:
                ingestion = {"status": "refreshing"}
            elif last_failure:
                ingestion = {"status": "degraded", "error": last_failure}
                warnings.append("Binance 官方公告刷新失败，当前展示本地最近一次成功索引。")
        feed = store.list_feed(
            start_ts=now - safe_window,
            end_ts=now,
            source_type=safe_source_type,
            language=safe_language,
            importance=safe_importance,
            symbol=safe_symbol,
            query=search,
            page=page,
            page_size=page_size,
        )
        items = feed["items"]
        high = sum(1 for item in items if item.get("importance") == "high")
        rights_ok = sum(1 for item in items if item.get("rights_status") == "official_link_only")
        if not items and ingestion.get("status") == "refreshing":
            warnings.append("Binance 官方公告正在后台更新，稍后刷新即可查看。")
        data_status = "ready" if items and ingestion.get("status") != "degraded" else "degraded" if items or ingestion.get("status") in {"degraded", "refreshing"} else "empty"
        return {
            "schema_version": NEWS_SCHEMA_VERSION,
            "generated_at": _utc_time_text(now),
            "data_status": data_status,
            "coverage": {
                "events": len(items),
                "clusters": len(items),
                "high_importance": high,
                "linked_symbols": len({symbol for item in items for symbol in item.get("symbols") or []}),
                "rights_verified": rights_ok,
                "sources": 1 if items else 0,
            },
            "warnings": list(dict.fromkeys(warnings)),
            "filters": {
                "source_type": safe_source_type,
                "language": safe_language,
                "importance": safe_importance,
                "symbol": safe_symbol,
                "q": _short(search, 80),
                "window_sec": safe_window,
            },
            "pagination": feed["pagination"],
            "summary": {
                "high_importance": high,
                "risk": sum(1 for item in items if item.get("event_kind") == "risk"),
                "opportunity": sum(1 for item in items if item.get("event_kind") == "opportunity"),
                "official": sum(1 for item in items if item.get("source_type") == "official_announcement"),
            },
            "channels": [
                {"key": "official", "label": "官方公告", "status": "ready" if items else "empty", "count": len(items), "rights_status": "official_link_only"},
                {"key": "authorized_zh", "label": "授权中文资讯", "status": "unavailable", "count": 0, "reason": "尚未配置可验证授权源"},
                {"key": "authorized_en", "label": "授权英文资讯", "status": "unavailable", "count": 0, "reason": "尚未配置可验证授权源"},
                {"key": "sentiment", "label": "市场情绪", "status": "unavailable", "count": 0, "reason": "未使用未授权社交数据"},
            ],
            "items": items,
            "ingestion": ingestion,
            "methodology": {
                "source_policy": "当前仅索引 Binance 官方公开公告标题、时间和原文链接，不复制受限正文。",
                "dedup": "按规范化标题聚类；同簇保留全部合法来源链接。",
                "ai_boundary": "仅高重要度事件生成规则化解读，并明确区分官方标题事实与可能影响推断。",
                "rights": "official_link_only 表示只展示必要元数据并回链官方原文。",
            },
        }

    try:
        if now_ts is not None:
            payload = build()
        else:
            cache_key = ":".join((
                "public:info", str(loaded.news_events_db_path), safe_source_type, safe_language,
                safe_importance, safe_symbol, _short(search, 80), str(page), str(page_size), str(safe_window),
            ))
            payload = runtime_cache_get_or_set(cache_key, PUBLIC_INFO_TTL_SEC, build)
    except Exception:
        return api_error("信息中心暂时不可用", code="upstream_unavailable")
    return api_ok(_strip_forbidden(payload), message="已读取授权信息事件")


def public_agents_overview_payload(
    *,
    window_sec: int = 14_400,
    settings: Settings | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    if _v2_disabled(loaded):
        return _v2_disabled_payload()
    now = int(now_ts or time.time())
    safe_window = normalize_window(window_sec)

    def build() -> dict[str, Any]:
        signal_store = _store(loaded)
        news_store = NewsEventStore(loaded.news_events_db_path)
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="public-agents") as executor:
            cockpit_future = executor.submit(
                _market_cockpit_raw,
                loaded,
                window_sec=safe_window,
                board_limit=8,
                now_ts=now_ts,
            )
            signals_future = executor.submit(
                signal_store.intelligence_events,
                start_ts=now - safe_window,
                end_ts=now,
                limit=500,
            )
            news_future = executor.submit(
                news_store.list_feed,
                start_ts=now - 86_400,
                end_ts=now,
                page=1,
                page_size=100,
            )
            cockpit = cockpit_future.result()
            signals = signals_future.result()
            news_items = news_future.result()["items"]
        return build_agent_overview(
            loaded,
            now_ts=now,
            window_sec=safe_window,
            cockpit=cockpit,
            signals=signals,
            news_items=news_items,
        )

    try:
        if now_ts is not None:
            payload = build()
        else:
            cache_key = f"public:agents:{loaded.market_snapshots_db_path}:{loaded.signal_events_db_path}:{loaded.news_events_db_path}:{safe_window}"
            payload = runtime_cache_get_or_set(cache_key, PUBLIC_AGENTS_TTL_SEC, build)
    except Exception:
        return api_error("Agent 决策暂时不可用", code="upstream_unavailable")
    return api_ok(_strip_forbidden(payload), message="已读取证据化 Agent 结论")


def _radar_intelligence_raw(
    settings: Settings,
    *,
    now_ts: int,
    window_sec: int,
    board_limit: int,
) -> dict[str, Any]:
    def load() -> dict[str, Any]:
        source = _store(settings).intelligence_events(
            start_ts=now_ts - 2_592_000,
            end_ts=now_ts,
            limit=2000,
        )
        return build_radar_intelligence(
            source,
            now_ts=now_ts,
            window_sec=window_sec,
            board_limit=board_limit,
        )

    cache_key = f"public:radar-intelligence:{settings.signal_events_db_path}:{window_sec}:{board_limit}"
    return runtime_cache_get_or_set(cache_key, PUBLIC_INTELLIGENCE_TTL_SEC, load)


def _radar_intelligence_targets(
    settings: Settings,
    refs: set[str],
    *,
    now_ts: int,
    window_sec: int = 2_592_000,
) -> dict[str, Any]:
    normalized_refs = {
        str(reference or "").strip().lower()
        for reference in refs
        if str(reference or "").strip()
    }
    if not normalized_refs:
        return {"items": []}

    def load() -> dict[str, Any]:
        source = _store(settings).intelligence_events(
            start_ts=now_ts - 2_592_000,
            end_ts=now_ts,
            limit=2000,
        )
        return build_radar_intelligence(
            source,
            now_ts=now_ts,
            window_sec=window_sec,
            board_limit=1,
            target_refs=normalized_refs,
        )

    cache_refs = ",".join(sorted(normalized_refs))
    cache_key = f"public:radar-intelligence-targets:{settings.signal_events_db_path}:{window_sec}:{cache_refs}"
    return runtime_cache_get_or_set(cache_key, PUBLIC_INTELLIGENCE_TTL_SEC, load)


def _requested_signal_refs(value: str) -> list[str]:
    refs: list[str] = []
    for raw in str(value or "").split(",")[:80]:
        reference = raw.strip().lower()
        if not PUBLIC_SIGNAL_REF_RE.fullmatch(reference) or reference in refs:
            continue
        refs.append(reference)
        if len(refs) >= PUBLIC_INTELLIGENCE_RESPONSE_LIMIT:
            break
    return refs


def _compact_rank(value: Any) -> dict[str, Any]:
    rank = value if isinstance(value, dict) else {}
    allowed = ("available", "label", "rank", "sample_size", "percentile", "reason")
    return {key: rank.get(key) for key in allowed if rank.get(key) is not None}


def _compact_intelligence(value: Any) -> dict[str, Any]:
    intelligence = value if isinstance(value, dict) else {}
    resonance = intelligence.get("resonance") if isinstance(intelligence.get("resonance"), dict) else {}
    lifecycle = intelligence.get("lifecycle") if isinstance(intelligence.get("lifecycle"), dict) else {}
    windows = []
    for source in resonance.get("windows", []):
        if not isinstance(source, dict):
            continue
        windows.append({
            key: source.get(key)
            for key in ("key", "active", "module_count", "signal_count")
            if source.get(key) is not None
        })
    return _strip_forbidden({
        "self_rank": _compact_rank(intelligence.get("self_rank")),
        "market_strength_rank": _compact_rank(intelligence.get("market_strength_rank")),
        "market_absolute_rank": _compact_rank(intelligence.get("market_absolute_rank")),
        "resonance": {
            key: resonance.get(key)
            for key in ("label", "active_count", "available")
            if resonance.get(key) is not None
        } | {"windows": windows},
        "lifecycle": {
            key: lifecycle.get(key)
            for key in ("state", "label", "age_sec", "basis")
            if lifecycle.get(key) is not None
        },
    })


def _public_intelligence_entry(entry: dict[str, Any]) -> dict[str, Any]:
    signal = entry.get("signal") if isinstance(entry.get("signal"), dict) else {}
    return {
        "signal": public_signal_item(signal),
        "intelligence": _compact_intelligence(entry.get("intelligence")),
    }


def public_radar_intelligence_payload(
    *,
    window_sec: int = 86400,
    board_limit: int = 5,
    signal_refs: str = "",
    settings: Settings | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    now = int(now_ts or time.time())
    safe_window = min(2_592_000, max(3600, int(window_sec or 86400)))
    safe_limit = min(12, max(1, int(board_limit or 5)))
    requested_refs = _requested_signal_refs(signal_refs)
    if str(signal_refs or "").strip() and not requested_refs:
        return api_error("信号引用格式无效", code="invalid_refs")
    raw = _radar_intelligence_raw(
        loaded,
        now_ts=now,
        window_sec=safe_window,
        board_limit=safe_limit,
    )

    source_entries = [
        entry
        for entry in raw.get("items", [])
        if isinstance(entry, dict)
        and int((entry.get("signal") or {}).get("ts") or 0) >= now - safe_window
    ]
    if requested_refs:
        entries_by_ref: dict[str, dict[str, Any]] = {}
        for entry in source_entries:
            signal = entry.get("signal") if isinstance(entry.get("signal"), dict) else {}
            public_ref = str(signal.get("public_ref") or "").lower()
            numeric_ref = str(signal.get("id") or "")
            if public_ref:
                entries_by_ref[public_ref] = entry
            if numeric_ref:
                entries_by_ref[numeric_ref] = entry
        selected_entries = [entries_by_ref[reference] for reference in requested_refs if reference in entries_by_ref]
    else:
        selected_entries = source_entries[:PUBLIC_INTELLIGENCE_RESPONSE_LIMIT]

    public_boards = []
    for source_board in raw.get("boards", []):
        if not isinstance(source_board, dict):
            continue
        public_boards.append({
            "key": _short(source_board.get("key") or "", 24),
            "title": _short(source_board.get("title") or "", 48),
            "description": _short(source_board.get("description") or "", 140),
            "count": int(source_board.get("count") or 0),
            "items": [
                _public_intelligence_entry(entry)
                for entry in source_board.get("items", [])
                if isinstance(entry, dict)
            ],
        })
    payload = {
        "schema_version": raw.get("schema_version"),
        "generated_at": raw.get("generated_at"),
        "window_sec": raw.get("window_sec"),
        "data_status": raw.get("data_status"),
        "methodology": raw.get("methodology"),
        "summary": raw.get("summary"),
        "projection": {
            "requested": len(requested_refs),
            "returned": len(selected_entries),
            "max_items": PUBLIC_INTELLIGENCE_RESPONSE_LIMIT,
        },
        "items": [_public_intelligence_entry(entry) for entry in selected_entries],
        "boards": public_boards,
    }
    return api_ok(_strip_forbidden(payload), message="已读取信号情报排名")


def _public_bot_actions(settings: Settings, symbol: str, signal_ref: str = "") -> dict[str, str]:
    bot_username = str(settings.ai_bot_username or "").strip().lstrip("@")
    bot_username = bot_username if re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username) else ""
    coin = symbol[:-4] if symbol.endswith("USDT") else symbol
    safe_ref = str(signal_ref or "").lower() if PUBLIC_SIGNAL_REF_RE.fullmatch(str(signal_ref or "").lower()) else ""
    start_context = f"_{safe_ref}" if safe_ref else ""
    return {
        "radar_url": f"/radar?symbol={symbol}{'&signal=' + safe_ref if safe_ref else ''}",
        "share_url": f"/coin/{symbol}{'?signal=' + safe_ref if safe_ref else ''}",
        "ai_url": f"https://t.me/{bot_username}?start=analyze_{coin}{start_context}" if bot_username and coin else "",
        "alert_url": f"https://t.me/{bot_username}?start=alert_{coin}{start_context}" if bot_username and coin else "",
    }


def public_coin_context_payload(
    symbol: str,
    *,
    settings: Settings | None = None,
    snapshot_loader: SnapshotLoader | None = None,
    chart_loader: ChartLoader | None = None,
    market_type: str = "futures",
    interval: str = "15m",
    bars: int = 96,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    normalized = _safe_symbol(symbol)
    if not normalized["symbol"]:
        return api_error(normalized["error"], code="invalid_symbol")
    target = normalized["symbol"]
    now = int(now_ts or time.time())
    safe_market = normalize_chart_market(market_type)
    safe_interval = normalize_chart_interval(interval)
    safe_bars = max(24, min(240, int(bars or 96)))

    def load_signal_context() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        timeline = _store(loaded).symbol_timeline(target, limit=30, compact=True)
        timeline_refs = {
            str(item.get("public_ref") or item.get("id") or "")
            for item in timeline
            if str(item.get("public_ref") or item.get("id") or "")
        }
        intelligence_raw = _radar_intelligence_targets(
            loaded,
            timeline_refs,
            now_ts=now,
            window_sec=2_592_000,
        )
        intelligence_by_ref = {
            str((entry.get("signal") or {}).get("public_ref") or ""): _compact_intelligence(entry.get("intelligence"))
            for entry in intelligence_raw.get("items", [])
            if isinstance(entry, dict)
        }
        public_timeline: list[dict[str, Any]] = []
        for item in timeline:
            reference = str(item.get("public_ref") or "")
            public_item = public_signal_item(item)
            public_item["intelligence"] = _strip_forbidden(intelligence_by_ref.get(reference, {}))
            public_timeline.append(public_item)
        return timeline, public_timeline

    def load_chart() -> list[list[Any]]:
        if chart_loader is not None:
            return chart_loader(loaded, target, safe_market, safe_interval, safe_bars)
        if snapshot_loader is not None:
            return []
        source = BinanceDataSource(loaded)
        try:
            method = source.spot_klines if safe_market == "spot" else source.klines
            return method(target, interval=safe_interval, limit=safe_bars)
        finally:
            http = getattr(source, "http", None)
            if http is not None and hasattr(http, "close"):
                http.close()

    def load_chart_safe() -> list[list[Any]]:
        try:
            if chart_loader is not None or now_ts is not None:
                return load_chart()
            chart_key = f"public:coin-chart:{target}:{safe_market}:{safe_interval}:{safe_bars}"
            return runtime_cache_get_or_set(chart_key, PUBLIC_SNAPSHOT_TTL_SEC, load_chart)
        except Exception:
            return []

    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="public-coin-context") as executor:
        signal_context_future = executor.submit(load_signal_context)
        snapshot_future = executor.submit(
            public_market_snapshot_payload,
            target,
            settings=loaded,
            snapshot_loader=snapshot_loader,
            now_ts=now,
        )
        chart_future = executor.submit(load_chart_safe)
        timeline, public_timeline = signal_context_future.result()
        snapshot_payload = snapshot_future.result()
        raw_klines = chart_future.result()
    chart = build_kline_chart(
        raw_klines if isinstance(raw_klines, list) else [],
        market_type=safe_market,
        interval=safe_interval,
        requested=safe_bars,
    )
    history_points: list[dict[str, Any]] = []
    try:
        if loaded.market_snapshots_db_path.exists():
            history_points = MarketSnapshotStore(loaded.market_snapshots_db_path).symbol_series(
                target,
                start_ts=now - max(86400, int(loaded.market_snapshot_retention_days) * 86400),
                end_ts=now,
                limit=240,
            )
    except Exception:
        history_points = []
    series = build_snapshot_series(history_points)
    module_counts: dict[str, int] = {}
    for item in timeline:
        module = str(item.get("module") or "other")
        module_counts[module] = module_counts.get(module, 0) + 1
    latest_signal_ref = str(timeline[0].get("public_ref") or "") if timeline else ""
    related_info = [
        public_signal_item(item)
        for item in timeline
        if str(item.get("module") or "") == "announcement"
    ][:12]
    warnings = [
        *[str(item) for item in chart.get("warnings", []) if str(item)],
        *[str(item) for item in series.get("warnings", []) if str(item)],
    ]
    market_ready = bool(snapshot_payload.get("ok"))
    data_status = "ready" if market_ready and chart.get("data_status") == "ready" else "degraded" if market_ready or raw_klines or history_points else "unavailable"
    payload = {
        "schema_version": PUBLIC_CONTEXT_SCHEMA_VERSION,
        "symbol": target,
        "coin": target[:-4] if target.endswith("USDT") else target,
        "market": snapshot_payload.get("data") if snapshot_payload.get("ok") else None,
        "market_error": "" if snapshot_payload.get("ok") else str(snapshot_payload.get("message") or "市场数据暂时不可用"),
        "summary": {
            "signal_count": len(timeline),
            "sent_count": sum(1 for item in timeline if item.get("status") == "sent"),
            "module_counts": module_counts,
            "latest_at": str(timeline[0].get("time") or "") if timeline else "",
        },
        "data_status": data_status,
        "warnings": warnings,
        "chart": chart,
        "series": series,
        "related_info": {
            "data_status": "ready" if related_info else "empty",
            "items": related_info,
            "methodology": "仅展示已进入统一信号库的官方公告事件，不抓取受限全文。",
        },
        "evidence_coverage": {
            "market": 1 if market_ready else 0,
            "chart_points": int((chart.get("coverage") or {}).get("returned") or 0),
            "snapshot_points": int((series.get("coverage") or {}).get("points") or 0),
            "signals": len(public_timeline),
            "announcements": len(related_info),
        },
        "timeline": public_timeline,
        "actions": _public_bot_actions(loaded, target, latest_signal_ref),
    }
    return api_ok(_strip_forbidden(payload), message="已读取单币上下文")


def public_watchlist_market_payload(
    symbols: str,
    *,
    settings: Settings | None = None,
    snapshot_loader: SnapshotLoader | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    normalized_symbols: list[str] = []
    invalid: list[str] = []
    for raw in str(symbols or "").split(",")[:20]:
        value = raw.strip()
        if not value:
            continue
        parsed = _safe_symbol(value)
        if parsed["symbol"]:
            if parsed["symbol"] not in normalized_symbols:
                normalized_symbols.append(parsed["symbol"])
        else:
            invalid.append(value[:24])
    normalized_symbols = normalized_symbols[:12]
    if not normalized_symbols:
        return api_error("请提供 1–12 个有效币种", code="invalid_symbols")

    def load_one(target: str) -> tuple[str, dict[str, Any]]:
        return target, public_market_snapshot_payload(
            target,
            settings=loaded,
            snapshot_loader=snapshot_loader,
            now_ts=now_ts,
        )

    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=min(4, len(normalized_symbols)), thread_name_prefix="public-watchlist") as executor:
        futures = {executor.submit(load_one, target): target for target in normalized_symbols}
        for future in as_completed(futures):
            target = futures[future]
            try:
                _, result = future.result()
            except Exception:
                result = api_error("市场数据暂时不可用", code="upstream_unavailable")
            results[target] = result
    cockpit_by_symbol: dict[str, dict[str, Any]] = {}
    try:
        if loaded.market_snapshots_db_path.exists():
            cockpit = _market_cockpit_raw(
                loaded,
                window_sec=3600,
                board_limit=8,
                now_ts=now_ts,
            )
            cockpit_by_symbol = {
                str(item.get("symbol") or ""): item
                for item in cockpit.get("assets", [])
                if isinstance(item, dict) and item.get("symbol")
            }
    except Exception:
        cockpit_by_symbol = {}

    def compact_flow(target: str) -> dict[str, Any] | None:
        source = cockpit_by_symbol.get(target)
        if not isinstance(source, dict):
            return None
        return {
            "window_sec": 3600,
            "spot_net_flow_usd": _number(source.get("spot_flow_usd")),
            "futures_net_flow_usd": _number(source.get("futures_flow_usd")),
            "oi_change_pct": _number(source.get("oi_change_pct")),
            "funding_pct": _number(source.get("funding_pct")),
            "updated_at": _short(source.get("updated_at") or "", 40),
            "data_status": _short(source.get("status") or "degraded", 24),
            "source": "market_snapshot_store",
        }
    items = [
        {
            "symbol": target,
            "ok": bool(results.get(target, {}).get("ok")),
            "market": results.get(target, {}).get("data"),
            "error": "" if results.get(target, {}).get("ok") else str(results.get(target, {}).get("message") or "暂时不可用"),
            "coin_url": f"/coin/{target}",
            "flow": compact_flow(target),
        }
        for target in normalized_symbols
    ]
    return api_ok(_strip_forbidden({"items": items, "count": len(items), "invalid": invalid}), message="已读取自选行情")


def public_api_health_payload(*, settings: Settings | None = None) -> dict[str, Any]:
    loaded = settings or Settings.load()
    try:
        signal_stats = _store(loaded).health_summary()
        database = {
            "status": "ok",
            "signals": int(signal_stats.get("total") or 0),
            "latest_at": str(signal_stats.get("latest_at") or ""),
        }
    except Exception:
        database = {"status": "degraded", "signals": None, "latest_at": ""}
    market_history: dict[str, Any] = {"status": "empty", "latest_at": "", "age_sec": None}
    try:
        if loaded.market_snapshots_db_path.exists():
            readiness = MarketSnapshotStore(loaded.market_snapshots_db_path).readiness_summary(
                loaded,
                now_ts=int(time.time()),
                requested_window_sec=3600,
            )
            market_history = {
                "status": readiness.get("status"),
                "latest_at": readiness.get("latest_at"),
                "age_sec": (readiness.get("freshness") or {}).get("age_sec"),
                "readiness": readiness,
            }
    except Exception:
        market_history = {"status": "degraded", "latest_at": "", "age_sec": None}
    realtime_market: dict[str, Any] = {
        "status": "empty", "latest_at": "", "age_sec": None,
        "symbols": 0, "features": 0,
    }
    try:
        if loaded.realtime_features_db_path.exists():
            realtime_stats = RealtimeFeatureStore(loaded.realtime_features_db_path).health_summary(
                now_ts=int(time.time()),
                fresh_sec=max(90, int(loaded.realtime_market_bucket_sec) * 2),
            )
            expected_exchanges = ["binance"]
            if bool(getattr(loaded, "realtime_bybit_enable", True)):
                expected_exchanges.append("bybit")
            if bool(getattr(loaded, "realtime_okx_enable", True)):
                expected_exchanges.append("okx")
            observed_exchanges = realtime_stats.get("exchanges") or {}
            exchange_health = {
                exchange: observed_exchanges.get(exchange, {
                    "status": "empty", "feature_count": 0, "symbol_count": 0,
                    "latest_bucket_end": 0, "age_sec": None,
                })
                for exchange in expected_exchanges
            }
            exchange_statuses = [item.get("status") for item in exchange_health.values()]
            realtime_status = (
                "ready" if exchange_statuses and all(status == "ready" for status in exchange_statuses)
                else "partial" if any(status == "ready" for status in exchange_statuses)
                else "stale" if any(status == "stale" for status in exchange_statuses)
                else "empty"
            )
            realtime_market = {
                "status": realtime_status,
                "latest_at": _utc_time_text(int(realtime_stats.get("latest_bucket_end") or 0))
                if realtime_stats.get("latest_bucket_end") else "",
                "age_sec": realtime_stats.get("age_sec"),
                "symbols": int(realtime_stats.get("symbol_count") or 0),
                "features": int(realtime_stats.get("feature_count") or 0),
                "exchanges": exchange_health,
            }
    except Exception:
        realtime_market = {
            "status": "degraded", "latest_at": "", "age_sec": None,
            "symbols": 0, "features": 0,
        }
    healthy = (
        database["status"] == "ok"
        and market_history.get("status") in {"ready", "warming_up", "partial"}
        and realtime_market.get("status") == "ready"
    )
    payload = {
        "status": "ok" if healthy else "degraded",
        "schema_version": PUBLIC_CONTEXT_SCHEMA_VERSION,
        "database": database,
        "market_history": market_history,
        "realtime_market": realtime_market,
        "cache": runtime_cache_stats(),
        "rate_limit": PUBLIC_API_LIMITER.stats(),
        "requests": PUBLIC_API_METRICS.stats(),
        "upstreams": UPSTREAM_SOURCE_METRICS.snapshot(),
        "frontend_telemetry": PUBLIC_TELEMETRY.stats(),
        "stream": PUBLIC_STREAM_METRICS.stats(),
        "cockpit_v2": {
            "mode": str(loaded.cockpit_v2_mode or "enabled"),
            "enabled": not _v2_disabled(loaded),
        },
        "features": {
            "signal_context": True,
            "intelligence": True,
            "coin_context": True,
            "watchlist": True,
            "market_overview": True,
            "radar_boards": True,
            "realtime_market": True,
            "realtime_intelligence": True,
            "data_source_registry": True,
            "funds_sectors": True,
            "funds_assets": True,
            "info_feed": True,
            "agents_overview": True,
            "stream": True,
        },
    }
    return api_ok(_strip_forbidden(payload), message="公开接口健康状态")


def _context_evidence(market: dict[str, Any]) -> list[dict[str, Any]]:
    metrics = market.get("metrics") if isinstance(market.get("metrics"), dict) else {}
    definitions = (
        ("price_1h_pct", "1h 价格", "价格短周期方向"),
        ("oi_1h_pct", "1h OI", "杠杆资金变化"),
        ("volume_ratio", "量能倍数", "15m 相对量能"),
        ("funding_pct", "资金费率", "当前结算周期"),
        ("quote_volume", "24h 成交额", "绝对流动性"),
    )
    evidence = []
    for key, label, description in definitions:
        metric = metrics.get(key)
        if not isinstance(metric, dict) or metric.get("value") is None:
            continue
        evidence.append({"key": key, "label": label, "description": description, "metric": metric})
    return evidence


def public_signal_context_payload(
    signal_id: int | str,
    *,
    settings: Settings | None = None,
    snapshot_loader: SnapshotLoader | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    loaded = settings or Settings.load()
    store = _store(loaded)
    with store.connect() as conn:
        item = store.signal_detail(signal_id, compact=True, conn=conn)
        if not item:
            return api_error("信号不存在", code="not_found")
        related = []
        symbol = str(item.get("symbol") or "")
        if symbol:
            related = [
                related_item
                for related_item in store.symbol_timeline(symbol, limit=10, compact=True, conn=conn)
                if int(related_item.get("id") or 0) != int(item.get("id") or 0)
            ][:6]

    stage = str(item.get("stage") or "").strip()
    lifecycle: dict[str, Any] = {
        "state": stage or "recorded",
        "label": stage or "已记录",
        "derived": False,
        "started_at": str(item.get("time") or ""),
        "duration_sec": 0,
    }
    rankings: dict[str, Any] = {}
    resonance: dict[str, Any] = {}

    def load_market_context() -> tuple[dict[str, Any] | None, str]:
        normalized = _safe_symbol(symbol)
        if not normalized["symbol"]:
            return None, ""
        try:
            return _load_public_snapshot(
                loaded,
                normalized["symbol"],
                snapshot_loader=snapshot_loader,
                now_ts=now_ts,
            ), ""
        except Exception:
            return None, "市场上下文暂时不可用"

    def load_intelligence_context() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        derived_lifecycle = lifecycle
        derived_rankings: dict[str, Any] = {}
        derived_resonance: dict[str, Any] = {}
        try:
            intelligence_raw = _radar_intelligence_targets(
                loaded,
                {str(item.get("public_ref") or item.get("id") or "")},
                now_ts=int(now_ts or time.time()),
                window_sec=2_592_000,
            )
            signal_ref = str(item.get("public_ref") or "")
            for entry in intelligence_raw.get("items", []):
                candidate = entry.get("signal") if isinstance(entry, dict) else {}
                if signal_ref and str(candidate.get("public_ref") or "") == signal_ref:
                    intelligence = entry.get("intelligence") if isinstance(entry.get("intelligence"), dict) else {}
                    derived_lifecycle = intelligence.get("lifecycle") or derived_lifecycle
                    derived_rankings = {
                        "self": intelligence.get("self_rank") or {},
                        "market_strength": intelligence.get("market_strength_rank") or {},
                        "market_absolute": intelligence.get("market_absolute_rank") or {},
                    }
                    derived_resonance = intelligence.get("resonance") or {}
                    break
        except Exception:
            pass
        return derived_lifecycle, derived_rankings, derived_resonance

    market: dict[str, Any] | None = None
    market_error = ""
    if symbol:
        # Market requests are I/O-bound while intelligence is local CPU/SQLite.
        # Run them together so a cold context request pays the slower branch,
        # rather than the sum of both independent branches.
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="public-signal-context") as executor:
            market_future = executor.submit(load_market_context)
            intelligence_future = executor.submit(load_intelligence_context)
            market, market_error = market_future.result()
            lifecycle, rankings, resonance = intelligence_future.result()
    else:
        lifecycle, rankings, resonance = load_intelligence_context()
    bot_username = str(loaded.ai_bot_username or "").strip().lstrip("@")
    bot_username = bot_username if re.fullmatch(r"[A-Za-z0-9_]{5,32}", bot_username) else ""
    coin = str(item.get("coin") or (symbol[:-4] if symbol.endswith("USDT") else symbol))
    payload = {
        "schema_version": PUBLIC_CONTEXT_SCHEMA_VERSION,
        "signal": public_signal_item(item),
        "market": market,
        "market_error": market_error,
        "evidence": _context_evidence(market or {}),
        "lifecycle": lifecycle,
        "rankings": rankings,
        "resonance": resonance,
        "related": {"same_symbol": _public_items(related)},
        "actions": {
            "signal_url": f"/radar?signal={item.get('public_ref') or int(item.get('id') or 0)}",
            "symbol_url": f"/radar?symbol={symbol}" if symbol else "/radar",
            "ai_url": f"https://t.me/{bot_username}?start=analyze_{coin}" if bot_username and coin else "",
            "alert_url": f"https://t.me/{bot_username}?start=alert_{coin}" if bot_username and coin else "",
        },
    }
    return api_ok(_strip_forbidden(payload), message="已读取信号上下文")


def public_signals_payload(
    *,
    limit: int = 50,
    cursor: int | None = None,
    module: str = "",
    symbol: str = "",
    status: str = "",
    q: str = "",
    window_sec: int = 86400,
    settings: Settings | None = None,
) -> dict[str, Any]:
    normalized = normalize_symbol_filter(symbol).get("symbol", "") if str(symbol or "").strip() else ""
    store = _store(settings)
    end_ts = int(time.time())
    start_ts = end_ts - max(1, min(int(window_sec or 86400), 2592000))
    with store.connect() as conn:
        result = store.list_signals(
            limit=max(1, min(int(limit or 50), 200)),
            cursor=cursor,
            module=str(module or "").strip().lower(),
            symbol=normalized,
            status=str(status or "").strip().lower(),
            start_ts=start_ts,
            end_ts=end_ts,
            q=str(q or "").strip()[:80],
            compact=True,
            conn=conn,
        )
    items = _public_items(result.get("items", []))
    return api_ok(
        {
            "items": items,
            "count": len(items),
            "next_cursor": result.get("next_cursor"),
            "filters": {
                "module": str(module or "").strip().lower(),
                "symbol": normalized,
                "status": str(status or "").strip().lower(),
                "q": str(q or "").strip()[:80],
                "window_sec": int(window_sec or 86400),
            },
        },
        message="已读取公开信号",
    )


def public_signal_detail_payload(signal_id: int | str, *, settings: Settings | None = None) -> dict[str, Any]:
    store = _store(settings)
    with store.connect() as conn:
        item = store.signal_detail(signal_id, compact=True, conn=conn)
        if not item:
            return api_error("信号不存在", code="not_found")
        related = []
        if item.get("symbol"):
            related = [
                related_item
                for related_item in store.symbol_timeline(
                    str(item.get("symbol") or ""), limit=8, compact=True, conn=conn
                )
                if int(related_item.get("id") or 0) != int(item.get("id") or 0)
            ][:6]
    detail = signal_detail_view(item, related)
    public_sections = [
        {
            "title": "信号摘要",
            "rows": [
                {"label": "时间", "value": str(item.get("time") or ""), "code": False},
                {"label": "模块", "value": str(item.get("module") or ""), "code": False},
                {"label": "币种", "value": str(item.get("symbol") or "全局/无币种"), "code": False},
                {"label": "状态", "value": str(item.get("status") or ""), "code": False},
                {"label": "摘要", "value": _short(item.get("excerpt") or item.get("title") or "", 600), "code": False},
            ],
        },
        {
            "title": "结构字段",
            "rows": [
                {"label": "signal_type", "value": str(item.get("signal_type") or ""), "code": False},
                {"label": "score", "value": str(item.get("score") if item.get("score") is not None else "-"), "code": False},
                {"label": "stage", "value": str(item.get("stage") or "-"), "code": False},
            ],
        },
    ]
    return _strip_forbidden({
        "ok": True,
        "item": public_signal_item(item),
        "detail": {
            "header": _strip_forbidden(detail.get("header") or {}),
            "sections": _strip_forbidden(public_sections),
            "related": {"same_symbol": _public_items(related)},
        },
        "message": "已读取公开信号详情",
    })


def public_signal_stats_payload(*, window_sec: int = 86400, settings: Settings | None = None) -> dict[str, Any]:
    store = _store(settings)
    safe_window = max(1, min(int(window_sec or 86400), 2592000))
    stats = store.stats_with_recent(window_sec=safe_window)
    latest = _public_items(stats.pop("latest", []))
    return _strip_forbidden({
        "ok": True,
        **stats,
        **signal_stats_display(stats),
        "latest": latest,
        "message": "已读取公开信号统计",
    })
