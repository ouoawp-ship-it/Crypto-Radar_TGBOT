from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape
from typing import Any, Optional

from config import Settings
from shared.market_links import coinglass_tv_url as _coinglass_tv_url
from shared.market_links import telegram_coin_links
from shared.numbers import to_float
from shared.storage import JsonStore

OPPORTUNITY_KEYWORDS = [
    "alpha", "airdrop", "tge", "token generation", "will list", "will launch", "将上线",
    "上线", "launchpool", "hodler", "megadrop", "binance wallet", "exclusive",
    "trading tournament", "trade to share", "token vouchers", "campaign", "rewards",
]
RISK_KEYWORDS = [
    "delist", "delisting", "remove", "will remove", "will delist", "下架", "移除",
    "停止交易", "cease trading", "suspend trading",
]
EXCLUDE_OPPORTUNITY_KEYWORDS = [
    "completed", "已完成", "maintenance", "维护", "trading bots services",
    "futures will launch", "perpetual contract", "usdⓈ-margined", "usd-margined",
    "coin-margined", "tradfi", "pre-ipo", "margin will add", "trading pairs",
]
ANNOUNCEMENT_WORD_BLACKLIST = {
    "BINANCE", "ALPHA", "WILL", "LIST", "LAUNCH", "REMOVE", "DELIST", "DELIS",
    "DELISTING", "MARGIN", "LOANS", "FUTURES", "SPOT", "EARN", "HODLER",
    "AIRDROPS", "AIRDROP", "WITH", "AND", "ON", "THE", "TO", "FOR", "TAG",
    "SEED", "APPLIED", "INTRODUCING", "USDT", "USD", "FDUSD", "USDC", "NFT",
    "API", "VIP", "BNB", "BSC",
}
CHAIN_CONTEXT_SYMBOLS = {
    "SOL", "BSC", "ETH", "BASE", "ARB", "OP", "BNB", "TRX", "TRON", "AVAX",
    "POLYGON", "MATIC", "SUI", "APT", "TON",
}
CHAIN_SYMBOL_TOKEN_NAMES = {
    "SOL": {"solana"},
    "ETH": {"ethereum"},
    "BNB": {"bnb", "binance coin"},
    "MATIC": {"matic", "polygon"},
    "AVAX": {"avalanche"},
    "APT": {"aptos"},
    "SUI": {"sui"},
    "TON": {"toncoin"},
}
CST = timezone(timedelta(hours=8))
LAUNCH_SUPPORTING_EVIDENCE_MAX_AGE_SEC = 8 * 60 * 60
_LAUNCH_STATE_TRANSIENT_KEYS = frozenset({
    "chart_png_bytes",
    "launch_lifecycle",
    "launch_package",
    "price_action_analysis",
})
_LAUNCH_END_REASON_TEXT = {
    "two_windows_below_watch_score": (
        "连续两个15分钟闭合窗口低于观察阈值，本轮启动跟踪结束"
    ),
    "two_closes_below_breakout": (
        "连续两根15分钟K线收盘跌破本轮有效突破位，本轮启动跟踪结束"
    ),
}


def compact_launch_state_records(
    state: dict[str, Any],
) -> tuple[dict[str, Any], int]:
    """Drop rebuildable launch-analysis payloads from durable JSON state."""

    compacted: dict[str, Any] = {}
    changed = 0
    for symbol, value in state.items():
        if not isinstance(value, dict):
            compacted[symbol] = value
            continue
        record = {
            key: item
            for key, item in value.items()
            if key not in _LAUNCH_STATE_TRANSIENT_KEYS
        }
        if len(record) != len(value):
            changed += 1
        compacted[symbol] = record
    return compacted, changed


def cst_now_text(fmt: str = "%m-%d %H:%M CST") -> str:
    return datetime.now(CST).strftime(fmt)


