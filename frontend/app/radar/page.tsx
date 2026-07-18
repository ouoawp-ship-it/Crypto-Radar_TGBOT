"use client";

import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";
import { getFundsAssets, getMarketOverview, getRealtimeIntelligence, getWorkstationRadarMomentumWindows } from "@/lib/api";
import type {
  CockpitBoard,
  CockpitBoardItem,
  FundsAsset,
  MarketOverview,
  RadarBoards,
  RealtimeIntelligenceItem,
  RealtimeIntelligencePayload
} from "@/lib/types";

const WINDOWS = ["15m", "30m", "1h", "4h", "1d"] as const;
type WindowKey = (typeof WINDOWS)[number];
type RankMode = "amount" | "strength";

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
  return Number.isNaN(date.getTime()) ? "--:--" : date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
}

function tone(value: unknown): string {
  const number = finite(value);
  if (number === null || number === 0) return "text-text-secondary";
  return number > 0 ? "text-good" : "text-risk";
}

function DirectionPill({ direction }: { direction?: string }) {
  const isLong = direction === "long";
  const isShort = direction === "short";
  return <span className={`rounded-sm border px-1.5 py-0.5 text-[9px] font-bold ${isLong ? "border-good/30 bg-good/10 text-good" : isShort ? "border-risk/30 bg-risk/10 text-risk" : "border-border-subtle text-text-muted"}`}>{isLong ? "多" : isShort ? "空" : "中"}</span>;
}

function ResonanceDots({ item }: { item: RealtimeIntelligenceItem }) {
  const windows = item.resonance?.windows || [];
  return (
    <span aria-label="五窗口共振" className="inline-flex gap-0.5">
      {WINDOWS.map((key) => {
        const window = windows.find((candidate) => candidate.key === key);
        const active = Boolean(window?.active);
        const direction = window?.direction;
        return <span className={`h-1.5 w-3 rounded-[1px] ${active ? direction === "long" ? "bg-good" : direction === "short" ? "bg-risk" : "bg-primary-500" : "bg-surface-container"}`} key={key} title={`${key} ${direction || "unavailable"}`} />;
      })}
    </span>
  );
}

function PanelHeader({ title, detail, action }: { title: string; detail?: string; action?: React.ReactNode }) {
  return <div className="workstation-panel-header"><div className="min-w-0"><h2 className="truncate text-[12px] font-semibold text-text-primary">{title}</h2>{detail ? <p className="truncate text-[9px] text-text-muted">{detail}</p> : null}</div>{action}</div>;
}

function boardValue(item: CockpitBoardItem, boardKey?: string, mode?: RankMode): string {
  const value = mode === "amount" && finite(item.magnitude_usd) !== null
    ? Math.sign(finite(item.value) || 1) * Math.abs(finite(item.magnitude_usd) || 0)
    : item.value;
  if (mode === "strength") return finite(item.strength_percentile) === null ? "—" : `P${Math.round(Number(item.strength_percentile))}`;
  if (item.unit === "usd" || (mode === "amount" && finite(item.magnitude_usd) !== null)) return money(value);
  if (item.unit === "score") return finite(value) === null ? "—" : `${Number(value) > 0 ? "+" : ""}${Number(value).toFixed(1)}`;
  return percent(value, item.unit === "percent_per_cycle" ? 3 : 2);
}

function MomentumSide({ items, boardKey, mode }: { items?: CockpitBoardItem[]; boardKey?: string; mode: RankMode }) {
  return (
    <div className="min-w-0">
      {(items || []).slice(0, 6).map((item, index) => (
        <Link className="flex h-[27px] items-center gap-2 border-b border-border-subtle/70 px-2 text-[10px] last:border-0 hover:bg-surface-container/60" href={`/funds?symbol=${item.symbol || ""}`} key={`${item.symbol}-${index}`}>
          <span className="w-4 shrink-0 text-right font-mono text-[9px] text-text-muted">{index + 1}</span>
          <span className="min-w-0 flex-1 truncate font-semibold text-text-primary">{item.coin || item.symbol || "—"}</span>
          <span className={`table-number shrink-0 ${tone(item.value)}`}>{boardValue(item, boardKey, mode)}</span>
        </Link>
      ))}
      {!(items || []).length ? <div className="grid h-[81px] place-items-center text-[10px] text-text-muted">窗口数据积累中</div> : null}
    </div>
  );
}

