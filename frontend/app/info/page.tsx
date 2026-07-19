"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CoinIcon } from "@/components/CoinIcon";
import { getInfoFeed } from "@/lib/api";
import type { InfoFeedPayload, InfoPlazaRank, NewsEvent } from "@/lib/types";

const CHANNELS = [
  { key: "news", title: "聚合资讯", query: { source_type: "news", language: "zh" } },
  { key: "english", title: "英文流资讯", query: { source_type: "news", language: "en" } },
  { key: "kol", title: "KOL聚合资讯", query: { source_type: "kol" } },
  { key: "plaza", title: "市场广场情绪", query: { source_type: "plaza" } }
] as const;

type FeedMode = "news" | "english" | "kol";

function ChannelIcon({ mode }: { mode: FeedMode | "plaza" }) {
  const testId = `info-channel-icon-${mode}`;
  if (mode === "english") return <span aria-hidden="true" className="grid h-[15px] w-[15px] shrink-0 place-items-center rounded-[2px] bg-[#f1edfb] text-[#7664bc]" data-testid={testId}><svg className="h-[9px] w-[9px]" viewBox="0 0 12 12"><path d="M3 1.8 10 6 3 10.2Z" fill="currentColor"/></svg></span>;
  if (mode === "kol") return <span aria-hidden="true" className="grid h-[15px] w-[15px] shrink-0 place-items-center rounded-[2px] bg-[#f4f1e9] text-[#34302a]" data-testid={testId}><svg className="h-[10px] w-[10px]" fill="none" viewBox="0 0 14 14"><circle cx="5" cy="4.2" r="2" fill="currentColor"/><circle cx="9.5" cy="5" r="1.6" fill="currentColor" opacity=".72"/><path d="M1.8 11c.2-2.3 1.5-3.5 3.3-3.5S8.2 8.7 8.4 11H1.8Zm6.3 0c-.1-1.1-.5-2-1.1-2.7.6-.5 1.3-.7 2.1-.7 1.7 0 2.8 1.1 3 3.4h-4Z" fill="currentColor"/></svg></span>;
  if (mode === "plaza") return <span aria-hidden="true" className="grid h-[15px] w-[15px] shrink-0 place-items-center rounded-[2px] bg-[#fbf5df] text-[#c89325]" data-testid={testId}><svg className="h-[9px] w-[9px]" fill="none" viewBox="0 0 12 12"><path d="M2 2h8v8H2z" stroke="currentColor"/><path d="M4 4h4v4H4z" fill="currentColor" opacity=".48"/></svg></span>;
  return <span aria-hidden="true" className="grid h-[15px] w-[15px] shrink-0 place-items-center rounded-[2px] bg-[#eaf7f6] text-[#4b9c97]" data-testid={testId}><svg className="h-[9px] w-[9px]" fill="none" viewBox="0 0 12 12"><path d="M3 1.5h6v9H3z" stroke="currentColor"/><path d="M4.5 4h3M4.5 6h3M4.5 8h2" stroke="currentColor" strokeLinecap="round"/></svg></span>;
}

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
  return <div className="mt-1 flex flex-wrap items-center gap-1">{symbols.slice(0, 4).map((symbol) => <span className="rounded-full border border-[#dfe2e6] bg-[#eef0f3] px-1.5 py-px font-mono text-[7px] font-semibold text-text-secondary" key={symbol}>$ {symbol.replace(/USDT$/i, "")}</span>)}</div>;
}

