"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { CoinIcon } from "@/components/CoinIcon";
import { SignalDetailDrawer } from "@/components/SignalDetailDrawer";
import { getMarketOverview, getRealtimeIntelligence, getWorkstationRadarMomentumWindows } from "@/lib/api";
import type {
  CockpitBoard,
  CockpitBoardItem,
  MarketOverview,
  RadarBoards,
  RealtimeAnomalyEvent,
  RealtimeIntelligenceItem,
  RealtimeIntelligencePayload
} from "@/lib/types";

const WINDOWS = ["15m", "30m", "1h", "4h", "1d"] as const;
type WindowKey = (typeof WINDOWS)[number];
type RankMode = "amount" | "strength";

const BOARD_LABELS: Record<string, { positive: string; negative: string }> = {
  price: { positive: "涨幅榜", negative: "跌幅榜" },
  oi: { positive: "持仓增加榜", negative: "持仓减少榜" },
  futures_flow: { positive: "主力合约流入榜", negative: "主力合约流出榜" },
  spot_flow: { positive: "主力现货流入榜", negative: "主力现货流出榜" }
};

function finite(value: unknown): number | null {
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

function RankBlocks({ item, fallbackPercentile, positive }: { item?: RealtimeIntelligenceItem; fallbackPercentile?: number | null; positive: boolean }) {
  const resonanceActive = Number(item?.resonance?.active_count || 0);
  const fallbackActive = fallbackPercentile === null || fallbackPercentile === undefined ? 0 : Math.ceil(fallbackPercentile / 20);
  const active = Math.max(0, Math.min(5, resonanceActive || fallbackActive));
  const short = item?.resonance?.direction ? item.resonance.direction === "short" : !positive;
  return <span aria-label={`五窗口共振 ${active}/5`} className="inline-flex gap-px">{WINDOWS.map((key, index) => <span className={`h-[5px] w-[5px] rounded-[1px] border ${index < active ? short ? "border-risk/55 bg-risk/65" : "border-primary-500/60 bg-primary-500/70" : "border-border-subtle bg-surface-container-low"}`} key={key}/>)}</span>;
}

function PanelTitle({ title, meta, action }: { title: string; meta?: string; action?: React.ReactNode }) {
  return <div className="workstation-panel-header"><div className="flex min-w-0 items-center gap-2"><h2 className="truncate text-[10px] font-bold text-text-primary">{title}</h2>{meta ? <span className="truncate font-mono text-[8px] text-text-muted">{meta}</span> : null}</div>{action}</div>;
}

function RankBadge({ label, rank, title }: { label: string; rank?: number; title?: string }) {
  return <span className="rounded-[3px] border border-border-subtle bg-surface-low px-1 py-[1px] text-[8px] leading-3 text-text-muted" title={title}>{label} <b className="font-mono font-semibold text-text-secondary">#{rank || "—"}</b></span>;
}

function fallbackEvents(items: RealtimeIntelligenceItem[]): RealtimeAnomalyEvent[] {
  return items.flatMap((item) => {
    const window = item.windows?.["5m"];
    const events: RealtimeAnomalyEvent[] = [];
    const price = finite(window?.price_change_pct);
    if (price !== null && Math.abs(price) >= 0.6) events.push({ id: `${item.symbol}:price`, symbol: item.symbol, coin: item.coin, observed_at: item.observed_at, window: "5m", event_type: price > 0 ? "price_up" : "price_down", label: price > 0 ? "价格暴涨" : "价格暴跌", direction: price > 0 ? "long" : "short", value: price, change_pct: price, rankings: item.rankings });
    const cvd = finite(window?.cvd_usd);
    if (cvd !== null && Math.abs(cvd) >= 1_000) events.push({ id: `${item.symbol}:flow`, symbol: item.symbol, coin: item.coin, observed_at: item.observed_at, window: "5m", event_type: cvd > 0 ? "perp_inflow" : "perp_outflow", label: cvd > 0 ? "合约净流入" : "合约净流出", direction: cvd > 0 ? "long" : "short", value: cvd, value_usd: cvd, change_pct: window?.cvd_ratio_pct, rankings: item.rankings });
    return events;
  }).slice(0, 80);
}

function lifecycleEvents(items: RealtimeIntelligenceItem[]): RealtimeAnomalyEvent[] {
  return items.flatMap((item) => {
    const events: RealtimeAnomalyEvent[] = [];
    const active = Number(item.resonance?.active_count || 0);
    const direction = String(item.resonance?.direction || "neutral");
    if (active >= 2 && direction !== "neutral") events.push({
      id: `${item.symbol}:resonance:${direction}`,
      symbol: item.symbol,
      coin: item.coin,
      observed_at: item.lifecycle?.observed_at || item.observed_at,
      window: "15m",
      event_type: "resonance_state",
      label: direction === "short" ? "空头共振" : "多头共振",
      detail: `${active}/5 个窗口同向 · ${item.lifecycle?.basis || "状态持续"}`,
      metric: "state",
      direction,
      value: null,
      rankings: item.rankings,
    });
    if (item.ambush?.triggered) events.push({
      id: `${item.symbol}:ambush:${item.ambush.direction || "neutral"}`,
      symbol: item.symbol,
      coin: item.coin,
      observed_at: item.lifecycle?.observed_at || item.observed_at,
      window: "15m",
      event_type: "ambush_state",
      label: item.ambush.direction === "short" ? "顶部派发" : "底部吸筹",
      detail: item.lifecycle?.basis || "资金同向积累，价格仍处压缩区间",
      metric: "state",
      direction: item.ambush.direction,
      value: null,
      rankings: item.rankings,
    });
    return events;
  }).slice(0, 12);
}

function mergeEventStreams(anomalies: RealtimeAnomalyEvent[], lifecycle: RealtimeAnomalyEvent[]): RealtimeAnomalyEvent[] {
  const merged: RealtimeAnomalyEvent[] = [];
  let lifecycleIndex = 0;
  for (let index = 0; index < anomalies.length && merged.length < 80; index += 1) {
    merged.push(anomalies[index]);
    if ((index + 1) % 3 === 0 && lifecycleIndex < lifecycle.length) {
      merged.push(lifecycle[lifecycleIndex]);
      lifecycleIndex += 1;
    }
  }
  while (lifecycleIndex < lifecycle.length && merged.length < 80) {
    merged.push(lifecycle[lifecycleIndex]);
    lifecycleIndex += 1;
  }
  return merged;
}

function EventFeed({ events, query }: { events: RealtimeAnomalyEvent[]; query: string }) {
  const filtered = events.filter((event) => !query || String(event.symbol || "").includes(query));
  return <div className="workstation-scroll min-h-0 flex-1 overflow-y-auto">
    {filtered.map((event, index) => {
      const positive = event.direction === "long";
      const primaryValue = event.metric === "state" ? "" : event.value_usd !== null && event.value_usd !== undefined ? money(event.value_usd) : percent(event.value);
      const self = event.rankings?.self;
      const strength = event.rankings?.market_strength;
      const absolute = event.rankings?.market_absolute;
      return <Link className="block border-b border-border-subtle px-2.5 py-[7px] transition-colors hover:bg-primary-50/55 min-[1024px]:py-[2px]" href={`/funds?symbol=${event.symbol || ""}`} key={event.id || `${event.symbol}-${event.event_type}-${index}`}>
        <div className="flex items-center gap-1.5">
          <span className="w-[33px] shrink-0 font-mono text-[8px] tabular-nums text-text-muted">{clock(event.observed_at)}</span>
          <CoinIcon coin={event.coin}/><span className="min-w-0 flex-1 truncate text-[10px] font-bold text-text-primary">${event.coin || event.symbol}</span>
          <span className={`rounded-[3px] px-1.5 py-[2px] text-[9px] font-semibold ${positive ? "bg-good/10 text-good" : "bg-risk/10 text-risk"}`}>{event.label || "异动"}</span>
        </div>
        <div className="mt-1 flex items-baseline justify-between gap-2 pl-[57px] text-[9px]"><span className="truncate text-text-muted">{event.detail || `${event.window || "5m"} 内 · ${event.metric === "volume" ? "成交量" : event.metric === "price" ? "价格" : event.metric === "liquidation" ? "爆仓额" : "主动资金"}`}</span>{primaryValue ? <span className={`shrink-0 font-mono font-semibold tabular-nums ${positive ? "text-good" : "text-risk"}`}>{primaryValue} {event.change_pct !== null && event.change_pct !== undefined && event.value_usd !== null && event.value_usd !== undefined ? `(${percent(event.change_pct, 1)})` : ""}</span> : null}</div>
        <div className="mt-1 flex gap-1 pl-[57px]"><RankBadge label="自身" rank={self?.rank} title={self?.method}/><RankBadge label="全场强度" rank={strength?.rank} title={strength?.method}/><RankBadge label="全场量级" rank={absolute?.rank} title={absolute?.method}/></div>
      </Link>;
    })}
    {!filtered.length ? <div className="grid h-28 place-items-center text-[10px] text-text-muted">{query ? `没有找到 ${query} 的异动` : "暂无异动事件 · 正在扫描"}</div> : null}
  </div>;
}

function boardValue(item: CockpitBoardItem, mode: RankMode) {
  if (mode === "strength") return finite(item.strength_percentile) === null ? "—" : `${Math.round(Number(item.strength_percentile))}分`;
  const magnitude = finite(item.magnitude_usd);
  const hasMagnitude = magnitude !== null && Math.abs(magnitude) > 0;
  const raw = hasMagnitude ? Math.sign(finite(item.value) || magnitude || 1) * Math.abs(magnitude) : item.value;
  return item.unit === "usd" || hasMagnitude ? money(raw) : percent(raw, item.unit === "percent_per_cycle" ? 3 : 2);
}

function MomentumList({ items, mode, positive, realtimeBySymbol, limit = 7 }: { items?: CockpitBoardItem[]; mode: RankMode; positive: boolean; realtimeBySymbol: Map<string, RealtimeIntelligenceItem>; limit?: number }) {
  return <div>{(items || []).slice(0, limit).map((item, index) => <Link className="grid h-[23px] grid-cols-[10px_14px_minmax(0,1fr)_29px_38px] items-center gap-[2px] border-b border-border-subtle/75 px-1 text-[8px] last:border-0 hover:bg-primary-50/50 min-[1024px]:h-[22px]" href={`/funds?symbol=${item.symbol || ""}`} key={`${item.symbol}-${index}`}>
    <span className="text-right font-mono text-[7px] text-text-muted">{index + 1}</span><CoinIcon coin={item.coin} size={13}/><span className="truncate font-semibold text-text-primary">{item.coin || item.symbol}</span><RankBlocks fallbackPercentile={finite(item.strength_percentile)} item={realtimeBySymbol.get(String(item.symbol || ""))} positive={positive}/><span className={`truncate text-right font-mono text-[7px] font-semibold tabular-nums ${positive ? "text-good" : "text-risk"}`}>{boardValue(item, mode)}</span>
  </Link>)}{!(items || []).length ? <div className="grid h-[74px] place-items-center text-[9px] text-text-muted">⏳ 暂无</div> : null}</div>;
}

function MomentumStrengthGrid({ items, positive, realtimeBySymbol }: { items?: CockpitBoardItem[]; positive: boolean; realtimeBySymbol: Map<string, RealtimeIntelligenceItem> }) {
  return <div className="grid grid-cols-1 sm:grid-cols-2" data-testid="radar-strength-grid">{(items || []).slice(0, 8).map((item, index) => {
    const realtime = realtimeBySymbol.get(String(item.symbol || ""));
    const active = Math.max(0, Math.min(5, Number(realtime?.resonance?.active_count || 0)));
    const score = finite(item.strength_percentile) ?? finite(realtime?.rankings?.market_strength?.percentile);
    return <Link className="flex h-[34px] min-w-0 flex-col items-center justify-center border-b border-r border-border-subtle/70 px-0.5 hover:bg-primary-50/55 min-[1024px]:h-[28px]" href={`/funds?symbol=${item.symbol || ""}`} key={`${item.symbol}-${index}`}>
      <span className="flex items-center gap-0.5"><small className="font-mono text-[6px] text-text-muted">{index + 1}</small><CoinIcon coin={item.coin} size={13}/></span>
      <span className="mt-0.5 inline-flex gap-px" aria-label={`五窗口共振 ${active}/5`}>{WINDOWS.map((key, block) => <i className={`h-[4px] w-[4px] rounded-[.5px] border ${block < active ? positive ? "border-primary-500/55 bg-primary-500/70" : "border-risk/50 bg-risk/65" : "border-border-subtle bg-surface-container-low"}`} key={key}/>)}</span>
      <span className={`mt-0.5 font-mono text-[6px] font-semibold ${positive ? "text-good" : "text-risk"}`}>{score === null ? "—" : `${Math.round(score)}%`}</span>
    </Link>;
  })}{!(items || []).length ? <div className="grid h-[136px] place-items-center text-[8px] text-text-muted sm:col-span-2">⏳ 暂无</div> : null}</div>;
}

function MomentumBoard({ board, realtimeBySymbol }: { board?: CockpitBoard; realtimeBySymbol: Map<string, RealtimeIntelligenceItem> }) {
  const labels = BOARD_LABELS[String(board?.key || "")] || { positive: board?.positive?.title || "上行", negative: board?.negative?.title || "下行" };
  const amountPositive = board?.amount_positive || board?.positive;
  const amountNegative = board?.amount_negative || board?.negative;
  const strengthPositive = board?.strength_positive || board?.positive;
  const strengthNegative = board?.strength_negative || board?.negative;
  return <section className="overflow-hidden rounded-[2px] border border-border-subtle bg-surface-panel">
    <div className="grid h-[25px] grid-cols-2 border-b border-border-subtle bg-surface-low text-[8px] font-semibold min-[1024px]:h-[23px]"><div className="flex items-center justify-between border-r border-border-subtle px-2 text-good"><span>▲ {labels.positive}</span><span className="rounded-[2px] bg-surface-container px-1 text-[7px] text-text-muted">量级榜</span></div><div className="flex items-center justify-between px-2 text-risk"><span>▼ {labels.negative}</span><span className="rounded-[2px] bg-surface-container px-1 text-[7px] text-text-muted">量级榜</span></div></div>
    <div className="grid grid-cols-2 divide-x divide-border-subtle"><MomentumList items={amountPositive?.items} mode="amount" positive realtimeBySymbol={realtimeBySymbol}/><MomentumList items={amountNegative?.items} mode="amount" positive={false} realtimeBySymbol={realtimeBySymbol}/></div>
    <div className="grid h-[23px] grid-cols-2 border-y border-border-subtle bg-surface-low/80 text-[8px] font-semibold"><div className="flex items-center justify-between border-r border-border-subtle px-2 text-good"><span>▲ {labels.positive}</span><span className="rounded-[2px] bg-warn/10 px-1 text-[7px] text-warn">强度榜</span></div><div className="flex items-center justify-between px-2 text-risk"><span>▼ {labels.negative}</span><span className="rounded-[2px] bg-warn/10 px-1 text-[7px] text-warn">强度榜</span></div></div>
    <div className="grid grid-cols-2 divide-x divide-border-subtle"><MomentumStrengthGrid items={strengthPositive?.items} positive realtimeBySymbol={realtimeBySymbol}/><MomentumStrengthGrid items={strengthNegative?.items} positive={false} realtimeBySymbol={realtimeBySymbol}/></div>
  </section>;
}

type ConfluenceEntry = CockpitBoardItem & { boardCount: number; divergent: boolean; positive: boolean };

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
  for (const board of boards) {
    const boardKey = String(board.key || "board");
    const positive = mode === "amount" ? board.amount_positive || board.positive : board.strength_positive || board.positive;
    const negative = mode === "amount" ? board.amount_negative || board.negative : board.strength_negative || board.negative;
    for (const item of (positive?.items || []).slice(0, 8)) add(item, boardKey, "positive");
    for (const item of (negative?.items || []).slice(0, 8)) add(item, boardKey, "negative");
  }
  const entries = [...tallies.values()].map(({ item, positive, negative }) => ({
    ...item,
    boardCount: new Set([...positive, ...negative]).size,
    divergent: positive.size > 0 && negative.size > 0,
    positive: positive.size >= negative.size,
  })).sort((a, b) => b.boardCount - a.boardCount || Number(b.strength_percentile || 0) - Number(a.strength_percentile || 0) || String(a.symbol || "").localeCompare(String(b.symbol || "")));
  const confirmed = entries.filter((item) => item.boardCount >= 2);
  return confirmed.length >= 5 ? confirmed : entries;
}

