"""
2小时持仓价格背离分析雷达（单文件版） DivergenceRadar
========================================

功能：
- 固定每 2 小时闭合窗口推送一次「持仓 vs 价格」背离分析；
- 背离度 = 持仓变化% - 价格变化%；
- 分类输出：🏗 庄家建仓 / 💰 恐慌抛售 / 🚀 多头共振 / ⚠️ 极端背离；
- 全市场扫描（最低成交额过滤），不依赖轮换池，避免漏掉 2 小时内的异动。

独立运行（在项目根目录执行）：
  python -m radars.pulse.divergence --once                   # 跑一轮，默认 dry-run
  python -m radars.pulse.divergence --once --send --confirm-real-send
生产主入口：python main.py pulse / loop / live

运行逻辑：
1. 读取 config/.env.oi（TG_BOT_TOKEN / TG_CHAT_ID / DIVERGENCE_* 配置）；
2. 一次 24h 行情接口全市场初筛（USDT 永续、排除稳定币与名单、成交额过滤）；
3. 逐币拉 5m K线与持仓历史，计算 2 小时价格/持仓变化；
4. 分类、排序、组装模板，用 shared/telegram.py 推送（dry-run 双门禁）；
5. 循环模式按 2 小时闭合窗口调度。

合并方式：
  from radars.pulse.divergence import run_once
  run_once(settings, gateway, send=..., confirm_real_send=...)
"""

from __future__ import annotations

import argparse
import html
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.settings import Settings  # noqa: E402
from shared.asset_classification import is_stable_crypto_asset  # noqa: E402
from shared.binance_data import BinanceDataSource  # noqa: E402
from shared.storage import JsonStore  # noqa: E402
from shared.telegram import TelegramGateway  # noqa: E402
from shared.time_windows import closed_window  # noqa: E402

TEMPLATE_ID = "TG_LAUNCH_ALERT"
_SERIES_POINTS = 25  # 5m x 25 根 = 2 小时窗口（24 个间隔）
_5M_MS = 5 * 60 * 1000

CATEGORIES = {
    "pressure": "📈 回调压力信号（持仓涨多价格涨少）",
    "build": "🏗 庄家建仓信号（持仓暴涨价格不动）",
    "panic": "💰 恐慌抛售信号（持仓价格双杀）",
    "extreme": "⚠️ 极端背离信号（异常剧烈波动）",
    "breakout": "📉 强势突破信号（持仓减少价格暴涨）",
    "resonance": "🚀 多头共振信号（持仓价格双升）",
}

