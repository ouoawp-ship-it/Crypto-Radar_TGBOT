from __future__ import annotations

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

from config import Settings
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
    signal_store_written: bool | None = None


def utc_ts() -> int:
    return int(time.time())


def chunk_text(text: str, limit: int) -> list[str]:
    if limit <= 0:
        raise ValueError("Telegram chunk limit must be positive")
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        if not line and current:
            if len(current) + 1 <= limit:
                current += "\n"
            else:
                chunks.append(current)
                current = ""
            continue

        if len(line) <= limit:
            if not current:
                current = line
            elif len(current) + 1 + len(line) <= limit:
                current = f"{current}\n{line}"
            else:
                chunks.append(current)
                current = line
            continue

        if current:
            chunks.append(current)
            current = ""
        remaining = line
        while len(remaining) > limit:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
        current = remaining
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
            (("topic_not_modified", "topic is not modified"), "telegram_topic_not_modified"),
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


def _telegram_delivery_is_uncertain(exc: BaseException) -> bool:
    """Return whether Telegram may have accepted a request without replying."""

    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return False
    network_error = classify_telegram_network_error(exc)
    if network_error in {"telegram_dns_failed", "telegram_tls_failed"}:
        return False
    return isinstance(
        exc,
        (
            requests.exceptions.ReadTimeout,
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
        ),
    )


def plain_fallback(text: str) -> str:
    without_tags = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"[*_`]", "", unescape(without_tags))


TOPIC_TEMPLATE_NAMES = {
    "TG_RADAR_SUMMARY": "资金摘要",
    "TG_LAUNCH_ALERT": "脉冲雷达",
    "TG_ANNOUNCEMENT_ALERT": "公告风险",
    "TG_TEST_MESSAGE": "测试消息",
    "TG_FLOW_RADAR": "资金流雷达",
    "TG_FUNDING_ALERT": "资金费率警报",
    "TG_CONSOLIDATION_BREAKOUT": "盘整突破雷达",
    "TG_ALTCOIN_CONTRACT_ANOMALY": "山寨合约异动",
}

PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS = frozenset({
    "TG_ALTCOIN_CONTRACT_ANOMALY",
})

# These templates must carry a valid positive forum-topic ID on every real
# request.  They may still be created by the explicit telegram-topic-setup
# operation; normal delivery never falls back to the group's General topic.
STRICT_TOPIC_TEMPLATE_IDS = frozenset({
    "TG_CONSOLIDATION_BREAKOUT",
})


def _positive_topic_thread_id(value: object) -> int:
    try:
        thread_id = int(str(value or "").strip())
    except (TypeError, ValueError):
        return 0
    return thread_id if thread_id > 0 else 0

PRODUCTION_TOPIC_TEMPLATE_IDS = (
    "TG_RADAR_SUMMARY",
    "TG_LAUNCH_ALERT",
    "TG_ANNOUNCEMENT_ALERT",
    "TG_FLOW_RADAR",
    "TG_FUNDING_ALERT",
)

DEFAULT_TOPIC_INTRO_VERSION = "2026-07-16-core-radar-v1"
TOPIC_INTRO_VERSIONS: dict[str, str] = {
    "TG_ANNOUNCEMENT_ALERT": "2026-08-04-announcement-risk-v1",
    "TG_LAUNCH_ALERT": "2026-08-27-pulse-universe-v4",
    "TG_FLOW_RADAR": "2026-08-30-flow-p1-v1",
    "TG_CONSOLIDATION_BREAKOUT": "2026-08-30-consolidation-breakout-v7",
    "TG_ALTCOIN_CONTRACT_ANOMALY": "2026-08-08-altcoin-contract-anomaly-v1",
}


