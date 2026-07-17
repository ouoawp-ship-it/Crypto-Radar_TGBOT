"use client";

import Link from "next/link";
import { FormEvent, useEffect, useRef, useState } from "react";
import { DataStatusBadge } from "@/components/DataStatusBadge";
import { ErrorState } from "@/components/ErrorState";
import { FeatureUnavailable } from "@/components/FeatureUnavailable";
import { PageTitle } from "@/components/PageTitle";
import { getInfoFeed } from "@/lib/api";
import { formatDateTime, safeText } from "@/lib/format";
import { cockpitV2Enabled } from "@/lib/features";
import type { InfoFeedPayload, NewsEvent } from "@/lib/types";

const WINDOWS = [
  { value: 86_400, label: "24h" },
  { value: 7 * 86_400, label: "7d" },
  { value: 30 * 86_400, label: "30d" }
];

function statusTone(status?: string): "good" | "warn" | "bad" | "neutral" {
  if (status === "ready") return "good";
  if (status === "degraded") return "warn";
  if (status === "unavailable") return "bad";
  return "neutral";
}

function importanceStyle(value?: string) {
  if (value === "high") return "border-red-200 bg-red-50 text-red-700";
  if (value === "medium") return "border-amber-200 bg-amber-50 text-amber-700";
  return "border-border-subtle bg-surface-container-low text-text-secondary";
}

function eventKindLabel(value?: string) {
  if (value === "risk") return "风险事件";
  if (value === "opportunity") return "关注事件";
  return "中性事件";
}

function NewsCard({ item, highlighted }: { item: NewsEvent; highlighted?: boolean }) {
  const analysis = item.ai_analysis;
  const sourceLinks = item.source_links?.length ? item.source_links : item.url ? [{ source: item.source, url: item.url, rights_status: item.rights_status }] : [];
  return (
    <article className={`cockpit-panel min-w-0 p-4 transition ${highlighted ? "ring-2 ring-primary-400" : ""}`} id={item.event_id ? `event-${item.event_id}` : undefined}>
      <div className="flex flex-wrap items-center gap-2 text-[10px]">
        <span className={`rounded-full border px-2 py-1 font-semibold uppercase ${importanceStyle(item.importance)}`}>{safeText(item.importance, "low")}</span>
        <span className="rounded-full border border-border-subtle px-2 py-1 font-semibold text-text-secondary">{eventKindLabel(item.event_kind)}</span>
        <span className="text-text-muted">{safeText(item.source)} · {safeText(item.language, "—").toUpperCase()}</span>
        {Number(item.cluster_size || 1) > 1 ? <span className="text-primary-700">{item.cluster_size} 个来源合并</span> : null}
      </div>
      <h2 className="mt-3 text-[15px] font-semibold leading-6 text-text-primary">{safeText(item.title)}</h2>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-text-muted">
        <span>{item.published_at ? formatDateTime(item.published_at) : `采集于 ${formatDateTime(item.collected_at)}`}</span>
        <span>{item.timestamp_quality === "source" ? "来源时间" : "来源未提供发布时间"}</span>
        <span>{safeText(item.rights_status, "link_only")}</span>
      </div>

      {item.symbols?.length ? <div className="mt-3 flex flex-wrap gap-1.5">{item.symbols.map((symbol) => <Link className="rounded-md bg-primary-50 px-2 py-1 text-[11px] font-semibold text-primary-700 hover:bg-primary-100" href={`/coin/${symbol}`} key={symbol}>{symbol}</Link>)}</div> : null}

      {analysis?.status === "ready" ? (
        <details className="mt-4 rounded-lg border border-border-subtle bg-surface-container-low/50 p-3">
          <summary className="cursor-pointer text-xs font-semibold text-text-primary">展开规则化解读与验证项</summary>
          <div className="mt-3 space-y-3 text-xs leading-5 text-text-secondary">
            <div><div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">官方事实</div><p>{safeText(analysis.fact_summary)}</p></div>
            <div><div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">可能影响 · 规则推断</div><p>{safeText(analysis.possible_impact)}</p></div>
            <div><div className="text-[10px] font-semibold uppercase tracking-wide text-text-muted">仍需验证</div><ul>{(analysis.verification_needed || []).map((value) => <li key={value}>· {value}</li>)}</ul></div>
            <p className="border-t border-border-subtle pt-2 text-[10px] text-text-muted">{safeText(analysis.fact_inference_boundary)}</p>
          </div>
        </details>
      ) : <p className="mt-4 rounded-lg bg-surface-container-low px-3 py-2 text-[11px] text-text-muted">{safeText(analysis?.reason, "该事件未生成自动解读，请直接核对官方原文。")}</p>}

      <div className="mt-4 flex flex-wrap items-center justify-between gap-2 border-t border-border-subtle pt-3">
        <span className="text-[10px] text-text-muted">只保留必要元数据，不复制受限正文</span>
        <div className="flex flex-wrap gap-2">
          {sourceLinks.slice(0, 3).map((link, index) => link.url ? <a className="btn-secondary h-8 px-3 text-xs" href={link.url} key={`${link.url}-${index}`} rel="noopener noreferrer" target="_blank">{safeText(link.source, "原文")} 原文 ↗</a> : null)}
        </div>
      </div>
    </article>
  );
}

