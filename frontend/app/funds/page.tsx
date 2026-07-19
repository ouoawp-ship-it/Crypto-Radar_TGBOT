"use client";

import { useCallback, useEffect, useState, type CSSProperties } from "react";
import { CandlestickChart } from "@/components/CandlestickChart";
import { CoinIcon } from "@/components/CoinIcon";
import { MetricSeriesChart } from "@/components/MetricSeriesChart";
import { getCoinContext, getWorkstationFundsOpenInterest, getWorkstationFundsOverview, getWorkstationFundsSeries } from "@/lib/api";
import type { CoinContext, CoinSeriesPoint, CrossExchangeOpenInterest, FundsAsset, FundsAssetsPayload, FundsOverviewPayload, FundsSectorsPayload } from "@/lib/types";

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
type FundsSort = "net_flow_usd" | "net_flow_change_pct" | "volume_usd" | "volume_change_pct" | "inflow_usd" | "outflow_usd" | "market_cap" | "price" | "price_change_pct";
type SortDirection = "asc" | "desc";
const SECTOR_WINDOWS = [{ value: 3600, label: "1 小时" }, { value: 14400, label: "4 小时" }, { value: 86400, label: "1 天" }] as const;
const ASSET_WINDOWS = [{ value: 900, label: "15 分钟" }, { value: 1800, label: "30 分钟" }, { value: 3600, label: "1 小时" }, { value: 14400, label: "4 小时" }, { value: 86400, label: "1 天" }] as const;

function finite(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null;
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

function cnMoney(value: unknown, signed = true) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  const sign = signed ? (parsed > 0 ? "+" : parsed < 0 ? "−" : "") : "";
  const absolute = Math.abs(parsed);
  if (absolute >= 1e12) return `${sign}${(absolute / 1e12).toFixed(2)}万亿`;
  if (absolute >= 1e8) return `${sign}${(absolute / 1e8).toFixed(2)}亿`;
  if (absolute >= 1e4) return `${sign}${(absolute / 1e4).toFixed(2)}万`;
  return `${sign}${absolute.toFixed(2)}`;
}