function MomentumBoard({ board, mode }: { board?: CockpitBoard; mode: RankMode }) {
  const positive = mode === "amount" ? board?.amount_positive || board?.positive : board?.strength_positive || board?.positive;
  const negative = mode === "amount" ? board?.amount_negative || board?.negative : board?.strength_negative || board?.negative;
  return (
    <section className="workstation-panel min-w-0">
      <PanelHeader detail={`覆盖 ${board?.coverage || 0} · ${mode === "amount" ? "绝对量" : "横截面分位"}`} title={board?.title || "数据待加载"} />
      <div className="grid grid-cols-2 divide-x divide-border-subtle">
        <div><div className="h-6 border-b border-border-subtle px-2 pt-1 text-[9px] font-semibold text-good">{positive?.title || "上行"}</div><MomentumSide boardKey={board?.key} items={positive?.items} mode={mode} /></div>
        <div><div className="h-6 border-b border-border-subtle px-2 pt-1 text-[9px] font-semibold text-risk">{negative?.title || "下行"}</div><MomentumSide boardKey={board?.key} items={negative?.items} mode={mode} /></div>
      </div>
    </section>
  );
}

function EventFeed({ items, query }: { items: RealtimeIntelligenceItem[]; query: string }) {
  const filtered = items.filter((item) => !query || String(item.symbol || "").includes(query));
  return (
    <div className="workstation-scroll min-h-0 flex-1 overflow-y-auto">
      {filtered.map((item) => {
        const analysis = item.surge?.triggered ? item.surge : item.ambush?.triggered ? item.ambush : item.surge;
        const label = item.surge?.triggered ? "异动" : item.ambush?.triggered ? "埋伏" : "观察";
        return (
          <Link className="block border-b border-border-subtle px-2.5 py-2 hover:bg-surface-container/55" href={`/funds?symbol=${item.symbol || ""}`} key={item.symbol}>
            <div className="flex items-center gap-2"><span className="font-mono text-[9px] text-text-muted">{clock(item.observed_at)}</span><span className="min-w-0 flex-1 truncate text-[11px] font-semibold text-text-primary">{item.coin || item.symbol}</span><DirectionPill direction={analysis?.direction} /></div>
            <div className="mt-1 flex items-center justify-between gap-2"><span className="text-[9px] text-text-muted">{label} · 分值 {finite(analysis?.score)?.toFixed(1) || "—"}</span><ResonanceDots item={item} /></div>
            <div className="mt-1 flex gap-2 font-mono text-[9px]"><span className={tone(item.windows?.["5m"]?.cvd_usd)}>CVD {money(item.windows?.["5m"]?.cvd_usd)}</span><span className={tone(item.windows?.["5m"]?.price_change_pct)}>价 {percent(item.windows?.["5m"]?.price_change_pct)}</span></div>
          </Link>
        );
      })}
      {!filtered.length ? <div className="px-5 py-14 text-center text-[11px] leading-5 text-text-muted">没有匹配的实时异动。数据不足时保持空状态，不用模拟记录填充。</div> : null}
    </div>
  );
}

