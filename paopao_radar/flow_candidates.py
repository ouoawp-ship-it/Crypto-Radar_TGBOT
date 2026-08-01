from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from .config import Settings
from .flow_radar import FLOW_CANDIDATE_STATE_SCHEMA_VERSION
from .storage import JsonStore


def build_candidate_list(
    settings: Settings,
    store: JsonStore,
    *,
    show_all: bool,
    limit: int,
) -> dict[str, object]:
    state = store.load(settings.flow_candidate_state_path, {})
    if (
        not isinstance(state, dict)
        or state.get("schema_version") != FLOW_CANDIDATE_STATE_SCHEMA_VERSION
        or not isinstance(state.get("candidates"), list)
    ):
        return {
            "status": "not_ready",
            "reason": "flow_candidate_state_not_available",
            "network_activity": False,
            "candidates": [],
        }
    candidates = sorted(
        (item for item in state["candidates"] if isinstance(item, dict)),
        key=lambda item: (
            int(item.get("priority_rank") or 0),
            str(item.get("symbol") or ""),
        ),
    )
    visible = candidates if show_all else candidates[: max(1, limit)]
    return {
        "status": "ok",
        "network_activity": False,
        "pool_mode": "unlimited",
        "total_candidates": len(candidates),
        "scan_limit": int(state.get("scan_limit") or settings.flow_scan_limit),
        "shown_candidates": len(visible),
        "unscanned_count": int(state.get("unscanned_count") or 0),
        "candidates": visible,
    }


def format_candidate_list(result: dict[str, object]) -> str:
    if result.get("status") != "ok":
        return "候选清单尚未生成；等待下一轮资金流雷达完成后再查看。"
    candidates = list(result.get("candidates") or [])
    lines = [
        "五因子资金流雷达 · 全市场候选清单",
        (
            f"候选总数: {int(result.get('total_candidates') or 0)} | "
            f"每轮优先轮换: {int(result.get('scan_limit') or 0)} | "
            f"尚未扫描: {int(result.get('unscanned_count') or 0)} | 网络请求: 0"
        ),
    ]
    for raw_item in candidates:
        item = raw_item if isinstance(raw_item, dict) else {}
        reasons = ",".join(
            str(value) for value in item.get("selection_reasons") or []
        ) or "eligible"
        last_scanned = item.get("last_scanned_at")
        last_text = (
            datetime.fromtimestamp(int(last_scanned), timezone.utc).strftime(
                "%Y-%m-%d %H:%M UTC"
            )
            if last_scanned
            else "从未"
        )
        lines.append(
            f"{int(item.get('priority_rank') or 0):>4}. "
            f"{item.get('symbol') or '-'} | "
            f"下轮顺位 {int(item.get('next_rotation_rank') or 0)} | "
            f"已扫 {int(item.get('scan_count') or 0)} 次 | "
            f"上次 {last_text} | {reasons}"
        )
    if int(result.get("shown_candidates") or 0) < int(
        result.get("total_candidates") or 0
    ):
        lines.append(
            f"当前只显示前 {int(result.get('shown_candidates') or 0)} 个；"
            "使用 flow-candidates --all 查看完整清单。"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查看本地五因子资金流候选清单")
    parser.add_argument("--all", action="store_true", help="显示完整候选清单")
    parser.add_argument("--top", type=int, default=50, help="默认显示前 N 个")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load()
    result = build_candidate_list(
        settings,
        JsonStore(settings.data_dir),
        show_all=bool(args.all),
        limit=max(1, int(args.top)),
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(format_candidate_list(result))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
