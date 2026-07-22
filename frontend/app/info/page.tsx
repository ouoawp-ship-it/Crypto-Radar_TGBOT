"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { CoinIcon } from "@/components/CoinIcon";
import { getInfoFeed, getWorkstationInfoBriefs, getWorkstationInfoDashboard } from "@/lib/api";
import type { InfoBrief, InfoBriefsPayload, InfoDashboardPayload, InfoFeedPayload, InfoPlazaRank, NewsEvent } from "@/lib/types";

const CHANNELS = [
  { key: "news", title: "聚合资讯", query: { channel: "news" } },
  { key: "english", title: "英文流资讯", query: { channel: "en" } },
  { key: "kol", title: "KOL聚合资讯", query: { channel: "kol" } },
  { key: "plaza", title: "市场广场情绪", query: { channel: "plaza" } }
] as const;

type FeedMode = "news" | "english" | "kol";

function ChannelIcon({ mode }: { mode: FeedMode | "plaza" }) {
  const testId = `info-channel-icon-${mode}`;
  const colors = mode === "english" ? "bg-[#ece9f7] text-[#7a5fd0] min-[1280px]:before:bg-[#ece9f7]" : mode === "kol" ? "bg-[#f3e9d9] text-[#383838] min-[1280px]:before:bg-[#f3e9d9]" : mode === "plaza" ? "bg-[#f3ecdd] text-[#c39a33] min-[1280px]:before:bg-[#f3ecdd]" : "bg-[#e2eef1] text-[#2c8d9b] min-[1280px]:before:bg-[#e2eef1]";
  const icon = mode === "english" ? <svg className="relative h-4 w-4" viewBox="0 0 12 12"><path d="M3 1.8 10 6 3 10.2Z" fill="currentColor"/></svg>
    : mode === "kol" ? <svg className="relative h-4 w-4" fill="none" viewBox="0 0 14 14"><circle cx="5" cy="4.2" r="2" fill="currentColor"/><circle cx="9.5" cy="5" r="1.6" fill="currentColor" opacity=".72"/><path d="M1.8 11c.2-2.3 1.5-3.5 3.3-3.5S8.2 8.7 8.4 11H1.8Zm6.3 0c-.1-1.1-.5-2-1.1-2.7.6-.5 1.3-.7 2.1-.7 1.7 0 2.8 1.1 3 3.4h-4Z" fill="currentColor"/></svg>
      : mode === "plaza" ? <svg className="relative h-4 w-4" fill="none" viewBox="0 0 12 12"><path d="M2 2h8v8H2z" stroke="currentColor"/><path d="M4 4h4v4H4z" fill="currentColor" opacity=".48"/></svg>
        : <svg className="relative h-4 w-4" fill="none" viewBox="0 0 12 12"><path d="M3 1.5h6v9H3z" stroke="currentColor"/><path d="M4.5 4h3M4.5 6h3M4.5 8h2" stroke="currentColor" strokeLinecap="round"/></svg>;
  return <span aria-hidden="true" className={`relative grid h-[22px] w-[22px] shrink-0 place-items-center rounded-[3px] text-[14px] font-extrabold leading-none before:hidden min-[1280px]:h-6 min-[1280px]:w-6 min-[1280px]:bg-transparent min-[1280px]:before:absolute min-[1280px]:before:left-0 min-[1280px]:before:top-px min-[1280px]:before:block min-[1280px]:before:h-[22px] min-[1280px]:before:w-[22px] min-[1280px]:before:rounded-[3px] ${colors}`} data-testid={testId}>{icon}</span>;
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
  const calendar = (candidate: Date) => candidate.toLocaleDateString("zh-CN", { timeZone: "Asia/Shanghai" });
  const today = new Date(Date.now());
  if (calendar(date) === calendar(today)) return clock(value);
  const yesterday = new Date(today.getTime() - 86_400_000);
  const prefix = calendar(date) === calendar(yesterday) ? "昨" : date.toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit", timeZone: "Asia/Shanghai" });
  return `${prefix} ${clock(value)}`;
}

function isWithin(value: string | undefined, hours: number, referenceTime: number) {
  const date = dateOf(value);
  if (!date) return false;
  const age = referenceTime - date.getTime();
  return age >= 0 && age <= hours * 3_600_000;
}

