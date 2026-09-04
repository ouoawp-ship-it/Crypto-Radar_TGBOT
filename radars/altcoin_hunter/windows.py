"""Six finite rolling windows derived exclusively from committed minute buckets."""
from __future__ import annotations

from collections import OrderedDict
from decimal import Decimal
from math import isfinite
from typing import Any

from .aggregation import MINUTE_MS, MinuteBucket, SeriesKey

WINDOW_MINUTES = (1, 3, 5, 15, 30, 60)


class RollingWindowEngine:
    def __init__(self, *, max_instruments: int = 1024, retention_minutes: int = 120) -> None:
        if max_instruments < 1 or retention_minutes < 60 or retention_minutes > 120:
            raise ValueError("window retention must be 60..120 minutes and capacity positive")
        self.max_instruments = max_instruments
        self.retention_minutes = retention_minutes
        self._rows: dict[SeriesKey, OrderedDict[int, MinuteBucket]] = {}

    @staticmethod
    def _key(bucket: MinuteBucket) -> SeriesKey:
        return bucket.source, bucket.exchange, bucket.market, bucket.instrument_id

    def ingest_committed(self, batch: Any) -> int:
        # The storage layer is the only production constructor of this receipt.
        # A PendingBatch or an arbitrary list cannot enter the window engine.
        from .storage import CommittedBatch
        if not isinstance(batch, CommittedBatch):
            raise TypeError("windows require a CommittedBatch returned by storage")
        incoming = tuple(b if isinstance(b, MinuteBucket) else MinuteBucket.from_dict(b) for b in batch.buckets)
        added = {self._key(bucket) for bucket in incoming} - set(self._rows)
        if len(self._rows) + len(added) > self.max_instruments:
            raise ValueError("window instrument capacity exceeded")
        accepted = 0
        for bucket in incoming:
            key = self._key(bucket)
            rows = self._rows.setdefault(key, OrderedDict())
            old = rows.get(bucket.start_ms)
            if old is not None:
                if old != bucket:
                    raise ValueError("committed bucket revision is not allowed")
                continue
            latest = max(rows, default=bucket.start_ms)
            if bucket.start_ms < latest - (self.retention_minutes - 1) * MINUTE_MS:
                continue
            rows[bucket.start_ms] = bucket
            if bucket.start_ms < latest:
                rows = OrderedDict(sorted(rows.items()))
                self._rows[key] = rows
            latest = max(rows)
            cutoff = latest - (self.retention_minutes - 1) * MINUTE_MS
            while rows and (next(iter(rows)) < cutoff or len(rows) > self.retention_minutes):
                rows.popitem(last=False)
            accepted += 1
        return accepted

    def query(
        self, *, source: str, exchange: str, market: str, instrument_id: str,
        end_ms: int, window_minutes: int,
    ) -> dict[str, Any]:
        if window_minutes not in WINDOW_MINUTES:
            raise ValueError("unsupported window; use 1,3,5,15,30,60 minutes")
        if isinstance(end_ms, bool) or not isinstance(end_ms, int) or end_ms % MINUTE_MS:
            raise ValueError("window end must be an explicit closed minute boundary")
        key = source, exchange, market, instrument_id
        start = end_ms - window_minutes * MINUTE_MS
        rows = self._rows.get(key, {})
        starts = range(start, end_ms, MINUTE_MS)
        missing = tuple(at for at in starts if at not in rows)
        buckets = tuple(rows[at] for at in range(start, end_ms, MINUTE_MS) if at in rows)
        reasons: set[str] = set()
        if missing:
            reasons.add("missing_minutes")
        epochs = {epoch for bucket in buckets for epoch in bucket.connection_epochs}
        if len(epochs) > 1:
            reasons.add("connection_epoch_changed")
        quote_currencies = {bucket.quote_currency for bucket in buckets}
        if len(quote_currencies) > 1:
            reasons.add("mixed_quote_currency")
        for bucket in buckets:
            reasons.update(bucket.quality_flags)
            if not bucket.complete:
                reasons.add("incomplete_minute")
        # Bucket presence and observed time answer different questions. A bucket
        # containing ten seconds of verified continuity counts as one observed
        # minute, but contributes only ten seconds to time coverage. Full time
        # coverage also does not override sequence/epoch or other quality gates.
        observed_coverage_ms = sum(bucket.coverage_ms for bucket in buckets)
        expected_coverage_ms = window_minutes * MINUTE_MS
        complete_minutes = sum(bucket.complete for bucket in buckets)
        result: dict[str, Any] = {
            "source": source, "exchange": exchange, "market": market,
            "instrument_id": instrument_id, "start_ms": start, "end_ms": end_ms,
            "window_minutes": window_minutes, "expected_minutes": window_minutes,
            "observed_minutes": len(buckets),
            "observed_minute_ratio": len(buckets) / window_minutes,
            "observed_coverage_ms": observed_coverage_ms,
            "expected_coverage_ms": expected_coverage_ms,
            "time_coverage_ratio": observed_coverage_ms / expected_coverage_ms,
            "complete_minutes": complete_minutes,
            "incomplete_minutes": len(buckets) - complete_minutes,
            "missing_minutes": missing, "quality_flags": tuple(sorted(reasons)),
            "quality_status": "incomplete" if reasons else "complete",
            "complete": not reasons, "connection_epochs": tuple(sorted(epochs)),
            "price_open": None, "price_high": None, "price_low": None, "price_close": None,
            "price_return_ratio": None, "buy_quote": None, "sell_quote": None,
            "quote_volume": None, "delta_quote": None, "taker_buy_ratio": None,
            "delta_ratio": None, "trade_count": None,
        }
        if reasons or not buckets:
            return result
        buy = sum((Decimal(bucket.buy_quote) for bucket in buckets), Decimal(0))
        sell = sum((Decimal(bucket.sell_quote) for bucket in buckets), Decimal(0))
        volume = buy + sell
        first, last = Decimal(buckets[0].price_open), Decimal(buckets[-1].price_close)
        metrics = {
            "price_open": str(first), "price_close": str(last),
            "price_high": str(max(Decimal(b.price_high) for b in buckets)),
            "price_low": str(min(Decimal(b.price_low) for b in buckets)),
            "price_return_ratio": str(last / first - 1),
            "buy_quote": str(buy), "sell_quote": str(sell), "quote_volume": str(volume),
            "delta_quote": str(buy - sell),
            "taker_buy_ratio": str(buy / volume) if volume else None,
            "delta_ratio": str((buy - sell) / volume) if volume else None,
            "trade_count": sum(b.trade_count for b in buckets),
            "quote_currency": buckets[0].quote_currency,
        }
        if any(not isfinite(float(value)) for name, value in metrics.items()
               if name not in {"quote_currency", "trade_count"} and value is not None):
            result.update({"complete": False, "quality_status": "incomplete", "quality_flags": ("numeric_overflow",)})
            return result
        result.update(metrics)
        return result

    def query_all(self, *, source: str, exchange: str, market: str, instrument_id: str,
                  end_ms: int) -> dict[int, dict[str, Any]]:
        return {minutes: self.query(source=source, exchange=exchange, market=market,
                                    instrument_id=instrument_id, end_ms=end_ms, window_minutes=minutes)
                for minutes in WINDOW_MINUTES}

    def stats(self) -> dict[str, int]:
        return {"instrument_count": len(self._rows), "minute_buckets": sum(len(rows) for rows in self._rows.values()),
                "retention_minutes": self.retention_minutes}


RollingWindows = RollingWindowEngine
