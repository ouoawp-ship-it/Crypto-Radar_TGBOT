"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { getInfoFeed } from "@/lib/api";
import type { InfoFeedPayload, NewsEvent } from "@/lib/types";

const CHANNELS = [
  { key: "news", title: "新闻", subtitle: "授权中文与官方事件", query: {} },
  { key: "english", title: "English", subtitle: "Authorized English sources", query: { language: "en" } },
  { key: "kol", title: "KOL", subtitle: "已授权观点源", query: { source_type: "kol" } },
  { key: "plaza", title: "Binance 广场", subtitle: "已授权广场源", query: { source_type: "plaza" } }
] as const;

function clock(value?: string) {
  if (!value) return "--:--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--";
  const sameDay = new Date().toDateString() === date.toDateString();
  return sameDay
    ? date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false })
    : date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}

function eventTone(item: NewsEvent) {
  if (item.event_kind === "risk") return "border-risk/35 bg-risk/5";
  if (item.event_kind === "opportunity") return "border-good/30 bg-good/5";
  return "border-border-subtle";
}

function FeedItem({ item }: { item: NewsEvent }) {
  const content = (
    <article className={`border-b px-3 py-2.5 transition hover:bg-surface-container/45 ${eventTone(item)}`}>
      <div className="flex items-center gap-2 text-[9px] text-text-muted">
        <span className="font-mono">{clock(item.published_at)}</span>
        <span className="min-w-0 flex-1 truncate">{item.source || "未知来源"}</span>
        {item.importance === "high" ? <span className="rounded-sm bg-risk/12 px-1 py-0.5 font-bold text-risk">重要</span> : null}
      </div>
      <h3 className="mt-1.5 text-[11px] font-semibold leading-[1.45] text-text-primary">{item.title || "未命名事件"}</h3>
      {item.ai_analysis?.fact_summary || item.summary ? <p className="mt-1 line-clamp-2 text-[10px] leading-[1.55] text-text-secondary">{item.ai_analysis?.fact_summary || item.summary}</p> : null}
      <div className="mt-2 flex items-center gap-1.5">
        {(item.symbols || []).slice(0, 3).map((symbol) => <span className="rounded-sm border border-border-subtle bg-surface-low px-1 py-0.5 font-mono text-[8px] text-primary-700" key={symbol}>{symbol.replace("USDT", "")}</span>)}
        <span className="ml-auto text-[8px] text-text-muted">{item.rights_status === "official_link_only" ? "官方链接" : item.rights_status || "来源待核验"}</span>
      </div>
    </article>
  );
  return item.url ? <a href={item.url} rel="noreferrer" target="_blank">{content}</a> : content;
}

function InfoColumn({ title, subtitle, payload, loading }: { title: string; subtitle: string; payload?: InfoFeedPayload; loading: boolean }) {
  const items = payload?.items || [];
  return (
    <section className="workstation-panel flex min-h-0 flex-col">
      <div className="workstation-panel-header h-10">
        <div><h2 className="text-[12px] font-semibold text-text-primary">{title}</h2><p className="text-[9px] text-text-muted">{subtitle}</p></div>
        <div className="flex items-center gap-1.5"><span className={`h-1.5 w-1.5 rounded-full ${payload?.data_status === "ready" ? "bg-good" : payload?.data_status === "unavailable" ? "bg-risk" : "bg-warn"}`} /><span className="font-mono text-[9px] text-text-muted">{items.length}</span></div>
      </div>
      <div className="workstation-scroll min-h-0 flex-1 overflow-y-auto">
        {items.map((item, index) => <FeedItem item={item} key={item.event_id || `${item.title}-${index}`} />)}
        {loading && !items.length ? Array.from({ length: 8 }).map((_, index) => <div className="h-[88px] animate-pulse border-b border-border-subtle bg-surface-low/60" key={index} />) : null}
        {!loading && !items.length ? <div className="grid h-full min-h-48 place-items-center px-6 text-center"><div><div className="text-[11px] font-semibold text-text-secondary">当前没有已授权内容</div><p className="mt-2 text-[9px] leading-5 text-text-muted">该信息源尚未接入或当前无事件；不会抓取受限全文填充。</p></div></div> : null}
      </div>
      {(payload?.warnings || []).length ? <div className="shrink-0 border-t border-warn/25 bg-warn/5 px-2 py-1 text-[8px] text-warn" title={(payload?.warnings || []).join("；")}>数据源有降级说明</div> : null}
    </section>
  );
}