function priceText(value: unknown) {
  const parsed = finite(value);
  if (parsed === null) return "—";
  if (Math.abs(parsed) >= 1_000) return parsed.toLocaleString("en-US", { maximumFractionDigits: 2 });
  if (Math.abs(parsed) >= 1) return parsed.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
  return parsed.toFixed(8).replace(/0+$/, "").replace(/\.$/, "");
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

function sectorsFromOverview(payload: FundsOverviewPayload): FundsSectorsPayload {
  return { schema_version: payload.schema_version, generated_at: payload.generated_at, window_sec: payload.sector_window_sec, market_type: payload.market_type, data_status: payload.data_status, coverage: payload.coverage, warnings: payload.warnings, summary: payload.summary, catalog: payload.catalog, sectors: payload.sectors, methodology: payload.methodology };
}

function assetsFromOverview(payload: FundsOverviewPayload): FundsAssetsPayload {
  return { schema_version: payload.schema_version, generated_at: payload.generated_at, window_sec: payload.asset_window_sec, market_type: payload.market_type, data_status: payload.data_status, coverage: payload.coverage, warnings: payload.warnings, filters: payload.filters, sort: payload.sort, distribution: payload.distribution, pagination: payload.pagination, items: payload.assets, methodology: payload.methodology };
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
  const sectors = (payload.sectors || []).filter((item) => finite(item.net_flow_usd) !== null).sort((a, b) => Math.abs(Number(b.net_flow_usd || 0)) - Math.abs(Number(a.net_flow_usd || 0)));
  const positive = sectors.filter((item) => Number(item.net_flow_usd || 0) >= 0).slice(0, 13);
  const negative = sectors.filter((item) => Number(item.net_flow_usd || 0) < 0).slice(0, 11);
  const positiveMax = Math.max(1, ...positive.map((item) => Math.abs(Number(item.net_flow_usd || 0))));
  const negativeMax = Math.max(1, ...negative.map((item) => Math.abs(Number(item.net_flow_usd || 0))));
  const compactPositivePositions = [[50, 10], [72, 23], [36, 23], [50, 23], [18, 31], [70, 40], [64, 31], [82, 32], [38, 40], [52, 40], [26, 40], [48, 31], [41, 46]];
  const widePositivePositions = [[50, 13], [64, 24], [36, 24], [84, 34], [20, 34], [64, 34], [50, 33], [38, 37], [52, 43], [20, 43], [36, 43], [70, 43], [84, 43]];
  const compactNegativePositions = [[62, 77], [40, 77], [23, 65], [55, 65], [38, 63], [69, 63], [23, 56], [69, 56], [40, 56], [55, 56], [82, 56]];
  const wideNegativePositions = [[61, 58], [55, 77], [81, 67], [39, 77], [18, 67], [50, 63], [61, 67], [39, 58], [20, 58], [38, 67], [81, 58]];
  const bubble = (item: (typeof sectors)[number], index: number, top: boolean) => {
    const ratio = Math.abs(Number(item.net_flow_usd || 0)) / (top ? positiveMax : negativeMax);
    const compactSize = 32 + Math.sqrt(ratio) * (top ? 36 : 24);
    const wideSize = 30 + Math.pow(ratio, 0.34) * (top ? 52 : 40);
    const compactPositions = top ? compactPositivePositions : compactNegativePositions;
    const widePositions = top ? widePositivePositions : wideNegativePositions;
    const [compactLeft, compactTop] = compactPositions[index % compactPositions.length];
    const [wideLeft, wideTop] = widePositions[index % widePositions.length];
    const positiveTone = Number(item.net_flow_usd || 0) >= 0;
    const bubbleStyle = {
      "--bubble-size-compact": `${compactSize}px`, "--bubble-size-wide": `${wideSize}px`,
      "--bubble-left-compact": `${compactLeft}%`, "--bubble-left-wide": `${wideLeft}%`,
      "--bubble-top-compact": `${compactTop}%`, "--bubble-top-wide": `${wideTop}%`,
      background: positiveTone ? "radial-gradient(circle at 35% 28%, rgb(75 201 154), rgb(31 165 117) 72%)" : "radial-gradient(circle at 35% 28%, rgb(239 112 119), rgb(218 72 83) 72%)",
      boxShadow: positiveTone ? "0 3px 10px rgb(31 165 117 / .28)" : "0 3px 10px rgb(218 72 83 / .24)",
    } as CSSProperties;
    return <div className={`funds-sector-bubble absolute grid -translate-x-1/2 -translate-y-1/2 place-items-center rounded-full border text-center text-[7px] font-semibold leading-tight text-white ${positiveTone ? "border-good/20" : "border-risk/20"}`} key={item.sector_id || item.label} style={bubbleStyle} title={`${item.label}: ${cnMoney(item.net_flow_usd)}`}><span className="max-w-[90%] truncate">{item.label}<small className="mt-0.5 block font-mono text-[6px] font-medium opacity-90">{cnMoney(item.net_flow_usd)}</small></span></div>;
  };
  return <div className="relative min-h-[320px] flex-1 overflow-hidden"><div className="absolute left-2 top-2 text-[7px] font-semibold text-good">流入 ↑</div><div className="absolute bottom-2 left-2 text-[7px] font-semibold text-risk">流出 ↓</div><span aria-hidden="true" className="absolute inset-x-2 top-1/2 border-t border-dashed border-border-subtle min-[1280px]:top-[52%]"/>{positive.map((item, index) => bubble(item, index, true))}{negative.map((item, index) => bubble(item, index, false))}{!sectors.length ? <div className="grid h-full place-items-center text-[9px] text-text-muted">板块资金真实样本正在积累</div> : null}<span className="absolute left-1/2 top-1/2 -translate-x-1/2 -translate-y-1/2 bg-surface-panel px-1 text-[7px] tracking-[.18em] text-text-muted/60 min-[1280px]:top-[52%]">PaoXX 数据</span></div>;
}

function SectorOverview({ payload, windowSec, onWindow }: { payload: FundsSectorsPayload; windowSec: number; onWindow: (value: number) => void }) {
  const summary = payload.summary || {};
  const total = Math.max(1, Math.abs(Number(summary.inflow_usd || 0)) + Math.abs(Number(summary.outflow_usd || 0)));
  const inflowRatio = Math.abs(Number(summary.inflow_usd || 0)) / total * 100;
  const positive = Number(summary.net_flow_usd || 0) >= 0;
  const leadingInflow = (payload.sectors || []).find((item) => item.label === summary.leading_inflow_sector || item.sector_id === summary.leading_inflow_sector);
  const leadingOutflow = (payload.sectors || []).find((item) => item.label === summary.leading_outflow_sector || item.sector_id === summary.leading_outflow_sector);
  return <section className="workstation-panel flex min-h-0 flex-col min-[1024px]:[&>.workstation-panel-header]:h-[38px] min-[1280px]:[&>.workstation-panel-header]:h-[46px]"><PanelTitle action={<div className="flex gap-0.5 rounded-[3px] border border-border-subtle bg-surface-low p-0.5">{SECTOR_WINDOWS.map((item) => <button aria-pressed={windowSec === item.value} className={`h-5 rounded-[2px] px-2 text-[7px] font-semibold ${windowSec === item.value ? "bg-primary-50 text-primary-700 ring-1 ring-primary-500/25" : "text-text-muted"}`} key={item.value} onClick={() => onWindow(item.value)} type="button">{item.label}</button>)}</div>} title="板块资金流"/><div className="min-h-[102px] border-b border-border-subtle bg-[#fcfcfc] px-2.5 py-2"><div className="flex items-end justify-between"><span className={`rounded-[2px] px-1.5 py-0.5 text-[7px] font-semibold ${positive ? "bg-good/10 text-good" : "bg-risk/10 text-risk"}`}>{payload.market_type === "spot" ? "现货" : "合约"} · {SECTOR_WINDOWS.find((item) => item.value === windowSec)?.label}</span><span className="text-[8px] text-text-muted">整体{positive ? "流入" : "流出"}</span><strong className={tone(summary.net_flow_usd)}>{cnMoney(summary.net_flow_usd)}</strong></div><div className="mt-2 flex h-1.5 overflow-hidden rounded-full bg-risk"><div className="bg-good" style={{ width: `${inflowRatio}%` }}/></div><div className="mt-1 flex justify-between text-[7px]"><span className="text-good">● 流入 {cnMoney(summary.inflow_usd, false)}</span><span className="text-risk">流出 {cnMoney(summary.outflow_usd, false)} ●</span></div><div className="mt-1 flex gap-1 text-[7px] text-text-muted"><span className="rounded-[2px] bg-good/5 px-1">领跑 {summary.leading_inflow_sector || "—"} {leadingInflow ? cnMoney(leadingInflow.net_flow_usd) : ""}</span><span className="rounded-[2px] bg-risk/5 px-1">领袖 {summary.leading_outflow_sector || "—"} {leadingOutflow ? cnMoney(leadingOutflow.net_flow_usd) : ""}</span><span className="ml-auto">更新 {payload.generated_at ? new Date(payload.generated_at).toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }) : "—"}</span></div></div><SectorBubbleChart payload={payload}/></section>;
}

