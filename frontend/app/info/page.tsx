"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { getInfoFeed } from "@/lib/api";
import type { InfoFeedPayload, NewsEvent } from "@/lib/types";

const CHANNELS = [
  { key: "news", title: "聚合资讯", source: "中文源", query: {} },
  { key: "english", title: "英文流资讯", source: "English Sources", query: { language: "en" } },
  { key: "kol", title: "KOL聚合资讯", source: "精选 KOL", query: { source_type: "kol" } },
  { key: "plaza", title: "币安广场情绪", source: "公开广场", query: { source_type: "plaza" } }
] as const;

function clock(value?: string) {
  if (!value) return "--:--";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "--:--";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "Asia/Shanghai" });
}

function isWithin(value: string | undefined, hours: number) {
  if (!value) return false;
  const timestamp = new Date(value).getTime();
  return Number.isFinite(timestamp) && Date.now() - timestamp <= hours * 3_600_000;
}

function summaryText(item?: NewsEvent) {
  return item?.ai_analysis?.fact_summary || item?.summary || item?.title || "暂无新增关键信息";
}

function channelDigest(payload?: InfoFeedPayload) {
  const items = payload?.items || [];
  const select = (hours: number) => items.find((item) => isWithin(item.published_at, hours)) || items[0];
  const high = items.find((item) => item.importance === "high") || items[0];
  return {
    total: Number(payload?.pagination?.total || items.length),
    summary: summaryText(high),
    eight: summaryText(select(8)),
    four: summaryText(select(4)),
    one: summaryText(select(1))
  };
}

function CoinIcon({ coin, size = 17 }: { coin?: string; size?: number }) {
  const label = String(coin || "?").replace("USDT", "").slice(0, 2).toUpperCase();
  const hue = [...label].reduce((sum, char) => sum + char.charCodeAt(0), 0) % 360;
  return <span aria-hidden="true" className="grid shrink-0 place-items-center rounded-full text-[6px] font-bold text-white" style={{ width: size, height: size, background: `linear-gradient(145deg,hsl(${hue} 72% 58%),hsl(${(hue + 32) % 360} 68% 43%))` }}>{label}</span>;
}

function FeedItem({ item, english = false }: { item: NewsEvent; english?: boolean }) {
  const risk = item.event_kind === "risk";
  const opportunity = item.event_kind === "opportunity";
  const content = <article className={`border-b border-border-subtle px-2.5 py-2 transition-colors hover:bg-primary-50/45 ${risk ? "border-l-2 border-l-risk" : opportunity ? "border-l-2 border-l-good" : "border-l-2 border-l-transparent"}`}>
    <div className="flex items-center gap-1.5 text-[8px] text-text-muted"><span className="font-mono tabular-nums">{clock(item.published_at)}</span><span className="min-w-0 flex-1 truncate">{item.source || "未知来源"}</span>{item.importance === "high" ? <span className="rounded-[3px] bg-risk/10 px-1 py-px font-semibold text-risk">高影响</span> : null}</div>
    <h3 className={`mt-1 text-[10px] font-semibold leading-[1.45] text-text-primary ${english ? "line-clamp-3" : "line-clamp-2"}`}>{item.title || item.summary || "未命名资讯"}</h3>
    {item.summary && item.summary !== item.title ? <p className="mt-1 line-clamp-2 text-[9px] leading-[1.5] text-text-secondary">{item.summary}</p> : null}
    <div className="mt-1.5 flex items-center gap-1">{(item.symbols || []).slice(0, 4).map((symbol) => <span className="rounded-[3px] bg-primary-50 px-1 py-px font-mono text-[7px] font-semibold text-primary-700" key={symbol}>${symbol.replace("USDT", "")}</span>)}<span className="ml-auto text-[7px] text-text-muted">{item.source_type || item.language || "公开源"}</span></div>
    {item.ai_analysis?.fact_summary ? <details className="mt-1.5 rounded-[3px] bg-surface-low px-2 py-1"><summary className="cursor-pointer text-[8px] font-semibold text-primary-600">✦ AI 解读</summary><p className="mt-1 text-[8px] leading-[1.5] text-text-secondary">{item.ai_analysis.fact_summary}</p>{item.ai_analysis.possible_impact ? <p className="mt-1 text-[8px] leading-[1.5] text-text-muted">可能影响：{item.ai_analysis.possible_impact}</p> : null}</details> : null}
  </article>;
  return item.url ? <a href={item.url} rel="noreferrer" target="_blank">{content}</a> : content;
}

