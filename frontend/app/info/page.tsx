"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CoinIcon } from "@/components/CoinIcon";
import { getInfoFeed } from "@/lib/api";
import type { InfoFeedPayload, NewsEvent } from "@/lib/types";

const CHANNELS = [
  { key: "news", title: "聚合资讯", query: { source_type: "news", language: "zh" } },
  { key: "english", title: "英文流资讯", query: { source_type: "news", language: "en" } },
  { key: "kol", title: "KOL聚合资讯", query: { source_type: "kol" } },
  { key: "plaza", title: "市场广场情绪", query: { source_type: "plaza" } }
] as const;

type FeedMode = "news" | "english" | "kol";

function dateOf(value?: string) {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

function clock(value?: string) {
  const date = dateOf(value);
  return date?.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" }) || "--:--";
}

function hourLabel(value?: string) {
  const date = dateOf(value);
  if (!date) return "--:00";
  const hour = date.toLocaleTimeString("zh-CN", { hour: "2-digit", hour12: false, timeZone: "Asia/Shanghai" }).replace("时", "");
  return `${hour}:00`;
}

function relativeClock(value?: string) {
  const date = dateOf(value);
  if (!date) return "--:--";
  const age = Date.now() - date.getTime();
  const prefix = age < 86_400_000 ? "今" : age < 172_800_000 ? "昨" : date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit", timeZone: "Asia/Shanghai" });
  return `${prefix} ${clock(value)}`;
}

function isWithin(value: string | undefined, hours: number, referenceTime: number) {
  const date = dateOf(value);
  if (!date) return false;
  const age = referenceTime - date.getTime();
  return age >= 0 && age <= hours * 3_600_000;
}

function summaryText(item?: NewsEvent) {
  return item?.ai_analysis?.fact_summary || item?.summary || item?.title || "暂无新增关键信息";
}

function channelDigest(payload?: InfoFeedPayload) {
  const items = payload?.items || [];
  const high = items.find((item) => item.importance === "high") || items[0];
  return summaryText(high);
}

function LiveBadge({ payload }: { payload?: InfoFeedPayload }) {
  const ready = (payload?.items || []).length > 0;
  return <span className={`inline-flex items-center gap-1 rounded-full px-1.5 py-0.5 text-[7px] font-bold ${ready ? "bg-good/10 text-good" : "bg-warn/10 text-warn"}`}><span className={`h-1 w-1 rounded-full ${ready ? "animate-pulse bg-good" : "bg-warn"}`}/>{ready ? "LIVE" : "SYNC"}</span>;
}

function SymbolTags({ symbols }: { symbols?: string[] }) {
  if (!symbols?.length) return null;
  return <div className="mt-1 flex flex-wrap items-center gap-1">{symbols.slice(0, 4).map((symbol) => <span className="rounded-[2px] border border-primary-100 bg-primary-50/70 px-1 py-px font-mono text-[7px] font-semibold text-primary-700" key={symbol}>${symbol.replace(/USDT$/i, "")}</span>)}</div>;
}

function FeedItem({ item, mode }: { item: NewsEvent; mode: FeedMode }) {
  const engagement = item.ai_analysis?.engagement;
  const score = Number(engagement?.score || 0);
  const body = <article className="border-b border-border-subtle px-2 py-1.5 transition-colors hover:bg-primary-50/45">
    {mode === "kol" ? <div className="mb-0.5 flex items-center gap-1 text-[7px] text-text-muted"><span className="font-mono">{relativeClock(item.published_at)}</span><b className="min-w-0 flex-1 truncate text-warn">{item.source || "@KOL"}</b>{score >= 100 ? <span className="rounded-[2px] bg-warn/10 px-1 py-px font-semibold text-warn">🔥 {score}</span> : null}</div> : <div className="mb-0.5 flex items-center gap-1 text-[7px] text-text-muted"><span className={`min-w-0 flex-1 truncate ${mode === "english" ? "font-semibold text-primary-600" : ""}`}>{item.source || "公开来源"}</span>{item.importance === "high" ? <span className="rounded-[2px] bg-risk/10 px-1 py-px font-semibold text-risk">高影响</span> : null}</div>}
    <h3 className={`${mode === "kol" ? "line-clamp-5" : "line-clamp-3"} text-[9px] font-medium leading-[1.48] text-text-primary`}>{item.title || item.summary || "未命名资讯"}</h3>
    {mode === "english" && item.summary && item.summary !== item.title ? <p className="line-clamp-1 text-[7px] leading-[1.45] text-text-secondary">{item.summary}</p> : null}
    {mode !== "kol" ? <div className="flex items-end justify-between gap-1"><SymbolTags symbols={item.symbols}/></div> : null}
  </article>;
  return item.url ? <a className="block" href={item.url} rel="noreferrer" target="_blank">{body}</a> : body;
}

function GroupedNews({ items }: { items: NewsEvent[] }) {
  const groups = new Map<string, NewsEvent[]>();
  items.forEach((item) => {
    const key = hourLabel(item.published_at);
    groups.set(key, [...(groups.get(key) || []), item]);
  });
  return <>{[...groups.entries()].map(([hour, rows]) => <div key={hour}><div className="flex h-[18px] items-center justify-between border-b border-border-subtle bg-surface-low px-2 text-[7px] text-text-muted"><span className="font-mono font-semibold">{hour}</span><span>{rows.length} 条</span></div>{rows.map((item, index) => <div className="grid grid-cols-[34px_minmax(0,1fr)]" key={item.event_id || `${item.title}-${index}`}><span className="border-b border-border-subtle py-1.5 text-center font-mono text-[7px] text-text-muted">{clock(item.published_at)}</span><FeedItem item={item} mode="news"/></div>)}</div>)}</>;
}

function InfoColumn({ title, payload, loading, showDigest, mode }: { title: string; payload?: InfoFeedPayload; loading: boolean; showDigest: boolean; mode: FeedMode }) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const items = (payload?.items || []).filter((item) => !query || `${item.title || ""} ${item.summary || ""} ${(item.symbols || []).join(" ")} ${item.source || ""}`.toLowerCase().includes(query.toLowerCase()));
  const icon = mode === "news" ? "▤" : mode === "english" ? "▶" : "♟";
  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col">
    <div className={`${mode === "kol" ? "flex justify-between" : "grid grid-cols-[auto_minmax(72px,1fr)_auto]"} h-[40px] shrink-0 items-center gap-1.5 border-b border-border-subtle bg-surface-low px-2 min-[1024px]:h-[32px]`}><h2 className="flex whitespace-nowrap text-[10px] font-bold text-text-primary"><span className="mr-1 text-primary-600">{icon}</span>{title}</h2>{mode !== "kol" ? <div className="relative"><span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[8px] text-text-muted">⌕</span><input aria-label={`搜索${title}`} className="h-6 w-full rounded-[3px] border border-border-subtle bg-surface-panel pl-5 pr-1.5 text-[8px] text-text-primary outline-none placeholder:text-text-muted focus:border-primary-500" onChange={(event) => setQuery(event.target.value)} placeholder="搜索…" value={query}/></div> : null}<LiveBadge payload={payload}/></div>
    {showDigest ? <button aria-expanded={expanded} className={`${expanded ? "min-h-[72px]" : "h-[43px] min-[1024px]:h-[26px] min-[1280px]:h-[22px]"} grid shrink-0 grid-cols-[38px_minmax(0,1fr)_auto] items-start gap-1 border-b border-border-subtle bg-[#faf8ff] px-2 py-1 text-left`} onClick={() => setExpanded((value) => !value)} type="button"><span className="rounded-[2px] bg-primary-50 px-1 py-0.5 text-center text-[7px] font-semibold text-primary-600">AI 解读</span><span className={`${expanded ? "" : "line-clamp-2"} text-[8px] leading-[1.45] text-text-secondary`}>{channelDigest(payload)}</span><span className="text-[7px] font-semibold text-warn">{expanded ? "收起" : "展开⌄"}</span></button> : null}
    <div className="workstation-scroll min-h-0 flex-1 overflow-auto">{mode === "news" ? <GroupedNews items={items}/> : items.map((item, index) => <FeedItem item={item} key={item.event_id || `${item.title}-${index}`} mode={mode}/>)}{loading && !items.length ? Array.from({ length: 7 }).map((_, index) => <div className="h-[76px] animate-pulse border-b border-border-subtle bg-surface-low/65" key={index}/>) : null}{!loading && !items.length ? <div className="grid h-36 place-items-center px-6 text-center text-[9px] text-text-muted">{query ? "没有匹配的资讯" : "公开来源正在同步"}</div> : null}</div>
  </section>;
}

type PlazaRank = { symbol: string; count: number; positive: number; negative: number; engagement: number; summary: string };

function plazaRanks(items: NewsEvent[], hours: number): PlazaRank[] {
  const ranks = new Map<string, PlazaRank>();
  const referenceTime = Math.max(...items.map((item) => dateOf(item.published_at)?.getTime() || 0));
  items.filter((item) => isWithin(item.published_at, hours, referenceTime)).forEach((item) => (item.symbols || []).forEach((raw) => {
    const symbol = raw.replace(/USDT$/i, "").toUpperCase();
    if (!symbol) return;
    const current = ranks.get(symbol) || { symbol, count: 0, positive: 0, negative: 0, engagement: 0, summary: summaryText(item) };
    current.count += 1;
    current.engagement += Number(item.ai_analysis?.engagement?.score || 0);
    if (item.event_kind === "opportunity") current.positive += 1;
    if (item.event_kind === "risk") current.negative += 1;
    ranks.set(symbol, current);
  }));
  return [...ranks.values()].sort((a, b) => b.count - a.count || b.engagement - a.engagement).slice(0, 12);
}

function PlazaColumn({ payload, loading, showDigest }: { payload?: InfoFeedPayload; loading: boolean; showDigest: boolean }) {
  const items = payload?.items || [];
  const active = plazaRanks(items, 4);
  const total = plazaRanks(items, 24);
  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col"><div className="flex h-[40px] shrink-0 items-center justify-between border-b border-border-subtle bg-surface-low px-2 min-[1024px]:h-[32px]"><h2 className="flex items-center text-[10px] font-bold text-text-primary"><span className="mr-1 text-warn">▣</span>市场广场情绪</h2><LiveBadge payload={payload}/></div>{showDigest ? <div className="flex h-[43px] shrink-0 items-start gap-1 border-b border-border-subtle bg-[#fffaf0] px-2 py-1 min-[1024px]:h-[26px] min-[1280px]:h-[22px]"><span className="rounded-[2px] bg-warn/10 px-1 py-0.5 text-[7px] font-semibold text-warn">AI 解读</span><p className="line-clamp-2 flex-1 text-[8px] leading-[1.45] text-text-secondary">公开社交流按币种聚合，结合帖子方向与互动强度生成 4h 活力榜和 24h 总榜。</p></div> : null}<div className="workstation-scroll min-h-0 flex-1 overflow-auto"><div className="flex h-7 items-center justify-between border-b border-border-subtle bg-surface-low px-2"><h3 className="text-[9px] font-bold">▧ 4h 活力榜</h3><span className="text-[7px] text-text-muted">快速更新 · Top 8</span></div>{active.slice(0, 8).map((item, index) => <div className="grid h-[45px] grid-cols-[18px_18px_minmax(0,1fr)_auto] items-center gap-1 border-b border-border-subtle px-2 text-[8px]" key={item.symbol}><span className="font-mono text-warn">{String(index + 1).padStart(2, "0")}</span><CoinIcon coin={item.symbol}/><span className="font-semibold">${item.symbol}<small className="ml-1 font-normal text-text-muted">多 {item.positive} · 空 {item.negative}</small></span><span className="font-mono font-semibold text-good">{item.count} 帖</span></div>)}{!loading && !active.length ? <div className="grid h-20 place-items-center text-[8px] text-text-muted">公开社交源暂无 4h 币种样本</div> : null}<div className="flex h-7 items-center justify-between border-y border-border-subtle bg-surface-low px-2"><h3 className="text-[9px] font-bold">◉ 24h 总榜</h3><span className="text-[7px] text-text-muted">情绪分析</span></div>{total.slice(0, 8).map((item, index) => { const sentiment = item.positive >= item.negative ? "偏多" : "偏空"; const ratio = Math.round(100 * Math.max(item.positive, item.negative) / Math.max(1, item.positive + item.negative)); const positiveRatio = Math.round(100 * item.positive / Math.max(1, item.positive + item.negative)); const heat = Math.max(1, Math.round(item.engagement / Math.max(1, item.count))); return <article className="min-h-[102px] border-b border-border-subtle px-2 py-2 min-[1280px]:min-h-[86px] min-[1280px]:py-1" key={item.symbol}><div className="flex items-center gap-1.5"><span className="font-mono text-[8px] text-warn">{String(index + 1).padStart(2, "0")}</span><CoinIcon coin={item.symbol}/><b className="text-[9px]">${item.symbol}</b><span className={`rounded-[2px] px-1 text-[7px] ${sentiment === "偏多" ? "bg-good/10 text-good" : "bg-risk/10 text-risk"}`}>{sentiment} {ratio}%</span><span className="ml-auto text-[7px] text-text-muted">{item.count} 帖</span></div><div className="mt-1 flex flex-wrap gap-1 text-[6px]"><span className="rounded-[2px] bg-good/5 px-1 text-good">广场 多 {positiveRatio}%</span><span className="rounded-[2px] bg-risk/5 px-1 text-risk">空 {100 - positiveRatio}%</span><span className="rounded-[2px] bg-primary-50 px-1 text-primary-700">互动 {heat}</span></div><p className="mt-1 line-clamp-2 text-[8px] leading-[1.4] text-text-secondary">{item.summary}</p></article>; })}</div></section>;
}

export default function InfoPage() {
  const [feeds, setFeeds] = useState<Record<string, InfoFeedPayload>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");
  const [disclaimer, setDisclaimer] = useState(false);
  const [showDigest, setShowDigest] = useState(true);

  const load = useCallback(async (bypassCache = false) => {
    setLoading(true); setError("");
    const results = await Promise.allSettled(CHANNELS.map(async (channel) => [channel.key, await getInfoFeed({ ...channel.query, page: 1, page_size: 80, window_sec: 2_592_000 }, { bypassCache })] as const));
    const next: Record<string, InfoFeedPayload> = {};
    const failures: string[] = [];
    results.forEach((result, index) => { if (result.status === "fulfilled") next[result.value[0]] = result.value[1]; else failures.push(CHANNELS[index].title); });
    if (Object.keys(next).length) { setFeeds((current) => ({ ...current, ...next })); setUpdatedAt(Object.values(next).find((item) => item.generated_at)?.generated_at || ""); }
    setError(failures.length ? `${failures.join("、")}加载失败，其余栏目仍可用` : "");
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { const timer = window.setInterval(() => void load(true), 60_000); return () => window.clearInterval(timer); }, [load]);

  const combined = useMemo(() => Object.values(feeds).flatMap((feed) => feed.items || []), [feeds]);
  const high = combined.filter((item) => item.importance === "high").length;

  return <div aria-busy={loading} className="workstation-page mercu-info-grid" data-testid="info-workstation">
    <section className="workstation-panel flex h-[44px] shrink-0 items-center gap-2.5 px-3 min-[1024px]:mx-1 min-[1024px]:h-[32px]"><div className="flex h-7 w-7 items-center justify-center rounded-[4px] bg-primary-50 text-primary-600">✦</div><div><div className="flex items-center gap-2"><h1 className="text-[12px] font-bold text-text-primary">AI 信息蒸馏</h1><span className="rounded-[3px] bg-warn/10 px-1.5 py-0.5 text-[7px] font-semibold text-warn">引擎 v2.6</span></div><p className="text-[7px] text-text-muted">全量聚合公开信息 · 实时增量更新 · <button className="underline decoration-dotted" onClick={() => setDisclaimer(true)} type="button">免责声明</button></p></div><div className="ml-auto flex items-center gap-2"><span className="hidden text-[8px] text-text-muted lg:inline">高影响 {high} · 更新 {clock(updatedAt)}</span><button aria-pressed={showDigest} className="h-7 rounded-full border border-warn/20 bg-warn/10 px-3 text-[8px] font-semibold text-warn min-[1024px]:h-6" disabled={loading} onClick={() => { setShowDigest((value) => !value); void load(true); }} type="button">{loading ? "分析中…" : "✦ 4h AI 综合分析 ·⌄"}</button></div></section>
    {error ? <div className="border border-risk/20 bg-risk/5 px-3 py-1 text-[8px] text-risk">{error}</div> : null}
    <main className="grid min-h-0 grid-cols-4 gap-2.5" data-testid="info-four-columns"><InfoColumn loading={loading} mode="news" payload={feeds.news} showDigest={showDigest} title="聚合资讯"/><InfoColumn loading={loading} mode="english" payload={feeds.english} showDigest={showDigest} title="英文流资讯"/><InfoColumn loading={loading} mode="kol" payload={feeds.kol} showDigest={showDigest} title="KOL聚合资讯"/><PlazaColumn loading={loading} payload={feeds.plaza} showDigest={showDigest}/></main>
    {disclaimer ? <div className="fixed inset-0 z-50 grid place-items-center bg-black/25 p-4" role="dialog"><div className="w-full max-w-lg rounded-lg border border-border-subtle bg-surface-panel p-5 shadow-xl"><div className="flex items-center justify-between"><h2 className="text-sm font-bold text-text-primary">免责声明</h2><button aria-label="关闭" className="text-lg text-text-muted" onClick={() => setDisclaimer(false)} type="button">×</button></div><div className="mt-4 space-y-3 text-[11px] leading-6 text-text-secondary"><p>本页面聚合官方公告、公开 RSS 与公开社交 API 的必要元数据，版权归原作者及发布平台所有，每条信息保留原始来源链接。</p><p>重要度、币种关联和情绪方向由规则引擎生成，用于信息筛选，不构成投资、交易、财务或法律建议。</p></div></div></div> : null}
  </div>;
}
