"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CandlestickChart } from "@/components/CandlestickChart";
import { CoinIcon } from "@/components/CoinIcon";
import { MetricSeriesChart } from "@/components/MetricSeriesChart";
import { getCoinContext, getWorkstationFundsOpenInterest, getWorkstationFundsSeries } from "@/lib/api";
import { formatDateTime, formatMetricValue, safeText } from "@/lib/format";
import { normalizeMarketSymbol } from "@/lib/symbol";
import type { CoinContext, CoinRelatedInfoItem, CrossExchangeOpenInterest, FundsSeriesPayload, RealtimeAnomalyEvent } from "@/lib/types";

const CHART_INTERVALS = ["1m", "5m", "15m", "1h", "4h", "1d"] as const;
const FLOW_WINDOWS = [
  { key: "16h", label: "16 小时", interval: "15m", bars: 64 },
  { key: "2d", label: "2 天", interval: "1h", bars: 48 },
  { key: "4d", label: "4 天", interval: "1h", bars: 96 },
  { key: "5d", label: "5 天", interval: "1h", bars: 120 },
  { key: "15d", label: "15 天", interval: "4h", bars: 90 },
  { key: "60d", label: "60 天", interval: "1d", bars: 60 },
] as const;
const EVENTS_PER_PAGE = 20;

function number(value: unknown) {
  const result = Number(value);
  return Number.isFinite(result) ? result : null;
}

function signed(value: unknown, unit?: string) {
  const numeric = number(value);
  if (numeric === null) return "--";
  const rendered = formatMetricValue(Math.abs(numeric), unit);
  return `${numeric >= 0 ? "+" : "-"}${rendered}`;
}

function eventTone(event: RealtimeAnomalyEvent) {
  return event.direction === "long" ? "good" : event.direction === "short" ? "risk" : "warn";
}

function eventDescription(event: RealtimeAnomalyEvent) {
  if (event.detail) return event.detail;
  const value = event.value_usd ?? event.value;
  const unit = event.value_usd !== null && event.value_usd !== undefined ? "usd" : event.metric === "funding" ? "percent_per_cycle" : "percent";
  return `${event.window || "5m"} 内 ${signed(value, unit)}`;
}

function DrawerTimeline({ events }: { events: RealtimeAnomalyEvent[] }) {
  const [page, setPage] = useState(1);
  const pageCount = Math.max(1, Math.ceil(events.length / EVENTS_PER_PAGE));
  const visible = events.slice((page - 1) * EVENTS_PER_PAGE, page * EVENTS_PER_PAGE);

  useEffect(() => setPage(1), [events]);

  return <section className="min-h-0">
    <h2 className="flex items-center gap-1.5 text-[10px] font-bold text-text-primary">异动时间轴 <span className="font-mono text-[8px] font-normal text-text-muted">· {events.length} 条 · 第 {page}/{pageCount} 页</span></h2>
    <div className="mt-1">
      {visible.length ? visible.map((event, index) => {
        const tone = eventTone(event);
        return <div className="grid min-h-[25px] grid-cols-[42px_7px_64px_minmax(0,1fr)] items-center gap-1 border-b border-border-subtle/65 text-[8px]" key={event.id || `${event.symbol}-${event.observed_at}-${index}`}>
          <time className="font-mono text-text-muted">{formatDateTime(event.observed_at).slice(-5)}</time>
          <span aria-hidden="true" className={`h-1.5 w-1.5 rounded-full border ${tone === "good" ? "border-good/45 bg-good/15" : tone === "risk" ? "border-risk/45 bg-risk/15" : "border-warn/45 bg-warn/15"}`}/>
          <span className={`truncate rounded-[2px] px-1 py-0.5 text-center font-semibold ${tone === "good" ? "bg-good/10 text-good" : tone === "risk" ? "bg-risk/10 text-risk" : "bg-warn/10 text-warn"}`}>{event.label || "异动"}</span>
          <span className="truncate font-mono text-text-secondary">{eventDescription(event)}</span>
        </div>;
      }) : <div className="grid h-28 place-items-center text-[9px] text-text-muted">暂无该币异动</div>}
    </div>
    {pageCount > 1 ? <div className="mt-2 flex items-center justify-center gap-1">
      <button className="h-6 rounded-[2px] border border-border-subtle px-2 text-[8px] text-text-secondary disabled:opacity-35" disabled={page === 1} onClick={() => setPage((value) => Math.max(1, value - 1))} type="button">« 上页</button>
      {Array.from({ length: pageCount }, (_, index) => index + 1).slice(0, 7).map((item) => <button aria-pressed={page === item} className={`h-6 min-w-6 rounded-[2px] border text-[8px] ${page === item ? "border-primary-300 bg-primary-50 text-primary-700" : "border-border-subtle text-text-muted"}`} key={item} onClick={() => setPage(item)} type="button">{item}</button>)}
      <button className="h-6 rounded-[2px] border border-border-subtle px-2 text-[8px] text-text-secondary disabled:opacity-35" disabled={page === pageCount} onClick={() => setPage((value) => Math.min(pageCount, value + 1))} type="button">下页 »</button>
    </div> : null}
  </section>;
}

