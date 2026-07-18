"use client";

import { useCallback, useEffect, useState } from "react";
import { CandlestickChart } from "@/components/CandlestickChart";
import { CoinIcon } from "@/components/CoinIcon";
import { MetricSeriesChart } from "@/components/MetricSeriesChart";
import { getCoinContext, getFundsAssets, getFundsSectors, getWorkstationFundsOpenInterest } from "@/lib/api";
import type { CoinContext, CoinSeriesPoint, CrossExchangeOpenInterest, FundsAsset, FundsAssetsPayload, FundsSectorsPayload } from "@/lib/types";

const SPANS = [
  { key: "16h", label: "16 小时", interval: "15m", bars: 64 },
  { key: "2d", label: "2 天", interval: "1h", bars: 48 },
  { key: "4d", label: "4 天", interval: "1h", bars: 96 },
  { key: "5d", label: "5 天", interval: "1h", bars: 120 },
  { key: "15d", label: "15 天", interval: "4h", bars: 90 },
  { key: "60d", label: "60 天", interval: "1d", bars: 60 }
] as const;

type MarketType = "spot" | "futures";
type SpanKey = (typeof SPANS)[number]["key"];
const SECTOR_WINDOWS = [{ value: 3600, label: "1 小时" }, { value: 14400, label: "4 小时" }, { value: 86400, label: "1 天" }] as const;
const ASSET_WINDOWS = [{ value: 900, label: "15 分钟" }, { value: 1800, label: "30 分钟" }, { value: 3600, label: "1 小时" }, { value: 14400, label: "4 小时" }, { value: 86400, label: "1 天" }] as const;

function finite(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function money(value: unknown, signed = true) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  const sign = signed ? (parsed > 0 ? "+" : parsed < 0 ? "−" : "") : "";
  const absolute = Math.abs(parsed);
  if (absolute >= 1e12) return `${sign}$${(absolute / 1e12).toFixed(2)}T`;
  if (absolute >= 1e9) return `${sign}$${(absolute / 1e9).toFixed(2)}B`;
  if (absolute >= 1e6) return `${sign}$${(absolute / 1e6).toFixed(1)}M`;
  if (absolute >= 1e3) return `${sign}$${(absolute / 1e3).toFixed(1)}K`;
  return `${sign}$${absolute.toFixed(2)}`;
}

function percent(value: unknown, digits = 2) {
  const parsed = finite(value);
  return parsed === null ? "—" : `${parsed > 0 ? "+" : ""}${parsed.toFixed(digits)}%`;
}

function tone(value: unknown) {
  const parsed = finite(value);
  return parsed === null || parsed === 0 ? "text-text-secondary" : parsed > 0 ? "text-good" : "text-risk";
}

function PanelTitle({ title, meta, action }: { title: string; meta?: string; action?: React.ReactNode }) {
  return <div className="workstation-panel-header"><div className="flex min-w-0 items-center gap-2"><h2 className="truncate text-[11px] font-bold text-text-primary">{title}</h2>{meta ? <span className="truncate text-[8px] text-text-muted">{meta}</span> : null}</div>{action}</div>;
}