function FeedItem({ item, mode }: { item: NewsEvent; mode: FeedMode }) {
  const title = item.title || item.summary || "未命名资讯";
  const content = mode === "news" ? <article className="border-b border-border-subtle px-2 py-2 transition-colors hover:bg-primary-50/45 min-[1024px]:px-2.5" data-info-row="news">
    <div className="flex items-start gap-1"><h3 className="line-clamp-3 min-w-0 flex-1 text-[10px] font-medium leading-[1.55] text-text-primary"><span className="mr-1 text-[7px] font-normal text-text-muted">{item.source || "公开来源"}</span>{title}</h3>{item.importance === "high" ? <span className="shrink-0 rounded-[2px] bg-risk/10 px-1 py-px text-[7px] font-semibold text-risk">高影响</span> : null}</div>
    <SymbolTags symbols={item.symbols}/>
  </article> : <article className="min-w-0 border-border-subtle px-2 py-1.5 transition-colors hover:bg-primary-50/45 min-[1024px]:px-2.5">
    <div className="mb-0.5 flex items-center gap-1 text-[7px] text-text-muted"><span className={`min-w-0 flex-1 truncate ${mode === "english" ? "font-semibold text-primary-600" : "font-semibold text-warn"}`}>{item.source || (mode === "kol" ? "@KOL" : "公开来源")}</span>{mode !== "kol" && item.importance === "high" ? <span className="rounded-[2px] bg-risk/10 px-1 py-px font-semibold text-risk">高影响</span> : null}</div>
    <h3 className={`${mode === "kol" ? "line-clamp-6" : "line-clamp-3"} text-[9px] font-medium leading-[1.5] text-text-primary`}>{title}</h3>
  </article>;
  const body = mode === "news" ? content : <div className={`grid grid-cols-[42px_minmax(0,1fr)] border-b border-border-subtle ${mode === "kol" ? "min-h-[40px]" : "min-h-[55px]"}`} data-info-row={mode}><span className="border-border-subtle py-1.5 text-center font-mono text-[7px] text-text-muted">{mode === "kol" ? relativeClock(item.published_at) : clock(item.published_at)}</span>{content}</div>;
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
  const digestSurface = mode === "news" ? "bg-[#f3f9f9]" : mode === "english" ? "bg-[#f7f6fc]" : "bg-[#fcf7f1]";
  const digestBadge = mode === "news" ? "bg-[#e9f5f4] text-[#4a9994]" : mode === "english" ? "bg-[#efecfa] text-[#7565b4]" : "bg-warn/10 text-warn";
  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col">
    <div className="flex h-[40px] shrink-0 items-center gap-1.5 border-b border-border-subtle bg-surface-low px-2 min-[1024px]:h-[31px]"><h2 className="flex items-center gap-1 whitespace-nowrap text-[10px] font-bold text-text-primary"><ChannelIcon mode={mode}/>{title}</h2>{mode !== "kol" ? <div className="relative ml-auto w-[92px] shrink-0"><span className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-[8px] text-text-muted">⌕</span><input aria-label={`搜索${title}`} className="h-[18px] w-full rounded-[3px] border border-border-subtle bg-surface-panel pl-5 pr-1.5 text-[8px] text-text-primary outline-none placeholder:text-text-muted focus:border-primary-500" onChange={(event) => setQuery(event.target.value)} placeholder="搜索…" value={query}/></div> : <span className="ml-auto"/>}<LiveBadge payload={payload}/></div>
    {showDigest ? <button aria-expanded={expanded} className={`${expanded ? "min-h-[72px]" : "h-[43px] min-[1024px]:h-6"} ${digestSurface} grid shrink-0 grid-cols-[38px_minmax(0,1fr)_auto] items-start gap-1 border-b border-border-subtle px-2 py-1 text-left`} onClick={() => setExpanded((value) => !value)} type="button"><span className={`${digestBadge} rounded-[2px] px-1 py-0.5 text-center text-[7px] font-semibold`}>AI 解读</span><span className={`${expanded ? "" : "line-clamp-2"} text-[8px] leading-[1.45] text-text-secondary`}>{channelDigest(payload)}</span><span className="text-[7px] font-semibold text-warn">{expanded ? "收起" : "展开⌄"}</span></button> : null}
    <div className="workstation-scroll min-h-0 flex-1 overflow-auto">{mode === "news" ? <GroupedNews items={items}/> : items.map((item, index) => <FeedItem item={item} key={item.event_id || `${item.title}-${index}`} mode={mode}/>)}{loading && !items.length ? Array.from({ length: 7 }).map((_, index) => <div className="h-[76px] animate-pulse border-b border-border-subtle bg-surface-low/65" key={index}/>) : null}{!loading && !items.length ? <div className="grid h-36 place-items-center px-6 text-center text-[9px] text-text-muted">{query ? "没有匹配的资讯" : "公开来源正在同步"}</div> : null}</div>
  </section>;
}

function plazaRanks(items: NewsEvent[], hours: number): InfoPlazaRank[] {
  const ranks = new Map<string, Required<Pick<InfoPlazaRank, "symbol" | "coin" | "posts" | "recent_1h_posts" | "previous_1h_posts" | "positive" | "negative" | "neutral" | "engagement" | "summary">>>();
  const referenceTime = Math.max(...items.map((item) => dateOf(item.published_at)?.getTime() || 0));
  items.filter((item) => isWithin(item.published_at, hours, referenceTime)).forEach((item) => (item.symbols || []).forEach((raw) => {
    const symbol = raw.replace(/USDT$/i, "").toUpperCase();
    if (!symbol) return;
    const current = ranks.get(symbol) || { symbol: `${symbol}USDT`, coin: symbol, posts: 0, recent_1h_posts: 0, previous_1h_posts: 0, positive: 0, negative: 0, neutral: 0, engagement: 0, summary: summaryText(item) };
    current.posts += 1;
    const observedAt = dateOf(item.published_at)?.getTime() || 0;
    const age = referenceTime - observedAt;
    if (age >= 0 && age <= 3_600_000) current.recent_1h_posts += 1;
    else if (age > 3_600_000 && age <= 7_200_000) current.previous_1h_posts += 1;
    current.engagement += Number(item.ai_analysis?.engagement?.score || 0);
    if (item.event_kind === "opportunity") current.positive += 1;
    else if (item.event_kind === "risk") current.negative += 1;
    else current.neutral += 1;
    ranks.set(symbol, current);
  }));
  return [...ranks.values()].map((item) => {
    const directional = item.positive + item.negative;
    const positivePct = directional ? Math.round(item.positive / directional * 100) : 50;
    const isNew = item.recent_1h_posts > 0 && item.previous_1h_posts === 0;
    return {
      ...item,
      recent_ratio: item.previous_1h_posts ? Math.round(item.recent_1h_posts / item.previous_1h_posts * 10) / 10 : null,
      is_new: isNew,
      positive_pct: positivePct,
      negative_pct: 100 - positivePct,
      sentiment: directional === 0 || Math.abs(positivePct - 50) < 10 ? "neutral" : positivePct > 50 ? "bullish" : "bearish",
      sentiment_confidence_pct: directional ? Math.max(positivePct, 100 - positivePct) : 0,
      engagement_per_post: Math.round(item.engagement / Math.max(1, item.posts)),
    };
  }).filter((item) => hours > 4 || Number(item.recent_1h_posts || 0) > 0).sort((a, b) => hours <= 4
    ? Number(Boolean(b.is_new)) - Number(Boolean(a.is_new)) || Number(b.recent_ratio || 0) - Number(a.recent_ratio || 0) || Number(b.recent_1h_posts || 0) - Number(a.recent_1h_posts || 0) || Number(b.posts || 0) - Number(a.posts || 0)
    : Number(b.posts || 0) - Number(a.posts || 0) || Number(b.engagement || 0) - Number(a.engagement || 0)).slice(0, 12);
}

function rankCoin(item: InfoPlazaRank) {
  return String(item.coin || item.symbol || "--").replace(/USDT$/i, "").toUpperCase();
}

function priceChange(value?: number | null) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  const amount = Number(value);
  return `${amount >= 0 ? "+" : ""}${amount.toFixed(2)}%`;
}

