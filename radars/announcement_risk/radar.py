from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional

from shared.binance_data import BinanceDataSource
from shared.market_links import telegram_coin_links
from ..common import (
    ANNOUNCEMENT_WORD_BLACKLIST,
    CHAIN_CONTEXT_SYMBOLS,
    CHAIN_SYMBOL_TOKEN_NAMES,
    CST,
    EXCLUDE_OPPORTUNITY_KEYWORDS,
    OPPORTUNITY_KEYWORDS,
    RISK_KEYWORDS,
    RadarComponent,
    tg_bold,
    tg_escape,
)


class AnnouncementRiskRadar(RadarComponent):
    """Binance official-announcement radar with local deduplication.

    It owns announcement classification and formatting only. Telegram delivery
    remains in the shared gateway so all five radars keep one safety boundary.
    """

    MAX_ALERTS_PER_SCAN = 8

    def build_announcement_alerts(
        self,
        source: BinanceDataSource,
        *,
        include_seen: bool = False,
    ) -> dict[str, Any]:
        try:
            articles = source.announcements(
                page_size=self.settings.announcement_page_size
            )
            contract_symbols = self._announcement_contract_symbols(source)
        except Exception as exc:
            error = type(exc).__name__
            return {
                "template_id": "TG_ANNOUNCEMENT_ALERT",
                "messages": [],
                "alerts": [],
                "articles_scanned": 0,
                "alerts_classified": 0,
                "status": "degraded",
                "error": error,
                "evidence": {
                    "status": "degraded",
                    "error": error,
                    "articles_scanned": 0,
                    "evidence_count": 0,
                    "standalone_pushes": 0,
                },
            }

        if not contract_symbols:
            return {
                "template_id": "TG_ANNOUNCEMENT_ALERT",
                "messages": [],
                "alerts": [],
                "articles_scanned": len(articles),
                "alerts_classified": 0,
                "status": "degraded",
                "error": "contract_catalog_unavailable",
                "evidence": {
                    "status": "degraded",
                    "error": "contract_catalog_unavailable",
                    "articles_scanned": len(articles),
                    "evidence_count": 0,
                    "standalone_pushes": 0,
                },
            }

        classified: list[dict[str, Any]] = []
        for article in articles:
            if not self._announcement_is_current(article):
                continue
            alert = self._classify_announcement(article, contract_symbols)
            if alert:
                classified.append(alert)

        evidence = self._store_announcement_evidence(classified, len(articles))
        state = self.store.load(self.settings.announcement_state_path, {})
        seen = state.get("seen", {}) if isinstance(state, dict) else {}
        if not isinstance(seen, dict):
            seen = {}
        pending = [
            alert for alert in classified
            if include_seen or str(alert.get("code") or "") not in seen
        ][: self.MAX_ALERTS_PER_SCAN]
        return {
            "template_id": "TG_ANNOUNCEMENT_ALERT",
            "messages": [self._format_announcement(alert) for alert in pending],
            "alerts": pending,
            "articles_scanned": len(articles),
            "alerts_classified": len(classified),
            "status": "ok" if evidence.get("status") == "ok" else "degraded",
            "error": str(evidence.get("error") or ""),
            "evidence": evidence,
        }

    def refresh_announcement_evidence(
        self,
        source: BinanceDataSource,
    ) -> dict[str, Any]:
        """Refresh the local announcement index without sending alerts."""

        return dict(self.build_announcement_alerts(source).get("evidence") or {})

    def _store_announcement_evidence(
        self,
        alerts: list[dict[str, Any]],
        articles_scanned: int,
    ) -> dict[str, Any]:
        evidence_by_symbol: dict[str, list[dict[str, Any]]] = {}
        now_ts = int(time.time())
        for alert in alerts:
            record = {
                "provider": "binance_official_announcement",
                "title": alert["title"],
                "kind": alert["kind"],
                "code": alert["code"],
                "reason": alert.get("reason", ""),
                "url": alert.get("url", ""),
                "release_ts": int(alert.get("release_ts") or 0),
                "expires_at": int(alert.get("expires_at", 0) or 0),
            }
            for symbol in alert.get("contract_symbols", []):
                normalized = str(symbol).upper()
                if not normalized.endswith("USDT"):
                    normalized = f"{normalized}USDT"
                evidence_by_symbol.setdefault(normalized, []).append(dict(record))

        for symbol, records in evidence_by_symbol.items():
            records.sort(
                key=lambda record: (
                    int(record.get("release_ts") or 0),
                    int(record.get("kind") == "risk"),
                    str(record.get("code") or ""),
                ),
                reverse=True,
            )
            evidence_by_symbol[symbol] = records[:3]

        def update(current: Any) -> dict[str, Any]:
            state = dict(current) if isinstance(current, dict) else {}
            state.update({
                "schema_version": 2,
                "evidence_updated_at": now_ts,
                "evidence_by_symbol": evidence_by_symbol,
            })
            return state

        try:
            self.store.update(self.settings.announcement_state_path, update, {})
        except Exception as exc:
            return {
                "status": "degraded",
                "error": type(exc).__name__,
                "articles_scanned": articles_scanned,
                "evidence_count": 0,
                "standalone_pushes": 0,
            }
        return {
            "status": "ok",
            "articles_scanned": articles_scanned,
            "evidence_count": sum(len(rows) for rows in evidence_by_symbol.values()),
            "symbols_with_evidence": len(evidence_by_symbol),
            "standalone_pushes": 0,
        }

    def mark_announcements_seen(self, alerts: list[dict[str, Any]]) -> None:
        if not alerts:
            return
        now_ts = int(time.time())

        def update(current: Any) -> dict[str, Any]:
            state = dict(current) if isinstance(current, dict) else {}
            seen = state.get("seen", {})
            if not isinstance(seen, dict):
                seen = {}
            else:
                seen = dict(seen)
            for alert in alerts:
                code = str(alert.get("code") or "")
                if not code:
                    continue
                release_ts = int(
                    alert.get("announcement_release_ts")
                    or alert.get("release_ts")
                    or 0
                )
                seen[code] = {
                    "title": str(alert.get("title") or ""),
                    "kind": str(alert.get("kind") or ""),
                    "symbol": str(alert.get("symbol") or ""),
                    "symbols": list(alert.get("symbols") or []),
                    "contract_symbols": list(alert.get("contract_symbols") or []),
                    "non_contract_symbols": list(alert.get("non_contract_symbols") or []),
                    "url": str(alert.get("url") or ""),
                    "release_ts": release_ts,
                    "announcement_release_ts": release_ts,
                    "expires_at": int(alert.get("expires_at") or 0),
                    "message_ids": list(alert.get("message_ids") or []),
                    "seen_at": now_ts,
                }
            cutoff = now_ts - 14 * 24 * 3600
            state["seen"] = {
                key: value
                for key, value in seen.items()
                if isinstance(value, dict)
                and int(value.get("seen_at") or now_ts) >= cutoff
            }
            return state

        self.store.update(self.settings.announcement_state_path, update, {})

    def _format_announcement(self, alert: dict[str, Any]) -> str:
        symbol_block = self._format_announcement_symbol_links(alert)
        title = tg_escape(alert.get("title") or "")
        url = escape(str(alert.get("url") or ""), quote=True)
        contract_count = len(alert.get("contract_symbols") or [])
        no_contract_count = len(alert.get("non_contract_symbols") or [])
        market_note = f"有合约{contract_count}个｜无合约{no_contract_count}个"
        if alert.get("kind") == "risk":
            return "\n".join([
                f"⚠️ {tg_bold('公告风险')}",
                "",
                f"{tg_bold('币种')}: {symbol_block}",
                f"{tg_bold('合约状态')}: {market_note}",
                f"{tg_bold('事件')}: 下架、移除交易对或停止交易",
                f"{tg_bold('公告')}: {title}",
                f"{tg_bold('官方原文')}: <a href=\"{url}\">Binance 公告</a>",
                "",
                "说明：这是官方事件提醒，不代表价格一定下跌。",
            ])
        return "\n".join([
            f"📢 {tg_bold('公告机会')}",
            "",
            f"{tg_bold('币种')}: {symbol_block}",
            f"{tg_bold('合约状态')}: {market_note}",
            f"{tg_bold('事件')}: 上新、Alpha 或活动",
            f"{tg_bold('公告')}: {title}",
            f"{tg_bold('判断')}: 先进入观察，等待资金面确认",
            f"{tg_bold('官方原文')}: <a href=\"{url}\">Binance 公告</a>",
            "",
            "说明：公告只作事件线索，不直接构成交易信号。",
        ])

    def _format_announcement_symbol_links(
        self,
        alert: dict[str, Any],
        max_count: int = 20,
    ) -> str:
        symbols = [
            str(symbol).upper()
            for symbol in alert.get("symbols", [])
            if str(symbol).strip()
        ]
        if not symbols:
            return "未识别具体币种"
        contract_symbols = {
            str(symbol).upper().removesuffix("USDT")
            for symbol in alert.get("contract_symbols", [])
        }
        parts: list[str] = []
        for symbol in symbols[:max_count]:
            base_symbol = symbol.removesuffix("USDT")
            if base_symbol in contract_symbols:
                parts.append(telegram_coin_links(symbol))
            else:
                parts.append(f"{tg_bold(symbol)}（无合约）")
        if len(symbols) > max_count:
            parts.append(f"另有{len(symbols) - max_count}个")
        return "、".join(parts) if len(parts) <= 4 else "\n" + "\n".join(
            f"- {part}" for part in parts
        )

    def _classify_announcement(
        self,
        article: dict[str, Any],
        contract_symbols: set[str] | None = None,
    ) -> Optional[dict[str, Any]]:
        title = str(article.get("title") or "")
        if not title:
            return None
        lowered = title.lower()
        code = str(article.get("code") or article.get("id") or title)
        symbols = self._extract_symbols(title)
        if not symbols:
            return None
        contract_symbols = contract_symbols or set()
        symbols_with_contract = [
            symbol for symbol in symbols
            if self._announcement_symbol_has_contract(symbol, contract_symbols)
        ]
        symbols_without_contract = [
            symbol for symbol in symbols
            if symbol not in symbols_with_contract
        ]
        symbol = self._format_symbol_list(symbols)
        url = self._announcement_url(article)
        announcement_release_ts = self._announcement_release_ts(article)
        expires_at = self._announcement_expires_at(article)
        if any(keyword in lowered for keyword in RISK_KEYWORDS):
            return {
                "kind": "risk",
                "code": code,
                "title": title,
                "symbol": symbol,
                "symbols": symbols,
                "contract_symbols": symbols_with_contract,
                "non_contract_symbols": symbols_without_contract,
                "url": url,
                "release_ts": announcement_release_ts,
                "announcement_release_ts": announcement_release_ts,
                "expires_at": expires_at,
                "priority": "high",
                "reason": "命中下架/移除/停止交易关键词",
            }
        if any(keyword in lowered for keyword in EXCLUDE_OPPORTUNITY_KEYWORDS):
            return None
        if any(keyword in lowered for keyword in OPPORTUNITY_KEYWORDS):
            return {
                "kind": "opportunity",
                "code": code,
                "title": title,
                "symbol": symbol,
                "symbols": symbols,
                "contract_symbols": symbols_with_contract,
                "non_contract_symbols": symbols_without_contract,
                "url": url,
                "release_ts": announcement_release_ts,
                "announcement_release_ts": announcement_release_ts,
                "expires_at": expires_at,
                "priority": "normal",
                "reason": "命中上新/Alpha/活动关键词",
            }
        return None

    @staticmethod
    def _extract_symbols(title: str) -> list[str]:
        title = AnnouncementRiskRadar._remove_chain_context_parentheses(title)
        symbols: list[str] = []
        for pattern in (r"\(([A-Z0-9]{2,12})\)", r"（([A-Z0-9]{2,12})）"):
            for match in re.finditer(pattern, title):
                AnnouncementRiskRadar._append_announcement_symbol(symbols, match.group(1))
        words = re.findall(r"\b[A-Z][A-Z0-9]{1,12}\b", title)
        for word in words:
            AnnouncementRiskRadar._append_announcement_symbol(symbols, word)
        return symbols[:20]

    @staticmethod
    def _append_announcement_symbol(symbols: list[str], symbol: str) -> None:
        normalized = symbol.strip().upper()
        if not normalized:
            return
        if normalized in ANNOUNCEMENT_WORD_BLACKLIST:
            return
        if re.fullmatch(r"20\d{2}", normalized):
            return
        if normalized not in symbols:
            symbols.append(normalized)

    @staticmethod
    def _remove_chain_context_parentheses(title: str) -> str:
        def replace(match: re.Match[str]) -> str:
            token_name = match.group(1).strip().lower()
            chain_symbol = match.group(2).upper()
            if chain_symbol not in CHAIN_CONTEXT_SYMBOLS:
                return match.group(0)
            valid_names = CHAIN_SYMBOL_TOKEN_NAMES.get(chain_symbol, set())
            if token_name in valid_names:
                return match.group(0)
            return match.group(1)

        return re.sub(r"\b([A-Za-z][A-Za-z0-9-]{1,32})\s*[\(（]([A-Z0-9]{2,12})[\)）]", replace, title)

    @staticmethod
    def _format_symbol_list(symbols: list[str], max_count: int = 8) -> str:
        if not symbols:
            return ""
        shown = ", ".join(symbols[:max_count])
        if len(symbols) > max_count:
            shown += f" +{len(symbols) - max_count}"
        return shown

    def _announcement_contract_symbols(self, source: BinanceDataSource) -> set[str]:
        try:
            symbols_info = source.usdt_perp_symbols()
        except Exception:
            return set()
        result: set[str] = set()
        for item in symbols_info:
            if not isinstance(item, dict):
                continue
            symbol = str(item.get("symbol") or "").upper()
            if symbol.endswith("USDT"):
                result.add(symbol[:-4])
        return result

    @staticmethod
    def _announcement_symbol_has_contract(symbol: str, contract_symbols: set[str]) -> bool:
        normalized = symbol.upper()
        if normalized.endswith("USDT"):
            normalized = normalized[:-4]
        return normalized in contract_symbols

    def _announcement_is_current(self, article: dict[str, Any]) -> bool:
        if not self.settings.announcement_only_today:
            return True
        today = datetime.now(CST).date()
        title_date = self._announcement_title_date(str(article.get("title") or ""))
        if title_date and title_date.date() < today:
            return False
        release_ts = self._announcement_release_ts(article)
        if release_ts <= 0:
            return True
        release_date = datetime.fromtimestamp(release_ts, CST).date()
        return release_date >= today

    def _announcement_expires_at(self, article: dict[str, Any]) -> int:
        title_date = self._announcement_title_date(str(article.get("title") or ""))
        if title_date:
            return int(title_date.timestamp())
        release_ts = self._announcement_release_ts(article)
        if release_ts <= 0:
            return 0
        days = max(1, int(self.settings.announcement_default_ttl_days))
        release_date = datetime.fromtimestamp(release_ts, CST).date()
        expires = datetime(
            release_date.year,
            release_date.month,
            release_date.day,
            23,
            59,
            59,
            tzinfo=CST,
        ) + timedelta(days=days)
        return int(expires.timestamp())

    @staticmethod
    def _announcement_title_date(title: str) -> Optional[datetime]:
        match = re.search(r"(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})", title)
        if not match:
            return None
        try:
            year, month, day = (int(match.group(index)) for index in (1, 2, 3))
            return datetime(year, month, day, 23, 59, 59, tzinfo=CST)
        except ValueError:
            return None

    @staticmethod
    def _announcement_release_ts(article: dict[str, Any]) -> int:
        for key in (
            "releaseDate",
            "releaseTime",
            "publishDate",
            "publishedAt",
            "publishTime",
            "createdAt",
            "date",
        ):
            value = article.get(key)
            if value in (None, ""):
                continue
            ts = AnnouncementRiskRadar._coerce_timestamp(value)
            if ts > 0:
                return ts
        return 0

    @staticmethod
    def _coerce_timestamp(value: Any) -> int:
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 10_000_000_000:
                ts /= 1000
            return int(ts)
        text = str(value).strip()
        if not text:
            return 0
        if text.isdigit():
            return AnnouncementRiskRadar._coerce_timestamp(int(text))
        try:
            normalized = text.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return int(parsed.timestamp())
        except ValueError:
            return 0

    @staticmethod
    def _announcement_url(article: dict[str, Any]) -> str:
        url = str(article.get("url") or article.get("webLink") or "")
        if url.startswith("http"):
            return url
        code = article.get("code")
        if code:
            return f"https://www.binance.com/zh-CN/support/announcement/{code}"
        return "https://www.binance.com/zh-CN/support/announcement"