function FlowPriceChart({ points, marketType }: { points: CoinSeriesPoint[]; marketType: MarketType }) {
  const metric = marketType === "spot" ? "spot_flow_usd" : "futures_flow_usd";
  let running = 0;
  const source = points.map((point, index) => { running += finite(point[metric]) || 0; return { index, cumulative: running, price: finite(point.price) }; }).filter((point) => point.price !== null);
  if (source.length < 2) return <div className="grid h-[150px] place-items-center text-[9px] text-text-muted">累计资金样本不足</div>;
  const flowMin = Math.min(...source.map((item) => item.cumulative));
  const flowMax = Math.max(...source.map((item) => item.cumulative));
  const priceMin = Math.min(...source.map((item) => Number(item.price)));
  const priceMax = Math.max(...source.map((item) => Number(item.price)));
  const x = (index: number) => 12 + index / Math.max(1, source.length - 1) * 576;
  const y = (value: number, min: number, max: number) => 132 - (value - min) / Math.max(max - min, 1e-9) * 108;
  const path = (key: "cumulative" | "price", min: number, max: number) => source.map((item, index) => `${index ? "L" : "M"}${x(index).toFixed(1)},${y(Number(item[key]), min, max).toFixed(1)}`).join(" ");
  return <div className="px-3 pb-2 pt-2"><div className="flex items-center gap-4 text-[8px]"><span className="text-primary-700">● 累计资金 {money(source.at(-1)?.cumulative)}</span><span className="text-text-muted">● 价格 {money(source.at(-1)?.price, false)}</span><span className="ml-auto text-text-muted">双轴归一化 · 比较趋势</span></div><svg aria-label="累计资金与价格时序" className="mt-1 h-[150px] w-full" role="img" viewBox="0 0 600 150">{[24, 78, 132].map((value) => <line className="stroke-border-subtle" strokeDasharray="3 5" x1="12" x2="588" y1={value} y2={value} key={value}/>)}<path className="fill-none stroke-primary-500" d={path("cumulative", flowMin, flowMax)} strokeWidth="2"/><path className="fill-none stroke-text-muted" d={path("price", priceMin, priceMax)} strokeWidth="1.3"/></svg></div>;
}

function AssetList({ assets, selected, onSelect }: { assets: FundsAsset[]; selected: string; onSelect: (symbol: string) => void }) {
  return <div className="workstation-scroll min-h-0 flex-1 overflow-auto">{assets.map((item, index) => <button className={`grid h-[38px] w-full grid-cols-[18px_18px_minmax(0,1fr)_74px] items-center gap-1.5 border-b border-border-subtle px-2 text-left hover:bg-primary-50/50 ${item.symbol === selected ? "border-l-2 border-l-primary-500 bg-primary-50/60" : "border-l-2 border-l-transparent"}`} onClick={() => onSelect(item.symbol || "")} type="button" key={item.symbol}><span className="text-right font-mono text-[8px] text-text-muted">{index + 1}</span><CoinIcon coin={item.coin} size={16}/><span className="min-w-0"><span className="block truncate text-[9px] font-semibold text-text-primary">{item.coin || item.symbol}</span><span className="block truncate text-[7px] text-text-muted">{item.sector?.primary_sector_label || "其他"}</span></span><span className="text-right"><span className={`block font-mono text-[9px] font-semibold ${tone(item.net_flow_usd)}`}>{money(item.net_flow_usd)}</span><span className={`block font-mono text-[7px] ${tone(item.price_change_pct)}`}>{percent(item.price_change_pct)}</span></span></button>)}{!assets.length ? <div className="grid h-40 place-items-center text-[9px] text-text-muted">资金数据正在积累</div> : null}</div>;
}

