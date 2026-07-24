from __future__ import annotations

from typing import Any, Mapping, Sequence


SMC_VERSION = 1
TIMEFRAME_ORDER = ("15m", "1h", "4h")


def _number(value: Any) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if result == result else 0.0


def _normalize_candles(
    items: Sequence[Mapping[str, Any]],
) -> list[dict[str, float | int]]:
    candles = sorted(
        [
            {
                "open_ts": int(_number(item.get("open_ts"))),
                "close_ts": int(_number(item.get("close_ts"))),
                "open": _number(item.get("open")),
                "high": _number(item.get("high")),
                "low": _number(item.get("low")),
                "close": _number(item.get("close")),
            }
            for item in items
            if isinstance(item, Mapping)
        ],
        key=lambda item: int(item["close_ts"]),
    )
    return [
        candle
        for candle in candles
        if int(candle["close_ts"]) > 0
        and min(
            float(candle["open"]),
            float(candle["high"]),
            float(candle["low"]),
            float(candle["close"]),
        )
        > 0
        and float(candle["high"])
        >= max(float(candle["open"]), float(candle["close"]))
        and float(candle["low"])
        <= min(float(candle["open"]), float(candle["close"]))
    ]


def _atr_series(candles: Sequence[Mapping[str, Any]]) -> list[float]:
    true_ranges: list[float] = []
    averages: list[float] = []
    for index, candle in enumerate(candles):
        high = _number(candle.get("high"))
        low = _number(candle.get("low"))
        previous_close = (
            _number(candles[index - 1].get("close"))
            if index > 0
            else _number(candle.get("open"))
        )
        true_ranges.append(
            max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        )
        window = true_ranges[max(0, index - 13):index + 1]
        averages.append(sum(window) / len(window) if window else 0.0)
    return averages


def _confirmed_swings(
    candles: Sequence[Mapping[str, Any]],
    *,
    swing_length: int,
) -> list[dict[str, Any]]:
    length = max(1, int(swing_length))
    raw: list[dict[str, Any]] = []
    for index in range(length, len(candles) - length):
        window = candles[index - length:index + length + 1]
        high = _number(candles[index].get("high"))
        low = _number(candles[index].get("low"))
        highs = [_number(candle.get("high")) for candle in window]
        lows = [_number(candle.get("low")) for candle in window]
        confirmed_index = index + length
        if high == max(highs) and highs.count(high) == 1:
            raw.append({
                "kind": "high",
                "index": index,
                "confirmed_index": confirmed_index,
                "ts": int(_number(candles[index].get("close_ts"))),
                "confirmed_ts": int(
                    _number(candles[confirmed_index].get("close_ts"))
                ),
                "level": high,
            })
        if low == min(lows) and lows.count(low) == 1:
            raw.append({
                "kind": "low",
                "index": index,
                "confirmed_index": confirmed_index,
                "ts": int(_number(candles[index].get("close_ts"))),
                "confirmed_ts": int(
                    _number(candles[confirmed_index].get("close_ts"))
                ),
                "level": low,
            })
    raw.sort(key=lambda item: (int(item["index"]), item["kind"]))

    alternating: list[dict[str, Any]] = []
    for swing in raw:
        if alternating and alternating[-1]["kind"] == swing["kind"]:
            previous = alternating[-1]
            replace = (
                swing["level"] > previous["level"]
                if swing["kind"] == "high"
                else swing["level"] < previous["level"]
            )
            if replace:
                alternating[-1] = swing
            continue
        alternating.append(swing)

    previous_by_kind: dict[str, float] = {}
    for swing in alternating:
        kind = str(swing["kind"])
        previous_level = previous_by_kind.get(kind)
        if previous_level is None:
            label = "SH" if kind == "high" else "SL"
        elif kind == "high":
            label = "HH" if swing["level"] > previous_level else "LH"
        else:
            label = "HL" if swing["level"] > previous_level else "LL"
        swing["label"] = label
        previous_by_kind[kind] = float(swing["level"])
    return alternating


