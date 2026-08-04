from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


CST = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class ClosedWindow:
    start: datetime
    end: datetime
    delay_sec: int
    interval_sec: int

    @property
    def start_ms(self) -> int:
        return int(self.start.timestamp() * 1000)

    @property
    def end_ms(self) -> int:
        return int(self.end.timestamp() * 1000)

    @property
    def interval_ms(self) -> int:
        return int(self.interval_sec * 1000)

    def label(self) -> str:
        if self.start.date() == self.end.date():
            return f"{self.start.strftime('%m-%d %H:%M')}-{self.end.strftime('%H:%M')} CST"
        return f"{self.start.strftime('%m-%d %H:%M')}-{self.end.strftime('%m-%d %H:%M')} CST"


def _floor_local_interval(moment: datetime, interval_sec: int, tz: timezone = CST) -> datetime:
    interval_sec = max(60, int(interval_sec))
    local = moment.astimezone(tz)
    day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    seconds = int((local - day_start).total_seconds())
    bucket = seconds // interval_sec
    return day_start + timedelta(seconds=bucket * interval_sec)


def closed_window(
    *,
    now: datetime | None = None,
    interval_sec: int = 3600,
    delay_sec: int = 300,
    tz: timezone = CST,
) -> ClosedWindow:
    current = now or datetime.now(tz)
    reference = current.astimezone(tz) - timedelta(seconds=max(0, int(delay_sec)))
    end = _floor_local_interval(reference, interval_sec, tz)
    start = end - timedelta(seconds=max(60, int(interval_sec)))
    return ClosedWindow(start=start, end=end, delay_sec=max(0, int(delay_sec)), interval_sec=max(60, int(interval_sec)))


def next_closed_window_epoch(
    value: float,
    *,
    interval_sec: int = 3600,
    delay_sec: int = 300,
    tz: timezone = CST,
) -> float:
    interval_sec = max(60, int(interval_sec))
    delay_sec = max(0, int(delay_sec))
    current = datetime.fromtimestamp(value, tz)
    boundary = _floor_local_interval(current, interval_sec, tz)
    candidate = boundary + timedelta(seconds=delay_sec)
    if current.timestamp() < candidate.timestamp():
        return candidate.timestamp()
    return (boundary + timedelta(seconds=interval_sec + delay_sec)).timestamp()