function SectorBubbleChart({ payload }: { payload: FundsSectorsPayload }) {
  const sectors = (payload.sectors || []).filter((item) => finite(item.net_flow_usd) !== null).sort((a, b) => Math.abs(Number(b.net_flow_usd || 0)) - Math.abs(Number(a.net_flow_usd || 0))).slice(0, 18);
  const max = Math.max(1, ...sectors.map((item) => Math.abs(Number(item.net_flow_usd || 0))));
  const positive = sectors.filter((item) => Number(item.net_flow_usd || 0) >= 0);
  const negative = sectors.filter((item) => Number(item.net_flow_usd || 0) < 0);
  const positivePositions = [[50, 43], [36, 39], [64, 38], [47, 28], [25, 31], [75, 29], [18, 45], [83, 45], [34, 20]];
  const negativePositions = [[50, 66], [35, 70], [64, 71], [47, 82], [25, 82], [74, 83], [17, 66], [83, 65], [38, 91], [59, 91]];
  const bubble = (item: (typeof sectors)[number], index: number, count: number, top: boolean) => {
    const size = 25 + Math.sqrt(Math.abs(Number(item.net_flow_usd || 0)) / max) * 25;
    const positions = top ? positivePositions : negativePositions;
    const [left, y] = positions[index % positions.length];
    const positiveTone = Number(item.net_flow_usd || 0) >= 0;
    return <div className={`absolute grid -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border text-center text-[7px] font-semibold leading-tight text-white shadow-sm ${positiveTone ? "border-good/20 bg-good/85" : "border-risk/20 bg-risk/85"}`} key={item.sector_id || item.label} style={{ width: size, height: size, left: `${Math.max(8, Math.min(92, left))}%`, top: `${Math.max(7, Math.min(92, y))}%` }} title={`${item.label}: ${money(item.net_flow_usd)}`}><span className="max-w-[90%] truncate">{item.label}</span></div>;
  };
  return <div className="relative min-h-[320px] flex-1 overflow-hidden bg-[linear-gradient(to_bottom,transparent_49.8%,rgb(var(--border-subtle))_50%,transparent_50.2%)]"><div className="absolute left-2 top-2 text-[7px] font-semibold text-good">流入 ↑</div><div className="absolute bottom-2 left-2 text-[7px] font-semibold text-risk">流出 ↓</div>{positive.map((item, index) => bubble(item, index, positive.length, true))}{negative.map((item, index) => bubble(item, index, negative.length, false))}{!sectors.length ? <div className="grid h-full place-items-center text-[9px] text-text-muted">板块资金真实样本正在积累</div> : null}<span className="absolute bottom-1 left-1/2 -translate-x-1/2 text-[7px] tracking-[.18em] text-text-muted/60">PaoXX 数据</span></div>;
}

function SectorOverview({ payload, windowSec, onWindow }: { payload: FundsSectorsPayload; windowSec: number; onWindow: (value: number) => void }) {
  const summary = payload.summary || {};
  const total = Math.max(1, Math.abs(Number(summary.inflow_usd || 0)) + Math.abs(Number(summary.outflow_usd || 0)));
  const inflowRatio = Math.abs(Number(summary.inflow_usd || 0)) / total * 100;
  return <section className="workstation-panel flex min-h-0 flex-col"><PanelTitle action={<div className="flex gap-0.5 rounded-[3px] border border-border-subtle bg-surface-low p-0.5">{SECTOR_WINDOWS.map((item) => <button aria-pressed={windowSec === item.value} className={`h-5 rounded-[2px] px-2 text-[7px] font-semibold ${windowSec === item.value ? "bg-primary-50 text-primary-700 ring-1 ring-primary-500/25" : "text-text-muted"}`} key={item.value} onClick={() => onWindow(item.value)} type="button">{item.label}</button>)}</div>} title="板块资金流"/><div className="min-h-[102px] border-b border-border-subtle px-2.5 py-2"><div className="flex items-end justify-between"><span className="rounded-[2px] bg-good/10 px-1.5 py-0.5 text-[7px] font-semibold text-good">{payload.market_type === "spot" ? "现货" : "合约"} · {SECTOR_WINDOWS.find((item) => item.value === windowSec)?.label}</span><span className="text-[8px] text-text-muted">整体{Number(summary.net_flow_usd || 0) >= 0 ? "补血" : "失血"}</span><strong className={tone(summary.net_flow_usd)}>{money(summary.net_flow_usd)}</strong></div><div className="mt-2 flex h-1.5 overflow-hidden rounded-full bg-risk"><div className="bg-good" style={{ width: `${inflowRatio}%` }}/></div><div className="mt-1 flex justify-between text-[7px]"><span className="text-good">● 流入 {money(summary.inflow_usd, false)}</span><span className="text-risk">流出 {money(summary.outflow_usd, false)} ●</span></div><div className="mt-1 flex justify-between text-[7px] text-text-muted"><span>领先 {summary.leading_inflow_sector || "—"}</span><span>更新 {payload.generated_at ? new Date(payload.generated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }) : "—"}</span></div></div><SectorBubbleChart payload={payload}/></section>;
}