def _swing_trend_before(
    swings: Sequence[Mapping[str, Any]],
    candle_index: int,
) -> str:
    last_high: Mapping[str, Any] | None = None
    last_low: Mapping[str, Any] | None = None
    for swing in swings:
        if int(_number(swing.get("confirmed_index"))) >= candle_index:
            continue
        if swing.get("kind") == "high":
            last_high = swing
        else:
            last_low = swing
    high_label = str((last_high or {}).get("label") or "")
    low_label = str((last_low or {}).get("label") or "")
    if high_label == "HH" and low_label == "HL":
        return "up"
    if high_label == "LH" and low_label == "LL":
        return "down"
    return ""


def _structure_events(
    candles: Sequence[Mapping[str, Any]],
    swings: Sequence[Mapping[str, Any]],
    atr: Sequence[float],
    *,
    timeframe: str,
    displacement_body_atr: float,
) -> list[dict[str, Any]]:
    candidates: dict[tuple[int, str], dict[str, Any]] = {}
    for swing in swings:
        direction = "up" if swing.get("kind") == "high" else "down"
        level = _number(swing.get("level"))
        start = int(_number(swing.get("confirmed_index"))) + 1
        for index in range(max(1, start), len(candles)):
            close = _number(candles[index].get("close"))
            previous_close = _number(candles[index - 1].get("close"))
            broken = (
                previous_close <= level < close
                if direction == "up"
                else previous_close >= level > close
            )
            if not broken:
                continue
            key = (index, direction)
            existing = candidates.get(key)
            if existing is None or int(swing["index"]) > int(
                existing["source_index"]
            ):
                candidates[key] = {
                    "index": index,
                    "direction": direction,
                    "level": level,
                    "source_index": int(swing["index"]),
                    "source_ts": int(_number(swing.get("ts"))),
                }
            break

    trend = ""
    events: list[dict[str, Any]] = []
    for candidate in sorted(
        candidates.values(),
        key=lambda item: (int(item["index"]), item["direction"]),
    ):
        index = int(candidate["index"])
        direction = str(candidate["direction"])
        if not trend:
            trend = _swing_trend_before(swings, index)
        base_type = "CHOCH" if trend and direction != trend else "BOS"
        candle = candles[index]
        body = abs(
            _number(candle.get("close")) - _number(candle.get("open"))
        )
        body_atr = body / atr[index] if atr[index] > 0 else 0.0
        displacement = body_atr >= max(0.0, float(displacement_body_atr))
        event_type = "MSS" if base_type == "CHOCH" and displacement else base_type
        event_ts = int(_number(candle.get("close_ts")))
        level = float(candidate["level"])
        events.append({
            **candidate,
            "type": event_type,
            "base_type": base_type,
            "timeframe": timeframe,
            "event_ts": event_ts,
            "body_atr": round(body_atr, 6),
            "displacement": displacement,
            "key": (
                f"{timeframe}:structure:{event_type}:{direction}:"
                f"{event_ts}:{level:.12g}"
            ),
        })
        trend = direction
    return events


def _liquidity_pools(
    candles: Sequence[Mapping[str, Any]],
    swings: Sequence[Mapping[str, Any]],
    atr: Sequence[float],
    *,
    timeframe: str,
    equal_tolerance_atr: float,
) -> list[dict[str, Any]]:
    pools: list[dict[str, Any]] = []
    for kind, pool_type in (("high", "BSL"), ("low", "SSL")):
        same_kind = [swing for swing in swings if swing.get("kind") == kind]
        for first, second in zip(same_kind, same_kind[1:]):
            second_index = int(_number(second.get("index")))
            reference_atr = atr[second_index] if second_index < len(atr) else 0.0
            fallback = _number(second.get("level")) * 0.001
            tolerance = max(
                reference_atr * max(0.0, float(equal_tolerance_atr)),
                fallback,
            )
            first_level = _number(first.get("level"))
            second_level = _number(second.get("level"))
            if abs(first_level - second_level) > tolerance:
                continue
            level = (first_level + second_level) / 2.0
            formed_ts = int(_number(second.get("confirmed_ts")))
            pool = {
                "type": pool_type,
                "level": level,
                "tolerance": tolerance,
                "start_ts": int(_number(first.get("ts"))),
                "end_ts": int(_number(second.get("ts"))),
                "formed_ts": formed_ts,
                "formed_index": int(_number(second.get("confirmed_index"))),
                "status": "active",
                "event_ts": 0,
                "direction": "down" if pool_type == "BSL" else "up",
                "timeframe": timeframe,
                "key": (
                    f"{timeframe}:liquidity:{pool_type}:"
                    f"{formed_ts}:{level:.12g}"
                ),
            }
            for index in range(pool["formed_index"] + 1, len(candles)):
                candle = candles[index]
                high = _number(candle.get("high"))
                low = _number(candle.get("low"))
                close = _number(candle.get("close"))
                swept = (
                    high > level and close < level
                    if pool_type == "BSL"
                    else low < level and close > level
                )
                broken = close > level if pool_type == "BSL" else close < level
                if swept:
                    pool["status"] = "swept"
                    pool["event_ts"] = int(_number(candle.get("close_ts")))
                    break
                if broken:
                    pool["status"] = "broken"
                    pool["event_ts"] = int(_number(candle.get("close_ts")))
                    break
            pools.append(pool)
    return sorted(pools, key=lambda item: int(item["formed_ts"]))


