from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from threading import local
from time import perf_counter
from typing import Any

from .config import Settings
from .data_sources import HttpClient


CST = timezone(timedelta(hours=8))
DEFAULT_FUNDING_EXCHANGES = ("BINANCE", "OKX", "BYBIT", "BITGET", "GATE")


def to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def funding_time_text(value_ms: int | float) -> str:
    if value_ms <= 0:
        return ""
    return datetime.fromtimestamp(float(value_ms) / 1000, CST).strftime("%Y-%m-%d %H:%M:%S")


def funding_interval_hours(value_ms: int | float) -> int:
    if value_ms <= 0:
        return 0
    hours = int(round(float(value_ms) / 3_600_000))
    return hours if hours > 0 else 0


def funding_interval_label(hours: int) -> str:
    return f"{hours}H" if hours > 0 else "未知周期"


def funding_latest_time_ms(points: list[dict[str, Any]]) -> int:
    times = sorted(to_int(item.get("time_ms")) for item in points if to_int(item.get("time_ms")) > 0)
    return times[-1] if times else 0


def funding_settlement_period_text(row: dict[str, Any]) -> str:
    previous_interval = to_int(row.get("previous_interval_hours"))
    current_interval = to_int(row.get("current_interval_hours")) or to_int(row.get("interval_hours"))
    if previous_interval > 0 and current_interval > 0 and previous_interval != current_interval:
        return f"{funding_interval_label(previous_interval)}→{funding_interval_label(current_interval)}"
    return funding_interval_label(current_interval)


def funding_last_settlement_text(row: dict[str, Any]) -> str:
    explicit = str(row.get("last_funding_time") or row.get("previous_funding_time") or "").strip()
    if explicit:
        return explicit
    last_ms = to_int(row.get("last_funding_time_ms") or row.get("previous_funding_time_ms"))
    if last_ms <= 0:
        next_ms = to_int(row.get("next_funding_time_ms"))
        interval_hours = to_int(row.get("current_interval_hours")) or to_int(row.get("interval_hours"))
        if next_ms > 0 and interval_hours > 0:
            last_ms = next_ms - interval_hours * 3_600_000
    return funding_time_text(last_ms) if last_ms > 0 else ""


def funding_cycle_text(funding_pct: float, interval_hours: int) -> str:
    if interval_hours > 0:
        return f"{funding_pct:+.3f}%/{funding_interval_label(interval_hours)}"
    return f"{funding_pct:+.3f}%"


def funding_extreme_label(funding_pct: float) -> str:
    return "极负" if funding_pct <= -0.5 else ""


def funding_interval_transition(points: list[dict[str, Any]], next_time_ms: int = 0) -> dict[str, Any]:
    normalized = sorted(
        [
            {"time": to_int(item.get("time_ms")), "rate_pct": to_float(item.get("rate_pct"))}
            for item in points
            if to_int(item.get("time_ms")) > 0
        ],
        key=lambda item: item["time"],
    )
    if next_time_ms > 0 and (not normalized or next_time_ms > normalized[-1]["time"]):
        normalized.append({"time": next_time_ms, "rate_pct": 0.0})
    if len(normalized) < 3:
        return {}

    previous_interval = funding_interval_hours(normalized[-2]["time"] - normalized[-3]["time"])
    current_interval = funding_interval_hours(normalized[-1]["time"] - normalized[-2]["time"])
    if previous_interval <= 0 or current_interval <= 0:
        return {}
    if current_interval >= previous_interval:
        return {"current_interval_hours": current_interval}

    previous_time = normalized[-2]["time"]
    current_time = normalized[-1]["time"]
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


def infer_interval_hours(points: list[dict[str, Any]], next_time_ms: int = 0) -> int:
    times = sorted(to_int(item.get("time_ms")) for item in points if to_int(item.get("time_ms")) > 0)
    if next_time_ms > 0 and (not times or next_time_ms > times[-1]):
        times.append(next_time_ms)
    if len(times) < 2:
        return 0
    return funding_interval_hours(times[-1] - times[-2])


def canonical_exchange_name(name: str) -> str:
    normalized = str(name or "").strip().upper().replace("-", "").replace("_", "")
    aliases = {
        "BIANCA": "BINANCE",
        "BINANCE": "BINANCE",
        "OKX": "OKX",
        "BYBIT": "BYBIT",
        "BITGET": "BITGET",
        "GATE": "GATE",
        "GATEIO": "GATE",
    }
    return aliases.get(normalized, normalized)


