from __future__ import annotations

from typing import Any, Mapping


def apply_binance_confirmation(
    item: dict[str, Any],
    checks: Mapping[str, bool],
    *,
    scope: str,
    window: str,
    observed_at: int,
) -> dict[str, Any]:
    normalized = {str(name): bool(ready) for name, ready in checks.items()}
    ready = [name for name, value in normalized.items() if value]
    missing = [name for name, value in normalized.items() if not value]
    total = len(normalized)
    ready_count = len(ready)
    confirmed = total > 0 and ready_count == total
    score = round(ready_count / total * 100) if total else 0
    confirmation = {
        "provider": "Binance",
        "source": "binance_native",
        "scope": scope,
        "window": window,
        "observed_at": int(observed_at),
        "status": "confirmed" if confirmed else "incomplete",
        "ready": ready,
        "missing": missing,
        "ready_count": ready_count,
        "total_count": total,
        "score": score,
    }
    item.update({
        "data_confirmation": confirmation,
        # Keep the existing persistence fields so historical P2 samples remain
        # comparable after switching away from cross-provider agreement scores.
        "data_quality_status": confirmation["status"],
        "data_quality_score": score,
        "quality_gate": "allow" if confirmed else "block",
        "primary_data_source": "binance_native",
    })
    return confirmation


def confirmation_text(item: Mapping[str, Any]) -> str:
    confirmation = item.get("data_confirmation")
    if not isinstance(confirmation, Mapping):
        return "Binance原生 · 未确认"
    ready_count = int(confirmation.get("ready_count") or 0)
    total_count = int(confirmation.get("total_count") or 0)
    window = str(confirmation.get("window") or "").strip()
    status = "完整" if confirmation.get("status") == "confirmed" else "缺项"
    parts = [f"Binance原生 {ready_count}/{total_count}", status]
    if window:
        parts.append(window)
    return " · ".join(parts)


def confirmation_summary(items: list[Mapping[str, Any]]) -> dict[str, int]:
    confirmed = 0
    incomplete = 0
    for item in items:
        confirmation = item.get("data_confirmation")
        if isinstance(confirmation, Mapping) and confirmation.get("status") == "confirmed":
            confirmed += 1
        else:
            incomplete += 1
    return {
        "checked": len(items),
        "confirmed": confirmed,
        "incomplete": incomplete,
    }


__all__ = [
    "apply_binance_confirmation",
    "confirmation_summary",
    "confirmation_text",
]