def topic_intro_version(
    template_id: str,
    settings: Settings | None = None,
) -> str:
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
    if template_id == "TG_ALTCOIN_CONTRACT_ANOMALY":
        return "\n".join([
            "【山寨合约异动｜说明】",
            "",
            "候选依据：",
            "市值、Binance OI/市值、资金费率等基础条件，用于决定监控哪些合约。",
            "",
            "实时确认因子共6类：",
            "1. 价格动量",
            "2. 成交量放大",
            "3. 主动买卖与CVD",
            "4. OI变化",
            "5. 资金费率变化",
            "6. 多空爆仓",
            "",
            "“实时确认：3项”表示当前有3类独立证据达到阈值，",
            "不代表综合分数、成功率或涨跌概率。",
            "",
            "候选依据与实时确认分开展示。",
            "所有信号均附带数据时间和完整度。",
        ])
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
        "- 适合当作阶段性市场总览；短周期异动由“脉冲雷达”负责。",
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
        "📌 <b>脉冲雷达使用说明</b>",
        "",
        "这里由新的脉冲雷达直接接管原启动预警话题，不再运行旧启动评分模型。",
        "",
        "<b>15分钟异动提醒</b>",
        "- 使用完整闭合的5分钟数据，计算15分钟价格、持仓量和主动成交资金流。",
        "- 严格只扫描可靠识别的加密货币合约；传统金融、稳定币和未知资产不会进入提醒。",
        "- 默认完整覆盖24小时成交额不少于100万美元的全部合格合约，不再固定截断为80个。",
        "- 先按市值分为高/中/低/待补全四档，再按流动性分为高/中/低三档；12个组合使用不同门槛。",
        "- 价格与持仓分别判定；持仓触发还要求实际OI金额变化达标，CVD同时检查净额和成交量占比。",
        "- 所有服务共用行情缓存和全局请求限流，细算与触发后图表使用独立预算。",
        "- 信号分为健康上涨、假强背离、空头回补、健康下跌、假弱承接和恐慌杀多。",
        "- 首次触发立即提醒；同一事件只有升级或方向反转才再次发送，最多3次。",
        "- 标题会明确写出价格触发、持仓触发或双触发，避免把持仓涨幅误写成价格涨跌。",
        "- 提醒附带已收线价格K线、OI K线和CVD K线；图表不可用时仍发送完整文字。",
        "",
        "<b>2小时持仓价格背离</b>",
        "- 每2小时汇总持仓变化、价格变化与两者背离度。",
        "- 全市场行情先做成交额过滤，再深入分析成交额靠前的200个合约。",
        "- 分类展示建仓、回调压力、强势突破、恐慌抛售、多头共振和极端背离。",
        "",
        "<b>复盘与安全</b>",
        "- 只有真实发送成功才写入跟随状态和复盘记录；dry-run不会消耗信号。",
        "- 1小时、4小时和2小时复盘分别在对应窗口到期后回填，不使用未来数据。",
        "- 手动执行 pulse-review-report 可查看真实样本数、各窗口命中率和本周TOP 5；不会自动刷榜。",
        "- 真实发送必须同时开启 --send 与 --confirm-real-send；部分发送失败会清理残缺消息。",
        "",
        "数据来自 Binance Spot + USDⓈ-M Futures 已闭合窗口；仅供预警参考，不构成投资建议。",
        ])

    if template_id == "TG_ANNOUNCEMENT_ALERT":
        ttl_days = max(1, int(settings.announcement_default_ttl_days))
        return "\n".join([
        "📌 <b>公告风险话题说明</b>",
        "",
        "这里独立跟踪 Binance 官方公告中的上新、活动、下架和停止交易风险。",
        "公告只说明交易所已公开的事件，不代表币价一定上涨或下跌。",
        "",
        "<b>运行规则</b>",
        "- 只读取 Binance 官方公告，不读取社交传言。",
        "- 只推送识别出币种且仍在有效期内的新公告。",
        f"- 未写明日期的公告默认按约 {ttl_days} 天有效；已发送记录最长保留约14天用于去重。",
        "- 公告独立推送，不参与脉冲雷达分类。",
        "- 本雷达失败不会停止其他四个雷达。",
        "",
        "<b>链接说明</b>",
        "- 点击币种打开 CoinGlass；点击代码可复制交易对；点击 TV 打开 TradingView。",
        "- 点击 Binance 公告可查看官方原文。",
        "",
        "共用同一个 Telegram 机器人；真实发送仍受统一安全门禁控制，不构成投资建议。",
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
        "- 推送正文会写明“统计窗口”，价格、OI、主动成交净额只在窗口数据完整时参与分类。",
        "- 使用 Binance 免费公开数据；主动成交净额由 K 线 taker 主动买入/卖出报价额计算，代表 Binance 内部成交方向，不代表交易所充提或全市场聚合。",
        "- P1.1 使用单一1小时闭合窗口和 7 类互斥规则；取消 60～100 分评分，避免把规则强弱伪装成精确概率。",
        f"- 每轮默认深扫 {settings.flow_scan_limit} 个：固定成交额头部最多 {settings.flow_fixed_top} 个，再加入价格/费率异动币，并永久保留至少四分之一名额用于公平轮换。",
        f"- 异动条件：24小时价格绝对变化≥{settings.flow_ticker_filter_pct:.2f}% 或资金费率绝对值≥{settings.flow_funding_filter_pct:.3f}%；异动过多时不会挤占轮换保底名额。",
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
        "- 资金费率使用 Binance USDⓈ-M 最新快照；缺失不会按 0 参与分类。",
        "- 价格、OI、现货主动成交、合约主动成交、费率五项全部就绪才允许进入信号推送。",
        "- 7 类按固定优先级互斥判定，同一币本轮只进入一个类别；未满足完整定义时不会靠零散条件凑成信号。",
        "",
        "<b>消息保留规则</b>",
        "- 新一轮资金流摘要完整发送并记录成功后，才删除上一轮摘要；发送失败时保留上一轮。",
        "- 如果摘要因长度被拆成多条消息，会保留最新一轮的全部分段。",
        "- 普通推送只保留本轮数据、达标分类、判断和数据确认；不构成投资建议。",
        ])
    if template_id == "TG_CONSOLIDATION_BREAKOUT":
        timeframes = " / ".join(
            str(item).upper()
            for item in settings.consolidation_breakout_timeframes
        )
        return "\n".join([
            "📌 <b>盘整突破雷达话题说明</b>",
            "",
            "这里专门推送已确认收线的盘整突破、跌破、真假验证和三推背离结构，并汇总每日1D盘整地图。",
            "",
            "<b>扫描与结构周期</b>",
            "- 默认覆盖 Binance USDⓈ-M 全部活跃 USDT 永续合约，不按24小时成交额淘汰长尾标的。",
            f"- 按合约代码稳定轮转，每批最多 {int(settings.consolidation_breakout_scan_limit)} 个；轮转进度会持久化，重启后继续。",
            "- 以约520个合约、每批40个、每5分钟一批计算，完整覆盖约需70分钟；达到条件的标的在所属批次立即推送。",
            f"- 旧多周期路径保持不变：{timeframes} 各自识别24 / 72 / 240根冻结箱体。",
            "- 自适应1D产品另用短期20/30/40/50、中期60/90/120/150、长期180/240/300/360/420/500根锚点，并选最长合格结构。",
            "- 自适应1D读取620根历史，采用5%影线裁剪、K线覆盖率、收盘覆盖率、触碰簇、箱宽、路径效率和边界稳定性硬门槛。",
            f"- 调度默认每{seconds_cn(settings.consolidation_breakout_interval_sec)}检查一次，只使用延迟{seconds_cn(settings.consolidation_breakout_close_delay_sec)}后已确认闭合的K线。",
            "",
            "<b>1D结构、4H预警与日报</b>",
            "- 1D收盘负责识别并冻结日线箱体；闭合4H K线只负责越界早期预警，不等同于1D确认；下一根1D收盘再独立确认日线事件。",
            "- 每日地图对应北京时间08:00闭合的UTC日K，但不是08:00定点推送；必须等同一目标日K完成全市场轮转覆盖后才汇总一次。",
            "- 日报默认最多展示20个重点结构，完整结构留在最近7份有界快照；待发只保留最新一份，失败按指数退避，不会恢复后集中补发旧日报。",
            "",
            "<b>1H箱体临界预警</b>",
            "- 独立产品用闭合1H K线冻结24 / 72 / 240根箱体，以15m优先、1H兜底提示价格正在测试上下沿；临近不是突破确认。",
            "- 同一箱体同一方向的一次接近过程只推一次；退回边界至少0.60 ATR并连续保持两根1H后才重新武装。",
            "- 影线越界归扫盘，收盘越界归原突破事件；这些结构事件优先，不再重复发送临近预警。",
            "- 4H / 1D / 1W共振只比较同方向冻结边界，并列明命中周期；没有命中时写明仅1H结构，不代表胜率或必然突破。",
            "",
            "<b>会推送的即时事件</b>",
            "1. 确认上破 / 确认下破：收盘越过冻结箱体边界和ATR缓冲。",
            "2. 放量突破 / 放量跌破：突破K线相对成交量达到配置门槛。",
            "3. 假突破 / 假跌破：突破后3根内重新深度收回箱体。",
            "4. 回踩 / 回抽确认：突破后12根内测试旧边界并再次收在突破方向。",
            "5. 上沿 / 下沿扫盘：影线越界但收盘仍在箱体内。",
            "6. 三推顶 / 三推底形成中：三个连续价格枢轴逐次创新高/新低，并分别匹配三个独立的同周期MACD局部峰谷。",
            "7. 三推背离确认：形成后12根内，收盘越过第二、三推之间冻结的颈线和ATR缓冲。",
            "",
            "<b>箱体与三推质量</b>",
            "- 上下沿各需至少2个分离的触碰簇，并限制箱宽、路径效率和边界漂移。",
            "- 边界确认后冻结，避免新高/新低移动边界制造伪穿越。",
            "- 同一币种同一周期同一根K线若多个期限同时触发，只推优先级最高的一条。",
            "- 价格与MACD枢轴都由左右各2根闭合K线确认，并在前后2根内一一对应，不读取未来数据。",
            "- 两段价格推进各需至少0.10 ATR，两段MACD各需至少弱化5%；形成前后出现更高高点/更低低点时旧第三推立即作废。",
            "- 量能递减和箱体边缘共振都满足为“强”，只满足一项为“一般”，两项都不满足为“弱”并仅更新内部状态，不推送。",
            "- 箱体与三推卡片均取消未经回测校准的/100评分，改用强/标准/观察或强/一般标签，并直接列出通过与未配合的原因及原始数值。",
            "- 三推按币种和周期独立检测，不会因短/中/长期三个箱体重复。",
            f"- 三推背离独立开关当前{'已启用' if getattr(settings, 'consolidation_breakout_three_push_enable', False) else '未启用'}。",
            "",
            "<b>随信号K线图</b>",
            "- 旧路径信号继续附带同周期价格K线、成交量和MACD；自适应1D图最多保留620根，足以复核500日结构。",
            "- 4H越界预警使用1D结构图并明确标为1D STRUCT / 4H TRIGGER，在图上标出4H事件时间和价格，不把它伪装成4H箱体。",
            "- 1H临界预警使用1H STRUCT / 15m TRIGGER或1H STRUCT / 1H TRIGGER，并标出触发时间和价格。",
            "- 三推图分别标出价格P1/P2/P3、三个独立MACD枢轴、冻结颈线和失效位。",
            "- 缺少跨周期图表上下文时只按受限预算补取1D历史；制图或图片校验失败时安全降级为原文字。",
            "- 图表只用于辅助复核，不参与信号判断、质量标签、排序或去重。",
            "",
            "<b>安全门禁</b>",
            f"- 1H临界预警：{'开' if getattr(settings, 'consolidation_hourly_proximity_enable', False) else '关'}；影子模式：{'开' if getattr(settings, 'consolidation_hourly_proximity_shadow_mode', True) else '关'}。",
            f"- 自适应1D产品：{'开' if getattr(settings, 'consolidation_daily_product_enable', False) else '关'}；影子模式：{'开' if getattr(settings, 'consolidation_daily_shadow_mode', True) else '关'}；日报：{'开' if getattr(settings, 'consolidation_daily_digest_enable', False) else '关'}；4H边界预警：{'开' if getattr(settings, 'consolidation_daily_boundary_events_enable', False) else '关'}。",
            "- 新版本默认产品关闭且影子模式开启；真实投递仍必须通过项目统一的发送双门、专属话题路由、去重和outbox。",
            "",
            "数据来自 Binance USDⓈ-M Futures 已闭合K线；仅作结构预警，不构成投资建议。",
        ])
    if template_id == "TG_FUNDING_ALERT":
        return "\n".join([
        "📌 <b>资金费率警报话题说明</b>",
        "",
        "这里专门跟踪 Binance USDⓈ-M Futures 的异常资金费率，不和脉冲雷达、资金流雷达混在一起。",
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
        self._last_delivery_reservation_reason = ""

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

        if daily_limit is not None and daily_limit >= 0 and self._daily_sent_count(history, template_id, now) >= daily_limit:
            result = PushResult("skipped", "template_daily_limit", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result

        if self._hourly_sent_count(history, now) >= self.settings.tg_global_hourly_limit:
            result = PushResult("skipped", "global_hourly_limit", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result

        if not send:
            print("\n========== TELEGRAM 安全演练 ==========")
            print(f"消息模板：{template_id}")
            print(f"防重复标识：{dedup_key}")
            print(
                "话题已配置："
                f"{'是' if topic_id else '否'}"
            )
            print(
                "回复目标已配置："
                f"{'是' if reply_to_message_id else '否'}"
            )
            if photo is not None:
                print(f"图片大小（字节）：{len(photo)}")
            print(text)
            print("========== 安全演练结束 ==============\n")
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

        if template_id in TOPIC_TEMPLATE_NAMES and not topic_id:
            result = PushResult("blocked", "telegram_topic_not_configured", False)
            self._record(
                history,
                template_id,
                dedup_key,
                result,
                text,
                topic_id="",
                reply_to_message_id=reply_to_message_id,
                signal_records=signal_records,
            )
            return result
        strict_topic = template_id in STRICT_TOPIC_TEMPLATE_IDS
        if strict_topic:
            strict_thread_id = _positive_topic_thread_id(topic_id)
            if strict_thread_id <= 0:
                result = PushResult(
                    "blocked",
                    "telegram_topic_invalid",
                    False,
                )
                self._record(
                    history,
                    template_id,
                    dedup_key,
                    result,
                    text,
                    topic_id="",
                    reply_to_message_id=reply_to_message_id,
                    signal_records=signal_records,
                )
                return result
        force_topic = (
            template_id in PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS
            or strict_topic
        )
        if force_topic:
            try:
                thread_id = int(topic_id)
            except (TypeError, ValueError):
                thread_id = 0
            if thread_id <= 0:
                result = PushResult(
                    "blocked",
                    "telegram_topic_not_preconfigured",
                    False,
                )
                self._record(
                    history,
                    template_id,
                    dedup_key,
                    result,
                    text,
                    topic_id="",
                    reply_to_message_id=reply_to_message_id,
                    signal_records=signal_records,
                )
                return result
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
            result = PushResult(
                "skipped",
                self._last_delivery_reservation_reason or "delivery_quarantine",
                False,
            )
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
            return result
        if photo is not None:
            self._last_delivery_diagnostics = None
            ok, message_ids = self._send_real_photo_bytes(
                photo,
                caption=text,
                parse_mode=parse_mode,
                topic_id=topic_id,
                force_topic=force_topic,
                reply_to_message_id=reply_to_message_id,
            )
        else:
            self._last_delivery_diagnostics = None
            ok, message_ids = self._send_real_message_ids(
                text,
                parse_mode=parse_mode,
                topic_id=topic_id,
                force_topic=force_topic,
                reply_to_message_id=reply_to_message_id,
            )
        delivery_uncertain = bool(
            not ok
            and self._last_delivery_diagnostics is not None
            and self._last_delivery_diagnostics.telegram_error_class
            == "telegram_delivery_uncertain"
        )
        if delivery_uncertain:
            reason = "telegram_delivery_uncertain"
        elif photo is not None:
            reason = "telegram_photo_api" if ok else "telegram_photo_api_failed"
        else:
            reason = "telegram_api" if ok else "telegram_api_failed"
        result_status = "sent" if ok else "partial" if message_ids else "failed"
        outbox_status = "uncertain" if delivery_uncertain else result_status
        result = PushResult(
            result_status,
            reason,
            ok,
            message_ids,
            delivery_id,
            self._last_delivery_diagnostics,
        )
        try:
            self._finish_delivery(
                delivery_id,
                status=outbox_status,
                message_ids=message_ids,
                diagnostics=result.diagnostics,
            )
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
        except Exception as exc:
            # Telegram has already accepted these message ids.  Return them to
            # the caller so it can roll back the new card instead of turning a
            # local ledger failure into an untracked delivery.
            result.signal_store_written = False
            print(
                "[telegram] post-delivery local persistence failed "
                f"error={type(exc).__name__}",
                file=sys.stderr,
            )
            return result
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
        reserved = {"ok": True, "reason": ""}
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
                status = str(item.get("status") or "")
                if (
                    template_id in PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS
                    and status == "sent"
                ):
                    reserved["ok"] = False
                    reserved["reason"] = "dedup_cooldown"
                    return records[-MAX_TELEGRAM_HISTORY_ITEMS:]
                if status == "uncertain":
                    reserved["ok"] = False
                    reserved["reason"] = "delivery_quarantine"
                    return records[-MAX_TELEGRAM_HISTORY_ITEMS:]
                if (
                    template_id in PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS
                    and status in {"pending", "partial"}
                ):
                    # After a crash the provider side effect is unknowable.
                    # Keep the production delivery pending for manual recovery
                    # instead of ever risking a duplicate fixed-topic message.
                    reserved["ok"] = False
                    reserved["reason"] = "delivery_quarantine"
                    return records[-MAX_TELEGRAM_HISTORY_ITEMS:]
                updated_at = int(item.get("updated_at", item.get("ts", 0)) or 0)
                if updated_at < quarantine_cutoff:
                    break
                if status in {"pending", "partial", "sent"}:
                    reserved["ok"] = False
                    reserved["reason"] = "delivery_quarantine"
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
        self._last_delivery_reservation_reason = str(reserved.get("reason") or "")
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

    def _send_real_message_ids(
        self,
        text: str,
        parse_mode: str,
        topic_id: str = "",
        force_topic: bool = False,
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
                force_topic
                or self.settings.tg_use_topic
                or str(self.settings.tg_chat_id).startswith("-100")
            ):
                try:
                    thread_id = int(topic_id)
                except ValueError:
                    if force_topic:
                        diagnostics.telegram_error_class = "telegram_bad_request"
                        return False, []
                else:
                    if thread_id <= 0 and force_topic:
                        diagnostics.telegram_error_class = "telegram_bad_request"
                        return False, []
                    if thread_id > 0:
                        payload["message_thread_id"] = thread_id
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
                    diagnostics.telegram_error_class = (
                        "telegram_delivery_uncertain"
                        if _telegram_delivery_is_uncertain(exc)
                        else diagnostics.network_error_class
                    )
                    diagnostics.response_ok = False
                    if diagnostics.telegram_error_class == "telegram_delivery_uncertain":
                        break
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
        force_topic: bool = False,
        reply_to_message_id: int | None = None,
    ) -> tuple[bool, list[int]]:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/sendPhoto"
        payload: dict[str, Any] = {
            "chat_id": self.settings.tg_chat_id,
            "caption": caption,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        reply_id = int(reply_to_message_id or 0)
        if reply_id > 0:
            payload["reply_to_message_id"] = reply_id
            payload["allow_sending_without_reply"] = True
        if topic_id and (
            force_topic
            or self.settings.tg_use_topic
            or str(self.settings.tg_chat_id).startswith("-100")
        ):
            try:
                thread_id = int(topic_id)
            except ValueError:
                if force_topic:
                    return False, []
            else:
                if thread_id <= 0 and force_topic:
                    return False, []
                if thread_id > 0:
                    payload["message_thread_id"] = thread_id
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
                while True:
                    response = requests.post(
                        url,
                        data=dict(payload),
                        files={"photo": ("telegram-card.png", photo, "image/png")},
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
                        diagnostics.telegram_error_class
                        == "telegram_reply_target_not_found"
                        and payload.get("reply_to_message_id")
                        and not diagnostics.reply_fallback_used
                    ):
                        payload.pop("reply_to_message_id", None)
                        payload.pop("allow_sending_without_reply", None)
                        diagnostics.reply_fallback_used = True
                        continue
                    if (
                        diagnostics.telegram_error_class == "telegram_parse_error"
                        and payload.get("parse_mode")
                        and not diagnostics.parse_fallback_used
                    ):
                        payload.pop("parse_mode", None)
                        payload["caption"] = plain_fallback(caption)[:1024]
                        diagnostics.parse_fallback_used = True
                        continue
                    break
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
                diagnostics.telegram_error_class = (
                    "telegram_delivery_uncertain"
                    if _telegram_delivery_is_uncertain(exc)
                    else diagnostics.network_error_class
                )
                diagnostics.response_ok = False
                if diagnostics.telegram_error_class == "telegram_delivery_uncertain":
                    break
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
        if template_id in PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS:
            return self._configured_topic_id_for_template(template_id)
        routed = (
            self._configured_topic_id_for_template(template_id)
            or self._saved_topic_id_for_template(template_id)
        )
        if template_id in TOPIC_TEMPLATE_NAMES:
            return routed
        return routed or self.settings.tg_topic_id

    def topic_route_configured(self, template_id: str) -> bool:
        """Report route readiness without exposing the configured topic ID."""

        topic_id = self._topic_id_for_template(template_id)
        if template_id not in STRICT_TOPIC_TEMPLATE_IDS:
            return bool(topic_id)
        return _positive_topic_thread_id(topic_id) > 0

    def _configured_topic_id_for_template(self, template_id: str) -> str:
        topic_routes = {
            "TG_RADAR_SUMMARY": self.settings.tg_radar_summary_topic_id,
            "TG_LAUNCH_ALERT": self.settings.tg_launch_alert_topic_id,
            "TG_ANNOUNCEMENT_ALERT": self.settings.tg_announcement_alert_topic_id,
            "TG_TEST_MESSAGE": self.settings.tg_test_topic_id,
            "TG_FLOW_RADAR": self.settings.tg_flow_radar_topic_id,
            "TG_FUNDING_ALERT": self.settings.tg_funding_alert_topic_id,
            "TG_CONSOLIDATION_BREAKOUT": str(getattr(
                self.settings,
                "tg_consolidation_breakout_topic_id",
                "",
            ) or "").strip(),
            "TG_ALTCOIN_CONTRACT_ANOMALY": str(getattr(
                self.settings,
                "tg_altcoin_contract_anomaly_topic_id",
                "",
            ) or "").strip(),
        }
        return topic_routes.get(template_id, "")

    def _ensure_topic_id_for_template(self, template_id: str) -> str:
        """Resolve an existing route without creating a Telegram topic."""

        return self._topic_id_for_template(template_id)

    def setup_topic(
        self,
        template_id: str,
        *,
        send: bool,
        confirm_real_send: bool,
    ) -> dict[str, Any]:
        """Explicitly create/reuse one topic and publish its pinned introduction."""

        if template_id not in TOPIC_TEMPLATE_NAMES:
            return {"status": "blocked", "reason": "telegram_topic_template_invalid"}
        if not send:
            return {"status": "blocked", "reason": "send_flag_not_set"}
        if not confirm_real_send:
            return {"status": "blocked", "reason": "missing_confirm_real_send"}
        if not self.settings.tg_bot_token or not self.settings.tg_chat_id:
            return {"status": "blocked", "reason": "telegram_not_configured"}

        if (
            template_id in PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS
            and not self._configured_topic_id_for_template(template_id)
        ):
            return {
                "status": "blocked",
                "reason": "telegram_topic_not_preconfigured",
            }

        topic_id = self._topic_id_for_template(template_id)
        topic_status = "reused"
        if not topic_id:
            topic_id = self._create_and_save_topic(template_id)
            topic_status = "created"
        if not topic_id:
            return {"status": "failed", "reason": "telegram_topic_setup_failed"}
        if (
            template_id in STRICT_TOPIC_TEMPLATE_IDS
            and _positive_topic_thread_id(topic_id) <= 0
        ):
            return {"status": "blocked", "reason": "telegram_topic_invalid"}
        rename_ok = True
        if topic_status == "reused" and template_id == "TG_LAUNCH_ALERT":
            rename_ok = self._rename_forum_topic(
                topic_id,
                TOPIC_TEMPLATE_NAMES[template_id],
            )
        if not self._ensure_topic_intro(template_id, topic_id, require_pin=True):
            return {
                "status": "failed",
                "reason": "telegram_topic_intro_failed",
                "topic": topic_status,
            }
        if not rename_ok:
            return {
                "status": "failed",
                "reason": "telegram_topic_rename_failed",
                "topic": topic_status,
                "intro": "published",
                "pinned": True,
            }
        return {
            "status": "ok",
            "reason": "telegram_topic_setup_complete",
            "template_id": template_id,
            "topic": topic_status,
            "intro": "published",
            "pinned": True,
        }

    def refresh_topic_intro(
        self,
        template_id: str,
        *,
        send: bool,
        confirm_real_send: bool,
    ) -> dict[str, Any]:
        """Refresh one existing topic introduction without creating a topic."""

        if template_id not in TOPIC_TEMPLATE_NAMES:
            return {"status": "blocked", "reason": "telegram_topic_template_invalid"}
        if not send:
            return {"status": "blocked", "reason": "send_flag_not_set"}
        if not confirm_real_send:
            return {"status": "blocked", "reason": "missing_confirm_real_send"}
        if not self.settings.tg_bot_token or not self.settings.tg_chat_id:
            return {"status": "blocked", "reason": "telegram_not_configured"}
        topic_id = self._topic_id_for_template(template_id)
        if not topic_id:
            return {
                "status": "blocked",
                "reason": "telegram_topic_not_configured",
            }
        if (
            template_id in STRICT_TOPIC_TEMPLATE_IDS
            and _positive_topic_thread_id(topic_id) <= 0
        ):
            return {"status": "blocked", "reason": "telegram_topic_invalid"}
        rename_ok = True
        if template_id == "TG_LAUNCH_ALERT":
            rename_ok = self._rename_forum_topic(
                topic_id,
                TOPIC_TEMPLATE_NAMES[template_id],
            )
        if not self._ensure_topic_intro(template_id, topic_id, require_pin=True):
            return {
                "status": "failed",
                "reason": "telegram_topic_intro_failed",
            }
        if not rename_ok:
            return {
                "status": "failed",
                "reason": "telegram_topic_rename_failed",
                "intro": "current_or_refreshed",
                "pinned": True,
            }
        return {
            "status": "ok",
            "reason": "telegram_topic_intro_refresh_complete",
            "template_id": template_id,
            "topic": "reused",
            "intro": "current_or_refreshed",
            "pinned": True,
        }

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
        if template_id in PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS:
            return ""
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

    def _rename_forum_topic(self, topic_id: str, name: str) -> bool:
        try:
            thread_id = int(topic_id)
        except (TypeError, ValueError):
            return False
        if thread_id <= 0 or not name:
            return False
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/editForumTopic"
        payload: dict[str, Any] = {
            "chat_id": self.settings.tg_chat_id,
            "message_thread_id": thread_id,
            "name": name,
        }
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=self.settings.tg_push_timeout_sec,
            )
        except requests.exceptions.RequestException as exc:
            print(
                "[telegram] editForumTopic failed "
                f"error={classify_telegram_network_error(exc)}",
                file=sys.stderr,
            )
            return False
        if response.status_code != 200:
            error_class, _error_code, _retry_after = classify_telegram_response(response)
            if error_class == "telegram_topic_not_modified":
                return True
            print(
                "[telegram] editForumTopic failed "
                f"status={response.status_code} error={error_class}",
                file=sys.stderr,
            )
            return False
        return True

    def _ensure_topic_intro(
        self,
        template_id: str,
        topic_id: str,
        *,
        require_pin: bool = False,
    ) -> bool:
        if (
            template_id in STRICT_TOPIC_TEMPLATE_IDS
            and _positive_topic_thread_id(topic_id) <= 0
        ):
            return False
        if (
            template_id in PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS
            and topic_id != self._configured_topic_id_for_template(template_id)
        ):
            return False
        intro = topic_intro_message(template_id, self.settings)
        if not intro:
            return False
        current_hash = intro_hash(intro)
        current_version = topic_intro_version(template_id, self.settings)
        intro_key = self._topic_intro_key(template_id, topic_id)
        record = self._topic_intro_record(intro_key)
        pin_requested = require_pin or self.settings.tg_topic_intro_pin
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
            if is_current and message_id > 0:
                if pin_requested and not record.get("pinned"):
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
        ok, message_ids = self._send_real_message_ids(
            intro,
            parse_mode="HTML",
            topic_id=topic_id,
            force_topic=(
                template_id in PRECONFIGURED_ONLY_TOPIC_TEMPLATE_IDS
                or template_id in STRICT_TOPIC_TEMPLATE_IDS
            ),
        )
        if not ok or not message_ids:
            return False
        message_id = message_ids[0]
        pinned = self._pin_message(message_id) if pin_requested else False
        if pin_requested and not pinned:
            self._delete_message(message_id)
            return False
        self._save_topic_intro_record(intro_key, template_id, topic_id, message_id, pinned, current_hash)
        if previous_message_id > 0 and previous_message_id != message_id:
            if not self._delete_message(previous_message_id):
                self._unpin_message(previous_message_id)
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
            "intro_version": topic_intro_version(template_id, self.settings),
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

    def _unpin_message(self, message_id: int) -> bool:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/unpinChatMessage"
        payload: dict[str, Any] = {
            "chat_id": self.settings.tg_chat_id,
            "message_id": message_id,
        }
        try:
            response = requests.post(
                url,
                json=payload,
                timeout=self.settings.tg_push_timeout_sec,
            )
        except requests.exceptions.RequestException as exc:
            print(
                "[telegram] unpinChatMessage failed "
                f"error={classify_telegram_network_error(exc)}",
                file=sys.stderr,
            )
            return False
        if response.status_code != 200:
            error_class, _error_code, _retry_after = classify_telegram_response(response)
            print(
                "[telegram] unpinChatMessage failed "
                f"status={response.status_code} error={error_class}",
                file=sys.stderr,
            )
            return False
        return True

    def delete_messages(self, message_ids: list[int]) -> int:
        return len(self.delete_messages_detailed(message_ids)["deleted_ids"])


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
            int(self.settings.topic_message_cleanup_max_age_sec),
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
        reason: str = "telegram_message_delete",
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
                    "lifecycle_delete_reason": str(reason or "telegram_message_delete"),
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
        history.append(record)
        self._append_history_record(record)
        try:
            from .signal_store import append_from_push as append_signal_store_from_push

            written = append_signal_store_from_push(
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
            result.signal_store_written = written > 0
        except Exception as exc:
            result.signal_store_written = False
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
