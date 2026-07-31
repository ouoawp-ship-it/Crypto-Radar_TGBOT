from __future__ import annotations

import json
import re
import socket
import sys
import time
import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from typing import Any

import requests

from .bot_market_context import enrich_telegram_with_market_context

from .config import Settings
from .storage import JsonStore
from .time_windows import CST


MAX_TELEGRAM_HISTORY_ITEMS = 1000


@dataclass
class TelegramDeliveryDiagnostics:
    operation: str = "sendMessage"
    http_attempts: int = 0
    total_chunks: int = 0
    completed_chunks: int = 0
    last_http_status: int | None = None
    telegram_error_code: int | None = None
    telegram_error_class: str = ""
    retry_after_sec: int | None = None
    parse_fallback_used: bool = False
    reply_fallback_used: bool = False
    network_error_class: str = ""
    response_ok: bool = False
    source_text_chars: int = 0
    max_source_line_chars: int = 0
    chunk_count: int = 0
    max_chunk_chars: int = 0

    def public_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "http_attempts": self.http_attempts,
            "total_chunks": self.total_chunks,
            "completed_chunks": self.completed_chunks,
            "last_http_status": self.last_http_status,
            "telegram_error_code": self.telegram_error_code,
            "telegram_error_class": self.telegram_error_class,
            "retry_after_sec": self.retry_after_sec,
            "parse_fallback_used": self.parse_fallback_used,
            "reply_fallback_used": self.reply_fallback_used,
            "network_error_class": self.network_error_class,
            "response_ok": self.response_ok,
            "source_text_chars": self.source_text_chars,
            "max_source_line_chars": self.max_source_line_chars,
            "chunk_count": self.chunk_count,
            "max_chunk_chars": self.max_chunk_chars,
        }

    def audit_fields(self) -> dict[str, Any]:
        return {
            "telegram_http_attempts": self.http_attempts,
            "telegram_last_http_status": self.last_http_status,
            "telegram_error_class": self.telegram_error_class,
            "telegram_completed_chunks": self.completed_chunks,
            "telegram_total_chunks": self.total_chunks,
            "telegram_parse_fallback_used": self.parse_fallback_used,
            "telegram_reply_fallback_used": self.reply_fallback_used,
            "telegram_retry_after_sec": self.retry_after_sec,
        }


@dataclass
class PushResult:
    status: str
    reason: str
    sent: bool = False
    message_ids: list[int] | None = None
    delivery_id: str = ""
    diagnostics: TelegramDeliveryDiagnostics | None = None


def utc_ts() -> int:
    return int(time.time())


def chunk_text(text: str, limit: int) -> list[str]:
    if limit <= 0:
        raise ValueError("Telegram chunk limit must be positive")
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        remaining = line
        while remaining:
            if current:
                available = limit - len(current) - 1
                if available <= 0:
                    chunks.append(current)
                    current = ""
                    continue
                if len(remaining) <= available:
                    current = f"{current}\n{remaining}"
                    remaining = ""
                else:
                    current = f"{current}\n{remaining[:available]}"
                    chunks.append(current)
                    current = ""
                    remaining = remaining[available:]
            elif len(remaining) > limit:
                chunks.append(remaining[:limit])
                remaining = remaining[limit:]
            else:
                current = remaining
                remaining = ""
        if not line and current:
            if len(current) + 1 <= limit:
                current += "\n"
            else:
                chunks.append(current)
                current = ""
    if current:
        chunks.append(current)
    if chunks:
        return chunks
    return [text] if text else []


def telegram_chunk_diagnostics(
    text: str,
    limit: int,
) -> dict[str, int]:
    chunks = chunk_text(text, limit)
    source_lines = text.splitlines() or ([text] if text else [])
    return {
        "source_text_chars": len(text),
        "max_source_line_chars": max((len(line) for line in source_lines), default=0),
        "chunk_count": len(chunks),
        "max_chunk_chars": max((len(chunk) for chunk in chunks), default=0),
    }


def _safe_telegram_error_payload(response: Any) -> tuple[int | None, str, int | None]:
    try:
        payload = response.json()
    except (TypeError, ValueError):
        return None, "", None
    if not isinstance(payload, dict):
        return None, "", None
    error_code = payload.get("error_code")
    normalized_code = (
        error_code
        if isinstance(error_code, int) and not isinstance(error_code, bool)
        else None
    )
    description = payload.get("description")
    normalized_description = (
        description.lower() if isinstance(description, str) else ""
    )
    parameters = payload.get("parameters")
    retry_after: int | None = None
    if isinstance(parameters, dict):
        candidate = parameters.get("retry_after")
        if (
            isinstance(candidate, int)
            and not isinstance(candidate, bool)
            and candidate >= 0
        ):
            retry_after = candidate
    return normalized_code, normalized_description, retry_after


def classify_telegram_response(response: Any) -> tuple[str, int | None, int | None]:
    status = int(getattr(response, "status_code", 0) or 0)
    error_code, description, retry_after = _safe_telegram_error_payload(response)
    if status == 200:
        return "telegram_ok", error_code, retry_after
    if status == 400:
        patterns = (
            (("message thread not found", "thread not found"), "telegram_topic_not_found"),
            (("topic_closed", "topic is closed"), "telegram_topic_closed"),
            (("chat not found",), "telegram_chat_not_found"),
            (("bot is not a member", "bot was kicked"), "telegram_bot_not_member"),
            (("not enough rights", "forbidden to send", "have no rights"), "telegram_send_permission_denied"),
            (("can't parse entities", "cant parse entities"), "telegram_parse_error"),
            (("message is too long",), "telegram_message_too_long"),
            (("reply message not found", "message to be replied not found"), "telegram_reply_target_not_found"),
        )
        for needles, error_class in patterns:
            if any(needle in description for needle in needles):
                return error_class, error_code, retry_after
        return "telegram_bad_request", error_code, retry_after
    if status == 401:
        return "telegram_auth_failed", error_code, retry_after
    if status == 403:
        return "telegram_forbidden", error_code, retry_after
    if status == 404:
        return "telegram_endpoint_not_found", error_code, retry_after
    if status == 429:
        return "telegram_rate_limited", error_code, retry_after
    if 500 <= status <= 599:
        return "telegram_provider_unavailable", error_code, retry_after
    return "telegram_http_error", error_code, retry_after


def classify_telegram_network_error(exc: BaseException) -> str:
    if isinstance(exc, requests.exceptions.Timeout):
        return "telegram_timeout"
    if isinstance(exc, requests.exceptions.SSLError):
        return "telegram_tls_failed"
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if (
            isinstance(current, socket.gaierror)
            or type(current).__name__ == "NameResolutionError"
        ):
            return "telegram_dns_failed"
        for candidate in (current.__cause__, current.__context__):
            if isinstance(candidate, BaseException):
                pending.append(candidate)
        for candidate in getattr(current, "args", ()):
            if isinstance(candidate, BaseException):
                pending.append(candidate)
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "telegram_connection_failed"
    return "telegram_http_error"