function AssetsOverview({ assets, payload, query, setQuery, windowSec, onWindow, onSelect, onPage, onRefresh, loading, error, sortKey, direction, onSort, favorites, onFavorite }: {
  assets: FundsAsset[];
  payload: FundsAssetsPayload;
  query: string;
  setQuery: (value: string) => void;
  windowSec: number;
  onWindow: (value: number) => void;
  onSelect: (value: string) => void;
  onPage: (value: number) => void;
  onRefresh: () => void;
  loading: boolean;
  error: string;
  sortKey: FundsSort;
  direction: SortDirection;
  onSort: (key: FundsSort) => void;
  favorites: Set<string>;
  onFavorite: (symbol: string) => void;
}) {
  const columns = "grid-cols-[40px_150px_120px_repeat(8,minmax(0,1fr))] min-[1280px]:grid-cols-[50px_158px_132px_repeat(8,minmax(0,1fr))]";
  const pagination = payload.pagination;
  const page = Math.max(1, Number(pagination?.page || 1));
  const pageSize = Math.max(1, Number(pagination?.page_size || 20));
  const pageCount = Math.max(1, Number(pagination?.page_count || 1));
  const total = Math.max(0, Number(pagination?.total || 0));
  const maxAbsNetFlow = Math.max(1, ...assets.map((item) => Math.abs(Number(item.net_flow_usd || 0))));
  const pageOptions = Array.from(new Set([1, 2, 3, page - 1, page, page + 1, pageCount].filter((value) => value >= 1 && value <= pageCount))).sort((a, b) => a - b);
  const updatedAt = payload.generated_at ? new Date(payload.generated_at).toLocaleString("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }) : "—";
  const header = (label: string, key: FundsSort) => <button aria-label={`按${label}排序`} className={`min-w-0 truncate text-right ${sortKey === key ? "font-bold text-good" : "text-text-muted"}`} onClick={() => onSort(key)} type="button">{label}{sortKey === key ? direction === "desc" ? "↓" : "↑" : ""}</button>;

  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col" data-testid="funds-assets-overview">
    <div className="flex h-[38px] shrink-0 items-center gap-2 border-b border-border-subtle bg-[#f1f4f9] px-2 min-[1024px]:h-[42px] min-[1024px]:px-0.5 min-[1280px]:h-[46px]">
      <div className="flex gap-0.5">{ASSET_WINDOWS.map((item) => <button aria-pressed={windowSec === item.value} className={`h-6 rounded-[3px] px-3 text-[8px] font-semibold min-[1024px]:px-4 min-[1280px]:px-5 ${windowSec === item.value ? "bg-primary-50 text-primary-700 ring-1 ring-primary-500/25" : "text-text-muted"}`} key={item.value} onClick={() => onWindow(item.value)} type="button">{item.label}</button>)}</div>
      <div className="relative ml-[30px] w-[255px] shrink-0 min-[1024px]:ml-auto min-[1024px]:mr-1 min-[1024px]:w-[264px] min-[1280px]:mr-[-18px] min-[1280px]:w-[276px]"><span className="absolute left-2 top-1/2 -translate-y-1/2 text-[9px] text-text-muted">⌕</span><input aria-label="搜索全体代币" className="h-7 w-full rounded-[3px] border border-border-subtle bg-surface-panel pl-6 pr-7 text-[8px] uppercase outline-none placeholder:text-text-muted focus:border-primary-500" onChange={(event) => setQuery(event.target.value.trim().toUpperCase())} placeholder="搜索全体代币 · 代码 / 名称" value={query}/><kbd className="absolute right-2 top-1/2 -translate-y-1/2 rounded border border-border-subtle px-1 font-mono text-[7px] text-text-muted">/</kbd></div>
      <div className="ml-auto flex min-w-0 shrink-0 items-center gap-1.5 text-[7px] text-text-muted min-[1024px]:ml-0 min-[1280px]:hidden"><span className={`h-1.5 w-1.5 shrink-0 rounded-full ${error ? "bg-risk" : loading ? "animate-pulse bg-warn" : "bg-good"}`}/><span className="truncate">更新 {updatedAt}</span><button aria-label="刷新资金榜" className="grid h-5 w-5 shrink-0 place-items-center rounded-[2px] border border-border-subtle" disabled={loading} onClick={onRefresh} type="button">↻</button></div>
    </div>
    <div className="workstation-scroll min-h-0 flex-1 overflow-auto">
      <div className={`sticky top-0 z-10 grid h-8 min-w-[666px] ${columns} items-center border-b border-border-subtle bg-[#f1f4f9] text-[9px] font-semibold min-[1280px]:min-w-[1320px] [&>*]:min-w-0 [&>*]:px-[9px]`}><span/><span className="text-text-muted">币种</span>{header("净流入($)", "net_flow_usd")}{header("净流入变比", "net_flow_change_pct")}{header("交易量($)", "volume_usd")}{header("交易量变动", "volume_change_pct")}{header("流入($)", "inflow_usd")}{header("流出($)", "outflow_usd")}{header("市值($)", "market_cap")}{header("当前币价($)", "price")}{header("价格(24h)", "price_change_pct")}</div>
      {assets.map((item, index) => {
        const symbol = item.symbol || "";
        const favorite = favorites.has(symbol);
        const positiveFlow = Number(item.net_flow_usd || 0) >= 0;
        const netFlowBarWidth = Math.max(2, Math.abs(Number(item.net_flow_usd || 0)) / maxAbsNetFlow * 100);
        return <div className={`grid h-10 min-w-[666px] w-full cursor-pointer ${columns} items-center border-b border-border-subtle text-left font-mono text-[11px] font-semibold hover:bg-primary-50/45 min-[1280px]:h-11 min-[1280px]:min-w-[1320px] [&>*]:min-w-0 [&>*]:px-[9px] [&>span]:truncate`} data-testid="funds-asset-row" key={symbol || index} onClick={() => onSelect(symbol)} onKeyDown={(event) => { if (event.key === "Enter") onSelect(symbol); }} role="button" tabIndex={0}>
          <button aria-label={`${favorite ? "取消" : "添加"}${item.coin || symbol}自选`} className={`h-full text-right text-[15px] font-normal ${favorite ? "text-warn" : "text-text-muted"}`} onClick={(event) => { event.stopPropagation(); onFavorite(symbol); }} type="button">{favorite ? "★" : "☆"}</button>
          <span className="flex items-center gap-2"><span className="w-5 shrink-0 text-right text-primary-600">{(page - 1) * pageSize + index + 1}</span><CoinIcon coin={item.coin} size={22}/><span className="truncate text-text-primary">{item.coin || symbol}</span></span>
          <span className="flex h-full items-center !px-0"><span className={`relative flex h-7 w-full items-center justify-end overflow-hidden rounded-[2px] pr-[9px] ${positiveFlow ? "text-good" : "text-risk"}`}><span aria-hidden="true" className={`absolute inset-y-0 right-0 ${positiveFlow ? "bg-good/10" : "bg-risk/10"}`} style={{ width: `${netFlowBarWidth}%` }}/><span className="relative z-[1]">{cnMoney(item.net_flow_usd, false)}</span></span></span>
          <span className={`justify-self-end font-mono ${tone(item.net_flow_change_pct)}`}>{percent(item.net_flow_change_pct)}</span>
          <span className="justify-self-end font-mono text-text-secondary">{cnMoney(item.volume_usd, false)}</span>
          <span className={`justify-self-end font-mono ${tone(item.volume_change_pct)}`}>{percent(item.volume_change_pct)}</span>
          <span className="justify-self-end font-mono text-good">{cnMoney(item.inflow_usd, false)}</span>
          <span className="justify-self-end font-mono text-risk">{cnMoney(item.outflow_usd, false)}</span>
          <span className="justify-self-end font-mono text-text-secondary">{cnMoney(item.market_cap, false)}</span>
          <span className="justify-self-end font-mono text-text-secondary">{priceText(item.price)}</span>
          <span className={`justify-self-end font-mono ${tone(item.price_change_pct)}`}>{percent(item.price_change_pct)}</span>
        </div>;
      })}
      {!assets.length ? <div className="grid h-40 place-items-center text-[9px] text-text-muted">{query ? "没有匹配的真实资产" : "资金样本正在积累"}</div> : null}
    </div>
    <footer className="flex h-7 shrink-0 items-center border-t border-border-subtle bg-surface-panel px-2 text-[7px] text-text-muted"><span>共 {total} 个代币 · 每页 {pageSize} 条 · 第 {page}/{pageCount} 页</span><div className="ml-auto flex items-center gap-0.5"><button aria-label="上一页" className="h-6 min-w-6 rounded-[2px] border border-border-subtle px-1 disabled:opacity-35" disabled={page <= 1} onClick={() => onPage(page - 1)} type="button">‹</button>{pageOptions.map((value, index) => <span className="contents" key={value}>{index > 0 && value - pageOptions[index - 1] > 1 ? <span className="px-1">…</span> : null}<button aria-current={value === page ? "page" : undefined} className={`h-6 min-w-6 rounded-[2px] border px-1 font-mono ${value === page ? "border-primary-500 bg-primary-50 text-primary-700" : "border-border-subtle"}`} onClick={() => onPage(value)} type="button">{value}</button></span>)}<button aria-label="下一页" className="h-6 min-w-6 rounded-[2px] border border-border-subtle px-1 disabled:opacity-35" disabled={page >= pageCount} onClick={() => onPage(page + 1)} type="button">›</button></div></footer>
  </section>;
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
  const [assetSort, setAssetSort] = useState<FundsSort>("net_flow_usd");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [favorites, setFavorites] = useState<Set<string>>(new Set());
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
      const query = { sector_window_sec: sectorWindow, asset_window_sec: assetWindow, sort: assetSort, direction: sortDirection, page: assetPage, page_size: 20, search: assetSearch || undefined };
      const [spotOverview, futuresOverview] = await Promise.all([
        getWorkstationFundsOverview({ ...query, market_type: "spot" }, options),
        getWorkstationFundsOverview({ ...query, market_type: "futures" }, options)
      ]);
      setSpotSectors(sectorsFromOverview(spotOverview)); setFuturesSectors(sectorsFromOverview(futuresOverview));
      setSpotAssets(assetsFromOverview(spotOverview)); setFuturesAssets(assetsFromOverview(futuresOverview));
    } catch (loadError) { setError(loadError instanceof Error ? loadError.message : "资金总览加载失败"); }
    finally { setLoading(false); }
  }, [assetPage, assetSearch, assetSort, assetWindow, sectorWindow, sortDirection]);

  const loadCoin = useCallback(async (bypassCache = false) => {
    if (!selected) { setCoinLoading(false); return; }
    setCoinLoading(true);
    const config = SPANS.find((item) => item.key === span) || SPANS[2];
    try {
      const kind = marketType === "spot" ? "spot_flow" : "futures_flow";
      const [context, workstationSeries, oi] = await Promise.all([
        getCoinContext(selected, { bypassCache }, { market_type: marketType, interval: config.interval, bars: config.bars, include_series: 0 }),
        getWorkstationFundsSeries(selected, kind, config.interval, config.bars, { bypassCache }),
        getWorkstationFundsOpenInterest(selected, { bypassCache })
      ]);
      setCoin({ ...context, series: workstationSeries }); setCrossOi(oi);
    } catch (loadError) { setError(loadError instanceof Error ? loadError.message : "单币资金视图加载失败"); }
    finally { setCoinLoading(false); }
  }, [marketType, selected, span]);

  useEffect(() => { void loadOverview(); }, [loadOverview]);
  useEffect(() => { void loadCoin(); }, [loadCoin]);
  useEffect(() => {
    try {
      const stored = JSON.parse(window.localStorage.getItem("paoxx-funds-favorites") || "[]");
      if (Array.isArray(stored)) setFavorites(new Set(stored.filter((item) => typeof item === "string")));
    } catch { setFavorites(new Set()); }
  }, []);
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
  const toggleSort = (key: FundsSort) => {
    setAssetPage(1);
    if (assetSort === key) setSortDirection((value) => value === "desc" ? "asc" : "desc");
    else { setAssetSort(key); setSortDirection("desc"); }
  };
  const toggleFavorite = (symbol: string) => {
    if (!symbol) return;
    setFavorites((current) => {
      const next = new Set(current);
      if (next.has(symbol)) next.delete(symbol); else next.add(symbol);
      window.localStorage.setItem("paoxx-funds-favorites", JSON.stringify([...next]));
      return next;
    });
  };

  return <div aria-busy={loading || Boolean(selected && coinLoading)} className="workstation-page mercu-funds-grid" data-testid="funds-workstation">
    <section className="workstation-scroll flex h-[44px] max-w-full shrink-0 items-center gap-2 overflow-x-auto border-b border-border-subtle bg-surface-panel px-2.5 min-[1024px]:items-end min-[1024px]:px-4 min-[1024px]:pb-0.5">
      <div className="flex rounded-[4px] border border-border-subtle bg-[#f1f4f9] p-[2px]" role="group">{(["spot", "futures"] as const).map((value) => <button aria-pressed={marketType === value} className={`h-6 min-w-12 rounded-[3px] px-3 text-[9px] font-semibold min-[1024px]:min-w-14 ${marketType === value ? "bg-surface-panel text-text-primary shadow-sm ring-1 ring-border-subtle" : "text-text-muted"}`} onClick={() => { setAssetPage(1); setMarketType(value); }} type="button" key={value}>{value === "futures" ? "合约" : "现货"}</button>)}</div>
      {selected ? <><button className="h-7 shrink-0 rounded-[3px] border border-border-subtle px-2 text-[8px] font-semibold text-primary-600" onClick={() => setSelected("")} type="button">← 返回资金榜</button><div className="flex shrink-0 gap-1" role="group">{SPANS.map((item) => <button aria-pressed={span === item.key} className={`h-6 rounded-[3px] px-2 font-mono text-[8px] font-semibold ${span === item.key ? "bg-surface-container text-text-primary" : "text-text-muted"}`} onClick={() => setSpan(item.key)} title={item.label} type="button" key={item.key}>{item.key}</button>)}</div></> : null}
      {selected ? <div className="ml-auto flex items-center gap-2"><span className={`h-1.5 w-1.5 rounded-full ${error ? "bg-risk" : loading || coinLoading ? "animate-pulse bg-warn" : "bg-good"}`}/><span className="max-w-64 truncate text-[8px] text-text-muted">{error || `${selected} · ${coin.data_status || "loading"}`}</span><button className="h-6 rounded-[3px] border border-border-subtle px-2 text-[8px] font-semibold text-text-secondary hover:bg-surface-low" disabled={loading || coinLoading} onClick={() => { void loadOverview(true); void loadCoin(true); }} type="button">刷新</button></div> : null}
    </section>

    {!selected ? <main className="mercu-funds-overview-grid grid min-h-0 flex-1 grid-cols-[225px_minmax(0,1fr)] gap-3 px-2.5 py-1.5 min-[1024px]:px-4 min-[1024px]:py-2"><SectorOverview onWindow={setSectorWindow} payload={currentSectors} windowSec={sectorWindow}/><AssetsOverview assets={currentAssets} direction={sortDirection} error={error} favorites={favorites} loading={loading} onFavorite={toggleFavorite} onPage={setAssetPage} onRefresh={() => { void loadOverview(true); }} onSelect={setSelected} onSort={toggleSort} onWindow={(value) => { setAssetPage(1); setAssetWindow(value); }} payload={marketType === "spot" ? spotAssets : futuresAssets} query={query} setQuery={setQuery} sortKey={assetSort} windowSec={assetWindow}/></main> : <>
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