export default function InfoPage() {
  const [feeds, setFeeds] = useState<Record<string, InfoFeedPayload>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");

  const load = useCallback(async (bypassCache = false) => {
    setLoading(true);
    setError("");
    const results = await Promise.allSettled(CHANNELS.map(async (channel) => [
      channel.key,
      await getInfoFeed({ ...channel.query, page: 1, page_size: 50 }, { bypassCache })
    ] as const));
    const next: Record<string, InfoFeedPayload> = {};
    const failures: string[] = [];
    results.forEach((result, index) => {
      if (result.status === "fulfilled") next[result.value[0]] = result.value[1];
      else failures.push(CHANNELS[index].title);
    });
    if (Object.keys(next).length) {
      setFeeds((current) => ({ ...current, ...next }));
      setUpdatedAt(Object.values(next).find((item) => item.generated_at)?.generated_at || "");
    }
    setError(failures.length ? `${failures.join("、")}加载失败，其他列仍可使用` : "");
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { const timer = window.setInterval(() => void load(true), 60_000); return () => window.clearInterval(timer); }, [load]);

  const totals = useMemo(() => {
    const all = Object.values(feeds);
    return {
      events: all.reduce((sum, item) => sum + Number(item.pagination?.total || item.items?.length || 0), 0),
      important: all.reduce((sum, item) => sum + Number(item.summary?.high_importance || 0), 0),
      risk: all.reduce((sum, item) => sum + Number(item.summary?.risk || 0), 0),
      opportunity: all.reduce((sum, item) => sum + Number(item.summary?.opportunity || 0), 0)
    };
  }, [feeds]);

  return (
    <div aria-busy={loading} className="workstation-page flex min-h-0 flex-col gap-[10px] p-3" data-testid="info-workstation">
      <section className="workstation-panel flex h-11 shrink-0 items-center gap-5 overflow-x-auto px-3 workstation-scroll">
        <div className="flex items-center gap-2"><span className="h-2 w-2 rounded-full bg-primary-500" /><span className="text-[11px] font-semibold text-text-primary">信息蒸馏</span></div>
        {[["事件", totals.events], ["重要", totals.important], ["风险", totals.risk], ["机会", totals.opportunity]].map(([label, value]) => <div className="flex items-baseline gap-1" key={String(label)}><span className="text-[9px] text-text-muted">{label}</span><span className="font-mono text-[11px] font-semibold text-text-secondary">{value}</span></div>)}
        <div className="ml-auto flex items-center gap-3"><span className="text-[9px] text-text-muted">{error || (updatedAt ? `更新 ${clock(updatedAt)}` : "等待授权信息源")}</span><button className="h-7 rounded-sm border border-border-subtle bg-surface-low px-2.5 text-[9px] font-semibold text-text-secondary hover:text-text-primary" disabled={loading} onClick={() => void load(true)} type="button">{loading ? "同步中" : "刷新"}</button></div>
      </section>
      <main className="grid min-h-0 flex-1 grid-cols-1 gap-3 md:grid-cols-2 min-[1160px]:grid-cols-4" data-testid="info-four-columns">
        {CHANNELS.map((channel) => <InfoColumn loading={loading} payload={feeds[channel.key]} subtitle={channel.subtitle} title={channel.title} key={channel.key} />)}
      </main>
    </div>
  );
}