function summaryText(item?: NewsEvent) {
  return item?.summary || item?.title || "暂无新增关键信息";
}

function channelDigest(payload?: InfoFeedPayload, brief?: InfoBrief) {
  if (brief?.summary && !brief.model_generated) return brief.summary;
  const items = payload?.items || [];
  const high = items.find((item) => item.importance === "high") || items[0];
  return summaryText(high);
}

function LiveBadge({ payload }: { payload?: InfoFeedPayload }) {
  const ready = (payload?.items || []).length > 0;
  return <span className={`inline-flex h-[21px] items-center gap-1.5 rounded-full px-3 text-[11px] font-bold min-[1280px]:h-[22px] ${ready ? "bg-[#def1ea] text-good" : "bg-warn/10 text-warn"}`}><span className={`h-1.5 w-1.5 rounded-full ${ready ? "animate-pulse bg-good" : "bg-warn"}`}/>{ready ? "LIVE" : "SYNC"}</span>;
}

function SymbolTags({ symbols }: { symbols?: string[] }) {
  if (!symbols?.length) return null;
  return <div className="mt-2 flex flex-wrap items-center gap-1.5">{symbols.slice(0, 4).map((symbol) => {
    const coin = symbol.replace(/USDT$/i, "");
    return <span className="inline-flex items-center gap-1 rounded-full border border-[#dfe2e6] bg-[#eef0f3] px-2 py-0.5 font-mono text-[11px] font-semibold text-text-secondary min-[1280px]:text-[10px]" key={symbol}><span aria-hidden="true" className="grid h-3 w-3 place-items-center text-[8px] font-extrabold text-text-primary">{coin.slice(0, 1)}</span>${coin}</span>;
  })}</div>;
}

function FeedItem({ item, mode }: { item: NewsEvent; mode: FeedMode }) {
  const title = item.title || item.summary || "未命名资讯";
  const streamSpacing = mode === "english"
    ? "pb-[11px] pl-[8px] pr-[7px] pt-[5px] min-[1280px]:pb-3 min-[1280px]:pl-[19px] min-[1280px]:pr-[13px] min-[1280px]:pt-2"
    : "pb-2.5 pl-[6px] pr-[10px] pt-1.5 min-[1280px]:pb-[13px] min-[1280px]:pl-[19px] min-[1280px]:pr-[13px] min-[1280px]:pt-[7px]";
  const content = mode === "news" ? <article className="border-b border-border-subtle pb-[7px] pl-2.5 pr-1.5 pt-[9px] transition-colors hover:bg-primary-50/45 min-[1280px]:px-3 min-[1280px]:py-[11px]" data-info-row="news">
    <div className="flex items-start gap-1.5"><h3 className="line-clamp-3 min-w-0 flex-1 text-[15px] font-normal leading-[1.65] text-text-primary"><span className="mr-1.5 text-[11px] text-text-muted">{item.source || "公开来源"}</span>{title}</h3>{item.importance === "high" ? <span className="shrink-0 rounded-[3px] bg-risk/10 px-1.5 py-0.5 text-[10px] font-semibold text-risk">高影响</span> : null}</div>
    <SymbolTags symbols={item.symbols}/>
  </article> : <article className={`min-w-0 border-border-subtle transition-colors hover:bg-primary-50/45 ${streamSpacing}`}>
    <div className="mb-1 flex items-center gap-1.5 text-[11px] text-text-muted"><span className={`min-w-0 flex-1 truncate ${mode === "english" ? "font-semibold text-[#7a5fd0]" : "font-semibold text-warn"}`}>{item.source || (mode === "kol" ? "@KOL" : "公开来源")}</span>{mode !== "kol" && item.importance === "high" ? <span className="rounded-[3px] bg-risk/10 px-1.5 py-0.5 font-semibold text-risk">高影响</span> : null}</div>
    <h3 className="line-clamp-4 text-[14px] font-normal leading-[1.6] text-text-primary">{title}</h3>
  </article>;
  const body = mode === "news" ? content : <div className={`grid grid-cols-[62px_minmax(0,1fr)] border-b border-border-subtle ${mode === "kol" ? "min-h-[60px] min-[1280px]:min-h-[66px]" : "min-h-[84px] min-[1280px]:min-h-[87px]"}`} data-info-row={mode}><span className="border-border-subtle py-3 text-center font-mono text-[11px] text-text-muted">{mode === "kol" ? relativeClock(item.published_at) : clock(item.published_at)}</span>{content}</div>;
  return item.url ? <a className="block" href={item.url} rel="noreferrer" target="_blank">{body}</a> : body;
}

