from __future__ import annotations

from typing import Any

from .numbers import normalize_timestamp_ms, to_float
from .time_windows import ClosedWindow


def kline_cvd_flow_info(
    klines: list[list[Any]],
    window: ClosedWindow | None = None,
) -> tuple[float, float, float, bool, int]:
    taker_buy_total = 0.0
    taker_sell_total = 0.0
    count = 0
    for kline in klines:
        if not isinstance(kline, list) or len(kline) < 11:
            continue
        if window is not None:
            timestamp = normalize_timestamp_ms(kline[0])
            if (
                not timestamp
                or timestamp < window.start_ms
                or timestamp >= window.end_ms
            ):
                continue
        quote_volume = to_float(kline[7], default=float("nan"))
        taker_buy_quote = to_float(kline[10], default=float("nan"))
        if quote_volume != quote_volume or taker_buy_quote != taker_buy_quote:
            continue
        if (
            quote_volume < 0
            or taker_buy_quote < 0
            or taker_buy_quote > quote_volume
        ):
            continue
        taker_buy_total += taker_buy_quote
        taker_sell_total += quote_volume - taker_buy_quote
        count += 1
    return (
        taker_buy_total - taker_sell_total,
        taker_buy_total,
        taker_sell_total,
        count > 0,
        count,
    )
