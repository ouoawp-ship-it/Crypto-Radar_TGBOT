from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .web_services.api_core import redact_api_payload


MODEL_VERSION = "signal-decision-v1"
NOT_ADVICE_TEXT = "仅用于信号整理和风险提示，不构成投资建议，不执行自动交易。"

DEFAULT_DECISION_WEIGHTS: dict[str, float] = {
    "signal_strength": 0.25,
    "module_confluence": 0.25,
    "signal_density": 0.15,
    "structure_confirmation": 0.20,
    "crowding_risk": -0.25,
    "failure_penalty": -0.20,
}

DECISION_DISPLAY: dict[str, dict[str, str]] = {
    "observe": {"label": "观察", "tone": "neutral"},
    "wait_pullback": {"label": "等待回踩", "tone": "warning"},
    "trial_position": {"label": "可试仓", "tone": "good"},
    "no_chase": {"label": "禁止追高", "tone": "warning"},
    "risk_alert": {"label": "风险警报", "tone": "bad"},
}

MODULE_WEIGHTS: dict[str, int] = {
    "launch": 64,
    "flow": 62,
    "structure": 58,
    "structure_review": 54,
    "funding": 50,
    "summary": 42,
    "announcement": 38,
    "test": 25,
}

STRONG_KEYWORDS = ("强", "突破", "启动", "放量", "拉升", "异动", "共振", "确认", "站稳", "流入")
MEDIUM_KEYWORDS = ("关注", "接近", "临界", "增强", "回踩", "支撑", "箱体", "资金流")
RISK_KEYWORDS = (
    "拥挤",
    "极负",
    "结算周期缩短",
    "风险加剧",
    "高杠杆",
    "追高",
    "假突破",
    "破位",
    "失败",
    "风险",
    "过热",
)
STRUCTURE_KEYWORDS = ("structure", "结构", "突破", "回踩", "箱体", "支撑", "压力", "确认", "站稳")
STRUCTURE_NEGATIVE_KEYWORDS = ("假突破", "破位", "跌破")
PULLBACK_KEYWORDS = ("拉升", "追高", "过热", "等待", "回踩", "连续", "启动")


def _clamp(value: Any, minimum: int = 0, maximum: int = 100) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        number = minimum
    return max(minimum, min(maximum, number))


def _text(item: dict[str, Any]) -> str:
    fields = (
        item.get("title"),
        item.get("excerpt"),
        item.get("text_html"),
        item.get("signal_type"),
        item.get("stage"),
        item.get("template_id"),
        item.get("module"),
        item.get("status"),
    )
    return " ".join(str(value or "") for value in fields)


def _contains(text: str, keywords: tuple[str, ...]) -> bool:
    lower = text.lower()
    return any(keyword.lower() in lower for keyword in keywords)


def _keyword_count(text: str, keywords: tuple[str, ...]) -> int:
    lower = text.lower()
    return sum(1 for keyword in keywords if keyword.lower() in lower)