function InfoPageContent() {
  const [payload, setPayload] = useState<InfoFeedPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [windowSec, setWindowSec] = useState(7 * 86_400);
  const [language, setLanguage] = useState("");
  const [importance, setImportance] = useState("");
  const [symbol, setSymbol] = useState("");
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [highlight, setHighlight] = useState("");
  const requestRef = useRef(0);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const initialWindow = Number(params.get("window_sec"));
    if (WINDOWS.some((item) => item.value === initialWindow)) setWindowSec(initialWindow);
    setLanguage(params.get("language") || "");
    setImportance(params.get("importance") || "");
    setSymbol((params.get("symbol") || "").toUpperCase());
    const initialQuery = params.get("q") || "";
    setQuery(initialQuery);
    setDraft(initialQuery);
    setHighlight(params.get("event") || "");
    setReady(true);
  }, []);

  useEffect(() => {
    if (!ready) return;
    const params = new URLSearchParams();
    params.set("window_sec", String(windowSec));
    if (language) params.set("language", language);
    if (importance) params.set("importance", importance);
    if (symbol) params.set("symbol", symbol);
    if (query) params.set("q", query);
    if (page > 1) params.set("page", String(page));
    if (highlight) params.set("event", highlight);
    window.history.replaceState({}, "", `${window.location.pathname}?${params.toString()}`);
  }, [ready, windowSec, language, importance, symbol, query, page, highlight]);

  async function load(refresh = false) {
    const request = ++requestRef.current;
    setLoading(true);
    setError("");
    try {
      const data = await getInfoFeed({ window_sec: windowSec, language, importance, symbol, q: query, page, page_size: 30 }, { bypassCache: refresh });
      if (request === requestRef.current) setPayload(data);
    } catch (loadError) {
      if (request === requestRef.current) setError(loadError instanceof Error ? loadError.message : "信息中心加载失败");
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }

  useEffect(() => { if (ready) void load(); }, [ready, windowSec, language, importance, symbol, query, page]);

  function applySearch(event: FormEvent) {
    event.preventDefault();
    setPage(1);
    setQuery(draft.trim());
  }

  const pageCount = Math.max(1, Number(payload?.pagination?.page_count || 1));
  return (
    <div className="space-y-3">
      <PageTitle title="信息中心" subtitle="把官方消息、关联资产与市场验证放在同一个版权合规的信息工作台中。" tags={["官方来源", "事件聚类", "事实 / 推断分离"]} />

      <section className="grid grid-cols-2 gap-2 lg:grid-cols-4">
        {(payload?.channels || [
          { key: "official", label: "官方公告", status: loading ? "loading" : "empty", count: 0 },
          { key: "authorized_zh", label: "授权中文资讯", status: "unavailable", count: 0 },
          { key: "authorized_en", label: "授权英文资讯", status: "unavailable", count: 0 },
          { key: "sentiment", label: "市场情绪", status: "unavailable", count: 0 }
        ]).map((channel) => <div className="cockpit-panel p-3" key={channel.key}>
          <div className="flex items-center justify-between gap-2"><span className="text-xs font-semibold text-text-primary">{safeText(channel.label)}</span><DataStatusBadge label={safeText(channel.status).toUpperCase()} tone={statusTone(channel.status)} /></div>
          <div className="table-number mt-2 text-xl font-semibold text-text-primary">{channel.count || 0}</div>
          <p className="mt-1 line-clamp-2 text-[10px] leading-4 text-text-muted">{safeText(channel.reason || channel.rights_status, channel.key === "official" ? "官方链接元数据" : "等待授权数据源")}</p>
        </div>)}
      </section>

      <section className="cockpit-panel p-3">
        <form className="grid gap-2 sm:grid-cols-2 xl:grid-cols-[auto_120px_130px_minmax(140px,0.7fr)_minmax(200px,1fr)_auto]" onSubmit={applySearch}>
          <div className="flex flex-wrap gap-1 rounded-lg bg-surface-container-low p-1">{WINDOWS.map((item) => <button className={`h-8 rounded-md px-3 text-xs font-semibold ${windowSec === item.value ? "bg-surface-panel text-primary-700 shadow-soft" : "text-text-secondary"}`} key={item.value} onClick={() => { setWindowSec(item.value); setPage(1); }} type="button">{item.label}</button>)}</div>
          <select aria-label="语言筛选" className="input h-10 text-xs" value={language} onChange={(event) => { setLanguage(event.target.value); setPage(1); }}><option value="">全部语言</option><option value="zh">中文</option><option value="en">English</option></select>
          <select aria-label="重要度筛选" className="input h-10 text-xs" value={importance} onChange={(event) => { setImportance(event.target.value); setPage(1); }}><option value="">全部重要度</option><option value="high">High</option><option value="medium">Medium</option><option value="low">Low</option></select>
          <input aria-label="币种筛选" className="input h-10 text-xs" placeholder="BTC 或 BTCUSDT" value={symbol} onChange={(event) => { setSymbol(event.target.value.toUpperCase()); setPage(1); }} />
          <input aria-label="搜索资讯" className="input h-10 text-xs" placeholder="搜索公告标题" value={draft} onChange={(event) => setDraft(event.target.value)} />
          <div className="flex gap-2"><button className="btn h-10 flex-1 px-4 text-xs" type="submit">应用</button><button className="btn-secondary h-10 px-3 text-xs" disabled={loading} onClick={() => void load(true)} type="button">刷新</button></div>
        </form>
      </section>

      {error ? <ErrorState message={error} onRetry={() => void load(true)} /> : null}

      <section className="grid gap-3 xl:grid-cols-2">
        {loading && !payload ? Array.from({ length: 4 }).map((_, index) => <div className="cockpit-panel h-64 animate-pulse bg-surface-container-low" key={index} />) : (payload?.items || []).map((item) => <NewsCard highlighted={highlight === item.event_id} item={item} key={item.event_id} />)}
      </section>
      {!loading && !(payload?.items || []).length ? <section className="cockpit-panel px-4 py-20 text-center"><h2 className="text-sm font-semibold text-text-primary">当前筛选没有授权信息事件</h2><p className="mt-2 text-xs text-text-muted">不会用未授权内容或虚构摘要填充空状态。</p></section> : null}

      <div className="flex items-center justify-between rounded-lg border border-border-subtle bg-surface-panel px-3 py-2"><span className="text-[11px] text-text-muted">第 {payload?.pagination?.page || page} / {pageCount} 页 · {payload?.pagination?.total || 0} 个事件簇</span><div className="flex gap-2"><button className="btn-secondary h-8 px-3 text-xs" disabled={page <= 1 || loading} onClick={() => setPage((value) => Math.max(1, value - 1))} type="button">上一页</button><button className="btn-secondary h-8 px-3 text-xs" disabled={page >= pageCount || loading} onClick={() => setPage((value) => Math.min(pageCount, value + 1))} type="button">下一页</button></div></div>

      {(payload?.warnings || []).length ? <section className="cockpit-panel border-amber-200 bg-amber-50/70 p-3"><h2 className="text-xs font-semibold text-amber-900">采集与授权说明</h2><ul className="mt-2 text-[11px] leading-5 text-amber-800">{payload?.warnings?.map((warning) => <li key={warning}>· {warning}</li>)}</ul></section> : null}
    </div>
  );
}

export default function InfoPage() {
  return cockpitV2Enabled ? <InfoPageContent /> : <FeatureUnavailable title="信息中心" />;
}
