from __future__ import annotations

import hashlib
import html
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterable
from urllib.parse import quote, urlparse

import requests

from .config import Settings
from .news_intelligence import NewsEventStore, ingest_binance_announcements


INFO_SOURCE_SCHEMA_VERSION = "2026-07-18"
PUBLIC_INFO_RSS_SOURCES: tuple[dict[str, str], ...] = (
    {
        "id": "panews_zh",
        "name": "PANews",
        "url": "https://www.panewslab.com/rss.xml?lang=zh&type=NEWS",
        "language": "zh",
    },
    {
        "id": "decrypt_en",
        "name": "Decrypt",
        "url": "https://decrypt.co/feed",
        "language": "en",
    },
    {
        "id": "kraken_blog_en",
        "name": "Kraken Blog",
        "url": "https://blog.kraken.com/feed",
        "language": "en",
    },
)
DEFAULT_KOL_HANDLES: tuple[str, ...] = (
    "vitalik.ca",
    "brian-armstrong.bsky.social",
    "cobie.bsky.social",
    "saylor.bsky.social",
    "aantonop.com",
)
DEFAULT_PLAZA_FEED_URI = "at://did:plc:5cgr3vgieoz4dh5nkhofpn33/app.bsky.feed.generator/aaaekwiqyodf4"
BLUESKY_PUBLIC_API = "https://public.api.bsky.app/xrpc"

_KNOWN_SYMBOLS = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "ether": "ETHUSDT",
    "eth": "ETHUSDT",
    "solana": "SOLUSDT",
    "sol": "SOLUSDT",
    "dogecoin": "DOGEUSDT",
    "doge": "DOGEUSDT",
    "ripple": "XRPUSDT",
    "xrp": "XRPUSDT",
    "cardano": "ADAUSDT",
    "ada": "ADAUSDT",
    "hyperliquid": "HYPEUSDT",
    "hype": "HYPEUSDT",
    "binance coin": "BNBUSDT",
    "bnb": "BNBUSDT",
    "chainlink": "LINKUSDT",
    "link": "LINKUSDT",
    "avalanche": "AVAXUSDT",
    "avax": "AVAXUSDT",
}
_SYMBOL_ALLOWLIST = {
    "AAVE", "ADA", "ALGO", "APT", "ARB", "ATOM", "AVAX", "BCH", "BNB", "BONK", "BTC",
    "CRV", "CRO", "DOGE", "DOT", "ENA", "ETC", "ETH", "FET", "FIL", "HBAR", "HYPE", "ICP",
    "INJ", "JTO", "JUP", "KAS", "LDO", "LINK", "LTC", "MATIC", "MKR", "NEAR", "ONDO", "OP",
    "ORDI", "PENDLE", "PEPE", "POL", "PYTH", "QNT", "RENDER", "RUNE", "SEI", "SHIB", "SOL",
    "STX", "SUI", "TAO", "TIA", "TON", "TRX", "UNI", "WIF", "XLM", "XMR", "XRP", "ZEC",
}
_RISK_TERMS = (
    "bearish", "sell-off", "selloff", "hack", "exploit", "scam", "fraud", "liquidation",
    "delist", "risk", "下跌", "暴跌", "抛售", "黑客", "攻击", "清算", "诈骗", "风险", "下架",
)
_OPPORTUNITY_TERMS = (
    "bullish", "breakout", "surge", "rally", "inflow", "accumulate", "listing",
    "上涨", "突破", "反弹", "流入", "增持", "上线", "利好",
)


def _clean(value: Any, limit: int = 500) -> str:
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]*>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[: max(0, int(limit))]


def _timestamp(value: Any) -> int:
    if isinstance(value, (int, float)):
        result = float(value)
        if result > 10_000_000_000:
            result /= 1000
        return int(result) if result > 0 else 0
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError, OverflowError):
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return 0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp())


def _symbols(text: str) -> list[str]:
    result: list[str] = []
    lowered = text.lower()
    for term, symbol in _KNOWN_SYMBOLS.items():
        if re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", lowered) and symbol not in result:
            result.append(symbol)
    for candidate in re.findall(r"\$([A-Z][A-Z0-9]{1,11})\b", text):
        symbol = f"{candidate}USDT" if not candidate.endswith("USDT") else candidate
        if symbol not in result:
            result.append(symbol)
    for candidate in re.findall(r"\b([A-Z][A-Z0-9]{1,11})\b", text):
        base = candidate[:-4] if candidate.endswith("USDT") else candidate
        if base not in _SYMBOL_ALLOWLIST:
            continue
        symbol = f"{base}USDT"
        if symbol not in result:
            result.append(symbol)
    return result[:20]


