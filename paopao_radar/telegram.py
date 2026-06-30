from __future__ import annotations

import re
import sys
import time
import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from typing import Any

import requests

from .config import Settings
from .storage import JsonStore
from .time_windows import CST


@dataclass
class PushResult:
    status: str
    reason: str
    sent: bool = False
    message_ids: list[int] | None = None


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
    "TG_STRUCTURE_RADAR": "结构突破",
    "TG_STRUCTURE_REVIEW": "结构复盘",
}

TOPIC_INTRO_VERSION = "2026-05-26-multisource-liquidity-v1"


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
        "📌 <b>启动预警话题说明</b>",
        "",
        "这里推送即时启动雷达，偏短周期异动提醒。",
        "重点看：阶段、分数、15m/1h价格、15m/1h OI、成交量放大和触发原因。",
        "",
        "扫描和发送频率：",
        f"- 默认每{seconds_cn(180)}检查一次；服务启动参数 --launch-interval 可以覆盖。",
        f"- 启动判断使用最近完整 15m 收线窗口，默认收线后延迟{seconds_cn(settings.launch_close_delay_sec)}再抓取。",
        f"- 同币同阶段默认冷却{seconds_cn(settings.launch_stage_cooldown_sec)}，避免重复刷同一阶段。",
        "- 同一币种后续更高阶段信号会自动回复上一条该币启动消息，方便沿着一条消息链追踪。",
        "",
        "阅读方式：",
        "1. 提前预警 = 开始异动，适合加入盯盘。",
        "2. 启动确认 = 多因子共振更强，但仍要等结构确认。",
        "3. 启动瞬间 = 波动最大，避免盲目追高。",
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
        "这里推送五因子资金流监控：价格、OI、现货CVD、合约CVD、资金费率。",
        "",
        "扫描和发送频率：",
        f"- 默认每{seconds_cn(settings.flow_interval_sec)}扫描一次，并在整点收线后延迟{seconds_cn(settings.flow_close_delay_sec)}发送。",
        "- 手动执行 flow-radar 会立即扫描，但仍统计上一完整闭合窗口；daemon/live 循环按闭合窗口调度。",
        "- 推送正文会写明“统计窗口”，价格、OI、CVD 只在窗口数据完整时参与评分。",
        "- 使用 Binance 免费公开数据；CVD 由 K 线主动买入成交额估算，代表 Binance 内部资金流，不代表全市场聚合。",
        "",
        "阅读方式：",
        "1. 真启动候选 = 现货和合约资金共同推动，费率未过热。",
        "2. 吸筹观察 = 价格未大涨，但 OI 和现货CVD提前增强。",
        "3. 合约拉盘/诱多派发 = 合约强于现货，追高风险更高。",
        "4. 可出现 7 类标题：真启动候选、吸筹观察、空头燃料、合约拉盘、挤空/止损、诱多/派发、恐慌下跌；本轮只显示达标分类。",
        "5. CVD 为主动买入量减主动卖出量；近0为中性，不按主动买入或主动卖出评分。",
        ])
    if template_id == "TG_STRUCTURE_RADAR":
        return "\n".join([
        "📌 <b>结构突破雷达话题说明</b>",
        "",
        "这里推送盘整箱体、压缩、临界突破和收线确认信号。",
        "",
        "扫描和发送频率：",
        f"- 提前临界扫描：每小时 {int(settings.structure_pre_scan_minute):02d} 分附近运行，只作为提前预警。",
        f"- 收线确认扫描：整点收线后延迟 {seconds_cn(settings.structure_confirm_delay_sec)} 运行，使用完整闭合 K 线。",
        f"- 单币同类结构信号冷却：{seconds_cn(settings.structure_cooldown_sec)}。",
        f"- K线图：默认最多随每轮前 {int(settings.structure_send_chart_top_n)} 个信号发送。",
        "",
        "信号类型：",
        "- 临近上沿 / 临近下沿：价格靠近箱体边缘，等待突破或跌破。",
        "- 压缩观察：ATR 或 BB 宽度压缩，说明波动正在收敛。",
        "- 突破确认 / 跌破确认：完整收线站上箱体上沿或跌破箱体下沿。",
        "- 假突破 / 假跌破：前面出现临界或突破，后续又收回箱体。",
        "",
        "评分说明：",
        "- 评分 = 边缘距离20 + 结构15 + 触碰10 + 压缩15 + 成交量10 + OI10 + 主动买卖10 + 高周期5 + Funding5。",
        "- 外部确认：使用Binance盘口深度，可选叠加Coinalyze历史清算；推送会用中文说明盘口流动性和清算方向辅助，分数修正限制在 -15~+15。",
        "- 等级 = S≥85，A≥70，B≥60，C≥50；低于 STRUCTURE_MIN_SCORE 不推送。",
        "",
        "阅读方式：",
        "1. 先看信号类型，再看距离上沿/下沿和箱体宽度。",
        "2. 临界信号不是做单确认，确认信号必须等整点后收线判断。",
        "3. 图表用于快速看箱体上下沿、当前价格、量能和 OI，不代表自动交易建议。",
        ])
    if template_id == "TG_STRUCTURE_REVIEW":
        return "\n".join([
        "📌 <b>结构复盘话题说明</b>",
        "",
        "这里推送结构雷达信号发出后的表现统计，用来判断临界、突破、假突破信号是否有效。",
        "",
        "扫描和发送频率：",
        f"- 默认复盘过去 {int(settings.structure_review_lookback_hours)} 小时的结构信号。",
        f"- 信号至少等待 {seconds_cn(settings.structure_review_min_age_minutes * 60)} 后开始复盘，最多跟踪 {int(settings.structure_review_forward_hours)} 小时。",
        f"- 真实推送复盘报告最小间隔：{seconds_cn(settings.structure_review_max_report_interval_sec)}。",
        "",
        "阅读方式：",
        "1. 先看总信号、已完成复盘、有效突破、假突破、无效震荡。",
        "2. 再看 S/A/B/C 各等级命中率，判断当前分数线是否过松。",
        "3. 参数建议不会未经确认自动修改 .env.oi；需要时可在 Web 控制台一键应用。",
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
    ) -> PushResult:
        now = utc_ts()
        cooldown = self.settings.tg_default_cooldown_sec if cooldown_sec is None else cooldown_sec
        history = self._load_history()
        topic_id = self._topic_id_for_template(template_id)

        duplicate = self._recent_match(history, dedup_key, cooldown)
        if duplicate:
            result = PushResult("skipped", "dedup_cooldown", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if daily_limit is not None and daily_limit >= 0 and self._daily_sent_count(history, template_id, now) >= daily_limit:
            result = PushResult("skipped", "template_daily_limit", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if self._hourly_sent_count(history, now) >= self.settings.tg_global_hourly_limit:
            result = PushResult("skipped", "global_hourly_limit", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if not send:
            print("\n========== TELEGRAM DRY-RUN ==========")
            print(f"template_id: {template_id}")
            print(f"dedup_key: {dedup_key}")
            if topic_id:
                print(f"topic_id: {topic_id}")
            if reply_to_message_id:
                print(f"reply_to_message_id: {reply_to_message_id}")
            print(text)
            print("========== END DRY-RUN ==============\n")
            result = PushResult("dry_run", "send_flag_not_set", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if not confirm_real_send:
            result = PushResult("blocked", "missing_confirm_real_send", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if not self.settings.tg_bot_token or not self.settings.tg_chat_id:
            result = PushResult("blocked", "telegram_not_configured", False)
            self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        topic_id = self._ensure_topic_id_for_template(template_id)
        self._ensure_topic_intro(template_id, topic_id)
        ok, message_ids = self._send_real_message_ids(
            text,
            parse_mode=parse_mode,
            topic_id=topic_id,
            reply_to_message_id=reply_to_message_id,
        )
        result = PushResult(
            "sent" if ok else "failed",
            "telegram_api" if ok else "telegram_api_failed",
            ok,
            message_ids,
        )
        self._record(history, template_id, dedup_key, result, text, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
        return result

    def send_photo(
        self,
        photo_path: str | Path,
        caption: str,
        template_id: str,
        dedup_key: str,
        *,
        send: bool,
        confirm_real_send: bool,
        cooldown_sec: int | None = None,
        parse_mode: str = "HTML",
        reply_to_message_id: int | None = None,
    ) -> PushResult:
        now = utc_ts()
        cooldown = self.settings.tg_default_cooldown_sec if cooldown_sec is None else cooldown_sec
        history = self._load_history()
        topic_id = self._topic_id_for_template(template_id)
        path = Path(photo_path)

        duplicate = self._recent_match(history, dedup_key, cooldown)
        if duplicate:
            result = PushResult("skipped", "dedup_cooldown", False)
            self._record(history, template_id, dedup_key, result, caption, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if self._hourly_sent_count(history, now) >= self.settings.tg_global_hourly_limit:
            result = PushResult("skipped", "global_hourly_limit", False)
            self._record(history, template_id, dedup_key, result, caption, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if not path.exists():
            result = PushResult("failed", "photo_not_found", False)
            self._record(history, template_id, dedup_key, result, caption, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if not send:
            print("\n========== TELEGRAM PHOTO DRY-RUN ==========")
            print(f"template_id: {template_id}")
            print(f"dedup_key: {dedup_key}")
            print(f"photo_path: {path}")
            if topic_id:
                print(f"topic_id: {topic_id}")
            print(caption)
            print("========== END PHOTO DRY-RUN ==============\n")
            result = PushResult("dry_run", "send_flag_not_set", False)
            self._record(history, template_id, dedup_key, result, caption, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if not confirm_real_send:
            result = PushResult("blocked", "missing_confirm_real_send", False)
            self._record(history, template_id, dedup_key, result, caption, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        if not self.settings.tg_bot_token or not self.settings.tg_chat_id:
            result = PushResult("blocked", "telegram_not_configured", False)
            self._record(history, template_id, dedup_key, result, caption, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
            return result

        topic_id = self._ensure_topic_id_for_template(template_id)
        self._ensure_topic_intro(template_id, topic_id)
        ok, message_ids = self._send_real_photo_ids(
            path,
            caption,
            parse_mode=parse_mode,
            topic_id=topic_id,
            reply_to_message_id=reply_to_message_id,
        )
        result = PushResult(
            "sent" if ok else "failed",
            "telegram_api" if ok else "telegram_api_failed",
            ok,
            message_ids,
        )
        self._record(history, template_id, dedup_key, result, caption, topic_id=topic_id, reply_to_message_id=reply_to_message_id)
        return result

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
        for idx, chunk in enumerate(chunk_text(text, self.settings.tg_push_split_limit)):
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

    def _send_real_photo_ids(
        self,
        photo_path: Path,
        caption: str,
        parse_mode: str,
        topic_id: str = "",
        reply_to_message_id: int | None = None,
    ) -> tuple[bool, list[int]]:
        url = f"https://api.telegram.org/bot{self.settings.tg_bot_token}/sendPhoto"
        payload: dict[str, Any] = {
            "chat_id": self.settings.tg_chat_id,
            "caption": caption[:1024],
            "parse_mode": parse_mode,
        }
        if reply_to_message_id:
            payload["reply_to_message_id"] = int(reply_to_message_id)
            payload["allow_sending_without_reply"] = True
        if topic_id and (
            self.settings.tg_use_topic or str(self.settings.tg_chat_id).startswith("-100")
        ):
            try:
                payload["message_thread_id"] = int(topic_id)
            except ValueError:
                pass
        for attempt in range(1, self.settings.tg_push_retry + 1):
            try:
                with photo_path.open("rb") as image:
                    response = requests.post(
                        url,
                        data=payload,
                        files={"photo": image},
                        timeout=self.settings.tg_push_timeout_sec,
                    )
                if response.status_code == 200:
                    message_ids: list[int] = []
                    self._append_message_id(response, message_ids)
                    return True, message_ids
                if response.status_code == 400 and payload.get("reply_to_message_id"):
                    no_reply = dict(payload)
                    no_reply.pop("reply_to_message_id", None)
                    no_reply.pop("allow_sending_without_reply", None)
                    with photo_path.open("rb") as image:
                        response = requests.post(
                            url,
                            data=no_reply,
                            files={"photo": image},
                            timeout=self.settings.tg_push_timeout_sec,
                        )
                    if response.status_code == 200:
                        message_ids = []
                        self._append_message_id(response, message_ids)
                        return True, message_ids
                if response.status_code == 400 and parse_mode:
                    fallback = dict(payload)
                    fallback.pop("parse_mode", None)
                    fallback["caption"] = plain_fallback(caption)[:1024]
                    with photo_path.open("rb") as image:
                        response = requests.post(
                            url,
                            data=fallback,
                            files={"photo": image},
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
            "TG_STRUCTURE_RADAR": self.settings.tg_structure_topic_id,
            "TG_STRUCTURE_REVIEW": self.settings.tg_structure_review_topic_id or self.settings.tg_structure_topic_id,
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
        if not self.settings.tg_bot_token or not self.settings.tg_chat_id:
            return 0
        deleted = 0
        for message_id in message_ids:
            if self._delete_message(message_id):
                deleted += 1
            time.sleep(0.15)
        return deleted

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

    def _save_history(self, history: list[dict[str, Any]]) -> None:
        now = int(time.time())
        retention_days = max(1, int(self.settings.tg_push_history_retention_days))
        cutoff = now - retention_days * 86400
        retained = [
            record for record in history
            if int(record.get("ts", now)) >= cutoff
        ]
        limit = max(100, int(self.settings.tg_push_history_limit))
        self.store.save(self.settings.tg_push_history_path, retained[-limit:])

    def _record(
        self,
        history: list[dict[str, Any]],
        template_id: str,
        dedup_key: str,
        result: PushResult,
        text: str,
        topic_id: str = "",
        reply_to_message_id: int | None = None,
    ) -> None:
        history.append({
            "ts": utc_ts(),
            "time": datetime.now(timezone.utc).isoformat(),
            "template_id": template_id,
            "dedup_key": dedup_key,
            "topic_id": topic_id,
            "status": result.status,
            "reason": result.reason,
            "sent": result.sent,
            "message_ids": result.message_ids or [],
            "reply_to_message_id": int(reply_to_message_id or 0),
            "preview": text[:240],
        })
        self._save_history(history)

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
            if record.get("status") == "sent":
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
