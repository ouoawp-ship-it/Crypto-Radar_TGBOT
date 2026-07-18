from __future__ import annotations

import hashlib
import html
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from .config import Settings
from .data_sources import BinanceDataSource


NEWS_SCHEMA_VERSION = "2026-07-18"
NEWS_STORE_SCHEMA_VERSION = 1
NEWS_MAX_QUERY_ROWS = 500
SAFE_NEWS_HOSTS = {
    "binance.com",
    "www.binance.com",
    "panewslab.com",
    "www.panewslab.com",
    "decrypt.co",
    "www.decrypt.co",
    "blog.kraken.com",
    "bsky.app",
    "www.bsky.app",
}
HIGH_IMPORTANCE_TERMS = (
    "will list", "将上线", "delist", "delisting", "will remove", "下架", "移除",
    "停止交易", "airdrop", "launchpool", "hodler", "megadrop", "alpha",
)
MEDIUM_IMPORTANCE_TERMS = (
    "trading pairs", "futures", "margin", "maintenance", "维护", "network upgrade",
    "deposit", "withdrawal", "充提", "升级",
)
RISK_TERMS = ("delist", "delisting", "will remove", "下架", "移除", "停止交易", "suspend")
OPPORTUNITY_TERMS = ("will list", "将上线", "airdrop", "launchpool", "hodler", "megadrop", "alpha")
SYMBOL_BLACKLIST = {
    "BINANCE", "ALPHA", "WILL", "LIST", "LAUNCH", "REMOVE", "DELIST", "MARGIN",
    "LOANS", "FUTURES", "SPOT", "EARN", "HODLER", "AIRDROPS", "AIRDROP", "WITH",
    "AND", "THE", "FOR", "TAG", "SEED", "USDT", "USD", "FDUSD", "USDC", "API",
    "VIP", "NFT", "UTC",
}


def _iso(ts: int | float | None) -> str:
    if not ts or float(ts) <= 0:
        return ""
    return datetime.fromtimestamp(float(ts), timezone.utc).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any, limit: int) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, int(limit))]


def _coerce_timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        result = float(value)
        if result > 10_000_000_000:
            result /= 1000
        return int(result) if result > 0 else 0
    text = str(value or "").strip()
    if not text:
        return 0
    if text.isdigit():
        return _coerce_timestamp(int(text))
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _article_timestamp(article: dict[str, Any]) -> int:
    for key in ("releaseDate", "releaseTime", "publishDate", "publishedAt", "publishTime", "createdAt", "date"):
        result = _coerce_timestamp(article.get(key))
        if result > 0:
            return result
    return 0


def _safe_url(value: Any, code: str = "") -> str:
    raw = str(value or "").strip()
    if not raw and code:
        raw = f"https://www.binance.com/en/support/announcement/{code}"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return ""
    host = str(parsed.hostname or "").lower()
    if parsed.scheme != "https" or host not in SAFE_NEWS_HOSTS:
        return ""
    return raw[:1000]


def normalize_event_symbol(value: Any) -> str:
    token = re.sub(r"[^A-Z0-9]", "", str(value or "").upper())
    if token.endswith("USDT"):
        token = token[:-4]
    if not token or token in SYMBOL_BLACKLIST or len(token) < 2 or len(token) > 12 or re.fullmatch(r"20\d{2}", token):
        return ""
    return f"{token}USDT"


def extract_announcement_symbols(title: str) -> list[str]:
    candidates: list[str] = []
    for pattern in (r"\(([A-Z0-9]{2,12})\)", r"（([A-Z0-9]{2,12})）"):
        candidates.extend(match.group(1) for match in re.finditer(pattern, title))
    candidates.extend(re.findall(r"\b[A-Z][A-Z0-9]{1,11}\b", title))
    result: list[str] = []
    for candidate in candidates:
        symbol = normalize_event_symbol(candidate)
        if symbol and symbol not in result:
            result.append(symbol)
    return result[:20]