function InfoColumn({ title, payload, loading, showDigest, english = false, kol = false }: { title: string; payload?: InfoFeedPayload; loading: boolean; showDigest: boolean; english?: boolean; kol?: boolean }) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const items = (payload?.items || []).filter((item) => !query || `${item.title || ""} ${item.summary || ""} ${(item.symbols || []).join(" ")}`.toLowerCase().includes(query.toLowerCase()));
  const digest = channelDigest(payload);
  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col">
    <div className="grid h-[40px] shrink-0 grid-cols-[auto_minmax(70px,1fr)_auto] items-center gap-1.5 border-b border-border-subtle bg-surface-low px-2"><h2 className="whitespace-nowrap text-[10px] font-bold text-text-primary">{title}</h2><div className="relative"><span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[8px] text-text-muted">⌕</span><input aria-label={`搜索${title}`} className="h-6 w-full rounded-[3px] border border-border-subtle bg-surface-panel pl-5 pr-1.5 text-[8px] text-text-primary outline-none placeholder:text-text-muted focus:border-primary-500" onChange={(event) => setQuery(event.target.value)} placeholder="搜索…" value={query}/></div><span className="inline-flex items-center gap-1 rounded-full bg-good/10 px-1.5 py-0.5 text-[7px] font-semibold text-good"><span className="h-1 w-1 animate-pulse rounded-full bg-good"/>LIVE</span></div>
    {showDigest ? <button aria-expanded={expanded} className={`${expanded ? "min-h-[72px]" : "h-[43px]"} grid shrink-0 grid-cols-[38px_1fr_auto] items-start gap-1 border-b border-border-subtle bg-[#faf8ff] px-2 py-1.5 text-left`} onClick={() => setExpanded((value) => !value)} type="button"><span className="rounded-[2px] bg-primary-50 px-1 py-0.5 text-center text-[7px] font-semibold text-primary-600">AI 解读</span><span className={`${expanded ? "" : "line-clamp-2"} text-[8px] leading-[1.45] text-text-secondary`}>{digest.summary}</span><span className="text-[7px] font-semibold text-primary-600">{expanded ? "收起" : "展开"}</span></button> : null}
    <div className="workstation-scroll min-h-0 flex-1 overflow-auto">{items.map((item, index) => <FeedItem english={english} item={kol ? { ...item, source: item.source || "KOL" } : item} key={item.event_id || `${item.title}-${index}`}/>)}{loading && !items.length ? Array.from({ length: 7 }).map((_, index) => <div className="h-[76px] animate-pulse border-b border-border-subtle bg-surface-low/65" key={index}/>) : null}{!loading && !items.length ? <div className="grid h-36 place-items-center text-[9px] text-text-muted">{query ? "没有匹配的资讯" : "暂无公开来源资讯"}</div> : null}</div>
  </section>;
}

type PlazaRank = { symbol: string; count: number; positive: number; negative: number; summary: string };

function plazaRanks(items: NewsEvent[], hours: number): PlazaRank[] {
  const ranks = new Map<string, PlazaRank>();
  items.filter((item) => isWithin(item.published_at, hours)).forEach((item) => (item.symbols || []).forEach((raw) => {
    const symbol = raw.replace(/USDT$/i, "").toUpperCase();
    if (!symbol) return;
    const current = ranks.get(symbol) || { symbol, count: 0, positive: 0, negative: 0, summary: summaryText(item) };
    current.count += 1;
    if (item.event_kind === "opportunity") current.positive += 1;
    if (item.event_kind === "risk") current.negative += 1;
    if (!current.summary) current.summary = summaryText(item);
    ranks.set(symbol, current);
  }));
  return [...ranks.values()].sort((a, b) => b.count - a.count || b.positive - a.positive).slice(0, 12);
}

