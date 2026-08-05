from __future__ import annotations

from typing import Any, Mapping


CORE_CRYPTO = frozenset({"BTC", "ETH"})
LARGE_CRYPTO = frozenset({
    "ADA",
    "AVAX",
    "BCH",
    "BNB",
    "DOGE",
    "DOT",
    "HBAR",
    "LINK",
    "LTC",
    "SOL",
    "TON",
    "TRX",
    "XLM",
    "XRP",
})

THEME_LABELS = {
    "AI": "AI",
    "ALPHA": "Alpha",
    "DEFI": "DeFi",
    "GAMING": "游戏",
    "INFRASTRUCTURE": "基础设施",
    "LAYER-1": "Layer-1",
    "LAYER-2": "Layer-2",
    "MEME": "Meme",
    "METAVERSE": "元宇宙",
    "NFT": "NFT",
    "PAYMENT": "支付",
    "POW": "PoW",
    "RWA": "RWA",
    "STORAGE": "存储",
}


def _tradfi(
    asset_class: str,
    asset_subclass: str,
    label: str,
    short: str,
) -> tuple[str, str, str, str]:
    return asset_class, asset_subclass, label, short


# Reviewed Binance Futures TradFi products. Exchange metadata remains the first
# source for new listings; this map supplies the economic meaning that the
# generic exchangeInfo fields do not consistently expose.
TRADFI_PRODUCTS = {
    "XAU": _tradfi("commodity", "precious_metal", "传统金融 · 贵金属 · 黄金", "GOLD"),
    "XAG": _tradfi("commodity", "precious_metal", "传统金融 · 贵金属 · 白银", "SILVER"),
    "XPT": _tradfi("commodity", "precious_metal", "传统金融 · 贵金属 · 铂金", "PLATINUM"),
    "XPD": _tradfi("commodity", "precious_metal", "传统金融 · 贵金属 · 钯金", "PALLADIUM"),
    "CL": _tradfi("commodity", "energy", "传统金融 · 能源 · 原油", "CRUDE OIL"),
    "NATGAS": _tradfi("commodity", "energy", "传统金融 · 能源 · 天然气", "NAT GAS"),
    "COPPER": _tradfi("commodity", "industrial_metal", "传统金融 · 工业金属 · 铜", "COPPER"),
    "QQQ": _tradfi("etf_index", "broad_market_etf", "传统金融 · 指数ETF · 纳斯达克100", "ETF INDEX"),
    "SPY": _tradfi("etf_index", "broad_market_etf", "传统金融 · 指数ETF · 标普500", "ETF INDEX"),
    "EWY": _tradfi("etf_index", "regional_etf", "传统金融 · 区域ETF · 韩国", "ETF INDEX"),
    "EWJ": _tradfi("etf_index", "regional_etf", "传统金融 · 区域ETF · 日本", "ETF INDEX"),
    "TQQQ": _tradfi("leveraged_etf", "leveraged_index_etf", "传统金融 · 杠杆ETF · 纳斯达克100多头", "LEVERAGED ETF"),
    "SQQQ": _tradfi("leveraged_etf", "inverse_index_etf", "传统金融 · 反向杠杆ETF · 纳斯达克100", "LEVERAGED ETF"),
    "SOXL": _tradfi("leveraged_etf", "leveraged_sector_etf", "传统金融 · 杠杆ETF · 半导体多头", "LEVERAGED ETF"),
    "SOXS": _tradfi("leveraged_etf", "inverse_sector_etf", "传统金融 · 反向杠杆ETF · 半导体", "LEVERAGED ETF"),
}

for _symbol, _sector in {
    "MSTR": "加密金融",
    "COIN": "加密金融",
    "HOOD": "金融科技",
    "CRCL": "金融科技",
    "PAYP": "金融科技",
    "TSLA": "大型科技",
    "AMZN": "大型科技",
    "META": "大型科技",
    "GOOGL": "大型科技",
    "AAPL": "大型科技",
    "MSFT": "大型科技",
    "NVDA": "半导体",
    "MU": "半导体",
    "SNDK": "半导体",
    "TSM": "半导体",
    "AVGO": "半导体",
    "INTC": "半导体",
    "PLTR": "数据与AI",
    "BABA": "全球市场",
    "SPCX": "大型科技",
}.items():
    TRADFI_PRODUCTS[_symbol] = _tradfi(
        "equity",
        "single_stock",
        f"传统金融 · 美股个股 · {_sector}",
        "EQUITY",
    )