def _cluster_key(title: str) -> str:
    normalized = title.lower()
    normalized = re.sub(r"20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}", " date ", normalized)
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return f"cluster_{hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:20]}"


def _importance(title: str) -> str:
    lowered = title.lower()
    if any(term in lowered for term in HIGH_IMPORTANCE_TERMS):
        return "high"
    if any(term in lowered for term in MEDIUM_IMPORTANCE_TERMS):
        return "medium"
    return "low"


def _event_kind(title: str) -> str:
    lowered = title.lower()
    if any(term in lowered for term in RISK_TERMS):
        return "risk"
    if any(term in lowered for term in OPPORTUNITY_TERMS):
        return "opportunity"
    return "neutral"


def _language(title: str) -> str:
    return "zh" if re.search(r"[\u4e00-\u9fff]", title) else "en"


def _analysis(title: str, symbols: list[str], importance: str, kind: str) -> dict[str, Any]:
    if importance != "high":
        return {
            "status": "not_generated",
            "reason": "仅对高重要度且已去重事件生成规则化解读",
            "generated_by": "rule_engine",
        }
    possible = "可能提高关联资产的短期波动，方向仍需结合价格、成交量、OI 与资金流验证。"
    if kind == "risk":
        possible = "可能带来流动性、可交易性或价格波动风险，需核对公告生效时间与持仓风险。"
    elif kind == "opportunity":
        possible = "可能提升短期关注度与成交活跃度，不代表价格必然上涨。"
    return {
        "status": "ready",
        "fact_summary": title,
        "possible_impact": possible,
        "related_assets": symbols,
        "verification_needed": ["核对官方原文和生效时间", "验证市场是否已通过价格、成交量、OI 或资金流反应"],
        "fact_inference_boundary": "fact_summary 来自官方标题；possible_impact 为规则推断，不是事实或投资建议。",
        "generated_by": "rule_engine",
        "version": "2026.07.1",
    }


def normalize_binance_articles(articles: list[dict[str, Any]], *, collected_at: int | None = None) -> list[dict[str, Any]]:
    collected = int(collected_at or time.time())
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = _clean_text(article.get("title"), 500)
        code = _clean_text(article.get("code") or article.get("id"), 200)
        url = _safe_url(article.get("url") or article.get("webLink"), code)
        if not title or not url:
            continue
        event_id = f"binance_{hashlib.sha1((code or title).encode('utf-8')).hexdigest()[:24]}"
        if event_id in seen:
            continue
        seen.add(event_id)
        symbols = extract_announcement_symbols(title)
        importance = _importance(title)
        kind = _event_kind(title)
        published_at = _article_timestamp(article)
        result.append({
            "event_id": event_id,
            "published_at": published_at,
            "source": "Binance",
            "source_type": "official_announcement",
            "title": title,
            "summary": "",
            "url": url,
            "symbols": symbols,
            "importance": importance,
            "language": _language(title),
            "cluster_id": _cluster_key(title),
            "event_kind": kind,
            "ai_analysis": _analysis(title, symbols, importance, kind),
            "rights_status": "official_link_only",
            "source_links": [{"source": "Binance", "url": url, "rights_status": "official_link_only"}],
            "timestamp_quality": "source" if published_at > 0 else "missing",
            "collected_at": collected,
        })
    return result