def _fair_value_gaps(
    candles: Sequence[Mapping[str, Any]],
    atr: Sequence[float],
    *,
    timeframe: str,
    displacement_body_atr: float,
) -> list[dict[str, Any]]:
    gaps: list[dict[str, Any]] = []
    for index in range(2, len(candles)):
        first = candles[index - 2]
        middle = candles[index - 1]
        current = candles[index]
        direction = ""
        bottom = 0.0
        top = 0.0
        if (
            _number(first.get("high")) < _number(current.get("low"))
            and _number(middle.get("close")) > _number(middle.get("open"))
        ):
            direction = "up"
            bottom = _number(first.get("high"))
            top = _number(current.get("low"))
        elif (
            _number(first.get("low")) > _number(current.get("high"))
            and _number(middle.get("close")) < _number(middle.get("open"))
        ):
            direction = "down"
            bottom = _number(current.get("high"))
            top = _number(first.get("low"))
        if not direction:
            continue
        body = abs(
            _number(middle.get("close")) - _number(middle.get("open"))
        )
        middle_atr = atr[index - 1] if atr[index - 1] > 0 else 0.0
        body_atr = body / middle_atr if middle_atr > 0 else 0.0
        formed_ts = int(_number(current.get("close_ts")))
        gap = {
            "direction": direction,
            "bottom": bottom,
            "top": top,
            "formed_ts": formed_ts,
            "formed_index": index,
            "status": "active",
            "mitigated_ts": 0,
            "body_atr": round(body_atr, 6),
            "displacement": body_atr
            >= max(0.0, float(displacement_body_atr)),
            "timeframe": timeframe,
            "key": (
                f"{timeframe}:fvg:{direction}:{formed_ts}:"
                f"{bottom:.12g}:{top:.12g}"
            ),
        }
        for later in range(index + 1, len(candles)):
            candle = candles[later]
            touched = (
                _number(candle.get("low")) <= top
                if direction == "up"
                else _number(candle.get("high")) >= bottom
            )
            if not touched:
                continue
            gap["mitigated_ts"] = int(_number(candle.get("close_ts")))
            filled = (
                _number(candle.get("low")) <= bottom
                if direction == "up"
                else _number(candle.get("high")) >= top
            )
            gap["status"] = "filled" if filled else "mitigated"
            break
        gaps.append(gap)
    return gaps


