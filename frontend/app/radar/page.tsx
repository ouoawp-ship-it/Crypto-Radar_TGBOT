"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CoinIcon } from "@/components/CoinIcon";
import { MercuCoinDrawer } from "@/components/MercuCoinDrawer";
import { SignalDetailDrawer } from "@/components/SignalDetailDrawer";
import { getMarketOverview, getWorkstationRadarAnomalies, getWorkstationRadarMomentumWindows, getWorkstationRadarRank, getWorkstationRadarSurge } from "@/lib/api";
import type {
  CockpitBoard,
  CockpitBoardItem,
  MarketOverview,
  RadarBoards,
  RadarConfluenceItem,
  RealtimeAnomalyEvent,
  RealtimeIntelligenceItem,
  WorkstationRadarAnomaliesPayload,
  WorkstationRadarRankPayload,
  WorkstationRadarSurgePayload
} from "@/lib/types";

const WINDOWS = ["15m", "30m", "1h", "4h", "1d"] as const;
type WindowKey = (typeof WINDOWS)[number];
type RankMode = "amount" | "strength";

const BOARD_LABELS: Record<string, { positive: string; negative: string }> = {
  price: { positive: "涨幅榜", negative: "跌幅榜" },
  oi: { positive: "持仓榜", negative: "持仓榜" },
  futures_flow: { positive: "主力合约流入榜", negative: "主力合约流出榜" },
  spot_flow: { positive: "主力现货流入榜", negative: "主力现货流出榜" }
};

const RADAR_ASSET_TYPES: Record<string, string> = {
  AAPL: "美股", AMD: "美股", AMZN: "美股", BABA: "美股", COIN: "美股",
  META: "美股", MSTR: "美股", MSFT: "美股", MU: "美股", NVDA: "美股",
  SNDK: "美股", SKHY: "美股", SKHYNIX: "美股", SPCX: "美股", TSLA: "美股",
  PAXG: "黄金", XAU: "黄金", XAUT: "黄金", XAG: "白银"
};

function radarAssetType(item: CockpitBoardItem, realtime?: RealtimeIntelligenceItem): string {
  const coin = String(item.coin || item.symbol || "").replace(/USDT$/i, "").toUpperCase();
  return String(item.asset_type || realtime?.asset_type || RADAR_ASSET_TYPES[coin] || "");
}

function AssetTypeBadge({ value }: { value: string }) {
  if (!value) return null;
  const metal = value === "黄金" || value === "白银";
  return <small className={`shrink-0 rounded-[2px] border px-1 py-px font-sans text-[8px] font-semibold leading-[10px] ${metal ? "border-[#e5bd57] bg-[#fff7dc] text-[#b8860b]" : "border-[#87a8ef] bg-[#e4edff] text-[#002fa7]"}`}>{value}</small>;
}

const RADAR_TIPS = {
  anomaly: [
    "异动指标说明",
    "AI 秒级捕捉市场异常并分析",
    "“自身”标签：与该币种历史数据对比排名",
    "“全场强度”：按相对历史强度进行全市场排名",
    "“全场量级”：按绝对数值进行全市场排名",
    "数值越小，排名越靠前"
  ].join("\n"),
  hotMoney: [
    "数据共振说明",
    "5 个时间维度：15m / 30m / 1h / 4h / 1d",
    "方块顺序与顶部时间维度一致，实心越多共振越强",
    "■ 实心：该时间维度上榜",
    "□ 空心：该时间维度未上榜"
  ].join("\n"),
  surge: "短期内快速异动速率排行——短线机会高发地",
  total: "全天累积异动最多的币——一整天都在波动，短线炒作机会与危险信号兼具",
  ambush: "还没启动但已经安静潜伏的币——持仓量在累积、价格相对平静，等待下一个突破点"
} as const;