def plain_fallback(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[*_`]", "", unescape(without_tags))


TOPIC_TEMPLATE_NAMES = {
    "TG_RADAR_SUMMARY": "资金摘要",
    "TG_LAUNCH_ALERT": "启动预警",
    "TG_ANNOUNCEMENT_ALERT": "公告风险",
    "TG_TEST_MESSAGE": "测试消息",
    "TG_FLOW_RADAR": "资金流雷达",
    "TG_FUNDING_ALERT": "资金费率警报",
    "TG_ONCHAIN_FLOW_ALERT": "链上活动雷达",
}

DEFAULT_TOPIC_INTRO_VERSION = "2026-07-16-core-radar-v1"
TOPIC_INTRO_VERSIONS = {
    "TG_ONCHAIN_FLOW_ALERT": "2026-07-29-oar-p3-v1",
}


def topic_intro_version(template_id: str) -> str:
    return TOPIC_INTRO_VERSIONS.get(
        template_id,
        DEFAULT_TOPIC_INTRO_VERSION,
    )


def seconds_cn(seconds: int) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 86400 and seconds % 86400 == 0:
        return f"{seconds // 86400}天"
    if seconds >= 3600 and seconds % 3600 == 0:
        return f"{seconds // 3600}小时"
    if seconds >= 60 and seconds % 60 == 0:
        return f"{seconds // 60}分钟"
    return f"{seconds}秒"


def intro_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def topic_intro_message(template_id: str, settings: Settings) -> str:
    if template_id == "TG_RADAR_SUMMARY":
        daily = settings.radar_summary_max_daily_push
        daily_text = "不限制" if daily < 0 else f"每天最多{daily}次"
        return "\n".join([
        "📌 <b>资金摘要话题说明</b>",
        "",
        "这里推送定时资金雷达摘要，用来快速浏览市场机会池。",
        "重点看：负费率榜、综合榜、埋伏池、动量池、新币池、背离雷达和值得关注。",
        "",
        "扫描和发送频率：",
        f"- 默认每{seconds_cn(settings.radar_summary_min_interval_sec)}检查并发送一次资金摘要。",
        f"- 资金摘要会在收线后延迟{seconds_cn(settings.radar_summary_close_delay_sec)}抓取上一完整统计窗口，避免使用未收完的数据。",
        f"- 发送上限：{daily_text}，避免大段榜单刷屏。",
        "- 适合当作阶段性市场总览；启动瞬间由“启动预警”负责。",
        "",
        "阅读方式：",
        "1. 先看“值得关注”，这是本轮浓缩结论。",
        "2. 综合榜偏多因子共振，埋伏池偏低位收筹，动量池偏短线活跃。",
        "3. 背离雷达只代表资金和价格不同步，不等于直接买卖信号。",
        "",
        "<b>📖 图例</b>",
        "负费率 = 空头拥挤，可能形成反向燃料",
        "🔥加速 = 费率继续变负",
        "⬇️变负 = 刚从正费率转为负费率",
        "⬆️回升 = 负费率缓和",
        "暗流 = OI增加但价格没动",
        "窗口 = 本次统计窗口内的完整收线数据",
        "背离 = OI窗口变化% - 价格窗口变化%",
        "OI·币安 = OI来自 Binance USDⓈ-M 已闭合窗口，不再使用外部聚合源改写",
        "市值 = Binance市场资料；缺失时为0分，不再使用成交额/OI倍数猜测市值",
        "链接 = 点击币种打开 CoinGlass，点击代码复制交易对，点击 TV 打开 TradingView",
        "",
        "<b>数据来源与计算口径</b>",
        "来源：Binance Spot + Binance USDⓈ-M Futures；仅代表 Binance 市场。",
        "主动成交净额 = taker主动买入报价额 - taker主动卖出报价额",
        "主动净占比 = 主动成交净额 / 总成交额",
        "OI变化 =（窗口末OI - 窗口初OI）/ 窗口初OI",
        "只采用实时或已闭合窗口行情；不改变本模块原触发阈值；不构成投资建议。",
        "",
        "<b>消息保留规则</b>",
        "新一轮资金摘要完整发送并记录成功后，才删除上一轮摘要；发送失败时保留上一轮。",
        "如果摘要因长度被拆成多条消息，会保留最新一轮的全部分段。",
        ])
    if template_id == "TG_LAUNCH_ALERT":
        return "\n".join([
        "📌 <b>启动预警使用说明</b>",
        "",
        "这里跟踪币种从“出现异动”到“确认启动”或“信号失效”的过程。每个尚未失效的币种各保留一条最新的“图表 + 说明”。同一币种出现重要更新时，只替换该币上一条消息，其他仍有效币种的消息不会删除。",
        "",
        "<b>先看什么</b>",
        "1. 先看“当前阶段”，再看“相对首次”和“相对上次”，判断价格、OI、资金费率和主动成交是否继续增强。",
        "2. 提前预警：刚出现异动，继续观察；启动确认：价格、OI、成交等因素进一步共振；启动瞬间：短线波动最强，不代表适合追高。",
        "3. 点击币种打开 CoinGlass；点击代码可复制交易对；点击 TV 打开 TradingView。",
        "",
        "<b>图表怎么看</b>",
        "- E1、E2…是本轮已经发布过的事件位置；带“<”表示更早的事件已超出当前图表范围。",
        "- K线图只使用 Binance 合约已经完整收线的 15 分钟行情。",
        "- 价格和 OI 的变化会分别与本轮首次信号、上一次成功推送的数据比较。",
        "",
        "<b>分数怎么计算（最高130分）</b>",
        "- 15分钟价格上涨≥4%：+25分；1小时价格上涨≥5%：+15分。",
        "- 最新收盘价突破前序约4小时最高价：+25分。",
        "- 最新15分钟成交额达到前序完整窗口均值的2倍：+20分。",
        "- 15分钟 OI 增长≥3%：+15分；1小时 OI 增长≥6%：+15分。",
        "- 资金暗流：1小时 OI 增长≥3%，同时1小时价格在 -2%～+2% 之间：+15分。",
        f"- 阶段图例：<{settings.launch_watch_score} 未触发；"
        f"{settings.launch_watch_score}-{settings.launch_primed_score - 1} 提前观察；"
        f"{settings.launch_primed_score}-{settings.launch_breakout_score - 1} 提前预警；"
        f"{settings.launch_breakout_score}-{settings.launch_launched_score - 1} 启动确认；"
        f"≥{settings.launch_launched_score} 启动瞬间。",
        "- 资金费率极端或结算周期变化只作为拥挤风险提示，目前不直接加分。",
        "",
        "<b>多久检查一次</b>",
        f"- BOT 默认每{seconds_cn(180)}检查一次市场，但不是使用 3 分钟K线。",
        f"- 所有判断只使用完整收线的 15 分钟K线，并在收线后延迟{seconds_cn(settings.launch_close_delay_sec)}读取，避免使用尚未结束的数据。",
        "- 同一根 15 分钟K线期间即使检查多次，未达到阶段变化或重要替换条件时也只会内部记录，不会重复推送。",
        "",
        "<b>什么时候结束并替换</b>",
        "- 连续两根完整15分钟K线低于观察阈值，或连续两根收盘价跌破本轮有效突破位，本轮信号才会确认失效。",
        "- 确认失效后会进入该币的失效清理；其他仍在监控中的币种不受影响。",
        "- 新消息发送并保存成功后，才会删除同一币种的上一条消息。",
        "- 删除失败会在后续更新时自动重试，优先保证每个有效币种的最新消息可见。",
        "",
        "<b>数据来源</b>",
        "- K线、价格、OI和资金费率来自 Binance USDⓈ-M Futures 原生接口。",
        "- 主动成交方向来自 Binance Spot + Futures 已闭合窗口。",
        "- 结果评估只使用已记录的15分钟收盘价，不使用盘中最高价或最低价。",
        "",
        "数据确认仅代表 Binance 市场；不构成投资建议。",
        ])
    if template_id == "TG_ANNOUNCEMENT_ALERT":
        ttl = max(1, int(settings.announcement_default_ttl_days))
        return "\n".join([
        "📌 <b>公告风险话题说明</b>",
        "",
        "这里推送 Binance Alpha、上新、活动机会，以及下架/移除/停止交易等风险事件。",
        "",
        "扫描和发送频率：",
        "- 跟随资金摘要主扫描检查 Binance 公告。",
        "- 默认只处理当天 CST 公告，已经推送过的公告不会重复推。",
        f"- 没有明确截止日期的公告默认保留{ttl}天，过期后尝试删除旧推送。",
        "",
        "阅读方式：",
        "1. 公告机会只代表事件触发，后续仍要等资金面确认。",
        "2. 风险提醒优先级更高，相关币种应暂停新增观察。",
        "",
        "<b>数据来源与正文边界</b>",
        "- 只采用 Binance 官方公告；普通推送只保留事件类型、相关币种、合约状态、公告标题、动态原因和官方链接。",
        "- 公告是已经发生的事件事实，不等于实时价格、OI 或主动成交信号。",
        "",
        "<b>统一处理规则</b>",
        "- 机会事件：记录后等待资金面确认，不因为上新、活动或 Alpha 公告直接判断上涨。",
        "- 风险事件：暂停新增观察，只保留风险记录；下架或停止交易可能令现货、合约流动性快速下降。",
        "- 所有公告仍需核对生效时间和官方原文；不构成投资建议。",
        ])
    if template_id == "TG_FLOW_RADAR":
        return "\n".join([
        "📌 <b>资金流雷达话题说明</b>",
        "",
        "这里推送五因子资金流监控：价格、OI、现货主动成交净额、合约主动成交净额、资金费率。",
        "",
        "扫描和发送频率：",
        f"- 默认每{seconds_cn(settings.flow_interval_sec)}扫描一次，并在整点收线后延迟{seconds_cn(settings.flow_close_delay_sec)}发送。",
        "- 手动执行 flow-radar 会立即扫描，但仍统计上一完整闭合窗口；daemon/live 循环按闭合窗口调度。",
        "- 推送正文会写明“统计窗口”，价格、OI、主动成交净额只在窗口数据完整时参与评分。",
        "- 使用 Binance 免费公开数据；主动成交净额由 K 线 taker 主动买入/卖出报价额计算，代表 Binance 内部成交方向，不代表交易所充提或全市场聚合。",
        "- P0.1 先运行单一1小时闭合窗口；旧评分只在本地留作新旧模型对照，不参与 Telegram 推送。",
        "",
        "阅读方式：",
        "1. 真启动候选 = 现货和合约资金共同推动，费率未过热。",
        "2. 吸筹观察 = 价格未大涨，但 OI 和现货主动成交净额提前增强。",
        "3. 合约拉盘/诱多派发 = 合约强于现货，追高风险更高。",
        "4. 可出现 7 类标题：真启动候选、吸筹观察、空头燃料、合约拉盘、挤空/止损、诱多/派发、恐慌下跌；本轮只显示达标分类。",
        "5. 主动成交净额 = taker主动买入报价额 - taker主动卖出报价额；主动净占比 = 主动成交净额 / 总成交额。",
        "6. 现货和合约方向必须同时通过“绝对净额 + 主动净占比”双门槛；小额偏差或低占比只按中性处理。",
        "",
        "<b>分类图例</b>",
        "- 真启动 = 价格、OI、现货主动成交净额、合约主动成交净额共振，且费率未过热。",
        "- 吸筹 = 价格未明显启动，但 OI 和现货主动成交净额提前增强。",
        "- 空头燃料 = 负费率叠加增仓，属于挤空候选。",
        "- 合约拉盘 = 合约主动买入强、现货主动买入弱，追高风险更高。",
        "- 挤空/止损 = 上涨伴随 OI 下降，可能由空头止损推动。",
        "- 诱多/派发 = 价格上涨但现货主动买入不足。",
        "- 恐慌下跌 = 下跌增仓且主动卖出增强，先按风险处理。",
        "",
        "<b>数据来源与计算口径</b>",
        "- 来源：Binance Spot + Binance USDⓈ-M Futures 原生公开行情；只代表 Binance 市场，不代表交易所充提或全市场聚合。",
        "- 价格变化 =（窗口收盘价 - 窗口开盘价）/ 窗口开盘价。",
        "- OI变化 =（窗口末持仓价值 - 窗口初持仓价值）/ 窗口初持仓价值。",
        "- 主动成交净额 = taker主动买入报价额 - taker主动卖出报价额。",
        f"- 现货双门槛：绝对净额≥${settings.flow_spot_net_min_usd:,.0f}，主动净占比≥{settings.flow_spot_net_ratio_min_pct:.1f}%。",
        f"- 合约双门槛：绝对净额≥${settings.flow_futures_net_min_usd:,.0f}，主动净占比≥{settings.flow_futures_net_ratio_min_pct:.1f}%。",
        "- 资金费率使用 Binance USDⓈ-M 最新快照；缺失不会按 0 参与评分。",
        "- 价格、OI、现货主动成交、合约主动成交、费率五项全部就绪才允许进入信号推送。",
        "- 评分最低从完整分类门禁通过后开始，未满足核心定义时不会靠零散加分凑成信号。",
        "",
        "<b>消息保留规则</b>",
        "- 新一轮资金流摘要完整发送并记录成功后，才删除上一轮摘要；发送失败时保留上一轮。",
        "- 如果摘要因长度被拆成多条消息，会保留最新一轮的全部分段。",
        "- 普通推送只保留本轮数据、达标分类、判断和数据确认；不构成投资建议。",
        ])
    if template_id == "TG_FUNDING_ALERT":
        return "\n".join([
        "📌 <b>资金费率警报话题说明</b>",
        "",
        "这里专门跟踪 Binance USDⓈ-M Futures 的异常资金费率，不和启动雷达、资金流雷达混在一起。",
        "重点观察极负/极正费率、结算周期变化，以及资金费率与价格、OI、主动成交之间的关系。",
        "",
        "<b>扫描与推送</b>",
        f"- 默认每{seconds_cn(settings.funding_alert_interval_sec)}扫描一次。",
        f"- 默认扫描 Binance 成交额前 {int(settings.funding_alert_scan_limit)} 个 USDT 合约。",
        "- 扫描不等于推送：只有首次触发、风险变化或达到有效更新条件时才发送。",
        f"- 同币同类警报默认冷却 {seconds_cn(settings.funding_alert_cooldown_sec)}；风险升级、警报类型变化或本轮结束可重新推送。",
        "- 每个仍在跟踪的币种只保留一条最新消息；新消息发送并保存成功后，才删除该币上一条，其他币种不受影响。",
        "- 最新消息会保留本轮首次快照、相对首次与上次变化以及已发布事件轴；普通扫描只记录，不计入事件轴。",
        "- 删除失败会保存待清理消息编号，并在后续扫描中自动重试。",
        "",
        "<b>最新跟踪卡包含</b>",
        "- 本轮首次出现时间、初始费率、价格、OI和主动成交。",
        "- 当前阶段、资金费率、结算周期和风险状态。",
        "- 相对首次及上次更新的价格、OI、费率和主动成交变化。",
        "- 本轮已发布事件轴；普通扫描只记录，不加入事件轴。",
        "",
        "<b>阅读方式</b>",
        "1. 极负费率 = 空头拥挤；如果价格不继续跌，容易变成挤空燃料。",
        "2. 极正费率 = 多头拥挤；如果价格滞涨，追高风险更大。",
        "3. 费率极端不等于直接买卖信号，必须结合价格、OI和主动成交确认。",
        "4. 结算周期从 8H 到 4H 或 4H 到 1H，代表交易所提高结算频率，应按高风险事件处理。",
        "5. 阶段会从首次异动、拥挤加剧、高危活跃、风险释放、热度衰减到本轮结束逐步跟踪。",
        f"6. 连续 {int(settings.funding_alert_decay_quiet_scans)} 次扫描不再满足异常条件会进入热度衰减；连续 {int(settings.funding_alert_end_quiet_scans)} 次后结束本轮。",
        "7. 本轮结束后再次触发会建立新一轮记录，不与旧轮次混合。",
        "",
        "<b>数据来源与计算口径</b>",
        "- 资金费率、结算时间和OI来自 Binance USDⓈ-M Futures 原生公开接口。",
        "- 价格、OI及主动成交变化使用 Binance 已闭合15分钟窗口。",
        "- 资金费率周期优先使用当前结算时间确认；周期缺失或上下次时间相同时补查 Binance 历史费率。",
        "- 周期补查仍失败时明确显示“本次未确认”，并保留上次已确认周期；不会伪造结算时间。",
        "",
        "<b>统一风险说明</b>",
        "- 资金费率只代表 Binance 合约市场的拥挤程度，不代表全市场，也不等于直接买卖方向。",
        "- 极端费率币种容易上下插针，必须结合价格、OI、主动成交和流动性确认；不构成投资建议。",
        ])
    if template_id == "TG_ONCHAIN_FLOW_ALERT":
        return "\n".join([
        "📌 <b>链上活动雷达话题说明</b>",
        "",
        "这里推送 Base ERC-20 Token 的链上活动规则报告与可选 AI 解读。",
        "链上资金流使用独立历史、outbox、冷却和小时配额，不占用主 BOT 推送额度。",
        "",
        "阅读方式：",
        "1. 流入交易所代表潜在可售供应，从交易所流出代表潜在提币或积累。",
        "2. 行为和钱包关联均为可解释候选；方向评分不是概率，规则分数也不是概率，不确认钱包属于同一主体。",
        "3. AI 只解释确定性事实，失败时仍保留规则摘要；数据不完整时降低结论等级。",
        "4. 入所不等于已经卖出，提币不等于已经买入或必然上涨。",
        ])
    if template_id == "TG_TEST_MESSAGE":
        return "\n".join([
        "📌 <b>测试消息话题说明</b>",
        "",
        "这里用于验证 bot token、群 ID、话题路由、置顶权限是否正常。",
        "",
        "扫描和发送频率：",
        "- 不会自动发送，只在手动执行 telegram-test 时发送。",
        "如果这里能收到消息，说明 Telegram 基础推送链路可用。",
        ])
    return ""


class TelegramGateway:
    def __init__(self, settings: Settings, store: JsonStore):
        self.settings = settings
        self.store = store
        self._last_delivery_diagnostics: TelegramDeliveryDiagnostics | None = None

    def send(
        self,
        text: str,
        template_id: str,
        dedup_key: str,
        *,
        send: bool,
        confirm_real_send: bool,
        cooldown_sec: int | None = None,
        daily_limit: int | None = None,
        parse_mode: str = "Markdown",
        reply_to_message_id: int | None = None,
        signal_records: list[dict[str, Any]] | None = None,
        photo: bytes | None = None,
        enrich_market_context: bool = True,
    ) -> PushResult:
        if (
            template_id == "TG_FUNDING_ALERT"
            and signal_records
            and any(
                isinstance(record, dict)
                and isinstance(record.get("event_snapshot"), dict)
                for record in signal_records
            )
        ):
            enrich_market_context = False
        if (
            enrich_market_context
            and str(parse_mode or "").upper() == "HTML"
            and signal_records
        ):
            text = enrich_telegram_with_market_context(
                self.settings,
                text,
                template_id,
                signal_records,
            )
        now = utc_ts()
        cooldown = self.settings.tg_default_cooldown_sec if cooldown_sec is None else cooldown_sec
        history = self._load_history()
        topic_id = self._topic_id_for_template(template_id)

        photo_error = self._photo_validation_error(photo, text) if photo is not None else ""
        if photo_error:
            result = PushResult("failed", photo_error, False, [])
            self._record(
                history,
                template_id,
                dedup_key,
                result,
                text,
                topic_id=topic_id,
                reply_to_message_id=reply_to_message_id,
                signal_records=signal_records,
            )
            return result

        duplicate = self._recent_match(history, dedup_key, cooldown)
        if duplicate:
            result = PushResult("skipped", "dedup_cooldown", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result

        if daily_limit is not None and daily_limit >= 0 and self._daily_sent_count(history, template_id, now) >= daily_limit:
            result = PushResult("skipped", "template_daily_limit", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result

        if self._hourly_sent_count(history, now) >= self.settings.tg_global_hourly_limit:
            result = PushResult("skipped", "global_hourly_limit", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result

        if not send:
            print("\n========== TELEGRAM DRY-RUN ==========")
            print(f"template_id: {template_id}")
            print(f"dedup_key: {dedup_key}")
            print(
                "topic_configured: "
                f"{str(bool(topic_id)).lower()}"
            )
            print(
                "reply_target_configured: "
                f"{str(bool(reply_to_message_id)).lower()}"
            )
            if photo is not None:
                print(f"photo_bytes: {len(photo)}")
            print(text)
            print("========== END DRY-RUN ==============\n")
            result = PushResult("dry_run", "send_flag_not_set", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result

        if not confirm_real_send:
            result = PushResult("blocked", "missing_confirm_real_send", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result

        if not self.settings.tg_bot_token or not self.settings.tg_chat_id:
            result = PushResult("blocked", "telegram_not_configured", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result

        topic_id = self._ensure_topic_id_for_template(template_id)
        self._ensure_topic_intro(template_id, topic_id)
        delivery_id = self._begin_delivery(
            template_id=template_id,
            dedup_key=dedup_key,
            topic_id=topic_id,
            total_chunks=(
                1
                if photo is not None
                else len(chunk_text(text, self.settings.tg_push_split_limit))
            ),
            now=now,
        )
        if not delivery_id:
            result = PushResult("skipped", "delivery_quarantine", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result
        if photo is not None:
            self._last_delivery_diagnostics = None
            ok, message_ids = self._send_real_photo_bytes(
                photo,
                caption=text,
                parse_mode=parse_mode,
                topic_id=topic_id,
            )
        else:
            self._last_delivery_diagnostics = None
            ok, message_ids = self._send_real_message_ids(
                text,
                parse_mode=parse_mode,
                topic_id=topic_id,
                reply_to_message_id=reply_to_message_id,
            )
        reason = (
            "telegram_photo_api" if ok else "telegram_photo_api_failed"
        ) if photo is not None else (
            "telegram_api" if ok else "telegram_api_failed"
        )
        result = PushResult(
            "sent" if ok else "partial" if message_ids else "failed",
            reason,
            ok,
            message_ids,
            delivery_id,
            self._last_delivery_diagnostics,
        )
        self._finish_delivery(
            delivery_id,
            status="sent" if ok else "partial" if message_ids else "failed",
            message_ids=message_ids,
            diagnostics=result.diagnostics,
        )
        self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
        if template_id == "TG_FUNDING_ALERT" and signal_records:
            if result.sent:
                for record in signal_records:
                    if isinstance(record, dict):
                        record["_funding_delete_callback"] = (
                            self.delete_messages_detailed
                        )
            elif result.message_ids:
                self.delete_messages_detailed(
                    list(result.message_ids),
                    reason="funding_partial_send_rollback",
                )
        if result.sent and template_id == "TG_RADAR_SUMMARY":
            self._cleanup_replaced_summary_messages(result.message_ids or [])
        if template_id == "TG_FLOW_RADAR":
            if result.sent:
                self._cleanup_replaced_flow_messages(result.message_ids or [])
            elif result.message_ids:
                self.delete_messages_detailed(
                    list(result.message_ids),
                    reason="flow_radar_partial_send_rollback",
                )
        return result

    def history_records(self) -> list[dict[str, Any]]:
        return self._load_history()

    def record_result(
        self,
        *,
        template_id: str,
        dedup_key: str,
        result: PushResult,
        text: str,
        signal_records: list[dict[str, Any]] | None = None,
    ) -> None:
        history = self._load_history()
        self._record(
            history,
            template_id,
            dedup_key,
            result,
            text,
            topic_id=self._topic_id_for_template(template_id),
            signal_records=signal_records,
        )

    def annotate_delivery_history(
        self,
        delivery_id: str,
        *,
        deleted_message_ids: list[int],
        failed_delete_message_ids: list[int],
    ) -> None:
        normalized_deleted = sorted(
            {int(item) for item in deleted_message_ids}
        )
        normalized_failed = sorted(
            {int(item) for item in failed_delete_message_ids}
        )

        def update(history: Any) -> list[dict[str, Any]]:
            records = history if isinstance(history, list) else []
            for record in reversed(records):
                if (
                    isinstance(record, dict)
                    and record.get("delivery_id") == delivery_id
                ):
                    record["oar_deleted_old_message_ids"] = normalized_deleted
                    record["oar_failed_delete_message_ids"] = normalized_failed
                    break
            return records

        self.store.update(self.settings.tg_push_history_path, update, [])

    @staticmethod
    def _photo_validation_error(photo: bytes, caption: str) -> str:
        if not isinstance(photo, bytes) or not photo.startswith(b"\x89PNG\r\n\x1a\n"):
            return "invalid_png"
        if len(photo) > 10 * 1024 * 1024:
            return "photo_too_large"
        if len(plain_fallback(caption)) > 1024:
            return "caption_too_long"
        return ""

    def _begin_delivery(
        self,
        *,
        template_id: str,
        dedup_key: str,
        topic_id: str,
        total_chunks: int,
        now: int,
    ) -> str:
        delivery_id = uuid.uuid4().hex
        reserved = {"ok": True}
        retention_cutoff = now - max(1, int(self.settings.tg_outbox_retention_days)) * 86400
        quarantine_cutoff = now - max(60, int(self.settings.tg_outbox_quarantine_sec))

        def reserve(value: Any) -> list[dict[str, Any]]:
            records = [
                item for item in (value if isinstance(value, list) else [])
                if isinstance(item, dict) and int(item.get("ts", now)) >= retention_cutoff
            ]
            for item in reversed(records):
                if item.get("dedup_key") != dedup_key:
                    continue
                updated_at = int(item.get("updated_at", item.get("ts", 0)) or 0)
                if updated_at < quarantine_cutoff:
                    break
                if item.get("status") in {"pending", "partial", "sent"}:
                    reserved["ok"] = False
                    return records[-MAX_TELEGRAM_HISTORY_ITEMS:]
            records.append({
                "delivery_id": delivery_id,
                "ts": now,
                "updated_at": now,
                "template_id": template_id,
                "dedup_key": dedup_key,
                "topic_id": topic_id,
                "status": "pending",
                "total_chunks": max(1, int(total_chunks)),
                "completed_chunks": 0,
                "message_ids": [],
            })
            return records[-MAX_TELEGRAM_HISTORY_ITEMS:]

        self.store.update(self.settings.tg_outbox_path, reserve, [])
        return delivery_id if reserved["ok"] else ""

    def _finish_delivery(
        self,
        delivery_id: str,
        *,
        status: str,
        message_ids: list[int],
        diagnostics: TelegramDeliveryDiagnostics | None = None,
    ) -> None:
        now = utc_ts()

        def finish(value: Any) -> list[dict[str, Any]]:
            records = list(value) if isinstance(value, list) else []
            for item in reversed(records):
                if isinstance(item, dict) and item.get("delivery_id") == delivery_id:
                    item["status"] = status
                    item["updated_at"] = now
                    item["completed_chunks"] = len(message_ids)
                    item["message_ids"] = list(message_ids)
                    if diagnostics is not None:
                        item.update(diagnostics.audit_fields())
                    break
            return records[-MAX_TELEGRAM_HISTORY_ITEMS:]

        self.store.update(self.settings.tg_outbox_path, finish, [])

    def _send_real(
        self,
        text: str,
        parse_mode: str,
        topic_id: str = "",
        reply_to_message_id: int | None = None,
    ) -> bool:
        ok, _message_ids = self._send_real_message_ids(
            text,
            parse_mode=parse_mode,
            topic_id=topic_id,
            reply_to_message_id=reply_to_message_id,
        )
        return ok

    def _send_real_message_ids(
        self,
        text: str,
        parse_mode: str,
        topic_id: str = "",
        reply_to_message_id: int | None = None,
    ) -> tuple[bool, list[int]]:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/sendMessage"
        message_ids: list[int] = []
        reply_id = int(reply_to_message_id or 0)
        chunks = chunk_text(text, self.settings.tg_push_split_limit)
        size_diagnostics = telegram_chunk_diagnostics(
            text,
            self.settings.tg_push_split_limit,
        )
        diagnostics = TelegramDeliveryDiagnostics(
            operation="sendMessage",
            total_chunks=len(chunks),
            source_text_chars=size_diagnostics["source_text_chars"],
            max_source_line_chars=size_diagnostics["max_source_line_chars"],
            chunk_count=size_diagnostics["chunk_count"],
            max_chunk_chars=size_diagnostics["max_chunk_chars"],
        )
        self._last_delivery_diagnostics = diagnostics
        if not chunks:
            diagnostics.telegram_error_class = "telegram_bad_request"
            return False, []
        for idx, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": self.settings.tg_chat_id,
                "text": chunk,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if reply_id > 0 and idx == 0:
                payload["reply_to_message_id"] = reply_id
                payload["allow_sending_without_reply"] = True
            if topic_id and (
                self.settings.tg_use_topic or str(self.settings.tg_chat_id).startswith("-100")
            ):
                try:
                    payload["message_thread_id"] = int(topic_id)
                except ValueError:
                    pass
            sent = False
            for attempt in range(1, self.settings.tg_push_retry + 1):
                try:
                    response = requests.post(url, json=payload, timeout=self.settings.tg_push_timeout_sec)
                    self._record_http_response(diagnostics, response)
                    error_class = diagnostics.telegram_error_class
                    if error_class == "telegram_ok":
                        sent = self._append_message_id(response, message_ids)
                        if not sent:
                            diagnostics.response_ok = False
                            diagnostics.telegram_error_class = "telegram_http_error"
                        break
                    if (
                        error_class == "telegram_reply_target_not_found"
                        and payload.get("reply_to_message_id")
                        and not diagnostics.reply_fallback_used
                    ):
                        no_reply = dict(payload)
                        no_reply.pop("reply_to_message_id", None)
                        no_reply.pop("allow_sending_without_reply", None)
                        diagnostics.reply_fallback_used = True
                        response = requests.post(url, json=no_reply, timeout=self.settings.tg_push_timeout_sec)
                        self._record_http_response(diagnostics, response)
                        if diagnostics.telegram_error_class == "telegram_ok":
                            sent = self._append_message_id(response, message_ids)
                            if not sent:
                                diagnostics.response_ok = False
                                diagnostics.telegram_error_class = "telegram_http_error"
                        break
                    if (
                        error_class == "telegram_parse_error"
                        and parse_mode
                        and not diagnostics.parse_fallback_used
                    ):
                        fallback = dict(payload)
                        fallback.pop("parse_mode", None)
                        fallback["text"] = plain_fallback(chunk)
                        diagnostics.parse_fallback_used = True
                        response = requests.post(url, json=fallback, timeout=self.settings.tg_push_timeout_sec)
                        self._record_http_response(diagnostics, response)
                        if diagnostics.telegram_error_class == "telegram_ok":
                            sent = self._append_message_id(response, message_ids)
                            if not sent:
                                diagnostics.response_ok = False
                                diagnostics.telegram_error_class = "telegram_http_error"
                        break
                    if error_class in {
                        "telegram_rate_limited",
                        "telegram_provider_unavailable",
                    } and attempt < self.settings.tg_push_retry:
                        delay = diagnostics.retry_after_sec or attempt
                        time.sleep(min(5, max(0, delay)))
                        continue
                    break
                except requests.exceptions.RequestException as exc:
                    diagnostics.http_attempts += 1
                    diagnostics.last_http_status = None
                    diagnostics.telegram_error_code = None
                    diagnostics.retry_after_sec = None
                    diagnostics.network_error_class = classify_telegram_network_error(exc)
                    diagnostics.telegram_error_class = diagnostics.network_error_class
                    diagnostics.response_ok = False
                    if attempt < self.settings.tg_push_retry:
                        time.sleep(min(5, attempt))
                        continue
                    break
            if not sent:
                break
            diagnostics.completed_chunks += 1
            time.sleep(0.25)
        ok = diagnostics.completed_chunks == diagnostics.total_chunks
        diagnostics.response_ok = ok
        return ok, message_ids

    def _send_real_photo_bytes(
        self,
        photo: bytes,
        *,
        caption: str,
        parse_mode: str,
        topic_id: str = "",
    ) -> tuple[bool, list[int]]:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/sendPhoto"
        payload: dict[str, Any] = {
            "chat_id": self.settings.tg_chat_id,
            "caption": caption,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if topic_id and (
            self.settings.tg_use_topic
            or str(self.settings.tg_chat_id).startswith("-100")
        ):
            try:
                payload["message_thread_id"] = int(topic_id)
            except ValueError:
                pass
        size_diagnostics = telegram_chunk_diagnostics(
            caption,
            max(1, len(caption) or 1),
        )
        diagnostics = TelegramDeliveryDiagnostics(
            operation="sendPhoto",
            total_chunks=1,
            source_text_chars=size_diagnostics["source_text_chars"],
            max_source_line_chars=size_diagnostics["max_source_line_chars"],
            chunk_count=1,
            max_chunk_chars=len(caption),
        )
        self._last_delivery_diagnostics = diagnostics
        for attempt in range(1, self.settings.tg_push_retry + 1):
            try:
                response = requests.post(
                    url,
                    data=payload,
                    files={"photo": ("launch-chart.png", photo, "image/png")},
                    timeout=self.settings.tg_push_timeout_sec,
                )
                self._record_http_response(diagnostics, response)
                if diagnostics.telegram_error_class == "telegram_ok":
                    message_ids: list[int] = []
                    sent = self._append_message_id(response, message_ids)
                    diagnostics.completed_chunks = 1 if sent else 0
                    diagnostics.response_ok = sent
                    if not sent:
                        diagnostics.telegram_error_class = "telegram_http_error"
                    return sent, message_ids
                if (
                    diagnostics.telegram_error_class == "telegram_parse_error"
                    and payload.get("parse_mode")
                    and not diagnostics.parse_fallback_used
                ):
                    fallback = dict(payload)
                    fallback.pop("parse_mode", None)
                    fallback["caption"] = plain_fallback(caption)[:1024]
                    diagnostics.parse_fallback_used = True
                    response = requests.post(
                        url,
                        data=fallback,
                        files={"photo": ("launch-chart.png", photo, "image/png")},
                        timeout=self.settings.tg_push_timeout_sec,
                    )
                    self._record_http_response(diagnostics, response)
                    if diagnostics.telegram_error_class == "telegram_ok":
                        message_ids = []
                        sent = self._append_message_id(response, message_ids)
                        diagnostics.completed_chunks = 1 if sent else 0
                        diagnostics.response_ok = sent
                        if not sent:
                            diagnostics.telegram_error_class = "telegram_http_error"
                        return sent, message_ids
                    return False, []
                if diagnostics.telegram_error_class in {
                    "telegram_rate_limited",
                    "telegram_provider_unavailable",
                } and attempt < self.settings.tg_push_retry:
                    delay = diagnostics.retry_after_sec or attempt
                    time.sleep(min(5, max(0, delay)))
                    continue
                return False, []
            except requests.exceptions.RequestException as exc:
                diagnostics.http_attempts += 1
                diagnostics.last_http_status = None
                diagnostics.telegram_error_code = None
                diagnostics.retry_after_sec = None
                diagnostics.network_error_class = classify_telegram_network_error(exc)
                diagnostics.telegram_error_class = diagnostics.network_error_class
                diagnostics.response_ok = False
                if attempt < self.settings.tg_push_retry:
                    time.sleep(min(5, attempt))
        return False, []

    @staticmethod
    def _append_message_id(response: requests.Response, message_ids: list[int]) -> bool:
        try:
            data = response.json()
        except ValueError:
            return False
        result = data.get("result", {}) if isinstance(data, dict) else {}
        if isinstance(result, dict):
            message_id = result.get("message_id")
            if isinstance(message_id, int):
                message_ids.append(message_id)
                return True
        return False

    @staticmethod
    def _record_http_response(
        diagnostics: TelegramDeliveryDiagnostics,
        response: requests.Response,
    ) -> None:
        diagnostics.http_attempts += 1
        diagnostics.last_http_status = int(response.status_code)
        error_class, error_code, retry_after = classify_telegram_response(response)
        diagnostics.telegram_error_class = error_class
        diagnostics.telegram_error_code = error_code
        if retry_after is not None or error_class != "telegram_ok":
            diagnostics.retry_after_sec = retry_after
        diagnostics.response_ok = error_class == "telegram_ok"

    def _topic_id_for_template(self, template_id: str) -> str:
        return (
            self._configured_topic_id_for_template(template_id)
            or self._saved_topic_id_for_template(template_id)
            or self.settings.tg_topic_id
        )

    def _configured_topic_id_for_template(self, template_id: str) -> str:
        topic_routes = {
            "TG_RADAR_SUMMARY": self.settings.tg_radar_summary_topic_id,
            "TG_LAUNCH_ALERT": self.settings.tg_launch_alert_topic_id,
            "TG_ANNOUNCEMENT_ALERT": self.settings.tg_announcement_alert_topic_id,
            "TG_TEST_MESSAGE": self.settings.tg_test_topic_id,
            "TG_FLOW_RADAR": self.settings.tg_flow_radar_topic_id,
            "TG_FUNDING_ALERT": self.settings.tg_funding_alert_topic_id,
            "TG_ONCHAIN_FLOW_ALERT": self.settings.tg_onchain_flow_topic_id,
        }
        return topic_routes.get(template_id, "")

    def _ensure_topic_id_for_template(self, template_id: str) -> str:
        topic_id = self._configured_topic_id_for_template(template_id)
        if topic_id:
            return topic_id
        topic_id = self._saved_topic_id_for_template(template_id)
        if topic_id:
            return topic_id
        if not self._should_auto_create_topic(template_id):
            return self.settings.tg_topic_id
        return self._create_and_save_topic(template_id) or self.settings.tg_topic_id

    def _should_auto_create_topic(self, template_id: str) -> bool:
        if template_id not in TOPIC_TEMPLATE_NAMES:
            return False
        if not self.settings.tg_auto_create_topics:
            return False
        chat_id = str(self.settings.tg_chat_id)
        return self.settings.tg_use_topic or chat_id.startswith("-100")

    def _saved_topic_id_for_template(self, template_id: str) -> str:
        data = self.store.load(self.settings.tg_topic_routes_path, {})
        if not isinstance(data, dict):
            return ""
        routes = data.get("routes", {})
        if not isinstance(routes, dict):
            return ""
        record = routes.get(template_id, {})
        if not isinstance(record, dict):
            return ""
        return str(record.get("topic_id") or "")

    def _create_and_save_topic(self, template_id: str) -> str:
        name = TOPIC_TEMPLATE_NAMES.get(template_id)
        if not name:
            return ""
        topic_id = self._create_forum_topic(name)
        if not topic_id:
            return ""
        data = self.store.load(self.settings.tg_topic_routes_path, {})
        if not isinstance(data, dict):
            data = {}
        routes = data.get("routes", {})
        if not isinstance(routes, dict):
            routes = {}
        routes[template_id] = {
            "name": name,
            "topic_id": topic_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        data["routes"] = routes
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.save(self.settings.tg_topic_routes_path, data)
        return topic_id

    def _create_forum_topic(self, name: str) -> str:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/createForumTopic"
        payload: dict[str, Any] = {
            "chat_id": self.settings.tg_chat_id,
            "name": name,
        }
        try:
            response = requests.post(url, json=payload, timeout=self.settings.tg_push_timeout_sec)
        except requests.exceptions.RequestException as exc:
            print(
                "[telegram] createForumTopic failed "
                f"error={classify_telegram_network_error(exc)}",
                file=sys.stderr,
            )
            return ""
        if response.status_code != 200:
            error_class, _error_code, _retry_after = classify_telegram_response(response)
            print(
                "[telegram] createForumTopic failed "
                f"status={response.status_code} error={error_class}",
                file=sys.stderr,
            )
            return ""
        try:
            data = response.json()
        except ValueError:
            return ""
        result = data.get("result", {}) if isinstance(data, dict) else {}
        if not isinstance(result, dict):
            return ""
        topic_id = result.get("message_thread_id")
        return str(topic_id or "")

    def publish_topic_intro(
        self,
        template_id: str,
        *,
        send: bool,
        confirm_real_send: bool,
    ) -> dict[str, object]:
        """Publish only the configured topic intro behind the real-send gates."""
        result: dict[str, object] = {
            "status": "blocked",
            "reason": "real_send_gate_blocked",
            "intro_configured": False,
            "intro_pinned": False,
            "persistent_messages": 0,
        }
        if not send or not confirm_real_send:
            return result
        if not self.settings.tg_bot_token or not self.settings.tg_chat_id:
            result["reason"] = "telegram_not_configured"
            return result
        topic_id = self._topic_id_for_template(template_id)
        if not topic_id:
            result["reason"] = "telegram_topic_not_configured"
            return result

        intro_key = self._topic_intro_key(template_id, topic_id)
        before = self._topic_intro_record(intro_key)
        if not self._ensure_topic_intro(template_id, topic_id):
            result["status"] = "failed"
            result["reason"] = "telegram_topic_intro_failed"
            return result
        after = self._topic_intro_record(intro_key)
        created = bool(after) and after.get("message_id") != before.get(
            "message_id"
        )
        result.update({
            "status": "ok",
            "reason": "",
            "intro_configured": True,
            "intro_pinned": bool(after.get("pinned")),
            "persistent_messages": 1 if created else 0,
        })
        return result

    def _ensure_topic_intro(self, template_id: str, topic_id: str) -> bool:
        if not self.settings.tg_topic_intro_enable:
            return False
        intro = topic_intro_message(template_id, self.settings)
        if not intro:
            return False
        current_hash = intro_hash(intro)
        current_version = topic_intro_version(template_id)
        intro_key = self._topic_intro_key(template_id, topic_id)
        record = self._topic_intro_record(intro_key)
        previous_message_id = 0
        if record:
            try:
                message_id = int(record.get("message_id") or 0)
            except (TypeError, ValueError):
                message_id = 0
            is_current = (
                record.get("intro_version") == current_version
                and record.get("content_hash") == current_hash
            )
            if is_current:
                if self.settings.tg_topic_intro_pin and message_id > 0 and not record.get("pinned"):
                    pinned = self._pin_message(message_id)
                    if pinned:
                        self._save_topic_intro_record(
                            intro_key,
                            template_id,
                            topic_id,
                            message_id,
                            pinned,
                            current_hash,
                        )
                    else:
                        return False
                return True
            previous_message_id = message_id
        ok, message_ids = self._send_real_message_ids(intro, parse_mode="HTML", topic_id=topic_id)
        if not ok or not message_ids:
            return False
        message_id = message_ids[0]
        pinned = self._pin_message(message_id) if self.settings.tg_topic_intro_pin else False
        if self.settings.tg_topic_intro_pin and not pinned:
            self._delete_message(message_id)
            return False
        self._save_topic_intro_record(intro_key, template_id, topic_id, message_id, pinned, current_hash)
        if previous_message_id > 0 and previous_message_id != message_id:
            self._delete_message(previous_message_id)
        return True

    @staticmethod
    def _topic_intro_key(template_id: str, topic_id: str) -> str:
        return f"{template_id}:{topic_id or 'main'}"

    def _topic_intro_record(self, intro_key: str) -> dict[str, Any]:
        data = self.store.load(self.settings.tg_topic_routes_path, {})
        if not isinstance(data, dict):
            return {}
        intros = data.get("intros", {})
        if not isinstance(intros, dict):
            return {}
        record = intros.get(intro_key, {})
        return record if isinstance(record, dict) else {}

    def _save_topic_intro_record(
        self,
        intro_key: str,
        template_id: str,
        topic_id: str,
        message_id: int,
        pinned: bool,
        content_hash: str,
    ) -> None:
        data = self.store.load(self.settings.tg_topic_routes_path, {})
        if not isinstance(data, dict):
            data = {}
        intros = data.get("intros", {})
        if not isinstance(intros, dict):
            intros = {}
        intros[intro_key] = {
            "template_id": template_id,
            "topic_id": topic_id,
            "message_id": message_id,
            "pinned": pinned,
            "intro_version": topic_intro_version(template_id),
            "content_hash": content_hash,
            "sent_at": datetime.now(timezone.utc).isoformat(),
        }
        data["intros"] = intros
        data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.store.save(self.settings.tg_topic_routes_path, data)

    def _pin_message(self, message_id: int) -> bool:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/pinChatMessage"
        payload: dict[str, Any] = {
            "chat_id": self.settings.tg_chat_id,
            "message_id": message_id,
            "disable_notification": True,
        }
        try:
            response = requests.post(url, json=payload, timeout=self.settings.tg_push_timeout_sec)
        except requests.exceptions.RequestException as exc:
            print(
                "[telegram] pinChatMessage failed "
                f"error={classify_telegram_network_error(exc)}",
                file=sys.stderr,
            )
            return False
        if response.status_code != 200:
            error_class, _error_code, _retry_after = classify_telegram_response(response)
            print(
                "[telegram] pinChatMessage failed "
                f"status={response.status_code} error={error_class}",
                file=sys.stderr,
            )
            return False
        return True

    def delete_messages(self, message_ids: list[int]) -> int:
        return len(self.delete_messages_detailed(message_ids)["deleted_ids"])

    @staticmethod
    def _latest_launch_topic_message_ids(
        history: list[dict[str, Any]],
    ) -> list[int]:
        launch_records = [
            record
            for record in history
            if isinstance(record, dict)
            and record.get("template_id") == "TG_LAUNCH_ALERT"
            and record.get("status") == "sent"
            and not record.get("lifecycle_deleted")
        ]
        for _index, record in sorted(
            enumerate(launch_records),
            key=lambda item: (int(item[1].get("ts") or 0), item[0]),
            reverse=True,
        ):
            message_ids = [
                int(message_id)
                for message_id in (record.get("message_ids") or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            ]
            deleted_ids = {
                int(message_id)
                for message_id in (record.get("deleted_message_ids") or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            }
            latest_ids = [
                message_id
                for message_id in message_ids
                if message_id not in deleted_ids
            ]
            if latest_ids:
                return latest_ids
        return []

    def latest_launch_topic_message_ids(self) -> list[int]:
        return self._latest_launch_topic_message_ids(self._load_history())

    def summary_topic_cleanup_plan(
        self,
        *,
        keep_message_ids: list[int],
    ) -> dict[str, list[int]]:
        return self._latest_topic_cleanup_plan(
            "TG_RADAR_SUMMARY",
            keep_message_ids=keep_message_ids,
        )

    def flow_topic_cleanup_plan(
        self,
        *,
        keep_message_ids: list[int],
    ) -> dict[str, list[int]]:
        return self._latest_topic_cleanup_plan(
            "TG_FLOW_RADAR",
            keep_message_ids=keep_message_ids,
        )

    def _latest_topic_cleanup_plan(
        self,
        template_id: str,
        *,
        keep_message_ids: list[int],
    ) -> dict[str, list[int]]:
        protected = {
            int(message_id)
            for message_id in keep_message_ids
            if isinstance(message_id, int) or str(message_id).isdigit()
        }
        intro_key = self._topic_intro_key(
            template_id,
            self._topic_id_for_template(template_id),
        )
        intro = self._topic_intro_record(intro_key)
        intro_message_id = intro.get("message_id")
        if isinstance(intro_message_id, int) or str(intro_message_id or "").isdigit():
            protected.add(int(intro_message_id))

        cutoff = utc_ts() - max(
            1,
            int(self.settings.launch_message_cleanup_max_age_sec),
        )
        deletable_ids: list[int] = []
        undeletable_ids: list[int] = []
        planned_ids: set[int] = set()
        for record in self._load_history():
            if (
                not isinstance(record, dict)
                or record.get("template_id") != template_id
                or record.get("status") != "sent"
            ):
                continue
            deleted_ids = {
                int(message_id)
                for message_id in (record.get("deleted_message_ids") or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            }
            unavailable_ids = {
                int(message_id)
                for message_id in (record.get("undeletable_message_ids") or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            }
            destination = (
                deletable_ids
                if int(record.get("ts") or 0) >= cutoff
                else undeletable_ids
            )
            for message_id in record.get("message_ids") or []:
                if not (isinstance(message_id, int) or str(message_id).isdigit()):
                    continue
                normalized = int(message_id)
                if (
                    normalized not in protected
                    and normalized not in deleted_ids
                    and normalized not in unavailable_ids
                    and normalized not in planned_ids
                ):
                    destination.append(normalized)
                    planned_ids.add(normalized)
        return {
            "deletable_ids": deletable_ids,
            "undeletable_ids": undeletable_ids,
        }

    def _cleanup_replaced_summary_messages(
        self,
        keep_message_ids: list[int],
    ) -> None:
        self._cleanup_replaced_topic_messages(
            "TG_RADAR_SUMMARY",
            keep_message_ids,
            reason_prefix="radar_summary",
        )

    def _cleanup_replaced_flow_messages(
        self,
        keep_message_ids: list[int],
    ) -> None:
        self._cleanup_replaced_topic_messages(
            "TG_FLOW_RADAR",
            keep_message_ids,
            reason_prefix="flow_radar",
        )

    def _cleanup_replaced_topic_messages(
        self,
        template_id: str,
        keep_message_ids: list[int],
        *,
        reason_prefix: str,
    ) -> None:
        try:
            plan = self._latest_topic_cleanup_plan(
                template_id,
                keep_message_ids=keep_message_ids,
            )
            undeletable_ids = list(plan.get("undeletable_ids") or [])
            if undeletable_ids:
                self.mark_history_messages_undeletable(
                    undeletable_ids,
                    reason=f"{reason_prefix}_delete_window_expired",
                )
            deletable_ids = list(plan.get("deletable_ids") or [])
            if deletable_ids:
                self.delete_messages_detailed(
                    deletable_ids,
                    reason=f"{reason_prefix}_replaced",
                )
        except Exception as exc:
            print(
                f"[telegram] {reason_prefix} cleanup failed {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def launch_topic_cleanup_candidates(
        self,
        *,
        keep_message_ids: list[int] | None = None,
    ) -> list[int]:
        return self.launch_topic_cleanup_plan(
            keep_message_ids=keep_message_ids,
        )["deletable_ids"]

    def launch_topic_cleanup_plan(
        self,
        *,
        keep_message_ids: list[int] | None = None,
    ) -> dict[str, list[int]]:
        history = self._load_history()
        explicit_keep = bool(keep_message_ids)
        protected = {
            int(message_id)
            for message_id in (keep_message_ids or [])
            if isinstance(message_id, int) or str(message_id).isdigit()
        }
        intro_key = self._topic_intro_key(
            "TG_LAUNCH_ALERT",
            self._topic_id_for_template("TG_LAUNCH_ALERT"),
        )
        intro = self._topic_intro_record(intro_key)
        intro_message_id = intro.get("message_id")
        if isinstance(intro_message_id, int) or str(intro_message_id or "").isdigit():
            protected.add(int(intro_message_id))

        launch_records = [
            record
            for record in history
            if isinstance(record, dict)
            and record.get("template_id") == "TG_LAUNCH_ALERT"
            and record.get("status") == "sent"
        ]
        if not explicit_keep:
            protected.update(self._latest_launch_topic_message_ids(history))

        cutoff = utc_ts() - max(
            1,
            int(self.settings.launch_message_cleanup_max_age_sec),
        )
        deletable_ids: list[int] = []
        undeletable_ids: list[int] = []
        planned_ids: set[int] = set()
        for record in launch_records:
            deleted_ids = {
                int(message_id)
                for message_id in (record.get("deleted_message_ids") or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            }
            unavailable_ids = {
                int(message_id)
                for message_id in (record.get("undeletable_message_ids") or [])
                if isinstance(message_id, int) or str(message_id).isdigit()
            }
            destination = (
                deletable_ids
                if int(record.get("ts") or 0) >= cutoff
                else undeletable_ids
            )
            for message_id in record.get("message_ids") or []:
                if not (isinstance(message_id, int) or str(message_id).isdigit()):
                    continue
                normalized = int(message_id)
                if (
                    normalized not in protected
                    and normalized not in deleted_ids
                    and normalized not in unavailable_ids
                    and normalized not in planned_ids
                ):
                    destination.append(normalized)
                    planned_ids.add(normalized)
        return {
            "deletable_ids": deletable_ids,
            "undeletable_ids": undeletable_ids,
        }

    def mark_history_messages_undeletable(
        self,
        message_ids: list[int],
        *,
        reason: str = "telegram_delete_window_expired",
    ) -> None:
        unavailable = {
            int(message_id)
            for message_id in message_ids
            if isinstance(message_id, int) or str(message_id).isdigit()
        }
        if not unavailable:
            return
        now_ts = utc_ts()

        def update_history(history: Any) -> list[dict[str, Any]]:
            records = history if isinstance(history, list) else []
            updated: list[dict[str, Any]] = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                record_message_ids = {
                    int(message_id)
                    for message_id in (record.get("message_ids") or [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                }
                matched = record_message_ids & unavailable
                if not matched:
                    updated.append(record)
                    continue
                existing = {
                    int(message_id)
                    for message_id in (record.get("undeletable_message_ids") or [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                }
                updated.append({
                    **record,
                    "undeletable_message_ids": sorted(existing | matched),
                    "lifecycle_undeletable_at": now_ts,
                    "lifecycle_undeletable_reason": str(reason),
                })
            return updated

        self.store.update(self.settings.tg_push_history_path, update_history, [])

    def delete_messages_detailed(
        self,
        message_ids: list[int],
        *,
        reason: str = "launch_signal_expired",
    ) -> dict[str, list[int]]:
        normalized_ids = list(dict.fromkeys(
            int(message_id)
            for message_id in message_ids
            if isinstance(message_id, int) or str(message_id).isdigit()
        ))
        if not self.settings.tg_bot_token or not self.settings.tg_chat_id:
            return {"deleted_ids": [], "failed_ids": normalized_ids}
        deleted_ids: list[int] = []
        failed_ids: list[int] = []
        for message_id in normalized_ids:
            if self._delete_message(message_id):
                deleted_ids.append(message_id)
            else:
                failed_ids.append(message_id)
            time.sleep(0.15)
        if deleted_ids:
            self._mark_history_messages_deleted(deleted_ids, reason=reason)
        return {"deleted_ids": deleted_ids, "failed_ids": failed_ids}

    def _mark_history_messages_deleted(
        self,
        message_ids: list[int],
        *,
        reason: str,
    ) -> None:
        deleted = {int(message_id) for message_id in message_ids}
        now_ts = utc_ts()

        def update_history(history: Any) -> list[dict[str, Any]]:
            records = history if isinstance(history, list) else []
            updated: list[dict[str, Any]] = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                record_message_ids = {
                    int(message_id)
                    for message_id in (record.get("message_ids") or [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                }
                matched = record_message_ids & deleted
                if not matched:
                    updated.append(record)
                    continue
                existing = {
                    int(message_id)
                    for message_id in (record.get("deleted_message_ids") or [])
                    if isinstance(message_id, int) or str(message_id).isdigit()
                }
                deleted_for_record = sorted(existing | matched)
                updated.append({
                    **record,
                    "deleted_message_ids": deleted_for_record,
                    "lifecycle_deleted": bool(record_message_ids) and record_message_ids <= set(deleted_for_record),
                    "lifecycle_deleted_at": now_ts,
                    "lifecycle_delete_reason": str(reason or "launch_signal_expired"),
                })
            return updated

        self.store.update(self.settings.tg_push_history_path, update_history, [])

    def _delete_message(self, message_id: int) -> bool:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/deleteMessage"
        payload: dict[str, Any] = {
            "chat_id": self.settings.tg_chat_id,
            "message_id": message_id,
        }
        try:
            response = requests.post(url, json=payload, timeout=self.settings.tg_push_timeout_sec)
        except requests.exceptions.RequestException as exc:
            print(
                "[telegram] deleteMessage failed "
                f"error={classify_telegram_network_error(exc)}",
                file=sys.stderr,
            )
            return False
        if response.status_code != 200:
            error_class, _error_code, _retry_after = classify_telegram_response(response)
            print(
                "[telegram] deleteMessage failed "
                f"status={response.status_code} error={error_class}",
                file=sys.stderr,
            )
            return False
        return True

    def _load_history(self) -> list[dict[str, Any]]:
        data = self.store.load(self.settings.tg_push_history_path, [])
        return data if isinstance(data, list) else []

    def _append_history_record(self, record: dict[str, Any]) -> None:
        now = int(time.time())
        retention_days = max(1, int(self.settings.tg_push_history_retention_days))
        cutoff = now - retention_days * 86400
        limit = min(MAX_TELEGRAM_HISTORY_ITEMS, max(100, int(self.settings.tg_push_history_limit)))

        def append(history: Any) -> list[dict[str, Any]]:
            records = history if isinstance(history, list) else []
            retained = [
                item for item in records
                if isinstance(item, dict) and int(item.get("ts", now)) >= cutoff
            ]
            retained.append(record)
            if len(retained) <= limit:
                return retained

            # Sent entries are the decision ledger for cooldown/hourly/daily
            # limits.  Never let high-volume skipped or dry-run audit entries
            # evict a still-retained sent entry, otherwise compaction could
            # change Telegram delivery semantics.  Non-sent audit entries use
            # the remaining bounded capacity.
            sent_count = sum(1 for item in retained if item.get("status") == "sent")
            audit_budget = max(0, limit - sent_count)
            compacted_reversed: list[dict[str, Any]] = []
            for item in reversed(retained):
                if item.get("status") == "sent":
                    compacted_reversed.append(item)
                elif audit_budget > 0:
                    compacted_reversed.append(item)
                    audit_budget -= 1
            compacted_reversed.reverse()
            return compacted_reversed

        self.store.update(self.settings.tg_push_history_path, append, [])

    def _record(
        self,
        history: list[dict[str, Any]],
        template_id: str,
        dedup_key: str,
        result: PushResult,
        text: str,
        topic_id: str = "",
        reply_to_message_id: int | None = None,
        signal_records: list[dict[str, Any]] | None = None,
    ) -> None:
        now = utc_ts()
        record = {
            "ts": now,
            "time": datetime.now(timezone.utc).isoformat(),
            "template_id": template_id,
            "dedup_key": dedup_key,
            "topic_id": topic_id,
            "status": result.status,
            "reason": result.reason,
            "sent": result.sent,
            "message_ids": result.message_ids or [],
            "delivery_id": result.delivery_id,
            "reply_to_message_id": int(reply_to_message_id or 0),
            "preview": text[:240],
        }
        if result.diagnostics is not None:
            record.update(result.diagnostics.audit_fields())
        if template_id == "TG_ONCHAIN_FLOW_ALERT" and signal_records:
            allowed = {
                "oar_card_key",
                "oar_content_hash",
                "chain_id",
                "contract",
                "symbol",
                "behavior_type",
                "score",
                "behavior_score",
                "context_hash",
                "analysis_status",
                "analysis_complete",
                "ai_status",
                "linked_source_count",
                "source_modules_text",
            }
            record["signal_records"] = [
                {
                    key: value
                    for key, value in item.items()
                    if key in allowed
                    and (
                        value is None
                        or isinstance(value, (str, int, float, bool))
                    )
                }
                for item in signal_records
                if isinstance(item, dict)
            ]
        history.append(record)
        self._append_history_record(record)
        try:
            from .symbol_dossier import append_signal_events_from_push

            append_signal_events_from_push(
                self.settings,
                self.store,
                template_id=template_id,
                dedup_key=dedup_key,
                status=result.status,
                sent=result.sent,
                text=text,
                ts=now,
                topic_id=topic_id,
                message_ids=result.message_ids or [],
                reply_to_message_id=reply_to_message_id,
            )
        except Exception as exc:
            print(f"[telegram] signal event index failed {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            from .signal_store import append_from_push as append_signal_store_from_push

            append_signal_store_from_push(
                self.settings,
                template_id=template_id,
                dedup_key=dedup_key,
                status=result.status,
                sent=result.sent,
                text=text,
                ts=now,
                topic_id=topic_id,
                message_ids=result.message_ids or [],
                reply_to_message_id=reply_to_message_id,
                structured_records=signal_records,
            )
        except Exception as exc:
            print(f"[telegram] signal store write failed {type(exc).__name__}: {exc}", file=sys.stderr)

    @staticmethod
    def _recent_match(history: list[dict[str, Any]], dedup_key: str, cooldown_sec: int) -> bool:
        if cooldown_sec <= 0:
            return False
        cutoff = utc_ts() - cooldown_sec
        for record in reversed(history):
            if record.get("dedup_key") != dedup_key:
                continue
            if int(record.get("ts", 0)) < cutoff:
                return False
            if record.get("status") == "sent" and not record.get("lifecycle_deleted"):
                return True
        return False

    @staticmethod
    def _hourly_sent_count(history: list[dict[str, Any]], now: int) -> int:
        cutoff = now - 3600
        return sum(1 for record in history if int(record.get("ts", 0)) >= cutoff and record.get("status") == "sent")

    @staticmethod
    def _daily_sent_count(history: list[dict[str, Any]], template_id: str, now: int) -> int:
        start_of_day = int(
            datetime.fromtimestamp(now, CST)
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .timestamp()
        )
        return sum(
            1 for record in history
            if record.get("template_id") == template_id
            and int(record.get("ts", 0)) >= start_of_day
            and record.get("status") == "sent"
        )