def _event_kind(text: str) -> str:
    lowered = text.lower()
    if any(term in lowered for term in _RISK_TERMS):
        return "risk"
    if any(term in lowered for term in _OPPORTUNITY_TERMS):
        return "opportunity"
    return "neutral"


def _importance(text: str, engagement: int = 0) -> str:
    lowered = text.lower()
    if engagement >= 500 or any(term in lowered for term in ("hack", "exploit", "delist", "listing", "黑客", "攻击", "下架", "上线")):
        return "high"
    if engagement >= 100 or any(term in lowered for term in ("funding", "etf", "regulation", "监管", "资金费率")):
        return "medium"
    return "low"


def _cluster_id(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text.lower())
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return f"cluster_{hashlib.sha1(normalized.encode('utf-8')).hexdigest()[:20]}"


def _event_id(prefix: str, identity: str) -> str:
    return f"{prefix}_{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:24]}"


def _element_text(element: ET.Element, names: Iterable[str]) -> str:
    wanted = set(names)
    for child in element:
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name in wanted:
            return str(child.text or child.attrib.get("href") or "")
    return ""


def normalize_rss_feed(
    xml_text: str,
    *,
    source_id: str,
    source_name: str,
    language: str,
    collected_at: int | None = None,
    limit: int = 40,
) -> list[dict[str, Any]]:
    collected = int(collected_at or time.time())
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    entries = [element for element in root.iter() if element.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    result: list[dict[str, Any]] = []
    for entry in entries[: max(1, min(100, int(limit)) )]:
        title = _clean(_element_text(entry, ("title",)), 500)
        url = _clean(_element_text(entry, ("link",)), 1000)
        identity = _clean(_element_text(entry, ("guid", "id")), 1000) or url or title
        summary = _clean(_element_text(entry, ("description", "summary", "content", "encoded")), 280)
        published_at = _timestamp(_element_text(entry, ("pubDate", "published", "updated", "date")))
        if not title or not url or urlparse(url).scheme != "https":
            continue
        symbols = _symbols(f"{title} {summary}")
        kind = _event_kind(f"{title} {summary}")
        importance = _importance(f"{title} {summary}")
        result.append({
            "event_id": _event_id(source_id, identity),
            "published_at": published_at,
            "source": source_name,
            "source_type": "news",
            "title": title,
            "summary": summary if summary != title else "",
            "url": url,
            "symbols": symbols,
            "importance": importance,
            "language": language,
            "cluster_id": _cluster_id(title),
            "event_kind": kind,
            "ai_analysis": {
                "status": "not_generated",
                "reason": "公开 RSS 元数据；保留来源回链，不复制完整正文。",
                "generated_by": "source_normalizer",
            },
            "rights_status": "public_rss_link",
            "source_links": [{"source": source_name, "url": url, "rights_status": "public_rss_link"}],
            "timestamp_quality": "source" if published_at else "missing",
            "collected_at": collected,
        })
    return result


def normalize_bluesky_feed(
    payload: dict[str, Any],
    *,
    source_type: str,
    collected_at: int | None = None,
    limit: int = 80,
) -> list[dict[str, Any]]:
    collected = int(collected_at or time.time())
    rows = payload.get("feed") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    result: list[dict[str, Any]] = []
    for row in rows[: max(1, min(120, int(limit)))]:
        post = row.get("post") if isinstance(row, dict) else None
        if not isinstance(post, dict):
            continue
        record = post.get("record") if isinstance(post.get("record"), dict) else {}
        author = post.get("author") if isinstance(post.get("author"), dict) else {}
        text = _clean(record.get("text"), 500)
        uri = str(post.get("uri") or "")
        handle = _clean(author.get("handle"), 120)
        display_name = _clean(author.get("displayName"), 120) or handle
        rkey = uri.rsplit("/", 1)[-1] if "/" in uri else ""
        url = f"https://bsky.app/profile/{quote(handle, safe='.-')}/post/{quote(rkey, safe='')}" if handle and rkey else ""
        if not text or not url:
            continue
        likes = max(0, int(post.get("likeCount") or 0))
        reposts = max(0, int(post.get("repostCount") or 0))
        replies = max(0, int(post.get("replyCount") or 0))
        engagement = likes + 2 * reposts + replies
        published_at = _timestamp(record.get("createdAt") or post.get("indexedAt"))
        symbols = _symbols(text)
        result.append({
            "event_id": _event_id(f"bsky_{source_type}", uri or url),
            "published_at": published_at,
            "source": f"@{handle}" if handle else display_name,
            "source_type": source_type,
            "title": text,
            "summary": "",
            "url": url,
            "symbols": symbols,
            "importance": _importance(text, engagement),
            "language": "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en",
            "cluster_id": _cluster_id(text),
            "event_kind": _event_kind(text),
            "ai_analysis": {
                "status": "not_generated",
                "reason": "公开社交帖子元数据；情绪标签由规则引擎生成。",
                "generated_by": "social_rule_engine",
                "author_handle": handle,
                "author_display_name": display_name,
                "engagement": {"likes": likes, "reposts": reposts, "replies": replies, "score": engagement},
            },
            "rights_status": "public_social_link",
            "source_links": [{"source": display_name, "url": url, "rights_status": "public_social_link"}],
            "timestamp_quality": "source" if published_at else "missing",
            "collected_at": collected,
        })
    return result


def ingest_public_info_sources(
    settings: Settings,
    *,
    now_ts: int | None = None,
    session: requests.Session | None = None,
    include_binance: bool = True,
    rss_sources: tuple[dict[str, str], ...] | None = None,
    kol_handles: tuple[str, ...] | None = None,
    plaza_feed_uri: str | None = None,
) -> dict[str, Any]:
    now = int(now_ts or time.time())
    owns_session = session is None
    client = session or requests.Session()
    client.headers.update({"User-Agent": "PaoxxRadar/1.0 (+https://paoxx.com)", "Accept": "application/json, application/rss+xml, application/xml;q=0.9"})
    sources: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    events: list[dict[str, Any]] = []
    timeout = max(3, min(15, int(settings.http_timeout_sec or 10)))

    try:
        if include_binance:
            try:
                result = ingest_binance_announcements(settings, max_pages=1, now_ts=now)
                sources.append({"id": "binance_announcements", "status": "ready", "events": int(result.get("events") or 0)})
            except Exception as exc:
                errors.append({"id": "binance_announcements", "error": type(exc).__name__})

        if settings.info_public_sources_enable:
            for spec in rss_sources or PUBLIC_INFO_RSS_SOURCES:
                source_id = str(spec.get("id") or "rss")
                try:
                    response = client.get(str(spec["url"]), timeout=timeout)
                    response.raise_for_status()
                    normalized = normalize_rss_feed(
                        response.text,
                        source_id=source_id,
                        source_name=str(spec["name"]),
                        language=str(spec.get("language") or "en"),
                        collected_at=now,
                    )
                    events.extend(normalized)
                    sources.append({"id": source_id, "status": "ready" if normalized else "empty", "events": len(normalized)})
                except Exception as exc:
                    errors.append({"id": source_id, "error": type(exc).__name__})

            handles = kol_handles if kol_handles is not None else settings.info_kol_handles
            for handle in handles[:12]:
                source_id = f"bsky_kol:{handle}"
                try:
                    response = client.get(
                        f"{BLUESKY_PUBLIC_API}/app.bsky.feed.getAuthorFeed",
                        params={"actor": handle, "limit": 12, "filter": "posts_no_replies"},
                        timeout=timeout,
                    )
                    response.raise_for_status()
                    normalized = normalize_bluesky_feed(response.json(), source_type="kol", collected_at=now, limit=12)
                    events.extend(normalized)
                    sources.append({"id": source_id, "status": "ready" if normalized else "empty", "events": len(normalized)})
                except Exception as exc:
                    errors.append({"id": source_id, "error": type(exc).__name__})

            feed_uri = settings.info_plaza_feed_uri if plaza_feed_uri is None else plaza_feed_uri
            if feed_uri:
                try:
                    response = client.get(
                        f"{BLUESKY_PUBLIC_API}/app.bsky.feed.getFeed",
                        params={"feed": feed_uri, "limit": 80},
                        timeout=timeout,
                    )
                    response.raise_for_status()
                    normalized = normalize_bluesky_feed(response.json(), source_type="plaza", collected_at=now, limit=80)
                    events.extend(normalized)
                    sources.append({"id": "bsky_crypto_plaza", "status": "ready" if normalized else "empty", "events": len(normalized)})
                except Exception as exc:
                    errors.append({"id": "bsky_crypto_plaza", "error": type(exc).__name__})

        store = NewsEventStore(settings.news_events_db_path)
        written = store.upsert_many(events) if events else 0
        retention = store.prune(
            now_ts=now,
            retention_days=settings.news_events_retention_days,
            limit=settings.news_events_limit,
        )
        return {
            "schema_version": INFO_SOURCE_SCHEMA_VERSION,
            "sources": sources,
            "errors": errors,
            "events": len(events),
            "written": written,
            "retention": retention,
            "status": "ready" if sources and not errors else "partial" if sources else "unavailable",
        }
    finally:
        if owns_session:
            client.close()


__all__ = [
    "DEFAULT_KOL_HANDLES",
    "DEFAULT_PLAZA_FEED_URI",
    "INFO_SOURCE_SCHEMA_VERSION",
    "PUBLIC_INFO_RSS_SOURCES",
    "ingest_public_info_sources",
    "normalize_bluesky_feed",
    "normalize_rss_feed",
]