function PlazaColumn({ payload, loading, showDigest }: { payload?: InfoFeedPayload; loading: boolean; showDigest: boolean }) {
  const items = payload?.items || [];
  const active = plazaRanks(items, 4);
  const total = plazaRanks(items, 24);
  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col"><div className="flex h-[40px] shrink-0 items-center justify-between border-b border-border-subtle bg-surface-low px-2"><h2 className="text-[10px] font-bold text-text-primary">币安广场情绪</h2><span className="inline-flex items-center gap-1 rounded-full bg-good/10 px-1.5 py-0.5 text-[7px] font-semibold text-good"><span className="h-1 w-1 animate-pulse rounded-full bg-good"/>LIVE</span></div>{showDigest ? <div className="flex h-[43px] shrink-0 items-start gap-1 border-b border-border-subtle bg-[#fffaf0] px-2 py-1.5"><span className="rounded-[2px] bg-warn/10 px-1 py-0.5 text-[7px] font-semibold text-warn">AI 解读</span><p className="line-clamp-2 flex-1 text-[8px] leading-[1.45] text-text-secondary">散户情绪分化，多空高情绪币种实时归并；仅统计公开且可验证的广场信息。</p></div> : null}<div className="workstation-scroll min-h-0 flex-1 overflow-auto"><div className="flex h-7 items-center justify-between border-b border-border-subtle bg-surface-low px-2"><h3 className="text-[9px] font-bold">4h 活力榜</h3><span className="text-[7px] text-text-muted">Top 8</span></div>{active.slice(0, 8).map((item, index) => <div className="grid h-[34px] grid-cols-[18px_18px_minmax(0,1fr)_auto] items-center gap-1 border-b border-border-subtle px-2 text-[8px]" key={item.symbol}><span className="font-mono text-warn">{String(index + 1).padStart(2, "0")}</span><CoinIcon coin={item.symbol}/><span className="font-semibold">{item.symbol}<small className="ml-1 font-normal text-text-muted">{item.positive}多 · {item.negative}空</small></span><span className="font-mono font-semibold text-good">{item.count} 条</span></div>)}{!loading && !active.length ? <div className="grid h-20 place-items-center text-[8px] text-text-muted">真实广场源暂无 4h 样本</div> : null}<div className="flex h-7 items-center justify-between border-y border-border-subtle bg-surface-low px-2"><h3 className="text-[9px] font-bold">24h 总榜</h3><span className="text-[7px] text-text-muted">情绪分析</span></div>{total.slice(0, 8).map((item, index) => { const sentiment = item.positive >= item.negative ? "偏多" : "偏空"; return <article className="border-b border-border-subtle px-2 py-2" key={item.symbol}><div className="flex items-center gap-1.5"><span className="font-mono text-[8px] text-warn">{String(index + 1).padStart(2, "0")}</span><CoinIcon coin={item.symbol}/><b className="text-[9px]">${item.symbol}</b><span className={`rounded-[2px] px-1 text-[7px] ${sentiment === "偏多" ? "bg-good/10 text-good" : "bg-risk/10 text-risk"}`}>{sentiment}</span><span className="ml-auto text-[7px] text-text-muted">{item.count} 条</span></div><p className="mt-1 line-clamp-2 text-[8px] leading-[1.45] text-text-secondary">{item.summary}</p></article>; })}</div></section>;
}

export default function InfoPage() {
  const [feeds, setFeeds] = useState<Record<string, InfoFeedPayload>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");
  const [disclaimer, setDisclaimer] = useState(false);
  const [showDigest, setShowDigest] = useState(false);

  const load = useCallback(async (bypassCache = false) => {
    setLoading(true); setError("");
    const results = await Promise.allSettled(CHANNELS.map(async (channel) => [channel.key, await getInfoFeed({ ...channel.query, page: 1, page_size: 80 }, { bypassCache })] as const));
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
    <section className="workstation-panel flex h-[38px] shrink-0 items-center gap-2.5 px-3"><div className="flex h-7 w-7 items-center justify-center rounded-[4px] bg-primary-50 text-primary-600">✦</div><div><div className="flex items-center gap-2"><h1 className="text-[12px] font-bold text-text-primary">AI 信息蒸馏</h1><span className="rounded-[3px] bg-warn/10 px-1.5 py-0.5 text-[7px] font-semibold text-warn">引擎 v2.4</span></div><p className="text-[7px] text-text-muted">全量聚合全网信息 · 实时增量更新</p></div><div className="ml-auto flex items-center gap-2"><span className="hidden text-[8px] text-text-muted lg:inline">高影响 {high} · 更新 {clock(updatedAt)}</span><button className="h-6 rounded-[3px] border border-border-subtle px-2 text-[8px] text-text-secondary" onClick={() => setDisclaimer(true)} type="button">免责声明</button><button aria-pressed={showDigest} className="h-6 rounded-full border border-warn/20 bg-warn/10 px-3 text-[8px] font-semibold text-warn" disabled={loading} onClick={() => { setShowDigest((value) => !value); void load(true); }} type="button">{loading ? "分析中…" : "4h AI 综合分析 ···"}</button></div></section>
    {error ? <div className="border border-risk/20 bg-risk/5 px-3 py-1 text-[8px] text-risk">{error}</div> : null}
    <main className="grid min-h-0 grid-cols-4 gap-1.5" data-testid="info-four-columns"><InfoColumn loading={loading} payload={feeds.news} showDigest={showDigest} title="聚合资讯"/><InfoColumn english loading={loading} payload={feeds.english} showDigest={showDigest} title="英文流资讯"/><InfoColumn kol loading={loading} payload={feeds.kol} showDigest={showDigest} title="KOL聚合资讯"/><PlazaColumn loading={loading} payload={feeds.plaza} showDigest={showDigest}/></main>
    {disclaimer ? <div className="fixed inset-0 z-50 grid place-items-center bg-black/25 p-4" role="dialog"><div className="w-full max-w-lg rounded-lg border border-border-subtle bg-surface-panel p-5 shadow-xl"><div className="flex items-center justify-between"><h2 className="text-sm font-bold text-text-primary">免责声明</h2><button aria-label="关闭" className="text-lg text-text-muted" onClick={() => setDisclaimer(false)} type="button">×</button></div><div className="mt-4 space-y-3 text-[11px] leading-6 text-text-secondary"><p>本页面只聚合公开渠道信息，版权归原作者及发布平台所有，每条资讯保留原始来源链接。</p><p>AI 解读与规则摘要用于压缩信息和标注事实边界，不构成投资、交易、财务或法律建议。</p><p>如来源授权状态不可确认，系统仅展示标题、短摘要与官方链接，不抓取受限全文。</p></div></div></div> : null}
  </div>;
}
