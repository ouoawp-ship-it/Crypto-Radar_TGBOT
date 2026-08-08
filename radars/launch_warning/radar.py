from __future__ import annotations

import hashlib
import sqlite3
import sys
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from html import escape
from math import isfinite
from typing import Any, Callable, Optional

import requests

from shared.asset_classification import classify_binance_instrument
from shared.binance_confirmation import (
    apply_binance_confirmation,
    confirmation_summary,
    confirmation_text,
)
from shared.binance_data import BinanceDataSource
from .chart import DISPLAY_CANDLE_LIMIT, render_launch_chart_png
from .ai_interpreter import (
    OpenAiCompatibleLaunchInterpreter,
    build_launch_ai_context,
)
from .ai_on_demand import (
    positive_telegram_user_id,
    telegram_bot_username_configured,
)
from .candidates import select_launch_candidates
from .directional_formatter import format_launch_directional_signal
from .directional_model import evaluate_directional_readiness
from .signal_phase import (
    build_one_hour_phase_summary,
    classify_launch_phase,
)
from .directional_runtime import (
    active_flow_window,
    build_directional_facts,
    build_trade_plans,
    select_directional_candidates,
)
from .fusion_formatter import format_launch_fusion_package
from .lifecycle import LaunchLifecycleStore
from .market_facts import (
    INTERVAL_MS,
    OI_24H_REQUIRED_POINTS,
    build_launch_market_facts,
    closed_kline_active_flow,
)
from .multi_timeframe import (
    TIMEFRAME_INTERVAL_MS,
    analyze_multi_timeframe,
    expand_timeframe_klines,
)
from .price_action import analyze_launch_price_action, required_15m_kline_limit
from .scoring import SCORE_SEMANTICS, score_launch_signal
from shared.funding_presentation import funding_table
from shared.time_windows import closed_window
from ..common import (
    CST,
    LAUNCH_SUPPORTING_EVIDENCE_MAX_AGE_SEC,
    RadarComponent,
    coin_link,
    compact_launch_state_records,
    cst_now_text,
    fmt_money,
    fmt_price,
    funding_cycle_text,
    funding_extreme_label,
    funding_interval_hours,
    funding_interval_label,
    funding_interval_transition,
    launch_end_reason_text,
    launch_funds_direction,
    liquidity_tier,
    market_cap_tier,
    pct,
    tg_bold,
    tg_escape,
    tg_quote,
    to_float,
)