function countText(value?: number) {
  return new Intl.NumberFormat("zh-CN", { maximumFractionDigits: 0 }).format(Number(value || 0));
}

function finite(value: unknown) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function sentimentLabel(item: InfoPlazaRank) {
  if (item.sentiment === "neutral") return "分歧";
  const bullish = item.sentiment === "bullish" || Number(item.positive_pct || 0) > Number(item.negative_pct || 0);
  const confidence = Number(item.sentiment_confidence_pct || Math.max(Number(item.positive_pct || 0), Number(item.negative_pct || 0)));
  return `${confidence >= 60 ? "强" : "偏"}${bullish ? "多" : "空"}`;
}

function PlazaColumn({ payload, loading, showDigest }: { payload?: InfoFeedPayload; loading: boolean; showDigest: boolean }) {
  const items = payload?.items || [];
  const active = payload?.plaza_rankings?.active_4h || plazaRanks(items, 4);
  const total = payload?.plaza_rankings?.total_24h || plazaRanks(items, 24);
  const providerLabel = payload?.plaza_rankings?.provider?.label || "市场广场";
  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col">
    <div className="flex h-[40px] shrink-0 items-center justify-between border-b border-border-subtle bg-surface-low px-2 min-[1024px]:h-[31px]">
      <h2 className="flex items-center gap-1 text-[10px] font-bold text-text-primary"><ChannelIcon mode="plaza"/>{providerLabel}情绪</h2>
      <LiveBadge payload={payload}/>
    </div>
    {showDigest ? <div className="flex h-[43px] shrink-0 items-start gap-1 border-b border-border-subtle bg-[#fbf8f1] px-2 py-1 min-[1024px]:h-6">
      <span className="rounded-[2px] bg-warn/10 px-1 py-0.5 text-[7px] font-semibold text-warn">AI 解读</span>
      <p className="line-clamp-2 flex-1 text-[8px] leading-[1.45] text-text-secondary">多币种热度与方向背离，警惕反向收割。</p>
      <span className="text-[7px] font-semibold text-warn">展开⌄</span>
    </div> : null}
    <div className="workstation-scroll min-h-0 flex-1 overflow-auto">
      <div className="flex h-6 items-center justify-between border-b border-border-subtle bg-surface-low px-2">
        <h3 className="text-[9px] font-bold">▧ 4h 活力榜</h3>
        <span className="text-[7px] text-text-muted">快速更新 · Top {Math.min(8, active.length)}</span>
      </div>
      {active.slice(0, 8).map((item, index) => {
        const coin = rankCoin(item);
        const ratio = finite(item.recent_ratio);
        const badge = item.is_new ? "NEW" : ratio === null ? "—" : `↑${ratio.toFixed(1)}×`;
        return <div className="grid h-[34px] grid-cols-[18px_18px_minmax(0,1fr)_auto] items-center gap-[7px] border-b border-border-subtle px-2.5 py-1 text-[8px]" key={item.symbol || coin}>
          <span className="font-mono text-warn">{String(index + 1).padStart(2, "0")}</span>
          <CoinIcon coin={coin}/>
          <span className="min-w-0 font-semibold">${coin} {item.asset_type ? <small className="rounded-[2px] bg-warn/10 px-1 text-warn">{item.asset_type}</small> : null} <small className={`${item.is_new ? "bg-warn/10 text-warn" : "text-warn"} rounded-[2px] font-mono font-semibold`}>{badge}</small><small className="block truncate font-normal leading-[1.45] text-text-muted">近 1h 提到 {countText(item.recent_1h_posts)} 次 · 上轮 {countText(item.previous_1h_posts)}</small></span>
          <span className="flex flex-col items-end whitespace-nowrap font-mono"><small className="text-[6px] font-normal text-text-muted">4h 共</small><b className="text-[9px] text-good">{countText(item.posts)} 帖</b></span>
        </div>;
      })}
      {!loading && !active.length ? <div className="grid h-20 place-items-center text-[8px] text-text-muted">广场数据源暂无 4h 币种样本</div> : null}
      <div className="flex h-6 items-center justify-between border-y border-border-subtle bg-surface-low px-2">
        <h3 className="text-[9px] font-bold">◉ 24h 总榜</h3>
        <span className="text-[7px] text-text-muted">情绪分析</span>
      </div>
      {total.slice(0, 8).map((item, index) => {
        const coin = rankCoin(item);
        const bullish = item.sentiment === "bullish" || Number(item.positive_pct || 0) > Number(item.negative_pct || 0);
        const neutral = item.sentiment === "neutral";
        const sentiment = sentimentLabel(item);
        const flow = finite(item.futures_flow_usd);
        const flowStrength = finite(item.futures_flow_strength);
        const flowLong = finite(item.futures_long_pct) ?? (flow !== null && flowStrength !== null ? Math.round(flow >= 0 ? flowStrength : 100 - flowStrength) : null);
        const flowShort = finite(item.futures_short_pct) ?? (flowLong === null ? null : 100 - flowLong);
        const change = finite(item.price_change_pct);
        return <article className="min-h-[82px] border-b border-border-subtle px-2 py-1.5" key={item.symbol || coin}>
          <div className="flex items-center gap-1">
            <span className="w-[18px] shrink-0 font-mono text-[8px] text-warn">{String(index + 1).padStart(2, "0")}</span>
            <CoinIcon coin={coin}/><b className="text-[9px]">${coin}</b>{item.asset_type ? <span className="rounded-[2px] bg-warn/10 px-1 text-[6px] text-warn">{item.asset_type}</span> : null}
            <span className={`${change !== null && change < 0 ? "text-risk" : "text-good"} font-mono text-[7px] font-semibold`}>{priceChange(item.price_change_pct)}</span>
            <span className={`rounded-[2px] px-1 text-[7px] ${neutral ? "bg-surface-low text-text-secondary" : bullish ? "bg-good/10 text-good" : "bg-risk/10 text-risk"}`}>{sentiment}</span>
            <span className="ml-auto text-[7px] text-text-muted">{countText(item.posts)} 帖</span>
          </div>
          <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[6px]">
            <span className="rounded-[2px] bg-surface-low px-1"><b className="font-semibold text-good">广场 多 {Math.round(Number(item.positive_pct || 0))}%</b><i className="mx-1 not-italic text-text-muted">·</i><b className="font-semibold text-risk">空 {Math.round(Number(item.negative_pct || 0))}%</b></span>
            <span className="rounded-[2px] bg-primary-50 px-1 text-primary-700">主力合约 <b className="text-good">多 {flowLong === null ? "—" : `${Math.round(flowLong)}%`}</b><i className="mx-1 not-italic text-text-muted">·</i><b className="text-risk">空 {flowShort === null ? "—" : `${Math.round(flowShort)}%`}</b></span>
          </div>
          <p className={`mt-1 line-clamp-2 text-[8px] leading-[1.4] ${neutral ? "text-text-secondary" : bullish ? "text-good" : "text-risk"}`}>{item.summary || `${coin} 广场讨论热度、方向和互动强度的聚合摘要。`}</p>
        </article>;
      })}
    </div>
  </section>;
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
    <section className="workstation-panel flex h-[44px] shrink-0 items-center gap-2.5 px-3 min-[1024px]:mx-1 min-[1024px]:h-[30px] min-[1024px]:px-0"><div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-[4px] bg-primary-50 text-primary-600 min-[1024px]:h-[30px] min-[1024px]:w-[30px] min-[1024px]:rounded-[2px]" data-testid="info-digest-icon">✦</div><div><div className="flex items-center gap-2"><h1 className="text-[12px] font-bold text-text-primary">AI 信息蒸馏</h1><span className="rounded-[3px] bg-warn/10 px-1.5 py-0.5 text-[7px] font-semibold text-warn">引擎 v2.4</span></div><p className="text-[7px] text-text-muted">全量聚合公开信息 · 实时增量更新 · <button className="underline decoration-dotted" onClick={() => setDisclaimer(true)} type="button">免责声明</button></p></div><div className="ml-auto flex items-center gap-2"><span className="sr-only">高影响 {high} · 更新 {clock(updatedAt)}</span><button aria-pressed={showDigest} className="h-7 rounded-full border border-warn/20 bg-warn/10 px-3 text-[8px] font-semibold text-warn min-[1024px]:h-6 min-[1024px]:px-7" disabled={loading} onClick={() => { setShowDigest((value) => !value); void load(true); }} type="button">{loading ? "分析中…" : "✦ 4h AI 综合分析 · •"}</button></div></section>
    {error ? <div className="border border-risk/20 bg-risk/5 px-3 py-1 text-[8px] text-risk">{error}</div> : null}
    <main className="grid min-h-0 grid-cols-4 gap-2.5" data-testid="info-four-columns"><InfoColumn loading={loading} mode="news" payload={feeds.news} showDigest={showDigest} title="聚合资讯"/><InfoColumn loading={loading} mode="english" payload={feeds.english} showDigest={showDigest} title="英文流资讯"/><InfoColumn loading={loading} mode="kol" payload={feeds.kol} showDigest={showDigest} title="KOL聚合资讯"/><PlazaColumn loading={loading} payload={feeds.plaza} showDigest={showDigest}/></main>
    {disclaimer ? <div className="fixed inset-0 z-50 grid place-items-center bg-black/25 p-4" role="dialog"><div className="w-full max-w-lg rounded-lg border border-border-subtle bg-surface-panel p-5 shadow-xl"><div className="flex items-center justify-between"><h2 className="text-sm font-bold text-text-primary">免责声明</h2><button aria-label="关闭" className="text-lg text-text-muted" onClick={() => setDisclaimer(false)} type="button">×</button></div><div className="mt-4 space-y-3 text-[11px] leading-6 text-text-secondary"><p>本页面聚合官方公告、公开 RSS 与公开社交 API 的必要元数据，版权归原作者及发布平台所有，每条信息保留原始来源链接。</p><p>重要度、币种关联和情绪方向由规则引擎生成，用于信息筛选，不构成投资、交易、财务或法律建议。</p></div></div></div> : null}
  </div>;
}
