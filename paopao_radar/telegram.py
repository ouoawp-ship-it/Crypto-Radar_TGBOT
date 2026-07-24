from __future__ import annotations

import json
import re
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
class PushResult:
    status: str
    reason: str
    sent: bool = False
    message_ids: list[int] | None = None
    delivery_id: str = ""


def utc_ts() -> int:
    return int(time.time())


def chunk_text(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        extra = len(line) + (1 if current else 0)
        if current and len(current) + extra > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks or [text]


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
    "TG_ONCHAIN_FLOW_ALERT": "链上交易所资金流",
}

TOPIC_INTRO_VERSION = "2026-07-16-core-radar-v1"


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
        ])
    if template_id == "TG_LAUNCH_ALERT":
        return "\n".join([
        "📌 <b>启动预警使用说明</b>",
        "",
        "这里跟踪币种从“出现异动”到“确认启动”或“信号失效”的过程。每个币种每一轮只保留最新一条“图表 + 说明”，旧版本会自动删除，避免重复刷屏。",
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
        "<b>多久检查一次</b>",
        f"- BOT 默认每{seconds_cn(180)}检查一次市场，但不是使用 3 分钟K线。",
        f"- 所有判断只使用完整收线的 15 分钟K线，并在收线后延迟{seconds_cn(settings.launch_close_delay_sec)}读取，避免使用尚未结束的数据。",
        "- 同一根 15 分钟K线期间即使检查多次，未达到阶段变化或重要替换条件时也只会内部记录，不会重复推送。",
        "",
        "<b>什么时候结束并删除</b>",
        "- 连续两根完整15分钟K线低于观察阈值，或连续两根收盘价跌破本轮有效突破位，本轮信号才会确认失效。",
        "- 确认失效后会删除该轮最新消息；仍然活跃的币种不会被清理。",
        "- 更新消息时，会先确认新消息发送并保存成功，再删除旧版本；删除失败会自动重试。",
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
        "",
        "阅读方式：",
        "1. 真启动候选 = 现货和合约资金共同推动，费率未过热。",
        "2. 吸筹观察 = 价格未大涨，但 OI 和现货主动成交净额提前增强。",
        "3. 合约拉盘/诱多派发 = 合约强于现货，追高风险更高。",
        "4. 可出现 7 类标题：真启动候选、吸筹观察、空头燃料、合约拉盘、挤空/止损、诱多/派发、恐慌下跌；本轮只显示达标分类。",
        "5. 主动成交净额 = taker主动买入报价额 - taker主动卖出报价额；近0为中性。",
        ])
    if template_id == "TG_FUNDING_ALERT":
        return "\n".join([
        "📌 <b>资金费率警报话题说明</b>",
        "",
        "这里专门推送资金费率异常，不和启动雷达、资金流雷达混在一起。",
        "重点看：极负费率、极正费率、多交易所共振、结算周期缩短、单交易所费率明显偏离。",
        "",
        "扫描和发送频率：",
        f"- 默认每{seconds_cn(settings.funding_alert_interval_sec)}扫描一次。",
        f"- 默认扫描 Binance 成交额前 {int(settings.funding_alert_scan_limit)} 个 USDT 合约。",
        f"- 默认交易所：{', '.join(settings.funding_alert_exchanges)}。",
        f"- 同币同类警报默认冷却 {seconds_cn(settings.funding_alert_cooldown_sec)}，风险升级或新类型警报会重新推送。",
        "- 同一个币再次出现信号时，会回复上一条该币资金费率警报，方便沿着同一条消息链追踪。",
        "",
        "阅读方式：",
        "1. 极负费率 = 空头拥挤；如果价格不继续跌，容易变成挤空燃料。",
        "2. 极正费率 = 多头拥挤；如果价格滞涨，追高风险更大。",
        "3. 多交易所共振比单交易所异常更重要，说明不是孤立盘口问题。",
        "4. 结算周期从 8H 到 4H 或 4H 到 1H，代表交易所提高结算频率，应按高风险事件处理。",
        "5. 阶段会从首次异动、拥挤加剧、高危活跃、风险释放到热度衰减逐步跟踪。",
        "6. 交易所偏离 = 最高资金费率 - 最低资金费率，用来判断是否存在单所盘口异常、局部清算压力或套利资金迁移。",
        "7. 资金费率只代表合约拥挤程度，不等于直接买卖方向。",
        ])
    if template_id == "TG_ONCHAIN_FLOW_ALERT":
        return "\n".join([
        "📌 <b>链上交易所资金流话题说明</b>",
        "",
        "这里专门推送已确认的链上代币与中心化交易所之间的异常资金流。",
        "链上资金流使用独立历史、outbox、冷却和小时配额，不占用主 BOT 推送额度。",
        "",
        "阅读方式：",
        "1. 流入交易所代表潜在可售供应，从交易所流出代表潜在提币或积累。",
        "2. 内部调拨、跨交易所、充值归集、低置信标签和缺失价格不会生成方向性警报。",
        "3. 方向评分不是概率，资金流只代表倾向，不保证价格必然上涨或下跌。",
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
            if topic_id:
                print(f"topic_id: {topic_id}")
            if reply_to_message_id:
                print(f"reply_to_message_id: {reply_to_message_id}")
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
            ok, message_ids = self._send_real_photo_bytes(
                photo,
                caption=text,
                parse_mode=parse_mode,
                topic_id=topic_id,
            )
        else:
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
            "sent" if ok else "failed",
            reason,
            ok,
            message_ids,
            delivery_id,
        )
        self._finish_delivery(
            delivery_id,
            status="sent" if ok else "partial" if message_ids else "failed",
            message_ids=message_ids,
        )
        self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id, signal_records=signal_records)
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

    def _finish_delivery(self, delivery_id: str, *, status: str, message_ids: list[int]) -> None:
        now = utc_ts()

        def finish(value: Any) -> list[dict[str, Any]]:
            records = list(value) if isinstance(value, list) else []
            for item in reversed(records):
                if isinstance(item, dict) and item.get("delivery_id") == delivery_id:
                    item["status"] = status
                    item["updated_at"] = now
                    item["completed_chunks"] = len(message_ids)
                    item["message_ids"] = list(message_ids)
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
        ok = True
        message_ids: list[int] = []
        reply_id = int(reply_to_message_id or 0)
        chunks = chunk_text(text, self.settings.tg_push_split_limit)
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
                    if response.status_code == 200:
                        self._append_message_id(response, message_ids)
                        sent = True
                        break
                    if response.status_code == 400 and payload.get("reply_to_message_id"):
                        no_reply = dict(payload)
                        no_reply.pop("reply_to_message_id", None)
                        no_reply.pop("allow_sending_without_reply", None)
                        response = requests.post(url, json=no_reply, timeout=self.settings.tg_push_timeout_sec)
                        if response.status_code == 200:
                            self._append_message_id(response, message_ids)
                            sent = True
                            break
                    if response.status_code == 400 and parse_mode:
                        fallback = dict(payload)
                        fallback.pop("parse_mode", None)
                        fallback["text"] = plain_fallback(chunk)
                        response = requests.post(url, json=fallback, timeout=self.settings.tg_push_timeout_sec)
                        sent = response.status_code == 200
                        if sent:
                            self._append_message_id(response, message_ids)
                        break
                    if response.status_code in {429, 500, 502, 503, 504}:
                        time.sleep(min(5, attempt))
                        continue
                    break
                except Exception:
                    if attempt < self.settings.tg_push_retry:
                        time.sleep(min(5, attempt))
            ok = ok and sent
            time.sleep(0.25)
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
        for attempt in range(1, self.settings.tg_push_retry + 1):
            try:
                response = requests.post(
                    url,
                    data=payload,
                    files={"photo": ("launch-chart.png", photo, "image/png")},
                    timeout=self.settings.tg_push_timeout_sec,
                )
                if response.status_code == 200:
                    message_ids: list[int] = []
                    self._append_message_id(response, message_ids)
                    return True, message_ids
                if response.status_code == 400 and payload.get("parse_mode"):
                    fallback = dict(payload)
                    fallback.pop("parse_mode", None)
                    fallback["caption"] = plain_fallback(caption)[:1024]
                    response = requests.post(
                        url,
                        data=fallback,
                        files={"photo": ("launch-chart.png", photo, "image/png")},
                        timeout=self.settings.tg_push_timeout_sec,
                    )
                    if response.status_code == 200:
                        message_ids = []
                        self._append_message_id(response, message_ids)
                        return True, message_ids
                    return False, []
                if response.status_code in {429, 500, 502, 503, 504}:
                    time.sleep(min(5, attempt))
                    continue
                return False, []
            except Exception:
                if attempt < self.settings.tg_push_retry:
                    time.sleep(min(5, attempt))
        return False, []

    @staticmethod
    def _append_message_id(response: requests.Response, message_ids: list[int]) -> None:
        try:
            data = response.json()
        except ValueError:
            return
        result = data.get("result", {}) if isinstance(data, dict) else {}
        if isinstance(result, dict):
            message_id = result.get("message_id")
            if isinstance(message_id, int):
                message_ids.append(message_id)

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
        except Exception:
            return ""
        if response.status_code != 200:
            print(f"[telegram] createForumTopic failed {response.status_code}: {response.text[:300]}", file=sys.stderr)
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

    def _ensure_topic_intro(self, template_id: str, topic_id: str) -> None:
        if not self.settings.tg_topic_intro_enable:
            return
        intro = topic_intro_message(template_id, self.settings)
        if not intro:
            return
        current_hash = intro_hash(intro)
        intro_key = self._topic_intro_key(template_id, topic_id)
        record = self._topic_intro_record(intro_key)
        if record:
            try:
                message_id = int(record.get("message_id") or 0)
            except (TypeError, ValueError):
                message_id = 0
            is_current = (
                record.get("intro_version") == TOPIC_INTRO_VERSION
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
                return
            if message_id > 0:
                self._delete_message(message_id)
        ok, message_ids = self._send_real_message_ids(intro, parse_mode="HTML", topic_id=topic_id)
        if not ok or not message_ids:
            return
        message_id = message_ids[0]
        pinned = self._pin_message(message_id) if self.settings.tg_topic_intro_pin else False
        self._save_topic_intro_record(intro_key, template_id, topic_id, message_id, pinned, current_hash)

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
            "intro_version": TOPIC_INTRO_VERSION,
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
        except Exception:
            return False
        if response.status_code != 200:
            print(f"[telegram] pinChatMessage failed {response.status_code}: {response.text[:300]}", file=sys.stderr)
            return False
        return True

    def delete_messages(self, message_ids: list[int]) -> int:
        return len(self.delete_messages_detailed(message_ids)["deleted_ids"])

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
        except Exception:
            return False
        if response.status_code != 200:
            print(f"[telegram] deleteMessage failed {response.status_code}: {response.text[:300]}", file=sys.stderr)
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