function CrossExchangeTable({ payload }: { payload: CrossExchangeOpenInterest | null }) {
  const rows = payload?.exchanges || [];
  return <section className="border-t border-border-subtle pt-2">
    <h2 className="text-[10px] font-bold text-text-primary">跨所持仓对比</h2>
    <div className="mt-1 overflow-hidden rounded-[2px] border border-border-subtle">
      {rows.length ? rows.map((row, index) => <div className="grid h-6 grid-cols-[22px_minmax(0,1fr)_72px_44px] items-center border-b border-border-subtle/70 px-2 text-[8px] last:border-0" key={`${row.exchange}-${index}`}>
        <span className="font-mono text-text-muted">#{index + 1}</span><span className="font-semibold text-text-primary">{safeText(row.exchange)}</span><span className="text-right font-mono text-text-secondary">{formatMetricValue(row.oi_usd, "usd")}</span><span className="text-right font-mono text-text-muted">{formatMetricValue(row.share_pct, "percent")}</span>
      </div>) : <div className="grid h-16 place-items-center text-[8px] text-text-muted">跨所持仓数据暂不可用</div>}
    </div>
  </section>;
}

function RelatedInfoRow({ item, index }: { item: CoinRelatedInfoItem; index: number }) {
  const content = <><div className="flex items-center gap-1 text-[7px]"><span className="max-w-[160px] truncate rounded-[2px] bg-warn/10 px-1 font-semibold text-warn">{safeText(item.source || item.display?.module_label, item.module || "情报")}</span><time className="ml-auto shrink-0 font-mono text-text-muted">{formatDateTime(item.published_at || item.time).slice(-5)}</time></div><p className="mt-1 text-[8px] leading-4 text-text-secondary">{safeText(item.title || item.summary || item.display?.title, item.excerpt || item.display?.summary)}</p></>;
  const className = "block border-b border-border-subtle/70 bg-[#f7f9fc] px-2.5 py-2 last:border-0 hover:bg-surface-container-low";
  return item.url
    ? <a className={className} href={item.url} key={item.event_id || index} rel="noreferrer" target="_blank">{content}</a>
    : <article className={className} key={item.public_ref || item.id || index}>{content}</article>;
}

function RelatedInfo({ data }: { data: CoinContext | null }) {
  const items = (data?.related_info?.items?.length ? data.related_info.items : data?.timeline || []) as CoinRelatedInfoItem[];
  return <section className="border-t border-border-subtle pt-2">
    <h2 className="text-[10px] font-bold text-text-primary">相关信息 <span className="font-normal text-text-muted">· 近 30 天</span></h2>
    <div className="mt-1 overflow-hidden rounded-[2px] border border-border-subtle">
      {items.length ? items.slice(0, 8).map((item, index) => <RelatedInfoRow index={index} item={item} key={item.event_id || item.public_ref || item.id || index}/>) : <div className="grid h-16 place-items-center text-[8px] text-text-muted">近 30 天暂无相关信息</div>}
    </div>
  </section>;
}

