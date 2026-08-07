from __future__ import annotations

import json
from typing import Any, Iterable, Mapping

from radars.common import tg_escape


TITLE = "🔎【山寨合约异动雷达｜监控池更新】"


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in {float("inf"), float("-inf")} else None


def _money(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "市值 缺失"
    if number >= 1_000_000_000:
        return f"市值 ${number / 1_000_000_000:.2f}B"
    if number >= 1_000_000:
        return f"市值 ${number / 1_000_000:.2f}M"
    if number >= 1_000:
        return f"市值 ${number / 1_000:.2f}K"
    return f"市值 ${number:.2f}"


def _ratio(value: Any) -> str:
    number = _number(value)
    return f"OI/市值 {number * 100:.1f}%" if number is not None else "OI/市值 缺失"


def _funding(value: Any) -> str:
    number = _number(value)
    return f"费率 {number * 100:.4f}%" if number is not None else "费率 缺失"


def candidate_line(snapshot: Mapping[str, Any], *, include_labels: bool = False) -> str:
    symbol = str(snapshot.get("symbol") or "未知币种")
    parts = [symbol]
    if include_labels:
        labels: list[str] = []
        tags = set(snapshot.get("candidate_tags") or [])
        if "short_squeeze_candidate" in tags:
            labels.append("潜在逼空")
        if "high_leverage_candidate" in tags:
            labels.append("潜在狗庄候选")
        if labels:
            parts.append(" + ".join(labels))
    parts.extend([
        _money(snapshot.get("market_cap_usd")),
        _ratio(snapshot.get("binance_oi_market_cap_ratio")),
        _funding(snapshot.get("funding_rate")),
    ])
    return "｜".join(parts)


def _snapshot_map(pool: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    return {
        str(item.get("symbol") or ""): item
        for item in pool.get("snapshots", [])
        if isinstance(item, Mapping) and item.get("symbol")
    }


def _source_text(pool: Mapping[str, Any]) -> str:
    sources = pool.get("data_sources") if isinstance(pool.get("data_sources"), Mapping) else {}
    cmc = str(sources.get("market_cap") or "CoinMarketCap官方API")
    oi = str(sources.get("open_interest") or "Binance USDⓈ-M Futures")
    premium = str(
        sources.get("funding_and_mark_price")
        or "binance_premium_index"
    )
    return f"市值={cmc}；OI={oi}；标记价/费率={premium}"


def render_console(pool: Mapping[str, Any]) -> str:
    universe = pool.get("universe") if isinstance(pool.get("universe"), Mapping) else {}
    mappings = pool.get("mapping_stats") if isinstance(pool.get("mapping_stats"), Mapping) else {}
    stats = pool.get("stats") if isinstance(pool.get("stats"), Mapping) else {}
    diagnostics = pool.get("diagnostics") if isinstance(pool.get("diagnostics"), Mapping) else {}
    snapshots = _snapshot_map(pool)
    network = str(diagnostics.get("network_status") or "未知")
    lines = [
        "山寨合约异动雷达｜候选池扫描",
        f"启动时间：{pool.get('generated_at') or '未知'}",
        f"网络与数据源：{network}｜{_source_text(pool)}",
        f"已加载USDT永续：{int(universe.get('loaded_usdt_perpetuals') or 0)}",
        f"山寨范围：{int(universe.get('eligible_altcoin_contracts') or 0)}｜排除：{int(universe.get('excluded_contracts') or 0)}",
        (
            "CMC映射："
            f"可信 {int(mappings.get('trusted_count') or 0)}｜"
            f"诊断 {int(mappings.get('diagnostic_count') or 0)}｜"
            f"冲突 {int(mappings.get('conflict_count') or 0)}｜"
            f"未映射 {int(mappings.get('unmapped_count') or 0)}｜"
            f"未进入可信池 {int(mappings.get('not_formally_mapped_count') or 0)}"
        ),
        (
            "候选统计："
            f"潜在逼空 {int(stats.get('short_squeeze_count') or 0)}｜"
            f"潜在狗庄候选 {int(stats.get('high_leverage_count') or 0)}｜"
            f"双重命中 {int(stats.get('dual_match_count') or 0)}｜"
            f"合并监控 {int(stats.get('merged_candidate_count') or 0)}"
        ),
        (
            "候选池差异："
            f"新增 {', '.join(pool.get('delta', {}).get('added', [])) or '无'}｜"
            f"保留 {', '.join(pool.get('delta', {}).get('retained', [])) or '无'}｜"
            f"移除 {', '.join(pool.get('delta', {}).get('removed', [])) or '无'}"
        ),
        f"候选池哈希：{str(pool.get('candidate_pool_hash') or '未知')}",
        "",
        "潜在逼空：",
    ]
    short_symbols = pool.get("short_squeeze_symbols") or []
    lines.extend(
        candidate_line(snapshots[symbol]) for symbol in short_symbols if symbol in snapshots
    )
    if not short_symbols:
        lines.append("无")
    lines.extend(["", "潜在狗庄候选（仅表示合约杠杆异常，不构成操纵事实认定）："])
    leverage_symbols = pool.get("high_leverage_symbols") or []
    lines.extend(
        candidate_line(snapshots[symbol]) for symbol in leverage_symbols if symbol in snapshots
    )
    if not leverage_symbols:
        lines.append("无")
    lines.extend(["", "双重命中："])
    dual_symbols = pool.get("dual_match_symbols") or []
    lines.extend(
        candidate_line(snapshots[symbol], include_labels=True)
        for symbol in dual_symbols if symbol in snapshots
    )
    if not dual_symbols:
        lines.append("无")
    reason_counts = mappings.get("reason_counts") if isinstance(mappings.get("reason_counts"), Mapping) else {}
    if reason_counts:
        lines.extend(["", "未进入正式池原因：" + "｜".join(
            f"{key}={reason_counts[key]}" for key in sorted(reason_counts)
        )])
    return "\n".join(lines).rstrip()


def _telegram_body_lines(pool: Mapping[str, Any]) -> list[str]:
    universe = pool.get("universe") if isinstance(pool.get("universe"), Mapping) else {}
    mappings = pool.get("mapping_stats") if isinstance(pool.get("mapping_stats"), Mapping) else {}
    stats = pool.get("stats") if isinstance(pool.get("stats"), Mapping) else {}
    snapshots = _snapshot_map(pool)
    complete = sum(
        item.get("data_quality") == "complete"
        for item in snapshots.values()
    )
    total = len(snapshots)
    completeness = f"{complete}/{total}" if total else "0/0"
    lines = [
        f"更新时间：{pool.get('generated_at') or '未知'}",
        "合约范围：Binance 当前交易中的 USDT 永续山寨合约",
        f"已加载合约数：{int(universe.get('loaded_usdt_perpetuals') or 0)}",
        f"可信市值映射数：{int(mappings.get('trusted_count') or 0)}",
        (
            f"未映射数（未进入可信池）：{int(mappings.get('not_formally_mapped_count') or 0)}"
            f"（诊断 {int(mappings.get('diagnostic_count') or 0)}；"
            f"冲突 {int(mappings.get('conflict_count') or 0)}；"
            f"未映射 {int(mappings.get('unmapped_count') or 0)}）"
        ),
        f"合并监控数量：{int(stats.get('merged_candidate_count') or 0)}",
        f"本轮新增：{', '.join(pool.get('delta', {}).get('added', [])) or '无'}",
        f"本轮移除：{', '.join(pool.get('delta', {}).get('removed', [])) or '无'}",
        f"数据源：{_source_text(pool)}",
        "实时监听状态：未启动（P1 单次 Dry-run）",
        f"数据完整度：{completeness}",
        "",
        f"潜在逼空（{int(stats.get('short_squeeze_count') or 0)}）：",
    ]
    short_symbols = pool.get("short_squeeze_symbols") or []
    lines.extend(candidate_line(snapshots[symbol]) for symbol in short_symbols if symbol in snapshots)
    if not short_symbols:
        lines.append("无")
    lines.extend([
        "",
        f"潜在狗庄候选（{int(stats.get('high_leverage_count') or 0)}）：",
        "说明：仅表示合约杠杆异常，不构成对操纵主体的事实认定。",
    ])
    leverage_symbols = pool.get("high_leverage_symbols") or []
    lines.extend(candidate_line(snapshots[symbol]) for symbol in leverage_symbols if symbol in snapshots)
    if not leverage_symbols:
        lines.append("无")
    lines.extend(["", f"双重命中（{int(stats.get('dual_match_count') or 0)}）："])
    dual_symbols = pool.get("dual_match_symbols") or []
    lines.extend(
        candidate_line(snapshots[symbol], include_labels=True)
        for symbol in dual_symbols if symbol in snapshots
    )
    if not dual_symbols:
        lines.append("无")
    return [tg_escape(line) for line in lines]


def paginate_telegram_lines(
    body_lines: Iterable[str],
    *,
    max_chars: int,
    title: str = TITLE,
) -> list[str]:
    limit = int(max_chars)
    if limit < 256:
        raise ValueError("telegram preview page limit is too small")
    title_line = tg_escape(title)
    raw_lines = list(body_lines)
    pages: list[list[str]] = []
    current: list[str] = []
    reserve = len(title_line) + len("\n第999/999页\n")
    for line in raw_lines:
        candidate = current + [line]
        if current and len("\n".join(candidate)) + reserve > limit:
            pages.append(current)
            current = [line]
        else:
            current = candidate
        if len("\n".join(current)) + reserve > limit:
            raise ValueError("one Telegram preview line exceeds page limit")
    pages.append(current)
    total = len(pages)
    rendered = [
        "\n".join([title_line, f"第{index}/{total}页", *page])
        for index, page in enumerate(pages, start=1)
    ]
    if any(len(page) > limit for page in rendered):
        raise ValueError("Telegram preview pagination invariant failed")
    return rendered


def render_telegram_preview(pool: Mapping[str, Any], *, max_chars: int) -> list[str]:
    return paginate_telegram_lines(
        _telegram_body_lines(pool),
        max_chars=max_chars,
    )


def render_json(pool: Mapping[str, Any], *, telegram_pages: list[str] | None = None) -> str:
    payload = dict(pool)
    if telegram_pages is not None:
        payload["telegram_preview_pages"] = telegram_pages
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)


__all__ = [
    "TITLE",
    "candidate_line",
    "paginate_telegram_lines",
    "render_console",
    "render_json",
    "render_telegram_preview",
]