function RuleBoard({ title, items, mode }: { title: string; items: RealtimeIntelligenceItem[]; mode: "surge" | "ambush" | "total" }) {
  return (
    <section className="workstation-panel min-w-0">
      <PanelHeader detail={mode === "total" ? "近 24h 封闭窗口累计" : "规则候选 · 非收益预测"} title={title} />
      <div className="workstation-scroll h-[194px] overflow-y-auto">
        {items.map((item, index) => {
          const analysis = mode === "surge" ? item.surge : item.ambush;
          const value = mode === "total" ? `${item.anomaly_24h?.count || 0} 次` : `${finite(analysis?.score)?.toFixed(1) || "—"}`;
          return <Link className="flex h-[28px] items-center gap-2 border-b border-border-subtle px-2 text-[10px] hover:bg-surface-container/55" href={`/funds?symbol=${item.symbol || ""}`} key={item.symbol}><span className="w-4 text-right font-mono text-[9px] text-text-muted">{index + 1}</span><span className="min-w-0 flex-1 truncate font-semibold text-text-primary">{item.coin}</span>{mode !== "total" ? <DirectionPill direction={analysis?.direction} /> : null}<span className={`table-number ${mode === "total" ? "text-primary-700" : tone(analysis?.direction === "short" ? -Number(analysis?.score || 0) : analysis?.score)}`}>{value}</span></Link>;
        })}
        {!items.length ? <div className="grid h-24 place-items-center px-4 text-center text-[10px] text-text-muted">当前没有达到阈值的候选</div> : null}
      </div>
    </section>
  );
}

function FundingMonitor({ items }: { items: FundsAsset[] }) {
  return <div className="workstation-scroll min-h-0 flex-1 overflow-y-auto">{items.slice(0, 9).map((item) => <Link className="flex h-8 items-center gap-2 border-b border-border-subtle px-2.5 text-[10px] hover:bg-surface-container/55" href={`/funds?symbol=${item.symbol || ""}`} key={item.symbol}><span className="min-w-0 flex-1 truncate font-semibold text-text-primary">{item.coin}</span><span className={`table-number ${tone(item.funding_pct)}`}>{percent(item.funding_pct, 4)}</span><span className="w-14 truncate text-right text-[9px] text-text-muted">OI {money(item.oi_usd, false)}</span></Link>)}{!items.length ? <div className="grid h-24 place-items-center text-[10px] text-text-muted">费率事实暂不可用</div> : null}</div>;
}