function finite(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function money(value: unknown, signed = true): string {
  const number = finite(value);
  if (number === null) return "—";
  const sign = signed ? (number > 0 ? "+" : number < 0 ? "−" : "") : "";
  const absolute = Math.abs(number);
  if (absolute >= 1e9) return `${sign}$${(absolute / 1e9).toFixed(2)}B`;
  if (absolute >= 1e6) return `${sign}$${(absolute / 1e6).toFixed(1)}M`;
  if (absolute >= 1e3) return `${sign}$${(absolute / 1e3).toFixed(1)}K`;
  return `${sign}$${absolute.toFixed(0)}`;
}

function marketMoney(value: unknown, signed = true): string {
  return money(value, signed).replace(/\.0([BMK])$/, "$1");
}

function percent(value: unknown, digits = 2): string {
  const number = finite(value);
  return number === null ? "—" : `${number > 0 ? "+" : ""}${number.toFixed(digits)}%`;
}

function clock(value?: string): string {
  if (!value) return "--:--";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "--:--" : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" });
}

function tone(value: unknown): string {
  const number = finite(value);
  return number === null || number === 0 ? "text-text-secondary" : number > 0 ? "text-good" : "text-risk";
}

function cycleDelta(currentValue: unknown, previousValue: unknown, deltaValue: unknown): string {
  const current = finite(currentValue);
  const previous = finite(previousValue);
  const delta = finite(deltaValue);
  if (current === null || previous === null || delta === null) return "上一周期数据积累中";
  if (previous < 0 && current >= 0) return `环比转正 ${marketMoney(delta, false)}`;
  if (previous > 0 && current <= 0) return `环比转负 ${marketMoney(delta, false)}`;
  return `环比${delta >= 0 ? "增加" : "减少"} ${marketMoney(Math.abs(delta), false)}`;
}

function trendState(currentValue: unknown, previousValue: unknown, kind: "flow" | "oi"): string {
  const current = finite(currentValue);
  const previous = finite(previousValue);
  if (current === null) return "数据积累中";
  const positive = current >= 0;
  const noun = kind === "oi" ? (positive ? "增仓" : "减仓") : (positive ? "流入" : "流出");
  if (previous === null) return noun;
  if (previous < 0 && current >= 0) return kind === "oi" ? "由减仓转增仓" : "由流出转流入";
  if (previous > 0 && current <= 0) return kind === "oi" ? "由增仓转减仓" : "由流入转流出";
  return `${noun}${Math.abs(current) >= Math.abs(previous) ? "扩大" : "放缓"}`;
}

function MarketTrendRow({ label, current, previous, delta, positiveRatio, kind }: { label: string; current?: number | null; previous?: number | null; delta?: number | null; positiveRatio?: number | null; kind: "flow" | "oi" }) {
  const currentNumber = finite(current);
  const positive = currentNumber === null ? null : currentNumber >= 0;
  const status = positive === null ? "积累中" : kind === "oi" ? (positive ? "增仓" : "减仓") : (positive ? "净流入" : "净流出");
  const suppliedRatio = finite(positiveRatio);
  const barRatio = suppliedRatio === null ? (positive === null ? 0 : 50) : Math.max(0, Math.min(100, suppliedRatio * 100));
  return <div className="border-b border-border-subtle px-4 pb-3 pt-3" data-testid={`market-trend-${kind}-${label}`}>
    <div className="flex items-center gap-2"><span className="text-[13px] font-semibold text-text-secondary">{label}</span><span className={`ml-auto font-mono text-[14px] font-semibold ${tone(current)}`}>{marketMoney(current)}</span><span className={`rounded-[2px] px-1.5 py-0.5 text-[10px] font-semibold ${positive === null ? "bg-surface-container text-text-muted" : positive ? "bg-good/10 text-good" : "bg-risk/10 text-risk"}`}>{status}</span></div>
    <div className="mt-2 flex h-[6px] overflow-hidden rounded-full bg-[#e33a46]"><span className="h-full bg-[#14aa6d]" style={{ width: `${barRatio}%` }}/></div>
    <div className="mt-2 text-[12px] leading-[18px] text-text-muted">较上一周期 {marketMoney(previous)} → {marketMoney(current)}，{cycleDelta(current, previous, delta)}，{trendState(current, previous, kind)}</div>
  </div>;
}

function MarketBreadthRow({ advancing = 0, declining = 0 }: { advancing?: number; declining?: number }) {
  const total = Math.max(1, advancing + declining);
  const advancingRatio = Math.round(advancing / total * 100);
  const state = advancingRatio >= 58 ? "涨多跌少" : advancingRatio <= 42 ? "跌多涨少" : "涨跌均衡";
  return <div className="px-2.5 pb-2 pt-[5px]"><div className="flex items-center justify-between gap-2"><span className="text-[9px] font-semibold text-text-secondary">全场涨跌</span><span className={`font-mono text-[10px] font-semibold ${advancing >= declining ? "text-good" : "text-risk"}`}>涨 {advancing} · 跌 {declining}</span><span className="rounded-[2px] bg-primary-50 px-1 py-px text-[7px] font-semibold text-primary-700">{state}</span></div><div className="mt-1 flex h-[5px] overflow-hidden rounded-full bg-[#e33a46]"><span className="bg-[#14aa6d]" style={{ width: `${advancingRatio}%` }}/></div><div className="mt-1 text-[7px] text-text-muted">上涨占比 {advancingRatio}% · 基于可用市场样本</div></div>;
}

function resonanceStates(item?: RealtimeIntelligenceItem, fallbackPercentile?: number | null): boolean[] {
  const windows = item?.resonance?.windows || [];
  if (windows.length) {
    const activeByWindow = new Map(windows.map((window) => [String(window.key || ""), Boolean(window.active)]));
    return WINDOWS.map((key) => activeByWindow.get(key) || false);
  }
  const resonanceActive = Number(item?.resonance?.active_count || 0);
  const fallbackActive = fallbackPercentile === null || fallbackPercentile === undefined ? 0 : Math.ceil(fallbackPercentile / 20);
  const active = Math.max(0, Math.min(WINDOWS.length, resonanceActive || fallbackActive));
  return WINDOWS.map((_key, index) => index < active);
}

function rankWindowStates(
  momentum: Partial<Record<WindowKey, RadarBoards>>,
  boardKey: string,
  mode: RankMode,
  positive: boolean,
  item: CockpitBoardItem,
): boolean[] | undefined {
  const supplied = item.window_states;
  if (supplied && Object.keys(supplied).length) return WINDOWS.map((window) => Boolean(supplied[window]));
  const symbol = String(item.symbol || "");
  let inspected = 0;
  const states = WINDOWS.map((window) => {
    const board = momentum[window]?.boards?.find((candidate) => candidate.key === boardKey);
    if (!board) return false;
    inspected += 1;
    const side = mode === "amount"
      ? (positive ? board.amount_positive || board.positive : board.amount_negative || board.negative)
      : (positive ? board.strength_positive || board.positive : board.strength_negative || board.negative);
    return Boolean(side?.items?.some((item) => String(item.symbol || "") === symbol));
  });
  return inspected ? states : undefined;
}

function RankBlocks({ item, fallbackPercentile, states: suppliedStates }: { item?: RealtimeIntelligenceItem; fallbackPercentile?: number | null; states?: boolean[] }) {
  const states = suppliedStates || resonanceStates(item, fallbackPercentile);
  const active = states.filter(Boolean).length;
  const activeWindows = WINDOWS.filter((_, index) => states[index]);
  return <span aria-label={`五窗口共振 ${active}/5`} className="inline-flex gap-px" title={activeWindows.length ? `${activeWindows.join("、")} 共振` : "五窗口暂未形成共振"}>{states.map((isActive, index) => <span className={`h-[5px] w-[5px] rounded-[1px] border ${isActive ? "border-[#002fa7] bg-[#002fa7]" : "border-border-subtle bg-surface-container-low"}`} key={WINDOWS[index]}/>)}</span>;
}

function PanelTitle({ title, meta, action, icon, iconClassName = "text-primary-600", tip }: { title: string; meta?: string; action?: React.ReactNode; icon?: React.ReactNode; iconClassName?: string; tip?: string }) {
  return <div className="workstation-panel-header"><div className="flex min-w-0 items-center gap-2">{icon ? <span aria-hidden="true" className={`shrink-0 text-[15px] ${iconClassName}`}>{icon}</span> : null}<h2 className="truncate text-[15px] font-bold text-text-primary">{title}</h2><span aria-label={tip ? `${title}说明` : undefined} className="cursor-help text-[11px] font-normal text-text-muted" role={tip ? "img" : undefined} title={tip}>ⓘ</span></div><div className="ml-auto flex shrink-0 items-center gap-3">{meta ? <span className="truncate font-mono text-[11px] text-text-muted">{meta}</span> : null}{action}</div></div>;
}

function FollowWindowBadge({ windowKey }: { windowKey: WindowKey }) {
  return <span className="mr-[6px] inline-flex items-center gap-1.5 rounded-full border border-[#8da7e8] bg-[#e2e7f4] px-4 py-1 text-[12px] font-semibold leading-[15px] text-[#002fa7] min-[1600px]:mr-0 min-[1600px]:px-[18px] min-[1600px]:leading-normal"><span className="h-2 w-2 rounded-full bg-[#002fa7]"/>跟随 {windowKey}</span>;
}

function RankBadge({ label, rank, title }: { label: string; rank?: number; title?: string }) {
  return <span className="whitespace-nowrap rounded-[4px] border border-border-subtle bg-surface-low px-2 py-0.5 text-[11px] leading-4 text-text-muted" title={title}>{label} <b className="font-mono font-semibold text-text-secondary">#{rank || "—"}</b></span>;
}

function EventFeed({ events, onSelectSymbol, query }: { events: RealtimeAnomalyEvent[]; onSelectSymbol: (symbol: string) => void; query: string }) {
  const filtered = events.filter((event) => !query || String(event.symbol || "").includes(query));
  return <div className="workstation-scroll min-h-0 flex-1 overflow-y-auto">
    {filtered.map((event, index) => {
      const positive = event.direction === "long";
      const directionalDetail = /\bOI\b/i.test(String(event.label || ""));
      const primaryValue = event.detail || event.metric === "state" ? "" : event.value_usd !== null && event.value_usd !== undefined ? money(event.value_usd) : percent(event.value);
      const self = event.rankings?.self;
      const strength = event.rankings?.market_strength;
      const absolute = event.rankings?.market_absolute;
      return <button className="radar-event-item relative block min-h-[128px] w-full border-b border-border-subtle px-3 py-[11px] text-left transition-colors hover:bg-primary-50/55 min-[1600px]:min-h-[104px] min-[1600px]:py-3" data-symbol={event.symbol || ""} key={event.id || `${event.symbol}-${event.event_type}-${index}`} onClick={() => onSelectSymbol(String(event.symbol || event.coin || ""))} type="button">
        <span className="absolute left-3 top-[15px] w-[46px] font-mono text-[11px] tabular-nums text-text-muted min-[1600px]:top-[10px]">{clock(event.observed_at)}</span>
        <span aria-hidden="true" className={`radar-event-dot ${positive ? "bg-good" : "bg-risk"}`}/>
        <div className="flex items-center gap-2 pl-10">
          <span className="radar-event-coin"><CoinIcon coin={event.coin} size={24}/></span><span className="min-w-0 truncate text-[15px] font-bold text-text-primary">${event.coin || event.symbol}</span>
          <span className={`min-w-0 flex-1 truncate text-[14px] font-semibold ${positive ? "text-good" : "text-risk"}`}>{event.label || "异动"}</span>
        </div>
        <div className="mt-2 flex items-baseline justify-between gap-2 pl-10 text-[13px]"><span className={`truncate ${directionalDetail ? positive ? "text-good" : "text-risk" : "text-text-muted"}`}>{event.detail || `${event.window || "5m"} 内 · ${event.metric === "volume" ? "成交量" : event.metric === "price" ? "价格" : event.metric === "liquidation" ? "爆仓额" : "主动资金"}`}</span>{primaryValue ? <span className={`shrink-0 font-mono font-semibold tabular-nums ${positive ? "text-good" : "text-risk"}`}>{primaryValue} {event.change_pct !== null && event.change_pct !== undefined && event.value_usd !== null && event.value_usd !== undefined ? `(${percent(event.change_pct, 1)})` : ""}</span> : null}</div>
        <div className="mt-[11px] flex flex-wrap gap-x-2 gap-y-1 pl-10"><RankBadge label="自身强度" rank={self?.rank} title={`该币历史样本中第 ${self?.rank || "—"} 极端${self?.method ? ` · ${self.method}` : ""}`}/><RankBadge label="全场强度" rank={strength?.rank} title={`按相对历史强度排 · 自身极端度全场对比${strength?.method ? ` · ${strength.method}` : ""}`}/><RankBadge label="全场量级" rank={absolute?.rank} title={`按绝对额排 · 大币靠前 · 小币突发不上榜${absolute?.method ? ` · ${absolute.method}` : ""}`}/></div>
      </button>;
    })}
    {!filtered.length ? <div className="grid h-28 place-items-center text-[10px] text-text-muted">{query ? `没有找到 ${query} 的异动` : "暂无异动事件 · 正在扫描"}</div> : null}
  </div>;
}

function flowMoney(value: unknown): string {
  const number = finite(value);
  if (number === null) return "—";
  const sign = number > 0 ? "+" : number < 0 ? "−" : "";
  return `${sign}$${(Math.abs(number) / 1e6).toFixed(1)}M`;
}

function boardValue(item: CockpitBoardItem, mode: RankMode, compactMillions = false) {
  if (mode === "strength") return finite(item.strength_percentile) === null ? "—" : `${Math.round(Number(item.strength_percentile))}分`;
  const magnitude = finite(item.magnitude_usd);
  const hasMagnitude = magnitude !== null && Math.abs(magnitude) > 0;
  const raw = hasMagnitude ? Math.sign(finite(item.value) || magnitude || 1) * Math.abs(magnitude) : item.value;
  return item.unit === "usd" || hasMagnitude ? compactMillions ? flowMoney(raw) : marketMoney(raw) : percent(raw, item.unit === "percent_per_cycle" ? 3 : 2);
}

function rankMagnitude(item: CockpitBoardItem) {
  return Math.abs(finite(item.magnitude_usd) ?? finite(item.value) ?? 0);
}

function MomentumList({ items, mode, onSelectSymbol, positive, realtimeBySymbol, scaleMax, windowStates, compactMillions = false, limit = 7 }: { items?: CockpitBoardItem[]; mode: RankMode; onSelectSymbol: (symbol: string) => void; positive: boolean; realtimeBySymbol: Map<string, RealtimeIntelligenceItem>; scaleMax?: number; windowStates: (item: CockpitBoardItem) => boolean[] | undefined; compactMillions?: boolean; limit?: number }) {
  const visible = (items || []).slice(0, limit);
  const maxMagnitude = Math.max(1, scaleMax || 0, ...visible.map(rankMagnitude));
  return <div>{visible.map((item, index) => {
    const realtime = realtimeBySymbol.get(String(item.symbol || ""));
    const barWidth = Math.min(70, 18 + rankMagnitude(item) / maxMagnitude * 66);
    return <button className="relative grid h-[29px] w-full grid-cols-[20px_20px_minmax(0,1fr)_48px_58px] items-center gap-1 overflow-hidden px-3 text-left text-[11px] hover:bg-primary-50/50 min-[1600px]:h-[30px]" data-symbol={item.symbol || ""} key={`${item.symbol}-${index}`} onClick={() => onSelectSymbol(String(item.symbol || item.coin || ""))} type="button">
      <i aria-hidden="true" className={`absolute bottom-1 right-0 top-[7px] rounded-[3px] not-italic ${positive ? "bg-[#daf1e8]" : "bg-[#fbe2e3]"}`} style={{ width: `${barWidth}%` }}/>
      <span className="relative z-[1] text-left font-mono text-[10px] text-text-muted">{index + 1}</span><span className="relative z-[1]"><CoinIcon coin={item.coin} size={18}/></span><span className="relative z-[1] flex min-w-0 items-center gap-1 overflow-hidden font-mono font-semibold text-text-primary"><span className="truncate">{item.coin || item.symbol}</span><AssetTypeBadge value={radarAssetType(item, realtime)}/></span><span className="relative z-[1]"><RankBlocks fallbackPercentile={finite(item.strength_percentile)} item={realtime} states={windowStates(item)}/></span><span className={`relative z-[1] truncate text-right font-mono text-[10px] font-semibold tabular-nums ${positive ? "text-good" : "text-risk"}`}>{boardValue(item, mode, compactMillions)}</span>
    </button>;
  })}{!visible.length ? <div className="grid h-[74px] place-items-center text-[9px] text-text-muted">⏳ 暂无</div> : null}</div>;
}

function MomentumStrengthGrid({ items, onSelectSymbol, positive, realtimeBySymbol, windowStates, compactMillions = false }: { items?: CockpitBoardItem[]; onSelectSymbol: (symbol: string) => void; positive: boolean; realtimeBySymbol: Map<string, RealtimeIntelligenceItem>; windowStates: (item: CockpitBoardItem) => boolean[] | undefined; compactMillions?: boolean }) {
  return <div className="grid grid-cols-1 sm:grid-cols-2 [&>:nth-last-child(-n+2)]:border-b-0" data-testid="radar-strength-grid">{(items || []).slice(0, 8).map((item, index) => {
    const realtime = realtimeBySymbol.get(String(item.symbol || ""));
    const score = finite(item.strength_percentile) ?? finite(realtime?.rankings?.market_strength?.percentile);
    const states = windowStates(item) || resonanceStates(realtime, finite(item.strength_percentile));
    const active = states.filter(Boolean).length;
    return <button className="grid h-[52px] min-w-0 grid-cols-[12px_18px_minmax(0,1fr)_38px] grid-rows-2 items-center gap-x-1 border-b border-r border-border-subtle/70 border-b-border-subtle/40 px-3 text-left hover:bg-primary-50/55" data-symbol={item.symbol || ""} key={`${item.symbol}-${index}`} onClick={() => onSelectSymbol(String(item.symbol || item.coin || ""))} type="button">
      <small className="row-span-2 text-right font-mono text-[9px] text-text-muted">{index + 1}</small>
      <span className="row-span-2"><CoinIcon coin={item.coin} size={16}/></span><span className="sr-only">{item.coin || item.symbol}</span>
      <span aria-label={`五窗口共振 ${active}/5`} className="col-start-3 row-span-2 inline-flex h-[7px] self-center items-stretch gap-px">{states.map((isActive, block) => <i className={`block h-[6px] w-[2px] shrink-0 rounded-[.5px] border ${isActive ? "border-[#002fa7] bg-[#002fa7]" : "border-border-subtle bg-surface-container-low"}`} key={WINDOWS[block]}/>)}</span>
      <span className="col-start-4 self-end text-right font-mono text-[9px] text-text-muted">{score === null ? "—" : `${Math.round(score)}分`}</span>
      <span className={`col-start-4 self-start truncate text-right font-mono text-[9px] font-semibold ${positive ? "text-good" : "text-risk"}`}>{boardValue(item, "amount", compactMillions)}</span>
    </button>;
  })}{!(items || []).length ? <div className="grid h-[136px] place-items-center text-[8px] text-text-muted sm:col-span-2">⏳ 暂无</div> : null}</div>;
}

function MomentumBoard({ board, momentum, onSelectSymbol, realtimeBySymbol }: { board?: CockpitBoard; momentum: Partial<Record<WindowKey, RadarBoards>>; onSelectSymbol: (symbol: string) => void; realtimeBySymbol: Map<string, RealtimeIntelligenceItem> }) {
  const labels = BOARD_LABELS[String(board?.key || "")] || { positive: board?.positive?.title || "上行", negative: board?.negative?.title || "下行" };
  const amountPositive = board?.amount_positive || board?.positive;
  const amountNegative = board?.amount_negative || board?.negative;
  const strengthPositive = board?.strength_positive || board?.positive;
  const strengthNegative = board?.strength_negative || board?.negative;
  const amountScaleMax = Math.max(1, ...[...(amountPositive?.items || []).slice(0, 7), ...(amountNegative?.items || []).slice(0, 7)].map(rankMagnitude));
  const amountUnit = board?.key === "price" ? "%" : "USDT";
  const compactMillions = board?.key === "futures_flow" || board?.key === "spot_flow";
  const statesFor = (mode: RankMode, positive: boolean, item: CockpitBoardItem) => rankWindowStates(momentum, String(board?.key || ""), mode, positive, item);
  return <section className="overflow-hidden rounded-[2px] border border-border-subtle bg-surface-panel">
    <div className="grid h-[37px] grid-cols-2 border-b border-border-subtle bg-surface-panel text-[12px] font-semibold min-[1600px]:h-[38px]"><div className="flex items-center justify-between border-r border-border-subtle px-3 max-[1599px]:min-w-0"><span className="flex items-center gap-1.5 text-good max-[1599px]:min-w-0 max-[1599px]:flex-1 max-[1599px]:overflow-hidden"><span className="max-[1599px]:min-w-0 max-[1599px]:truncate max-[1599px]:whitespace-nowrap">▲ {labels.positive}</span><span className="rounded-[3px] bg-surface-container px-1.5 py-0.5 text-[10px] text-text-muted max-[1599px]:shrink-0">量级榜</span></span><small className="font-mono text-[9px] font-normal text-text-muted/60 max-[1599px]:ml-1 max-[1599px]:shrink-0">{amountUnit}</small></div><div className="flex items-center justify-between px-3 max-[1599px]:min-w-0"><span className="flex items-center gap-1.5 text-risk max-[1599px]:min-w-0 max-[1599px]:flex-1 max-[1599px]:overflow-hidden"><span className="max-[1599px]:min-w-0 max-[1599px]:truncate max-[1599px]:whitespace-nowrap">▼ {labels.negative}</span><span className="rounded-[3px] bg-surface-container px-1.5 py-0.5 text-[10px] text-text-muted max-[1599px]:shrink-0">量级榜</span></span><small className="font-mono text-[9px] font-normal text-text-muted/60 max-[1599px]:ml-1 max-[1599px]:shrink-0">{amountUnit}</small></div></div>
    <div className="grid grid-cols-2 divide-x divide-border-subtle"><MomentumList compactMillions={compactMillions} items={amountPositive?.items} mode="amount" onSelectSymbol={onSelectSymbol} positive realtimeBySymbol={realtimeBySymbol} scaleMax={amountScaleMax} windowStates={(symbol) => statesFor("amount", true, symbol)}/><MomentumList compactMillions={compactMillions} items={amountNegative?.items} mode="amount" onSelectSymbol={onSelectSymbol} positive={false} realtimeBySymbol={realtimeBySymbol} scaleMax={amountScaleMax} windowStates={(symbol) => statesFor("amount", false, symbol)}/></div>
    <div className="grid h-[40px] grid-cols-2 border-y border-border-subtle bg-surface-panel text-[12px] font-semibold min-[1600px]:h-[39px]"><div className="flex items-center justify-between border-r border-border-subtle px-3 max-[1599px]:min-w-0"><span className="flex items-center gap-1.5 text-good max-[1599px]:min-w-0 max-[1599px]:flex-1 max-[1599px]:overflow-hidden"><span className="max-[1599px]:min-w-0 max-[1599px]:truncate max-[1599px]:whitespace-nowrap">▲ {labels.positive}</span><span className="rounded-[3px] bg-warn/10 px-1.5 py-0.5 text-[10px] text-warn max-[1599px]:shrink-0">强度榜</span></span><small className="text-[9px] font-normal text-text-muted/60 max-[1599px]:ml-1 max-[1599px]:shrink-0">强度分</small></div><div className="flex items-center justify-between px-3 max-[1599px]:min-w-0"><span className="flex items-center gap-1.5 text-risk max-[1599px]:min-w-0 max-[1599px]:flex-1 max-[1599px]:overflow-hidden"><span className="max-[1599px]:min-w-0 max-[1599px]:truncate max-[1599px]:whitespace-nowrap">▼ {labels.negative}</span><span className="rounded-[3px] bg-warn/10 px-1.5 py-0.5 text-[10px] text-warn max-[1599px]:shrink-0">强度榜</span></span><small className="text-[9px] font-normal text-text-muted/60 max-[1599px]:ml-1 max-[1599px]:shrink-0">强度分</small></div></div>
    <div className="grid grid-cols-2 divide-x divide-border-subtle"><MomentumStrengthGrid compactMillions={compactMillions} items={strengthPositive?.items} onSelectSymbol={onSelectSymbol} positive realtimeBySymbol={realtimeBySymbol} windowStates={(item) => statesFor("strength", true, item)}/><MomentumStrengthGrid compactMillions={compactMillions} items={strengthNegative?.items} onSelectSymbol={onSelectSymbol} positive={false} realtimeBySymbol={realtimeBySymbol} windowStates={(item) => statesFor("strength", false, item)}/></div>
  </section>;
}

type ConfluenceEntry = CockpitBoardItem & { boardCount: number; divergent: boolean; positive: boolean };

function confluenceFromPayload(items: RadarConfluenceItem[] | undefined): ConfluenceEntry[] {
  return (items || []).map((item) => ({
    ...item,
    boardCount: Math.max(1, Number(item.board_count || 1)),
    divergent: Boolean(item.divergent),
    positive: item.direction !== "negative" && item.direction !== "outflow",
  }));
}

function confluenceFromBoards(boards: CockpitBoard[], mode: RankMode): ConfluenceEntry[] {
  const tallies = new Map<string, { item: CockpitBoardItem; positive: Set<string>; negative: Set<string> }>();
  const add = (item: CockpitBoardItem, boardKey: string, direction: "positive" | "negative") => {
    const symbol = String(item.symbol || "");
    if (!symbol) return;
    const current = tallies.get(symbol) || { item, positive: new Set<string>(), negative: new Set<string>() };
    const currentMagnitude = Math.abs(Number(current.item.magnitude_usd ?? current.item.value ?? 0));
    const nextMagnitude = Math.abs(Number(item.magnitude_usd ?? item.value ?? 0));
    if (nextMagnitude >= currentMagnitude) current.item = item;
    current[direction].add(boardKey);
    tallies.set(symbol, current);
  };
  for (const board of boards.filter((board) => ["oi", "futures_flow", "spot_flow"].includes(String(board.key || "")))) {
    const boardKey = String(board.key || "board");
    const positive = mode === "amount" ? board.amount_positive || board.positive : board.strength_positive || board.positive;
    const negative = mode === "amount" ? board.amount_negative || board.negative : board.strength_negative || board.negative;
    for (const item of (positive?.items || []).slice(0, 8)) add(item, boardKey, "positive");
    for (const item of (negative?.items || []).slice(0, 8)) add(item, boardKey, "negative");
  }
  const entries = [...tallies.values()].map(({ item, positive, negative }) => ({
    ...item,
    boardCount: Math.max(positive.size, negative.size),
    divergent: positive.size > 0 && negative.size > 0,
    positive: positive.size >= negative.size,
  })).sort((a, b) => b.boardCount - a.boardCount || Number(b.strength_percentile || 0) - Number(a.strength_percentile || 0) || Number(b.positive) - Number(a.positive) || String(a.symbol || "").localeCompare(String(b.symbol || "")));
  return entries.filter((item) => item.boardCount >= 2);
}

function RuleBoard({ title, subtitle, items, mode, onSelectSymbol }: { title: string; subtitle: string; items: RealtimeIntelligenceItem[]; mode: "surge" | "ambush" | "total"; onSelectSymbol: (symbol: string) => void }) {
  const tip = mode === "surge" ? RADAR_TIPS.surge : mode === "total" ? RADAR_TIPS.total : RADAR_TIPS.ambush;
  const model = mode === "surge" ? "+ 加速识别模型" : mode === "total" ? "+ 算法标注引擎" : "持仓蓄积 · 价格静默";
  return <section className="workstation-panel flex min-h-0 flex-col"><PanelTitle tip={tip} title={title}/><div className="flex items-center border-b border-border-subtle px-2 py-1 text-[8px] text-text-muted"><span className="truncate">{subtitle}</span><span className="ml-auto shrink-0 text-[7px] text-primary-600">{model}</span></div><div className="workstation-scroll min-h-0 flex-1 overflow-auto">{items.map((item, index) => {
    const analysis = mode === "ambush" ? item.ambush : item.surge;
    const ageSec = finite(item.lifecycle?.age_sec);
    const elapsed = ageSec && ageSec >= 3_600 ? `已 ${Math.floor(ageSec / 3_600)}h` : ageSec ? `已 ${Math.max(1, Math.floor(ageSec / 60))}m` : "";
    const value = mode === "total" ? `${item.anomaly_24h?.count || 0}次` : mode === "ambush" && elapsed ? elapsed : `${finite(analysis?.score)?.toFixed(1) || "—"}分`;
    const positive = mode === "total" ? Number(item.anomaly_24h?.long_count || 0) >= Number(item.anomaly_24h?.short_count || 0) : analysis?.direction !== "short";
    return <button className="grid h-[28px] w-full grid-cols-[16px_18px_minmax(40px,1fr)_48px_auto] items-center gap-1 border-b border-border-subtle/75 px-2 text-left text-[9px] hover:bg-primary-50/50" data-symbol={item.symbol || ""} key={item.symbol} onClick={() => onSelectSymbol(String(item.symbol || item.coin || ""))} type="button"><span className="font-mono text-[8px] text-text-muted">{index + 1}</span><CoinIcon coin={item.coin} size={15}/><span className="truncate font-semibold text-text-primary">{item.coin}</span><RankBlocks item={item}/><span className={`font-mono font-semibold ${positive ? "text-good" : "text-risk"}`}>{value}</span></button>;
  })}{!items.length ? <div className="grid h-20 place-items-center text-[9px] text-text-muted">暂无符合条件的币种</div> : null}</div></section>;
}

export default function RadarPage() {
  const [momentum, setMomentum] = useState<Partial<Record<WindowKey, RadarBoards>>>({});
  const [anomalies, setAnomalies] = useState<WorkstationRadarAnomaliesPayload>({});
  const [surgeBoard, setSurgeBoard] = useState<WorkstationRadarSurgePayload>({});
  const [rankBoard, setRankBoard] = useState<WorkstationRadarRankPayload>({});
  const [overview, setOverview] = useState<MarketOverview>({});
  const [windowKey, setWindowKey] = useState<WindowKey>("15m");
  const [query, setQuery] = useState("");
  const [debouncedQuery, setDebouncedQuery] = useState("");
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedCoin, setSelectedCoin] = useState("");
  const [selectedSignal, setSelectedSignal] = useState("");

  const load = useCallback(async (bypassCache = false) => {
    setLoading(true);
    setError("");
    const options = { bypassCache };
    const results = await Promise.allSettled([
      getWorkstationRadarMomentumWindows(10, options),
      getWorkstationRadarAnomalies(100, options),
      getWorkstationRadarSurge(5, options),
      getWorkstationRadarRank(14, 8, options),
      getMarketOverview(900, options),
    ] as const);
    const [windowsResult, anomaliesResult, surgeResult, rankResult, marketResult] = results;
    if (windowsResult.status === "fulfilled") setMomentum(windowsResult.value.windows as Partial<Record<WindowKey, RadarBoards>>);
    if (anomaliesResult.status === "fulfilled") setAnomalies(anomaliesResult.value);
    if (surgeResult.status === "fulfilled") setSurgeBoard(surgeResult.value);
    if (rankResult.status === "fulfilled") setRankBoard(rankResult.value);
    if (marketResult.status === "fulfilled") setOverview(marketResult.value);
    const failures = results.filter((result) => result.status === "rejected");
    if (failures.length) setError(failures.length === results.length ? "雷达工作站加载失败" : `${failures.length} 个雷达模块暂时不可用`);
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(query), 100);
    return () => window.clearTimeout(timer);
  }, [query]);
  useEffect(() => {
    const syncFromLocation = () => setSelectedSignal(new URLSearchParams(window.location.search).get("signal") || "");
    syncFromLocation();
    window.addEventListener("popstate", syncFromLocation);
    return () => window.removeEventListener("popstate", syncFromLocation);
  }, []);
  useEffect(() => { if (paused || query) return; const timer = window.setInterval(() => void load(true), 30_000); return () => window.clearInterval(timer); }, [load, paused, query]);

  const selectSignal = useCallback((signalId: number | string) => {
    const value = String(signalId || "");
    const url = new URL(window.location.href);
    if (value) url.searchParams.set("signal", value); else url.searchParams.delete("signal");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
    setSelectedSignal(value);
  }, []);

  const items = rankBoard.universe || [];
  const events = useMemo(() => {
    return (anomalies.items || []).slice(0, 100);
  }, [anomalies.items]);
  const boards = momentum[windowKey]?.boards || [];
  const confluence = momentum[windowKey]?.confluence;
  const realtimeBySymbol = useMemo(() => new Map(items.map((item) => [String(item.symbol || ""), item])), [items]);
  const surge = surgeBoard.items || [];
  const ambush = rankBoard.ambush || [];
  const total = rankBoard.total || [];
  const market = overview.overview || {};
  const previousMarket = market.comparison?.previous || {};
  const marketDelta = market.comparison?.delta || {};
  const tendency = useMemo(() => {
    const supplied = confluenceFromPayload(confluence?.amount);
    return (supplied.length ? supplied : confluenceFromBoards(boards, "amount")).slice(0, 7);
  }, [boards, confluence?.amount]);
  const strengthFlow = useMemo(() => {
    const supplied = confluenceFromPayload(confluence?.strength);
    return (supplied.length ? supplied : confluenceFromBoards(boards, "strength")).slice(0, 7);
  }, [boards, confluence?.strength]);

  return <><div aria-busy={loading} className="workstation-page mercu-radar-grid" data-testid="radar-workstation">
    <aside className="workstation-panel flex min-h-0 flex-col" data-testid="radar-event-feed">
      <PanelTitle action={<span className="inline-flex h-[22px] items-center gap-1.5 rounded-full bg-good/10 px-3 text-[11px] font-semibold text-good"><span className="h-2 w-2 animate-pulse rounded-full bg-good"/>LIVE</span>} meta={`更新 ${clock(anomalies.observed_at || anomalies.generated_at)}`} tip={RADAR_TIPS.anomaly} title="异动监控"/>
      <div className="radar-scan-bar grid h-[60px] grid-cols-[minmax(0,1fr)_128px] items-center gap-2 border-b border-border-subtle bg-[#f1f4f8] pl-[18px] pr-0 min-[1600px]:grid-cols-[minmax(0,1fr)_168px] min-[1600px]:pr-3"><span className="flex min-w-0 items-center gap-[14px] text-[14px] font-semibold text-primary-700"><i aria-hidden="true" className="radar-scan-orbit" data-testid="radar-scan-orbit"><span/></i><span className="flex min-w-0 flex-col"><span className="flex items-center gap-1.5 truncate leading-5"><i aria-hidden="true" className="hidden h-1.5 w-1.5 shrink-0 rounded-full bg-good/60 min-[1600px]:block"/>AI 全市场扫描</span><small className="font-mono text-[10px] font-normal leading-4 text-text-muted">1000+ 币种</small></span></span><div className="relative"><svg aria-hidden="true" className="absolute left-3 top-1/2 h-[15px] w-[15px] -translate-y-1/2 text-text-muted" fill="none" viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.5"/><path d="m10.5 10.5 3 3" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5"/></svg><input aria-label="搜索币种" className="h-[34px] w-full rounded-[4px] border border-border-subtle bg-surface-panel pl-8 pr-2 text-[11px] uppercase text-text-primary outline-none placeholder:text-text-muted focus:border-primary-500" onChange={(event) => setQuery(event.target.value.trim().toUpperCase())} placeholder="搜索币种..." value={query}/></div></div>
      {error ? <div className="border-b border-risk/20 bg-risk/5 px-2 py-1 text-[8px] text-risk">{error} · 保留上次数据</div> : null}
      <EventFeed events={events} onSelectSymbol={setSelectedCoin} query={debouncedQuery}/>
      <div className="flex h-7 shrink-0 items-center border-t border-border-subtle px-2 text-[8px] text-text-muted md:hidden"><span>{events.length} 条异动 · {paused ? "已暂停" : query ? "搜索中暂停刷新" : "30s 增量"}</span><button className="ml-auto font-semibold text-text-secondary" onClick={() => setPaused((value) => !value)} type="button">{paused ? "继续" : "暂停"}</button><button className="ml-2 font-semibold text-primary-600" disabled={loading} onClick={() => void load(true)} type="button">{loading ? "更新中…" : "立即更新"}</button></div>
    </aside>

    <main className="workstation-scroll min-h-0 overflow-y-auto" data-testid="radar-hot-money">
      <section className="workstation-panel flex min-h-[610px] flex-col">
        <PanelTitle action={<div className="flex h-[36px] items-center overflow-hidden rounded-[6px] border border-[#cbd2dc] bg-[#e9edf3]">{WINDOWS.map((key) => <button aria-pressed={windowKey === key} className={`h-[28px] w-[46px] min-w-0 rounded-[4px] px-0 font-mono text-[13px] font-semibold max-[767px]:w-11 min-[1600px]:w-[60px] ${windowKey === key ? "bg-[#d1daeb] text-[#002fa7] ring-1 ring-[#738dcc]" : "text-text-muted hover:bg-[#f8f8f9] hover:text-text-primary"}`} key={key} onClick={() => setWindowKey(key)} type="button">{key}</button>)}</div>} icon="◎" meta={`更新 ${clock(momentum[windowKey]?.generated_at)}`} tip={RADAR_TIPS.hotMoney} title="热钱观察榜单"/>
        <div className="grid min-h-0 flex-1 auto-rows-[489px] grid-cols-2 content-start gap-x-[18px] gap-y-[17px] overflow-visible pb-[11px] pl-3 pr-[18px] pt-3 min-[1600px]:auto-rows-[500px] min-[1600px]:gap-y-[18px] min-[1600px]:pt-[11px]" data-testid="radar-momentum-matrix">{["price", "oi", "futures_flow", "spot_flow"].map((key) => <MomentumBoard board={boards.find((board) => board.key === key)} key={key} momentum={momentum} onSelectSymbol={setSelectedCoin} realtimeBySymbol={realtimeBySymbol}/>)}</div>
      </section>
      <div className="mt-1.5 grid h-[220px] min-h-0 grid-cols-[.9fr_1.15fr_.95fr] gap-1.5" data-testid="radar-paoxx-extension"><RuleBoard items={surge} mode="surge" onSelectSymbol={setSelectedCoin} subtitle="1h 滚动 · 加速度排序 · TOP 5" title="Surge 飙升榜"/><RuleBoard items={total} mode="total" onSelectSymbol={setSelectedCoin} subtitle="24h 累计异动 · TOP 14" title="24h 异动总榜"/><RuleBoard items={ambush} mode="ambush" onSelectSymbol={setSelectedCoin} subtitle="持仓蓄积 / 价格平静 / 等待突破" title="埋伏池"/></div>
    </main>

    <aside className="workstation-scroll min-h-0 overflow-y-auto" data-testid="radar-side-intelligence">
      <section className="workstation-panel h-[669px] min-[1600px]:h-[679px]"><PanelTitle action={<FollowWindowBadge windowKey={windowKey}/>} icon="↝" iconClassName="text-warn" title="资金倾向性"/>
        <div className="flex h-[37px] items-center gap-2 bg-surface-panel px-4 text-[13px] font-bold text-text-primary min-[1600px]:h-[38px]"><span aria-hidden="true" className="h-[14px] w-[3px] rounded-full bg-warn/60"/>资金合流</div>
        {tendency.map((item, index) => <button className="grid h-[39px] w-full grid-cols-[22px_24px_auto_minmax(0,1fr)_auto] items-center gap-1.5 px-4 text-left text-[13px] hover:bg-primary-50/50" data-symbol={item.symbol || ""} key={`${item.symbol}-${index}`} onClick={() => setSelectedCoin(String(item.symbol || item.coin || ""))} title={item.divergent ? "多榜方向存在分歧" : "多榜方向一致"} type="button"><span className="font-mono text-[11px] text-text-muted">{index + 1}</span><CoinIcon coin={item.coin} size={22}/><span className="truncate font-semibold">{item.coin || item.symbol}</span><span className="flex items-center gap-1 justify-self-start"><span className="rounded-[3px] border border-border-subtle px-1.5 py-0.5 text-[10px] text-text-muted">{item.boardCount}榜</span>{item.divergent ? <span className="rounded-[3px] bg-surface-container px-1.5 py-0.5 text-[9px] text-text-muted">分歧</span> : null}</span><span className={`font-semibold ${item.positive ? "text-good" : "text-risk"}`}>{item.positive ? "流入" : "流出"}</span></button>)}
        <div className="flex h-[38px] items-center gap-2 border-y border-border-subtle bg-surface-panel px-4 text-[13px] font-bold text-text-primary min-[1600px]:h-[45px]"><span aria-hidden="true" className="h-[14px] w-[3px] rounded-full bg-warn/60"/>资金力度</div>
        {strengthFlow.map((item, index) => <button className="grid h-[39px] w-full grid-cols-[22px_24px_auto_minmax(0,1fr)_auto] items-center gap-1.5 px-4 text-left text-[13px] hover:bg-primary-50/50" data-symbol={item.symbol || ""} key={`${item.symbol}-${index}`} onClick={() => setSelectedCoin(String(item.symbol || item.coin || ""))} title={item.divergent ? "强度榜方向存在分歧" : "强度榜方向一致"} type="button"><span className="font-mono text-[11px] text-text-muted">{index + 1}</span><CoinIcon coin={item.coin} size={22}/><span className="truncate font-semibold">{item.coin || item.symbol}</span><span className="flex items-center gap-1 justify-self-start"><span className="rounded-[3px] border border-border-subtle px-1.5 py-0.5 text-[10px] text-text-muted">{item.boardCount}榜</span>{item.divergent ? <span className="rounded-[3px] bg-surface-container px-1.5 py-0.5 text-[9px] text-text-muted">分歧</span> : null}</span><span className={item.positive ? "text-good" : "text-risk"}>{item.positive ? "流入" : "流出"}</span></button>)}
      </section>
      <section className="workstation-panel mt-2.5 overflow-hidden"><PanelTitle action={<FollowWindowBadge windowKey={windowKey}/>} icon="◆" iconClassName="text-warn" title="全场态势"/><div><MarketTrendRow current={market.futures_net_flow_usd} delta={marketDelta.futures_net_flow_usd} kind="flow" label="合约资金净流入" positiveRatio={market.futures_positive_ratio} previous={previousMarket.futures_net_flow_usd}/><MarketTrendRow current={market.spot_net_flow_usd} delta={marketDelta.spot_net_flow_usd} kind="flow" label="现货资金净流入" positiveRatio={market.spot_positive_ratio} previous={previousMarket.spot_net_flow_usd}/><MarketTrendRow current={market.oi_net_change_usd} delta={marketDelta.oi_net_change_usd} kind="oi" label="持仓量净增长" positiveRatio={market.oi_positive_ratio} previous={previousMarket.oi_net_change_usd}/><MarketBreadthRow advancing={market.advancing} declining={market.declining}/></div></section>
    </aside>
  </div>{selectedCoin ? <MercuCoinDrawer events={events} onClose={() => setSelectedCoin("")} symbol={selectedCoin}/> : null}{selectedSignal ? <SignalDetailDrawer onClose={() => selectSignal("")} onSelectSignal={selectSignal} signalId={selectedSignal}/> : null}</>;
}