def _order_blocks(
    candles: Sequence[Mapping[str, Any]],
    structures: Sequence[Mapping[str, Any]],
    *,
    timeframe: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    blocks: list[dict[str, Any]] = []
    breakers: list[dict[str, Any]] = []
    mitigations: list[dict[str, Any]] = []
    for structure in structures:
        event_index = int(_number(structure.get("index")))
        direction = str(structure.get("direction") or "")
        origin_index = -1
        for index in range(event_index - 1, max(-1, event_index - 13), -1):
            candle = candles[index]
            opposite = (
                _number(candle.get("close")) < _number(candle.get("open"))
                if direction == "up"
                else _number(candle.get("close")) > _number(candle.get("open"))
            )
            if opposite:
                origin_index = index
                break
        if origin_index < 0:
            continue
        origin = candles[origin_index]
        bottom = (
            _number(origin.get("low"))
            if direction == "up"
            else _number(origin.get("open"))
        )
        top = (
            _number(origin.get("open"))
            if direction == "up"
            else _number(origin.get("high"))
        )
        formed_ts = int(_number(structure.get("event_ts")))
        block = {
            "direction": direction,
            "bottom": min(bottom, top),
            "top": max(bottom, top),
            "origin_ts": int(_number(origin.get("close_ts"))),
            "formed_ts": formed_ts,
            "formed_index": event_index,
            "status": "active",
            "mitigated_ts": 0,
            "invalidated_ts": 0,
            "timeframe": timeframe,
            "key": (
                f"{timeframe}:ob:{direction}:{formed_ts}:"
                f"{min(bottom, top):.12g}:{max(bottom, top):.12g}"
            ),
        }
        mitigation_added = False
        invalidated_index = -1
        for later in range(event_index + 1, len(candles)):
            candle = candles[later]
            close = _number(candle.get("close"))
            invalidated = (
                close < block["bottom"]
                if direction == "up"
                else close > block["top"]
            )
            if invalidated:
                block["status"] = "invalidated"
                block["invalidated_ts"] = int(
                    _number(candle.get("close_ts"))
                )
                invalidated_index = later
                break
            touched = (
                _number(candle.get("low")) <= block["top"]
                and _number(candle.get("high")) >= block["bottom"]
            )
            if touched and not mitigation_added:
                block["status"] = "mitigated"
                block["mitigated_ts"] = int(_number(candle.get("close_ts")))
                mitigations.append({
                    "direction": direction,
                    "bottom": block["bottom"],
                    "top": block["top"],
                    "event_ts": block["mitigated_ts"],
                    "source_key": block["key"],
                    "timeframe": timeframe,
                    "key": f"{block['key']}:mitigated:{block['mitigated_ts']}",
                })
                mitigation_added = True
        blocks.append(block)
        if invalidated_index < 0:
            continue
        breaker_direction = "down" if direction == "up" else "up"
        breaker = {
            "direction": breaker_direction,
            "bottom": block["bottom"],
            "top": block["top"],
            "formed_ts": block["invalidated_ts"],
            "formed_index": invalidated_index,
            "status": "active",
            "mitigated_ts": 0,
            "timeframe": timeframe,
            "source_key": block["key"],
            "key": (
                f"{timeframe}:breaker:{breaker_direction}:"
                f"{block['invalidated_ts']}:{block['bottom']:.12g}:"
                f"{block['top']:.12g}"
            ),
        }
        for later in range(invalidated_index + 1, len(candles)):
            candle = candles[later]
            touched = (
                _number(candle.get("low")) <= breaker["top"]
                and _number(candle.get("high")) >= breaker["bottom"]
            )
            if touched:
                breaker["status"] = "mitigated"
                breaker["mitigated_ts"] = int(
                    _number(candle.get("close_ts"))
                )
                break
        breakers.append(breaker)
    return blocks, breakers, mitigations


def _dealing_range(
    candles: Sequence[Mapping[str, Any]],
    swings: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    highs = [swing for swing in swings if swing.get("kind") == "high"]
    lows = [swing for swing in swings if swing.get("kind") == "low"]
    if not highs or not lows or not candles:
        return {}
    high_swing = highs[-1]
    low_swing = lows[-1]
    high = _number(high_swing.get("level"))
    low = _number(low_swing.get("level"))
    if high <= low:
        return {}
    equilibrium = (high + low) / 2.0
    close = _number(candles[-1].get("close"))
    zone = "premium" if close > equilibrium else "discount" if close < equilibrium else "equilibrium"
    return {
        "high": high,
        "low": low,
        "equilibrium": equilibrium,
        "zone": zone,
        "start_ts": min(
            int(_number(high_swing.get("ts"))),
            int(_number(low_swing.get("ts"))),
        ),
        "end_ts": int(_number(candles[-1].get("close_ts"))),
    }


def _frame_events(
    structures: Sequence[Mapping[str, Any]],
    liquidity: Sequence[Mapping[str, Any]],
    fvgs: Sequence[Mapping[str, Any]],
    breakers: Sequence[Mapping[str, Any]],
    mitigations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for structure in structures:
        events.append({
            "key": str(structure.get("key") or ""),
            "event_type": "structure",
            "label": str(structure.get("type") or "BOS"),
            "direction": str(structure.get("direction") or ""),
            "event_ts": int(_number(structure.get("event_ts"))),
            "level": _number(structure.get("level")),
            "displacement": bool(structure.get("displacement")),
            "priority": 20,
        })
    for pool in liquidity:
        if pool.get("status") != "swept":
            continue
        events.append({
            "key": f"{pool.get('key')}:swept:{pool.get('event_ts')}",
            "event_type": "liquidity_sweep",
            "label": f"{pool.get('type')} SWEEP",
            "direction": str(pool.get("direction") or ""),
            "event_ts": int(_number(pool.get("event_ts"))),
            "level": _number(pool.get("level")),
            "priority": 10,
        })
    for gap in fvgs:
        if gap.get("displacement"):
            events.append({
                "key": str(gap.get("key") or ""),
                "event_type": "displacement_fvg",
                "label": "FVG",
                "direction": str(gap.get("direction") or ""),
                "event_ts": int(_number(gap.get("formed_ts"))),
                "bottom": _number(gap.get("bottom")),
                "top": _number(gap.get("top")),
                "priority": 30,
            })
        if int(_number(gap.get("mitigated_ts"))) > 0:
            events.append({
                "key": f"{gap.get('key')}:retest:{gap.get('mitigated_ts')}",
                "event_type": "fvg_retest",
                "label": "FVG RETEST",
                "direction": str(gap.get("direction") or ""),
                "event_ts": int(_number(gap.get("mitigated_ts"))),
                "bottom": _number(gap.get("bottom")),
                "top": _number(gap.get("top")),
                "priority": 40,
            })
    for mitigation in mitigations:
        events.append({
            "key": str(mitigation.get("key") or ""),
            "event_type": "mitigation",
            "label": "MB",
            "direction": str(mitigation.get("direction") or ""),
            "event_ts": int(_number(mitigation.get("event_ts"))),
            "bottom": _number(mitigation.get("bottom")),
            "top": _number(mitigation.get("top")),
            "priority": 40,
        })
    for breaker in breakers:
        events.append({
            "key": str(breaker.get("key") or ""),
            "event_type": "breaker",
            "label": "BRK",
            "direction": str(breaker.get("direction") or ""),
            "event_ts": int(_number(breaker.get("formed_ts"))),
            "bottom": _number(breaker.get("bottom")),
            "top": _number(breaker.get("top")),
            "priority": 35,
        })
    deduplicated = {
        str(event["key"]): event
        for event in events
        if str(event.get("key") or "") and int(event.get("event_ts") or 0) > 0
    }
    return sorted(
        deduplicated.values(),
        key=lambda event: (
            int(event["event_ts"]),
            int(event["priority"]),
            str(event["key"]),
        ),
    )


def _empty_frame(status: str, candle_count: int) -> dict[str, Any]:
    return {
        "data_status": status,
        "candle_count": candle_count,
        "trend": "neutral",
        "swings": [],
        "structures": [],
        "liquidity": [],
        "fvgs": [],
        "order_blocks": [],
        "breaker_blocks": [],
        "mitigation_blocks": [],
        "dealing_range": {},
        "events": [],
    }


def _analyze_timeframe(
    candles: Sequence[Mapping[str, Any]],
    *,
    timeframe: str,
    swing_length: int,
    equal_tolerance_atr: float,
    displacement_body_atr: float,
    max_zone_age_bars: int,
) -> dict[str, Any]:
    normalized = _normalize_candles(candles)
    minimum = max(7, max(1, int(swing_length)) * 2 + 3)
    if len(normalized) < minimum:
        return _empty_frame("insufficient_history", len(normalized))
    expected_interval = {
        "15m": 15 * 60,
        "1h": 60 * 60,
        "4h": 4 * 60 * 60,
    }.get(timeframe, 0)
    if expected_interval and any(
        int(_number(normalized[index].get("close_ts")))
        - int(_number(normalized[index - 1].get("close_ts")))
        != expected_interval
        for index in range(1, len(normalized))
    ):
        return _empty_frame("gap", len(normalized))
    atr = _atr_series(normalized)
    swings = _confirmed_swings(normalized, swing_length=swing_length)
    structures = _structure_events(
        normalized,
        swings,
        atr,
        timeframe=timeframe,
        displacement_body_atr=displacement_body_atr,
    )
    liquidity = _liquidity_pools(
        normalized,
        swings,
        atr,
        timeframe=timeframe,
        equal_tolerance_atr=equal_tolerance_atr,
    )
    fvgs = _fair_value_gaps(
        normalized,
        atr,
        timeframe=timeframe,
        displacement_body_atr=displacement_body_atr,
    )
    blocks, breakers, mitigations = _order_blocks(
        normalized,
        structures,
        timeframe=timeframe,
    )
    earliest_index = max(0, len(normalized) - max(16, int(max_zone_age_bars)))
    cutoff_ts = int(_number(normalized[earliest_index].get("close_ts")))
    recent_fvgs = [
        gap for gap in fvgs
        if int(_number(gap.get("formed_ts"))) >= cutoff_ts
    ][-8:]
    recent_blocks = [
        block for block in blocks
        if int(_number(block.get("formed_ts"))) >= cutoff_ts
    ][-6:]
    recent_breakers = [
        block for block in breakers
        if int(_number(block.get("formed_ts"))) >= cutoff_ts
    ][-4:]
    recent_mitigations = [
        block for block in mitigations
        if int(_number(block.get("event_ts"))) >= cutoff_ts
    ][-4:]
    events = _frame_events(
        structures[-10:],
        liquidity[-8:],
        recent_fvgs,
        recent_breakers,
        recent_mitigations,
    )
    trend = (
        str(structures[-1].get("direction") or "neutral")
        if structures
        else _swing_trend_before(swings, len(normalized))
    )
    return {
        "data_status": "ready",
        "candle_count": len(normalized),
        "last_close_ts": int(_number(normalized[-1].get("close_ts"))),
        "atr": round(atr[-1], 12),
        "trend": trend or "neutral",
        "swings": swings[-16:],
        "structures": structures[-10:],
        "liquidity": liquidity[-8:],
        "fvgs": recent_fvgs,
        "order_blocks": recent_blocks,
        "breaker_blocks": recent_breakers,
        "mitigation_blocks": recent_mitigations,
        "dealing_range": _dealing_range(normalized, swings),
        "events": events[-32:],
        "latest_event": events[-1] if events else {},
    }


def analyze_smc_frames(
    candles_by_timeframe: Mapping[str, Sequence[Mapping[str, Any]]],
    *,
    swing_length: int = 2,
    equal_tolerance_atr: float = 0.15,
    displacement_body_atr: float = 1.0,
    max_zone_age_bars: int = 96,
) -> dict[str, Any]:
    """Analyze deterministic SMC structures from already-closed candles."""

    frames = {
        timeframe: _analyze_timeframe(
            candles_by_timeframe.get(timeframe) or [],
            timeframe=timeframe,
            swing_length=max(1, int(swing_length)),
            equal_tolerance_atr=max(0.0, float(equal_tolerance_atr)),
            displacement_body_atr=max(0.0, float(displacement_body_atr)),
            max_zone_age_bars=max(16, int(max_zone_age_bars)),
        )
        for timeframe in TIMEFRAME_ORDER
    }
    bias_4h = str(frames["4h"].get("trend") or "neutral")
    bias_1h = str(frames["1h"].get("trend") or "neutral")
    directional_4h = bias_4h if bias_4h in {"up", "down"} else ""
    directional_1h = bias_1h if bias_1h in {"up", "down"} else ""
    direction = directional_4h or directional_1h or "neutral"
    alignment = (
        "aligned"
        if directional_4h and directional_4h == directional_1h
        else "mixed"
        if directional_4h and directional_1h
        else "partial"
        if direction != "neutral"
        else "neutral"
    )
    execution_trend = str(frames["15m"].get("trend") or "neutral")
    execution_alignment = (
        "aligned"
        if direction in {"up", "down"} and execution_trend == direction
        else "counter"
        if direction in {"up", "down"}
        and execution_trend in {"up", "down"}
        else "neutral"
    )
    return {
        "enabled": True,
        "version": SMC_VERSION,
        "data_status": str(frames["15m"].get("data_status") or "unavailable"),
        "swing_length": max(1, int(swing_length)),
        "equal_tolerance_atr": max(0.0, float(equal_tolerance_atr)),
        "displacement_body_atr": max(0.0, float(displacement_body_atr)),
        "timeframes": frames,
        "htf_bias": {
            "direction": direction,
            "alignment": alignment,
            "bias_4h": bias_4h,
            "bias_1h": bias_1h,
            "execution_15m": execution_trend,
            "execution_alignment": execution_alignment,
        },
        "events": list(frames["15m"].get("events") or []),
    }


def advance_smc_state(
    previous: Mapping[str, Any] | None,
    analysis: Mapping[str, Any] | None,
) -> dict[str, Any]:
    previous_state = dict(previous or {})
    if not isinstance(analysis, Mapping) or not analysis.get("enabled"):
        if previous_state:
            previous_state["data_status"] = "unavailable"
            return previous_state
        return {
            "enabled": True,
            "version": SMC_VERSION,
            "data_status": "unavailable",
            "status": "watching",
            "event_key": "",
            "processed_event_keys": [],
            "snapshot": {},
        }

    state = {
        **previous_state,
        "enabled": True,
        "version": SMC_VERSION,
        "data_status": str(analysis.get("data_status") or "unavailable"),
        "status": str(previous_state.get("status") or "watching"),
        "event_key": str(previous_state.get("event_key") or ""),
        "snapshot": dict(analysis),
        "htf_bias": dict(analysis.get("htf_bias") or {}),
    }
    processed = [
        str(key)
        for key in (previous_state.get("processed_event_keys") or [])
        if str(key)
    ]
    processed_set = set(processed)
    events = [
        dict(event)
        for event in (analysis.get("events") or [])
        if isinstance(event, Mapping)
        and str(event.get("key") or "")
        and str(event.get("key")) not in processed_set
    ]
    events.sort(
        key=lambda event: (
            int(_number(event.get("event_ts"))),
            int(_number(event.get("priority"))),
            str(event.get("key") or ""),
        )
    )
    for event in events:
        key = str(event.get("key") or "")
        event_type = str(event.get("event_type") or "")
        direction = str(event.get("direction") or "")
        event_ts = int(_number(event.get("event_ts")))
        state["event_key"] = key
        state["latest_event"] = event
        state["event_window_end_ts"] = event_ts
        if event_type == "liquidity_sweep" and direction in {"up", "down"}:
            state.update({
                "status": "liquidity_sweep",
                "direction": direction,
                "chain_started_ts": event_ts,
                "sweep_event": event,
                "choch_event": {},
                "displacement_event": {},
                "retest_event": {},
                "bos_event": {},
            })
        elif event_type == "structure":
            label = str(event.get("label") or "")
            chain_direction = str(state.get("direction") or "")
            if (
                label in {"CHOCH", "MSS"}
                and state.get("status") == "liquidity_sweep"
                and direction == chain_direction
            ):
                state["status"] = "displacement" if label == "MSS" else "choch"
                state["choch_event"] = event
                if label == "MSS":
                    state["displacement_event"] = event
            elif (
                label == "BOS"
                and state.get("status") == "retest"
                and direction == chain_direction
            ):
                state["status"] = "bos_confirmed"
                state["bos_event"] = event
            elif state.get("status") in {"watching", "structure"}:
                state["status"] = "structure"
                state["direction"] = direction
        elif (
            event_type == "displacement_fvg"
            and state.get("status") in {"choch", "displacement"}
            and direction == str(state.get("direction") or "")
        ):
            state["status"] = "displacement"
            state["displacement_event"] = event
        elif (
            event_type in {"fvg_retest", "mitigation"}
            and state.get("status") == "displacement"
            and direction == str(state.get("direction") or "")
        ):
            state["status"] = "retest"
            state["retest_event"] = event
        processed.append(key)
        processed_set.add(key)
    state["processed_event_keys"] = processed[-128:]
    return state


__all__ = [
    "SMC_VERSION",
    "advance_smc_state",
    "analyze_smc_frames",
]
