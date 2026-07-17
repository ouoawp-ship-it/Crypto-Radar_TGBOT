from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Settings
from .market_cockpit import load_market_cockpit, normalize_window
from .news_intelligence import NewsEventStore
from .signal_store import SignalEventStore


AGENT_SCHEMA_VERSION = "2026-07-17"
AGENT_ENGINE_VERSION = "2026.07.1"
AGENT_STORE_SCHEMA_VERSION = 1
AGENT_DISCLAIMER = "市场观察，不构成投资建议；结论必须结合原始证据自行验证。"
MODEL_INFO = {
    "provider": "local",
    "model": "rule-engine",
    "version": AGENT_ENGINE_VERSION,
    "mode": "deterministic_evidence_first",
    "llm_generated": False,
}


def _iso(ts: int | float | None) -> str:
    if not ts or float(ts) <= 0:
        return ""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _fmt_pct(value: Any) -> str:
    number = _number(value)
    return "—" if number is None else f"{number:+.2f}%"


def _fmt_usd(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "—"
    sign = "+" if number > 0 else "" if number == 0 else "-"
    absolute = abs(number)
    if absolute >= 1_000_000_000:
        return f"{sign}${absolute / 1_000_000_000:.2f}B"
    if absolute >= 1_000_000:
        return f"{sign}${absolute / 1_000_000:.2f}M"
    if absolute >= 1_000:
        return f"{sign}${absolute / 1_000:.1f}K"
    return f"{sign}${absolute:.2f}"


def _public_signal_ref(item: dict[str, Any]) -> str:
    return str(item.get("public_ref") or item.get("id") or "").strip()[:80]


def _insight_id(agent_type: str, scope: str) -> str:
    digest = hashlib.sha1(f"{agent_type}:{scope}".encode("utf-8")).hexdigest()[:20]
    return f"agent_{digest}"


@dataclass(frozen=True)
class AgentInsightStore:
    db_path: Path

    def __post_init__(self) -> None:
        object.__setattr__(self, "db_path", Path(self.db_path))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            self._ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agent_insights (
                insight_id TEXT PRIMARY KEY,
                agent_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                generated_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                state TEXT NOT NULL,
                confidence REAL,
                summary TEXT NOT NULL,
                evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                counter_evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                model_info_json TEXT NOT NULL DEFAULT '{}',
                data_status TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(agent_type, scope)
            );
            CREATE TABLE IF NOT EXISTS agent_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_agent_expiry ON agent_insights(expires_at DESC);
            CREATE INDEX IF NOT EXISTS idx_agent_type_generated ON agent_insights(agent_type, generated_at DESC);
            """
        )
        conn.execute("INSERT OR REPLACE INTO agent_store_meta(key, value) VALUES('schema_version', ?)", (str(AGENT_STORE_SCHEMA_VERSION),))

    def upsert_many(self, insights: list[dict[str, Any]]) -> int:
        written = 0
        with self.connect() as conn:
            for insight in insights[:100]:
                insight_id = str(insight.get("insight_id") or "").strip()
                agent_type = str(insight.get("agent_type") or "").strip()
                scope = str(insight.get("scope") or "").strip()
                if not insight_id or not agent_type or not scope:
                    continue
                generated_at = int(insight.get("generated_at_ts") or 0)
                expires_at = int(insight.get("expires_at_ts") or 0)
                conn.execute(
                    """
                    INSERT INTO agent_insights(
                        insight_id, agent_type, scope, generated_at, expires_at, state, confidence,
                        summary, evidence_refs_json, counter_evidence_refs_json, model_info_json,
                        data_status, payload_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(agent_type, scope) DO UPDATE SET
                        insight_id=excluded.insight_id, generated_at=excluded.generated_at,
                        expires_at=excluded.expires_at, state=excluded.state,
                        confidence=excluded.confidence, summary=excluded.summary,
                        evidence_refs_json=excluded.evidence_refs_json,
                        counter_evidence_refs_json=excluded.counter_evidence_refs_json,
                        model_info_json=excluded.model_info_json, data_status=excluded.data_status,
                        payload_json=excluded.payload_json
                    """,
                    (
                        insight_id, agent_type, scope, generated_at, expires_at,
                        str(insight.get("state") or "unavailable"), _number(insight.get("confidence")),
                        str(insight.get("summary") or "")[:2000],
                        json.dumps(insight.get("evidence_refs") or [], ensure_ascii=False, separators=(",", ":")),
                        json.dumps(insight.get("counter_evidence_refs") or [], ensure_ascii=False, separators=(",", ":")),
                        json.dumps(insight.get("model_info") or MODEL_INFO, ensure_ascii=False, separators=(",", ":")),
                        str(insight.get("data_status") or "unavailable"),
                        json.dumps(insight, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
                written += 1
        return written

    def list_latest(self, *, agent_type: str = "", now_ts: int | None = None, include_expired: bool = False) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if agent_type:
            clauses.append("agent_type = ?")
            params.append(agent_type)
        if not include_expired:
            clauses.append("expires_at > ?")
            params.append(int(now_ts or time.time()))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT payload_json FROM agent_insights WHERE {' AND '.join(clauses)} ORDER BY generated_at DESC LIMIT 100",
                params,
            ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                item = json.loads(str(row["payload_json"] or "{}"))
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                result.append(item)
        return result


class EvidenceBuilder:
    def __init__(self, generated_at: str):
        self.generated_at = generated_at
        self.items: dict[str, dict[str, Any]] = {}

    def add(
        self,
        *,
        kind: str,
        scope: str,
        key: str,
        label: str,
        value: Any,
        unit: str = "",
        source: str,
        observed_at: str = "",
        data_status: str = "ready",
        url: str = "",
        note: str = "",
    ) -> str:
        ref = f"ev_{hashlib.sha1(f'{kind}:{scope}:{key}:{observed_at}'.encode('utf-8')).hexdigest()[:20]}"
        self.items[ref] = {
            "ref": ref,
            "kind": kind,
            "scope": scope,
            "key": key,
            "label": label,
            "value": value,
            "unit": unit,
            "source": source,
            "observed_at": observed_at or self.generated_at,
            "data_status": data_status,
            "url": url,
            "note": note,
        }
        return ref


def _base_insight(
    agent_type: str,
    scope: str,
    *,
    now: int,
    state: str,
    confidence: float | None,
    summary: str,
    evidence_refs: list[str],
    counter_refs: list[str] | None = None,
    data_status: str,
    ttl_sec: int = 180,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "insight_id": _insight_id(agent_type, scope),
        "agent_type": agent_type,
        "scope": scope,
        "generated_at": _iso(now),
        "generated_at_ts": now,
        "expires_at": _iso(now + ttl_sec),
        "expires_at_ts": now + ttl_sec,
        "state": state,
        "confidence": round(confidence, 2) if confidence is not None else None,
        "summary": summary,
        "evidence_refs": evidence_refs,
        "counter_evidence_refs": counter_refs or [],
        "model_info": dict(MODEL_INFO),
        "data_status": data_status,
        "disclaimer": AGENT_DISCLAIMER,
        **extra,
    }


def _global_insight(cockpit: dict[str, Any], evidence: EvidenceBuilder, *, now: int) -> dict[str, Any]:
    overview = cockpit.get("overview") if isinstance(cockpit.get("overview"), dict) else {}
    coverage = cockpit.get("coverage") if isinstance(cockpit.get("coverage"), dict) else {}
    observed_at = str(cockpit.get("generated_at") or evidence.generated_at)
    refs: list[str] = []
    values = (
        ("breadth_pct", "上涨广度", overview.get("breadth_pct"), "percent", "market_cockpit"),
        ("spot_net_flow_usd", "现货主动资金差", overview.get("spot_net_flow_usd"), "usd", "market_cockpit"),
        ("futures_net_flow_usd", "合约主动资金差", overview.get("futures_net_flow_usd"), "usd", "market_cockpit"),
    )
    for key, label, value, unit, source in values:
        status = "ready" if _number(value) is not None else "unavailable"
        ref = evidence.add(kind="market_metric", scope="global", key=key, label=label, value=value, unit=unit, source=source, observed_at=observed_at, data_status=status)
        if status == "ready":
            refs.append(ref)
    data_ready = str(cockpit.get("data_status")) == "ready" and len(refs) == 3
    breadth = _number(overview.get("breadth_pct"))
    spot = _number(overview.get("spot_net_flow_usd"))
    futures = _number(overview.get("futures_net_flow_usd"))
    if not data_ready or breadth is None or spot is None or futures is None:
        return _base_insight(
            "global", "market", now=now, state="insufficient_data", confidence=None,
            summary="关键资金或广度数据未达到 ready，安全门禁已停止生成方向性结论。",
            evidence_refs=refs, data_status="degraded" if coverage.get("assets") else "unavailable",
            label="全局 Agent", state_label="数据不足", details={"coverage": coverage, "bias": overview.get("bias")},
        )
    if breadth >= 20 and spot > 0 and futures > 0:
        state, state_label = "strengthening", "同步增强"
    elif breadth <= -20 and spot < 0 and futures < 0:
        state, state_label = "weakening", "同步走弱"
    else:
        state, state_label = "mixed", "分歧观察"
    confidence = min(0.92, 0.62 + min(0.18, abs(breadth) / 200) + (0.06 if spot * futures > 0 else 0))
    summary = f"4h 市场广度为 `{_fmt_pct(breadth)}`，现货主动资金差 `{_fmt_usd(spot)}`，合约主动资金差 `{_fmt_usd(futures)}`；规则状态为{state_label}。"
    counter_refs = []
    if (breadth > 0 and spot + futures < 0) or (breadth < 0 and spot + futures > 0):
        counter_refs = refs
    return _base_insight(
        "global", "market", now=now, state=state, confidence=confidence, summary=summary,
        evidence_refs=refs, counter_refs=counter_refs, data_status="ready", label="全局 Agent",
        state_label=state_label, details={"coverage": coverage, "bias": overview.get("bias")},
    )


def _major_insight(asset: dict[str, Any] | None, symbol: str, evidence: EvidenceBuilder, *, now: int) -> dict[str, Any]:
    coin = symbol[:-4]
    if not asset:
        return _base_insight(
            "major", symbol, now=now, state="insufficient_data", confidence=None,
            summary=f"{coin} 当前没有可验证的 4h 市场快照，未生成方向结论。",
            evidence_refs=[], data_status="unavailable", label=f"{coin} 解盘 Agent", state_label="数据不足",
            actions={"coin_url": f"/coin/{symbol}", "radar_url": f"/radar?symbol={symbol}"},
        )
    observed_at = str(asset.get("updated_at") or evidence.generated_at)
    metrics = (
        ("price", "当前价格", asset.get("price"), "usd"),
        ("price_change_pct", "4h 价格方向", asset.get("price_change_pct"), "percent"),
        ("oi_change_pct", "4h OI 变化", asset.get("oi_change_pct"), "percent"),
        ("spot_flow_usd", "4h 现货主动资金差", asset.get("spot_flow_usd"), "usd"),
        ("futures_flow_usd", "4h 合约主动资金差", asset.get("futures_flow_usd"), "usd"),
        ("funding_pct", "资金费率", asset.get("funding_pct"), "percent_per_cycle"),
    )
    refs: list[str] = []
    missing: list[str] = []
    ref_by_key: dict[str, str] = {}
    for key, label, value, unit in metrics:
        status = "ready" if _number(value) is not None and str(asset.get("status") or "fresh") == "fresh" else "unavailable"
        ref = evidence.add(kind="market_metric", scope=symbol, key=key, label=label, value=value, unit=unit, source="market_snapshot_store", observed_at=observed_at, data_status=status)
        ref_by_key[key] = ref
        if status == "ready":
            refs.append(ref)
        else:
            missing.append(label)
    required = ("price_change_pct", "oi_change_pct", "spot_flow_usd", "futures_flow_usd", "funding_pct")
    ready = str(asset.get("status") or "fresh") == "fresh" and all(_number(asset.get(key)) is not None for key in required)
    if not ready:
        return _base_insight(
            "major", symbol, now=now, state="insufficient_data", confidence=None,
            summary=f"{coin} 的关键证据未全部达到 ready，缺失或降级：{'、'.join(missing) or '快照状态'}；未生成方向结论。",
            evidence_refs=refs, data_status="degraded", label=f"{coin} 解盘 Agent", state_label="数据不足",
            missing_facts=missing, actions={"coin_url": f"/coin/{symbol}", "radar_url": f"/radar?symbol={symbol}"},
        )
    price_change = float(asset["price_change_pct"])
    oi_change = float(asset["oi_change_pct"])
    spot = float(asset["spot_flow_usd"])
    futures = float(asset["futures_flow_usd"])
    funding = float(asset["funding_pct"])
    net_flow = spot + futures
    if price_change > 0 and oi_change > 0 and net_flow > 0:
        state, state_label = "strengthening", "偏强观察"
    elif price_change < 0 and oi_change > 0 and net_flow < 0:
        state, state_label = "weakening", "偏弱观察"
    elif abs(funding) >= 0.5:
        state, state_label = "crowded", "拥挤风险"
    else:
        state, state_label = "divergent", "分歧观察"
    aligned = sum((price_change > 0) == (value > 0) for value in (oi_change, spot, futures))
    confidence = min(0.9, 0.58 + aligned * 0.07)
    counter_refs: list[str] = []
    if (price_change > 0) != (net_flow > 0):
        counter_refs.extend([ref_by_key["price_change_pct"], ref_by_key["spot_flow_usd"], ref_by_key["futures_flow_usd"]])
    if abs(funding) >= 0.5:
        counter_refs.append(ref_by_key["funding_pct"])
    summary = (
        f"{coin} 4h 价格 `{_fmt_pct(price_change)}`、OI `{_fmt_pct(oi_change)}`，"
        f"现货/合约主动资金差分别为 `{_fmt_usd(spot)}` / `{_fmt_usd(futures)}`，"
        f"资金费率 `{_fmt_pct(funding)}`；规则状态为{state_label}。"
    )
    return _base_insight(
        "major", symbol, now=now, state=state, confidence=confidence, summary=summary,
        evidence_refs=refs, counter_refs=list(dict.fromkeys(counter_refs)), data_status="ready",
        label=f"{coin} 解盘 Agent", state_label=state_label,
        actions={"coin_url": f"/coin/{symbol}", "radar_url": f"/radar?symbol={symbol}"},
    )


def _anomaly_insights(signals: list[dict[str, Any]], evidence: EvidenceBuilder, *, now: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in signals:
        symbol = str(item.get("symbol") or "").upper()
        if not symbol.endswith("USDT") or str(item.get("status") or "") != "sent":
            continue
        groups.setdefault(symbol, []).append(item)
    result: list[dict[str, Any]] = []
    for symbol, items in sorted(groups.items(), key=lambda pair: max(int(row.get("ts") or 0) for row in pair[1]), reverse=True)[:6]:
        items.sort(key=lambda row: int(row.get("ts") or 0), reverse=True)
        refs: list[str] = []
        modules: set[str] = set()
        risk = False
        for item in items[:5]:
            reference = _public_signal_ref(item)
            module = str(item.get("module") or "signal")
            modules.add(module)
            text = str(item.get("signal_type") or item.get("title") or item.get("excerpt") or "结构化信号")[:180]
            lowered = text.lower()
            if module == "announcement" or any(term in lowered for term in ("risk", "风险", "下架", "移除")):
                risk = True
            refs.append(evidence.add(
                kind="signal_event", scope=symbol, key=reference or str(item.get("id") or "signal"),
                label=str(item.get("signal_type") or module), value=text, source="signal_store",
                observed_at=str(item.get("time") or evidence.generated_at), data_status="ready",
                url=f"/radar?symbol={symbol}", note="已发送的结构化雷达事件",
            ))
        bucket = "risk" if risk else "strong"
        state_label = "高风险观察" if risk else "偏强观察"
        latest_ref = _public_signal_ref(items[0])
        coin = symbol[:-4]
        summary = f"{coin} 近 4h 出现 `{len(items)}` 条已发送信号，覆盖 `{len(modules)}` 个模块；归入{state_label}，需在单币工作台验证资金与 OI。"
        result.append(_base_insight(
            "anomaly", symbol, now=now, state="observe", confidence=min(0.88, 0.58 + len(items) * 0.05 + len(modules) * 0.06),
            summary=summary, evidence_refs=refs, data_status="ready", label=f"{coin} 异常候选",
            state_label=state_label, bucket=bucket,
            actions={
                "coin_url": f"/coin/{symbol}", "radar_url": f"/radar?symbol={symbol}",
                "signal_ref": latest_ref,
            },
        ))
    return result


def _message_insights(news_items: list[dict[str, Any]], evidence: EvidenceBuilder, *, now: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in news_items:
        if str(item.get("importance") or "") != "high":
            continue
        event_id = str(item.get("event_id") or "")
        if not event_id:
            continue
        title = str(item.get("title") or "")[:300]
        ref = evidence.add(
            kind="news_event", scope=event_id, key=event_id, label="高重要度官方公告",
            value=title, source=str(item.get("source") or "official"),
            observed_at=str(item.get("published_at") or item.get("collected_at") or evidence.generated_at),
            data_status=str(item.get("data_status") or "ready"), url=str(item.get("url") or ""),
            note=str(item.get("rights_status") or "link_only"),
        )
        symbols = [str(value) for value in item.get("symbols") or []]
        scope = symbols[0] if symbols else event_id
        result.append(_base_insight(
            "message", scope, now=now, state="new_event", confidence=0.9,
            summary=f"官方公告：{title}。当前仅确认标题与来源链接，可能影响需结合行情证据验证。",
            evidence_refs=[ref], data_status="ready" if item.get("published_at") else "degraded",
            ttl_sec=900, label="消息 Agent", state_label="新增重要事件",
            event_id=event_id, symbols=symbols, actions={"info_url": f"/info?event={event_id}", "source_url": item.get("url")},
        ))
        if len(result) >= 4:
            break
    return result


def build_agent_overview(
    settings: Settings,
    *,
    now_ts: int | None = None,
    window_sec: int = 14_400,
    cockpit: dict[str, Any] | None = None,
    signals: list[dict[str, Any]] | None = None,
    news_items: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = int(now_ts or time.time())
    safe_window = normalize_window(window_sec)
    market = cockpit if cockpit is not None else load_market_cockpit(settings, window_sec=safe_window, now_ts=now)
    signal_rows = signals if signals is not None else SignalEventStore(settings.signal_events_db_path).intelligence_events(
        start_ts=now - safe_window, end_ts=now, limit=500,
    )
    if news_items is None:
        news_items = NewsEventStore(settings.news_events_db_path).list_feed(
            start_ts=now - 86_400, end_ts=now, page=1, page_size=100,
        )["items"]
    generated_at = str(market.get("generated_at") or _iso(now))
    evidence = EvidenceBuilder(generated_at)
    assets = {
        str(item.get("symbol") or ""): item
        for item in market.get("assets") or []
        if isinstance(item, dict)
    }
    global_agent = _global_insight(market, evidence, now=now)
    majors = [_major_insight(assets.get(symbol), symbol, evidence, now=now) for symbol in ("BTCUSDT", "ETHUSDT")]
    anomalies = _anomaly_insights(signal_rows, evidence, now=now)
    messages = _message_insights(news_items, evidence, now=now)
    bot_username = str(settings.ai_bot_username or "").strip().lstrip("@")
    if bot_username:
        for insight in [*majors, *anomalies]:
            symbol = str(insight.get("scope") or "")
            if not symbol.endswith("USDT"):
                continue
            coin = symbol[:-4]
            signal_ref = str((insight.get("actions") or {}).get("signal_ref") or "")
            suffix = f"_{signal_ref}" if signal_ref else ""
            insight.setdefault("actions", {})["ai_url"] = f"https://t.me/{bot_username}?start=analyze_{coin}{suffix}"
    insights = [global_agent, *majors, *anomalies, *messages]
    AgentInsightStore(settings.agent_insights_db_path).upsert_many(insights)
    ready_count = sum(1 for item in insights if item.get("data_status") == "ready")
    data_status = "ready" if global_agent.get("data_status") == "ready" else "degraded" if insights else "unavailable"
    warnings: list[str] = []
    if global_agent.get("data_status") != "ready":
        warnings.append("全局关键数据未全部达到 ready，已停止方向性结论。")
    if any(item.get("data_status") != "ready" for item in majors):
        warnings.append("BTC/ETH 的 OI、资金流或费率证据不完整时，仅展示数据不足状态。")
    if not messages:
        warnings.append("最近 24h 没有已索引的高重要度官方公告。")
    return {
        "schema_version": AGENT_SCHEMA_VERSION,
        "engine_version": AGENT_ENGINE_VERSION,
        "generated_at": _iso(now),
        "expires_at": _iso(now + 180),
        "window_sec": safe_window,
        "data_status": data_status,
        "coverage": {
            "insights": len(insights),
            "ready": ready_count,
            "evidence": len(evidence.items),
            "signals": len(signal_rows),
            "news_events": len(news_items),
        },
        "warnings": warnings,
        "agents": {
            "global": global_agent,
            "majors": majors,
            "anomalies": anomalies,
            "messages": messages,
        },
        "evidence": list(evidence.items.values()),
        "model_info": dict(MODEL_INFO),
        "safety": {
            "rule_first": True,
            "ready_only_for_direction": True,
            "numbers_formatted_by_code": True,
            "evidence_required": True,
            "disclaimer": AGENT_DISCLAIMER,
        },
    }


__all__ = [
    "AGENT_DISCLAIMER",
    "AGENT_ENGINE_VERSION",
    "AGENT_SCHEMA_VERSION",
    "AgentInsightStore",
    "build_agent_overview",
]