def launch_end_reason_text(reason: Any) -> str:
    return _LAUNCH_END_REASON_TEXT.get(
        str(reason or ""),
        "启动条件已失效，本轮启动跟踪结束",
    )


def tg_escape(value: Any) -> str:
    return escape(str(value), quote=False)


def tg_bold(value: Any) -> str:
    return f"<b>{tg_escape(value)}</b>"


def tg_quote(title: str) -> str:
    return f"<blockquote><b>{tg_escape(title)}</b></blockquote>"


def launch_funds_direction(spot_active_net_usd: Any, futures_active_net_usd: Any) -> str:
    spot = to_float(spot_active_net_usd)
    futures = to_float(futures_active_net_usd)
    if spot == 0 or futures == 0:
        return "unknown"
    if spot > 0 and futures > 0:
        return "both_buy"
    if spot < 0 and futures < 0:
        return "both_sell"
    if spot > 0 > futures:
        return "divergence_spot_buy_futures_sell"
    return "divergence_spot_sell_futures_buy"


def seconds_text(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def coinglass_tv_url(coin_or_symbol: str) -> str:
    return _coinglass_tv_url(coin_or_symbol)


def coin_link(item: dict[str, Any]) -> str:
    raw = str(item.get("coin") or item.get("symbol") or "")
    return telegram_coin_links(raw)


def pct_cell(value: float, width: int = 7, decimals: int = 1) -> str:
    return f"{value:+.{decimals}f}%".rjust(width)


def score_cell(value: int) -> str:
    return f"{value:>3}分"


def append_metric_row(lines: list[str], item: dict[str, Any], metrics: str) -> None:
    lines.append(coin_link(item))
    lines.append(tg_escape(metrics))


def pct(current: float, previous: float) -> float:
    if previous <= 0:
        return 0.0
    return (current - previous) / previous * 100


def fmt_price(value: float) -> str:
    if value >= 1:
        return f"${value:.3g}"
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6g}"


def fmt_money(value: float) -> str:
    if value >= 1_000_000_000:
        return f"${value / 1_000_000_000:.1f}B"
    if value >= 1_000_000:
        return f"${value / 1_000_000:.0f}M"
    if value >= 1_000:
        return f"${value / 1_000:.0f}K"
    return f"${value:.0f}"


def market_cap_tier(value: float) -> str:
    if value <= 0:
        return "未知市值"
    if value >= 10_000_000_000:
        return "高市值"
    if value >= 1_000_000_000:
        return "中市值"
    return "低市值"


def liquidity_tier(value: float) -> str:
    if value <= 0:
        return "未知流动性"
    if value >= 100_000_000:
        return "高流动性"
    if value >= 20_000_000:
        return "中流动性"
    return "低流动性"


def funding_trend(previous: Optional[float], current: float) -> str:
    if previous is None:
        return "🆕"
    if previous >= 0 and current < 0:
        return "⬇️变负"
    delta = current - previous
    if current < 0 and delta < -0.01:
        return "🔥加速"
    if current < 0 and delta > 0.01:
        return "⬆️回升"
    return "➡️"


def funding_time_text(value_ms: float) -> str:
    if value_ms <= 0:
        return ""
    return datetime.fromtimestamp(value_ms / 1000, CST).strftime("%Y-%m-%d %H:%M:%S")


def funding_interval_hours(value_ms: float) -> int:
    if value_ms <= 0:
        return 0
    hours = int(round(value_ms / 3_600_000))
    return hours if hours > 0 else 0


def funding_interval_label(hours: int) -> str:
    if hours <= 0:
        return "未知周期"
    return f"{hours}H"


def funding_cycle_text(funding_pct: float, interval_hours: int) -> str:
    if interval_hours > 0:
        return f"{funding_pct:+.3f}%/{funding_interval_label(interval_hours)}"
    return f"{funding_pct:+.3f}%"


def funding_extreme_label(funding_pct: float) -> str:
    if funding_pct <= -1.0:
        return "极负"
    if funding_pct <= -0.5:
        return "极负"
    return ""