SIGNAL_DIRECTIONS = {
    "build": "long",
    "pressure": "short",
    "breakout": "long",
    "panic": "short",
    "resonance": "long",
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class DivergenceConfig:
    interval_sec: int = 2 * 3600
    close_delay_sec: int = 30
    scan_limit: int = 200
    min_quote_volume_usd: float = 5_000_000.0
    top_n: int = 8
    extreme_abs: float = 10.0
    extreme_top_n: int = 5
    build_oi_min: float = 5.0
    build_price_max: float = 1.0
    pressure_oi_min: float = 5.0
    pressure_price_min: float = 1.0
    pressure_price_max: float = 3.0
    resonance_oi_min: float = 5.0
    resonance_price_min: float = 3.0
    panic_oi_max: float = -5.0
    panic_price_max: float = -3.0
    breakout_oi_max: float = -5.0
    breakout_price_min: float = 3.0
    loop_interval_sec: int = 60

    @classmethod
    def from_env(cls, settings: Settings) -> "DivergenceConfig":
        return cls(
            interval_sec=max(3600, _env_int("DIVERGENCE_INTERVAL_SEC", 2 * 3600)),
            close_delay_sec=max(0, _env_int("DIVERGENCE_CLOSE_DELAY_SEC", 30)),
            scan_limit=max(1, _env_int("DIVERGENCE_SCAN_LIMIT", 200)),
            min_quote_volume_usd=max(
                0.0, _env_float("DIVERGENCE_MIN_QUOTE_VOLUME", 5_000_000.0)
            ),
            top_n=max(1, _env_int("DIVERGENCE_TOP_N", 8)),
            extreme_abs=max(1.0, _env_float("DIVERGENCE_EXTREME_ABS", 10.0)),
            extreme_top_n=max(1, _env_int("DIVERGENCE_EXTREME_TOP_N", 5)),
            build_oi_min=_env_float("DIVERGENCE_BUILD_OI_MIN", 5.0),
            build_price_max=_env_float("DIVERGENCE_BUILD_PRICE_MAX", 1.0),
            panic_oi_max=_env_float("DIVERGENCE_PANIC_OI_MAX", -5.0),
            panic_price_max=_env_float("DIVERGENCE_PANIC_PRICE_MAX", -3.0),
            resonance_oi_min=_env_float("DIVERGENCE_RESONANCE_OI_MIN", 5.0),
            resonance_price_min=_env_float("DIVERGENCE_RESONANCE_PRICE_MIN", 3.0),
            pressure_oi_min=_env_float("DIVERGENCE_PRESSURE_OI_MIN", 5.0),
            pressure_price_min=_env_float("DIVERGENCE_PRESSURE_PRICE_MIN", 1.0),
            pressure_price_max=_env_float("DIVERGENCE_PRESSURE_PRICE_MAX", 3.0),
            breakout_oi_max=_env_float("DIVERGENCE_BREAKOUT_OI_MAX", -5.0),
            breakout_price_min=_env_float("DIVERGENCE_BREAKOUT_PRICE_MIN", 3.0),
            loop_interval_sec=max(30, _env_int("DIVERGENCE_LOOP_INTERVAL_SEC", 60)),
        )


def to_float(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def pct(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return (current - previous) / previous * 100.0


def classify(
    oi_pct: float,
    price_pct: float,
    divergence: float,
    cfg: DivergenceConfig,
) -> Optional[str]:
    if oi_pct >= cfg.build_oi_min and abs(price_pct) < cfg.build_price_max:
        return "build"
    if (
        oi_pct >= cfg.pressure_oi_min
        and cfg.pressure_price_min <= price_pct < cfg.pressure_price_max
    ):
        return "pressure"
    if oi_pct >= cfg.resonance_oi_min and price_pct >= cfg.resonance_price_min:
        return "resonance"
    if oi_pct <= cfg.panic_oi_max and price_pct <= cfg.panic_price_max:
        return "panic"
    if oi_pct <= cfg.breakout_oi_max and price_pct >= cfg.breakout_price_min:
        return "breakout"
    if abs(divergence) >= cfg.extreme_abs:
        return "extreme"
    return None


def _close_series(rows: list[Any]) -> list[float]:
    values: list[float] = []
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, (list, tuple)) and len(row) >= 5:
            value = to_float(row[4])
            if value > 0:
                values.append(value)
    return values


def _oi_value_series(rows: list[Any]) -> list[float]:
    values: list[float] = []
    for row in sorted(
        (row for row in rows if isinstance(row, Mapping)),
        key=lambda row: int(to_float(row.get("timestamp"))),
    ):
        value = to_float(row.get("sumOpenInterestValue"))
        if value > 0:
            values.append(value)
    return values


def analyze_symbol(
    source: BinanceDataSource,
    symbol: str,
    window_end_ms: int,
) -> Optional[dict[str, Any]]:
    start_ms = max(0, window_end_ms - _SERIES_POINTS * _5M_MS)
    try:
        klines = source.klines(
            symbol, interval="5m", limit=_SERIES_POINTS,
            start_time=start_ms, end_time=window_end_ms - 1,
        )
        oi_rows = source.open_interest_hist(
            symbol, period="5m", limit=_SERIES_POINTS, end_time=window_end_ms,
        )
    except Exception:
        return None
    closes = _close_series(klines)
    oi_values = _oi_value_series(oi_rows)
    if len(closes) < _SERIES_POINTS or len(oi_values) < _SERIES_POINTS:
        return None
    price_pct = pct(closes[-1], closes[-_SERIES_POINTS])
    oi_pct = pct(oi_values[-1], oi_values[-_SERIES_POINTS])
    divergence = oi_pct - price_pct
    return {
        "symbol": symbol,
        "coin": symbol.replace("USDT", ""),
        "price": closes[-1],
        "price_pct": price_pct,
        "oi_pct": oi_pct,
        "divergence": divergence,
    }

# ---------- 扫描与分类 ----------

def build_analysis(
    source: BinanceDataSource,
    cfg: DivergenceConfig,
    settings: Settings,
    window: Any,
) -> dict[str, Any]:
    excluded = {str(item).upper() for item in settings.excluded_base_assets}
    tickers = source.ticker_24h() or []
    candidates: list[tuple[float, str]] = []
    for ticker in tickers:
        if not isinstance(ticker, Mapping):
            continue
        symbol = str(ticker.get("symbol") or "").strip().upper()
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if is_stable_crypto_asset(base) or base in excluded:
            continue
        quote_volume = to_float(ticker.get("quoteVolume"))
        if quote_volume < cfg.min_quote_volume_usd:
            continue
        candidates.append((quote_volume, symbol))
    candidates.sort(key=lambda row: (-row[0], row[1]))
    candidates = candidates[: cfg.scan_limit]

    items: list[dict[str, Any]] = []
    symbol_list = [symbol for _volume, symbol in candidates]
    workers = min(8, max(1, len(symbol_list)))

    def analyze_slice(slice_symbols: list[str]) -> list[dict[str, Any]]:
        local = BinanceDataSource(settings)
        try:
            classified: list[dict[str, Any]] = []
            for symbol in slice_symbols:
                item = analyze_symbol(local, symbol, window.end_ms)
                if item is None:
                    continue
                category = classify(
                    item["oi_pct"],
                    item["price_pct"],
                    item["divergence"],
                    cfg,
                )
                if category is not None:
                    item["category"] = category
                    classified.append(item)
            return classified
        finally:
            local.close()

    chunk_size = max(1, (len(symbol_list) + workers - 1) // workers)
    slices = [
        symbol_list[offset:offset + chunk_size]
        for offset in range(0, len(symbol_list), chunk_size)
    ]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for partial in pool.map(analyze_slice, slices):
            items.extend(partial)

    pressure = sorted(
        [item for item in items if item["category"] == "pressure"],
        key=lambda item: item["divergence"],
        reverse=True,
    )[: cfg.top_n]
    build = sorted(
        [item for item in items if item["category"] == "build"],
        key=lambda item: item["oi_pct"],
        reverse=True,
    )[: cfg.top_n]
    breakout = sorted(
        [item for item in items if item["category"] == "breakout"],
        key=lambda item: item["price_pct"],
        reverse=True,
    )[: cfg.top_n]
    panic = sorted(
        [item for item in items if item["category"] == "panic"],
        key=lambda item: item["oi_pct"],
    )[: cfg.top_n]
    resonance = sorted(
        [item for item in items if item["category"] == "resonance"],
        key=lambda item: item["oi_pct"],
        reverse=True,
    )[: cfg.top_n]
    extreme = sorted(
        [item for item in items if item["category"] == "extreme"],
        key=lambda item: abs(item["divergence"]),
        reverse=True,
    )[: cfg.extreme_top_n]

    return {
        "build": build,
        "pressure": pressure,
        "breakout": breakout,
        "panic": panic,
        "resonance": resonance,
        "extreme": extreme,
        "scanned": len(candidates),
        "analyzed": len(items),
    }


def _row(idx: int, item: dict[str, Any]) -> str:
    coin = str(item["coin"])
    oi = item["oi_pct"]
    price = item["price_pct"]
    divergence = item["divergence"]
    return (
        f"{idx:>2}. {coin:<12} 持仓{oi:+6.2f}% | "
        f"价格 {price:+6.2f}% | 背离{divergence:+6.1f}"
    )


def _rows(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["暂无"]
    return [_row(idx, item) for idx, item in enumerate(items, start=1)]


def format_card(
    analysis: dict[str, Any],
    cfg: DivergenceConfig,
    window: Any,
    sample_label: str = "",
) -> str:
    divider = "\u2501" * 20
    time_text = window.end.strftime("%m-%d %H:%M")
    lines = [
        f"🤖 自动推送{sample_label} - {time_text}",
        "",
        "⚖️ 2小时持仓价格背离分析结果",
        divider,
        "",
    ]
    for key in ("build", "pressure", "breakout", "panic", "resonance", "extreme"):
        lines.append(CATEGORIES[key])
        lines.append("--------------------------------------")
        lines.extend(_rows(analysis[key]))
        lines.append("")
    lines.extend([
        divider,
        "📊 背离信号解读:",
        "🏗 庄家建仓 → 主力提前布局，价格待启动",
        "📈 回调压力 → 持仓过热，短期可能回调",
        "📉 强势突破 → 散户离场，趋势或加速",
        "💰 恐慌抛售 → 双杀下跌，谨慎抄底",
        "🚀 多头共振 → 资金价格齐涨，趋势强劲",
        "⚠️ 极端背离 → 异常信号，重点关注",
        "",
        "💡 背离度 = 持仓变化% - 价格变化%",
        "   正值越大 → 持仓领先价格，可能回调",
        "   负值越小 → 价格领先持仓，可能加速",
        "🟡 数据来源: 币安 Binance",
    ])
    return "<pre>" + "\n".join(lines) + "</pre>"


def run_cycle(
    settings: Settings,
    gateway: TelegramGateway,
    cfg: DivergenceConfig,
    *,
    send: bool,
    confirm_real_send: bool,
    scan_limit: int | None = None,
) -> dict[str, Any]:
    window = closed_window(interval_sec=cfg.interval_sec, delay_sec=cfg.close_delay_sec)
    source = BinanceDataSource(settings)
    diagnostics: dict[str, Any] = {}
    try:
        effective = DivergenceConfig(**{
            **vars(cfg),
            "scan_limit": max(1, scan_limit or cfg.scan_limit),
        })
        analysis = build_analysis(source, effective, settings, window)
        text = format_card(analysis, effective, window)
        dedup_key = f"divergence:{window.end.strftime('%Y%m%d%H%M')}"
        signal_records = [
            {
                "symbol": item.get("symbol") or "",
                "stage": category,
                "category": category,
                "signal_direction": SIGNAL_DIRECTIONS.get(category, ""),
                "evaluation_eligible": category in SIGNAL_DIRECTIONS,
                "price": item.get("price") or 0.0,
                "price_pct": item.get("price_pct") or 0.0,
                "oi_change_pct": item.get("oi_pct") or 0.0,
                "divergence": item.get("divergence") or 0.0,
                "window_sec": effective.interval_sec,
                "quality_gate": "allow",
                "primary_data_source": "binance_native",
            }
            for category in (
                "build", "pressure", "breakout", "panic", "resonance", "extreme",
            )
            for item in analysis.get(category) or []
        ]
        result = gateway.send(
            text,
            TEMPLATE_ID,
            dedup_key,
            send=send,
            confirm_real_send=confirm_real_send,
            cooldown_sec=effective.interval_sec,
            daily_limit=None,
            parse_mode="HTML",
            signal_records=signal_records,
            enrich_market_context=False,
        )
        if not result.sent and result.message_ids:
            gateway.delete_messages_detailed(
                list(result.message_ids),
                reason="pulse_partial_send_rollback",
            )
        review_items: list[dict[str, Any]] = []
        if result.sent and result.message_ids:
            message_id = result.message_ids[0]
            for category in (
                "build", "pressure", "breakout", "panic", "resonance", "extreme",
            ):
                for item in analysis.get(category) or []:
                    review_items.append({
                        "radar": "divergence",
                        "template": category,
                        "symbol": item.get("symbol") or "",
                        "price": item.get("price") or 0.0,
                        "oi_pct": item.get("oi_pct") or 0.0,
                        "price_pct": item.get("price_pct") or 0.0,
                        "divergence": item.get("divergence") or 0.0,
                        "message_id": message_id,
                    })
        if review_items:
            try:
                from radars.pulse.review_store import record_signals
                record_signals(settings, review_items)
            except Exception as exc:
                print(f"[review] record failed {type(exc).__name__}", file=sys.stderr)
        diagnostics = {
            "window_end": window.end.strftime("%Y-%m-%d %H:%M:%S"),
            "scanned": analysis["scanned"],
            "analyzed": analysis["analyzed"],
            "build": len(analysis["build"]),
            "pressure": len(analysis["pressure"]),
            "breakout": len(analysis["breakout"]),
            "panic": len(analysis["panic"]),
            "resonance": len(analysis["resonance"]),
            "extreme": len(analysis["extreme"]),
            "review": len(review_items),
            "push": {"status": result.status, "reason": result.reason},
            "source": source.diagnostics(),
        }
    finally:
        source.close()
    print(json.dumps(diagnostics, ensure_ascii=False, indent=2))
    return diagnostics


def run_once(
    settings: Settings | None = None,
    gateway: TelegramGateway | None = None,
    *,
    send: bool = False,
    confirm_real_send: bool = False,
    scan_limit: int | None = None,
    cfg: DivergenceConfig | None = None,
) -> dict[str, Any]:
    settings = settings or Settings.load()
    cfg = cfg or DivergenceConfig.from_env(settings)
    gateway = gateway or TelegramGateway(settings, JsonStore(settings.data_dir))
    return run_cycle(
        settings, gateway, cfg,
        send=send, confirm_real_send=confirm_real_send, scan_limit=scan_limit,
    )


def _run_loop(settings: Settings, gateway: TelegramGateway, cfg: DivergenceConfig, args: argparse.Namespace) -> int:
    last_window_ms = 0
    try:
        while True:
            window = closed_window(interval_sec=cfg.interval_sec, delay_sec=cfg.close_delay_sec)
            if window.end_ms != last_window_ms:
                run_cycle(
                    settings, gateway, cfg,
                    send=args.send,
                    confirm_real_send=args.confirm_real_send,
                    scan_limit=args.scan_limit,
                )
                last_window_ms = window.end_ms
            time.sleep(max(10, cfg.loop_interval_sec))
    except KeyboardInterrupt:
        print("\n[divergence-radar] 已停止")
    return 0


def _send_test_push(
    settings: Settings,
    gateway: TelegramGateway,
    send: bool,
    confirm_real_send: bool,
) -> int:
    sample = {
        "build": [
            {"coin": "ICX", "oi_pct": 27.92, "price_pct": 0.61, "divergence": 27.3},
            {"coin": "SOPH", "oi_pct": 19.69, "price_pct": -4.17, "divergence": 23.9},
            {"coin": "INX", "oi_pct": 3.44, "price_pct": 13.96, "divergence": -10.5},
        ],
        "breakout": [
            {"coin": "LUNA2", "oi_pct": -0.29, "price_pct": 15.59, "divergence": -15.9},
        ],
        "pressure": [
            {"coin": "VELVET", "oi_pct": 8.79, "price_pct": 1.36, "divergence": 7.4},
            {"coin": "PLTR", "oi_pct": 7.59, "price_pct": 1.60, "divergence": 6.0},
        ],
        "panic": [
            {"coin": "MAV", "oi_pct": -12.48, "price_pct": -4.82, "divergence": -7.7},
            {"coin": "SCRT", "oi_pct": -10.13, "price_pct": -9.07, "divergence": -1.1},
        ],
        "resonance": [
            {"coin": "LUNA2", "oi_pct": -0.29, "price_pct": 15.59, "divergence": -15.9},
            {"coin": "ACU", "oi_pct": 16.37, "price_pct": 9.67, "divergence": 6.7},
        ],
        "extreme": [
            {"coin": "INX", "oi_pct": 3.44, "price_pct": 13.96, "divergence": -10.5},
            {"coin": "LUNA2", "oi_pct": -0.29, "price_pct": 15.59, "divergence": -15.9},
        ],
        "scanned": 120,
        "analyzed": 120,
    }
    window = closed_window(interval_sec=7200, delay_sec=300)
    text = format_card(sample, DivergenceConfig.from_env(settings), window, sample_label="（实例）")
    result = gateway.send(
        text,
        TEMPLATE_ID,
        f"divergence:test-push:{int(time.time())}",
        send=send,
        confirm_real_send=confirm_real_send,
        cooldown_sec=0,
        daily_limit=None,
        parse_mode="HTML",
    )
    print(f"测试推送状态: {result.status} ({result.reason})")
    return 0 if result.sent else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="2小时持仓价格背离分析雷达（单文件版）")
    parser.add_argument("--once", action="store_true", help="只跑一轮（默认）")
    parser.add_argument("--loop", action="store_true", help="常驻循环")
    parser.add_argument("--send", action="store_true", help="允许真实发送，仍需 --confirm-real-send")
    parser.add_argument("--confirm-real-send", action="store_true", help="确认真实发送")
    parser.add_argument("--scan-limit", type=int, default=None, help="本轮扫描币种上限")
    parser.add_argument("--test-push", action="store_true", help="推送内置示例卡片，验证链路")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    cfg = DivergenceConfig.from_env(settings)
    gateway = TelegramGateway(settings, JsonStore(settings.data_dir))
    if args.test_push:
        return _send_test_push(settings, gateway, args.send, args.confirm_real_send)
    if args.loop:
        return _run_loop(settings, gateway, cfg, args)
    run_once(
        settings, gateway,
        send=args.send,
        confirm_real_send=args.confirm_real_send,
        scan_limit=args.scan_limit,
        cfg=cfg,
    )
    return 0


__all__ = [
    "DivergenceConfig",
    "SIGNAL_DIRECTIONS",
    "classify",
    "build_analysis",
    "run_cycle",
    "run_once",
]


if __name__ == "__main__":
    raise SystemExit(main())
