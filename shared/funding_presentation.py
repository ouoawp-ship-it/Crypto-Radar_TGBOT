"""Shared funding presentation helpers used by multiple radar packages."""

from __future__ import annotations

from html import escape
import re
import unicodedata
from typing import Any

from config import Settings
from .funding_sources import (
    funding_cycle_text,
    funding_extreme_label,
    funding_last_settlement_text,
    funding_settlement_period_text,
    to_float,
    to_int,
)


def funding_rate_label(funding_pct: float, settings: Settings) -> str:
    if funding_pct <= settings.funding_alert_super_negative_pct:
        return "超极负"
    if funding_pct <= settings.funding_alert_extreme_negative_pct:
        return "极负"
    if funding_pct >= abs(settings.funding_alert_super_negative_pct):
        return "超极正"
    if funding_pct >= settings.funding_alert_extreme_positive_pct:
        return "极正"
    return funding_extreme_label(funding_pct)


def short_funding_time(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "-"
    match = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", text)
    if match:
        return f"{match.group(2)}-{match.group(3)} {match.group(4)}:{match.group(5)}"
    return text[:14]


def _display_width(value: Any) -> int:
    return sum(
        2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        for char in str(value or "")
    )


def _display_ljust(value: Any, width: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    result: list[str] = []
    used = 0
    for char in text:
        char_width = 2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1
        if used + char_width > width:
            break
        result.append(char)
        used += char_width
    return "".join(result) + (" " * max(0, width - used))


FUNDING_TABLE_COLUMNS = (
    ("交易所", 8),
    ("费率/周期", 18),
    ("上次结算", 12),
    ("本次周期", 16),
    ("下次结算", 12),
)


def _funding_table_row(values: list[Any]) -> str:
    padded = [
        _display_ljust(value, width)
        for value, (_, width) in zip(values, FUNDING_TABLE_COLUMNS)
    ]
    return "  ".join(padded).rstrip()


def funding_table_lines(
    rows: list[dict[str, Any]],
    settings: Settings,
) -> list[str]:
    lines = [_funding_table_row([label for label, _ in FUNDING_TABLE_COLUMNS])]
    for row in rows:
        exchange = str(row.get("exchange") or "Unknown").strip()
        funding_pct = to_float(row.get("funding_pct"))
        interval_hours = to_int(row.get("interval_hours"))
        rate = funding_cycle_text(funding_pct, interval_hours)
        label = funding_rate_label(funding_pct, settings)
        last_time = short_funding_time(funding_last_settlement_text(row))
        period = funding_settlement_period_text(row)
        next_time = short_funding_time(str(row.get("next_funding_time") or ""))
        lines.append(_funding_table_row([
            exchange,
            f"{rate} {label}".strip(),
            last_time,
            period,
            next_time,
        ]))
    return lines


def funding_table(rows: list[dict[str, Any]], settings: Settings) -> str:
    text = "\n".join(funding_table_lines(rows, settings))
    return "<pre>" + escape(text, quote=False) + "</pre>"