def funding_interval_transition(history: list[dict[str, Any]], next_time_ms: int = 0) -> dict[str, Any]:
    points = sorted(
        [
            {
                "time": int(to_float(item.get("fundingTime"))),
                "rate": to_float(item.get("fundingRate")) * 100,
            }
            for item in history
            if to_float(item.get("fundingTime")) > 0
        ],
        key=lambda item: item["time"],
    )
    if next_time_ms > 0 and (not points or next_time_ms > points[-1]["time"]):
        points.append({"time": next_time_ms, "rate": 0.0})
    if len(points) < 3:
        return {}
    previous_interval = funding_interval_hours(points[-2]["time"] - points[-3]["time"])
    current_interval = funding_interval_hours(points[-1]["time"] - points[-2]["time"])
    if previous_interval <= 0 or current_interval <= 0:
        return {}
    if current_interval >= previous_interval:
        return {"current_interval_hours": current_interval}
    previous_time = points[-2]["time"]
    current_time = points[-1]["time"]
    return {
        "current_interval_hours": current_interval,
        "previous_interval_hours": previous_interval,
        "previous_funding_time_ms": previous_time,
        "current_funding_time_ms": current_time,
        "transition_text": (
            f"{funding_time_text(previous_time)} {funding_interval_label(previous_interval)}结算一次"
            f" → {funding_time_text(current_time)} {funding_interval_label(current_interval)}结算一次"
        ),
    }


def estimate_sideways_days(klines: list[list[Any]], max_range_pct: float = 80.0) -> int:
    if not klines:
        return 0
    highs: list[float] = []
    lows: list[float] = []
    days = 0
    for kline in reversed(klines):
        high = to_float(kline[2])
        low = to_float(kline[3])
        if high <= 0 or low <= 0:
            continue
        highs.append(high)
        lows.append(low)
        days += 1
        low_all = min(lows)
        high_all = max(highs)
        if low_all > 0 and (high_all - low_all) / low_all * 100 > max_range_pct:
            return max(0, days - 1)
    return days


def score_funding(funding_pct: float) -> int:
    if funding_pct < -0.5:
        return 25
    if funding_pct < -0.1:
        return 22
    if funding_pct < -0.05:
        return 18
    if funding_pct < -0.03:
        return 14
    if funding_pct < -0.01:
        return 10
    if funding_pct < 0:
        return 5
    return 0


def score_mcap(mcap: float, max_score: int = 25) -> int:
    if mcap <= 0:
        return 0
    if 0 < mcap < 50_000_000:
        return max_score
    if mcap < 100_000_000:
        return max_score - 3
    if mcap < 200_000_000:
        return max_score - 5
    if mcap < 300_000_000:
        return max_score - 8
    if mcap < 500_000_000:
        return max_score - 13
    if mcap < 1_000_000_000:
        return max(0, max_score - 18)
    return 0


def score_sideways(days: int, max_score: int = 25) -> int:
    if days >= 120:
        return max_score
    if days >= 90:
        return max_score - 3
    if days >= 75:
        return max_score - 7
    if days >= 60:
        return max_score - 11
    if days >= 45:
        return max_score - 15
    return 0


def score_oi(oi_pct: float, max_score: int = 25) -> int:
    value = abs(oi_pct)
    if value >= 15:
        return max_score
    if value >= 8:
        return max_score - 3
    if value >= 5:
        return max_score - 7
    if value >= 3:
        return max_score - 11
    if value >= 2:
        return max_score - 15
    return 0

class RadarComponent:
    """Shared settings/store context without cross-radar imports."""

    def __init__(self, settings: Settings, store: JsonStore):
        self.settings = settings
        self.store = store

    def _is_excluded_symbol(self, symbol: str) -> bool:
        coin = str(symbol or "").upper().removesuffix("USDT")
        return coin in set(self.settings.excluded_base_assets)