function RuleBoard({ title, subtitle, items, mode }: { title: string; subtitle: string; items: RealtimeIntelligenceItem[]; mode: "surge" | "ambush" | "total" }) {
  return <section className="workstation-panel flex min-h-0 flex-col"><PanelTitle title={title}/><div className="border-b border-border-subtle px-2 py-1 text-[8px] text-text-muted">{subtitle}</div><div className="workstation-scroll min-h-0 flex-1 overflow-auto">{items.map((item, index) => {
    const analysis = mode === "ambush" ? item.ambush : item.surge;
    const value = mode === "total" ? `${item.anomaly_24h?.count || 0}次` : `${finite(analysis?.score)?.toFixed(1) || "—"}分`;
    const positive = mode === "total" ? Number(item.anomaly_24h?.long_count || 0) >= Number(item.anomaly_24h?.short_count || 0) : analysis?.direction !== "short";
    return <Link className="grid h-[28px] grid-cols-[16px_18px_minmax(40px,1fr)_48px_auto] items-center gap-1 border-b border-border-subtle/75 px-2 text-[9px] hover:bg-primary-50/50" href={`/funds?symbol=${item.symbol || ""}`} key={item.symbol}><span className="font-mono text-[8px] text-text-muted">{index + 1}</span><CoinIcon coin={item.coin} size={15}/><span className="truncate font-semibold text-text-primary">{item.coin}</span><RankBlocks item={item} positive={positive}/><span className={`font-mono font-semibold ${positive ? "text-good" : "text-risk"}`}>{value}</span></Link>;
  })}{!items.length ? <div className="grid h-20 place-items-center text-[9px] text-text-muted">暂无符合条件的币种</div> : null}</div></section>;
}