function GroupedNews({ items }: { items: NewsEvent[] }) {
  const groups = new Map<string, NewsEvent[]>();
  items.forEach((item) => {
    const key = hourLabel(item.published_at);
    groups.set(key, [...(groups.get(key) || []), item]);
  });
  return <>{[...groups.entries()].map(([hour, rows]) => <div key={hour}><div className="flex h-[29px] items-center justify-between border-b border-border-subtle bg-surface-container px-3 text-[10px] text-text-muted"><span className="font-mono font-semibold">{hour}</span><span>{rows.length} 条</span></div>{rows.map((item, index) => <div className="grid grid-cols-[54px_minmax(0,1fr)] min-[1280px]:grid-cols-[62px_minmax(0,1fr)]" key={item.event_id || `${item.title}-${index}`}><span className="border-b border-border-subtle pb-[7px] pt-[9px] text-center font-mono text-[11px] text-text-muted min-[1280px]:pb-[11px] min-[1280px]:pt-[11px]">{clock(item.published_at)}</span><FeedItem item={item} mode="news"/></div>)}</div>)}</>;
}

function InfoColumn({ title, payload, brief, loading, showDigest, mode }: { title: string; payload?: InfoFeedPayload; brief?: InfoBrief; loading: boolean; showDigest: boolean; mode: FeedMode }) {
  const [query, setQuery] = useState("");
  const [expanded, setExpanded] = useState(false);
  const items = (payload?.items || []).filter((item) => !query || `${item.title || ""} ${item.summary || ""} ${(item.symbols || []).join(" ")} ${item.source || ""}`.toLowerCase().includes(query.toLowerCase()));
  const digestSurface = mode === "news" ? "bg-[#f3f8f9]" : mode === "english" ? "bg-[#f7f6fc]" : "bg-[#fcf8f1]";
  const digestBadge = mode === "news" ? "bg-[#e9f5f4] text-[#4a9994]" : mode === "english" ? "border border-[#e2dcf5] text-[#7a5fd0]" : "border border-[#f0dfc0] text-warn";
  const digestBadgeSpacing = mode === "news" ? "px-1.5 py-1" : "whitespace-nowrap px-[5px] py-0.5";
  const headerHeight = mode === "english" ? "h-[66px] min-[1280px]:h-[52px]" : mode === "kol" ? "h-[44px] min-[1280px]:h-[47px]" : "h-[47px] min-[1280px]:h-[52px]";
  const digestHeight = mode === "english" ? "h-[58px] min-[1280px]:h-[39px]" : "h-[59px] min-[1280px]:h-[39px]";
  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col rounded-[5px]">
    <div className={`${headerHeight} flex shrink-0 items-center gap-2 border-b border-border-subtle bg-surface-low px-3 min-[1280px]:gap-3 min-[1280px]:pl-3.5 min-[1280px]:pr-4`}><h2 className={`flex items-center gap-2 text-[15px] font-bold leading-tight text-text-primary ${mode === "english" ? "max-w-[96px] whitespace-normal min-[1280px]:max-w-none min-[1280px]:whitespace-nowrap" : "whitespace-nowrap"}`}><ChannelIcon mode={mode}/>{title}</h2>{mode !== "kol" ? <div className="relative ml-auto w-[145px] shrink-0 min-[1280px]:w-[147px]"><svg aria-hidden="true" className="pointer-events-none absolute left-2.5 top-1/2 h-[14px] w-[14px] -translate-y-1/2 text-text-muted" fill="none" viewBox="0 0 16 16"><circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.5"/><path d="m10.5 10.5 3 3" stroke="currentColor" strokeLinecap="round" strokeWidth="1.5"/></svg><input aria-label={`搜索${title}`} className="h-[27px] w-full rounded-[5px] border border-[#cbd2dc] bg-surface-container pl-8 pr-2 text-[12px] text-text-primary outline-none placeholder:text-text-muted focus:border-primary-500" onChange={(event) => setQuery(event.target.value)} placeholder="搜索…" value={query}/></div> : <span className="ml-auto"/>}<LiveBadge payload={payload}/></div>
    {showDigest ? <button aria-expanded={expanded} className={`${expanded ? "min-h-[96px]" : digestHeight} ${digestSurface} grid shrink-0 grid-cols-[48px_minmax(0,1fr)_auto] items-start gap-2 border-b border-border-subtle px-3 py-3 text-left min-[1280px]:grid-cols-[44px_minmax(0,1fr)_auto] min-[1280px]:gap-3 min-[1280px]:px-3.5 min-[1280px]:py-2`} onClick={() => setExpanded((value) => !value)} title="规则聚合摘要" type="button"><span className={`${digestBadge} ${digestBadgeSpacing} rounded-[3px] text-center text-[10px] font-semibold`}>规则摘要</span><span className={`${expanded ? "" : "line-clamp-2"} text-[12px] leading-[1.55] text-text-secondary`}>{channelDigest(payload, brief)}</span><span className="text-[10px] font-semibold text-warn">{expanded ? "收起" : "展开⌄"}</span></button> : null}
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

function PlazaColumn({ payload, brief, loading, showDigest }: { payload?: InfoFeedPayload; brief?: InfoBrief; loading: boolean; showDigest: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const items = payload?.items || [];
  const active = payload?.plaza_rankings?.active_4h || plazaRanks(items, 4);
  const total = payload?.plaza_rankings?.total_24h || plazaRanks(items, 24);
  const providerLabel = payload?.plaza_rankings?.provider?.label || "市场广场";
  return <section className="workstation-panel flex min-h-0 min-w-0 flex-col rounded-[5px]">
    <div className="flex h-[44px] shrink-0 items-center justify-between border-b border-border-subtle bg-surface-low px-3 min-[1280px]:h-[47px] min-[1280px]:pl-3.5 min-[1280px]:pr-4">
      <h2 className="flex items-center gap-2 text-[15px] font-bold text-text-primary"><ChannelIcon mode="plaza"/>{providerLabel}情绪</h2>
      <LiveBadge payload={payload}/>
    </div>
    {showDigest ? <button aria-expanded={expanded} className={`${expanded ? "min-h-[96px]" : "h-[38px] min-[1280px]:h-[39px]"} flex shrink-0 items-start gap-2 border-b border-border-subtle bg-[#fbf8f1] px-3 py-2 text-left min-[1280px]:px-3.5`} data-testid="info-plaza-digest" onClick={() => setExpanded((value) => !value)} title="规则聚合摘要" type="button">
      <span className="whitespace-nowrap rounded-[3px] border border-[#f0dfc0] px-[5px] py-0.5 text-[10px] font-semibold text-warn">规则摘要</span>
      <span className={`${expanded ? "" : "line-clamp-1"} flex-1 text-[12px] leading-[1.55] text-text-secondary`}>{channelDigest(payload, brief)}</span>
      <span className="text-[10px] font-semibold text-warn">{expanded ? "收起" : "展开⌄"}</span>
    </button> : null}
    <div className="workstation-scroll min-h-0 flex-1 overflow-auto">
      <div className="flex h-[40px] items-center justify-between border-b border-border-subtle bg-surface-container px-[14px] min-[1280px]:px-[18px]">
        <h3 className="flex items-center gap-1 text-[13px] font-bold"><svg aria-hidden="true" className="h-[14px] w-[14px]" fill="none" viewBox="0 0 14 14"><rect height="12" rx="0.5" stroke="#7f8a9a" width="12" x="1" y="1"/><path d="m2.5 10 3-3 2 1.5 4-5" stroke="#ef3f46" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1"/></svg><span>4h 活力榜</span></h3>
        <span className="text-[10px] text-text-muted">快速冒头 · Top {Math.min(8, active.length)}</span>
      </div>
      {active.slice(0, 8).map((item, index) => {
        const coin = rankCoin(item);
        const ratio = finite(item.recent_ratio);
        const badge = item.is_new ? "NEW" : ratio === null ? "—" : `↑${ratio.toFixed(1)}×`;
        const textIcon = ["XAU", "XAG", "MSTR", "BZ"].includes(coin) ? coin.slice(0, 1) : "";
        const negative = item.sentiment === "bearish";
        return <div className="grid h-[50.4px] grid-cols-[24px_27px_minmax(0,1fr)_auto] items-center gap-2 border-b border-border-subtle px-4 py-1.5 text-[11px] min-[1280px]:h-[54px] min-[1280px]:px-5" key={item.symbol || coin}>
          <span className={`font-mono ${index === 0 ? "text-warn" : "text-text-muted"}`}>{String(index + 1).padStart(2, "0")}</span>
          {textIcon ? <span className="grid h-[22px] w-[22px] place-items-center text-[11px] font-semibold text-text-primary">{textIcon}</span> : <CoinIcon coin={coin} size={22}/>}
          <span className="min-w-0 text-[12px] font-semibold">${coin} {item.asset_type ? <small className="rounded-[2px] border border-warn/30 bg-warn/5 px-1 text-[9px] text-warn">{item.asset_type}</small> : null} <small className={`${item.is_new ? "border border-good/30 bg-good/5 px-0.5 text-good" : "text-warn"} rounded-[2px] font-mono text-[9px] font-semibold`}>{badge}</small><small className="block truncate text-[10px] font-normal leading-[1.45] text-text-muted">近 1h 提到 {countText(item.recent_1h_posts)} 次{item.is_new ? "" : ` · 上轮 ${countText(item.previous_1h_posts)}`}</small></span>
          <span className="flex flex-col items-end whitespace-nowrap font-mono"><small className="text-[9px] font-normal text-text-muted">4h 共</small><b className={`text-[14px] ${negative ? "text-risk" : "text-good"}`}>{countText(item.posts)} 帖</b></span>
        </div>;
      })}
      {!loading && !active.length ? <div className="grid h-20 place-items-center text-[8px] text-text-muted">广场数据源暂无 4h 币种样本</div> : null}
      <div className="-mt-0.5 flex h-[39px] items-center justify-between border-y border-border-subtle bg-surface-container px-[14px] min-[1280px]:mt-0 min-[1280px]:px-[18px]">
        <h3 className="text-[13px] font-bold">⊕ 24h 总榜</h3>
        <span className="text-[10px] text-text-muted">情绪分析</span>
      </div>
      {total.slice(0, 8).map((item, index) => {
        const coin = rankCoin(item);
        const bullish = item.sentiment === "bullish" || Number(item.positive_pct || 0) > Number(item.negative_pct || 0);
        const neutral = item.sentiment === "neutral";
        const sentiment = sentimentLabel(item);
        const flowLong = finite(item.futures_long_pct);
        const flowShort = finite(item.futures_short_pct);
        const change = finite(item.price_change_pct);
        return <article className={`${index === 0 ? "bg-[#faf6ee]" : ""} min-h-[129px] border-b border-border-subtle px-3 py-2.5 min-[1280px]:min-h-[130px]`} key={item.symbol || coin}>
          <div className="flex items-center gap-1">
            <span className="w-[22px] shrink-0 font-mono text-[10px] text-warn">{String(index + 1).padStart(2, "0")}</span>
            <CoinIcon coin={coin} size={22}/><b className="text-[13px]">${coin}</b>{item.asset_type ? <span className="rounded-[2px] bg-warn/10 px-1 text-[9px] text-warn">{item.asset_type}</span> : null}
            <span className={`${change !== null && change < 0 ? "text-risk" : "text-good"} font-mono text-[10px] font-semibold`}>{priceChange(item.price_change_pct)}</span>
            <span className={`rounded-[2px] px-1 text-[9px] ${neutral ? "bg-surface-low text-text-secondary" : bullish ? "bg-good/10 text-good" : "bg-risk/10 text-risk"}`}>{sentiment}</span>
            <span className="ml-auto text-[9px] text-text-muted">{countText(item.posts)} 帖</span>
          </div>
          <div className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-0.5 text-[10px]">
            <span className="rounded-[2px] bg-surface-low px-1"><b className="font-semibold text-good">广场 多 {Math.round(Number(item.positive_pct || 0))}%</b><i className="mx-1 not-italic text-text-muted">·</i><b className="font-semibold text-risk">空 {Math.round(Number(item.negative_pct || 0))}%</b></span>
            <span className="rounded-[2px] bg-primary-50 px-1 text-primary-700">主力合约 <b className="text-good">多 {flowLong === null ? "—" : `${Math.round(flowLong)}%`}</b><i className="mx-1 not-italic text-text-muted">·</i><b className="text-risk">空 {flowShort === null ? "—" : `${Math.round(flowShort)}%`}</b></span>
          </div>
          <p className={`mt-1.5 line-clamp-2 text-[11px] leading-[1.45] ${index === 0 || neutral ? "text-text-secondary" : bullish ? "text-good" : "text-risk"}`}>{item.summary || `${coin} 广场讨论热度、方向和互动强度的聚合摘要。`}</p>
        </article>;
      })}
    </div>
  </section>;
}

export default function InfoPage() {
  const [feeds, setFeeds] = useState<Record<string, InfoFeedPayload>>({});
  const [dashboard, setDashboard] = useState<InfoDashboardPayload>({});
  const [briefs, setBriefs] = useState<InfoBriefsPayload>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [updatedAt, setUpdatedAt] = useState("");
  const [disclaimer, setDisclaimer] = useState(false);
  const [showDigest, setShowDigest] = useState(true);

  const load = useCallback(async (bypassCache = false) => {
    setLoading(true); setError("");
    const [results, meta] = await Promise.all([
      Promise.allSettled(CHANNELS.map(async (channel) => [channel.key, await getInfoFeed({ ...channel.query, page: 1, page_size: 80, window_sec: 2_592_000 }, { bypassCache })] as const)),
      Promise.allSettled([getWorkstationInfoDashboard(2_592_000, { bypassCache }), getWorkstationInfoBriefs(14_400, { bypassCache })])
    ]);
    const next: Record<string, InfoFeedPayload> = {};
    const failures: string[] = [];
    results.forEach((result, index) => { if (result.status === "fulfilled") next[result.value[0]] = result.value[1]; else failures.push(CHANNELS[index].title); });
    if (meta[0].status === "fulfilled") setDashboard(meta[0].value); else failures.push("信息总览");
    if (meta[1].status === "fulfilled") setBriefs(meta[1].value); else failures.push("信息摘要");
    if (Object.keys(next).length) { setFeeds((current) => ({ ...current, ...next })); setUpdatedAt(meta[0].status === "fulfilled" ? meta[0].value.generated_at || "" : Object.values(next).find((item) => item.generated_at)?.generated_at || ""); }
    setError(failures.length ? `${failures.join("、")}加载失败，其余栏目仍可用` : "");
    setLoading(false);
  }, []);

  useEffect(() => { void load(); }, [load]);
  useEffect(() => { const timer = window.setInterval(() => void load(true), 60_000); return () => window.clearInterval(timer); }, [load]);

  const combined = useMemo(() => Object.values(feeds).flatMap((feed) => feed.items || []), [feeds]);
  const briefByChannel = useMemo(() => Object.fromEntries((briefs.items || []).map((item) => [String(item.channel || ""), item])), [briefs]);
  const high = Number(dashboard.summary?.high_importance ?? combined.filter((item) => item.importance === "high").length);

  return <div aria-busy={loading} className="workstation-page mercu-info-grid" data-testid="info-workstation">
    <section className="info-command-bar workstation-panel -mx-2 flex h-[67px] shrink-0 items-center gap-3.5 rounded-none border-x-0 border-t-0 px-[17px] max-[767px]:mx-0 min-[1280px]:-mx-[14px] min-[1280px]:h-[71px] min-[1280px]:px-[23px]"><div className="info-command-icon flex h-[46px] w-[46px] shrink-0 items-center justify-center rounded-[6px] bg-surface-container" data-testid="info-digest-icon"><svg aria-hidden="true" className="h-6 w-6" fill="none" viewBox="0 0 24 24"><path d="M13 2.5c.45 4.08 2.42 6.07 6.5 6.5-4.08.45-6.05 2.42-6.5 6.5-.45-4.08-2.42-6.05-6.5-6.5 4.08-.43 6.05-2.42 6.5-6.5Z" fill="#c28a0f"/><circle cx="5.1" cy="15.7" r="1.45" fill="#6079b8"/><circle cx="18.7" cy="4.3" r="1.15" fill="#d6a72e"/></svg></div><div className="info-command-copy min-w-0"><div className="flex items-center gap-[5px]"><h1 aria-label="信息聚合" className="whitespace-nowrap text-[18px] font-bold text-text-primary">信息聚合</h1><span className="whitespace-nowrap rounded-[3px] border border-[#e5d2a5] bg-white px-2 py-0.5 text-[10px] font-semibold text-[#b8860b]">规则 v2.4</span></div><div className="flex items-center text-[12px] text-text-muted"><p className="truncate">全量聚合全网信息 · 实时增量更新</p><button className="info-disclaimer-link ml-5 shrink-0 underline decoration-dotted" onClick={() => setDisclaimer(true)} type="button">免责声明</button></div></div><div className="info-command-actions ml-auto flex items-center gap-2"><span className="sr-only">高影响 {high} · 更新 {clock(updatedAt)}</span><button aria-pressed={showDigest} className="info-digest-toggle flex h-[38px] w-[197px] items-center justify-center gap-1.5 rounded-full border border-[#e1cb96] bg-[#f6efdf] px-3 text-[13px] font-semibold text-[#b8860b] min-[1280px]:h-[40px] min-[1280px]:w-[201px]" disabled={loading} onClick={() => { setShowDigest((value) => !value); void load(true); }} type="button">{loading ? "更新中…" : <><svg aria-hidden="true" className="h-4 w-4" fill="none" viewBox="0 0 16 16"><path d="M8 1.5c.25 3.2 1.8 4.75 5 5-3.2.25-4.75 1.8-5 5-.25-3.2-1.8-4.75-5-5 3.2-.25 4.75-1.8 5-5Z" fill="currentColor"/><path d="m12.2 11.2 1.3 1.3 1.3-1.3" stroke="currentColor" strokeLinecap="round" strokeLinejoin="round"/></svg><span>4h 规则摘要</span></>}</button></div></section>
    {error ? <div className="border border-risk/20 bg-risk/5 px-3 py-1 text-[8px] text-risk">{error}</div> : null}
    <main className="grid min-h-0 grid-cols-4 gap-2 min-[1280px]:gap-[14px]" data-testid="info-four-columns"><InfoColumn brief={briefByChannel.news} loading={loading} mode="news" payload={feeds.news} showDigest={showDigest} title="聚合资讯"/><InfoColumn brief={briefByChannel.en} loading={loading} mode="english" payload={feeds.english} showDigest={showDigest} title="英文流资讯"/><InfoColumn brief={briefByChannel.kol} loading={loading} mode="kol" payload={feeds.kol} showDigest={showDigest} title="KOL聚合资讯"/><PlazaColumn brief={briefByChannel.plaza} loading={loading} payload={feeds.plaza} showDigest={showDigest}/></main>
    {disclaimer ? <div className="fixed inset-0 z-50 grid place-items-center bg-black/25 p-4" role="dialog"><div className="w-full max-w-lg rounded-lg border border-border-subtle bg-surface-panel p-5 shadow-xl"><div className="flex items-center justify-between"><h2 className="text-sm font-bold text-text-primary">免责声明</h2><button aria-label="关闭" className="text-lg text-text-muted" onClick={() => setDisclaimer(false)} type="button">×</button></div><div className="mt-4 space-y-3 text-[11px] leading-6 text-text-secondary"><p>本页面聚合官方公告、公开 RSS 与公开社交 API 的必要元数据，版权归原作者及发布平台所有，每条信息保留原始来源链接。</p><p>重要度、币种关联和情绪方向由规则引擎生成，用于信息筛选，不构成投资、交易、财务或法律建议。</p></div></div></div> : null}
  </div>;
}