export function MercuCoinDrawer({ events, onClose, symbol }: { events: RealtimeAnomalyEvent[]; onClose: () => void; symbol: string }) {
  const normalized = normalizeMarketSymbol(symbol);
  const coin = normalized.replace(/USDT$/i, "");
  const closeRef = useRef<HTMLButtonElement>(null);
  const [data, setData] = useState<CoinContext | null>(null);
  const [crossOi, setCrossOi] = useState<CrossExchangeOpenInterest | null>(null);
  const [flowSeries, setFlowSeries] = useState<FundsSeriesPayload | null>(null);
  const [interval, setInterval] = useState<(typeof CHART_INTERVALS)[number]>("15m");
  const [marketType, setMarketType] = useState<"futures" | "spot">("futures");
  const [flowWindow, setFlowWindow] = useState<(typeof FLOW_WINDOWS)[number]["key"]>("4d");
  const [loading, setLoading] = useState(true);
  const [flowLoading, setFlowLoading] = useState(true);
  const [error, setError] = useState("");
  const [flowError, setFlowError] = useState("");

  const load = useCallback(async (bypassCache = false) => {
    if (!normalized) return;
    setLoading(true);
    setError("");
    const [contextResult, oiResult] = await Promise.allSettled([
      getCoinContext(normalized, { bypassCache }, { market_type: marketType, interval, bars: 160 }),
      getWorkstationFundsOpenInterest(normalized, { bypassCache }),
    ]);
    if (contextResult.status === "fulfilled") setData(contextResult.value); else setError("单币上下文暂时不可用");
    if (oiResult.status === "fulfilled") setCrossOi(oiResult.value);
    setLoading(false);
  }, [interval, marketType, normalized]);

  const loadFlowSeries = useCallback(async (bypassCache = false) => {
    if (!normalized) return;
    const config = FLOW_WINDOWS.find((item) => item.key === flowWindow) || FLOW_WINDOWS[2];
    setFlowLoading(true);
    setFlowError("");
    try {
      setFlowSeries(await getWorkstationFundsSeries(normalized, "spot_flow", config.interval, config.bars, { bypassCache }));
    } catch {
      setFlowError("资金流时序暂时不可用");
    } finally {
      setFlowLoading(false);
    }
  }, [flowWindow, normalized]);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { void loadFlowSeries(); }, [loadFlowSeries]);
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    closeRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => { if (event.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKeyDown);
    return () => { document.body.style.overflow = previous; window.removeEventListener("keydown", onKeyDown); };
  }, [onClose]);

  const metrics = data?.market?.metrics || {};
  const price = metrics.price?.value;
  const change = number(metrics.price_24h_pct?.value);
  const symbolEvents = useMemo(() => events.filter((event) => normalizeMarketSymbol(event.symbol || event.coin || "") === normalized), [events, normalized]);
  const activeFlowWindow = FLOW_WINDOWS.find((item) => item.key === flowWindow) || FLOW_WINDOWS[2];
  const series = flowSeries?.points || [];

  return <div className="fixed inset-0 z-[100] bg-[#182130]/22 backdrop-blur-[2px]" data-testid="coin-drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section aria-label={`${coin} 单币详情`} aria-modal="true" className="fixed bottom-0 right-0 top-0 z-[101] flex w-full flex-col overflow-hidden bg-surface-panel shadow-[-16px_0_48px_rgba(24,33,48,.18)] min-[700px]:w-[82vw] min-[1160px]:w-[min(100vw,1160px)]" data-testid="mercu-coin-drawer" role="dialog">
      <header className="sticky top-0 z-10 shrink-0 border-b border-border-subtle bg-surface-panel px-3.5 py-2.5">
        <div className="flex items-center gap-2"><CoinIcon coin={coin} size={19}/><h1 className="text-[14px] font-bold text-text-primary">${coin}</h1><span className="font-mono text-[11px] font-semibold text-text-secondary">{price == null ? "--" : `$${Number(price).toLocaleString("en-US", { maximumFractionDigits: Number(price) < 1 ? 6 : 2 })}`}</span><span className={`rounded-[2px] px-1.5 py-0.5 font-mono text-[9px] font-semibold ${change !== null && change >= 0 ? "bg-good/12 text-good" : "bg-risk/12 text-risk"}`}>{change === null ? "--" : `${change >= 0 ? "+" : ""}${change.toFixed(2)}%`}</span>
          <button aria-label="关闭" className="ml-auto grid h-7 w-7 place-items-center border border-border-subtle text-base text-text-muted hover:bg-surface-low" onClick={onClose} ref={closeRef} type="button">×</button>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 pl-7 text-[8px] text-text-muted"><span>流通市值 <b className="font-mono font-medium text-text-secondary">{formatMetricValue(metrics.market_cap?.value, "usd")}</b></span><span>·</span><span>全网持仓 <b className="font-mono font-medium text-text-secondary">{formatMetricValue(crossOi?.total_oi_usd ?? metrics.oi_value?.value, "usd")}</b></span><span>·</span><span>24h 成交 <b className="font-mono font-medium text-text-secondary">{formatMetricValue(metrics.quote_volume?.value, "usd")}</b></span>{loading ? <span className="ml-1 text-primary-600">更新中…</span> : null}</div>
      </header>

      {error ? <div className="border-b border-risk/20 bg-risk/5 px-3 py-1.5 text-[8px] text-risk">{error}<button className="ml-2 font-semibold underline" onClick={() => void load(true)} type="button">重试</button></div> : null}
      <div className="grid min-h-0 flex-1 overflow-hidden min-[760px]:grid-cols-[38%_62%]">
        <div className="workstation-scroll min-h-0 overflow-y-auto border-r border-border-subtle px-3 py-2">
          <DrawerTimeline events={symbolEvents}/>
        </div>
        <div className="workstation-scroll min-h-0 overflow-y-auto">
          <section className="border-b border-border-subtle px-2.5 py-2">
            <div className="mb-1.5 flex flex-wrap items-center gap-1">
              {CHART_INTERVALS.map((item) => <button aria-pressed={interval === item} className={`h-6 min-w-8 rounded-[2px] border px-2 font-mono text-[8px] ${interval === item ? "border-warn/40 bg-warn/10 text-warn" : "border-border-subtle bg-surface-low text-text-muted"}`} key={item} onClick={() => setInterval(item)} type="button">{item === "1d" ? "1D" : item}</button>)}
              <span className="mx-1 h-4 w-px bg-border-subtle"/>
              {(["futures", "spot"] as const).map((item) => <button aria-pressed={marketType === item} className={`h-6 min-w-10 rounded-[2px] border px-2 text-[8px] ${marketType === item ? "border-warn/40 bg-warn/10 text-warn" : "border-border-subtle bg-surface-low text-text-muted"}`} key={item} onClick={() => setMarketType(item)} type="button">{item === "futures" ? "合约" : "现货"}</button>)}
            </div>
            <div className="overflow-hidden rounded-[3px] bg-[#101214] p-1 [&_.stroke-border-subtle]:stroke-white/10 [&_.fill-text-muted]:fill-white/50"><CandlestickChart points={data?.chart?.points || []}/></div>
          </section>

          <section className="border-b border-border-subtle px-3 py-2.5">
            <div className="flex items-center gap-1"><h2 className="text-[10px] font-bold text-text-primary">资金流</h2><span className="text-[8px] text-text-muted">· 过去 {activeFlowWindow.label}</span>{flowLoading ? <span className="text-[7px] text-primary-600">更新中…</span> : null}<div className="ml-auto flex gap-1">{FLOW_WINDOWS.map((item) => <button aria-pressed={flowWindow === item.key} className={`h-5 min-w-7 rounded-[2px] px-1.5 font-mono text-[7px] ${flowWindow === item.key ? "bg-primary-50 text-primary-700 ring-1 ring-primary-500/25" : "text-text-muted hover:bg-surface-low"}`} key={item.key} onClick={() => setFlowWindow(item.key)} type="button">{item.key}</button>)}</div></div>
            {flowError ? <div className="mt-1 text-[8px] text-risk">{flowError}<button className="ml-2 font-semibold underline" onClick={() => void loadFlowSeries(true)} type="button">重试</button></div> : null}
            <div className="mt-2 grid gap-2 xl:grid-cols-2"><div className="rounded-[2px] border border-border-subtle p-2"><MetricSeriesChart label="现货累计净流" metric="spot_flow_usd" points={series} unit="usd"/></div><div className="rounded-[2px] border border-border-subtle p-2"><MetricSeriesChart label="合约累计净流" metric="futures_flow_usd" points={series} unit="usd"/></div></div>
          </section>

          <div className="grid gap-3 px-3 py-2.5 xl:grid-cols-2"><CrossExchangeTable payload={crossOi}/><RelatedInfo data={data}/></div>
        </div>
      </div>
    </section>
  </div>;
}