@dataclass(frozen=True)
class NewsEventStore:
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
            conn.execute("PRAGMA foreign_keys=ON")
            self._ensure_schema(conn)
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _ensure_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS news_events (
                event_id TEXT PRIMARY KEY,
                published_at INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                source_type TEXT NOT NULL,
                title TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL,
                importance TEXT NOT NULL DEFAULT 'low',
                language TEXT NOT NULL DEFAULT 'en',
                cluster_id TEXT NOT NULL,
                event_kind TEXT NOT NULL DEFAULT 'neutral',
                ai_analysis_json TEXT NOT NULL DEFAULT '{}',
                rights_status TEXT NOT NULL,
                source_links_json TEXT NOT NULL DEFAULT '[]',
                timestamp_quality TEXT NOT NULL DEFAULT 'missing',
                collected_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS news_event_symbols (
                event_id TEXT NOT NULL REFERENCES news_events(event_id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                PRIMARY KEY(event_id, symbol)
            );
            CREATE TABLE IF NOT EXISTS news_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_news_published ON news_events(published_at DESC, collected_at DESC);
            CREATE INDEX IF NOT EXISTS idx_news_cluster ON news_events(cluster_id, published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_news_source_type ON news_events(source_type, published_at DESC);
            CREATE INDEX IF NOT EXISTS idx_news_symbol ON news_event_symbols(symbol, event_id);
            """
        )
        conn.execute("INSERT OR REPLACE INTO news_store_meta(key, value) VALUES('schema_version', ?)", (str(NEWS_STORE_SCHEMA_VERSION),))

    def upsert_many(self, events: list[dict[str, Any]]) -> int:
        now = int(time.time())
        written = 0
        with self.connect() as conn:
            for event in events[:2000]:
                event_id = str(event.get("event_id") or "").strip()
                title = _clean_text(event.get("title"), 500)
                url = _safe_url(event.get("url"), "")
                if not event_id or not title or not url:
                    continue
                conn.execute(
                    """
                    INSERT INTO news_events(
                        event_id, published_at, source, source_type, title, summary, url, importance,
                        language, cluster_id, event_kind, ai_analysis_json, rights_status,
                        source_links_json, timestamp_quality, collected_at, updated_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(event_id) DO UPDATE SET
                        published_at=excluded.published_at, source=excluded.source,
                        source_type=excluded.source_type, title=excluded.title, summary=excluded.summary,
                        url=excluded.url, importance=excluded.importance, language=excluded.language,
                        cluster_id=excluded.cluster_id, event_kind=excluded.event_kind,
                        ai_analysis_json=excluded.ai_analysis_json, rights_status=excluded.rights_status,
                        source_links_json=excluded.source_links_json,
                        timestamp_quality=excluded.timestamp_quality, collected_at=excluded.collected_at,
                        updated_at=excluded.updated_at
                    """,
                    (
                        event_id, int(event.get("published_at") or 0), _clean_text(event.get("source"), 80),
                        _clean_text(event.get("source_type"), 60), title, _clean_text(event.get("summary"), 1000),
                        url, str(event.get("importance") or "low"), str(event.get("language") or "en"),
                        str(event.get("cluster_id") or _cluster_key(title)), str(event.get("event_kind") or "neutral"),
                        json.dumps(event.get("ai_analysis") or {}, ensure_ascii=False, separators=(",", ":")),
                        str(event.get("rights_status") or "link_only"),
                        json.dumps(event.get("source_links") or [], ensure_ascii=False, separators=(",", ":")),
                        str(event.get("timestamp_quality") or "missing"), int(event.get("collected_at") or now), now,
                    ),
                )
                conn.execute("DELETE FROM news_event_symbols WHERE event_id = ?", (event_id,))
                for symbol in event.get("symbols") or []:
                    normalized = normalize_event_symbol(symbol)
                    if normalized:
                        conn.execute("INSERT OR IGNORE INTO news_event_symbols(event_id, symbol) VALUES(?, ?)", (event_id, normalized))
                written += 1
        return written

    def latest_collected_at(self) -> int:
        with self.connect() as conn:
            row = conn.execute("SELECT MAX(collected_at) AS value FROM news_events").fetchone()
        return int(row["value"] or 0) if row else 0

    def channel_counts(self, *, start_ts: int = 0, end_ts: int = 0) -> dict[str, int]:
        clauses = ["1=1"]
        params: list[Any] = []
        if start_ts > 0:
            clauses.append("COALESCE(NULLIF(published_at, 0), collected_at) >= ?")
            params.append(int(start_ts))
        if end_ts > 0:
            clauses.append("COALESCE(NULLIF(published_at, 0), collected_at) <= ?")
            params.append(int(end_ts))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT source_type, language, COUNT(*) AS count FROM news_events WHERE {' AND '.join(clauses)} GROUP BY source_type, language",
                params,
            ).fetchall()
        result: dict[str, int] = {}
        for row in rows:
            source_type = str(row["source_type"] or "")
            language = str(row["language"] or "")
            count = int(row["count"] or 0)
            result[source_type] = result.get(source_type, 0) + count
            result[f"{source_type}:{language}"] = result.get(f"{source_type}:{language}", 0) + count
        return result

    def prune(self, *, now_ts: int | None = None, retention_days: int = 90, limit: int = 5000) -> dict[str, int]:
        now = int(now_ts or time.time())
        cutoff = now - max(1, int(retention_days or 90)) * 86_400
        safe_limit = max(100, min(100_000, int(limit or 5000)))
        with self.connect() as conn:
            before = int(conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0])
            conn.execute(
                "DELETE FROM news_events WHERE COALESCE(NULLIF(published_at, 0), collected_at) < ?",
                (cutoff,),
            )
            conn.execute(
                """
                DELETE FROM news_events
                WHERE event_id NOT IN (
                    SELECT event_id FROM news_events
                    ORDER BY COALESCE(NULLIF(published_at, 0), collected_at) DESC, event_id DESC
                    LIMIT ?
                )
                """,
                (safe_limit,),
            )
            conn.execute("DELETE FROM news_event_symbols WHERE event_id NOT IN (SELECT event_id FROM news_events)")
            after = int(conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0])
        return {"before": before, "after": after, "removed": max(0, before - after)}

    def list_feed(
        self,
        *,
        start_ts: int = 0,
        end_ts: int = 0,
        source_type: str = "",
        language: str = "",
        importance: str = "",
        symbol: str = "",
        query: str = "",
        page: int = 1,
        page_size: int = 30,
    ) -> dict[str, Any]:
        clauses = ["1=1"]
        params: list[Any] = []
        if start_ts > 0:
            clauses.append("COALESCE(NULLIF(n.published_at, 0), n.collected_at) >= ?")
            params.append(int(start_ts))
        if end_ts > 0:
            clauses.append("COALESCE(NULLIF(n.published_at, 0), n.collected_at) <= ?")
            params.append(int(end_ts))
        if source_type:
            clauses.append("n.source_type = ?")
            params.append(str(source_type)[:60])
        if language in {"zh", "en"}:
            clauses.append("n.language = ?")
            params.append(language)
        if importance in {"high", "medium", "low"}:
            clauses.append("n.importance = ?")
            params.append(importance)
        normalized_symbol = normalize_event_symbol(symbol)
        if normalized_symbol:
            clauses.append("EXISTS(SELECT 1 FROM news_event_symbols ns WHERE ns.event_id=n.event_id AND ns.symbol=?)")
            params.append(normalized_symbol)
        search = _clean_text(query, 80)
        if search:
            clauses.append("(n.title LIKE ? ESCAPE '\\' OR n.summary LIKE ? ESCAPE '\\')")
            pattern = f"%{search.replace('%', '\\%').replace('_', '\\_')}%"
            params.extend((pattern, pattern))
        where = " AND ".join(clauses)
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT n.* FROM news_events n WHERE {where} ORDER BY COALESCE(NULLIF(n.published_at, 0), n.collected_at) DESC, n.event_id DESC LIMIT ?",
                [*params, NEWS_MAX_QUERY_ROWS],
            ).fetchall()
            event_ids = [str(row["event_id"]) for row in rows]
            symbols_by_event: dict[str, list[str]] = {event_id: [] for event_id in event_ids}
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                for row in conn.execute(
                    f"SELECT event_id, symbol FROM news_event_symbols WHERE event_id IN ({placeholders}) ORDER BY symbol",
                    event_ids,
                ).fetchall():
                    symbols_by_event[str(row["event_id"])].append(str(row["symbol"]))

        clusters: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = self._row_item(row, symbols_by_event.get(str(row["event_id"]), []))
            cluster_id = item["cluster_id"]
            if cluster_id not in clusters:
                clusters[cluster_id] = item
                continue
            primary = clusters[cluster_id]
            existing = {(str(link.get("source")), str(link.get("url"))) for link in primary["source_links"]}
            for link in item["source_links"]:
                key = (str(link.get("source")), str(link.get("url")))
                if key not in existing:
                    primary["source_links"].append(link)
                    existing.add(key)
            primary["cluster_size"] += 1
            primary["symbols"] = sorted(set(primary["symbols"] + item["symbols"]))

        grouped = list(clusters.values())
        safe_size = max(1, min(100, int(page_size or 30)))
        safe_page = max(1, int(page or 1))
        total = len(grouped)
        start = (safe_page - 1) * safe_size
        return {
            "items": grouped[start:start + safe_size],
            "pagination": {
                "page": safe_page,
                "page_size": safe_size,
                "page_count": max(1, (total + safe_size - 1) // safe_size),
                "total": total,
                "bounded_at": NEWS_MAX_QUERY_ROWS,
            },
        }

    @staticmethod
    def _row_item(row: sqlite3.Row, symbols: list[str]) -> dict[str, Any]:
        def load_json(key: str, fallback: Any) -> Any:
            try:
                value = json.loads(str(row[key] or ""))
            except (json.JSONDecodeError, TypeError):
                return fallback
            return value

        published_at = int(row["published_at"] or 0)
        collected_at = int(row["collected_at"] or 0)
        links = load_json("source_links_json", [])
        if not isinstance(links, list):
            links = []
        return {
            "event_id": str(row["event_id"]),
            "published_at": _iso(published_at),
            "collected_at": _iso(collected_at),
            "source": str(row["source"]),
            "source_type": str(row["source_type"]),
            "title": str(row["title"]),
            "summary": str(row["summary"]),
            "url": str(row["url"]),
            "symbols": symbols,
            "importance": str(row["importance"]),
            "language": str(row["language"]),
            "cluster_id": str(row["cluster_id"]),
            "cluster_size": 1,
            "event_kind": str(row["event_kind"]),
            "ai_analysis": load_json("ai_analysis_json", {}),
            "rights_status": str(row["rights_status"]),
            "source_links": links[:10],
            "timestamp_quality": str(row["timestamp_quality"]),
            "data_status": "ready" if published_at > 0 else "degraded",
        }


def ingest_binance_announcements(
    settings: Settings,
    *,
    articles: list[dict[str, Any]] | None = None,
    source: BinanceDataSource | None = None,
    max_pages: int = 1,
    now_ts: int | None = None,
) -> dict[str, Any]:
    collector = source or BinanceDataSource(settings)
    try:
        rows = articles if articles is not None else collector.announcements(page_size=min(50, settings.announcement_page_size), max_pages=max_pages)
        events = normalize_binance_articles(rows, collected_at=now_ts)
        store = NewsEventStore(settings.news_events_db_path)
        written = store.upsert_many(events)
        retention = store.prune(
            now_ts=now_ts,
            retention_days=settings.news_events_retention_days,
            limit=settings.news_events_limit,
        )
        return {
            "source": "Binance",
            "source_type": "official_announcement",
            "articles": len(rows),
            "events": len(events),
            "written": written,
            "retention": retention,
            "rights_status": "official_link_only",
        }
    finally:
        if source is None:
            collector.http.close()


__all__ = [
    "NEWS_SCHEMA_VERSION",
    "NewsEventStore",
    "extract_announcement_symbols",
    "ingest_binance_announcements",
    "normalize_binance_articles",
    "normalize_event_symbol",
]
