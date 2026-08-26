"""
15分钟价格/持仓异动提醒雷达（单文件版）
========================================

功能：
- 每 15 分钟严格排除 TradFi、稳定币和未知资产，完整扫描达到最低流动性要求的
  加密合约；正数 scan-limit 只用于人工诊断；
- 按市值四档 × 流动性三档分别计算价格、持仓、OI金额和CVD门槛，并以受控并发
  完成 5m/15m/30m/1h/24h 细算；
- 按「价格 × 持仓 × CVD(主动资金流)」组合分为 6 类模板：
    健康上涨（新多进场） / 假强背离（警惕拉高出货） / 空头回补（挤空）
    健康下跌（新空进场） / 假弱承接（下跌接货） / 恐慌杀多（多头止损）
- 达到分级阈值后推送 Telegram 卡片，标题明确区分价格、持仓或双触发；
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
- 价格和持仓使用独立阈值；持仓还需通过实际OI金额门槛，CVD同时检查净额和
  15分钟成交额占比；
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

# 允许从任意目录直接以脚本方式运行（python radars/simple_alert.py）
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from config.settings import Settings  # noqa: E402
from shared.asset_classification import (  # noqa: E402
    classify_binance_instrument,
    crypto_contract_eligibility,
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

_MARKET_CAP_TIER_LABELS = {
    "high": "高市值",
    "medium": "中市值",
    "low": "低市值",
    "unknown": "市值待补全",
}

_LIQUIDITY_TIER_LABELS = {
    "high": "高流动性",
    "medium": "中流动性",
    "low": "低流动性",
}

_LSR_CACHE: dict[str, tuple[float, float | None]] = {}
PULSE_CHART_KLINE_RESERVE = 40


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


def _env_float_alias(name: str, legacy_name: str, default: float) -> float:
    if os.getenv(name) is not None:
        return _env_float(name, default)
    return _env_float(legacy_name, default)


@dataclass(frozen=True)
class PulseCandidate:
    symbol: str
    base: str
    quote_volume_24h: float
    price_change_24h: float
    market_cap: float | None
    market_cap_source: str
    market_cap_tier: str
    liquidity_tier: str
    classification: Mapping[str, Any]


@dataclass(frozen=True)
class SimpleAlertConfig:
    # 0 scans the complete eligible universe; positive values are manual caps.
    scan_limit: int = 0
    fixed_top: int = 30
    rotation_slots: int = 10
    ticker_filter_pct: float = 2.0
    min_quote_volume_usd: float = 1_000_000.0
    market_cap_high_min_usd: float = 1_000_000_000.0
    market_cap_medium_min_usd: float = 100_000_000.0
    liquidity_high_min_usd: float = 20_000_000.0
    liquidity_medium_min_usd: float = 5_000_000.0
    price_threshold_high_pct: float = 8.0
    price_threshold_medium_pct: float = 12.0
    price_threshold_low_pct: float = 15.0
    price_threshold_unknown_pct: float = 20.0
    oi_threshold_high_pct: float = 8.0
    oi_threshold_medium_pct: float = 12.0
    oi_threshold_low_pct: float = 15.0
    oi_threshold_unknown_pct: float = 20.0
    liquidity_factor_high: float = 0.85
    liquidity_factor_medium: float = 1.0
    liquidity_factor_low: float = 1.25
    oi_delta_high_liquidity_min_usd: float = 250_000.0
    oi_delta_medium_liquidity_min_usd: float = 100_000.0
    oi_delta_low_liquidity_min_usd: float = 50_000.0
    cvd_ratio_high_liquidity_min_pct: float = 0.5
    cvd_ratio_medium_liquidity_min_pct: float = 1.0
    cvd_ratio_low_liquidity_min_pct: float = 2.0
    low_liquidity_min_volume_multiple: float = 1.5
    scan_workers: int = 8
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
            scan_limit=max(
                0,
                _env_int(
                    "SIMPLE_ALERT_SCAN_LIMIT",
                    int(settings.pulse_simple_scan_limit),
                ),
            ),
            fixed_top=max(0, _env_int("SIMPLE_ALERT_FIXED_TOP", 30)),
            rotation_slots=max(0, _env_int("SIMPLE_ALERT_ROTATION_SLOTS", 10)),
            ticker_filter_pct=max(0.0, _env_float("SIMPLE_ALERT_TICKER_FILTER_PCT", 2.0)),
            min_quote_volume_usd=max(
                0.0, _env_float("SIMPLE_ALERT_MIN_QUOTE_VOLUME", 1_000_000.0)
            ),
            market_cap_high_min_usd=max(
                1.0,
                _env_float("SIMPLE_ALERT_MARKET_CAP_HIGH_MIN_USD", 1_000_000_000.0),
            ),
            market_cap_medium_min_usd=max(
                1.0,
                _env_float("SIMPLE_ALERT_MARKET_CAP_MEDIUM_MIN_USD", 100_000_000.0),
            ),
            liquidity_high_min_usd=max(
                1.0,
                _env_float("SIMPLE_ALERT_LIQUIDITY_HIGH_MIN_USD", 20_000_000.0),
            ),
            liquidity_medium_min_usd=max(
                1.0,
                _env_float("SIMPLE_ALERT_LIQUIDITY_MEDIUM_MIN_USD", 5_000_000.0),
            ),
            price_threshold_high_pct=max(
                1.0,
                _env_float_alias(
                    "SIMPLE_ALERT_PRICE_THRESHOLD_HIGH_PCT",
                    "SIMPLE_ALERT_THRESHOLD_CORE_PCT",
                    8.0,
                ),
            ),
            price_threshold_medium_pct=max(
                1.0,
                _env_float_alias(
                    "SIMPLE_ALERT_PRICE_THRESHOLD_MEDIUM_PCT",
                    "SIMPLE_ALERT_THRESHOLD_LARGE_PCT",
                    12.0,
                ),
            ),
            price_threshold_low_pct=max(
                1.0,
                _env_float_alias(
                    "SIMPLE_ALERT_PRICE_THRESHOLD_LOW_PCT",
                    "SIMPLE_ALERT_THRESHOLD_ALT_PCT",
                    15.0,
                ),
            ),
            price_threshold_unknown_pct=max(
                1.0,
                _env_float_alias(
                    "SIMPLE_ALERT_PRICE_THRESHOLD_UNKNOWN_PCT",
                    "SIMPLE_ALERT_THRESHOLD_UNKNOWN_PCT",
                    20.0,
                ),
            ),
            oi_threshold_high_pct=max(
                1.0, _env_float("SIMPLE_ALERT_OI_THRESHOLD_HIGH_PCT", 8.0)
            ),
            oi_threshold_medium_pct=max(
                1.0, _env_float("SIMPLE_ALERT_OI_THRESHOLD_MEDIUM_PCT", 12.0)
            ),
            oi_threshold_low_pct=max(
                1.0, _env_float("SIMPLE_ALERT_OI_THRESHOLD_LOW_PCT", 15.0)
            ),
            oi_threshold_unknown_pct=max(
                1.0, _env_float("SIMPLE_ALERT_OI_THRESHOLD_UNKNOWN_PCT", 20.0)
            ),
            liquidity_factor_high=max(
                0.1, _env_float("SIMPLE_ALERT_LIQUIDITY_FACTOR_HIGH", 0.85)
            ),
            liquidity_factor_medium=max(
                0.1, _env_float("SIMPLE_ALERT_LIQUIDITY_FACTOR_MEDIUM", 1.0)
            ),
            liquidity_factor_low=max(
                0.1, _env_float("SIMPLE_ALERT_LIQUIDITY_FACTOR_LOW", 1.25)
            ),
            oi_delta_high_liquidity_min_usd=max(
                0.0, _env_float("SIMPLE_ALERT_OI_DELTA_HIGH_MIN_USD", 250_000.0)
            ),
            oi_delta_medium_liquidity_min_usd=max(
                0.0, _env_float("SIMPLE_ALERT_OI_DELTA_MEDIUM_MIN_USD", 100_000.0)
            ),
            oi_delta_low_liquidity_min_usd=max(
                0.0, _env_float("SIMPLE_ALERT_OI_DELTA_LOW_MIN_USD", 50_000.0)
            ),
            cvd_ratio_high_liquidity_min_pct=max(
                0.0, _env_float("SIMPLE_ALERT_CVD_RATIO_HIGH_MIN_PCT", 0.5)
            ),
            cvd_ratio_medium_liquidity_min_pct=max(
                0.0, _env_float("SIMPLE_ALERT_CVD_RATIO_MEDIUM_MIN_PCT", 1.0)
            ),
            cvd_ratio_low_liquidity_min_pct=max(
                0.0, _env_float("SIMPLE_ALERT_CVD_RATIO_LOW_MIN_PCT", 2.0)
            ),
            low_liquidity_min_volume_multiple=max(
                0.0,
                _env_float("SIMPLE_ALERT_LOW_LIQ_MIN_VOLUME_MULTIPLE", 1.5),
            ),
            scan_workers=max(
                1,
                min(16, _env_int("SIMPLE_ALERT_SCAN_WORKERS", 8)),
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

    def market_cap_tier(self, market_cap: float | None) -> str:
        if market_cap is None or market_cap <= 0:
            return "unknown"
        if market_cap >= self.market_cap_high_min_usd:
            return "high"
        if market_cap >= self.market_cap_medium_min_usd:
            return "medium"
        return "low"

    def liquidity_tier(self, quote_volume_24h: float) -> str | None:
        if quote_volume_24h >= self.liquidity_high_min_usd:
            return "high"
        if quote_volume_24h >= self.liquidity_medium_min_usd:
            return "medium"
        if quote_volume_24h >= self.min_quote_volume_usd:
            return "low"
        return None

    def trigger_thresholds(
        self,
        market_cap_tier: str,
        liquidity_tier: str,
    ) -> tuple[float, float]:
        price_base = {
            "high": self.price_threshold_high_pct,
            "medium": self.price_threshold_medium_pct,
            "low": self.price_threshold_low_pct,
            "unknown": self.price_threshold_unknown_pct,
        }.get(market_cap_tier, self.price_threshold_unknown_pct)
        oi_base = {
            "high": self.oi_threshold_high_pct,
            "medium": self.oi_threshold_medium_pct,
            "low": self.oi_threshold_low_pct,
            "unknown": self.oi_threshold_unknown_pct,
        }.get(market_cap_tier, self.oi_threshold_unknown_pct)
        factor = {
            "high": self.liquidity_factor_high,
            "medium": self.liquidity_factor_medium,
            "low": self.liquidity_factor_low,
        }.get(liquidity_tier, self.liquidity_factor_low)
        return price_base * factor, oi_base * factor

    def oi_delta_min_usd(self, liquidity_tier: str) -> float:
        return {
            "high": self.oi_delta_high_liquidity_min_usd,
            "medium": self.oi_delta_medium_liquidity_min_usd,
            "low": self.oi_delta_low_liquidity_min_usd,
        }.get(liquidity_tier, self.oi_delta_low_liquidity_min_usd)

    def cvd_ratio_min_pct(self, liquidity_tier: str) -> float:
        return {
            "high": self.cvd_ratio_high_liquidity_min_pct,
            "medium": self.cvd_ratio_medium_liquidity_min_pct,
            "low": self.cvd_ratio_low_liquidity_min_pct,
        }.get(liquidity_tier, self.cvd_ratio_low_liquidity_min_pct)


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
    market_caps: Mapping[str, float] | None = None,
    market_cap_sources: Mapping[str, str] | None = None,
) -> tuple[list[PulseCandidate], dict[str, Any]]:
    """Build a fail-closed crypto universe, then apply an optional manual cap."""

    market_caps = market_caps or {}
    market_cap_sources = market_cap_sources or {}
    diagnostics: dict[str, Any] = {
        "active_usdt_contracts": 0,
        "eligible_crypto_contracts": 0,
        "rejected": {},
        "below_liquidity_floor": 0,
        "ticker_missing": 0,
        "market_cap_known": 0,
        "market_cap_unknown": 0,
        "tier_matrix": {},
        "manual_cap": max(0, int(limit)),
    }
    try:
        exchange_info = source.exchange_info()
    except Exception:
        exchange_info = None
    raw_contracts = (
        exchange_info.get("symbols")
        if isinstance(exchange_info, Mapping)
        else None
    )
    if not isinstance(raw_contracts, list):
        diagnostics["catalogue_status"] = "unavailable"
        diagnostics["full_coverage"] = False
        return [], diagnostics

    excluded = {
        str(item).strip().upper()
        for item in source.settings.excluded_base_assets
        if str(item).strip()
    }
    eligible: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    rejected: dict[str, int] = {}
    for contract in raw_contracts:
        if not isinstance(contract, Mapping):
            continue
        if (
            str(contract.get("status") or "").upper() != "TRADING"
            or str(contract.get("quoteAsset") or "").upper() != "USDT"
        ):
            continue
        diagnostics["active_usdt_contracts"] += 1
        symbol = str(contract.get("symbol") or "").strip().upper()
        allowed, reason, classification = crypto_contract_eligibility(
            symbol,
            contract,
            excluded_base_assets=excluded,
        )
        if not allowed:
            rejected[reason] = rejected.get(reason, 0) + 1
            continue
        eligible[symbol] = (contract, classification)
    diagnostics["rejected"] = dict(sorted(rejected.items()))
    diagnostics["eligible_crypto_contracts"] = len(eligible)

    try:
        tickers = source.ticker_24h()
    except Exception:
        tickers = []
    seen_tickers: set[str] = set()
    rows: list[PulseCandidate] = []
    for ticker in tickers if isinstance(tickers, list) else []:
        if not isinstance(ticker, Mapping):
            continue
        symbol = str(ticker.get("symbol") or "").strip().upper()
        if symbol not in eligible or symbol in seen_tickers:
            continue
        seen_tickers.add(symbol)
        contract, classification = eligible[symbol]
        base = str(contract.get("baseAsset") or symbol[:-4]).strip().upper()
        quote_volume = _number(ticker.get("quoteVolume")) or 0.0
        liquidity_tier = cfg.liquidity_tier(quote_volume)
        if liquidity_tier is None:
            diagnostics["below_liquidity_floor"] += 1
            continue
        market_cap = _number(market_caps.get(base))
        rows.append(PulseCandidate(
            symbol=symbol,
            base=base,
            quote_volume_24h=quote_volume,
            price_change_24h=_number(ticker.get("priceChangePercent")) or 0.0,
            market_cap=market_cap,
            market_cap_source=str(market_cap_sources.get(base) or ""),
            market_cap_tier=cfg.market_cap_tier(market_cap),
            liquidity_tier=liquidity_tier,
            classification=classification,
        ))
    rows.sort(key=lambda row: (-row.quote_volume_24h, row.symbol))
    diagnostics["ticker_missing"] = max(0, len(eligible) - len(seen_tickers))
    diagnostics["eligible_after_liquidity"] = len(rows)
    diagnostics["market_cap_known"] = sum(
        candidate.market_cap is not None for candidate in rows
    )
    diagnostics["market_cap_unknown"] = sum(
        candidate.market_cap is None for candidate in rows
    )

    if limit <= 0 or limit >= len(rows):
        selected = list(rows)
    else:
        fixed_top = min(max(0, cfg.fixed_top), limit)
        selected = list(rows[:fixed_top])
        selected_set = {candidate.symbol for candidate in selected}
        remaining = rows[fixed_top:]
        anomalies = [
            candidate
            for candidate in remaining
            if abs(candidate.price_change_24h) >= cfg.ticker_filter_pct
        ]
        anomalies.sort(key=lambda candidate: (
            -abs(candidate.price_change_24h),
            -candidate.quote_volume_24h,
            candidate.symbol,
        ))
        rotation = [
            candidate
            for candidate in remaining
            if abs(candidate.price_change_24h) < cfg.ticker_filter_pct
        ]
        rotation.sort(key=lambda candidate: candidate.symbol)
        rotation_reserve = min(
            max(0, cfg.rotation_slots),
            max(0, limit - len(selected)),
            len(rotation),
        )
        anomaly_limit = max(len(selected), limit - rotation_reserve)
        for candidate in anomalies:
            if len(selected) >= anomaly_limit:
                break
            if candidate.symbol in selected_set:
                continue
            selected.append(candidate)
            selected_set.add(candidate.symbol)

        if len(selected) < limit and rotation:
            offset = (window_index * max(1, len(selected) + 1)) % len(rotation)
            ordered = rotation[offset:] + rotation[:offset]
            for candidate in ordered:
                if len(selected) >= limit:
                    break
                if candidate.symbol not in selected_set:
                    selected.append(candidate)
                    selected_set.add(candidate.symbol)

        if len(selected) < limit:
            for candidate in anomalies:
                if len(selected) >= limit:
                    break
                if candidate.symbol not in selected_set:
                    selected.append(candidate)
                    selected_set.add(candidate.symbol)

    matrix: dict[str, int] = {}
    for candidate in selected:
        key = f"{candidate.market_cap_tier}x{candidate.liquidity_tier}"
        matrix[key] = matrix.get(key, 0) + 1
    diagnostics["tier_matrix"] = dict(sorted(matrix.items()))
    diagnostics["selected"] = len(selected)
    diagnostics["full_coverage"] = len(selected) == len(rows)
    diagnostics["catalogue_status"] = "ready"
    return selected, diagnostics


def _analyze_symbol(
    source: BinanceDataSource,
    candidate: PulseCandidate,
    window_end_ms: int,
    cfg: SimpleAlertConfig,
) -> dict[str, Any] | None:
    symbol = candidate.symbol
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
    futures_gross_15 = sum(quotes[-3:]) if len(quotes) >= 3 else None
    spot_gross_15 = sum(spot_quotes[-3:]) if len(spot_quotes) >= 3 else None
    if futures_gross_15 is None and spot_gross_15 is None:
        cvd_gross_15 = None
    else:
        cvd_gross_15 = (futures_gross_15 or 0.0) + (spot_gross_15 or 0.0)
    cvd_ratio_15m_pct = (
        abs(cvd_net) / cvd_gross_15 * 100.0
        if cvd_net is not None and cvd_gross_15 and cvd_gross_15 > 0
        else None
    )
    cvd_ratio_min_pct = cfg.cvd_ratio_min_pct(candidate.liquidity_tier)
    cvd_required_usd = max(
        cfg.cvd_min_net_usd,
        (cvd_gross_15 or 0.0) * cvd_ratio_min_pct / 100.0,
    )
    price_threshold, oi_threshold = cfg.trigger_thresholds(
        candidate.market_cap_tier,
        candidate.liquidity_tier,
    )
    oi_delta_15m_usd = (
        oi_values[-1] - oi_values[-4]
        if len(oi_values) > 3
        else None
    )
    oi_delta_min_usd = cfg.oi_delta_min_usd(candidate.liquidity_tier)
    template = classify_template(
        price_map.get(3),
        oi_map.get(3),
        cvd_net,
        cfg.direction_deadband_pct,
        cvd_required_usd,
    )
    price_15m = price_map.get(3)
    oi_15m = oi_map.get(3)
    price_triggered = abs(price_15m or 0.0) >= price_threshold
    oi_triggered = (
        abs(oi_15m or 0.0) >= oi_threshold
        and abs(oi_delta_15m_usd or 0.0) >= oi_delta_min_usd
    )
    if template is not None:
        if not (price_triggered or oi_triggered):
            template = None
        elif (
            candidate.liquidity_tier == "low"
            and not (price_triggered and oi_triggered)
            and (volume_map.get(3) or 0.0)
            < cfg.low_liquidity_min_volume_multiple
        ):
            template = None

    trigger_source = (
        "both"
        if price_triggered and oi_triggered
        else "price"
        if price_triggered
        else "oi"
        if oi_triggered
        else "none"
    )

    return {
        "symbol": symbol,
        "base": candidate.base,
        "tier": candidate.market_cap_tier,
        "tier_label": _MARKET_CAP_TIER_LABELS[candidate.market_cap_tier],
        "market_cap_tier": candidate.market_cap_tier,
        "market_cap_tier_label": _MARKET_CAP_TIER_LABELS[
            candidate.market_cap_tier
        ],
        "liquidity_tier": candidate.liquidity_tier,
        "liquidity_tier_label": _LIQUIDITY_TIER_LABELS[
            candidate.liquidity_tier
        ],
        "price_threshold": price_threshold,
        "oi_threshold": oi_threshold,
        "oi_delta_min_usd": oi_delta_min_usd,
        "trigger_source": trigger_source,
        "price_triggered": price_triggered,
        "oi_triggered": oi_triggered,
        "template": template,
        "current_price": closes[-1] if closes else None,
        "current_oi_usd": oi_values[-1] if oi_values else None,
        "quote_volume_24h": candidate.quote_volume_24h,
        "price_map": price_map,
        "oi_map": oi_map,
        "volume_map": volume_map,
        "futures_flow": futures_flow,
        "spot_flow": spot_flow,
        "cvd_net_15m": cvd_net,
        "cvd_gross_15m": cvd_gross_15,
        "cvd_ratio_15m_pct": cvd_ratio_15m_pct,
        "cvd_ratio_min_pct": cvd_ratio_min_pct,
        "cvd_required_usd": cvd_required_usd,
        "oi_delta_15m_usd": oi_delta_15m_usd,
        "market_cap": candidate.market_cap,
        "market_cap_source": candidate.market_cap_source,
        "asset_category": dict(candidate.classification),
        "long_short_ratio": None,
    }


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


def _price_trigger_text(value: float | None) -> str:
    if value is None:
        return "价格—"
    direction = "上涨" if value > 0 else "下跌" if value < 0 else "持平"
    return f"价格{direction} {abs(value):.2f}% {_arrow(value)}"


def _oi_trigger_text(value: float | None) -> str:
    if value is None:
        return "持仓—"
    direction = "增加" if value > 0 else "减少" if value < 0 else "持平"
    return f"持仓{direction} {abs(value):.2f}% {_arrow(value)}"


def _trigger_headline(item: Mapping[str, Any]) -> tuple[str, str]:
    source = str(item.get("trigger_source") or "")
    price = (item.get("price_map") or {}).get(3)
    oi = (item.get("oi_map") or {}).get(3)
    if source == "both":
        return "价格+持仓触发", f"{_price_trigger_text(price)} · {_oi_trigger_text(oi)}"
    if source == "oi":
        return "持仓触发", f"{_oi_trigger_text(oi)} · {_price_trigger_text(price)}"
    return "价格触发", _price_trigger_text(price)


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
    trigger_label, trigger_headline = _trigger_headline(item)
    market_cap = item.get("market_cap")
    market_cap_source = str(item.get("market_cap_source") or "")
    market_cap_source_text = (
        f"（{market_cap_source}）"
        if market_cap is not None and market_cap_source
        else ""
    )
    market_trend = _updown(price_map.get(288))
    cvd_net = item.get("cvd_net_15m")
    cvd_label, cvd_arrow = _cvd_direction(
        cvd_net,
        _number(item.get("cvd_required_usd")) or cfg.cvd_min_net_usd,
    )
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
        f"{meta['icon']} {meta['title']} · {trigger_label} (第{count}次) {meta['icon']}",
        "",
        f"{display_pair} (<code>{base}</code>) {meta['color']} {trigger_headline}",
        "",
        f"🔗 <a href='{tv_url}'>𝑻𝒓𝒂𝒅𝒊𝒏𝒈𝑽𝒊𝒆𝒘</a> | <a href='{cg_url}'>𝑪𝒐𝒊𝒏𝒈𝒍𝒂𝒔𝒔</a>",
        "",
        f"⏰ 提醒时间: {datetime.fromtimestamp(time.time(), CST).strftime('%Y-%m-%d %H:%M:%S')} (北京时间)",
        "━━━━━━━━━━━━━━━━━━━━",
        "<pre>💰 基础信息",
        f"当前价格: {_fmt_price(item.get('current_price'))}",
        f"当前市值: {_fmt_money(market_cap)} {market_trend}{market_cap_source_text}",
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
        f"💡 分档阈值: {item.get('market_cap_tier_label') or item.get('tier_label') or '市值待补全'}"
        f" × {item.get('liquidity_tier_label') or '中流动性'}｜"
        f"价格≥{float(item.get('price_threshold') or item.get('threshold') or 0):.1f}%｜"
        f"持仓≥{float(item.get('oi_threshold') or item.get('threshold') or 0):.1f}%"
        f"且ΔOI≥{_fmt_money(_number(item.get('oi_delta_min_usd')))} "
        f"{meta['threshold_emoji']}",
        "",
        f"📌 组合判断: {meta['combo']}",
        "",
        f"📌 结论: {meta['conclusion']}",
        "",
        (
            "🟡 数据来源: 币安 Binance（价格/OI/CVD）"
            + (
                "；CoinPaprika（备用市值）"
                if market_cap_source == "CoinPaprika备用市值"
                else ""
            )
        ),
    ]
    return "\n".join(lines)


def _pulse_chart_category(item: Mapping[str, Any]) -> str:
    return {
        "high": "高市值加密",
        "medium": "中市值加密",
        "low": "低市值加密",
        "unknown": "市值待补全加密",
        "core": "核心主流",
        "large": "主流加密",
        "alt": "山寨币",
    }.get(str(item.get("tier") or ""), "未分类")


def _pulse_chart_checkpoints(
    state: Mapping[str, Any],
    symbol: str,
    count: int,
    signal_close_ts: int,
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
    numbered.append((max(1, count), signal_close_ts))
    return [
        {
            "checkpoint_no": checkpoint_no,
            "window_end_ts": timestamp,
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
    """Build closed 1h context plus the current hour's closed 15m tail."""

    try:
        signal_close_ts = int(window_end_ms // 1000)
        quarter_rows = source.klines(
            str(item.get("symbol") or ""),
            interval="15m",
            limit=DISPLAY_CANDLE_LIMIT * 4 + 4,
            end_time=window_end_ms - 1,
        )
        quarter_candles: list[dict[str, float | int]] = []
        for row in quarter_rows:
            if not isinstance(row, (list, tuple)) or len(row) < 11:
                continue
            close_ms = _number(row[6])
            open_price = _number(row[1])
            high_price = _number(row[2])
            low_price = _number(row[3])
            close_price = _number(row[4])
            quote_volume = _number(row[7])
            taker_buy_quote = _number(row[10])
            if (
                int(close_ms) >= window_end_ms
                or min(open_price, high_price, low_price, close_price) <= 0
                or high_price < low_price
                or quote_volume < 0
                or taker_buy_quote < 0
                or taker_buy_quote > quote_volume
            ):
                continue
            quarter_candles.append({
                "close_ts": int(close_ms) // 1000 + 1,
                "open": open_price,
                "high": high_price,
                "low": low_price,
                "close": close_price,
                "quote_volume": quote_volume,
                "cvd_delta": 2.0 * taker_buy_quote - quote_volume,
            })
        quarter_candles.sort(key=lambda value: int(value["close_ts"]))
        if len(quarter_candles) < 20:
            return None
        if int(quarter_candles[-1]["close_ts"]) < signal_close_ts:
            return None

        hourly_groups: dict[int, list[dict[str, float | int]]] = {}
        for quarter in quarter_candles:
            close_ts = int(quarter["close_ts"])
            hour_end_ts = ((close_ts + 60 * 60 - 1) // (60 * 60)) * (60 * 60)
            hourly_groups.setdefault(hour_end_ts, []).append(quarter)

        candles: list[dict[str, Any]] = []
        complete_hour_ends: list[int] = []
        for hour_end_ts, group in sorted(hourly_groups.items()):
            ordered = sorted(group, key=lambda value: int(value["close_ts"]))
            expected_closes = [hour_end_ts - offset for offset in (45 * 60, 30 * 60, 15 * 60, 0)]
            if [int(value["close_ts"]) for value in ordered] != expected_closes:
                continue
            deltas = [_number(value["cvd_delta"]) for value in ordered]
            candles.append({
                "close_ts": hour_end_ts,
                "open": _number(ordered[0]["open"]),
                "high": max(_number(value["high"]) for value in ordered),
                "low": min(_number(value["low"]) for value in ordered),
                "close": _number(ordered[-1]["close"]),
                "quote_volume": sum(_number(value["quote_volume"]) for value in ordered),
                "cvd_delta": sum(deltas),
                "cvd_parts": deltas,
                "timeframe": "1h",
            })
            complete_hour_ends.append(hour_end_ts)
        if len(complete_hour_ends) < 5:
            return None
        latest_hourly_close_ts = max(complete_hour_ends)
        for quarter in quarter_candles:
            if int(quarter["close_ts"]) <= latest_hourly_close_ts:
                continue
            candles.append({
                **quarter,
                "cvd_parts": [_number(quarter["cvd_delta"])],
                "timeframe": "15m",
            })
        if max(int(candle["close_ts"]) for candle in candles) < signal_close_ts:
            return None
        candles = sorted(
            candles,
            key=lambda value: int(value["close_ts"]),
        )[-DISPLAY_CANDLE_LIMIT:]
        running_cvd = 0.0
        for candle in candles:
            cvd_path = [running_cvd]
            for delta in candle.pop("cvd_parts", [candle.get("cvd_delta", 0.0)]):
                running_cvd += _number(delta)
                cvd_path.append(running_cvd)
            candle["cvd_open"] = cvd_path[0]
            candle["cvd_high"] = max(cvd_path)
            candle["cvd_low"] = min(cvd_path)
            candle["cvd_close"] = cvd_path[-1]
        first_chart_start_ts = min(
            int(candle["close_ts"])
            - (15 * 60 if candle.get("timeframe") == "15m" else 60 * 60)
            for candle in candles
        )
        oi_rows = source.open_interest_hist(
            str(item.get("symbol") or ""),
            period="15m",
            limit=DISPLAY_CANDLE_LIMIT * 4 + 4,
            start_time=max(0, first_chart_start_ts - 15 * 60) * 1000,
            end_time=window_end_ms,
        )
        oi_points = sorted(
            (
                (int(_number(row.get("timestamp"))) // 1000, _number(row.get("sumOpenInterestValue")))
                for row in oi_rows
                if isinstance(row, Mapping)
                and _number(row.get("timestamp")) > 0
                and _number(row.get("sumOpenInterestValue")) > 0
            ),
            key=lambda point: point[0],
        )
        for candle in candles:
            close_ts = int(candle["close_ts"])
            duration_sec = 15 * 60 if candle.get("timeframe") == "15m" else 60 * 60
            start_ts = close_ts - duration_sec
            baseline = next(
                (
                    point
                    for point in reversed(oi_points)
                    if point[0] <= start_ts
                ),
                None,
            )
            samples: list[tuple[int, float]] = []
            if baseline is not None and start_ts - baseline[0] <= 15 * 60:
                samples.append(baseline)
            samples.extend(
                point
                for point in oi_points
                if start_ts < point[0] <= close_ts
            )
            if not samples:
                continue
            values = [point[1] for point in samples]
            candle["oi_open"] = values[0]
            candle["oi_high"] = max(values)
            candle["oi_low"] = min(values)
            candle["oi_close"] = values[-1]
            candle["oi_value"] = values[-1]
        current_oi = _number(item.get("current_oi_usd"))
        if current_oi > 0:
            latest_candle = max(candles, key=lambda value: int(value["close_ts"]))
            previous_close = next(
                (
                    _number(candle.get("oi_close"))
                    for candle in reversed(candles[:-1])
                    if _number(candle.get("oi_close")) > 0
                ),
                current_oi,
            )
            oi_open = _number(latest_candle.get("oi_open")) or previous_close
            oi_high = _number(latest_candle.get("oi_high")) or oi_open
            oi_low = _number(latest_candle.get("oi_low")) or oi_open
            latest_candle["oi_open"] = oi_open
            latest_candle["oi_high"] = max(oi_high, oi_open, current_oi)
            latest_candle["oi_low"] = min(oi_low, oi_open, current_oi)
            latest_candle["oi_close"] = current_oi
            latest_candle["oi_value"] = current_oi
        if sum(1 for candle in candles if _number(candle.get("oi_close")) > 0) < 2:
            return None
        price_map = item.get("price_map")
        oi_map = item.get("oi_map")
        return render_pulse_chart_png(
            symbol=str(item.get("symbol") or ""),
            candles=candles,
            checkpoints=_pulse_chart_checkpoints(
                state,
                str(item.get("symbol") or ""),
                count,
                signal_close_ts,
            ),
            cycle_no=count,
            asset_category=_pulse_chart_category(item),
            signal_change_pct=(
                price_map.get(3) if isinstance(price_map, Mapping) else None
            ),
            signal_oi_change_pct=(
                oi_map.get(3) if isinstance(oi_map, Mapping) else None
            ),
            width=1440,
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
    observed_symbols: set[str],
    cfg: SimpleAlertConfig,
) -> None:
    for symbol in list(state.keys()):
        record = state.get(symbol)
        if not isinstance(record, dict):
            state.pop(symbol, None)
            continue
        if symbol not in observed_symbols:
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
    cycle_started = time.monotonic()
    now_ts = int(time.time()) if now_ts is None else int(now_ts)
    window = closed_window(interval_sec=900, delay_sec=cfg.close_delay_sec)
    store = JsonStore(settings.data_dir)
    state = _load_state(store, _state_path(cfg, settings))
    effective_scan_limit = max(
        0,
        int(cfg.scan_limit if scan_limit is None else scan_limit),
    )
    source = BinanceDataSource(settings)
    diagnostics: dict[str, Any] = {}
    try:
        try:
            raw_market_caps = source.market_caps() or {}
        except Exception:
            raw_market_caps = {}
        if not isinstance(raw_market_caps, Mapping):
            raw_market_caps = {}
        market_caps = {
            str(base).upper(): value
            for base, value in raw_market_caps.items()
        }
        market_cap_sources = {
            str(base).upper(): "Binance市场资料"
            for base in market_caps
        }
        try:
            fallback_market_caps = source.coinpaprika_market_caps() or {}
        except Exception:
            fallback_market_caps = {}
        if not isinstance(fallback_market_caps, Mapping):
            fallback_market_caps = {}
        for base, value in fallback_market_caps.items():
            normalized_base = str(base).upper()
            if normalized_base not in market_caps:
                market_caps[normalized_base] = value
                market_cap_sources[normalized_base] = "CoinPaprika备用市值"
        pool, universe = _candidate_pool(
            source, cfg, effective_scan_limit,
            window_index=int(window.end_ms // _15M_MS),
            market_caps=market_caps,
            market_cap_sources=market_cap_sources,
        )
        analysis_budget = len(pool)
        if hasattr(source, "budget"):
            source.budget.ensure_limit(
                "open_interest_hist",
                max(
                    settings.oi_hist_budget,
                    analysis_budget + PULSE_CHART_KLINE_RESERVE,
                ),
            )
            source.budget.ensure_limit(
                "klines",
                max(
                    settings.kline_budget,
                    analysis_budget + PULSE_CHART_KLINE_RESERVE,
                ),
            )
            source.budget.ensure_limit(
                "spot_klines",
                max(settings.kline_budget, analysis_budget),
            )
        completed: list[dict[str, Any]] = []
        triggered: list[dict[str, Any]] = []
        worker_count = min(max(1, cfg.scan_workers), max(1, len(pool)))
        if pool:
            with ThreadPoolExecutor(
                max_workers=worker_count,
                thread_name_prefix="pulse-scan",
            ) as executor:
                futures = {
                    executor.submit(
                        _analyze_symbol,
                        source,
                        candidate,
                        window.end_ms,
                        cfg,
                    ): candidate.symbol
                    for candidate in pool
                }
                for future in as_completed(futures):
                    try:
                        item = future.result()
                    except Exception:
                        item = None
                    if item is None:
                        continue
                    completed.append(item)
                    if item.get("template"):
                        triggered.append(item)
        completed.sort(key=lambda item: str(item.get("symbol") or ""))
        triggered.sort(key=lambda item: str(item.get("symbol") or ""))
        for item in triggered:
            item["long_short_ratio"] = _long_short_ratio(source, item["symbol"])
            item["tv_url"] = f"https://www.tradingview.com/chart/?symbol=BINANCE:{item['symbol']}"
            item["cg_url"] = f"https://www.coinglass.com/tv/zh/Binance_{item['symbol']}"

        triggered_symbols = {str(item["symbol"]) for item in triggered}
        pushes: list[dict[str, Any]] = []
        review_items: list[dict[str, Any]] = []
        for item in triggered:
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
            _tick_quiet(
                state,
                triggered_symbols,
                {str(item.get("symbol") or "") for item in completed},
                cfg,
            )
            _prune_expired(state, now_ts, cfg.follow_window_sec)
            _save_state(store, _state_path(cfg, settings), state)
        if review_items:
            try:
                from radars.pulse.review_store import record_signals
                record_signals(settings, review_items)
            except Exception as exc:
                print(f"[review] record failed {type(exc).__name__}", file=sys.stderr)
        duration_sec = round(time.monotonic() - cycle_started, 3)
        complete_coverage = bool(
            universe.get("full_coverage")
            and len(completed) == len(pool)
            and duration_sec < 15 * 60
        )
        diagnostics = {
            "window_end": window.end.strftime("%Y-%m-%d %H:%M:%S"),
            "scan_limit": effective_scan_limit,
            "scan_mode": (
                "all_eligible_crypto"
                if effective_scan_limit == 0
                else "manual_cap"
            ),
            "chart_kline_reserve": PULSE_CHART_KLINE_RESERVE,
            "scan_workers": worker_count,
            "scanned": len(pool),
            "analysis_completed": len(completed),
            "analysis_failed": len(pool) - len(completed),
            "triggered": len(triggered),
            "coverage_status": "complete" if complete_coverage else "partial",
            "completed_within_15m": duration_sec < 15 * 60,
            "cycle_duration_sec": duration_sec,
            "universe": universe,
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
    """推送一张示例卡片，并复用正式提醒的 K 线图发送链路。"""
    cfg = SimpleAlertConfig.from_env(settings)
    item = {
        "symbol": "CETUSUSDT",
        "base": "CETUS",
        "tier": "low",
        "tier_label": _MARKET_CAP_TIER_LABELS["low"],
        "market_cap_tier": "low",
        "market_cap_tier_label": _MARKET_CAP_TIER_LABELS["low"],
        "liquidity_tier": "high",
        "liquidity_tier_label": _LIQUIDITY_TIER_LABELS["high"],
        "price_threshold": cfg.trigger_thresholds("low", "high")[0],
        "oi_threshold": cfg.trigger_thresholds("low", "high")[1],
        "oi_delta_min_usd": cfg.oi_delta_min_usd("high"),
        "trigger_source": "oi",
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
        "cvd_required_usd": 5000.0,
        "market_cap": 16600000.0,
        "long_short_ratio": 2.15,
        "tv_url": "https://www.tradingview.com/chart/?symbol=BINANCE:CETUSUSDT",
        "cg_url": "https://www.coinglass.com/tv/zh/Binance_CETUSUSDT",
    }
    chart_png: bytes | None = None
    source: BinanceDataSource | None = None
    try:
        source = BinanceDataSource(settings)
        window = closed_window(interval_sec=900, delay_sec=cfg.close_delay_sec)
        chart_png = _render_pulse_chart(
            source,
            item,
            {},
            1,
            window.end_ms,
        )
    except Exception:
        chart_png = None
    finally:
        if source is not None:
            source.close()

    text = _format_card(item, 1, cfg)
    result = gateway.send(
        text,
        TEMPLATE_ID,
        f"simple-alert:test-push:{int(time.time())}",
        send=send,
        confirm_real_send=confirm_real_send,
        cooldown_sec=0,
        parse_mode="HTML",
        photo=chart_png,
        enrich_market_context=False,
    )
    chart_status = "K线图已生成" if chart_png is not None else "K线图不可用，已降级为文字"
    print(f"测试推送状态: {result.status} ({result.reason})；{chart_status}")
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
    "PULSE_CHART_KLINE_RESERVE",
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
