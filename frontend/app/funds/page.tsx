"use client";

import Link from "next/link";
import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import { DataStatusBadge } from "@/components/DataStatusBadge";
import { ErrorState } from "@/components/ErrorState";
import { FeatureUnavailable } from "@/components/FeatureUnavailable";
import { PageTitle } from "@/components/PageTitle";
import { SectorBubbleChart } from "@/components/SectorBubbleChart";
import { WatchlistButton } from "@/components/WatchlistButton";
import { getFundsAssets, getFundsSectors } from "@/lib/api";
import { formatDateTime, formatMetricValue, safeText } from "@/lib/format";
import { cockpitV2Enabled } from "@/lib/features";
import type { FundsAsset, FundsAssetsPayload, FundsSectorsPayload } from "@/lib/types";

const WINDOWS = [
  { value: 900, label: "15m" },
  { value: 1800, label: "30m" },
  { value: 3600, label: "1h" },
  { value: 14400, label: "4h" },
  { value: 86400, label: "1d" }
];

const SORTS = [
  { value: "net_flow_usd", label: "净流入" },
  { value: "price_change_pct", label: "涨跌幅" },
  { value: "volume_usd", label: "成交额" },
  { value: "oi_usd", label: "OI" },
  { value: "funding_pct", label: "资金费率" },
  { value: "market_cap", label: "市值" }
];

function tone(value?: number | null) {
  const number = Number(value);
  if (!Number.isFinite(number) || number === 0) return "text-text-muted";
  return number > 0 ? "text-emerald-700" : "text-red-700";
}

function statusTone(status?: string): "good" | "warn" | "neutral" {
  if (status === "ready") return "good";
  if (status === "degraded" || status === "stale") return "warn";
  return "neutral";
}

function SummaryCard({ label, value, detail, toneClass = "text-text-primary" }: { label: string; value: string; detail?: string; toneClass?: string }) {
  return <div className="cockpit-panel p-3"><div className="text-[11px] font-semibold text-text-muted">{label}</div><div className={`table-number mt-1 text-lg font-semibold ${toneClass}`}>{value}</div>{detail ? <div className="mt-1 text-[10px] text-text-muted">{detail}</div> : null}</div>;
}

function AssetMobileCard({ item }: { item: FundsAsset }) {
  return (
    <article className="cockpit-panel p-3">
      <div className="flex items-start justify-between gap-3">
        <div><div className="table-number text-base font-semibold text-text-primary">{safeText(item.symbol)}</div><div className="mt-0.5 text-[11px] text-text-muted">{safeText(item.sector?.primary_sector_label, "其他")} · {formatDateTime(item.updated_at)}</div></div>
        <DataStatusBadge label={safeText(item.data_status, "unavailable").toUpperCase()} tone={statusTone(item.data_status)} />
      </div>
      <div className="mt-3 grid grid-cols-2 gap-px overflow-hidden rounded-lg border border-border-subtle bg-border-subtle">
        {[
          ["价格", formatMetricValue(item.price)],
          ["涨跌", formatMetricValue(item.price_change_pct, "percent")],
          ["净流入", formatMetricValue(item.net_flow_usd, "usd")],
          ["OI 变化", formatMetricValue(item.oi_change_pct, "percent")]
        ].map(([label, value]) => <div className="bg-surface-panel p-2.5" key={label}><div className="text-[10px] text-text-muted">{label}</div><div className="table-number mt-1 text-sm font-semibold text-text-primary">{value}</div></div>)}
      </div>
      <div className="mt-3 grid grid-cols-3 gap-2"><WatchlistButton compact symbol={safeText(item.symbol)} /><Link className="btn-secondary px-2 text-xs" href={`/radar?symbol=${item.symbol}`}>信号</Link><Link className="btn px-2 text-xs" href={`/coin/${item.symbol}`}>证据</Link></div>
    </article>
  );
}

