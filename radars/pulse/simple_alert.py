"""
15分钟价格/持仓异动提醒雷达（单文件版）
========================================

功能：
- 每 15 分钟两段式扫描：先用一次 24 小时行情接口全市场初筛（固定成交额头部 + 当天异动币），
  再只对候选币做 5m/15m/30m/1h/24h 价格/持仓/量能/资金流/多空比细算；
- 按「价格 × 持仓 × CVD(主动资金流)」组合分为 6 类模板：
    健康上涨（新多进场） / 假强背离（警惕拉高出货） / 空头回补（挤空）
    健康下跌（新空进场） / 假弱承接（下跌接货） / 恐慌杀多（多头止损）
- 达到分级阈值（按资产类别分档）后推送 Telegram 卡片；
- 首次触发立即发送，之后进入跟随监控：同币种 2 小时事件窗口内，
  只有「升级」或「状态反转」才再发，每事件最多 3 次；
  连续 2 个窗口安静后重置，事件窗口到期后重新按第 1 次计。

独立运行（在项目根目录执行）：
  python -m radars.pulse.simple_alert --once                 # 跑一轮，默认 dry-run 打印
  python -m radars.pulse.simple_alert --once --send --confirm-real-send
生产主入口：python main.py pulse / loop / live

运行逻辑：
1. 从 config/.env.oi 读取配置（TG_BOT_TOKEN / TG_CHAT_ID / SIMPLE_ALERT_* 阈值）；
2. 用 shared/binance_data.py 拉取数据（缓存/超时/重试/请求预算）；
3. 计算 5m/15m/30m/1h/24h 价格与持仓变化、成交量倍数、合约/现货主动净流入；
4. 按 15 分钟闭合窗口分类成 6 种状态，只有达到对应资产档位阈值才进入推送；
5. 用 shared/telegram.py 推送（去重、限流、dry-run 双重门禁全部复用）；
6. 跟随状态写入 data/simple_alert_state.json，重启不丢失。

合并方式（未来与其它雷达共用同一 bot/数据源）：
  from radars.pulse.simple_alert import run_once
  run_once(settings, gateway, send=..., confirm_real_send=...)   # 由 runtime/cli.py 调用

说明：
- 触发阈值 = 15 分钟价格变化或持仓变化，任一达到即触发（按资产类别分档）；
- 方向判断用小阈值（默认 ±1%），CVD 方向用 15 分钟合约+现货主动净额合计；
- 多空比只在触发后按需拉取，带 5 分钟缓存；失败时卡片显示「—」，不影响分类；
- 合约/现货资金流单边缺失时按可用侧计算，双侧缺失则不分类、不推送。
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
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

# 允许从任意目录直接以脚本方式运行（python radars/simple_alert.py）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.settings import Settings  # noqa: E402
from shared.asset_classification import (  # noqa: E402
    classify_binance_instrument,
    is_stable_crypto_asset,
)
from shared.binance_data import BinanceDataSource  # noqa: E402
from radars.pulse.chart import (  # noqa: E402
    DISPLAY_CANDLE_LIMIT,
    render_pulse_chart_png,
)
from shared.storage import JsonStore  # noqa: E402
from shared.telegram import TelegramGateway, plain_fallback  # noqa: E402
from shared.time_windows import CST, closed_window  # noqa: E402

TEMPLATE_ID = "TG_LAUNCH_ALERT"

# 5m K线/持仓点数：300 根 ≈ 25 小时，足够算到 24 小时变化
_SERIES_POINTS = 300
_5M_MS = 5 * 60 * 1000
_15M_MS = 15 * 60 * 1000
# (标签, 5m 根数)
_PRICE_OI_WINDOWS = (
    ("5分钟", 1),
    ("15分钟", 3),
    ("30分钟", 6),
    ("1小时", 12),
    ("24小时", 288),
)
_FLOW_WINDOWS = (
    ("5分钟", 1),
    ("15分钟", 3),
    ("1小时", 12),
    ("24小时", 288),
)
_VOLUME_WINDOWS = (
    ("5分钟", 1),
    ("15分钟", 3),
    ("30分钟", 6),
    ("1小时", 12),
)

TEMPLATE_META: dict[str, dict[str, str]] = {
    "health_up": {
        "title": "15分钟健康上涨提醒",
        "icon": "🚀",
        "color": "🟢",
        "direction": "上涨",
        "arrow": "↗️",
        "threshold_emoji": "🟢",
        "conclusion": "健康上涨，新多进场（仅供预警参考）",
        "combo": "价格↑ · 持仓↑ · CVD↑",
    },
    "false_strong": {
        "title": "15分钟假强背离提醒",
        "icon": "⚠️",
        "color": "🟡",
        "direction": "上涨",
        "arrow": "↗️",
        "threshold_emoji": "🟡",
        "conclusion": "假强背离，主动资金在流出，警惕拉高出货，不追多",
        "combo": "价格↑ · 持仓↑ · CVD↓",
    },
    "short_covering": {
        "title": "15分钟空头回补提醒",
        "icon": "⚠️",
        "color": "🟡",
        "direction": "上涨",
        "arrow": "↗️",
        "threshold_emoji": "🟡",
        "conclusion": "空头回补推动上涨（挤空），回补结束易回落，不追多",
        "combo": "价格↑ · 持仓↓ · CVD↑",
    },
    "health_down": {
        "title": "15分钟健康下跌提醒",
        "icon": "📉",
        "color": "🔴",
        "direction": "下跌",
        "arrow": "↘️",
        "threshold_emoji": "🔴",
        "conclusion": "健康下跌，新空进场（仅供预警参考）",
        "combo": "价格↓ · 持仓↑ · CVD↓",
    },
    "false_weak": {
        "title": "15分钟假弱承接提醒",
        "icon": "⚠️",
        "color": "🟡",
        "direction": "下跌",
        "arrow": "↘️",
        "threshold_emoji": "🟡",
        "conclusion": "假弱承接，下跌中主动资金进场，不追空，等待企稳信号",
        "combo": "价格↓ · 持仓↑ · CVD↑",
    },
    "panic_dump": {
        "title": "15分钟恐慌杀多提醒",
        "icon": "📉",
        "color": "🔴",
        "direction": "下跌",
        "arrow": "↘️",
        "threshold_emoji": "🔴",
        "conclusion": "恐慌杀多，多头止损离场，不追空，等待企稳",
        "combo": "价格↓ · 持仓↓ · CVD↓",
    },
}

# 8 组合 → 6 模板（上涨减仓/空头平仓两个组合不推送）
_COMBO_TEMPLATES: dict[tuple[str, str, str], str] = {
    ("up", "up", "up"): "health_up",
    ("up", "up", "down"): "false_strong",
    ("up", "down", "up"): "short_covering",
    ("down", "up", "down"): "health_down",
    ("down", "up", "up"): "false_weak",
    ("down", "down", "down"): "panic_dump",
}

SIGNAL_DIRECTIONS = {
    "health_up": "long",
    "false_strong": "short",
    "short_covering": "short",
    "health_down": "short",
    "false_weak": "long",
    "panic_dump": "short",
}

_TIER_LABELS = {
    "core": "核心主流",
    "large": "大盘",
    "alt": "山寨币",
    "unknown": "其它",
}

_LSR_CACHE: dict[str, tuple[float, float | None]] = {}


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
class SimpleAlertConfig:
    scan_limit: int = 120
    fixed_top: int = 30
    rotation_slots: int = 10
    ticker_filter_pct: float = 2.0
    min_quote_volume_usd: float = 5_000_000.0
    threshold_core_pct: float = 8.0
    threshold_large_pct: float = 12.0
    threshold_alt_pct: float = 15.0
    threshold_unknown_pct: float = 20.0
    direction_deadband_pct: float = 1.0
    cvd_min_net_usd: float = 5_000.0
    follow_window_sec: int = 2 * 3600
    follow_max_count: int = 3
    follow_escalation_pct: float = 30.0
    quiet_windows_limit: int = 2
    close_delay_sec: int = 60
    loop_interval_sec: int = 180
    state_path: Path | None = None

    @classmethod
    def from_env(cls, settings: Settings) -> "SimpleAlertConfig":
        return cls(
            scan_limit=max(1, _env_int("SIMPLE_ALERT_SCAN_LIMIT", 120)),
            fixed_top=max(0, _env_int("SIMPLE_ALERT_FIXED_TOP", 30)),
            rotation_slots=max(0, _env_int("SIMPLE_ALERT_ROTATION_SLOTS", 10)),
            ticker_filter_pct=max(0.0, _env_float("SIMPLE_ALERT_TICKER_FILTER_PCT", 2.0)),
            min_quote_volume_usd=max(
                0.0, _env_float("SIMPLE_ALERT_MIN_QUOTE_VOLUME", 5_000_000.0)
            ),
            threshold_core_pct=max(
                1.0, _env_float("SIMPLE_ALERT_THRESHOLD_CORE_PCT", 8.0)
            ),
            threshold_large_pct=max(
                1.0, _env_float("SIMPLE_ALERT_THRESHOLD_LARGE_PCT", 12.0)
            ),
            threshold_alt_pct=max(
                1.0, _env_float("SIMPLE_ALERT_THRESHOLD_ALT_PCT", 15.0)
            ),
            threshold_unknown_pct=max(
                1.0, _env_float("SIMPLE_ALERT_THRESHOLD_UNKNOWN_PCT", 20.0)
            ),
            direction_deadband_pct=max(
                0.0, _env_float("SIMPLE_ALERT_DIRECTION_DEADBAND_PCT", 1.0)
            ),
            cvd_min_net_usd=max(
                0.0, _env_float("SIMPLE_ALERT_CVD_MIN_NET_USD", 5_000.0)
            ),
            follow_window_sec=max(
                300, _env_int("SIMPLE_ALERT_FOLLOW_WINDOW_SEC", 2 * 3600)
            ),
            follow_max_count=max(1, _env_int("SIMPLE_ALERT_FOLLOW_MAX_COUNT", 3)),
            follow_escalation_pct=max(
                1.0, _env_float("SIMPLE_ALERT_FOLLOW_ESCALATION_PCT", 30.0)
            ),
            quiet_windows_limit=max(1, _env_int("SIMPLE_ALERT_QUIET_WINDOWS", 2)),
            close_delay_sec=max(0, _env_int("SIMPLE_ALERT_CLOSE_DELAY_SEC", 60)),
            loop_interval_sec=max(30, _env_int("SIMPLE_ALERT_LOOP_INTERVAL_SEC", 180)),
            state_path=(
                settings.data_dir
                / os.getenv("SIMPLE_ALERT_STATE_FILE", "simple_alert_state.json")
            ),
        )

    def threshold_for_tier(self, tier: str) -> float:
        return {
            "core": self.threshold_core_pct,
            "large": self.threshold_large_pct,
            "alt": self.threshold_alt_pct,
            "unknown": self.threshold_unknown_pct,
        }.get(tier, self.threshold_unknown_pct)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _direction(value: float | None, threshold: float) -> str:
    if value is None:
        return "flat"
    if value >= threshold:
        return "up"
    if value <= -threshold:
        return "down"
    return "flat"


def classify_template(
    price_15m_pct: float | None,
    oi_15m_pct: float | None,
    cvd_net_usd: float | None,
    direction_deadband_pct: float,
    cvd_min_net_usd: float,
) -> str | None:
    """按 价格×持仓×CVD 组合归类到 6 个模板；无法分类时返回 None。"""
    if price_15m_pct is None or oi_15m_pct is None or cvd_net_usd is None:
        return None
    price_dir = _direction(price_15m_pct, direction_deadband_pct)
    oi_dir = _direction(oi_15m_pct, direction_deadband_pct)
    cvd_dir = _direction(cvd_net_usd, cvd_min_net_usd)
    if cvd_dir == "flat":
        return None
    return _COMBO_TEMPLATES.get((price_dir, oi_dir, cvd_dir))


def _series_pct(series: list[float], offset: int) -> float | None:
    if len(series) > offset and series[-1 - offset] and series[-1 - offset] > 0:
        return (series[-1] / series[-1 - offset] - 1.0) * 100.0
    return None


def _window_flow(taker_quote: list[float], quote: list[float], bars: int) -> float | None:
    if len(taker_quote) < bars or len(quote) < bars:
        return None
    return sum(
        2.0 * t - q
        for t, q in zip(taker_quote[-bars:], quote[-bars:])
    )


def _volume_multiple(quote: list[float], bars: int) -> float | None:
    if len(quote) <= bars:
        return None
    current = sum(quote[-bars:])
    previous = quote[:-bars]
    blocks = len(previous) // bars
    if blocks <= 0:
        return None
    baseline = sum(previous[-bars * blocks:]) / blocks
    if baseline <= 0:
        return None
    return current / baseline


def _close_series(rows: list[Any]) -> list[float]:
    out: list[float] = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 11:
            value = _number(row[4])
            if value is not None:
                out.append(value)
    return out


def _quote_series(rows: list[Any]) -> list[float]:
    out: list[float] = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 11:
            value = _number(row[7])
            if value is not None:
                out.append(value)
    return out


def _taker_quote_series(rows: list[Any]) -> list[float]:
    out: list[float] = []
    for row in rows:
        if isinstance(row, (list, tuple)) and len(row) >= 11:
            value = _number(row[10])
            if value is not None:
                out.append(value)
    return out


def _oi_value_series(rows: list[Any]) -> list[float]:
    out: list[float] = []
    for row in sorted(
        (row for row in rows if isinstance(row, Mapping)),
        key=lambda row: int(row.get("timestamp", 0) or 0),
    ):
        value = _number(row.get("sumOpenInterestValue"))
        if value is not None:
            out.append(value)
    return out


def _asset_tier(symbol: str) -> str:
    try:
        sub = str(classify_binance_instrument(symbol).get("asset_subclass") or "")
    except Exception:
        sub = ""
    if sub == "core_crypto":
        return "core"
    if sub == "large_crypto":
        return "large"
    if sub == "altcoin":
        return "alt"
    return "unknown"


def _candidate_pool(
    source: BinanceDataSource,
    cfg: SimpleAlertConfig,
    limit: int,
    window_index: int,
) -> list[str]:
    """两段式候选选择：全市场 24h 行情初筛 → 固定头部 + 当天异动优先。

    第一段只发一次 ticker/24hr 请求覆盖全部 USDT 永续；
    第二段从通过最低成交额过滤的池子里选：
      1) 固定成交额头部（fixed_top 个，每轮必扫）；
      2) 当天 24h 异动币（|24h 涨跌幅| ≥ ticker_filter_pct，按异动幅度排序）；
      3) 剩余名额用轮换补齐，保证中小币也有机会被扫到。
    """
    try:
        tickers = source.ticker_24h()
    except Exception:
        tickers = []
    excluded = {str(item).upper() for item in source.settings.excluded_base_assets}
    rows: list[tuple[float, float, str]] = []
    for ticker in tickers if isinstance(tickers, list) else []:
        if not isinstance(ticker, Mapping):
            continue
        symbol = str(ticker.get("symbol") or "").strip().upper()
        if not symbol.endswith("USDT"):
            continue
        base = symbol[:-4]
        if is_stable_crypto_asset(base) or base in excluded:
            continue
        quote_volume = _number(ticker.get("quoteVolume")) or 0.0
        if quote_volume < cfg.min_quote_volume_usd:
            continue
        change_24h = abs(_number(ticker.get("priceChangePercent")) or 0.0)
        rows.append((quote_volume, change_24h, symbol))
    rows.sort(key=lambda row: (-row[0], row[2]))

    fixed_top = min(max(0, cfg.fixed_top), limit)
    selected = [row[2] for row in rows[:fixed_top]]
    selected_set = set(selected)
    remaining = rows[fixed_top:]

    anomalies = [row for row in remaining if row[1] >= cfg.ticker_filter_pct]
    anomalies.sort(key=lambda row: (-row[1], -row[0], row[2]))
    rotation = [row[2] for row in remaining if row[1] < cfg.ticker_filter_pct]
    rotation.sort()
    rotation_reserve = min(
        max(0, cfg.rotation_slots),
        max(0, limit - len(selected)),
        len(rotation),
    )
    anomaly_limit = max(len(selected), limit - rotation_reserve)
    for _volume, _change, symbol in anomalies:
        if len(selected) >= anomaly_limit:
            break
        if symbol in selected_set:
            continue
        selected.append(symbol)
        selected_set.add(symbol)

    if len(selected) < limit and rotation:
        offset = (window_index * max(1, len(selected) + 1)) % len(rotation)
        ordered = rotation[offset:] + rotation[:offset]
        for symbol in ordered:
            if len(selected) >= limit:
                break
            if symbol not in selected_set:
                selected.append(symbol)
                selected_set.add(symbol)

    if len(selected) < limit:
        for _volume, _change, symbol in anomalies:
            if len(selected) >= limit:
                break
            if symbol not in selected_set:
                selected.append(symbol)
                selected_set.add(symbol)
    return selected


def _analyze_symbol(
    source: BinanceDataSource,
    symbol: str,
    window_end_ms: int,
    cfg: SimpleAlertConfig,
) -> dict[str, Any] | None:
    start_ms = max(0, window_end_ms - _SERIES_POINTS * _5M_MS)
    try:
        klines = source.klines(
            symbol, interval="5m", limit=_SERIES_POINTS,
            start_time=start_ms, end_time=window_end_ms - 1,
        )
        oi_rows = source.open_interest_hist(
            symbol, period="5m", limit=_SERIES_POINTS, end_time=window_end_ms,
        )
        spot_rows = source.spot_klines(
            symbol, interval="5m", limit=_SERIES_POINTS,
            start_time=start_ms, end_time=window_end_ms - 1,
        )
    except Exception:
        return None
    if not klines or not oi_rows:
        return None

    closes = _close_series(klines)
    quotes = _quote_series(klines)
    takers = _taker_quote_series(klines)
    oi_values = _oi_value_series(oi_rows)
    spot_quotes = _quote_series(spot_rows)
    spot_takers = _taker_quote_series(spot_rows)
    if not closes or not oi_values:
        return None

    price_map = {
        bars: _series_pct(closes, bars)
        for _label, bars in _PRICE_OI_WINDOWS
    }
    oi_map = {
        bars: _series_pct(oi_values, bars)
        for _label, bars in _PRICE_OI_WINDOWS
    }
    volume_map = {
        bars: _volume_multiple(quotes, bars)
        for _label, bars in _VOLUME_WINDOWS
    }
    futures_flow = {
        bars: _window_flow(takers, quotes, bars)
        for _label, bars in _FLOW_WINDOWS
    }
    spot_flow = {
        bars: _window_flow(spot_takers, spot_quotes, bars)
        for _label, bars in _FLOW_WINDOWS
    }

    futures_15 = futures_flow.get(3)
    spot_15 = spot_flow.get(3)
    if futures_15 is None and spot_15 is None:
        cvd_net = None
    else:
        cvd_net = (futures_15 or 0.0) + (spot_15 or 0.0)

    tier = _asset_tier(symbol)
    threshold = cfg.threshold_for_tier(tier)
    template = classify_template(
        price_map.get(3),
        oi_map.get(3),
        cvd_net,
        cfg.direction_deadband_pct,
        cfg.cvd_min_net_usd,
    )
    if template is not None:
        price_15m = price_map.get(3)
        oi_15m = oi_map.get(3)
        if not (
            abs(price_15m or 0.0) >= threshold
            or abs(oi_15m or 0.0) >= threshold
        ):
            template = None

    quote_volume_24h = sum(quotes[-288:]) if len(quotes) >= 288 else None
    return {
        "symbol": symbol,
        "base": symbol[:-4],
        "tier": tier,
        "tier_label": _TIER_LABELS.get(tier, "其它"),
        "threshold": threshold,
        "template": template,
        "current_price": closes[-1] if closes else None,
        "current_oi_usd": oi_values[-1] if oi_values else None,
        "quote_volume_24h": quote_volume_24h,
        "price_map": price_map,
        "oi_map": oi_map,
        "volume_map": volume_map,
        "futures_flow": futures_flow,
        "spot_flow": spot_flow,
        "cvd_net_15m": cvd_net,
        "market_cap": None,
        "long_short_ratio": None,
    }


def _preview_url(cmc_map: Mapping[str, Any], base: str) -> str | None:
    """用 CMC ID 拼 logo 直链；缺失时退回 CMC 币种页（og 图可预览）。"""
    info = cmc_map.get(str(base or "").upper())
    if not isinstance(info, Mapping):
        return None
    try:
        cmc_id = int(info.get("cmc_id") or 0)
    except (TypeError, ValueError):
        cmc_id = 0
    if cmc_id > 0:
        return f"https://s2.coinmarketcap.com/static/img/coins/128x128/{cmc_id}.png"
    slug = str(info.get("slug") or "")
    if slug:
        return f"https://coinmarketcap.com/currencies/{slug}/"
    return None


_COINGECKO_LOGO_CACHE: dict[str, tuple[float, str | None]] = {}


def _coingecko_logo_url(
    source: BinanceDataSource,
    base: str,
    *,
    expected_slug: str = "",
) -> str | None:
    """查 CoinGecko 高清 large 图，按 CMC slug 校验防错配；失败返回 None。缓存 24 小时。"""
    key = str(base or "").strip().upper()
    if not key:
        return None
    now = time.time()
    cached = _COINGECKO_LOGO_CACHE.get(key)
    if cached is not None and now - cached[0] < 86400:
        return cached[1]
    url: str | None = None
    try:
        data = source.http.get_json(
            "https://api.coingecko.com/api/v3/search",
            {"query": base},
            cache_key=f"cglogo:search:{key}",
            quality_key="coingeckoLogo",
            timeout=8,
            retries=1,
            cache=False,
        )
        candidates: list[str] = []
        coins = data.get("coins") if isinstance(data, dict) else None
        for coin in coins if isinstance(coins, list) else []:
            if isinstance(coin, Mapping) and str(coin.get("symbol") or "").upper() == key:
                candidates.append(str(coin.get("id") or ""))
        coin_id = ""
        if expected_slug:
            coin_id = next((cid for cid in candidates if cid == expected_slug), "")
        elif candidates:
            coin_id = candidates[0]
        if coin_id:
            detail = source.http.get_json(
                f"https://api.coingecko.com/api/v3/coins/{coin_id}",
                cache_key=f"cglogo:detail:{key}",
                quality_key="coingeckoLogo",
                timeout=8,
                retries=1,
                cache=False,
            )
            image = (detail.get("image") or {}) if isinstance(detail, dict) else {}
            url = str(image.get("large") or "") or None
    except Exception:
        url = None
    _COINGECKO_LOGO_CACHE[key] = (now, url)
    if len(_COINGECKO_LOGO_CACHE) > 200:
        _COINGECKO_LOGO_CACHE.clear()
    return url


def _long_short_ratio(
    source: BinanceDataSource,
    symbol: str,
    ttl_sec: float = 300.0,
) -> float | None:
    now = time.time()
    cached = _LSR_CACHE.get(symbol)
    if cached is not None and now - cached[0] < ttl_sec:
        return cached[1]
    value: float | None = None
    try:
        data = source.http.get_json(
            source.endpoint("/futures/data/topLongShortPositionRatio"),
            {"symbol": symbol, "period": "15m", "limit": 1},
            cache_key=f"tlsr:{symbol}:15m",
            quality_key="topLongShortPositionRatio",
            timeout=8,
            retries=1,
            cache=False,
        )
        if isinstance(data, list) and data and isinstance(data[0], Mapping):
            value = _number(data[0].get("longShortRatio"))
    except Exception:
        value = None
    _LSR_CACHE[symbol] = (now, value)
    if len(_LSR_CACHE) > 200:
        _LSR_CACHE.clear()
    return value


# ---------- 模板格式化 ----------

def _fmt_pct(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{value:+.{digits}f}%"


def _fmt_price(value: float | None) -> str:
    if value is None:
        return "—"
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:.4f}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6f}"


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    absolute = abs(value)
    def compact(value: float) -> str:
        return f"{value:.2f}".rstrip("0").rstrip(".")
    if absolute >= 1e9:
        return f"{sign}${compact(absolute / 1e9)}B"
    if absolute >= 1e6:
        return f"{sign}${compact(absolute / 1e6)}M"
    if absolute >= 1e3:
        return f"{sign}${compact(absolute / 1e3)}K"
    return f"{sign}${compact(absolute)}"


def _fmt_flow(value: float | None) -> str:
    if value is None:
        return "—"
    sign = "+" if value >= 0 else "-"
    absolute = abs(value)
    if absolute >= 1e6:
        return f"{sign}{absolute / 1e6:.1f}M"
    if absolute >= 1e3:
        return f"{sign}{absolute / 1e3:.1f}K"
    return f"{sign}{absolute:.0f}"


def _fmt_ratio(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}"


def _volume_emoji(multiple: float | None) -> str:
    if multiple is None or multiple < 2.0:
        return "➡️"
    if multiple < 5.0:
        return "🔺"
    if multiple < 20.0:
        return "⚡"
    return "💥"


def _fmt_multiple(value: float | None) -> str:
    return "—" if value is None else f"{value:.2f}倍"


def _arrow(value: float | None) -> str:
    if value is None or abs(value) < 1e-9:
        return "➡️"
    return "↗️" if value > 0 else "↘️"


def _updown(value: float | None) -> str:
    if value is None or abs(value) < 1e-9:
        return "➡️"
    return "📈" if value > 0 else "📉"



def _disp_width(text: str) -> int:
    try:
        from unicodedata import east_asian_width
        return sum(2 if east_asian_width(ch) in ("W", "F") else 1 for ch in text)
    except Exception:
        return len(text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _disp_width(text))


def _lsr_hint(ratio: float | None) -> str:
    if ratio is None:
        return ""
    if ratio >= 3.0:
        return "大户多头持仓极占优，警惕回调"
    if ratio >= 1.5:
        return "偏多，大户多头持仓占优"
    if ratio >= 1.2:
        return "略偏多"
    if ratio > 0.8:
        return "多空均衡"
    if ratio > 0.5:
        return "略偏空"
    if ratio >= 0.33:
        return "偏空，大户空头持仓占优"
    return "大户空头持仓极占优，警惕反弹"


def _lsr_line(ratio: float | None) -> str:
    if ratio is None:
        return "—"
    return f"{ratio:.2f}（{_lsr_hint(ratio)}）"


def _cvd_direction(value: float | None, min_net_usd: float) -> tuple[str, str]:
    if value is None:
        return "无法判断", "➡️"
    if value >= min_net_usd:
        return "上升", "↗️"
    if value <= -min_net_usd:
        return "下降", "↘️"
    return "持平", "➡️"

def _flow_emoji(value: float | None, market: str) -> str:
    if value is None or value == 0:
        return "➡️"
    return "📈" if value > 0 else "📉"


def _html_safe(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _bold_italic_serif(text: str) -> str:
    styled: list[str] = []
    for char in text:
        if "A" <= char <= "Z":
            styled.append(chr(0x1D468 + ord(char) - ord("A")))
        elif "a" <= char <= "z":
            styled.append(chr(0x1D482 + ord(char) - ord("a")))
        else:
            styled.append(char)
    return "".join(styled)


def _format_card(item: Mapping[str, Any], count: int, cfg: SimpleAlertConfig) -> str:
    symbol = str(item["symbol"])
    raw_base = str(item["base"])
    base = html.escape(raw_base)
    display_pair = html.escape(_bold_italic_serif(f"{raw_base}/USDT"))
    template = str(item["template"])
    meta = TEMPLATE_META[template]
    price_map = item["price_map"]
    oi_map = item["oi_map"]
    volume_map = item["volume_map"]
    futures_flow = item["futures_flow"]
    spot_flow = item["spot_flow"]

    price_15m = price_map.get(3)
    oi_15m = oi_map.get(3)
    trigger_metric = (
        oi_15m
        if (oi_15m is not None and (price_15m is None or abs(oi_15m) >= abs(price_15m)))
        else price_15m
    )
    market_cap = item.get("market_cap")
    market_trend = _updown(price_map.get(288))
    cvd_net = item.get("cvd_net_15m")
    cvd_label, cvd_arrow = _cvd_direction(cvd_net, cfg.cvd_min_net_usd)
    futures_15 = futures_flow.get(3)
    spot_15 = spot_flow.get(3)
    if futures_15 is not None and spot_15 is not None:
        cvd_source_label = "合约+现货净"
    elif futures_15 is not None:
        cvd_source_label = "合约口径净"
    elif spot_15 is not None:
        cvd_source_label = "现货口径净"
    else:
        cvd_source_label = "净"
    tv_url = str(item.get("tv_url") or f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol}")
    cg_url = str(item.get("cg_url") or f"https://www.coinglass.com/tv/zh/Binance_{symbol}")

    price_oi_lines = [f"当前持仓: {_fmt_money(item.get('current_oi_usd'))}"]
    for label, bars in _PRICE_OI_WINDOWS:
        price = price_map.get(bars)
        oi = oi_map.get(bars)
        price_oi_lines.append(
            f"{_pad(label, 6)}: {_pad(f'价格{_fmt_pct(price)}', 12)}{_updown(price)} | "
            f"{_pad(f'持仓{_fmt_pct(oi)}', 12)}{_updown(oi)}"
        )
    volume_lines = []
    for label, bars in _VOLUME_WINDOWS:
        multiple = volume_map.get(bars)
        emoji = _volume_emoji(multiple)
        volume_lines.append(f"{_pad(label, 6)}: {_pad(_fmt_multiple(multiple), 8)}{emoji}")
    flow_lines = []
    for label, bars in _FLOW_WINDOWS:
        futures = futures_flow.get(bars)
        spot = spot_flow.get(bars)
        flow_lines.append(
            f"{_pad(label, 6)}: {_pad(f'合约{_fmt_flow(futures)}', 12)}{_flow_emoji(futures, 'futures')} | "
            f"{_pad(f'现货{_fmt_flow(spot)}', 12)}{_flow_emoji(spot, 'spot')}"
        )
    if all(value is None for value in futures_flow.values()):
        flow_lines.append("⚠️ 合约资金流不可用")
    if all(value is None for value in spot_flow.values()):
        flow_lines.append("⚠️ 该币无币安现货，资金流仅按合约口径")

    lines = [
        f"{meta['icon']} {meta['title']} (第{count}次) {meta['icon']}",
        "",
        f"{display_pair} (<code>{base}</code>) {meta['color']} {meta['direction']} "
        f"{abs(trigger_metric or 0.0):.2f}% {meta['arrow']}",
        "",
        f"🔗 <a href='{tv_url}'>𝑻𝒓𝒂𝒅𝒊𝒏𝒈𝑽𝒊𝒆𝒘</a> | <a href='{cg_url}'>𝑪𝒐𝒊𝒏𝒈𝒍𝒂𝒔𝒔</a>",
        "",
        f"⏰ 提醒时间: {datetime.fromtimestamp(time.time(), CST).strftime('%Y-%m-%d %H:%M:%S')} (北京时间)",
        "━━━━━━━━━━━━━━━━━━━━",
        "<pre>💰 基础信息",
        f"当前价格: {_fmt_price(item.get('current_price'))}",
        f"当前市值: {_fmt_money(market_cap)} {market_trend}",
        f"{_pad('大户多空比', 10)}: {_lsr_line(item.get('long_short_ratio'))}",
        f"{_pad('15分钟价格', 10)}: {_fmt_pct(price_15m)} {_arrow(price_15m)}",
        f"{_pad('15分钟持仓', 10)}: {_fmt_pct(oi_15m)} {_arrow(oi_15m)}</pre>",
        "<pre>📊 价格 & 持仓变化",
        *price_oi_lines[:-1],
        price_oi_lines[-1] + "</pre>",
        "<pre>📊 成交量倍数分析 (币安数据)",
        *volume_lines[:-1],
        volume_lines[-1] + "</pre>",
        "<pre>💸 资金流向",
        *flow_lines[:-1],
        flow_lines[-1] + "</pre>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"🧭 15分钟CVD方向: {cvd_label} {cvd_arrow}（{cvd_source_label} {_fmt_flow(cvd_net)}）",
        "",
        f"💡 提醒阈值: 15分钟价格或持仓变动≥{item['threshold']:.1f}%"
        f"（{item['tier_label']}档） {meta['threshold_emoji']}",
        "",
        f"📌 组合判断: {meta['combo']}",
        "",
        f"📌 结论: {meta['conclusion']}",
        "",
        f"🟡 数据来源: 币安 Binance",
    ]
    return "\n".join(lines)


def _pulse_chart_category(item: Mapping[str, Any]) -> str:
    return {
        "core": "核心主流",
        "large": "主流加密",
        "alt": "山寨币",
    }.get(str(item.get("tier") or ""), "未分类")


def _pulse_chart_checkpoints(
    state: Mapping[str, Any],
    symbol: str,
    count: int,
    current_close_ts: int,
) -> list[dict[str, Any]]:
    record = state.get(symbol)
    existing = record if isinstance(record, Mapping) else {}
    numbered: list[tuple[int, int]] = []
    if count >= 2:
        first_ts = int(existing.get("event_start_ts", 0) or 0)
        if first_ts > 0:
            numbered.append((1, first_ts))
    if count >= 3:
        previous_ts = int(existing.get("last_sent_ts", 0) or 0)
        if previous_ts > 0:
            numbered.append((count - 1, previous_ts))
    numbered.append((max(1, count), current_close_ts))
    return [
        {
            "checkpoint_no": checkpoint_no,
            "window_end_ts": min(timestamp, current_close_ts),
            "stage": "",
        }
        for checkpoint_no, timestamp in numbered
    ]


def _render_pulse_chart(
    source: BinanceDataSource,
    item: Mapping[str, Any],
    state: Mapping[str, Any],
    count: int,
    window_end_ms: int,
) -> bytes | None:
    """Build the optional 1h chart after a pulse has passed its send gate."""

    try:
        rows = source.klines(
            str(item.get("symbol") or ""),
            interval="1h",
            limit=DISPLAY_CANDLE_LIMIT + 1,
            end_time=window_end_ms - 1,
        )
        candles: list[dict[str, float | int]] = []
        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 8:
                continue
            close_ms = _number(row[6])
            open_price = _number(row[1])
            high_price = _number(row[2])
            low_price = _number(row[3])
            close_price = _number(row[4])
            quote_volume = _number(row[7])
            if (
                close_ms is None
                or int(close_ms) >= window_end_ms
                or open_price is None
                or high_price is None
                or low_price is None
                or close_price is None
            ):
                continue
            candles.append({
                "close_ts": int(close_ms) // 1000 + 1,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "quote_volume": max(0.0, quote_volume or 0.0),
            })
        if len(candles) < 5:
            return None
        current_close_ts = max(int(candle["close_ts"]) for candle in candles)
        return render_pulse_chart_png(
            symbol=str(item.get("symbol") or ""),
            candles=candles,
            checkpoints=_pulse_chart_checkpoints(
                state,
                str(item.get("symbol") or ""),
                count,
                current_close_ts,
            ),
            cycle_no=count,
            asset_category=_pulse_chart_category(item),
            width=1080,
            height=720,
        )
    except Exception:
        # The chart is presentation-only; market or rendering failures retain
        # the existing text alert instead of consuming the pulse signal.
        return None


def _state_path(cfg: SimpleAlertConfig, settings: Settings) -> Path:
    return cfg.state_path or settings.data_dir / "simple_alert_state.json"


def _load_state(store: JsonStore, path: Path) -> dict[str, Any]:
    state = store.load(path, {})
    return state if isinstance(state, dict) else {}


def _save_state(store: JsonStore, path: Path, state: dict[str, Any]) -> None:
    store.save(path, state)


def _follow_action(
    state: dict[str, Any],
    item: Mapping[str, Any],
    cfg: SimpleAlertConfig,
    now_ts: int,
) -> tuple[int, str] | None:
    symbol = str(item["symbol"])
    template = str(item["template"])
    record = state.get(symbol)
    if record is None:
        return 1, template
    if now_ts - int(record.get("event_start_ts", 0) or 0) > cfg.follow_window_sec:
        return 1, template
    count = int(record.get("count", 1) or 1)
    if count >= cfg.follow_max_count:
        return None
    if template == str(record.get("template") or ""):
        metric = max(abs(item["price_map"].get(3) or 0.0), abs(item["oi_map"].get(3) or 0.0))
        peak = float(record.get("peak_metric") or 0.0)
        if metric >= peak * (1.0 + cfg.follow_escalation_pct / 100.0):
            return count + 1, template
        return None
    return count + 1, template


def _update_state_on_send(
    state: dict[str, Any],
    item: Mapping[str, Any],
    count: int,
    template: str,
    cfg: SimpleAlertConfig,
    now_ts: int,
) -> None:
    symbol = str(item["symbol"])
    metric = max(abs(item["price_map"].get(3) or 0.0), abs(item["oi_map"].get(3) or 0.0))
    record = state.get(symbol)
    if record is None or now_ts - int(record.get("event_start_ts", 0) or 0) > cfg.follow_window_sec:
        record = {
            "event_start_ts": now_ts,
            "count": 0,
            "peak_metric": 0.0,
            "template": "",
            "quiet_windows": 0,
            "last_sent_ts": 0,
        }
    record["count"] = count
    record["template"] = template
    record["peak_metric"] = max(float(record.get("peak_metric") or 0.0), metric)
    record["quiet_windows"] = 0
    record["last_sent_ts"] = now_ts
    state[symbol] = record


def _tick_quiet(
    state: dict[str, Any],
    triggered_symbols: set[str],
    cfg: SimpleAlertConfig,
) -> None:
    for symbol in list(state.keys()):
        record = state.get(symbol)
        if not isinstance(record, dict):
            state.pop(symbol, None)
            continue
        if symbol in triggered_symbols:
            record["quiet_windows"] = 0
            continue
        record["quiet_windows"] = int(record.get("quiet_windows", 0) or 0) + 1
        if record["quiet_windows"] >= cfg.quiet_windows_limit:
            state.pop(symbol, None)


def _prune_expired(state: dict[str, Any], now_ts: int, follow_window_sec: int) -> None:
    for symbol in list(state.keys()):
        record = state.get(symbol)
        if not isinstance(record, dict):
            state.pop(symbol, None)
            continue
        if now_ts - int(record.get("event_start_ts", 0) or 0) > follow_window_sec:
            state.pop(symbol, None)


# ---------- 主循环 ----------

def run_cycle(
    settings: Settings,
    gateway: TelegramGateway,
    cfg: SimpleAlertConfig,
    *,
    send: bool,
    confirm_real_send: bool,
    scan_limit: int | None = None,
    now_ts: int | None = None,
) -> dict[str, Any]:
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    window = closed_window(interval_sec=900, delay_sec=cfg.close_delay_sec)
    store = JsonStore(settings.data_dir)
    state = _load_state(store, _state_path(cfg, settings))
    source = BinanceDataSource(
        settings, oi_hist_budget=max(settings.oi_hist_budget, cfg.scan_limit + 40)
    )
    diagnostics: dict[str, Any] = {}
    try:
        try:
            market_caps = source.market_caps() or {}
        except Exception:
            market_caps = {}
        pool = _candidate_pool(
            source, cfg, max(1, scan_limit or cfg.scan_limit),
            window_index=int(window.end_ms // _15M_MS),
        )
        analyzed: list[dict[str, Any]] = []
        for symbol in pool:
            item = _analyze_symbol(source, symbol, window.end_ms, cfg)
            if item and item.get("template"):
                item["market_cap"] = market_caps.get(item["base"])
                analyzed.append(item)
        for item in analyzed:
            item["long_short_ratio"] = _long_short_ratio(source, item["symbol"])
            item["tv_url"] = f"https://www.tradingview.com/chart/?symbol=BINANCE:{item['symbol']}"
            item["cg_url"] = f"https://www.coinglass.com/tv/zh/Binance_{item['symbol']}"

        triggered_symbols = {str(item["symbol"]) for item in analyzed}
        pushes: list[dict[str, Any]] = []
        review_items: list[dict[str, Any]] = []
        for item in analyzed:
            action = _follow_action(state, item, cfg, now_ts)
            if action is None:
                continue
            count, template = action
            text = _format_card(item, count, cfg)
            chart_png = (
                _render_pulse_chart(
                    source,
                    item,
                    state,
                    count,
                    window.end_ms,
                )
                if len(plain_fallback(text)) <= 1024
                else None
            )
            dedup_key = f"simple-alert:{item['symbol']}:{window.end_ms}:{template}:{count}"
            result = gateway.send(
                text,
                TEMPLATE_ID,
                dedup_key,
                send=send,
                confirm_real_send=confirm_real_send,
                cooldown_sec=0,
                parse_mode="HTML",
                signal_records=[{
                    "symbol": item["symbol"],
                    "stage": template,
                    "category": template,
                    "signal_direction": SIGNAL_DIRECTIONS[template],
                    "evaluation_eligible": True,
                    "price": item.get("current_price") or 0.0,
                    "price_pct": (item.get("price_map") or {}).get(3) or 0.0,
                    "oi_change_pct": (item.get("oi_map") or {}).get(3) or 0.0,
                    "window_sec": 15 * 60,
                    "quality_gate": "allow",
                    "primary_data_source": "binance_native",
                }],
                photo=chart_png,
                enrich_market_context=False,
            )
            if result.sent:
                _update_state_on_send(state, item, count, template, cfg, now_ts)
            elif result.message_ids:
                gateway.delete_messages_detailed(
                    list(result.message_ids),
                    reason="pulse_partial_send_rollback",
                )
            pushes.append({
                "symbol": item["symbol"],
                "template": template,
                "count": count,
                "status": result.status,
                "reason": result.reason,
                "chart_status": "ready" if chart_png is not None else "unavailable",
            })
            if result.sent and result.message_ids:
                review_items.append({
                    "radar": "alert",
                    "template": template,
                    "symbol": item["symbol"],
                    "price": item.get("current_price") or 0.0,
                    "oi_pct": (item.get("oi_map") or {}).get(3) or 0.0,
                    "price_pct": (item.get("price_map") or {}).get(3) or 0.0,
                    "message_id": result.message_ids[0],
                })
        if send and confirm_real_send:
            _tick_quiet(state, triggered_symbols, cfg)
            _prune_expired(state, now_ts, cfg.follow_window_sec)
            _save_state(store, _state_path(cfg, settings), state)
        if review_items:
            try:
                from radars.pulse.review_store import record_signals
                record_signals(settings, review_items)
            except Exception as exc:
                print(f"[review] record failed {type(exc).__name__}", file=sys.stderr)
        diagnostics = {
            "window_end": window.end.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_limit": max(1, scan_limit or cfg.scan_limit),
            "scanned": len(pool),
            "triggered": len(analyzed),
            "pushes": pushes,
            "state_active_symbols": len(state),
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
    cfg: SimpleAlertConfig | None = None,
) -> dict[str, Any]:
    settings = settings or Settings.load()
    cfg = cfg or SimpleAlertConfig.from_env(settings)
    gateway = gateway or TelegramGateway(settings, JsonStore(settings.data_dir))
    return run_cycle(
        settings, gateway, cfg,
        send=send, confirm_real_send=confirm_real_send, scan_limit=scan_limit,
    )


def _run_loop(settings: Settings, gateway: TelegramGateway, cfg: SimpleAlertConfig, args: argparse.Namespace) -> int:
    last_window_ms = 0
    try:
        while True:
            window = closed_window(interval_sec=900, delay_sec=cfg.close_delay_sec)
            if window.end_ms != last_window_ms:
                run_cycle(
                    settings, gateway, cfg,
                    send=args.send,
                    confirm_real_send=args.confirm_real_send,
                    scan_limit=args.scan_limit,
                )
                last_window_ms = window.end_ms
            time.sleep(max(5, cfg.loop_interval_sec))
    except KeyboardInterrupt:
        print("\n[simple-alert] 已停止")
    return 0



def _send_test_push(
    settings: Settings,
    gateway: TelegramGateway,
    send: bool,
    confirm_real_send: bool,
) -> int:
    """推送一张内置示例卡片，用于验证 token/chat 链路。"""
    cfg = SimpleAlertConfig.from_env(settings)
    item = {
        "symbol": "CETUSUSDT",
        "base": "CETUS",
        "tier": "alt",
        "tier_label": _TIER_LABELS["alt"],
        "threshold": cfg.threshold_alt_pct,
        "template": "health_up",
        "current_price": 0.0215,
        "current_oi_usd": 2520000.0,
        "quote_volume_24h": 1e8,
        "price_map": {1: 2.71, 3: 7.06, 6: 15.08, 12: 16.32, 288: 23.82},
        "oi_map": {1: 1.53, 3: 31.11, 6: 57.85, 12: 60.56, 288: 72.79},
        "volume_map": {1: 0.17, 3: 1.29, 6: 79.84, 12: 64.30},
        "futures_flow": {1: 12000.0, 3: 45000.0, 12: 180000.0, 288: 320000.0},
        "spot_flow": {1: 38000.0, 3: 147000.0, 12: 367100.0, 288: 578000.0},
        "cvd_net_15m": 47000.0,
        "market_cap": 16600000.0,
        "long_short_ratio": 2.15,
        "tv_url": "https://www.tradingview.com/chart/?symbol=BINANCE:CETUSUSDT",
        "cg_url": "https://www.coinglass.com/tv/zh/Binance_CETUSUSDT",
    }
    preview_url = None
    preview_source = None
    try:
        preview_source = BinanceDataSource(settings)
        marketing = preview_source.marketing_symbols() or []
        cmc_map = {}
        for row in marketing:
            if isinstance(row, Mapping):
                base = str(row.get("base_asset") or "").upper()
                cmc_map[base] = {"cmc_id": row.get("cmc_id"), "slug": row.get("slug")}
        cetus_info = cmc_map.get("CETUS") if isinstance(cmc_map, dict) else None
        cetus_slug = str(cetus_info.get("slug") or "") if isinstance(cetus_info, Mapping) else ""
        preview_url = _coingecko_logo_url(preview_source, "CETUS", expected_slug=cetus_slug) or _preview_url(cmc_map, "CETUS")
    except Exception:
        preview_url = None
    finally:
        if preview_source is not None:
            preview_source.close()

    text = _format_card(item, 1, cfg)
    result = gateway.send(
        text,
        TEMPLATE_ID,
        f"simple-alert:test-push:{int(time.time())}",
        send=send,
        confirm_real_send=confirm_real_send,
        cooldown_sec=0,
        parse_mode="HTML",
        link_preview_url=preview_url,
    )
    print(f"测试推送状态: {result.status} ({result.reason})")
    return 0 if result.sent else 1

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="15分钟价格/持仓异动提醒雷达（单文件版）"
    )
    parser.add_argument("--once", action="store_true", help="只跑一轮（默认）")
    parser.add_argument("--loop", action="store_true", help="常驻循环")
    parser.add_argument("--interval", type=int, default=None, help="循环检查间隔秒数（默认取配置）")
    parser.add_argument("--send", action="store_true", help="允许真实发送，仍需 --confirm-real-send")
    parser.add_argument("--confirm-real-send", action="store_true", help="确认真实发送")
    parser.add_argument("--scan-limit", type=int, default=None, help="本轮扫描币种上限")
    parser.add_argument("--test-push", action="store_true", help="推送一张内置示例卡片，验证 token/chat 链路")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    cfg = SimpleAlertConfig.from_env(settings)
    if args.interval is not None:
        cfg = SimpleAlertConfig(**{**vars(cfg), "loop_interval_sec": max(30, args.interval)})
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
    "SimpleAlertConfig",
    "SIGNAL_DIRECTIONS",
    "TEMPLATE_ID",
    "TEMPLATE_META",
    "classify_template",
    "run_cycle",
    "run_once",
]


if __name__ == "__main__":
    raise SystemExit(main())