def _score_value(item: dict[str, Any]) -> float | None:
    value = item.get("score")
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def signal_strength_score(items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    scores: list[int] = []
    for item in items:
        explicit = _score_value(item)
        module = str(item.get("module") or "").lower()
        base = explicit if explicit is not None else MODULE_WEIGHTS.get(module, 40)
        text = _text(item)
        if _contains(text, STRONG_KEYWORDS):
            base += 12
        elif _contains(text, MEDIUM_KEYWORDS):
            base += 6
        if str(item.get("status") or "").lower() in {"failed", "blocked"}:
            base -= 12
        scores.append(_clamp(base))
    return _clamp(max(scores) * 0.65 + (sum(scores) / len(scores)) * 0.35)


def module_confluence_score(items: list[dict[str, Any]]) -> int:
    modules = {str(item.get("module") or "").lower() for item in items if str(item.get("module") or "").strip()}
    modules.discard("test")
    count = len(modules)
    if count >= 4:
        return 92
    if count == 3:
        return 78
    if count == 2:
        return 58
    if count == 1:
        return 28
    return 0


def signal_density_score(items: list[dict[str, Any]]) -> int:
    count = len(items)
    if count <= 0:
        return 0
    if count == 1:
        return 22
    if count <= 3:
        return 52
    if count <= 8:
        return 70
    if count <= 15:
        return 82
    return 95


def crowding_risk_score(items: list[dict[str, Any]]) -> int:
    if not items:
        return 0
    risk = 0
    funding_count = 0
    for item in items:
        text = _text(item)
        module = str(item.get("module") or "").lower()
        status = str(item.get("status") or "").lower()
        if module == "funding":
            funding_count += 1
            risk += 10
        risk += min(35, _keyword_count(text, RISK_KEYWORDS) * 14)
        if status in {"failed", "blocked"}:
            risk += 16
    if funding_count >= 3:
        risk += 54
    if len(items) >= 10:
        risk += 16
    return _clamp(risk)


def structure_confirmation_score(items: list[dict[str, Any]]) -> int:
    score = 0
    for item in items:
        module = str(item.get("module") or "").lower()
        text = _text(item)
        if module in {"structure", "structure_review"}:
            score += 34
        if _contains(text, STRUCTURE_KEYWORDS):
            score += 16
        if _contains(text, STRUCTURE_NEGATIVE_KEYWORDS):
            score -= 28
    return _clamp(score)


def failure_penalty(items: list[dict[str, Any]]) -> int:
    statuses = Counter(str(item.get("status") or "").lower() for item in items)
    penalty = statuses.get("failed", 0) * 24 + statuses.get("blocked", 0) * 20 + statuses.get("skipped", 0) * 8 + statuses.get("dry_run", 0) * 4
    return _clamp(penalty)


def _risk_level(crowding: int, penalty: int) -> str:
    risk = max(crowding, penalty)
    if risk >= 70:
        return "高"
    if risk >= 35:
        return "中"
    return "低"


def _confidence(scores: dict[str, int], code: str) -> int:
    raw = (
        scores["signal_strength"] * DEFAULT_DECISION_WEIGHTS["signal_strength"]
        + scores["module_confluence"] * DEFAULT_DECISION_WEIGHTS["module_confluence"]
        + scores["signal_density"] * DEFAULT_DECISION_WEIGHTS["signal_density"]
        + scores["structure_confirmation"] * DEFAULT_DECISION_WEIGHTS["structure_confirmation"]
        + scores["crowding_risk"] * DEFAULT_DECISION_WEIGHTS["crowding_risk"]
        + scores["failure_penalty"] * DEFAULT_DECISION_WEIGHTS["failure_penalty"]
    )
    baseline = 42 if code == "observe" else 55
    return _clamp(baseline + raw * 0.65)


def _decision_code(scores: dict[str, int], items: list[dict[str, Any]]) -> str:
    strength = scores["signal_strength"]
    confluence = scores["module_confluence"]
    density = scores["signal_density"]
    risk = scores["crowding_risk"]
    structure = scores["structure_confirmation"]
    penalty = scores["failure_penalty"]
    joined_text = " ".join(_text(item) for item in items)

    if risk >= 82 or (risk >= 68 and penalty >= 35):
        return "risk_alert"
    if risk >= 58 and (strength >= 65 or density >= 75):
        return "no_chase"
    if confluence >= 70 and structure >= 45 and risk < 45 and penalty < 25 and strength >= 55:
        return "trial_position"
    if strength >= 85 and confluence >= 55 and density >= 50 and risk < 58 and structure < 35:
        return "no_chase"
    if strength >= 66 and confluence >= 45 and (structure < 55 or _contains(joined_text, PULLBACK_KEYWORDS) or density >= 70):
        return "wait_pullback"
    if strength >= 75 and risk < 58 and structure < 55 and any(str(item.get("module") or "").lower() in {"launch", "flow"} for item in items):
        return "wait_pullback"
    if confluence >= 55 and structure >= 35 and risk < 55:
        return "wait_pullback"
    return "observe"


def _summary_for(code: str, symbol: str) -> str:
    label = DECISION_DISPLAY[code]["label"]
    prefix = f"{symbol} " if symbol else ""
    if code == "risk_alert":
        return f"{prefix}风险信号显著，优先考虑防守，停止追入并等待风险缓和。"
    if code == "no_chase":
        return f"{prefix}信号较强但短线热度或拥挤风险偏高，不适合追高。"
    if code == "trial_position":
        return f"{prefix}多模块信号共振且风险未明显恶化，可考虑小仓位试探并严格风控。"
    if code == "wait_pullback":
        return f"{prefix}已有启动或突破迹象，但更适合等待回踩、重新站稳或结构确认。"
    return f"{prefix}出现值得关注的信号，但确认条件不足，暂以{label}为主。"


def _reasons(items: list[dict[str, Any]], scores: dict[str, int]) -> list[str]:
    modules = sorted({str(item.get("module") or "").lower() for item in items if item.get("module")})
    statuses = Counter(str(item.get("status") or "").lower() for item in items)
    reasons: list[str] = []
    if modules:
        reasons.append(f"最近窗口出现 {len(modules)} 个模块信号：{'、'.join(modules[:6])}")
    if scores["signal_strength"] >= 70:
        reasons.append("信号强度较高，存在启动或突破迹象")
    elif scores["signal_strength"] >= 45:
        reasons.append("信号强度中等，需要继续等待确认")
    if scores["structure_confirmation"] >= 55:
        reasons.append("结构信号出现确认迹象")
    elif scores["structure_confirmation"] <= 20:
        reasons.append("结构确认不足，暂不适合直接追入")
    if statuses.get("sent", 0):
        reasons.append(f"已有 {statuses.get('sent', 0)} 条已发送信号进入结构化记录")
    return reasons[:6] or ["最近窗口信号数量有限，模型保持保守判断"]


def _risks(items: list[dict[str, Any]], scores: dict[str, int]) -> list[str]:
    risks: list[str] = []
    if scores["crowding_risk"] >= 60:
        risks.append("短线拥挤或追高风险偏高")
    if scores["failure_penalty"] >= 25:
        risks.append("近期 failed / blocked / skipped 较多，降低置信度")
    if scores["signal_density"] >= 80:
        risks.append("短时间信号密度过高，可能代表过热")
    if scores["structure_confirmation"] < 35:
        risks.append("缺少结构确认，容易出现假突破或回落")
    if not risks:
        risks.append("暂未发现显著风险，但仍需等待后续信号确认")
    return risks[:6]


def _watch_points(code: str, scores: dict[str, int]) -> list[str]:
    points = ["观察下一轮资金流是否继续增强", "关注 funding 是否继续拥挤"]
    if code in {"wait_pullback", "no_chase"}:
        points.insert(0, "等待回踩后重新站稳")
    if code == "trial_position":
        points.insert(0, "仅考虑小仓位试探，并预先设置风控条件")
    if code == "risk_alert":
        points.insert(0, "优先确认风险是否缓和，避免新增暴露")
    if scores["structure_confirmation"] < 50:
        points.append("等待结构雷达给出更明确确认")
    return points[:6]


def _related_signals(items: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    related: list[dict[str, Any]] = []
    for item in items[: max(0, int(limit))]:
        related.append({
            "id": item.get("id"),
            "time": item.get("time") or "",
            "module": item.get("module") or "",
            "symbol": item.get("symbol") or "",
            "status": item.get("status") or "",
            "score": item.get("score"),
            "stage": item.get("stage") or "",
            "excerpt": str(redact_api_payload(item.get("excerpt") or item.get("title") or ""))[:180],
        })
    return related


def evaluate_decision(items: list[dict[str, Any]], *, symbol: str = "") -> dict[str, Any]:
    safe_items = [dict(redact_api_payload(item)) for item in items]
    scores = {
        "signal_strength": signal_strength_score(safe_items),
        "module_confluence": module_confluence_score(safe_items),
        "signal_density": signal_density_score(safe_items),
        "crowding_risk": crowding_risk_score(safe_items),
        "structure_confirmation": structure_confirmation_score(safe_items),
        "failure_penalty": failure_penalty(safe_items),
    }
    code = _decision_code(scores, safe_items)
    confidence = _confidence(scores, code)
    scores["total"] = confidence
    display = DECISION_DISPLAY[code]
    normalized_symbol = str(symbol or (safe_items[0].get("symbol") if safe_items else "") or "").upper()
    return {
        "model_version": MODEL_VERSION,
        "symbol": normalized_symbol,
        "decision": {
            "label": display["label"],
            "code": code,
            "tone": display["tone"],
            "confidence": confidence,
            "risk_level": _risk_level(scores["crowding_risk"], scores["failure_penalty"]),
            "summary": _summary_for(code, normalized_symbol),
            "not_advice": NOT_ADVICE_TEXT,
        },
        "scores": scores,
        "reasons": _reasons(safe_items, scores),
        "risks": _risks(safe_items, scores),
        "watch_points": _watch_points(code, scores),
        "related_signals": _related_signals(safe_items),
        "weights": dict(DEFAULT_DECISION_WEIGHTS),
    }


def enhance_signal_with_decision(item: dict[str, Any], decision_payload: dict[str, Any]) -> dict[str, Any]:
    enhanced = dict(redact_api_payload(item))
    decision = decision_payload.get("decision", {}) if isinstance(decision_payload, dict) else {}
    enhanced["decision"] = {
        "label": decision.get("label", "观察"),
        "code": decision.get("code", "observe"),
        "tone": decision.get("tone", "neutral"),
        "confidence": int(decision.get("confidence", 0) or 0),
        "risk_level": decision.get("risk_level", "低"),
    }
    return enhanced