function AssetsOverview({ assets, payload, query, setQuery, windowSec, onWindow, onSelect, onPage, onRefresh, loading, error }: { assets: FundsAsset[]; payload: FundsAssetsPayload; query: string; setQuery: (value: string) => void; windowSec: number; onWindow: (value: number) => void; onSelect: (value: string) => void; onPage: (value: number) => void; onRefresh: () => void; loading: boolean; error: string }) {
  const columns = "grid-cols-[28px_38px_minmax(120px,1.5fr)_repeat(9,minmax(48px,1fr))]";
  const pagination = payload.pagination;
  const page = Math.max(1, Number(pagination?.page || 1));
  const pageSize = Math.max(1, Number(pagination?.page_size || 20));
  const pageCount = Math.max(1, Number(pagination?.page_count || 1));
  const total = Math.max(0, Number(pagination?.total || 0));
  const pageOptions = Array.from(new Set([1, 2, 3, page - 1, page, page + 1, pageCount].filter((value) => value >= 1 && value <= pageCount))).sort((a, b) => a - b);
  const updatedAt = payload.generated_at ? new Date(payload.generated_at).toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col" data-testid="funds-assets-overview"><div className="flex h-[38px] shrink-0 items-center gap-2 border-b border-border-subtle bg-surface-low px-2"><div className="flex gap-0.5">{ASSET_WINDOWS.map((item) => <button aria-pressed={windowSec === item.value} className={`h-6 rounded-[3px] px-3 text-[8px] font-semibold ${windowSec === item.value ? "bg-primary-50 text-primary-700 ring-1 ring-primary-500/25" : "text-text-muted"}`} key={item.value} onClick={() => onWindow(item.value)} type="button">{item.label}</button>)}</div><div className="relative ml-[30px] w-[255px] shrink-0"><span className="absolute left-2 top-1/2 -translate-y-1/2 text-[9px] text-text-muted">⌕</span><input aria-label="搜索全体代币" className="h-7 w-full rounded-[3px] border border-border-subtle bg-surface-panel pl-6 pr-2 text-[8px] uppercase outline-none placeholder:text-text-muted focus:border-primary-500" onChange={(event) => setQuery(event.target.value.trim().toUpperCase())} placeholder="搜索全体代币 · 代码 / 名称" value={query}/></div><div className="ml-auto flex min-w-0 items-center gap-1.5 text-[7px] text-text-muted"><span className={`h-1.5 w-1.5 shrink-0 rounded-full ${error ? "bg-risk" : loading ? "animate-pulse bg-warn" : "bg-good"}`}/><span className="truncate">更新 {updatedAt}</span><button aria-label="刷新资金榜" className="grid h-5 w-5 shrink-0 place-items-center rounded-[2px] border border-border-subtle" disabled={loading} onClick={onRefresh} type="button">↻</button></div></div><div className="workstation-scroll min-h-0 flex-1 overflow-auto"><div className={`sticky top-0 z-10 grid h-8 min-w-[650px] ${columns} items-center border-b border-border-subtle bg-surface-low px-2 text-[7px] font-semibold text-text-muted [&>span]:min-w-0 [&>span]:truncate`}><span/><span/><span>币种</span><span className="text-right">净流入($)</span><span className="text-right">净流入变动</span><span className="text-right">交易量($)</span><span className="text-right">交易量变动</span><span className="text-right">流入($)</span><span className="text-right">流出($)</span><span className="text-right">市值($)</span><span className="text-right">当前币价</span><span className="text-right">价格(24h)</span></div>{assets.map((item, index) => <button className={`grid h-10 min-w-[650px] w-full ${columns} items-center border-b border-border-subtle px-2 text-left text-[8px] hover:bg-primary-50/45 [&>span]:min-w-0 [&>span]:truncate`} key={item.symbol || index} onClick={() => onSelect(item.symbol || "")} type="button"><span className="text-center text-[13px] text-text-muted">☆</span><span className="font-mono text-center text-primary-600">{(page - 1) * pageSize + index + 1}</span><span className="flex items-center gap-1.5 font-semibold"><CoinIcon coin={item.coin}/>{item.coin || item.symbol}</span><span className="justify-self-end rounded-[2px] bg-good/10 px-1.5 py-1 font-mono font-semibold text-good">{money(item.net_flow_usd)}</span><span className={`justify-self-end font-mono ${tone(item.net_flow_change_pct)}`}>{percent(item.net_flow_change_pct)}</span><span className="justify-self-end font-mono text-text-secondary">{money(item.volume_usd, false)}</span><span className={`justify-self-end font-mono ${tone(item.volume_change_pct)}`}>{percent(item.volume_change_pct)}</span><span className="justify-self-end font-mono text-good">{money(item.inflow_usd, false)}</span><span className="justify-self-end font-mono text-risk">{money(item.outflow_usd, false)}</span><span className="justify-self-end font-mono text-text-secondary">{money(item.market_cap, false)}</span><span className="justify-self-end font-mono text-text-secondary">{money(item.price, false)}</span><span className={`justify-self-end font-mono ${tone(item.price_change_pct)}`}>{percent(item.price_change_pct)}</span></button>)}{!assets.length ? <div className="grid h-40 place-items-center text-[9px] text-text-muted">{query ? "没有匹配的真实资产" : "资金样本正在积累"}</div> : null}</div><footer className="flex h-7 shrink-0 items-center border-t border-border-subtle bg-surface-panel px-2 text-[7px] text-text-muted"><span>共 {total} 个代币 · 每页 {pageSize} 条 · 第 {page}/{pageCount} 页</span><div className="ml-auto flex items-center gap-0.5"><button aria-label="上一页" className="h-6 min-w-6 rounded-[2px] border border-border-subtle px-1 disabled:opacity-35" disabled={page <= 1} onClick={() => onPage(page - 1)} type="button">‹</button>{pageOptions.map((value, index) => <span className="contents" key={value}>{index > 0 && value - pageOptions[index - 1] > 1 ? <span className="px-1">…</span> : null}<button aria-current={value === page ? "page" : undefined} className={`h-6 min-w-6 rounded-[2px] border px-1 font-mono ${value === page ? "border-primary-500 bg-primary-50 text-primary-700" : "border-border-subtle"}`} onClick={() => onPage(value)} type="button">{value}</button></span>)}<button aria-label="下一页" className="h-6 min-w-6 rounded-[2px] border border-border-subtle px-1 disabled:opacity-35" disabled={page >= pageCount} onClick={() => onPage(page + 1)} type="button">›</button></div></footer></section>;
}

