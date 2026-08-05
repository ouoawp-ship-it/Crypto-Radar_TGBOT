from __future__ import annotations

import re
from html import unescape


TEMPLATE_LABELS = {
    "TG_LAUNCH_ALERT": "启动雷达",
    "TG_FLOW_RADAR": "资金流雷达",
    "TG_FUNDING_ALERT": "资金费率警报",
    "TG_RADAR_SUMMARY": "资金摘要",
    "TG_ANNOUNCEMENT_ALERT": "公告风险",
}
ACTIVE_SIGNAL_TEMPLATE_IDS = frozenset((*TEMPLATE_LABELS, "TG_TEST_MESSAGE"))

SYMBOL_ALIASES = {
    "比特币": "BTC",
    "大饼": "BTC",
    "以太坊": "ETH",
    "以太": "ETH",
    "币安币": "BNB",
    "索拉纳": "SOL",
    "狗狗币": "DOGE",
    "狗狗": "DOGE",
}

SYMBOL_STOP_WORDS = {
    "AI",
    "API",
    "APP",
    "BOT",
    "CST",
    "UTC",
    "WEB",
    "TV",
    "USD",
    "USDT",
    "OI",
    "CVD",
    "K",
    "H",
    "M",
    "VIP",
    "OK",
    "OKX",
    "BYBIT",
    "BITGET",
    "GATE",
    "BINANCE",
    "COINGLASS",
    "COINPAPRIKA",
}


def normalize_symbol(value: str) -> str:
    symbol = re.sub(r"[^A-Za-z0-9]", "", value or "").upper()
    if not symbol:
        raise ValueError("币种不能为空")
    if symbol.endswith("USD") and not symbol.endswith("USDT"):
        symbol = f"{symbol}T"
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    if not re.fullmatch(r"[A-Z0-9]{3,30}", symbol):
        raise ValueError("币种格式不正确")
    return symbol


def format_price(value: float | None) -> str:
    if value is None:
        return "暂无"
    if value >= 1000:
        return f"${value:,.2f}"
    if value >= 1:
        return f"${value:.4f}".rstrip("0").rstrip(".")
    if value < 0.000001:
        return f"${value:.12f}".rstrip("0").rstrip(".")
    return f"${value:.8f}".rstrip("0").rstrip(".")


def clean_signal_text(text: str) -> str:
    cleaned = unescape(str(text or ""))
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 \2", cleaned)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"[*_#]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _signal_visible_text(text: str) -> str:
    """Return user-visible signal text without link targets or markup."""

    cleaned = unescape(str(text or ""))
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\[([^\]]+)\]\((?:[^()]|\([^)]*\))*\)", r"\1", cleaned)
    cleaned = re.sub(r"https?://[^\s<>'\"]+", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"`([^`]*)`", r"\1", cleaned)
    cleaned = re.sub(r"[*_#]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip()


def _safe_normalize_symbol(value: str) -> str:
    try:
        symbol = normalize_symbol(value)
    except ValueError:
        return ""
    coin = symbol[:-4] if symbol.endswith("USDT") else symbol
    if coin in SYMBOL_STOP_WORDS or len(coin) < 2:
        return ""
    return symbol


def extract_symbols_from_text(text: str) -> list[str]:
    raw = _signal_visible_text(text)
    found: list[str] = []

    def add(value: str) -> None:
        symbol = _safe_normalize_symbol(value)
        if symbol and symbol not in found:
            found.append(symbol)

    for alias, symbol in SYMBOL_ALIASES.items():
        if alias in raw:
            add(symbol)
    for match in re.finditer(r"Binance[_\-/]([A-Za-z0-9]{2,24}USDT)", raw, flags=re.IGNORECASE):
        add(match.group(1))
    for match in re.finditer(r"\b([A-Za-z0-9]{2,24}USDT)\b", raw, flags=re.IGNORECASE):
        add(match.group(1))
    for match in re.finditer(r"\[([A-Za-z][A-Za-z0-9]{1,20})\]", raw):
        add(match.group(1))
    if not found:
        for match in re.finditer(r"\b([A-Za-z][A-Za-z0-9]{1,20})\b", raw):
            token = match.group(1).upper()
            if token not in SYMBOL_STOP_WORDS:
                add(token)
                if found:
                    break
    return found[:12]


def signal_event_template_label(template_id: str) -> str:
    return TEMPLATE_LABELS.get(str(template_id or ""), str(template_id or "未知信号"))
