from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Sequence

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt

from .structure_radar import Candle, StructureSignal


def _fmt_pct(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:+.1f}%"


def _fmt_ratio(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.2f}x"


def _chart_filename(signal: StructureSignal, timestamp: datetime | None = None) -> str:
    stamp = (timestamp or datetime.now()).strftime("%Y%m%d_%H%M")
    return f"structure_{signal.symbol}_{signal.interval}_{stamp}.png"


def generate_structure_chart(
    signal: StructureSignal,
    candles: Sequence[Candle],
    output_dir: str | Path,
    timestamp: datetime | None = None,
) -> str:
    if not candles:
        raise ValueError("candles is empty")
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / _chart_filename(signal, timestamp)

    times = [datetime.fromtimestamp(c.open_time / 1000) for c in candles]
    opens = [c.open for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    volumes = [c.quote_volume or c.volume for c in candles]
    x_values = mdates.date2num(times)
    width = max(0.002, (x_values[-1] - x_values[0]) / max(1, len(x_values)) * 0.65) if len(x_values) > 1 else 0.004

    fig, (price_ax, volume_ax) = plt.subplots(
        2,
        1,
        figsize=(11, 6.5),
        sharex=True,
        gridspec_kw={"height_ratios": [3.2, 1]},
    )
    fig.patch.set_facecolor("#f8fafc")
    price_ax.set_facecolor("#ffffff")
    volume_ax.set_facecolor("#ffffff")

    for idx, x in enumerate(x_values):
        color = "#059669" if closes[idx] >= opens[idx] else "#dc2626"
        price_ax.vlines(x, lows[idx], highs[idx], color=color, linewidth=1.1)
        body_low = min(opens[idx], closes[idx])
        body_high = max(opens[idx], closes[idx])
        body_height = max(body_high - body_low, max(closes) * 0.0008)
        price_ax.add_patch(
            plt.Rectangle(
                (x - width / 2, body_low),
                width,
                body_height,
                facecolor=color,
                edgecolor=color,
                linewidth=0.8,
                alpha=0.85,
            )
        )
        volume_ax.bar(x, volumes[idx], width=width, color=color, alpha=0.55)

    price_ax.axhline(signal.box_high, color="#2563eb", linestyle="--", linewidth=1.4, label="box high")
    price_ax.axhline(signal.box_low, color="#7c3aed", linestyle="--", linewidth=1.4, label="box low")
    price_ax.axhline(signal.price, color="#111827", linestyle="-", linewidth=1.0, alpha=0.8, label="current")
    price_ax.fill_between(
        [x_values[0], x_values[-1]],
        [signal.box_low, signal.box_low],
        [signal.box_high, signal.box_high],
        color="#dbeafe",
        alpha=0.20,
    )

    price_ax.set_title(
        f"{signal.symbol} {signal.interval} | {signal.signal_type} | {signal.level} {signal.score:.0f}",
        loc="left",
        fontsize=14,
        fontweight="bold",
    )
    note = (
        f"dist high {signal.distance_to_high_pct:+.2f}%\n"
        f"dist low {signal.distance_to_low_pct:+.2f}%\n"
        f"vol {_fmt_ratio(signal.volume_ratio)}\n"
        f"OI1h {_fmt_pct(signal.oi_change_pct_1h)}"
    )
    price_ax.text(
        0.012,
        0.98,
        note,
        transform=price_ax.transAxes,
        va="top",
        ha="left",
        fontsize=9,
        bbox={"boxstyle": "round,pad=0.35", "facecolor": "#ffffff", "edgecolor": "#cbd5e1", "alpha": 0.92},
    )
    price_ax.grid(True, color="#e5e7eb", linewidth=0.7)
    volume_ax.grid(True, axis="y", color="#e5e7eb", linewidth=0.7)
    volume_ax.set_ylabel("Volume")
    price_ax.set_ylabel("Price")
    volume_ax.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return str(path)