def _base_asset(symbol: str, metadata: Mapping[str, Any]) -> str:
    base = str(metadata.get("baseAsset") or "").strip().upper()
    if base:
        return base
    normalized = str(symbol or "").strip().upper()
    for quote in ("USDT", "USDC", "FDUSD", "USD"):
        if normalized.endswith(quote) and len(normalized) > len(quote):
            return normalized[: -len(quote)]
    return normalized


def _metadata_words(metadata: Mapping[str, Any]) -> set[str]:
    values = metadata.get("underlyingSubType") or []
    if not isinstance(values, (list, tuple, set)):
        values = [values]
    words = {
        str(value or "").strip().upper().replace("_", "-")
        for value in values
        if str(value or "").strip()
    }
    underlying_type = str(metadata.get("underlyingType") or "").strip().upper()
    if underlying_type:
        words.add(underlying_type.replace("_", "-"))
    return words


def classify_binance_instrument(
    symbol: str,
    metadata: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a deterministic, display-safe classification for one contract."""

    info = metadata if isinstance(metadata, Mapping) else {}
    base = _base_asset(symbol, info)
    words = _metadata_words(info)

    if base in TRADFI_PRODUCTS:
        asset_class, subclass, label, short = TRADFI_PRODUCTS[base]
        return {
            "instrument_type": "usdt_perpetual",
            "asset_family": "tradfi",
            "asset_class": asset_class,
            "asset_subclass": subclass,
            "asset_category_label": label,
            "asset_category_short": short,
            "asset_theme_tags": [],
            "asset_category_source": "reviewed_binance_product",
        }

    if words & {"TOKENIZED-STOCK", "TOKENIZED-EQUITY", "BSTOCK"}:
        return {
            "instrument_type": "tokenized_equity",
            "asset_family": "tradfi",
            "asset_class": "equity",
            "asset_subclass": "tokenized_stock",
            "asset_category_label": "传统金融 · 代币化股票",
            "asset_category_short": "TOKENIZED STOCK",
            "asset_theme_tags": [],
            "asset_category_source": "binance_exchange_info",
        }

    metadata_classes = (
        ({"STOCK", "EQUITY"}, "equity", "single_stock", "传统金融 · 个股永续", "EQUITY"),
        ({"ETF"}, "etf_index", "etf", "传统金融 · ETF永续", "ETF"),
        ({"COMMODITY", "METAL", "ENERGY"}, "commodity", "other", "传统金融 · 大宗商品", "COMMODITY"),
        ({"FOREX", "FX"}, "forex", "currency_pair", "传统金融 · 外汇", "FOREX"),
    )
    for expected, asset_class, subclass, label, short in metadata_classes:
        if words & expected:
            return {
                "instrument_type": "usdt_perpetual",
                "asset_family": "tradfi",
                "asset_class": asset_class,
                "asset_subclass": subclass,
                "asset_category_label": label,
                "asset_category_short": short,
                "asset_theme_tags": [],
                "asset_category_source": "binance_exchange_info",
            }

    themes = [
        THEME_LABELS[word]
        for word in sorted(words)
        if word in THEME_LABELS
    ]
    if "INDEX" in words:
        tier = "加密指数"
        subclass = "crypto_index"
        short = "CRYPTO INDEX"
    elif base in CORE_CRYPTO:
        tier = "核心主流"
        subclass = "core_crypto"
        short = "CRYPTO CORE"
    elif base in LARGE_CRYPTO:
        tier = "主流加密"
        subclass = "large_crypto"
        short = "CRYPTO MAJOR"
    else:
        tier = "山寨币"
        subclass = "altcoin"
        short = "CRYPTO ALT"
    suffix = f" · {' / '.join(themes[:2])}" if themes else ""
    return {
        "instrument_type": "usdt_perpetual",
        "asset_family": "crypto",
        "asset_class": "crypto",
        "asset_subclass": subclass,
        "asset_category_label": f"加密货币 · {tier}{suffix}",
        "asset_category_short": short,
        "asset_theme_tags": themes,
        "asset_category_source": (
            "binance_exchange_info"
            if themes or "INDEX" in words
            else "reviewed_crypto_tier"
            if base in CORE_CRYPTO or base in LARGE_CRYPTO
            else "symbol_fallback"
        ),
    }


__all__ = ["classify_binance_instrument"]