def _optional_finite(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if isfinite(parsed) else None


def _premium_basis_pct(premium: Any) -> float | None:
    if not isinstance(premium, dict):
        return None
    mark = _optional_finite(premium.get("markPrice"))
    index = _optional_finite(premium.get("indexPrice"))
    if mark is None or index is None or index <= 0:
        return None
    return (mark / index - 1.0) * 100.0


class LaunchWarningRadar(RadarComponent):
    _AI_CACHE_VERSION = "launch-ai-interpreter-v4"
    _PREVIOUS_AI_CACHE_VERSION = "launch-ai-interpreter-v3"
    _LEGACY_AI_CACHE_VERSIONS = ("launch-ai-interpreter-v2",)

    def _launch_fusion_active(self) -> bool:
        return bool(
            self.settings.launch_fusion_enable
            and self.settings.launch_lifecycle_v2_enable
            and self.settings.launch_message_package_v2_enable
        )

    def _launch_directional_active(self) -> bool:
        return bool(
            getattr(self.settings, "launch_directional_enable", False)
            and self._launch_fusion_active()
        )

    def _load_launch_announcement_evidence(
        self,
        *,
        now_ts: int,
    ) -> dict[str, list[dict[str, Any]]]:
        state = self.store.load(self.settings.announcement_state_path, {})
        if not isinstance(state, dict):
            return {}
        updated_at = int(state.get("evidence_updated_at") or 0)
        age_sec = now_ts - updated_at
        if (
            updated_at <= 0
            or age_sec < 0
            or age_sec > LAUNCH_SUPPORTING_EVIDENCE_MAX_AGE_SEC
        ):
            return {}
        raw = state.get("evidence_by_symbol")
        if not isinstance(raw, dict):
            return {}
        result: dict[str, list[dict[str, Any]]] = {}
        for symbol, values in raw.items():
            if not isinstance(values, list):
                continue
            active = [
                dict(value)
                for value in values
                if isinstance(value, dict)
                and int(value.get("expires_at") or 0) > now_ts
            ]
            if active:
                result[str(symbol).upper()] = active[:3]
        return result

    def _load_launch_accumulation_evidence(
        self,
        *,
        now_ts: int,
    ) -> dict[str, dict[str, Any]]:
        state = self.store.load(
            self.settings.accumulation_quality_diagnostics_path,
            {},
        )
        if not isinstance(state, dict):
            return {}
        scans = state.get("scans")
        if not isinstance(scans, list):
            return {}
        latest = next(
            (
                scan
                for scan in reversed(scans)
                if isinstance(scan, dict)
                and int(scan.get("scan_completed_at") or 0) > 0
            ),
            None,
        )
        if not isinstance(latest, dict):
            return {}
        completed_at = int(latest.get("scan_completed_at") or 0)
        age_sec = now_ts - completed_at
        if age_sec < 0 or age_sec > LAUNCH_SUPPORTING_EVIDENCE_MAX_AGE_SEC:
            return {}
        results = latest.get("results")
        if not isinstance(results, list):
            return {}
        return {
            str(result.get("symbol") or "").upper(): {
                **dict(result),
                "evidence_status": "available",
                "evidence_age_sec": max(0, now_ts - completed_at),
                "score_effect": "supporting_only",
            }
            for result in results
            if isinstance(result, dict) and str(result.get("symbol") or "").strip()
        }

    def build_launch_alerts(self, source: BinanceDataSource) -> dict[str, Any]:
        fusion_active = self._launch_fusion_active()
        budget_cap = min(self.settings.oi_hist_budget, self.settings.kline_budget)
        if self.settings.launch_scan_limit <= 0 or budget_cap <= 0:
            return {
                "template_id": "TG_LAUNCH_ALERT",
                "messages": [],
                "alerts": [],
            }
        state = self.store.load(self.settings.launch_state_path, {})
        if not isinstance(state, dict):
            state = {}
        now_ts = int(time.time())
        launch_window = closed_window(
            now=datetime.fromtimestamp(now_ts, timezone.utc),
            interval_sec=15 * 60,
            delay_sec=self.settings.launch_close_delay_sec,
        )
        self._prune_launch_state(state, now_ts)

        lifecycle_store: LaunchLifecycleStore | None = None
        lifecycle_diagnostics: dict[str, Any] = {
            "enabled": bool(self.settings.launch_lifecycle_v2_enable),
            "status": "disabled",
            "active_symbols": 0,
            "forced_symbols": 0,
            "recorded": 0,
            "opened": 0,
            "failed": 0,
            "frozen": 0,
            "publish_candidates": 0,
            "silent_observations": 0,
            "errors": 0,
            "outcome_v2": {
                "enabled": bool(self.settings.launch_outcome_v2_enable),
                "status": "disabled",
            },
            "price_action_v3": {
                "enabled": bool(self.settings.launch_price_action_v3_enable),
                "status": (
                    "active"
                    if self.settings.launch_price_action_v3_enable
                    and self.settings.launch_lifecycle_v2_enable
                    and self.settings.launch_message_package_v2_enable
                    else "shadow"
                    if self.settings.launch_price_action_v3_enable
                    and self.settings.launch_lifecycle_v2_enable
                    else "misconfigured"
                    if self.settings.launch_price_action_v3_enable
                    else "disabled"
                ),
                "tracked": 0,
            },
            "fusion": {
                "requested": bool(self.settings.launch_fusion_enable),
                "active": fusion_active,
                "status": (
                    "active"
                    if fusion_active
                    else "misconfigured"
                    if self.settings.launch_fusion_enable
                    else "disabled"
                ),
            },
            "directional": {
                "requested": bool(
                    getattr(self.settings, "launch_directional_enable", False)
                ),
                "active": self._launch_directional_active(),
                "status": (
                    "active"
                    if self._launch_directional_active()
                    else "misconfigured"
                    if getattr(self.settings, "launch_directional_enable", False)
                    else "disabled"
                ),
                "selected": 0,
                "ready": 0,
                "degraded": 0,
                "publish_skipped": 0,
                "network_calls": 0,
            },
        }
        lifecycle_active_symbols: list[str] = []
        lifecycle_active_modes: dict[str, bool] = {}
        lifecycle_active_profiles: dict[str, dict[str, Any]] = {}
        if self.settings.launch_lifecycle_v2_enable:
            try:
                lifecycle_store = LaunchLifecycleStore(
                    self.settings.signal_events_db_path,
                    watch_score=self.settings.launch_watch_score,
                    start_score=self.settings.launch_min_score_push,
                    invalid_windows_required=self.settings.launch_lifecycle_invalid_windows,
                    package_enabled=self.settings.launch_message_package_v2_enable,
                    package_score_delta=self.settings.launch_package_score_delta,
                    package_price_delta_pct=self.settings.launch_package_price_delta_pct,
                    package_oi_delta_pct=self.settings.launch_package_oi_delta_pct,
                    outcome_enabled=self.settings.launch_outcome_v2_enable,
                    outcome_follow_through_pct=self.settings.launch_outcome_follow_through_pct,
                    outcome_min_samples=self.settings.launch_outcome_min_samples,
                    breakout_score=self.settings.launch_breakout_score,
                    launched_score=self.settings.launch_launched_score,
                    price_action_enabled=(
                        self.settings.launch_price_action_v3_enable
                        or fusion_active
                    ),
                    fusion_enabled=fusion_active,
                    directional_enabled=self._launch_directional_active(),
                    same_stage_min_interval_sec=(
                        self.settings.launch_same_stage_min_interval_sec
                    ),
                )
                lifecycle_diagnostics["outcome_v2"] = lifecycle_store.refresh_outcomes(
                    evaluated_at=now_ts
                )
                lifecycle_active_symbols = lifecycle_store.list_active_symbols()
                lifecycle_active_modes = lifecycle_store.active_symbol_modes()
                lifecycle_active_profiles = lifecycle_store.active_symbol_profiles()
                lifecycle_diagnostics["status"] = (
                    "package_active"
                    if self.settings.launch_message_package_v2_enable
                    else "shadow"
                )
                lifecycle_diagnostics["active_symbols"] = len(lifecycle_active_symbols)
            except (OSError, sqlite3.Error, ValueError) as exc:
                lifecycle_diagnostics["status"] = "degraded"
                lifecycle_diagnostics["errors"] = 1
                lifecycle_diagnostics["error"] = type(exc).__name__

        forced_symbols: list[str] = []
        if lifecycle_store is not None:
            forced_symbols.extend(lifecycle_active_symbols)
            forced_symbols.extend(
                str(symbol)
                for symbol, record in state.items()
                if isinstance(record, dict)
                and str(record.get("stage") or "") in {"watching", "primed", "breakout", "launched", "cooling"}
            )
        forced_symbol_order = {
            symbol: position
            for position, symbol in enumerate(dict.fromkeys(forced_symbols))
        }
        lifecycle_diagnostics["forced_symbols"] = len(forced_symbol_order)

        symbol_metadata: dict[str, dict[str, Any]] = {}
        if hasattr(source, "usdt_perp_symbols"):
            try:
                symbol_metadata = {
                    str(row.get("symbol") or "").upper(): dict(row)
                    for row in source.usdt_perp_symbols()
                    if isinstance(row, dict) and row.get("symbol")
                }
            except Exception:
                symbol_metadata = {}

        ticker_map = {
            item.get("symbol"): item
            for item in source.ticker_24h()
            if str(item.get("symbol", "")).endswith("USDT")
        }
        try:
            premium_items = source.premium_index() if hasattr(source, "premium_index") else []
        except Exception:
            premium_items = []
        premium_map = {
            item.get("symbol"): item
            for item in premium_items
            if str(item.get("symbol", "")).endswith("USDT")
        }
        lifecycle_active_symbol_set = set(lifecycle_active_symbols)
        binance_market_caps = source.market_caps() if hasattr(source, "market_caps") else {}
        binance_market_caps = binance_market_caps or {}
        candidates: list[dict[str, Any]] = []
        missing_mcap_coins: set[str] = set()
        for symbol, ticker in ticker_map.items():
            quote_volume = to_float(ticker.get("quoteVolume"))
            if self._is_excluded_symbol(str(symbol or "")):
                continue
            candidate_fusion_active = lifecycle_active_modes.get(
                str(symbol),
                fusion_active,
            )
            active_profile = lifecycle_active_profiles.get(str(symbol), {})
            candidate_directional_active = bool(
                active_profile.get("directional")
                if active_profile
                else self._launch_directional_active()
            )
            if candidate_fusion_active:
                if symbol_metadata and str(symbol).upper() not in symbol_metadata:
                    continue
                if (
                    hasattr(source, "usdt_perp_symbols")
                    and not symbol_metadata
                    and symbol not in forced_symbol_order
                ):
                    continue
            if quote_volume < self.settings.radar_min_quote_volume and symbol not in forced_symbol_order:
                continue
            coin = str(symbol).replace("USDT", "")
            mcap = to_float(binance_market_caps.get(coin))
            if mcap <= 0:
                missing_mcap_coins.add(coin)
            premium = premium_map.get(symbol, {}) if isinstance(premium_map, dict) else {}
            classification = classify_binance_instrument(
                str(symbol),
                symbol_metadata.get(str(symbol).upper()),
            )
            candidates.append({
                "symbol": symbol,
                "coin": coin,
                "quote_volume": quote_volume,
                "price_24h": _optional_finite(
                    ticker.get("priceChangePercent")
                ),
                "price": to_float(ticker.get("lastPrice")),
                "funding_available": isinstance(premium, dict) and bool(premium),
                "funding_pct": to_float(premium.get("lastFundingRate")) * 100 if isinstance(premium, dict) else 0.0,
                "funding_next_time_ms": int(to_float(premium.get("nextFundingTime"))) if isinstance(premium, dict) else 0,
                "basis_pct": _premium_basis_pct(premium),
                "launch_lifecycle_active": symbol in lifecycle_active_symbol_set,
                "launch_fusion_cycle": candidate_fusion_active,
                "launch_directional_cycle": candidate_directional_active,
                "launch_directional_locked_side": str(
                    active_profile.get("direction") or ""
                ),
                "mcap": mcap,
                "mcap_source": "Binance" if mcap > 0 else "",
                "market_cap_tier": market_cap_tier(mcap),
                "liquidity_tier": liquidity_tier(quote_volume),
                **classification,
            })
        if missing_mcap_coins and hasattr(source, "coinpaprika_market_caps"):
            coinpaprika_market_caps = source.coinpaprika_market_caps() or {}
            for item in candidates:
                if item["mcap"] > 0 or item["coin"] not in missing_mcap_coins:
                    continue
                mcap = to_float(coinpaprika_market_caps.get(item["coin"]))
                if mcap > 0:
                    item["mcap"] = mcap
                    item["mcap_source"] = "CoinPaprika"
                    item["market_cap_tier"] = market_cap_tier(mcap)
        candidate_limit = min(self.settings.launch_scan_limit, budget_cap)
        if fusion_active:
            selection = select_launch_candidates(
                candidates,
                active_symbols=forced_symbol_order,
                limit=candidate_limit,
                closed_window_end_ts=int(launch_window.end.timestamp()),
            )
            candidates = list(selection["selected"])
            lifecycle_diagnostics["candidate_selection"] = selection["stats"]
        else:
            candidates.sort(
                key=lambda item: (
                    0 if item["symbol"] in forced_symbol_order else 1,
                    forced_symbol_order.get(item["symbol"], 0),
                    -item["quote_volume"],
                )
            )
            candidates = candidates[:candidate_limit]

        alerts: list[dict[str, Any]] = []
        watchlist: list[dict[str, Any]] = []

        analyzed_items: list[dict[str, Any]] = []
        analysis_skipped: dict[str, int] = {}
        for item in candidates:
            item_fusion_active = bool(item.get("launch_fusion_cycle"))
            analyzed = (
                self._analyze_launch_symbol(
                    source,
                    item,
                    window=launch_window,
                )
                if item_fusion_active
                else self._analyze_launch_symbol(source, item)
            )
            if not analyzed:
                continue
            if str(analyzed.get("analysis_status") or "") == "invalid":
                error = str(
                    analyzed.get("analysis_error")
                    or "launch_analysis_invalid"
                )
                analysis_skipped[error] = analysis_skipped.get(error, 0) + 1
                continue
            analyzed_items.append(analyzed)
        lifecycle_diagnostics["analysis_quality"] = {
            "ready": len(analyzed_items),
            "skipped": sum(analysis_skipped.values()),
            "skipped_by_reason": analysis_skipped,
        }

        accumulation_evidence = self._load_launch_accumulation_evidence(
            now_ts=now_ts,
        )
        announcement_evidence = self._load_launch_announcement_evidence(
            now_ts=now_ts,
        )
        for analyzed in analyzed_items:
            symbol = str(analyzed.get("symbol") or "").upper()
            if symbol in accumulation_evidence:
                analyzed["accumulation_quality_evidence"] = dict(
                    accumulation_evidence[symbol]
                )
            if symbol in announcement_evidence:
                analyzed["announcement_evidence"] = [
                    dict(record) for record in announcement_evidence[symbol]
                ]
        lifecycle_diagnostics["supporting_evidence"] = {
            "score_effect": "none",
            "accumulation_symbols": sum(
                1 for item in analyzed_items if item.get("accumulation_quality_evidence")
            ),
            "announcement_symbols": sum(
                1 for item in analyzed_items if item.get("announcement_evidence")
            ),
        }

        for analyzed in analyzed_items:
            facts = analyzed.get("market_facts")
            if bool(analyzed.get("launch_fusion_cycle")) and isinstance(facts, dict):
                checks = {
                    "15m价格": facts.get("price_15m_pct") is not None,
                    "1h价格": facts.get("price_1h_pct") is not None,
                    "15m/1h OI": (
                        facts.get("oi_15m_pct") is not None
                        and facts.get("oi_1h_pct") is not None
                    ),
                    "成交量": facts.get("volume_ratio_15m") is not None,
                    "时间轴对齐": (
                        facts.get("status") == "ok"
                        and int(facts.get("aligned_points") or 0) >= 17
                    ),
                }
                confirmation_window = "严格闭合15m；1h/4h同序列推导"
            else:
                checks = {
                    "15m价格": True,
                    "1h价格": True,
                    "15m/1h OI": True,
                    "成交量": True,
                    "突破结构": True,
                }
                confirmation_window = "15m闭合窗口（1h=4根）"
            apply_binance_confirmation(
                analyzed,
                checks,
                scope="Binance USDⓈ-M Futures",
                window=confirmation_window,
                observed_at=int(analyzed.get("window_end_ts") or now_ts),
            )
            price_action = analyzed.get("price_action_analysis")
            if (
                isinstance(price_action, dict)
                and str(price_action.get("data_status") or "") == "ready"
            ):
                lifecycle_diagnostics["price_action_v3"]["tracked"] += 1

        any_fusion_cycle = any(
            bool(analyzed.get("launch_fusion_cycle"))
            for analyzed in analyzed_items
        )
        if (
            self.settings.launch_message_package_v2_enable
            or any_fusion_cycle
        ) and analyzed_items:
            from shared.bot_market_context import closed_market_contexts_for_symbols

            package_symbols = [
                str(analyzed.get("symbol") or "")
                for analyzed in analyzed_items
                if bool(analyzed.get("launch_fusion_cycle"))
                or self._discovery_score(analyzed) >= self.settings.launch_watch_score
                or str(analyzed.get("symbol") or "") in lifecycle_active_symbols
            ]
            market_contexts = closed_market_contexts_for_symbols(
                self.settings,
                package_symbols,
                now_ts=now_ts,
            )
            for analyzed in analyzed_items:
                market = market_contexts.get(str(analyzed.get("symbol") or ""))
                if not isinstance(market, dict):
                    continue
                if (
                    bool(analyzed.get("launch_fusion_cycle"))
                    and int(to_float(market.get("window_end_ts")))
                    != int(launch_window.end.timestamp())
                ):
                    continue
                if bool(analyzed.get("launch_fusion_cycle")):
                    for side in ("spot", "futures"):
                        market_flow = market.get(f"{side}_flow_usd")
                        market_ratio = market.get(f"{side}_active_ratio")
                        direct_status = str(
                            analyzed.get(f"{side}_active_status") or ""
                        )
                        if (
                            direct_status
                            in {
                                "binance_unavailable",
                                "budget_exhausted",
                                "window_incomplete",
                            }
                            and market_flow is not None
                            and market_ratio is not None
                        ):
                            analyzed[f"{side}_active_net_usd"] = market_flow
                            analyzed[f"{side}_active_ratio"] = market_ratio
                            analyzed[f"{side}_active_status"] = "available"
                else:
                    analyzed["spot_active_net_usd"] = market.get(
                        "spot_flow_usd"
                    )
                    analyzed["futures_active_net_usd"] = market.get(
                        "futures_flow_usd"
                    )
                    analyzed["spot_active_ratio"] = market.get(
                        "spot_active_ratio"
                    )
                    analyzed["futures_active_ratio"] = market.get(
                        "futures_active_ratio"
                    )
                analyzed["funds_direction"] = launch_funds_direction(
                    analyzed.get("spot_active_net_usd"),
                    analyzed.get("futures_active_net_usd"),
                )

        if any_fusion_cycle:
            for analyzed in analyzed_items:
                if not bool(analyzed.get("launch_fusion_cycle")):
                    continue
                self._apply_launch_fusion_score(analyzed)

        any_directional_cycle = any(
            bool(item.get("launch_directional_cycle"))
            for item in analyzed_items
        )
        if (
            self._launch_directional_active() or any_directional_cycle
        ) and analyzed_items:
            directional_diagnostics = self._enrich_directional_candidates(
                source,
                analyzed_items,
                window_end_ms=launch_window.end_ms,
            )
            lifecycle_diagnostics["directional"].update(
                directional_diagnostics
            )

        if lifecycle_store is not None and analyzed_items:
            try:
                lifecycle_items = [
                    analyzed
                    for analyzed in analyzed_items
                    if self._directional_candidate_trackable(analyzed)
                ]
                lifecycle_results = lifecycle_store.record_observations([
                    (
                        analyzed,
                        self._launch_stage(self._evidence_score(analyzed)),
                        now_ts,
                    )
                    for analyzed in lifecycle_items
                ])
                for analyzed, lifecycle in zip(lifecycle_items, lifecycle_results):
                    analyzed["launch_lifecycle"] = lifecycle
                    if (
                        bool(analyzed.get("launch_directional_cycle"))
                        and str(lifecycle.get("cycle_status") or "") == "failed"
                    ):
                        analyzed["directional_cycle_invalidated"] = {
                            "reason": str(
                                lifecycle.get("end_reason")
                                or "directional_cycle_failed"
                            ),
                            "previous_direction": str(
                                analyzed.get("launch_directional_locked_side") or ""
                            ),
                            "next_direction": str(
                                (
                                    analyzed.get("directional_readiness")
                                    if isinstance(
                                        analyzed.get("directional_readiness"), dict
                                    )
                                    else {}
                                ).get("direction")
                                or ""
                            ),
                            "semantics": "close_old_cycle_before_new_direction",
                        }
                    lifecycle_status = str(lifecycle.get("status") or "")
                    if lifecycle_status in {"opened", "active", "failed"}:
                        lifecycle_diagnostics["recorded"] += 1
                    if lifecycle_status == "opened":
                        lifecycle_diagnostics["opened"] += 1
                    if lifecycle_status == "failed":
                        lifecycle_diagnostics["failed"] += 1
                    if lifecycle_status == "frozen":
                        lifecycle_diagnostics["frozen"] += 1
                    publication = lifecycle.get("publication")
                    if isinstance(publication, dict) and publication.get("enabled"):
                        if publication.get("publish_required"):
                            lifecycle_diagnostics["publish_candidates"] += 1
                        elif lifecycle_status in {"opened", "active", "failed", "duplicate"}:
                            lifecycle_diagnostics["silent_observations"] += 1
            except (OSError, sqlite3.Error, ValueError) as exc:
                lifecycle_diagnostics["status"] = "degraded"
                lifecycle_diagnostics["errors"] += 1
                lifecycle_diagnostics["error"] = type(exc).__name__

        observed_symbols: set[str] = set()
        for analyzed in analyzed_items:
            observed_symbols.add(str(analyzed["symbol"]))
            optional_evidence = self._optional_evidence_score(analyzed)
            next_stage = (
                self._launch_stage(optional_evidence)
                if optional_evidence is not None
                else "idle"
            )
            watchlist.append(self._launch_watch_record(analyzed, now_ts))
            if not self._directional_candidate_publishable(analyzed):
                lifecycle_diagnostics["directional"]["publish_skipped"] = (
                    int(
                        lifecycle_diagnostics["directional"].get(
                            "publish_skipped",
                            0,
                        )
                    )
                    + 1
                )
                continue
            previous = state.get(analyzed["symbol"], {})
            lifecycle = analyzed.get("launch_lifecycle")
            publication = (
                lifecycle.get("publication")
                if isinstance(lifecycle, dict)
                and isinstance(lifecycle.get("publication"), dict)
                else {}
            )
            if (
                self.settings.launch_message_package_v2_enable
                and isinstance(lifecycle, dict)
                and publication.get("enabled")
            ):
                current_stage = str(lifecycle.get("current_stage") or next_stage)
                previous_published = publication.get("previous_published")
                previous_stage = (
                    str(previous_published.get("stage") or "idle")
                    if isinstance(previous_published, dict)
                    else "idle"
                )
                reply_message_ids = [
                    int(message_id)
                    for message_id in (
                        publication.get("reply_message_ids") or []
                    )
                    if isinstance(message_id, int)
                    or str(message_id).isdigit()
                ]
                reply_to_message_id = (
                    reply_message_ids[0]
                    if reply_message_ids
                    else int(previous.get("last_message_id", 0) or 0)
                )
                record = {
                    **(previous if isinstance(previous, dict) else {}),
                    **analyzed,
                    "stage": current_stage,
                    "first_seen": int(lifecycle.get("first_window_end") or now_ts),
                    "last_seen": now_ts,
                    "last_active_at": now_ts,
                    "appear_count": int(lifecycle.get("observation_no") or 1),
                    "previous_stage": previous_stage,
                    "reply_to_message_id": reply_to_message_id,
                    "launch_message_package_v2": True,
                    "launch_package": publication,
                }
                for lifecycle_key in ("cooling_at", "delete_pending"):
                    record.pop(lifecycle_key, None)
                if str(lifecycle.get("cycle_status") or "") == "failed":
                    record["failed_at"] = int(lifecycle.get("window_end_ts") or now_ts)
                    record["fail_reason"] = str(lifecycle.get("end_reason") or "lifecycle_failed")
                else:
                    record.pop("failed_at", None)
                    record.pop("fail_reason", None)
                    # The state record is reused across cycles so a terminal
                    # marker from the closed cycle must not turn the new
                    # active cycle into a terminal-only update.
                    record.pop("directional_cycle_invalidated", None)
                if publication.get("publish_required"):
                    alerts.append(record)
                state[analyzed["symbol"]] = record
                continue
            if next_stage == "idle":
                inactive = self._inactive_launch_record(
                    previous,
                    now_ts,
                    fail_reason="launch_score_fell",
                )
                if inactive is not None:
                    state[analyzed["symbol"]] = inactive
                continue

            previous_stage = previous.get("stage", "idle")
            if previous_stage == "failed":
                reply_head = int(previous.get("last_message_id", 0) or 0)
                previous = (
                    {
                        "last_message_id": reply_head,
                        "last_message_ids": [reply_head],
                        "message_ids": list(previous.get("message_ids") or []),
                    }
                    if reply_head > 0
                    else {}
                )
                previous_stage = "idle"
            stage_changed = self._stage_rank(next_stage) > self._stage_rank(previous_stage)
            last_pushed = int(previous.get("last_pushed", 0) or 0)
            cooldown_ok = now_ts - last_pushed >= self.settings.launch_stage_cooldown_sec
            appear_count = int(previous.get("appear_count", 0) or 0) + 1
            record = {
                **previous,
                **analyzed,
                "stage": next_stage,
                "first_seen": previous.get("first_seen", now_ts),
                "last_seen": now_ts,
                "last_active_at": now_ts,
                "appear_count": appear_count,
                "previous_stage": previous_stage,
                "reply_to_message_id": int(previous.get("last_message_id", 0) or 0),
            }
            for lifecycle_key in ("cooling_at", "failed_at", "fail_reason", "delete_pending"):
                record.pop(lifecycle_key, None)
            if (
                stage_changed
                and cooldown_ok
                and self._evidence_score(analyzed)
                >= self.settings.launch_min_score_push
            ):
                alerts.append(record)
            state[analyzed["symbol"]] = record

        if analyzed_items and lifecycle_store is None:
            for symbol, previous in list(state.items()):
                if symbol in observed_symbols or not isinstance(previous, dict):
                    continue
                inactive = self._inactive_launch_record(
                    previous,
                    now_ts,
                    fail_reason="launch_candidate_disappeared",
                )
                if inactive is not None:
                    state[symbol] = inactive

        persisted_state, _compacted_records = compact_launch_state_records(state)
        self.store.save(self.settings.launch_state_path, persisted_state)
        self.store.save(self.settings.launch_watchlist_path, {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "count": len(watchlist),
            "score_contract_version": 2,
            "items": sorted(
                watchlist,
                key=LaunchWarningRadar._discovery_score,
                reverse=True,
            )[:30],
        })
        self.store.append_record(
            self.settings.launch_watch_history_path,
            self._launch_history_record(watchlist, alerts, now_ts),
            limit=self.settings.launch_watch_history_limit,
        )
        alerts = alerts[:5]
        chart_diagnostics: dict[str, Any] = {
            "enabled": bool(self.settings.launch_chart_v2_enable),
            "status": (
                "active"
                if self.settings.launch_chart_v2_enable
                and self.settings.launch_message_package_v2_enable
                else "misconfigured"
                if self.settings.launch_chart_v2_enable
                else "disabled"
            ),
            "ready": 0,
            "unavailable": 0,
        }
        if (
            self.settings.launch_chart_v2_enable
            and self.settings.launch_message_package_v2_enable
        ):
            for alert in alerts:
                if not alert.get("launch_message_package_v2"):
                    continue
                if self._attach_launch_chart(source, alert):
                    chart_diagnostics["ready"] += 1
                else:
                    chart_diagnostics["unavailable"] += 1
        ai_diagnostics = self._interpret_directional_alerts(alerts)
        messages = [self._format_launch_alert(alert) for alert in alerts]
        return {
            "template_id": "TG_LAUNCH_ALERT",
            "messages": messages,
            "alerts": alerts,
            "watchlist_count": len(watchlist),
            "diagnostics": {
                "binance_confirmation": confirmation_summary(analyzed_items),
                "lifecycle_v2": lifecycle_diagnostics,
                "chart_v2": chart_diagnostics,
                "ai_interpreter": ai_diagnostics,
            },
        }

    def mark_launch_pushed(self, alerts: list[dict[str, Any]]) -> None:
        if not alerts:
            return
        state = self.store.load(self.settings.launch_state_path, {})
        if not isinstance(state, dict):
            return
        now_ts = int(time.time())
        for alert in alerts:
            symbol = alert.get("symbol")
            if symbol in state and isinstance(state[symbol], dict):
                state[symbol]["last_pushed"] = now_ts
                state[symbol]["last_pushed_stage"] = alert.get("stage")
                message_ids = [
                    int(message_id)
                    for message_id in (alert.get("message_ids") or [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                ]
                if message_ids:
                    existing_message_ids = [
                        int(message_id)
                        for message_id in (state[symbol].get("message_ids") or [])
                        if isinstance(message_id, int) or str(message_id).isdigit()
                    ]
                    state[symbol]["last_message_id"] = message_ids[0]
                    state[symbol]["last_message_ids"] = message_ids
                    state[symbol]["message_ids"] = (
                        message_ids
                        if alert.get("launch_message_package_v2")
                        else list(dict.fromkeys(
                            [*existing_message_ids, *message_ids]
                        ))[-100:]
                    )
                    state[symbol]["last_message_stage"] = alert.get("stage")
        self.store.save(self.settings.launch_state_path, state)

    def commit_launch_package(
        self,
        alert: dict[str, Any],
        message_ids: list[int],
        *,
        published_at: int | None = None,
    ) -> dict[str, Any]:
        lifecycle = alert.get("launch_lifecycle")
        publication = alert.get("launch_package")
        if (
            not self.settings.launch_message_package_v2_enable
            or not isinstance(lifecycle, dict)
            or not isinstance(publication, dict)
        ):
            return {"status": "disabled", "delete_message_ids": []}
        return self._launch_lifecycle_store(package_enabled=True).commit_package(
            cycle_id=int(lifecycle.get("cycle_id") or 0),
            observation_id=int(lifecycle.get("observation_id") or 0),
            message_ids=message_ids,
            checkpoint_reasons=list(publication.get("checkpoint_reasons") or []),
            published_at=int(published_at or time.time()),
        )

    def complete_launch_package_cleanup(
        self,
        *,
        cycle_id: int,
        deleted_ids: list[int],
        failed_ids: list[int],
        updated_at: int | None = None,
        expire_latest: bool = False,
    ) -> dict[str, Any]:
        return self._launch_lifecycle_store(package_enabled=True).complete_package_cleanup(
            cycle_id=int(cycle_id),
            deleted_ids=deleted_ids,
            failed_ids=failed_ids,
            updated_at=int(updated_at or time.time()),
            expire_latest=expire_latest,
        )

    def reconcile_launch_topic_messages(
        self,
        *,
        deleted_ids: list[int],
        updated_at: int | None = None,
    ) -> dict[str, int]:
        deleted = {
            int(message_id)
            for message_id in deleted_ids
            if isinstance(message_id, int) or str(message_id).isdigit()
        }
        result = {
            "cycles_updated": 0,
            "message_ids_removed": 0,
            "state_records_updated": 0,
        }
        if not deleted:
            return result
        reconciled = self._launch_lifecycle_store(
            package_enabled=True
        ).reconcile_topic_message_cleanup(
            deleted_ids=sorted(deleted),
            updated_at=int(updated_at or time.time()),
        )
        result.update(reconciled)

        state = self.store.load(self.settings.launch_state_path, {})
        if not isinstance(state, dict):
            return result
        changed = False
        for record in state.values():
            if not isinstance(record, dict):
                continue
            record_changed = False
            for key in ("message_ids", "last_message_ids"):
                values = [
                    int(message_id)
                    for message_id in (record.get(key) or [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                ]
                filtered = [
                    message_id
                    for message_id in values
                    if message_id not in deleted
                ]
                if filtered != values:
                    record[key] = filtered
                    record_changed = True
            last_message_id = record.get("last_message_id")
            if (
                isinstance(last_message_id, int)
                or str(last_message_id or "").isdigit()
            ) and int(last_message_id) in deleted:
                remaining = list(record.get("last_message_ids") or [])
                record["last_message_id"] = int(remaining[0]) if remaining else 0
                record_changed = True
            if record_changed:
                changed = True
                result["state_records_updated"] += 1
        if changed:
            self.store.save(self.settings.launch_state_path, state)
        return result

    def pending_launch_package_cleanups(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if not self.settings.launch_message_package_v2_enable:
            return []
        return self._launch_lifecycle_store(package_enabled=True).list_pending_cleanups(
            limit=limit,
            now_ts=int(time.time()),
            max_age_sec=self.settings.launch_message_cleanup_max_age_sec,
        )

    def _launch_lifecycle_store(self, *, package_enabled: bool | None = None) -> LaunchLifecycleStore:
        return LaunchLifecycleStore(
            self.settings.signal_events_db_path,
            watch_score=self.settings.launch_watch_score,
            start_score=self.settings.launch_min_score_push,
            invalid_windows_required=self.settings.launch_lifecycle_invalid_windows,
            package_enabled=(
                self.settings.launch_message_package_v2_enable
                if package_enabled is None
                else bool(package_enabled)
            ),
            package_score_delta=self.settings.launch_package_score_delta,
            package_price_delta_pct=self.settings.launch_package_price_delta_pct,
            package_oi_delta_pct=self.settings.launch_package_oi_delta_pct,
            outcome_enabled=self.settings.launch_outcome_v2_enable,
            outcome_follow_through_pct=self.settings.launch_outcome_follow_through_pct,
            outcome_min_samples=self.settings.launch_outcome_min_samples,
            breakout_score=self.settings.launch_breakout_score,
            launched_score=self.settings.launch_launched_score,
            price_action_enabled=(
                self.settings.launch_price_action_v3_enable
                or self._launch_fusion_active()
            ),
            fusion_enabled=self._launch_fusion_active(),
            directional_enabled=self._launch_directional_active(),
            same_stage_min_interval_sec=(
                self.settings.launch_same_stage_min_interval_sec
            ),
        )

    def cleanup_failed_launch_messages(
        self,
        delete_messages: Callable[[list[int]], dict[str, list[int]] | int] | None = None,
        *,
        now_ts: int | None = None,
    ) -> dict[str, Any]:
        """Retain launch-topic history; kept as a compatibility no-op."""

        return {
            "enabled": False,
            "mode": "retain_history_reply_chain",
            "failed_signals": 0,
            "candidate_messages": 0,
            "deleted_messages": 0,
            "undeletable_messages": 0,
            "failed_deletions": 0,
            "pending_signals": 0,
            "dry_run": True,
        }

    def _attach_launch_chart(
        self,
        source: BinanceDataSource,
        alert: dict[str, Any],
    ) -> bool:
        lifecycle = alert.get("launch_lifecycle")
        publication = alert.get("launch_package")
        if not isinstance(lifecycle, dict) or not isinstance(publication, dict):
            alert["chart_status"] = "unavailable"
            alert["chart_error"] = "missing_lifecycle_context"
            return False

        first_window_end = int(to_float(lifecycle.get("first_window_end")))
        current_window_end = int(to_float(lifecycle.get("window_end_ts")))
        if first_window_end <= 0 or current_window_end <= 0:
            alert["chart_status"] = "unavailable"
            alert["chart_error"] = "invalid_chart_window"
            return False
        interval_sec = 60 * 60
        requested_visible_start = min(
            first_window_end - 16 * interval_sec,
            current_window_end - 288 * interval_sec,
        )
        start_ts = max(
            0,
            requested_visible_start - 40 * interval_sec,
            current_window_end - 360 * interval_sec,
        )
        candle_count = max(
            328,
            (current_window_end - start_ts + interval_sec - 1) // interval_sec,
        )
        candle_count = min(360, candle_count)
        try:
            rows = source.klines(
                str(alert.get("symbol") or ""),
                interval="1h",
                limit=int(candle_count),
                start_time=start_ts * 1000,
                end_time=current_window_end * 1000 - 1,
            )
            candles = [
                {
                    "close_ts": int(to_float(row[6])) // 1000 + 1,
                    "open": to_float(row[1]),
                    "high": to_float(row[2]),
                    "low": to_float(row[3]),
                    "close": to_float(row[4]),
                    "quote_volume": to_float(row[7]),
                }
                for row in rows
                if isinstance(row, list) and len(row) >= 8
                and int(to_float(row[6])) < current_window_end * 1000
            ]
            checkpoints = [
                dict(checkpoint)
                for checkpoint in (publication.get("checkpoints") or [])
                if isinstance(checkpoint, dict)
            ]
            chart_price_action = (
                dict(lifecycle["price_action"])
                if isinstance(lifecycle.get("price_action"), dict)
                else None
            )
            current = publication.get("current")
            if isinstance(current, dict):
                current_checkpoint = dict(current)
                current_checkpoint["checkpoint_no"] = int(
                    publication.get("checkpoint_no") or len(checkpoints) + 1
                )
                checkpoints.append(current_checkpoint)
                if isinstance(current.get("price_action"), dict):
                    chart_price_action = dict(current["price_action"])
            image = render_launch_chart_png(
                symbol=str(alert.get("symbol") or ""),
                candles=candles,
                checkpoints=checkpoints,
                cycle_no=int(lifecycle.get("cycle_no") or 1),
                price_action=chart_price_action,
                asset_category=str(alert.get("asset_category_short") or ""),
                width=1080,
                height=720,
            )
        except Exception as exc:
            alert["chart_status"] = "unavailable"
            alert["chart_error"] = type(exc).__name__
            return False

        alert["chart_png_bytes"] = image
        alert["chart_status"] = "ready"
        alert["chart_candle_count"] = min(len(candles), DISPLAY_CANDLE_LIMIT)
        alert["chart_source_candle_count"] = len(candles)
        alert["chart_checkpoint_count"] = len(checkpoints)
        alert["chart_generated_in_memory"] = True
        alert["chart_timeframe"] = "1h"
        alert["chart_trigger_timeframe"] = "15m"
        if chart_price_action:
            alert["chart_price_action_status"] = str(
                chart_price_action.get("status") or ""
            )
        return True

    def _analyze_launch_symbol(
        self,
        source: BinanceDataSource,
        item: dict[str, Any],
        *,
        window: Any | None = None,
    ) -> Optional[dict[str, Any]]:
        symbol = item["symbol"]
        mode_marker = item.get("launch_fusion_cycle")
        fusion_active = (
            bool(mode_marker)
            if isinstance(mode_marker, bool)
            else self._launch_fusion_active()
        )
        window = window or closed_window(
            interval_sec=15 * 60,
            delay_sec=self.settings.launch_close_delay_sec,
        )
        price_action_follow_up = bool(
            (
                self.settings.launch_price_action_v3_enable
                or fusion_active
            )
            and item.get("launch_lifecycle_active")
        )
        kline_limit = required_15m_kline_limit(
            self.settings.launch_pa_box_lookback,
            follow_up=price_action_follow_up,
        )
        lookback_ms = kline_limit * 15 * 60 * 1000
        klines = source.klines(
            symbol,
            interval="15m",
            limit=kline_limit,
            start_time=max(0, window.end_ms - lookback_ms),
            end_time=window.end_ms - 1,
        )
        oi_limit = OI_24H_REQUIRED_POINTS if fusion_active else 17
        oi_hist = source.open_interest_hist(
            symbol,
            period="15m",
            limit=oi_limit,
            start_time=max(0, window.end_ms - (oi_limit - 1) * INTERVAL_MS),
            end_time=window.end_ms,
        )
        minimum_points = 17 if fusion_active else 5
        if len(klines) < minimum_points or len(oi_hist) < minimum_points:
            if fusion_active:
                return {
                    **item,
                    "analysis_status": "invalid",
                    "analysis_error": "launch_market_facts_insufficient_history",
                    "window_end_ts": int(window.end.timestamp()),
                }
            return None

        scoring_klines = sorted(
            klines,
            key=lambda row: int(to_float(row[0])) if len(row) > 0 else 0,
        )[-17:]
        closes = [to_float(kline[4]) for kline in scoring_klines]
        highs = [to_float(kline[2]) for kline in scoring_klines]
        quote_volumes = [to_float(kline[7]) for kline in scoring_klines]
        ordered_oi_hist = sorted(
            oi_hist,
            key=lambda row: int(to_float(row.get("timestamp"))),
        )
        oi_values = [
            to_float(row.get("sumOpenInterestValue"))
            for row in ordered_oi_hist[-17:]
        ]
        if min(closes[-5:]) <= 0 or min(oi_values[-5:]) <= 0:
            if fusion_active:
                return {
                    **item,
                    "analysis_status": "invalid",
                    "analysis_error": "launch_market_facts_input_invalid",
                    "window_end_ts": int(window.end.timestamp()),
                }
            return None

        market_facts: dict[str, Any] | None = None
        if fusion_active:
            market_facts = build_launch_market_facts(
                klines,
                ordered_oi_hist[-17:],
                window_end_ms=window.end_ms,
                ticker_24h={
                    "priceChangePercent": item.get("price_24h"),
                },
                oi_24h_rows=ordered_oi_hist,
            )
            if market_facts.get("status") != "ok":
                return {
                    **item,
                    "analysis_status": "invalid",
                    "analysis_error": str(
                        market_facts.get("error")
                        or "launch_market_facts_input_invalid"
                    ),
                    "window_end_ts": int(window.end.timestamp()),
                }
            price_15m = market_facts.get("price_15m_pct")
            price_1h = market_facts.get("price_1h_pct")
            oi_15m = market_facts.get("oi_15m_pct")
            oi_1h = market_facts.get("oi_1h_pct")
            volume_ratio = market_facts.get("volume_ratio_15m")
        else:
            price_15m = pct(closes[-1], closes[-2])
            price_1h = pct(closes[-1], closes[-5])
            oi_15m = pct(oi_values[-1], oi_values[-2])
            oi_1h = pct(oi_values[-1], oi_values[-5])
            avg_volume = sum(quote_volumes[:-1]) / max(1, len(quote_volumes[:-1]))
            volume_ratio = quote_volumes[-1] / avg_volume if avg_volume > 0 else 0
        previous_high = max(highs[:-1])
        breakout = closes[-1] > previous_high if previous_high > 0 else False

        score = 0
        reasons: list[str] = []
        if not fusion_active and price_15m >= 4:
            score += 25
            reasons.append(f"15m价格 {price_15m:+.1f}%")
        if not fusion_active and price_1h >= 5:
            score += 15
            reasons.append(f"1h价格 {price_1h:+.1f}%")
        if not fusion_active and breakout:
            score += 25
            reasons.append("突破近4h高点")
        if not fusion_active and volume_ratio >= 2:
            score += 20
            reasons.append(f"成交 {volume_ratio:.1f}x 均值")
        if not fusion_active and oi_15m >= 3:
            score += 15
            reasons.append(f"15m OI {oi_15m:+.1f}%")
        if not fusion_active and oi_1h >= 6:
            score += 15
            reasons.append(f"1h OI {oi_1h:+.1f}%")
        if (
            not fusion_active
            and oi_1h >= 3
            and abs(price_1h) <= 2
        ):
            score += 15
            reasons.append("资金暗流但价格未大动")

        recent_volatility_pct = (
            market_facts.get("recent_volatility_pct")
            if market_facts is not None
            else None
        )
        preliminary_fusion: dict[str, Any] | None = None
        if fusion_active:
            preliminary_fusion = score_launch_signal({
                "price_15m": price_15m,
                "price_1h": price_1h,
                "oi_15m": oi_15m,
                "oi_1h": oi_1h,
                "volume_ratio": volume_ratio,
                "breakout": breakout,
                "asset_subclass": item.get("asset_subclass"),
                "liquidity_tier": item.get("liquidity_tier"),
                "recent_volatility_pct": recent_volatility_pct,
            })
            score = int(preliminary_fusion.get("score") or 0)

        funding_pct = to_float(item.get("funding_pct"))
        next_funding_time_ms = int(to_float(item.get("funding_next_time_ms")))
        if (
            bool(item.get("launch_lifecycle_active"))
            or score >= self.settings.launch_watch_score
            or funding_pct <= -0.5
        ):
            funding_context = self._launch_funding_context(source, symbol, funding_pct, next_funding_time_ms)
        else:
            funding_context = {
                "funding_available": bool(item.get("funding_available")),
                "funding_pct": funding_pct,
                "funding_next_time_ms": next_funding_time_ms,
                "funding_interval_hours": 0,
                "funding_interval_transition": "",
            }
        if funding_pct <= -0.5:
            reasons.append(f"资金费率{funding_cycle_text(funding_pct, int(funding_context.get('funding_interval_hours', 0) or 0))}极负")
        elif funding_context.get("funding_interval_transition"):
            reasons.append("资金费率结算周期缩短")

        result = {
            **item,
            "analysis_status": "ready",
            "discovery_score": score,
            "score": score,
            "closed_price": closes[-1],
            "closed_oi_usd": oi_values[-1],
            "closed_quote_volume": quote_volumes[-1],
            "price_15m": price_15m,
            "price_1h": price_1h,
            "price_4h": (
                market_facts.get("price_4h_pct")
                if market_facts is not None
                else None
            ),
            "price_24h_semantics": (
                market_facts.get("price_24h_semantics")
                if market_facts is not None
                else "rolling_24h_not_closed_window"
            ),
            "oi_15m": oi_15m,
            "oi_1h": oi_1h,
            "oi_4h": (
                market_facts.get("oi_4h_pct")
                if market_facts is not None
                else None
            ),
            "oi_24h": (
                market_facts.get("oi_24h_closed_pct")
                if market_facts is not None
                else None
            ),
            "oi_24h_status": (
                str(market_facts.get("oi_24h_status") or "")
                if market_facts is not None
                else ""
            ),
            "oi_24h_semantics": (
                str(market_facts.get("oi_24h_semantics") or "")
                if market_facts is not None
                else ""
            ),
            "volume_ratio": volume_ratio,
            "breakout": breakout,
            "breakout_price": previous_high,
            "reasons": reasons[:5],
            "kline_points": len(klines),
            "oi_points": len(oi_hist),
            "window_end_ts": int(window.end.timestamp()),
            "recent_volatility_pct": recent_volatility_pct,
            "market_facts": market_facts,
            "fusion_analysis": preliminary_fusion,
            "price_oi_quadrant": (
                str(
                    (market_facts.get("quadrants") or {})
                    .get("15m", {})
                    .get("key", "")
                )
                if market_facts is not None
                else ""
            ),
            "price_oi_quadrants": (
                dict(market_facts.get("quadrants") or {})
                if market_facts is not None
                else {}
            ),
            **funding_context,
        }
        if fusion_active:
            futures_active = closed_kline_active_flow(
                scoring_klines[-1],
                window_end_ms=window.end_ms,
            )
            spot_active = self._closed_spot_active_flow(
                source,
                symbol,
                window_end_ms=window.end_ms,
            )
            result.update({
                "futures_active_net_usd": futures_active["net_usd"],
                "futures_active_ratio": futures_active["ratio"],
                "futures_active_status": futures_active["status"],
                "spot_active_net_usd": spot_active["net_usd"],
                "spot_active_ratio": spot_active["ratio"],
                "spot_active_status": spot_active["status"],
            })
        if (
            self.settings.launch_price_action_v3_enable
            or fusion_active
        ):
            result["price_action_analysis"] = analyze_launch_price_action(
                klines,
                window_end_ms=window.end_ms,
                lookback=self.settings.launch_pa_box_lookback,
                max_box_range_pct=self.settings.launch_pa_max_box_range_pct,
                min_body_ratio=self.settings.launch_pa_min_body_ratio,
                wick_body_ratio=self.settings.launch_pa_wick_body_ratio,
            )
        return result

    @staticmethod
    def _closed_spot_active_flow(
        source: BinanceDataSource,
        symbol: str,
        *,
        window_end_ms: int,
        periods: int = 1,
    ) -> dict[str, Any]:
        unavailable = {
            "status": "binance_unavailable",
            "net_usd": None,
            "gross_usd": None,
            "ratio": None,
        }
        if not hasattr(source, "spot_klines"):
            return unavailable
        if hasattr(source, "spot_symbols"):
            try:
                spot_symbols = source.spot_symbols()
            except Exception:
                return unavailable
            if spot_symbols is None:
                return unavailable
            if str(symbol).upper() not in spot_symbols:
                return {**unavailable, "status": "spot_pair_not_listed"}
        budget = getattr(source, "budget", None)
        used = getattr(budget, "used", {}) if budget is not None else {}
        limits = getattr(budget, "limits", {}) if budget is not None else {}
        if (
            "spot_klines" in limits
            and int(used.get("spot_klines", 0))
            >= int(limits.get("spot_klines", 0))
        ):
            return {**unavailable, "status": "budget_exhausted"}
        try:
            requested_periods = max(1, min(4, int(periods)))
            rows = source.spot_klines(
                symbol,
                interval="15m",
                limit=requested_periods,
                start_time=max(
                    0,
                    int(window_end_ms) - requested_periods * INTERVAL_MS,
                ),
                end_time=int(window_end_ms) - 1,
            )
        except Exception:
            return unavailable
        if not isinstance(rows, list) or not rows:
            return unavailable
        if requested_periods > 1:
            return active_flow_window(
                rows,
                interval_ms=INTERVAL_MS,
                window_end_ms=int(window_end_ms),
                periods=requested_periods,
            )
        return closed_kline_active_flow(rows[-1], window_end_ms=window_end_ms)

    def _enrich_directional_candidates(
        self,
        source: BinanceDataSource,
        items: list[dict[str, Any]],
        *,
        window_end_ms: int,
    ) -> dict[str, Any]:
        budget = getattr(source, "budget", None)
        used = getattr(budget, "used", {}) if budget is not None else {}
        limits = getattr(budget, "limits", {}) if budget is not None else {}
        remaining_kline_calls = max(
            0,
            int(limits.get("klines", 0)) - int(used.get("klines", 0)),
        )
        configured_limit = max(
            0,
            int(getattr(self.settings, "launch_directional_max_candidates", 6)),
        )
        directional_items = [
            item
            for item in items
            if bool(item.get("launch_directional_cycle", True))
        ]
        selected_symbols = set(select_directional_candidates(
            directional_items,
            limit=min(configured_limit, remaining_kline_calls // 5),
        ))
        diagnostics = {
            "selected": len(selected_symbols),
            "ready": 0,
            "degraded": 0,
            "network_calls": 0,
            "selection_semantics": "bounded_after_existing_15m_discovery",
            "phase_forming": 0,
            "phase_confirmed": 0,
            "phase_extended_no_chase": 0,
            "phase_insufficient": 0,
            "phase_publish_blocked": 0,
        }
        for item in items:
            symbol = str(item.get("symbol") or "").upper()
            if not bool(item.get("launch_directional_cycle", True)):
                item["directional_analysis_status"] = "legacy_cycle"
                continue
            if symbol not in selected_symbols:
                item["directional_analysis_status"] = "budget_deferred"
                continue
            before_futures = int(used.get("klines", 0))
            before_spot = int(used.get("spot_klines", 0))
            try:
                base_rows = {
                    timeframe: source.klines(
                        symbol,
                        interval=timeframe,
                        limit=limit,
                        end_time=int(window_end_ms) - 1,
                    )
                    for timeframe, limit in {
                        "5m": 40,
                        "15m": 40,
                        "1h": 120,
                        "4h": 100,
                        "1d": 230,
                    }.items()
                }
                expanded = expand_timeframe_klines(
                    base_rows,
                    window_end_ms=int(window_end_ms),
                )
                multi = analyze_multi_timeframe(
                    expanded,
                    window_end_ms=int(window_end_ms),
                    rolling_24h={
                        "price_change_pct": item.get("price_24h"),
                        "oi_change_pct": item.get("oi_24h"),
                        "semantics": "background_only",
                    },
                )
                futures_flow = active_flow_window(
                    base_rows.get("15m", []),
                    interval_ms=TIMEFRAME_INTERVAL_MS["15m"],
                    window_end_ms=int(window_end_ms),
                    periods=4,
                )
                spot_flow = self._closed_spot_active_flow(
                    source,
                    symbol,
                    window_end_ms=int(window_end_ms),
                    periods=4,
                )
                trade_plans = build_trade_plans(multi)
                directional_facts = build_directional_facts(
                    item,
                    multi,
                    spot_flow=spot_flow,
                    futures_flow=futures_flow,
                    trade_plans=trade_plans,
                )
                flow_states: list[str] = []
                for flow, minimum_net in (
                    (spot_flow, self.settings.flow_spot_net_min_usd),
                    (futures_flow, self.settings.flow_futures_net_min_usd),
                ):
                    flow_status = str(flow.get("status") or "")
                    flow_net = _optional_finite(flow.get("net_usd"))
                    flow_gross = _optional_finite(flow.get("gross_usd"))
                    if flow_status not in {"available", "no_trades"}:
                        flow_states.append("insufficient")
                    elif (
                        flow_net is not None
                        and flow_gross is not None
                        and flow_gross >= float(minimum_net)
                        and abs(flow_net) >= float(minimum_net)
                    ):
                        flow_states.append("sufficient")
                    else:
                        flow_states.append("low")
                directional_facts.update({
                    "spot_cvd_net_usd": spot_flow.get("net_usd"),
                    "spot_cvd_gross_usd": spot_flow.get("gross_usd"),
                    "futures_cvd_net_usd": futures_flow.get("net_usd"),
                    "futures_cvd_gross_usd": futures_flow.get("gross_usd"),
                    "active_flow_scale_status": (
                        "insufficient"
                        if "insufficient" in flow_states
                        else "sufficient"
                        if flow_states == ["sufficient", "sufficient"]
                        else "low"
                    ),
                })
                signal = evaluate_directional_readiness(directional_facts)
            except Exception:
                item.update({
                    "directional_analysis_status": "local_error",
                    "directional_analysis_error": "launch_directional_analysis_failed",
                })
                diagnostics["degraded"] += 1
                continue
            finally:
                diagnostics["network_calls"] += max(
                    0,
                    int(used.get("klines", 0)) - before_futures,
                ) + max(
                    0,
                    int(used.get("spot_klines", 0)) - before_spot,
                )

            try:
                one_hour_summary = build_one_hour_phase_summary(
                    base_rows.get("1h", []),
                    window_end_ms=int(window_end_ms),
                    interval_ms=TIMEFRAME_INTERVAL_MS["1h"],
                )
                frames = multi.get("timeframes")
                frames = frames if isinstance(frames, Mapping) else {}
                one_hour_frame = frames.get("1h")
                one_hour_frame = (
                    one_hour_frame
                    if isinstance(one_hour_frame, Mapping)
                    else {}
                )
                one_hour_summary.update({
                    "bullish_reference_price": one_hour_frame.get(
                        "reference_high"
                    ),
                    "bearish_reference_price": one_hour_frame.get(
                        "reference_low"
                    ),
                })
                launch_phase = classify_launch_phase(
                    directional_facts,
                    one_hour_summary,
                    directional_signal=signal,
                )
            except Exception:
                launch_phase = classify_launch_phase(
                    directional_facts,
                    {},
                    directional_signal=signal,
                )
                launch_phase.update({
                    "timing_stage": "insufficient",
                    "execution_status": "blocked_data",
                    "primary_block_reason": "phase_local_error",
                    "initial_alert_eligible": False,
                    "plan_eligible": False,
                    "ai_eligible": False,
                    "reason_codes": ["phase_local_error"],
                })

            direction = str(signal.get("direction") or "none")
            plan_key = (
                "bearish"
                if direction.startswith("bearish")
                else "bullish"
                if direction.startswith("bullish")
                else ""
            )
            plan = trade_plans.get(plan_key, {}) if plan_key else {}
            item.update({
                "directional_analysis_status": (
                    "ready"
                    if signal.get("data_complete") or signal.get("observation_ready")
                    else "degraded"
                ),
                "multi_timeframe": multi,
                "directional_trade_plans": trade_plans,
                "directional_facts": directional_facts,
                "directional_readiness": signal,
                "launch_directional_readiness": signal,
                "spot_cvd_1h": spot_flow,
                "futures_cvd_1h": futures_flow,
                "launch_phase": launch_phase,
            })
            phase_status = str(
                launch_phase.get("timing_stage") or "insufficient"
            )
            phase_key = f"phase_{phase_status}"
            if phase_key in diagnostics:
                diagnostics[phase_key] += 1
            if (
                plan.get("status") == "available"
                and launch_phase.get("plan_eligible") is True
            ):
                item.update({
                    "entry_zone": plan.get("entry_zone"),
                    "invalidation_price": plan.get("invalidation_price"),
                    "targets": list(plan.get("targets") or []),
                    "risk_reward_ratio": plan.get("risk_reward_ratio"),
                })
            if signal.get("data_complete") or signal.get("observation_ready"):
                discovery_score = self._discovery_score(item)
                locked_side = str(
                    item.get("launch_directional_locked_side") or ""
                )
                current_side = (
                    "bearish"
                    if direction
                    in {
                        "bearish",
                        "bearish_candidate",
                        "bearish_divergence_watch",
                    }
                    else "bullish"
                    if direction
                    in {
                        "bullish",
                        "bullish_candidate",
                        "bullish_divergence_watch",
                    }
                    else ""
                )
                active_side = current_side or locked_side
                evidence = signal.get("evidence")
                evidence = evidence if isinstance(evidence, dict) else {}
                if active_side:
                    new_score = int(
                        to_float(
                            signal.get(f"{active_side}_evidence_score")
                            if signal.get(f"{active_side}_evidence_score") is not None
                            else signal.get(f"{active_side}_readiness")
                        )
                    )
                    item.update({
                        "discovery_score": discovery_score,
                        "bullish_evidence_score": int(
                            to_float(
                                signal.get("bullish_evidence_score")
                                if signal.get("bullish_evidence_score") is not None
                                else signal.get("bullish_readiness")
                            )
                        ),
                        "bearish_evidence_score": int(
                            to_float(
                                signal.get("bearish_evidence_score")
                                if signal.get("bearish_evidence_score") is not None
                                else signal.get("bearish_readiness")
                            )
                        ),
                        "score": new_score,
                        "raw_rule_score": new_score,
                        "evidence_score": new_score,
                        "score_semantics": "rule_score_not_probability",
                        "trigger_path": (
                            f"directional:{direction}"
                            if current_side
                            else f"directional:{locked_side}"
                        ),
                        "supporting_evidence": list(evidence.get(active_side) or []),
                        "counter_evidence": list(
                            evidence.get(
                                "bullish" if active_side == "bearish" else "bearish"
                            )
                            or []
                        ),
                        "limitations": list(signal.get("limitations") or []),
                        "evidence_strength": (
                            "strong"
                            if new_score >= 80
                            else "medium"
                            if new_score >= 60
                            else "low"
                        ),
                    })
                diagnostics["ready"] += 1
            else:
                diagnostics["degraded"] += 1
        return diagnostics

    @staticmethod
    def _directional_candidate_publishable(item: Mapping[str, Any]) -> bool:
        """Publish only timely first alerts while retaining active updates."""

        if not bool(item.get("launch_directional_cycle")):
            return True
        if not LaunchWarningRadar._directional_candidate_trackable(item):
            return False
        phase = item.get("launch_phase")
        if not isinstance(phase, Mapping):
            return False
        lifecycle = item.get("launch_lifecycle")
        publication = (
            lifecycle.get("publication")
            if isinstance(lifecycle, Mapping)
            and isinstance(lifecycle.get("publication"), Mapping)
            else item.get("launch_package")
        )
        previous_published = (
            publication.get("previous_published")
            if isinstance(publication, Mapping)
            else None
        )
        if isinstance(previous_published, Mapping) and bool(previous_published):
            return True
        return phase.get("initial_alert_eligible") is True

    @staticmethod
    def _directional_candidate_trackable(item: Mapping[str, Any]) -> bool:
        """Keep complete directional facts in bounded lifecycle tracking."""

        if not bool(item.get("launch_directional_cycle")):
            return True
        return bool(
            str(item.get("directional_analysis_status") or "") == "ready"
            and str(item.get("trigger_path") or "").startswith(
                ("directional:bullish", "directional:bearish")
            )
        )

    @staticmethod
    def _directional_observation_id(alert: dict[str, Any]) -> int:
        lifecycle = alert.get("launch_lifecycle")
        if not isinstance(lifecycle, dict):
            return 0
        return int(to_float(lifecycle.get("observation_id")))

    def _directional_ai_cache_key(
        self,
        alert: dict[str, Any],
        *,
        model: str,
        operator_prompt: str = "",
        base_url: str = "",
        cache_version: str = "",
    ) -> str:
        observation_id = self._directional_observation_id(alert)
        if observation_id <= 0:
            return ""
        prompt_hash = hashlib.sha256(
            str(operator_prompt or "").encode("utf-8")
        ).hexdigest()
        endpoint_hash = hashlib.sha256(
            str(base_url or "").encode("utf-8")
        ).hexdigest()
        material = (
            f"{observation_id}:{model}:{endpoint_hash}:{prompt_hash}:"
            f"{cache_version or self._AI_CACHE_VERSION}"
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _apply_directional_ai_result(
        alert: dict[str, Any],
        result: dict[str, Any],
        *,
        source: str,
    ) -> bool:
        alert["ai_interpreter"] = dict(result)
        alert["ai_interpretation_status"] = str(
            result.get("status") or "invalid_ai_output"
        )
        alert["ai_interpretation_source"] = source
        if result.get("status") != "available":
            alert.pop("ai_interpretation", None)
            return False
        parts = [str(result.get("summary") or "").strip()]
        risks = [
            str(value).strip()
            for value in (result.get("risk_notes") or [])[:2]
            if str(value).strip()
        ]
        waits = [
            str(value).strip()
            for value in (result.get("wait_for") or [])[:2]
            if str(value).strip()
        ]
        if risks:
            parts.append(f"风险：{'；'.join(risks)}")
        if waits:
            parts.append(f"等待：{'；'.join(waits)}")
        alert["ai_interpretation"] = " ".join(
            part for part in parts if part
        )[:600]
        return True

    def _persist_directional_ai_cache(
        self,
        alert: dict[str, Any],
        *,
        cache_key: str,
    ) -> None:
        if not cache_key:
            return
        if not hasattr(self.store, "load") or not hasattr(self.store, "save"):
            return
        state = self.store.load(self.settings.launch_state_path, {})
        if not isinstance(state, dict):
            return
        symbol = str(alert.get("symbol") or "")
        record = state.get(symbol)
        result = alert.get("ai_interpreter")
        if not isinstance(record, dict) or not isinstance(result, dict):
            return
        record["launch_ai_interpreter_cache"] = {
            "key": cache_key,
            "result": dict(result),
            "interpretation": str(alert.get("ai_interpretation") or "")[:600],
        }
        compacted, _ = compact_launch_state_records(state)
        self.store.save(self.settings.launch_state_path, compacted)

    def _interpret_directional_alerts(
        self,
        alerts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        interpreter_enabled = bool(
            self._launch_directional_active()
            and getattr(self.settings, "launch_ai_interpreter_enable", False)
        )
        automatic_enabled = bool(
            interpreter_enabled
            and getattr(self.settings, "launch_ai_auto_enable", False)
        )
        ai_configured = bool(
            str(getattr(self.settings, "ai_api_key", "") or "").strip()
            and str(getattr(self.settings, "ai_base_url", "") or "").strip()
            and str(getattr(self.settings, "ai_model", "") or "").strip()
        )
        bot_username = str(
            getattr(self.settings, "tg_bot_username", "") or ""
        ).strip().lstrip("@")
        private_admin_user_id = positive_telegram_user_id(
            getattr(self.settings, "tg_private_control_admin_user_id", None)
        )
        on_demand_route_ready = bool(
            getattr(self.settings, "tg_private_control_enable", False)
            and private_admin_user_id is not None
            and telegram_bot_username_configured(bot_username)
        )
        on_demand_available = bool(
            interpreter_enabled and ai_configured and on_demand_route_ready
        )
        on_demand_status = (
            "on_demand"
            if on_demand_available
            else "not_configured"
            if not ai_configured
            else "on_demand_route_not_configured"
        )
        diagnostics = {
            "enabled": automatic_enabled,
            "on_demand_available": on_demand_available,
            "status": (
                "disabled"
                if not interpreter_enabled
                else on_demand_status
                if not automatic_enabled
                else "not_configured"
            ),
            "eligible": 0,
            "calls": 0,
            "cached": 0,
            "deferred": 0,
            "max_calls_per_cycle": 1,
            "available": 0,
            "degraded": 0,
            "semantics": "ai_interprets_rules_and_never_changes_them",
        }
        def on_demand_snapshot_ready(alert: Mapping[str, Any]) -> bool:
            if not bool(alert.get("launch_message_package_v2")):
                return False
            try:
                snapshot = build_launch_ai_context(alert)
            except (TypeError, ValueError):
                return False
            rule_result = snapshot.get("rule_result")
            if not isinstance(rule_result, Mapping):
                return False
            stage = rule_result.get("stage") or rule_result.get("status")
            return bool(
                str(rule_result.get("direction") or "").strip()
                and str(stage or "").strip()
            )

        for alert in alerts:
            alert["ai_interpretation_source"] = "none"
            alert["ai_on_demand_ready"] = False
            alert.pop("ai_interpretation", None)
            if not interpreter_enabled:
                alert["ai_interpretation_status"] = "disabled"
            elif automatic_enabled:
                alert["ai_interpretation_status"] = "not_eligible"
            elif not ai_configured:
                alert["ai_interpretation_status"] = "not_configured"
            elif not getattr(self.settings, "tg_private_control_enable", False):
                alert["ai_interpretation_status"] = "private_control_not_ready"
            elif private_admin_user_id is None:
                alert["ai_interpretation_status"] = "private_control_not_ready"
            elif not telegram_bot_username_configured(bot_username):
                alert["ai_interpretation_status"] = "bot_username_missing"
            elif on_demand_snapshot_ready(alert):
                alert["ai_interpretation_status"] = "on_demand"
                alert["ai_on_demand_ready"] = True
            else:
                alert["ai_interpretation_status"] = (
                    "on_demand_card_not_supported"
                )
        if not automatic_enabled:
            diagnostics["eligible"] = sum(
                alert.get("ai_on_demand_ready") is True for alert in alerts
            )
            if on_demand_available and diagnostics["eligible"] == 0:
                diagnostics["status"] = "on_demand_no_supported_card"
            return diagnostics
        for alert in alerts:
            directional = alert.get("directional_readiness")
            if (
                not isinstance(directional, Mapping)
                or not bool(directional.get("data_complete"))
                or alert.get("directional_cycle_invalidated")
            ):
                alert["ai_interpretation_status"] = (
                    "not_eligible_directional_incomplete"
                )
                continue
            launch_phase = alert.get("launch_phase")
            if not isinstance(launch_phase, Mapping):
                alert["ai_interpretation_status"] = (
                    "not_eligible_phase_missing"
                )
                continue
            if launch_phase.get("ai_eligible") is not True:
                timing_stage = str(
                    launch_phase.get("timing_stage") or "insufficient"
                )
                execution_status = str(
                    launch_phase.get("execution_status") or "blocked_data"
                )
                alert["ai_interpretation_status"] = (
                    "not_eligible_phase_extended"
                    if (
                        timing_stage == "extended_no_chase"
                        or execution_status == "blocked_extension"
                    )
                    else "not_eligible_phase_low_volume"
                    if execution_status == "blocked_volume"
                    else "not_eligible_phase_low_flow_scale"
                    if execution_status == "blocked_flow_scale"
                    else "not_eligible_phase_crowding"
                    if execution_status == "blocked_crowding"
                    else "not_eligible_phase_insufficient"
                    if (
                        timing_stage == "insufficient"
                        or execution_status == "blocked_data"
                    )
                    else "not_eligible_phase_timing"
                )
                continue
        eligible_alerts = [
            alert
            for alert in alerts
            if isinstance(alert.get("directional_readiness"), dict)
            and bool(alert["directional_readiness"].get("data_complete"))
            and isinstance(alert.get("launch_phase"), Mapping)
            and alert["launch_phase"].get("ai_eligible") is True
            and not alert.get("directional_cycle_invalidated")
        ]
        diagnostics["eligible"] = len(eligible_alerts)
        if not eligible_alerts:
            diagnostics["status"] = "no_eligible_alert"
            return diagnostics
        for alert in eligible_alerts:
            alert["ai_interpretation_status"] = "not_configured"
        api_key = str(getattr(self.settings, "ai_api_key", "") or "").strip()
        base_url = str(getattr(self.settings, "ai_base_url", "") or "").strip()
        model = str(getattr(self.settings, "ai_model", "") or "").strip()
        operator_prompt = str(
            getattr(self.settings, "ai_operator_prompt", "") or ""
        ).strip()
        if not (api_key and base_url and model):
            return diagnostics
        uncached_alerts: list[dict[str, Any]] = []
        for alert in eligible_alerts:
            cache_key = self._directional_ai_cache_key(
                alert,
                model=model,
                operator_prompt=operator_prompt,
                base_url=base_url,
            )
            cached = alert.get("launch_ai_interpreter_cache")
            cached_result = (
                dict(cached["result"])
                if isinstance(cached, dict)
                and isinstance(cached.get("result"), dict)
                else None
            )
            previous_cache_keys = {
                self._directional_ai_cache_key(
                    alert,
                    model=model,
                    operator_prompt=operator_prompt,
                    base_url=base_url,
                    cache_version=cache_version,
                )
                for cache_version in (
                    self._PREVIOUS_AI_CACHE_VERSION,
                    *self._LEGACY_AI_CACHE_VERSIONS,
                )
            }
            previous_cache_match = bool(
                cached_result
                and cached.get("key") in previous_cache_keys
            )
            reusable_previous_result = bool(
                previous_cache_match
                and cached_result.get("status") != "ai_output_truncated"
            )
            if (
                cache_key
                and isinstance(cached, dict)
                and cached_result is not None
                and (
                    cached.get("key") == cache_key
                    or reusable_previous_result
                )
            ):
                if self._apply_directional_ai_result(
                    alert,
                    cached_result,
                    source="cache",
                ):
                    diagnostics["available"] += 1
                else:
                    diagnostics["degraded"] += 1
                diagnostics["cached"] += 1
                if reusable_previous_result:
                    self._persist_directional_ai_cache(
                        alert,
                        cache_key=cache_key,
                    )
                continue
            uncached_alerts.append(alert)
        diagnostics["deferred"] = max(0, len(uncached_alerts) - 1)
        for alert in uncached_alerts[1:]:
            alert["ai_interpretation_status"] = "deferred_cycle_limit"
        if not uncached_alerts:
            diagnostics["status"] = "cached"
            return diagnostics
        first_alert = uncached_alerts[0]
        session = requests.Session()
        try:
            interpreter = OpenAiCompatibleLaunchInterpreter(
                api_key=api_key,
                base_url=base_url,
                model=model,
                session=session,
                timeout_sec=float(getattr(self.settings, "ai_timeout_sec", 60)),
                max_retries=0,
                operator_prompt=operator_prompt,
            )
        except (TypeError, ValueError):
            session.close()
            diagnostics["status"] = "invalid_configuration"
            first_alert["ai_interpretation_status"] = "invalid_configuration"
            return diagnostics
        diagnostics["status"] = "ok"
        try:
            for alert in [first_alert]:
                signal = alert.get("directional_readiness")
                if not isinstance(signal, dict) or not signal.get("data_complete"):
                    continue
                plan = {
                    "status": "available",
                    "entry_zone_low": (
                        (alert.get("entry_zone") or {}).get("low")
                        if isinstance(alert.get("entry_zone"), dict)
                        else None
                    ),
                    "entry_zone_high": (
                        (alert.get("entry_zone") or {}).get("high")
                        if isinstance(alert.get("entry_zone"), dict)
                        else None
                    ),
                    "invalidation_price": alert.get("invalidation_price"),
                    "targets": list(alert.get("targets") or []),
                    "risk_reward_ratio": alert.get("risk_reward_ratio"),
                }
                result = interpreter.interpret(
                    {
                        **alert,
                        "rule_result": signal,
                        "plan": plan,
                    },
                    enabled=True,
                )
                diagnostics["calls"] += 1
                if self._apply_directional_ai_result(
                    alert,
                    result,
                    source="provider",
                ):
                    diagnostics["available"] += 1
                else:
                    diagnostics["degraded"] += 1
                self._persist_directional_ai_cache(
                    alert,
                    cache_key=self._directional_ai_cache_key(
                        alert,
                        model=model,
                        operator_prompt=operator_prompt,
                        base_url=base_url,
                    ),
                )
        finally:
            session.close()
        return diagnostics

    def _apply_launch_fusion_score(self, item: dict[str, Any]) -> None:
        fusion = score_launch_signal({
            "price_15m": item.get("price_15m"),
            "price_1h": item.get("price_1h"),
            "oi_15m": item.get("oi_15m"),
            "oi_1h": item.get("oi_1h"),
            "volume_ratio": item.get("volume_ratio"),
            "breakout": item.get("breakout"),
            "spot_active_ratio": item.get("spot_active_ratio"),
            "futures_active_ratio": item.get("futures_active_ratio"),
            "asset_subclass": item.get("asset_subclass"),
            "liquidity_tier": item.get("liquidity_tier"),
            "recent_volatility_pct": item.get("recent_volatility_pct"),
        })
        raw_score = min(100, int(fusion.get("score") or 0))
        trigger_path = str(fusion.get("trigger_path") or "none")
        effective_score = raw_score
        policy_block_reason = ""
        if (
            trigger_path == "none"
            and raw_score >= self.settings.launch_min_score_push
        ):
            effective_score = max(
                0,
                int(self.settings.launch_min_score_push) - 1,
            )
            policy_block_reason = "no_independent_evidence_path"

        evidence_strength = (
            "strong"
            if raw_score >= self.settings.launch_breakout_score
            else "medium"
            if raw_score >= self.settings.launch_watch_score
            else "low"
        )
        identity_coverage = (
            "classified"
            if str(item.get("asset_category_source") or "")
            not in {"", "symbol_fallback"}
            else "conservative_fallback"
        )
        item.update({
            "discovery_score": effective_score,
            "discovery_raw_score": raw_score,
            "score": effective_score,
            "raw_rule_score": raw_score,
            "score_semantics": SCORE_SEMANTICS,
            "evidence_strength": evidence_strength,
            "data_completeness": "complete",
            "identity_coverage": identity_coverage,
            "trigger_path": trigger_path,
            "policy_block_reason": policy_block_reason,
            "supporting_evidence": list(
                fusion.get("supporting_evidence") or []
            ),
            "counter_evidence": list(fusion.get("counter_evidence") or []),
            "fusion_analysis": fusion,
            "reasons": list(fusion.get("supporting_evidence") or [])[:5],
            "limitations": [
                "rule_score_not_probability",
                "rolling_24h_not_closed_window",
                "active_flow_may_be_unavailable",
            ],
        })

    def _launch_funding_context(
        self,
        source: BinanceDataSource,
        symbol: str,
        funding_pct: float,
        next_funding_time_ms: int = 0,
    ) -> dict[str, Any]:
        context: dict[str, Any] = {
            "funding_available": True,
            "funding_pct": funding_pct,
            "funding_next_time_ms": next_funding_time_ms,
            "funding_interval_hours": 0,
            "funding_interval_transition": "",
        }
        if not hasattr(source, "funding_rate"):
            return context
        try:
            history = source.funding_rate(symbol, limit=4)
        except Exception:
            history = []
        if not isinstance(history, list):
            history = []
        transition = funding_interval_transition(history, next_funding_time_ms)
        if transition:
            context["funding_interval_hours"] = int(transition.get("current_interval_hours", 0) or 0)
            context["funding_interval_transition"] = str(transition.get("transition_text") or "")
            context["funding_previous_interval_hours"] = int(transition.get("previous_interval_hours", 0) or 0)
            context["funding_previous_time_ms"] = int(transition.get("previous_funding_time_ms", 0) or 0)
            context["funding_current_time_ms"] = int(transition.get("current_funding_time_ms", 0) or 0)
            return context

        points = sorted(
            [int(to_float(item.get("fundingTime"))) for item in history if to_float(item.get("fundingTime")) > 0]
        )
        if len(points) >= 2:
            context["funding_interval_hours"] = funding_interval_hours(points[-1] - points[-2])
        elif len(points) == 1 and next_funding_time_ms > points[-1]:
            context["funding_interval_hours"] = funding_interval_hours(next_funding_time_ms - points[-1])
        return context

    @staticmethod
    def _discovery_score(item: Mapping[str, Any]) -> int:
        value = item.get("discovery_score")
        if value is None:
            value = item.get("score")
        return int(to_float(value))

    @staticmethod
    def _optional_evidence_score(item: Mapping[str, Any]) -> int | None:
        value = item.get("evidence_score")
        if value is not None:
            return int(to_float(value))
        signal = item.get("directional_readiness")
        if isinstance(signal, Mapping):
            direction = str(signal.get("direction") or "")
            side = (
                "bearish"
                if direction.startswith("bearish")
                else "bullish"
                if direction.startswith("bullish")
                else ""
            )
            if side:
                canonical = signal.get(f"{side}_evidence_score")
                legacy = signal.get(f"{side}_readiness")
                return int(to_float(canonical if canonical is not None else legacy))
        analysis_status = str(item.get("directional_analysis_status") or "")
        directional_context = bool(item.get("launch_directional_cycle")) or (
            analysis_status not in {"", "disabled", "legacy_cycle"}
        )
        if directional_context:
            return None
        return LaunchWarningRadar._discovery_score(item)

    @staticmethod
    def _evidence_score(item: Mapping[str, Any]) -> int:
        value = LaunchWarningRadar._optional_evidence_score(item)
        return value if value is not None else 0

    def _launch_stage(self, score: int) -> str:
        return self.launch_stage_for_score(
            score,
            watching=self.settings.launch_watch_score,
            primed=self.settings.launch_primed_score,
            breakout=self.settings.launch_breakout_score,
            launched=self.settings.launch_launched_score,
        )

    def _inactive_launch_record(
        self,
        previous: Any,
        now_ts: int,
        *,
        fail_reason: str,
    ) -> dict[str, Any] | None:
        if not isinstance(previous, dict) or not previous:
            return None
        record = dict(previous)
        stage = str(record.get("stage") or "idle")
        active_stages = {"watching", "primed", "breakout", "launched"}
        grace_sec = max(0, int(self.settings.launch_invalidation_grace_sec))

        if stage in active_stages:
            record["previous_stage"] = stage
            record["stage"] = "cooling"
            record["cooling_at"] = int(now_ts)
            record["last_seen"] = int(now_ts)
            if grace_sec > 0:
                return record
            stage = "cooling"

        if stage == "cooling":
            cooling_at = int(record.get("cooling_at", now_ts) or now_ts)
            record["last_seen"] = int(now_ts)
            if int(now_ts) - cooling_at < grace_sec:
                return record
            record["stage"] = "failed"
            record["failed_at"] = int(now_ts)
            record["fail_reason"] = str(fail_reason)
            record.pop("delete_pending", None)
            record["message_cleanup_complete"] = True
            return record

        if stage == "failed":
            return record
        return None

    @staticmethod
    def launch_stage_for_score(
        score: int,
        *,
        watching: int = 45,
        primed: int = 60,
        breakout: int = 75,
        launched: int = 90,
    ) -> str:
        if score >= launched:
            return "launched"
        if score >= breakout:
            return "breakout"
        if score >= primed:
            return "primed"
        if score >= watching:
            return "watching"
        return "idle"

    @staticmethod
    def _stage_rank(stage: str) -> int:
        return {
            "idle": 0,
            "cooling": 0,
            "failed": 0,
            "risk": 0,
            "watching": 1,
            "primed": 2,
            "breakout": 3,
            "launched": 4,
        }.get(stage, 0)

    @staticmethod
    def _stage_label(stage: str) -> str:
        return {
            "idle": "未触发",
            "cooling": "降温确认",
            "failed": "失效",
            "risk": "风险",
            "watching": "提前观察",
            "primed": "提前预警",
            "breakout": "启动确认",
            "launched": "启动瞬间",
        }.get(stage, stage or "未知")

    @staticmethod
    def _format_launch_funding_transitions(rows: list[dict[str, Any]]) -> list[str]:
        lines: list[str] = []
        for row in rows:
            transition = str(row.get("funding_interval_transition") or "").strip()
            if transition:
                exchange = str(row.get("exchange") or "Unknown").strip()
                lines.append(f"{exchange}周期: {transition}")
        return lines

    @staticmethod
    def _launch_package_time(value: Any) -> str:
        timestamp = int(to_float(value))
        if timestamp <= 0:
            return "未知"
        return datetime.fromtimestamp(timestamp, CST).strftime("%m-%d %H:%M")

    @staticmethod
    def _launch_package_duration(value: Any) -> str:
        seconds = max(0, int(to_float(value)))
        hours, remainder = divmod(seconds, 3600)
        minutes = remainder // 60
        if hours:
            return f"{hours}小时{minutes:02d}分钟"
        return f"{minutes}分钟"

    @staticmethod
    def _launch_package_stage_delay(value: Any) -> str:
        seconds = max(0, int(to_float(value)))
        if seconds == 0:
            return "首次信号即达到"
        return f"{LaunchWarningRadar._launch_package_duration(seconds)}后"

    @staticmethod
    def _launch_package_delta(current: Any, base: Any) -> float | None:
        current_value = to_float(current)
        base_value = to_float(base)
        if base_value <= 0:
            return None
        return (current_value / base_value - 1.0) * 100.0

    @staticmethod
    def _launch_package_funding(
        snapshot: dict[str, Any] | None,
        *,
        last_confirmed_interval_hours: int = 0,
    ) -> str:
        if not isinstance(snapshot, dict):
            return "暂不可用"
        funding = to_float(snapshot.get("funding_pct"))
        interval = int(to_float(snapshot.get("funding_interval_hours")))
        if interval > 0:
            return funding_cycle_text(funding, interval)
        if last_confirmed_interval_hours > 0:
            return (
                f"{funding:+.4f}%/本次未确认"
                f"（上次确认{funding_interval_label(last_confirmed_interval_hours)}）"
            )
        return f"{funding:+.4f}%/周期暂不可用"

    @staticmethod
    def _launch_package_direction(value: Any) -> str:
        return {
            "both_buy": "现货与合约主动买入同步",
            "both_sell": "现货与合约主动卖出同步",
            "divergence_spot_buy_futures_sell": "现货主动买入、合约主动卖出",
            "divergence_spot_sell_futures_buy": "现货主动卖出、合约主动买入",
            "unknown": "主动成交方向暂不可用",
        }.get(str(value or "unknown"), "主动成交方向暂不可用")

    @staticmethod
    def _launch_price_action_label(state: Any) -> str:
        if not isinstance(state, dict) or not state.get("enabled"):
            return ""
        return {
            "watching": "结构监控中",
            "breakout_15m": "15m实体收盘突破，等待1h确认",
            "confirmed_1h": "1h实体收盘确认，等待4h确认",
            "confirmed_4h": "4h实体收盘确认",
            "sweep_high_15m": "15m长上影扫高，假突破",
            "sweep_low_15m": "15m长下影下探后收回，假跌破",
            "false_breakout_15m": "15m回落并收长影，突破失败",
            "failed_breakout_15m": "15m重新收回结构内，突破失效",
            "false_breakout_1h": "1h长上/下影扫除，假突破确认",
            "failed_breakout_1h": "1h未能站稳结构位，突破失效",
            "false_breakout_4h": "4h长上/下影扫除，假突破确认",
            "failed_breakout_4h": "4h未能站稳结构位，突破失效",
        }.get(str(state.get("status") or ""), "结构状态待确认")

    def _launch_price_action_lines(self, state: Any) -> list[str]:
        label = self._launch_price_action_label(state)
        if not label or not isinstance(state, dict):
            return []
        status = str(state.get("status") or "")
        timeframe = (
            "4h"
            if status.endswith("_4h")
            else "1h"
            if status.endswith("_1h")
            else "15m"
        )
        timeframes = state.get("timeframes")
        frame = (
            timeframes.get(timeframe)
            if isinstance(timeframes, dict)
            and isinstance(timeframes.get(timeframe), dict)
            else {}
        )
        details: list[str] = []
        level = to_float(state.get("level"))
        if level > 0:
            details.append(f"结构位 {fmt_price(level)}")
        direction = str(state.get("direction") or "")
        if direction:
            details.append("上破方向" if direction == "up" else "下破方向")
        if "sweep_high" in status or (
            "false_breakout" in status and direction == "up"
        ):
            details.append(
                f"上影/实体 {to_float(frame.get('upper_wick_body_ratio')):.2f}x"
            )
        elif "sweep_low" in status or (
            "false_breakout" in status and direction == "down"
        ):
            details.append(
                f"下影/实体 {to_float(frame.get('lower_wick_body_ratio')):.2f}x"
            )
        confirmed = [
            str(value)
            for value in (state.get("confirmed_timeframes") or [])
            if str(value)
        ]
        if confirmed:
            details.append("已确认 " + "→".join(confirmed))
        lines = ["", tg_quote("结构确认"), f"状态: {tg_escape(label)}"]
        if details:
            lines.append("｜".join(details))
        return lines

    @staticmethod
    def _launch_supporting_evidence_lines(item: dict[str, Any]) -> list[str]:
        lines: list[str] = []
        accumulation = item.get("accumulation_quality_evidence")
        if isinstance(accumulation, dict):
            result = "通过" if accumulation.get("eligible") else "未通过"
            reason = str(accumulation.get("reason_text") or "暂无明确结论")
            lines.append(
                f"吸筹质量: {result}｜{tg_escape(reason)}（辅助证据，不参与打分）"
            )

        announcements = item.get("announcement_evidence")
        if isinstance(announcements, list):
            for evidence in announcements[:2]:
                if not isinstance(evidence, dict):
                    continue
                kind = "风险" if evidence.get("kind") == "risk" else "机会"
                title = tg_escape(str(evidence.get("title") or "Binance 官方公告"))
                url = escape(str(evidence.get("url") or ""), quote=True)
                link = f'<a href="{url}">查看公告</a>' if url.startswith("https://") else ""
                suffix = f"｜{link}" if link else ""
                lines.append(
                    f"官方公告: {kind}｜{title}{suffix}（辅助证据，不参与打分）"
                )

        return ["", tg_quote("辅助证据"), *lines] if lines else []

    def _format_launch_package(self, item: dict[str, Any]) -> str:
        if (
            (
                self._launch_directional_active()
                or bool(item.get("launch_directional_cycle"))
            )
            and isinstance(item.get("directional_readiness"), dict)
        ):
            return format_launch_directional_signal(item)
        mode_marker = item.get("launch_fusion_cycle")
        fusion_active = (
            bool(mode_marker)
            if isinstance(mode_marker, bool)
            else self._launch_fusion_active()
        )
        if fusion_active:
            return format_launch_fusion_package(item, self.settings)
        lifecycle = item.get("launch_lifecycle")
        publication = item.get("launch_package")
        if not isinstance(lifecycle, dict) or not isinstance(publication, dict):
            return self._format_launch_alert({**item, "launch_message_package_v2": False})

        market_cap = to_float(item.get("mcap"))
        quote_volume = to_float(item.get("quote_volume"))
        market_cap_source = str(item.get("mcap_source") or "").strip()
        market_cap_text = (
            f"{fmt_money(market_cap)}（{market_cap_tier(market_cap)}，来源 {market_cap_source or '未知'}）"
            if market_cap > 0
            else "未收录（Binance/CoinPaprika 均无）"
        )
        liquidity_text = (
            f"{fmt_money(quote_volume)}/24h（{liquidity_tier(quote_volume)}）"
            if quote_volume > 0
            else "暂无数据（未知流动性）"
        )
        first = publication.get("first") if isinstance(publication.get("first"), dict) else {}
        previous = (
            publication.get("previous_published")
            if isinstance(publication.get("previous_published"), dict)
            else first
        )
        current = publication.get("current") if isinstance(publication.get("current"), dict) else {}
        checkpoint_no = int(publication.get("checkpoint_no") or 1)
        current_stage = self._stage_label(str(current.get("stage") or item.get("stage") or "idle"))
        first_stage = self._stage_label(str(first.get("stage") or "idle"))
        peak_stage = self._stage_label(str(lifecycle.get("peak_stage") or item.get("stage") or "idle"))
        price_from_first = self._launch_package_delta(current.get("price"), first.get("price"))
        oi_from_first = self._launch_package_delta(current.get("oi_usd"), first.get("oi_usd"))
        price_from_previous = self._launch_package_delta(current.get("price"), previous.get("price"))
        oi_from_previous = self._launch_package_delta(current.get("oi_usd"), previous.get("oi_usd"))
        reasons = [
            {
                "cycle_opened": "首次达到启动阈值",
                "stage_changed": "生命周期阶段变化",
                "score_delta": f"分数变化≥{self.settings.launch_package_score_delta}",
                "price_delta": f"价格变化≥{self.settings.launch_package_price_delta_pct:g}%",
                "oi_delta": f"OI变化≥{self.settings.launch_package_oi_delta_pct:g}%",
                "funding_interval_changed": "资金费率结算周期变化",
                "funds_divergence": "现货/合约主动成交方向背离",
                "price_action_changed": "突破/假突破结构状态变化",
                "confirmation_changed": "1小时确认状态变化",
                "quadrant_changed": "价格与持仓结构变化",
                "active_message_missing": "有效信号消息缺失，自动恢复",
            }.get(str(reason), "其他状态变化")
            for reason in (publication.get("checkpoint_reasons") or [])
        ]

        checkpoints = [
            checkpoint
            for checkpoint in (publication.get("checkpoints") or [])
            if isinstance(checkpoint, dict)
        ]
        timeline = checkpoints[-5:] + [current]
        timeline_lines: list[str] = []
        for point in timeline:
            event_no = (
                checkpoint_no
                if point is current
                else int(point.get("checkpoint_no") or 0)
            )
            timeline_lines.append(
                f"{event_no:02d}. "
                f"{self._launch_package_time(point.get('window_end_ts'))}｜"
                f"{self._stage_label(str(point.get('stage') or 'idle'))} "
                f"{int(point.get('score') or 0)}分"
            )

        last_confirmed_interval = 0
        for point in [previous, *reversed(checkpoints), first]:
            point_interval = int(to_float(point.get("funding_interval_hours")))
            if point_interval > 0:
                last_confirmed_interval = point_interval
                break
        first_funding = self._launch_package_funding(first)
        current_funding = self._launch_package_funding(
            current,
            last_confirmed_interval_hours=last_confirmed_interval,
        )
        direction_text = self._launch_package_direction(current.get("funds_direction"))
        price_action_lines = self._launch_price_action_lines(
            current.get("price_action")
        )
        supporting_evidence_lines = self._launch_supporting_evidence_lines(item)
        outcome_evaluation = (
            lifecycle.get("outcome_evaluation")
            if isinstance(lifecycle.get("outcome_evaluation"), dict)
            else {}
        )
        progress = (
            outcome_evaluation.get("progress")
            if isinstance(outcome_evaluation.get("progress"), dict)
            else {}
        )
        outcome = (
            outcome_evaluation.get("outcome")
            if isinstance(outcome_evaluation.get("outcome"), dict)
            else {}
        )
        reliability = (
            outcome_evaluation.get("reliability")
            if isinstance(outcome_evaluation.get("reliability"), dict)
            else {}
        )
        outcome_lines: list[str] = []
        if outcome_evaluation.get("enabled") and progress:
            follow_threshold = to_float(
                reliability.get("follow_through_threshold_pct")
                or self.settings.launch_outcome_follow_through_pct
            )
            if outcome:
                outcome_label = {
                    "launched_follow_through": "达到启动瞬间且价格完成跟随",
                    "confirmed_follow_through": "达到启动确认且价格完成跟随",
                    "price_follow_through_only": "价格完成跟随但未达到启动确认",
                    "confirmed_no_follow_through": "达到启动确认但价格未完成跟随",
                    "false_start": "未达到启动确认且价格未完成跟随",
                }.get(str(outcome.get("label") or ""), "本轮已完成评估")
                outcome_lines.extend([
                    "",
                    tg_quote("本轮结果"),
                    f"状态: 已结束｜{outcome_label}",
                    f"结束收益: {to_float(progress.get('end_return_pct')):+.2f}%｜"
                    f"有效观察: {int(progress.get('observation_count') or 0)}根15m",
                ])
            else:
                outcome_lines.extend([
                    "",
                    tg_quote("本轮进展"),
                    "状态: 监控中｜本轮结束后才计入历史样本",
                ])
            outcome_lines.extend([
                f"最高/最低收盘变动: "
                f"{to_float(progress.get('max_favorable_return_pct')):+.2f}% / "
                f"{to_float(progress.get('max_adverse_return_pct')):+.2f}%",
                f"OI最高/最低变动: "
                f"{to_float(progress.get('max_oi_increase_pct')):+.2f}% / "
                f"{to_float(progress.get('max_oi_decrease_pct')):+.2f}%",
                "首次达到启动确认: "
                + (
                    self._launch_package_stage_delay(progress.get("time_to_confirm_sec"))
                    if progress.get("confirmed")
                    else "尚未达到"
                ),
                "首次达到启动瞬间: "
                + (
                    self._launch_package_stage_delay(progress.get("time_to_launch_sec"))
                    if progress.get("launched")
                    else "尚未达到"
                ),
                "",
                tg_quote("历史可靠度"),
            ])
            completed_samples = int(reliability.get("completed_samples") or 0)
            minimum_samples = int(
                reliability.get("minimum_samples")
                or self.settings.launch_outcome_min_samples
            )
            if reliability.get("rates_available"):
                outcome_lines.extend([
                    f"状态: 已达到复盘门槛｜同口径 {completed_samples} 轮",
                    f"启动确认率 {to_float(reliability.get('confirmed_rate_pct')):.1f}%｜"
                    f"启动瞬间率 {to_float(reliability.get('launched_rate_pct')):.1f}%｜"
                    f"收盘跟随率 {to_float(reliability.get('followed_through_rate_pct')):.1f}%",
                    f"中位最高/最低收盘变动 "
                    f"{to_float(reliability.get('median_max_favorable_return_pct')):+.2f}% / "
                    f"{to_float(reliability.get('median_max_adverse_return_pct')):+.2f}%",
                ])
            else:
                outcome_lines.extend([
                    f"状态: 样本积累中｜同口径已完成 {completed_samples}/{minimum_samples} 轮",
                    f"原始计数: 启动确认 {int(reliability.get('confirmed_count') or 0)}｜"
                    f"启动瞬间 {int(reliability.get('launched_count') or 0)}｜"
                    f"收盘涨幅达到 +{follow_threshold:g}% "
                    f"{int(reliability.get('followed_through_count') or 0)}",
                    "样本未达门槛，不展示比例，也不自动调整信号参数。",
                ])
            symbol_samples = int(
                reliability.get("symbol_completed_samples") or 0
            )
            if symbol_samples:
                outcome_lines.append(
                    f"该币历史完整周期: {symbol_samples}轮；样本不足时不单列比率。"
                )
        status = str(lifecycle.get("cycle_status") or "active")
        end_reason_text = launch_end_reason_text(lifecycle.get("end_reason"))
        ending_lines = (
            [
                "",
                tg_quote("本轮结束"),
                f"失效原因: {tg_escape(end_reason_text)}",
            ]
            if status == "failed"
            else []
        )
        return "\n".join([
            f"🚀 {coin_link(item)}｜第{int(lifecycle.get('cycle_no') or 1)}轮启动跟踪｜事件{checkpoint_no:02d}",
            f"⏰ {cst_now_text()}",
            "",
            f"{tg_bold('当前')}: {current_stage} {int(current.get('score') or item.get('score') or 0)}分",
            f"{tg_bold('首次出现')}: {self._launch_package_time(first.get('window_end_ts'))}｜{first_stage} {int(first.get('score') or 0)}分",
            f"{tg_bold('持续时间')}: {self._launch_package_duration(lifecycle.get('duration_sec'))}",
            f"{tg_bold('最高阶段')}: {peak_stage}",
            f"{tg_bold('本次更新')}: {tg_escape('、'.join(reasons) or '重要状态更新')}",
            "",
            tg_quote("市场概况"),
            f"品类: {tg_escape(item.get('asset_category_label') or '未分类')}",
            f"市值: {market_cap_text}",
            f"流动性: {liquidity_text}",
            *price_action_lines,
            *supporting_evidence_lines,
            "",
            tg_quote("相对首次"),
            f"价格: {price_from_first:+.2f}%" if price_from_first is not None else "价格: 暂不可比",
            f"OI: {oi_from_first:+.2f}%" if oi_from_first is not None else "OI: 暂不可比",
            f"资金费率: {first_funding} → {current_funding}",
            "",
            tg_quote("相对上次发布"),
            f"价格: {price_from_previous:+.2f}%" if price_from_previous is not None else "价格: 暂不可比",
            f"OI: {oi_from_previous:+.2f}%" if oi_from_previous is not None else "OI: 暂不可比",
            f"分数: {int(previous.get('score') or 0)} → {int(current.get('score') or 0)}",
            f"主动成交: {tg_escape(direction_text)}",
            "",
            tg_quote("事件轴"),
            *timeline_lines,
            *outcome_lines,
            "",
            f"{tg_bold('数据确认')}: {tg_escape(confirmation_text(item))}",
            *ending_lines,
        ])

    def _format_launch_alert(self, item: dict[str, Any]) -> str:
        if item.get("launch_message_package_v2"):
            return self._format_launch_package(item)
        stage_name = self._stage_label(str(item.get("stage", "")))
        previous_stage = self._stage_label(str(item.get("previous_stage", "idle")))
        current_stage = self._stage_label(str(item.get("stage", "")))
        market_cap = to_float(item.get("mcap"))
        quote_volume = to_float(item.get("quote_volume"))
        market_cap_source = str(item.get("mcap_source") or "").strip()
        market_cap_text = (
            f"{fmt_money(market_cap)}（{market_cap_tier(market_cap)}，来源 {market_cap_source or '未知'}）"
            if market_cap > 0
            else "未收录（Binance/CoinPaprika 均无）"
        )
        liquidity_text = (
            f"{fmt_money(quote_volume)}/24h（{liquidity_tier(quote_volume)}）"
            if quote_volume > 0
            else "暂无数据（未知流动性）"
        )
        funding_pct = to_float(item.get("funding_pct"))
        funding_interval = int(to_float(item.get("funding_interval_hours")))
        funding_text = funding_cycle_text(funding_pct, funding_interval)
        funding_label = funding_extreme_label(funding_pct)
        if funding_label:
            funding_text = f"{funding_text}（{funding_label}）"
        funding_transition = str(item.get("funding_interval_transition") or "").strip()
        funding_available = bool(item.get("funding_available")) or funding_interval > 0 or bool(funding_transition)
        raw_funding_exchanges = item.get("funding_exchanges", [])
        if not isinstance(raw_funding_exchanges, list):
            raw_funding_exchanges = []
        funding_exchanges = [
            row for row in raw_funding_exchanges
            if isinstance(row, dict) and row.get("exchange")
        ]
        funding_exchange_table = funding_table(funding_exchanges, self.settings) if funding_exchanges else ""
        funding_transition_lines = self._format_launch_funding_transitions(funding_exchanges)
        supporting_evidence_lines = self._launch_supporting_evidence_lines(item)
        single_funding_available = funding_available and not funding_exchange_table
        lines = [
            f"🚀 {tg_bold('启动雷达')} {coin_link(item)}",
            f"⏰ {cst_now_text()}",
            "",
            f"{tg_bold('阶段')}: {stage_name}",
            f"{tg_bold('分数')}: {item['score']}",
            f"{tg_bold('状态')}: {previous_stage} -> {current_stage} | 累计{item.get('appear_count', 1)}次",
            "",
            tg_quote("市场概况"),
            f"品类: {tg_escape(item.get('asset_category_label') or '未分类')}",
            f"市值: {market_cap_text}",
            f"流动性: {liquidity_text}",
            "",
            tg_quote("触发明细"),
            f"15m价格: {item['price_15m']:+.1f}%",
            f"1h价格: {item['price_1h']:+.1f}%",
            f"15m OI: {item['oi_15m']:+.1f}%",
            f"1h OI: {item['oi_1h']:+.1f}%",
            f"成交量: {item['volume_ratio']:.1f}x 均值",
            f"数据确认: ✅ {tg_escape(confirmation_text(item))}",
            *([f"资金费率: {funding_text}"] if single_funding_available else []),
            *([f"结算周期: {funding_transition}"] if funding_transition and single_funding_available else []),
            *(
                ["", tg_quote("多交易所资金费率"), funding_exchange_table, *funding_transition_lines]
                if funding_exchange_table
                else []
            ),
            *supporting_evidence_lines,
            "",
            tg_quote("判断"),
            "资金和价格开始共振，疑似进入启动阶段" if item.get("breakout") else "资金开始异动，进入观察状态",
        ]
        return "\n".join(lines)

    def _prune_launch_state(self, state: dict[str, Any], now_ts: int) -> None:
        for symbol, record in list(state.items()):
            if not isinstance(record, dict):
                del state[symbol]
                continue
            last_seen = int(record.get("last_seen", 0) or 0)
            if last_seen <= 0:
                del state[symbol]
                continue
            stage = str(record.get("stage") or "")
            anchor = int(record.get("failed_at", last_seen) or last_seen) if stage == "failed" else last_seen
            age = now_ts - anchor
            ttl = self.settings.launch_failed_ttl_sec if stage == "failed" else self.settings.launch_state_ttl_sec
            if ttl > 0 and age > ttl:
                del state[symbol]

    @staticmethod
    def _launch_watch_record(item: dict[str, Any], now_ts: int) -> dict[str, Any]:
        def optional_round(value: Any, digits: int = 4) -> float | None:
            if value is None or value == "":
                return None
            try:
                return round(float(value), digits)
            except (TypeError, ValueError):
                return None

        launch_phase = (
            item.get("launch_phase")
            if isinstance(item.get("launch_phase"), Mapping)
            else {}
        )
        directional = (
            item.get("directional_readiness")
            if isinstance(item.get("directional_readiness"), Mapping)
            else {}
        )
        bullish_evidence = directional.get("bullish_evidence_score")
        if bullish_evidence is None:
            bullish_evidence = directional.get("bullish_readiness")
        bearish_evidence = directional.get("bearish_evidence_score")
        if bearish_evidence is None:
            bearish_evidence = directional.get("bearish_readiness")

        return {
            "ts": now_ts,
            "symbol": item["symbol"],
            "coin": item["coin"],
            "score": item["score"],
            "discovery_score": LaunchWarningRadar._discovery_score(item),
            "score_contract_version": 2,
            "evidence_score": LaunchWarningRadar._optional_evidence_score(item),
            "bullish_evidence_score": optional_round(bullish_evidence, 2),
            "bearish_evidence_score": optional_round(bearish_evidence, 2),
            "closed_price": round(to_float(item.get("closed_price")), 12),
            "closed_oi_usd": round(to_float(item.get("closed_oi_usd")), 2),
            "closed_quote_volume": round(to_float(item.get("closed_quote_volume")), 2),
            "price_15m": round(item["price_15m"], 4),
            "price_1h": round(item["price_1h"], 4),
            "price_4h": optional_round(item.get("price_4h")),
            "price_24h": optional_round(item.get("price_24h")),
            "price_24h_semantics": str(
                item.get("price_24h_semantics")
                or "rolling_24h_not_closed_window"
            ),
            "oi_15m": round(item["oi_15m"], 4),
            "oi_1h": round(item["oi_1h"], 4),
            "oi_4h": optional_round(item.get("oi_4h")),
            "oi_24h": optional_round(item.get("oi_24h")),
            "oi_24h_status": str(item.get("oi_24h_status") or ""),
            "oi_24h_semantics": str(item.get("oi_24h_semantics") or ""),
            "data_quality_status": str(item.get("data_quality_status") or "not_checked"),
            "data_quality_score": round(to_float(item.get("data_quality_score")), 2),
            "quality_gate": str(item.get("quality_gate") or "degraded"),
            "primary_data_source": str(item.get("primary_data_source") or "binance"),
            "volume_ratio": round(item["volume_ratio"], 4),
            "breakout": bool(item["breakout"]),
            "quote_volume": round(item["quote_volume"], 2),
            "mcap": round(to_float(item.get("mcap")), 2),
            "mcap_source": str(item.get("mcap_source") or ""),
            "market_cap_tier": market_cap_tier(to_float(item.get("mcap"))),
            "liquidity_tier": liquidity_tier(to_float(item.get("quote_volume"))),
            "instrument_type": str(item.get("instrument_type") or ""),
            "asset_family": str(item.get("asset_family") or ""),
            "asset_class": str(item.get("asset_class") or ""),
            "asset_subclass": str(item.get("asset_subclass") or ""),
            "asset_category_label": str(item.get("asset_category_label") or "未分类"),
            "asset_category_source": str(item.get("asset_category_source") or ""),
            "funding_available": bool(item.get("funding_available")),
            "funding_pct": round(to_float(item.get("funding_pct")), 6),
            "basis_pct": optional_round(item.get("basis_pct"), 6),
            "funding_interval_hours": int(to_float(item.get("funding_interval_hours"))),
            "funding_interval_transition": str(item.get("funding_interval_transition") or ""),
            "reasons": item.get("reasons", []),
            "raw_rule_score": int(
                item.get("raw_rule_score")
                if item.get("raw_rule_score") is not None
                else item.get("score") or 0
            ),
            "score_semantics": str(
                item.get("score_semantics") or ""
            ),
            "evidence_strength": str(
                item.get("evidence_strength") or ""
            ),
            "trigger_path": str(item.get("trigger_path") or ""),
            "policy_block_reason": str(
                item.get("policy_block_reason") or ""
            ),
            "price_oi_quadrant": str(
                item.get("price_oi_quadrant") or ""
            ),
            "supporting_evidence": list(
                item.get("supporting_evidence") or []
            ),
            "counter_evidence": list(item.get("counter_evidence") or []),
            "lifecycle_stage": str(
                (
                    item.get("launch_lifecycle")
                    if isinstance(item.get("launch_lifecycle"), dict)
                    else {}
                ).get("current_stage")
                or ""
            ),
            "spot_active_net_usd": optional_round(
                item.get("spot_active_net_usd"),
                2,
            ),
            "futures_active_net_usd": optional_round(
                item.get("futures_active_net_usd"),
                2,
            ),
            "spot_active_ratio": optional_round(
                item.get("spot_active_ratio"),
                6,
            ),
            "futures_active_ratio": optional_round(
                item.get("futures_active_ratio"),
                6,
            ),
            "spot_active_status": str(item.get("spot_active_status") or ""),
            "futures_active_status": str(
                item.get("futures_active_status") or ""
            ),
            "directional_analysis_status": str(
                item.get("directional_analysis_status") or "disabled"
            ),
            "directional_status": str(
                directional.get("status") or ""
            ),
            "directional_direction": str(
                directional.get("direction") or ""
            ),
            "bullish_readiness": optional_round(
                directional.get("bullish_readiness"),
                2,
            ),
            "bearish_readiness": optional_round(
                directional.get("bearish_readiness"),
                2,
            ),
            "timing_stage": str(
                launch_phase.get("timing_stage") or ""
            ),
            "execution_status": str(
                launch_phase.get("execution_status") or ""
            ),
            "position_status": str(
                launch_phase.get("position_status") or ""
            ),
            "primary_block_reason": str(
                launch_phase.get("primary_block_reason") or ""
            ),
        }

    def _launch_history_record(
        self,
        watchlist: list[dict[str, Any]],
        alerts: list[dict[str, Any]],
        now_ts: int,
    ) -> dict[str, Any]:
        sorted_items = sorted(
            watchlist,
            key=lambda item: (
                LaunchWarningRadar._optional_evidence_score(item) is not None,
                LaunchWarningRadar._optional_evidence_score(item) or 0,
                LaunchWarningRadar._discovery_score(item),
            ),
            reverse=True,
        )
        valid_evidence_scores = [
            score
            for item in watchlist
            if (score := LaunchWarningRadar._optional_evidence_score(item))
            is not None
        ]
        buckets = {"idle": 0, "watching": 0, "primed": 0, "breakout": 0, "launched": 0}
        for item in sorted_items:
            stage = str(item.get("lifecycle_stage") or "")
            if stage not in buckets:
                evidence = LaunchWarningRadar._optional_evidence_score(item)
                stage = self._launch_stage(evidence) if evidence is not None else "idle"
            buckets[stage] = buckets.get(stage, 0) + 1
        return {
            "ts": now_ts,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "scanned": len(watchlist),
            "alert_count": len(alerts),
            "score_contract_version": 2,
            "top_score": max(valid_evidence_scores, default=0),
            "top_evidence_score": max(valid_evidence_scores, default=None),
            "top_discovery_score": max(
                (int(item.get("discovery_score") or 0) for item in watchlist),
                default=0,
            ),
            "buckets": buckets,
            "top_symbols": [item["symbol"] for item in sorted_items[:8]],
            "items": sorted_items[:10],
        }