export default function RadarPage() {
  const [momentum, setMomentum] = useState<Partial<Record<WindowKey, RadarBoards>>>({});
  const [realtime, setRealtime] = useState<RealtimeIntelligencePayload>({});
  const [overview, setOverview] = useState<MarketOverview>({});
  const [windowKey, setWindowKey] = useState<WindowKey>("15m");
  const [query, setQuery] = useState("");
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selectedSignal, setSelectedSignal] = useState("");

  const load = useCallback(async (bypassCache = false) => {
    setLoading(true);
    setError("");
    try {
      const options = { bypassCache };
      const [windowPayload, intelligence, market] = await Promise.all([
        getWorkstationRadarMomentumWindows(10, options), getRealtimeIntelligence(30, options), getMarketOverview(900, options),
      ]);
      setMomentum(windowPayload.windows as Partial<Record<WindowKey, RadarBoards>>);
      setRealtime(intelligence); setOverview(market);
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "雷达工作站加载失败");
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    const syncFromLocation = () => setSelectedSignal(new URLSearchParams(window.location.search).get("signal") || "");
    syncFromLocation();
    window.addEventListener("popstate", syncFromLocation);
    return () => window.removeEventListener("popstate", syncFromLocation);
  }, []);
  useEffect(() => { if (paused) return; const timer = window.setInterval(() => void load(true), 15_000); return () => window.clearInterval(timer); }, [load, paused]);

  const selectSignal = useCallback((signalId: number | string) => {
    const value = String(signalId || "");
    const url = new URL(window.location.href);
    if (value) url.searchParams.set("signal", value); else url.searchParams.delete("signal");
    window.history.replaceState(window.history.state, "", `${url.pathname}${url.search}${url.hash}`);
    setSelectedSignal(value);
  }, []);

  const items = realtime.items || [];
  const events = useMemo(() => {
    const anomalies = realtime.anomaly_events?.length ? realtime.anomaly_events : fallbackEvents(items);
    return mergeEventStreams(anomalies, lifecycleEvents(items));
  }, [items, realtime.anomaly_events]);
  const boards = momentum[windowKey]?.boards || [];
  const realtimeBySymbol = useMemo(() => new Map(items.map((item) => [String(item.symbol || ""), item])), [items]);
  const surge = useMemo(() => items.filter((item) => item.surge?.triggered).sort((a, b) => Number(b.surge?.score || 0) - Number(a.surge?.score || 0)).slice(0, 5), [items]);
  const ambush = useMemo(() => items.filter((item) => item.ambush?.triggered).sort((a, b) => Number(b.ambush?.score || 0) - Number(a.ambush?.score || 0)).slice(0, 8), [items]);
  const total = useMemo(() => items.filter((item) => Number(item.anomaly_24h?.count || 0) > 0).sort((a, b) => Number(b.anomaly_24h?.count || 0) - Number(a.anomaly_24h?.count || 0)).slice(0, 14), [items]);
  const market = overview.overview || {};
  const tendency = useMemo(() => confluenceFromBoards(boards, "amount").slice(0, 7), [boards]);
  const strengthFlow = useMemo(() => confluenceFromBoards(boards, "strength").slice(0, 7), [boards]);

  return <><div aria-busy={loading} className="workstation-page mercu-radar-grid" data-testid="radar-workstation">
    <aside className="workstation-panel flex min-h-0 flex-col" data-testid="radar-event-feed">
      <PanelTitle action={<span className="inline-flex items-center gap-1 text-[8px] font-semibold text-good"><span className="h-1.5 w-1.5 animate-pulse rounded-full bg-good"/>LIVE</span>} meta={clock(realtime.observed_at || realtime.generated_at)} title="异动监控"/>
      <div className="grid h-[34px] grid-cols-[1fr_86px] items-center gap-2 border-b border-border-subtle bg-primary-50 px-2.5"><span className="truncate text-[10px] font-semibold text-primary-700">✦ AI 全市场扫描</span><div className="relative"><span className="absolute left-2 top-1/2 -translate-y-1/2 text-[8px] text-text-muted">⌕</span><input aria-label="搜索币种" className="h-6 w-full rounded-[3px] border border-border-subtle bg-surface-panel pl-5 pr-1 text-[8px] uppercase text-text-primary outline-none placeholder:text-text-muted focus:border-primary-500" onChange={(event) => setQuery(event.target.value.trim().toUpperCase())} placeholder="搜索币种..." value={query}/></div></div>
      {error ? <div className="border-b border-risk/20 bg-risk/5 px-2 py-1 text-[8px] text-risk">{error} · 保留上次数据</div> : null}
      <EventFeed events={events} query={query}/>
      <div className="flex h-7 shrink-0 items-center border-t border-border-subtle px-2 text-[8px] text-text-muted"><span>{events.length} 条异动 · {paused ? "已暂停" : "15s 增量"}</span><button className="ml-auto font-semibold text-text-secondary" onClick={() => setPaused((value) => !value)} type="button">{paused ? "继续" : "暂停"}</button><button className="ml-2 font-semibold text-primary-600" disabled={loading} onClick={() => void load(true)} type="button">{loading ? "更新中…" : "立即更新"}</button></div>
    </aside>

    <main className="workstation-scroll min-h-0 overflow-y-auto" data-testid="radar-hot-money">
      <section className="workstation-panel flex min-h-[610px] flex-col [&>.workstation-panel-header]:h-10 min-[1024px]:[&>.workstation-panel-header]:h-[38px]">
        <PanelTitle action={<div className="flex items-center gap-0.5">{WINDOWS.map((key) => <button aria-pressed={windowKey === key} className={`h-6 min-w-9 rounded-[3px] px-2 font-mono text-[8px] font-semibold max-[640px]:min-w-11 ${windowKey === key ? "bg-primary-50 text-primary-700 ring-1 ring-primary-500/30" : "text-text-muted hover:bg-surface-low hover:text-text-primary"}`} key={key} onClick={() => setWindowKey(key)} type="button">{key}</button>)}</div>} meta={`更新 ${clock(momentum[windowKey]?.generated_at)}`} title="热钱观察榜单"/>
        <div className="grid min-h-0 flex-1 grid-cols-2 gap-1.5 overflow-hidden p-1.5 min-[1024px]:gap-[11px] min-[1024px]:py-2 min-[1024px]:pl-2 min-[1024px]:pr-3" data-testid="radar-momentum-matrix">{["price", "oi", "futures_flow", "spot_flow"].map((key) => <MomentumBoard board={boards.find((board) => board.key === key)} key={key} realtimeBySymbol={realtimeBySymbol}/>)}</div>
      </section>
      <div className="mt-1.5 grid h-[220px] min-h-0 grid-cols-[.9fr_1.15fr_.95fr] gap-1.5"><RuleBoard items={surge} mode="surge" subtitle="1h 滚动 · 加速度排序 · TOP 5" title="Surge 飙升榜"/><RuleBoard items={total} mode="total" subtitle="24h 累计异动 · TOP 14" title="24h 异动总榜"/><RuleBoard items={ambush} mode="ambush" subtitle="持仓蓄积 / 价格平静 / 等待突破" title="埋伏池"/></div>
    </main>

    <aside className="workstation-scroll min-h-0 overflow-y-auto" data-testid="radar-side-intelligence">
      <section className="workstation-panel"><PanelTitle action={<span className="rounded-full bg-primary-50 px-2 py-0.5 text-[7px] font-semibold text-primary-700">典型 {windowKey}</span>} title="资金倾向性"/>
        <div className="border-b border-border-subtle px-2 py-1.5 text-[9px] font-bold text-text-primary">资金流</div>
        {tendency.map((item, index) => <Link className="grid h-[28px] grid-cols-[16px_18px_minmax(0,1fr)_auto_auto] items-center gap-1 border-b border-border-subtle px-2 text-[9px] hover:bg-primary-50/50" href={`/funds?symbol=${item.symbol || ""}`} key={`${item.symbol}-${index}`} title={item.divergent ? "多榜方向存在分歧" : "多榜方向一致"}><span className="font-mono text-[8px] text-text-muted">{index + 1}</span><CoinIcon coin={item.coin} size={15}/><span className="truncate font-semibold">{item.coin || item.symbol}</span><span className="rounded-[2px] border border-border-subtle px-1 text-[7px] text-text-muted">{item.boardCount}榜{item.divergent ? "·分歧" : ""}</span><span className={`font-semibold ${item.positive ? "text-good" : "text-risk"}`}>{item.positive ? "流入" : "流出"}</span></Link>)}
        <div className="border-y border-border-subtle bg-surface-low px-2 py-1.5 text-[9px] font-bold text-text-primary">资金力度</div>
        {strengthFlow.map((item, index) => <Link className="grid h-[28px] grid-cols-[16px_18px_minmax(0,1fr)_auto_auto] items-center gap-1 border-b border-border-subtle px-2 text-[9px] hover:bg-primary-50/50" href={`/funds?symbol=${item.symbol || ""}`} key={`${item.symbol}-${index}`} title={item.divergent ? "强度榜方向存在分歧" : "强度榜方向一致"}><span className="font-mono text-[8px] text-text-muted">{index + 1}</span><CoinIcon coin={item.coin} size={15}/><span className="truncate font-semibold">{item.coin || item.symbol}</span><span className="rounded-[2px] border border-border-subtle px-1 text-[7px] text-text-muted">{item.boardCount}榜{item.divergent ? "·分歧" : ""}</span><span className={item.positive ? "text-good" : "text-risk"}>{item.positive ? "流入" : "流出"}</span></Link>)}
      </section>
      <section className="workstation-panel mt-1.5 overflow-hidden"><PanelTitle action={<span className="rounded-full bg-primary-50 px-2 py-0.5 text-[7px] font-semibold text-primary-700">典型 {windowKey}</span>} title="全场态势"/><div>{[["合约资金流入", money(market.futures_net_flow_usd), market.futures_net_flow_usd],["现货资金流入", money(market.spot_net_flow_usd), market.spot_net_flow_usd],["持仓量净增长", money(market.oi_net_change_usd), market.oi_net_change_usd],["全场涨跌", `涨 ${market.advancing || 0} · 跌 ${market.declining || 0}`, market.breadth_pct]].map(([label, value, raw]) => <div className="border-b border-border-subtle px-2 py-2 last:border-0" key={String(label)}><div className="flex items-center justify-between gap-2"><span className="text-[9px] font-semibold text-text-secondary">{label}</span><span className={`font-mono text-[10px] font-semibold ${tone(raw)}`}>{String(value)}</span></div><div className="mt-1 text-[7px] text-text-muted">较上一周期 · 基于可用市场样本滚动统计</div></div>)}</div></section>
    </aside>
  </div>{selectedSignal ? <SignalDetailDrawer onClose={() => selectSignal("")} onSelectSignal={selectSignal} signalId={selectedSignal}/> : null}</>;
}