function FundsPageContent() {
  const [marketType, setMarketType] = useState<"spot" | "futures">("spot");
  const [windowSec, setWindowSec] = useState(3600);
  const [sector, setSector] = useState("");
  const [dataStatus, setDataStatus] = useState("");
  const [sortKey, setSortKey] = useState("net_flow_usd");
  const [direction, setDirection] = useState<"asc" | "desc">("desc");
  const [page, setPage] = useState(1);
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState("");
  const [sectors, setSectors] = useState<FundsSectorsPayload | null>(null);
  const [assets, setAssets] = useState<FundsAssetsPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const requestRef = useRef(0);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const market = params.get("market_type");
    const windowValue = Number(params.get("window_sec"));
    const initialQuery = params.get("q") || "";
    if (market === "spot" || market === "futures") setMarketType(market);
    if (WINDOWS.some((item) => item.value === windowValue)) setWindowSec(windowValue);
    setSector(params.get("sector") || "");
    setDataStatus(params.get("data_status") || "");
    setSortKey(params.get("sort") || "net_flow_usd");
    setDirection(params.get("direction") === "asc" ? "asc" : "desc");
    setQuery(initialQuery);
    setDraft(initialQuery);
    setReady(true);
  }, []);

  useEffect(() => {
    if (!ready) return;
    const params = new URLSearchParams();
    params.set("market_type", marketType);
    params.set("window_sec", String(windowSec));
    if (query) params.set("q", query);
    if (sector) params.set("sector", sector);
    if (dataStatus) params.set("data_status", dataStatus);
    params.set("sort", sortKey);
    params.set("direction", direction);
    if (page > 1) params.set("page", String(page));
    window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
  }, [ready, marketType, windowSec, query, sector, dataStatus, sortKey, direction, page]);

  async function load(refresh = false) {
    const request = ++requestRef.current;
    setLoading(true);
    setError("");
    try {
      const [sectorPayload, assetPayload] = await Promise.all([
        getFundsSectors(windowSec, marketType, { bypassCache: refresh }),
        getFundsAssets({ window_sec: windowSec, market_type: marketType, q: query, sector, data_status: dataStatus, sort: sortKey, direction, page, page_size: 50 }, { bypassCache: refresh })
      ]);
      if (request !== requestRef.current) return;
      setSectors(sectorPayload);
      setAssets(assetPayload);
    } catch (loadError) {
      if (request === requestRef.current) setError(loadError instanceof Error ? loadError.message : "资金中心加载失败");
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }

  useEffect(() => { if (ready) void load(); }, [ready, marketType, windowSec, query, sector, dataStatus, sortKey, direction, page]);

  const sectorOptions = sectors?.catalog || [];
  const sectorById = useMemo(() => new Map(sectorOptions.map((item) => [item.id, item.label])), [sectorOptions]);
  const pageCount = Math.max(1, Number(assets?.pagination?.page_count || 1));
  const summary = sectors?.summary;

  function applySearch(event: FormEvent) {
    event.preventDefault();
    setPage(1);
    setQuery(draft.trim().toUpperCase());
  }

  return (
    <div className="space-y-3">
      <PageTitle title="资金中心" subtitle="把板块轮动、主动买卖成交差与资产级证据放在同一个可追溯工作台中。" tags={[`${marketType === "spot" ? "现货" : "合约"} CVD`, `${WINDOWS.find((item) => item.value === windowSec)?.label || "1h"} 窗口`, `分类 ${safeText(sectors?.catalog_version)}`]} />

      <section className="cockpit-panel p-2.5">
        <div className="flex flex-col gap-2 lg:flex-row lg:items-center lg:justify-between">
          <div className="flex flex-wrap gap-1 rounded-lg bg-surface-container-low p-1">
            {(["spot", "futures"] as const).map((item) => <button className={`h-8 rounded-md px-4 text-xs font-semibold ${marketType === item ? "bg-surface-panel text-primary-700 shadow-soft" : "text-text-secondary"}`} key={item} onClick={() => { setMarketType(item); setPage(1); }} type="button">{item === "spot" ? "现货" : "合约"}</button>)}
          </div>
          <div className="flex flex-wrap items-center gap-1 rounded-lg bg-surface-container-low p-1">
            {WINDOWS.map((item) => <button className={`h-8 rounded-md px-3 text-xs font-semibold ${windowSec === item.value ? "bg-surface-panel text-primary-700 shadow-soft" : "text-text-secondary"}`} key={item.value} onClick={() => { setWindowSec(item.value); setPage(1); }} type="button">{item.label}</button>)}
          </div>
          <button className="btn-secondary h-9 text-xs" disabled={loading} onClick={() => void load(true)} type="button">{loading ? "刷新中" : "刷新数据"}</button>
        </div>
      </section>

      {error ? <ErrorState message={error} onRetry={() => void load(true)} /> : null}

      <section className="grid grid-cols-2 gap-2 lg:grid-cols-6">
        <SummaryCard label="总净流入" value={formatMetricValue(summary?.net_flow_usd, "usd")} toneClass={tone(summary?.net_flow_usd)} />
        <SummaryCard label="主动买入额" value={formatMetricValue(summary?.inflow_usd, "usd")} detail="已覆盖封闭窗口" />
        <SummaryCard label="主动卖出额" value={formatMetricValue(summary?.outflow_usd, "usd")} detail="已覆盖封闭窗口" />
        <SummaryCard label="资产覆盖" value={`${summary?.covered_assets || 0} / ${summary?.asset_count || 0}`} />
        <SummaryCard label="流入领先" value={safeText(sectorById.get(summary?.leading_inflow_sector), "—")} />
        <SummaryCard label="流出领先" value={safeText(sectorById.get(summary?.leading_outflow_sector), "—")} />
      </section>

      <div className="grid gap-3 xl:grid-cols-[minmax(360px,0.82fr)_minmax(680px,1.18fr)]">
        <section className="cockpit-panel min-w-0">
          <div className="cockpit-panel-header"><div><h2 className="text-sm font-semibold text-text-primary">板块资金</h2><p className="mt-0.5 text-[11px] text-text-muted">气泡面积=绝对净额 · 颜色=方向</p></div><DataStatusBadge label={safeText(sectors?.data_status, loading ? "loading" : "empty").toUpperCase()} tone={statusTone(sectors?.data_status)} /></div>
          {loading && !sectors ? <div className="h-[340px] animate-pulse bg-surface-container-low" /> : <SectorBubbleChart sectors={sectors?.sectors || []} selected={sector} onSelect={(value) => { setSector(value); setPage(1); }} />}
          {sector ? <div className="border-t border-border-subtle px-3 py-2 text-xs text-text-secondary">当前筛选：{safeText(sectorById.get(sector), sector)} <button className="ml-2 font-semibold text-primary-700" onClick={() => setSector("")} type="button">清除</button></div> : null}
        </section>

        <section className="cockpit-panel min-w-0">
          <div className="cockpit-panel-header"><div><h2 className="text-sm font-semibold text-text-primary">资产资金表</h2><p className="mt-0.5 text-[11px] text-text-muted">{assets?.pagination?.total || 0} 个匹配资产 · 缺失值始终排在最后</p></div><span className="text-[11px] text-text-muted">{formatDateTime(assets?.generated_at)}</span></div>
          <form className="grid gap-2 border-b border-border-subtle p-3 sm:grid-cols-2 xl:grid-cols-[minmax(160px,1fr)_140px_120px_120px_auto]" onSubmit={applySearch}>
            <input aria-label="搜索资产" className="input h-9 w-full text-xs" placeholder="BTC 或 BTCUSDT" value={draft} onChange={(event) => setDraft(event.target.value.toUpperCase())} />
            <select aria-label="板块筛选" className="input h-9 text-xs" value={sector} onChange={(event) => { setSector(event.target.value); setPage(1); }}><option value="">全部板块</option>{sectorOptions.map((item) => <option key={item.id} value={item.id}>{item.label}</option>)}</select>
            <select aria-label="数据状态" className="input h-9 text-xs" value={dataStatus} onChange={(event) => { setDataStatus(event.target.value); setPage(1); }}><option value="">全部状态</option><option value="ready">可用</option><option value="degraded">降级</option><option value="stale">过期</option><option value="unavailable">未覆盖</option></select>
            <select aria-label="排序字段" className="input h-9 text-xs" value={sortKey} onChange={(event) => { setSortKey(event.target.value); setPage(1); }}>{SORTS.map((item) => <option key={item.value} value={item.value}>{item.label}</option>)}</select>
            <div className="flex gap-1"><button className="btn h-9 flex-1 px-3 text-xs" type="submit">应用</button><button aria-label="切换排序方向" className="btn-secondary h-9 w-10 px-0" onClick={() => { setDirection((value) => value === "desc" ? "asc" : "desc"); setPage(1); }} type="button">{direction === "desc" ? "↓" : "↑"}</button></div>
          </form>

          <div className="hidden overflow-x-auto md:block">
            <table className="w-full min-w-[1040px] border-collapse text-left text-xs">
              <thead className="bg-surface-container-low text-[10px] uppercase tracking-wide text-text-muted"><tr>{["资产", "价格 / 涨跌", "净流入", "买入 / 卖出", "成交额", "OI / 变化", "费率", "市值", "状态", "操作"].map((label) => <th className="whitespace-nowrap border-b border-border-subtle px-3 py-2 font-semibold" key={label}>{label}</th>)}</tr></thead>
              <tbody>
                {loading && !assets ? Array.from({ length: 8 }).map((_, index) => <tr key={index}><td className="p-3" colSpan={10}><div className="h-8 animate-pulse rounded bg-surface-container-low" /></td></tr>) : (assets?.items || []).map((item) => <tr className="border-b border-border-subtle last:border-0 hover:bg-surface-container-low/60" key={item.symbol}>
                  <td className="px-3 py-2.5"><div className="font-semibold text-text-primary">{safeText(item.symbol)}</div><div className="mt-0.5 text-[10px] text-text-muted">{safeText(item.sector?.primary_sector_label, "其他")}</div></td>
                  <td className="px-3 py-2.5"><div className="table-number font-semibold text-text-primary">{formatMetricValue(item.price)}</div><div className={`table-number mt-0.5 ${tone(item.price_change_pct)}`}>{formatMetricValue(item.price_change_pct, "percent")}</div></td>
                  <td className={`table-number px-3 py-2.5 font-semibold ${tone(item.net_flow_usd)}`}>{formatMetricValue(item.net_flow_usd, "usd")}</td>
                  <td className="table-number px-3 py-2.5 text-text-secondary">{formatMetricValue(item.inflow_usd, "usd")}<span className="mx-1 text-text-muted">/</span>{formatMetricValue(item.outflow_usd, "usd")}</td>
                  <td className="table-number px-3 py-2.5 text-text-secondary">{formatMetricValue(item.volume_usd, "usd")}</td>
                  <td className="px-3 py-2.5"><div className="table-number text-text-secondary">{formatMetricValue(item.oi_usd, "usd")}</div><div className={`table-number mt-0.5 ${tone(item.oi_change_pct)}`}>{formatMetricValue(item.oi_change_pct, "percent")}</div></td>
                  <td className={`table-number px-3 py-2.5 ${tone(item.funding_pct)}`}>{formatMetricValue(item.funding_pct, "percent_per_cycle")}</td>
                  <td className="table-number px-3 py-2.5 text-text-secondary">{formatMetricValue(item.market_cap, "usd")}</td>
                  <td className="px-3 py-2.5"><DataStatusBadge label={safeText(item.data_status).toUpperCase()} tone={statusTone(item.data_status)} /></td>
                  <td className="px-3 py-2.5"><div className="flex gap-1"><WatchlistButton compact symbol={safeText(item.symbol)} /><Link className="btn-secondary h-10 px-3" href={`/coin/${item.symbol}`}>证据</Link></div></td>
                </tr>)}
              </tbody>
            </table>
          </div>
          <div className="grid gap-2 p-2 md:hidden">{(assets?.items || []).map((item) => <AssetMobileCard item={item} key={item.symbol} />)}</div>
          {!loading && !(assets?.items || []).length ? <div className="px-4 py-16 text-center text-sm text-text-muted">当前筛选没有匹配资产。</div> : null}
          <div className="flex items-center justify-between border-t border-border-subtle px-3 py-2"><span className="text-[11px] text-text-muted">第 {assets?.pagination?.page || page} / {pageCount} 页</span><div className="flex gap-2"><button className="btn-secondary h-8 px-3 text-xs" disabled={page <= 1 || loading} onClick={() => setPage((value) => Math.max(1, value - 1))} type="button">上一页</button><button className="btn-secondary h-8 px-3 text-xs" disabled={page >= pageCount || loading} onClick={() => setPage((value) => Math.min(pageCount, value + 1))} type="button">下一页</button></div></div>
        </section>
      </div>

      {(sectors?.warnings || assets?.warnings || []).length ? <section className="cockpit-panel border-amber-200 bg-amber-50/70 p-3"><h2 className="text-xs font-semibold text-amber-900">数据口径与降级说明</h2><ul className="mt-2 space-y-1 text-[11px] leading-5 text-amber-800">{Array.from(new Set([...(sectors?.warnings || []), ...(assets?.warnings || [])])).map((item) => <li key={item}>· {item}</li>)}</ul></section> : null}
    </div>
  );
}

export default function FundsPage() {
  return cockpitV2Enabled ? <FundsPageContent /> : <FeatureUnavailable title="资金中心" />;
}
