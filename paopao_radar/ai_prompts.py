from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .config import Settings


DEFAULT_ASSISTANT_PROMPT = """你是泡泡雷达的 AI 助手。回答必须用中文，简洁直接。
你可以解释运行状态、价格提醒和雷达信号，但不能声称自己能直接交易。
涉及投资判断时强调风险，不给确定收益承诺。"""


DEFAULT_ANALYST_PROMPT = """你是泡泡雷达的加密货币数据分析师，专门分析二级市场、合约市场、资金流、链上数据和交易所信号。

你的任务不是复述数据，而是从数据中找出异常、矛盾、主力意图、潜在风险和后续观察点。回答必须基于用户给出的数据，不允许编造不存在的数据。

分析原则：
1. 先识别数据类型：判断用户发来的是启动雷达、资金流、结构突破、合约数据、现货数据、链上转账、公告、K线截图文字，还是混合数据。
2. 先提取关键信息：币种、时间窗口、价格变化、成交量、OI、资金费率、CVD、市值、流动性、清算、盘口、链上转账、公告事件。
3. 做多空矛盾分析：
   - 价涨 + OI涨：新资金追多或突破确认，但要警惕高位诱多。
   - 价涨 + OI跌：空头回补或轧空，持续性要看现货成交和CVD。
   - 价跌 + OI涨：主动开空或多头被压制，容易继续下探。
   - 价跌 + OI跌：多头止损或去杠杆，可能接近短线释放。
   - 价格横盘 + OI上升：杠杆堆积，后续容易出现单边清算。
4. 做现货与合约分歧：
   - 现货强、合约弱：更像真实吸筹。
   - 合约强、现货弱：更像拉盘、诱多或短线逼空。
   - CVD与价格背离时，必须指出背离方向和含义。
5. 做杠杆风险判断：
   - 如果有 OI 和市值，计算 OI / 市值。
   - 大于 5%：杠杆偏高。
   - 大于 20%：高杠杆风险。
   - 大于 100%：极端异常，重点提示多空双爆风险。
   - 如果缺市值或 OI，明确说“无法计算”，不要乱估。
6. 做主力意图推演：
   - 判断更像吸筹、试盘、拉盘、逼空、诱多、派发、砸盘、洗盘还是去杠杆。
   - 必须给出支持这个判断的证据。
   - 同时给出一个反向解释，说明这个判断可能错在哪里。
7. 做风险等级：
   - 使用标签：低风险观察 / 中风险异动 / 高风险博弈 / 极端风险。
   - 给出最关键的失效条件，例如跌回突破位、OI快速回落、现货CVD转负、费率过热等。
8. 输出必须简洁、有穿透力，不要空泛废话，不要重复用户原文。

输出格式固定为：

一、数据类型
说明这是什么数据，以及最重要的字段。

二、核心异常
列出 3-5 个最关键的异常点。

三、多空博弈
分析多头、空头、主力资金分别可能在做什么。

四、风险判断
给出风险等级、主要风险来源、失效条件。

五、后续观察
列出接下来最该盯的 3 个指标。

六、直接结论
用 3 句话以内给出最直接的判断。
不要输出空泛免责声明；但必须输出风险等级、失效条件和不确定性来源。"""


PROMPT_KEYS = ("assistant_prompt", "analyst_prompt")
MAX_PROMPT_LEN = 20000


def default_prompts() -> dict[str, str]:
    return {
        "assistant_prompt": DEFAULT_ASSISTANT_PROMPT,
        "analyst_prompt": DEFAULT_ANALYST_PROMPT,
    }


def _prompt_path(settings: Settings | None = None, path: Path | None = None) -> Path:
    if path is not None:
        return path
    loaded = settings or Settings.load()
    return loaded.ai_prompts_path


def load_ai_prompts(settings: Settings | None = None, path: Path | None = None) -> dict[str, Any]:
    prompt_path = _prompt_path(settings, path)
    defaults = default_prompts()
    loaded: dict[str, Any] = {}
    if prompt_path.exists():
        try:
            data = json.loads(prompt_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                loaded = data
        except Exception:
            loaded = {}
    prompts = {
        key: str(loaded.get(key) or defaults[key]).strip() or defaults[key]
        for key in PROMPT_KEYS
    }
    return {
        "ok": True,
        "path": str(prompt_path),
        "exists": prompt_path.exists(),
        "prompts": prompts,
        "defaults": defaults,
        "updated_at": str(loaded.get("updated_at") or ""),
    }


def save_ai_prompts(
    updates: dict[str, Any],
    settings: Settings | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    prompt_path = _prompt_path(settings, path)
    current = load_ai_prompts(settings, prompt_path)["prompts"]
    changed: list[str] = []
    for key in PROMPT_KEYS:
        if key not in updates:
            continue
        value = str(updates.get(key) or "").strip()
        if not value:
            return {"ok": False, "error": f"{key} 不能为空"}
        if len(value) > MAX_PROMPT_LEN:
            return {"ok": False, "error": f"{key} 不能超过 {MAX_PROMPT_LEN} 字符"}
        if value != current[key]:
            current[key] = value
            changed.append(key)
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        **current,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    prompt_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "path": str(prompt_path),
        "changed": changed,
        "prompts": current,
        "message": "AI 提示词已保存，正在自动应用",
    }


def reset_ai_prompts(settings: Settings | None = None, path: Path | None = None) -> dict[str, Any]:
    prompt_path = _prompt_path(settings, path)
    defaults = default_prompts()
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        json.dumps({**defaults, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {
        "ok": True,
        "path": str(prompt_path),
        "changed": list(PROMPT_KEYS),
        "prompts": defaults,
        "message": "AI 提示词已恢复默认，正在自动应用",
    }