def exchange_symbol(symbol: str, exchange: str) -> str:
    raw = str(symbol or "").upper().strip()
    base = raw[:-4] if raw.endswith("USDT") else raw
    name = canonical_exchange_name(exchange)
    if name == "OKX":
        return f"{base}-USDT-SWAP"
    if name == "GATE":
        return f"{base}_USDT"
    return f"{base}USDT"


class MultiExchangeFundingClient:
    def __init__(self, settings: Settings, http: HttpClient):
        self.settings = settings
        self.http = http
        self.last_batch_metrics: dict[str, Any] = {}
        self._request_context = local()

    def snapshot(self, symbol: str, include_history: bool = True) -> list[dict[str, Any]]:
        normalized = str(symbol or "").upper().strip()
        if not normalized:
            return []
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return self.snapshot_many([normalized], include_history=include_history).get(normalized, [])
        # A synchronous call made by an async host cannot nest asyncio.run(). Keep
        # the legacy blocking behavior for that uncommon compatibility path.
        rows: list[dict[str, Any]] = []
        for exchange in self._enabled_exchanges():
            row = self._snapshot_one(normalized, exchange, include_history=include_history)
            if row:
                rows.append(row)
        return rows

    def snapshot_many(
        self,
        symbols: list[str] | tuple[str, ...],
        include_history: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        """Fetch a bounded symbol batch while preserving the synchronous public API."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            pass
        else:
            raise RuntimeError(
                "snapshot_many() cannot run inside an active event loop; "
                "await snapshot_many_async() instead"
            )
        return asyncio.run(self.snapshot_many_async(symbols, include_history=include_history))

    async def snapshot_many_async(
        self,
        symbols: list[str] | tuple[str, ...],
        include_history: bool = True,
    ) -> dict[str, list[dict[str, Any]]]:
        normalized_symbols = self._normalized_symbols(symbols)
        exchanges = self._enabled_exchanges()
        started = perf_counter()
        if not normalized_symbols or not exchanges:
            self.last_batch_metrics = self._batch_metrics(0, 0, 0, 0.0, 0.0, {}, {})
            return {symbol: [] for symbol in normalized_symbols}

        rows_by_symbol: dict[str, dict[str, dict[str, Any]]] = {
            symbol: {} for symbol in normalized_symbols
        }
        jobs = iter(
            (symbol, exchange)
            for symbol in normalized_symbols
            for exchange in exchanges
        )
        total_jobs = len(normalized_symbols) * len(exchanges)
        worker_count = min(self._scan_concurrency(), total_jobs)
        active = 0
        peak_active = 0
        duration_total = 0.0
        succeeded: dict[str, int] = {}
        failed: dict[str, int] = {}

        async def worker() -> None:
            nonlocal active, peak_active, duration_total
            while True:
                try:
                    symbol, exchange = next(jobs)
                except StopIteration:
                    return
                request_started = perf_counter()
                active += 1
                peak_active = max(peak_active, active)
                try:
                    row = await asyncio.to_thread(
                        self._snapshot_one,
                        symbol,
                        exchange,
                        include_history,
                    )
                except Exception:
                    row = {}
                finally:
                    active -= 1
                    duration_total += perf_counter() - request_started
                if row:
                    rows_by_symbol[symbol][exchange] = row
                    succeeded[exchange] = succeeded.get(exchange, 0) + 1
                else:
                    failed[exchange] = failed.get(exchange, 0) + 1

        workers = [asyncio.create_task(worker()) for _ in range(worker_count)]
        await asyncio.gather(*workers)
        elapsed = perf_counter() - started
        success_count = sum(succeeded.values())
        failure_count = sum(failed.values())
        self.last_batch_metrics = self._batch_metrics(
            total_jobs,
            success_count,
            failure_count,
            elapsed,
            duration_total,
            succeeded,
            failed,
            peak_concurrency=peak_active,
        )
        return {
            symbol: [
                rows_by_symbol[symbol][exchange]
                for exchange in exchanges
                if exchange in rows_by_symbol[symbol]
            ]
            for symbol in normalized_symbols
        }

    def _normalized_symbols(self, symbols: list[str] | tuple[str, ...]) -> tuple[str, ...]:
        limit = max(1, int(getattr(self.settings, "funding_max_symbols_per_batch", 120) or 120))
        result: list[str] = []
        for raw in symbols:
            symbol = str(raw or "").upper().strip()
            if symbol and symbol not in result:
                result.append(symbol)
            if len(result) >= limit:
                break
        return tuple(result)

    def _scan_concurrency(self) -> int:
        configured = int(getattr(self.settings, "funding_scan_concurrency", 8) or 8)
        return min(8, max(6, configured))

    def _batch_metrics(
        self,
        requests: int,
        succeeded: int,
        failed: int,
        elapsed: float,
        duration_total: float,
        successes_by_exchange: dict[str, int],
        failures_by_exchange: dict[str, int],
        peak_concurrency: int = 0,
    ) -> dict[str, Any]:
        return {
            "symbols": requests // max(1, len(self._enabled_exchanges())),
            "exchange_requests": requests,
            "succeeded": succeeded,
            "failed": failed,
            "success_rate": round(succeeded / requests, 4) if requests else 0.0,
            "failure_rate": round(failed / requests, 4) if requests else 0.0,
            "elapsed_sec": round(elapsed, 4),
            "average_response_ms": round(duration_total * 1000 / requests, 3) if requests else 0.0,
            "concurrency": self._scan_concurrency(),
            "peak_concurrency": peak_concurrency,
            "request_timeout_sec": self._request_timeout(),
            "successes_by_exchange": dict(successes_by_exchange),
            "failures_by_exchange": dict(failures_by_exchange),
        }

    def _enabled_exchanges(self) -> tuple[str, ...]:
        values = getattr(self.settings, "launch_funding_exchanges", DEFAULT_FUNDING_EXCHANGES)
        result: list[str] = []
        for item in values or DEFAULT_FUNDING_EXCHANGES:
            name = canonical_exchange_name(item)
            if name in DEFAULT_FUNDING_EXCHANGES and name not in result:
                result.append(name)
        return tuple(result or DEFAULT_FUNDING_EXCHANGES)

    def _snapshot_one(self, symbol: str, exchange: str, include_history: bool = True) -> dict[str, Any]:
        methods = {
            "BINANCE": self._binance_snapshot,
            "OKX": self._okx_snapshot,
            "BYBIT": self._bybit_snapshot,
            "BITGET": self._bitget_snapshot,
            "GATE": self._gate_snapshot,
        }
        method = methods.get(exchange)
        if method is None:
            return {}
        previous_deadline = getattr(self._request_context, "deadline", None)
        self._request_context.deadline = perf_counter() + self._request_timeout()
        try:
            return method(symbol, include_history=include_history)
        except Exception:
            return {}
        finally:
            if previous_deadline is None:
                try:
                    del self._request_context.deadline
                except AttributeError:
                    pass
            else:
                self._request_context.deadline = previous_deadline

    def _get_json(self, exchange: str, url: str, params: dict[str, Any], cache_key: str) -> Any:
        timeout = self._remaining_request_timeout()
        if timeout <= 0:
            return None
        return self.http.get_json(
            url,
            params,
            cache_key=cache_key,
            quality_key=f"funding:{exchange}",
            retries=1,
            timeout=timeout,
        )

    def _request_timeout(self) -> float:
        configured = float(getattr(self.settings, "funding_request_timeout_sec", 8) or 8)
        return max(0.1, configured)

    def _remaining_request_timeout(self) -> float:
        configured = self._request_timeout()
        deadline = getattr(self._request_context, "deadline", None)
        if deadline is None:
            return configured
        remaining = float(deadline) - perf_counter()
        # Avoid starting another current/history request when the exchange-symbol
        # job has no meaningful deadline budget left.
        if remaining <= 0.05:
            return 0.0
        return min(configured, remaining)

    def _record(
        self,
        exchange: str,
        display_symbol: str,
        funding_pct: float,
        next_time_ms: int,
        history: list[dict[str, Any]],
        interval_hours: int = 0,
    ) -> dict[str, Any]:
        transition = funding_interval_transition(history, next_time_ms)
        last_time_ms = funding_latest_time_ms(history)
        interval = (
            interval_hours
            or int(transition.get("current_interval_hours", 0) or 0)
            or infer_interval_hours(history, next_time_ms)
        )
        return {
            "exchange": exchange,
            "symbol": display_symbol,
            "funding_pct": funding_pct,
            "interval_hours": interval,
            "current_interval_hours": interval,
            "previous_interval_hours": to_int(transition.get("previous_interval_hours")),
            "last_funding_time_ms": last_time_ms,
            "last_funding_time": funding_time_text(last_time_ms),
            "next_funding_time_ms": next_time_ms,
            "next_funding_time": funding_time_text(next_time_ms),
            "funding_interval_transition": str(transition.get("transition_text") or ""),
            "extreme_label": funding_extreme_label(funding_pct),
        }

    def _binance_snapshot(self, symbol: str, include_history: bool = True) -> dict[str, Any]:
        display_symbol = exchange_symbol(symbol, "BINANCE")
        base_url = self.settings.binance_fapi_base_url.rstrip("/")
        current = self._get_json(
            "Binance",
            f"{base_url}/fapi/v1/premiumIndex",
            {"symbol": display_symbol},
            f"funding:binance:current:{display_symbol}",
        )
        if isinstance(current, list):
            current = next((item for item in current if item.get("symbol") == display_symbol), {})
        if not isinstance(current, dict) or not current:
            return {}
        history = self._binance_history(display_symbol) if include_history else []
        return self._record(
            "Binance",
            display_symbol,
            to_float(current.get("lastFundingRate")) * 100,
            to_int(current.get("nextFundingTime")),
            history,
        )

    def _binance_history(self, display_symbol: str) -> list[dict[str, Any]]:
        limit = max(3, int(self.settings.launch_funding_history_limit))
        data = self._get_json(
            "Binance",
            f"{self.settings.binance_fapi_base_url.rstrip('/')}/fapi/v1/fundingRate",
            {"symbol": display_symbol, "limit": limit},
            f"funding:binance:history:{display_symbol}:{limit}",
        )
        return [
            {"time_ms": to_int(item.get("fundingTime")), "rate_pct": to_float(item.get("fundingRate")) * 100}
            for item in data
            if isinstance(item, dict)
        ] if isinstance(data, list) else []

    def _okx_snapshot(self, symbol: str, include_history: bool = True) -> dict[str, Any]:
        display_symbol = exchange_symbol(symbol, "OKX")
        current = self._get_json(
            "OKX",
            "https://www.okx.com/api/v5/public/funding-rate",
            {"instId": display_symbol},
            f"funding:okx:current:{display_symbol}",
        )
        item = self._first_data_item(current)
        if not item:
            return {}
        history = self._okx_history(display_symbol) if include_history else []
        interval = funding_interval_hours(to_int(item.get("fundingTime")) - to_int(item.get("prevFundingTime")))
        return self._record(
            "OKX",
            display_symbol,
            to_float(item.get("fundingRate")) * 100,
            to_int(item.get("fundingTime")),
            history,
            interval,
        )

    def _okx_history(self, display_symbol: str) -> list[dict[str, Any]]:
        limit = max(3, int(self.settings.launch_funding_history_limit))
        data = self._get_json(
            "OKX",
            "https://www.okx.com/api/v5/public/funding-rate-history",
            {"instId": display_symbol, "limit": limit},
            f"funding:okx:history:{display_symbol}:{limit}",
        )
        items = data.get("data", []) if isinstance(data, dict) else []
        return [
            {"time_ms": to_int(item.get("fundingTime")), "rate_pct": to_float(item.get("fundingRate")) * 100}
            for item in items
            if isinstance(item, dict)
        ] if isinstance(items, list) else []

    def _bybit_snapshot(self, symbol: str, include_history: bool = True) -> dict[str, Any]:
        display_symbol = exchange_symbol(symbol, "BYBIT")
        current = self._get_json(
            "Bybit",
            "https://api.bybit.com/v5/market/tickers",
            {"category": "linear", "symbol": display_symbol},
            f"funding:bybit:current:{display_symbol}",
        )
        item = self._bybit_first_item(current)
        if not item:
            return {}
        history = self._bybit_history(display_symbol) if include_history else []
        return self._record(
            "Bybit",
            display_symbol,
            to_float(item.get("fundingRate")) * 100,
            to_int(item.get("nextFundingTime")),
            history,
            to_int(item.get("fundingIntervalHour")),
        )

    def _bybit_history(self, display_symbol: str) -> list[dict[str, Any]]:
        limit = max(3, int(self.settings.launch_funding_history_limit))
        data = self._get_json(
            "Bybit",
            "https://api.bybit.com/v5/market/funding/history",
            {"category": "linear", "symbol": display_symbol, "limit": limit},
            f"funding:bybit:history:{display_symbol}:{limit}",
        )
        result = data.get("result", {}) if isinstance(data, dict) else {}
        items = result.get("list", []) if isinstance(result, dict) else []
        return [
            {"time_ms": to_int(item.get("fundingRateTimestamp")), "rate_pct": to_float(item.get("fundingRate")) * 100}
            for item in items
            if isinstance(item, dict)
        ] if isinstance(items, list) else []

    def _bitget_snapshot(self, symbol: str, include_history: bool = True) -> dict[str, Any]:
        display_symbol = exchange_symbol(symbol, "BITGET")
        current = self._get_json(
            "Bitget",
            "https://api.bitget.com/api/v2/mix/market/current-fund-rate",
            {"symbol": display_symbol, "productType": "usdt-futures"},
            f"funding:bitget:current:{display_symbol}",
        )
        item = self._first_data_item(current)
        if not item:
            return {}
        history = self._bitget_history(display_symbol) if include_history else []
        return self._record(
            "Bitget",
            display_symbol,
            to_float(item.get("fundingRate")) * 100,
            to_int(item.get("nextUpdate")),
            history,
            to_int(item.get("fundingRateInterval")),
        )

    def _bitget_history(self, display_symbol: str) -> list[dict[str, Any]]:
        limit = max(3, int(self.settings.launch_funding_history_limit))
        data = self._get_json(
            "Bitget",
            "https://api.bitget.com/api/v2/mix/market/history-fund-rate",
            {"symbol": display_symbol, "productType": "usdt-futures", "pageSize": limit},
            f"funding:bitget:history:{display_symbol}:{limit}",
        )
        items = data.get("data", []) if isinstance(data, dict) else []
        return [
            {"time_ms": to_int(item.get("fundingTime")), "rate_pct": to_float(item.get("fundingRate")) * 100}
            for item in items
            if isinstance(item, dict)
        ] if isinstance(items, list) else []

    def _gate_snapshot(self, symbol: str, include_history: bool = True) -> dict[str, Any]:
        display_symbol = exchange_symbol(symbol, "GATE")
        current = self._get_json(
            "Gate",
            f"https://fx-api.gateio.ws/api/v4/futures/usdt/contracts/{display_symbol}",
            {},
            f"funding:gate:current:{display_symbol}",
        )
        if not isinstance(current, dict) or not current:
            return {}
        history = self._gate_history(display_symbol) if include_history else []
        return self._record(
            "Gate",
            display_symbol,
            to_float(current.get("funding_rate")) * 100,
            to_int(current.get("funding_next_apply")) * 1000,
            history,
            funding_interval_hours(to_int(current.get("funding_interval")) * 1000),
        )

    def _gate_history(self, display_symbol: str) -> list[dict[str, Any]]:
        limit = max(3, int(self.settings.launch_funding_history_limit))
        data = self._get_json(
            "Gate",
            "https://fx-api.gateio.ws/api/v4/futures/usdt/funding_rate",
            {"contract": display_symbol, "limit": limit},
            f"funding:gate:history:{display_symbol}:{limit}",
        )
        return [
            {"time_ms": to_int(item.get("t")) * 1000, "rate_pct": to_float(item.get("r")) * 100}
            for item in data
            if isinstance(item, dict)
        ] if isinstance(data, list) else []

    @staticmethod
    def _first_data_item(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        items = data.get("data", [])
        if isinstance(items, list) and items:
            first = items[0]
            return first if isinstance(first, dict) else {}
        return items if isinstance(items, dict) else {}

    @staticmethod
    def _bybit_first_item(data: Any) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {}
        result = data.get("result", {})
        items = result.get("list", []) if isinstance(result, dict) else []
        if isinstance(items, list) and items:
            first = items[0]
            return first if isinstance(first, dict) else {}
        return {}