function CrossOi({ payload }: { payload: CrossExchangeOpenInterest }) {
  const rows = payload.exchanges || [];
  return <div className="p-2.5"><div className="mb-2 flex items-baseline justify-between"><span className="text-[8px] text-text-muted">全网持仓</span><span className="font-mono text-[11px] font-semibold text-text-primary">{money(payload.total_oi_usd, false)}</span></div>{rows.map((item) => <div className="mb-2" key={item.exchange}><div className="flex items-center justify-between text-[8px]"><span className="capitalize text-text-secondary">{item.exchange}</span><span className="font-mono text-text-primary">{item.status === "ready" ? `${money(item.oi_usd, false)} · ${percent(item.share_pct)}` : "不可用"}</span></div><div className="mt-1 h-1 overflow-hidden rounded-full bg-surface-container"><div className="h-full rounded-full bg-primary-500" style={{ width: `${Math.max(0, Math.min(100, Number(item.share_pct || 0)))}%` }}/></div></div>)}{!rows.length ? <div className="grid h-24 place-items-center text-[8px] text-text-muted">跨所持仓正在加载</div> : null}<p className="mt-2 text-[7px] leading-4 text-text-muted">缺失交易所不按 0 计入分母；统一按美元名义价值比较。</p></div>;
}

export default function FundsPage() {
  const [marketType, setMarketType] = useState<MarketType>("spot");
  const [span, setSpan] = useState<SpanKey>("4d");
  const [selected, setSelected] = useState("");
  const [query, setQuery] = useState("");
  const [assetSearch, setAssetSearch] = useState("");
  const [assetPage, setAssetPage] = useState(1);
  const [sectorWindow, setSectorWindow] = useState(3600);
  const [assetWindow, setAssetWindow] = useState(900);
  const [spotSectors, setSpotSectors] = useState<FundsSectorsPayload>({});
  const [futuresSectors, setFuturesSectors] = useState<FundsSectorsPayload>({});
  const [spotAssets, setSpotAssets] = useState<FundsAssetsPayload>({});
  const [futuresAssets, setFuturesAssets] = useState<FundsAssetsPayload>({});
  const [coin, setCoin] = useState<CoinContext>({});
  const [crossOi, setCrossOi] = useState<CrossExchangeOpenInterest>({});
  const [loading, setLoading] = useState(true);
  const [coinLoading, setCoinLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const symbol = new URLSearchParams(window.location.search).get("symbol")?.toUpperCase();
    if (symbol && /^[A-Z0-9]{2,20}(?:USDT)?$/.test(symbol)) setSelected(symbol.endsWith("USDT") ? symbol : `${symbol}USDT`);
  }, []);

  const loadOverview = useCallback(async (bypassCache = false) => {
    setLoading(true); setError("");
    try {
      const options = { bypassCache };
      const [spotSectorData, futuresSectorData, spotAssetData, futuresAssetData] = await Promise.all([
        getFundsSectors(sectorWindow, "spot", options), getFundsSectors(sectorWindow, "futures", options),
        getFundsAssets({ window_sec: assetWindow, market_type: "spot", sort: "net_flow_usd", direction: "desc", page: assetPage, page_size: 20, search: assetSearch || undefined }, options),
        getFundsAssets({ window_sec: assetWindow, market_type: "futures", sort: "net_flow_usd", direction: "desc", page: assetPage, page_size: 20, search: assetSearch || undefined }, options)
      ]);
      setSpotSectors(spotSectorData); setFuturesSectors(futuresSectorData); setSpotAssets(spotAssetData); setFuturesAssets(futuresAssetData);
    } catch (loadError) { setError(loadError instanceof Error ? loadError.message : "资金总览加载失败"); }
    finally { setLoading(false); }
  }, [assetPage, assetSearch, assetWindow, sectorWindow]);

  const loadCoin = useCallback(async (bypassCache = false) => {
    if (!selected) { setCoinLoading(false); return; }
    setCoinLoading(true);
    const config = SPANS.find((item) => item.key === span) || SPANS[2];
    try {
      const [context, oi] = await Promise.all([getCoinContext(selected, { bypassCache }, { market_type: marketType, interval: config.interval, bars: config.bars }), getWorkstationFundsOpenInterest(selected, { bypassCache })]);
      setCoin(context); setCrossOi(oi);
    } catch (loadError) { setError(loadError instanceof Error ? loadError.message : "单币资金视图加载失败"); }
    finally { setCoinLoading(false); }
  }, [marketType, selected, span]);

  useEffect(() => { void loadOverview(); }, [loadOverview]);
  useEffect(() => { void loadCoin(); }, [loadCoin]);
  useEffect(() => {
    const timer = window.setTimeout(() => { setAssetPage(1); setAssetSearch(query); }, 250);
    return () => window.clearTimeout(timer);
  }, [query]);
  useEffect(() => { window.history.replaceState({}, "", selected ? `${window.location.pathname}?symbol=${selected}` : window.location.pathname); }, [selected]);

  const currentAssets = marketType === "spot" ? spotAssets.items || [] : futuresAssets.items || [];
  const visibleAssets = currentAssets.filter((item) => !query || String(item.symbol || "").includes(query));
  const selectedAsset = currentAssets.find((item) => item.symbol === selected) || futuresAssets.items?.find((item) => item.symbol === selected) || spotAssets.items?.find((item) => item.symbol === selected);
  const series = coin.series?.points || [];
  const oiDistribution = futuresAssets.distribution || {};
  const spotSummary = spotSectors.summary || {};
  const futuresSummary = futuresSectors.summary || {};
  const currentSectors = marketType === "spot" ? spotSectors : futuresSectors;

  return <div aria-busy={loading || Boolean(selected && coinLoading)} className="workstation-page mercu-funds-grid" data-testid="funds-workstation">
    <section className="workstation-scroll flex h-[44px] max-w-full shrink-0 items-center gap-2 overflow-x-auto border-b border-border-subtle px-2.5">
      <div className="flex rounded-[4px] border border-border-subtle bg-surface-low p-[2px]" role="group">{(["spot", "futures"] as const).map((value) => <button aria-pressed={marketType === value} className={`h-7 min-w-12 rounded-[3px] px-3 text-[9px] font-semibold ${marketType === value ? "bg-surface-panel text-text-primary shadow-sm ring-1 ring-border-subtle" : "text-text-muted"}`} onClick={() => { setAssetPage(1); setMarketType(value); }} type="button" key={value}>{value === "futures" ? "合约" : "现货"}</button>)}</div>
      {selected ? <><button className="h-7 shrink-0 rounded-[3px] border border-border-subtle px-2 text-[8px] font-semibold text-primary-600" onClick={() => setSelected("")} type="button">← 返回资金榜</button><div className="flex shrink-0 gap-1" role="group">{SPANS.map((item) => <button aria-pressed={span === item.key} className={`h-6 rounded-[3px] px-2 font-mono text-[8px] font-semibold ${span === item.key ? "bg-surface-container text-text-primary" : "text-text-muted"}`} onClick={() => setSpan(item.key)} title={item.label} type="button" key={item.key}>{item.key}</button>)}</div></> : null}
      {selected ? <div className="ml-auto flex items-center gap-2"><span className={`h-1.5 w-1.5 rounded-full ${error ? "bg-risk" : loading || coinLoading ? "animate-pulse bg-warn" : "bg-good"}`}/><span className="max-w-64 truncate text-[8px] text-text-muted">{error || `${selected} · ${coin.data_status || "loading"}`}</span><button className="h-6 rounded-[3px] border border-border-subtle px-2 text-[8px] font-semibold text-text-secondary hover:bg-surface-low" disabled={loading || coinLoading} onClick={() => { void loadOverview(true); void loadCoin(true); }} type="button">刷新</button></div> : null}
    </section>

    {!selected ? <main className="grid min-h-0 flex-1 grid-cols-[225px_minmax(0,1fr)] gap-3 px-2.5 py-1.5"><SectorOverview onWindow={setSectorWindow} payload={currentSectors} windowSec={sectorWindow}/><AssetsOverview assets={currentAssets} error={error} loading={loading} onPage={setAssetPage} onRefresh={() => { void loadOverview(true); }} onSelect={setSelected} onWindow={(value) => { setAssetPage(1); setAssetWindow(value); }} payload={marketType === "spot" ? spotAssets : futuresAssets} query={query} setQuery={setQuery} windowSec={assetWindow}/></main> : <>
      <section className="grid h-[62px] shrink-0 grid-cols-6 gap-1.5 px-1.5">{[
        ["现货净流", money(spotSummary.net_flow_usd), spotSummary.net_flow_usd, `${spotSummary.covered_assets || 0}/${spotSummary.asset_count || 0} 资产`],
        ["合约净流", money(futuresSummary.net_flow_usd), futuresSummary.net_flow_usd, `${futuresSummary.covered_assets || 0}/${futuresSummary.asset_count || 0} 资产`],
        ["当前价格", money(selectedAsset?.price, false), selectedAsset?.price_change_pct, percent(selectedAsset?.price_change_pct)],
        ["全网持仓", money(crossOi.total_oi_usd || selectedAsset?.oi_usd, false), selectedAsset?.oi_change_pct, percent(selectedAsset?.oi_change_pct)],
        ["资金费率", percent(selectedAsset?.funding_pct, 4), selectedAsset?.funding_pct, "当前周期"],
        ["跨所集中", percent(crossOi.top_exchange_share_pct), null, `${crossOi.coverage?.exchanges || 0}/${crossOi.coverage?.target || 3} 场所`]
      ].map(([label, value, raw, detail]) => <div className="workstation-panel px-2.5 py-1.5" key={String(label)}><div className="text-[8px] text-text-muted">{label}</div><div className={`mt-0.5 truncate font-mono text-[11px] font-semibold ${tone(raw)}`}>{String(value)}</div><div className="truncate text-[7px] text-text-muted">{detail}</div></div>)}</section>
      <main className="grid min-h-0 flex-1 grid-cols-[250px_minmax(0,1fr)_280px] gap-1.5 px-1.5 pb-1.5"><section className="workstation-panel flex min-h-0 flex-col"><PanelTitle action={<span className="font-mono text-[8px] text-text-muted">{visibleAssets.length}</span>} meta={`${ASSET_WINDOWS.find((item) => item.value === assetWindow)?.label} 窗口`} title={`${marketType === "spot" ? "现货" : "合约"}资金榜`}/><div className="border-b border-border-subtle p-1.5"><input aria-label="搜索资金资产" className="h-7 w-full rounded-[3px] border border-border-subtle bg-surface-panel px-2 text-[9px] uppercase text-text-primary outline-none" onChange={(event) => setQuery(event.target.value.trim().toUpperCase())} placeholder="搜索 BTC..." value={query}/></div><AssetList assets={visibleAssets} onSelect={setSelected} selected={selected}/></section><section className="grid min-h-0 grid-rows-[minmax(260px,1.35fr)_minmax(180px,.65fr)] gap-1.5"><div className="workstation-panel min-h-0 overflow-auto workstation-scroll"><PanelTitle action={<span className="text-[8px] text-text-muted">{coin.chart?.coverage?.returned || 0} 根</span>} meta={`${coin.chart?.source || "行情源"} · ${coin.chart?.interval || "—"}`} title={`${selected} ${marketType === "spot" ? "现货" : "合约（永续）"}`}/><CandlestickChart points={coin.chart?.points || []}/></div><div className="workstation-panel min-h-0 overflow-auto workstation-scroll"><PanelTitle meta={`过去 ${SPANS.find((item) => item.key === span)?.label || span}`} title={`${marketType === "spot" ? "现货" : "合约"}资金流`}/><FlowPriceChart marketType={marketType} points={series}/></div></section><aside className="grid min-h-0 grid-rows-[190px_minmax(180px,1fr)_150px] gap-1.5"><section className="workstation-panel overflow-auto workstation-scroll"><PanelTitle action={<span className={`text-[7px] ${crossOi.data_status === "ready" ? "text-good" : "text-warn"}`}>{(crossOi.data_status || "loading").toUpperCase()}</span>} meta="Binance · Bybit · OKX" title="跨所持仓对比"/><CrossOi payload={crossOi}/></section><section className="workstation-panel overflow-auto workstation-scroll"><PanelTitle meta={`${series.length} 个历史快照`} title="OI & 资金费率"/><div className="grid gap-2 p-2.5"><MetricSeriesChart label="OI 历史走势" metric="oi_usd" points={series} unit="usd"/><MetricSeriesChart label="资金费率历史" metric="funding_pct" points={series} unit="percent_per_cycle"/></div></section><section className="workstation-panel"><PanelTitle meta={`${oiDistribution.oi_covered_assets || 0} 个资产`} title="持仓分布 / 集中度"/><div className="p-2.5">{[["Top 10 OI", oiDistribution.top_10_oi_share_pct], ["Top 50 OI", oiDistribution.top_50_oi_share_pct], ["最大交易所", crossOi.top_exchange_share_pct]].map(([label, value]) => <div className="mb-2.5" key={String(label)}><div className="flex justify-between text-[8px]"><span className="text-text-muted">{label}</span><span className="font-mono text-text-primary">{percent(value)}</span></div><div className="mt-1 h-1 rounded-full bg-surface-container"><div className="h-full rounded-full bg-primary-500" style={{ width: `${Math.max(0, Math.min(100, Number(value || 0)))}%` }}/></div></div>)}</div></section></aside></main>
    </>}
  </div>;
}
