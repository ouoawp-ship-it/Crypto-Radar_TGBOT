from __future__ import annotations

from html import escape
from urllib.parse import quote


def binance_usdt_symbol(coin_or_symbol: str) -> str:
    """Return the normalized Binance USDT pair used by signal links."""

    symbol = str(coin_or_symbol or "").strip().upper()
    if symbol and not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    return symbol


def coinglass_tv_url(coin_or_symbol: str) -> str:
    symbol = binance_usdt_symbol(coin_or_symbol)
    return f"https://www.coinglass.com/tv/zh/Binance_{quote(symbol, safe='')}"


def tradingview_tv_url(coin_or_symbol: str) -> str:
    symbol = binance_usdt_symbol(coin_or_symbol)
    # Radar signals track Binance USDT perpetuals, whose TradingView suffix is .P.
    tv_symbol = quote(f"BINANCE:{symbol}.P", safe="")
    return f"https://www.tradingview.com/chart/?symbol={tv_symbol}"


def telegram_coin_links(coin_or_symbol: str) -> str:
    """Render CoinGlass, copyable pair text and a direct TradingView link."""

    symbol = binance_usdt_symbol(coin_or_symbol)
    coin = symbol[:-4] if symbol.endswith("USDT") else symbol
    safe_coin = escape(coin, quote=False)
    safe_symbol = escape(symbol, quote=False)
    coinglass_url = escape(coinglass_tv_url(symbol), quote=True)
    tradingview_url = escape(tradingview_tv_url(symbol), quote=True)
    return (
        f'<a href="{coinglass_url}"><b>{safe_coin}</b></a>'
        f' · 📋 <code>{safe_symbol}</code>'
        f' · <a href="{tradingview_url}"><b>TV</b></a>'
    )