export default function RadarPage() {
  const [momentum, setMomentum] = useState<Partial<Record<WindowKey, RadarBoards>>>({});
  const [realtime, setRealtime] = useState<RealtimeIntelligencePayload>({});
  const [overview, setOverview] = useState<MarketOverview>({});
  const [funding, setFunding] = useState<FundsAsset[]>([]);
  const [windowKey, setWindowKey] = useState<WindowKey>("1h");
  const [rankMode, setRankMode] = useState<RankMode>("amount");
  const [query, setQuery] = useState("");
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");

  const load = useCallback(async (bypassCache = false) => {
    setLoading(true);
    setError("");
    try {
      const options = { bypassCache };
      const [windowPayload, intelligence, market, fundingAssets] = await Promise.all([
        getWorkstationRadarMomentumWindows(8, options),
        getRealtimeIntelligence(30, options),
        getMarketOverview(3600, options),
        getFundsAssets({ window_sec: 3600, market_type: "futures", sort: "funding_pct", direction: "desc", page_size: 20 }, options)
      ]);
      setMomentum(windowPayload.windows as Partial<Record<WindowKey, RadarBoards>>);
      setRealtime(intelligence);
      setOverview(market);
      setFunding(fundingAssets.items || []);
      setUpdatedAt(intelligence.observed_at || intelligence.generated_at || market.generated_at || "");
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : "雷达工作站加载失败");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => {
    if (paused) return;
    const timer = window.setInterval(() => void load(true), 15_000);
    return () => window.clearInterval(timer);
  }, [load, paused]);

  const items = realtime.items || [];
  const boards = momentum[windowKey]?.boards || [];
  const surge = useMemo(() => items.filter((item) => item.surge?.triggered).sort((a, b) => Number(b.surge?.score || 0) - Number(a.surge?.score || 0)).slice(0, 5), [items]);
  const ambush = useMemo(() => items.filter((item) => item.ambush?.triggered).sort((a, b) => Number(b.ambush?.score || 0) - Number(a.ambush?.score || 0)).slice(0, 8), [items]);
  const total = useMemo(() => items.filter((item) => Number(item.anomaly_24h?.count || 0) > 0).sort((a, b) => Number(b.anomaly_24h?.count || 0) - Number(a.anomaly_24h?.count || 0)).slice(0, 14), [items]);
  const whales = useMemo(() => [...items].sort((a, b) => Number(b.windows?.["5m"]?.long_liquidation_usd || 0) + Number(b.windows?.["5m"]?.short_liquidation_usd || 0) - Number(a.windows?.["5m"]?.long_liquidation_usd || 0) - Number(a.windows?.["5m"]?.short_liquidation_usd || 0)).slice(0, 8), [items]);
  const market = overview.overview || {};

  return (
    <div aria-busy={loading} className="workstation-page grid gap-[10px] p-[10px] min-[1160px]:grid-cols-[268px_minmax(0,1fr)_268px]" data-testid="radar-workstation">
      <aside className="workstation-panel flex min-h-0 flex-col" data-testid="radar-event-feed">
        <PanelHeader action={<span className={`h-1.5 w-1.5 rounded-full ${error ? "bg-risk" : loading ? "animate-pulse bg-warn" : "bg-good"}`} />} detail={`${items.length} 币种 · ${paused ? "已暂停" : "15s 刷新"}`} title="异动流" />
        <div className="grid grid-cols-[1fr_auto] gap-1 border-b border-border-subtle p-2">
          <input aria-label="筛选异动币种" className="h-8 min-w-0 rounded-sm border border-border-subtle bg-surface-low px-2 text-[11px] uppercase text-text-primary outline-none focus:border-primary-500" onChange={(event) => setQuery(event.target.value.trim().toUpperCase())} placeholder="筛选 BTC" value={query} />
          <button className="h-8 rounded-sm border border-border-subtle bg-surface-low px-2 text-[10px] font-semibold text-text-secondary hover:text-text-primary" onClick={() => setPaused((value) => !value)} type="button">{paused ? "继续" : "暂停"}</button>
        </div>
        {error ? <div className="border-b border-risk/30 bg-risk/10 px-2 py-1.5 text-[9px] text-risk">{error} · 保留上次成功数据</div> : null}
        <EventFeed items={items} query={query} />
        <div className="flex h-8 shrink-0 items-center justify-between border-t border-border-subtle px-2 text-[9px] text-text-muted"><span>{clock(updatedAt)} UTC</span><button className="font-semibold text-primary-700" disabled={loading} onClick={() => void load(true)} type="button">{loading ? "刷新中" : "立即刷新"}</button></div>
      </aside>

      <main className="grid min-h-0 gap-[8px] grid-rows-[minmax(0,1fr)_232px]" data-testid="radar-hot-money">
        <section className="workstation-panel flex min-h-0 flex-col">
          <PanelHeader action={<div className="flex rounded-sm border border-border-subtle bg-surface-canvas p-0.5"><button aria-pressed={rankMode === "amount"} className={`h-6 rounded-[2px] px-2 text-[9px] font-semibold ${rankMode === "amount" ? "bg-surface-container text-text-primary" : "text-text-muted"}`} onClick={() => setRankMode("amount")} type="button">资金合流</button><button aria-pressed={rankMode === "strength"} className={`h-6 rounded-[2px] px-2 text-[9px] font-semibold ${rankMode === "strength" ? "bg-surface-container text-text-primary" : "text-text-muted"}`} onClick={() => setRankMode("strength")} type="button">资金力度</button></div>} detail="价格 · OI · 合约 CVD · 现货 CVD" title="热钱五窗口" />
          <div aria-label="五窗口共振" className="flex h-9 shrink-0 items-center gap-1 border-b border-border-subtle px-2" role="group">
            {WINDOWS.map((key) => <button aria-pressed={windowKey === key} className={`h-6 min-w-12 rounded-sm px-3 font-mono text-[10px] font-semibold ${windowKey === key ? "bg-primary-500 text-on-primary" : "bg-surface-low text-text-muted hover:text-text-primary"}`} key={key} onClick={() => setWindowKey(key)} type="button">{key}</button>)}
            <span className="ml-auto text-[9px] text-text-muted">封闭窗口 · 独立口径</span>
          </div>
          <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 p-2 sm:grid-cols-2">
            {["price", "oi", "futures_flow", "spot_flow"].map((key) => <MomentumBoard board={boards.find((board) => board.key === key)} key={key} mode={rankMode} />)}
          </div>
        </section>
        <div className="grid min-h-0 grid-cols-1 gap-2 min-[700px]:grid-cols-[0.9fr_1.2fr_0.9fr]">
          <RuleBoard items={surge} mode="surge" title="Surge · 1h 滚动" />
          <RuleBoard items={total} mode="total" title="24h 异动总榜" />
          <RuleBoard items={ambush} mode="ambush" title="埋伏池" />
        </div>
      </main>

      <aside className="grid min-h-0 gap-2 grid-rows-[154px_minmax(0,1fr)_minmax(0,1fr)]" data-testid="radar-side-intelligence">
        <section className="workstation-panel">
          <PanelHeader detail={`覆盖 ${overview.coverage?.assets || 0} 资产`} title="全场态势" />
          <div className="grid grid-cols-2 gap-px bg-border-subtle">
            {[
              ["市场广度", percent(market.breadth_pct), market.breadth_pct],
              ["现货净流", money(market.spot_net_flow_usd), market.spot_net_flow_usd],
              ["合约净流", money(market.futures_net_flow_usd), market.futures_net_flow_usd],
              ["OI 变化", money(market.oi_net_change_usd), market.oi_net_change_usd]
            ].map(([label, value, raw]) => <div className="bg-surface-panel px-2 py-2" key={String(label)}><div className="text-[9px] text-text-muted">{label}</div><div className={`table-number mt-0.5 truncate text-[11px] font-semibold ${tone(raw)}`}>{String(value)}</div></div>)}
          </div>
        </section>
        <section className="workstation-panel flex min-h-0 flex-col">
          <PanelHeader detail="封闭 5m 清算额与主动成交" title="鲸鱼监控" />
          <div className="workstation-scroll min-h-0 flex-1 overflow-y-auto">{whales.map((item) => { const long = Number(item.windows?.["5m"]?.long_liquidation_usd || 0); const short = Number(item.windows?.["5m"]?.short_liquidation_usd || 0); return <Link className="block border-b border-border-subtle px-2.5 py-2 hover:bg-surface-container/55" href={`/funds?symbol=${item.symbol || ""}`} key={item.symbol}><div className="flex items-center"><span className="flex-1 text-[10px] font-semibold text-text-primary">{item.coin}</span><span className="table-number text-[9px] text-primary-700">{money(long + short, false)}</span></div><div className="mt-1 flex justify-between text-[9px]"><span className="text-risk">多爆 {money(long, false)}</span><span className="text-good">空爆 {money(short, false)}</span></div></Link>; })}{!whales.length ? <div className="grid h-24 place-items-center text-[10px] text-text-muted">实时清算样本积累中</div> : null}</div>
        </section>
        <section className="workstation-panel flex min-h-0 flex-col">
          <PanelHeader detail="费率与单所 OI 事实" title="费率 / 基差监控" />
          <FundingMonitor items={funding} />
        </section>
      </aside>
    </div>
  );
}
